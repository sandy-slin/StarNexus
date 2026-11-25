"""第 35 轮：看门狗兜底按「连续断了多久」判，不按「多久没收到消息」（主人 11-10 同意）。

本机 agent.log 11 次「长连接已静默」里多数是：用户空闲 >15 分钟 + 看门狗这一拍恰好碰上一次
正常重连（断开 0.2 秒 / 刚连上）⇒ 被判死。
"""

from __future__ import annotations

import time

from bridge.app import WATCHDOG_SILENT_SEC, Bridge


class _B:
    """只带看门狗要的那几个属性。"""

    should_self_exit = Bridge.should_self_exit
    _down_for = Bridge._down_for
    silent_for = Bridge.silent_for
    touch = Bridge.touch

    def __init__(self, alive: bool, idle_sec: float):
        self.alive = alive
        self._last_event_at = time.monotonic() - idle_sec
        self._last_event_wall = time.time() - idle_sec

    def connection_alive(self):
        return self.alive

    def stuck_reason(self):
        return ""

    def _work_in_flight(self):
        return False


def test_idle_user_plus_momentary_reconnect_is_not_killed():
    """🔴 回归：空闲 1 小时，上一拍还活着，这一拍正在重连 ⇒ 不许退。"""
    b = _B(alive=True, idle_sec=3600)
    assert b.should_self_exit() is False          # 看门狗看见它活着，记一笔
    b.alive = False                               # 下一拍碰上重连
    assert b.should_self_exit() is False


def test_really_down_for_longer_than_threshold_still_exits():
    b = _B(alive=True, idle_sec=3600)
    b.should_self_exit()
    b.alive = False
    b._last_alive_at = time.monotonic() - (WATCHDOG_SILENT_SEC + 30)
    assert b.should_self_exit() is True
    assert "连续断了" in b._watchdog_reason


def test_events_arriving_mean_not_down_even_if_probe_says_dead():
    """探活认不出（返回 False）但事件还在进来 ⇒ 连接其实是通的 ⇒ 不退。"""
    b = _B(alive=False, idle_sec=5)
    b._last_alive_at = time.monotonic() - (WATCHDOG_SILENT_SEC + 30)
    assert b.should_self_exit() is False


def test_never_seen_alive_falls_back_to_silence():
    """刚起来就一直连不上 ⇒ 没有「最后一次活着」⇒ 按原来的静默时长兜底。"""
    assert _B(alive=False, idle_sec=WATCHDOG_SILENT_SEC + 30).should_self_exit() is True
    assert _B(alive=False, idle_sec=WATCHDOG_SILENT_SEC - 30).should_self_exit() is False
