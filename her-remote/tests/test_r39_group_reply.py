"""第 39 轮（用户 11-19）：群里什么时候回。

  ① 只有主人 + 本机器人的群 ⇒ 主人说什么都回（不用 @）
  ② 别的群（多人群、拉了别的机器人的群）⇒ 只有 @ 了本机器人才回

人数靠 `GET /im/v1/chats/:id` 的 `user_count` / `bot_count`（11-19 测试群实测：只有主人时 `'1'` / `'1'`）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import OWNER, _fake_bridge  # noqa: E402

BOT = "ou_bot_her_remote_00000000000000000"
COLLEAGUE = "ou_colleague0000000000000000000000"
STRANGER = "ou_stranger0000000000000000000000"


def _mention(open_id, key="@_user_1"):
    return types.SimpleNamespace(key=key, id=types.SimpleNamespace(open_id=open_id), name="x")


def _msg(*, sender=OWNER, chat="oc_g", mentions=None, mid="om_1", text="在吗", mtype="text"):
    content = f'{{"text":"{text}"}}' if mtype == "text" else '{"image_key":"img_x"}'
    return types.SimpleNamespace(event=types.SimpleNamespace(
        sender=types.SimpleNamespace(sender_type="user",
                                     sender_id=types.SimpleNamespace(open_id=sender)),
        message=types.SimpleNamespace(message_id=mid, chat_id=chat, chat_type="group",
                                      message_type=mtype, content=content, mentions=mentions)))


class _Client:
    """假的飞书：按群给人数；记下问了几次（看缓存）。"""

    def __init__(self, groups, bot_open_id=BOT, fail=False):
        self.groups, self.bot_open_id, self.fail, self.calls = groups, bot_open_id, fail, []

    def _authed(self, method, path, body=None):
        self.calls.append(path)
        if path == "/bot/v3/info":
            return {"code": 0, "bot": {"open_id": self.bot_open_id}} if self.bot_open_id else {"code": 99991672}
        if self.fail:
            return {"code": 99991672, "msg": "no scope"}
        users, bots = self.groups[path.rsplit("/", 1)[-1]]
        return {"code": 0, "data": {"user_count": str(users), "bot_count": str(bots)}}


def _bridge(tmp, client):
    b = _fake_bridge(tmp)
    b.settings.owner_open_ids = frozenset({OWNER})
    b._seen_messages = {}
    b.sent = []
    b._say = lambda chat_id, text: b.sent.append(text) or True
    b._say_later = lambda chat_id, text: b.sent.append(text) or True
    b.replier.send = lambda chat_id, text: b.sent.append(text) or True
    b.group_client = client
    b.chat_binds = None
    b.dispatched = []
    return b


def _run(b, msg):
    import bridge.app as app_mod

    def fake(coro, loop):
        b.dispatched.append(coro)
        coro.close()

    orig = app_mod.asyncio.run_coroutine_threadsafe
    app_mod.asyncio.run_coroutine_threadsafe = fake
    try:
        b._loop = object()
        b.on_message(msg)
    finally:
        app_mod.asyncio.run_coroutine_threadsafe = orig
    return len(b.dispatched)


def _new(groups, **kw):
    tmp = tempfile.mkdtemp()
    return _bridge(tmp, _Client(groups, **kw))


# ── ① 只有主人 + 本机器人 ──

def test_private_group_answers_without_mention():
    b = _new({"oc_g": (1, 1)})
    assert _run(b, _msg()) == 1, "只有主人和机器人的群，主人不 @ 也该回"


def test_private_group_also_answers_with_mention():
    b = _new({"oc_g": (1, 1)})
    assert _run(b, _msg(mentions=[_mention(BOT)])) == 1


# ── ② 多人群 / 拉了别的机器人 ──

def test_multi_person_group_ignores_owner_without_mention():
    b = _new({"oc_g": (3, 1)})
    assert _run(b, _msg()) == 0, "多人群里主人没 @ 它，不该回"
    assert not b.sent, f"多人群里没 @ 也不该说任何话：{b.sent}"


def test_multi_person_group_answers_when_owner_mentions_the_bot():
    b = _new({"oc_g": (3, 1)})
    assert _run(b, _msg(mentions=[_mention(BOT)])) == 1


def test_multi_person_group_ignores_mention_of_someone_else():
    """主人在多人群里 @ 了同事（不是 @ 它）⇒ 不回。"""
    b = _new({"oc_g": (3, 1)})
    assert _run(b, _msg(mentions=[_mention(COLLEAGUE)])) == 0


def test_at_all_is_not_a_mention_of_me():
    b = _new({"oc_g": (3, 1)})
    at_all = types.SimpleNamespace(key="@_all", id=types.SimpleNamespace(open_id=""), name="所有人")
    assert _run(b, _msg(mentions=[at_all])) == 0


def test_group_with_another_bot_counts_as_multi():
    """用户原话「只有主人和 her-remote 的群」⇒ 多拉了一个别的机器人就要 @。"""
    b = _new({"oc_g": (1, 2)})
    assert _run(b, _msg()) == 0
    assert _run(b, _msg(mid="om_2", mentions=[_mention(BOT)])) == 1


def test_multi_person_group_silent_on_images_too():
    """多人群里没 @ 的截图不该回「我只看得懂文字」。"""
    b = _new({"oc_g": (3, 1)})
    assert _run(b, _msg(mtype="image")) == 0
    assert not b.sent, b.sent


def test_colleague_mentioning_the_bot_is_still_ignored():
    """非主人照旧静默：同事 @ 它也不回（回了等于让他指挥主人的电脑）。"""
    b = _new({"oc_g": (3, 1)})
    assert _run(b, _msg(sender=COLLEAGUE, mentions=[_mention(BOT)])) == 0
    assert not b.sent


def test_private_dm_unaffected():
    """单聊不走这套：不问人数，直接回。"""
    b = _new({})
    m = _msg()
    m.event.message.chat_type = "p2p"
    assert _run(b, m) == 1
    assert not [c for c in b.group_client.calls if c.startswith("/im/v1/chats")]


# ── 查不出来时的兜底 ──

def test_unknown_member_count_requires_mention():
    """没开「查看群信息」⇒ 按多人群算（要 @），宁可漏回不乱回。"""
    b = _new({}, fail=True)
    assert _run(b, _msg()) == 0
    assert _run(b, _msg(mid="om_2", mentions=[_mention(BOT)])) == 1


def test_unknown_member_count_but_bridge_made_session_group_answers():
    """桥自己建的会话群（建的时候只拉了主人）：「每一句都直达」是答应过的，查不出人数也不能突然要 @。"""
    b = _new({}, fail=True)
    b.chat_binds = types.SimpleNamespace(of_chat=lambda cid: object() if cid == "oc_g" else None)
    assert _run(b, _msg()) == 1


def test_unknown_bot_identity_treats_any_mention_as_me():
    """拿不到自己的 open_id ⇒ 有 @ 就当 @ 我：主人 @ 了它却石沉大海更糟。"""
    b = _new({"oc_g": (3, 1)}, bot_open_id="")
    assert _run(b, _msg(mentions=[_mention(COLLEAGUE)])) == 1


def test_member_count_is_cached_but_expires():
    """一分钟内不重复问；过了一分钟重新问（有人被拉进来要能感知到）。"""
    b = _new({"oc_g": (1, 1)})
    _run(b, _msg(mid="om_1"))
    _run(b, _msg(mid="om_2"))
    asks = [c for c in b.group_client.calls if c.startswith("/im/v1/chats")]
    assert len(asks) == 1, f"一分钟内问了 {len(asks)} 次"
    # 有人被拉进群，缓存过期后要变成「要 @」
    b.group_client.groups["oc_g"] = (2, 1)
    cid = "oc_g"
    ts, v = b._group_private_cache[cid]
    b._group_private_cache[cid] = (ts - b.GROUP_PRIVATE_TTL - 1, v)
    assert _run(b, _msg(mid="om_3")) == 2, "拉了人进来、缓存过期之后，没 @ 的那句还被回了"
    assert b._group_private_cache[cid][1] is False, "拉了人进来之后还当成私群"


def test_at_all_is_not_me_even_when_bot_identity_unknown():
    """认不出自己时「有 @ 就当 @ 我」—— 但 @所有人 不算（不然多人群里每条群公告都会回）。"""
    b = _new({"oc_g": (3, 1)}, bot_open_id="")
    at_all = types.SimpleNamespace(key="@_all", id=types.SimpleNamespace(open_id=""), name="所有人")
    assert _run(b, _msg(mentions=[at_all])) == 0
