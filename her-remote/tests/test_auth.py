"""鉴权与幂等测试 —— 这是「别人不能在我电脑上干活」的那道锁。

全是 DENY 用例，所以每条都配阳性对照：要是桩不完整，"被拒"可能只是因为
代码根本没跑到判断那行。每个 deny 测试后面都紧跟一条"同样形状但应该放行"。
"""

from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.policy import Policy  # noqa: E402

OWNER = "ou_owner"
STRANGER = "ou_stranger"
WS = "/tmp/bridge-test-ws"


def make_event(
    open_id: str = OWNER,
    sender_type: str = "user",
    text: str = "hello",
    message_id: str = "om_1",
    chat_type: str = "p2p",
):
    """照飞书 im.message.receive_v1 的真实结构造事件。"""
    return types.SimpleNamespace(
        event=types.SimpleNamespace(
            sender=types.SimpleNamespace(
                sender_type=sender_type,
                sender_id=types.SimpleNamespace(
                    open_id=open_id, union_id="on_x", user_id="u_x"
                ),
            ),
            message=types.SimpleNamespace(
                message_id=message_id,
                chat_id="oc_1",
                chat_type=chat_type,
                message_type="text",
                content=json.dumps({"text": text}, ensure_ascii=False),
            ),
        )
    )


class _ClosedPairing:
    """配对窗口关着的门：没码、没主人 ⇒ 一律拒绝且静默。

    用它而不是 MagicMock，是为了让「拒绝」来自真实的判断分支，
    而不是来自桩的默认返回值（那样 DENY 用例会假通过）。
    """

    def consider(self, open_id, text, message_id=""):
        from bridge.pairing import Outcome, PairingResult

        return PairingResult(
            outcome=Outcome.NO_WINDOW, owner_open_id="", reply="", attempts_used=0
        )


def make_bridge(owners=(OWNER,)):
    """造一个不碰网络的 Bridge。"""
    from bridge.app import Bridge, Settings

    policy = Policy(
        mode="readonly",
        workspaces=(WS,),
        default_workspace=WS,
        model="claude-haiku-5-5",
        turn_timeout_sec=60,
    )
    settings = Settings(
        app_id="cli_test",
        app_secret="secret",
        owner_open_ids=frozenset(owners),
        policy=policy,
        max_reply_chars=5000,
        session_idle_reset_min=30,
        progress_first_notice_sec=90,
        progress_escalate_sec=420,
        progress_max_notices=2,
    )
    bridge = Bridge.__new__(Bridge)
    bridge.settings = settings
    bridge._seen_messages = {}
    bridge._last_event_at = 0.0
    # 没有主人时走配对码那条路。这里给一个「窗口关着」的门：
    # 既没有码也没有主人 ⇒ 任何消息都不该被执行（fail-closed）。
    bridge.pairing = _ClosedPairing()
    bridge._say = lambda chat_id, text: None

    dispatched: list[tuple] = []
    bridge._loop = object()  # 非 None，让 on_message 走到派发那步

    # 换掉派发，只记录「本来会不会干活」
    import bridge.app as app_module

    original = app_module.asyncio.run_coroutine_threadsafe

    def fake_dispatch(coro, loop):
        dispatched.append(("dispatch", coro))
        coro.close()  # 不真跑，避免 warning
        return None

    app_module.asyncio.run_coroutine_threadsafe = fake_dispatch
    bridge._restore = lambda: setattr(
        app_module.asyncio, "run_coroutine_threadsafe", original
    )
    return bridge, dispatched


# ---------- 鉴权 ----------


def test_owner_message_is_dispatched():
    """阳性对照放第一条：主人的消息必须真的被派发。

    这条过不了，下面所有 deny 测试都没有意义。
    """
    bridge, dispatched = make_bridge()
    try:
        bridge.on_message(make_event(open_id=OWNER))
        assert len(dispatched) == 1
    finally:
        bridge._restore()


def test_stranger_is_silently_dropped():
    bridge, dispatched = make_bridge()
    try:
        bridge.on_message(make_event(open_id=STRANGER))
        assert dispatched == []
    finally:
        bridge._restore()


