"""第 34 轮 WATCHDOG：长连接卡死从 15 分钟发现缩到 2~3 分钟，而且不误杀。

两条快判据（`Bridge.stuck_reason()`，原理在 `bridge/wsfast.py` 第 34 轮那段）：
  ① SDK 的事件循环卡住 > `WATCHDOG_LOOP_STALL_SEC`（lark-oapi #169 的 `requests.post` 是同步的，
     卡住它 = 整个循环停摆）。
  ② 当前连接 `pong_limit()` 秒没收到飞书 pong（只在这条连接上真收到过 pong 才启用）。
不误杀的三条：空闲（判据不看业务事件）/ 睡醒（monotonic + 跳变重新计时）/ 有任务在跑（快判据不动手）。
15 分钟静默兜底保留。

全部用假对象 + 本进程里的 asyncio 循环；不碰主人正在跑的 bridge、不连飞书。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from bridge import app as app_mod  # noqa: E402
from bridge import wsfast  # noqa: E402
from bridge.app import (  # noqa: E402
    WATCHDOG_LOOP_STALL_SEC,
    WATCHDOG_SILENT_SEC,
    WATCHDOG_SLEEP_JUMP_SEC,
    WATCHDOG_TICK_SEC,
)
from test_interference import _fake_bridge  # noqa: E402


# ---------- 假对象 ----------


class _Conn:
    def __init__(self, state=1):
        self.state = state


def _client(conn=None, ping_interval=90, pong_ago=None):
    c = types.SimpleNamespace(_conn=conn if conn is not None else _Conn(), _ping_interval=ping_interval)
    if pong_ago is not None:
        c._r34_pong_at = time.monotonic() - pong_ago
        c._r34_pong_conn = c._conn
    return c


def _bridge(tmp, client=None, stall=None):
    b = _fake_bridge(tmp)
    b._ws_client = client
    b._loop_stall = stall
    b._last_event_wall = time.time()
    b._last_event_at = time.monotonic()
    return b


def _busy(b):
    session = b.sessions.get("scope-busy")
    asyncio.run(session.lock.acquire())


def _pong_frame(kind="pong"):
    from lark_oapi.ws.pb.pbbp2_pb2 import Frame

    f = Frame()
    h = f.headers.add()
    h.key, h.value = "type", kind
    f.method = 0  # CONTROL
    return f


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# ---------- 1. pong 记录（真 SDK 子类） ----------


def test_real_sdk_subclass_records_pong_on_current_connection():
    """真 SDK 1.7.5 的 `_handle_control_frame` 被子类接住：收到 pong ⇒ 记时间 + 记连接。"""
    c = wsfast.make_client("cli_x", "s", event_handler=None)
    c._conn = object()
    assert wsfast.pong_age(c) is None, "没收到过 pong ⇒ 不知道，不是死"
    asyncio.run(c._handle_control_frame(_pong_frame()))
    age = wsfast.pong_age(c)
    assert age is not None and age < 5


def test_ping_frame_is_not_a_pong():
    c = wsfast.make_client("cli_x", "s", event_handler=None)
    c._conn = object()
    asyncio.run(c._handle_control_frame(_pong_frame("ping")))
    assert wsfast.pong_age(c) is None


def test_pong_from_previous_connection_does_not_count():
    """重连之后旧连接的 pong 不算数 —— 否则新连接一上来就被判「200 秒没 pong」。"""
    c = _client(pong_ago=10)
    c._conn = _Conn()  # 换了一条新连接
    assert wsfast.pong_age(c) is None


def test_pong_limit_tracks_server_ping_interval():
    """2026-11-10 实测服务端下发 PingInterval=90 ⇒ 2×90+30 = 210 秒。"""
    assert wsfast.pong_limit(_client(ping_interval=90)) == 210
    assert wsfast.pong_limit(_client(ping_interval=120)) == 270
    assert wsfast.pong_limit(_client(ping_interval=5)) == wsfast.PONG_MIN_SEC, "下限挡住网络抖动"
    assert wsfast.pong_limit(types.SimpleNamespace()) == 270, "拿不到间隔 ⇒ 按 SDK 默认 120"


def test_sdk_loop_is_the_module_level_loop():
    """SDK 换版本、模块级 loop 没了 ⇒ 这条要红（探针会变成永远「不知道」，退回 15 分钟）。"""
    import lark_oapi.ws.client as ws_client_mod

    assert wsfast.sdk_loop() is ws_client_mod.loop


# ---------- 2. 循环探针（真 asyncio 循环） ----------


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    while not loop.is_running():
        time.sleep(0.01)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(2)
    loop.close()


def test_probe_healthy_loop_reports_zero(running_loop):
    clock = _Clock()
    p = wsfast.LoopProbe(lambda: running_loop, clock)
    for _ in range(5):
        assert p.tick() == 0.0
        time.sleep(0.05)  # 让循环执行回调
        clock.t += WATCHDOG_TICK_SEC


def test_probe_detects_loop_blocked_by_sync_call(running_loop):
    """#169 的形状：循环里有个同步调用卡住（这里用 Event.wait 模拟无超时的 requests.post）。"""
    release = threading.Event()
    running_loop.call_soon_threadsafe(release.wait, 10)
    clock = _Clock()
    p = wsfast.LoopProbe(lambda: running_loop, clock)
    assert p.tick() == 0.0
    clock.t += 60
    assert p.tick() == 60
    clock.t += 90
    assert p.tick() == 150
    release.set()  # 卡住的调用返回 ⇒ 循环恢复
    time.sleep(0.1)
    clock.t += 15
    assert p.tick() == 0.0, "恢复后要重新计时"


