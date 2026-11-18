"""1.9.4：进会话聊天（relay）不再一个会话挡住所有会话（2026-11-18 22:38 两个 multica 群互相回「上一句还在送」）。"""
import threading
import time
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge.fleet import relay as relay_mod
from bridge.fleet.model import ActionResult

SID_A = "aaaaaaaa-1111-2222-3333-444444444444"
SID_B = "bbbbbbbb-1111-2222-3333-444444444444"


class SlowRelay(relay_mod.Relay):
    """`_send_locked` 换成「等一会儿再回」，模拟目标会话正在想。"""

    def __init__(self, hold):
        super().__init__(SimpleNamespace(home="/tmp/sn-relay-none", bridge_home="/tmp/sn-relay-none",
                                         claude_bin="claude", jobs=None),
                         courier=SimpleNamespace(close=lambda: None))
        self.hold = hold

    def _send_locked(self, sid, text, timeout, delivery_timeout):
        self.hold.wait(5)
        return ActionResult(True, "回复:" + text, sid, delivered=True)


def run_bg(relay, sid, text, box):
    th = threading.Thread(target=lambda: box.append(relay.send(sid, text)))
    th.start()
    time.sleep(0.2)
    return th


class RelayConcurrencyTest(unittest.TestCase):
    def test_other_session_not_blocked(self):
        hold = threading.Event()
        relay = SlowRelay(hold)
        box = []
        th = run_bg(relay, SID_A, "A 的长任务", box)
        other = []
        th2 = threading.Thread(target=lambda: other.append(relay.send(SID_B, "B 的问题")))
        th2.start()
        time.sleep(0.2)
        hold.set()
        th.join(5)
        th2.join(5)
        self.assertTrue(other[0].ok, other[0].text)
        self.assertNotEqual(other[0].text, relay_mod.WHY_BUSY_RELAY)
        self.assertTrue(box[0].ok)

    def test_same_session_still_one_at_a_time(self):
        hold = threading.Event()
        relay = SlowRelay(hold)
        box = []
        th = run_bg(relay, SID_A, "第一句", box)
        second = relay.send(SID_A, "第二句")
        hold.set()
        th.join(5)
        self.assertFalse(second.ok)
        self.assertEqual(second.text, relay_mod.WHY_BUSY_RELAY)

    def test_lock_released_after_send(self):
        hold = threading.Event()
        hold.set()
        relay = SlowRelay(hold)
        self.assertTrue(relay.send(SID_A, "一").ok)
        self.assertTrue(relay.send(SID_A, "二").ok)


if __name__ == "__main__":
    unittest.main()


class MovedGroupTest(unittest.TestCase):
    """22:42 会话改绑到新群后，旧群那轮回来不能把目标续回去。"""

    def bridge(self, bound):
        from bridge import app as app_mod

        br = app_mod.Bridge.__new__(app_mod.Bridge)
        br.chat_binds = SimpleNamespace(of_scope=lambda scope: bound)
        return br

    def test_old_group_after_move_is_moved_away(self):
        bound = SimpleNamespace(tool="claude", id="s1", on=False)
        self.assertTrue(self.bridge(bound)._moved_away("group:oc_old", "claude", "s1"))

    def test_active_group_not_moved(self):
        bound = SimpleNamespace(tool="claude", id="s1", on=True)
        self.assertFalse(self.bridge(bound)._moved_away("group:oc_g", "claude", "s1"))

    def test_dm_never_moved(self):
        self.assertFalse(self.bridge(None)._moved_away("dm:u1", "claude", "s1"))

    def test_unbound_other_session_not_moved(self):
        bound = SimpleNamespace(tool="claude", id="other", on=False)
        self.assertFalse(self.bridge(bound)._moved_away("group:oc_g", "claude", "s1"))


class CodexQueuedMessageTest(unittest.TestCase):
    """22:58 Codex App 正在跑长任务时，排队的那句被说成「电脑上没人开着」。"""

    def adapter(self):
        from bridge.fleet import codex as codex_mod

        ad = codex_mod.CodexAdapter.__new__(codex_mod.CodexAdapter)
        ad._new_records = lambda tid, marks: []
        ad.pending_queued = lambda tid: 1
        return ad, codex_mod

    def test_app_busy_is_not_nobody_home(self):
        ad, codex_mod = self.adapter()
        res = ad._await_reply("t", "现在是什么情况", {}, 0.01, 0.01, 0.0, owner="app")
        self.assertEqual(res.text, codex_mod.RELAY_QUEUED_BUSY)
        self.assertTrue(res.delivered)

    def test_nobody_home_still_reported(self):
        ad, codex_mod = self.adapter()
        res = ad._await_reply("t", "x", {}, 0.01, 0.01, 0.0, owner="")
        self.assertEqual(res.text, codex_mod.RELAY_QUEUED_NOBODY_HOME)
