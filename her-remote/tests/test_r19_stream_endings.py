"""第 19 轮 S1-A1/A2/A3：流式回答卡在「非正常收尾」的三条路上要关掉，不许停在「生成中」。

S1 用真 claude 量出来的现场：
- 只读档等确认（deferred）：卡推上 150 字后一直「生成中」，聊天列表挂着「正在回答…」，
  下面又发了一条**一模一样**的文字，再下面才是审批卡。
- 点「停下」：卡停在半句，从不收尾。
- run_turn 抛异常 / 没跑完：同上。

这里真跑 `Bridge._run_one` + 真 `StreamReply`（bridge/stream.py），只把 runner 换成
一个会先吐几段文字再给结果的假 runner、把飞书 SDK 换成只记账的假 client。
判据看假飞书上**最后一次 settings 调用有没有把 streaming_mode 关掉**、以及聊天里同一段话出现几次。
一条飞书写请求都不发。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.app import Bridge  # noqa: E402
from bridge.approval import ApprovalGate  # noqa: E402
from bridge.policy import Policy  # noqa: E402
from bridge.review import ApprovalStore, Reviewer  # noqa: E402
from bridge.runner import TurnResult  # noqa: E402
from bridge.sessions import SessionStore  # noqa: E402

SCOPE = "dm:ou_x"
SAID = "我先看一下这个目录里有什么文件，然后按你说的把结果整理出来，大概需要一两分钟时间。" * 2


class _Resp:
    def __init__(self, ok=True, code=0, data=None):
        self._ok, self.code, self.data, self.msg = ok, code, data, ""

    def success(self):
        return self._ok


class FakeClient:
    """假飞书：cardkit + im，只记账。"""

    def __init__(self):
        self.pushes: list[str] = []
        self.settings: list[dict] = []
        outer = self

        class _CE:
            def content(self, request):
                outer.pushes.append(request.request_body.content)
                return _Resp(True)

        class _Card:
            def create(self, request):
                return _Resp(True, 0, types.SimpleNamespace(card_id="card_stream"))

            def settings(self, request):
                outer.settings.append(json.loads(request.request_body.settings))
                return _Resp(True)

        class _Msg:
            def create(self, request):
                return _Resp(True, 0, types.SimpleNamespace(message_id="om_stream"))

        self.cardkit = types.SimpleNamespace(v1=types.SimpleNamespace(card=_Card(), card_element=_CE()))
        self.im = types.SimpleNamespace(v1=types.SimpleNamespace(message=_Msg()))

    def closed(self) -> bool:
        """最后一次 settings 把流式关掉了没有。"""
        return bool(self.settings) and \
            self.settings[-1].get("config", {}).get("streaming_mode") is False


class FakeReplier:
    def __init__(self):
        self.sent: list[str] = []
        self.approvals: list[str] = []

    def send(self, chat_id, text):
        self.sent.append(text)
        return True

    def send_status_card(self, chat_id, text, with_stop=True):
        return "om_status"

    def patch_status_card(self, message_id, text, with_stop=True):
        return True

    def send_approval_card(self, chat_id, title, body, approve_value, deny_value):
        self.approvals.append(body)
        return True

    def send_card(self, chat_id, card_json):
        return True


class FakeRunner:
    """先吐一段文字（让流式卡建起来、推上一帧），再按场景给结果 / 抛异常。"""

    def __init__(self, make_result, bridge):
        self.make_result = make_result
        self.bridge = bridge

    async def run_turn(self, text, workspace, *, on_text=None, cancel=None, **_):
        on_text(SAID)
        for _ in range(60):                       # 等 stream_pump 真把这一帧推出去
            await asyncio.sleep(0.05)
            if self.bridge.client.pushes:
                break
        return self.make_result(self.bridge)


def _bridge(tmp, make_result, mode="readonly"):
    policy = Policy(mode=mode, workspaces=(tmp,), default_workspace=tmp,
                    model="claude-sonnet-5-5", turn_timeout_sec=60)
    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(
        owner_open_ids=frozenset({"ou_x"}), policy=policy, custom_rules=(),
        progress_first_notice_sec=90, progress_escalate_sec=420, streaming=True,
    )
    b.client = FakeClient()
    b.replier = FakeReplier()
    b.sessions = SessionStore(tmp, 30)
    b.gate = ApprovalGate(os.path.join(tmp, "approvals"))
    b.approvals = ApprovalStore(b"k" * 32)
    b.reviewer = Reviewer(custom_rules=())
    b._awaiting, b._cancels, b._paused = {}, {}, {}
    b._paused_path = os.path.join(tmp, "paused.json")
    b.prefs = None
    b.said = []
    b._say = lambda chat_id, text: b.said.append(text) or True
    b._say_with_buttons = lambda chat_id, text, buttons: b.said.append(text) or True
    b._send_buttons = lambda chat_id, buttons: True
    b.runner = FakeRunner(make_result, b)
    return b


def _run(b):
    asyncio.run(b._run_one(b.sessions.get(SCOPE), "oc_chat", "帮我建个文件"))


def _times_said(b, text) -> int:
    """这一段话一共到了用户眼前几次：卡上（只要推上过就算一次）+ 消息里。"""
    on_card = 1 if any(text in p for p in b.client.pushes) else 0
    return on_card + sum(1 for s in b.replier.sent if text in s)


def _deferred(b):
    # 照真钩子的样子留一条待办，让 _ask_approval 走到「发审批卡」那一步
    directory = b.gate.dir_for(SCOPE)
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, "d1g3st.pending"), "w", encoding="utf-8") as fh:
        json.dump({"tool_name": "Bash", "tool_input": {"command": "echo hello > out.txt"}}, fh)
    return TurnResult(ok=True, text=SAID, session_id="s1", deferred=True)


def test_deferred_closes_card_and_does_not_repeat_text():
    """A1：等确认 ⇒ 卡收尾（关流），同一段话不在下面再发一遍。"""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, _deferred)
        _run(b)
        assert b.client.pushes, "前提没成立：流式卡一帧都没推"
        assert b.client.closed(), "等确认时回答卡没关 ⇒ 一直「生成中」"
        assert _times_said(b, SAID) == 1, f"同一段话出现了 {_times_said(b, SAID)} 次"


def test_cancel_closes_card_and_does_not_repeat_text():
    """A2：点停下 ⇒ 卡收尾，已经在卡上的那段不再发一条消息。"""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, lambda b: TurnResult(ok=True, text=SAID, cancelled=True), mode="trusted")
        _run(b)
        assert b.client.closed(), "停下后回答卡没关"
        assert _times_said(b, SAID) == 1
        assert len(b.said) == 1, f"停下后应该恰好说一句（告诉他停了），实际 {b.said}"


def test_cancel_with_empty_text_keeps_what_was_shown():
    """停下时 result.text 为空（实测会这样）⇒ 卡上保留已经显示的那段，并关流。"""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, lambda b: TurnResult(ok=True, text="", cancelled=True), mode="trusted")
        _run(b)
        assert b.client.closed()
        assert SAID in b.client.pushes[-1], "停下后卡上的字被清掉了"


def test_failure_closes_card_and_still_reports_error():
    """A3：没跑完 ⇒ 卡收尾，错误照样说出来（不能被卡吞掉）。"""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, lambda b: TurnResult(ok=False, text="", error="超时了"), mode="trusted")
        _run(b)
        assert b.client.closed(), "失败后回答卡没关"
        assert any("超时了" in s for s in b.said), "错误没告诉用户"


def test_exception_closes_card():
    """A3：run_turn 抛异常 ⇒ 异常照旧往上冒，但卡要先关掉。"""
    def boom(b):
        raise RuntimeError("子进程炸了")

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, boom, mode="trusted")
        with pytest.raises(RuntimeError):
            _run(b)
        assert b.client.closed(), "抛异常后回答卡没关"


def test_normal_turn_still_delivers_once():
    """阳性对照：正常跑完 ⇒ 卡交付正文，不额外发消息（老行为不能被这次改坏）。"""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, lambda b: TurnResult(ok=True, text=SAID), mode="trusted")
        _run(b)
        assert b.client.closed()
        assert _times_said(b, SAID) == 1
        assert b.replier.sent == []


def test_status_card_stays_quiet_while_answer_card_is_moving(monkeypatch):
    """S1-A4：回答卡还在冒字 ⇒ 不冒出「还在跑」；回答卡停住 ⇒ 状态卡照常出来（阳性对照）。"""
    import bridge.app as app_mod

    # 比例照线上：刷新间隔（线上 15s）远大于流式节流（0.6s）
    monkeypatch.setattr(app_mod, "_STATUS_REFRESH_SEC", 1.2)

    def run(keep_pushing: bool):
        statuses: list[str] = []

        class Runner:
            def __init__(self, b):
                self.b = b

            async def run_turn(self, text, workspace, *, on_text=None, **_):
                acc = ""
                for i in range(70):          # 3.5 秒
                    if keep_pushing or i == 0:
                        acc += f"第{i}句话写在这里，字数要够二十四个才会推上去一帧。"
                        on_text(acc)
                    await asyncio.sleep(0.05)
                return TurnResult(ok=True, text=acc)

        with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
            b = _bridge(tmp, None, mode="trusted")
            b.settings.progress_first_notice_sec = 1
            b.replier.send_status_card = lambda chat, text, with_stop=True: statuses.append(text) or "om_s"
            b.replier.patch_status_card = lambda mid, text, with_stop=True: statuses.append(text) or True
            b.runner = Runner(b)
            _run(b)
        return statuses

    moving = run(keep_pushing=True)
    stalled = run(keep_pushing=False)
    assert not [s for s in moving if "还在跑" in s], f"回答卡在动还冒状态卡：{moving}"
    assert [s for s in stalled if "还在跑" in s], "阳性对照：回答卡停住了，状态卡却没出来"


def test_stream_card_to_open_id_uses_open_id_type():
    """菜单点击后回复目标是 open_id（ou_）⇒ 流式卡要按 open_id 发，不能写死 chat_id。"""
    from bridge.stream import StreamReply

    seen = []

    class Msg:
        def create(self, request):
            seen.append(request.receive_id_type)
            return _Resp(True, 0, types.SimpleNamespace(message_id="om_x"))

    client = FakeClient()
    client.im.v1.message = Msg()
    for target in ("ou_abc", "oc_abc"):
        s = StreamReply(client, target)
        assert asyncio.run(s.start()), target
    assert seen == ["open_id", "chat_id"], seen


# ====================================================================
# 第 24 轮 R-stream 复现的几条
# ====================================================================


def test_url_password_glued_to_cjk_is_masked_on_card():
    """🔴 「连接串是postgres://admin:口令@…」原来卡上原样显示（URL 规则还在用 \\b）。"""
    secret = "S3cr3tPass"
    text = f"连接串是postgres://admin:{secret}@db.prod.internal:5432/main 。"
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, lambda b: TurnResult(ok=True, text=text), mode="trusted")
        b.runner.make_result = lambda b: TurnResult(ok=True, text=text)
        _run(b)
    assert not any(secret in p for p in b.client.pushes), b.client.pushes[-1]


