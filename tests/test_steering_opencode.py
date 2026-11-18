"""中途插话 1.8.0：opencode 走本机常驻服务，`delivery: steer` 插进同一轮（docs/product/steering.md）。"""
import json
import threading
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge.fleet import opencode as oc
from bridge.fleet.model import FleetJobs, can
from bridge.fleet.opencode import OpencodeAdapter

SID = "ses_e1a194299ffeqZKoHAd6baW4t6"
RUN_OUT = json.dumps({"type": "text", "sessionID": SID, "part": {"type": "text", "text": "OC-OK"}}) + "\n"


def res(stdout="", rc=0):
    return SimpleNamespace(stdout=stdout, stderr="", returncode=rc, error="", timed_out=False)


class FakeRunner:
    """按命令分派：service status / api / run。run 可以挂住，模拟一轮在跑。"""

    def __init__(self, service=True, active=True, delivery="steer"):
        self.service, self.active, self.delivery = service, active, delivery
        self.calls = []
        self.release = threading.Event()
        self.running = threading.Event()

    def run(self, argv, **kw):
        argv = list(argv)
        self.calls.append(argv)
        if argv[1:3] == ["service", "status"]:
            return res("http://127.0.0.1:49374\n" if self.service else "", 0 if self.service else 1)
        if argv[1] == "api":
            method, path = argv[2], argv[3]
            if path == "/api/session/active":
                return res(json.dumps({"data": {SID: {}} if self.active else {}}))
            if path.endswith("/prompt"):
                body = json.loads(argv[argv.index("-d") + 1])
                return res(json.dumps({"data": {"id": "msg_1", "delivery": self.delivery, "payload": body}}))
            if path.endswith("/interrupt"):
                return res(json.dumps({"interrupted": True}))
            return res("{}")
        if argv[1] == "run":
            self.running.set()
            on_spawn = kw.get("on_spawn")
            if on_spawn:
                on_spawn(4242)
            self.release.wait(5)
            return res(RUN_OUT)
        raise AssertionError(argv)


class Sess:
    cwd = "/tmp"


def adapter(runner):
    ad = OpencodeAdapter(jobs=FleetJobs(lstart_of=lambda pid: "x"), runner=runner, db_path="/nonexistent",
                         binary="/bin/opencode")
    ad.find = lambda sid: Sess()
    return ad


def send_in_background(ad):
    box = {}
    th = threading.Thread(target=lambda: box.update(out=ad.send(SID, "原任务", mode="trusted")))
    th.start()
    return th, box


class OpencodeSteerTest(unittest.TestCase):
    def test_caps(self):
        self.assertTrue(can("opencode", "steer"))

    def test_send_uses_service_and_steer_lands(self):
        runner = FakeRunner()
        ad = adapter(runner)
        th, box = send_in_background(ad)
        self.assertTrue(runner.running.wait(3))
        run_argv = [c for c in runner.calls if c[1] == "run"][0]
        self.assertNotIn("--standalone", run_argv)
        self.assertTrue(ad.steerable(SID))
        got = ad.steer(SID, "改主意了")
        self.assertTrue(got.ok)
        self.assertEqual(got.text, oc.STEER_OK)
        prompt = [c for c in runner.calls if c[1] == "api" and c[3].endswith("/prompt")][0]
        self.assertEqual(json.loads(prompt[prompt.index("-d") + 1]), {"text": "改主意了", "delivery": "steer"})
        runner.release.set()
        th.join(5)
        self.assertTrue(box["out"].ok)
        self.assertFalse(ad.steerable(SID))

    def test_no_service_falls_back_to_standalone_and_no_steer(self):
        runner = FakeRunner(service=False)
        ad = adapter(runner)
        th, _ = send_in_background(ad)
        self.assertTrue(runner.running.wait(3))
        self.assertIn("--standalone", [c for c in runner.calls if c[1] == "run"][0])
        self.assertFalse(ad.steerable(SID))
        self.assertFalse(ad.steer(SID, "x").ok)
        runner.release.set()
        th.join(5)

    def test_turn_not_active_in_service_is_not_steered(self):
        runner = FakeRunner(active=False)
        ad = adapter(runner)
        th, _ = send_in_background(ad)
        self.assertTrue(runner.running.wait(3))
        self.assertFalse(ad.steer(SID, "x").ok)
        self.assertFalse([c for c in runner.calls if c[1] == "api" and c[3].endswith("/prompt")])
        runner.release.set()
        th.join(5)

    def test_queued_delivery_counts_as_not_steered(self):
        runner = FakeRunner(delivery="queue")
        ad = adapter(runner)
        th, _ = send_in_background(ad)
        self.assertTrue(runner.running.wait(3))
        self.assertFalse(ad.steer(SID, "x").ok)
        runner.release.set()
        th.join(5)

    def test_idle_session_not_steerable(self):
        ad = adapter(FakeRunner())
        self.assertFalse(ad.steer(SID, "x").ok)
        self.assertFalse(ad.steer("not-an-id", "x").ok)

    def test_stop_interrupts_service_turn(self):
        runner = FakeRunner()
        ad = adapter(runner)
        stopped = []
        ad.stop_own = lambda sid: stopped.append(sid) or SimpleNamespace(ok=True)
        th, _ = send_in_background(ad)
        self.assertTrue(runner.running.wait(3))
        ad.stop(SID)
        self.assertTrue([c for c in runner.calls if c[1] == "api" and c[3].endswith("/interrupt")])
        self.assertEqual(stopped, [SID])
        runner.release.set()
        th.join(5)


if __name__ == "__main__":
    unittest.main()
