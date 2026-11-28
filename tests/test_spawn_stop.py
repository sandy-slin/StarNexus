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


class _ClaudeTermAdapter:
    """控制层路由测试用：记录 term/relay/send 哪条被调了。

    `term_can`：can_term_send 的返回 (can, why)。`term_ok`：term_send/term_key 成功与否。
    """

    def __init__(self, *, term_can=(False, ""), term_ok=True, relay_can=(False, "")):
        self._term_can = term_can
        self._term_ok = term_ok
        self._relay_can = relay_can
        self.called = []

    def can_term_send(self, sid, *, for_key=False):
        self.called.append(("can_term_send", for_key))
        return self._term_can

    def term_send(self, sid, text):
        self.called.append(("term_send", text))
        return ActionResult(self._term_ok, "" if self._term_ok else "打不进", sid)

    def term_key(self, sid, key):
        self.called.append(("term_key", key))
        return ActionResult(self._term_ok, "", sid)

    def can_relay(self, sid):
        return self._relay_can

    def relay(self, sid, text, timeout=120.0, mode=None):
        self.called.append(("relay", text))
        return ActionResult(True, "relay 送了", sid)

    def send(self, sid, text, **kwargs):
        self.called.append(("send", text))
        return ActionResult(True, "send 跑了", sid)

    def stop(self, sid):
        self.called.append(("stop", sid))
        return ActionResult(False, "停不了", sid)

    def relay_stop(self, sid):
        self.called.append(("relay_stop", sid))
        return ActionResult(False, "到电脑上按 Esc", sid)


SID = "11111111-2222-4333-8444-555555555555"


class ClaudeTermRoutingTest(unittest.TestCase):
    """termsend 排在 relay 之前，且只接斜杠命令；普通话永远不走终端。"""

    def _control(self, adapter):
        return FleetControl(FakeFleet({"claude": adapter}))

    def test_slash_command_goes_to_terminal_first(self):
        a = _ClaudeTermAdapter(term_can=(True, ""))
        out = self._control(a).send("claude", SID, "/stop", relay=True)
        self.assertEqual(a.called[0], ("can_term_send", False))
        self.assertIn(("term_send", "/stop"), a.called)
        self.assertNotIn(("relay", "/stop"), a.called)  # 没落到 relay
        self.assertTrue(out.ok)

    def test_plain_text_never_touches_terminal(self):
        a = _ClaudeTermAdapter(term_can=(True, ""), relay_can=(True, ""))
        out = self._control(a).send("claude", SID, "改一下这个函数", relay=True)
        self.assertNotIn("can_term_send", [c[0] for c in a.called])  # wants_terminal 先挡了
        self.assertIn(("relay", "改一下这个函数"), a.called)
        self.assertTrue(out.ok)

    def test_terminal_fallback_empty_why_falls_through_to_relay(self):
        # can_term_send 回 (False, "") = 退回 ⇒ 斜杠命令继续走 relay（那边会给说清为什么的话）
        a = _ClaudeTermAdapter(term_can=(False, ""), relay_can=(True, ""))
        out = self._control(a).send("claude", SID, "/clear", relay=True)
        self.assertIn(("can_term_send", False), a.called)
        self.assertNotIn("term_send", [c[0] for c in a.called])
        self.assertIn(("relay", "/clear"), a.called)

    def test_terminal_real_no_stops_here(self):
        # can_term_send 回 (False, 有话) = 真做不到 ⇒ 照实说，不再往 relay 走
        a = _ClaudeTermAdapter(term_can=(False, "它正忙着"), relay_can=(True, ""))
        out = self._control(a).send("claude", SID, "/stop", relay=True)
        self.assertFalse(out.ok)
        self.assertEqual(out.text, "它正忙着")
        self.assertNotIn("relay", [c[0] for c in a.called])

    def test_stop_presses_esc_first(self):
        a = _ClaudeTermAdapter(term_can=(True, ""))
        out = self._control(a).stop("claude", SID, relay=True)
        self.assertEqual(a.called[0], ("can_term_send", True))  # for_key=True
        self.assertIn(("term_key", "esc"), a.called)
        self.assertNotIn("relay_stop", [c[0] for c in a.called])
        self.assertTrue(out.ok)

    def test_stop_falls_back_when_esc_not_possible(self):
        a = _ClaudeTermAdapter(term_can=(False, ""))
        out = self._control(a).stop("claude", SID, relay=True)
        self.assertIn(("relay_stop", SID), a.called)
        self.assertFalse(out.ok)


class CodexSandboxModeTest(unittest.TestCase):
    def test_mobile_start_defaults_to_trusted_mode(self):
        from inspect import signature
        from bridge.fleet.codex import CodexAdapter

        self.assertEqual(signature(CodexAdapter.start).parameters["mode"].default, "trusted")

    def test_trusted_is_full_access(self):
        from bridge.fleet.codex import SANDBOX_BY_MODE

        self.assertEqual(SANDBOX_BY_MODE["trusted"], "danger-full-access")
        self.assertEqual(SANDBOX_BY_MODE["write"], "workspace-write")
        self.assertEqual(SANDBOX_BY_MODE["readonly"], "read-only")



if __name__ == "__main__":
    unittest.main()


class CursorHiddenTest(unittest.TestCase):
    def test_default_fleet_has_no_cursor(self):
        import os
        import tempfile
        from unittest import mock

        from bridge.fleet import control as control_mod

        with mock.patch.dict(os.environ, {"CARHER_FLEET_CURSOR": ""}):
            fleet = control_mod.build_default(policy=lambda: None, bridge_home=tempfile.mkdtemp())
        self.assertIsNone(fleet.fleet.adapter("cursor"))
        self.assertIsNotNone(fleet.fleet.adapter("codex"))
        with mock.patch.dict(os.environ, {"CARHER_FLEET_CURSOR": "1"}):
            fleet = control_mod.build_default(policy=lambda: None, bridge_home=tempfile.mkdtemp())
        self.assertIsNotNone(fleet.fleet.adapter("cursor"))
