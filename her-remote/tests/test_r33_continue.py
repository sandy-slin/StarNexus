"""第 33 轮：闲超过 `session_idle_reset_min` 开了新会话 ⇒ 回复下面挂「↩︎ 接着上次（…）」，点了换回上一个会话。

真跑 `_run_locally` / `_run_one` / `on_card_action`，只把 claude 换掉；看发了什么按钮、下一轮 resume 的是哪个 id。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import OWNER, _fake_bridge, _put_pending  # noqa: E402

from bridge.fleet.actions import continue_label, resolve_continue_click  # noqa: E402
from bridge.runner import TurnResult  # noqa: E402

CHAT = "oc_1"
SCOPE = f"dm:{OWNER}"   # 点击时 `_click_scope` 按点的人认单聊


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.settings.config_path = os.path.join(tmp, "config.toml")
    b.settings.progress_first_notice_sec = 90
    b.settings.progress_escalate_sec = 420
    b.settings.streaming = False
    b.sent, b.ran, b.buttons = [], [], []
    b._say = lambda chat_id, text: b.sent.append(text) or True
    b._say_later = lambda chat_id, text: b.sent.append(text)
    b._say_with_buttons = lambda chat_id, text, buttons: b.sent.append(text) or True
    b._send_buttons = lambda chat_id, buttons: b.buttons.append(list(buttons)) or True
    b._chat_ids = {SCOPE: CHAT}
    b.workspaces = types.SimpleNamespace(get=lambda scope: "/tmp")
    from bridge.review import Reviewer

    b.reviewer = Reviewer(custom_rules=())
    counter = {"n": 0}

    class Runner:
        async def run_turn(self, text, workspace, *, resume=None, on_session=None, **_):
            b.ran.append((text, resume))
            if resume:
                return TurnResult(ok=True, text="好的", session_id=resume)
            counter["n"] += 1
            return TurnResult(ok=True, text="好的", session_id=f"s{counter['n']}")

    b.runner = Runner()
    return b


def _say_now(b, text):
    asyncio.run(b._run_locally(SCOPE, CHAT, text))


def _go_idle(b):
    b.sessions.get(SCOPE).last_active = time.monotonic() - b.sessions.idle_reset_sec - 5


def _continue_buttons(b):
    found = []
    for row in b.buttons:
        for button in row:
            value = button["behaviors"][0].get("value") or {}
            if value.get("act") == "cont":
                found.append(button)
    return found


def _click(b, value, clicker=OWNER):
    ev = types.SimpleNamespace(event=types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=clicker),
        context=types.SimpleNamespace(open_chat_id=CHAT),
        action=types.SimpleNamespace(value=value, form_value=None, input_value=None, option=None)))
    resp = b.on_card_action(ev)
    return resp.toast.type, resp.toast.content


def _idle_then_new_topic(b):
    _say_now(b, "帮我看看 deploy.sh 为什么挂了")
    _go_idle(b)
    b.buttons.clear()
    _say_now(b, "顺便问下今天天气")


# ---------- 按钮出不出 ----------


def test_idle_reset_turn_gets_button_with_previous_words():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _idle_then_new_topic(b)
        assert b.ran[-1] == ("顺便问下今天天气", None), "超时那一轮应该开新会话"
        buttons = _continue_buttons(b)
        assert len(buttons) == 1, f"超时那一轮没挂「接着上次」：{b.buttons}"
        label = buttons[0]["text"]["content"]
        assert label == "↩︎ 接着上次（帮我看看 deploy.…）", label
        assert set(buttons[0]["behaviors"][0]["value"]) == {"act", "n"}, "按钮值里不许带会话 id"


def test_normal_continuation_has_no_button():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _say_now(b, "第一句")
        _say_now(b, "第二句")
        assert b.ran[-1] == ("第二句", "s1")
        assert _continue_buttons(b) == []


def test_first_ever_turn_has_no_button():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _say_now(b, "第一句")
        assert _continue_buttons(b) == []


def test_slash_new_has_no_button():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _say_now(b, "第一句")
        asyncio.run(b._handle_command(SCOPE, CHAT, "/new"))
        _say_now(b, "新话题")
        assert b.ran[-1] == ("新话题", None)
        assert _continue_buttons(b) == [], "/new 主动开的不该出「接着上次」"
        # /new 之后再闲过头也不出：上一个会话已经被他主动丢掉了 ⇒ 只能回到 /new 之后那个
        _go_idle(b)
        b.buttons.clear()
        _say_now(b, "又过了很久")
        buttons = _continue_buttons(b)
        assert len(buttons) == 1 and "新话题" in buttons[0]["text"]["content"]


def test_after_restart_no_previous_id_no_button():
    """重启 = 内存清空：会话 turns=0、没有 claude_session_id ⇒ 不出。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _idle_then_new_topic(b)
        fresh = _bridge(tmp)            # 新进程
        _go_idle(fresh)
        _say_now(fresh, "重启后第一句")
        assert _continue_buttons(fresh) == []
        assert _click(fresh, _continue_buttons(b)[0]["behaviors"][0]["value"])[0] == "error", \
            "重启前的按钮在重启后还能点"


