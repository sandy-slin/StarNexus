"""会话总控接进飞书（section E）。

全部用假 adapter / 假 replier / tmp_path；**不起真 CLI、不碰真实会话**。
盯这几件事：
- 命令分派：/sessions、会话、总控、/cancel、取消
- 卡片 JSON 合法：schema 2.0、没有 action 容器、< 20KB
- 伪造回调被拒（op / tool / id 不在白名单或不在最近一次扫描里）
- 待发目标 5 分钟过期；发之前 adapter 再查一次（终端刚打开它 ⇒ 不发、不起子进程）
- Claude send 不加载用户 settings：`--setting-sources project`、认证键来自 settings.json、没有用户的 allow 规则
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

from bridge.fleet import ActionResult, FleetJobs, Message, RunResult, Session, SubprocessRunner  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_CARD_BYTES,
    build_detail_card,
    build_overview_card,
    resolve_fleet_click,
)
from bridge.fleet.control import FleetControl  # noqa: E402
from bridge.fleet.scan import Fleet, Overview  # noqa: E402
from bridge.policy import Policy  # noqa: E402

OWNER = "ou_owner"
CHAT = "oc_chat"
SCOPE = f"dm:{OWNER}"
CID = "33333333-3333-4333-8333-333333333333"
TID = "019a0000-0000-7000-8000-000000000001"


def _policy(tmp_path, mode="readonly"):
    return Policy(mode=mode, workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                  model="claude-sonnet-5-5", turn_timeout_sec=60, ceiling_mode="trusted")


# ---------- 假 adapter ----------


class FakeAdapter:
    def __init__(self, tool, sessions=(), messages=(), delay=0.0, boom=False, note=""):
        self.tool = tool
        self.sessions = list(sessions)
        self.messages = list(messages)
        self.delay = delay
        self.boom = boom
        self.last_error = note
        self.sent = []
        self.stopped = []
        self.can_send_now = True

    def list(self, limit=20):
        if self.delay:
            time.sleep(self.delay)
        if self.boom:
            raise RuntimeError("坏了")
        return self.sessions[:limit]

    def running(self):
        return [s for s in self.sessions if s.pids]

    def find(self, sid):
        return next((s for s in self.sessions if s.id == sid), None)

    def read(self, sid, n=4):
        return self.messages[-n:]

    def send(self, sid, text, timeout=600, **kw):
        if not self.can_send_now:  # 模拟 adapter 在锁里复查：刚被终端打开
            return ActionResult(False, "它开在终端里，发进去会分叉成两段对话", sid)
        self.sent.append((sid, text, kw))
        return ActionResult(True, f"回复：{text}", sid)

    def stop(self, sid):
        self.stopped.append(sid)
        return ActionResult(False, "终端里的会话只能在终端里停", sid)


def _sess(tool, sid, **kw):
    base = dict(tool=tool, surface="cli", id=sid, title="修登录", cwd="/Users/x/codes/demo",
                status="idle", owner="none", updated_at=time.time() - 120, can_send=True)
    base.update(kw)
    return Session(**base)


def _control(*adapters, **kw):
    return FleetControl(Fleet({a.tool: a for a in adapters}, timeout=kw.pop("timeout", 1.0)), **kw)


# ---------- scan ----------


def test_overview_merges_tools_live_first():
    claude = FakeAdapter("claude", [_sess("claude", CID), _sess("claude", "c2", status="running", pids=(9,))])
    codex = FakeAdapter("codex", [_sess("codex", TID)])
    view = Fleet({"claude": claude, "codex": codex}).overview()
    assert view.sessions[0].id == "c2"  # 在跑的排最前
    assert {s.tool for s in view.sessions} == {"claude", "codex"}
    assert view.running_count() == 1 and view.notes == []


def test_overview_one_tool_failing_or_slow_keeps_the_rest():
    import threading

    release = threading.Event()

    class Hung(FakeAdapter):
        def list(self, limit=20):
            release.wait(10)  # 一直卡着，直到测试放开
            return [_sess("cursor", "abcd1234")]

    ok = FakeAdapter("claude", [_sess("claude", CID)])
    broken = FakeAdapter("codex", boom=True)
    fleet = Fleet({"claude": ok, "codex": broken, "cursor": Hung("cursor")}, timeout=0.5)
    try:
        started = time.monotonic()
        view = fleet.overview()
        # 不等卡住的那家（机器很忙时线程调度会慢，上限放宽，但远小于 10 秒）
        assert time.monotonic() - started < 5
        assert [s.id for s in view.sessions] == [CID]
        joined = " ".join(view.notes)
        assert "Codex" in joined and "Cursor" in joined and "太慢" in joined
        # 上一次还卡着 ⇒ 这次不再叠一个线程
        again = fleet.overview()
        assert any("上一次还没读完" in n for n in again.notes)
    finally:
        release.set()


def test_overview_passes_adapter_note():
    view = Fleet({"codex": FakeAdapter("codex", note="lsof 超时")}).overview()
    assert any("lsof 超时" in n for n in view.notes)


def test_real_adapters_overview_under_budget(tmp_path):
    """三家真 adapter 指到空的假目录 + 假 ps：不抛、在预算内。"""
    from bridge.fleet.claude import ClaudeAdapter
    from bridge.fleet.codex import CodexAdapter
    from bridge.fleet.cursor import CursorAdapter

    class PsOnly(SubprocessRunner):
        def run(self, argv, **kw):
            return RunResult(returncode=0, stdout="")

    jobs = FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None)
    runner = PsOnly()
    fleet = Fleet({
        "claude": ClaudeAdapter(str(tmp_path / "c"), str(tmp_path / "b"), str(tmp_path / "a"), jobs=jobs, runner=runner),
        "codex": CodexAdapter(home=str(tmp_path / "x"), jobs=jobs, runner=runner),
        "cursor": CursorAdapter(str(tmp_path / "cu"), str(tmp_path / "cuu"), jobs=jobs, runner=runner),
    })
    view = fleet.overview()
    assert view.sessions == []
    assert all("太慢" not in n and "出错" not in n for n in view.notes), view.notes


# ---------- 卡片 ----------


def _assert_card_ok(blob):
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob
    assert len(blob.encode("utf-8")) < MAX_CARD_BYTES
    return card


from tests._overview_rows import tap_as_button  # noqa: E402


def _buttons(card):
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                found.append(node)
            elif tap_as_button(node):  # 11-16 会话行是交互容器
                found.append(tap_as_button(node))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(card)
    return found


def test_overview_card_valid_and_grouped():
    sessions = [_sess("claude", f"c{i}", title="很长的标题" * 30) for i in range(40)]
    sessions += [_sess("codex", TID, status="running", pids=(5,))]
    view = Overview(sessions=sessions, notes=["Cursor 读得太慢，这次没等它"])
    card = _assert_card_ok(build_overview_card(view))
    values = [b["behaviors"][0]["value"] for b in _buttons(card)]
    assert {"act": "fleet", "op": "open", "tool": "codex", "id": TID} in values
    assert values[-1] == {"act": "fleet", "op": "refresh"}
    assert "Cursor 读得太慢" in json.dumps(card, ensure_ascii=False)


def test_overview_card_line_format():
    from bridge.fleet.cards import row_label

    label = row_label(_sess("codex", TID, status="running"))
    assert label.startswith("Codex · demo · 在跑 · 2 分钟前")


def test_detail_card_buttons_follow_capabilities():
    msgs = [Message("user", "u" * 5000), Message("assistant", "a"), Message("user", "b"),
            Message("assistant", "c"), Message("user", "d")]
    can = _assert_card_ok(build_detail_card(_sess("claude", CID, can_stop=True), msgs))
    # 只看对着这个会话的按钮；底下那排导航（[回到总览]，act=do）是 actions.py 的
    ops = [b["behaviors"][0]["value"]["op"] for b in _buttons(can)
           if b["behaviors"][0]["value"]["act"] == "fleet"]
    # 第 33 轮：闲着的会话多一个「✓ 结束」（在跑的没有，见下面 held）
    assert ops == ["send", "stop", "refresh", "settle"]
    blob = json.dumps(can, ensure_ascii=False)
    assert "u" * 5000 not in blob  # 最近 4 条、每条截短

    held = _sess("claude", CID, can_send=False, why_not_send="它开在终端里，发进去会分叉",
                 status="running", pids=(7,), owner="terminal")
    card = _assert_card_ok(build_detail_card(held, msgs))
    ops = [b["behaviors"][0]["value"]["op"] for b in _buttons(card)
           if b["behaviors"][0]["value"]["act"] == "fleet"]
    assert ops == ["refresh"]
    text = json.dumps(card, ensure_ascii=False)
    assert "它开在终端里" in text and "这个会话不是从手机上开的" in text


def test_detail_card_redacts_messages():
    msgs = [Message("assistant", "key 是 sk-ant-api03-" + "A" * 40)]
    blob = build_detail_card(_sess("claude", CID), msgs, redact=lambda s: s.replace("sk-ant", "[x]"))
    assert "sk-ant" not in blob


# ---------- 回调白名单 ----------

KNOWN = {("claude", CID), ("codex", TID)}


@pytest.mark.parametrize("value", [
    {"act": "fleet", "op": "open", "tool": "claude", "id": "99999999-9999-4999-8999-999999999999"},
    {"act": "fleet", "op": "exec", "tool": "claude", "id": CID},
    {"act": "fleet", "op": "send", "tool": "bash", "id": CID},
    {"act": "fleet", "op": "send", "tool": "claude", "id": CID, "text": "rm -rf ~"},
    {"act": "fleet", "op": "send", "tool": "codex", "id": CID},
    {"act": "fleet", "op": "open", "tool": "claude", "id": ["x"]},
    {"act": "menu", "op": "open", "tool": "claude", "id": CID},
    {"act": "fleet", "op": "open", "tool": "claude"},
    "not a dict",
    None,
])
def test_forged_clicks_rejected(value):
    assert resolve_fleet_click(value, KNOWN) is None


def test_known_click_and_bare_refresh_accepted():
    assert resolve_fleet_click({"act": "fleet", "op": "send", "tool": "claude", "id": CID}, KNOWN) == {
        "op": "send", "tool": "claude", "id": CID}
    assert resolve_fleet_click({"act": "fleet", "op": "refresh"}, set())["op"] == "refresh"


# ---------- 待发目标 ----------


def test_pending_target_expires_after_ttl():
    clock = [1000.0]
    ctl = _control(FakeAdapter("claude", [_sess("claude", CID)]), ttl=300, now=lambda: clock[0])
    ctl.overview()
    ctl.set_pending(SCOPE, "claude", CID)
    clock[0] += 301
    target, expired = ctl.take_pending(SCOPE)
    assert target is None and expired
    assert ctl.take_pending(SCOPE) == (None, False)  # 取一次就清掉


def test_pending_target_within_ttl_and_cancel():
    clock = [1000.0]
    ctl = _control(FakeAdapter("claude", [_sess("claude", CID)]), now=lambda: clock[0])
    ctl.overview()
    ctl.set_pending(SCOPE, "claude", CID)
    clock[0] += 299
    target, _ = ctl.take_pending(SCOPE)
    assert target.id == CID and "修登录" in target.label
    ctl.set_pending(SCOPE, "claude", CID)
    assert ctl.clear_pending(SCOPE) is not None and ctl.peek_pending(SCOPE) is None


def test_control_routes_policy_to_claude_and_mode_to_others(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    codex = FakeAdapter("codex", [_sess("codex", TID, cwd=str(tmp_path))])
    ctl = _control(claude, codex)
    ctl.overview()
    pol = _policy(tmp_path, "write")
    ctl.send("claude", CID, "hi", policy=pol, approval_dir="/tmp/a")
    ctl.send("codex", TID, "hi", policy=pol)
    assert claude.sent[0][2]["policy"] is pol and claude.sent[0][2]["approval_dir"] == "/tmp/a"
    # 🔴 Codex / Cursor 一律按只读发，不跟随手机档位 —— 它们没有审批钩子，红线管不到
    assert codex.sent[0][2] == {"mode": "readonly"}


def test_codex_and_cursor_never_get_loosened_mode(tmp_path):
    """全授权档也不能把 sandbox 放开（本机 codex 默认 danger-full-access）。"""
    for mode in ("readonly", "write", "trusted"):
        codex = FakeAdapter("codex", [_sess("codex", TID, cwd=str(tmp_path))])
        ctl = _control(FakeAdapter("claude", []), codex)
        ctl.overview()
        ctl.send("codex", TID, "hi", policy=_policy(tmp_path, mode))
        assert codex.sent[0][2] == {"mode": "readonly"}, mode


def test_codex_send_outside_workspace_is_refused(tmp_path):
    """和 Claude 一样过工作目录白名单 —— 原来 Codex 在白名单外的项目也能发。"""
    outside = _sess("codex", TID, cwd="/definitely/not/in/workspaces")
    codex = FakeAdapter("codex", [outside])
    ctl = _control(FakeAdapter("claude", []), codex)
    ctl.overview()
    res = ctl.send("codex", TID, "hi", policy=_policy(tmp_path, "trusted"))
    assert not res.ok and "目录" in res.text
    assert codex.sent == [], "白名单外的项目也发出去了"


# ---------- 接进 Bridge ----------


class FakeReplier:
    def __init__(self):
        self.texts = []
        self.cards = []

    def send(self, chat_id, text):
        from bridge.redact import redact_with_notice

        self.texts.append(redact_with_notice(text))
        return True

    def send_card(self, chat_id, card):
        self.cards.append(card)
        return True


class _AlreadyConsented:
    """「这个会话 24 小时内确认过了」。

    🔴 下面这批测试写在**进对话模式要确认一次**之前，它们盯的是「点了 [接着聊] 之后
    下一句发到哪」，不是那道确认闸门。所以这里把确认当成已经点过，让它们继续盯自己那件事。
    闸门本身（没确认过 ⇒ 先弹卡、**不记目标**）在 `tests/test_relay_integration.py` 里有
    专门的用例 + 阳性对照，不靠这个替身。
    """

    last_save_ok = True

    def __init__(self):
        self.granted_keys = []

    def granted(self, *args, **kw):
        return True

    def grant(self, *args, **kw):
        self.granted_keys.append(tuple(args))

    def revoke(self, *args, **kw):
        pass


def _bridge(tmp_path, *adapters, mode="readonly"):
    from bridge.app import Bridge
    from bridge.approval import ApprovalGate
    from bridge.review import Reviewer

    b = Bridge.__new__(Bridge)
    b.relay_consent = _AlreadyConsented()
    b._pending_consent = {}
    pol = _policy(tmp_path, mode)
    b.settings = types.SimpleNamespace(owner_open_ids=frozenset({OWNER}), policy=pol, custom_rules=())
    b.replier = FakeReplier()
    b._chat_ids = {SCOPE: CHAT}
    b._paused = {}
    b._awaiting = {}
    b._cancels = {}
    b._loop = None
    b.reviewer = Reviewer()
    b.gate = ApprovalGate(str(tmp_path / "approvals"))
    b.fleet = _control(*adapters)
    b._fleet_inflight = {}
    b.handled = []

    async def fake_run_one(session, chat_id, text):  # 自己的 claude 不该被叫到
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


@pytest.mark.parametrize("text", ["/sessions", "会话", "总控", "/会话", "/SESSIONS"])
def test_sessions_command_sends_overview_card(tmp_path, text):
    b = _bridge(tmp_path, FakeAdapter("claude", [_sess("claude", CID)]))
    _run(b._handle(SCOPE, CHAT, text))
    assert len(b.replier.cards) == 1 and b.handled == []
    _assert_card_ok(b.replier.cards[0])
    assert ("claude", CID) in b.fleet.known_keys()


def test_word_inside_a_sentence_is_not_a_command(tmp_path):
    b = _bridge(tmp_path, FakeAdapter("claude"))
    _run(b._handle(SCOPE, CHAT, "会话为什么断了"))
    assert b.replier.cards == [] and b.handled == ["会话为什么断了"]


def _click(b, value, clicker=OWNER):
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=CHAT))
    return b._on_fleet_click(event, clicker, value)


def _toast(resp):
    toast = getattr(resp, "toast", None)
    return f"{getattr(toast, 'type', '')} {getattr(toast, 'content', '')}"


def test_send_flow_sends_next_message_and_posts_redacted_reply(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is not None
    secret = "sk-ant-api03-" + "Q" * 40
    claude.send = lambda sid, text, timeout=600, **kw: ActionResult(True, f"token={secret}", sid)
    _run(b._handle(SCOPE, CHAT, "接着修"))
    assert b.handled == []  # 没走自己的 claude
    reply = b.replier.texts[-1]
    assert "回复" in reply and secret not in reply  # 过了 reply 的脱敏
    # 🔴 目标保持住：下一句还发给它，并明说怎么退出（原来发一句就清掉 ⇒ 下一句悄悄落到桥自己的 claude）
    assert b.fleet.peek_pending(SCOPE) is not None
    assert '"op": "quit"' in b.replier.cards[-1]  # 11-05：退出口是按钮，不叫人打 /cancel


def test_second_message_still_goes_to_the_session(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    claude.send = lambda sid, text, timeout=600, **kw: (claude.sent.append((sid, text, kw)), ActionResult(True, "好", sid))[1]
    _run(b._handle(SCOPE, CHAT, "看看测试为什么挂"))
    _run(b._handle(SCOPE, CHAT, "那你修一下"))
    assert [t for _, t, _ in claude.sent] == ["看看测试为什么挂", "那你修一下"]
    assert b.handled == [], "第二句落到了桥自己的 claude"


def test_delivered_failure_does_not_say_not_sent(tmp_path):
    """🔴 超时 / 被停 / 等确认时消息已经进了会话 —— 说「没发进去」会让用户重发、重复执行。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    claude.send = lambda sid, text, timeout=600, **kw: ActionResult(False, "等太久没回完", sid, delivered=True)
    _run(b._handle(SCOPE, CHAT, "跑全量测试"))
    msg = b.replier.texts[-1]
    assert "没发进去" not in msg and "已经发给" in msg and "别急着重发" in msg


