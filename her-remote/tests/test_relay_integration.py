"""进会话聊天接进飞书（spec D）：对话模式、那一次权限确认、指示器、/cancel、诚实的 /stop。

全部用假 adapter / 假 replier / tmp_path。**不起真 CLI、不碰用户现有会话、不发任何信号。**

这条测试盯的是 D 段那几件**出过事**的事：
- [接着聊] 现在对「终端里开着的 Claude 会话」要出现（去掉「会分叉」那句拒绝）
- 进对话模式前**必须**问一次权限；没确认就记目标 = 他下一句话悄悄进了真实会话
- 确认过 24 小时内不再问；换了会话编号要重新问
- 对话模式里每条消息都带「正在和谁说话 + /cancel」
- 回复过 `reply.py` 的脱敏和分段
- `/stop` 对终端会话**不假装能停**，也不给目标发信号
- `/cancel` 是可靠的退出口（不然用户永远留在对话模式里）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import ActionResult, Message, Session  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_CARD_BYTES,
    TALKING_PREFIX,
    build_confirm_relay_card,
    build_detail_card,
    resolve_fleet_click,
    resolve_relay_click,
)
from bridge.fleet.consent import RelayConsent  # noqa: E402
from bridge.fleet.control import FleetControl  # noqa: E402
from bridge.fleet.scan import Fleet  # noqa: E402
from bridge.policy import Policy  # noqa: E402

OWNER = "ou_owner"
CHAT = "oc_chat"
SCOPE = f"dm:{OWNER}"
CID = "44444444-4444-4444-8444-444444444444"
CID2 = "55555555-5555-4555-8555-555555555555"


def _policy(tmp_path, mode="readonly"):
    return Policy(mode=mode, workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                  model="claude-sonnet-5-5", turn_timeout_sec=60, ceiling_mode="trusted")


# ---------- 假 adapter：终端里开着的会话，只能走 relay ----------


class RelayClaude:
    """终端里开着的 Claude 会话：`send()` 会分叉所以不能用，`relay()` 能送进去。"""

    tool = "claude"

    def __init__(self, sessions=(), reply="好，我看看", relay_ok=True, why=""):
        self.sessions = list(sessions)
        self.messages = [Message("user", "之前说的"), Message("assistant", "之前答的")]
        self.last_error = ""
        self.relayed: list = []
        self.sent: list = []
        self.stopped: list = []
        self.relay_stopped: list = []
        self.signals: list = []  # 🔴 一直该是空的：relay 从不给目标发信号
        self.reply = reply
        self.relay_ok = relay_ok
        self.why = why

    # --- 只读 ---
    def list(self, limit=20):
        return self.sessions[:limit]

    def running(self):
        return [s for s in self.sessions if s.pids]

    def find(self, sid):
        return next((s for s in self.sessions if s.id == sid), None)

    def read(self, sid, n=4):
        return self.messages[-n:]

    # --- 两条路 ---
    def can_relay(self, sid):
        return (True, "") if self.relay_ok else (False, self.why)

    def relay(self, sid, text, timeout=120.0, mode=None):
        self.relayed.append((sid, text))
        if not self.relay_ok:
            return ActionResult(False, self.why, sid)
        return ActionResult(True, self.reply, sid, delivered=True)

    def send(self, sid, text, timeout=600, **kw):
        self.sent.append((sid, text))
        return ActionResult(True, "resume 回复", sid)

    def stop(self, sid):
        self.stopped.append(sid)
        return ActionResult(False, "终端里的会话只能在终端里停", sid)

    def relay_stop(self, sid):
        self.relay_stopped.append(sid)
        from bridge.fleet.relay import WHY_STOP_TARGET

        return ActionResult(False, WHY_STOP_TARGET, sid)


def _live(sid=CID, **kw):
    """终端里开着的会话：can_send=False（会分叉）+ can_relay=True。"""
    base = dict(tool="claude", surface="cli", id=sid, title="修登录", cwd="/Users/x/codes/demo",
                status="running", pids=(4242,), owner="terminal", updated_at=time.time() - 60,
                can_send=False, why_not_send="它开在电脑的终端里，从手机发进去会分叉成两段对话",
                can_relay=True)
    base.update(kw)
    return Session(**base)


class FakeReplier:
    def __init__(self):
        self.texts: list = []
        self.cards: list = []

    def send(self, chat_id, text):
        from bridge.redact import redact_with_notice

        self.texts.append(redact_with_notice(text))
        return True

    def send_card(self, chat_id, card):
        self.cards.append(card)
        return True


def _bridge(tmp_path, adapter, mode="readonly", consent_path=""):
    from bridge.app import Bridge
    from bridge.approval import ApprovalGate
    from bridge.review import Reviewer

    b = Bridge.__new__(Bridge)
    pol = _policy(tmp_path, mode)
    b.settings = types.SimpleNamespace(owner_open_ids=frozenset({OWNER}), policy=pol, custom_rules=())
    b.replier = FakeReplier()
    b._chat_ids = {SCOPE: CHAT}
    b._paused = {}
    b._awaiting = {}
    b._cancels = {}
    b._loop = None
    b._pending_consent = {}
    b.reviewer = Reviewer()
    b.gate = ApprovalGate(str(tmp_path / "approvals"))
    b.fleet = FleetControl(Fleet({adapter.tool: adapter}, timeout=1.0))
    b._fleet_inflight = {}
    b.relay_consent = RelayConsent(str(consent_path or tmp_path / "relay_consent.json"))
    b.handled = []

    async def fake_run_one(session, chat_id, text):  # 桥自己的 claude 不该被叫到
        b.handled.append(text)

    b._run_one = fake_run_one
    b.sessions = types.SimpleNamespace(
        get=lambda scope: types.SimpleNamespace(
            scope=scope, workspace=str(tmp_path), lock=asyncio.Lock(), waiting=0),
    )
    b.workspaces = types.SimpleNamespace(get=lambda scope: str(tmp_path))
    return b


def _run(coro):
    return asyncio.run(coro)


def _click(b, value, clicker=OWNER):
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=CHAT))
    if value.get("act") == "relay":
        return b._on_relay_click(event, clicker, value)
    return b._on_fleet_click(event, clicker, value)


def _talk(b, sid=CID, tool="claude"):
    """走完「点接着聊 → 确认权限」，进对话模式。"""
    _click(b, {"act": "fleet", "op": "send", "tool": tool, "id": sid})
    _click(b, {"act": "relay", "op": "talk", "tool": tool, "id": sid})


def _assert_card_ok(blob):
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob  # schema 2.0 不认 action 容器（踩过）
    assert len(blob.encode("utf-8")) < MAX_CARD_BYTES
    return card


def _buttons(card):
    found: list = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return found


# ---------- 详情卡：终端里开着的会话现在能接着聊 ----------


def test_detail_card_offers_talk_for_a_live_terminal_session():
    """🔴 spec D 第一条：去掉「会分叉」那句拒绝，终端里开着的会话要出现 [接着聊]。"""
    card = _assert_card_ok(build_detail_card(_live(), [Message("assistant", "在跑")]))
    ops = [b["behaviors"][0]["value"]["op"] for b in _buttons(card)]
    assert "send" in ops, "终端里开着的会话没有 [接着聊]"
    blob = json.dumps(card, ensure_ascii=False)
    assert "分叉" not in blob, "还在拿「会分叉」劝退用户 —— 那条路已经不是唯一的了"


def test_detail_card_still_refuses_when_neither_path_works():
    """阳性对照：两条路都走不通时照样贴拒绝理由，不画一个点了必被拒的按钮。"""
    dead = _live(can_relay=False, why_not_relay="这个会话没有名字，发不了。")
    card = _assert_card_ok(build_detail_card(dead, []))
    ops = [b["behaviors"][0]["value"]["op"] for b in _buttons(card)]
    assert "send" not in ops
    assert "没有名字" in json.dumps(card, ensure_ascii=False)


def test_detail_card_prefers_the_relay_reason_for_terminal_sessions():
    """终端会话两条路都不通时要贴 relay 那句 —— 「会分叉」是另一条路的理由，会误导。"""
    both = _live(can_relay=False, why_not_relay="它在电脑上被挂起了（按过 Ctrl-Z）。")
    blob = json.dumps(_assert_card_ok(build_detail_card(both, [])), ensure_ascii=False)
    assert "挂起" in blob and "分叉" not in blob


def test_detail_card_shows_the_talking_indicator():
    """对话模式里卡片要**一直**显示在和谁说话 + 怎么退出。"""
    blob = json.dumps(_assert_card_ok(build_detail_card(_live(), [], talking=True)), ensure_ascii=False)
    # 11-05：退出口从「发 /cancel」换成卡上的「退出对话」按钮（bridge/fleet/actions.py）
    assert TALKING_PREFIX in blob and '"op": "quit"' in blob


def test_detail_card_redacts_the_title_in_the_indicator():
    """指示器里的会话名来自用户项目里的文字 ⇒ 要过脱敏（顺序：先脱敏整段再截断）。"""
    secret = "sk-ant-api03-" + "Z" * 40
    blob = build_detail_card(_live(title=f"key 是 {secret}"), [], redact=lambda s: s.replace(secret, "[已隐藏]"),
                             talking=True)
    assert secret not in blob


# ---------- 那一次权限确认 ----------


def test_consent_card_says_what_it_actually_means():
    """🔴 这张卡是「放开」类确认：必须说清权限来自电脑那边、手机的只读管不到、停不了。"""
    card = _assert_card_ok(build_confirm_relay_card(_live()))
    blob = json.dumps(card, ensure_ascii=False)
    assert "权限" in blob and "管不到" in blob
    assert "Esc" in blob, "没说清它那一轮只能在电脑上停"
    assert "24 小时" in blob, "没说确认会记多久"
    ops = [b["behaviors"][0]["value"]["op"] for b in _buttons(card)]
    assert ops == ["talk", "no"], "确认卡要有「确认」和「算了」两个口"


def test_clicking_talk_does_not_set_a_target_before_consent(tmp_path):
    """🔴 没确认过**先弹卡、不记目标**：记了就等于他下一句话直接进了真实会话。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is None, "还没确认权限就记下了目标"
    assert b._pending_consent.get(SCOPE) == ("claude", CID)
    # 这时候说一句话 ⇒ 不该发给那个会话
    _run(b._handle(SCOPE, CHAT, "把登录改掉"))
    assert claude.relayed == [] and claude.sent == []
    assert b.handled == ["把登录改掉"], "没确认权限的话落到了远端会话"


