"""第 20 轮：确认卡挂着时又来一句新话 ⇒ 那张卡作废、明说；点旧卡不再回「好，继续」。

S2 日志挖掘 Top 1（8 次同意里 3 次中招，11-07 15:13 还在发生）：
defer → 约 2 秒后同一会话又进来一句（连点两个菜单快捷问题）→ 新这句 resume 同一个
claude 会话 ⇒ 挂起的 tool_use 变成 `Tool result missing`，模型绕过去 ⇒ 之后点旧卡
「同意」回「好，继续」，**那一步再也不会跑**，`.ok` 躺在盘上。

这里真跑 `_ask_approval`（出卡）→ `_run_locally`（新消息）→ `on_card_action`（点旧卡），
看盘上的文件、聊天里说了什么、toast 说了什么。不比源码字符串。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import SCOPE, _click, _fake_bridge, _put_pending, _suffixes  # noqa: E402

DIGEST = "ab12cd34ef56ab12cd34ef56"


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.sent, b.cards, b.ran = [], [], []
    b._say = lambda chat_id, text: b.sent.append(text) or True
    b._say_with_buttons = lambda chat_id, text, buttons: b.sent.append(text) or True
    b.replier.send_approval_card = lambda *a, **kw: b.cards.append(a) or True
    b._chat_for = lambda scope: "chat1"
    b.workspaces = types.SimpleNamespace(get=lambda scope: "/tmp")

    # 走**真的** `_run_one`（作废在它里面、审查放行之后），只把 claude 换掉
    from bridge.review import Reviewer
    from bridge.runner import TurnResult

    b.reviewer = Reviewer(custom_rules=())
    b.settings.progress_first_notice_sec = 90
    b.settings.progress_escalate_sec = 420
    b.settings.streaming = False

    class Runner:
        async def run_turn(self, text, workspace, **_):
            b.ran.append(text)
            return TurnResult(ok=True, text="好的", session_id="s1")

    b.runner = Runner()
    return b


def _defer(b):
    """照真链路出一张确认卡：钩子落 .pending → 一轮 defer → _ask_approval 发卡。"""
    _put_pending(b, SCOPE, DIGEST, "echo hi > out.txt")
    result = types.SimpleNamespace(deferred=True, text="", session_id="s1", ok=True,
                                   denials=[], error="")
    b._ask_approval(SCOPE, "chat1", b.sessions.get(SCOPE), result)
    assert b.cards, "前提没成立：卡没发出去"
    # 用**真发出去那张卡**上的令牌（send_approval_card 的第 4 个参数 = 同意按钮的 value）
    approve_value = b.cards[-1][3]
    return types.SimpleNamespace(to_value=lambda: {k: v for k, v in approve_value.items() if k != "act"})


def _toast(resp) -> str:
    body = getattr(resp, "toast", None) or {}
    if not body and isinstance(getattr(resp, "__dict__", None), dict):
        body = resp.__dict__.get("toast") or {}
    content = body.get("content") if isinstance(body, dict) else getattr(body, "content", "")
    return content or ""


def test_new_message_voids_the_pending_card_and_says_so():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token = _defer(b)
        assert any(n.endswith(".asked") for n in _suffixes(b, SCOPE))

        asyncio.run(b._run_locally(SCOPE, "chat1", "再帮我看看另一个目录"))
        assert b.ran == ["再帮我看看另一个目录"], "新消息没跑"
        # 第 32 轮：这句话 = 回答那张卡（没批）。这里的假 replier 拿不到 message_id、换不了卡
        # ⇒ 退回一句话交代；不再说「等这条回完再说一次」
        assert any("没批" in t for t in b.sent), f"没告诉他那一步没批：{b.sent}"
        assert not any("等这条回完再说一次" in t for t in b.sent), b.sent
        assert _suffixes(b, SCOPE) == [], "待确认记录没作废"
        assert SCOPE not in b._awaiting

        resp = _click(b, token)
        toast = _toast(resp)
        assert "继续" not in toast and "没做" in toast, f"点旧卡的回应：{toast!r}"
        assert not any(n.endswith(".ok") for n in _suffixes(b, SCOPE)), "作废的卡点了还写下放行证"
        assert b.ran == ["再帮我看看另一个目录"], "点作废的卡还续跑了"


def test_normal_chain_still_runs_the_approved_step():
    """阳性对照：没有新消息插进来 ⇒ 点同意照旧写 .ok、续跑。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token = _defer(b)
        scheduled = []
        import bridge.app as app_mod

        original = app_mod.asyncio.run_coroutine_threadsafe
        app_mod.asyncio.run_coroutine_threadsafe = lambda coro, loop: (scheduled.append(coro),
                                                                        coro.close())
        try:
            resp = _click(b, token)
        finally:
            app_mod.asyncio.run_coroutine_threadsafe = original
        assert "继续" in _toast(resp)
        assert any(n.endswith(".ok") for n in _suffixes(b, SCOPE)), "正常同意没写放行证"
        assert scheduled, "正常同意没续跑"


