"""FC-13 / FC-06：权限收窄到「只拦删家目录」，其余危险操作逐次审批。

钩子测试直接跑 `approval.HOOK_SOURCE`（和线上同一份脚本），不靠替身。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from tests import helpers  # noqa: F401

from bridge import approval, cmdclass
from bridge.policy import ALWAYS_DENY
from bridge.review import ReviewContext, Reviewer, Verdict


class RedLineSplitTest(unittest.TestCase):
    HARD = ["rm -rf ~", "rm -rf $HOME", "rm -Rf /", "find ~ -delete", 'rm -rf "$HOME"']
    ASK = ["git push origin main", "git -C . push", "sudo ls", "su root", "cat ~/.ssh/id_rsa",
           "cat .env", "python3 -c 1", "git reset --hard", "kubectl delete pod x"]
    PLAIN = ["ls", "rm -rf build", "git status", "echo hi > a.txt", "rm -rf ~/proj/build"]

    def test_only_home_deletion_is_hard(self):
        for cmd in self.HARD:
            self.assertTrue(cmdclass.never_approvable(cmd), cmd)
        for cmd in self.ASK + self.PLAIN:
            self.assertEqual(cmdclass.never_approvable(cmd), "", cmd)

    def test_dangerous_ops_need_approval(self):
        for cmd in self.ASK:
            self.assertTrue(cmdclass.approval_reason(cmd), cmd)
        for cmd in self.PLAIN:
            self.assertEqual(cmdclass.approval_reason(cmd), "", cmd)

    def test_hook_copy_matches_module(self):
        ns = {}
        exec(compile(approval.HOOK_SOURCE.replace('if __name__ == "__main__":', "if False:"),
                     "hook", "exec"), ns)
        for cmd in self.HARD + self.ASK + self.PLAIN:
            self.assertEqual(ns["never_approvable"](cmd), cmdclass.never_approvable(cmd), cmd)
            self.assertEqual(ns["approval_reason"](cmd), cmdclass.approval_reason(cmd), cmd)

    def test_claude_hard_deny_list_is_empty(self):
        self.assertEqual(ALWAYS_DENY, ())
        self.assertIn("Read", approval.HOOK_MATCHER.split("|"))


class HookRunTest(unittest.TestCase):
    """真跑钩子脚本：trusted 档 + 工作目录 = 临时目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.ws = os.path.join(root, "ws")
        self.dir = os.path.join(root, "approvals")
        os.makedirs(self.ws)
        os.makedirs(self.dir)
        self.hook = os.path.join(root, "hook.py")
        with open(self.hook, "w", encoding="utf-8") as handle:
            handle.write(approval.HOOK_SOURCE)
        approval.write_safe_prefixes(self.dir)
        with open(os.path.join(self.dir, "workspaces.json"), "w", encoding="utf-8") as handle:
            json.dump([self.ws], handle)
        with open(os.path.join(self.dir, "TRUSTED"), "w") as handle:
            handle.write("1")

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, tool, tool_input, flavor=""):
        env = dict(os.environ, CARHER_APPROVAL_DIR=self.dir)
        env.pop("CARHER_HOOK_FLAVOR", None)
        if flavor:
            env["CARHER_HOOK_FLAVOR"] = flavor
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": self.ws, "session_id": "s1"}
        out = subprocess.run([sys.executable, self.hook], input=json.dumps(event), text=True,
                             capture_output=True, env=env, timeout=20)
        self.assertEqual(out.returncode, 0, out.stderr)
        text = out.stdout.strip()
        if not text:
            return "", ""
        got = json.loads(text)["hookSpecificOutput"]
        return got["permissionDecision"], got["permissionDecisionReason"]

    def pending(self):
        return [n for n in os.listdir(self.dir) if n.endswith(".pending")]

    def test_claude_trusted_workspace_write_allowed(self):
        self.assertEqual(self.run_hook("Bash", {"command": "echo hi > a.txt"})[0], "allow")

    def test_claude_home_deletion_denied(self):
        self.assertEqual(self.run_hook("Bash", {"command": "rm -rf ~"})[0], "deny")
        self.assertEqual(self.pending(), [])

    def test_claude_git_push_deferred_then_approved(self):
        cmd = {"command": "git push origin main"}
        decision, _ = self.run_hook("Bash", cmd)
        self.assertEqual(decision, "defer")
        self.assertEqual(len(self.pending()), 1)
        key = approval.action_digest("Bash", cmd)
        with open(os.path.join(self.dir, key + ".ok"), "w") as handle:
            handle.write("1")
        self.assertEqual(self.run_hook("Bash", cmd)[0], "allow")
        self.assertEqual(self.run_hook("Bash", cmd)[0], "defer")  # 一次批准只放一次

    def test_claude_credential_read_deferred(self):
        self.assertEqual(self.run_hook("Bash", {"command": "cat ~/.ssh/id_rsa"})[0], "defer")
        self.assertEqual(self.run_hook("Read", {"file_path": os.path.expanduser("~/.ssh/id_rsa")})[0],
                         "defer")

    def test_claude_plain_read_not_judged(self):
        self.assertEqual(self.run_hook("Read", {"file_path": os.path.join(self.ws, "a.txt")}), ("", ""))

    def test_write_outside_workspace_deferred(self):
        target = os.path.join(self.tmp.name, "outside.txt")
        self.assertEqual(self.run_hook("Write", {"file_path": target, "content": "x"})[0], "defer")

    def test_codex_flavor_turns_defer_into_deny_and_allow_into_silence(self):
        decision, reason = self.run_hook("Bash", {"command": "sudo ls"}, flavor="codex")
        self.assertEqual(decision, "deny")
        self.assertIn("批准", reason)
        self.assertEqual(len(self.pending()), 1)
        self.assertEqual(self.run_hook("Bash", {"command": "echo hi > a.txt"}, flavor="codex"), ("", ""))
        self.assertEqual(self.run_hook("Bash", {"command": "rm -rf ~"}, flavor="codex")[0], "deny")


