"""第 18 轮：发完消息立刻给个「收到」的表情，别让人干等。

用户原话：「我发消息后最后有个反应 比如 get 符号 你可以参考 openclaw」

## 为什么值得做

手机上发一句话，助手要十几秒到几分钟才回。这段时间屏幕上**什么都没有** ——
用户分不出「它收到了正在干」和「它根本没收到」。今天一整天那几次「没反应」，
有一半的调查成本就花在这个区分上（见 `docs/iteration/r15-identity.md`）。
一个表情是最便宜的「我收到了」。

## 判据

- 🔴 **不能挡住正事**：表情发失败、超时、没权限，都只是少一个表情，绝不影响这一轮
  （同 `_greet_in_background` 的红字：少一条说明 ≠ 整个功能没了）。
- 🔴 **3 秒纪律**：飞书回调/事件要 3 秒内返回。表情是一次 HTTP，必须丢后台，不能在
  `on_message` 里同步等（`bridge/app.py` 文件头那条）。
- 只给**主人**的消息加（陌生人照旧静默，防探测 —— 第 15 轮那条红线）。
- 一条消息只加一次（飞书对重复表情会报错，而且刷屏）。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest

from bridge.reply import Replier


class _FakeClient:
    """只认表情那一个接口；其余调用一律炸，免得测试悄悄走了别的路。"""

    def __init__(self, fail=False, boom=False):
        self.calls = []
        self._fail = fail
        self._boom = boom

    class _Im:
        def __init__(self, outer):
            self._outer = outer

        class _V1:
            def __init__(self, outer):
                self.message_reaction = outer

        @property
        def v1(self):
            return self._V1(self._outer)

    @property
    def im(self):
        return self._Im(_Reaction(self))


class _Reaction:
    def __init__(self, client):
        self._client = client

    def create(self, request):
        if self._client._boom:
            raise RuntimeError("网络炸了")
        self._client.calls.append(request)
        return type("R", (), {"success": lambda self_: not self._client._fail,
                              "code": 0 if not self._client._fail else 99991672,
                              "msg": "ok"})()


def _replier(**kw):
    return Replier(_FakeClient(**kw), max_chars=2000)


def test_ack_sends_a_reaction():
    r = _replier()
    assert r.ack("om_123") is True
    assert len(r._client.calls) == 1


def test_ack_failure_is_swallowed():
    """🔴 没权限 / 接口拒绝 ⇒ 返回 False，但**不许抛**（少一个表情不能影响这一轮）。"""
    r = _replier(fail=True)
    assert r.ack("om_123") is False


def test_ack_exception_is_swallowed():
    r = _replier(boom=True)
    assert r.ack("om_123") is False


def test_ack_needs_a_message_id():
    r = _replier()
    assert r.ack("") is False
    assert r.ack(None) is False
    assert r._client.calls == []


# 第 19 轮：原来这里两条按源码文本找 "ack" 的测试已删（改完后匹配上的是函数名，判据失效）。
# 行为由 tests/test_r19_ack_coverage.py 真跑兜着：陌生人不贴、只有派发出去的才贴、办完必撤。


# ---------- 办完了把「在办」撤掉 ----------
#
# 用户原话：「办完了应该表情应该取消啊」。
# 「在办」一直挂着 = 他回头翻聊天记录时分不出哪条还在跑、哪条早就完了。
# 删除接口要的是**加的时候拿到的 reaction_id**（`DELETE …/reactions/:reaction_id`），
# 所以 `ack()` 要把它存下来。权限项和加表情是同一个（「发送、删除消息表情回复」），
# 不用新申请。


class _AckClient:
    """加表情返回一个 reaction_id，删除时记下删的是哪个。"""

    def __init__(self, fail_delete=False):
        self.created = []
        self.deleted = []
        self._fail_delete = fail_delete
        outer = self

        class _R:
            def create(self, request):
                outer.created.append(request)
                return type("Resp", (), {
                    "success": lambda s: True, "code": 0,
                    "data": type("D", (), {"reaction_id": "rx_42"})()})()

            def delete(self, request):
                outer.deleted.append((request.message_id, request.reaction_id))
                ok = not outer._fail_delete
                return type("Resp", (), {"success": lambda s: ok, "code": 0 if ok else 500})()

        class _V1:
            message_reaction = _R()

        self.im = type("IM", (), {"v1": _V1()})()


def test_ack_remembers_the_reaction_id():
    r = Replier(_AckClient(), max_chars=2000)
    assert r.ack("om_1") is True
    assert r.pending_ack("om_1") == "rx_42", "没记下 reaction_id ⇒ 之后删不掉"


def test_done_removes_the_ack():
    client = _AckClient()
    r = Replier(client, max_chars=2000)
    r.ack("om_1")
    assert r.unack("om_1") is True
    assert client.deleted == [("om_1", "rx_42")], "删的不是加的那一个"
    assert r.pending_ack("om_1") is None, "删完还记着 ⇒ 下次会重复删"


def test_unack_without_ack_does_nothing():
    """从没加过（没权限、加失败）⇒ 什么都不删，也不报错。"""
    client = _AckClient()
    r = Replier(client, max_chars=2000)
    assert r.unack("om_never") is False
    assert client.deleted == []


def test_unack_failure_is_swallowed():
    """删失败只是多挂一个表情，不能影响这一轮已经发出去的回答。"""
    r = Replier(_AckClient(fail_delete=True), max_chars=2000)
    r.ack("om_1")
    assert r.unack("om_1") is False


def test_unack_is_called_when_the_turn_ends():
    """一轮结束 ⇒ 撤掉「在办」。**真跑一遍**，不查源码文本。

    🔴 第一版我写成「源码里 `unack` 附近有没有 `finally`」—— 它找到的是调用处
    `_handle_then_unack` 这个名字里的 `unack`，离真正那段 finally 很远，判据就错了。
    今天第四次犯「拿文本位置当行为」这个病，所以这条直接跑真函数。
    """
    import asyncio
    import tempfile

    from test_interference import _fake_bridge

    with tempfile.TemporaryDirectory() as tmp:
        b = _fake_bridge(tmp)
        undone = []
        b.replier.unack = lambda mid: undone.append(mid) or True

        async def ok(scope, chat_id, text):
            return None

        b._handle = ok
        asyncio.run(b._handle_then_unack("dm:owner", "oc_1", "你好", "om_1"))
        assert undone == ["om_1"], "跑完一轮没撤掉「在办」"


def test_unack_still_happens_when_the_turn_blows_up():
    """🔴 出错的那一轮**更要**撤 —— 恰恰是出问题的那几条最需要看清状态。"""
    import asyncio
    import tempfile

    import pytest

    from test_interference import _fake_bridge

    with tempfile.TemporaryDirectory() as tmp:
        b = _fake_bridge(tmp)
        undone = []
        b.replier.unack = lambda mid: undone.append(mid) or True

        async def boom(scope, chat_id, text):
            raise RuntimeError("这一轮炸了")

        b._handle = boom
        with pytest.raises(RuntimeError):
            asyncio.run(b._handle_then_unack("dm:owner", "oc_1", "你好", "om_2"))
        assert undone == ["om_2"], "出错的那一轮一直挂着「在办」"