def test_consent_then_talk_mode_delivers(tmp_path):
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    assert b.fleet.peek_pending(SCOPE) is not None
    _run(b._handle(SCOPE, CHAT, "把登录改掉"))
    assert [t for _, t in claude.relayed] == ["把登录改掉"]
    assert claude.sent == [], "走了会分叉的 resume 那条路"
    assert b.handled == []


def test_consent_is_remembered_for_24h_and_not_asked_again(tmp_path):
    """确认过的会话 24 小时内不再问（不然连续多轮每句弹一张卡）。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    def confirm_cards():  # 进对话模式时还会跟一张按钮小卡片（[退出对话]…），只数确认卡
        return [c for c in b.replier.cards if '"act": "relay"' in c]

    cards_before = len(confirm_cards())
    b.fleet.clear_pending(SCOPE)
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert len(confirm_cards()) == cards_before, "确认过了还再弹一次确认卡"
    assert b.fleet.peek_pending(SCOPE) is not None, "确认过的会话该直接进对话模式"


def test_consent_is_per_session_id(tmp_path):
    """换了会话编号就是新会话（在电脑上 /exit 再开）⇒ 要重新确认。"""
    claude = RelayClaude([_live(), _live(CID2, title="另一个")])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    b.fleet.clear_pending(SCOPE)
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID2})
    assert b.fleet.peek_pending(SCOPE) is None, "另一个会话没重新问就进去了"
    assert b._pending_consent.get(SCOPE) == ("claude", CID2)


def test_consent_expires_after_the_ttl(tmp_path):
    clock = [1000.0]
    store = RelayConsent(str(tmp_path / "c.json"), now=lambda: clock[0])
    store.grant(SCOPE, "claude", CID)
    assert store.granted(SCOPE, "claude", CID)
    clock[0] += 24 * 60 * 60 + 1
    assert not store.granted(SCOPE, "claude", CID), "确认过了 24 小时还算有效"


def test_consent_survives_a_restart(tmp_path):
    """看门狗会自杀重启 ⇒ 只放内存等于每次重启都要重新点一次确认。"""
    path = tmp_path / "c.json"
    RelayConsent(str(path)).grant(SCOPE, "claude", CID)
    assert RelayConsent(str(path)).granted(SCOPE, "claude", CID)


def test_unreadable_consent_file_means_ask_again(tmp_path):
    """读失败一律当没确认过 —— 多问一次只是啰嗦，少问一次是没同意就动了手。"""
    path = tmp_path / "c.json"
    path.write_text("{ 半截 JSON", encoding="utf-8")
    assert not RelayConsent(str(path)).granted(SCOPE, "claude", CID)


def test_consent_is_per_scope(tmp_path):
    """群里点过确认不代表单聊里也确认过（群里能看到的人更多）。"""
    store = RelayConsent(str(tmp_path / "c.json"))
    store.grant("group:g1", "claude", CID)
    assert not store.granted(SCOPE, "claude", CID)


def test_declining_consent_leaves_no_target(tmp_path):
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _click(b, {"act": "relay", "op": "no", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is None
    _run(b._handle(SCOPE, CHAT, "随便说一句"))
    assert claude.relayed == [] and b.handled == ["随便说一句"]


def test_consent_store_failure_still_lets_this_one_through(tmp_path):
    """存不下来只是下次再问一次，这一次照样进（不能因为磁盘满就整个用不了）。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()

    class Boom(RelayConsent):
        def grant(self, scope, tool, sid):
            raise OSError("磁盘满了")

    b.relay_consent = Boom(str(tmp_path / "boom.json"))
    _talk(b)
    assert b.fleet.peek_pending(SCOPE) is not None, "确认存不下来就进不去了"


