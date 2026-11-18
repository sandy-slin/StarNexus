"""1.1.0 一张卡开新会话（NS-01 ~ NS-16，见 docs/product/full-control.md §FC-16）。"""

import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from bridge import app as app_mod
from bridge.fleet import nscard
from bridge.fleet.catalog import Choice
from bridge.fleet.newsession import NewSessionState, pick_effort, pick_model


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _tags(card, tag):
    return [n for n in _walk(card) if n.get("tag") == tag]


def _rows(card):
    return [n for n in card["body"]["elements"] if n.get("tag") == "column_set"]


def _draft(tool="claude", projects=None, models=None):
    store = nscard.DraftStore()
    return store, store.new("dm:ou_me", tool, projects if projects is not None else
                            [("/w/a", "a"), ("/w/b", "b")], models or ["m-1", "m-2"])


class CardLayoutTest(unittest.TestCase):
    def build(self, draft, efforts=("low", "high"), default_model="m-1", default_effort=""):
        return nscard.build_card(draft, ("claude", "codex", "opencode"), default_model=default_model,
                                 efforts=efforts, default_effort=default_effort)

    def test_ns01_three_rows_tool_project_model_effort_text_start(self):
        _, draft = _draft()
        card = self.build(draft)
        rows = _rows(card)
        first = [n.get("placeholder", {}).get("content") for n in _tags(rows[0], "select_static")]
        second = [n.get("placeholder", {}).get("content") for n in _tags(rows[1], "select_static")]
        self.assertEqual(first, ["工具", "项目"])
        self.assertEqual(second, ["模型", "强度"])
        form = _tags(card, "form")[0]
        self.assertEqual([n["name"] for n in _tags(form, "input")],
                         [nscard.NAME_INPUT, nscard.SESSION_INPUT, nscard.TEXT_INPUT])
        start = [b for b in _tags(form, "button")]
        self.assertEqual([b["text"]["content"] for b in start], ["▶ 开始"])
        self.assertEqual(start[0]["form_action_type"], "submit")

    def test_ns02_defaults_selected(self):
        _, draft = _draft(tool="codex")
        card = self.build(draft, default_model="gpt-6-sol", default_effort="high")
        sels = {n["placeholder"]["content"]: n for n in _tags(card, "select_static")}
        self.assertEqual(sels["工具"]["initial_option"], "codex")
        self.assertEqual(sels["项目"]["initial_option"], "p0")
        self.assertEqual(sels["模型"]["initial_option"], "def")
        self.assertEqual(sels["模型"]["options"][0]["text"]["content"], "缺省（gpt-6-sol）")
        self.assertEqual(sels["强度"]["initial_option"], "def")
        self.assertIn("缺省（高", sels["强度"]["options"][0]["text"]["content"])

    def test_ns03_project_list_ends_with_new_project(self):
        _, draft = _draft()
        card = self.build(draft)
        proj = next(n for n in _tags(card, "select_static") if n["placeholder"]["content"] == "项目")
        self.assertEqual(proj["options"][-1], {"text": {"tag": "plain_text", "content": "➕ 新建项目"},
                                               "value": "new"})
        self.assertNotIn("/w/a", json.dumps(card))

    def test_ns04_project_box_prefilled_from_dropdown_and_session_name_recommended(self):
        _, draft = _draft()
        draft.proj = 1
        boxes = {n["name"]: n for n in _tags(self.build(draft), "input")}
        self.assertEqual(boxes[nscard.NAME_INPUT]["default_value"], "b")
        self.assertEqual(boxes[nscard.SESSION_INPUT]["default_value"], "b")
        self.assertNotIn("default_value", boxes[nscard.TEXT_INPUT])

    def test_ns04b_new_project_clears_boxes_keeps_dropdown(self):
        _, draft = _draft()
        draft.proj = None
        card = self.build(draft)
        proj = next(n for n in _tags(card, "select_static") if n["placeholder"]["content"] == "项目")
        self.assertEqual(proj["initial_option"], "new")
        boxes = {n["name"]: n for n in _tags(card, "input")}
        self.assertNotIn("default_value", boxes[nscard.NAME_INPUT])
        self.assertNotIn("default_value", boxes[nscard.SESSION_INPUT])
        self.assertNotIn("📂 选已有项目", json.dumps(card, ensure_ascii=False))

    def test_ns04c_recommend_session_name(self):
        self.assertEqual(nscard.recommend_session_name("~/work/AI_native"), "AI_native")
        self.assertEqual(nscard.recommend_session_name("~"), "")
        self.assertEqual(nscard.recommend_session_name(""), "")

    def test_ns05_no_effort_row_cell_for_opencode(self):
        _, draft = _draft(tool="opencode")
        card = self.build(draft, efforts=())
        self.assertEqual([n["placeholder"]["content"] for n in _tags(_rows(card)[1], "select_static")], ["模型"])

    def test_ns06_dropdowns_outside_form_inputs_inside(self):
        _, draft = _draft()
        card = self.build(draft)
        form = _tags(card, "form")[0]
        self.assertEqual(_tags(form, "select_static"), [])
        for node in _tags(card, "select_static"):
            self.assertEqual(node["behaviors"][0]["type"], "callback")
        self.assertEqual(len(_tags(card, "input")), len(_tags(form, "input")))
        for node in _tags(form, "input"):
            self.assertNotIn("behaviors", node)

    def test_starting_card_has_no_input(self):
        card = nscard.build_starting_card("codex", "proj", "缺省（gpt-6-sol）", "hi")
        self.assertEqual(_tags(card, "input"), [])
        self.assertIn("hi", json.dumps(card, ensure_ascii=False))


