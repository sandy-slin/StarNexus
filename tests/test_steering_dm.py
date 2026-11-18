"""中途插话 1.8.0：私聊里桥自己跑的 claude、新建会话第一轮（docs/product/steering.md）。"""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge import runner as runner_mod
from bridge.fleet.claude import STEER_OK
from bridge.fleet.model import ActionResult

FAKE = os.path.join(os.path.dirname(__file__), "fake_runner_claude.py")
NEW_SID = "33333333-4444-5555-6666-777777777777"


class FakeArgvRunner(runner_mod.ClaudeRunner):
    def __init__(self, wait):
        super().__init__(SimpleNamespace(turn_timeout_sec=20), config_dir="/tmp/sn-steer-dm")
        self.wait = wait

    def _build_argv(self, prompt, resume, workspace="", policy=None):
        return [sys.executable, FAKE, str(self.wait), "-p", prompt, "--output-format", "stream-json"]


async def _turn_with_steer(runner, steer_text, delay):
    task = asyncio.create_task(runner.run_turn("原任务", "/tmp", steer_key="dm:u1"))
    steered = None
    if steer_text is not None:
        for _ in range(200):
            if runner.steerable("dm:u1"):
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(delay)
        steered = runner.steer("dm:u1", steer_text)
    result = await task
    return result, steered


class RunnerSteerTest(unittest.TestCase):
    def test_argv_feeds_stdin(self):
        out = runner_mod._stream_argv(["claude", "-p", "hi", "--output-format", "stream-json", "--verbose"])
        self.assertNotIn("hi", out)
        self.assertEqual(out[out.index("--input-format") + 1], "stream-json")
        self.assertIn("--replay-user-messages", out)

    def test_plain_turn_still_works(self):
        result, _ = asyncio.run(_turn_with_steer(FakeArgvRunner(0), None, 0))
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "GOT:原任务")

    def test_steer_lands_in_same_turn(self):
        runner = FakeArgvRunner(1.5)
        result, steered = asyncio.run(_turn_with_steer(runner, "改主意了", 0.2))
        self.assertTrue(steered)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "GOT:原任务|改主意了")
        self.assertFalse(runner.steerable("dm:u1"))

    def test_steer_after_end_is_refused(self):
        runner = FakeArgvRunner(0)
        asyncio.run(_turn_with_steer(runner, None, 0))
        self.assertFalse(runner.steer("dm:u1", "晚了"))

    def test_no_key_no_registration(self):
        runner = FakeArgvRunner(0.5)

        async def go():
            task = asyncio.create_task(runner.run_turn("原任务", "/tmp"))
            await asyncio.sleep(0.2)
            seen = runner.steerable("")
            await task
            return seen
        self.assertFalse(asyncio.run(go()))


class _Lock:
    def __init__(self, locked):
        self._locked = locked

    def locked(self):
        return self._locked


def dm_bridge(steer_ok, busy=True):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    calls = []
    bridge.runner = SimpleNamespace(steerable=lambda key: busy, steer=lambda key, text: calls.append(text) or steer_ok)
    bridge.reviewer = None
    said = []
    bridge._say = lambda chat_id, text: said.append(text) or True
    return bridge, said, calls


class DmSteerTest(unittest.TestCase):
    def test_busy_dm_turn_is_steered(self):
        bridge, said, calls = dm_bridge(True)
        self.assertTrue(bridge._try_steer_local("dm:u1", "oc_dm", "换个思路"))
        self.assertEqual(calls, ["换个思路"])
        self.assertEqual(said, [STEER_OK])

    def test_not_steerable_falls_back(self):
        bridge, said, calls = dm_bridge(True, busy=False)
        self.assertFalse(bridge._try_steer_local("dm:u1", "oc_dm", "x"))
        self.assertEqual((said, calls), ([], []))

    def test_write_failed_falls_back(self):
        bridge, said, _ = dm_bridge(False)
        self.assertFalse(bridge._try_steer_local("dm:u1", "oc_dm", "x"))
        self.assertEqual(said, [])

    def test_paused_not_steered(self):
        bridge, said, calls = dm_bridge(True)
        bridge._paused = {"dm:u1": "x"}
        self.assertFalse(bridge._try_steer_local("dm:u1", "oc_dm", "x"))
        self.assertEqual(calls, [])

    def test_run_locally_steers_instead_of_queueing(self):
        bridge, said, calls = dm_bridge(True)
        session = SimpleNamespace(lock=_Lock(True), waiting=0, workspace="")
        bridge.sessions = SimpleNamespace(get=lambda scope: session)
        bridge.workspaces = SimpleNamespace(get=lambda scope: "/tmp")
        asyncio.run(bridge._run_locally("dm:u1", "oc_dm", "换个思路"))
        self.assertEqual(said, [STEER_OK])
        self.assertEqual(session.waiting, 0)


class FakeControl:
    def __init__(self, ok):
        self.ok = ok
        self.steered = []

    def peek_pending_fresh(self, scope):
        return None, False

    def steer(self, tool, sid, text):
        self.steered.append((tool, sid, text))
        return ActionResult(self.ok, STEER_OK if self.ok else "", sid)


def new_session_bridge(ok, busy):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.fleet = FakeControl(ok)
    bridge.reviewer = None
    bridge._group_bind_arm = lambda *a: asyncio.sleep(0, result=False)
    bridge._restart_talk_offer = lambda *a: False
    bridge._session_policy = lambda tool, sid: SimpleNamespace(mode="trusted")
    bridge._fleet_inflight = {"dm:u1": busy}
    said = []
    bridge._say = lambda chat_id, text: said.append(text) or True
    bridge._say_with_buttons = lambda chat_id, text, buttons: said.append(text) or True
    return bridge, said


class FirstTurnSteerTest(unittest.TestCase):
    def test_first_turn_of_new_claude_session_is_steered(self):
        bridge, said = new_session_bridge(True, ("claude", NEW_SID))
        self.assertTrue(asyncio.run(bridge._maybe_send_to_fleet("dm:u1", "oc_dm", "顺便加个测试")))
        self.assertEqual(bridge.fleet.steered, [("claude", NEW_SID, "顺便加个测试")])
        self.assertEqual(said, [STEER_OK])

    def test_first_turn_closing_says_so_honestly(self):
        bridge, said = new_session_bridge(False, ("claude", NEW_SID))
        self.assertTrue(asyncio.run(bridge._maybe_send_to_fleet("dm:u1", "oc_dm", "x")))
        self.assertEqual(said, ["新会话第一轮正好在收尾，这句没插进去。等它回来再说一次。"])

    def test_placeholder_id_is_not_steered(self):
        from bridge.fleet.newsession import INFLIGHT_ID

        bridge, said = new_session_bridge(True, ("codex", INFLIGHT_ID))
        self.assertFalse(asyncio.run(bridge._maybe_send_to_fleet("dm:u1", "oc_dm", "x")))
        self.assertEqual(bridge.fleet.steered, [])


class AdapterStartIdTest(unittest.TestCase):
    def test_control_passes_session_id_only_to_claude(self):
        import inspect

        from bridge.fleet import claude, control

        self.assertIn("session_id", inspect.signature(claude.ClaudeAdapter.start).parameters)
        self.assertIn("session_id", inspect.signature(control.FleetControl.start).parameters)


if __name__ == "__main__":
    unittest.main()
