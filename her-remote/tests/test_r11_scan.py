"""第 11 轮角色 D：后台「等你」扫描和主人点「会话」撞车 + 单聊对话模式中重启。

复现原文在 `docs/iteration/review-r9.md` 第 5、6 条。

## 第 6 条（撞车）
后台每 10 秒 `FleetControl.overview()`，和主人点「会话」共用 `Fleet._busy`（每家一个）。
撞上时那一家被「上一次还没读完，这次先跳过」⇒ 总览卡标题写「电脑上现在没有开着的会话」，而实际有一个在等你。
- 修法 1（撞车本身）：`notify.SharedScan` —— 前台撞上后台那一次扫描时**等它扫完、用同一份结果**
- 修法 2（底线）：这次有哪家没读到 ⇒ 总览**不许说「没有」**；那一家上次读到过的会话照样列出来并注明是之前的

## 第 5 条（重启）
重启前在对话模式（单聊）⇒ 落一个小标记（`talk_mode.json`）；重启后第一句先问「这句要发给它吗？」+ 两个按钮，
**不自动转发**。点「发给它」⇒ 现查会话 + 按现在的样子查确认记录，过了才走正常管道；不过 ⇒ 弹确认卡。

夹具放测试目录旁边（`here_dir` 同 `tests/test_groupsetup.py`），不放 tmp_path。
"""

from __future__ import annotations

import inspect
import json
import os
import random
import re
import sys
import threading
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from test_chatbind import (  # noqa: E402
    CID,
    DM,
    DM_CHAT,
    OWNER,
    FakeClaude,
    _bridge,
    _click,
    _run,
    _sess,
)

from bridge.fleet.cards import overview_headline  # noqa: E402
from bridge.fleet.notify import WaitingNotifier  # noqa: E402

NONE_LINE = overview_headline([])  # 「电脑上现在没有开着的会话。」—— 按 cards 现在的写法取，不抄字面量
EMPTY_LINE = "一个会话都没读到。"


@pytest.fixture
def here_dir():
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".r11-scan-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ====================================================================
# 第 6 条：撞车
# ====================================================================


class SlowClaude(FakeClaude):
    """`list()` 睡一会儿（本机实测三家各 50~270ms）。`gate` 给了就等它放行（确定性撞车用）。"""

    def __init__(self, sessions=(), delay=0.0):
        super().__init__(sessions)
        self.delay = delay
        self.gate = None
        self.calls = 0
        self._lock = threading.Lock()

    def list(self, limit=20):
        with self._lock:
            self.calls += 1
        if self.gate is not None:
            self.gate.wait(5)
        if self.delay:
            time.sleep(self.delay)
        return self.sessions[:limit]


def _waiting():
    return _sess(status="waiting", owner="terminal")


def _scan_bridge(here, adapter, timeout=1.0):
    from bridge.fleet.control import FleetControl
    from bridge.fleet.scan import Fleet

    b = _bridge(here, adapter, None)
    b.fleet = FleetControl(Fleet({adapter.tool: adapter}, timeout=timeout))
    # 人在电脑前（空闲 0 秒）⇒ 后台那一拍照扫、但不推卡：这里只关心扫描撞车
    b.waiting_notifier = WaitingNotifier(enabled=True, idle=lambda: 0.0)
    b.settings.notify_waiting = True
    return b


def _overview_cards(b):
    return [c for c in b.replier.cards_to(DM_CHAT) if "电脑上的会话" in c]


def _card_text(card: str) -> str:
    data = json.loads(card)
    return " | ".join(e.get("content", "") for e in data["body"]["elements"] if e.get("tag") == "markdown")