class ResolveClickTest(unittest.TestCase):
    def test_ns07_whitelist(self):
        ok = nscard.value("tool", "abc123")
        self.assertEqual(nscard.resolve_click(ok, "codex")["pick"], "codex")
        self.assertIsNone(nscard.resolve_click(ok, "cursor"))
        self.assertIsNone(nscard.resolve_click(dict(ok, x=1), "codex"))
        self.assertIsNone(nscard.resolve_click({"act": "nsc", "op": "rm", "n": "abc"}))
        self.assertIsNone(nscard.resolve_click(nscard.value("proj", "abc"), "/etc"))
        self.assertIsNone(nscard.resolve_click(nscard.value("model", "abc"), "gpt-6-sol"))
        self.assertEqual(nscard.resolve_click(nscard.value("model", "abc"), "m3")["pick"], "m3")
        got = nscard.resolve_click(nscard.value("go", "abc"), None,
                                   {nscard.TEXT_INPUT: "  hello ", nscard.NAME_INPUT: " demo ", nscard.SESSION_INPUT: 3})
        self.assertEqual((got["text"], got["project"], got["session"]), ("hello", "demo", ""))
        self.assertIsNone(nscard.resolve_click(nscard.value("go", "abc"), None, None)["text"] or None)
        self.assertIsNone(nscard.resolve_click({"act": "nsc", "op": "go", "n": "../x"}))

    def test_draft_scope_and_expiry(self):
        now = [1000.0]
        store = nscard.DraftStore(now=lambda: now[0])
        draft = store.new("dm:a", "claude", [("/w/a", "a")], [])
        self.assertIsNone(store.get("dm:b", draft.nonce)[0])
        self.assertIs(store.get("dm:a", draft.nonce)[0], draft)
        now[0] += nscard.DRAFT_TTL_SEC + 1
        self.assertIsNone(store.get("dm:a", draft.nonce)[0])

    def test_new_card_replaces_old(self):
        store = nscard.DraftStore()
        old = store.new("dm:a", "claude", [], [])
        store.new("dm:a", "codex", [], [])
        self.assertIsNone(store.get("dm:a", old.nonce)[0])


class SplitArgsTest(unittest.TestCase):
    def split(self, tool, text):
        return app_mod._split_ns_args(tool, text.split(), pick_model, pick_effort)

    def test_ns08_one_line_with_first_sentence(self):
        self.assertEqual(self.split("claude", "proj opus high 帮我 看看"),
                         (["proj"], "claude-opus-5-5", "high", "帮我 看看"))
        self.assertEqual(self.split("codex", "名字 ~/work/x luna 只回复 OK"),
                         (["名字", "~/work/x"], "gpt-6-luna", "", "只回复 OK"))
        self.assertEqual(self.split("codex", "proj -- 第一句"), (["proj"], "", "", "第一句"))

    def test_old_forms_unchanged(self):
        self.assertEqual(self.split("claude", "proj opus high"), (["proj"], "claude-opus-5-5", "high", ""))
        self.assertEqual(self.split("codex", "名字 ~/work/x"), (["名字", "~/work/x"], "", "", ""))
        self.assertEqual(self.split("claude", "opus"), ([], "claude-opus-5-5", "", ""))


