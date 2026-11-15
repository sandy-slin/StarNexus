import os
import tempfile
import threading
import unittest
from types import SimpleNamespace

import tests.helpers  # noqa: F401
from bridge.fleet import ceo
from bridge.fleet.model import ActionResult


class FakeBrain:
    def __init__(self, plan, reviews):
        self.plan = plan
        self.reviews = list(reviews)
        self.prompts = []

    def ask(self, prompt, schema):
        self.prompts.append(prompt)
        if schema is ceo.PLAN_SCHEMA:
            if isinstance(self.plan, Exception):
                raise self.plan
            return self.plan
        if not self.reviews:
            return {"summary": "收尾", "decision": "finish", "report": "完成"}
        r = self.reviews.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeControl:
    def __init__(self, fail_tools=()):
        self.starts, self.sends, self.stops = [], [], []
        self.fail_tools = fail_tools
        self.lock = threading.Lock()

    def start(self, tool, cwd, text, *, policy=None, approval_dir="", timeout=600, model="", on_pid=None):
        with self.lock:
            self.starts.append((tool, cwd, text, model))
            n = len(self.starts)
        if tool in self.fail_tools:
            return ActionResult(False, "起不来", "")
        return ActionResult(True, f"{tool} 结果 {n}", f"sid-{tool}-{n}")

    def send(self, tool, sid, text, *, policy=None, approval_dir="", timeout=600, relay=False):
        with self.lock:
            self.sends.append((tool, sid, text, relay))
        return ActionResult(True, f"追加结果 {sid}", sid)

    def stop(self, tool, sid, relay=False):
        self.stops.append((tool, sid))
        return ActionResult(True, "停了", sid)


PLAN = {"summary": "分两路", "subtasks": [
    {"title": "改代码", "tool": "claude", "model": "claude-opus-5-5", "project": 1, "brief": "实现功能"},
    {"title": "评审", "tool": "codex", "model": "gpt-6-sol", "project": 0, "brief": "评审方案"},
]}


class CeoTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "ceo.json")
        self.said = []

    def orch(self, brain, control, store=None):
        store = store or ceo.MissionStore(self.path)
        return ceo.Orchestrator(
            store, brain, control, lambda chat, text: self.said.append((chat, text)),
            policy=lambda: SimpleNamespace(mode="trusted"),
            projects=lambda: [("remote_control", "/w/rc"), ("her", "/w/her")],
            spawn=lambda fn: fn())

    def texts(self):
        return "\n".join(t for _, t in self.said)

    def test_plan_dispatch_finish(self):
        control = FakeControl()
        brain = FakeBrain(PLAN, [{"summary": "都好了", "decision": "finish", "report": "总结果：OK"}])
        o = self.orch(brain, control)
        ok, mid = o.start("oc_ceo", "做个功能")
        self.assertTrue(ok)
        m = o.store.missions[mid]
        self.assertEqual(m["state"], "done")
        self.assertEqual({(s[0], s[1], s[3]) for s in control.starts},
                         {("claude", "/w/her", "claude-opus-5-5"), ("codex", "/w/rc", "gpt-6-sol")})
        self.assertIn("CEO 分工", self.texts())
        self.assertIn("总结果：OK", self.texts())
        self.assertTrue(all(s["session_id"] for s in m["subtasks"]))
        self.assertIn("claude 结果", brain.prompts[-1])

    def test_followup_goes_to_same_session(self):
        control = FakeControl()
        brain = FakeBrain(PLAN, [
            {"summary": "评审发现问题", "decision": "continue", "followups": [{"n": 1, "instruction": "按评审修"}]},
            {"summary": "好了", "decision": "finish", "report": "交付"},
        ])
        o = self.orch(brain, control)
        _, mid = o.start("oc_ceo", "做个功能")
        self.assertEqual(len(control.starts), 2)
        self.assertEqual(len(control.sends), 1)
        tool, sid, text, relay = control.sends[0]
        self.assertEqual(tool, "claude")
        self.assertTrue(sid.startswith("sid-claude"))
        self.assertEqual(text, "按评审修")
        self.assertTrue(relay)
        self.assertEqual(o.store.missions[mid]["round"], 2)

    def test_ask_human_then_answer(self):
        control = FakeControl()
        brain = FakeBrain(PLAN, [
            {"summary": "两个方案", "decision": "ask_human", "question": "选 A 还是 B？"},
            {"summary": "按 A 做完", "decision": "finish", "report": "按 A 交付"},
        ])
        o = self.orch(brain, control)
        _, mid = o.start("oc_ceo", "做个功能")
        m = o.store.missions[mid]
        self.assertEqual(m["state"], "waiting_human")
        self.assertIn("需要你拍板", self.texts())
        self.assertIn("选 A 还是 B", self.texts())
        self.assertTrue(o.handle_text("oc_ceo", "选 A"))
        self.assertEqual(m["state"], "done")
        self.assertIn("选 A", brain.prompts[-1])
        self.assertIn("按 A 交付", self.texts())

    def test_round_limit_forces_finish(self):
        control = FakeControl()
        loop = {"summary": "再来", "decision": "continue", "followups": [{"n": 2, "instruction": "再看"}]}
        brain = FakeBrain(PLAN, [loop] * 10)
        o = self.orch(brain, control)
        _, mid = o.start("oc_ceo", "做个功能")
        m = o.store.missions[mid]
        self.assertEqual(m["state"], "done")
        self.assertEqual(m["round"], ceo.MAX_ROUNDS)
        self.assertIn("最后一轮", brain.prompts[-1])

    def test_subtask_failure_reported_to_brain(self):
        control = FakeControl(fail_tools=("codex",))
        brain = FakeBrain(PLAN, [{"summary": "codex 挂了", "decision": "finish", "report": "部分完成"}])
        o = self.orch(brain, control)
        _, mid = o.start("oc_ceo", "做个功能")
        subs = o.store.missions[mid]["subtasks"]
        self.assertEqual({s["tool"]: s["state"] for s in subs}, {"claude": "done", "codex": "failed"})
        self.assertIn("没做成", brain.prompts[-1])

    def test_unknown_decision_with_followups_respects_round_limit(self):
        control = FakeControl()
        loop = {"summary": "再来", "decision": "wait", "followups": [{"n": 2, "instruction": "再看"}]}
        brain = FakeBrain(PLAN, [loop] * 10)
        o = self.orch(brain, control)
        _, mid = o.start("oc_ceo", "做个功能")
        m = o.store.missions[mid]
        self.assertEqual(m["state"], "done")
        self.assertEqual(m["round"], ceo.MAX_ROUNDS)
        self.assertEqual(len(control.sends), ceo.MAX_ROUNDS - 1)

    def test_unknown_decision_without_followups_delivers_report(self):
        brain = FakeBrain(PLAN, [{"summary": "摘要", "decision": None, "report": "正式报告"}])
        o = self.orch(brain, FakeControl())
        _, mid = o.start("oc_ceo", "做个功能")
        m = o.store.missions[mid]
        self.assertEqual(m["state"], "done")
        self.assertEqual(m["report"], "正式报告")
        self.assertIn("交付", [e[1] for e in m["events"]])

    def test_followups_tolerate_bad_entries(self):
        control = FakeControl()
        brain = FakeBrain(PLAN, [
            {"summary": "改", "decision": "continue",
             "followups": [None, "x", {"n": "1", "instruction": "按评审修"}, {"n": 9, "instruction": "无"},
                           {"n": 2, "instruction": "  "}]},
            {"summary": "好了", "decision": "finish", "report": "交付"},
        ])
        o = self.orch(brain, control)
        _, mid = o.start("oc_ceo", "做个功能")
        self.assertEqual(o.store.missions[mid]["state"], "done")
        self.assertEqual([(t, x) for t, _, x, _ in control.sends], [("claude", "按评审修")])

    def test_continue_without_followups_uses_report(self):
        brain = FakeBrain(PLAN, [{"summary": "摘要", "decision": "continue", "followups": [], "report": "正式报告"}])
        o = self.orch(brain, FakeControl())
        _, mid = o.start("oc_ceo", "做个功能")
        m = o.store.missions[mid]
        self.assertEqual(m["report"], "正式报告")
        self.assertIn("正式报告", self.texts())
        self.assertIn("交付", [e[1] for e in m["events"]])

    def test_question_at_round_limit_asks_human_and_extends_limit(self):
        control = FakeControl()
        loop = {"summary": "再来", "decision": "continue", "followups": [{"n": 2, "instruction": "再看"}]}
        last = {"summary": "要取舍", "decision": "continue", "question": "A 还是 B？",
                "followups": [{"n": 2, "instruction": "再看"}]}
        brain = FakeBrain(PLAN, [loop] * (ceo.MAX_ROUNDS - 1) + [last])
        o = self.orch(brain, control)
        _, mid = o.start("oc_ceo", "做个功能")
        m = o.store.missions[mid]
        self.assertEqual(m["state"], "waiting_human")
        self.assertEqual(m["round"], ceo.MAX_ROUNDS)
        brain.reviews = [loop, {"summary": "好了", "decision": "finish", "report": "按 A 交付"}]
        self.assertTrue(o.handle_text("oc_ceo", "选 A"))
        self.assertEqual(m["limit"], ceo.MAX_ROUNDS * 2)
        self.assertEqual(m["state"], "done")
        self.assertEqual(m["round"], ceo.MAX_ROUNDS + 2)
        self.assertIn("按 A 交付", self.texts())

    def test_blank_text_ignored(self):
        o = self.orch(FakeBrain(PLAN, []), FakeControl())
        o.store.ceo_chats = getattr(o.store, "ceo_chats", None)
        self.assertFalse(o.handle_text("oc_ceo", ""))
        self.assertFalse(o.handle_text("oc_ceo", "   "))

    def test_plan_failure(self):
        o = self.orch(FakeBrain(ceo.BrainError("超时"), []), FakeControl())
        _, mid = o.start("oc_ceo", "做个功能")
        self.assertEqual(o.store.missions[mid]["state"], "failed")
        self.assertIn("没拆成", self.texts())

    def test_bad_model_and_tool_sanitized(self):
        plan = {"summary": "x", "subtasks": [
            {"title": "a", "tool": "cursor", "model": "rm -rf", "project": 99, "brief": "做 a"}]}
        control = FakeControl()
        o = self.orch(FakeBrain(plan, []), control)
        o.start("oc_ceo", "任务")
        tool, cwd, _, model = control.starts[0]
        self.assertEqual((tool, cwd, model), ("claude", "/w/rc", ""))

    def test_subtask_cap(self):
        plan = {"summary": "x", "subtasks": [{"title": f"t{i}", "tool": "codex", "brief": "b"} for i in range(9)]}
        control = FakeControl()
        o = self.orch(FakeBrain(plan, []), control)
        o.start("oc_ceo", "任务")
        self.assertEqual(len(control.starts), ceo.MAX_SUBTASKS)

    def test_one_active_mission_per_chat(self):
        store = ceo.MissionStore(self.path)
        o = ceo.Orchestrator(store, FakeBrain(PLAN, []), FakeControl(), lambda c, t: self.said.append((c, t)),
                             policy=lambda: None, projects=lambda: [("p", "/p")], spawn=lambda fn: None)
        self.assertTrue(o.start("oc_ceo", "任务一")[0])
        ok, msg = o.start("oc_ceo", "任务二")
        self.assertFalse(ok)
        self.assertIn("还有一个任务", msg)
        self.assertTrue(o.start("oc_other", "任务二")[0])

    def test_text_while_running_is_noted(self):
        store = ceo.MissionStore(self.path)
        o = ceo.Orchestrator(store, FakeBrain(PLAN, []), FakeControl(), lambda c, t: self.said.append((c, t)),
                             policy=lambda: None, projects=lambda: [("p", "/p")], spawn=lambda fn: None)
        _, mid = o.start("oc_ceo", "任务一")
        store.missions[mid]["state"] = "running"
        self.assertTrue(o.handle_text("oc_ceo", "顺便加个日志"))
        self.assertIn("顺便加个日志", store.missions[mid]["human"])

    def test_non_ceo_chat_ignored(self):
        o = self.orch(FakeBrain(PLAN, []), FakeControl())
        self.assertFalse(o.handle_text("oc_random", "你好"))
        self.assertEqual(self.said, [])

    def test_cancel_stops_running_subtasks(self):
        store = ceo.MissionStore(self.path)
        control = FakeControl()
        o = ceo.Orchestrator(store, FakeBrain(PLAN, []), control, lambda c, t: self.said.append((c, t)),
                             policy=lambda: None, projects=lambda: [("p", "/p")], spawn=lambda fn: None)
        _, mid = o.start("oc_ceo", "任务")
        store.missions[mid]["state"] = "running"
        store.missions[mid]["subtasks"] = [{"n": 1, "tool": "claude", "session_id": "s1", "state": "running"}]
        self.assertIn("已叫停", o.cancel("oc_ceo"))
        self.assertEqual(control.stops, [("claude", "s1")])
        self.assertEqual(store.missions[mid]["state"], "cancelled")

    def test_cancel_first_turn_signals_spawned_process(self):
        store = ceo.MissionStore(self.path)
        control = FakeControl()
        control.signals = []
        control.signal_spawned = lambda tool, pid: control.signals.append((tool, pid))
        o = ceo.Orchestrator(store, FakeBrain(PLAN, []), control, lambda c, t: None,
                             policy=lambda: None, projects=lambda: [("p", "/p")], spawn=lambda fn: None)
        _, mid = o.start("oc_ceo", "任务")
        store.missions[mid]["state"] = "running"
        store.missions[mid]["subtasks"] = [
            {"n": 1, "tool": "codex", "session_id": "", "pid": 4321, "state": "running"},
            {"n": 2, "tool": "claude", "session_id": "s2", "state": "running"},
            {"n": 3, "tool": "claude", "session_id": "s3", "state": "done"},
        ]
        o.cancel("oc_ceo")
        self.assertEqual(control.signals, [("codex", 4321)])
        self.assertEqual(control.stops, [("claude", "s2")])

    def test_dispatch_records_pid_then_clears_it(self):
        seen = []

        class PidControl(FakeControl):
            def start(self, tool, cwd, text, *, policy=None, approval_dir="", timeout=600, model="", on_pid=None):
                on_pid(999)
                seen.append(dict(sub_ref[0]))
                return super().start(tool, cwd, text, policy=policy, approval_dir=approval_dir,
                                     timeout=timeout, model=model)

        sub_ref = []
        store = ceo.MissionStore(self.path)
        o = ceo.Orchestrator(store, FakeBrain(PLAN, []), PidControl(), lambda c, t: None,
                             policy=lambda: None, projects=lambda: [("p", "/p")], spawn=lambda fn: None)
        _, mid = o.start("oc_ceo", "任务")
        m = store.missions[mid]
        m["state"] = "running"
        sub = {"n": 1, "title": "t", "tool": "codex", "model": "", "project": "p", "cwd": "/p", "brief": "b",
               "next": "b", "state": "running", "session_id": "", "result": "", "turns": 0}
        m["subtasks"] = [sub]
        sub_ref.append(sub)
        o._one(m, sub)
        self.assertEqual(seen[0].get("pid"), 999)
        self.assertNotIn("pid", sub)

    def test_restart_recover_and_resume(self):
        store = ceo.MissionStore(self.path)
        o = ceo.Orchestrator(store, FakeBrain(PLAN, []), FakeControl(), lambda c, t: None,
                             policy=lambda: None, projects=lambda: [("p", "/p")], spawn=lambda fn: None)
        _, mid = o.start("oc_ceo", "任务")
        store.missions[mid]["state"] = "running"
        store.save()
        store2 = ceo.MissionStore(self.path)
        control = FakeControl()
        o2 = self.orch(FakeBrain(PLAN, []), control, store=store2)
        self.assertEqual(o2.recover(), 1)
        self.assertEqual(store2.missions[mid]["state"], "interrupted")
        self.assertTrue(o2.is_ceo_chat("oc_ceo"))
        self.assertIn("接着做", o2.resume("oc_ceo"))
        self.assertEqual(store2.missions[mid]["state"], "done")

    def test_store_file_private(self):
        store = ceo.MissionStore(self.path)
        store.chats["oc"] = {}
        store.save()
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_parse_command(self):
        self.assertEqual(ceo.parse_command("/ceo"), ("help", ""))
        self.assertEqual(ceo.parse_command("/ceo 群"), ("mark", ""))
        self.assertEqual(ceo.parse_command("/ceo 停"), ("stop", ""))
        self.assertEqual(ceo.parse_command("/ceo 状态"), ("status", ""))
        self.assertEqual(ceo.parse_command("/ceo 继续"), ("resume", ""))
        self.assertEqual(ceo.parse_command("/ceo 写一个网页"), ("start", "写一个网页"))
        self.assertEqual(ceo.parse_command("/sessions"), ("", ""))


