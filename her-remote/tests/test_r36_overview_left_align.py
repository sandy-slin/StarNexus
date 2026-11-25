"""11-16 总览改版：每个面板第一行「➕ 新开一个 X」、顶上不再有通用入口、会话行左对齐（交互容器）。

主人原话：「新会话 放在每个分类（claude cursor codex）会话的上方。这样用户就不用去选择了」
「列表都向左靠起，这样显得整齐一点，现在是居中」。全部假数据，不发飞书。
"""

from __future__ import annotations

import json
import os
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import cards, newsession  # noqa: E402
from bridge.fleet.actions import resolve_newsession_click  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_CARD_BYTES, MAX_CARD_COMPONENTS, build_newsession_card, build_overview_card, resolve_fleet_click,
)
from bridge.fleet.model import Session  # noqa: E402
from bridge.fleet.scan import Overview  # noqa: E402

ALL = ["claude", "codex", "cursor"]


@pytest.fixture
def installed(monkeypatch):
    monkeypatch.setattr(cards, "_startable_tools", lambda: list(ALL))


def _sess(tool="claude", sid="11111111-1111-4111-8111-111111111111", title="修登录", **kw):
    base = dict(tool=tool, surface="cli", id=sid, title=title, cwd="/Users/x/codes/demo", status="idle",
                owner="none", updated_at=time.time() - 60)
    base.update(kw)
    return Session(**base)


def _sid(i):
    return f"{i:08x}-1111-4111-8111-111111111111"


def _walk(node, tag=None):
    if isinstance(node, dict):
        if tag is None or node.get("tag") == tag:
            yield node
        for v in node.values():
            yield from _walk(v, tag)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, tag)


def _panels(card):
    out = {}
    for p in _walk(card, "collapsible_panel"):
        title = p["header"]["title"]["content"]
        for name, tool in (("Claude", "claude"), ("Codex", "codex"), ("Cursor", "cursor")):
            if f"**{name}" in title:
                out[tool] = p
    return out


def _value(node):
    return node["behaviors"][0]["value"]


def _card(view):
    blob = build_overview_card(view, seen_at=None, settled=None)
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    card = json.loads(blob)
    assert cards._components(card) <= MAX_CARD_COMPONENTS
    return card


# ---------- 1. 新开放到每个面板第一行 ----------


def test_every_panel_starts_with_its_own_new_session_row(installed):
    view = Overview(sessions=[_sess(t, _sid(i)) for i, t in enumerate(ALL)])
    panels = _panels(_card(view))
    assert set(panels) == set(ALL)
    for tool, panel in panels.items():
        first = panel["elements"][0]
        assert first["tag"] == "interactive_container" and first["horizontal_align"] == "left"
        assert _value(first) == {"act": "nsess", "op": "begin", "t": tool}
        assert resolve_newsession_click(_value(first)) == {"op": "begin", "n": "", "t": tool}
        name = cards.NEWSESSION_TOOL_NAMES[tool]
        assert first["elements"][0]["content"] == f"➕ 新开一个 {name}"


def test_no_generic_new_session_entry_on_top(installed):
    card = _card(Overview(sessions=[_sess()]))
    top = card["body"]["elements"]
    assert not any(n.get("tag") in ("button", "interactive_container") for n in top[:-1]), \
        "顶上（面板外、刷新之前）不该再有按钮"
    nsess = [b["value"] for n in _walk(card) for b in n.get("behaviors", []) or []
             if b.get("type") == "callback" and b["value"].get("act") == "nsess"]
    assert {"act": "nsess", "op": "begin"} not in nsess
    assert all(v.get("t") for v in nsess)


def test_installed_tool_without_sessions_still_gets_a_panel(installed):
    card = _card(Overview(sessions=[_sess("claude")]))
    panels = _panels(card)
    for tool in ("codex", "cursor"):
        p = panels[tool]
        assert "还没有会话" in p["header"]["title"]["content"]
        assert p["expanded"] is False
        assert [_value(e) for e in p["elements"]] == [{"act": "nsess", "op": "begin", "t": tool}]


def test_nothing_installed_and_nothing_read_still_renders(monkeypatch):
    monkeypatch.setattr(cards, "_startable_tools", lambda: [])
    card = _card(Overview(sessions=[]))
    assert not _panels(card)
    assert "一个会话都没读到" in json.dumps(card, ensure_ascii=False)


def test_available_tools_failing_does_not_kill_the_overview(monkeypatch):
    def boom(which=None):
        raise OSError("which 炸了")

    monkeypatch.undo()  # 去掉 conftest 的桩，走真的 `_startable_tools`
    monkeypatch.setattr(newsession, "available_tools", boom)
    card = _card(Overview(sessions=[_sess()]))
    assert set(_panels(card)) == {"claude"}


# ---------- 2. 会话行左对齐 ----------


def test_session_rows_are_left_aligned_containers_with_the_old_value(installed):
    sessions = [_sess(sid=_sid(1), status="waiting", title="问方案"), _sess(sid=_sid(2), title="写文档")]
    panel = _panels(_card(Overview(sessions=sessions)))["claude"]
    rows = [e for e in panel["elements"] if e.get("tag") == "interactive_container"
            and _value(e).get("act") == "fleet"]
    assert [_value(r) for r in rows] == [{"act": "fleet", "op": "open", "tool": "claude", "id": _sid(1)},
                                         {"act": "fleet", "op": "open", "tool": "claude", "id": _sid(2)}]
    for r in rows:
        assert r["width"] == "fill" and r["horizontal_align"] == "left" and r["has_border"] is True
        assert r["corner_radius"] == "8px" and r["padding"] == "6px 12px 6px 12px"
        assert [e["tag"] for e in r["elements"]] == ["markdown"]
    assert not [b for b in _walk(panel, "button") if _value(b).get("op") == "open"], "会话行不再是按钮"
    # 等你的那行醒目（蓝边浅蓝底），闲着的灰边无底色
    assert rows[0]["border_color"] == "blue" and rows[0]["background_style"] == "blue-50"
    assert rows[1]["border_color"] == "grey" and "background_style" not in rows[1]


