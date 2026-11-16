import unittest

from tests.helpers import Clock, FakeBinds, FakeBound, View, sess
from bridge.fleet.grouppush import GroupPusher, Push, build_push_text
from bridge.fleet.model import AskQuestion, PendingAsk


def make(clock, **kw):
    return GroupPusher(now=clock, **kw)


class GroupPushTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.binds = FakeBinds([FakeBound("oc_g1", "claude", "s1")])

    def test_waiting_pushed_after_min_wait_once(self):
        gp = make(self.clock)
        v = View([sess(status="waiting")])
        self.assertEqual(gp.due(v, self.binds), [])
        self.clock.tick(25)
        out = gp.due(v, self.binds)
        self.assertEqual([p.kind for p in out], ["waiting"])
        self.assertEqual(out[0].chat_id, "oc_g1")
        self.clock.tick(60)
        self.assertEqual(gp.due(v, self.binds), [])

    def test_short_waiting_not_pushed(self):
        gp = make(self.clock)
        gp.due(View([sess(status="waiting")]), self.binds)
        self.clock.tick(10)
        gp.due(View([sess(status="running")]), self.binds)
        self.clock.tick(30)
        self.assertEqual(gp.due(View([sess(status="running")]), self.binds), [])

    def test_new_question_pushes_again(self):
        gp = make(self.clock)
        q1 = PendingAsk(tool="AskUserQuestion", questions=(AskQuestion("选 A 还是 B?", options=(("A", ""), ("B", ""))),))
        q2 = PendingAsk(tool="Bash", summary="rm build")
        gp.due(View([sess(status="waiting", pending=q1)]), self.binds)
        self.clock.tick(25)
        self.assertEqual(len(gp.due(View([sess(status="waiting", pending=q1)]), self.binds)), 1)
        self.clock.tick(5)
        gp.due(View([sess(status="waiting", pending=q2)]), self.binds)
        self.clock.tick(25)
        self.assertEqual(len(gp.due(View([sess(status="waiting", pending=q2)]), self.binds)), 1)

    def test_waiting_ends_then_again_is_new(self):
        gp = make(self.clock)
        gp.due(View([sess(status="waiting")]), self.binds)
        self.clock.tick(25)
        self.assertEqual(len(gp.due(View([sess(status="waiting")]), self.binds)), 1)
        gp.due(View([sess(status="running")]), self.binds)
        gp.due(View([sess(status="waiting")]), self.binds)
        self.clock.tick(25)
        self.assertEqual(len(gp.due(View([sess(status="waiting")]), self.binds)), 1)

    def test_unbound_or_off_not_pushed(self):
        gp = make(self.clock)
        binds = FakeBinds([FakeBound("oc_g1", "claude", "s1", on=False)])
        gp.due(View([sess(status="waiting"), sess(sid="s2", status="waiting")]), binds)
        self.clock.tick(30)
        self.assertEqual(gp.due(View([sess(status="waiting"), sess(sid="s2", status="waiting")]), binds), [])

    def test_history_not_replayed_on_first_sight(self):
        gp = make(self.clock)
        old = sess(last_turn="done", last_turn_at=self.clock() - 3600)
        self.assertEqual(gp.due(View([old]), self.binds), [])
        self.clock.tick(100)
        self.assertEqual(gp.due(View([old]), self.binds), [])

    def test_done_pushed_after_settle(self):
        gp = make(self.clock)
        gp.due(View([sess(status="running")]), self.binds)
        done_at = self.clock() + 5
        self.clock.tick(10)
        v = View([sess(last_turn="done", last_turn_at=done_at)])
        self.assertEqual(gp.due(v, self.binds), [])
        self.clock.tick(50)
        out = gp.due(v, self.binds)
        self.assertEqual([p.kind for p in out], ["done"])
        self.clock.tick(100)
        self.assertEqual(gp.due(v, self.binds), [])

    def test_failed_pushed_immediately(self):
        gp = make(self.clock)
        gp.due(View([sess(status="running")]), self.binds)
        self.clock.tick(2)
        out = gp.due(View([sess(last_turn="failed", last_turn_at=self.clock() - 1)]), self.binds)
        self.assertEqual([p.kind for p in out], ["failed"])

    def test_group_relay_turn_not_duplicated(self):
        gp = make(self.clock)
        gp.due(View([sess(status="running")]), self.binds)
        start = self.clock()
        self.clock.tick(40)
        gp.note_relay("claude", "s1", start)
        v = View([sess(last_turn="done", last_turn_at=self.clock() - 2)])
        self.clock.tick(100)
        self.assertEqual(gp.due(v, self.binds), [])

    def test_done_rate_limited_per_session(self):
        gp = make(self.clock)
        gp.due(View([sess(status="running")]), self.binds)
        t1 = self.clock()
        self.clock.tick(60)
        self.assertEqual(len(gp.due(View([sess(last_turn="done", last_turn_at=t1)]), self.binds)), 1)
        t2 = self.clock()
        self.clock.tick(60)
        self.assertEqual(gp.due(View([sess(last_turn="done", last_turn_at=t2)]), self.binds), [])
        self.clock.tick(600)
        self.assertEqual(len(gp.due(View([sess(last_turn="done", last_turn_at=t2)]), self.binds)), 1)

    def test_burst_limit_defers_not_drops(self):
        rows = [FakeBound(f"oc_{i}", "claude", f"s{i}") for i in range(4)]
        binds = FakeBinds(rows)
        gp = make(self.clock, burst=2)
        v = View([sess(sid=f"s{i}", status="waiting") for i in range(4)])
        gp.due(v, binds)
        self.clock.tick(25)
        self.assertEqual(len(gp.due(v, binds)), 2)
        self.clock.tick(61)
        self.assertEqual(len(gp.due(v, binds)), 2)

    def test_disabled(self):
        gp = make(self.clock, enabled=False)
        gp.due(View([sess(status="waiting")]), self.binds)
        self.clock.tick(30)
        self.assertEqual(gp.due(View([sess(status="waiting")]), self.binds), [])

    def test_text_needs_human_and_redacts(self):
        q = PendingAsk(tool="AskUserQuestion", questions=(AskQuestion("用哪个 token sk-SECRET?", options=(("是", ""),)),))
        p = Push("waiting", "oc", sess(status="waiting", pending=q))
        text = build_push_text(p, "Claude「任务」", "proj", redact=lambda s: s.replace("sk-SECRET", "***"))
        self.assertIn("需要人介入", text)
        self.assertNotIn("sk-SECRET", text)
        done = build_push_text(Push("done", "oc", sess()), "Claude「任务」", "proj", "x" * 2000)
        self.assertLess(len(done), 1200)
        self.assertIn("做完", done)


if __name__ == "__main__":
    unittest.main()


class DecisionContextTest(unittest.TestCase):
    def test_waiting_push_has_status_ask_options_and_consequence(self):
        from types import SimpleNamespace as NS

        from bridge.fleet.grouppush import Push, build_push_text

        pending = NS(questions=(), tool="Bash", summary="rm -rf build/")
        sess = NS(tool="claude", id="s", pending=pending)
        text = build_push_text(Push("waiting", "oc_g", sess), "Claude「x」", "proj", "打包前要清掉旧产物")
        for word in ("需要人介入", "**现状**", "**卡在**", "**你可以**", "**不处理**"):
            self.assertIn(word, text)