class BrainTest(unittest.TestCase):
    def test_brain_argv_has_no_tools_and_parses(self):
        seen = {}

        def run(argv, **kw):
            seen["argv"] = argv
            seen["kw"] = kw
            return SimpleNamespace(returncode=0, stdout='{"is_error":false,"structured_output":{"summary":"s","subtasks":[]}}')

        brain = ceo.ClaudeBrain(run=run, settings=lambda: {"apiKeyHelper": "x", "permissions": {"allow": ["*"]}})
        out = brain.ask("hi", ceo.PLAN_SCHEMA)
        self.assertEqual(out["summary"], "s")
        argv = seen["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", argv)
        self.assertNotIn("allow", argv[argv.index("--settings") + 1])

    def test_brain_errors(self):
        bad = ceo.ClaudeBrain(run=lambda argv, **kw: SimpleNamespace(returncode=1, stdout="oops"))
        with self.assertRaises(ceo.BrainError):
            bad.ask("x", ceo.PLAN_SCHEMA)
        err = ceo.ClaudeBrain(run=lambda argv, **kw: SimpleNamespace(returncode=0, stdout='{"is_error":true,"result":"quota"}'))
        with self.assertRaises(ceo.BrainError):
            err.ask("x", ceo.PLAN_SCHEMA)


if __name__ == "__main__":
    unittest.main()
