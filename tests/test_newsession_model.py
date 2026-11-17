import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from bridge import app as app_mod
from bridge.fleet.newsession import COMMANDS, START_TOOLS, NewSessionState, pick_model
from bridge.fleet.chatbind import group_name
from bridge.policy import Policy
from bridge.workspace import Outcome, create_missing_workspace


class PickModelTest(unittest.TestCase):
    def test_short_command_and_visible_tools(self):
        self.assertIn("/ns", COMMANDS)
        self.assertEqual(START_TOOLS, ("claude", "codex", "opencode"))

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

    def test_effort_aliases_per_tool(self):
        from bridge.fleet.newsession import pick_effort

        self.assertEqual(pick_effort("claude", "high"), "high")
        self.assertEqual(pick_effort("claude", "高"), "high")
        self.assertEqual(pick_effort("claude", "ultra"), "")
        self.assertEqual(pick_effort("codex", "ultra"), "ultra")
        self.assertEqual(pick_effort("codex", "remote_control"), "")

    def test_state_keeps_effort(self):
        state = NewSessionState()
        state.arm("dm:x", "claude", "/tmp", "claude-opus-5-5", "high")
        armed, _ = state.peek("dm:x")
        self.assertEqual((armed["model"], armed["effort"]), ("claude-opus-5-5", "high"))

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

    def test_explicit_model_and_effort_shown(self):
        bridge = make_bridge()
        bridge._arm_new_session("dm:x", "oc", "claude", "/tmp", "claude-opus-5-5", "high")
        armed = bridge._newsession_state().peek("dm:x")[0]
        self.assertEqual((armed["model"], armed["effort"]), ("claude-opus-5-5", "high"))
        self.assertIn("claude-opus-5-5 / 高", bridge.sent[-1])

    def test_carried_effort_for_same_tool(self):
        bridge = make_bridge()
        bridge._newsession_models()["dm:x"] = ("claude", "claude-opus-5-5", "high")
        bridge._arm_new_session("dm:x", "oc", "claude", "/tmp")
        self.assertEqual(bridge._newsession_state().peek("dm:x")[0]["effort"], "high")

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
        bridge._open_session_group = mock.AsyncMock(return_value=None)
        bridge._newsession_state().arm("dm:x", "codex", "/tmp", "gpt-6-luna")
        with mock.patch.object(app_mod, "_FleetWaitCard", FakeWaitCard):
            handled = asyncio.run(bridge._maybe_start_new_session("dm:x", "oc", "你好"))
        self.assertTrue(handled)
        self.assertEqual(calls[0][0], "codex")
        self.assertEqual(calls[0][3]["model"], "gpt-6-luna")
        bridge._open_session_group.assert_awaited_once_with("oc", "dm:x", "x", "codex", "s-new")


class MissingWorkspaceTest(unittest.TestCase):
    def test_creates_one_missing_child_inside_allowed_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            policy = Policy("trusted", (root,), root, "", 60, "trusted")
            target = root + "/new-session"
            result = create_missing_workspace(policy, target)
            self.assertEqual(result.outcome, Outcome.OK)
            self.assertTrue(__import__("os").path.isdir(target))

    def test_rejects_outside_without_creating(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            policy = Policy("trusted", (root,), root, "", 60, "trusted")
            target = outside + "/should-not-exist"
            result = create_missing_workspace(policy, target)
            self.assertEqual(result.outcome, Outcome.OUTSIDE)
            self.assertFalse(__import__("os").path.exists(target))


class GroupNameTest(unittest.TestCase):
    def test_uses_sn_prefix_tool_title_format(self):
        self.assertEqual(group_name("codex", "修登录", "abcdef"), "SN·Codex·修登录")

    def test_includes_project_basename(self):
        self.assertEqual(group_name("codex", "修登录", "abcdef", project="/Users/x/work/remote_control/"),
                         "SN·Codex·remote_control·修登录")

    def test_duplicate_name_gets_short_id(self):
        first = group_name("claude", "修登录", "abcdef12", project="/p/app")
        second = group_name("claude", "修登录", "99887766", project="/p/app", taken=[first])
        self.assertEqual(first, "SN·Claude·app·修登录")
        self.assertEqual(second, "SN·Claude·app·修登录 #998877")

    def test_project_separator_is_sanitized(self):
        self.assertEqual(group_name("codex", "t", "a", project="/p/a·b"), "SN·Codex·a-b·t")


if __name__ == "__main__":
    unittest.main()


class NameAndPathTest(unittest.TestCase):
    def test_name_then_full_path(self):
        import tempfile

        bridge = make_bridge()
        bridge.fleet = object()
        root = tempfile.mkdtemp()
        bridge.settings.policy = Policy("trusted", (root,), root, "", 60, "trusted")
        bridge._new_current_dir = lambda scope: root
        armed = []
        bridge._arm_new_session = lambda scope, chat_id, tool, cwd, model="", effort="", name="": armed.append(
            (tool, cwd, name))
        handled = asyncio.run(bridge._handle_newsession_command("dm:x", "oc", f"/ns claude ai-native {root}"))
        self.assertTrue(handled)
        self.assertEqual(armed, [("claude", os.path.realpath(root), "ai-native")])
        self.assertFalse(any("完整路径" in t for t in bridge.sent))

    def test_state_keeps_name(self):
        state = NewSessionState()
        state.arm("dm:x", "claude", "/tmp", name="ai-native")
        self.assertEqual(state.peek("dm:x")[0]["name"], "ai-native")
