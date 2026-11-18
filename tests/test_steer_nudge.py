"""1.9.3：插话后审批钩子拦一次下一步工具调用，让 Claude 先看新消息（docs/product/steering.md）。"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests import helpers  # noqa: F401

from bridge import approval
from tests.test_steering import SID, adapter
from tests.test_steering_dm import FakeArgvRunner


class HookSteerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "approvals")
        os.makedirs(self.dir)
        self.hook = os.path.join(self.tmp.name, "hook.py")
        with open(self.hook, "w", encoding="utf-8") as handle:
            handle.write(approval.HOOK_SOURCE)
        with open(os.path.join(self.dir, "TRUSTED"), "w") as handle:
            handle.write("1")
        with open(os.path.join(self.dir, "workspaces.json"), "w", encoding="utf-8") as handle:
            json.dump([self.tmp.name], handle)

    def tearDown(self):
        self.tmp.cleanup()

    def run_hook(self, command="sleep 4 && date >> steer.txt"):
        event = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.tmp.name,
                 "session_id": "s1"}
        out = subprocess.run([sys.executable, self.hook], input=json.dumps(event), text=True,
                             capture_output=True, timeout=20,
                             env=dict(os.environ, CARHER_APPROVAL_DIR=self.dir))
        self.assertEqual(out.returncode, 0, out.stderr)
        if not out.stdout.strip():
            return "", ""
        got = json.loads(out.stdout)["hookSpecificOutput"]
        return got["permissionDecision"], got["permissionDecisionReason"]

    def test_next_tool_call_denied_once_with_the_steer_text(self):
        self.assertTrue(approval.mark_steer(self.dir, "改主意了：只回复 LR-STEER"))
        decision, reason = self.run_hook()
        self.assertEqual(decision, "deny")
        self.assertIn("LR-STEER", reason)
        self.assertIn("新的话", reason)
        self.assertEqual(self.run_hook()[0], "allow")

    def test_no_steer_no_change(self):
        self.assertEqual(self.run_hook()[0], "allow")

    def test_stale_steer_ignored(self):
        approval.mark_steer(self.dir, "旧的")
        old = time.time() - 3600
        os.utime(os.path.join(self.dir, "STEER"), (old, old))
        self.assertEqual(self.run_hook()[0], "allow")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "STEER")))

    def test_steer_text_is_redacted(self):
        secret = "sk-" + "a" * 40
        approval.mark_steer(self.dir, f"用这个 key {secret}")
        with open(os.path.join(self.dir, "STEER"), encoding="utf-8") as handle:
            self.assertNotIn(secret, handle.read())

    def test_clear_steer(self):
        approval.mark_steer(self.dir, "x")
        approval.clear_steer(self.dir)
        self.assertEqual(self.run_hook()[0], "allow")

    def test_no_dir_is_noop(self):
        self.assertFalse(approval.mark_steer("", "x"))
        approval.clear_steer("")


class FleetSteerMarksTest(unittest.TestCase):
    def test_steer_writes_marker_and_turn_end_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            ad = adapter(1.5)
            box = {}
            env = dict(os.environ, CARHER_APPROVAL_DIR=directory)
            th = threading.Thread(target=lambda: box.update(res=ad._run_streaming(
                SID, ["claude", "-p", "原任务", "--output-format", "json"], "原任务", 20, "/tmp", env,
                lambda pid: None)))
            th.start()
            for _ in range(100):
                if ad.steerable(SID):
                    break
                time.sleep(0.02)
            self.assertTrue(ad.steer(SID, "改主意了").ok)
            self.assertTrue(os.path.exists(os.path.join(directory, "STEER")))
            th.join(15)
            self.assertFalse(os.path.exists(os.path.join(directory, "STEER")))


class RunnerSteerMarksTest(unittest.TestCase):
    def test_dm_steer_writes_marker_and_turn_end_clears_it(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeArgvRunner(1.5)
            seen = {}

            async def go():
                task = asyncio.create_task(runner.run_turn("原任务", "/tmp", steer_key="dm:u1",
                                                           approval_dir=directory))
                for _ in range(200):
                    if runner.steerable("dm:u1"):
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.2)
                seen["steered"] = runner.steer("dm:u1", "改主意了")
                seen["marked"] = os.path.exists(os.path.join(directory, "STEER"))
                return await task

            result = asyncio.run(go())
            self.assertTrue(seen["steered"])
            self.assertTrue(seen["marked"])
            self.assertTrue(result.ok)
            self.assertFalse(os.path.exists(os.path.join(directory, "STEER")))


if __name__ == "__main__":
    unittest.main()
