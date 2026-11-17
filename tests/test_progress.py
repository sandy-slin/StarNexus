"""FC-14：等待卡原地刷新，并显示这一轮用了哪些工具、改了哪些文件、正在做什么。"""
import asyncio
import json
import os
import tempfile
import unittest

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.progress import mark, read_progress


def claude_use(name, inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": name, "input": inp}]}}


def codex_call(name, args):
    return {"type": "response_item", "payload": {"type": "function_call", "name": name,
                                                  "arguments": json.dumps(args)}}


class ReadProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "s.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, *records):
        with open(self.path, "a", encoding="utf-8") as handle:
            for rec in records:
                handle.write(json.dumps(rec) + "\n")

    def test_only_new_part_after_mark(self):
        self.write(claude_use("Bash", {"command": "old"}))
        offset = mark(self.path)
        self.write(claude_use("Bash", {"command": "pytest -q"}),
                   claude_use("Edit", {"file_path": "/w/src/a.py"}))
        lines = read_progress(self.path, offset).lines()
        self.assertEqual(lines[0], "🔧 用了：跑命令×1、改文件×1")
        self.assertIn("a.py", lines[1])
        self.assertIn("改文件", lines[2])

    def test_codex_exec_and_apply_patch(self):
        self.write(codex_call("exec_command", {"cmd": "ls"}),
                   {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "apply_patch",
                                                         "input": "*** Begin Patch\n*** Update File: b.py\n"}})
        got = read_progress(self.path, 0)
        self.assertEqual(got.tools, {"跑命令": 1, "改文件": 1})
        self.assertEqual(got.files, ["b.py"])

    def test_nothing_new_no_lines(self):
        self.write(claude_use("Bash", {"command": "x"}))
        self.assertEqual(read_progress(self.path, mark(self.path)).lines(), [])
        self.assertEqual(read_progress("", 0).lines(), [])


class FakeReplier:
    def __init__(self):
        self.sent, self.patched = [], []

    def send_status_card(self, chat_id, text, with_stop=True):
        self.sent.append(text)
        return "om_1"

    def patch_status_card(self, mid, text, with_stop=True):
        self.patched.append(text)
        return True


class WaitCardTest(unittest.TestCase):
    def test_one_card_patched_with_progress(self):
        replier = FakeReplier()
        steps = iter([["🔧 用了：跑命令×1"], ["🔧 用了：跑命令×2"], ["🔧 用了：跑命令×2"]])
        card = app_mod._FleetWaitCard(replier, "oc", "Codex", can_stop=True, first_delay=0,
                                      progress=lambda: next(steps, ["🔧 用了：跑命令×2"]))

        async def go():
            stop = asyncio.Event()
            original = app_mod._STATUS_REFRESH_SEC
            app_mod._STATUS_REFRESH_SEC = 0.01
            try:
                task = asyncio.create_task(card.run(stop))
                await asyncio.sleep(0.08)
                stop.set()
                await task
            finally:
                app_mod._STATUS_REFRESH_SEC = original

        asyncio.run(go())
        self.assertEqual(len(replier.sent), 1)
        self.assertIn("跑命令×1", replier.sent[0])
        self.assertTrue(any("跑命令×2" in t for t in replier.patched))

    def test_progress_failure_does_not_break_card(self):
        replier = FakeReplier()

        def boom():
            raise OSError("x")

        card = app_mod._FleetWaitCard(replier, "oc", "Codex", can_stop=False, first_delay=0, progress=boom)

        async def go():
            stop = asyncio.Event()
            task = asyncio.create_task(card.run(stop))
            await asyncio.sleep(0.02)
            stop.set()
            await task

        asyncio.run(go())
        self.assertEqual(len(replier.sent), 1)


