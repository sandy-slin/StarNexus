"""1.4.0 /m 常用功能卡，M-01 ~ M-14：三种场景出卡、按钮作用范围、旧命令兼容、文案统一。"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge import menu
from bridge.fleet import sessperm
from bridge.fleet.statuscard import CardInfo, MOVED_CARD, StatusCardKeeper, StatusCards, build_card
from bridge.policy import Policy


def policy(mode="trusted", ceiling="trusted"):
    return Policy(mode=mode, workspaces=("/tmp",), default_workspace="/tmp", model="claude-sonnet-5-5",
                  turn_timeout_sec=60, ceiling_mode=ceiling)


def walk(node, tag):
    out = []
    if isinstance(node, dict):
        if node.get("tag") == tag:
            out.append(node)
        for v in node.values():
            out += walk(v, tag)
    elif isinstance(node, list):
        for v in node:
            out += walk(v, tag)
    return out


def button_texts(card):
    return [b["text"]["content"] for b in walk(card, "button")]


class FakeFeishu:
    def __init__(self):
        self.sent, self.patched, self.pinned = [], [], []

    def send(self, chat_id, card):
        mid = f"om_{len(self.sent) + 1}"
        self.sent.append((chat_id, card, mid))
        return mid

    def patch(self, mid, card):
        self.patched.append((mid, card))
        return True

    def pin(self, mid):
        self.pinned.append(mid)
        return True, 0, ""


class DmCardTest(unittest.TestCase):
    def card(self, pol=None):
        return json.loads(menu.build_menu_card("~/work", pol or policy(), [("Claude", "缺省"), ("Codex", "gpt-6-sol")]))

    def test_m01_first_rows_sessions_and_new(self):
        rows = walk(self.card()["body"]["elements"], "column_set")
        first = [c["elements"][0]["text"]["content"] for c in rows[0]["columns"]]
        self.assertEqual(first, ["🖥 看看会话", "➕ 新建会话"])

    def test_m02_default_models_and_global_perm_on_card(self):
        card = self.card()
        texts = button_texts(card)
        for want in ("Claude", "Codex", "opencode", "只读", "可写", "✅ 全部授权"):
            self.assertIn(want, texts)
        blob = json.dumps(card, ensure_ascii=False)
        self.assertIn("Codex gpt-6-sol", blob)
        perm_vals = [b["behaviors"][0]["value"] for b in walk(card, "button") if b["behaviors"][0]["value"].get("pm")]
        self.assertEqual([v["pm"] for v in perm_vals], ["readonly", "write", "trusted"])

    def test_m03_more_panel_has_the_rest(self):
        panel = walk(self.card(), "collapsible_panel")[0]
        inside = button_texts(panel)
        for want in ("📂 换项目", "↑ 最近问过", "ℹ️ 现在状态", "🩺 运行诊断", "⬆️ 版本和更新", "❓ 帮助"):
            self.assertIn(want, inside)
        self.assertTrue(any(q in inside for _, q in menu.QUICK_QUESTIONS))
        outside = [t for t in button_texts(self.card()) if t not in inside]
        self.assertLessEqual(len(outside), 8)

    def test_m04_shortcuts_footer(self):
        blob = json.dumps(self.card(), ensure_ascii=False)
        for cmd in ("/ns", "/p r|w|f", "/stop", "/sessions"):
            self.assertIn(cmd, blob)
        self.assertIn(menu.ASK_M, blob)

    def test_m05_ceiling_limits_perm_buttons(self):
        texts = button_texts(self.card(policy("readonly", ceiling="write")))
        self.assertIn("可写", texts)
        self.assertNotIn("全部授权", texts)

    def test_m06_card_values_are_whitelisted(self):
        for b in walk(self.card(), "button"):
            v = b["behaviors"][0]["value"]
            if v.get("act") == "menu":
                self.assertTrue(menu.resolve_click(v), v)
        self.assertEqual(menu.resolve_click({"act": "menu", "pm": "write"}), "/mode! write")
        self.assertEqual(menu.resolve_click({"act": "menu", "pm": "root"}), "")
        self.assertEqual(menu.resolve_click({"act": "menu", "cmd": "/update"}), "/update")


class GroupCardTest(unittest.TestCase):
    def info(self, perm="trusted"):
        return CardInfo(tool="codex", sid="c1", title="修登录", project="rc", model="gpt-6-sol",
                        mode=sessperm.LABELS[perm], status="idle", perm=perm,
                        perm_modes=("readonly", "write", "trusted"))

    def test_m07_status_card_has_session_perm_row(self):
        card = json.loads(build_card(self.info("write")))
        vals = [b["behaviors"][0]["value"] for b in walk(card, "button") if b["behaviors"][0]["value"].get("act") == "sp"]
        self.assertEqual(vals, [{"act": "sp", "m": m, "src": "sc"} for m in sessperm.MODES])
        self.assertIn("✅ 可写", button_texts(card))
        for want in ("🧠 模型", "⏹ 停下", "✅ 完成"):
            self.assertIn(want, button_texts(card))
        self.assertEqual(sessperm.resolve_click(vals[0]), "readonly")
        self.assertIsNone(sessperm.resolve_click({"act": "sp", "m": "readonly", "src": "x"}))

    def test_m08_resend_moves_card_and_retires_old(self):
        fs = FakeFeishu()
        keeper = StatusCardKeeper(StatusCards(os.path.join(tempfile.mkdtemp(), "s.json")), fs.send, fs.patch, fs.pin)
        keeper.ensure("oc_g", self.info())
        self.assertEqual(keeper.resend("oc_g", self.info()), "moved_pinned")
        self.assertEqual(len(fs.sent), 2)
        self.assertEqual(fs.patched[-1], ("om_1", MOVED_CARD))
        self.assertEqual(keeper.store.get("oc_g")["message_id"], "om_2")
        self.assertEqual(walk(json.loads(MOVED_CARD), "button"), [])
        keeper.ensure("oc_g", self.info())
        self.assertEqual(len(fs.sent), 2)


def make_bridge(tmp, bound=None, pol=None):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.prefs = None
    bridge.settings = SimpleNamespace(policy=pol or policy(), owner_open_ids=frozenset({"ou_me"}), config_path="")
    bridge.session_perms = sessperm.SessionPerms(os.path.join(tmp, "p.json"))
    bridge.chat_binds = SimpleNamespace(of_chat=lambda cid: bound if bound and cid == bound.chat_id else None,
                                        all=lambda: [bound] if bound else [])
    bridge.fleet = SimpleNamespace(cached=lambda tool, sid: None)
    bridge.model_prefs = None
    bridge.ceo = None
    bridge.sent, bridge.cards, bridge.menus = [], [], []
    bridge._say = lambda chat_id, text: bridge.sent.append((chat_id, text)) or True
    bridge._say_later = lambda chat_id, text: bridge.sent.append((chat_id, text))
    bridge._refresh_status_card_later = lambda chat_id: None
    fs = FakeFeishu()
    bridge.fs = fs
    bridge.status_cards = StatusCardKeeper(StatusCards(os.path.join(tmp, "sc.json")), fs.send, fs.patch, fs.pin)
    return bridge


BOUND = SimpleNamespace(chat_id="oc_g", tool="codex", id="c1", on=True, name="SN·Codex·rc", scope="group:oc_g")


class RoutingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_m09_m_in_session_group_moves_status_card(self):
        bridge = make_bridge(self.tmp, BOUND)
        bridge._send_menu = lambda chat_id, scope: bridge.menus.append(chat_id)
        for word in ("/m", "菜单", "?", "/menu"):
            self.assertTrue(asyncio.run(bridge._handle_command("group:oc_g", "oc_g", word)))
        self.assertEqual(bridge.menus, [])
        self.assertEqual(len(bridge.fs.sent), 4)
        self.assertIn("这个会话的权限", bridge.fs.sent[-1][1])

    def test_m10_m_in_dm_and_other_groups_sends_global_card(self):
        bridge = make_bridge(self.tmp, BOUND)
        bridge._send_menu = lambda chat_id, scope: bridge.menus.append(chat_id)
        asyncio.run(bridge._handle_command("dm:ou_me", "oc_dm", "/m"))
        asyncio.run(bridge._handle_command("group:oc_ceo", "oc_ceo", "/m"))
        self.assertEqual(bridge.menus, ["oc_dm", "oc_ceo"])
        self.assertEqual(bridge.fs.sent, [])

    def test_m11_status_card_perm_click_changes_only_this_session(self):
        bridge = make_bridge(self.tmp, BOUND)
        event = SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_g"))
        out = bridge._on_session_perm_click(event, "ou_me", {"act": "sp", "m": "readonly", "src": "sc"})
        self.assertEqual(out.toast.type, "success")
        self.assertEqual(bridge.session_perms.mode("codex", "c1"), "readonly")
        self.assertEqual(bridge.policy.mode, "trusted")
        self.assertIn("✅ 只读", json.dumps(out.card.data, ensure_ascii=False))
        self.assertIn("会话状态卡", json.dumps(out.card.data, ensure_ascii=False))

    def test_m12_global_perm_click_applies_in_one_step(self):
        from bridge.prefs import PrefsStore

        bridge = make_bridge(self.tmp, None, policy("readonly"))
        bridge.prefs = PrefsStore(os.path.join(self.tmp, "prefs.json"), bridge.settings.policy)
        out = bridge._menu_switch_in_place({"act": "menu", "pm": "trusted"})
        self.assertIsNotNone(out)
        toast, card, said = out
        self.assertEqual(bridge.policy.mode, "trusted")
        self.assertIn("✅ 全部授权", card)
        self.assertIn("全局权限改成「全部授权」", said)


class CompatCopyTest(unittest.TestCase):
    def test_m13_old_commands_still_known(self):
        known = menu.known_commands()
        for cmd in ("/m", "/menu", "/mode", "/权限", "/p", "/model", "/sessions", "/newsession", "/stop",
                    "/dirs", "/history", "/help", "/update", "/诊断"):
            self.assertIn(cmd, known)
        for word in ("菜单", "?", "？", "/m"):
            self.assertTrue(menu.is_menu_request(word))

    def test_m14_help_welcome_and_typo_point_to_m(self):
        from bridge import pairing

        self.assertTrue(menu.HELP.startswith("**不知道怎么做就发 /m**"))
        self.assertIn("发 /m", pairing.bound_message("trusted"))
        self.assertIn("发 /m", pairing.greeting_message("readonly"))
        line, buttons = menu.unknown_command_reply("/sesions")
        self.assertIn(menu.ASK_M, line)
        self.assertEqual(buttons[-1]["text"]["content"], "📋 菜单")


if __name__ == "__main__":
    unittest.main()
