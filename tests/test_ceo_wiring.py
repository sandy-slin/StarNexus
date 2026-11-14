import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace

from bridge import app as app_mod
from bridge.fleet.ceo import MissionStore, Orchestrator


class FakeBrain:
    def ask(self, prompt, schema):
        raise AssertionError("brain should not be called in wiring tests")


def make_bridge(binds=None):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    tmp = tempfile.mkdtemp()
    bridge.sent = []
    bridge._say = lambda chat_id, text: bridge.sent.append((chat_id, text)) or True
    bridge.chat_binds = binds
    bridge.fleet = SimpleNamespace()
    bridge.started = []
    bridge.ceo = Orchestrator(
        MissionStore(os.path.join(tmp, "ceo.json")), FakeBrain(), bridge.fleet, bridge._say,
        policy=lambda: None, projects=lambda: [("repo", tmp)],
        spawn=lambda fn: bridge.started.append(fn),
    )
    return bridge


def run(coro):
    return asyncio.run(coro)


class FakeBinds:
    def __init__(self, bound=None):
        self.bound = bound or {}

    def of_chat(self, chat_id):
        return self.bound.get(chat_id)


class CeoCommandTest(unittest.TestCase):
    def test_help(self):
        bridge = make_bridge()
        self.assertTrue(run(bridge._handle_command("group:oc_a", "oc_a", "/ceo")))
        self.assertIn("CEO 编排", bridge.sent[-1][1])

    def test_mark_in_group(self):
        bridge = make_bridge(FakeBinds())
        run(bridge._handle_command("group:oc_a", "oc_a", "/ceo 群"))
        self.assertTrue(bridge.ceo.is_ceo_chat("oc_a"))
        self.assertIn("CEO 群", bridge.sent[-1][1])

    def test_mark_refused_in_dm(self):
        bridge = make_bridge(FakeBinds())
        run(bridge._handle_command("dm:ou_x", "oc_dm", "/ceo 群"))
        self.assertFalse(bridge.ceo.is_ceo_chat("oc_dm"))

    def test_mark_refused_when_group_bound_to_session(self):
        bridge = make_bridge(FakeBinds({"oc_a": SimpleNamespace(on=True)}))
        run(bridge._handle_command("group:oc_a", "oc_a", "/ceo 群"))
        self.assertFalse(bridge.ceo.is_ceo_chat("oc_a"))
        self.assertIn("已经绑了一个会话", bridge.sent[-1][1])

    def test_start_task_spawns_mission(self):
        bridge = make_bridge(FakeBinds())
        run(bridge._handle_command("group:oc_a", "oc_a", "/ceo 写一个 hello world"))
        self.assertEqual(len(bridge.started), 1)
        self.assertEqual(bridge.ceo.store.latest("oc_a")["goal"], "写一个 hello world")
        self.assertTrue(bridge.ceo.is_ceo_chat("oc_a"))

    def test_status_and_unmark(self):
        bridge = make_bridge(FakeBinds())
        run(bridge._handle_command("group:oc_a", "oc_a", "/ceo 状态"))
        self.assertIn("还没有 CEO 任务", bridge.sent[-1][1])
        run(bridge._handle_command("group:oc_a", "oc_a", "/ceo 群"))
        run(bridge._handle_command("group:oc_a", "oc_a", "/ceo 退出"))
        self.assertFalse(bridge.ceo.is_ceo_chat("oc_a"))

    def test_unavailable_when_no_ceo(self):
        bridge = make_bridge()
        bridge.ceo = None
        run(bridge._handle_command("group:oc_a", "oc_a", "/ceo 任务"))
        self.assertIn("用不了", bridge.sent[-1][1])


class CeoTextRoutingTest(unittest.TestCase):
    def test_plain_text_in_ceo_group_goes_to_ceo(self):
        bridge = make_bridge(FakeBinds())
        bridge.ceo.mark_chat("oc_a")
        self.assertTrue(run(bridge._maybe_ceo_text("group:oc_a", "oc_a", "做个调研")))
        self.assertEqual(len(bridge.started), 1)

    def test_other_group_and_dm_untouched(self):
        bridge = make_bridge(FakeBinds())
        bridge.ceo.mark_chat("oc_a")
        self.assertFalse(run(bridge._maybe_ceo_text("group:oc_b", "oc_b", "你好")))
        self.assertFalse(run(bridge._maybe_ceo_text("dm:ou_x", "oc_a", "你好")))
        self.assertEqual(bridge.started, [])

    def test_handle_routes_ceo_before_sessions(self):
        bridge = make_bridge(FakeBinds())
        bridge.ceo.mark_chat("oc_a")
        calls = []

        async def nope(*args):
            calls.append(args)
            return False

        bridge._remember_input = lambda scope, text: None
        bridge._sync_talk_marker = lambda scope: None
        bridge._maybe_start_new_session = nope
        bridge._maybe_send_to_fleet = nope
        bridge._run_locally = nope
        run(bridge._handle("group:oc_a", "oc_a", "把登录页改成深色"))
        self.assertEqual(calls, [])
        self.assertEqual(len(bridge.started), 1)
        run(bridge._handle("group:oc_b", "oc_b", "普通群的话"))
        self.assertEqual(len(calls), 3)


class BuildCeoTest(unittest.TestCase):
    def test_none_without_fleet(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.fleet = None
        self.assertIsNone(bridge._build_ceo(tempfile.mkdtemp()))

    def test_builds_with_fleet(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.fleet = SimpleNamespace(fleet=SimpleNamespace(adapter=lambda tool: None))
        bridge._say = lambda chat_id, text: True
        ceo = bridge._build_ceo(tempfile.mkdtemp())
        self.assertIsNotNone(ceo)
        self.assertEqual(ceo.recover(), 0)


if __name__ == "__main__":
    unittest.main()