def test_pre_spawn_refusal_still_says_not_sent(tmp_path):
    """阳性对照：真的没起进程（比如终端占着）时，仍然如实说「没发进去」。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    claude.send = lambda sid, text, timeout=600, **kw: ActionResult(False, "它开在终端里", sid)
    _run(b._handle(SCOPE, CHAT, "继续"))
    assert "没发进去" in b.replier.texts[-1]


def test_stop_reaches_inflight_fleet_send(tmp_path):
    """手机正在往电脑上的会话发 —— /stop 要停它，不能回「现在没有在跑的任务」。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    stopped = []
    # relay=True：对话模式里的 /stop 走 adapter 的 relay_stop（Claude 终端会话那条只会
    # 照实说「到电脑上按 Esc」，不发任何信号）
    b.fleet.stop = lambda tool, sid, relay=False: (
        stopped.append((tool, sid, relay)), ActionResult(True, "已经让它停下了。", sid))[1]
    b._fleet_inflight[SCOPE] = ("claude", CID)
    _run(b._handle(SCOPE, CHAT, "/stop"))
    assert stopped == [("claude", CID, True)]
    assert "没有在跑" not in b.replier.texts[-1]


def test_send_rechecked_before_spawn(tmp_path):
    """点 [接着聊] 时能发；五分钟里终端打开了它 ⇒ 发的时候 adapter 复查拒掉，如实说。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    claude.can_send_now = False
    _run(b._handle(SCOPE, CHAT, "接着修"))
    assert claude.sent == [] and "没发进去" in b.replier.texts[-1] and "终端" in b.replier.texts[-1]


def test_expired_target_falls_back_to_normal_flow(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    clock = [1000.0]
    b.fleet._now = lambda: clock[0]
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    clock[0] += 301
    _run(b._handle(SCOPE, CHAT, "接着修"))
    assert claude.sent == [] and b.handled == ["接着修"]
    assert any("5 分钟" in t for t in b.replier.texts)


@pytest.mark.parametrize("word", ["/cancel", "取消"])
def test_cancel_clears_target(tmp_path, word):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _run(b._handle(SCOPE, CHAT, word))
    assert b.fleet.peek_pending(SCOPE) is None
    _run(b._handle(SCOPE, CHAT, "接着修"))
    assert claude.sent == [] and b.handled == ["接着修"]


def test_reviewer_deny_blocks_fleet_send(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    from bridge.review import Verdict

    b.reviewer = types.SimpleNamespace(review_request=lambda ctx: types.SimpleNamespace(
        verdict=Verdict.DENY, reason="不做这个"))
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _run(b._handle(SCOPE, CHAT, "删掉家目录"))
    assert claude.sent == [] and "不做这个" in b.replier.texts[-1]


def test_forged_and_stranger_clicks_do_nothing(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    resp = _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": "deadbeef-0000-4000-8000-000000000000"})
    assert "过期" in _toast(resp) and b.fleet.peek_pending(SCOPE) is None
    resp = _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID}, clicker="ou_stranger")
    assert b.fleet.peek_pending(SCOPE) is None and "主人" in _toast(resp)


def test_group_click_sets_target_on_group_scope(tmp_path):
    """群里点 [接着聊]：下一条消息的 scope 是 group:<chat>，目标要记在那儿。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    group = "oc_group"
    b._chat_ids[f"group:{group}"] = group
    b.fleet.overview()
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=group))
    b._on_fleet_click(event, OWNER, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(f"group:{group}") is not None
    assert b.fleet.peek_pending(SCOPE) is None


def test_card_action_routes_fleet_before_token_check(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    b.approvals = types.SimpleNamespace(redeem_detailed=lambda *a: pytest.fail("不该走审批令牌"))
    b.touch = lambda: None
    data = types.SimpleNamespace(event=types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=OWNER),
        action=types.SimpleNamespace(value={"act": "fleet", "op": "send", "tool": "claude", "id": CID}),
        context=types.SimpleNamespace(open_chat_id=CHAT)))
    b.on_card_action(data)
    assert b.fleet.peek_pending(SCOPE) is not None


