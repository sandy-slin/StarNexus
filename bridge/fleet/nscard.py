"""一张卡开新会话（1.1.0，林森 11-17 定义）。

卡上三行（全部在 form 外 ⇒ 每个下拉选了就回调、输入框回车就发）：

1. 【工具 ▾】（默认上次用的）【项目 ▾】（默认最近用的，末项「➕ 新建项目」）
   选了「➕ 新建项目」⇒ 原地换卡，项目那一格直接换成项目名输入框（不再是下拉 + 下面再一个框）；
   底下多一个「📂 选已有项目」按钮换回下拉
2. 【模型 ▾】（默认「缺省（具体名）」，随工具变）【强度 ▾】（默认「缺省」，随工具 / 模型变）
3. 【第一句话（回车就开）】【▶ 开始】 —— 空着点「开始」⇒ 第一句用「hi」

## 回调值（白名单）

`{"act": "nsc", "op": <OPS>, "n": <服务端草稿序号>}`。工具 / 项目 / 模型 / 强度的**值**来自
`action.option`（下拉选项的 value），输入框的字来自 `action.input_value`。
下拉选项的 value 只有枚举词或下标：工具是 `claude|codex|opencode`，项目是 `p<i>` / `new`，
模型是 `m<i>` / `def`，强度是 `e<i>` / `def`。路径、模型全名都只在服务端草稿里 ⇒
伪造的回调最多是「换成另一个已列出来的选项」。

## 为什么「开始」按钮不能读到输入框里的字

飞书客户端（本机 Lark messenger.asar）的 input 只在**回车**时回调 `input_value`；
在 form 外，按钮点击不带输入框的值。所以：打了字 ⇒ 回车开；没打字 ⇒ 点「开始」用「hi」开。
卡上那一行小字把这一点写明，不让人以为打了字再点按钮能发出去。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ACT = "nsc"
OPS = frozenset({"tool", "proj", "model", "effort", "pname", "back", "send", "go", "cancel"})
TOOLS = ("claude", "codex", "opencode")
TOOL_NAMES = {"claude": "Claude", "codex": "Codex", "opencode": "opencode"}
DEFAULT_PROMPT = "hi"
DRAFT_TTL_SEC = 30 * 60
MAX_PROJECTS = 12
MAX_MODELS = 12
MAX_PROMPT_CHARS = 1000
MAX_PROJECT_NAME = 48
PROJECT_CELLS = 30
TEXT_INPUT = "nsc_text"
NAME_INPUT = "nsc_pname"
NEW_PROJECT = "new"
DEFAULT_OPT = "def"


@dataclass
class Draft:
    """一张新会话卡在服务端记着的东西。卡上只有下标。"""

    nonce: str
    scope: str
    tool: str
    projects: list                 # [(真实路径, 显示名)]
    proj: Optional[int] = 0        # 选中的下标；None = 选了「新建项目」
    new_name: str = ""             # 「新建项目」时填的名字
    model: str = ""                # 空 = 缺省
    effort: str = ""               # 空 = 缺省
    models: list = field(default_factory=list)   # 当前工具的模型全名
    expires: float = 0.0
    started: bool = False


class DraftStore:
    """每个聊天一张草稿（新发一张卡就顶掉旧的）。只在内存。"""

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._lock = threading.Lock()
        self._by_nonce: dict = {}
        self._by_scope: dict = {}

    def new(self, scope: str, tool: str, projects: list, models: list, proj: Optional[int] = 0) -> Draft:
        draft = Draft(secrets.token_hex(6), scope, tool, list(projects)[:MAX_PROJECTS],
                      proj if projects else None, models=list(models)[:MAX_MODELS],
                      expires=self._now() + DRAFT_TTL_SEC)
        with self._lock:
            old = self._by_scope.pop(scope, None)
            if old is not None:
                self._by_nonce.pop(old, None)
            self._by_scope[scope] = draft.nonce
            self._by_nonce[draft.nonce] = draft
        return draft

    def get(self, scope: str, nonce: str) -> tuple:
        """(草稿, "") 或 (None, 为什么不行)。"""
        with self._lock:
            draft = self._by_nonce.get(nonce)
            if draft is None or draft.scope != scope:
                return None, "这张卡已经过期了，点「➕ 新建会话」再开一张"
            if self._now() > draft.expires:
                self._drop(draft)
                return None, "这张卡超过 30 分钟了，点「➕ 新建会话」再开一张"
            draft.expires = self._now() + DRAFT_TTL_SEC
            return draft, ""

    def finish(self, draft: Draft) -> None:
        with self._lock:
            self._drop(draft)

    def _drop(self, draft: Draft) -> None:
        self._by_nonce.pop(draft.nonce, None)
        if self._by_scope.get(draft.scope) == draft.nonce:
            self._by_scope.pop(draft.scope, None)


def value(op: str, nonce: str) -> dict:
    if op not in OPS:
        raise ValueError(f"不认识的新会话卡 op：{op!r}")
    return {"act": ACT, "op": op, "n": str(nonce)}


def resolve_click(raw: Any, option: Any = None, text: Any = None) -> Optional[dict]:
    """回调 → {"op", "n", "pick", "text"}；形状不对 ⇒ None。`pick` 是下拉选中的 value（已核形状）。"""
    if not isinstance(raw, dict) or raw.get("act") != ACT or set(raw) != {"act", "op", "n"}:
        return None
    op, nonce = raw.get("op"), raw.get("n")
    if op not in OPS or not isinstance(nonce, str) or not nonce.isalnum() or len(nonce) > 32:
        return None
    pick = option if isinstance(option, str) else ""
    if op == "tool" and pick not in TOOLS:
        return None
    if op == "proj" and not (pick == NEW_PROJECT or _index(pick, "p") is not None):
        return None
    if op == "model" and not (pick == DEFAULT_OPT or _index(pick, "m") is not None):
        return None
    if op == "effort" and not (pick == DEFAULT_OPT or _index(pick, "e") is not None):
        return None
    words = text.strip()[:MAX_PROMPT_CHARS] if isinstance(text, str) else ""
    return {"op": op, "n": nonce, "pick": pick, "text": words}


def _index(pick: str, prefix: str) -> Optional[int]:
    if not isinstance(pick, str) or not pick.startswith(prefix):
        return None
    digits = pick[len(prefix):]
    if not digits.isdigit() or len(digits) > 2:
        return None
    return int(digits)


def pick_index(pick: str, prefix: str) -> Optional[int]:
    return _index(pick, prefix)


def effort_label(effort: str) -> str:
    from .catalog import EFFORT_LABELS

    return f"{EFFORT_LABELS.get(effort, effort)}（{effort}）"


# ---------- 卡片 ----------


def _select(placeholder: str, options: list, initial: str, op: str, nonce: str) -> dict:
    node = {
        "tag": "select_static", "width": "fill",
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "options": [{"text": {"tag": "plain_text", "content": text}, "value": val} for text, val in options],
        "behaviors": [{"type": "callback", "value": value(op, nonce)}],
    }
    if initial and any(val == initial for _, val in options):
        node["initial_option"] = initial
    return node


def _input(name: str, placeholder: str, op: str, nonce: str, default: str = "",
           max_length: int = MAX_PROMPT_CHARS) -> dict:
    node = {
        "tag": "input", "name": name, "width": "fill", "max_length": max_length,
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "behaviors": [{"type": "callback", "value": value(op, nonce)}],
    }
    if default:
        node["default_value"] = default
    return node


def _button(label: str, op: str, nonce: str, primary: bool = False) -> dict:
    return {
        "tag": "button", "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default", "width": "fill", "size": "medium",
        "behaviors": [{"type": "callback", "value": value(op, nonce)}],
    }


def _row(*cells: tuple) -> dict:
    return {"tag": "column_set", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "weighted", "weight": weight, "vertical_align": "center", "elements": [node]}
        for node, weight in cells]}


def _md(text: str, small: bool = False) -> dict:
    node = {"tag": "markdown", "content": text}
    if small:
        node["text_size"] = "notation"
    return node


def _card(title: str, template: str, elements: list) -> dict:
    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "body": {"elements": elements}}


def build_card(draft: Draft, tools: tuple, *, default_model: str, efforts: tuple, default_effort: str = "",
               escape: Callable[[str], str] = lambda s: s, warn: str = "") -> dict:
    """一张卡：工具/项目 · 模型/强度 · 第一句话/开始。`default_model`：缺省时实际用的模型名（显示用）。"""
    from .cards import clip_cells

    nonce = draft.nonce
    shown_tools = [t for t in TOOLS if t in tools] or [draft.tool]
    tool_sel = _select("工具", [(TOOL_NAMES[t], t) for t in shown_tools], draft.tool, "tool", nonce)
    proj_opts = [(clip_cells(name, PROJECT_CELLS), f"p{i}") for i, (_path, name) in enumerate(draft.projects)]
    proj_opts.append(("➕ 新建项目", NEW_PROJECT))
    proj_init = NEW_PROJECT if draft.proj is None else f"p{draft.proj}"
    if draft.proj is None:
        proj_cell = _input(NAME_INPUT, "新项目名，回车", "pname", nonce, draft.new_name, MAX_PROJECT_NAME)
    else:
        proj_cell = _select("项目", proj_opts, proj_init, "proj", nonce)
    elements: list = [_row((tool_sel, 2), (proj_cell, 3))]
    if draft.proj is None and draft.new_name:
        elements.append(_md(f"新项目：**{escape(draft.new_name)}**（开的时候建在 ~/work 下）", small=True))
    default_text = f"缺省（{default_model}）" if default_model else "缺省"
    model_opts = [(default_text, DEFAULT_OPT)] + [(clip_cells(m, PROJECT_CELLS), f"m{i}")
                                                  for i, m in enumerate(draft.models)]
    model_init = f"m{draft.models.index(draft.model)}" if draft.model in draft.models else DEFAULT_OPT
    model_sel = _select("模型", model_opts, model_init, "model", nonce)
    if efforts:
        eff_default = f"缺省（{effort_label(default_effort)}）" if default_effort else "缺省"
        eff_opts = [(eff_default, DEFAULT_OPT)] + [(effort_label(e), f"e{i}") for i, e in enumerate(efforts)]
        eff_init = f"e{efforts.index(draft.effort)}" if draft.effort in efforts else DEFAULT_OPT
        eff_sel = _select("强度", eff_opts, eff_init, "effort", nonce)
        elements.append(_row((model_sel, 3), (eff_sel, 2)))
    else:
        elements.append(_row((model_sel, 1)))
    if warn:
        elements.append(_md(f"⚠️ {escape(warn)}"))
    elements.append(_row((_input(TEXT_INPUT, "第一句话，回车就开", "send", nonce), 3),
                         (_button("▶ 开始", "go", nonce, primary=True), 1)))
    elements.append(_md("打了字按回车开；不打字直接点「开始」，第一句用「hi」。开好会自动建群，"
                        "权限和终端一样。", small=True))
    if draft.proj is None and draft.projects:
        elements.append(_row((_button("📂 选已有项目", "back", nonce), 1), (_button("✖️ 不建了", "cancel", nonce), 1)))
    else:
        elements.append(_row((_button("✖️ 不建了", "cancel", nonce), 1)))
    return _card("➕ 新建会话", "turquoise", elements)


def build_starting_card(tool: str, project: str, model_text: str, prompt: str,
                        escape: Callable[[str], str] = lambda s: s) -> dict:
    """回车 / 开始之后原地换成这张：没有输入框（防止再回车又开一个）。"""
    from .model import short_title

    lines = [f"正在新开 **{TOOL_NAMES.get(tool, tool)}** 会话……",
             f"项目：{escape(project)}", f"模型：{escape(model_text)}",
             "第一句：" + escape(short_title(prompt, 60))]
    return _card("⏳ 正在开新会话", "wathet", [_md("\n".join(lines)),
                                             _md("开好会建一个群，回复发到群里；这里留一个「进群」按钮。", small=True)])


def build_cancelled_card() -> dict:
    return _card("➕ 新建会话", "grey", [_md("好，不建了。")])


def dumps(card: dict) -> str:
    return json.dumps(card, ensure_ascii=False)
