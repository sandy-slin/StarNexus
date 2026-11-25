"""第 5 轮（UX 侦察 Top 3/4/5）：点了要有看得见的回应、发出去之后看得到在等、不给点不动的按钮。

1. 服务重启后点旧的总控卡 / 确认卡：原来只闪一个 toast（一两秒就没了 = 「点了没反应」）
   ⇒ 现在还要在聊天里留一条带按钮的消息
2. 发进电脑上的会话之后最长 15 分钟没有任何动静 ⇒ 一张**原地刷新**的等待卡（只发一张，之后改它）；
   超时那句话不再叫人点一个不存在的「🔄 刷新」
3. 「上一条还在发」那句给的「⏹ 停下」：终端会话点了必然「停不了」⇒ 能停才放

夹具不放 tmp_path（macOS 上是 pathguard 豁免前缀），用测试目录旁边的临时目录。
"""

from __future__ import annotations

import inspect
import json
import os
import re
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import ActionResult  # noqa: E402
from bridge.fleet import relay as relay_mod  # noqa: E402

from tests.test_relay_integration import (  # noqa: E402
    CHAT,
    CID,
    OWNER,
    SCOPE,
    RelayClaude,
    _bridge,
    _buttons,
    _live,
    _run,
    _talk,
)


@pytest.fixture
def here_dir():
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".r5-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _ops(blob: str) -> list:
    return [b["behaviors"][0]["value"].get("op") for b in _buttons(json.loads(blob))]


def _labels(blob: str) -> list:
    out = []
    for b in _buttons(json.loads(blob)):
        text = b.get("text", {})
        out.append(text.get("content", "") if isinstance(text, dict) else str(text))
    return out


def _toast(resp) -> str:
    toast = getattr(resp, "toast", None)
    return f"{getattr(toast, 'type', '')} {getattr(toast, 'content', '')}"


def _event(chat=CHAT):
    return types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=chat))


# ---------- 1. 重启后点旧卡片：toast 之外要留一条带按钮的消息 ----------


@pytest.mark.parametrize("op", ["open", "refresh", "send", "stop"])
def test_stale_fleet_card_leaves_a_message_with_a_button(here_dir, op):
    """🔴 重启后 `_known` 是空的 ⇒ 点总览 / 详情卡上任何一下都落进「过期了」。
    原来只有 toast；现在聊天里还要多一条话 + 「回到总览」按钮（点它不需要 scope，重启后也点得动）。"""
    b = _bridge(here_dir, RelayClaude([_live()]))
    # 不 overview() = 刚重启，什么都没扫过
    resp = b._on_fleet_click(_event(), OWNER, {"act": "fleet", "op": op, "tool": "claude", "id": CID})
    assert "过期" in _toast(resp), "toast 本身别丢"
    assert b.replier.texts, "只闪了一个 toast，聊天里什么都没留下"
    assert "失效" in b.replier.texts[-1] or "过期" in b.replier.texts[-1]
    assert "sessions" in _ops(b.replier.cards[-1]), "留了话但没给下一步的按钮"
    assert b.fleet.peek_pending(SCOPE) is None, "过期的卡不许顺手记目标"