def test_probe_unknown_when_loop_not_running():
    """长连接线程退出（被明确拒绝）⇒ 循环不转 ⇒ 不知道，交给静默兜底，别误判卡死。"""
    loop = asyncio.new_event_loop()
    try:
        assert wsfast.LoopProbe(lambda: loop).tick() is None
    finally:
        loop.close()
    assert wsfast.LoopProbe(lambda: None).tick() is None
    assert wsfast.LoopProbe(lambda: "not a loop").tick() is None


def test_probe_reset_drops_pending(running_loop):
    release = threading.Event()
    running_loop.call_soon_threadsafe(release.wait, 10)
    clock = _Clock()
    p = wsfast.LoopProbe(lambda: running_loop, clock)
    p.tick()
    clock.t += 300
    p.reset()
    release.set()
    time.sleep(0.05)
    assert p.tick() == 0.0


# ---------- 3. 快判据：会更早触发 ----------


def test_stalled_sdk_loop_exits_in_minutes_not_fifteen():
    """连接对象已经没了（#169：断线后卡在重连）+ 循环卡 135 秒 ⇒ 现在就退，不等 15 分钟。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(conn=types.SimpleNamespace(state=3)), stall=WATCHDOG_LOOP_STALL_SEC + 15)
        b._last_event_at = time.monotonic() - 60       # 用户 1 分钟前刚说过话
        b._last_event_wall = time.time() - 60
        assert b.should_self_exit() is True
        assert "事件循环" in b._watchdog_reason


def test_short_stall_is_tolerated():
    """一次慢的握手（本机日志最长 92 秒）不许判死。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(), stall=92)
        assert b.should_self_exit() is False


def test_pong_stopped_on_open_connection_exits():
    """连接看着是 OPEN，但 210 秒没收到飞书 pong ⇒ 判死。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=250))
        assert b.should_self_exit() is True
        assert "心跳回包" in b._watchdog_reason


def test_pong_resumes_then_alive():
    """pong 恢复 ⇒ 立刻不再判死。"""
    with tempfile.TemporaryDirectory() as tmp:
        c = _client(pong_ago=250)
        b = _bridge(tmp, c)
        assert b.should_self_exit() is True
        c._r34_pong_at = time.monotonic()
        assert b.should_self_exit() is False


def test_recent_pong_is_alive():
    with tempfile.TemporaryDirectory() as tmp:
        assert _bridge(tmp, _client(pong_ago=100)).should_self_exit() is False


# ---------- 4. 不误杀 ----------


def test_idle_for_a_day_with_pongs_flowing_is_not_killed():
    """用户一整天没说话（静默 24 小时），但 pong 正常 ⇒ 绝不退。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=30), stall=0.0)
        b._last_event_at = time.monotonic() - 24 * 3600
        b._last_event_wall = time.time() - 24 * 3600
        assert b.should_self_exit() is False


def test_server_never_sends_pong_falls_back_to_old_rules():
    """服务端不回 pong（没实测过它一定回）⇒ 这条信号不参与；连接 OPEN 照旧算活。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=None), stall=0.0)
        b._last_event_at = time.monotonic() - 10 * 3600
        b._last_event_wall = time.time() - 10 * 3600
        assert b.should_self_exit() is False


def test_inflight_task_blocks_fast_kill():
    """有任务在跑 ⇒ 快判据不动手（回复走 HTTP 能送达，腰斩才是真丢）。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=600), stall=600)
        _busy(b)
        assert b.should_self_exit() is False


