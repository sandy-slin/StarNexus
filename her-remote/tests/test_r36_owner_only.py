"""第 36 轮：「这个机器人只响应我」—— 新会话群里被拉进来的人发话（含 @机器人）不派发。"""
import types

import pytest

from tests.test_auth import OWNER, STRANGER, make_bridge, make_event


def _msg(open_id, mentions):
    e = make_event(open_id=open_id, chat_type="group", message_id=f"om_{open_id}_{bool(mentions)}")
    e.event.message.chat_id = "oc_newsess_group"
    e.event.message.mentions = mentions
    return e


@pytest.mark.parametrize("who,mentions,expected", [
    (OWNER, None, 1),                                                          # 阳性对照
    (STRANGER, None, 0),
    (STRANGER, [types.SimpleNamespace(key="@_user_1", name="bot")], 0),
])
def test_only_owner_is_dispatched_in_a_new_session_group(who, mentions, expected):
    b, dispatched = make_bridge()
    b._note_stranger_knock = lambda oid: None
    b._mark_group_msg_proven = lambda: None
    # 新会话群 = 桥建的、只拉了主人 ⇒ 第 39 轮起不 @ 也回的那种群
    b._group_is_private = lambda chat_id: True
    try:
        b.on_message(_msg(who, mentions))
    finally:
        b._restore()
    assert len(dispatched) == expected