def test_r9_6_deterministic_collision_card_does_not_say_none(here_dir):
    """复审第 6 条「另跑一次确定性撞车看卡面」：后台那一拍正扫着 Claude，主人点「会话」。"""
    claude = SlowClaude([_waiting()])
    b = _scan_bridge(here_dir, claude)
    claude.gate = threading.Event()
    bg = threading.Thread(target=b._notify_tick)
    bg.start()
    deadline = time.monotonic() + 3
    while claude.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert claude.calls == 1, "后台那一拍没开始扫"
    # 0.2 秒后后台那一次扫完（前台这时已经点下去了）
    threading.Timer(0.2, claude.gate.set).start()
    _run(b._fleet_overview(DM_CHAT))
    bg.join(5)
    cards = _overview_cards(b)
    assert len(cards) == 1
    text = _card_text(cards[0])
    assert NONE_LINE not in text and EMPTY_LINE not in text, text
    assert "在等你" in text, text
    assert "上一次还没读完" not in text, text


def test_r9_6_concurrent_background_scan_vs_n_clicks_never_says_none(here_dir):
    """后台在扫的同时前台连点 N 次 ⇒ 0 次说「没有」。

    复审原样是「后台每 10 秒一拍、前台随机隔 0.5~5.5 秒点 20 次」，要跑一分多钟、20 次只撞 0~2 次。
    这里把两边都压紧（后台一拍接一拍、前台隔 0~40ms），每一次点都几乎必撞 —— 现码上这条红得很稳。
    """
    claude = SlowClaude([_waiting()], delay=0.05)
    b = _scan_bridge(here_dir, claude)
    stop = threading.Event()

    def background():
        while not stop.is_set():
            b._notify_tick()

    bg = threading.Thread(target=background, daemon=True)
    bg.start()
    rng = random.Random(11)
    n = 20
    try:
        for _ in range(n):
            time.sleep(rng.uniform(0, 0.04))
            _run(b._fleet_overview(DM_CHAT))
    finally:
        stop.set()
        bg.join(5)
    cards = _overview_cards(b)
    assert len(cards) == n
    said_none = [c for c in cards if NONE_LINE in _card_text(c) or EMPTY_LINE in _card_text(c)]
    assert said_none == [], f"{len(said_none)}/{n} 次在有会话的时候说了「没有」"
    assert all("在等你" in _card_text(c) for c in cards)


def test_status_line_also_goes_through_the_shared_scan(here_dir):
    """`/status` 的「电脑上在跑 N 个」原来直接调 `Fleet.running_count` ⇒ 撞上后台那一拍说「在跑 0 个」。"""
    claude = SlowClaude([_sess(status="running")])
    b = _scan_bridge(here_dir, claude)
    b.sessions = types.SimpleNamespace(get=lambda s: types.SimpleNamespace(workspace=str(here_dir)))
    b.connection_alive = lambda: True
    b._last_event_at = time.monotonic()
    claude.gate = threading.Event()
    bg = threading.Thread(target=b._notify_tick)
    bg.start()
    deadline = time.monotonic() + 3
    while claude.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    threading.Timer(0.2, claude.gate.set).start()
    _run(b._handle_command(DM, DM_CHAT, "/status"))
    bg.join(5)
    status = [t for t in b.replier.to(DM_CHAT) if "连着飞书" in t]
    assert status and "电脑上在跑 1 个会话" in status[-1], status


class HangOnce(SlowClaude):
    """第 `hang_on` 次 `list()` 卡住，直到测试放开（真「慢」，不是撞车）。"""

    def __init__(self, sessions=(), hang_on=1):
        super().__init__(sessions)
        self.hang_on = hang_on
        self.release = threading.Event()

    def list(self, limit=20):
        with self._lock:
            self.calls += 1
            mine = self.calls
        if mine == self.hang_on:
            self.release.wait(10)
        return self.sessions[:limit]


def test_slow_tool_without_any_earlier_read_is_not_reported_as_none(here_dir):
    """底线：这一家这次没读到、之前也没读到过 ⇒ 不知道有没有，不许说「没有」。"""
    claude = HangOnce([_waiting()], hang_on=1)
    b = _scan_bridge(here_dir, claude, timeout=0.2)
    try:
        _run(b._fleet_overview(DM_CHAT))          # 读得太慢，没等它
        _run(b._fleet_overview(DM_CHAT))          # 上一次还卡着 ⇒ 跳过
    finally:
        claude.release.set()
    cards = _overview_cards(b)
    assert len(cards) == 2
    for card in cards:
        text = _card_text(card)
        assert NONE_LINE not in text and EMPTY_LINE not in text, text
        assert "Claude 这次没读出来" in text, text
    fallback_free = [t for t in b.replier.to(DM_CHAT) if EMPTY_LINE in t]
    assert fallback_free == []


