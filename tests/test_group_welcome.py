import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from bridge import app as app_mod
from bridge.fleet import chatbind
from bridge.fleet.chatbind import CREATED, REUSED, Bound, GroupResult


def make_bridge(granted=True):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.said = []
    sess = SimpleNamespace(title="车联", cwd="/tmp", tool="claude", id="s1")
    bridge.fleet = SimpleNamespace(cached=lambda tool, sid: sess, set_pending=lambda *a: None)
    bridge.chat_binds = SimpleNamespace(turn_on=mock.Mock())
    bridge.group_client = None
    bridge._relay_granted = lambda *a: granted
    bridge._grant_by_open_click = mock.Mock()
    bridge._say_with_buttons = lambda chat_id, text, buttons: bridge.said.append((chat_id, text))
    bridge._say = lambda chat_id, text: bridge.said.append((chat_id, text))
    bridge._rights_line = lambda tool, sid: "· 这个会话的权限：**全部授权**\n"
    bridge._talk_buttons = lambda *a: []
    bridge._group_perm_report = lambda: None
    bridge._group_msg_proven = lambda: True
    bridge._offer_session_perm = mock.Mock()
    bridge._refresh_status_card = mock.Mock()
    bridge._ask_relay_consent = mock.Mock()

    async def enter(chat_id):
        return None

    bridge._group_enter_button = enter
    return bridge


def run(bridge, how, on=True):
    bound = Bound("oc_g", "claude", "s1", "SN·Claude·车联", 0.0, on)
    with mock.patch.object(chatbind, "ensure_group", return_value=GroupResult(how, bound)):
        asyncio.run(bridge._open_session_group("oc_dm", "dm:x", "ou", "claude", "s1", jumped=True))


class GroupWelcomeTest(unittest.TestCase):
    def test_reused_group_does_not_repeat_welcome(self):
        bridge = make_bridge()
        run(bridge, REUSED)
        self.assertEqual([t for c, t in bridge.said if c == "oc_g"], [])
        bridge._refresh_status_card.assert_called_once_with("oc_g")

    def test_reused_group_still_records_consent_silently(self):
        bridge = make_bridge(granted=False)
        run(bridge, REUSED)
        bridge._grant_by_open_click.assert_called_once()
        self.assertEqual([t for c, t in bridge.said if c == "oc_g"], [])
        bridge._ask_relay_consent.assert_not_called()

    def test_reopened_group_says_welcome_again(self):
        bridge = make_bridge()
        run(bridge, REUSED, on=False)
        bridge.chat_binds.turn_on.assert_called_once_with("oc_g")
        texts = [t for c, t in bridge.said if c == "oc_g"]
        self.assertEqual(len(texts), 1)
        self.assertIn("现在就能说", texts[0])

    def test_new_group_says_welcome_once(self):
        bridge = make_bridge()
        run(bridge, CREATED)
        texts = [t for c, t in bridge.said if c == "oc_g"]
        self.assertEqual(len(texts), 1)
        self.assertIn("这个会话的权限", texts[0])


class RightsLineTest(unittest.TestCase):
    def make(self, mode, background=False):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.fleet = SimpleNamespace(cached=lambda tool, sid: SimpleNamespace())
        bridge._session_policy = lambda tool, sid: SimpleNamespace(mode=mode)
        bridge._runs_in_background = lambda sess: background
        return bridge

    def test_claude_uses_session_permission_not_old_shared_wording(self):
        line = self.make("trusted")._rights_line("claude", "s1")
        self.assertIn("这个会话的权限：**全部授权**", line)
        self.assertNotIn("共用", line)

    def test_readonly_and_background(self):
        self.assertIn("只读", self.make("readonly")._rights_line("codex", "s1"))
        line = self.make("write", background=True)._rights_line("claude", "s1")
        self.assertIn("后台接着跑", line)
        self.assertIn("可写", line)


if __name__ == "__main__":
    unittest.main()
