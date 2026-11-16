"""一次扫描：三家并发列会话，每家独立超时，绝不抛异常。

`Fleet(adapters).overview()` → `Overview`：在跑的 + 最近的（跨三家），某家读失败 / 超时
就只少那一家，并在 `notes` 里留一句大白话。手机上的卡片只看这个结构。

🔴 超时的那一家**不等它**：线程池里的线程会自己跑完（adapter 里所有子进程都带 timeout），
但这次总览不带它的结果。所以这里不用 `with ThreadPoolExecutor()`（退出时会 join 等它）。
"""

from __future__ import annotations

import concurrent.futures as _cf
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .model import Session, friendly_error

logger = logging.getLogger("feishu-bridge.fleet")

TOOL_LABELS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor", "opencode": "opencode"}
DEFAULT_TIMEOUT_SEC = 1.2  # 本机实测三家各自 50~270ms；留余量但总览要 < 1.5 秒
# 每家读多少个历史会话（在跑 / 等你的另算，全读）。第 28 轮从 6 提到 30：总览「还有 N 个，点开看全部」
# 要真能列出来，读 6 个时 Codex / Cursor 那组点开也只有 6 行。本机实测三家读 30 个各 70~200ms，
# 在 DEFAULT_TIMEOUT_SEC 之内；总览卡本身每组仍只列 MAX_ROWS_PER_TOOL 行，卡不会变大。
RECENT_PER_TOOL = 30
# 🔴 2026-11-06：**「等你」排第一**，排在「在跑」前面。
# 原来是 running 在前、waiting 在后，而总览每组只列 8 行 ⇒ 实测 10 个在跑 + 2 个等你时，
# 列出来的 8 行全是 🟢，两个在等主人的会话被压进「还有 11 个更早的没列出来」里 ——
# 恰恰是主人掏出手机最想找的那一类（它卡住了、在等我点头）。在跑的不用他管，等他的才要他管。
_STATUS_RANK = {"waiting": 0, "running": 1, "idle": 2, "stopped": 3, "unknown": 4}


@dataclass
class Overview:
    sessions: list = field(default_factory=list)  # 全部（在跑的在前），跨三家
    notes: list = field(default_factory=list)  # 哪家读不到 / 超时，一家一句
    took_sec: float = 0.0
    at: float = 0.0
    # 这次**按时读完**的那几家（就算一个会话都没有）。总控据此决定替换哪几家的缓存。
    ok_tools: list = field(default_factory=list)

    def by_tool(self) -> dict:
        out: dict = {}
        for sess in self.sessions:
            out.setdefault(sess.tool, []).append(sess)
        return out

    @property
    def running(self) -> list:
        return [s for s in self.sessions if s.pids or s.status in ("running", "waiting")]

    def running_count(self) -> int:
        return len(self.running)


def _sort_key(sess: Session) -> tuple:
    live = bool(sess.pids) or sess.status in ("running", "waiting")
    return (not live, _STATUS_RANK.get(sess.status, 9), -(sess.updated_at or 0))


class Fleet:
    """把几家 adapter 拼成一个总控。`adapters`：tool → Adapter（或 Adapter 列表）。"""

    def __init__(self, adapters: Any, timeout: float = DEFAULT_TIMEOUT_SEC,
                 per_tool: int = RECENT_PER_TOOL) -> None:
        if isinstance(adapters, dict):
            items = list(adapters.items())
        else:
            items = [(getattr(a, "tool", ""), a) for a in adapters or ()]
        self.adapters = {tool: ad for tool, ad in items if tool}
        self.timeout = float(timeout)
        self.per_tool = int(per_tool)
        self._pool = _cf.ThreadPoolExecutor(max_workers=max(2, len(self.adapters) * 2),
                                            thread_name_prefix="fleet-scan")
        # 同一家上一次扫描还没回来（超时了还在跑）就不再叠一个：慢的那家不会越堆越多
        self._busy: set = set()
        self._busy_guard = threading.Lock()

    def adapter(self, tool: str) -> Optional[Any]:
        return self.adapters.get(tool)

    def _one(self, tool: str, adapter: Any, limit: int) -> tuple:
        try:
            sessions = list(adapter.list(limit) or [])
        except Exception as exc:  # adapter 约定不抛；万一抛了也只少这一家
            # 外面会拼成「Claude：…」，这里别再带一遍家名
            return [], friendly_error(exc, "这次没读出来", log_as=f"扫描 {tool} 失败")
        finally:
            with self._busy_guard:
                self._busy.discard(tool)
        note = str(getattr(adapter, "last_error", "") or "")
        return [s for s in sessions if isinstance(s, Session)], note

    def overview(self, limit: Optional[int] = None) -> Overview:
        started = time.monotonic()
        limit = self.per_tool if limit is None else int(limit)
        out = Overview(at=time.time())
        futures = {}
        for tool, adapter in self.adapters.items():
            with self._busy_guard:
                if tool in self._busy:
                    out.notes.append(f"{TOOL_LABELS.get(tool, tool)} 上一次还没读完，这次先跳过")
                    continue
                self._busy.add(tool)
            try:
                futures[tool] = self._pool.submit(self._one, tool, adapter, limit)
            except Exception as exc:  # 线程池关了之类
                with self._busy_guard:
                    self._busy.discard(tool)
                out.notes.append(friendly_error(exc, f"{TOOL_LABELS.get(tool, tool)} 没能开始读"))
        deadline = started + self.timeout
        for tool, fut in futures.items():
            label = TOOL_LABELS.get(tool, tool)
            try:
                sessions, note = fut.result(timeout=max(0.0, deadline - time.monotonic()))
            except _cf.TimeoutError:
                out.notes.append(f"{label} 读得太慢，这次没等它")
                continue
            except Exception as exc:
                out.notes.append(friendly_error(exc, f"{label} 没读出来"))
                continue
            out.sessions.extend(sessions)
            out.ok_tools.append(tool)
            if note:
                out.notes.append(f"{label}：{note}")
        out.sessions.sort(key=_sort_key)
        out.took_sec = time.monotonic() - started
        return out

    def running_count(self) -> Optional[int]:
        """电脑上开着几个会话（给 /status）。读不出返回 None。"""
        try:
            return self.overview().running_count()
        except Exception:
            return None
