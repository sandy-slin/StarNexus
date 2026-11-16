"""会话群的置顶状态卡（产品定义 §5.1 / §6，验收 FC-01、FC-04）。

一个会话群一张状态卡：工具、模型 / effort、项目、权限档位、状态、最近进展，
外加会话级按钮（🧠 模型 / ⏹ 停下 / ✅ 完成 / 🔄 刷新）。建群后发一次并置顶，
之后**原地更新**同一张卡（不新增消息），message_id 落盘，重启后还能接着更新。

置顶要 `im:message.pins` 一类权限；没开就只发不置顶，卡照样能用。
卡片回调值只带 `{"act": "sc", "op": ...}`，会话由服务端按「点的是哪个群」现查绑定。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ACT = "sc"
OPS = ("refresh", "done", "reopen")
STATE_FILE = "status_cards.json"
DONE_PREFIX = "✅"

STATUS_TEXT = {
    "running": "🟢 正在跑",
    "waiting": "🟡 等你处理",
    "idle": "⚪ 闲着，等下一步",
    "stopped": "⚪ 停着",
    "unknown": "⚪ 状态未知",
}
TURN_TEXT = {"done": "✅ 上一轮完成", "failed": "❌ 上一轮出错"}
TOOL_LABELS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}


@dataclass(frozen=True)
class CardInfo:
    tool: str
    sid: str
    title: str = ""
    project: str = ""
    model: str = ""
    mode: str = ""
    status: str = "unknown"
    last_turn: str = ""
    last_turn_at: float = 0.0
    progress: str = ""
    finished: bool = False

    def signature(self) -> str:
        return json.dumps([self.model, self.mode, self.status, self.last_turn, self.last_turn_at,
                           self.progress, self.finished, self.title, self.project], ensure_ascii=False)


def value(op: str) -> dict:
    return {"act": ACT, "op": op}


def resolve_click(raw: Any) -> str:
    """回调值 → op；形状不对 ⇒ 空串（多一个键都不认）。"""
    if not isinstance(raw, dict) or raw.get("act") != ACT or set(raw) != {"act", "op"}:
        return ""
    op = raw.get("op")
    return op if op in OPS else ""


def _button(label: str, val: dict, primary: bool = False) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": val}],
    }


def _rows(buttons: list, per: int = 2) -> list:
    out = []
    for i in range(0, len(buttons), per):
        out.append({
            "tag": "column_set", "horizontal_spacing": "8px",
            "columns": [{"tag": "column", "width": "weighted", "weight": 1, "elements": [b]}
                        for b in buttons[i:i + per]],
        })
    return out


def _clock(ts: float) -> str:
    if not ts:
        return ""
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def build_card(info: CardInfo) -> str:
    tool = TOOL_LABELS.get(info.tool, info.tool)
    if info.finished:
        state = "✅ 已完成（在群里再说一句就能接着做）"
    else:
        state = STATUS_TEXT.get(info.status, STATUS_TEXT["unknown"])
    lines = [
        f"**工具**：{tool}",
        f"**模型**：{info.model or '工具默认'}",
        f"**项目**：{info.project or '未知'}",
        f"**权限**：{info.mode or '未知'}",
        f"**状态**：{state}",
    ]
    turn = TURN_TEXT.get(info.last_turn, "")
    if turn:
        lines.append(f"**最近一轮**：{turn}" + (f"（{_clock(info.last_turn_at)}）" if info.last_turn_at else ""))
    if info.progress:
        lines.append(f"**最近进展**：{info.progress}")
    lines.append("")
    lines.append("💬 继续：直接在群里说话，就发给这个会话。")
    buttons = [
        _button("🧠 模型", {"act": "mdl", "op": "home", "to": "s"}),
        _button("⏹ 停下", {"act": "do", "op": "stop"}),
        _button("↩︎ 重新打开" if info.finished else "✅ 完成", value("reopen" if info.finished else "done")),
        _button("🔄 刷新", value("refresh")),
    ]
    title = f"{tool}「{info.title or info.sid[:8]}」"
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": title[:60]},
            "subtitle": {"tag": "plain_text", "content": "会话状态卡"},
            "template": "green" if info.finished else ("orange" if info.status == "waiting" else "blue"),
        },
        "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)}] + _rows(buttons)},
    }
    return json.dumps(card, ensure_ascii=False)


class StatusCards:
    """chat_id → {message_id, pinned, finished, sig}。线程安全，落盘。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._rows: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:  # noqa: BLE001
            logger.exception("状态卡记录读不出来，当成没有")
            return
        if isinstance(raw, dict):
            self._rows = {k: v for k, v in raw.items() if isinstance(v, dict) and str(k).startswith("oc_")}

    def _save(self) -> None:
        if not self.path:
            return
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".status_cards.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._rows, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            logger.exception("状态卡记录写不进去")

    def get(self, chat_id: str) -> dict:
        with self._lock:
            return dict(self._rows.get(chat_id) or {})

    def put(self, chat_id: str, **fields: Any) -> None:
        with self._lock:
            row = dict(self._rows.get(chat_id) or {})
            row.update(fields)
            self._rows[chat_id] = row
            self._save()

    def forget(self, chat_id: str) -> None:
        with self._lock:
            if self._rows.pop(chat_id, None) is not None:
                self._save()


class StatusCardKeeper:
    """发 / 置顶 / 原地更新。外部依赖全部注入，便于测试。

    - `send(chat_id, card_json) -> message_id | None`
    - `patch(message_id, card_json) -> bool`
    - `pin(message_id) -> (ok, code, msg)`；可以是 None（不置顶）
    """

    def __init__(self, store: StatusCards, send: Callable, patch: Callable,
                 pin: Optional[Callable] = None) -> None:
        self.store = store
        self._send = send
        self._patch = patch
        self._pin = pin
        self._lock = threading.Lock()

    def finished(self, chat_id: str) -> bool:
        return bool(self.store.get(chat_id).get("finished"))

    def set_finished(self, chat_id: str, on: bool) -> None:
        self.store.put(chat_id, finished=bool(on))

    def ensure(self, chat_id: str, info: CardInfo, *, force: bool = False) -> str:
        """有卡就原地更新（内容没变不打扰），没卡或更新失败就新发并置顶。返回做了什么。"""
        with self._lock:
            row = self.store.get(chat_id)
            card = build_card(info)
            sig = info.signature()
            mid = row.get("message_id") or ""
            if mid:
                if not force and row.get("sig") == sig:
                    return "same"
                try:
                    ok = bool(self._patch(mid, card))
                except Exception:  # noqa: BLE001
                    logger.exception("状态卡原地更新抛异常")
                    ok = False
                if ok:
                    self.store.put(chat_id, sig=sig)
                    return "patched"
                logger.info("状态卡原地更新失败，重新发一张 chat=%s", chat_id[:12])
            try:
                new_mid = self._send(chat_id, card)
            except Exception:  # noqa: BLE001
                logger.exception("状态卡发送抛异常")
                new_mid = None
            if not new_mid:
                return "failed"
            pinned = False
            if self._pin is not None:
                try:
                    pinned, code, msg = self._pin(new_mid)
                    if not pinned:
                        logger.info("状态卡没置顶上 code=%s msg=%s", code, str(msg)[:80])
                except Exception:  # noqa: BLE001
                    logger.exception("状态卡置顶抛异常")
                    pinned = False
            self.store.put(chat_id, message_id=new_mid, pinned=bool(pinned), sig=sig)
            return "sent_pinned" if pinned else "sent"
