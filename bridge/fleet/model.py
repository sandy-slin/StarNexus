"""会话总控的公共底座：数据结构、进程表、「哪些进程是桥自己起的」登记簿。

## 为什么要有这个文件

三家（Claude / Codex / Cursor）各写一个 adapter，但「这个 pid 还是不是当初那个进程」
「这个进程是不是我们起的」这两件事**只能有一份实现**。各写各的，迟早有一家漏掉
pid 复用的判断，然后给一个毫不相干的新进程发了 SIGINT。

## 边界

- **只用标准库**，所有子进程都带 `timeout=`。
- **读失败不抛**：ps 跑不出来就得到一张空表 + `error` 一句话，上层照样能出卡片。
- **停进程只走 `FleetJobs.signal()`**：没登记的、登记了但 lstart 变了的（pid 被复用），
  一律不发信号。这是安全红线第一条的唯一闸门。
- 文案是给手机上的人看的大白话，不是给开发看的报错。
"""

from __future__ import annotations

import calendar
import datetime as _dt
import logging
import os
import signal as _signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

logger = logging.getLogger("feishu-bridge.fleet")

TOOLS = ("claude", "codex", "cursor", "opencode")

# 每个工具声明自己支持什么（产品定义 §5.9「能力声明」）。菜单、卡片、新建会话按这张表显示或隐藏，
# 不再到处写 `tool in ("claude", "codex")`。新加工具：写一个 adapter + 在这里加一行。
CAPS = {
    "claude": frozenset({"start", "send", "stop", "fork", "model", "effort", "approval", "progress", "choices",
                         "steer"}),
    "codex": frozenset({"start", "send", "stop", "fork", "archive", "model", "effort", "approval", "progress",
                        "choices"}),
    "opencode": frozenset({"start", "send", "stop", "fork", "model", "choices", "steer"}),
    "cursor": frozenset({"send"}),
}


def can(tool: str, capability: str) -> bool:
    return capability in CAPS.get(str(tool or ""), frozenset())
SURFACES = ("cli", "app", "bridge")
STATUSES = ("running", "idle", "waiting", "stopped", "unknown")
OWNERS = ("none", "terminal", "app", "bridge", "unknown")
LAST_TURNS = ("done", "failed")  # `Session.last_turn` 只认这两个，别的一律当读不到

# 默认原因：adapter 没说清楚的时候兜底用，总比卡片上一片空白强
DEFAULT_WHY_NOT_SEND = "这个会话现在不能从手机上发消息。"
# `why_not_relay` **不兜底**：relay 这条路只对一小部分会话存在，给每个会话都安一句
# 「不能接着聊」等于在卡片上到处贴看不懂的拒绝。没有就留空，卡片不显示。
DEFAULT_WHY_NOT_STOP = "这个会话不是从手机上开的，不能从这里停。"

PS_TIMEOUT_SEC = 3.0


# ---------- 报错：给手机一句人话，原文只进日志 ----------

# 2026-11 第 9 轮复审实测：异常原文直接进了 `ActionResult.text` / 总览 notes ⇒
# 手机上看到 `[Errno 2] No such file or directory: '/Users/<真名>/.claude/…'`。
# 本机路径带着同事真名、会跟着截图转出去；英文异常名用户也看不懂。
# 所以 `bridge/fleet/` 里 `except … as exc` 之后**给用户的文字一律走 `friendly_error`**，
# 原文（类型 + 内容 + 调用栈）由它记进日志 —— 两边别弄反（tests/test_r11_errors.py 盯着）。
ERR_TIMEOUT = "等太久没反应，过一会儿再试一次。"
ERR_NOT_FOUND = "电脑上找不到要用的文件或程序（可能删掉或挪走了），刷新一下再试。"
ERR_PERMISSION = "被电脑上的系统权限挡住了，到电脑上看一眼再试一次。"
ERR_NETWORK = "连接断了，过一会儿再试一次。"
ERR_BUSY_DB = "它的记录这会儿被别的程序占着、读不了，过一会儿再试一次。"
ERR_OTHER = "出了点意外，过一会儿再试一次；还不行就到电脑上看看。"


