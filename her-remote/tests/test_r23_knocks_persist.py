"""第 23 轮（S2 日志挖掘 Top 4）：「有人在找这台电脑」计数活过重启，且不落 open_id 原文。"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import _fake_bridge  # noqa: E402


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.settings.config_path = os.path.join(tmp, "config.toml")
    return b


def test_count_survives_restart_and_stores_no_open_id():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._note_stranger_knock("ou_stranger_1")
        b._note_stranger_knock("ou_stranger_1")
        b._note_stranger_knock("ou_stranger_2")
        fresh = _bridge(tmp)                       # 重启：新对象、同一个目录
        assert fresh.stranger_knocks()["count"] == 3
        assert fresh.stranger_knocks()["people"] == 2
        raw = open(os.path.join(tmp, "stranger_knocks.json"), encoding="utf-8").read()
        assert "ou_" not in raw, raw
        assert oct(os.stat(os.path.join(tmp, "stranger_knocks.json")).st_mode & 0o777) == "0o600"


def test_old_knocks_expire_after_a_day(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._note_stranger_knock("ou_x")
        later = time.time() + 25 * 3600
        monkeypatch.setattr("bridge.app.time.time", lambda: later)
        assert _bridge(tmp).stranger_knocks()["count"] == 0
        b2 = _bridge(tmp)
        b2._note_stranger_knock("ou_y")
        assert b2.stranger_knocks()["count"] == 1, "过期的旧计数被接着加了"


def test_unwritable_dir_does_not_break_the_message_path():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.settings.config_path = "/dev/null/nope/config.toml"   # 目录建不出来
        b._note_stranger_knock("ou_x")                          # 不抛
        assert b.stranger_knocks()["count"] == 1


def test_corrupt_file_starts_from_zero():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "stranger_knocks.json"), "w") as fh:
            fh.write("{坏了")
        assert _bridge(tmp).stranger_knocks()["count"] == 0


def test_knock_after_restart_continues_the_count():
    """重启后**先来一次敲门**（不是先看诊断卡）⇒ 接着旧数加，不能从 1 重新数。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._note_stranger_knock("ou_a")
        b._note_stranger_knock("ou_a")
        fresh = _bridge(tmp)
        fresh._note_stranger_knock("ou_b")
        assert fresh.stranger_knocks() ["count"] == 3
        assert _bridge(tmp).stranger_knocks()["people"] == 2
