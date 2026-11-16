import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge.fleet.catalog import Catalog, Choice, ModelPrefs
from bridge.fleet.modelcard import home_card, list_card, resolve_click


def write(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(data if isinstance(data, str) else json.dumps(data))


class CatalogFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.claude = os.path.join(root, "settings.json")
        self.codex_home = os.path.join(root, "codex")
        os.makedirs(self.codex_home)
        write(self.claude, {"model": "claude-opus-5-5", "modelPicker": {"options": [
            {"model": "claude-opus-5-5"}, {"model": "claude-sonnet-5-5"},
            {"model": "gpt-6-sol", "behavesAs": "claude-sonnet-5-5"}, {"model": "-bad"}]}})
        write(os.path.join(self.codex_home, "config.toml"), 'model = "gpt-6-sol"\nmodel_catalog_json = "cat.json"\n')
        write(os.path.join(self.codex_home, "cat.json"), {"models": [
            {"slug": "gpt-6-luna", "visibility": "list", "priority": 3,
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "max"}],
             "default_reasoning_level": "medium"},
            {"slug": "gpt-6-sol", "visibility": "list", "priority": 2,
             "supported_reasoning_levels": [{"effort": "low"}, {"effort": "ultra"}]},
            {"slug": "hidden", "visibility": "hide", "priority": 1}]})
        self.catalog = Catalog(self.claude, self.codex_home)
        self.prefs_path = os.path.join(root, "model_prefs.json")
        self.prefs = ModelPrefs(self.prefs_path, self.catalog)

    def tearDown(self):
        self.tmp.cleanup()


class CatalogTest(CatalogFixture):
    def test_claude_reads_model_picker_native_first(self):
        self.assertEqual(self.catalog.names("claude"), ("claude-opus-5-5", "claude-sonnet-5-5", "gpt-6-sol"))

    def test_codex_reads_visible_models_by_priority(self):
        self.assertEqual(self.catalog.names("codex"), ("gpt-6-sol", "gpt-6-luna"))
        self.assertEqual(self.catalog.efforts("codex", "gpt-6-luna"), ("low", "medium", "max"))

    def test_fallback_when_config_missing(self):
        empty = Catalog("/nonexistent/settings.json", "/nonexistent/codex")
        self.assertIn("claude-sonnet-5-5", empty.names("claude"))
        self.assertIn("gpt-6-sol", empty.names("codex"))


class ModelPrefsTest(CatalogFixture):
    def test_defaults_are_per_tool(self):
        self.assertTrue(self.prefs.set_default("claude", model="claude-sonnet-5-5"))
        self.assertTrue(self.prefs.set_default("codex", model="gpt-6-luna", effort="max"))
        self.assertEqual(self.prefs.default("claude"), Choice("claude-sonnet-5-5", ""))
        self.assertEqual(self.prefs.default("codex"), Choice("gpt-6-luna", "max"))

    def test_unknown_model_or_effort_rejected(self):
        self.assertFalse(self.prefs.set_default("codex", model="nope"))
        self.assertFalse(self.prefs.set_default("codex", model="gpt-6-luna", effort="ultra"))
        self.assertFalse(self.prefs.set_default("cursor", model="gpt-6-luna"))

    def test_session_override_does_not_touch_default(self):
        self.prefs.set_default("codex", model="gpt-6-sol")
        self.assertTrue(self.prefs.set_session("codex", "t1", model="gpt-6-luna"))
        self.assertEqual(self.prefs.effective("codex", "t1").model, "gpt-6-luna")
        self.assertEqual(self.prefs.effective("codex", "t2").model, "gpt-6-sol")
        self.assertEqual(self.prefs.default("codex").model, "gpt-6-sol")

    def test_changing_model_drops_unsupported_effort(self):
        self.prefs.set_session("codex", "t1", model="gpt-6-sol", effort="ultra")
        self.prefs.set_session("codex", "t1", model="gpt-6-luna")
        self.assertEqual(self.prefs.session("codex", "t1"), Choice("gpt-6-luna", ""))

    def test_persists_across_reload(self):
        self.prefs.set_default("claude", model="claude-opus-5-5", effort="high")
        self.prefs.set_session("codex", "t1", model="gpt-6-luna")
        again = ModelPrefs(self.prefs_path, self.catalog)
        self.assertEqual(again.default("claude"), Choice("claude-opus-5-5", "high"))
        self.assertEqual(again.session("codex", "t1").model, "gpt-6-luna")

    def test_corrupt_file_is_ignored(self):
        write(self.prefs_path, "{not json")
        self.assertEqual(ModelPrefs(self.prefs_path, self.catalog).default("claude"), Choice())


