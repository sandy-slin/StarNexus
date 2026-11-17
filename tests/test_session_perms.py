"""1.3.0 会话级权限 + 模型下拉，SP-01 ~ SP-16。"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet import sessperm
from bridge.fleet.sessperm import SessionPerms
from bridge.policy import Policy


def policy(mode="readonly", ceiling="trusted"):
    return Policy(mode=mode, workspaces=("/tmp",), default_workspace="/tmp", model="claude-sonnet-5-5",
                  turn_timeout_sec=60, ceiling_mode=ceiling)


def bridge_with(tmp, pol=None, bound=None):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.prefs = None
    pol = pol or policy()
    bridge.settings = SimpleNamespace(policy=pol, owner_open_ids=frozenset({"ou_owner"}),
                                      progress_first_notice_sec=90, progress_escalate_sec=0)
    bridge.session_perms = SessionPerms(os.path.join(tmp, "session_perms.json"))
    bridge.chat_binds = SimpleNamespace(of_chat=lambda cid: bound if bound and cid == bound.chat_id else None)
    bridge.sent, bridge.cards = [], []
    bridge._say = lambda chat_id, text: bridge.sent.append((chat_id, text)) or True
    bridge._say_later = lambda chat_id, text: bridge.sent.append((chat_id, text))
    bridge._send_card_or_text = lambda chat_id, card, fb: bridge.cards.append((chat_id, json.loads(card)))
    bridge._refresh_status_card_later = lambda chat_id: None
    return bridge


BOUND = SimpleNamespace(chat_id="oc_g", tool="codex", id="t-1", name="SN·Codex·repo", scope="group:oc_g")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "p.json")

    def test_sp01_unset_follows_global(self):
        self.assertEqual(SessionPerms(self.path).mode("codex", "x"), "")

    def test_sp02_set_persists_and_reloads(self):
        SessionPerms(self.path).set("codex", "x", "write")
        self.assertEqual(SessionPerms(self.path).mode("codex", "x"), "write")

    def test_sp03_feishu_origin_kept_after_switch(self):
        store = SessionPerms(self.path)
        store.mark_feishu("claude", "y")
        self.assertEqual(store.mode("claude", "y"), "trusted")
        store.set("claude", "y", "readonly")
        self.assertTrue(store.from_feishu("claude", "y"))
        self.assertEqual(store.mode("claude", "y"), "readonly")

    def test_sp04_bad_mode_and_corrupt_file(self):
        store = SessionPerms(self.path)
        self.assertFalse(store.set("codex", "x", "root"))
        with open(self.path, "w") as fh:
            fh.write("{broken")
        self.assertEqual(SessionPerms(self.path).mode("codex", "x"), "")

    def test_sp05_aliases(self):
        for word, want in (("只读", "readonly"), ("可写", "write"), ("全部授权", "trusted"), ("全授权", "trusted")):
            self.assertEqual(sessperm.parse_mode(word), want)
        self.assertIsNone(sessperm.parse_mode("随便"))


class CardTest(unittest.TestCase):
    def test_sp06_three_buttons_current_marked(self):
        card = sessperm.card("write", "SN·Codex·repo")
        blob = json.dumps(card, ensure_ascii=False)
        for word in ("只读", "✅ 可写", "全部授权", "/权限 只读"):
            self.assertIn(word, blob)
        values = [c["elements"][0]["behaviors"][0]["value"] for c in card["body"]["elements"][1]["columns"]]
        self.assertEqual(values, [{"act": "sp", "m": m} for m in sessperm.MODES])

    def test_sp07_click_whitelist(self):
        self.assertEqual(sessperm.resolve_click({"act": "sp", "m": "write"}), "write")
        self.assertIsNone(sessperm.resolve_click({"act": "sp", "m": "write", "sid": "x"}))
        self.assertIsNone(sessperm.resolve_click({"act": "sp", "m": "sudo"}))


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_sp08_session_mode_overrides_global(self):
        bridge = bridge_with(self.tmp, policy("trusted"))
        bridge.session_perms.set("codex", "t-1", "readonly")
        self.assertEqual(bridge._session_policy("codex", "t-1").mode, "readonly")
        self.assertEqual(bridge._session_policy("codex", "other").mode, "trusted")

    def test_sp09_ceiling_still_caps(self):
        bridge = bridge_with(self.tmp, policy("readonly", ceiling="write"))
        bridge.session_perms.set("codex", "t-1", "trusted")
        self.assertEqual(bridge._session_policy("codex", "t-1").mode, "write")
        self.assertEqual(app_mod._full_access(bridge.policy).mode, "write")

    def test_sp10_feishu_sessions_full_access(self):
        self.assertEqual(app_mod._full_access(policy("readonly")).mode, "trusted")


class GroupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_sp11_group_command_changes_only_this_session(self):
        bridge = bridge_with(self.tmp, policy("trusted"), BOUND)
        bridge._handle_session_mode("oc_g", "/权限 只读")
        self.assertEqual(bridge.session_perms.mode("codex", "t-1"), "readonly")
        self.assertEqual(bridge.policy.mode, "trusted")
        self.assertIn("只读", bridge.sent[-1][1])

    def test_sp12_group_command_without_arg_sends_card(self):
        bridge = bridge_with(self.tmp, policy("trusted"), BOUND)
        bridge._handle_session_mode("oc_g", "/权限")
        self.assertIn("跟随全局", json.dumps(bridge.cards[-1][1], ensure_ascii=False))

    def test_sp13_clamped_switch_says_why(self):
        bridge = bridge_with(self.tmp, policy("readonly", ceiling="write"), BOUND)
        text = bridge._apply_session_mode(BOUND, "trusted")
        self.assertIn("最多放到「可写」", text)

    def test_sp14_offer_on_group_create_only_for_desktop_sessions(self):
        bridge = bridge_with(self.tmp, policy("trusted"), BOUND)
        bridge._offer_session_perm("oc_g", "codex", "t-1", "x")
        self.assertEqual(len(bridge.cards), 1)
        bridge.session_perms.mark_feishu("codex", "t-1")
        bridge._offer_session_perm("oc_g", "codex", "t-1", "x")
        self.assertEqual(len(bridge.cards), 1)

    def test_sp15_click_needs_owner_and_group(self):
        bridge = bridge_with(self.tmp, policy("trusted"), BOUND)
        event = SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_g"))
        bad = bridge._on_session_perm_click(event, "ou_other", {"act": "sp", "m": "readonly"})
        self.assertEqual(bad.toast.type, "error")
        self.assertEqual(bridge.session_perms.mode("codex", "t-1"), "")
        ok = bridge._on_session_perm_click(event, "ou_owner", {"act": "sp", "m": "write"})
        self.assertEqual(ok.toast.type, "success")
        self.assertEqual(bridge.session_perms.mode("codex", "t-1"), "write")
        elsewhere = bridge._on_session_perm_click(SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_dm")),
                                                   "ou_owner", {"act": "sp", "m": "readonly"})
        self.assertEqual(elsewhere.toast.type, "error")


class ModelDropdownTest(unittest.TestCase):
    def test_sp16_list_card_uses_selects(self):
        from bridge.fleet.catalog import Choice
        from bridge.fleet.modelcard import list_card, resolve_click

        catalog = SimpleNamespace(names=lambda tool: ("gpt-6-sol", "gpt-6-luna"),
                                  efforts=lambda tool, name="": ("low", "high"))
        prefs = SimpleNamespace(catalog=catalog, default=lambda tool: Choice("gpt-6-luna", "high"),
                                session=lambda tool, sid: Choice(), effective=lambda tool, sid="": Choice())
        card = json.loads(list_card(prefs, "d", "codex"))
        selects = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("tag") == "select_static":
                    selects.append(node)
                if node.get("tag") == "button":
                    self.assertNotIn(node["behaviors"][0]["value"].get("op"), ("m", "e"))
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
        walk(card)
        self.assertEqual(len(selects), 2)
        self.assertEqual(selects[0]["initial_option"], "m1")
        self.assertEqual(selects[1]["initial_option"], "high")
        self.assertNotIn("gpt-6", json.dumps([s["behaviors"] for s in selects]))
        self.assertEqual(resolve_click(selects[0]["behaviors"][0]["value"], "m0")["i"], 0)
        self.assertEqual(resolve_click(selects[1]["behaviors"][0]["value"], "low")["e"], "low")
        self.assertIsNone(resolve_click(selects[0]["behaviors"][0]["value"], "rm -rf"))


if __name__ == "__main__":
    unittest.main()