def _error_kind(exc: BaseException) -> str:
    import sqlite3
    import urllib.error

    # 顺序有讲究：TimeoutError / FileNotFoundError / PermissionError 都是 OSError 的子类
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return ERR_TIMEOUT
    if isinstance(exc, (FileNotFoundError, NotADirectoryError, IsADirectoryError)):
        return ERR_NOT_FOUND
    if isinstance(exc, PermissionError):
        return ERR_PERMISSION
    if isinstance(exc, (ConnectionError, urllib.error.URLError)):
        return ERR_NETWORK
    if isinstance(exc, sqlite3.Error):
        return ERR_BUSY_DB
    return ERR_OTHER


def friendly_error(exc: BaseException, doing: str = "", *, log_as: str = "") -> str:
    """异常 → 给手机看的一句人话（+ 下一步）；**原始异常进日志**。

    `doing`：前面那半句（「读不到进程列表」）。空 ⇒ 只给原因 —— 发送失败那条
    app.py 已经套了「❌ 没发进去：」，这里再加一层就成了「没发进去：没发出去：」。
    `log_as`：日志里那句（带 tool / sid 方便排查）；空 ⇒ 用 `doing`。
    返回值里**不带**异常原文、异常类名、路径。
    """
    try:
        logger.error("%s（原文 %s: %s）", log_as or doing or "会话总控出错", type(exc).__name__, exc,
                     exc_info=exc)
    except Exception:  # noqa: BLE001 —— 记日志失败也不能让报错这一步再炸
        pass
    body = _error_kind(exc)
    return f"{doing}：{body}" if doing else body


# ---------- 数据结构 ----------


@dataclass(frozen=True)
class AskQuestion:
    """AskUserQuestion 里的一道题。`options` = ((label, description), …)。

    文字都是**原文**（没脱敏、没截断）—— 脱敏和截断是卡片那边的事，顺序见 `cards.pending_elements`。
    """

    question: str
    header: str = ""
    multi: bool = False
    options: tuple = ()


@dataclass(frozen=True)
class PendingAsk:
    """会话「等你」时，它卡在哪一步：jsonl 尾部最后一条**还没有 tool_result 的 tool_use**。

    - `tool == "AskUserQuestion"` ⇒ `questions` 有题目和选项
    - 别的工具 ⇒ `summary` 是一行原文摘要（命令 / 文件路径 / 网址…），可能带凭据，**卡片上先脱敏**
    只用来显示；手机上点选项回答不做（relay 发的是会话间消息，能不能当答案没验过）。
    """

    tool: str
    summary: str = ""
    questions: tuple = ()