class SendStageTest(unittest.TestCase):
    def _write(self, rows):
        import json, tempfile
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.close()
        return handle.name

    def test_codex_stages(self):
        from bridge.fleet.progress import read_progress
        path = self._write([])
        self.assertEqual(read_progress(path, 0).stage, "sent")
        path = self._write([{"type": "event_msg", "payload": {"type": "user_message"}}])
        self.assertEqual(read_progress(path, 0).stage, "delivered")
        path = self._write([{"type": "event_msg", "payload": {"type": "user_message"}},
                            {"type": "response_item", "payload": {"type": "reasoning"}}])
        self.assertEqual(read_progress(path, 0).stage, "replying")

    def test_claude_stages(self):
        from bridge.fleet.progress import read_progress
        path = self._write([{"type": "user", "message": {"content": "你好"}}])
        self.assertEqual(read_progress(path, 0).stage, "delivered")
        path = self._write([{"type": "user", "message": {"content": [{"type": "tool_result"}]}}])
        self.assertEqual(read_progress(path, 0).stage, "sent")
        path = self._write([{"type": "user", "message": {"content": "你好"}},
                            {"type": "assistant", "message": {"content": [{"type": "text", "text": "好"}]}}])
        self.assertEqual(read_progress(path, 0).stage, "replying")

    def test_card_opens_at_once_and_walks_three_steps_on_one_message(self):
        replier = FakeReplier()
        steps = iter([("delivered", []), ("replying", ["🔧 用了：跑命令×1"])])
        card = app_mod._FleetWaitCard(replier, "oc", "Codex", can_stop=True, first_delay=90,
                                      progress=lambda: next(steps, ("replying", ["🔧 用了：跑命令×1"])))

        async def go():
            self.assertTrue(await card.open())
            stop = asyncio.Event()
            original = app_mod._STAGE_POLL_SEC
            app_mod._STAGE_POLL_SEC = 0.01
            try:
                task = asyncio.create_task(card.run(stop))
                await asyncio.sleep(0.08)
                stop.set()
                await task
            finally:
                app_mod._STAGE_POLL_SEC = original

        asyncio.run(go())
        self.assertEqual(len(replier.sent), 1)
        self.assertIn("✅ 已发送 → ⏳ 已送达 → ⚪ 正在回复", replier.sent[0])
        self.assertTrue(any("✅ 已送达 → ⏳ 正在回复" in t for t in replier.patched))
        self.assertTrue(any("✍️ 正在回复" in t and "跑命令×1" in t for t in replier.patched))

    def test_stage_never_goes_back(self):
        card = app_mod._FleetWaitCard(FakeReplier(), "oc", "Codex", can_stop=False, first_delay=0)
        card._take(("replying", []))
        card._take(("delivered", []))
        self.assertEqual(card._stage, "replying")

    def test_open_failure_falls_back(self):
        class Broken(FakeReplier):
            def send_status_card(self, chat_id, text, with_stop=True):
                return None

        card = app_mod._FleetWaitCard(Broken(), "oc", "Codex", can_stop=False, first_delay=0)
        self.assertFalse(asyncio.run(card.open()))


class WaitCardFinishTest(unittest.TestCase):
    """FC-11：主人点「停下」⇒ 等待卡收尾写「已停下」，不按「没正常回完」的警告说。"""

    def finish_with(self, result):
        replier = FakeReplier()
        card = app_mod._FleetWaitCard(replier, "oc", "Claude", can_stop=True, first_delay=0)

        async def go():
            await card.open()
            await card.finish(result)

        asyncio.run(go())
        return replier.patched[-1]

    def test_owner_stop_says_stopped(self):
        from bridge.fleet.claude import STOPPED_BY_OWNER
        from bridge.fleet.model import ActionResult

        text = self.finish_with(ActionResult(False, STOPPED_BY_OWNER, "s1", delivered=True))
        self.assertIn("⏹ 已停下", text)
        self.assertNotIn("没等到", text)

    def test_waiting_approval_says_approval(self):
        from bridge.fleet.claude import WHY_SEND_DEFERRED
        from bridge.fleet.model import ActionResult

        text = self.finish_with(ActionResult(False, WHY_SEND_DEFERRED, "s1", delivered=True))
        self.assertIn("在等你批准", text)
        self.assertNotIn("没等到", text)

    def test_timeout_still_warns(self):
        from bridge.fleet.model import ActionResult

        text = self.finish_with(ActionResult(False, "太久没回完", "s1", delivered=True))
        self.assertIn("没等到它正常回复", text)


if __name__ == "__main__":
    unittest.main()


class WaitCardStopTest(unittest.TestCase):
    def test_stop_button_appears_once_bridge_owns_the_turn(self):
        stops = []

        class Rec(FakeReplier):
            def send_status_card(self, chat_id, text, with_stop=True):
                stops.append(with_stop)
                return super().send_status_card(chat_id, text, with_stop)

            def patch_status_card(self, mid, text, with_stop=True):
                stops.append(with_stop)
                return super().patch_status_card(mid, text, with_stop)

        owned = {"yes": False}
        card = app_mod._FleetWaitCard(Rec(), "oc", "Codex", can_stop=lambda: owned["yes"], first_delay=90)

        async def go():
            original = app_mod._STAGE_POLL_SEC
            app_mod._STAGE_POLL_SEC = 0.01
            try:
                await card.open()
                owned["yes"] = True
                stop = asyncio.Event()
                task = asyncio.create_task(card.run(stop))
                await asyncio.sleep(0.05)
                stop.set()
                await task
            finally:
                app_mod._STAGE_POLL_SEC = original

        asyncio.run(go())
        self.assertFalse(stops[0])
        self.assertTrue(stops[-1])

    def test_control_stoppable_follows_bridge_jobs(self):
        from bridge.fleet.control import FleetControl
        from bridge.fleet.model import FleetJobs

        jobs = FleetJobs()

        class Adapter:
            pass

        adapter = Adapter()
        adapter.jobs = jobs

        class Fleet:
            def adapter(self, tool):
                return adapter

        control = FleetControl.__new__(FleetControl)
        control.fleet = Fleet()
        sid = "01a1df76-0000-7000-8000-000000000000"
        self.assertFalse(control.stoppable("codex", sid))
        import os
        jobs.register_spawned(os.getpid(), "codex", sid)
        self.assertTrue(control.stoppable("codex", sid))