def test_failed_turn_has_no_button():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _say_now(b, "第一句")
        _go_idle(b)

        class Bad:
            async def run_turn(self, *a, **kw):
                return TurnResult(ok=False, text="", error="坏了")

        b.runner = Bad()
        b.buttons.clear()
        _say_now(b, "第二句")
        assert _continue_buttons(b) == []


# ---------- 点了以后 ----------


def test_click_swaps_back_and_next_turn_resumes_old_id():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _idle_then_new_topic(b)
        assert b.sessions.get(SCOPE).claude_session_id == "s2"
        value = _continue_buttons(b)[0]["behaviors"][0]["value"]
        ran_before = len(b.ran)
        kind, _ = _click(b, value)
        assert kind == "info"
        assert len(b.ran) == ran_before, "点的时候不许跑任何东西"
        assert "好，接着上次那个话题。" in b.sent
        _say_now(b, "那继续查 deploy.sh")
        assert b.ran[-1] == ("那继续查 deploy.sh", "s1"), f"下一轮 resume 的不是旧 id：{b.ran}"
        # 点过一次就作废
        assert _click(b, value)[0] == "error"


def test_click_rejected_when_forged_or_not_owner():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _idle_then_new_topic(b)
        real = _continue_buttons(b)[0]["behaviors"][0]["value"]
        bad_values = [
            {"act": "cont", "n": "deadbeef0000"},                 # 序号对不上
            {"act": "cont", "n": real["n"], "id": "s-evil"},      # 夹带会话 id
            {"act": "cont", "n": 123},
            {"act": "cont"},
        ]
        for value in bad_values:
            assert _click(b, value)[0] == "error", value
        assert _click(b, real, clicker="ou_stranger")[0] == "error", "非主人也能点"
        assert b.sessions.get(SCOPE).claude_session_id == "s2", "伪造点击把会话换掉了"
        # 真的那个还能用
        assert _click(b, real)[0] == "info"
        assert b.sessions.get(SCOPE).claude_session_id == "s1"


def test_click_after_new_is_void():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _idle_then_new_topic(b)
        value = _continue_buttons(b)[0]["behaviors"][0]["value"]
        asyncio.run(b._handle_command(SCOPE, CHAT, "/new"))
        assert _click(b, value)[0] == "info"
        assert b.sessions.get(SCOPE).claude_session_id is None, "/new 之后点旧按钮把会话换回去了"


def test_click_refused_while_approval_waiting():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _idle_then_new_topic(b)
        value = _continue_buttons(b)[0]["behaviors"][0]["value"]
        _put_pending(b, SCOPE, "ab12cd34ef56ab12cd34ef56", "echo hi > out.txt")
        assert _click(b, value)[0] == "info"
        assert b.sessions.get(SCOPE).claude_session_id == "s2", "确认卡在等时换了会话 ⇒ 批准续跑接不上"


def test_label_and_resolver_shapes():
    assert continue_label("") == "↩︎ 接着上次"
    assert continue_label("短句") == "↩︎ 接着上次（短句）"
    assert continue_label("一二三四五六七八九十甲乙丙") == "↩︎ 接着上次（一二三四五六七八九十甲乙…）"
    assert resolve_continue_click({"act": "cont", "n": "ab"}) == "ab"
    assert resolve_continue_click({"act": "cont", "n": "x" * 33}) == ""
    assert resolve_continue_click({"act": "do", "n": "ab"}) == ""