def test_text_consent_channel_only_works_for_the_pending_session(tmp_path):
    """`/talk!` 是确认卡发不出去时的退路，但不能凭空进任何会话。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _run(b._handle(SCOPE, CHAT, "/talk!"))
    assert b.fleet.peek_pending(SCOPE) is None, "没弹过卡也能用 /talk! 进去"
    assert "没有在等确认" in b.replier.texts[-1]
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _run(b._handle(SCOPE, CHAT, "/talk!"))
    assert b.fleet.peek_pending(SCOPE) is not None


# ---------- 回调白名单 ----------


KNOWN = {("claude", CID)}


@pytest.mark.parametrize("value", [
    {"act": "relay", "op": "talk", "tool": "claude", "id": "99999999-9999-4999-8999-999999999999"},
    {"act": "relay", "op": "grant", "tool": "claude", "id": CID},
    {"act": "relay", "op": "talk", "tool": "bash", "id": CID},
    {"act": "relay", "op": "talk", "tool": "claude", "id": CID, "text": "rm -rf ~"},
    {"act": "fleet", "op": "talk", "tool": "claude", "id": CID},
    {"act": "relay", "op": "talk", "tool": "claude"},
    {"act": "relay", "op": "talk", "tool": "claude", "id": ["x"]},
    "not a dict",
    None,
])
def test_forged_consent_clicks_rejected(value):
    """🔴 这张卡点一下的后果是「让电脑上那个会话不问就动手」⇒ 白名单要严。"""
    assert resolve_relay_click(value, KNOWN) is None


def test_real_consent_click_accepted():
    assert resolve_relay_click({"act": "relay", "op": "talk", "tool": "claude", "id": CID}, KNOWN) == {
        "op": "talk", "tool": "claude", "id": CID}


def test_relay_and_fleet_values_do_not_cross():
    """两张卡各用自己的 act ⇒ 伪造一个换不到另一个的权限。"""
    assert resolve_fleet_click({"act": "relay", "op": "talk", "tool": "claude", "id": CID}, KNOWN) is None
    assert resolve_relay_click({"act": "fleet", "op": "send", "tool": "claude", "id": CID}, KNOWN) is None


def test_stranger_cannot_consent(tmp_path):
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID}, clicker="ou_someone_else")
    assert b.fleet.peek_pending(SCOPE) is None, "别人点了确认就进了对话模式"


# ---------- 对话模式里的每一句 ----------


def test_every_reply_carries_the_indicator(tmp_path):
    """spec D：对话模式中消息要**一直**显示「正在和 X 说话，退出发 /cancel」。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    for word in ("第一句", "第二句", "第三句"):
        _run(b._handle(SCOPE, CHAT, word))
        reply = b.replier.texts[-1]
        assert TALKING_PREFIX in reply, f"「{word}」的回复没带指示器"
        assert '"op": "quit"' in b.replier.cards[-1], f"「{word}」的回复下面没挂「退出对话」按钮"
    assert [t for _, t in claude.relayed] == ["第一句", "第二句", "第三句"]


