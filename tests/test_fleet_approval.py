"""FC-06：会话（Claude / Codex）在群里要批准的一步 ⇒ 出审批卡 + @ 主人；同意续跑，拒绝不做。"""
import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.approval import ApprovalGate
from bridge.review import ApprovalStore


class FakeReplier:
    def __init__(self):
        self.md, self.cards = [], []

    def send_md_id(self, chat_id, text):
        self.md.append((chat_id, text))
        return "om_md"

    def send_approval_card(self, chat_id, title, body, approve_value, deny_value):
        self.cards.append((chat_id, title, body, approve_value, deny_value))
        return f"om_card{len(self.cards)}"


def make_bridge(root):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.gate = ApprovalGate(os.path.join(root, "approvals"))
    bridge.approvals = ApprovalStore(b"k" * 32, used_path=os.path.join(root, "used.json"))
    bridge.replier = FakeReplier()
    bridge.settings = SimpleNamespace(owner_open_ids=frozenset({"ou_me"}))
    bridge.said = []
    bridge._say = lambda chat_id, text: bridge.said.append((chat_id, text)) or True
    bridge._say_later = lambda chat_id, text: bridge.said.append((chat_id, text))
    bridge._loop = None
    return bridge


def drop_pending(gate, scope, command):
    directory = gate.dir_for(scope)
    from bridge.approval import action_digest

    key = action_digest("Bash", {"command": command})
    with open(os.path.join(directory, key + ".pending"), "w", encoding="utf-8") as handle:
        json.dump({"tool_name": "Bash", "tool_input": {"command": command}, "session_id": "s"}, handle)
    return key


class FleetApprovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = make_bridge(self.tmp.name)
        self.scope = "fleet:codex:t1"

    def tearDown(self):
        self.tmp.cleanup()

    def test_card_sent_with_owner_mention(self):
        drop_pending(self.bridge.gate, self.scope, "git push origin main")
        sent = self.bridge._fleet_ask_approval("group:oc_g", "oc_g", "codex", "t1", self.scope)
        self.assertEqual(sent, 1)
        self.assertIn('<at user_id="ou_me"></at>', self.bridge.replier.md[0][1])
        chat_id, _title, body, approve, deny = self.bridge.replier.cards[0]
        self.assertEqual(chat_id, "oc_g")
        self.assertIn("git push", body)
        self.assertEqual(approve["act"], "approve")
        self.assertEqual(approve["scope"], self.scope)
        self.assertIn(self.scope, self.bridge._fleet_awaiting)

    def test_nothing_pending_sends_nothing(self):
        self.assertEqual(self.bridge._fleet_ask_approval("group:oc_g", "oc_g", "codex", "t1", self.scope), 0)
        self.assertEqual(self.bridge.replier.cards, [])

    def test_new_session_pending_moves_to_session_scope(self):
        src = "fleet:claude:new-dm:ou_me"
        key = drop_pending(self.bridge.gate, src, "sudo ls")
        self.bridge._fleet_ask_approval("group:oc_g", "oc_g", "claude", "s9", src)
        dst = "fleet:claude:s9"
        self.assertTrue(os.path.exists(os.path.join(self.bridge.gate.dir_for(dst), key + ".asked")))
        self.assertEqual(self.bridge.replier.cards[0][3]["scope"], dst)

    def test_approve_writes_grant_and_schedules_continue(self):
        key = drop_pending(self.bridge.gate, self.scope, "git push origin main")
        self.bridge._fleet_ask_approval("group:oc_g", "oc_g", "codex", "t1", self.scope)
        self.bridge._loop = object()
        scheduled = []

        def fake_schedule(coro, loop):
            scheduled.append(coro)
            coro.close()

        with mock.patch.object(app_mod.asyncio, "run_coroutine_threadsafe", fake_schedule):
            resp = self.bridge._on_fleet_approval_click({"act": "approve"}, self.scope, key)
        self.assertTrue(os.path.exists(os.path.join(self.bridge.gate.dir_for(self.scope), key + ".ok")))
        self.assertEqual(len(scheduled), 1)
        self.assertIn("继续", resp.toast.content)

    def test_home_deletion_cannot_be_approved(self):
        key = drop_pending(self.bridge.gate, self.scope, "rm -rf ~")
        self.bridge._fleet_ask_approval("group:oc_g", "oc_g", "codex", "t1", self.scope)
        self.bridge._on_fleet_approval_click({"act": "approve"}, self.scope, key)
        self.assertFalse(os.path.exists(os.path.join(self.bridge.gate.dir_for(self.scope), key + ".ok")))

    def test_deny_clears_and_does_not_continue(self):
        key = drop_pending(self.bridge.gate, self.scope, "sudo ls")
        self.bridge._fleet_ask_approval("group:oc_g", "oc_g", "codex", "t1", self.scope)
        self.bridge._on_fleet_approval_click({"act": "deny"}, self.scope, key)
        self.assertNotIn(self.scope, self.bridge._fleet_awaiting)
        self.assertFalse(self.bridge._has_waiting_approval(self.scope))
        self.assertIn("不做", self.bridge.said[-1][1])

    def test_continue_keeps_grant_for_next_send(self):
        self.bridge._fleet_awaiting = {self.scope: {"chat_id": "oc_g", "chat_scope": "group:oc_g",
                                                    "tool": "codex", "sid": "t1"}}
        seen = []
        self.bridge.fleet = SimpleNamespace(set_pending=lambda *a: None)

        async def fake_send(scope, chat_id, text):
            seen.append((scope, text, self.scope in self.bridge._fleet_keep_grants))
            return True

        self.bridge._maybe_send_to_fleet = fake_send
        asyncio.run(self.bridge._fleet_continue_after_approval(self.scope))
        self.assertEqual(seen[0][0], "group:oc_g")
        self.assertIn("重新执行", seen[0][1])
        self.assertTrue(seen[0][2])
        self.assertNotIn(self.scope, self.bridge._fleet_keep_grants)


if __name__ == "__main__":
    unittest.main()
