"""第 22 轮（S2 日志挖掘 Top 5）：常驻菜单同一个键 3 秒内连点只算一次。

83 次菜单点击里 11 次是 ≤3 秒的同键重复（`/sessions` 7、`/mode` 4），每下都单独跑 ⇒
聊天里一连串一样的总览卡。真事件对象（SDK 模型）→ 真 `on_bot_menu`，数派发了几次。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_mode_switch_e2e import OWNER, _bridge, _capture_dispatch, _menu_event  # noqa: E402


@pytest.fixture
def clicks(tmp_path, monkeypatch):
    import bridge.app as app_mod

    clock = {"t": 1000.0}
    monkeypatch.setattr(app_mod.time, "monotonic", lambda: clock["t"])
    b = _bridge(tmp_path)
    b.touch = lambda: None
    sent, restore = _capture_dispatch(b)

    def click(key, after=0.0, who=OWNER):
        clock["t"] += after
        b.on_bot_menu(_menu_event(key, who))
        return sent[::2]

    yield click
    restore()


def test_same_key_twice_in_one_second_dispatches_once(clicks):
    clicks("/sessions")
    assert clicks("/sessions", after=1.0) == ["/sessions"]


def test_different_keys_both_dispatch(clicks):
    clicks("/sessions")
    assert clicks("/mode", after=1.0) == ["/sessions", "/mode"]


def test_same_key_after_five_seconds_dispatches_again(clicks):
    clicks("/sessions")
    assert clicks("/sessions", after=5.0) == ["/sessions", "/sessions"]


def test_stop_is_never_swallowed(clicks):
    clicks("/stop")
    assert clicks("/stop", after=0.5) == ["/stop", "/stop"]


def test_burst_of_four_like_the_log(clicks):
    """11-07 18:45 现场：`/sessions` 连着 4 下。"""
    for gap in (0, 0.6, 0.7, 0.8):
        out = clicks("/sessions", after=gap)
    assert out == ["/sessions"]
