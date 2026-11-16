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


if __name__ == "__main__":
    unittest.main()