def test_slow_tool_shows_what_it_had_last_time_and_says_so(here_dir):
    """之前读到过 ⇒ 列出上次那几行，并注明是之前的样子（不说「没有」）。"""
    claude = HangOnce([_waiting()], hang_on=2)
    b = _scan_bridge(here_dir, claude, timeout=0.2)
    try:
        _run(b._fleet_overview(DM_CHAT))          # 第 1 次：读到了
        _run(b._fleet_overview(DM_CHAT))          # 第 2 次：卡住
        _run(b._fleet_overview(DM_CHAT))          # 第 3 次：上一次还卡着
    finally:
        claude.release.set()
    cards = _overview_cards(b)
    assert len(cards) == 3
    for card in cards[1:]:
        text = _card_text(card)
        assert NONE_LINE not in text, text
        assert "在等你" in text, text
        assert "之前" in text and "前的样子" in text, text


def test_positive_control_really_no_sessions_still_says_none(here_dir):
    """阳性对照：三家都按时读完、真的没有开着的 ⇒ 照样说「没有」。"""
    b = _scan_bridge(here_dir, SlowClaude([], delay=0.01))
    _run(b._fleet_overview(DM_CHAT))
    text = _card_text(_overview_cards(b)[-1])
    assert NONE_LINE in text and EMPTY_LINE in text, text
    # 只有闲着的会话（没进程）也算「没有开着的」
    b2 = _scan_bridge(here_dir, SlowClaude([_sess(status="idle", pids=(), owner="none")]))
    _run(b2._fleet_overview(DM_CHAT))
    assert NONE_LINE in _card_text(_overview_cards(b2)[-1])


def test_positive_control_background_tick_still_sees_raw_scan(here_dir):
    """后台推送那边拿到的是**原样**的扫描结果（不掺上次的缓存），不然一次跳过会被当成「还在等」。"""
    from bridge.fleet.notify import SharedScan

    claude = HangOnce([_waiting()], hang_on=2)
    b = _scan_bridge(here_dir, claude, timeout=0.2)
    scanner = SharedScan(b.fleet)
    try:
        first = scanner.overview()
        second = scanner.overview()
    finally:
        claude.release.set()
    assert first.ok_tools == ["claude"] and [s.id for s in first.sessions] == [CID]
    assert second.ok_tools == [] and second.sessions == []


def test_shared_scan_runs_one_scan_for_concurrent_callers(here_dir):
    """同一时刻两个调用方 ⇒ 只扫一次、拿同一份结果。"""
    from bridge.fleet.notify import SharedScan

    claude = SlowClaude([_waiting()])
    claude.gate = threading.Event()
    b = _scan_bridge(here_dir, claude)
    scanner = SharedScan(b.fleet)
    got = []
    first = threading.Thread(target=lambda: got.append(scanner.overview()))
    first.start()
    deadline = time.monotonic() + 3
    while claude.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.005)
    second = threading.Thread(target=lambda: got.append(scanner.overview()))
    second.start()
    time.sleep(0.05)
    claude.gate.set()
    first.join(5)
    second.join(5)
    assert claude.calls == 1
    assert len(got) == 2 and got[0] is got[1]
    # 扫完之后再来一个 ⇒ 重新扫（不是永远吃第一份）
    scanner.overview()
    assert claude.calls == 2


def test_shared_scan_passes_errors_through_and_recovers(here_dir):
    from bridge.fleet.notify import SharedScan

    class Exploding:
        fleet = None

        def __init__(self):
            self.n = 0

        def overview(self):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("扫描整个炸了")
            return types.SimpleNamespace(ok_tools=[], sessions=[], notes=[], at=time.time())

    scanner = SharedScan(Exploding())
    with pytest.raises(RuntimeError):
        scanner.overview()
    assert scanner.overview().sessions == []        # 下一次照常扫，没卡在上一次的 Future 上


