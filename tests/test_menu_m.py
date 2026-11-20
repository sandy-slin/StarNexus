"""1.4.0/1.4.1 /h 常用功能卡，M-01 ~ M-20：三种场景出卡、按钮作用范围、旧命令兼容、文案统一。"""

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
    PICK = {"tool": "codex", "tools": ["claude", "codex", "opencode"], "models": ["gpt-6-sol", "gpt-6-luna"],
            "model": "gpt-6-luna", "efforts": ["low", "high"], "effort": "high"}

    def card(self, pol=None, pick=PICK):
        return json.loads(menu.build_menu_card("~/work", pol or policy(), [("Claude", "缺省"), ("Codex", "gpt-6-sol")],
                                               pick))

    def test_m01_first_rows_sessions_and_new(self):
        rows = walk(self.card()["body"]["elements"], "column_set")
        first = [c["elements"][0]["text"]["content"] for c in rows[0]["columns"]]
        self.assertEqual(first, ["🖥 看看会话", "➕ 新建会话"])

    def test_m02_settings_are_dropdowns_with_current_value(self):
        card = self.card()
        selects = walk(card, "select_static")
        by_op = {}
        for node in selects:
            v = node["behaviors"][0]["value"]
            by_op[v.get("op") or ("pm" if "pm" in v else "?")] = node
        self.assertEqual(set(by_op), {"tool", "model", "effort", "pm"})
        self.assertEqual(by_op["tool"]["initial_option"], "codex")
        self.assertEqual(by_op["model"]["initial_option"], "m1")
        self.assertEqual(by_op["effort"]["initial_option"], "high")
        self.assertEqual(by_op["pm"]["initial_option"], "trusted")
        self.assertEqual([o["value"] for o in by_op["pm"]["options"]], ["readonly", "write", "trusted"])
        texts = button_texts(card)
        for gone in ("只读", "可写", "全部授权", "Claude", "opencode"):
            self.assertNotIn(gone, texts)
        self.assertNotIn("gpt-6", json.dumps([n["behaviors"] for n in selects]))

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

    def test_m05_ceiling_limits_perm_options(self):
        pm = [n for n in walk(self.card(policy("readonly", ceiling="write")), "select_static")
              if "pm" in n["behaviors"][0]["value"]][0]
        self.assertEqual([o["value"] for o in pm["options"]], ["readonly", "write"])

    def test_m15_no_model_pick_falls_back_to_text(self):
        blob = json.dumps(self.card(pick=None), ensure_ascii=False)
        self.assertIn("Codex gpt-6-sol", blob)

    def test_m16_mm_click_whitelist(self):
        self.assertEqual(menu.resolve_mm_click({"act": "mm", "op": "tool"}, "claude"),
                         {"op": "tool", "tool": "claude", "pick": "claude"})
        self.assertEqual(menu.resolve_mm_click({"act": "mm", "op": "model", "tool": "codex"}, "m3")["pick"], "m3")
        self.assertEqual(menu.resolve_mm_click({"act": "mm", "op": "effort", "tool": "codex"}, "d")["pick"], "d")
        self.assertIsNone(menu.resolve_mm_click({"act": "mm", "op": "tool"}, "cursor"))
        self.assertIsNone(menu.resolve_mm_click({"act": "mm", "op": "model", "tool": "codex"}, "gpt-6-sol"))
        self.assertIsNone(menu.resolve_mm_click({"act": "mm", "op": "model", "tool": "codex", "x": 1}, "m1"))
        self.assertIsNone(menu.resolve_mm_click({"act": "mm", "op": "effort", "tool": "codex"}, "x;rm"))

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

    def test_m07_status_card_has_session_perm_dropdown(self):
        card = json.loads(build_card(self.info("write")))
        selects = walk(card, "select_static")
        self.assertEqual(len(selects), 1)
        self.assertEqual(selects[0]["behaviors"][0]["value"], {"act": "sp", "m": "?", "src": "sc"})
        self.assertEqual(selects[0]["initial_option"], "write")
        self.assertNotIn("**权限**：", json.dumps(card, ensure_ascii=False))
        texts = button_texts(card)
        for want in ("🧠 模型", "⏹ 停下", "✅ 完成"):
            self.assertIn(want, texts)
        for gone in ("只读", "可写", "全部授权"):
            self.assertNotIn(gone, texts)
        rows = walk(card, "column_set")
        self.assertTrue(all(len(r["columns"]) <= 2 for r in rows))
        val = selects[0]["behaviors"][0]["value"]
        self.assertEqual(sessperm.resolve_click(val, "readonly"), "readonly")
        self.assertIsNone(sessperm.resolve_click(val, "root"))
        self.assertIsNone(sessperm.resolve_click({"act": "sp", "m": "readonly", "src": "x"}))
        self.assertEqual(sessperm.resolve_click({"act": "sp", "m": "write"}), "write")

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
        for word in ("/h", "菜单", "?", "/menu"):
            self.assertTrue(asyncio.run(bridge._handle_command("group:oc_g", "oc_g", word)))
        self.assertEqual(bridge.menus, [])
        self.assertEqual(len(bridge.fs.sent), 4)
        self.assertIn("这个会话的权限", bridge.fs.sent[-1][1])

    def test_m10_m_in_dm_and_other_groups_sends_global_card(self):
        bridge = make_bridge(self.tmp, BOUND)
        bridge._send_menu = lambda chat_id, scope: bridge.menus.append(chat_id)
        asyncio.run(bridge._handle_command("dm:ou_me", "oc_dm", "/h"))
        asyncio.run(bridge._handle_command("group:oc_ceo", "oc_ceo", "/h"))
        self.assertEqual(bridge.menus, ["oc_dm", "oc_ceo"])
        self.assertEqual(bridge.fs.sent, [])

    def test_m11_status_card_perm_click_changes_only_this_session(self):
        bridge = make_bridge(self.tmp, BOUND)
        event = SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_g"), action=SimpleNamespace(option="readonly"))
        out = bridge._on_session_perm_click(event, "ou_me", {"act": "sp", "m": "?", "src": "sc"})
        self.assertEqual(out.toast.type, "success")
        self.assertEqual(bridge.session_perms.mode("codex", "c1"), "readonly")
        self.assertEqual(bridge.policy.mode, "trusted")
        sel = walk(out.card.data, "select_static")[0]
        self.assertEqual(sel["initial_option"], "readonly")
        self.assertIn("会话状态卡", json.dumps(out.card.data, ensure_ascii=False))

    def test_m12_global_perm_click_applies_in_one_step(self):
        from bridge.prefs import PrefsStore

        bridge = make_bridge(self.tmp, None, policy("readonly"))
        bridge.prefs = PrefsStore(os.path.join(self.tmp, "prefs.json"), bridge.settings.policy)
        out = bridge._menu_switch_in_place({"act": "menu", "pm": "trusted"})
        self.assertIsNotNone(out)
        toast, card, said = out
        self.assertEqual(bridge.policy.mode, "trusted")
        pm = [n for n in walk(json.loads(card), "select_static") if "pm" in n["behaviors"][0]["value"]][0]
        self.assertEqual(pm["initial_option"], "trusted")
        self.assertIn("全局权限改成「全部授权」", said)

    def test_m17_pm_dropdown_option_goes_through_whitelist(self):
        bridge = make_bridge(self.tmp, None)
        seen = []
        bridge._menu_switch_in_place = lambda value: seen.append(value) or ("t", None, "")
        event = SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_dm"), action=SimpleNamespace(option="write"))
        bridge._chat_ids = {"dm:ou_me": "oc_dm"}
        out = bridge._on_menu_click(event, "ou_me", {"act": "menu", "pm": "?"})
        self.assertEqual(seen[-1]["pm"], "write")
        bad = bridge._on_menu_click(SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_dm"),
                                                    action=SimpleNamespace(option="root")),
                                    "ou_me", {"act": "menu", "pm": "?"})
        self.assertEqual(bad.toast.type, "error")
        self.assertEqual(len(seen), 1)
        self.assertEqual(out.toast.type, "info")


