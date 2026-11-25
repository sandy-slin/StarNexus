"""悬浮菜单点下去，落到他**正看着的那个会话群**（第 40 轮，2026-11-22）。

主人原话：「我每个会话群都是和会话绑定的，怎么区分不出来呢，
还有你可以看最近的聊天信息 是和哪个会话聊天啊」。

# 要修的是什么

飞书的菜单点击事件**不带 chat_id**。证据两条：
- SDK 的 `P2ApplicationBotMenuV6Data` 只有 `operator` / `event_key` / `timestamp`
- 真实日志 209 次点击，每条记的也只有这三样

所以 `app._on_menu_click` 原来无条件写 `scope = f"dm:{clicker}"` ⇒ 他在
「Claude · zing 开发」群里点「⏹ 停下」，停的是**单聊**那个会话。
现场 `chat_binds.json` 有 **28 个**会话群，全落错。

# 为什么猜得准（主人指出来的那条路）

群和会话**一对一落盘**，而「他正看着哪个群」= 最近有动静的那个群。
`ChatBinds.touch()` 在两个入口打点：消息入口（群里说话）和卡片回调
（群里点按钮，这个**带 chat_id**，是确定的不是猜的）。

# 为什么要时间窗

窗口外落回单聊只是「没停对」；而落到一个他早就不看的群，是**在别人的活上动手**。
所以宁可窗口短（10 分钟）。
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet.chatbind import ChatBinds  # noqa: E402


def _binds() -> ChatBinds:
    return ChatBinds(os.path.join(tempfile.mkdtemp(), "chat_binds.json"))


def _three_groups() -> ChatBinds:
    binds = _binds()
    for cid, sid, name in (("oc_aaa111", "sidA", "Claude · zing 开发"),
                           ("oc_bbb222", "sidB", "Claude · 一键安装"),
                           ("oc_ccc333", "sidC", "Claude · Kiro账户")):
        binds.bind(cid, "claude", sid, name)
    return binds


def test_most_recent_group_wins():
    """在哪个群说话，菜单就落到那个群 —— 这是本轮的核心。"""
    binds = _three_groups()
    time.sleep(0.01)
    binds.touch("oc_bbb222")
    got = binds.most_recent_active(within_sec=600)
    assert got is not None and got.chat_id == "oc_bbb222"
    assert got.scope == "group:oc_bbb222", got.scope

    # 换个群再说话 ⇒ 落点跟着换
    time.sleep(0.01)
    binds.touch("oc_ccc333")
    got = binds.most_recent_active(within_sec=600)
    assert got is not None and got.chat_id == "oc_ccc333"


def test_stale_group_is_not_picked():
    """🔴 阴性对照：窗口外的群**不认**，落回单聊（和改之前行为一样）。

    落单聊只是「没停对」；落到一个他三天没看的群是在别人的活上动手。
    """
    binds = _three_groups()
    with binds._lock:
        for cid in list(binds._by_chat):
            old = binds._by_chat[cid]
            binds._by_chat[cid] = dataclasses.replace(
                old, created_at=time.time() - 9999, seen_at=time.time() - 660)
    assert binds.most_recent_active(within_sec=600) is None
    # 阳性对照：把窗口放宽就选得出来 ⇒ 证明「None」是窗口判的，不是数据没了
    assert binds.most_recent_active(within_sec=99999) is not None


def test_unbound_group_is_never_picked():
    """🔴 解绑过的群（on=False）一个都不许被选中 —— 它已经不转发了。"""
    binds = _binds()
    binds.bind("oc_off999", "claude", "sidX", "Claude · 已解绑")
    binds.turn_off("oc_off999")
    binds.touch("oc_off999")
    assert binds.most_recent_active(within_sec=600) is None


def test_no_groups_at_all_returns_none():
    """一个群都没有 ⇒ None（新装的机器就是这样），调用方照旧走单聊。"""
    assert _binds().most_recent_active(within_sec=600) is None


def test_seen_at_survives_reload_and_falls_back():
    """`seen_at` 存得住；旧文件没这个键 ⇒ 回落到建群时间，不是 0。"""
    binds = _three_groups()
    binds.touch("oc_aaa111")
    binds._save()
    again = ChatBinds(binds._path)
    row = again.of_chat("oc_aaa111")
    assert row is not None and row.seen_at > 0
    assert row.active_at == row.seen_at

    # 旧文件（没有 seen_at）⇒ active_at 用 created_at
    legacy = _binds()
    legacy.bind("oc_old555", "claude", "sidOld", "Claude · 老群")
    with legacy._lock:
        old = legacy._by_chat["oc_old555"]
        legacy._by_chat["oc_old555"] = dataclasses.replace(old, seen_at=0.0)
    row = legacy.of_chat("oc_old555")
    assert row.seen_at == 0.0
    assert row.active_at == row.created_at > 0, "没打过点就该回落到建群时间"


def test_touch_is_a_noop_for_unknown_chat():
    """不认识的 chat_id 打点不许建出一条绑定来（它是消息入口，什么 id 都可能进来）。"""
    binds = _binds()
    binds.touch("oc_never_seen")
    binds.touch("")
    binds.touch(None)  # type: ignore[arg-type]
    assert binds.of_chat("oc_never_seen") is None
    assert binds.most_recent_active(within_sec=600) is None


# ---------- 接线：按源码盯住 ----------

def _app_src() -> str:
    import bridge.app as app
    return open(app.__file__, encoding="utf-8").read()


def test_menu_click_consults_the_group_before_falling_back_to_dm():
    """🔴 菜单点击必须先问「最近哪个群」，问不出来才落单聊。

    这条盯着别退回「无条件 dm」那个老样子。
    """
    src = _app_src()
    start = src.index("def on_bot_menu")
    body = src[start:src.index("\n    def on_card_action", start)]
    assert "most_recent_active(within_sec=MENU_GROUP_WINDOW_SEC)" in body, \
        "菜单点击没查最近活跃的群 ⇒ 又会落到单聊"
    i_dm = body.index('scope = f"dm:{clicker}"')
    i_group = body.index("most_recent_active")
    assert i_dm < i_group, "单聊是兜底，必须排在查群**之后**（查到就覆盖掉）"


def test_both_entries_touch_the_group():
    """消息入口和卡片回调都要打点，少一个「最近活跃」就不准。

    卡片回调那条尤其要紧：它**带 chat_id**，是这条链上唯一确定的信号。
    """
    src = _app_src()
    assert src.count("_binds.touch(") >= 2, "打点少于两处 —— 消息入口和卡片回调都要有"
    card = src[src.index("def on_card_action"):src.index("def on_card_action") + 4000]
    assert "open_chat_id" in card and "touch(" in card, "卡片回调没打点"


def test_window_is_short_and_explained():
    """窗口不许放得太宽 —— 宁可落回单聊，也别在他早就不看的群里动手。"""
    from bridge.app import MENU_GROUP_WINDOW_SEC
    assert 60 <= MENU_GROUP_WINDOW_SEC <= 30 * 60, MENU_GROUP_WINDOW_SEC
