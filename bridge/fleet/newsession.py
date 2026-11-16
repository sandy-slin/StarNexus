"""手机上「➕ 新建会话」：在电脑上新开一个 Claude / Codex 会话，之后就在飞书里接着聊。

## 流程（三步，全是按钮）

1. 点「➕ 新建会话」（或发「新建会话」/ `/newsession` / `/ns`）⇒ 选工具卡：Claude / Codex。
2. 点一个工具 ⇒ 选项目卡：当前项目 + 白名单里最近动过的仓库（`workspace.recent_repos`）。
3. 点一个项目 ⇒ **下一句话**就是新会话的第一句。开好之后自动进对话模式，后面每句走现有的
   `control.send(relay=True)` 管道（Claude / Codex 都按当前权限档位）。

## 回调值（白名单，同 `actions.resolve_do_click` 的思路）

- `{"act": "new", "op": "tool", "tool": <claude|codex>}`
- `{"act": "new", "op": "dir", "n": <服务端发的序号>, "i": <第几个>}`

🔴 value 来自飞书、理论上能伪造 ⇒ **目录不放进卡片**，卡上只带序号；要开在哪个目录记在服务端
（`NewSessionState`），序号对不上 / 过期 / 多带键一律不认。点了之后 app 还会再过一次
`policy.resolve_workspace`（白名单可能在这期间被改小）。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Callable, Optional

from .model import TOOLS
from .scan import TOOL_LABELS

ACT = "new"
OFFER_TTL_SEC = 10 * 60
ARMED_TTL_SEC = 10 * 60
MAX_DIRS = 8
# 新会话那一句在飞时 `_fleet_inflight` 里的占位 id（真 id 要等它回来才知道）
INFLIGHT_ID = "(new)"

COMMANDS = frozenset({"/newsession", "/ns", "/新建会话"})
WORDS = frozenset({"新建会话", "新会话", "开新会话"})

START_TOOLS = ("claude", "codex", "opencode")
TOOL_ALIASES = {"claude": "claude", "cc": "claude", "codex": "codex", "cx": "codex", "opencode": "opencode",
                "oc": "opencode"}
MODEL_ALIASES = {
    "claude": {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5",
               "claude-sonnet-5-5": "claude-sonnet-5-5", "claude-opus-5-5": "claude-opus-5-5",
               "claude-haiku-5-5": "claude-haiku-5-5"},
    "codex": {"sol": "gpt-6-sol", "luna": "gpt-6-luna", "gpt-6-sol": "gpt-6-sol", "gpt-6-luna": "gpt-6-luna"},
}


def pick_model(tool: str, word: str) -> str:
    """`opus` / `luna` / 全名 → 这家认的模型名；认不出 ⇒ 空串（用默认模型）。"""
    return MODEL_ALIASES.get(tool, {}).get(str(word or "").strip().lower(), "")


EFFORT_ALIASES = {
    "low": "low", "低": "low", "medium": "medium", "med": "medium", "中": "medium",
    "high": "high", "高": "high", "xhigh": "xhigh", "很高": "xhigh",
    "max": "max", "最高": "max", "ultra": "ultra", "极限": "ultra",
}
EFFORTS_BY_TOOL = {"claude": ("low", "medium", "high", "xhigh", "max"),
                   "codex": ("low", "medium", "high", "xhigh", "max", "ultra")}


def pick_effort(tool: str, word: str) -> str:
    """`high` / `高` → 这家认的推理强度；认不出或这家不支持 ⇒ 空串。"""
    effort = EFFORT_ALIASES.get(str(word or "").strip().lower(), "")
    return effort if effort in EFFORTS_BY_TOOL.get(tool, ()) else ""


def model_hint(tool: str) -> str:
    names = sorted({v for v in MODEL_ALIASES.get(tool, {}).values()})
    return " / ".join(names)


TOOL_HINTS = {
    "claude": "和终端一样按当前权限干活",
    "codex": "和终端一样按当前权限干活",
    "opencode": "第三个工具（试用）：按当前权限自动批准，模型用它自己的默认",
}


def tool_value(tool: str) -> dict:
    if tool not in START_TOOLS:
        raise ValueError(f"不认识的工具：{tool!r}")
    return {"act": ACT, "op": "tool", "tool": tool}


def dir_value(nonce: str, index: int) -> dict:
    return {"act": ACT, "op": "dir", "n": str(nonce), "i": str(int(index))}


def resolve_click(value: Any) -> Optional[dict]:
    """回调 value → {"op": "tool", "tool"} 或 {"op": "dir", "n", "i"}；形状不对 ⇒ None。"""
    if not isinstance(value, dict) or value.get("act") != ACT:
        return None
    op = value.get("op")
    if op == "tool":
        tool = value.get("tool")
        if set(value) != {"act", "op", "tool"} or not isinstance(tool, str) or tool not in START_TOOLS:
            return None
        return {"op": "tool", "tool": tool}
    if op == "dir":
        if set(value) != {"act", "op", "n", "i"}:
            return None
        nonce, idx = value.get("n"), value.get("i")
        if not isinstance(nonce, str) or not nonce or len(nonce) > 32:
            return None
        if not isinstance(idx, str) or not idx.isdigit() or len(idx) > 2:
            return None
        return {"op": "dir", "n": nonce, "i": int(idx)}
    return None


class NewSessionState:
    """每个聊天（scope）一份：发出去的项目选项（offer）+ 选好了、等第一句话的那个（armed）。只在内存里。"""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._offers: dict = {}  # scope → {"n", "tool", "dirs", "expires"}
        self._armed: dict = {}  # scope → {"tool", "cwd", "expires"}

    def offer(self, scope: str, tool: str, dirs: list) -> str:
        nonce = secrets.token_hex(6)
        with self._lock:
            self._offers[scope] = {"n": nonce, "tool": tool, "dirs": list(dirs),
                                   "expires": self._now() + OFFER_TTL_SEC}
        return nonce

    def take(self, scope: str, nonce: str, index: int) -> tuple:
        """(tool, 目录) 或 (None, 为什么不行)。对上了就把这份 offer 用掉。"""
        with self._lock:
            got = self._offers.get(scope)
            if got is None or got["n"] != nonce:
                return None, "这张卡过期了，点「➕ 新建会话」重新选"
            if self._now() > got["expires"]:
                self._offers.pop(scope, None)
                return None, "这张卡超过 10 分钟了，点「➕ 新建会话」重新选"
            if not 0 <= index < len(got["dirs"]):
                return None, "这个选项认不出来，点「➕ 新建会话」重新选"
            self._offers.pop(scope, None)
            return got["tool"], got["dirs"][index]

    def arm(self, scope: str, tool: str, cwd: str, model: str = "", effort: str = "", fork_from: str = "") -> None:
        with self._lock:
            self._armed[scope] = {"tool": tool, "cwd": cwd, "model": model, "effort": effort,
                                  "fork_from": fork_from, "expires": self._now() + ARMED_TTL_SEC}

    def peek(self, scope: str) -> tuple:
        """(没过期的 armed 或 None, 是否过期)。过期的顺手清掉。"""
        with self._lock:
            got = self._armed.get(scope)
            if got is None:
                return None, False
            if self._now() > got["expires"]:
                self._armed.pop(scope, None)
                return None, True
            return dict(got), False

    def disarm(self, scope: str) -> bool:
        """清掉这个聊天的选项和等第一句的那个；原来有东西 ⇒ True。"""
        with self._lock:
            offer = self._offers.pop(scope, None)
            armed = self._armed.pop(scope, None)
        return offer is not None or armed is not None


# ---------- 卡片 ----------


def _button(label: str, value: dict, primary: bool = False) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _card(title: str, elements: list) -> str:
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": "turquoise"},
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )


def build_tool_card(cancel_button: dict, tools: Optional[tuple] = None) -> str:
    """第一步：用哪个工具开。`tools`：这台电脑上真接上了的（默认全部）。"""
    from .actions import button_rows

    shown = tuple(t for t in START_TOOLS if tools is None or t in tools)
    lines = [f"· **{TOOL_LABELS[t]}** — {TOOL_HINTS[t]}" for t in shown]
    elements: list = [
        {"tag": "markdown", "content": "在电脑上新开一个会话，用哪个？\n" + "\n".join(lines)},
    ]
    elements.extend(button_rows([_button(TOOL_LABELS[t], tool_value(t), primary=(t == "claude"))
                                 for t in shown]))
    elements.extend(button_rows([cancel_button]))
    return _card("➕ 新建会话", elements)


def build_dir_card(tool: str, labels: list, nonce: str, cancel_button: dict) -> str:
    """第二步：开在哪个项目。`labels` 是调用方**脱敏过**的短路径，第一个是当前项目。"""
    from .actions import button_rows

    elements: list = [{"tag": "markdown",
                       "content": f"新的 **{TOOL_LABELS.get(tool, tool)}** 会话开在哪个项目？（最近动过的在前）"}]
    buttons = []
    for i, label in enumerate(labels):
        text = f"📂 {label}" + ("（当前）" if i == 0 else "")
        buttons.append(_button(text, dir_value(nonce, i), primary=(i == 0)))
    for button in buttons:
        elements.extend(button_rows([button]))
    elements.append({"tag": "markdown", "text_size": "notation",
                     "content": "要的不在这里：发 `/newsession " + tool + " 项目名`。"})
    elements.extend(button_rows([cancel_button]))
    return _card(f"➕ 新建 {TOOL_LABELS.get(tool, tool)} 会话", elements)


def permission_line(tool: str, mode_label: str) -> str:
    if tool == "claude":
        return f"· 权限按当前「{mode_label}」档位，和终端一样"
    return f"· {TOOL_LABELS.get(tool, tool)} 按当前「{mode_label}」档位，和终端一样"
