"""「🧠 模型」卡片：按工具设默认模型 / effort，会话群里给这个会话单独设（产品定义 §5.4）。

卡片上只带工具名、序号和 effort 名；真正的模型名由服务端按本机目录现查，
会话由服务端按「点的是哪个群」现查绑定，绝不信卡片上的会话 id。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .catalog import EFFORT_LABELS, LABELS, TOOLS, Choice, ModelPrefs

ACT = "mdl"
OPS = ("home", "open", "m", "e")
TARGETS = ("d", "s")


def _button(label: str, value: dict, primary: bool = False) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
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


def _card(title: str, elements: list) -> str:
    return json.dumps({
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "indigo"},
        "body": {"elements": elements},
    }, ensure_ascii=False)


def value(op: str, to: str = "d", tool: str = "", **extra: Any) -> dict:
    out = {"act": ACT, "op": op, "to": to}
    if tool:
        out["tool"] = tool
    out.update(extra)
    return out


def resolve_click(raw: Any, option: Any = None) -> Optional[dict]:
    """卡片回调 → 干净的点击；形状不对 ⇒ None。

    1.3.0 起模型和强度是下拉：回调值里只有 op/to/tool，选中项在 `option`（模型是 `m序号`，强度是档名）。
    旧卡上的按钮（值里直接带 `i` / `e`）照样认。
    """
    if not isinstance(raw, dict) or raw.get("act") != ACT:
        return None
    op, to, tool = raw.get("op"), raw.get("to", "d"), raw.get("tool", "")
    if op not in OPS or to not in TARGETS:
        return None
    if op != "home" and tool not in TOOLS:
        return None
    out = {"op": op, "to": to, "tool": tool}
    pick = option if isinstance(option, str) else ""
    if op == "m":
        idx = raw.get("i")
        if idx is None and pick.startswith("m") and pick[1:].isdigit() and len(pick) <= 3:
            idx = int(pick[1:])
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0 or idx > 64:
            return None
        out["i"] = idx
    if op == "e":
        effort = raw.get("e", pick)
        if not isinstance(effort, str) or not effort.isalpha() or len(effort) > 10:
            return None
        out["e"] = effort
    return out


def home_card(prefs: ModelPrefs, session: Optional[tuple] = None) -> str:
    """总览：每个工具的默认模型；在会话群里多一行「这个会话」。`session` = (tool, sid, label)。"""
    lines = []
    for tool in TOOLS:
        lines.append(f"**{LABELS[tool]} 默认**：{prefs.default(tool).text()}")
    buttons = [_button(f"设 {LABELS[t]} 默认", value("open", "d", t)) for t in TOOLS]
    if session:
        tool, sid, label = session
        own = prefs.session(tool, sid)
        now = prefs.effective(tool, sid)
        lines.append("")
        lines.append(f"**这个会话**（{label}）：{now.text()}" + ("" if own.model or own.effort else "（跟随默认）"))
        buttons.insert(0, _button("换这个会话的模型", value("open", "s", tool), primary=True))
    lines.append("")
    lines.append("默认模型只影响以后新建的会话；会话自己的模型从下一轮开始生效。")
    return _card("🧠 模型", [{"tag": "markdown", "content": "\n".join(lines)}] + _rows(buttons))


def _select(placeholder: str, options: list, initial: str, val: dict) -> dict:
    node = {
        "tag": "select_static", "width": "fill",
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "options": [{"text": {"tag": "plain_text", "content": text}, "value": v} for text, v in options],
        "behaviors": [{"type": "callback", "value": val}],
    }
    if initial and any(v == initial for _, v in options):
        node["initial_option"] = initial
    return node


def list_card(prefs: ModelPrefs, to: str, tool: str, sid: str = "", label: str = "") -> str:
    """选模型 + 选强度，两个下拉放一行（1.3.0）。下拉的值只有序号 / 档名，真名服务端现查。"""
    current = prefs.session(tool, sid) if to == "s" else prefs.default(tool)
    shown = prefs.effective(tool, sid) if to == "s" else current
    names = prefs.catalog.names(tool)
    head = (f"给 **{label or LABELS[tool]}** 这个会话选模型" if to == "s"
            else f"设 **{LABELS[tool]}** 的默认模型")
    elements = [{"tag": "markdown", "content": f"{head}\n现在：{shown.text()}"}]
    model_opts = [(name, f"m{idx}") for idx, name in enumerate(names)]
    model_init = f"m{names.index(shown.model)}" if shown.model in names else ""
    model_sel = _select("模型", model_opts, model_init, value("m", to, tool))
    efforts = prefs.catalog.efforts(tool, shown.model)
    cells = [(model_sel, 3)]
    if efforts:
        effort_opts = [(f"{EFFORT_LABELS.get(e, e)}（{e}）", e) for e in efforts]
        cells.append((_select("推理强度", effort_opts, shown.effort, value("e", to, tool)), 2))
    elements.append({"tag": "column_set", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "weighted", "weight": w, "vertical_align": "center", "elements": [node]}
        for node, w in cells]})
    elements += _rows([_button("↩︎ 回到模型总览", value("home", to))])
    return _card("🧠 选模型", elements)


def describe(choice: Choice) -> str:
    return choice.text()