class ModelDropdownWiringTest(unittest.TestCase):
    def setUp(self):
        from bridge.fleet.catalog import Choice

        self.tmp = tempfile.mkdtemp()
        self.store = {}

        class Prefs:
            catalog = SimpleNamespace(names=lambda tool: ("gpt-6-sol", "gpt-6-luna") if tool == "codex" else ("opus",),
                                      efforts=lambda tool, name="": ("low", "high") if tool == "codex" else ())

            def default(inner, tool):
                return self.store.get(tool, Choice())

            def set_default(inner, tool, model=None, effort=None):
                old = self.store.get(tool, Choice())
                self.store[tool] = Choice(old.model if model is None else model, old.effort if effort is None else effort)
                return True

        self.bridge = make_bridge(self.tmp, None)
        self.bridge.model_prefs = Prefs()
        self.bridge._ns_last_tool = lambda scope: "claude"

    def click(self, value, option):
        ev = SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_dm"), action=SimpleNamespace(option=option))
        return self.bridge._on_mm_click(ev, "ou_me", value)

    def selects(self, out):
        return {n["behaviors"][0]["value"].get("op", "pm"): n for n in walk(out.card.data, "select_static")}

    def test_m18_switch_tool_redraws_models_in_place(self):
        out = self.click({"act": "mm", "op": "tool"}, "codex")
        sel = self.selects(out)
        self.assertEqual(sel["tool"]["initial_option"], "codex")
        self.assertEqual([o["value"] for o in sel["model"]["options"]], ["d", "m0", "m1"])
        self.assertIn("effort", sel)
        self.assertEqual(self.store, {})

    def test_m19_pick_model_and_effort_sets_default(self):
        self.click({"act": "mm", "op": "model", "tool": "codex"}, "m0")
        out = self.click({"act": "mm", "op": "effort", "tool": "codex"}, "high")
        self.assertEqual((self.store["codex"].model, self.store["codex"].effort), ("gpt-6-sol", "high"))
        sel = self.selects(out)
        self.assertEqual((sel["model"]["initial_option"], sel["effort"]["initial_option"]), ("m0", "high"))
        self.click({"act": "mm", "op": "model", "tool": "codex"}, "d")
        self.assertEqual(self.store["codex"].model, "")
        self.assertTrue(any("Codex 默认" in t for _, t in self.bridge.sent))

    def test_m20_mm_click_rejects_stranger_and_bad_index(self):
        ev = SimpleNamespace(context=SimpleNamespace(open_chat_id="oc_dm"), action=SimpleNamespace(option="m0"))
        self.assertEqual(self.bridge._on_mm_click(ev, "ou_x", {"act": "mm", "op": "model", "tool": "codex"}).toast.type,
                         "error")
        out = self.click({"act": "mm", "op": "model", "tool": "codex"}, "m9")
        self.assertEqual(out.toast.type, "error")
        self.assertEqual(self.store, {})


