"""中途插话（1.7.0，docs/product/steering.md）：Claude 一轮在跑时把话写进去；群里不再冒无需处理的提示。"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet import claude as claude_mod
from bridge.fleet.claude import STEER_OK, ClaudeAdapter, _stream_argv
from bridge.fleet.model import ActionResult, FleetJobs, SubprocessRunner, can

SID = "11111111-2222-3333-4444-555555555555"
FAKE = os.path.join(os.path.dirname(__file__), "fake_stream_claude.py")


class FakeClaudeRunner(SubprocessRunner):
    def __init__(self, wait, late=False):
        self.wait = wait
        self.late = late

    def popen(self, argv, *, cwd=None, env=None):
        self.argv = argv
        return subprocess.Popen([sys.executable, FAKE, str(self.wait)] + (["late"] if self.late else []), stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)


def adapter(wait, late=False):
    ad = ClaudeAdapter(home="/tmp/sn-steer-none", bridge_home="/tmp/sn-steer-none",
                       jobs=FleetJobs(lstart_of=lambda pid: "x"), runner=FakeClaudeRunner(wait, late))
    return ad


class StreamArgvTest(unittest.TestCase):
    def test_prompt_removed_and_stream_flags(self):
        argv = ["claude", "-p", "hi", "--resume", SID, "--output-format", "json", "--permission-mode", "manual"]
        out = _stream_argv(argv)
        self.assertNotIn("hi", out)
        self.assertEqual(out[:4], ["claude", "-p", "--resume", SID])
        for flag in ("--input-format", "--replay-user-messages", "--verbose"):
            self.assertIn(flag, out)
        self.assertEqual(out[out.index("--output-format") + 1], "stream-json")
        self.assertIn("manual", out)

    def test_caps(self):
        self.assertTrue(can("claude", "steer"))
        self.assertFalse(can("cursor", "steer"))
        self.assertTrue(can("codex", "steer"))


class ClaudeSteerTest(unittest.TestCase):
    def run_turn(self, ad, text="原任务"):
        box = {}
        th = threading.Thread(target=lambda: box.update(res=ad._run_streaming(
            SID, ["claude", "-p", text, "--output-format", "json"], text, 20, "/tmp", None, lambda pid: None)))
        th.start()
        return th, box

    def wait_live(self, ad):
        for _ in range(100):
            if ad.steerable(SID):
                return True
            time.sleep(0.02)
        return False

    def test_steer_lands_in_same_turn(self):
        ad = adapter(1.5)
        th, box = self.run_turn(ad)
        self.assertTrue(self.wait_live(ad))
        got = ad.steer(SID, "改主意了")
        self.assertTrue(got.ok)
        self.assertEqual(got.text, STEER_OK)
        th.join(15)
        payload = json.loads(box["res"].stdout)
        self.assertEqual(payload["result"], "GOT:原任务|改主意了")
        self.assertEqual(box["res"].returncode, 0)
        self.assertFalse(ad.steerable(SID))

    def test_steer_racing_the_end_is_not_lost(self):
        ad = adapter(1.0, late=True)
        th, box = self.run_turn(ad)
        self.assertTrue(self.wait_live(ad))
        self.assertTrue(ad.steer(SID, "撞上收尾").ok)
        th.join(15)
        self.assertEqual(json.loads(box["res"].stdout)["result"], "GOT:原任务\n\nLATE:撞上收尾")
        self.assertEqual(box["res"].returncode, 0)

    def test_steer_after_turn_ended_is_refused(self):
        ad = adapter(0)
        th, box = self.run_turn(ad)
        th.join(15)
        self.assertEqual(json.loads(box["res"].stdout)["result"], "GOT:原任务")
        got = ad.steer(SID, "晚了")
        self.assertFalse(got.ok)

    def test_one_shot_runner_path_unchanged(self):
        ad = ClaudeAdapter(home="/tmp/sn-steer-none", jobs=FleetJobs(lstart_of=lambda pid: "x"),
                           runner=SimpleNamespace(run=lambda *a, **k: None))
        self.assertFalse(ad.steerable(SID))
        self.assertFalse(ad.steer(SID, "x").ok)


class FakeControl:
    def __init__(self, sess, steer_ok=True):
        self.sess = sess
        self.pending = {}
        self.steered = []
        self.steer_ok = steer_ok

    def refresh(self, tool, sid):
        return self.sess, ""

    def cached(self, tool, sid):
        return self.sess

    def peek_pending_fresh(self, scope):
        return self.pending.get(scope), False

    def peek_pending(self, scope):
        return self.pending.get(scope)

    def set_pending(self, scope, tool, sid):
        target = SimpleNamespace(tool=tool, id=sid, label="Claude「x」")
        self.pending[scope] = target
        return target

    def clear_pending(self, scope):
        self.pending.pop(scope, None)

    def steer(self, tool, sid, text):
        self.steered.append((tool, sid, text))
        return ActionResult(self.steer_ok, STEER_OK if self.steer_ok else "", sid)


def bridge_with(sess, steer_ok=True):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.fleet = FakeControl(sess, steer_ok)
    bridge.reviewer = None
    bridge._group_bind_arm = lambda *a: asyncio.sleep(0, result=False)
    bridge._restart_talk_offer = lambda *a: False
    bridge._session_policy = lambda tool, sid: SimpleNamespace(mode="trusted")
    bridge._talk_buttons = lambda tool, sid: []
    said = []
    bridge._say = lambda chat_id, text: said.append(text) or True
    bridge._say_with_buttons = lambda chat_id, text, buttons: said.append(text) or True
    return bridge, said


class BridgeSteerTest(unittest.TestCase):
    def test_same_session_busy_is_steered(self):
        sess = SimpleNamespace(tool="claude", id=SID, owner="bridge", title="x", cwd="/tmp")
        bridge, said = bridge_with(sess)
        bridge.fleet.set_pending("group:oc_g", "claude", SID)
        bridge._fleet_inflight = {"group:oc_g": ("claude", SID)}
        handled = asyncio.run(bridge._maybe_send_to_fleet("group:oc_g", "oc_g", "停下剩下的"))
        self.assertTrue(handled)
        self.assertEqual(bridge.fleet.steered, [("claude", SID, "停下剩下的")])
        self.assertEqual(said, [STEER_OK])

    def test_steer_refused_falls_back_to_busy_notice(self):
        sess = SimpleNamespace(tool="claude", id=SID, owner="terminal", title="x", cwd="/tmp")
        bridge, said = bridge_with(sess, steer_ok=False)
        bridge.fleet.set_pending("group:oc_g", "claude", SID)
        bridge._fleet_inflight = {"group:oc_g": ("claude", SID)}
        handled = asyncio.run(bridge._maybe_send_to_fleet("group:oc_g", "oc_g", "再说一句"))
        self.assertTrue(handled)
        self.assertEqual(said, ["上一条还在发，等它回来再说。"])

    def test_codex_busy_is_steered(self):
        sess = SimpleNamespace(tool="codex", id="t1", owner="bridge", title="x", cwd="/tmp")
        bridge, said = bridge_with(sess)
        bridge.fleet.set_pending("group:oc_g", "codex", "t1")
        bridge._fleet_inflight = {"group:oc_g": ("codex", "t1")}
        asyncio.run(bridge._maybe_send_to_fleet("group:oc_g", "oc_g", "x"))
        self.assertEqual(bridge.fleet.steered, [("codex", "t1", "x")])
        self.assertEqual(said, [STEER_OK])

    def test_cursor_not_steered(self):
        sess = SimpleNamespace(tool="cursor", id="c1", owner="none", title="x", cwd="/tmp")
        bridge, said = bridge_with(sess)
        bridge.fleet.set_pending("group:oc_g", "cursor", "c1")
        bridge._fleet_inflight = {"group:oc_g": ("cursor", "c1")}
        asyncio.run(bridge._maybe_send_to_fleet("group:oc_g", "oc_g", "x"))
        self.assertEqual(bridge.fleet.steered, [])
        self.assertEqual(said, ["上一条还在发，等它回来再说。"])

    def test_paused_not_steered(self):
        sess = SimpleNamespace(tool="claude", id=SID, owner="bridge", title="x", cwd="/tmp")
        bridge, _said = bridge_with(sess)
        bridge._paused = {"group:oc_g": "x"}
        target = SimpleNamespace(tool="claude", id=SID, label="x")
        self.assertFalse(asyncio.run(bridge._try_steer("group:oc_g", "oc_g", target, "x")))
        self.assertEqual(bridge.fleet.steered, [])


class NoNeedlessHintTest(unittest.TestCase):
    def test_bridge_owned_turn_counts_as_background(self):
        self.assertTrue(app_mod.Bridge._runs_in_background(SimpleNamespace(tool="claude", owner="bridge")))
        self.assertTrue(app_mod.Bridge._runs_in_background(SimpleNamespace(tool="claude", owner="none")))
        self.assertFalse(app_mod.Bridge._runs_in_background(SimpleNamespace(tool="claude", owner="terminal")))

    def test_group_message_after_change_has_no_hint(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        sess = SimpleNamespace(tool="claude", id=SID, status="idle", name="x", title="x", cwd="/tmp",
                               owner="terminal")
        bridge.fleet = FakeControl(sess)
        bound = SimpleNamespace(tool="claude", id=SID, chat_id="oc_g", scope="group:oc_g", on=True, name="SN·Claude")
        bridge.chat_binds = SimpleNamespace(active_of_scope=lambda scope: bound)
        bridge.relay_consent = SimpleNamespace(granted=lambda *a: False, grant=lambda *a: None,
                                               latest_ident=lambda *a: "旧\x00/old\x001")
        bridge._talk_changed_why = lambda *a: "电脑上那个会话已经关了"
        said = []
        bridge._ask_relay_consent = lambda *a: None
        bridge._say = lambda chat_id, text: said.append(text) or True
        bridge._say_with_buttons = lambda chat_id, text, buttons: said.append(text) or True
        handled = asyncio.run(bridge._group_bind_arm("group:oc_g", "oc_g", "你好"))
        self.assertFalse(handled)
        self.assertEqual(said, [])


if __name__ == "__main__":
    unittest.main()
