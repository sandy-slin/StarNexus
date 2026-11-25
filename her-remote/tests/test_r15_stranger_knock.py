"""第 15 轮：有人跟这台电脑的机器人说话，而他不是主人 —— 两边都抓不到任何线索。

现场（2026-11-06 21:34/21:35，主人这台的真实日志）：
    INFO feishu-bridge 丢弃非授权用户消息 open_id=ou_efcdd50889eda96153178083fed34463
领导原话：「他什么消息都没有反应」。

**静默本身不改**：回陌生人等于确认「这个机器人背后有东西在听」（防探测）。
要改的是另一半 —— **主人完全不知道有人在敲门**。他手上唯一能看的是那张诊断卡，
而那张卡上没有这一项，于是「两个人用了同一个机器人」这种事谁都查不出来，
只能靠我去翻他的日志（今天就是这么过的，而且我两次把猜测当结论说出去）。

所以：静默照旧 + 记一笔 + 诊断卡上显示「有人在找这台电脑」。
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import OWNER, _fake_bridge  # noqa: E402

STRANGER = "ou_stranger0000000000000000000000"


def _msg(open_id, text="在吗", mid="om_1"):
    return types.SimpleNamespace(
        event=types.SimpleNamespace(
            sender=types.SimpleNamespace(sender_type="user",
                                        sender_id=types.SimpleNamespace(open_id=open_id)),
            message=types.SimpleNamespace(
                message_id=mid, chat_id="oc_x", chat_type="p2p",
                message_type="text", content=f'{{"text":"{text}"}}', mentions=None),
        )
    )


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.sent = []
    b.replier.send = lambda chat_id, text: b.sent.append((chat_id, text)) or True
    b._say = lambda chat_id, text: b.sent.append((chat_id, text)) or True
    return b


def test_stranger_gets_no_reply():
    """不回陌生人（防探测）—— 这一条不许被改松。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.on_message(_msg(STRANGER))
        assert b.sent == [], f"回了陌生人：{b.sent}"


def test_stranger_knock_is_recorded_for_the_owner():
    """主人要能看见「有人在找这台电脑」。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.on_message(_msg(STRANGER))
        knocks = b.stranger_knocks()
        assert knocks["count"] == 1, knocks
        assert knocks["last_at"] > 0


def test_repeated_knocks_are_counted_not_duplicated():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        for i in range(3):
            b.on_message(_msg(STRANGER, mid=f"om_{i}"))
        assert b.stranger_knocks()["count"] == 3


def test_owner_message_does_not_count_as_a_knock():
    """阳性对照：主人自己说话不算敲门，否则这一项永远亮着。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.settings.owner_open_ids = frozenset({OWNER})
        b._seen_messages = {}
        b._handle = lambda *a, **kw: None   # 不真跑一轮，只看有没有被记成敲门
        try:
            b.on_message(_msg(OWNER))
        except Exception:
            pass   # 假 bridge 没装全后面那条路；这条用例只关心敲门计数
        assert b.stranger_knocks()["count"] == 0


def test_health_card_shows_the_knock_in_plain_words():
    """诊断卡上要有这一项，而且是人话 —— 不许出现 open_id 这种内部名词。"""
    from bridge import status as status_mod

    assert any(k == "knocks" for k, _ in status_mod.HEALTH_FIELDS)
    line = dict(status_mod.HEALTH_FIELDS)["knocks"]
    assert "open_id" not in line and "ou_" not in line