class ModelCardTest(CatalogFixture):
    def test_resolve_click_whitelist(self):
        self.assertEqual(resolve_click({"act": "mdl", "op": "m", "to": "s", "tool": "codex", "i": 1}),
                         {"op": "m", "to": "s", "tool": "codex", "i": 1})
        self.assertIsNone(resolve_click({"act": "mdl", "op": "m", "to": "s", "tool": "codex", "i": "1"}))
        self.assertIsNone(resolve_click({"act": "mdl", "op": "rm", "to": "d", "tool": "codex"}))
        self.assertIsNone(resolve_click({"act": "mdl", "op": "open", "to": "d", "tool": "cursor"}))
        self.assertIsNone(resolve_click({"act": "mdl", "op": "e", "to": "d", "tool": "codex", "e": "x;rm"}))

    def test_cards_never_carry_model_names_or_session_ids(self):
        self.prefs.set_session("codex", "secret-sid", model="gpt-6-luna")
        card = json.loads(list_card(self.prefs, "s", "codex", "secret-sid", "SN·Codex·x·y"))
        values = json.dumps([b["behaviors"] for b in _buttons(card)])
        self.assertNotIn("secret-sid", values)
        self.assertNotIn("gpt-6-luna", values)

    def test_home_card_shows_session_line_only_in_group(self):
        self.assertNotIn("这个会话", home_card(self.prefs, None))
        self.assertIn("这个会话", home_card(self.prefs, ("codex", "t1", "SN·Codex·x·y")))


def _buttons(card):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                out.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(card)
    return out


class ArgvTest(unittest.TestCase):
    def test_codex_model_args(self):
        from bridge.fleet.codex import _model_args

        argv = ["codex", "exec"]
        self.assertEqual(_model_args(argv, "gpt-6-luna", "high"), "")
        self.assertEqual(argv[2:], ["-m", "gpt-6-luna", "-c", 'model_reasoning_effort="high"'])
        bad = ["codex"]
        self.assertTrue(_model_args(bad, "-x", ""))
        self.assertTrue(_model_args(bad, "", 'hi"; rm'))
        self.assertEqual(bad, ["codex"])

    def test_claude_send_argv_puts_model_before_multi_value_flags(self):
        from bridge.fleet.claude import ClaudeAdapter, _model_problem

        adapter = object.__new__(ClaudeAdapter)
        adapter.claude_bin = "claude"
        adapter._auth = lambda: ({}, {})
        policy = SimpleNamespace(allowed_tools=lambda: ["Read"], disallowed_tools=lambda: ["Bash(sudo:*)"])
        argv = adapter._send_argv("sid", "hi", policy, None, model="claude-opus-5-5", effort="high")
        self.assertLess(argv.index("--model"), argv.index("--allowed-tools"))
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5-5")
        self.assertEqual(argv[argv.index("--effort") + 1], "high")
        self.assertNotIn("--model", adapter._send_argv("sid", "hi", policy, None))
        self.assertTrue(_model_problem("", "ultra"))
        self.assertEqual(_model_problem("claude-opus-5-5", "max"), "")


class ControlForwardTest(unittest.TestCase):
    def test_send_forwards_model_only_when_set(self):
        from bridge.fleet.control import FleetControl
        from bridge.fleet.model import ActionResult

        calls = []

        class Adapter:
            def send(self, sid, text, **kw):
                calls.append(kw)
                return ActionResult(True, "ok", sid)

        fleet = SimpleNamespace(adapter=lambda tool: Adapter())
        control = FleetControl(fleet)
        control._cwd_of = lambda tool, sid: ""
        policy = SimpleNamespace(mode="trusted", resolve_workspace=lambda p: p)
        control.send("codex", "t1", "hi", policy=policy, model="gpt-6-luna", effort="high")
        control.send("codex", "t1", "hi", policy=policy)
        self.assertEqual(calls[0]["model"], "gpt-6-luna")
        self.assertEqual(calls[0]["effort"], "high")
        self.assertNotIn("model", calls[1])


if __name__ == "__main__":
    unittest.main()