def test_status_shows_running_count(tmp_path):
    b = _bridge(tmp_path, FakeAdapter("claude", [_sess("claude", CID, pids=(3,), status="running")]))
    b.sessions = types.SimpleNamespace(get=lambda s: types.SimpleNamespace(workspace=str(tmp_path)))
    b.connection_alive = lambda: True
    b._last_event_at = time.monotonic()
    _run(b._handle_command(SCOPE, CHAT, "/status"))
    assert "电脑上在跑 1 个会话" in b.replier.texts[-1]


def test_help_mentions_sessions():
    from bridge.app import HELP

    # 2026-11-05 全按钮化：退出靠「退出对话」按钮，帮助里不再叫人打 /cancel
    assert "/sessions" in HELP and "退出对话" in HELP


def test_fleet_send_uses_isolated_approval_dir(tmp_path):
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude, mode="trusted")
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _run(b._handle(SCOPE, CHAT, "改一下"))
    kw = claude.sent[0][2]
    assert kw["approval_dir"] and kw["approval_dir"] != b.gate.dir_for(SCOPE)
    assert os.path.exists(os.path.join(kw["approval_dir"], "TRUSTED"))
    assert json.load(open(os.path.join(kw["approval_dir"], "workspaces.json"))) == [str(tmp_path)]