@dataclass(frozen=True)
class Session:
    """一个会话在总控里的样子。

    `why_not_send` / `why_not_stop`：能做时为空；不能做时**必有**一句大白话
    （adapter 没给就用默认句），卡片直接贴出来。
    """

    tool: str
    surface: str
    id: str
    title: str = ""
    cwd: str = ""
    status: str = "unknown"
    pids: tuple = ()
    owner: str = "unknown"
    updated_at: float = 0
    can_send: bool = False
    can_stop: bool = False
    why_not_send: str = ""
    why_not_stop: str = ""
    # 🔴 `can_relay` 和 `can_send` 是**两条不同的路**，别合并：
    #   can_send  = 没人占着它，桥能起 `claude -p --resume` 接着跑（占着时会静默分叉）
    #   can_relay = 它正开在终端里，能把一句话**送进**它（会话间消息，不分叉、不打断）
    # 一个会话通常只满足一个。合成一个布尔值的后果是「有人占着」时走了 resume 那条路 ⇒ 分叉。
    can_relay: bool = False
    why_not_relay: str = ""
    extra: dict = field(default_factory=dict)
    # 「等你」时它在问什么 / 等你确认哪一步（只有 adapter 读得出来时才有；读不出来就是 None，卡上不显示）
    pending: Optional[PendingAsk] = None
    # 第 32 轮：**最后一轮**是怎么结束的（只有 adapter 从记录里真读到结束信号时才填）：
    #   "done"   = 好好跑完了（Claude 最后一条是收尾的回复 / Codex 最后是 task_complete）
    #   "failed" = 读到了失败信号（Claude 的 API 报错记录 / 桥自己发的 Codex 那一轮 turn.failed）
    #   ""       = 读不到 / 还在跑 / 被人打断 —— 卡上就不标，**不猜**
    # `last_turn_at`：那一轮结束的时间（记录里的时间戳；读不到就是 0，这时 last_turn 也清空）。
    last_turn: str = ""
    last_turn_at: float = 0

    def __post_init__(self) -> None:
        if self.tool not in TOOLS:
            raise ValueError(f"tool 只能是 {TOOLS}，收到 {self.tool!r}")
        if self.surface not in SURFACES:
            raise ValueError(f"surface 只能是 {SURFACES}，收到 {self.surface!r}")
        # 状态 / 归属读不准是常态（各家格式会变），降级成 unknown 而不是炸
        if self.status not in STATUSES:
            object.__setattr__(self, "status", "unknown")
        if self.owner not in OWNERS:
            object.__setattr__(self, "owner", "unknown")
        pids = []
        for pid in self.pids or ():
            try:
                pids.append(int(pid))
            except (TypeError, ValueError):
                continue
        object.__setattr__(self, "pids", tuple(pids))
        try:
            updated = float(self.updated_at or 0)
        except (TypeError, ValueError):
            updated = 0.0
        object.__setattr__(self, "updated_at", updated if updated > 0 else 0)
        object.__setattr__(self, "can_send", bool(self.can_send))
        object.__setattr__(self, "can_stop", bool(self.can_stop))
        object.__setattr__(self, "can_relay", bool(self.can_relay))
        object.__setattr__(self, "why_not_relay", "" if self.can_relay else (self.why_not_relay or ""))
        object.__setattr__(
            self, "why_not_send", "" if self.can_send else (self.why_not_send or DEFAULT_WHY_NOT_SEND)
        )
        object.__setattr__(
            self, "why_not_stop", "" if self.can_stop else (self.why_not_stop or DEFAULT_WHY_NOT_STOP)
        )
        if not isinstance(self.extra, dict):
            object.__setattr__(self, "extra", {})
        if not isinstance(self.pending, PendingAsk):
            object.__setattr__(self, "pending", None)
        try:
            turn_at = float(self.last_turn_at or 0)
        except (TypeError, ValueError):
            turn_at = 0.0
        turn_at = turn_at if turn_at > 0 and turn_at == turn_at else 0  # NaN 也不要
        # 没有时间的「跑完了」没法和「上次给你发卡」比先后 ⇒ 当成读不到
        last_turn = self.last_turn if self.last_turn in LAST_TURNS and turn_at else ""
        object.__setattr__(self, "last_turn", last_turn)
        object.__setattr__(self, "last_turn_at", turn_at if last_turn else 0)

    @property
    def key(self) -> tuple:
        """跨三家唯一：(tool, id)。"""
        return (self.tool, self.id)

    @property
    def age_text(self) -> str:
        return ago(self.updated_at)


@dataclass(frozen=True)
class Message:
    role: str  # user | assistant
    text: str
    at: float = 0


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    text: str
    session_id: str = ""
    # 🔴 **消息到底进没进会话**。ok=False 不等于没进去：超时、等确认、被停下时消息
    # 早已在会话里、对方可能已经动手了。原来一律显示「❌ 没发进去」⇒ 用户重发 ⇒
    # 在他真实的会话里重复执行一次（2026-11-04 体验审查）。起过子进程就算 delivered。
    delivered: bool = False


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    pid: int = 0
    error: str = ""  # 起都没起来（找不到命令等）时的说明

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.error


# ---------- 子进程 ----------