def test_message_without_pending_card_says_nothing_extra():
    """没有挂着的卡 ⇒ 新消息照常跑，不多说一句。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        asyncio.run(b._run_locally(SCOPE, "chat1", "你好"))
        assert b.ran == ["你好"] and not any("作废" in t for t in b.sent), b.sent


# ====================================================================
# 第 24 轮 R-approval 复现的几条
# ====================================================================

def _capture_continue(b):
    import bridge.app as app_mod

    got = []
    original = app_mod.asyncio.run_coroutine_threadsafe

    def fake(coro, loop):
        got.append(coro)
        coro.close()

    app_mod.asyncio.run_coroutine_threadsafe = fake
    b._loop = object()
    return got, lambda: setattr(app_mod.asyncio, "run_coroutine_threadsafe", original)


def _defer_with_session(b, hook_session="sess-from-hook"):
    directory = b.gate.dir_for(SCOPE)
    import json as _j

    with open(os.path.join(directory, DIGEST + ".pending"), "w") as fh:
        _j.dump({"tool_name": "Bash", "tool_input": {"command": "echo hi > out.txt"},
                 "session_id": hook_session}, fh)
    result = types.SimpleNamespace(deferred=True, text="", session_id=hook_session, ok=True,
                                   denials=[], error="")
    b._ask_approval(SCOPE, "chat1", b.sessions.get(SCOPE), result)
    v = b.cards[-1][3]
    return types.SimpleNamespace(to_value=lambda: {k: x for k, x in v.items() if k != "act"})


def test_approve_after_restart_resumes_the_hooks_session():
    """🔴 重启（内存里会话 id、_awaiting 全没了）后点同意 ⇒ 要 resume 钩子记下的那个会话，不能开新会话。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token = _defer_with_session(b)
        fresh = _bridge(tmp)                    # 重启
        fresh.approvals = b.approvals           # 同一把签名密钥（真机是落盘的）
        got, restore = _capture_continue(fresh)
        try:
            resp = _click(fresh, token)
        finally:
            restore()
        assert got, f"没续跑：{_toast(resp)}"
        assert fresh.sessions.get(SCOPE).claude_session_id == "sess-from-hook", "续跑没接上钩子那个会话"
        assert fresh.sessions.get(SCOPE).resume_target(fresh.sessions.idle_reset_sec) == "sess-from-hook"


def test_approve_after_long_idle_still_resumes():
    """确认卡挂了 31 分钟才点 ⇒ 空闲重置规则不能让续跑开新会话。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token = _defer_with_session(b)
        b.sessions.get(SCOPE).last_active -= 31 * 60
        got, restore = _capture_continue(b)
        try:
            _click(b, token)
        finally:
            restore()
        assert b.sessions.get(SCOPE).resume_target(b.sessions.idle_reset_sec) == "sess-from-hook"


def test_approve_with_no_resumable_session_does_not_say_continue():
    """哪儿都拿不到会话 id ⇒ 不说「好，继续」、不开新会话。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token = _defer(b)                       # 待办里没有 session_id
        fresh = _bridge(tmp)
        fresh.approvals = b.approvals
        got, restore = _capture_continue(fresh)
        try:
            resp = _click(fresh, token)
        finally:
            restore()
        assert not got, "接不上还续跑了（会开一个只收到「继续」的新会话）"
        assert "接不上" in _toast(resp), _toast(resp)


def _void_then_reask(b):
    """A 卡挂着 → 新消息作废 A → 新一轮又问到同一条命令（同指纹）出 B 卡。"""
    token_a = _defer(b)
    asyncio.run(b._run_locally(SCOPE, "chat1", "先看看别的"))
    token_b = _defer(b)
    return token_a, token_b


def test_clicking_voided_card_approve_does_not_approve_the_new_card():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token_a, _token_b = _void_then_reask(b)
        got, restore = _capture_continue(b)
        try:
            resp = _click(b, token_a)
        finally:
            restore()
        assert "作废" in _toast(resp), _toast(resp)
        assert not any(n.endswith(".ok") for n in _suffixes(b, SCOPE)), "旧卡批动了新卡"
        assert any(n.endswith(".asked") for n in _suffixes(b, SCOPE)), "新卡被动了"
        assert not got


def test_clicking_voided_card_deny_does_not_kill_the_new_card():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token_a, token_b = _void_then_reask(b)
        resp = _click(b, token_a, act="deny")
        assert "作废" in _toast(resp)
        assert any(n.endswith(".asked") for n in _suffixes(b, SCOPE)), "点旧卡「不行」把新卡清掉了"
        got, restore = _capture_continue(b)
        try:
            resp_b = _click(b, token_b)
        finally:
            restore()
        assert "继续" in _toast(resp_b) and got, _toast(resp_b)


def test_new_card_with_same_digest_is_not_called_voided_later():
    """A 作废后同一条命令出了 B，他 /new 之后点 B ⇒ 不能说「你后来发了新消息」。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _token_a, token_b = _void_then_reask(b)
        b.gate.clear(SCOPE)                      # 等价于 /new
        resp = _click(b, token_b)
        assert "作废（你后来发了新消息）" not in _toast(resp), _toast(resp)


def test_message_denied_by_review_does_not_void_the_card():
    """被审查层直接拒掉的话根本没 resume 会话 ⇒ 卡不能作废。"""
    from bridge.review import Verdict

    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        _defer(b)
        b.reviewer = types.SimpleNamespace(review_request=lambda ctx: types.SimpleNamespace(
            verdict=Verdict.DENY, reason="不做这件事"))
        asyncio.run(b._run_locally(SCOPE, "chat1", "git push 到 main"))
        assert any(n.endswith(".asked") for n in _suffixes(b, SCOPE)), "被拒的话把卡作废了"
        assert not any("作废" in t for t in b.sent), b.sent


def test_already_approved_queued_continuation_is_not_voided():
    """刚点了同意（盘上只剩 .ok、续跑在排队）时一条消息先拿到锁 ⇒ 不作废、不说「没做」。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        token = _defer(b)
        got, restore = _capture_continue(b)
        try:
            _click(b, token)
        finally:
            restore()
        assert any(n.endswith(".ok") for n in _suffixes(b, SCOPE))
        asyncio.run(b._run_locally(SCOPE, "chat1", "顺便问一句"))
        assert any(n.endswith(".ok") for n in _suffixes(b, SCOPE)), "刚批的 .ok 被清掉了"
        assert not any("作废" in t for t in b.sent), b.sent
