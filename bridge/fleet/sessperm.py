"""每个会话自己的权限档位（1.3.0）。

规则（林森 11-18）：
- 从飞书开的会话（一张卡 / `/ns` / CEO 子任务）一律「全部授权」，开好就记下来。
- 其他会话（电脑上原有的）建群时给三个按钮：只读 / 可写 / 全部授权；没选过就跟随全局档位。
- 在会话群里切权限（按钮或 `/权限 只读|可写|全部授权`）只改这个群绑定的会话，不动全局。

落盘 `session_perms.json`：{"tool:sid": {"mode": ..., "origin": "feishu"|"group", "at": ...}}。
档位最终仍被本机上限（`Policy.with_mode` 的 ceiling）压住，聊天里提不过 config.toml。
卡片回调只带 `{"act": "sp", "m": 档位}`，会话由服务端按「点的是哪个群」现查绑定。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("feishu-bridge.sessperm")

ACT = "sp"
MODES = ("readonly", "write", "trusted")
LABELS = {"readonly": "只读", "write": "可写", "trusted": "全部授权"}
EXPLAIN = {
    "readonly": "只看、只回答，不改文件",
    "write": "能改项目里的文件，项目外和高风险操作先问你",
    "trusted": "和终端一样，能读写和执行",
}
ALIASES = {
    "r": "readonly", "w": "write", "f": "trusted",
    "只读": "readonly", "readonly": "readonly", "ro": "readonly",
    "可写": "write", "可改": "write", "可改文件": "write", "write": "write", "w": "write",
    "全部授权": "trusted", "全授权": "trusted", "全开": "trusted", "trusted": "trusted",
}
ORIGIN_FEISHU = "feishu"
ORIGIN_GROUP = "group"
MAX_ROWS = 500
COMMAND_HINT = "发「/p r」只读、「/p w」可写、「/p f」全部授权也能切（「/权限 只读」等中文照样认）"


def parse_mode(word: str) -> Optional[str]:
    return ALIASES.get(str(word or "").strip().lower())


class SessionPerms:
    """会话 → 档位。线程安全，原子写盘；读坏了当成空表（于是跟随全局档位）。"""

    def __init__(self, path: str, *, now=time.time) -> None:
        self.path = path
        self._now = now
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
            logger.exception("会话权限记录读不出来，当成没有（跟随全局档位）")
            return
        if isinstance(raw, dict):
            self._rows = {str(k): v for k, v in raw.items()
                          if isinstance(v, dict) and v.get("mode") in MODES}

    def _save(self) -> bool:
        if not self.path:
            return False
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".session_perms.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._rows, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
            return True
        except Exception:  # noqa: BLE001
            logger.exception("会话权限记录写不进去")
            return False

    @staticmethod
    def key(tool: str, sid: str) -> str:
        return f"{tool}:{sid}"

    def get(self, tool: str, sid: str) -> Optional[dict]:
        if not sid:
            return None
        with self._lock:
            row = self._rows.get(self.key(tool, sid))
            return dict(row) if row else None

    def mode(self, tool: str, sid: str) -> str:
        """这个会话自己选的档位；没选过 ⇒ 空串（调用方跟随全局档位）。"""
        row = self.get(tool, sid)
        return row["mode"] if row else ""

    def from_feishu(self, tool: str, sid: str) -> bool:
        row = self.get(tool, sid)
        return bool(row and row.get("origin") == ORIGIN_FEISHU)

    def set(self, tool: str, sid: str, mode: str, origin: str = ORIGIN_GROUP) -> bool:
        if not sid or mode not in MODES:
            return False
        with self._lock:
            key = self.key(tool, sid)
            old = self._rows.pop(key, None) or {}
            self._rows[key] = {"mode": mode, "origin": old.get("origin") or origin, "at": self._now()}
            while len(self._rows) > MAX_ROWS:
                self._rows.pop(next(iter(self._rows)))
            return self._save()

    def mark_feishu(self, tool: str, sid: str) -> bool:
        """飞书上新开的会话：全部授权，并记下来源（建群时就不再给切换按钮）。"""
        if not sid:
            return False
        with self._lock:
            self._rows.pop(self.key(tool, sid), None)
            self._rows[self.key(tool, sid)] = {"mode": "trusted", "origin": ORIGIN_FEISHU, "at": self._now()}
            return self._save()


# ---------- 卡片 ----------


def value(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(mode)
    return {"act": ACT, "m": mode}


def resolve_click(raw: Any) -> Optional[str]:
    """回调 → 档位；形状不对 ⇒ None。只认 {"act": "sp", "m": 三档之一}，多一个键都不认。"""
    if not isinstance(raw, dict) or raw.get("act") != ACT or set(raw) != {"act", "m"}:
        return None
    mode = raw.get("m")
    return mode if mode in MODES else None


def buttons(current: str) -> list:
    """三个按钮，当前档打 ✅ 并高亮。"""
    out = []
    for mode in MODES:
        on = mode == current
        out.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": ("✅ " if on else "") + LABELS[mode]},
            "type": "primary" if on else "default",
            "width": "fill", "size": "medium",
            "behaviors": [{"type": "callback", "value": value(mode)}],
        })
    return out


def card(current: str, label: str, follows_global: bool = False, note: str = "") -> dict:
    lines = [f"**{label}** 现在是「{LABELS.get(current, current)}」"
             + ("（跟随全局）" if follows_global else "") + f"：{EXPLAIN.get(current, '')}"]
    lines += [f"· {LABELS[m]}：{EXPLAIN[m]}" for m in MODES]
    if note:
        lines.append(note)
    lines.append(f"只改这个会话，下一轮生效。{COMMAND_HINT}。")
    return {
        "schema": "2.0", "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "🔑 这个会话的权限"}, "template": "orange"},
        "body": {"elements": [
            {"tag": "markdown", "content": "\n".join(lines)},
            {"tag": "column_set", "horizontal_spacing": "8px", "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]} for b in buttons(current)]},
        ]},
    }
