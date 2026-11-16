"""会话总控的状态：最近一次扫描、每个聊天的「待发目标」、把发 / 停分给对应那家。

飞书那一层（`bridge/app.py`）只管收发；这里不碰飞书、只用标准库，方便单测。

- **已知 id**：只有最近一次扫描（总览或详情刷新）里出现过的 (tool, id) 才能被按钮点到。
- **待发目标**：点 [接着聊] 之后，这个聊天的下一条普通消息发到那个会话。一个聊天只记一个，
  5 分钟过期；`/cancel` 或「取消」清掉。真正发之前 adapter 会**在锁里再查一次**能不能发
  （五分钟里终端可能打开了它）。
- **对话模式**：点 [接着聊] 进去之后**一直**发给那个会话，直到 `/cancel`。
  进去之前要过一次权限确认（`consent.RelayConsent`，24 小时记忆）。

## 两条路的分工（别混）

| | 走哪个方法 | 什么时候能用 |
|---|---|---|
| 普通「接着聊」 | `adapter.send()` | **没人占着**那个会话（Claude 一定要没人占，不然分叉） |
| 对话模式 | `adapter.relay()` / `Relay.send()` | 目标**正开着**也行（会话间消息 / codex queue） |
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .model import ActionResult, Session, friendly_error, short_title
from .scan import TOOL_LABELS, Fleet, Overview

logger = logging.getLogger("feishu-bridge.fleet")

PENDING_TTL_SEC = 5 * 60
DETAIL_MESSAGES = 4

WHY_NO_ADAPTER = "这类会话这台电脑上读不了。"
# 工作目录白名单之外。手机用户改不了 config.toml —— 告诉他在哪看、找谁改，不甩术语。
# 🔴 app._maybe_send_to_fleet 靠**开头那半句**认出它、给它配「换项目」按钮 —— 开头别改。
WHY_OUTSIDE_WORKSPACE = (
    "这个项目不在手机能用的目录里，先不发。点「换项目」看现在能用哪些；"
    "要放开得在电脑上改这个助手的设置。"
)
WHY_UNKNOWN_TARGET = "这个会话不在刚才的列表里了，点「回到总览」重新看一眼。"


@dataclass(frozen=True)
class Target:
    tool: str
    id: str
    title: str
    expires_at: float

    @property
    def label(self) -> str:
        name = self.title or self.id[:8]
        return f"{TOOL_LABELS.get(self.tool, self.tool)}「{name}」"


class FleetControl:
    def __init__(self, fleet: Fleet, *, ttl: float = PENDING_TTL_SEC,
                 now: Callable[[], float] = time.time) -> None:
        self.fleet = fleet
        self.ttl = float(ttl)
        self._now = now
        self._lock = threading.Lock()
        self._known: dict = {}  # (tool, id) → Session（最近一次扫描）
        self._pending: dict = {}  # scope → Target
        self.last: Optional[Overview] = None

    # ---------- 扫描 ----------

    def overview(self) -> Overview:
        view = self.fleet.overview()
        with self._lock:
            # 🔴 **按家合并**，不整体替换。某一家这次读超时 / 被跳过时它不在结果里 ——
            # 整体替换会把它的编号全清掉，用户点之前那张卡上的按钮就报「过期了」
            # （2026-11-04 稳定性审查）。只替换这次真读到的那几家。
            fresh_tools = {sess.tool for sess in view.sessions}
            fresh_tools |= set(getattr(view, "ok_tools", ()) or ())
            kept = {k: v for k, v in self._known.items() if k[0] not in fresh_tools}
            kept.update({sess.key: sess for sess in view.sessions})
            self._known = kept
            self.last = view
        return view

    def running(self) -> list:
        """最近一次扫描里正在跑的会话（私聊「停下」选哪个用，FC-11/12）。手机自己起的排前面，再按更新时间。"""
        with self._lock:
            items = [s for s in self._known.values() if getattr(s, "status", "") == "running"]
        return sorted(items, key=lambda s: (not getattr(s, "can_stop", False), -float(getattr(s, "updated_at", 0) or 0)))

    def known_keys(self) -> set:
        with self._lock:
            return set(self._known)

    def cached(self, tool: str, sid: str) -> Optional[Session]:
        with self._lock:
            return self._known.get((tool, sid))

    def _remember(self, sess: Session) -> None:
        with self._lock:
            self._known[sess.key] = sess

    def refresh(self, tool: str, sid: str) -> tuple:
        """现查一个会话还在不在 → (Session 或 None, 读失败时的一句话)。

        给「一个群 = 一个会话」用（bridge/fleet/chatbind.py）：群绑定是长期的，
        重启之后 `_known` 是空的，群里来一条消息时要靠这里把那个会话重新认回来。
        - 查到了 ⇒ 记进 `_known`（之后确认卡、停下这些按钮才点得动）
        - `(None, "")` ⇒ **确实没了**（电脑上关掉了 / 删了）
        - `(None, "原因")` ⇒ **没读出来**，不知道在不在 —— 调用方别当成「没了」
        """
        adapter = self.fleet.adapter(tool)
        if adapter is None:
            return None, WHY_NO_ADAPTER
        try:
            sess = adapter.find(sid)
        except Exception as exc:  # adapter 约定不抛；保险
            return None, friendly_error(exc, "现在读不出它的状态", log_as=f"现查会话失败 tool={tool}")
        if sess is None:
            # 🔴 **「找不到」和「读不出来」要分开**。2026-11-05 体验审查：Codex 的 state db
            # 被 App 锁住、Cursor 的库读不到时 find() 也返回 None ⇒ 群里报「会话已经关掉或删了」，
            # 而它其实还在电脑上好好跑着。读失败时把 adapter 的 last_error 带出去，
            # 调用方走「读不出来」那条分支（app.py 的「现在读不出它的状态」）。
            err = str(getattr(adapter, "last_error", "") or "")
            if err:
                return None, f"现在读不出它的状态：{err}"
            return None, ""
        self._remember(sess)
        return sess, ""

    def record_path(self, tool: str, sid: str) -> str:
        """会话记录文件（进展卡读新增部分用）。Claude = jsonl；Codex = 最新的 rollout 分段。读不到 ⇒ 空串。"""
        adapter = self.fleet.adapter(tool)
        try:
            if tool == "claude" and hasattr(adapter, "_jsonl_path"):
                return adapter._jsonl_path(sid) or ""
            if tool == "codex" and hasattr(adapter, "_segments"):
                segs = adapter._segments(sid)
                return segs[0] if segs else ""
        except Exception:  # noqa: BLE001 —— 只少了进展那几行
            return ""
        return ""

    def detail(self, tool: str, sid: str, n: int = DETAIL_MESSAGES) -> tuple:
        """(Session 或 None, 最近 n 条, 一句说明)。状态现查；查不到用上次扫描的并说明。"""
        adapter = self.fleet.adapter(tool)
        if adapter is None:
            return None, [], WHY_NO_ADAPTER
        note = ""
        fresh = None
        try:
            fresh = adapter.find(sid)
        except Exception as exc:  # adapter 约定不抛；保险
            note = friendly_error(exc, "现在的状态没读出来", log_as=f"刷新会话状态失败 tool={tool}")
        sess = fresh or self.cached(tool, sid)
        if fresh is None and sess is not None and not note:
            note = "现在的状态没读出来，下面是刚才列表里的样子。"
        if sess is not None:
            self._remember(sess)
        try:
            messages = list(adapter.read(sid, n) or [])
        except Exception as exc:
            messages = []
            note = note or friendly_error(exc, "最近的消息没读出来")
        if not messages and getattr(adapter, "last_error", ""):
            note = note or str(adapter.last_error)
        return sess, messages, note

    # ---------- 待发目标 ----------

    def set_pending(self, scope: str, tool: str, sid: str) -> Target:
        sess = self.cached(tool, sid)
        title = (sess.title if sess is not None else "") or ""
        target = Target(tool, sid, title, self._now() + self.ttl)
        with self._lock:
            self._pending[scope] = target
        return target

    def peek_pending_fresh(self, scope: str) -> tuple:
        """(没过期的 Target 或 None, 是否过期)。**只看不取** —— 取走是 /cancel / 过期的事。

        🔴 原来发消息走 `take_pending`（看一眼就清掉），导致消息在飞的那段时间里
        「在不在对话模式」这个状态凭空消失：补一句会落进桥自己的 claude、/cancel 说
        没东西可取消、刷新卡片看不出在跟谁说话（2026-11-04 三个审查角色各自撞到）。
        """
        target = self.peek_pending(scope)
        if target is None:
            return None, False
        if self._now() > target.expires_at:
            return None, True
        return target, False

    def peek_pending(self, scope: str) -> Optional[Target]:
        with self._lock:
            return self._pending.get(scope)

    def take_pending(self, scope: str) -> tuple:
        """(Target 或 None, 是否已过期)。取出即清掉。"""
        with self._lock:
            target = self._pending.pop(scope, None)
        if target is None:
            return None, False
        if self._now() > target.expires_at:
            return None, True
        return target, False

    def clear_pending(self, scope: str) -> Optional[Target]:
        with self._lock:
            return self._pending.pop(scope, None)

    # ---------- 发 / 停 ----------

    def send(self, tool: str, sid: str, text: str, *, policy: Any = None, approval_dir: str = "",
             timeout: float = 600, relay: bool = False, model: str = "", effort: str = "") -> ActionResult:
        """按家分派。三家的 send 都在锁里**再查一次**有没有人占着，这里不重复查。

        `relay=True`：走「进会话聊天」那条路（adapter 的 `relay()`）。Codex / Cursor 的
        relay 和 send 的区别在**能不能发给一个别人正开着的会话**、以及怎么取回复；
        没实现 relay 的那家自动退回 `send()`，调用方不用分情况。
        """
        adapter = self.fleet.adapter(tool)
        if adapter is None:
            return ActionResult(False, WHY_NO_ADAPTER, sid)
        try:
            if tool == "claude":
                # 🔴 **终端里开着的会话只能走 relay**。`send()` 那条路是 `claude -p --resume`，
                # 对一个有进程占着的会话会在同一个 jsonl 里**静默分叉**，终端那边永远看不到
                # （claude.py 模块头的实测）。所以这里按「有没有人占着」选路，不按调用方的心愿：
                # relay=True 但其实没人占着 ⇒ 还是走 send（同步拿回复，不用等信使）；
                # relay=False 但终端占着 ⇒ send 自己会拒（它在锁里复查），不会分叉。
                if relay and hasattr(adapter, "relay"):
                    can, why = self._claude_relay_ready(adapter, sid)
                    if can:
                        # relay 不起目标进程、不传档位：目标用的是它在电脑上那套权限
                        return adapter.relay(sid, text, timeout=timeout)
                    if why:
                        return ActionResult(False, why, sid)
                return adapter.send(sid, text, timeout=timeout, policy=policy, approval_dir=approval_dir,
                                    **_model_kw(model, effort))
            # Codex 跟随当前权限档位：普通 trusted 可写，write 限定工作区，readonly 才只读。
            # 工作目录仍要和 Claude 一样过一遍白名单。
            cwd = self._cwd_of(tool, sid)
            if cwd and policy is not None and hasattr(policy, "resolve_workspace"):
                if policy.resolve_workspace(cwd) is None:
                    return ActionResult(False, WHY_OUTSIDE_WORKSPACE, sid)
            method = getattr(adapter, "relay", None) if relay else None
            if method is None:
                method = adapter.send
            mode = getattr(policy, "mode", "trusted") if policy is not None else "trusted"
            extra = _model_kw(model, effort) if tool == "codex" else {}
            if tool == "codex" and approval_dir:
                extra["approval_dir"] = approval_dir
            return method(sid, text, timeout=timeout, mode=mode, **extra)
        except Exception as exc:
            # 不带前缀：app.py 那边已经套了「❌ 没发进去：」，再加一层就是「没发进去：没发出去：」
            return ActionResult(False, friendly_error(exc, log_as=f"会话总控发送失败 tool={tool}"), sid)

    def start(self, tool: str, cwd: str, text: str, *, policy: Any = None, approval_dir: str = "",
              timeout: float = 600, model: str = "", on_pid: Any = None, effort: str = "") -> ActionResult:
        """手机上「➕ 新建会话」：在 `cwd` 新开一个会话，`text` 是第一句。

        闸门和 `send()` 同一套：工作目录先过 `policy.resolve_workspace`；Claude 按手机档位 + 审批钩子；
        Codex 跟随当前权限档位：trusted 可写，write 限定工作区，readonly 只读。开成了就记进 `_known`，
        之后的 [停下] / 确认卡 / 对话模式都认得它。
        """
        adapter = self.fleet.adapter(tool)
        if adapter is None or not hasattr(adapter, "start"):
            return ActionResult(False, WHY_NO_ADAPTER, "")
        if policy is None or not hasattr(policy, "resolve_workspace"):
            return ActionResult(False, "手机发消息的权限档位没配好，先不开。", "")
        resolved = policy.resolve_workspace(cwd) if cwd else None
        if not resolved:
            return ActionResult(False, WHY_OUTSIDE_WORKSPACE, "")
        try:
            extra = _model_kw(model, effort) if tool in ("claude", "codex") else {}
            if on_pid is not None and tool in ("claude", "codex"):
                extra["on_pid"] = on_pid
            if tool == "claude":
                result = adapter.start(resolved, text, timeout=timeout, policy=policy, approval_dir=approval_dir,
                                       **extra)
            else:
                if tool == "codex" and approval_dir:
                    extra["approval_dir"] = approval_dir
                result = adapter.start(resolved, text, timeout=timeout,
                                       mode=getattr(policy, "mode", "trusted"), **extra)
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, log_as=f"新建会话失败 tool={tool}"), "")
        if result.session_id:
            try:
                sess = adapter.find(result.session_id)
            except Exception:  # noqa: BLE001
                sess = None
            if sess is None:
                sess = Session(tool=tool, surface="cli", id=result.session_id, title=short_title(text, 40),
                               cwd=resolved, status="idle", owner="none", updated_at=self._now(),
                               can_send=True)
            self._remember(sess)
        return result

    def _claude_relay_ready(self, adapter: Any, sid: str) -> tuple:
        """这条 Claude 会话该不该走 relay → (走不走, 不走时要不要告诉用户为什么)。

        `why` 为空串的意思是「**退回 `send()`**」，不是「拒绝」：没人占着的会话走 resume
        那条路更好（同步拿回复，不用起信使），而 `send()` 自己会在锁里复查一次。
        `why` 有内容才是真的做不到，照实说。
        """
        checker = getattr(adapter, "can_relay", None)
        if checker is None:
            return False, ""
        try:
            can, why = checker(sid)
        except Exception as exc:  # adapter 约定不抛；防它自己炸
            return False, friendly_error(exc, "看不出能不能送进去", log_as=f"查能不能接着聊失败 sid={sid}")
        if can:
            return True, ""
        # 不能 relay。终端正占着它 ⇒ 只有 relay 这条路，没成就得说清为什么
        # （退回 send() 的话会被「会分叉」拒绝，那句话在 relay 存在之后是误导）。
        sess = self.cached("claude", sid)
        if sess is not None and sess.owner == "terminal":
            return False, why or "现在送不进去。到电脑上和它说吧。"
        return False, ""

    def _cwd_of(self, tool: str, sid: str) -> str:
        sess = self.cached(tool, sid)
        return str(getattr(sess, "cwd", "") or "") if sess else ""

    def signal_spawned(self, tool: str, pid: int) -> ActionResult:
        """停一个桥自己起的进程（新开会话那一轮，还不知道会话编号时用）。只认登记过的进程。"""
        adapter = self.fleet.adapter(tool)
        jobs = getattr(adapter, "jobs", None)
        if jobs is None:
            return ActionResult(False, WHY_NO_ADAPTER, "")
        return jobs.signal(pid)

    def stop(self, tool: str, sid: str, *, relay: bool = False) -> ActionResult:
        """`relay=True`：对话模式里的 `/stop`。

        停不了的一律照实说在哪停（Claude 终端会话、Codex App / 终端那一轮都停不了），
        **不假装能停** —— 从外面发信号会直接关掉用户整个会话。
        """
        adapter = self.fleet.adapter(tool)
        if adapter is None:
            return ActionResult(False, WHY_NO_ADAPTER, sid)
        try:
            method = (getattr(adapter, "relay_stop", None) if relay else None) or adapter.stop
            return method(sid)
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, "没停成", log_as=f"会话总控停止失败 tool={tool}"), sid)


def build_default(*, policy: Callable[[], Any], bridge_home: str, hook_settings: Any = None,
                  jobs: Any = None, hook_path: Any = None) -> FleetControl:
    """真实家目录下的三家 + 一份共用的 FleetJobs。"""
    from .claude import ClaudeAdapter
    from .codex import CodexAdapter
    from .cursor import CursorAdapter
    from .model import FleetJobs

    jobs = jobs if jobs is not None else FleetJobs()
    adapters = {
        "claude": ClaudeAdapter(bridge_home=bridge_home, jobs=jobs, policy=policy,
                                hook_settings=hook_settings),
        "codex": CodexAdapter(jobs=jobs, hook_path=hook_path),
        "cursor": CursorAdapter(jobs=jobs, mode=lambda: getattr(policy(), "mode", "readonly")),
    }
    return FleetControl(Fleet(adapters))


def _model_kw(model: str = "", effort: str = "") -> dict:
    """只把真设了的模型 / effort 传给 adapter：老的 adapter（和测试替身）不认这两个参数。"""
    out = {}
    if model:
        out["model"] = model
    if effort:
        out["effort"] = effort
    return out
