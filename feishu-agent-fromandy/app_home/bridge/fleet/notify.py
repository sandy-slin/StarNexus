"""Claude 会话进入「等你」时，主动往主人单聊推一张卡（第 7 轮）。

## 为什么

外面最常被抱怨的一条：**它在等我，手机却不响**（CC #29438 👍59、#28765 👍44）。
总览里已经把「等你」排第一了，但要主人先想起来去点「会话」。

## 这里只做判断，不发消息

`WaitingNotifier.due(view)` 吃一次总控扫描（`FleetControl.overview()` 的结果），吐出
**这一拍该推的会话**；发不发、发到哪由 `app.py` 接线（`Bridge._notify_tick`）。
判断和发送分开，是为了能用假时钟把 20 秒 / 10 分钟 / 1 分钟 3 张测死，不用真睡。

## 规则（brief-r7.md）

1. 只推「刚进入」等你：同一次 waiting 只推一次；回到别的状态再进 waiting 才算新的一次
2. 持续 ≥ 20 秒才推：Claude 经常短暂进 waiting 又马上继续，秒级的不推
3. 人在电脑前（键鼠空闲 < 120 秒）不推；读不出来 ⇒ 当人不在、照推（漏推比多推一次更糟）
4. 一个会话 10 分钟最多一次；全部会话加起来 1 分钟最多 3 张
5. 只做 Claude：Codex / Cursor 没有等价的「在等人」信号，不猜。桥自己起的那一轮也不推
   （它有自己的审批卡）
6. 开关在 config.toml：`[notify]` 下 `waiting = false` 就一张都不推

卡片上**不放会话的消息内容**（隐私 + 卡片大小），只有名字、项目和一个「看看它」按钮。
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

from .cards import _card, _escape, _md, _row, project_of, session_label
from .actions import fleet_button

logger = logging.getLogger("feishu-bridge.fleet")

MIN_WAIT_SEC = 20  # 持续这么久才推
PER_SESSION_SEC = 10 * 60  # 同一个会话两次推送至少隔这么久
BURST_LIMIT = 3  # 全部会话加起来，BURST_WINDOW_SEC 内最多推几张
BURST_WINDOW_SEC = 60
PRESENT_IDLE_SEC = 120  # 键鼠空闲少于这么多秒 ⇒ 人在电脑前
IDLE_PROBE_TIMEOUT_SEC = 2

TOOL = "claude"

# `ioreg -c IOHIDSystem` 里 `"HIDIdleTime" = <纳秒>`。
# 主会话 2026-11-06 实测：人在手机上时连读 3 次 7977.4 / 7978.5 / 7979.6 秒（每秒 +1）；
# 刚动过鼠标时 0.02 秒。`-r -d 1` 只打这一个节点（4KB），不加是整棵子树（330KB），值一样。
_IOREG_ARGV = ["/usr/sbin/ioreg", "-r", "-c", "IOHIDSystem", "-d", "1"]
_HID_IDLE_RE = re.compile(r'"HIDIdleTime"\s*=\s*(\d+)')


def parse_hid_idle(text: str) -> Optional[float]:
    """ioreg 输出 → 空闲秒数；没有那一行 ⇒ None。"""
    match = _HID_IDLE_RE.search(text or "")
    if not match:
        return None
    return int(match.group(1)) / 1e9


def human_idle_seconds(run: Callable = subprocess.run, platform: str = "") -> Optional[float]:
    """键盘鼠标多久没动（秒）。读不出来一律 None —— 调用方把 None 当「人不在」。

    - 只有 macOS 读（`ioreg`，argv 列表、不走 shell、2 秒超时）
    - 🔴 Windows 先不做：等价物是 `user32.GetLastInputInfo`，要 ctypes，留给以后。
      现在一律返回 None ⇒ 照推（宁可人在电脑前时多响一次，不要人不在时不响）
    - Linux 等其他平台同样 None
    """
    platform = platform or sys.platform
    if not platform.startswith("darwin"):
        return None
    try:
        proc = run(_IOREG_ARGV, capture_output=True, text=True, timeout=IDLE_PROBE_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return parse_hid_idle(getattr(proc, "stdout", "") or "")


class WaitingNotifier:
    """吃总控扫描结果、吐这一拍该推的会话。线程不安全 —— 只在一条后台线程里用。

    `due()` 返回的会话**已经记作「推过一次尝试」**（占掉每会话 10 分钟和每分钟 3 张的名额），
    调用方发完再 `record(key, ok)`：ok ⇒ 这一次等待算推过了，不再推；没送达 ⇒ 等每会话的冷却过了再试
    （不会每 10 秒重打一次飞书）。
    """

    def __init__(self, *, enabled: bool = True, now: Callable[[], float] = None,
                 idle: Callable[[], Optional[float]] = human_idle_seconds,
                 min_wait: float = MIN_WAIT_SEC, per_session: float = PER_SESSION_SEC,
                 burst: int = BURST_LIMIT, burst_window: float = BURST_WINDOW_SEC,
                 present_idle: float = PRESENT_IDLE_SEC) -> None:
        self.enabled = bool(enabled)
        self._now = now or time.monotonic
        self._idle = idle
        self.min_wait = float(min_wait)
        self.per_session = float(per_session)
        self.burst = int(burst)
        self.burst_window = float(burst_window)
        self.present_idle = float(present_idle)
        self._since: dict = {}  # key → 这一次 waiting 第一次被看到的时刻
        self._done: set = set()  # 这一次 waiting 已经推到了的 key
        self._last_try: dict = {}  # key → 上一次尝试推的时刻（限流用，跨 waiting 段）
        self._recent: deque = deque()  # 最近的尝试时刻（全局限流）

    def _human_present(self) -> bool:
        try:
            idle = self._idle() if self._idle is not None else None
        except Exception:  # noqa: BLE001 —— 读不出来 ⇒ 当人不在
            logger.debug("读键鼠空闲时间失败", exc_info=True)
            return False
        if idle is None:
            return False
        try:
            return float(idle) < self.present_idle
        except (TypeError, ValueError):
            return False

    def due(self, view: Any) -> list:
        if not self.enabled:
            return []
        now = self._now()
        sessions = list(getattr(view, "sessions", None) or [])
        ok_tools = set(getattr(view, "ok_tools", None) or ())
        claude_read = TOOL in ok_tools or any(getattr(s, "tool", "") == TOOL for s in sessions)
        if not claude_read:
            # 这次 Claude 没读到（超时 / 上一次还没读完）≠ 会话不等了。什么都不动：
            # 清掉计时会把一次长等待切成两段 ⇒ 多推一次。
            return []
        waiting = {
            s.key: s for s in sessions
            if s.tool == TOOL and s.status == "waiting" and s.owner != "bridge"
        }
        for key in list(self._since):
            if key not in waiting:
                # 这一次等待结束了（回去干活 / 闲着 / 进程没了）⇒ 下一次 waiting 是新的一次
                self._since.pop(key, None)
                self._done.discard(key)
        for key in waiting:
            self._since.setdefault(key, now)
        for key, at in list(self._last_try.items()):
            if now - at >= self.per_session:
                del self._last_try[key]
        while self._recent and now - self._recent[0] >= self.burst_window:
            self._recent.popleft()

        candidates = [
            sess for key, sess in waiting.items()
            if key not in self._done
            and now - self._since[key] >= self.min_wait
            and key not in self._last_try
        ]
        room = self.burst - len(self._recent)
        if not candidates or room <= 0:
            return []
        # 键鼠探针只在真有要推的时候跑一次
        if self._human_present():
            return []
        candidates.sort(key=lambda s: self._since[s.key])  # 等得最久的先
        picked = candidates[:room]
        for sess in picked:
            self._last_try[sess.key] = now
            self._recent.append(now)
        return picked

    def record(self, key: tuple, ok: bool) -> None:
        """发完回报一声。ok ⇒ 这一次等待算推到了。"""
        if ok and key in self._since:
            self._done.add(key)


def build_waiting_card(sess: Any, redact: Optional[Callable[[str], str]] = None) -> str:
    """「🟡 Claude「名」在等你」+ 项目一行 + 「👀 看看它」。不放任何消息内容。"""
    clean = redact or (lambda s: s)
    label = session_label(sess, clean)
    project = project_of(getattr(sess, "cwd", "") or "")
    try:
        project = clean(project)
    except Exception:  # noqa: BLE001 —— 脱敏失败不能让卡出不来
        project = ""
    tool = getattr(sess, "tool", TOOL)
    sid = str(getattr(sess, "id", "") or "")
    elements = [
        _md(f"📁 {_escape(project) or '不知道哪个项目'}"),
        _row([fleet_button("open", tool, sid, "👀 看看它", primary=True)]),
    ]
    return _card(f"🟡 {label}在等你", "yellow", elements)


def waiting_fallback_text(sess: Any, redact: Optional[Callable[[str], str]] = None) -> str:
    """卡片发不出去时的文字版。"""
    clean = redact or (lambda s: s)
    label = session_label(sess, clean)
    project = project_of(getattr(sess, "cwd", "") or "")
    try:
        project = clean(project)
    except Exception:  # noqa: BLE001
        project = ""
    return f"🟡 {label}在等你\n📁 {project or '不知道哪个项目'}\n发「会话」就能看到它。"


# ---------- 后台扫描和主人点「会话」共用一次扫描（第 11 轮，复审第 6 条） ----------
#
# 🔴 后台这一拍（`Bridge._notify_tick`，每 10 秒）和主人点「会话」原来各扫各的，而 `Fleet` 对每一家
# 只许同时有一次扫描（`Fleet._busy`，防慢的那家越堆越多）。两边撞上 ⇒ 后到的那次把这一家「跳过」⇒
# 总览卡写「电脑上现在没有开着的会话 / 一个会话都没读到」，而实际有一个在等你（复审实测 20 次撞 0~2 次）。
#
# 选的修法：**撞上就等那一次扫完、用同一份结果**（不是拿缓存）。理由：
# - 扫描本身有上限（`Fleet.timeout`，默认 1.2 秒），等它最多就是这么久，实测 50~270ms；
# - 拿到的是**这一刻**的真实结果，不用在卡上标「几秒前的」，后台推送那边也照样拿原样的扫描；
# - 用缓存的话，缓存是 ≤10 秒前的，状态可能已经变了（刚从「等你」回去干活），卡上要么撒小谎、要么多一句解释。
# 真「慢」（超时 / 上一次还卡着）不是撞车，等也等不来 ⇒ 交给下面的 `fill_unread`：那一家不许说「没有」。


class _ScanJob:
    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result = None
        self.error: Optional[BaseException] = None


class SharedScan:
    """包一层 `FleetControl`：同一时刻只跑一次 `overview()`，后到的等它扫完、拿同一份结果。

    - 不缓存：扫完就放掉，下一次调用重新扫
    - 扫描抛异常 ⇒ 这一次的所有调用方都拿到这个异常（和直接调一样），下一次照常扫
    - 等太久（那一次超出预算还没回来，几乎不会）⇒ 自己再扫一次，不无限等
    - 每家「按时读完」的时刻记在 `read_at`（给 `fill_unread` 说「几秒前的样子」）
    """

    def __init__(self, control: Any, wait_cap: Optional[float] = None) -> None:
        self.control = control
        timeout = getattr(getattr(control, "fleet", None), "timeout", None)
        try:
            default_cap = float(timeout) + 1.0 if timeout is not None else 3.0
        except (TypeError, ValueError):
            default_cap = 3.0
        self.wait_cap = float(wait_cap) if wait_cap is not None else default_cap
        self.read_at: dict = {}  # tool → 最近一次按时读完的 time.time()
        self._lock = threading.Lock()
        self._job: Optional[_ScanJob] = None

    def overview(self) -> Any:
        with self._lock:
            job = self._job
            mine = job is None
            if mine:
                job = self._job = _ScanJob()
        if not mine:
            if job.done.wait(self.wait_cap):
                if job.error is not None:
                    raise job.error
                return job.result
            logger.info("共用扫描：上一次超出预算还没回来，这次自己扫")
            return self.control.overview()
        try:
            view = self.control.overview()
            job.result = view
            at = getattr(view, "at", 0.0) or time.time()
            for tool in getattr(view, "ok_tools", None) or ():
                self.read_at[tool] = at
            return view
        except BaseException as exc:  # noqa: BLE001 —— 原样交给每个调用方
            job.error = exc
            raise
        finally:
            with self._lock:
                if self._job is job:
                    self._job = None
            job.done.set()


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    return f"{seconds // 3600} 小时"


def unread_tools(view: Any, control: Any) -> list:
    """这次总览里**没按时读完**的那几家（超时 / 上一次还卡着 / 出错）。"""
    adapters = getattr(getattr(control, "fleet", None), "adapters", None) or {}
    ok = set(getattr(view, "ok_tools", None) or ())
    return [tool for tool in adapters if tool not in ok]


def fill_unread(view: Any, control: Any, read_at: Optional[dict] = None,
                now: Optional[float] = None) -> Any:
    """给**主人看的**总览补上没读到的那几家：上次读到过 ⇒ 列出上次那几行并注明是之前的；没读到过 ⇒ 说不知道。

    🔴 底线：总览永远不能在「有会话」的时候说「没有」。某一家这次没读到，「没有」就是不知道，不是零。
    只给前台用 —— 后台推送要的是原样的扫描（掺了缓存会把「这次没读到」当成「还在等」）。
    返回一份新的 `Overview`，原来那份不动（它可能正被后台那一拍同时用着）。
    """
    import dataclasses

    from .scan import TOOL_LABELS, _sort_key

    missing = unread_tools(view, control)
    if not missing:
        return view
    now = time.time() if now is None else now
    read_at = read_at or {}
    try:
        known = control.known_keys()
    except Exception:  # noqa: BLE001 —— 读不出缓存就当没有缓存
        known = set()
    have = {s.key for s in getattr(view, "sessions", None) or ()}
    sessions = list(getattr(view, "sessions", None) or ())
    notes = list(getattr(view, "notes", None) or ())
    for tool in missing:
        label = TOOL_LABELS.get(tool, tool)
        # 扫描那边「上一次还没读完 / 读得太慢」那句和下面这句说的是同一件事 ⇒ 留一句（下面这句带下一步）
        notes = [n for n in notes if not (str(n).startswith(f"{label} ")
                                          and ("上一次还没读完" in str(n) or "读得太慢" in str(n)))]
        cached = []
        for key in sorted(k for k in known if k[0] == tool and k not in have):
            try:
                sess = control.cached(*key)
            except Exception:  # noqa: BLE001
                sess = None
            if sess is not None:
                cached.append(sess)
        at = read_at.get(tool)
        if cached:
            sessions.extend(cached)
            when = f"{_ago(now - at)}前的样子" if at else "上次的样子"
            notes.append(f"{label} 这次没读出来，列的是之前读到的（{when}），点进去会重新看")
        else:
            notes.append(f"{label} 这次没读出来，不知道它那边有没有开着的会话，过几秒点「刷新」再看")
    sessions.sort(key=_sort_key)
    return dataclasses.replace(view, sessions=sessions, notes=notes)