class SubprocessRunner:
    """所有子进程的唯一出口。测试注入假的，绝不起真 CLI。

    - stdin 默认 DEVNULL（`codex exec` 不关 stdin 会一直等着读，卡到超时）
    - `on_spawn(pid)` 在进程起来、**还没等它结束之前**调用 ⇒ adapter 在这里登记 FleetJobs，
      这样等待期间用户点「停下」也能认出是我们的
    - 超时：杀掉的是我们自己刚起的子进程，不涉及别人的
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
        input_text: Optional[str] = None,
        on_spawn: Optional[Callable[[int], None]] = None,
    ) -> RunResult:
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=cwd or None,
                env=env,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, ValueError) as exc:
            return RunResult(returncode=-1, error=friendly_error(exc, f"没能启动 {os.path.basename(str(argv[0])) if argv else '?'}"))
        if on_spawn is not None:
            try:
                on_spawn(proc.pid)
            except Exception:  # 登记失败不能让子进程变成没人管的孤儿
                logger.exception("on_spawn 回调失败 pid=%s", proc.pid)
        try:
            out, err = proc.communicate(input=input_text, timeout=timeout)
            return RunResult(returncode=proc.returncode, stdout=out or "", stderr=err or "", pid=proc.pid)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                out, err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                out, err = "", ""
            return RunResult(
                returncode=proc.returncode if proc.returncode is not None else -1,
                stdout=out or "",
                stderr=err or "",
                timed_out=True,
                pid=proc.pid,
            )

    def popen(self, argv: Sequence[str], *, cwd: Optional[str] = None, env: Optional[dict] = None):
        """常驻一轮用（中途插话）：stdin / stdout / stderr 都是管道，调用方自己读、自己收。"""
        return subprocess.Popen(
            list(argv),
            cwd=cwd or None,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )


# ---------- 时间 ----------

_MONTHS = {m: i for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), start=1
)}


def normalize_lstart(text: str) -> str:
    """`Tue Nov  3 19:02:35 2026` → `Tue Nov 3 19:02:35 2026`（压空白，便于逐字比较）。"""
    return " ".join(str(text or "").split())


def _ctime_fields(text: str) -> Optional[tuple]:
    parts = normalize_lstart(text).split(" ")
    if len(parts) != 5:
        return None
    _wd, mon, day, hms, year = parts
    month = _MONTHS.get(mon[:3].title())
    if month is None:
        return None
    try:
        hh, mm, ss = (int(x) for x in hms.split(":"))
        return (int(year), month, int(day), hh, mm, ss)
    except ValueError:
        return None


def parse_lstart(text: str) -> float:
    """ps 的 lstart（**本地时间**，`LC_ALL=C` 格式）→ epoch 秒；解析不了返回 0。"""
    fields = _ctime_fields(text)
    if fields is None:
        return 0.0
    try:
        return float(time.mktime(fields + (0, 0, -1)))
    except (OverflowError, ValueError):
        return 0.0


def parse_proc_start(value: Any) -> tuple:
    """注册表里的 procStart → 候选 epoch 秒（元组，可能 0~2 个）。

    实测见过三种写法：
    - 毫秒/秒时间戳（数字或数字字符串）
    - UTC ISO（`2026-11-03T11:02:35.538Z`；不带时区按 UTC）
    - ctime 格式字符串（Claude 2.1.317 实测是 **UTC** 的 `Tue Nov  3 11:02:35 2026`）
      —— 也有可能哪个版本写成本地时间，所以两种都给出来，由调用方逐个比

    两个候选也还是精确到秒的比较，pid 恰好复用、又恰好差整 8 小时起的概率可以忽略。
    """
    if value is None or isinstance(value, bool):
        return ()
    if isinstance(value, (int, float)):
        num = float(value)
        return (num / 1000.0 if num > 1e11 else num,) if num > 0 else ()
    text = str(value).strip()
    if not text:
        return ()
    try:
        num = float(text)
    except ValueError:
        pass
    else:
        return parse_proc_start(num)
    fields = _ctime_fields(text)
    if fields is not None:
        cands = [float(calendar.timegm(fields + (0, 0, 0)))]
        local = parse_lstart(text)
        if local and local not in cands:
            cands.append(local)
        return tuple(cands)
    iso = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        stamp = _dt.datetime.fromisoformat(iso)
    except ValueError:
        return ()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return (stamp.timestamp(),)


def parse_etime(text: str) -> int:
    """ps etime `[[dd-]hh:]mm:ss` → 秒；解析不了返回 0。"""
    text = str(text or "").strip()
    if not text:
        return 0
    days = 0
    if "-" in text:
        day_text, text = text.split("-", 1)
        try:
            days = int(day_text)
        except ValueError:
            return 0
    try:
        nums = [int(x) for x in text.split(":")]
    except ValueError:
        return 0
    if not 1 <= len(nums) <= 3:
        return 0
    while len(nums) < 3:
        nums.insert(0, 0)
    hh, mm, ss = nums
    return days * 86400 + hh * 3600 + mm * 60 + ss


def humanize_age(seconds: Any) -> str:
    """多久以前：刚刚 / 3 分钟前 / 2 小时前 / 2 天前。负数、读不出来都算刚刚。"""
    try:
        sec = float(seconds)
    except (TypeError, ValueError):
        return "刚刚"
    if sec != sec or sec < 60:  # NaN 也当刚刚
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)} 小时前"
    return f"{int(sec // 86400)} 天前"


def ago(epoch: Any, now: Optional[float] = None) -> str:
    """epoch 秒 → 「3 分钟前」；没有时间（0 / None）返回空串，卡片上就不显示。"""
    try:
        stamp = float(epoch or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    return humanize_age((time.time() if now is None else now) - stamp)


def short_title(text: Any, limit: int = 40) -> str:
    """压成一行、超长截断加「…」。按字符数算（中文一个字算一个）。"""
    flat = " ".join(str(text or "").split())
    if limit <= 0:
        return ""
    if len(flat) <= limit:
        return flat
    return flat[: max(limit - 1, 0)].rstrip() + "…"


# ---------- 进程表 ----------

PS_ARGV = ("ps", "-axww", "-o", "pid=,ppid=,stat=,lstart=,etime=,args=")


def _ps_env() -> dict:
    env = dict(os.environ)
    env["LC_ALL"] = "C"  # lstart 的月份/星期要是英文，否则解析不了
    return env


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    stat: str
    lstart: str  # 已压空白，本地时间，例 `Tue Nov 3 19:02:35 2026`
    etime_sec: int
    args: str

    @property
    def started_at(self) -> float:
        return parse_lstart(self.lstart)

    @property
    def stopped(self) -> bool:
        """ps STAT 带 T = 被 Ctrl-Z / SIGSTOP 挂起。"""
        return "T" in self.stat


def _same_second(cand: float, lstart_epoch: float) -> bool:
    # lstart 只到秒；时间戳可能带毫秒 ⇒ 向下取整再比，留 1 秒余量给写注册表的那一下
    return abs(int(cand) - int(lstart_epoch)) <= 1


class ProcTable:
    """一次 ps 的快照。`LC_ALL=C ps -axww -o pid=,ppid=,stat=,lstart=,etime=,args=`。

    读失败（没有 ps、超时）⇒ 空表 + `error` 一句话，不抛。
    """

    def __init__(self, procs: Iterable[Proc] = (), error: str = "") -> None:
        self._by_pid = {p.pid: p for p in procs}
        self.error = error

    # ---------- 构造 ----------

    @staticmethod
    def parse_line(line: str) -> Optional[Proc]:
        parts = line.split(None, 9)
        # pid ppid stat + lstart 5 段 + etime = 9 段；args 可能为空（僵尸进程）
        if len(parts) < 9:
            return None
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            return None
        lstart = " ".join(parts[3:8])
        if _ctime_fields(lstart) is None:
            return None
        return Proc(
            pid=pid,
            ppid=ppid,
            stat=parts[2],
            lstart=lstart,
            etime_sec=parse_etime(parts[8]),
            args=parts[9].rstrip("\n") if len(parts) > 9 else "",
        )

    @classmethod
    def parse(cls, text: str, error: str = "") -> "ProcTable":
        procs = []
        for line in (text or "").splitlines():
            if not line.strip():
                continue
            proc = cls.parse_line(line)
            if proc is not None:
                procs.append(proc)
        return cls(procs, error=error)

    @classmethod
    def snapshot(cls, runner: Optional[SubprocessRunner] = None, timeout: float = PS_TIMEOUT_SEC) -> "ProcTable":
        runner = runner or SubprocessRunner()
        try:
            result = runner.run(PS_ARGV, timeout=timeout, env=_ps_env())
        except Exception as exc:  # runner 是可注入的，防它自己炸
            return cls(error=friendly_error(exc, "读不到进程列表"))
        if result.error:
            return cls(error="读不到进程列表：" + result.error)
        if result.timed_out:
            return cls(error="读进程列表超时了")
        if result.returncode != 0 and not result.stdout.strip():
            logger.warning("ps 退出码 %s，读不到进程列表", result.returncode)
            return cls(error="读不到进程列表（系统命令没跑成）")
        return cls.parse(result.stdout)

    # ---------- 查询 ----------

    def __len__(self) -> int:
        return len(self._by_pid)

    def __contains__(self, pid: Any) -> bool:
        return self.get(pid) is not None

    def __iter__(self):
        return iter(self._by_pid.values())

    def get(self, pid: Any) -> Optional[Proc]:
        try:
            return self._by_pid.get(int(pid))
        except (TypeError, ValueError):
            return None

    def alive(self, pid: Any) -> bool:
        return self.get(pid) is not None

    def lstart(self, pid: Any) -> str:
        proc = self.get(pid)
        return proc.lstart if proc else ""

    def children(self, ppid: int) -> list:
        return [p for p in self._by_pid.values() if p.ppid == ppid]

    def find(self, predicate: Callable[[Proc], bool]) -> list:
        out = []
        for proc in self._by_pid.values():
            try:
                if predicate(proc):
                    out.append(proc)
            except Exception:
                continue
        return out

    def same_process(self, pid: Any, proc_start: Any) -> bool:
        """pid 还在、而且就是当初那个进程（启动时间对得上）⇒ True。

        `proc_start` 可以是：ps lstart 原样字符串（本地时间）、毫秒/秒时间戳、UTC ISO、
        Claude 注册表里的 UTC ctime 串。对不上、读不出来一律 False —— 宁可说「不在跑」，
        也不能把复用了 pid 的别的进程认成会话。
        """
        proc = self.get(pid)
        if proc is None:
            return False
        if isinstance(proc_start, str) and normalize_lstart(proc_start) == proc.lstart:
            return True
        actual = proc.started_at
        if not actual:
            return False
        return any(_same_second(cand, actual) for cand in parse_proc_start(proc_start))


# ---------- 桥自己起的进程 ----------


@dataclass(frozen=True)
class Job:
    pid: int
    lstart: str
    tool: str
    session_id: str
    registered_at: float = 0


def _ps_lstart_of(pid: int) -> str:
    """只查一个 pid 的 lstart；不在 / 读失败 ⇒ 空串。"""
    try:
        out = subprocess.run(
            ["ps", "-ww", "-o", "lstart=", "-p", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=PS_TIMEOUT_SEC,
            env=_ps_env(),
            stdin=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""
    return normalize_lstart(out.stdout) if out.returncode == 0 else ""


class FleetJobs:
    """登记簿：桥**自己起的**进程。停进程只认这里。

    `owns(pid)` = 登记过 **且** 现在这个 pid 的 lstart 和登记时一样。
    进程退出后 pid 被别人复用，lstart 一定变 ⇒ 不认。
    登记时拿不到 lstart（进程已经没了）⇒ 不登记 —— 没法防复用的条目宁可没有。
    """

    def __init__(
        self,
        lstart_of: Callable[[int], str] = _ps_lstart_of,
        kill: Callable[[int, int], None] = os.kill,
    ) -> None:
        self._lstart_of = lstart_of
        self._kill = kill
        self._lock = threading.Lock()
        self._jobs: dict = {}
        self._stopped: set = set()

    def mark_stopped(self, tool: str, session_id: str) -> None:
        with self._lock:
            self._stopped.add((str(tool), str(session_id)))

    def take_stopped(self, tool: str, session_id: str) -> bool:
        """这一轮是不是被手机上的「停下」打断的（读一次就清）。"""
        with self._lock:
            key = (str(tool), str(session_id))
            hit = key in self._stopped
            self._stopped.discard(key)
            return hit

    def _current_lstart(self, pid: int) -> str:
        try:
            return normalize_lstart(self._lstart_of(pid))
        except Exception:
            return ""

    def register(self, pid: int, lstart: str, tool: str, session_id: str) -> Optional[Job]:
        lstart = normalize_lstart(lstart)
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return None
        if pid <= 0 or not lstart:
            logger.warning("不登记 pid=%s：拿不到启动时间，没法防 pid 复用", pid)
            return None
        job = Job(pid=pid, lstart=lstart, tool=str(tool), session_id=str(session_id), registered_at=time.time())
        with self._lock:
            self._jobs[pid] = job
        return job

    def register_spawned(self, pid: int, tool: str, session_id: str) -> Optional[Job]:
        """刚 spawn 出来的子进程：现查 lstart 再登记（给 `SubprocessRunner.run(on_spawn=)` 用）。"""
        return self.register(pid, self._current_lstart(pid), tool, session_id)

    def forget(self, pid: int) -> None:
        with self._lock:
            self._jobs.pop(int(pid), None)

    def get(self, pid: int) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(int(pid))

    def jobs(self, tool: str = "", session_id: str = "") -> list:
        with self._lock:
            items = list(self._jobs.values())
        return [
            j for j in items
            if (not tool or j.tool == tool) and (not session_id or j.session_id == session_id)
        ]

    def owns(self, pid: Any, table: Optional[ProcTable] = None) -> bool:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        job = self.get(pid)
        if job is None:
            return False
        now = table.lstart(pid) if table is not None else self._current_lstart(pid)
        return bool(now) and now == job.lstart

    def prune(self, table: Optional[ProcTable] = None) -> int:
        """清掉已经退出（或 pid 被复用）的条目，返回清掉几个。"""
        removed = 0
        for job in self.jobs():
            if not self.owns(job.pid, table):
                self.forget(job.pid)
                removed += 1
        return removed

    def signal(self, pid: Any, sig: int = _signal.SIGINT) -> ActionResult:
        """唯一发信号的地方。不是我们的一律拒，不碰 os.kill。"""
        job = None
        try:
            job = self.get(int(pid))
        except (TypeError, ValueError):
            pass
        sid = job.session_id if job else ""
        if not self.owns(pid):
            if job is not None:  # 登记过但已经退出 / 被复用 ⇒ 顺手清掉
                self.forget(job.pid)
                return ActionResult(False, "它已经结束了，不用再停。", sid)
            return ActionResult(False, "这个进程不是从手机上开的，不能从这里停。", sid)
        try:
            self._kill(int(pid), sig)
        except ProcessLookupError:
            self.forget(int(pid))
            return ActionResult(False, "它已经结束了，不用再停。", sid)
        except OSError as exc:
            return ActionResult(False, friendly_error(exc, "没停下来"), sid)
        return ActionResult(True, "已经让它停下了。", sid)

    def stop_session(self, tool: str, session_id: str, sig: int = _signal.SIGINT) -> ActionResult:
        """停掉某会话下桥自己起的所有进程；一个都没有 ⇒ ok=False。"""
        jobs = self.jobs(tool=tool, session_id=session_id)
        if not jobs:
            return ActionResult(False, "手机上没有在给它跑的任务，没有可停的。", session_id)
        results = [self.signal(j.pid, sig) for j in jobs]
        if any(r.ok for r in results):
            self.mark_stopped(tool, session_id)
            return ActionResult(True, "已经让它停下了。这一轮不再继续，会话还在，可以接着说。", session_id)
        return ActionResult(False, results[-1].text, session_id)


# ---------- adapter 基类 ----------


class Adapter:
    """每家一个子类。默认实现：什么都列不出来、发不了、停不了（带中文原因）。

    约定：
    - 任何方法都不抛给上层；读失败返回已有的，把说明放进 `self.last_error`
    - 子进程一律走 `self.runner`，自己起的进程登记进 `self.jobs`
    """

    tool: str = ""

    def __init__(self, jobs: Optional[FleetJobs] = None, runner: Optional[SubprocessRunner] = None) -> None:
        self.jobs = jobs if jobs is not None else FleetJobs()
        self.runner = runner if runner is not None else SubprocessRunner()
        self.last_error = ""

    def list(self, limit: int = 20) -> list:
        return []

    def running(self) -> list:
        return []

    def read(self, id: str, n: int = 4) -> list:
        return []

    def send(self, id: str, text: str, timeout: float = 600) -> ActionResult:
        return ActionResult(False, "这类会话暂时不支持从手机上发消息。", id)

    def stop(self, id: str) -> ActionResult:
        return ActionResult(False, "这类会话暂时不支持从手机上停。", id)

    def find(self, id: str) -> Optional[Session]:
        """先在 running() 里找，再在 list() 里找；找不到 None。"""
        for getter in (self.running, lambda: self.list(200)):
            try:
                for sess in getter():
                    if sess.id == id:
                        return sess
            except Exception as exc:
                self.last_error = friendly_error(exc, "读会话失败")
        return None

    def stop_own(self, id: str) -> ActionResult:
        """子类的 stop 可以直接用：只停 FleetJobs 里登记在这个会话名下的进程。"""
        return self.jobs.stop_session(self.tool, id)


def resolve_lsof() -> str:
    """lsof 的绝对路径。

    🔴 不能裸写 "lsof"：桥是 launchd 起的，PATH 只有 /opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin，
    而 lsof 在 /usr/sbin。裸调用 ⇒ 找不到 ⇒ Cursor 发完收不掉多出来的 worker-server，
    每发一个新项目就漏一个常驻进程（2026-11-04 稳定性审查）。
    """
    import shutil

    for cand in ("/usr/sbin/lsof", "/usr/bin/lsof"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("lsof") or "/usr/sbin/lsof"