def test_sticky_target_keeps_every_sentence_in_the_session(tmp_path):
    """🔴 目标要保持住：原来发一句就清掉 ⇒ 用户接的「那你修一下」悄悄落到桥自己的 claude。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "看看测试为什么挂"))
    _run(b._handle(SCOPE, CHAT, "那你修一下"))
    assert [t for _, t in claude.relayed] == ["看看测试为什么挂", "那你修一下"]
    assert b.handled == []


def test_reply_goes_through_redaction(tmp_path):
    """回复可能很长、也可能带凭据 ⇒ 过 reply.py 的脱敏。"""
    secret = "sk-ant-api03-" + "Q" * 40
    claude = RelayClaude([_live()], reply=f"我看到 key 是 {secret}")
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "读一下配置"))
    reply = b.replier.texts[-1]
    assert secret not in reply, "远端会话的回复把凭据原样发进飞书了"


def test_long_reply_is_chunked_not_truncated(tmp_path):
    """长回复走 replier.send ⇒ 按上限分段，不静默截断。"""
    from bridge.reply import chunk_text

    claude = RelayClaude([_live()], reply="行\n" * 20000)
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "把全部日志念一遍"))
    assert b.replier.texts, "长回复一条都没发出来"
    assert len(chunk_text(b.replier.texts[-1], 500)) > 1


def test_delivered_but_no_reply_never_says_not_sent(tmp_path):
    """🔴 超时时消息**已经在会话里**了 —— 说「没发进去」会让用户重发、重复执行一次。"""
    claude = RelayClaude([_live()])
    claude.relay = lambda sid, text, timeout=120.0, mode=None: ActionResult(
        False, "发进去了，它还没回。这条不会自动重发。", sid, delivered=True)
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "跑全量测试"))
    msg = b.replier.texts[-1]
    assert "没发进去" not in msg and "别急着重发" in msg
    assert TALKING_PREFIX in msg, "还在对话模式里，指示器不能丢"


def test_refusal_before_sending_still_says_not_sent(tmp_path):
    """阳性对照：真的没送出去（目标被挂起之类）⇒ 照实说，让他放心重发。"""
    claude = RelayClaude([_live()], relay_ok=False,
                         why="它在电脑上被挂起了（按过 Ctrl-Z），发进去不会有人理。")
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "继续"))
    assert "没发进去" in b.replier.texts[-1] and "挂起" in b.replier.texts[-1]


def test_reviewer_deny_blocks_the_relay(tmp_path):
    """审查层照常生效 —— 换了条路不等于绕过闸门。"""
    from bridge.review import Verdict

    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.reviewer = types.SimpleNamespace(review_request=lambda ctx: types.SimpleNamespace(
        verdict=Verdict.DENY, reason="不做这个"))
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "把家目录删了"))
    assert claude.relayed == [] and "不做这个" in b.replier.texts[-1]


def test_paused_scope_does_not_relay(tmp_path):
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    b._paused[SCOPE] = "等你确认"
    _run(b._handle(SCOPE, CHAT, "接着改"))
    assert claude.relayed == [] and "继续" in b.replier.texts[-1]
    assert '"op": "resume"' in b.replier.cards[-1], "暂停了没给「继续」按钮"


def test_one_sentence_at_a_time(tmp_path):
    """上一句还在送就别叠下一句（一个目标串行）。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    b._fleet_inflight[SCOPE] = ("claude", CID)
    _run(b._handle(SCOPE, CHAT, "再来一句"))
    assert claude.relayed == [] and "上一条还在发" in b.replier.texts[-1]