# ---------- Claude send：不让用户的权限规则叠加进来 ----------


class RecordingRunner(SubprocessRunner):
    def __init__(self, stdout=None):
        self.calls = []
        self.stdout = stdout or json.dumps({"type": "result", "result": "好", "session_id": CID})

    def run(self, argv, *, timeout, cwd=None, env=None, input_text=None, on_spawn=None):
        if argv[0] == "ps":
            return RunResult(returncode=0, stdout="")
        self.calls.append({"argv": list(argv), "env": env, "cwd": cwd})
        return RunResult(returncode=0, stdout=self.stdout)


USER_SETTINGS = {
    "env": {"ANTHROPIC_BASE_URL": "https://gw.example.test", "ANTHROPIC_AUTH_TOKEN": "tok-from-settings",
            "SOME_OTHER": "nope"},
    "apiKeyHelper": "/usr/local/bin/helper",
    "permissions": {"defaultMode": "auto", "allow": ["Read(//Users/someone/**)", "Bash(npm install *)"]},
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user-stop-hook"}]}]},
}


def _claude(tmp_path, runner, mode="readonly", hook=None):
    from bridge.fleet.claude import ClaudeAdapter

    home = tmp_path / "claude"
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    proj = home / "projects" / "-work"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{CID}.jsonl"
    path.write_text(json.dumps({"type": "user", "cwd": str(work), "entrypoint": "cli",
                                "message": {"role": "user", "content": "hi"}}) + "\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))
    (home / "settings.json").write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    pol = Policy(mode=mode, workspaces=(str(work),), default_workspace=str(work),
                 model="claude-sonnet-5-5", turn_timeout_sec=60)
    return ClaudeAdapter(str(home), str(tmp_path / "bridge"), str(tmp_path / "app"),
                         jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None),
                         runner=runner, policy=pol, hook_settings=hook)