def test_json_password_with_spaces_never_reaches_a_mid_frame():
    from bridge.stream import _safe_body

    full = '配置是 {"password": "correct horse battery staple"} 好了。'
    for i in range(len(full) + 1):
        body = _safe_body(full[:i])
        assert not any(w in body for w in ("correct", "horse", "battery", "staple")), body


def test_cancel_mid_second_block_keeps_what_was_on_card():
    """停在第二段中途：result.text 只有第一段 ⇒ 卡上已显示的第二段不能被抹掉。"""
    first = "第一段：我先看一下目录结构，里面有前端、后端和脚本三个部分。"
    shown = first + "\n\n第二段：后端目录里有一个 main.py，它负责启动服务，我接着看它的路由，"

    class Runner:
        def __init__(self, b):
            self.b = b

        async def run_turn(self, text, workspace, *, on_text=None, **_):
            on_text(shown)
            for _ in range(60):
                await asyncio.sleep(0.05)
                if self.b.client.pushes:
                    break
            return TurnResult(ok=True, text=first, cancelled=True)

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, None, mode="trusted")
        b.runner = Runner(b)
        _run(b)
    assert "第二段" in b.client.pushes[-1] or any("第二段" in s for s in b.replier.sent), \
        f"停下后第二段哪里都没了：{b.client.pushes[-1]!r} / {b.replier.sent}"
    assert b.client.closed()


