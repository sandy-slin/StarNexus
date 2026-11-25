"""常驻菜单（输入框上方那一排）。

## 为什么这组测试盯得这么死

菜单的限制是 2026-11-04 **实打飞书接口**逐个试出来的，文档是错的：
文档说"5 个主菜单 × 每个 10 个子菜单"，实际 `bot_menus` 是拍平数组、
**总数上限 10**（`code 99992402 / field: bot.bot_menus / "the max len is 10"`）。

写错的代价不是报错，而是**配不上去**：`210011 invalid request` 的 error body
里只有 log_id，不说哪个字段错了。所以把每个实测出来的约束都钉成测试，
下次改菜单在本地就红，不用再去打一遍接口试。
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge import botmenu  # noqa: E402


def test_tree_passes_its_own_validator():
    assert botmenu.validate_tree() == []


def test_total_nodes_within_the_measured_limit():
    """🔴 父子加起来 ≤ 10。文档写的 5×10 是错的。"""
    nodes = botmenu.build_bot_menus()
    assert len(nodes) <= botmenu.MAX_NODES, (
        f"{len(nodes)} 个节点超了实测上限 {botmenu.MAX_NODES} "
        "⇒ 配置会被拒：99992402 the max len is 10"
    )


def test_top_level_within_measured_limit():
    """悬浮样式（strategy=3）实测 5 主过、6 主拒。可切换样式（1/2）只到 3。"""
    assert len(botmenu.MENU_TREE) <= botmenu.MAX_TOP


def test_uses_floating_style():
    """🔴 必须是 3（悬浮，常驻输入框上方）。

    第一版用了 2 —— 那是「可切换」样式：菜单占输入框的位置、要点左边按钮切。
    用户原话「看到了但和豆包不一样」。依据：只有 strategy=3 接受 4~5 个主菜单，
    而官方文档写明只有悬浮样式允许超过 3 个主菜单。
    """
    assert botmenu.DISPLAY_FLOAT == 3


def test_children_within_measured_limit():
    for name, _key, children in botmenu.MENU_TREE:
        assert len(children) <= botmenu.MAX_CHILDREN, name


# ---------- 实测出来的必填字段 ----------


def test_every_node_has_i18n_name():
    """🔴 `i18n_name` 必填。少了被 210011 拒，而那个错不说是哪个字段。"""
    for node in botmenu.build_bot_menus():
        assert node.get("i18n_name"), f"{node.get('default_name')} 少了 i18n_name"
        assert node["i18n_name"].get("zh_cn")


def test_content_type_is_numeric():
    """🔴 `menu_content_type` 是数字 [0,1,2,3,4]，不是字符串。

    传 "event" 会被拒：`menu_content_type is optional, options: [0,1,2,3,4]`。
    """
    for node in botmenu.build_bot_menus():
        value = node.get("menu_content_type")
        assert isinstance(value, int), f"{node.get('default_name')} 的类型是 {type(value)}"
        assert value in (0, 1, 2, 3, 4)


def test_event_nodes_carry_a_key_and_parents_do_not():
    """推事件的节点（type=2）要有 event_key；挂子菜单的父节点（type=0）不推事件。"""
    nodes = botmenu.build_bot_menus()
    parents = {n.get("parent_menu_id") for n in nodes if n.get("parent_menu_id")}
    for node in nodes:
        if node["menu_content_type"] == botmenu.CONTENT_TYPE_EVENT:
            assert node.get("event_key"), node
            assert node["menu_id"] not in parents, f"{node['default_name']} 有子菜单却推事件"
        elif node["menu_content_type"] == botmenu.CONTENT_TYPE_LINK:
            # 跳转链接（11-10 加的「🌐 网页版」）：不推事件、不挂子菜单（test_r34_webmenu.py 细查）
            assert not node.get("event_key") and node.get("redirect_link"), node
            assert node["menu_id"] not in parents
        else:
            assert not node.get("event_key")
            assert node["menu_id"] in parents, f"{node['default_name']} 不推事件也没子菜单"


def test_children_reference_a_real_parent():
    nodes = botmenu.build_bot_menus()
    ids = {n["menu_id"] for n in nodes}
    for node in nodes:
        parent = node.get("parent_menu_id")
        if parent:
            assert parent in ids, f"{node['default_name']} 的父节点 {parent} 不存在"


def test_menu_ids_are_unique():
    nodes = botmenu.build_bot_menus()
    ids = [n["menu_id"] for n in nodes]
    assert len(ids) == len(set(ids))


def test_event_keys_are_unique():
    """重复的 key 会让两个菜单项做同一件事 —— 多半是写错了。"""
    keys = [
        n["event_key"] for n in botmenu.build_bot_menus() if n.get("event_key")
    ]
    assert len(keys) == len(set(keys))


def test_event_key_length():
    """event_key 上限 30 字符。"""
    for node in botmenu.build_bot_menus():
        if node.get("event_key"):
            assert len(node["event_key"]) <= 30


# ---------- 解析 ----------


def test_every_menu_key_resolves_to_something():
    """菜单上每一项点了都必须有反应 —— 解不出来就是个死按钮。"""
    for name, key, children in botmenu.MENU_TREE:
        if key:
            assert botmenu.resolve_event_key(key) or botmenu.LINK_URLS.get(key), f"「{name}」点了没反应"
        for child_name, child_key in children:
            assert (botmenu.resolve_event_key(child_key)
                    or botmenu.LINK_URLS.get(child_key)), f"「{child_name}」点了没反应"


@pytest.mark.parametrize(
    "key",
    [
        "",
        "rm -rf /",
        "/stop; rm -rf /",
        "/mode trusted",
        "q:unknown",
        "../../etc/passwd",
        "/MENU",
    ],
)
def test_unknown_keys_rejected(key):
    """🔴 只认预设过的 key。

    事件来自飞书，但内容不该被直接信任 —— 要是把 event_key 原样当消息执行，
    能伪造这个事件的东西就能让 agent 跑任意指令。
    注意 `/mode trusted` 也要拒：带参数的形式不在预设里，
    否则就能绕过"放开权限要点两次"那条设计。
    """
    assert botmenu.resolve_event_key(key) == ""


def test_sessions_chip_replaces_projects():
    """「🖥 会话」换掉了「📁 项目」：总数不变（仍 ≤ 10），点了发 /sessions。"""
    names = [name for name, _key, _children in botmenu.MENU_TREE]
    assert "🖥 会话" in names and "📁 项目" not in names
    key = next(k for name, k, _ in botmenu.MENU_TREE if name == "🖥 会话")
    assert botmenu.resolve_event_key(key) == "/sessions"


def test_question_keys_map_to_full_wording():
    """菜单上显示短标签，发给 agent 的是完整问句。"""
    assert botmenu.resolve_event_key("q:projects") == "我有哪些项目？各自是什么"


def test_menu_only_contains_harmless_actions():
    """误点一下不能动到文件、不能提权。

    `/mode` 和 `/model` 只是**显示选择卡**，本身不改设置（见 test_menu.py 里
    的 test_mode_switch_needs_two_taps）。
    """
    # /sessions 只列会话、出卡片；接着聊 / 停都要在卡片上再点、且各自有闸门
    # /history 只发一张「最近问过」卡；选中只是把那句填进框，要再点「发送」才跑（bridge/history.py）
    allowed = {"/status", "/dirs", "/mode", "/model", "/menu", "/stop", "/new", "/help", "/sessions",
               "/history"}
    for name, key, children in botmenu.MENU_TREE:
        pairs = ([(name, key)] if key else []) + list(children)
        for label, k in pairs:
            if k.startswith("q:") or botmenu.is_link(k):  # 链接只在飞书里打开网页，不碰电脑
                continue
            assert k in allowed, f"「{label}」→ {k} 不是无害命令"


def test_no_bang_command_in_the_menu():
    """🔴 `/mode!`（跳过二次确认）绝不能出现在菜单里。

    它是卡片回调的内部形式。做成常驻菜单项就等于"一次误点直接提权"。
    """
    for _, key, children in botmenu.MENU_TREE:
        assert "!" not in key
        for _, child_key in children:
            assert "!" not in child_key


# ---------- 校验器自己有效 ----------


def test_validator_catches_too_many_nodes(monkeypatch):
    """阳性对照：撑爆上限时 validate_tree 必须红。"""
    fat = tuple(
        (f"父{i}", "", tuple((f"子{i}{j}", "/status") for j in range(5)))
        for i in range(3)
    )
    monkeypatch.setattr(botmenu, "MENU_TREE", fat)
    problems = botmenu.validate_tree()
    assert any("上限" in p for p in problems)


def test_validator_catches_a_dead_key(monkeypatch):
    monkeypatch.setattr(
        botmenu, "MENU_TREE", (("父", "", (("死按钮", "/nope"),)),)
    )
    assert any("解不出" in p for p in botmenu.validate_tree())


def test_validator_catches_childless_parent(monkeypatch):
    """没事件也没子菜单 ⇒ 点了什么都不会发生。"""
    monkeypatch.setattr(botmenu, "MENU_TREE", (("孤父", "", ()),))
    assert any("什么都不会发生" in p for p in botmenu.validate_tree())


def test_menu_write_errors_are_in_plain_words():
    """写菜单失败时的两个关键码要说人话（第 12 轮排查：这两条正是「没悬浮菜单」分不清的原因）。"""
    from bridge import botmenu

    for code, want in ((210020, "审核"), (210021, "开发者后台")):
        got = botmenu._human({"code": code, "msg": "raw"}, botmenu.CONFIG_ERRORS)
        assert want in got and "code=" not in got, (code, got)


def test_menu_failure_line_uses_plain_words():
    """调用点：setup() 写菜单失败那行走 _human，不再拼原始 code=。"""
    import inspect

    from bridge import botmenu

    src = inspect.getsource(botmenu.setup)
    assert 'WARN=常驻菜单没配上：{_human(out, CONFIG_ERRORS)}' in src