def test_claude_send_does_not_load_user_settings(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    runner = RecordingRunner()
    ad = _claude(tmp_path, runner)
    res = ad.send(CID, "继续")
    assert res.ok, res.text
    call = runner.calls[0]
    argv, env = call["argv"], call["env"]
    sources = argv[argv.index("--setting-sources") + 1]
    # 🔴 一个来源都不加载：project 级 allow 规则也会叠加（2026-11-04 安全审查）
    assert sources == "", f"--setting-sources 应为空串，拿到 {sources!r}"
    assert "--strict-mcp-config" in argv
    # 认证键来自 settings.json
    assert env["ANTHROPIC_AUTH_TOKEN"] == "tok-from-settings"
    assert env["ANTHROPIC_BASE_URL"] == "https://gw.example.test"
    assert "SOME_OTHER" not in env
    # 用户的 allow 规则 / hooks 哪儿都没有
    blob = json.dumps(argv) + json.dumps(env)
    for leaked in ("Read(//Users/someone/**)", "npm install", "defaultMode", "user-stop-hook"):
        assert leaked not in blob
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings == {"apiKeyHelper": "/usr/local/bin/helper"}


def test_claude_send_write_mode_needs_hook(tmp_path):
    runner = RecordingRunner()
    ad = _claude(tmp_path, runner, mode="write")
    res = ad.send(CID, "改一下", approval_dir=str(tmp_path / "ap"))
    assert not res.ok and "只读" in res.text and runner.calls == []


def test_claude_send_carries_bridge_hook_and_approval_dir(tmp_path):
    from bridge.approval import hook_settings

    runner = RecordingRunner()
    hook = hook_settings("/x/hooks/approval_hook.py")
    ad = _claude(tmp_path, runner, mode="write", hook=lambda: hook)
    res = ad.send(CID, "改一下", approval_dir=str(tmp_path / "ap"))
    assert res.ok, res.text
    call = runner.calls[0]
    settings = json.loads(call["argv"][call["argv"].index("--settings") + 1])
    assert settings["hooks"] == hook["hooks"] and "permissions" not in settings
    assert call["env"]["CARHER_APPROVAL_DIR"] == str(tmp_path / "ap")


def test_claude_send_deferred_is_not_a_normal_reply(tmp_path):
    runner = RecordingRunner(stdout=json.dumps({"type": "result", "result": "", "session_id": CID,
                                                "stop_reason": "tool_deferred"}))
    ad = _claude(tmp_path, runner)
    res = ad.send(CID, "删个文件")
    assert not res.ok and "没做" in res.text


def test_inherited_auth_only_picks_auth_keys(tmp_path):
    from bridge.approval import inherited_auth

    path = tmp_path / "settings.json"
    path.write_text(json.dumps(USER_SETTINGS), encoding="utf-8")
    env, top = inherited_auth(str(path))
    assert set(env) == {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}
    assert top == {"apiKeyHelper": "/usr/local/bin/helper"}
    assert inherited_auth(str(tmp_path / "missing.json")) == ({}, {})


# ---------- Codex：档位压 sandbox ----------


def test_codex_mode_maps_to_sandbox_flag():
    from bridge.fleet.codex import SANDBOX_BY_MODE

    assert SANDBOX_BY_MODE["readonly"] == "read-only"
    assert SANDBOX_BY_MODE["write"] == "workspace-write"


def test_group_click_after_restart_does_not_hijack_dm(tmp_path):
    """🔴 重启后 _chat_ids 里只有单聊。群里点旧卡片 [接着聊]：
    原来目标记到 `dm:<点的人>`，还把单聊 scope 指向这个群 ⇒ 他私聊的下一句被转发走。
    现在：不猜，拒绝；单聊的映射原样不动。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    before = dict(b._chat_ids)
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id="oc_unknown_group"))
    resp = b._on_fleet_click(event, OWNER, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert "重启" in json.dumps(resp.__dict__, ensure_ascii=False, default=lambda o: o.__dict__)
    assert b.fleet.peek_pending(SCOPE) is None, "目标被记到了单聊上"
    assert b._chat_ids == before, "卡片回调改写了 _chat_ids"


def test_dm_click_still_works(tmp_path):
    """阳性对照：在记过的单聊里点，照常记目标。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is not None


@pytest.mark.parametrize("word", ["/cancel", "取消"])
def test_cancel_ends_sticky_target(tmp_path, word):
    """目标现在会保持住 ⇒ 退出口必须可靠：/cancel 之后下一句回到正常流程。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    claude.send = lambda sid, text, timeout=600, **kw: ActionResult(True, "好", sid)
    _run(b._handle(SCOPE, CHAT, "第一句"))
    assert b.fleet.peek_pending(SCOPE) is not None
    _run(b._handle(SCOPE, CHAT, word))
    assert b.fleet.peek_pending(SCOPE) is None
    _run(b._handle(SCOPE, CHAT, "这句回到正常流程"))
    assert b.handled == ["这句回到正常流程"]


# ---------- relay 分派（control.send/stop 的 relay=True） ----------


class RelayAdapter(FakeAdapter):
    """有 relay() / relay_stop() 的 adapter（Codex / Cursor 现在就是这形状）。"""

    def __init__(self, tool, sessions=()):
        super().__init__(tool, sessions)
        self.relayed = []
        self.relay_stopped = []

    def relay(self, sid, text, timeout=600, **kw):
        self.relayed.append((sid, text, kw))
        return ActionResult(True, f"relay 回复：{text}", sid, delivered=True)

    def relay_stop(self, sid):
        self.relay_stopped.append(sid)
        return ActionResult(False, "这一轮只能在电脑上停", sid)


def test_control_relay_true_uses_relay_and_still_clamps_to_readonly(tmp_path):
    """relay=True 走 relay()，但**档位照样压成只读**（它们没有审批钩子）。"""
    codex = RelayAdapter("codex", [_sess("codex", TID, cwd=str(tmp_path))])
    ctl = _control(FakeAdapter("claude", []), codex)
    ctl.overview()
    res = ctl.send("codex", TID, "hi", policy=_policy(tmp_path, "trusted"), relay=True)
    assert res.ok and res.delivered
    assert codex.relayed[0][2] == {"mode": "readonly"}
    assert codex.sent == []  # 没走老的 send


def test_control_relay_still_checks_the_workspace_whitelist(tmp_path):
    """relay 也要过工作目录白名单 —— 不能因为换了条路就绕过闸门。"""
    codex = RelayAdapter("codex", [_sess("codex", TID, cwd="/definitely/not/in/workspaces")])
    ctl = _control(FakeAdapter("claude", []), codex)
    ctl.overview()
    res = ctl.send("codex", TID, "hi", policy=_policy(tmp_path, "trusted"), relay=True)
    assert not res.ok and "目录" in res.text
    assert codex.relayed == [] and codex.sent == []


def test_control_relay_falls_back_to_send_when_adapter_has_none(tmp_path):
    """没实现 relay 的那家自动退回 send()，公开接口不变。"""
    codex = FakeAdapter("codex", [_sess("codex", TID, cwd=str(tmp_path))])
    ctl = _control(FakeAdapter("claude", []), codex)
    ctl.overview()
    res = ctl.send("codex", TID, "hi", policy=_policy(tmp_path), relay=True)
    assert res.ok and codex.sent[0][2] == {"mode": "readonly"}


def test_control_send_without_relay_is_unchanged(tmp_path):
    """老调用方（不传 relay）行为一点不变。"""
    codex = RelayAdapter("codex", [_sess("codex", TID, cwd=str(tmp_path))])
    ctl = _control(FakeAdapter("claude", []), codex)
    ctl.overview()
    ctl.send("codex", TID, "hi", policy=_policy(tmp_path))
    assert codex.sent and codex.relayed == []


def test_control_relay_stop_prefers_relay_stop(tmp_path):
    codex = RelayAdapter("codex", [_sess("codex", TID, cwd=str(tmp_path))])
    ctl = _control(FakeAdapter("claude", []), codex)
    ctl.overview()
    res = ctl.stop("codex", TID, relay=True)
    assert not res.ok and "电脑上停" in res.text
    assert codex.relay_stopped == [TID] and codex.stopped == []
    ctl.stop("codex", TID)  # 不传 relay ⇒ 还是老的 stop
    assert codex.stopped == [TID]


def test_control_relay_errors_never_escape(tmp_path, caplog):
    class Boom(FakeAdapter):
        def relay(self, sid, text, timeout=600, **kw):
            raise RuntimeError("relay 炸了")

    codex = Boom("codex", [_sess("codex", TID, cwd=str(tmp_path))])
    ctl = _control(FakeAdapter("claude", []), codex)
    ctl.overview()
    with caplog.at_level("ERROR", logger="feishu-bridge.fleet"):
        res = ctl.send("codex", TID, "hi", policy=_policy(tmp_path), relay=True)
    # 第 11 轮：原文只进日志，回给手机的是人话
    assert not res.ok and res.text and "relay 炸了" not in res.text
    assert "relay 炸了" in caplog.text


def test_click_after_restart_via_menu_only_still_works(tmp_path):
    """🔴 2026-11-05 用户实测：重启后只点了常驻菜单「🖥 会话」、没打字，再点 [接着聊]
    ⇒ 一直报「服务刚重启过」。菜单事件只带 open_id，_chat_ids 里记的是 `ou_…`，
    卡片回调带真 chat_id `oc_…`，永远对不上。记的就是点的人自己 ⇒ 能确定是单聊，要放行。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {SCOPE: OWNER}  # 菜单点击记下的样子
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is not None, "菜单入口进来的单聊点不了 [接着聊]"
    assert b._chat_ids[SCOPE] == CHAT, "没把真 chat_id 补记上，下一次还会对不上"


def test_click_in_unrelated_group_after_restart_still_refused(tmp_path):
    """阳性对照：记的是另一个 chat（卡片在别的群里）仍然不猜 —— 别把昨晚修的安全问题改回去。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {SCOPE: "oc_some_other_chat"}
    b.fleet.overview()
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id="oc_group_x"))
    b._on_fleet_click(event, OWNER, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is None
    assert b._chat_ids[SCOPE] == "oc_some_other_chat"


# ---------- 重启后一条记录都没有：问飞书，别猜 ----------
#
# 🔴 2026-11-05 用户第二次报同一个形状：「之前点击某个会话的时候，现在没有了呢？」
# 真因是 `_chat_ids` 只在内存里，而这个桥重启是常态（看门狗自杀等拉起 + 每次上线，
# 当晚 4 次）⇒ 聊天记录里**所有**旧卡片一点都报「服务刚重启过，先发一句话」。
# 之前那条「记的就是点的人自己 ⇒ 认它」只救了「点过菜单」那一种；一条记录都没有时仍然拒。
# 现在有 `im:chat:read`（11-05 已开并发版）⇒ 直接问飞书这个 chat 是群还是单聊。


class _AskFeishu:
    """假的建群 client，只实现 `is_group`（真实那层是 `GroupSetup.is_group`）。"""

    def __init__(self, answer):
        self.answer = answer
        self.asked = []

    def is_group(self, chat_id):
        self.asked.append(chat_id)
        return self.answer


def test_cold_click_asks_feishu_and_accepts_a_dm(tmp_path):
    """内存里一条记录都没有（刚重启）+ 飞书说这是单聊 ⇒ 放行，并把 chat_id 记下来。"""
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {}  # 刚重启，什么都没记
    b.group_client = _AskFeishu(False)  # 飞书：这是单聊
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.group_client.asked == [CHAT], "没去问飞书"
    assert b.fleet.peek_pending(SCOPE) is not None, "飞书都说是单聊了还拒 ⇒ 旧卡片全死"
    assert b._chat_ids[SCOPE] == CHAT, "没补记，下一次还要再问一遍"


def test_cold_click_in_a_group_gets_a_group_scope(tmp_path):
    """飞书说这是群 ⇒ 走 group scope，**不许**记进 `dm:` 那一格。

    记错的后果是两头都错：他私聊发的下一句会被当成要转发给那个会话的话。
    """
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {}
    b.group_client = _AskFeishu(True)  # 飞书：这是群
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is None, "群里点的卡片不该落到单聊那一格"
    assert SCOPE not in b._chat_ids, f"把群 chat_id 写进 dm 那格了：{b._chat_ids}"


def test_cold_click_still_refuses_when_feishu_cannot_say(tmp_path):
    """阳性对照：飞书也答不出来（没权限 / 网络不通）⇒ **仍然不猜**，让他重新点一下。

    猜错比多点一次贵得多（把群当单聊 ⇒ 私聊的下一句被转发走）。
    """
    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {}
    b.group_client = _AskFeishu(None)  # 问不出来
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is None
    assert SCOPE not in b._chat_ids


def test_cold_click_survives_a_broken_group_client(tmp_path):
    """`is_group` 抛了也不能让点击崩掉 —— 退回「不猜」。"""
    class Boom:
        def is_group(self, _cid):
            raise RuntimeError("飞书炸了")

    claude = FakeAdapter("claude", [_sess("claude", CID)])
    b = _bridge(tmp_path, claude)
    b._chat_ids = {}
    b.group_client = Boom()
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(SCOPE) is None
