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


class MilestoneTest(unittest.TestCase):
    def test_picks_short_milestone_line(self):
        from bridge.fleet.grouppush import milestone_of

        self.assertEqual(milestone_of("看了下代码"), "")
        self.assertEqual(milestone_of("计划："), "")
        self.assertEqual(milestone_of("看了下代码\n计划已确定，三步走"), "计划已确定，三步走")
        self.assertEqual(milestone_of("x" * 200), "")

    def test_gap_and_dedupe(self):
        from bridge.fleet.grouppush import Milestones

        now = [1000.0]
        m = Milestones(gap=600, now=lambda: now[0])
        self.assertEqual(m.due(("codex", "s"), "阶段一完成"), "阶段一完成")
        self.assertEqual(m.due(("codex", "s"), "阶段一完成"), "")
        self.assertEqual(m.due(("codex", "s"), "阶段二完成"), "")
        now[0] += 601
        self.assertEqual(m.due(("codex", "s"), "阶段二完成"), "阶段二完成")


class MilestoneWiringTest(unittest.TestCase):
    def test_running_bound_session_pushes_once(self):
        from types import SimpleNamespace as NS

        from bridge import app as app_mod

        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        said = []
        bridge._say = lambda chat, text: said.append((chat, text)) or True
        sess = NS(tool="codex", id="s1", status="running", title="修登录")
        view = NS(sessions=[sess])
        binds = NS(of_session=lambda t, s: NS(chat_id="oc_g", on=True))
        control = NS(detail=lambda t, s, n: (sess, [NS(role="assistant", text="阶段一已完成，开始写测试")], ""))
        bridge._milestone_tick(view, binds, control)
        bridge._milestone_tick(view, binds, control)
        self.assertEqual(len(said), 1)
        self.assertIn("阶段一已完成", said[0][1])

    def test_inflight_from_group_not_pushed(self):
        from types import SimpleNamespace as NS

        from bridge import app as app_mod

        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge._fleet_inflight = {"group:oc_g": ("codex", "s1")}
        said = []
        bridge._say = lambda chat, text: said.append(text) or True
        sess = NS(tool="codex", id="s1", status="running", title="x")
        bridge._milestone_tick(NS(sessions=[sess]), NS(of_session=lambda t, s: NS(chat_id="oc_g", on=True)),
                               NS(detail=lambda t, s, n: (sess, [NS(role="assistant", text="已完成")], "")))
        self.assertEqual(said, [])
