"""FC-11 / FC-12：私聊「停下」列出在跑的会话；手机发起的直接停，终端 / App 的照实说并给替代方法。"""
import asyncio
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.cards import resolve_fleet_click
from bridge.fleet.control import FleetControl
from bridge.fleet.model import Session


def sess(tool, sid, status="running", can_stop=False, why="", owner="terminal", at=0.0, title=""):
    return Session(tool=tool, surface="cli", id=sid, title=title or sid, status=status, can_stop=can_stop,
                   why_not_stop=why, owner=owner, updated_at=at)


class RunningTest(unittest.TestCase):
    def test_running_only_and_stoppable_first(self):
        control = FleetControl(fleet=SimpleNamespace())
        for s in (sess("claude", "a", at=3), sess("codex", "b", can_stop=True, owner="bridge", at=1),
                  sess("codex", "c", status="idle")):
            control._remember(s)
        self.assertEqual([s.id for s in control.running()], ["b", "a"])


class OfferTest(unittest.TestCase):
    def make(self, sessions):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        control = FleetControl(fleet=SimpleNamespace())
        for s in sessions:
            control._remember(s)
        control.overview = lambda: None
        bridge.fleet = control
        bridge.out = []
        bridge._say_with_buttons = lambda chat, text, buttons: bridge.out.append((text, buttons))
        return bridge

    def values(self, buttons):
        return [b["behaviors"][0]["value"] for b in buttons]

    def test_lists_with_right_actions(self):
        bridge = self.make([sess("codex", "01a1-own", can_stop=True, owner="bridge"),
                            sess("claude", "term-1", why="终端里的会话只能在终端里停，从外面停会直接关掉整个会话。")])
        self.assertTrue(asyncio.run(bridge._offer_stop_choices("oc_dm")))
        text, buttons = bridge.out[0]
        self.assertIn("可以直接停", text)
        self.assertIn("只能在终端里停", text)
        ops = [(v.get("op"), v.get("id")) for v in self.values(buttons) if v.get("act") == "fleet"]
        self.assertIn(("stop", "01a1-own"), ops)
        self.assertIn(("remind", "term-1"), ops)
        self.assertNotIn(("stop", "term-1"), ops)
        known = bridge.fleet.known_keys()
        for value in self.values(buttons):
            if value.get("act") == "fleet":
                self.assertIsNotNone(resolve_fleet_click(value, known))

    def test_nothing_running(self):
        bridge = self.make([sess("codex", "x", status="idle")])
        self.assertFalse(asyncio.run(bridge._offer_stop_choices("oc_dm")))
        self.assertEqual(bridge.out, [])


class RemindTest(unittest.TestCase):
    def make(self, current):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.fleet = SimpleNamespace(refresh=lambda tool, sid: (current, ""))
        bridge.out = []
        bridge._say_with_buttons = lambda chat, text, buttons: bridge.out.append(text)
        return bridge

    def test_still_running_reminds(self):
        bridge = self.make(sess("codex", "t1", owner="app"))
        asyncio.run(bridge._remind_stop_later("oc_dm", "codex", "t1", delay=0.01))
        self.assertIn("Codex App", bridge.out[0])

    def test_finished_no_reminder(self):
        bridge = self.make(sess("codex", "t1", status="idle"))
        asyncio.run(bridge._remind_stop_later("oc_dm", "codex", "t1", delay=0.01))
        self.assertEqual(bridge.out, [])


if __name__ == "__main__":
    unittest.main()


class StoppedMarkTest(unittest.TestCase):
    def test_stop_marks_and_is_read_once(self):
        from bridge.fleet.model import FleetJobs

        killed = []
        jobs = FleetJobs(lstart_of=lambda pid: "Mon Nov 17 01:00:00 2026", kill=lambda pid, sig: killed.append(pid))
        jobs.register_spawned(4242, "codex", "t1")
        res = jobs.stop_session("codex", "t1")
        self.assertTrue(res.ok)
        self.assertIn("会话还在", res.text)
        self.assertEqual(killed, [4242])
        self.assertTrue(jobs.take_stopped("codex", "t1"))
        self.assertFalse(jobs.take_stopped("codex", "t1"))