# ---------- 调用点还在 ----------


def test_call_site_overview_and_tick_and_status_use_the_shared_scanner():
    from bridge.app import Bridge

    for fn in (Bridge._fleet_overview, Bridge._notify_tick):
        src = inspect.getsource(fn)
        assert re.search(r"self\._fleet_scanner\(\)", src), fn.__name__
        assert not re.search(r"control\.overview\b", src), f"{fn.__name__} 还在直接调 control.overview"
    src = inspect.getsource(Bridge._handle_command)
    assert re.search(r"self\._fleet_scanner\(\)", src)
    assert "fleet.running_count" not in src
    src = inspect.getsource(Bridge._fleet_overview)
    assert re.search(r"fill_unread\(\s*view\s*,", src)
    assert re.search(r"self\._honest_overview_card\(", src)


def test_call_site_scanner_is_built_from_shared_scan():
    from bridge.app import Bridge

    src = inspect.getsource(Bridge._fleet_scanner)
    assert re.search(r"SharedScan\(\s*control\s*\)", src)


# ====================================================================
# 第 5 条：单聊对话模式中重启
# ====================================================================

RELAY_TALK = {"act": "relay", "op": "talk", "tool": "claude", "id": CID}
FLEET_SEND = {"act": "fleet", "op": "send", "tool": "claude", "id": CID}


def _dm_bridge(here, claude):
    b = _bridge(here, claude, None)
    b.group_client = None
    b._talk_mark_path = os.path.join(str(here), "talk_mode.json")  # Bridge.__init__ 里同一行（见调用点测试）
    b.fleet.overview()
    return b


def _restart(here, claude):
    """同一个家目录再起一个：内存态全空、落盘态读回；后台扫描已跑过一拍。"""
    return _dm_bridge(here, claude)


def _into_talk_mode(b):
    _click(b, FLEET_SEND)
    _click(b, RELAY_TALK)
    _run(b._handle(DM, DM_CHAT, "第一句"))


def _offer_values(b):
    out = []
    for card in b.replier.cards_to(DM_CHAT):
        for m in re.finditer(r'"value":\s*(\{[^{}]*\})', card):
            value = json.loads(m.group(1))
            if value.get("act") == "rtalk":
                out.append(value)
    return out


def _card_click(b, value, chat_id=DM_CHAT, clicker=OWNER):
    """走真入口 `on_card_action`（顺带验分派那一行）。"""
    event = types.SimpleNamespace(
        context=types.SimpleNamespace(open_chat_id=chat_id),
        operator=types.SimpleNamespace(open_id=clicker),
        action=types.SimpleNamespace(value=value),
    )
    if not hasattr(b, "_last_event_at"):
        b._last_event_at = time.monotonic()
    return b.on_card_action(types.SimpleNamespace(event=event))


def _offer(b, op):
    vals = [v for v in _offer_values(b) if v["op"] == op]
    assert vals, "没弹「这句要发给它吗」那两个按钮"
    return vals[-1]


def test_r9_5_restart_in_talk_mode_asks_before_anything(here_dir):
    """复审第 5 条原样：重启后下一句原来悄悄落进桥自己的 claude，回他的话 = []。"""
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    assert claude.relayed == [(CID, "第一句")]
    tail = b.replier.to(DM_CHAT)[-1]
    assert "你发的每一句都进它那边" in tail

    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    # 🔴 不自动转发，也不悄悄交给桥自己的 claude
    assert claude.relayed == [(CID, "第一句")]
    assert claude.sent == []
    assert b2.handled == []
    said = b2.replier.to(DM_CHAT)
    assert any("刚才服务重启过" in t and "修登录" in t and "这句要发给它吗" in t for t in said), said
    assert {v["op"] for v in _offer_values(b2)} == {"send", "no"}
    # 按钮上只有一个序号，不带会话 / 原话（那些在服务端记着，卡片值伪造不了）
    assert all(set(v) == {"act", "op", "n"} for v in _offer_values(b2))


