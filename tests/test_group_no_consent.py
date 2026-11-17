import asyncio
import unittest
from types import SimpleNamespace

from bridge import app as app_mod


class FakeControl:
    def __init__(self, sess):
        self.sess = sess
        self.pending = {}

    def refresh(self, tool, sid):
        return self.sess, ""

    def cached(self, tool, sid):
        return self.sess

    def peek_pending_fresh(self, scope):
        return self.pending.get(scope), False

    def set_pending(self, scope, tool, sid):
        target = SimpleNamespace(tool=tool, id=sid)
        self.pending[scope] = target
        return target

    def clear_pending(self, scope):
        self.pending.pop(scope, None)


class BoundGroupNoConsentCardTest(unittest.TestCase):
    def test_owner_message_goes_straight_in_without_confirm_card(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        sess = SimpleNamespace(tool="codex", id="s1", status="idle", name="规划", cwd="/tmp", owner="none")
        bridge.fleet = FakeControl(sess)
        bound = SimpleNamespace(tool="codex", id="s1", chat_id="oc_g", scope="group:oc_g", on=True, name="SN·Codex")
        bridge.chat_binds = SimpleNamespace(active_of_scope=lambda scope: bound)
        granted = []
        bridge.relay_consent = SimpleNamespace(
            granted=lambda *a: False,
            grant=lambda *a: granted.append(a),
            latest_ident=lambda *a: None,
        )
        asked, said = [], []
        bridge._ask_relay_consent = lambda *a: asked.append(a)
        bridge._say = lambda chat_id, text: said.append(text) or True
        bridge._say_with_buttons = lambda chat_id, text, buttons: said.append(text) or True

        handled = asyncio.run(bridge._group_bind_arm("group:oc_g", "oc_g", "你好"))

        self.assertFalse(handled)
        self.assertEqual(asked, [])
        self.assertEqual(len(granted), 1)
        self.assertIn("group:oc_g", bridge.fleet.pending)
        self.assertFalse(any("确认" in text for text in said))


class BoundGroupClickNoConsentCardTest(unittest.TestCase):
    def test_click_session_in_bound_group_enters_talk_without_confirm_card(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        sess = SimpleNamespace(tool="claude", id="c2", status="idle", name="lr-live", cwd="/tmp", owner="terminal")
        bridge.fleet = FakeControl(sess)
        bound = SimpleNamespace(tool="claude", id="c2", chat_id="oc_g", on=True, name="SN·Claude·lr-live")
        bridge.chat_binds = SimpleNamespace(of_chat=lambda chat: bound)
        store = {"granted": False}
        bridge._relay_granted = lambda scope, tool, sid: store["granted"]
        bridge._grant_by_open_click = lambda scope, tool, sid: store.update(granted=True)
        bridge._group_bind_click = lambda *a: None
        bridge._sync_talk_marker = lambda scope: None
        bridge._enter_talk_text = lambda target, scope="": "正在和它说话"
        bridge._talk_buttons = lambda tool, sid: []
        asked, said = [], []
        bridge._ask_relay_consent = lambda *a: asked.append(a)
        bridge._say_with_buttons = lambda chat_id, text, buttons: said.append(text) or True

        bridge._group_session_click("oc_g", "group:oc_g", "claude", "c2")

        self.assertEqual(asked, [])
        self.assertTrue(store["granted"])
        self.assertIn("group:oc_g", bridge.fleet.pending)
        self.assertEqual(said, ["正在和它说话"])

    def test_unbound_group_click_still_asks_consent(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        sess = SimpleNamespace(tool="claude", id="c2", status="idle", name="lr-live", cwd="/tmp", owner="terminal")
        bridge.fleet = FakeControl(sess)
        bridge.chat_binds = SimpleNamespace(of_chat=lambda chat: None)
        bridge._relay_granted = lambda scope, tool, sid: False
        bridge._grant_by_open_click = lambda *a: self.fail("普通群不该自动确认")
        bridge._group_bind_click = lambda *a: None
        asked = []
        bridge._ask_relay_consent = lambda *a: asked.append(a)

        bridge._group_session_click("oc_plain", "group:oc_plain", "claude", "c2")

        self.assertEqual(len(asked), 1)
        self.assertNotIn("group:oc_plain", bridge.fleet.pending)


if __name__ == "__main__":
    unittest.main()