# ---------- /cancel ----------


@pytest.mark.parametrize("word", ["/cancel", "取消"])
def test_cancel_leaves_talk_mode_and_the_next_line_is_normal(tmp_path, word):
    """🔴 目标是粘住的 ⇒ 退出口必须可靠，不然他永远在跟远端会话说话。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "第一句"))
    _run(b._handle(SCOPE, CHAT, word))
    assert b.fleet.peek_pending(SCOPE) is None
    _run(b._handle(SCOPE, CHAT, "这句该回到正常流程"))
    assert b.handled == ["这句该回到正常流程"]
    assert [t for _, t in claude.relayed] == ["第一句"]


def test_cancel_does_not_pretend_to_stop_the_turn(tmp_path):
    """/cancel 只退出对话模式，**不**停目标那一轮 —— 要说清楚，否则他以为活也撤了。

    🔴 第 41 轮改了判据的**措辞**，没改它的意思：那一轮现在真停得了（在那个窗口里按
    Esc，`control.stop` 先试这条路），所以文案不再说「只能到电脑上按 Esc」，
    改成指向卡片上的「停下」按钮。这里要钉的还是原来那两件事：
    ① 说清它手里那一轮**还在跑**、② `/cancel` 自己**一个停止动作都没发**。
    """
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "/cancel"))
    msg = b.replier.texts[-1]
    assert "还在跑" in msg, "没说清它手里那一轮还在跑"
    assert "停下" in msg, "没告诉他怎么停"
    assert claude.stopped == [] and claude.relay_stopped == [] and claude.signals == []


def test_cancel_also_voids_a_waiting_consent_card(tmp_path):
    """说了取消，再去点那张还挂着的确认卡不该进对话模式。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _run(b._handle(SCOPE, CHAT, "/cancel"))
    assert b._pending_consent.get(SCOPE) is None
    _run(b._handle(SCOPE, CHAT, "/talk!"))
    assert b.fleet.peek_pending(SCOPE) is None, "取消之后 /talk! 还能进去"


