"""第 19 轮 S3：「在办」表情在每一条收消息的路上，贴没贴、撤没撤、该不该贴。

全部**真跑一遍**：把一条假飞书事件喂给 `on_message`，把派发出去的协程真跑完，
最后看假飞书上那条消息**还挂着几个表情**。不查源码里有没有 `ack` 这个名字
（第 18 轮那条接线测试就是靠名字找位置，找错了还是绿的）。

假飞书只认表情的 create / delete 两个接口，记下每条消息上现在挂着哪些 reaction_id。
`Replier.ack/unack` 用的是**真的那份**（bridge/reply.py），只把 SDK client 换成假的 ——
reply.py 里的修补（在路上就被撤、重复贴）也一并被测到。

一条飞书写请求都不发。
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.reply import Replier  # noqa: E402

OWNER = "ou_owner"


# ---------- 假飞书：只有表情接口 ----------


class FakeReactions:
    """记每条消息上**现在**挂着哪些表情。`gate` 给了 ⇒ create 卡在那儿，直到 gate 被 set（模拟 HTTP 在路上）。"""

    def __init__(self, gate: threading.Event | None = None):
        self.on: dict[str, set] = {}
        self.creates: list = []
        self.deletes: list = []
        self.gate = gate
        self.entered = threading.Event()
        self._n = 0
        self._lock = threading.Lock()
        outer = self

        class _R:
            def create(self, request):
                outer.entered.set()
                if outer.gate is not None:
                    outer.gate.wait(5)
                with outer._lock:
                    outer._n += 1
                    rid = f"rx_{outer._n}"
                    outer.creates.append(request.message_id)
                    outer.on.setdefault(request.message_id, set()).add(rid)
                return types.SimpleNamespace(success=lambda: True, code=0,
                                             data=types.SimpleNamespace(reaction_id=rid))

            def delete(self, request):
                with outer._lock:
                    outer.deletes.append((request.message_id, request.reaction_id))
                    outer.on.get(request.message_id, set()).discard(request.reaction_id)
                return types.SimpleNamespace(success=lambda: True, code=0)

        self.im = types.SimpleNamespace(v1=types.SimpleNamespace(message_reaction=_R()))

    def hanging(self, mid: str) -> int:
        """这条消息上现在还挂着几个表情。"""
        return len(self.on.get(mid, set()))


# 「跑的时候挂着没有」要等一下再看：贴表情是丢后台发的，主会话也可能改成「过一小会儿再贴」
# （秒回的不闪，见 docs/iteration/r19-ack.md）。最多等 3 秒，看到就停。
_WAIT = 3.0


def _shows_up(rx: "FakeReactions", mid: str) -> int:
    import time

    end = time.monotonic() + _WAIT
    while time.monotonic() < end and rx.hanging(mid) == 0:
        time.sleep(0.01)
    return rx.hanging(mid)


async def _ashows_up(rx: "FakeReactions", mid: str) -> int:
    import time

    end = time.monotonic() + _WAIT
    while time.monotonic() < end and rx.hanging(mid) == 0:
        await asyncio.sleep(0.01)
    return rx.hanging(mid)


def _acking(replier, reactions: FakeReactions):
    """给测试用的假 replier 装上**真的** ack / unack（reply.py 那份），发消息部分保持原样。"""
    real = Replier(reactions, max_chars=2000)
    replier.ack = real.ack
    replier.unack = real.unack
    replier.pending_ack = real.pending_ack
    return replier


def _event(mid, *, chat_id, chat_type="p2p", mtype="text", text="把登录改掉", content=None):
    if content is None:
        content = ('{"text": "%s"}' % text) if mtype == "text" else '{"image_key": "img_x"}'
    return types.SimpleNamespace(event=types.SimpleNamespace(
        sender=types.SimpleNamespace(sender_type="user",
                                     sender_id=types.SimpleNamespace(open_id=OWNER)),
        message=types.SimpleNamespace(message_id=mid, chat_id=chat_id, chat_type=chat_type,
                                      message_type=mtype, content=content, mentions=None),
    ))


def _deliver(b, monkeypatch, msg):
    """喂一条事件给 on_message，再把它派发出去的协程**真跑完**。返回跑了几个。"""
    import bridge.app as app_mod

    captured: list = []
    monkeypatch.setattr(app_mod.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: captured.append(coro))
    b.on_message(msg)
    monkeypatch.undo()
    for coro in captured:
        asyncio.run(coro)
    return len(captured)


def _prep(b, tmp_path):
    """on_message 要的那几个字段。`_loop` 给个非 None、又没有 call_soon_threadsafe 的东西 ⇒ 表情同步贴。"""
    b._seen_messages = {}
    b._loop = object()
    # 🔴 config_path 指到测试目录：群消息证明之类的落盘不许退回真实的 ~/.carher-feishu-agent
    b.settings.config_path = str(tmp_path / "config.toml")
    return b


# ---------- 单聊：桥自己的 claude 跑一轮（阳性对照：第 18 轮就接好的那条） ----------


def _dm_bridge(tmp_path, adapter=None):
    from test_relay_integration import RelayClaude, _bridge, _live

    claude = adapter or RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    reactions = FakeReactions()
    _acking(b.replier, reactions)
    return _prep(b, tmp_path), claude, reactions


def test_local_turn_is_marked_while_running_and_cleared_after(tmp_path, monkeypatch):
    from test_relay_integration import CHAT

    b, _claude, rx = _dm_bridge(tmp_path)
    during = []

    async def run_one(session, chat_id, text):
        during.append(await _ashows_up(rx, "om_local"))

    b._run_one = run_one
    assert _deliver(b, monkeypatch, _event("om_local", chat_id=CHAT, text="看看有哪些项目")) == 1
    assert during == [1], "桥自己跑的那一轮，跑的时候没挂「在办」"
    assert rx.hanging("om_local") == 0, "跑完了「在办」还挂着"


# ---------- 🔴 对话模式：发到电脑上的会话，等那边回 ----------


def test_relay_to_desktop_session_is_marked_until_the_reply_comes_back(tmp_path, monkeypatch):
    """最需要「在办」的一条：消息进了电脑上的会话，回复要等那边跑完（实测一次 99 秒）。

    贴：发进去的那一刻（adapter.relay 在跑）消息上必须挂着；撤：回复贴回来之后必须没了。
    """
    from test_relay_integration import CHAT, SCOPE, _talk

    b, claude, rx = _dm_bridge(tmp_path)
    b.fleet.overview()
    _talk(b)
    assert b.fleet.peek_pending(SCOPE) is not None, "夹具没进对话模式"
    during = []
    real_relay = claude.relay

    def relay(sid, text, timeout=120.0, mode=None):
        during.append(_shows_up(rx, "om_relay"))
        return real_relay(sid, text, timeout=timeout, mode=mode)

    claude.relay = relay
    _deliver(b, monkeypatch, _event("om_relay", chat_id=CHAT))
    assert [t for _, t in claude.relayed] == ["把登录改掉"], "这句没走对话模式"
    assert during == [1], "发到电脑会话、等它回的那段时间没挂「在办」"
    assert rx.hanging("om_relay") == 0, "电脑会话回完了「在办」还挂着"


def test_relay_that_fails_still_clears(tmp_path, monkeypatch):
    """电脑那边没送达 / 超时 ⇒ 也要撤（出问题的那条最需要看清状态）。"""
    from test_relay_integration import CHAT, RelayClaude, _live, _talk

    claude = RelayClaude([_live()])
    b, _c, rx = _dm_bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)

    def relay(sid, text, timeout=120.0, mode=None):
        raise RuntimeError("电脑那边炸了")

    claude.relay = relay
    _deliver(b, monkeypatch, _event("om_relay_boom", chat_id=CHAT))
    assert rx.hanging("om_relay_boom") == 0, "发送炸了之后「在办」一直挂着"


# ---------- 群：绑了会话的群（一个群 = 一个会话） ----------


def test_group_bound_to_a_session_is_marked_while_relaying(tmp_path, monkeypatch):
    """群里主人说一句 ⇒ 进那个会话。每句都会有回复，所以「在办」不比回复更吵；只给主人的话贴。"""
    from test_r11_auto_enter import _fresh

    b, claude, gid = _fresh(tmp_path)
    rx = FakeReactions()
    _acking(b.replier, rx)
    _prep(b, tmp_path)
    during = []
    real_relay = claude.relay

    def relay(sid, text, timeout=120.0, mode=None):
        during.append(_shows_up(rx, "om_group"))
        return real_relay(sid, text, timeout=timeout, mode=mode)

    claude.relay = relay
    _deliver(b, monkeypatch, _event("om_group", chat_id=gid, chat_type="group"))
    assert claude.relayed, "群里这句没进绑定的会话"
    assert during == [1], "群里发到会话、等它回的时候没挂「在办」"
    assert rx.hanging("om_group") == 0, "群里回完了「在办」还挂着"


def test_group_message_from_someone_else_gets_nothing(tmp_path, monkeypatch):
    """群里别人说话 ⇒ 不贴（鉴权在前，第 15 轮那条红线）。"""
    from test_r11_auto_enter import _fresh

    b, _claude, gid = _fresh(tmp_path)
    rx = FakeReactions()
    _acking(b.replier, rx)
    _prep(b, tmp_path)
    msg = _event("om_other", chat_id=gid, chat_type="group")
    msg.event.sender.sender_id.open_id = "ou_colleague"
    _deliver(b, monkeypatch, msg)
    assert rx.creates == [], "给群里别人的话贴了表情"


# ---------- 去重 ----------


def test_redelivered_message_is_not_marked_twice(tmp_path, monkeypatch):
    from test_relay_integration import CHAT

    b, _claude, rx = _dm_bridge(tmp_path)
    _deliver(b, monkeypatch, _event("om_dup", chat_id=CHAT, text="你好"))
    _deliver(b, monkeypatch, _event("om_dup", chat_id=CHAT, text="你好"))
    assert rx.creates.count("om_dup") <= 1, "飞书重推的同一条又贴了一次"
    assert rx.hanging("om_dup") == 0


def test_redelivery_after_loop_not_ready_is_marked_once(tmp_path, monkeypatch):
    """事件循环没起来时进来一条（不记去重，等重推）⇒ 重推时**不许再贴一个**。

    🔴 原来第二次 ack 会再 create 一次，并把第一次的 reaction_id 覆盖掉 ⇒ 第一个表情永远撤不掉。
    修在 reply.py：同一条消息已经挂着 / 正在贴 ⇒ 不再贴。
    """
    from test_relay_integration import CHAT

    b, _claude, rx = _dm_bridge(tmp_path)
    b._loop = None
    _deliver(b, monkeypatch, _event("om_early", chat_id=CHAT, text="你好"))
    b._loop = object()
    _deliver(b, monkeypatch, _event("om_early", chat_id=CHAT, text="你好"))
    assert rx.creates.count("om_early") <= 1, "重推时又贴了一个"
    assert rx.hanging("om_early") == 0, "处理完了还挂着（第一次贴的那个撤不掉了）"


# ---------- 🔴 贴了、但这条路根本不派发 ⇒ 永远没人撤 ----------
#
# 这几条要改的是 bridge/app.py::on_message（贴表情的位置），归主会话。
# 方案见 docs/iteration/r19-ack.md。用 strict xfail：app.py 改好之后它们会 XPASS ⇒ 报错 ⇒
# 那时把下面这个标记删掉（不许留着，留着就看不见它再坏）。


@pytest.mark.parametrize("mtype", ["image", "file", "audio", "sticker"])
def test_non_text_message_is_not_left_marked(tmp_path, monkeypatch, mtype):
    """发图片 / 文件 / 语音 / 表情包 ⇒ 不进后台，也就没有 finally 去撤 ⇒ 「在办」挂一辈子。"""
    from test_relay_integration import CHAT

    b, _claude, rx = _dm_bridge(tmp_path)
    _deliver(b, monkeypatch, _event(f"om_{mtype}", chat_id=CHAT, mtype=mtype))
    assert rx.hanging(f"om_{mtype}") == 0, f"{mtype} 消息上的「在办」永远不会撤"


def test_message_without_readable_text_is_not_left_marked(tmp_path, monkeypatch):
    """富文本（post）现在读不出正文 ⇒ 当没话说丢掉；丢掉的话就不该挂着「在办」。"""
    from test_relay_integration import CHAT

    post = '{"title": "", "content": [[{"tag": "text", "text": "把登录改掉"}]]}'
    b, _claude, rx = _dm_bridge(tmp_path)
    _deliver(b, monkeypatch, _event("om_post", chat_id=CHAT, mtype="post", content=post))
    assert rx.hanging("om_post") == 0, "丢掉的那条挂着「在办」，看着像还在干"


def test_loop_not_ready_leaves_nothing_marked(tmp_path, monkeypatch):
    """事件循环没起来 ⇒ 这一条先不处理（等重推）⇒ 这时候不该已经挂上「在办」。"""
    from test_relay_integration import CHAT

    b, _claude, rx = _dm_bridge(tmp_path)
    b._loop = None
    _deliver(b, monkeypatch, _event("om_noloop", chat_id=CHAT, text="你好"))
    assert rx.hanging("om_noloop") == 0, "没处理的那条挂着「在办」；飞书不重推的话就一直挂着"


# ---------- 秒回的命令：贴了马上撤 = 闪一下 ----------


@pytest.mark.parametrize("cmd", ["/stop", "/help", "菜单"])
def test_instant_command_is_not_flashed(tmp_path, monkeypatch, cmd):
    """/stop、/help、菜单这种当场就回的：回复本身就是「收到了」，再贴一下撤一下只是闪 + 多两次 HTTP。

    🔴 判据只看「这条最后有没有被当成命令处理掉」—— 不能按「以 / 开头」判：
    `/Users/me/codes 里有什么` 是个普通问题，要贴（见下一条阳性对照）。
    """
    from test_relay_integration import CHAT

    b, _claude, rx = _dm_bridge(tmp_path)
    _deliver(b, monkeypatch, _event("om_cmd", chat_id=CHAT, text=cmd))
    assert rx.creates == [], f"{cmd} 当场就回了，还贴了一下「在办」"


def test_slash_path_question_is_still_marked(tmp_path, monkeypatch):
    """阳性对照：以 / 开头、但其实是个问题的那句，照样要贴（它要跑一轮）。"""
    from test_relay_integration import CHAT

    b, _claude, rx = _dm_bridge(tmp_path)
    during = []

    async def run_one(session, chat_id, text):
        during.append(await _ashows_up(rx, "om_path"))

    b._run_one = run_one
    _deliver(b, monkeypatch, _event("om_path", chat_id=CHAT, text="/Users/me/codes 里有什么"))
    assert during == [1], "以 / 开头的普通问题跑的时候没挂「在办」"
    assert rx.hanging("om_path") == 0


# ---------- reply.py：贴还在路上就被撤 ----------


def test_unack_while_ack_is_still_in_flight_still_clears():
    """🔴 贴表情是丢后台线程发的，秒回的那一轮可能**先跑完**：撤的时候 reaction_id 还没回来 ⇒
    原来 unack 什么都不做就返回，接着 ack 回来把 id 记下 ⇒ 这条永远挂着「在办」。
    """
    gate = threading.Event()
    rx = FakeReactions(gate=gate)
    r = Replier(rx, max_chars=2000)
    t = threading.Thread(target=r.ack, args=("om_race",))
    t.start()
    assert rx.entered.wait(5), "假 create 没被调到"
    r.unack("om_race")          # 这一轮已经跑完，贴表情的 HTTP 还在路上
    gate.set()
    t.join(5)
    assert rx.hanging("om_race") == 0, "贴还在路上时撤 ⇒ 表情最后还是挂上了"
    assert r.pending_ack("om_race") is None


def test_ack_after_unack_of_a_finished_message_is_a_fresh_ack():
    """阳性对照：撤完之后同一条再贴（不该发生，但发生了也别卡住）⇒ 照常贴上、照常能撤。"""
    rx = FakeReactions()
    r = Replier(rx, max_chars=2000)
    r.ack("om_x")
    r.unack("om_x")
    assert rx.hanging("om_x") == 0
    assert r.ack("om_x") is True
    assert rx.hanging("om_x") == 1
    r.unack("om_x")
    assert rx.hanging("om_x") == 0


def test_ack_failure_while_in_flight_does_not_poison_the_next_one():
    """贴失败了 ⇒ 「在路上」的记号要清掉，不然这条消息以后再也贴不上 / 撤的时候误以为还在路上。"""

    class Boom(FakeReactions):
        def __init__(self):
            super().__init__()
            self.fail = True
            real = self.im.v1.message_reaction
            outer = self

            class _R:
                def create(self, request):
                    if outer.fail:
                        raise RuntimeError("网络断了")
                    return real.create(request)

                def delete(self, request):
                    return real.delete(request)

            self.im = types.SimpleNamespace(v1=types.SimpleNamespace(message_reaction=_R()))

    rx = Boom()
    r = Replier(rx, max_chars=2000)
    assert r.ack("om_f") is False
    rx.fail = False
    assert r.ack("om_f") is True, "失败一次之后这条再也贴不上了"
    assert rx.hanging("om_f") == 1, "失败一次之后再贴，其实没真贴上（被当成还在路上）"
    r.unack("om_f")
    assert rx.hanging("om_f") == 0
