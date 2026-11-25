"""第 15 轮（R2 审计实测）：主人自己发的话被吃掉，或者发了不回。

1. **去重记早了**：`message_id` 先进 `_seen_messages`，再提文字、再派发。中间任何一步出错
   （或事件循环还没起来），飞书会**重推同一个 message_id**（官方：3 秒内要处理完且不抛异常，
   否则触发重推）⇒ 被自己的去重吃掉 ⇒ **永久静默**。R2 实测两种场景各一次：进后台 0、回用户 0。
2. **敲门计数把群里闲聊算进去了**：R2 实测群里 4 条同事发言 ⇒ count=4、people=3。
   这一行存在的意义是「有人找错了机器人」，群噪音让它永远亮着，看了也不信。
3. **发图片 / 文件 / 语音一声不响**：R2 实测 6 种非文本消息全部静默。
   手机上顺手发张截图问「这是什么」是主人今天就会撞的。
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


def _msg(open_id, *, mid="om_1", chat_type="p2p", mtype="text", text="在吗"):
    content = f'{{"text":"{text}"}}' if mtype == "text" else '{"image_key":"img_x"}'
    return types.SimpleNamespace(
        event=types.SimpleNamespace(
            sender=types.SimpleNamespace(sender_type="user",
                                        sender_id=types.SimpleNamespace(open_id=open_id)),
            message=types.SimpleNamespace(
                message_id=mid, chat_id="oc_x", chat_type=chat_type,
                message_type=mtype, content=content, mentions=None),
        )
    )


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.settings.owner_open_ids = frozenset({OWNER})
    b._seen_messages = {}
    b.sent = []
    b._say = lambda chat_id, text: b.sent.append(text) or True
    b.replier.send = lambda chat_id, text: b.sent.append(text) or True
    b.dispatched = []
    import bridge.app as app_mod

    def fake_dispatch(coro, loop):
        b.dispatched.append(coro)
        coro.close()

    b._fake_dispatch = fake_dispatch
    b._app_mod = app_mod
    return b


def _run(b, msg):
    orig = b._app_mod.asyncio.run_coroutine_threadsafe
    b._app_mod.asyncio.run_coroutine_threadsafe = b._fake_dispatch
    try:
        b.on_message(msg)
    finally:
        b._app_mod.asyncio.run_coroutine_threadsafe = orig


# ---------- 1. 去重不能吃掉没处理成的那条 ----------


def test_redelivery_after_loop_not_ready_is_processed():
    """事件循环还没起来时进来一条 ⇒ 没处理成 ⇒ 飞书重推同一个 id 时**必须**能进后台。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._loop = None
        _run(b, _msg(OWNER, mid="om_retry"))
        assert b.dispatched == []
        b._loop = object()                       # 循环起来了
        _run(b, _msg(OWNER, mid="om_retry"))     # 飞书重推同一个 message_id
        assert len(b.dispatched) == 1, "重推被自己的去重吃掉了 —— 主人这句话永远没人处理"


def test_redelivery_after_crash_is_processed():
    """派发前抛异常 ⇒ 这条没处理成 ⇒ 重推必须能进来。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        boom = {"on": True}
        real = b._extract_text

        def flaky(message):
            if boom["on"]:
                raise RuntimeError("模拟入口出错")
            return real(message)

        b._extract_text = flaky
        try:
            _run(b, _msg(OWNER, mid="om_crash"))
        except RuntimeError:
            pass
        boom["on"] = False
        _run(b, _msg(OWNER, mid="om_crash"))
        assert len(b.dispatched) == 1, "出错那条的重推被吃掉了"


def test_real_duplicate_is_still_dropped():
    """阳性对照：**处理成了**的那条，重推照旧去重（否则同一句会跑两遍）。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _run(b, _msg(OWNER, mid="om_dup"))
        _run(b, _msg(OWNER, mid="om_dup"))
        assert len(b.dispatched) == 1


# ---------- 2. 敲门计数只认私聊 ----------


def test_group_chatter_is_not_a_knock():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        for i in range(4):
            _run(b, _msg(STRANGER, mid=f"om_g{i}", chat_type="group"))
        assert b.stranger_knocks()["count"] == 0, "群里同事闲聊被当成「找错机器人」"


def test_private_message_from_stranger_is_a_knock():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _run(b, _msg(STRANGER, mid="om_p"))
        assert b.stranger_knocks()["count"] == 1


# ---------- 3. 非文本消息要回一句 ----------


def test_owner_image_gets_a_reply():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _run(b, _msg(OWNER, mid="om_img", mtype="image"))
        assert b.sent, "主人发了图片，一声不响"
        assert "文字" in b.sent[-1], b.sent


def test_owner_sticker_stays_quiet():
    """发个表情只是在逗，回一句说明反而吵。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _run(b, _msg(OWNER, mid="om_st", mtype="sticker"))
        assert b.sent == []


def test_stranger_image_stays_silent():
    """🔴 陌生人发什么都照旧静默（防探测）。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _run(b, _msg(STRANGER, mid="om_si", mtype="image"))
        assert b.sent == []
