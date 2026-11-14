import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from bridge import app as app_mod
from bridge.fleet.newsession import NewSessionState, pick_model


class PickModelTest(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(pick_model("claude", "opus"), "claude-opus-5-5")
        self.assertEqual(pick_model("claude", "Sonnet"), "claude-sonnet-5-5")
        self.assertEqual(pick_model("codex", "luna"), "gpt-6-luna")
        self.assertEqual(pick_model("codex", "gpt-6-sol"), "gpt-6-sol")

    def test_unknown_or_cross_tool(self):
        self.assertEqual(pick_model("claude", "luna"), "")
        self.assertEqual(pick_model("codex", "opus"), "")
        self.assertEqual(pick_model("cursor", "opus"), "")
        self.assertEqual(pick_model("claude", "remote_control"), "")

    def test_state_keeps_model(self):
        state = NewSessionState()
        state.arm("dm:x", "codex", "/tmp", "gpt-6-luna")
        armed, _ = state.peek("dm:x")
        self.assertEqual(armed["model"], "gpt-6-luna")
        state.arm("dm:x", "claude", "/tmp")
        self.assertEqual(state.peek("dm:x")[0]["model"], "")


def make_bridge():
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.prefs = None
    bridge.settings = SimpleNamespace(policy=SimpleNamespace(mode="trusted", turn_timeout_sec=60, workspaces=[]),
                                      progress_first_notice_sec=90, progress_escalate_sec=0)
    bridge.sent = []
    bridge._say = lambda chat_id, text: bridge.sent.append(text) or True
    bridge._say_with_buttons = lambda chat_id, text, buttons: bridge.sent.append(text) or True
    return bridge


class ArmTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(app_mod.workspace_mod, "short_name", lambda path, policy: "repo")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_explicit_model_shown_and_stored(self):
        bridge = make_bridge()
        bridge._arm_new_session("dm:x", "oc", "codex", "/tmp", "gpt-6-luna")
        self.assertEqual(bridge._newsession_state().peek("dm:x")[0]["model"], "gpt-6-luna")
        self.assertIn("gpt-6-luna", bridge.sent[-1])

    def test_default_model_hint(self):
        bridge = make_bridge()
        bridge._arm_new_session("dm:x", "claude", "claude", "/tmp")
        self.assertEqual(bridge._newsession_state().peek("dm:x")[0]["model"], "")
        self.assertIn("模型：默认", bridge.sent[-1])

    def test_carried_model_only_for_same_tool(self):
        bridge = make_bridge()
        bridge._newsession_models()["dm:x"] = ("claude", "claude-opus-5-5")
        bridge._arm_new_session("dm:x", "oc", "claude", "/tmp")
        self.assertEqual(bridge._newsession_state().peek("dm:x")[0]["model"], "claude-opus-5-5")
        bridge._newsession_models()["dm:x"] = ("claude", "claude-opus-5-5")
        bridge._arm_new_session("dm:x", "oc", "codex", "/tmp")
        self.assertEqual(bridge._newsession_state().peek("dm:x")[0]["model"], "")
        self.assertNotIn("dm:x", bridge._newsession_models())


class FakeWaitCard:
    def __init__(self, *args, **kwargs):
        pass

    async def run(self, stop):
        await stop.wait()

    async def finish(self, result):
        return None


class StartPassesModelTest(unittest.TestCase):
    def test_model_reaches_control_start(self):
        bridge = make_bridge()
        calls = []

        def start(tool, cwd, text, **kwargs):
            calls.append((tool, cwd, text, kwargs))
            return SimpleNamespace(ok=True, session_id="s-new", text="好的")

        target = SimpleNamespace(label="Codex · repo", tool="codex", id="s-new")
        bridge.fleet = SimpleNamespace(start=start, set_pending=lambda scope, tool, sid: target)
        bridge._fleet_inflight = {}
        bridge._paused = {}
        bridge.reviewer = None
        bridge.replier = SimpleNamespace(send=lambda chat_id, text: bridge.sent.append(text) or True)
        bridge._grant_by_open_click = lambda *a: None
        bridge._sync_talk_marker = lambda scope: None
        bridge._send_buttons = lambda *a: None
        bridge._talk_buttons = lambda *a: []
        bridge._newsession_state().arm("dm:x", "codex", "/tmp", "gpt-6-luna")
        with mock.patch.object(app_mod, "_FleetWaitCard", FakeWaitCard):
            handled = asyncio.run(bridge._maybe_start_new_session("dm:x", "oc", "你好"))
        self.assertTrue(handled)
        self.assertEqual(calls[0][0], "codex")
        self.assertEqual(calls[0][3]["model"], "gpt-6-luna")


if __name__ == "__main__":
    unittest.main()
