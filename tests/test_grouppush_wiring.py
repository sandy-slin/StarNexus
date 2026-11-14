import unittest
from types import SimpleNamespace

from tests.helpers import Clock, FakeBinds, FakeBound, View, sess
from bridge import app as app_mod
from bridge.fleet.grouppush import GroupPusher
from bridge.fleet.model import Message


class FakeScan:
    def __init__(self, view):
        self.view = view

    def overview(self):
        return self.view


class FakeControl:
    def __init__(self, reply="全部改完，测试通过。"):
        self.reply = reply
        self.detail_calls = []

    def detail(self, tool, sid, n=4):
        self.detail_calls.append((tool, sid))
        return None, [Message(role="user", text="改一下"), Message(role="assistant", text=self.reply)], ""


def make_bridge(view, clock, binds, waiting_enabled=False):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.fleet = FakeControl()
    bridge.chat_binds = binds
    bridge.group_pusher = GroupPusher(now=clock)
    bridge.waiting_notifier = SimpleNamespace(enabled=waiting_enabled, due=lambda v: [])
    bridge.settings = SimpleNamespace(owner_open_ids=set())
    bridge.sent = []
    bridge._say = lambda chat_id, text: bridge.sent.append((chat_id, text)) or True
    bridge._fleet_scanner = lambda: FakeScan(bridge.view)
    bridge.view = view
    return bridge


class WiringTest(unittest.TestCase):
    def test_tick_pushes_waiting_to_bound_group(self):
        clock = Clock()
        binds = FakeBinds([FakeBound("oc_group", "claude", "s1")])
        bridge = make_bridge(View([sess(status="waiting")]), clock, binds)
        bridge._notify_tick()
        clock.tick(25)
        bridge._notify_tick()
        self.assertEqual(len(bridge.sent), 1)
        self.assertEqual(bridge.sent[0][0], "oc_group")
        self.assertIn("需要人介入", bridge.sent[0][1])

    def test_tick_pushes_done_with_last_reply(self):
        clock = Clock()
        binds = FakeBinds([FakeBound("oc_group", "codex", "c1")])
        bridge = make_bridge(View([sess(tool="codex", sid="c1", status="running")]), clock, binds)
        bridge._notify_tick()
        bridge.view = View([sess(tool="codex", sid="c1", last_turn="done", last_turn_at=clock() + 1)])
        clock.tick(60)
        bridge._notify_tick()
        self.assertEqual(len(bridge.sent), 1)
        self.assertIn("全部改完", bridge.sent[0][1])

    def test_group_push_error_does_not_break_owner_push(self):
        clock = Clock()
        bridge = make_bridge(View([]), clock, FakeBinds(), waiting_enabled=True)
        called = []
        bridge.waiting_notifier = SimpleNamespace(enabled=True, due=lambda v: called.append(1) or [])

        def boom(view):
            raise RuntimeError("x")

        bridge._group_push_tick = boom
        bridge._notify_tick()
        self.assertEqual(called, [1])

    def test_settings_switch(self):
        raw_off = app_mod._truthy(False)
        self.assertFalse(raw_off)
        self.assertTrue(hasattr(app_mod.Settings, "__dataclass_fields__"))
        self.assertIn("notify_group", app_mod.Settings.__dataclass_fields__)


if __name__ == "__main__":
    unittest.main()