class CompatCopyTest(unittest.TestCase):
    def test_m13_old_commands_still_known(self):
        known = menu.known_commands()
        for cmd in ("/h", "/menu", "/mode", "/权限", "/p", "/model", "/sessions", "/newsession", "/stop",
                    "/dirs", "/history", "/help", "/update", "/诊断"):
            self.assertIn(cmd, known)
        for word in ("菜单", "?", "？", "/h"):
            self.assertTrue(menu.is_menu_request(word))

    def test_m14_help_welcome_and_typo_point_to_m(self):
        from bridge import pairing

        self.assertTrue(menu.HELP.startswith("**不知道怎么做就发 /h**"))
        self.assertIn("发 /h", pairing.bound_message("trusted"))
        self.assertIn("发 /h", pairing.greeting_message("readonly"))
        line, buttons = menu.unknown_command_reply("/sesions")
        self.assertIn(menu.ASK_M, line)
        self.assertEqual(buttons[-1]["text"]["content"], "📋 菜单")


    def test_m21_redundant_aliases_removed(self):
        known = {c.lower() for c in menu.known_commands()}
        for cmd in ("/m", "/s", "/session", "/会话", "/总控", "/历史", "/最近", "/模型", "/diag", "/diagnose",
                    "/upgrade", "/ls", "/pwd", "/where", "/新建会话", "/搜会话", "/找会话", "/复制会话", "/归档",
                    "/重命名"):
            self.assertNotIn(cmd, known)
            line, _ = menu.unknown_command_reply(cmd)
            self.assertIn(menu.ASK_M, line)
        from bridge.fleet import newsession
        from bridge import workspace

        for cmd in ("/p", "/stop", "/cd", "/fork", "/archive"):
            self.assertIn(cmd, known)
        self.assertEqual(newsession.COMMANDS, {"/newsession", "/ns"})
        self.assertEqual(workspace.COMMANDS, {"/cd", "/dirs"})

    def test_m22_user_text_advertises_only_core_commands(self):
        for text in (menu.HELP, menu.SHORTCUTS):
            for gone in ("/sessions", "/dirs", "/model", "/mode!"):
                self.assertNotIn(gone, text)


if __name__ == "__main__":
    unittest.main()


class SlashMFreedTest(unittest.TestCase):
    """2026-11-20：/h 代替 /m，/m 空出来。"""

    def test_h_opens_menu_and_m_does_not(self):
        self.assertTrue(menu.is_menu_request("/h"))
        self.assertTrue(menu.is_menu_request("/H"))
        self.assertFalse(menu.is_menu_request("/m"))

    def test_m_not_a_known_command(self):
        self.assertNotIn("/m", menu.known_commands())
        self.assertIn("/h", menu.known_commands())