def test_inflight_task_still_has_the_hour_long_backstop():
    """阳性对照：任务卡死也不能永远不重启 —— 老的 阈值×4 宽限照旧生效。"""
    from bridge.app import _INFLIGHT_GRACE

    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, None, stall=600)
        _busy(b)
        b._last_event_at = time.monotonic() - WATCHDOG_SILENT_SEC * (_INFLIGHT_GRACE + 1)
        assert b.should_self_exit() is True


def test_wake_from_sleep_restarts_fast_clocks():
    """合盖醒来：墙钟多走 1 小时 ⇒ 刚睡醒 ⇒ 快判据重新计时，旧 pong / 旧探针都不算。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=250), stall=300)
        b._loop_probe = wsfast.LoopProbe(lambda: None)
        mono, wall = time.monotonic(), time.time()
        b._watchdog_clock = (mono - WATCHDOG_TICK_SEC, wall - WATCHDOG_TICK_SEC - 3600)
        b._watchdog_note_clock()
        assert b._loop_stall is None
        assert b.stuck_reason() == ""
        assert b.should_self_exit() is False


def test_normal_tick_is_not_a_wake():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=250))
        mono, wall = time.monotonic(), time.time()
        b._watchdog_clock = (mono - 15, wall - 15 - (WATCHDOG_SLEEP_JUMP_SEC - 5))
        b._watchdog_note_clock()
        assert b.should_self_exit() is True, "没睡（跳变不足阈值）⇒ 快判据照常"


def test_after_wake_fast_signal_needs_full_threshold_again():
    """醒来 100 秒后 pong 还没来 ⇒ 不退；醒够 210 秒还没来 ⇒ 退。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=10_000))
        b._watchdog_awake_since = time.monotonic() - 100
        assert b.should_self_exit() is False
        b._watchdog_awake_since = time.monotonic() - 220
        assert b.should_self_exit() is True


def test_clock_rollback_is_not_a_wake():
    """NTP 回拨（墙钟往回走）⇒ 不当成睡醒，也不报错。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=250))
        mono, wall = time.monotonic(), time.time()
        b._watchdog_clock = (mono - 15, wall + 3600)
        b._watchdog_note_clock()
        assert not hasattr(b, "_watchdog_awake_since")


# ---------- 5. 兜底与整拍 ----------


def test_fifteen_minute_backstop_kept_when_no_new_signal():
    """新信号全拿不到（探针 None、没 pong、连接没了）⇒ 照旧 15 分钟才退。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, None, stall=None)
        b._last_event_at = time.monotonic() - (WATCHDOG_SILENT_SEC - 60)
        b._last_event_wall = time.time() - (WATCHDOG_SILENT_SEC - 60)
        assert b.should_self_exit() is False
        b._last_event_at = time.monotonic() - (WATCHDOG_SILENT_SEC + 60)
        b._last_event_wall = time.time() - (WATCHDOG_SILENT_SEC + 60)
        assert b.should_self_exit() is True
        assert "连续断了" in b._watchdog_reason
    assert WATCHDOG_SILENT_SEC == 15 * 60


def test_stuck_reason_never_raises():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=250))
        b.connection_alive = lambda: 1 / 0
        assert b.stuck_reason() == ""


def test_watchdog_tick_exits_75_with_reason_and_keeps_log_marker(monkeypatch, caplog):
    """整拍：判死 ⇒ exit 75，日志里还带「长连接已静默」（status.py 按它数看门狗重启）。"""
    from bridge import status as status_mod

    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=250))
        b._loop_probe = wsfast.LoopProbe(lambda: None)
        codes = []

        def fake_exit(code):
            codes.append(code)
            raise SystemExit(code)

        monkeypatch.setattr(app_mod.os, "_exit", fake_exit)
        with caplog.at_level("ERROR"), pytest.raises(SystemExit):
            b._watchdog_tick()
        assert codes == [75]
        line = next(r.getMessage() for r in caplog.records if "主动退出" in r.getMessage())
        assert "心跳回包" in line
        assert status_mod.classify_log_errors([line]).get("看门狗重启") == 1


def test_watchdog_tick_healthy_does_not_exit(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _client(pong_ago=10))
        b._loop_probe = wsfast.LoopProbe(lambda: None)
        monkeypatch.setattr(app_mod.os, "_exit", lambda code: pytest.fail("不该退出"))
        for _ in range(3):
            b._watchdog_tick()


def test_tick_is_fine_grained_enough_for_three_minutes():
    """最坏发现时间 = 循环卡死阈值 + 一拍 ≤ 3 分钟。"""
    assert WATCHDOG_LOOP_STALL_SEC + WATCHDOG_TICK_SEC <= 180
    assert wsfast.pong_limit(_client(ping_interval=90)) + WATCHDOG_TICK_SEC <= 240
