"""第 14 轮 Y：点「接着聊」一下就进群（2026-11-06 用户原话：
「用户点击接着聊，直接进群，没有就建一个群，然后进群，一气呵成。而不是再弹出一个进群聊，让用户再点击一下」）。

做法：飞书按钮的 `behaviors` 是一个数组，文档写明「支持同时生效跳转链接和回传交互」
（open.feishu.cn 按钮组件文档 behaviors 字段说明，11-06 现查）。所以已经有群的会话，
详情卡上的「接着聊」**同一个按钮**带两样：
- `open_url` 到那个群（`applink …/client/chat/open?openChatId=`）⇒ 客户端直接跳进群
- `callback`（op=`go`）⇒ 桥照旧在群里发第一句「现在就能说」+ 权限说明、记确认，
  而且**不再**往单聊里补那句「已经有群了 + ➡️ 进群说话」

没群的会话：详情卡**不预先建群**（只看一眼不该多一个群），首次照旧两下（接着聊 → 进群说话）。
不能接着聊的会话：没有这个按钮、不建群。

🔴 一个群都不真建（假 client）；夹具放测试目录旁边，不放 tmp_path（pathguard 豁免前缀）。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import pathlib
import shutil
import sys
import threading
import types
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from test_chatbind import (  # noqa: E402
    CID,
    DM,
    DM_CHAT,
    OWNER,
    FakeClaude,
    FakeGroups,
    _bridge,
    _sess,
)

from bridge.fleet import Message  # noqa: E402
from bridge.fleet.cards import build_detail_card, resolve_fleet_click  # noqa: E402

LINK_PREFIX = "https://applink.feishu.cn/client/chat/open?openChatId="


class LinkGroups(FakeGroups):
    """假建群接口 + 真实那一层的 `open_link`（纯拼字符串，不碰网络）。"""

    @staticmethod
    def open_link(chat_id):
        from bridge.fleet.groupsetup import GroupSetup

        return GroupSetup.open_link(chat_id)


@pytest.fixture
def here_dir():
    base = pathlib.Path(_HERE).parent / f".r14-onetap-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _make(here_dir, **sess_kw):
    claude = FakeClaude([_sess(**sess_kw)])
    groups = LinkGroups()
    b = _bridge(here_dir, claude, groups)
    b.fleet.overview()
    return b, claude, groups


def _with_group(here_dir, **sess_kw):
    """先让这个会话有一个群（走真实的建群那条路），再把两边的消息清掉，只看之后的。"""
    b, claude, groups = _make(here_dir, **sess_kw)
    asyncio.run(b._open_session_group(DM_CHAT, DM, OWNER, "claude", CID))
    gid = groups.created[0][0]
    b.replier.texts.clear()
    b.replier.cards.clear()
    return b, claude, groups, gid


def _buttons(card_json):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(card_json))
    return found


def _talk_button(card_json):
    hits = [b for b in _buttons(card_json) if "接着聊" in b["text"]["content"] or "继续说" in b["text"]["content"]]
    return hits[0] if hits else None


def _kinds(button):
    return [beh.get("type") for beh in button["behaviors"]]


def _jump_url(button):
    return next((beh.get("default_url") for beh in button["behaviors"] if beh.get("type") == "open_url"), "")


def _callback(button):
    return next((beh.get("value") for beh in button["behaviors"] if beh.get("type") == "callback"), None)


def _detail(b, scope=DM):
    asyncio.run(b._fleet_detail(DM_CHAT if scope == DM else scope.split(":", 1)[1], "claude", CID, scope))
    cards = [c for _cid, c in b.replier.cards]
    assert cards, "详情卡没发出来"
    return cards[-1]


# ---------- 卡片本身（cards.build_detail_card）----------


def test_detail_card_with_group_link_makes_talk_a_jump_plus_callback():
    link = LINK_PREFIX + "oc_g1"
    card = build_detail_card(_sess(), [Message("assistant", "在跑")], group_link=link)
    button = _talk_button(card)
    assert button is not None
    assert _kinds(button) == ["open_url", "callback"], "已有群：一个按钮要同时跳转 + 回调"
    assert _jump_url(button) == link
    assert _callback(button) == {"act": "fleet", "op": "go", "tool": "claude", "id": CID}


def test_detail_card_without_group_link_stays_callback_only():
    button = _talk_button(build_detail_card(_sess(), []))
    assert _kinds(button) == ["callback"]
    assert _callback(button)["op"] == "send"


def test_detail_card_drops_a_non_feishu_link():
    """链接不过 `safe_link` 白名单 ⇒ 退回只回调的按钮（不放任意跳转）。"""
    button = _talk_button(build_detail_card(_sess(), [], group_link="https://evil.example/x"))
    assert _kinds(button) == ["callback"] and _callback(button)["op"] == "send"


def test_detail_card_cannot_talk_has_no_talk_button_even_with_link():
    sess = _sess(can_relay=False, why_not_relay="App 里的会话发不进去", can_send=False)
    assert _talk_button(build_detail_card(sess, [], group_link=LINK_PREFIX + "oc_g1")) is None


def test_go_op_passes_the_whitelist():
    known = {("claude", CID)}
    assert resolve_fleet_click({"act": "fleet", "op": "go", "tool": "claude", "id": CID}, known) == \
        {"op": "go", "tool": "claude", "id": CID}
    assert resolve_fleet_click({"act": "fleet", "op": "go", "tool": "claude", "id": "nope"}, known) is None


# ---------- 详情卡怎么来的（app._fleet_detail）----------


def test_dm_detail_for_session_with_group_jumps_straight_into_that_group(here_dir):
    b, _claude, groups, gid = _with_group(here_dir)
    button = _talk_button(_detail(b))
    assert _jump_url(button) == LINK_PREFIX + gid, "已有群：详情卡上的「接着聊」要直接跳进那个群"
    assert _callback(button)["op"] == "go"
    assert len(groups.created) == 1, "看详情不该再建群"


def test_dm_detail_for_unbound_group_still_jumps(here_dir):
    """主人解绑过（群留着）：点「接着聊」一样一下进群，回调那边会重新接上。"""
    b, _claude, _groups, gid = _with_group(here_dir)
    b.chat_binds.turn_off(gid)
    assert _jump_url(_talk_button(_detail(b))) == LINK_PREFIX + gid


def test_dm_detail_without_group_does_not_create_one(here_dir):
    """没群：只看一眼不建群，按钮照旧只回调（首次两下）。"""
    b, _claude, groups = _make(here_dir)
    button = _talk_button(_detail(b))
    assert _kinds(button) == ["callback"] and _callback(button)["op"] == "send"
    assert groups.created == []


def test_detail_for_session_that_cannot_talk_has_no_button_and_no_group(here_dir):
    b, _claude, groups = _make(here_dir, can_relay=False, why_not_relay="App 里的会话发不进去", can_send=False)
    card = _detail(b)
    assert _talk_button(card) is None
    assert groups.created == []


def test_no_link_for_a_session_that_can_no_longer_talk_even_if_it_had_a_group(here_dir):
    """有过群、后来变成不能接着聊（比如挪进了 App）⇒ 不给跳转链接（卡上那层之外再兜一层）。"""
    b, _claude, _groups, gid = _with_group(here_dir)
    assert b._session_group_link(DM, _sess()) == LINK_PREFIX + gid, "阳性对照"
    dead = _sess(can_relay=False, why_not_relay="App 里的会话发不进去", can_send=False)
    assert b._session_group_link(DM, dead) == ""


def test_group_scope_detail_never_jumps_elsewhere(here_dir):
    """在**群里**看详情，「接着聊」的意思是「把这个群改绑到它」，不能跳到另一个群去。"""
    b, _claude, _groups, gid = _with_group(here_dir)
    b._chat_ids[f"group:oc_other"] = "oc_other"
    button = _talk_button(_detail(b, scope="group:oc_other"))
    assert _kinds(button) == ["callback"] and _callback(button)["op"] == "send"


# ---------- 点下去之后（app._on_fleet_click → _open_session_group）----------


class _Loop:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def drain(self):
        async def nothing():
            await asyncio.sleep(0.05)

        for _ in range(3):
            asyncio.run_coroutine_threadsafe(nothing(), self.loop).result(5)

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()


def _click(b, op):
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=DM_CHAT))
    loop = _Loop()
    b._loop = loop.loop
    try:
        resp = b._on_fleet_click(event, OWNER, {"act": "fleet", "op": op, "tool": "claude", "id": CID})
        loop.drain()
    finally:
        loop.close()
        b._loop = None
    return str(getattr(getattr(resp, "toast", None), "content", ""))


def _dm_said(b):
    texts = "\n".join(b.replier.to(DM_CHAT))
    cards = "\n".join(b.replier.cards_to(DM_CHAT))
    return texts + "\n" + cards


def test_go_click_does_not_pop_a_second_enter_button_in_dm(here_dir):
    b, _claude, _groups, gid = _with_group(here_dir)
    toast = _click(b, "go")
    assert "进群" in toast
    said = _dm_said(b)
    assert "进群说话" not in said and "已经有群了" not in said, "已经跳进群了，单聊里又弹一个进群按钮"
    # 群里第一句照旧发（第 11 轮：现在就能说 + 权限说明 + 别拉人 + 退出）
    group_said = "\n".join(b.replier.to(gid))
    assert "现在就能说" in group_said and "别拉别人进来" in group_said
    assert b._relay_granted(f"group:{gid}", "claude", CID), "点「接着聊」这一下没记成确认"


def test_go_click_turns_an_unbound_group_back_on(here_dir):
    b, _claude, _groups, gid = _with_group(here_dir)
    b.chat_binds.turn_off(gid)
    _click(b, "go")
    assert b.chat_binds.of_chat(gid).on, "跳进群了，群却还是解绑状态 ⇒ 说话不转发"


def test_go_click_when_group_was_dissolved_gives_the_new_group_button(here_dir):
    """跳过去的那个群已经被解散 ⇒ 重建，单聊里**要**给新群的进群按钮（不能是死路）。"""
    b, _claude, groups, gid = _with_group(here_dir)
    groups.alive.discard(gid)
    _click(b, "go")
    new_gid = groups.created[-1][0]
    assert new_gid != gid
    assert LINK_PREFIX + new_gid in _dm_said(b)


def test_go_click_dissolved_and_cannot_rebuild_falls_back_to_dm_talk(here_dir):
    b, _claude, groups, gid = _with_group(here_dir)
    groups.alive.discard(gid)
    groups.code = 99991672
    _click(b, "go")
    assert "就在这里跟它聊" in "\n".join(b.replier.to(DM_CHAT)), "建不了群时没退回单聊对话模式"


def test_old_send_card_still_gets_the_enter_button(here_dir):
    """升级前发出去的旧详情卡（只有回调）：点了照旧在单聊里给进群按钮，不能变成没反应。"""
    b, _claude, _groups, gid = _with_group(here_dir)
    _click(b, "send")
    assert LINK_PREFIX + gid in _dm_said(b)


def test_first_time_send_creates_group_and_gives_enter_button(here_dir):
    """首次（没群）：照旧建群 + 单聊里一个进群按钮（两下，原因见模块说明）。"""
    b, _claude, groups = _make(here_dir)
    _click(b, "send")
    assert len(groups.created) == 1
    assert LINK_PREFIX + groups.created[0][0] in _dm_said(b)


# ---------- 调用点还在（查调用形状，不查名字）----------


def test_call_sites_are_wired():
    from bridge.app import Bridge

    detail = inspect.getsource(Bridge._fleet_detail)
    assert "group_link=self._session_group_link(scope, sess)" in detail
    click = inspect.getsource(Bridge._on_fleet_click)
    assert "self._open_session_group(chat_id, scope, clicker, tool, sid, jumped=op == \"go\")" in click
    opener = inspect.getsource(Bridge._open_session_group)
    assert "elif jumped:" in opener
    cards_src = inspect.getsource(build_detail_card)
    assert "jump_callback_button(" in cards_src
