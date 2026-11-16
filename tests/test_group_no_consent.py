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


if __name__ == "__main__":
    unittest.main()
