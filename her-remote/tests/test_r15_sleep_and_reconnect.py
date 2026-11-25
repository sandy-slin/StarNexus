"""第 15 轮：电脑睡醒之后，看门狗看不出自己断了多久；重连期间也说不清状态。

## 现场（主人这台，2026-11-06 真实日志 + pmset）

    20:35:40 ERROR Lark receive message loop exit, err: no close frame received or sent
    20:59:49 INFO  Lark trying to reconnect for the 1st time
    21:14:21 INFO  Lark connected to wss://...
  pmset: 20:28:51 Entering Sleep ... 21:12:53 Wake from Deep Idle [lid]

39 分钟收不到任何消息，看门狗（阈值 15 分钟）一次都没动。

## 这一轮改两处

1. **墙上时钟**：`silent_for` 原来只用 `time.monotonic()`。macOS 上它是 `mach_absolute_time()`，
   合盖睡眠期间**不前进**（睡 8 小时醒来可能只涨了几秒）⇒ 判不出「断了很久」。
   🔴 不能只换成 wall clock：NTP 回拨 / 用户改系统时间会让它变成负数或巨大值。
   所以两个都记，判故障时取**大的那个**，并且 wall 的差值为负时丢掉（时钟被回拨）。
2. **重连状态来自 SDK 自己**：`lark.ws.Client` 有 `on_reconnecting` / `on_reconnected`
   两个回调（本机实测：是实例属性，默认 no-op）。接上之后「正在重连」是现场事实，
   不用再靠「多久没收到消息」反推。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import _fake_bridge  # noqa: E402


def _b(tmp):
    b = _fake_bridge(tmp)
    b._ws_client = None
    return b


# ---------- 1. 睡醒之后判得出断了多久 ----------


def test_silent_for_uses_wall_clock_when_monotonic_froze():
    """合盖的形状：monotonic 没动，墙上时钟过了 40 分钟 ⇒ 必须判成断了 40 分钟。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        b.touch()
        # monotonic 冻住（睡眠），wall 往前走 40 分钟
        b._last_event_wall = time.time() - 40 * 60
        assert b.silent_for >= 40 * 60 - 5, b.silent_for


def test_clock_rollback_does_not_fake_a_long_outage():
    """🔴 NTP 回拨 / 改系统时间：wall 差值为负 ⇒ 丢掉它，不许判成断线。

    变异验证时发现：只断言 `silent_for < 60` 抓不住「去掉这道防护」——
    负数本来就 < 60，而 `max(mono, 负数)` 恰好等于 mono。真正要钉的是
    **不许返回负数**（负数会让 `should_self_exit` 永远为假，看门狗彻底失效），
    而且这台机器刚 touch 过 ⇒ 必须判成「没断」。
    """
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        b.touch()
        b._last_event_wall = time.time() + 3600   # 「上次收到消息」在未来
        assert b.silent_for >= 0, f"返回了负数：{b.silent_for}"
        assert b.silent_for < 60, b.silent_for
        assert b.should_self_exit() is False


def test_missing_wall_stamp_falls_back_to_monotonic():
    """老配置 / 还没 touch 过（`_last_event_wall` 是 0）⇒ 不许把 1970 年算成断了 56 年。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        b._last_event_at = time.monotonic()
        b._last_event_wall = 0.0
        assert b.silent_for < 60, b.silent_for
        assert b.should_self_exit() is False


def test_watchdog_exits_after_sleep_shaped_outage():
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        b.touch()
        b._last_event_wall = time.time() - 40 * 60
        assert b.should_self_exit() is True


def test_short_quiet_is_not_an_outage():
    """阳性对照：用户只是没说话 ⇒ 不许自杀。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        b.touch()
        assert b.should_self_exit() is False


# ---------- 2. 重连状态是 SDK 给的，不是猜的 ----------


def test_reconnect_callbacks_are_wired_to_the_sdk():
    """调用点还在：run() 里要把两个回调挂到 ws 客户端上。"""
    import inspect

    from bridge import app as app_mod

    cls = next(v for v in vars(app_mod).values()
               if inspect.isclass(v) and hasattr(v, "_watchdog_loop"))
    src = inspect.getsource(cls.run)
    assert "on_reconnecting" in src and "on_reconnected" in src


def test_reconnecting_then_reconnected_updates_state():
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        assert b.reconnect_state() == ""
        b._on_ws_reconnecting()
        assert "重连" in b.reconnect_state()
        b._on_ws_reconnected()
        assert b.reconnect_state() == ""


def test_reconnecting_shows_up_on_the_health_card():
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        b._on_ws_reconnecting()
        data = b._collect_health("dm:owner")
        assert "重连" in str(data.get("connection", "")), data
        # 平时不出现这一行，断过才出现
        assert data.get("reconnects"), data


def test_reconnect_count_is_remembered_for_diagnosis():
    """断过几次要留痕 —— 「低流量 0 故障」不等于没问题。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp)
        for _ in range(3):
            b._on_ws_reconnecting()
            b._on_ws_reconnected()
        assert b.reconnect_total() == 3
