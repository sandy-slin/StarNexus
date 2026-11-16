"""会话管理（§4）：复制（fork）、归档、重命名。"""
import asyncio
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.codex import CodexAdapter
from bridge.fleet.model import ActionResult, FleetJobs
from bridge.fleet.newsession import NewSessionState
from bridge.fleet.statuscard import CardInfo, build_card

TID = "01a1e584-52a1-7dd0-9bae-cd807d0f8845"


class FakeRunner:
    def __init__(self, stdout="", rc=0):
        self.argv = None
        self.stdout, self.rc = stdout, rc

    def run(self, argv, **kw):
        self.argv = argv
        return SimpleNamespace(stdout=self.stdout, stderr="", returncode=self.rc, error="", timed_out=False)


class CodexForkArchiveTest(unittest.TestCase):
    def adapter(self, runner):
        ad = CodexAdapter(jobs=FleetJobs(lstart_of=lambda pid: "x"), runner=runner)
        ad._codex = lambda: "/bin/codex"
        return ad

    def test_fork_argv(self):
        out = '{"type":"thread.started","thread_id":"01a1e5ac-c2b0-73d1-a2a8-19f6b86ff354"}\n' \
              '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}\n'
        runner = FakeRunner(stdout=out)
        res = self.adapter(runner).start("/tmp", "hi", mode="write", fork_from=TID)
        self.assertEqual(runner.argv[:3], ["/bin/codex", "exec", "fork"])
        self.assertEqual(runner.argv[-3:], ["--", TID, "hi"])
        self.assertEqual(res.session_id, "01a1e5ac-c2b0-73d1-a2a8-19f6b86ff354")

    def test_fork_bad_id(self):
        self.assertFalse(self.adapter(FakeRunner()).start("/tmp", "hi", fork_from="../x").ok)

    def test_archive(self):
        runner = FakeRunner()
        self.assertTrue(self.adapter(runner).archive(TID).ok)
        self.assertEqual(runner.argv, ["/bin/codex", "archive", TID])
        self.assertFalse(self.adapter(FakeRunner(rc=1)).archive(TID).ok)


class BridgeTest(unittest.TestCase):
    def make(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bound = SimpleNamespace(tool="codex", id=TID, name="SN·Codex·x·修登录", chat_id="oc_g")
        bridge.chat_binds = SimpleNamespace(of_chat=lambda c: bound if c == "oc_g" else None,
                                            set_name=lambda c, n: bridge.names.append(n))
        bridge.names, bridge.said = [], []
        bridge._say = lambda chat, text: bridge.said.append((chat, text)) or True
        bridge.fleet = SimpleNamespace(cached=lambda t, s: SimpleNamespace(cwd="/u/work/x"),
                                       archive=lambda t, s: ActionResult(True, "已归档。", s))
        bridge.settings = SimpleNamespace(owner_open_ids=frozenset({"ou_me"}),
                                          policy=SimpleNamespace(resolve_workspace=lambda c: c, mode="trusted"))
        bridge._effective_model = lambda tool, sid: SimpleNamespace(model="gpt-6-luna", effort="high")
        bridge._new_state = NewSessionState()
        bridge._newsession_state = lambda: bridge._new_state
        bridge._chat_ids = {"dm:ou_me": "oc_dm"}
        bridge.done = []
        bridge._mark_session_done = lambda chat, on: bridge.done.append((chat, on))
        bridge._fleet_settled_store = lambda: SimpleNamespace(settle=lambda t, s: bridge.done.append(("settle", s)))
        bridge.group_client = SimpleNamespace(rename=lambda c, n: (True, 0, ""))
        bridge._refresh_status_card = lambda c, force=False: ""
        return bridge

    def test_fork_arms_dm_with_source(self):
        bridge = self.make()
        asyncio.run(bridge._session_manage("group:oc_g", "oc_g", "/fork", "/fork"))
        armed, _ = bridge._new_state.peek("dm:ou_me")
        self.assertEqual((armed["tool"], armed["cwd"], armed["fork_from"], armed["model"]),
                         ("codex", "/u/work/x", TID, "gpt-6-luna"))
        self.assertTrue(any(c == "oc_dm" for c, _ in bridge.said))

    def test_archive_marks_done(self):
        bridge = self.make()
        asyncio.run(bridge._session_manage("group:oc_g", "oc_g", "/archive", "/archive"))
        self.assertIn(("oc_g", True), bridge.done)
        self.assertIn(("settle", TID), bridge.done)

    def test_rename_keeps_prefix(self):
        bridge = self.make()
        asyncio.run(bridge._session_manage("group:oc_g", "oc_g", "/rename", "/rename 修支付"))
        self.assertEqual(bridge.names, ["SN·Codex·修支付"])

    def test_outside_group_explains(self):
        bridge = self.make()
        asyncio.run(bridge._session_manage("dm:ou_me", "oc_dm", "/fork", "/fork"))
        self.assertIn("会话群", bridge.said[0][1])

    def test_status_card_has_buttons(self):
        card = build_card(CardInfo(tool="codex", sid=TID))
        self.assertIn('"op": "fork"', card)
        self.assertIn('"op": "archive"', card)


if __name__ == "__main__":
    unittest.main()


class DmScopeTest(unittest.TestCase):
    def test_dm_scope_without_seen_chat(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.settings = SimpleNamespace(owner_open_ids=frozenset({"ou_me"}))
        bridge._chat_ids = {}
        self.assertEqual(bridge._owner_dm_scope(), "dm:ou_me")


class CodexPickTest(unittest.TestCase):
    def test_picks_newest_version(self):
        import os
        import tempfile
        from unittest import mock

        from bridge.fleet import codex as codex_mod

        d = tempfile.mkdtemp()
        old, new = os.path.join(d, "old"), os.path.join(d, "new")
        for path, ver in ((old, "0.142.3"), (new, "0.167.0")):
            with open(path, "w") as fh:
                fh.write(f"#!/bin/sh\necho codex-cli {ver}\n")
            os.chmod(path, 0o755)
        ad = CodexAdapter(jobs=FleetJobs())
        with mock.patch.object(codex_mod.shutil, "which", return_value=old), \
                mock.patch.object(codex_mod.os.path, "expanduser", side_effect=lambda p: p.replace("~/.carher-npm-global/bin/codex", new)):
            self.assertEqual(ad._codex(), new)
