"""第三个工具 opencode：适配层（§5.9）最小接入验收的自动化部分。"""
import json
import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge.fleet.model import CAPS, FleetJobs, can
from bridge.fleet.newsession import build_tool_card
from bridge.fleet.opencode import OpencodeAdapter, is_session_id, parse_run_output

SID = "ses_e1a194299ffeqZKoHAd6baW4t6"
RUN_OUT = (json.dumps({"type": "step_start", "sessionID": SID, "part": {"type": "step-start"}}) + "\n"
           + json.dumps({"type": "text", "sessionID": SID, "part": {"type": "text", "text": "OC-OK"}}) + "\n")


def make_db(path, directory):
    conn = sqlite3.connect(path)
    conn.execute("create table session_v2 (id text, parent_id text, title text, directory text, time_updated integer)")
    conn.execute("create table session_message (id text, session_id text, type text, seq integer, "
                 "time_created integer, data text)")
    conn.execute("insert into session_v2 values (?, null, '只回复 OC', ?, 1794858575017)", (SID, directory))
    rows = [("user", {"text": "只回复：OC-OK"}), ("assistant", {"content": [{"type": "text", "text": "OC-OK"}]}),
            ("idle", {"outcome": "succeeded"})]
    for i, (kind, data) in enumerate(rows):
        conn.execute("insert into session_message values (?, ?, ?, ?, ?, ?)",
                     (f"m{i}", SID, kind, i, 1794858466924 + i, json.dumps(data)))
    conn.commit()
    conn.close()


class FakeRunner:
    def __init__(self, stdout=RUN_OUT, rc=0):
        self.argv, self.cwd = None, None
        self.stdout, self.rc = stdout, rc

    def run(self, argv, **kw):
        self.argv, self.cwd = argv, kw.get("cwd")
        return SimpleNamespace(stdout=self.stdout, stderr="", returncode=self.rc, error="", timed_out=False)


class OpencodeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, "opencode.db")
        make_db(self.db, self.dir)

    def adapter(self, runner=None):
        return OpencodeAdapter(jobs=FleetJobs(lstart_of=lambda pid: "x"), runner=runner or FakeRunner(),
                               db_path=self.db, binary="/bin/opencode")

    def test_parse(self):
        self.assertEqual(parse_run_output(RUN_OUT), (SID, "OC-OK", ""))
        self.assertTrue(is_session_id(SID))
        self.assertFalse(is_session_id("../x"))

    def test_list_read_find(self):
        ad = self.adapter()
        sessions = ad.list()
        self.assertEqual([s.id for s in sessions], [SID])
        self.assertEqual(sessions[0].cwd, self.dir)
        self.assertTrue(sessions[0].can_send)
        self.assertEqual([(m.role, m.text) for m in ad.read(SID)], [("user", "只回复：OC-OK"), ("assistant", "OC-OK")])
        self.assertIsNotNone(ad.find(SID))

    def test_start_trusted_auto_and_model(self):
        runner = FakeRunner()
        res = self.adapter(runner).start(self.dir, "hi", mode="trusted", model="cpa-gui/chatgpt-gpt-5.5")
        self.assertTrue(res.ok)
        self.assertEqual(res.session_id, SID)
        self.assertEqual(runner.argv[:5], ["/bin/opencode", "run", "--standalone", "--format", "json"])
        self.assertIn("--auto", runner.argv)
        self.assertEqual(runner.argv[-2:], ["--", "hi"])
        self.assertEqual(runner.cwd, self.dir)

    def test_default_model_when_none_given(self):
        from bridge.fleet import opencode as oc

        runner = FakeRunner()
        self.adapter(runner).start(self.dir, "hi")
        self.assertEqual(runner.argv[runner.argv.index("-m") + 1], oc.DEFAULT_MODEL)

    def test_readonly_no_auto_and_send_resumes(self):
        runner = FakeRunner()
        res = self.adapter(runner).send(SID, "again", mode="readonly")
        self.assertTrue(res.ok)
        self.assertNotIn("--auto", runner.argv)
        self.assertEqual(runner.argv[runner.argv.index("-s") + 1], SID)

    def test_fork_needs_new_id(self):
        runner = FakeRunner()
        res = self.adapter(runner).start(self.dir, "hi", fork_from=SID)
        self.assertFalse(res.ok)
        self.assertIn("--fork", runner.argv)

    def test_bad_model_rejected(self):
        self.assertFalse(self.adapter().start(self.dir, "hi", model="-x; rm").ok)

    def test_failure_reported(self):
        out = json.dumps({"type": "error", "sessionID": SID, "error": {"name": "X", "data": {"message": "quota"}}})
        res = self.adapter(FakeRunner(stdout=out, rc=1)).send(SID, "hi")
        self.assertFalse(res.ok)
        self.assertIn("quota", res.text)

    def test_caps_and_tool_card(self):
        self.assertTrue(can("opencode", "start"))
        self.assertFalse(can("opencode", "approval"))
        self.assertTrue(all(can(t, "start") for t in ("claude", "codex", "opencode")))
        self.assertIn("cursor", CAPS)
        card = build_tool_card({"tag": "button"}, tools=("claude", "codex"))
        self.assertNotIn("opencode", card)
        self.assertIn("opencode", build_tool_card({"tag": "button"}, tools=("claude", "codex", "opencode")))


if __name__ == "__main__":
    unittest.main()


class PwdEnvTest(unittest.TestCase):
    def test_pwd_follows_cwd(self):
        import tempfile

        from bridge.fleet.model import FleetJobs

        seen = {}

        class R:
            def run(self, argv, **kw):
                seen.update(kw)
                return SimpleNamespace(stdout=RUN_OUT, stderr="", returncode=0, error="", timed_out=False)

        d = tempfile.mkdtemp()
        OpencodeAdapter(jobs=FleetJobs(), runner=R(), db_path="/nonexistent", binary="/bin/opencode").start(d, "hi")
        self.assertEqual(seen["env"]["PWD"], d)
        self.assertEqual(seen["cwd"], d)