def test_stale_consent_card_leaves_a_message_with_a_button(here_dir):
    """确认卡那条路：重启后 `_known` 是空的，`resolve_relay_click` 先返回 None ——
    第 4 轮补的那条消息在它后面，根本走不到。"""
    b = _bridge(here_dir, RelayClaude([_live()]))
    resp = b._on_relay_click(_event(), OWNER, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    assert "过期" in _toast(resp)
    assert b.replier.texts and "sessions" in _ops(b.replier.cards[-1])
    assert b.fleet.peek_pending(SCOPE) is None


def test_stale_click_by_a_stranger_leaves_nothing(here_dir):
    """阳性对照：不是主人点的 ⇒ 照旧只拒，不往聊天里发任何东西。"""
    b = _bridge(here_dir, RelayClaude([_live()]))
    b._on_fleet_click(_event(), "ou_stranger", {"act": "fleet", "op": "open", "tool": "claude", "id": CID})
    assert b.replier.texts == [] and b.replier.cards == []


def test_fresh_click_does_not_get_the_stale_notice(here_dir):
    """阳性对照：扫过之后正常点 ⇒ 不许多出一条「失效了」。"""
    b = _bridge(here_dir, RelayClaude([_live()]))
    b.fleet.overview()
    b._on_fleet_click(_event(), OWNER, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert not any("失效" in t for t in b.replier.texts)


def test_unknown_chat_after_restart_says_what_to_do_in_the_chat(here_dir):
    """scope 对不上（重启后飞书也答不出是群还是单聊）：toast 之外留一句话，说清楚怎么办。"""
    b = _bridge(here_dir, RelayClaude([_live()]))
    b.fleet.overview()
    b._chat_ids = {}
    b.group_client = types.SimpleNamespace(is_group=lambda cid: None)
    resp = b._on_fleet_click(_event(), OWNER, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert "重启" in _toast(resp)
    assert b.replier.texts, "只闪了一个 toast"
    assert "发一句" in b.replier.texts[-1]
    assert b.fleet.peek_pending(SCOPE) is None and SCOPE not in b._chat_ids, "闸门不能因为补消息放松"


def test_callsite_stale_notice_is_wired_into_both_click_paths():
    """🔴 调用点还在：两条点击路径的「过期」分支里真调了 `_stale_card_notice(event, clicker)`。"""
    from bridge.app import Bridge

    for name in ("_on_fleet_click", "_on_relay_click"):
        src = inspect.getsource(getattr(Bridge, name))
        assert re.search(r"self\._stale_card_notice\(\s*event\s*,\s*clicker\s*\)", src), name
        assert re.search(r"if click is None:\s*\n(?:\s*#.*\n)*\s*self\._stale_card_notice\(", src), name


# ---------- 2. 发出去之后：一张原地刷新的等待卡 ----------


class _CardReplier:
    """带状态卡接口的假 replier：记下发了几张、改了几次、每次带不带停下。"""

    def __init__(self):
        self.texts: list = []
        self.cards: list = []
        self.status_sent: list = []  # (text, with_stop)
        self.status_patched: list = []  # (message_id, text, with_stop)

    def send(self, chat_id, text):
        from bridge.redact import redact_with_notice

        self.texts.append(redact_with_notice(text))
        return True

    def send_card(self, chat_id, card):
        self.cards.append(card)
        return True

    def send_status_card(self, chat_id, text, with_stop=True):
        self.status_sent.append((text, with_stop))
        return f"om_{len(self.status_sent)}"

    def patch_status_card(self, message_id, text, with_stop=True):
        self.status_patched.append((message_id, text, with_stop))
        return True


def _slow_bridge(here_dir, reply_after=0.4, result=None, **live_kw):
    import bridge.app as app_module

    claude = RelayClaude([_live(**live_kw)])

    def slow_relay(sid, text, timeout=120.0, mode=None):
        time.sleep(reply_after)
        return result or ActionResult(True, "好，改完了", sid, delivered=True)

    claude.relay = slow_relay
    b = _bridge(here_dir, claude)
    b.replier = _CardReplier()
    b.settings.progress_first_notice_sec = 0
    b.settings.progress_escalate_sec = 0
    b.fleet.overview()
    _talk(b)
    return b, app_module


def test_waiting_for_a_reply_shows_one_card_updated_in_place(here_dir, monkeypatch):
    """🔴 原来只有「📨 发给…了，等它回…」，之后最长 15 分钟什么都没有。
    现在：一张等待卡，**只发一张**，之后原地改（不刷屏）；回来后收尾成「它回了」。"""
    b, app_module = _slow_bridge(here_dir)
    monkeypatch.setattr(app_module, "_STATUS_REFRESH_SEC", 0.05)
    _run(b._handle(SCOPE, CHAT, "把测试跑一遍"))
    assert len(b.replier.status_sent) == 1, f"没有等待卡、或者每次刷新都发了一张新的：{b.replier.status_sent}"
    assert "等" in b.replier.status_sent[0][0]
    assert b.replier.status_patched, "等待卡发出去之后没再动过"
    assert {p[0] for p in b.replier.status_patched} == {"om_1"}, "改的不是同一张"
    assert "回了" in b.replier.status_patched[-1][1], "回复到了，卡还停在「还在等」"
    assert any("好，改完了" in t for t in b.replier.texts), "回复本身别丢"


def test_waiting_card_has_no_stop_button_for_a_terminal_session(here_dir, monkeypatch):
    """终端会话停不了 ⇒ 等待卡上也不许挂「停下」（Top 5 同一个形状）。"""
    b, app_module = _slow_bridge(here_dir)
    monkeypatch.setattr(app_module, "_STATUS_REFRESH_SEC", 0.05)
    _run(b._handle(SCOPE, CHAT, "把测试跑一遍"))
    assert b.replier.status_sent and b.replier.status_sent[0][1] is False
    assert all(p[2] is False for p in b.replier.status_patched)


def test_waiting_card_offers_stop_when_it_can_stop(here_dir, monkeypatch):
    """阳性对照：能停的会话，在等的时候卡上有「停下」，收尾那次去掉。"""
    b, app_module = _slow_bridge(here_dir, can_stop=True)
    monkeypatch.setattr(app_module, "_STATUS_REFRESH_SEC", 0.05)
    _run(b._handle(SCOPE, CHAT, "把测试跑一遍"))
    assert b.replier.status_sent[0][1] is True
    assert b.replier.status_patched[-1][2] is False, "已经回了还挂着「停下」"


def test_quick_reply_sends_no_waiting_card(here_dir):
    """几秒就回的不发等待卡（噪音）—— 第一张要等 progress_first_notice_sec。"""
    b, _ = _slow_bridge(here_dir, reply_after=0)
    b.settings.progress_first_notice_sec = 30
    _run(b._handle(SCOPE, CHAT, "在吗"))
    assert b.replier.status_sent == []


def test_waiting_card_closes_honestly_on_timeout(here_dir, monkeypatch):
    """超时 ⇒ 卡收尾成「没等到」，不许写成「回了」。"""
    timeout = ActionResult(False, relay_mod.TEXT_TIMEOUT, CID, delivered=True)
    b, app_module = _slow_bridge(here_dir, result=timeout)
    monkeypatch.setattr(app_module, "_STATUS_REFRESH_SEC", 0.05)
    _run(b._handle(SCOPE, CHAT, "把测试跑一遍"))
    last = b.replier.status_patched[-1][1]
    assert "回了" not in last and "没等到" in last, last


def test_waiting_card_never_breaks_the_send(here_dir, monkeypatch):
    """状态卡接口炸了 ⇒ 回复照样贴回来。"""
    b, app_module = _slow_bridge(here_dir)
    monkeypatch.setattr(app_module, "_STATUS_REFRESH_SEC", 0.05)

    def boom(*a, **kw):
        raise RuntimeError("飞书炸了")

    b.replier.send_status_card = boom
    _run(b._handle(SCOPE, CHAT, "把测试跑一遍"))
    assert any("好，改完了" in t for t in b.replier.texts)


def test_callsite_waiting_card_is_wired_into_the_relay_path():
    """🔴 调用点还在：`_maybe_send_to_fleet` 里真建了等待卡、真在发的前后起和收。"""
    from bridge.app import Bridge

    src = inspect.getsource(Bridge._maybe_send_to_fleet)
    assert re.search(r"_FleetWaitCard\(\s*self\.replier\s*,\s*chat_id", src)
    assert re.search(r"asyncio\.create_task\(\s*wait_card\.run\(", src)
    assert re.search(r"await\s+wait_card\.finish\(", src)


# ---------- 2b. 超时那句：说到的按钮都要真有 ----------


def test_timeout_text_points_only_at_buttons_that_exist(here_dir):
    """🔴 原来超时那句叫人「点 🔄 刷新」，下面按钮只有「看看它现在」。"""
    claude = RelayClaude([_live()])
    claude.relay = lambda sid, text, timeout=120.0, mode=None: ActionResult(
        False, relay_mod.TEXT_TIMEOUT, sid, delivered=True)
    b = _bridge(here_dir, claude)
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "跑全量测试"))
    msg, card = b.replier.texts[-1], b.replier.cards[-1]
    assert "🔄" not in msg and "刷新" not in msg, msg
    labels = _labels(card)
    for quoted in re.findall(r"点[^「」]{0,4}「([^」]+)」", msg):
        assert any(quoted in label for label in labels), f"文案叫人点「{quoted}」，按钮里没有：{labels}"
    assert "不会自动重发" in msg, "这句是防重复执行的，不能删"
    assert "不会再推" in msg, "超时后不补推 ⇒ 要照实说，不然他会一直等"


def test_timeout_constant_has_no_refresh_button_reference():
    assert "刷新" not in relay_mod.TEXT_TIMEOUT


# ---------- 3. 「上一条还在发」：能停才放「停下」 ----------


def test_in_flight_notice_has_no_stop_for_a_terminal_session(here_dir):
    """🔴 终端会话点「⏹ 停下」必然是「停不了」⇒ 别给。"""
    b = _bridge(here_dir, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    b._fleet_inflight[SCOPE] = ("claude", CID)
    _run(b._handle(SCOPE, CHAT, "再来一句"))
    assert "上一条还在发" in b.replier.texts[-1]
    ops = _ops(b.replier.cards[-1])
    assert "stop" not in ops, f"给了一个点了必然停不了的按钮：{ops}"
    assert "quit" in ops


def test_in_flight_notice_keeps_stop_when_it_can_stop(here_dir):
    """阳性对照：能停的会话照样有「停下」。"""
    b = _bridge(here_dir, RelayClaude([_live(can_stop=True)]))
    b.fleet.overview()
    _talk(b)
    b._fleet_inflight[SCOPE] = ("claude", CID)
    _run(b._handle(SCOPE, CHAT, "再来一句"))
    assert "stop" in _ops(b.replier.cards[-1])


def test_callsite_in_flight_notice_uses_talk_buttons():
    """🔴 调用点还在：在飞那句的按钮来自 `_talk_buttons(busy_tool, busy_id)`（按在飞的那个会话判）。"""
    from bridge.app import Bridge

    src = inspect.getsource(Bridge._maybe_send_to_fleet)
    at = src.index('"上一条还在发，')
    block = src[at:at + 300]
    assert re.search(r"self\._talk_buttons\(\s*busy_tool\s*,\s*busy_id\s*\)", block), block
    assert 'do_button("stop")' not in src