def make_bridge(root):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    policy = SimpleNamespace(mode="trusted", turn_timeout_sec=60, workspaces=[root],
                             resolve_workspace=lambda p: p if str(p).startswith(root) else None)
    bridge.prefs = None
    bridge.settings = SimpleNamespace(policy=policy, owner_open_ids={"ou_me"},
                                      config_path=os.path.join(root, "config.toml"))
    bridge.cards, bridge.patched, bridge.said = [], [], []
    bridge.replier = SimpleNamespace(send_card=lambda chat, card: bridge.cards.append(json.loads(card)) or True,
                                     patch_card=lambda mid, card: bridge.patched.append(json.loads(card)) or True)
    bridge._say = lambda chat, text: bridge.said.append(text) or True
    bridge._say_with_buttons = lambda chat, text, buttons: bridge.said.append(text) or True
    catalog = SimpleNamespace(names=lambda tool: {"claude": ("claude-opus-5-5", "claude-sonnet-5-5"),
                                                  "codex": ("gpt-6-sol", "gpt-6-luna"),
                                                  "opencode": ("cpa-gui/x",)}[tool],
                              native=lambda tool: Choice({"claude": "claude-sonnet-5-5", "codex": "gpt-6-sol",
                                                          "opencode": "cpa-gui/x"}[tool], ""),
                              efforts=lambda tool, model="": ("low", "medium", "high"))
    bridge.model_prefs = SimpleNamespace(catalog=catalog, effective=lambda tool, sid="": Choice())
    bridge.fleet = SimpleNamespace(fleet=SimpleNamespace(adapters={"claude": 1, "codex": 1, "opencode": 1}))
    bridge._new_current_dir = lambda scope: ""
    bridge._click_scope = lambda event, clicker: ("oc_dm", "dm:ou_me")
    bridge._unknown_chat_notice = lambda chat: None
    return bridge


def event(value, option=None, form=None, mid="om_card"):
    return SimpleNamespace(action=SimpleNamespace(value=value, option=option, form_value=form, input_value=None),
                           context=SimpleNamespace(open_message_id=mid, open_chat_id="oc_dm"))


class BridgeFlowTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        for name in ("alpha", "beta"):
            os.makedirs(os.path.join(self.root, name, ".git"))
        self.bridge = make_bridge(self.root)
        self.patch = mock.patch.object(app_mod.workspace_mod, "recent_repos",
                                       lambda policy, cur, n: [os.path.join(self.root, "alpha"),
                                                               os.path.join(self.root, "beta")])
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def send_card(self, tool=""):
        self.bridge._send_ns_card("dm:ou_me", "oc_dm", tool)
        card = self.bridge.cards[-1]
        nonce = next(n for n in _walk(card) if n.get("act") == "nsc")["n"]
        return card, nonce

    def click(self, op, nonce, option=None, form=None):
        res = self.bridge._on_ns_card_click(event(nscard.value(op, nonce), option, form), "ou_me",
                                            nscard.value(op, nonce))
        return res.toast.content if res.toast else "", (res.card.data if res.card else None)

    def go(self, nonce, project=None, session=None, text=None):
        """点「开始」：form_value 带回三个框（None = 那个框没出现在 form_value 里）。"""
        form = {k: v for k, v in ((nscard.NAME_INPUT, project), (nscard.SESSION_INPUT, session),
                                  (nscard.TEXT_INPUT, text)) if v is not None}
        jobs = []
        self.bridge._loop = SimpleNamespace(call_soon_threadsafe=lambda f: None)
        self.bridge._ns_card_run = lambda *a: jobs.append(a) or _noop()
        with mock.patch.object(asyncio, "run_coroutine_threadsafe", lambda coro, loop: coro.close()):
            toast, card = self.click("go", nonce, None, form)
        return toast, card, jobs

    def test_ns09_menu_and_ns_send_same_card(self):
        asyncio.run(self.bridge._handle_newsession_command("dm:ou_me", "oc_dm", "/ns"))
        asyncio.run(self.bridge._handle_newsession_command("dm:ou_me", "oc_dm", "新建会话"))
        self.assertEqual(len(self.bridge.cards), 2)
        self.assertTrue(all(c["header"]["title"]["content"] == "➕ 新建会话" for c in self.bridge.cards))

    def test_ns10_switch_tool_changes_models_and_resets_choice(self):
        _, nonce = self.send_card("claude")
        self.click("model", nonce, "m0")
        _, card = self.click("tool", nonce, "codex")
        model_sel = next(n for n in _tags(card, "select_static") if n["placeholder"]["content"] == "模型")
        self.assertEqual([o["text"]["content"] for o in model_sel["options"]],
                         ["缺省（gpt-6-sol）", "gpt-6-sol", "gpt-6-luna"])
        self.assertEqual(model_sel["initial_option"], "def")

    def test_ns11_default_tool_is_claude_even_after_codex(self):
        _, nonce = self.send_card("codex")
        self.go(nonce)
        fresh = make_bridge(self.root)
        fresh._send_ns_card("dm:ou_me", "oc_dm")
        tool_sel = next(n for n in _tags(fresh.cards[-1], "select_static") if n["placeholder"]["content"] == "工具")
        self.assertEqual(tool_sel["initial_option"], "claude")

    def test_ns12_go_without_text_uses_hi_and_starting_card(self):
        _, nonce = self.send_card("claude")
        toast, card, jobs = self.go(nonce)
        self.assertEqual(toast, "正在开")
        self.assertEqual(card["header"]["title"]["content"], "⏳ 正在开新会话")
        self.assertEqual(jobs[0][8], "hi")
        self.assertEqual(jobs[0][2], "om_card")
        self.assertEqual(jobs[0][3:6], ("claude", os.path.join(self.root, "alpha"), ""))
        self.assertEqual(jobs[0][9], "alpha")
        self.assertIn("会话名：alpha", json.dumps(card, ensure_ascii=False))
        self.assertEqual(self.click("go", nonce)[0], "这张卡已经过期了，点「➕ 新建会话」再开一张")

    def test_ns13_start_carries_form_text_model_effort_and_session_name(self):
        """FC-35：填完字直接点「开始」，不按回车，字都要带上（1.5.1 手机上丢字的回归）。"""
        _, nonce = self.send_card("codex")
        _, card = self.click("proj", nonce, "p1")
        box = next(n for n in _tags(card, "input") if n["name"] == nscard.NAME_INPUT)
        self.assertEqual(box["default_value"], os.path.join(self.root, "beta"))
        self.click("model", nonce, "m1")
        self.click("effort", nonce, "e2")
        _, _, jobs = self.go(nonce, project=os.path.join(self.root, "beta"), session="修登录", text="只回复 OK")
        scope, chat, mid, tool, project, new_path, model, effort, text, name = jobs[0]
        self.assertEqual((tool, project, new_path, model, effort, text, name),
                         ("codex", os.path.join(self.root, "beta"), "", "gpt-6-luna", "high", "只回复 OK", "修登录"))

    def test_ns14_typed_project_name_creates_new_dir(self):
        """FC-36：项目框里填新名字 ⇒ 在新建根下建目录；会话名不填 ⇒ 用新项目名。"""
        self.bridge._ns_new_root = self.root
        _, nonce = self.send_card()
        _, card = self.click("proj", nonce, "new")
        self.assertEqual(self.go(nonce)[0], "先在「项目」框里填项目名（新名字会在 ~/work 下新建）")
        _, nonce = self.send_card()
        self.assertIn("项目名只能用", self.go(nonce, project="../etc")[0])
        _, nonce = self.send_card()
        toast, card, jobs = self.go(nonce, project="demo-1")
        self.assertEqual(toast, "正在开")
        self.assertEqual((jobs[0][4], jobs[0][5], jobs[0][9]), ("", os.path.join(self.root, "demo-1"), "demo-1"))

    def test_ns14b_typed_existing_dir_or_label_reuses_it(self):
        """FC-37：项目框里填的是已有目录 / 列表里的项目 ⇒ 直接用，不新建。"""
        self.bridge._ns_new_root = self.root
        os.makedirs(os.path.join(self.root, "gamma"))
        _, nonce = self.send_card()
        _, _, jobs = self.go(nonce, project="gamma")
        self.assertEqual((jobs[0][4], jobs[0][5], jobs[0][9]), (os.path.join(self.root, "gamma"), "", "gamma"))
        _, nonce = self.send_card()
        _, _, jobs = self.go(nonce, project=os.path.join(self.root, "beta"), session="")
        self.assertEqual((jobs[0][4], jobs[0][5], jobs[0][9]), (os.path.join(self.root, "beta"), "", "beta"))
        open(os.path.join(self.root, "afile"), "w").close()
        _, nonce = self.send_card()
        self.assertIn("同名文件", self.go(nonce, project="afile")[0])

    def test_ns15_non_owner_and_forged_rejected(self):
        _, nonce = self.send_card()
        res = self.bridge._on_ns_card_click(event(nscard.value("go", nonce)), "ou_other", nscard.value("go", nonce))
        self.assertIsNone(res.card)
        res = self.bridge._on_ns_card_click(event(nscard.value("proj", nonce), "p9"), "ou_me",
                                            nscard.value("proj", nonce))
        self.assertIn("认不出来", res.toast.content)
        res = self.bridge._on_ns_card_click(event(nscard.value("go", "ffffff")), "ou_me", nscard.value("go", "ffffff"))
        self.assertIn("过期", res.toast.content)

    def test_ns16_background_run_patches_card_with_enter_button_and_no_dm_spam(self):
        bridge = self.bridge
        bound = SimpleNamespace(chat_id="oc_group", name="SN·Codex·beta")
        calls = {}

        async def fake_start(scope, chat_id, text, card_msg_id="", out=None):
            calls.update(text=text, card=card_msg_id, armed=bridge._newsession_state().peek(scope)[0])
            out.update(sid="s-1", bound=bound)
            return True

        async def fake_button(chat):
            return {"tag": "button", "text": {"tag": "plain_text", "content": "➡️ 进群说话"}}

        bridge._maybe_start_new_session = fake_start
        bridge._group_enter_button = fake_button
        asyncio.run(bridge._ns_card_run("dm:ou_me", "oc_dm", "om_card", "codex", os.path.join(self.root, "beta"),
                                        "", "gpt-6-luna", "high", "hi"))
        self.assertEqual(calls["armed"]["model"], "gpt-6-luna")
        asyncio.run(bridge._ns_card_run("dm:ou_me", "oc_dm", "om_card", "codex", os.path.join(self.root, "beta"),
                                        "", "", "", "hi", "修登录"))
        self.assertEqual(calls["armed"]["name"], "修登录")
        asyncio.run(bridge._ns_card_run("dm:ou_me", "oc_dm", "om_card", "codex", os.path.join(self.root, "beta"),
                                        "", "gpt-6-luna", "high", "hi"))
        self.assertEqual(calls["armed"]["effort"], "high")
        self.assertEqual(calls["armed"]["name"], "beta")
        self.assertEqual(calls["card"], "om_card")
        done = bridge.patched[-1]
        text = json.dumps(done, ensure_ascii=False)
        self.assertIn("SN·Codex·beta", text)
        self.assertIn("进群说话", text)
        self.assertEqual(bridge.said, [])

    def test_ns17_background_failure_patches_error(self):
        bridge = self.bridge

        async def fake_start(scope, chat_id, text, card_msg_id="", out=None):
            out.update(sid="", error="codex 没装好")
            return True

        bridge._maybe_start_new_session = fake_start
        asyncio.run(bridge._ns_card_run("dm:ou_me", "oc_dm", "om_card", "codex", os.path.join(self.root, "beta"),
                                        "", "", "", "hi"))
        self.assertIn("codex 没装好", json.dumps(bridge.patched[-1], ensure_ascii=False))

    def test_ns18_one_line_command_starts_immediately(self):
        bridge = self.bridge
        seen = {}

        async def fake_start(scope, chat_id, text, **kw):
            seen.update(text=text, armed=bridge._newsession_state().peek(scope)[0])
            return True

        bridge._maybe_start_new_session = fake_start
        target = os.path.join(self.root, "alpha")
        with mock.patch.object(app_mod.workspace_mod, "resolve_request",
                               lambda policy, cur, raw: SimpleNamespace(ok=True, workspace=target, outcome=None)):
            asyncio.run(bridge._handle_newsession_command("dm:ou_me", "oc_dm", "/ns codex alpha luna high 只回复 OK"))
        self.assertEqual(seen["text"], "只回复 OK")
        self.assertEqual((seen["armed"]["model"], seen["armed"]["effort"], seen["armed"]["cwd"]),
                         ("gpt-6-luna", "high", target))