class ReviewerTest(unittest.TestCase):
    def test_push_and_secret_no_longer_denied_at_message_level(self):
        reviewer = Reviewer()
        push = reviewer.review_request(ReviewContext(text="帮我 git push 一下", mode="trusted"))
        self.assertIsNot(push.verdict, Verdict.DENY)
        secret = reviewer.review_request(ReviewContext(text="看看 ~/.ssh/config", mode="trusted"))
        self.assertIs(secret.verdict, Verdict.ASK)
        home = reviewer.review_request(ReviewContext(text="rm -rf ~", mode="trusted"))
        self.assertIs(home.verdict, Verdict.DENY)


class CodexGuardArgsTest(unittest.TestCase):
    def make(self, hook_path):
        from bridge.fleet.codex import CodexAdapter

        return CodexAdapter(home=tempfile.gettempdir(), hook_path=hook_path)

    def test_trusted_with_hook_is_full_access_plus_guard(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as hook:
            env = {}
            args = self.make(hook.name)._sandbox_args("trusted", "/tmp/appr", env)
            self.assertIn('sandbox_mode="danger-full-access"', args)
            self.assertIn("--dangerously-bypass-hook-trust", args)
            self.assertTrue(any(a.startswith("hooks.PreToolUse=") and hook.name in a for a in args))
            self.assertEqual(env["CARHER_HOOK_FLAVOR"], "codex")
            self.assertEqual(env["CARHER_APPROVAL_DIR"], "/tmp/appr")

    def test_trusted_without_hook_falls_back_to_workspace_write(self):
        args = self.make("/nonexistent/hook.py")._sandbox_args("trusted", "/tmp/appr", {})
        self.assertIn('sandbox_mode="workspace-write"', args)
        self.assertNotIn("--dangerously-bypass-hook-trust", args)
        args = self.make(None)._sandbox_args("trusted", "", {})
        self.assertIn('sandbox_mode="workspace-write"', args)

    def test_readonly_and_write_unchanged(self):
        adapter = self.make(None)
        self.assertEqual(adapter._sandbox_args("readonly", "/x", {}), ["-c", 'sandbox_mode="read-only"'])
        self.assertEqual(adapter._sandbox_args("write", "/x", {}), ["-c", 'sandbox_mode="workspace-write"'])


if __name__ == "__main__":
    unittest.main()
