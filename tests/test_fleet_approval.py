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


class FleetWorkspaceScopeTest(unittest.TestCase):
    """会话的工作区。

    FC-13（2026-11-17）原来把工作区收窄成项目目录。2026-11-21 主人改口径：
    **保持红线，范围扩大到整个工作区** —— 全授权档下工作区就是全局清单（`policy.workspaces`），
    不再按项目目录收窄。现场证据：11-20 23:37~23:49 八张审批卡全是「碰到了工作目录外的 X」，
    X 是 `~/.carher-feishu-agent` / `backup/` 这类家目录内、项目外的路径 ⇒ 全授权档形同没开。
    红线（删家目录 / git push / 提权 / 读凭据 / 往终端敲命令 / 内联解释器）不受影响，仍然拦。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bridge = make_bridge(self.tmp.name)
        # 项目目录放家目录下：临时目录（/var/folders、/tmp）在钩子里算系统路径，测不出越界
        self.home_tmp = tempfile.TemporaryDirectory(dir=os.path.expanduser("~"), prefix=".sn-unit-")
        self.project = os.path.realpath(os.path.join(self.home_tmp.name, "proj"))
        os.makedirs(self.project)
        self.policy = SimpleNamespace(mode="trusted", workspaces=(os.path.expanduser("~"),))

    def tearDown(self):
        self.home_tmp.cleanup()
        self.tmp.cleanup()

    def workspaces(self, directory):
        with open(os.path.join(directory, "workspaces.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_trusted_keeps_whole_workspace(self):
        """全授权档：项目目录给了也不收窄，工作区 = 全局清单。"""
        directory = self.bridge._fleet_approval_dir("codex", "t1", self.policy, self.project)
        self.assertEqual(self.workspaces(directory), list(self.policy.workspaces))

    def test_non_trusted_still_narrows_to_project(self):
        """可改档：收窄照旧 —— 放宽只针对主人明确开了全授权的那一档。"""
        policy = SimpleNamespace(mode="write", workspaces=(os.path.expanduser("~"),))
        directory = self.bridge._fleet_approval_dir("codex", "t3", policy, self.project)
        self.assertEqual(self.workspaces(directory), [self.project])

    def test_missing_project_falls_back_to_policy(self):
        directory = self.bridge._fleet_approval_dir("codex", "t1", self.policy, "")
        self.assertEqual(self.workspaces(directory), list(self.policy.workspaces))
        gone = os.path.join(self.tmp.name, "gone")
        directory = self.bridge._fleet_approval_dir("codex", "t2", self.policy, gone)
        self.assertEqual(self.workspaces(directory), list(self.policy.workspaces))

    def test_hook_allows_write_inside_workspace_outside_project(self):
        import subprocess
        import sys

        from bridge import approval

        directory = self.bridge._fleet_approval_dir("codex", "t1", self.policy, self.project)
        hook = os.path.join(self.tmp.name, "hook.py")
        with open(hook, "w", encoding="utf-8") as handle:
            handle.write(approval.HOOK_SOURCE)
        outside = os.path.join(self.home_tmp.name, "outside.txt")

        def decide(command):
            event = {"tool_name": "Bash", "tool_input": {"command": command},
                     "cwd": self.project, "session_id": "s1"}
            out = subprocess.run([sys.executable, hook], input=json.dumps(event), text=True,
                                 capture_output=True, timeout=20,
                                 env=dict(os.environ, CARHER_APPROVAL_DIR=directory,
                                          CARHER_HOOK_FLAVOR="codex"))
            self.assertEqual(out.returncode, 0, out.stderr)
            if not out.stdout.strip():
                return ""
            return json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"]

        # 项目内、项目外但工作区内：全授权档都不再弹卡
        self.assertEqual(decide("echo hi > inside.txt"), "")
        self.assertEqual(decide(f"echo hi > {outside}"), "")
        # 工作区外仍然拦（钩子 flavor=codex 下 defer 呈现为 deny）。
        # 不用 /tmp 探测：`_SYSTEM_PREFIXES` 把它当系统路径，本来就不按越界判。
        self.assertEqual(decide("echo hi > /Users/Shared/sn-unit-outside.txt"), "deny")
        # 红线：点同意也不许，和放宽无关
        self.assertEqual(decide("git push origin main"), "deny")