def test_card_broken_mid_stream_still_gets_closed():
    """中间某帧超时 ⇒ 卡坏了 ⇒ 至少要关流，不能挂着「正在回答…」；正文照旧用消息发。"""
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, lambda b: TurnResult(ok=True, text=SAID + "后面还有很多。"), mode="trusted")
        orig = b.client.cardkit.v1.card_element.content
        calls = {"n": 0}

        class CE:
            def content(self, request):
                calls["n"] += 1
                if calls["n"] >= 1:
                    raise TimeoutError("飞书超时")
                return orig(request)

        b.client.cardkit.v1.card_element = CE()

        class Runner:
            async def run_turn(self, text, workspace, *, on_text=None, **_):
                on_text(SAID)
                await asyncio.sleep(1.0)
                return TurnResult(ok=True, text=SAID + "后面还有很多。")

        b.runner = Runner()
        _run(b)
    assert calls["n"] >= 1, "前提：推送确实失败过"
    assert b.client.closed(), "推到一半坏掉的卡没关流"
    assert any("后面还有很多" in s for s in b.replier.sent), "卡坏了，正文也没用消息补上"


def test_streamer_construction_failure_does_not_crash_long_turn(monkeypatch):
    """流式对象造不出来 + 这一轮超过首次状态卡时间 ⇒ 不能因为壳少方法把整轮答案丢掉。"""
    import bridge.app as app_mod
    import bridge.stream as stream_mod

    monkeypatch.setattr(app_mod, "_STATUS_REFRESH_SEC", 0.2)

    def boom(*a, **k):
        raise RuntimeError("SDK 版本不对")

    monkeypatch.setattr(stream_mod, "StreamReply", boom)

    class Runner:
        async def run_turn(self, text, workspace, *, on_text=None, **_):
            on_text(SAID)
            await asyncio.sleep(1.5)
            return TurnResult(ok=True, text=SAID)

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, None, mode="trusted")
        b.settings.progress_first_notice_sec = 0.3
        b.runner = Runner()
        _run(b)
    assert any(SAID in s for s in b.replier.sent), "整轮答案丢了"


