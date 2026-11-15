import unittest
from types import SimpleNamespace

from bridge.fleet.control import FleetControl
from bridge.fleet.model import ActionResult


class FakeAdapter:
    def __init__(self):
        self.calls = []
        self.jobs = SimpleNamespace(signal=lambda pid: ActionResult(True, f"停了 {pid}", ""))

    def start(self, cwd, text, **kwargs):
        self.calls.append(kwargs)
        if kwargs.get("on_pid"):
            kwargs["on_pid"](77)
        return ActionResult(True, "ok", "")


class FakeFleet:
    def __init__(self, adapters):
        self.adapters = adapters

    def adapter(self, tool):
        return self.adapters.get(tool)


class ControlOnPidTest(unittest.TestCase):
    def setUp(self):
        self.codex, self.cursor = FakeAdapter(), FakeAdapter()
        self.control = FleetControl(FakeFleet({"codex": self.codex, "cursor": self.cursor}))
        self.policy = SimpleNamespace(resolve_workspace=lambda p: p, mode="trusted")

    def test_on_pid_forwarded_for_codex(self):
        got = []
        self.control.start("codex", "/tmp", "hi", policy=self.policy, on_pid=got.append)
        self.assertEqual(got, [77])

    def test_codex_start_follows_policy_mode(self):
        self.control.start("codex", "/tmp", "hi", policy=self.policy)
        self.assertEqual(self.codex.calls[0]["mode"], "trusted")


    def test_on_pid_not_forwarded_for_cursor(self):
        self.control.start("cursor", "/tmp", "hi", policy=self.policy, on_pid=lambda pid: None)
        self.assertNotIn("on_pid", self.cursor.calls[0])

    def test_signal_spawned_uses_adapter_jobs(self):
        res = self.control.signal_spawned("codex", 77)
        self.assertTrue(res.ok)
        self.assertFalse(self.control.signal_spawned("nope", 77).ok)


class CodexSandboxModeTest(unittest.TestCase):
    def test_trusted_is_workspace_write(self):
        from bridge.fleet.codex import SANDBOX_BY_MODE

        self.assertEqual(SANDBOX_BY_MODE["trusted"], "workspace-write")
        self.assertEqual(SANDBOX_BY_MODE["write"], "workspace-write")
        self.assertEqual(SANDBOX_BY_MODE["readonly"], "read-only")



if __name__ == "__main__":
    unittest.main()
