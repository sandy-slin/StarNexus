"""会话绑定的群里推「要人介入的事」和「重要进展」（不推每条更新）。

## 推什么
1. 🟡 在等你：会话进入 waiting 持续 ≥ 20 秒 ⇒ 推一次（同一段等待只推一次；问了新的算新的一次）。
   要人介入的**一定推**：不看人在不在电脑前，也不吃单聊那边的静音。
2. ✅ 做完停下：最后一轮 `done`，并且停着没再动 ≥ 45 秒（在等下一步指令）⇒ 推一次，带上它最后说的话。
3. ❌ 出错：最后一轮 `failed` ⇒ 推一次。

## 不推什么
- 群里自己发进去、桥已经把回复贴回群里的那一轮（`note_relay` 记下的时间窗内结束的）
- 桥启动后第一次看到的会话的旧结果（第一拍只记底，不补发历史）
- 解绑过（`on=False`）的群、没绑群的会话
- 同一个会话的「做完」10 分钟最多一次；全部群加起来 1 分钟最多 6 条

这里只做判断；发消息由 `app.py`（`Bridge._group_push_tick`）接线。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

MIN_WAIT_SEC = 20
SETTLE_SEC = 45
DONE_GAP_SEC = 10 * 60
BURST_LIMIT = 6
BURST_WINDOW_SEC = 60
RELAY_SLACK_SEC = 30
EXCERPT_CHARS = 800


@dataclass(frozen=True)
class Push:
    kind: str  # waiting | done | failed
    chat_id: str
    sess: Any
    turn_at: float = 0.0


def _pending_text(sess: Any) -> str:
    pending = getattr(sess, "pending", None)
    if pending is None:
        return ""
    questions = getattr(pending, "questions", None) or ()
    if questions:
        first = questions[0]
        text = str(getattr(first, "question", "") or "")
        options = [str(o[0]) for o in (getattr(first, "options", None) or ()) if o]
        if options:
            text += "\n" + " / ".join(options[:6])
        return text
    tool = str(getattr(pending, "tool", "") or "")
    summary = str(getattr(pending, "summary", "") or "")
    return f"{tool} {summary}".strip()


def pending_key(sess: Any) -> str:
    return _pending_text(sess)[:200]


class GroupPusher:
    """线程安全：`note_relay` 在事件循环线程调，`due` 在后台线程调。"""

    def __init__(self, *, enabled: bool = True, now: Callable[[], float] = time.time,
                 min_wait: float = MIN_WAIT_SEC, settle: float = SETTLE_SEC,
                 done_gap: float = DONE_GAP_SEC, burst: int = BURST_LIMIT,
                 burst_window: float = BURST_WINDOW_SEC) -> None:
        self.enabled = enabled
        self._now = now
        self.min_wait = min_wait
        self.settle = settle
        self.done_gap = done_gap
        self.burst = burst
        self.burst_window = burst_window
        self._lock = threading.Lock()
        self._seen: dict = {}  # key → 第一次看到时它的 last_turn_at（底）
        self._wait_since: dict = {}
        self._wait_done: dict = {}  # key → 推过的那个问题指纹
        self._turn_pushed: dict = {}  # key → 推过的 last_turn_at
        self._done_at: dict = {}  # key → 上次推「做完」的时刻
        self._relays: dict = {}  # key → [(start, end)]
        self._recent: deque = deque()

    def note_relay(self, tool: str, sid: str, start: float, end: Optional[float] = None) -> None:
        """群里发进去的一轮：这段时间里结束的回合不再推（桥已经把回复贴回群里了）。"""
        end = self._now() if end is None else end
        with self._lock:
            spans = [s for s in self._relays.get((tool, sid), []) if self._now() - s[1] < 3600]
            spans.append((float(start), float(end)))
            self._relays[(tool, sid)] = spans[-20:]

    def _relayed(self, key: tuple, turn_at: float) -> bool:
        for start, end in self._relays.get(key, ()):
            if start - 5 <= turn_at <= end + RELAY_SLACK_SEC:
                return True
        return False

    def due(self, view: Any, binds: Any) -> list:
        if not self.enabled or binds is None:
            return []
        now = self._now()
        sessions = list(getattr(view, "sessions", None) or [])
        ok_tools = set(getattr(view, "ok_tools", None) or ())
        out: list = []
        with self._lock:
            while self._recent and now - self._recent[0] >= self.burst_window:
                self._recent.popleft()
            waiting_now = set()
            for sess in sessions:
                key = (sess.tool, sess.id)
                try:
                    bound = binds.of_session(sess.tool, sess.id)
                except Exception:  # noqa: BLE001
                    bound = None
                turn_at = float(getattr(sess, "last_turn_at", 0) or 0)
                first = key not in self._seen
                if first:
                    self._seen[key] = turn_at
                    if getattr(sess, "last_turn", ""):
                        self._turn_pushed[key] = turn_at
                if bound is None or not getattr(bound, "on", True):
                    continue
                if sess.status == "waiting":
                    waiting_now.add(key)
                    sig = pending_key(sess)
                    if key not in self._wait_since:
                        self._wait_since[key] = now
                    if key in self._wait_done and sig and self._wait_done[key] != sig:
                        self._wait_done.pop(key, None)
                        self._wait_since[key] = now
                    if key not in self._wait_done and now - self._wait_since[key] >= self.min_wait:
                        out.append(Push("waiting", bound.chat_id, sess))
                        self._wait_done[key] = sig or "-"
                    continue
                turn = getattr(sess, "last_turn", "")
                if turn not in ("done", "failed") or not turn_at:
                    continue
                if turn_at <= self._turn_pushed.get(key, 0):
                    continue
                if self._relayed(key, turn_at):
                    self._turn_pushed[key] = turn_at
                    continue
                if turn == "failed":
                    out.append(Push("failed", bound.chat_id, sess, turn_at))
                    self._turn_pushed[key] = turn_at
                    continue
                if now - turn_at < self.settle:
                    continue
                if now - self._done_at.get(key, -1e18) < self.done_gap:
                    continue
                out.append(Push("done", bound.chat_id, sess, turn_at))
                self._turn_pushed[key] = turn_at
                self._done_at[key] = now
            for key in list(self._wait_since):
                if key not in waiting_now and key[0] in ok_tools | {s.tool for s in sessions}:
                    self._wait_since.pop(key, None)
                    self._wait_done.pop(key, None)
            room = max(0, self.burst - len(self._recent))
            picked = out[:room]
            for _ in picked:
                self._recent.append(now)
            for push in out[room:]:
                key = (push.sess.tool, push.sess.id)
                if push.kind == "waiting":
                    self._wait_done.pop(key, None)
                else:
                    self._turn_pushed[key] = self._seen.get(key, 0) if push.turn_at else 0
                    if push.kind == "done":
                        self._done_at.pop(key, None)
            return picked


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def build_push_text(push: Push, label: str, project: str, last_reply: str = "",
                    redact: Optional[Callable[[str], str]] = None) -> str:
    clean = redact or (lambda s: s)
    where = f"📁 {project}" if project else ""
    if push.kind == "waiting":
        ask = _clip(clean(_pending_text(push.sess)), 300)
        lines = [f"🟡 **{label}在等你处理**", where]
        if ask:
            lines.append(f"它卡在：{ask}")
        lines.append("需要人介入：到电脑上处理那一步，或在群里回一句给它。")
    elif push.kind == "failed":
        lines = [f"❌ **{label}这一轮出错了**", where, "需要人看一下：在群里回一句让它重试，或到电脑上看。"]
    else:
        lines = [f"✅ **{label}做完一轮，停下等下一步**", where]
        reply = _clip(clean(last_reply), EXCERPT_CHARS)
        if reply:
            lines.append("它最后说：\n" + reply)
        lines.append("在群里直接回话就能接着给它派活。")
    return "\n".join(line for line in lines if line)


# ---------- 重要进展（§5.8：计划确定 / 阶段完成 / 产出文档）----------


MILESTONE_GAP_SEC = 10 * 60
MILESTONE_CHARS = 90
# 认出「值得说一句」的句子。宁少不多：只是过程描述（跑测试、改文件）不推。
_MILESTONE_WORDS = (
    "计划", "方案", "阶段", "已完成", "完成了", "搞定了", "修好了", "已修复",
    "已生成", "已导出", "已部署", "已提交", "文档已", "结论",
)


def milestone_of(text: str) -> str:
    """从一段回复里挑一句「重要进展」；没有 ⇒ 空串。

    只认短句（一行一条），太长的是解释过程，不适合推。命中词见 `_MILESTONE_WORDS`。
    """
    for raw in str(text or "").splitlines():
        line = " ".join(raw.split()).strip(" \t#*-•>")
        if not line or len(line) > MILESTONE_CHARS:
            continue
        if line.endswith("：") or line.endswith(":"):
            continue
        if any(word in line for word in _MILESTONE_WORDS):
            return line
    return ""


class Milestones:
    """同一会话 10 分钟最多推一条；同一句话不重复推。"""

    def __init__(self, *, gap: float = MILESTONE_GAP_SEC, now: Callable[[], float] = time.time) -> None:
        self.gap = float(gap)
        self._now = now
        self._lock = threading.Lock()
        self._last: dict = {}  # key -> (句子, 时间)

    def due(self, key: tuple, text: str) -> str:
        line = milestone_of(text)
        if not line:
            return ""
        now = self._now()
        with self._lock:
            prev = self._last.get(key)
            if prev is not None and (prev[0] == line or now - prev[1] < self.gap):
                return ""
            self._last[key] = (line, now)
        return line
