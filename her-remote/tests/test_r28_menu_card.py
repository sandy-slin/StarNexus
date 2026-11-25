"""第 28 轮：「全部按钮」卡（`menu.build_menu_card`）优化。

用户原话（2026-11-08）：「全部按钮可以尝试优化下。」

数据（本机 agent.log 11-04 ~ 11-08，悬浮栏 147 次点击）：会话 60、权限 30、模型 18、
最近问过 11、全部按钮 9、哪里有改动 5、停下 5、换项目 5、找 TODO 2、有哪些项目 2。
⇒ 点得最多的「看看会话」「最近问过」原来**根本不在**「全部按钮」卡上。

这里盯：
1. 补上的三个入口（会话 / 最近问过 / 运行诊断）都在卡上，且 `resolve_click` 认得
2. 点得多的在最上面，没数据的收进折叠面板（面板只放按钮，不放 form）
3. 顶部状态行还在；一排最多两个；按钮字不重复；JSON 合法、20KB 内
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.menu import (  # noqa: E402
    CONTROLS,
    QUICK_QUESTIONS,
    build_menu_card,
    known_commands,
    resolve_click,
)
from bridge.policy import Policy  # noqa: E402

MAX_CARD_BYTES = 20 * 1024
NEW_ENTRIES = {"/history": "↑ 最近问过", "/sessions": "🖥 看看会话", "/诊断": "🩺 运行诊断"}


def _policy():
    return Policy(
        mode="trusted",
        workspaces=("/tmp/ws",),
        default_workspace="/tmp/ws",
        model="claude-sonnet-5-5",
        turn_timeout_sec=900,
        ceiling_mode="trusted",
    )


def _card():
    return json.loads(build_menu_card("~/codes/carher-admin", _policy()))


def _walk(node, out, inside_panel=False):
    """所有按钮 → [(按钮, 是否在折叠面板里)]。"""
    if isinstance(node, dict):
        if node.get("tag") == "button":
            out.append((node, inside_panel))
        here = inside_panel or node.get("tag") == "collapsible_panel"
        for value in node.values():
            _walk(value, out, here)
    elif isinstance(node, list):
        for value in node:
            _walk(value, out, inside_panel)
    return out


def _buttons(card=None):
    return _walk(card or _card(), [])


def _label(button):
    return button["text"]["content"]


def _value(button):
    return button["behaviors"][0]["value"]


# ---------- 1. 补上的入口 ----------


def test_new_entries_are_on_the_card_and_resolve():
    """🔴 旧码上红：会话 / 最近问过 / 运行诊断原来都不在「全部按钮」卡上。"""
    on_card = {_value(b).get("cmd"): _label(b) for b, _ in _buttons()}
    for cmd, label in NEW_ENTRIES.items():
        assert on_card.get(cmd) == label, f"卡上没有「{label}」（{cmd}）：{on_card}"
        assert resolve_click({"act": "menu", "cmd": cmd}) == cmd, f"{cmd} 点了会被当成伪造回调"


def test_every_button_on_the_card_resolves_and_is_a_known_command():
    """卡上每一个按钮点了都得有反应；cmd 只许用已有命令（不新增命令）。"""
    known = known_commands()
    for button, _ in _buttons():
        value = _value(button)
        assert value["act"] == "menu"
        assert resolve_click(value), f"「{_label(button)}」点了会报「卡片不对劲」：{value}"
        if "cmd" in value:
            assert value["cmd"] in known, f"{value['cmd']} 不是已有命令"


def test_new_entry_labels_match_the_buttons_elsewhere():
    """标签和 `fleet.actions.DEFAULT_LABELS` 一字不差 —— 帮助、别的卡叫他点的就是这几个字。"""
    from bridge.fleet.actions import DEFAULT_LABELS, DO_COMMANDS

    by_cmd = {cmd: DEFAULT_LABELS[op] for op, cmd in DO_COMMANDS.items()}
    for cmd, label in CONTROLS:
        if cmd in by_cmd:
            assert label == by_cmd[cmd], f"{cmd}：菜单卡写「{label}」，别处写「{by_cmd[cmd]}」"


# ---------- 2. 常用在上、少用收起 ----------


def test_most_used_entries_come_first_and_stay_visible():
    """🔴 旧码上红：悬浮栏点得最多的「看看会话」（60 次）排第一、主色、不在面板里。"""
    buttons = _buttons()
    first, in_panel = buttons[0]
    assert _value(first).get("cmd") == "/sessions"
    assert first["type"] == "primary"
    assert not in_panel
    visible = {_value(b).get("cmd") for b, p in buttons if not p}
    # 悬浮栏真有人点过的控制，都要露在外面
    for cmd in ("/sessions", "/history", "/mode", "/model", "/stop", "/dirs"):
        assert cmd in visible, f"{cmd} 被收起来了"


def test_rarely_used_entries_fold_into_one_collapsed_panel():
    """🔴 旧码上红：没有点击数据的那几样收进一个默认收起的折叠面板。"""
    card = _card()
    panels = [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]
    assert len(panels) == 1
    panel = panels[0]
    assert panel["expanded"] is False
    assert '"form"' not in json.dumps(panel), "折叠面板里不能放 form"
    folded = {_label(b) for b, p in _buttons(card) if p}
    assert folded, "面板是空的"
    # 标题要说清里面有什么 —— 收起来的东西不说名字，就等于没有
    title = panel["header"]["title"]["content"]
    for cmd, label in CONTROLS:
        if label in folded:
            assert label.split(" ", 1)[-1] in title, f"面板标题没提「{label}」：{title}"


def test_panel_icon_matches_the_one_feishu_accepted():
    """菜单卡自己抄了一份箭头图标（不 import cards，免得跟着那个文件一起坏）⇒ 盯两份一致。"""
    from bridge.fleet.cards import PANEL_ICON

    panel = next(e for e in _card()["body"]["elements"] if e.get("tag") == "collapsible_panel")
    assert panel["header"]["icon"] == PANEL_ICON
    assert panel["header"]["icon_expanded_angle"] == -180


def test_nothing_was_dropped():
    """原来卡上的每一个按钮都还在（挪位置，不删）。"""
    labels = {_label(b) for b, _ in _buttons()}
    for _, label in QUICK_QUESTIONS:
        assert label in labels
    for _, label in CONTROLS:
        assert label in labels
    assert len(_buttons()) == len(QUICK_QUESTIONS) + len(CONTROLS)


# ---------- 3. 老规矩 ----------


def test_status_line_still_on_top():
    first = _card()["body"]["elements"][0]
    assert first["tag"] == "markdown"
    content = first["content"]
    assert "`~/codes/carher-admin`" in content and "🔑" in content and "🧠" in content


def test_at_most_two_buttons_per_row_including_inside_the_panel():
    def rows(node):
        if isinstance(node, dict):
            if node.get("tag") == "column_set":
                yield node
            for value in node.values():
                yield from rows(value)
        elif isinstance(node, list):
            for value in node:
                yield from rows(value)

    found = list(rows(_card()))
    assert found
    for row in found:
        assert len(row["columns"]) <= 2


def test_button_labels_are_unique():
    labels = [_label(b) for b, _ in _buttons()]
    assert len(labels) == len(set(labels)), labels


def test_card_json_valid_and_within_20kb():
    for blob in (build_menu_card(), build_menu_card("~/codes/" + "很长的目录名" * 20, _policy())):
        card = json.loads(blob)
        assert card["schema"] == "2.0"
        assert '"tag": "action"' not in blob
        assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
