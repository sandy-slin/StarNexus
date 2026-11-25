"""第 34 轮（11-10 加固）：悬浮栏加「🌐 网页版」—— 飞书直接打开 chat.auto-link.com.cn。

为什么是「跳转链接」不是「推事件」：电脑关机 / 合盖 / 断网时，没有任何进程收得到菜单事件；
跳转是飞书客户端自己做的 ⇒ 电脑不在也能点。

取值依据（没实打过飞书接口，见 `botmenu.py` 文件头）：
- 官方文档 v7 ability PATCH：`menu_content_type` 「1：跳转链接」；`redirect_link` 「当 menu_content_type = 1 时必填」，
  字段只有 `pc_url` / `mobile_url`
- SDK 1.7.5 `lark_oapi/api/application/v7/model/bot_menu_redirect_link.py`：同样只有这两个字段

让位：「🔑 权限」挪进「⚡ 常用」；「⚡ 常用」实测最多 5 子 ⇒ 挤出「找 TODO」（菜单卡上还在）。
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge import botmenu, menu_heal  # noqa: E402

WEB = "https://chat.auto-link.com.cn/"


def _node(name):
    found = [n for n in botmenu.build_bot_menus() if n["default_name"] == name]
    assert len(found) == 1, f"菜单里「{name}」有 {len(found)} 个"
    return found[0]


# ---------- 1. 形状：跳转链接，不推事件 ----------


def test_web_chip_is_a_link_inside_common():
    """11-10 主人改定：「🌐 网页版」放进「⚡ 常用」，第一排留给「🧠 模型」。"""
    node = _node("🌐 网页版")
    parent = _node("⚡ 常用")
    assert node.get("parent_menu_id") == parent["menu_id"]
    assert node["menu_content_type"] == 1 == botmenu.CONTENT_TYPE_LINK
    assert node["redirect_link"] == {"pc_url": WEB, "mobile_url": WEB}


def test_web_chip_pushes_no_event():
    """🔴 带了 event_key 就不再是「电脑关着也能用」—— 一个字段都不许有。"""
    node = _node("🌐 网页版")
    assert "event_key" not in node
    assert node["i18n_name"]["zh_cn"] == "🌐 网页版"   # 文件头：i18n_name 必填


def test_only_link_nodes_carry_redirect_link():
    for node in botmenu.build_bot_menus():
        has = "redirect_link" in node
        assert has == (node["menu_content_type"] == botmenu.CONTENT_TYPE_LINK), node


def test_link_key_is_never_accepted_as_an_event():
    """🔴 链接项的 key 只是本地名字。要是有事件带着它进来，那是伪造的 ⇒ 当没认出来。"""
    assert "link:web" not in botmenu.ALLOWED_KEYS
    assert botmenu.resolve_event_key("link:web") == ""


def test_write_menu_sends_the_link_node():
    """真正发出去的 PATCH body 里有这一项（假 http，一个真请求都不发）。"""
    seen = []

    def fake(method, path, token, body=None):
        seen.append((method, path, body))
        return {"code": 0}

    botmenu.write_menu("cli_x", "t", http=fake)
    method, path, body = seen[0]
    assert method == "PATCH" and path.endswith("/cli_x/ability")
    links = [n for n in body["bot"]["bot_menus"] if n.get("redirect_link")]
    assert [n["default_name"] for n in links] == ["🌐 网页版"]
    json.dumps(body, ensure_ascii=False)  # 能序列化


# ---------- 2. 布局：权限挪进常用，总数仍是实测上限内 ----------


def test_layout_after_the_move():
    top = [name for name, _k, _c in botmenu.MENU_TREE]
    assert top == ["🖥 会话", "↑ 最近", "🧠 模型", "⚡ 常用", "⏹ 停下"]
    common = dict((n, c) for n, _k, c in botmenu.MENU_TREE)["⚡ 常用"]
    labels = [label for label, _ in common]
    assert labels[0] == "🔑 权限", "权限是常用里点得最多的（agent.log /mode 33 次）⇒ 放第一个"
    assert ("🔑 权限", "/mode") in common and ("全部按钮", "/menu") in common
    assert ("🌐 网页版", "link:web") in common
    assert dict((n, k) for n, k, _c in botmenu.MENU_TREE)["🧠 模型"] == "/model"


def test_still_within_measured_limits():
    nodes = botmenu.build_bot_menus()
    assert len(nodes) <= 10
    assert len(botmenu.MENU_TREE) <= 5
    for name, _key, children in botmenu.MENU_TREE:
        assert len(children) <= 5, name
    assert botmenu.validate_tree() == []


def test_permission_button_still_works_and_needs_two_taps():
    """挪了位置，key 没变 ⇒ `on_bot_menu` → `/mode` 那条路一字不改。"""
    assert botmenu.resolve_event_key("/mode") == "/mode"


def test_squeezed_out_question_is_still_reachable():
    """「找 TODO」被挤出悬浮栏 —— 没删功能：菜单卡（全部按钮）上直接露着，key 也照样认。"""
    from bridge import menu

    assert "q:todo" in menu._PINNED_QUESTIONS
    assert botmenu.resolve_event_key("q:todo") == botmenu.QUESTION_TEXT["q:todo"]


def test_help_mentions_the_web_entry_and_where_permission_went():
    from bridge.menu import HELP

    assert "「🌐 网页版」" in HELP
    perm = next(line for line in HELP.splitlines() if "「🔑 权限」" in line)
    assert "⚡ 常用" in perm, perm


# ---------- 3. 校验器：阳性对照 ----------


def test_validator_now_rejects_six_children(monkeypatch):
    """🔴 旧 MAX_CHILDREN=10（抄后台文档）会放过 6 子；实测 6 子报 210011。"""
    six = (("父", "", tuple((f"子{i}", "/status") for i in range(6))),)
    monkeypatch.setattr(botmenu, "MENU_TREE", six)
    assert any("子菜单 6 个" in p for p in botmenu.validate_tree())


def test_validator_rejects_a_non_https_link(monkeypatch):
    monkeypatch.setitem(botmenu.LINK_URLS, "link:web", "http://chat.auto-link.com.cn/")
    assert any("不是 https" in p for p in botmenu.validate_tree())


# ---------- 4. 发版：老用户怎么拿到新菜单 ----------


def _tree_without_web():
    return tuple((n, k, tuple(c for c in kids if c[0] != "🌐 网页版"))
                 for n, k, kids in botmenu.MENU_TREE)


def test_fingerprint_follows_the_menu_shape(monkeypatch):
    """🔴 菜单改了 ⇒ 指纹变 ⇒ 老机器的 `done: true` 作废 ⇒ 自动更新 / 自愈会重写一次。

    原来指纹只看权限清单：改 MENU_TREE 之后老机器永远拿不到新菜单。
    """
    before = menu_heal.scopes_fingerprint()
    old_tree = _tree_without_web()
    monkeypatch.setattr(botmenu, "MENU_TREE", old_tree)
    assert menu_heal.scopes_fingerprint() != before


def test_old_done_record_is_stale_and_new_one_holds(tmp_path, monkeypatch):
    path = str(tmp_path / "menu_heal.json")
    # 模拟上一版装好的机器：用旧菜单算的指纹
    old_tree = _tree_without_web()
    with monkeypatch.context() as m:
        m.setattr(botmenu, "MENU_TREE", old_tree)
        menu_heal.mark_done(path, "cli_x", "1.old")
        assert menu_heal.is_done(path, "cli_x")
    assert not menu_heal.is_done(path, "cli_x"), "菜单变了老标记还算数 ⇒ 新菜单到不了老用户"
    menu_heal.mark_done(path, "cli_x", "1.new")
    assert menu_heal.is_done(path, "cli_x"), "写成一次就该停（不然每次重启都发版）"


def test_fingerprint_is_stable_for_the_same_menu():
    assert menu_heal.scopes_fingerprint() == menu_heal.scopes_fingerprint()
