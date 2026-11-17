"""1.2.0 从 GitHub 自升级（tools/selfupgrade.py），UP-01 ~ UP-22。"""

import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("selfupgrade", os.path.join(ROOT, "tools", "selfupgrade.py"))
selfupgrade = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(selfupgrade)

LS = """aaa\trefs/tags/1.0.0
bbb\trefs/tags/1.0.0^{}
ccc\trefs/tags/1.0.1
ddd\trefs/tags/1.0.1^{}
eee\trefs/tags/1.10.0
fff\trefs/tags/v2.0.0
ggg\trefs/tags/1.2.0-rc1
"""


class TagTest(unittest.TestCase):
    def test_up01_newest_semver_uses_peeled_commit(self):
        self.assertEqual(selfupgrade.newest_tag(LS), ("1.10.0", "eee"))
        self.assertEqual(selfupgrade.newest_tag("aaa\trefs/tags/1.0.0\nbbb\trefs/tags/1.0.0^{}\n"), ("1.0.0", "bbb"))
        self.assertEqual(selfupgrade.newest_tag("x\trefs/heads/main\n"), ("", ""))

    def test_up02_version_compare_numeric(self):
        self.assertGreater(selfupgrade.version("1.10.0"), selfupgrade.version("1.9.9"))
        self.assertIsNone(selfupgrade.version("v1.0.0"))
        self.assertIsNone(selfupgrade.version("1.0"))


class FakeWorld:
    """假的 git / launchctl / rsync / unittest：按场景决定结果，记录调用。"""

    def __init__(self, home, *, remote_tag="1.2.0", tests_ok=True, start_ok=True, stable=True, clone_ok=True):
        self.home = home
        self.remote_tag = remote_tag
        self.tests_ok, self.start_ok, self.stable, self.clone_ok = tests_ok, start_ok, stable, clone_ok
        self.calls = []
        self.pid = 100
        self.clock = 1_800_000_000.0
        self.new_bridge = "NEW"

    def run(self, argv, cwd=None, env=None, capture_output=True, text=True, timeout=None):
        self.calls.append(argv)
        out = SimpleNamespace(returncode=0, stdout="", stderr="")
        if argv[:2] == ["git", "ls-remote"]:
            out.stdout = f"abc123\trefs/tags/1.0.1\ndef456\trefs/tags/{self.remote_tag}\n"
        elif argv[:2] == ["git", "clone"]:
            if not self.clone_ok:
                out.returncode, out.stderr = 128, "fatal: network"
                return out
            src = argv[-1]
            os.makedirs(os.path.join(src, "bridge"), exist_ok=True)
            with open(os.path.join(src, "bridge", "app.py"), "w") as fh:
                fh.write(self.new_bridge)
        elif argv[:2] == ["git", "rev-parse"]:
            out.stdout = "def456\n"
        elif "unittest" in argv:
            out.returncode = 0 if self.tests_ok else 1
            out.stderr = "Ran 300 tests\nOK" if self.tests_ok else "FAILED (failures=2)"
        elif argv[0] == "rsync":
            subprocess.run(argv, check=True)
        elif argv[:2] == ["launchctl", "kickstart"]:
            self.pid += 1
            bridge_code = open(os.path.join(self.home, "bridge", "app.py")).read()
            good = self.start_ok or bridge_code != self.new_bridge
            if good:
                with open(os.path.join(self.home, "logs", "agent.log"), "a") as fh:
                    fh.write("2026-11-17 23:00:00 INFO 桥已启动。\nconnected to wss://x\n")
        elif argv[:2] == ["launchctl", "print"]:
            alive = self.stable or open(os.path.join(self.home, "bridge", "app.py")).read() != self.new_bridge
            out.stdout = f"\tstate = {'running' if alive else 'exited'}\n\tpid = {self.pid}\n"
        return out

    def sleep(self, sec):
        self.clock += sec

    def now(self):
        return self.clock


class UpgradeFlowTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        os.makedirs(os.path.join(self.home, "bridge"))
        os.makedirs(os.path.join(self.home, "logs"))
        os.makedirs(os.path.join(self.home, "venv", "bin"))
        with open(os.path.join(self.home, "bridge", "app.py"), "w") as fh:
            fh.write("OLD")
        open(os.path.join(self.home, "logs", "agent.log"), "w").close()
        self.notes = []

    def make(self, **kw):
        world = FakeWorld(self.home, **kw)
        up = selfupgrade.Upgrader(self.home, "git@example:x.git", run=world.run, sleep=world.sleep, now=world.now,
                                  notify=self.notes.append)
        return world, up

    def bridge_code(self):
        return open(os.path.join(self.home, "bridge", "app.py")).read()

    def test_up03_noop_when_installed_is_latest(self):
        world, up = self.make(remote_tag="1.0.1")
        up.save({"installed": "1.0.1"})
        self.assertEqual(up.tick(), "noop")
        self.assertFalse(any(c[:2] == ["git", "clone"] for c in world.calls))

    def test_up04_happy_path_installs_and_records(self):
        world, up = self.make()
        up.save({"installed": "1.0.1"})
        self.assertEqual(up.tick(), "installed")
        self.assertEqual(self.bridge_code(), "NEW")
        state = up.state()
        self.assertEqual((state["installed"], state["installed_sha"]), ("1.2.0", "def456"))
        self.assertIn("✅", self.notes[-1])
        self.assertTrue(any(p.startswith("bridge.bak-upgrade-") for p in os.listdir(self.home)))

    def test_up05_tests_fail_marks_bad_and_keeps_running_code(self):
        world, up = self.make(tests_ok=False)
        up.save({"installed": "1.0.1"})
        self.assertEqual(up.tick(), "failed")
        self.assertEqual(self.bridge_code(), "OLD")
        self.assertIn("1.2.0", up.state()["bad"])
        self.assertFalse(any(c[:2] == ["launchctl", "kickstart"] for c in world.calls))
        self.assertEqual(up.tick(), "bad")

    def test_up06_service_not_starting_rolls_back(self):
        world, up = self.make(start_ok=False)
        up.save({"installed": "1.0.1"})
        self.assertEqual(up.tick(), "rolled_back")
        self.assertEqual(self.bridge_code(), "OLD")
        self.assertEqual(up.state()["installed"], "1.0.1")
        self.assertIn("1.2.0", up.state()["bad"])
        self.assertIn("服务已恢复", self.notes[-1])

    def test_up07_crash_after_start_rolls_back(self):
        world, up = self.make(stable=False)
        up.save({"installed": "1.0.1"})
        self.assertEqual(up.tick(), "rolled_back")
        self.assertEqual(self.bridge_code(), "OLD")

    def test_up08_busy_waits(self):
        world, up = self.make()
        up.save({"installed": "1.0.1"})
        with open(os.path.join(self.home, "inflight.json"), "w") as fh:
            json.dump({"dm:x": {"tool": "claude"}}, fh)
        self.assertEqual(up.tick(), "busy")
        self.assertEqual(self.bridge_code(), "OLD")

    def test_up09_clone_failure_not_marked_bad(self):
        world, up = self.make(clone_ok=False)
        up.save({"installed": "1.0.1"})
        self.assertEqual(up.tick(), "failed")
        self.assertNotIn("1.2.0", up.state().get("bad", {}))

    def test_up10_newer_tag_after_bad_is_tried(self):
        world, up = self.make(remote_tag="1.2.1")
        up.save({"installed": "1.0.1", "bad": {"1.2.0": "测试没过"}})
        self.assertEqual(up.tick(), "installed")

    def test_up11_dry_run_does_nothing(self):
        world, up = self.make()
        up.save({"installed": "1.0.1"})
        self.assertEqual(up.tick(dry_run=True), "would_install")
        self.assertEqual(self.bridge_code(), "OLD")

    def test_up15_restart_clears_guard_crash_count(self):
        world, up = self.make(start_ok=False)
        up.save({"installed": "1.0.1"})
        state_file = os.path.join(self.home, "crashloop.state")
        open(state_file, "w").write("streak=7\n")
        up.tick()
        self.assertFalse(os.path.exists(state_file))

    def test_up12_old_backups_pruned(self):
        for i in range(8):
            os.makedirs(os.path.join(self.home, f"bridge.bak-upgrade-2026010{i}-000000"))
        world, up = self.make()
        up.save({"installed": "1.0.1"})
        up.tick()
        left = [p for p in os.listdir(self.home) if p.startswith("bridge.bak-upgrade-")]
        self.assertEqual(len(left), selfupgrade.KEEP_BACKUPS)


class NoOfficialReportTest(unittest.TestCase):
    def test_up13_guard_skips_report_with_local_deploy(self):
        with open(os.path.join(ROOT, "installers", "launch-guard.sh"), encoding="utf-8") as fh:
            text = fh.read()
        body = text[text.index("report_crashloop() {"):]
        self.assertLess(body.index('.local-deploy" ] && return 0'), body.index("curl "))

    def test_up14_update_word_reports_github_status(self):
        from bridge import app as app_mod

        text = app_mod._github_upgrade_status()
        self.assertIn("GitHub", text)


class NotifyTest(unittest.TestCase):
    """UP-20 ~ UP-22：通知读 [feishu] 段，逐个目标发，机器人不在群里时改用 lark-cli 以用户身份发群。"""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)
        with open(os.path.join(self.home, "config.toml"), "w", encoding="utf-8") as fh:
            fh.write('[feishu]\napp_id = "cli_x"\napp_secret = "s"\nowner_open_ids = ["ou_owner"]\n')
        self.calls, self.posts = [], []

    def make(self, replies, cli_rc=0):
        def run(argv, **kw):
            self.calls.append(argv)
            return SimpleNamespace(returncode=cli_rc, stdout="", stderr="")
        up = selfupgrade.Upgrader(self.home, "git@example:x.git", run=run, sleep=lambda s: None,
                                  now=lambda: 0.0, notify=lambda t: None)

        def http_json(url, body, token=""):
            self.posts.append((url, body))
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t"}
            return replies.pop(0)
        up.http_json = http_json
        return up

    def test_up20_reads_feishu_section(self):
        cfg = self.make([]).feishu_cfg()
        self.assertEqual((cfg["app_id"], cfg["owners"]), ("cli_x", ["ou_owner"]))

    def test_up21_bot_out_of_group_falls_back_to_user(self):
        up = self.make([{"code": 0}, {"code": 230002, "msg": "Bot/User can NOT be out of the chat."}])
        up._notify_feishu("hello")
        sent = [b["receive_id"] for u, b in self.posts if "messages" in u]
        self.assertEqual(sent, ["ou_owner", selfupgrade.DEV_CHAT])
        cli = [c for c in self.calls if "+messages-send" in c]
        self.assertEqual(len(cli), 1)
        self.assertIn(selfupgrade.DEV_CHAT, cli[0])
        self.assertEqual(cli[0][cli[0].index("--as") + 1], "user")

    def test_up22_owner_failure_does_not_block_group(self):
        up = self.make([{"code": 99991663, "msg": "x"}, {"code": 0}])
        up._notify_feishu("hello")
        sent = [b["receive_id"] for u, b in self.posts if "messages" in u]
        self.assertEqual(sent, ["ou_owner", selfupgrade.DEV_CHAT])
        self.assertFalse([c for c in self.calls if "+messages-send" in c])


if __name__ == "__main__":
    unittest.main()