def test_restart_offer_send_goes_through_recheck_and_normal_pipeline(here_dir):
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    _card_click(b2, _offer(b2, "send"))
    assert claude.relayed == [(CID, "第一句"), (CID, "那你把它改了")]
    assert b2.handled == []
    # 之后又回到对话模式：下一句照样进它那边
    _run(b2._handle(DM, DM_CHAT, "再看看"))
    assert claude.relayed[-1] == (CID, "再看看")


def test_restart_offer_send_re_asks_consent_when_the_session_changed(here_dir):
    """重启后它在电脑上改了名 ⇒ 旧确认不算数，弹确认卡（引用这句），不发。"""
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    claude.sessions = [_sess(title="另一个活")]
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "删掉旧表"))
    _card_click(b2, _offer(b2, "send"))
    assert claude.relayed == [(CID, "第一句")]
    assert b2.handled == []
    relay_cards = [c for c in b2.replier.cards_to(DM_CHAT) if '"act": "relay"' in c]
    assert relay_cards and "删掉旧表" in relay_cards[-1]
    # 点确认 ⇒ 卡上引用的那句才发出去（走现成的确认后补发）
    _click(b2, RELAY_TALK)
    assert claude.relayed[-1] == (CID, "删掉旧表")


def test_restart_offer_send_when_session_is_gone_says_so(here_dir):
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    claude.sessions = []
    _card_click(b2, _offer(b2, "send"))
    assert claude.relayed == [(CID, "第一句")] and b2.handled == []
    assert any("这句没发" in t for t in b2.replier.to(DM_CHAT))


def test_restart_offer_no_hands_the_sentence_to_the_bridge(here_dir):
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    _card_click(b2, _offer(b2, "no"))
    assert b2.handled == [(DM, "那你把它改了")]
    assert claude.relayed == [(CID, "第一句")]


def test_restart_offer_is_one_shot_and_old_buttons_die(here_dir):
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    send = _offer(b2, "send")
    # 第二句：已经问过了 ⇒ 按平常处理（屏幕上那句已经说清对话断了）
    _run(b2._handle(DM, DM_CHAT, "算了你来看"))
    assert b2.handled == [(DM, "算了你来看")]
    before = list(claude.relayed)
    # 伪造的序号 / 多带一个键（想夹带发给谁）⇒ 不认，而且不吃掉真按钮
    _card_click(b2, dict(send, n="000000000000"))
    _card_click(b2, dict(send, tool="claude", id=CID))
    assert claude.relayed == before and b2.handled == [(DM, "算了你来看")]
    _card_click(b2, send)
    _card_click(b2, send)                              # 同一个按钮点第二下：失效
    assert claude.relayed.count((CID, "那你把它改了")) == 1


def test_restart_offer_rejects_non_owner(here_dir):
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    _card_click(b2, _offer(b2, "send"), clicker="ou_stranger")
    assert claude.relayed == [(CID, "第一句")]
    # 主人那一道闸单独验：就算认成了主人的聊天（`_click_scope` 被绕过），不是主人照样拒
    real_scope = b2._click_scope
    b2._click_scope = lambda event, clicker: (DM_CHAT, DM)
    try:
        _card_click(b2, _offer(b2, "send"), clicker="ou_stranger")
    finally:
        b2._click_scope = real_scope
    assert claude.relayed == [(CID, "第一句")]
    _card_click(b2, _offer(b2, "send"))                # 主人点：这个按钮还在
    assert claude.relayed[-1] == (CID, "那你把它改了")


def test_positive_control_no_talk_mode_before_restart_means_no_notice(here_dir):
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _run(b._handle(DM, DM_CHAT, "你好"))
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    assert b2.handled == [(DM, "那你把它改了")]
    assert not any("服务重启过" in t for t in b2.replier.to(DM_CHAT))
    assert _offer_values(b2) == []


def test_positive_control_quit_before_restart_means_no_notice(here_dir):
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    _run(b._handle(DM, DM_CHAT, "退出对话"))
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    assert b2.handled == [(DM, "那你把它改了")]
    assert _offer_values(b2) == []