async def _noop():
    return None


class QuietStartTest(unittest.TestCase):
    def test_quiet_start_keeps_dm_silent(self):
        """一张卡那条路：私聊里不发「正在开」，失败原因写进 out。"""
        from tests.test_newsession_model import make_bridge as base_bridge

        bridge = base_bridge()
        bridge._newsession_state().arm("dm:x", "codex", "/tmp")
        bridge.fleet = SimpleNamespace(start=lambda *a, **kw: SimpleNamespace(ok=False, session_id="", text="起不来"))
        bridge._fleet_inflight, bridge._paused, bridge.reviewer = {}, {}, None
        bridge.replier = SimpleNamespace(send=lambda chat_id, text: bridge.sent.append(text) or True)
        out = {}

        class Wait:
            def __init__(self, replier, *a, **kw):
                self.replier = replier

            async def run(self, stop):
                await stop.wait()

            async def finish(self, result):
                return None

        with mock.patch.object(app_mod, "_FleetWaitCard", Wait):
            asyncio.run(bridge._maybe_start_new_session("dm:x", "oc", "hi", card_msg_id="om", out=out))
        self.assertEqual(out["error"], "起不来")
        self.assertFalse(any("正在新开" in str(m) for m in bridge.sent))


if __name__ == "__main__":
    unittest.main()