def test_long_moving_turn_still_shows_status_card_after_escalate(monkeypatch):
    """一直边说边干的长轮次：到了 escalate 线状态卡（带停下按钮）必须出来。"""
    import bridge.app as app_mod

    monkeypatch.setattr(app_mod, "_STATUS_REFRESH_SEC", 1.2)
    statuses: list = []

    class Runner:
        async def run_turn(self, text, workspace, *, on_text=None, **_):
            acc = ""
            for i in range(80):            # 4 秒，一直在冒字
                acc += f"第{i}句话写在这里，字数要够二十四个才会推上去一帧。"
                on_text(acc)
                await asyncio.sleep(0.05)
            return TurnResult(ok=True, text=acc)

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, None, mode="trusted")
        b.settings.progress_first_notice_sec = 0.5
        b.settings.progress_escalate_sec = 2
        b.replier.send_status_card = lambda c, t, with_stop=True: statuses.append(t) or "om_s"
        b.replier.patch_status_card = lambda m, t, with_stop=True: statuses.append(t) or True
        b.runner = Runner()
        _run(b)
    assert [s for s in statuses if "还在跑" in s], "过了 escalate 线还没有状态卡 ⇒ 没有停下按钮"


def test_deferred_overflow_without_pending_still_sends_full_text():
    """卡装不下 + 等确认 + 没拿到待办 ⇒ 卡上说「完整内容在下面」，下面必须真有。"""
    long_text = ("远程看电脑要先想清楚需求，再选工具。" * 30 + "\n\n") * 40

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _bridge(tmp, lambda b: TurnResult(ok=True, text=long_text, deferred=True,
                                              session_id="s1"))
        _run(b)
    assert any(long_text.strip()[:200] in s for s in b.replier.sent), "全文没发出来"
