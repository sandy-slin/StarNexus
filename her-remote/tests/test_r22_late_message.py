"""第 22 轮（S2 日志挖掘 Top 3）：Mac 睡着 ⇒ 消息晚到 11~39 分钟，他不知道为什么这么慢。

真事件 → 真 `on_message`，看回复前有没有那句「晚到了」。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bridge.app import _late_minutes  # noqa: E402
from test_r19_ack_coverage import _deliver, _dm_bridge, _event  # noqa: E402


def _late_event(mid, chat_id, minutes_ago):
    ev = _event(mid, chat_id=chat_id, text="看看有哪些项目")
    ev.event.message.create_time = (str(int((time.time() - minutes_ago * 60) * 1000))
                                    if minutes_ago is not None else None)
    return ev


def _said(tmp_path, monkeypatch, minutes_ago, reconnected=True, order=None):
    from test_relay_integration import CHAT

    b, _c, _rx = _dm_bridge(tmp_path)
    if reconnected:
        b._on_ws_reconnected()             # 本机自己看到的「刚断过」证据
    else:
        b._started_wall = time.time() - 3600   # 进程起来一小时、期间没断过
    said = []
    b._say = lambda chat, text: (said.append(text), order is not None and order.append("say"))[0] or True

    async def run_one(session, chat_id, text):
        if order is not None:
            order.append("reply")

    b._run_one = run_one
    _deliver(b, monkeypatch, _late_event("om_late", CHAT, minutes_ago))
    return [t for t in said if "晚到" in t]


def test_twenty_minutes_late_says_so(tmp_path, monkeypatch):
    said = _said(tmp_path, monkeypatch, 20)
    assert len(said) == 1 and "20" in said[0] and "晚到" in said[0], said


@pytest.mark.parametrize("minutes_ago", [0, 1, None])
def test_on_time_or_unknown_says_nothing(tmp_path, monkeypatch, minutes_ago):
    assert _said(tmp_path, monkeypatch, minutes_ago) == []


@pytest.mark.parametrize("raw", ["", "abc", None, "-5", str(int((time.time() + 3600) * 1000))])
def test_bad_create_time_is_zero(raw):
    assert _late_minutes(raw) == 0


def test_clock_ahead_without_disconnect_says_nothing(tmp_path, monkeypatch):
    """R-entry 复现：本机时钟快 3 分钟以上、但这期间根本没断过线 ⇒ 不能每条都说「电脑睡着了」。"""
    assert _said(tmp_path, monkeypatch, 20, reconnected=False) == []


def test_late_note_is_sent_before_the_reply(tmp_path, monkeypatch):
    order: list = []
    _said(tmp_path, monkeypatch, 20, order=order)
    assert order[:2] == ["say", "reply"], order