def test_bot_sender_is_dropped_even_if_open_id_matches():
    """别的机器人发言也会进事件流。open_id 撞上也不能放行。"""
    bridge, dispatched = make_bridge()
    try:
        bridge.on_message(make_event(open_id=OWNER, sender_type="bot"))
        assert dispatched == []
    finally:
        bridge._restore()


def test_empty_allowlist_refuses_everyone():
    """fail-closed：白名单没配时谁都不行，包括看起来像主人的。"""
    bridge, dispatched = make_bridge(owners=())
    try:
        bridge.on_message(make_event(open_id=OWNER))
        assert dispatched == []
    finally:
        bridge._restore()


def test_group_message_from_owner_is_dispatched():
    """阳性对照：群消息路径也要通，否则「群里不工作」会被误读成鉴权正常。

    第 39 轮起群里不 @ 只在「只有主人 + 本机器人」的群里回 ⇒ 这里给一个那样的群
    （多人群 / 要 @ 的规则在 test_r39_group_reply.py）。
    """
    bridge, dispatched = make_bridge()
    bridge._group_is_private = lambda chat_id: True
    try:
        bridge.on_message(make_event(open_id=OWNER, chat_type="group"))
        assert len(dispatched) == 1
    finally:
        bridge._restore()


# ---------- 幂等 ----------


def test_duplicate_message_id_runs_once():
    """平台是至少一次投递，成功也可能重推。同一条不能干两遍活。"""
    bridge, dispatched = make_bridge()
    try:
        bridge.on_message(make_event(message_id="om_dup"))
        bridge.on_message(make_event(message_id="om_dup"))
        assert len(dispatched) == 1
    finally:
        bridge._restore()


def test_different_message_ids_both_run():
    """阳性对照：去重不能把正常的后续消息也吃掉。"""
    bridge, dispatched = make_bridge()
    try:
        bridge.on_message(make_event(message_id="om_a"))
        bridge.on_message(make_event(message_id="om_b"))
        assert len(dispatched) == 2
    finally:
        bridge._restore()


# ---------- 文本提取 ----------


def test_strips_at_mention_placeholder():
    from bridge.app import Bridge

    message = types.SimpleNamespace(
        content=json.dumps({"text": "@_user_1 看一下 git status"}, ensure_ascii=False)
    )
    assert Bridge._extract_text(message) == "看一下 git status"


def test_empty_text_is_dropped():
    bridge, dispatched = make_bridge()
    try:
        bridge.on_message(make_event(text="   "))
        assert dispatched == []
    finally:
        bridge._restore()


def test_malformed_content_does_not_crash():
    from bridge.app import Bridge

    message = types.SimpleNamespace(content="not json at all")
    assert Bridge._extract_text(message) == ""


# ---------- 会话隔离 ----------


def test_dm_and_group_are_separate_scopes():
    from bridge.sessions import SessionStore

    dm = SessionStore.scope_for("oc_1", "p2p", OWNER)
    group = SessionStore.scope_for("oc_1", "group", OWNER)
    assert dm != group


def test_same_dm_is_one_scope():
    from bridge.sessions import SessionStore

    first = SessionStore.scope_for("oc_1", "p2p", OWNER)
    second = SessionStore.scope_for("oc_2", "p2p", OWNER)
    # 单聊按人算，换个 chat_id 还是同一个人的会话
    assert first == second


def test_idle_session_does_not_resume():
    import time

    from bridge.sessions import Session

    session = Session(scope="dm:x", workspace=WS, claude_session_id="sid-1")
    session.last_active = time.monotonic() - 3600
    assert session.resume_target(idle_reset_sec=1800) is None


def test_fresh_session_does_resume():
    """阳性对照：没空闲时必须续上，否则上面那条可能只是永远返回 None。"""
    from bridge.sessions import Session

    session = Session(scope="dm:x", workspace=WS, claude_session_id="sid-1")
    assert session.resume_target(idle_reset_sec=1800) == "sid-1"