def test_positive_control_same_process_never_asks(here_dir):
    """不重启：对话模式里照常发，一句「重启过」都不说。"""
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    _run(b._handle(DM, DM_CHAT, "第二句"))
    assert claude.relayed == [(CID, "第一句"), (CID, "第二句")]
    assert not any("服务重启过" in t for t in b.replier.to(DM_CHAT))


def test_restart_before_any_sentence_still_asks(here_dir):
    """刚点完确认（屏幕上写着「直接打字发过来就行」）就重启了，一句还没说。"""
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _click(b, FLEET_SEND)
    _click(b, RELAY_TALK)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "开始吧"))
    assert claude.relayed == [] and b2.handled == []
    assert _offer_values(b2)


def test_quit_button_after_restart_clears_the_marker(here_dir):
    """重启后先点旧的「退出对话」⇒ 说退出了；下一句不再问。"""
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "/cancel"))
    assert any("退出了" in t and "修登录" in t for t in b2.replier.to(DM_CHAT)), b2.replier.to(DM_CHAT)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    assert b2.handled == [(DM, "那你把它改了")] and _offer_values(b2) == []


def test_expired_talk_mode_before_restart_says_so_and_handles_normally(here_dir):
    """重启前那次「接着聊」本来就过了 5 分钟 ⇒ 同不重启时那条路：说一声，按平常处理。"""
    claude = FakeClaude([_sess()])
    b = _dm_bridge(here_dir, claude)
    _into_talk_mode(b)
    path = b._talk_mark_path
    with open(path, encoding="utf-8") as handle:
        marks = json.load(handle)
    marks[DM]["expires_at"] = time.time() - 60
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(marks, handle)
    b2 = _restart(here_dir, claude)
    _run(b2._handle(DM, DM_CHAT, "那你把它改了"))
    assert b2.handled == [(DM, "那你把它改了")]
    assert any("服务重启过" in t and "5 分钟" in t for t in b2.replier.to(DM_CHAT))
    assert _offer_values(b2) == []


def test_marker_never_names_a_group_and_survives_garbage(here_dir):
    claude = FakeClaude([_sess()])
    path = os.path.join(str(here_dir), "talk_mode.json")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{半截")
    b = _dm_bridge(here_dir, claude)
    _run(b._handle(DM, DM_CHAT, "你好"))           # 坏文件 ⇒ 当没有，不崩
    assert b.handled == [(DM, "你好")]
    _into_talk_mode(b)
    with open(path, encoding="utf-8") as handle:
        marks = json.load(handle)
    assert list(marks) == [DM]
    assert marks[DM]["tool"] == "claude" and marks[DM]["id"] == CID


# ---------- 调用点还在 ----------


def test_call_site_init_sets_the_marker_path():
    from bridge.app import Bridge

    src = inspect.getsource(Bridge.__init__)
    assert re.search(r'self\._talk_mark_path\s*=\s*os\.path\.join\(\s*home\s*,\s*"talk_mode\.json"\s*\)', src)


def test_call_site_offer_and_sync_and_click_dispatch():
    from bridge.app import Bridge

    src = inspect.getsource(Bridge._maybe_send_to_fleet)
    assert re.search(r"self\._restart_talk_offer\(\s*scope\s*,\s*chat_id\s*,\s*text\s*\)", src)
    src = inspect.getsource(Bridge._handle)
    assert re.search(r"finally:\s*\n\s*self\._sync_talk_marker\(\s*scope\s*\)", src)
    src = inspect.getsource(Bridge.on_card_action)
    assert re.search(r'value\.get\("act"\)\s*==\s*"rtalk"', src)
    assert re.search(r"return self\._on_restart_talk_click\(\s*event\s*,\s*clicker\s*,\s*value\s*\)", src)
    for fn in (Bridge._on_fleet_click, Bridge._on_relay_click, Bridge._open_session_group):
        assert re.search(r"self\._sync_talk_marker\(\s*scope\s*\)", inspect.getsource(fn)), fn.__name__
    src = inspect.getsource(Bridge._handle_command)
    assert re.search(r"self\._forget_restart_talk\(\s*scope\s*\)", src)
