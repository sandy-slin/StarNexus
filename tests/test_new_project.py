"""`/ns project new <路径> [--git]`：白名单内新建项目，不覆盖、不越界，可选 git init。"""
import asyncio
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge import workspace as ws
from bridge.policy import Policy


def make_policy(root):
    return SimpleNamespace(workspaces=(root,), default_workspace=root,
                           resolve_workspace=lambda c: Policy.resolve_workspace(SimpleNamespace(workspaces=(root,)), c))


class CreateTest(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.policy = make_policy(self.root)

    def test_creates_nested_and_resolves(self):
        res = ws.create_project(self.policy, os.path.join(self.root, "work", "demo"))
        self.assertTrue(res.ok, res.reply)
        self.assertTrue(os.path.isdir(os.path.join(self.root, "work", "demo")))
        self.assertEqual(res.workspace, os.path.join(self.root, "work", "demo"))

    def test_refuses_existing(self):
        os.makedirs(os.path.join(self.root, "old"))
        res = ws.create_project(self.policy, os.path.join(self.root, "old"))
        self.assertFalse(res.ok)
        self.assertIn("不覆盖", res.reply)

    def test_refuses_outside_dotdot_relative_system(self):
        self.assertFalse(ws.create_project(self.policy, "/tmp/elsewhere-sn-test").ok)
        self.assertFalse(ws.create_project(self.policy, os.path.join(self.root, "a", "..", "..", "x")).ok)
        self.assertFalse(ws.create_project(self.policy, "work/demo").ok)
        self.assertFalse(ws.create_project(self.policy, "/usr/local/sn-demo").ok)
        self.assertFalse(ws.create_project(self.policy, self.root).ok)
        self.assertFalse(os.path.exists("/tmp/elsewhere-sn-test"))

    def test_depth_limit(self):
        deep = os.path.join(self.root, *["d"] * 5)
        self.assertFalse(ws.create_project(self.policy, deep).ok)

    def test_git_init_real(self):
        res = ws.create_project(self.policy, os.path.join(self.root, "g"), git=True)
        self.assertTrue(res.ok, res.reply)
        self.assertIn("git init", res.reply)
        log = subprocess.run(["git", "log", "--oneline"], cwd=res.workspace, capture_output=True, text=True)
        self.assertIn("initial commit", log.stdout)


class CommandTest(unittest.TestCase):
    def make(self, root):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.settings = SimpleNamespace(policy=make_policy(root))
        bridge.said, bridge.armed, bridge.cards, bridge.names = [], [], [], []
        bridge._say = lambda chat, text: bridge.said.append(text) or True
        bridge._arm_new_session = lambda scope, chat, tool, cwd, *a, **kw: (bridge.armed.append((tool, cwd)), bridge.names.append(kw.get("name", "")))
        bridge._send_new_tool_card = lambda chat: bridge.cards.append(chat)
        bridge._send_ns_card = lambda scope, chat, tool="", project="", *a: bridge.cards.append((chat, project))
        return bridge

    def test_with_tool_arms_directly(self):
        root = os.path.realpath(tempfile.mkdtemp())
        bridge = self.make(root)
        asyncio.run(bridge._new_project("dm:ou_me", "oc_dm", [os.path.join(root, "p1"), "--git", "codex"]))
        self.assertEqual(bridge.armed, [("codex", os.path.join(root, "p1"))])
        self.assertTrue(os.path.isdir(os.path.join(root, "p1", ".git")))
        self.assertEqual(bridge.names, ["p1"])

    def test_without_tool_sends_one_card_on_new_project(self):
        root = os.path.realpath(tempfile.mkdtemp())
        bridge = self.make(root)
        asyncio.run(bridge._new_project("dm:ou_me", "oc_dm", [os.path.join(root, "p2")]))
        self.assertEqual(bridge.cards, [("oc_dm", os.path.join(root, "p2"))])

    def test_refusal_is_said(self):
        root = os.path.realpath(tempfile.mkdtemp())
        bridge = self.make(root)
        asyncio.run(bridge._new_project("dm:ou_me", "oc_dm", ["/etc/x"]))
        self.assertEqual(bridge.armed, [])
        self.assertTrue(bridge.said)


if __name__ == "__main__":
    unittest.main()
