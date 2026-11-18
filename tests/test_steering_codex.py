"""Codex 中途插话（1.9.0）：接着聊走 app-server，`turn/steer` 插进同一轮，`turn/interrupt` 停。"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from tests import helpers  # noqa: F401

from bridge.fleet.codex import STEER_OK, STOPPED_BY_OWNER, CodexAdapter
from bridge.fleet.model import FleetJobs, SubprocessRunner

TID = "01a1e5ac-c2b0-73d1-a2a8-19f6b86ff354"
FAKE = os.path.join(os.path.dirname(__file__), "fake_codex_appserver.py")


class FakeAppServerRunner(SubprocessRunner):
    def __init__(self, wait, mode=""):
        self.wait, self.mode = wait, mode
        self.tmp = tempfile.mkdtemp()
        self.argv_out = os.path.join(self.tmp, "argv.json")
        self.log_out = os.path.join(self.tmp, "log.json")
        self.argv = None

    def popen(self, argv, *, cwd=None, env=None):
        self.argv = list(argv)
        env = dict(os.environ, FAKE_ARGV_OUT=self.argv_out, FAKE_LOG_OUT=self.log_out)
        return subprocess.Popen([sys.executable, FAKE, str(self.wait), self.mode], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, env=env)

    def log(self):
        for _ in range(50):
            if os.path.exists(self.log_out):
                with open(self.log_out) as fh:
                    return json.load(fh)
            time.sleep(0.05)
        return []


def adapter(runner, hook=None):
    ad = CodexAdapter(home=tempfile.mkdtemp(), jobs=FleetJobs(lstart_of=lambda pid: "x"), runner=runner,
                      codex_bin="/bin/codex", hook_path=hook)
    ad._row = lambda tid, include_archived=True: {"id": TID, "cwd": tempfile.gettempdir(), "archived": 0}
    ad._state = lambda ids=None: {}
    return ad


def send_async(ad, text="原任务", mode="trusted", timeout=20, **kw):
    box = {}
    th = threading.Thread(target=lambda: box.update(res=ad.send(TID, text, timeout=timeout, mode=mode, **kw)))
    th.start()
    return th, box


def wait_live(ad):
    for _ in range(200):
        if ad.steerable(TID):
            return True
        time.sleep(0.02)
    return False


class CodexSteerTest(unittest.TestCase):
    def test_plain_turn_returns_reply(self):
        runner = FakeAppServerRunner(0.1)
        res = adapter(runner).send(TID, "只回复 OK", timeout=20, mode="write")
        self.assertTrue(res.ok, res.text)
        self.assertEqual(res.text, "回：只回复 OK")
        self.assertEqual(runner.argv[:2], ["/bin/codex", "app-server"])
        resume = [m for m in runner.log() if m["method"] == "thread/resume"][0]["params"]
        self.assertEqual(resume["sandbox"], "workspace-write")
        self.assertEqual(resume["approvalPolicy"], "never")

    def test_steer_lands_in_same_turn(self):
        runner = FakeAppServerRunner(1.5)
        ad = adapter(runner)
        th, box = send_async(ad)
        self.assertTrue(wait_live(ad))
        got = ad.steer(TID, "改主意了")
        self.assertTrue(got.ok)
        self.assertEqual(got.text, STEER_OK)
        th.join(15)
        self.assertTrue(box["res"].ok)
        self.assertEqual(box["res"].text, "回：改主意了")
        methods = [m["method"] for m in runner.log()]
        self.assertEqual(methods.count("turn/start"), 1)
        self.assertIn("turn/steer", methods)
        self.assertFalse(ad.steerable(TID))

    def test_steer_after_turn_refused(self):
        ad = adapter(FakeAppServerRunner(0.1))
        self.assertTrue(ad.send(TID, "hi", timeout=20, mode="write").ok)
        self.assertFalse(ad.steer(TID, "晚了").ok)

    def test_stop_interrupts_live_turn(self):
        runner = FakeAppServerRunner(10)
        ad = adapter(runner)
        th, box = send_async(ad)
        self.assertTrue(wait_live(ad))
        self.assertTrue(ad.stop(TID).ok)
        th.join(15)
        self.assertFalse(box["res"].ok)
        self.assertEqual(box["res"].text, STOPPED_BY_OWNER)
        self.assertIn("turn/interrupt", [m["method"] for m in runner.log()])

    def test_failed_turn_reports_reason(self):
        res = adapter(FakeAppServerRunner(0.1, "fail")).send(TID, "hi", timeout=20, mode="write")
        self.assertFalse(res.ok)
        self.assertIn("额度用完了", res.text)
        self.assertTrue(res.delivered)

    def test_timeout_interrupts(self):
        res = adapter(FakeAppServerRunner(30)).send(TID, "hi", timeout=1, mode="write")
        self.assertFalse(res.ok)
        self.assertIn("太久没回完", res.text)

    def test_trusted_with_hook_passes_guard_and_bypass(self):
        with tempfile.NamedTemporaryFile(suffix=".py") as hook:
            runner = FakeAppServerRunner(0.1)
            res = adapter(runner, hook=hook.name).send(TID, "hi", timeout=20, mode="trusted",
                                                       approval_dir="/tmp/appr")
            self.assertTrue(res.ok, res.text)
            self.assertTrue(any(a.startswith("hooks.PreToolUse=") and hook.name in a for a in runner.argv))
            self.assertNotIn("--dangerously-bypass-hook-trust", runner.argv)
            resume = [m for m in runner.log() if m["method"] == "thread/resume"][0]["params"]
            self.assertEqual(resume["sandbox"], "danger-full-access")
            self.assertEqual(resume["config"], {"bypass_hook_trust": True})

    def test_trusted_without_hook_falls_back(self):
        runner = FakeAppServerRunner(0.1)
        adapter(runner, hook="/nonexistent/hook.py").send(TID, "hi", timeout=20, mode="trusted",
                                                          approval_dir="/tmp/appr")
        resume = [m for m in runner.log() if m["method"] == "thread/resume"][0]["params"]
        self.assertEqual(resume["sandbox"], "workspace-write")
        self.assertNotIn("config", resume)

    def test_model_and_effort_on_turn(self):
        runner = FakeAppServerRunner(0.1)
        adapter(runner).send(TID, "hi", timeout=20, mode="write", model="gpt-6-astra", effort="high")
        start = [m for m in runner.log() if m["method"] == "turn/start"][0]["params"]
        self.assertEqual(start["model"], "gpt-6-astra")
        self.assertEqual(start["effort"], "high")

    def test_bad_model_rejected_before_spawn(self):
        runner = FakeAppServerRunner(0.1)
        res = adapter(runner).send(TID, "hi", timeout=20, mode="write", model="-bad")
        self.assertFalse(res.ok)
        self.assertIsNone(runner.argv)


if __name__ == "__main__":
    unittest.main()