def test_cancel_with_nothing_pending_is_harmless(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    _run(b._handle(SCOPE, CHAT, "取消"))
    assert "没有要取消的" in b.replier.texts[-1]


# ---------- /stop：不假装能停 ----------


def test_stop_in_talk_mode_tells_the_truth_and_sends_no_signal(tmp_path):
    """🔴 实测 SIGINT 会关掉用户**整个会话** ⇒ 一个信号都不发，只说在哪停。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "/stop"))
    msg = b.replier.texts[-1]
    assert "Esc" in msg, "没告诉用户去哪停"
    assert "停了" not in msg and "正在停" not in msg, "假装停下了"
    assert claude.relay_stopped == [CID], "没走 relay_stop 那条诚实的路"
    assert claude.signals == [], "给用户自己的会话发了信号"


def test_stop_offers_cancel_as_the_thing_that_does_work(tmp_path):
    """停不了的时候要给出**能做到**的替代：退出对话模式。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "/stop"))
    assert "退出对话" in b.replier.texts[-1]
    assert '"op": "quit"' in b.replier.cards[-1], "停不了却没给「退出对话」按钮"


def test_stop_reaches_the_sticky_target_even_when_nothing_is_inflight(tmp_path):
    """在对话模式里但这一刻没有在发 ⇒ /stop 仍该对着那个会话答，不能回「没有在跑的任务」。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    assert b._fleet_inflight == {}
    _run(b._handle(SCOPE, CHAT, "/stop"))
    assert "没有在跑" not in b.replier.texts[-1]
    assert claude.relay_stopped == [CID]


# ---------- 选路：别走回会分叉的那条 ----------


def test_control_sends_a_live_session_through_relay(tmp_path):
    claude = RelayClaude([_live()])
    ctl = FleetControl(Fleet({"claude": claude}, timeout=1.0))
    ctl.overview()
    res = ctl.send("claude", CID, "hi", policy=_policy(tmp_path), relay=True)
    assert res.ok and res.delivered
    assert claude.relayed and claude.sent == [], "有人占着还走了 resume ⇒ 会分叉"


def test_control_falls_back_to_resume_when_nobody_holds_it(tmp_path):
    """没人占着的会话走 resume 更好（同步拿回复，不用起信使）。"""
    claude = RelayClaude([_live(can_relay=False, owner="none", pids=(), status="idle")],
                         relay_ok=False, why="这个会话现在没开着。")
    ctl = FleetControl(Fleet({"claude": claude}, timeout=1.0))
    ctl.overview()
    res = ctl.send("claude", CID, "hi", policy=_policy(tmp_path), relay=True)
    assert res.ok and claude.sent, "没人占着却没退回 resume"


def test_control_explains_instead_of_forking_when_relay_is_refused(tmp_path):
    """终端占着 + relay 不通 ⇒ 照实说，**不**退回 resume（那条路会分叉）。"""
    claude = RelayClaude([_live()], relay_ok=False, why="这个会话没有名字，发不了。")
    ctl = FleetControl(Fleet({"claude": claude}, timeout=1.0))
    ctl.overview()
    res = ctl.send("claude", CID, "hi", policy=_policy(tmp_path), relay=True)
    assert not res.ok and "没有名字" in res.text
    assert claude.sent == [], "relay 不通就去 resume 了 ⇒ 分叉"


def test_relay_errors_never_escape_to_the_user(tmp_path):
    class Boom(RelayClaude):
        def can_relay(self, sid):
            raise RuntimeError("查的时候炸了")

    claude = Boom([_live()])
    ctl = FleetControl(Fleet({"claude": claude}, timeout=1.0))
    ctl.overview()
    res = ctl.send("claude", CID, "hi", policy=_policy(tmp_path), relay=True)
    assert not res.ok and claude.sent == []


# ---------- 文案闸门 ----------


def test_help_explains_talk_mode():
    """帮助里要讲清对话模式：怎么进、怎么退、权限是谁的、停不了。"""
    from bridge.app import HELP

    # 怎么退：「退出对话」按钮（2026-11-05 全按钮化，不再叫人打 /cancel）
    assert "接着聊" in HELP and "退出对话" in HELP
    assert "权限" in HELP, "没说权限是电脑那边那套"
    assert "Esc" in HELP, "没说它那一轮只能在电脑上停"


def test_talk_copy_has_no_internal_jargon():
    """给手机用户看的话里不许出现内部词（同 test_copy_quality 的闸门）。"""
    import bridge.fleet.cards as cards
    from bridge.fleet import relay as relay_mod

    texts = [v for k, v in vars(cards).items()
             if k.startswith(("TALKING_", "WHY_", "TEXT_")) and isinstance(v, str)]
    texts += [v for k, v in vars(relay_mod).items()
              if k.startswith(("WHY_", "TEXT_")) and isinstance(v, str)]
    assert texts, "一条文案都没抓到，这条测试失效了"
    for text in texts:
        lowered = text.lower()
        for word in ("agent", "stderr", "traceback", "returncode", "subtype", "exception", "jsonl",
                     "socket", "sigint", "pid"):
            assert word not in lowered, f"文案里有内部词 {word}：{text[:60]}"


# ---------- 每句话发之前重查 / 别把手机的档位带过去 ----------


class _Counting(RelayClaude):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.checks: list = []
        self.relay_kwargs: list = []

    def can_relay(self, sid):
        self.checks.append(sid)
        return super().can_relay(sid)

    def relay(self, sid, text, timeout=120.0, **kw):
        self.relay_kwargs.append(kw)
        self.relayed.append((sid, text))
        return ActionResult(True, self.reply, sid, delivered=True)


def test_every_sentence_is_rechecked_before_it_goes_in(tmp_path):
    """🔴 权限档在电脑上随时能改 ⇒ **每句**发之前都要重查，不能只在进对话模式时查一次。

    档位变了还照发，消息会被扣在那儿等人点，而用户只看到「发出去了」。
    """
    claude = _Counting([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "第一句"))
    _run(b._handle(SCOPE, CHAT, "第二句"))
    assert len(claude.checks) == 2, f"只查了 {len(claude.checks)} 次，第二句没重查"


def test_relay_never_carries_the_phone_permission_mode(tmp_path):
    """relay 不起目标进程 ⇒ 不许把手机这边的档位 / 批准目录塞给它。

    目标用的是**它在电脑上那套权限**（这正是进之前要确认一次的原因）。
    真传了反而更危险：会让人以为手机这边的档位管得到它。
    """
    claude = _Counting([_live()])
    b = _bridge(tmp_path, claude, mode="trusted")
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "改一下"))
    kw = claude.relay_kwargs[0]
    assert "policy" not in kw and "approval_dir" not in kw
    assert kw.get("mode") in (None, ""), "把手机上的档位带给了目标会话"


# ---------- scope：别把别的聊天拉进对话模式 ----------


def test_group_consent_does_not_drag_the_dm_into_talk_mode(tmp_path):
    """群里点的确认只作用于那个群 —— scope 必须和「下一条消息会落到哪」一致。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {SCOPE: CHAT, "group:g1": "oc_group"}
    b.fleet.overview()
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id="oc_group"))
    b._on_fleet_click(event, OWNER, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    b._on_relay_click(event, OWNER, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending("group:g1") is not None
    assert b.fleet.peek_pending(SCOPE) is None, "群里的确认把单聊也拉进了对话模式"


def test_restart_before_any_message_does_not_guess_the_scope(tmp_path):
    """重启后 _chat_ids 是空的 ⇒ 不猜（原来会把目标记到点的人的单聊上）。"""
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {}
    b.fleet.overview()
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id="oc_group"))
    resp = b._on_fleet_click(event, OWNER, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is None and b.fleet.peek_pending("group:g1") is None
    assert "会话" in str(getattr(getattr(resp, "toast", None), "content", ""))


def test_talk_mode_target_expires_and_the_next_line_is_normal(tmp_path):
    """忘了 /cancel 是常态（手机上没有常驻界面提醒）⇒ 过期后回到正常流程并说明白。"""
    clock = [1000.0]
    claude = RelayClaude([_live()])
    b = _bridge(tmp_path, claude)
    b.fleet._now = lambda: clock[0]
    b.fleet.overview()
    _talk(b)
    clock[0] += 301
    _run(b._handle(SCOPE, CHAT, "那你修一下"))
    assert claude.relayed == [] and b.handled == ["那你修一下"]
    assert any("接着聊" in t for t in b.replier.texts)


# ---------- 2026-11-04 三路审查：对话模式的状态不许凭空消失 ----------


def _talk_bridge(tmp_path, adapter):
    """进了对话模式、且目标已记下的桥。"""
    b = _bridge(tmp_path, adapter)
    b.fleet.overview()
    b.fleet.set_pending(SCOPE, adapter.tool, CID)
    return b


def test_second_sentence_while_in_flight_is_not_rerouted(tmp_path):
    """🔴 第一句在飞时补第二句：原来目标已被取走 ⇒ 这句悄悄落进桥自己的 claude。"""
    ad = RelayClaude([_live(CID)])
    b = _talk_bridge(tmp_path, ad)
    b._fleet_inflight[SCOPE] = ("claude", CID)
    _run(b._handle(SCOPE, CHAT, "顺手把 README 也改一下"))
    assert b.handled == [], "在飞时的第二句被桥自己的 claude 执行了"
    assert "上一条还在发" in b.replier.texts[-1]
    assert b.fleet.peek_pending(SCOPE) is not None, "对话模式被这一句弄丢了"


def test_cancel_while_in_flight_really_exits(tmp_path):
    """🔴 在飞时 /cancel：原来回「没有要取消的」，回复到了之后对话模式又自己挂回来。"""
    ad = RelayClaude([_live(CID)])
    b = _talk_bridge(tmp_path, ad)
    b._fleet_inflight[SCOPE] = ("claude", CID)
    _run(b._handle(SCOPE, CHAT, "/cancel"))
    said = b.replier.texts[-1]
    assert "退出" in said and "没有要取消的" not in said
    assert b._talk_quit.get(SCOPE) is True, "没记下退出意向 ⇒ 回复到了会把模式挂回来"


def test_peek_does_not_consume_the_target(tmp_path):
    """对话模式是「看」出来的，不是「取」出来的 —— 否则刷新卡片时看不出在跟谁说话。"""
    ad = RelayClaude([_live(CID)])
    b = _talk_bridge(tmp_path, ad)
    t1, expired = b.fleet.peek_pending_fresh(SCOPE)
    t2, _ = b.fleet.peek_pending_fresh(SCOPE)
    assert t1 is not None and t2 is not None and not expired


def test_stop_also_stops_the_local_turn(tmp_path):
    """🔴 对话模式里 /stop：原来只回「到电脑上按 Esc」，本地那一轮一路跑到底。"""
    import asyncio as _a

    ad = RelayClaude([_live(CID)])
    b = _talk_bridge(tmp_path, ad)
    b.fleet.stop = lambda tool, sid, relay=False: ActionResult(False, "这个会话停不了，只能到电脑上按 Esc。", sid)
    ev = _a.Event()
    b._cancels[SCOPE] = ev
    _run(b._handle(SCOPE, CHAT, "/stop"))
    assert ev.is_set(), "本地那一轮没被停"
    assert "手机这边自己跑的那一轮也停了" in b.replier.texts[-1]


def test_indicator_not_swallowed_by_unclosed_code_fence(tmp_path):
    """🔴 回复里有没闭合的 ``` ⇒ 原来尾巴上的「正在和 X 说话」被吞进代码块。"""
    from bridge import reply as r

    ad = RelayClaude([_live(CID)], reply="看这里：\n```py\nprint(1)\nx = 2")
    b = _bridge(tmp_path, ad)
    b.fleet.overview()
    # 2026-11-06 第 10 轮起，单聊发之前也做身份复查（review-r9 🔴1）⇒ 目标只在确认之后才会被摆上，
    # 这条测试原来跳过确认直接摆目标，现在先补一笔确认记录（它测的是代码块指示器，不是确认）
    b.relay_consent.grant(SCOPE, "claude", CID, b._relay_ident("claude", CID))
    b.fleet.set_pending(SCOPE, "claude", CID)
    _run(b._handle(SCOPE, CHAT, "看一下"))
    sent = next(t for t in reversed(b.replier.texts) if "回复" in t)
    blocks = r.parse(sent, detect=False)
    where = [blk.kind for blk in blocks if "正在和" in blk.body]
    assert where and all(k == "text" for k in where), f"指示器落进了 {where}"