def test_row_text_is_escaped_markdown(installed):
    panel = _panels(_card(Overview(sessions=[_sess(title="**粗** <at id=all></at> [x](http://e.com)")])))["claude"]
    md = [e["elements"][0]["content"] for e in panel["elements"] if _value(e).get("act") == "fleet"][0]
    assert "<at" not in md and "](" not in md and "**粗**" not in md


def test_container_value_resolves_exactly_like_the_old_button():
    known = [("claude", _sid(1))]
    value = cards.fleet_value("open", "claude", _sid(1))
    node = cards._tap_row("x", value)
    assert resolve_fleet_click(node["behaviors"][0]["value"], known) == {"op": "open", "tool": "claude",
                                                                         "id": _sid(1)}


def test_clicking_a_container_row_goes_through_on_card_action(tmp_path):
    """真走 `on_card_action`：事件里 action.tag = interactive_container，value 同按钮 ⇒ 进详情那条路。"""
    from tests.test_chatbind import FakeClaude
    from tests.test_relay_integration import CHAT, OWNER, _bridge

    sess = _sess(sid=_sid(1))
    b = _bridge(tmp_path, FakeClaude([sess]))
    b.fleet.overview()
    card = json.loads(build_overview_card(b.fleet.last, seen_at=None, settled=None))
    row = next(e for e in _walk(card, "interactive_container") if _value(e).get("op") == "open")
    action = types.SimpleNamespace(tag="interactive_container", value=_value(row), option=None,
                                   form_value=None, input_value=None)
    event = types.SimpleNamespace(operator=types.SimpleNamespace(open_id=OWNER),
                                  context=types.SimpleNamespace(open_chat_id=CHAT), action=action)
    resp = b.on_card_action(types.SimpleNamespace(event=event))
    assert resp.toast.type == "info" and resp.toast.content == "收到，正在读"


# ---------- 3. 组件上限：多一层容器后照样收得住 ----------


def test_component_count_includes_the_container_layer_and_shrink_still_holds(installed):
    sessions = [_sess(sid=_sid(i), title=f"会话{i}", status="waiting") for i in range(120)]
    blob = build_overview_card(Overview(sessions=sessions), seen_at=None, settled=None, expand="claude")
    card = json.loads(blob)
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    assert cards._components(card) <= MAX_CARD_COMPONENTS
    row = next(e for e in _walk(card, "interactive_container") if _value(e).get("op") == "open")
    assert cards._components(row) == 2, "容器 + 里面那段 markdown 都算组件"
    shown = [e for e in _walk(card, "interactive_container") if _value(e).get("op") == "open"]
    assert 8 < len(shown) < 120
    assert "放不下" in json.dumps(card, ensure_ascii=False)


# ---------- 4. 从面板进来的新会话卡：没有工具下拉 ----------


def test_fixed_tool_card_has_no_tool_dropdown():
    card = build_newsession_card("ab12", ALL, "codex", [("demo", True), ("other", False)], 0, fixed=True)
    sels = list(_walk(card, "select_static"))
    assert [s["behaviors"][0]["value"]["op"] for s in sels] == ["proj"]
    assert sels[0]["initial_option"] == "p0"
    assert card["header"]["title"]["content"] == "➕ 新开一个 Codex"
    box = next(_walk(card, "input"))
    assert box["behaviors"][0]["value"] == {"act": "nsess", "op": "send", "n": "ab12"}


def test_unfixed_card_still_has_the_tool_dropdown():
    card = build_newsession_card("ab12", ALL, "codex", [("demo", True)], 0)
    ops = [s["behaviors"][0]["value"]["op"] for s in _walk(card, "select_static")]
    assert ops == ["tsel", "proj"]


def _ns_bridge(tmp_path, monkeypatch):
    from tests.test_r36_newsession_ui import _bridge, proj_a

    monkeypatch.setattr(newsession, "available_tools", lambda which=None: list(ALL))
    proj_a(tmp_path)
    return _bridge(tmp_path)


def _ns_click(b, value):
    from tests.test_r36_newsession_ui import _click

    return _click(b, value)


def test_begin_from_a_panel_gives_the_fixed_card(tmp_path, monkeypatch):
    b = _ns_bridge(tmp_path, monkeypatch)
    out = _ns_click(b, {"act": "nsess", "op": "begin", "t": "cursor"})
    card = out["card"]
    assert card["header"]["title"]["content"] == "➕ 新开一个 Cursor"
    assert [s["behaviors"][0]["value"]["op"] for s in _walk(card, "select_static")] == ["proj"]
    # 换项目原地换卡后还是工具已定的那张
    nonce = next(_walk(card, "input"))["behaviors"][0]["value"]["n"]
    from tests.test_r36_newsession_ui import _click

    again = _click(b, {"act": "nsess", "op": "proj", "n": nonce}, option="p0")["card"]
    assert [s["behaviors"][0]["value"]["op"] for s in _walk(again, "select_static")] == ["proj"]
    assert again["header"]["title"]["content"] == "➕ 新开一个 Cursor"


def test_begin_without_tool_keeps_the_dropdown(tmp_path, monkeypatch):
    b = _ns_bridge(tmp_path, monkeypatch)
    card = _ns_click(b, {"act": "nsess", "op": "begin"})["card"]
    assert [s["behaviors"][0]["value"]["op"] for s in _walk(card, "select_static")][0] == "tsel"
