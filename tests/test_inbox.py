"""FC-09：群里发图片 / 文件 ⇒ 存进会话项目的 .starnexus/inbox/，路径附在消息里发给 Agent。"""
import asyncio
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet import inbox


class ParseTest(unittest.TestCase):
    def test_image_file_post(self):
        self.assertEqual(inbox.attachments_of("image", json.dumps({"image_key": "img_1"})),
                         [("image", "img_1", "")])
        self.assertEqual(inbox.attachments_of("file", json.dumps({"file_key": "f_1", "file_name": "a.log"})),
                         [("file", "f_1", "a.log")])
        post = {"zh_cn": {"title": "", "content": [[{"tag": "text", "text": "看图"},
                                                     {"tag": "img", "image_key": "img_2"}],
                                                    [{"tag": "img", "image_key": "img_2"}]]}}
        self.assertEqual(inbox.attachments_of("post", json.dumps(post)), [("image", "img_2", "")])

    def test_nothing_to_download(self):
        self.assertEqual(inbox.attachments_of("text", json.dumps({"text": "hi"})), [])
        self.assertEqual(inbox.attachments_of("audio", json.dumps({"file_key": "x"})), [])
        self.assertEqual(inbox.attachments_of("image", "not json"), [])


class SaveTest(unittest.TestCase):
    def test_name_has_no_path_parts(self):
        name = inbox.safe_name("../../etc/passwd", "k", "file")
        self.assertNotIn("/", name)
        self.assertTrue(name.endswith("passwd"))
        self.assertTrue(inbox.safe_name("", "img_v3_abc", "image", "image/jpeg").endswith(".jpg"))

    def test_inbox_ignored_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as cwd:
            directory = inbox.inbox_dir(cwd)
            self.assertEqual(directory, os.path.join(cwd, ".starnexus", "inbox"))
            with open(os.path.join(cwd, ".starnexus", ".gitignore"), encoding="utf-8") as handle:
                self.assertEqual(handle.read().strip(), "*")
            first = inbox.save(directory, "a.png", b"1")
            second = inbox.save(directory, "a.png", b"2")
            self.assertNotEqual(first, second)
            with open(first, "rb") as handle:
                self.assertEqual(handle.read(), b"1")

    def test_too_big_rejected(self):
        with tempfile.TemporaryDirectory() as cwd, mock.patch.object(inbox, "MAX_BYTES", 3):
            with self.assertRaises(ValueError):
                inbox.save(cwd, "a.bin", b"1234")

    def test_compose(self):
        self.assertIn("/p/a.png", inbox.compose("这是什么", ["/p/a.png"]))
        self.assertTrue(inbox.compose("", ["/p/a.png"]).startswith("请先查看附件"))


class WiringTest(unittest.TestCase):
    def make(self, cwd):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.settings = SimpleNamespace(owner_open_ids=frozenset({"ou_me"}),
                                          policy=SimpleNamespace(default_workspace=cwd))
        bridge.client = object()
        bridge.said = []
        bridge._say = lambda chat, text: bridge.said.append(text) or True
        bound = SimpleNamespace(tool="codex", id="s1")
        bridge.chat_binds = SimpleNamespace(active_of_scope=lambda scope: bound)
        bridge.fleet = SimpleNamespace(_cwd_of=lambda tool, sid: cwd if (tool, sid) == ("codex", "s1") else "",
                                       peek_pending_fresh=lambda scope: (None, False))
        bridge.handled = []

        async def fake_handle(scope, chat_id, text, message_id, late_note=""):
            bridge.handled.append((scope, chat_id, text))

        bridge._handle_then_unack = fake_handle
        return bridge

    def test_image_saved_into_bound_session_project_then_sent(self):
        with tempfile.TemporaryDirectory() as cwd:
            bridge = self.make(cwd)
            with mock.patch.object(inbox, "download", return_value=(b"PNG", "", "image/png")):
                asyncio.run(bridge._attachments_then_handle("group:oc_g", "oc_g", "描述这张图", "om_1",
                                                            [("image", "img_1", "")]))
            self.assertEqual(len(bridge.handled), 1)
            text = bridge.handled[0][2]
            self.assertTrue(text.startswith("描述这张图"))
            path = text.strip().splitlines()[-1][2:]
            self.assertTrue(path.startswith(os.path.join(cwd, ".starnexus", "inbox")))
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), b"PNG")

    def test_download_failure_says_so_and_does_not_send(self):
        with tempfile.TemporaryDirectory() as cwd:
            bridge = self.make(cwd)
            with mock.patch.object(inbox, "download", side_effect=RuntimeError("code=234003")):
                asyncio.run(bridge._attachments_then_handle("group:oc_g", "oc_g", "", "om_1",
                                                            [("image", "img_1", "")]))
            self.assertEqual(bridge.handled, [])
            self.assertIn("下载失败", bridge.said[0])

    def test_on_message_routes_image_instead_of_rejecting(self):
        with tempfile.TemporaryDirectory() as cwd:
            bridge = self.make(cwd)
            bridge._seen_messages = {}
            bridge._loop = object()
            bridge.touch = lambda: None
            bridge._say_later = lambda chat, text: bridge.said.append(text)
            calls = []
            bridge._attachments_then_handle = lambda *a: ("attach", a)
            message = SimpleNamespace(message_id="om_9", message_type="image", chat_id="oc_g", chat_type="group",
                                      content=json.dumps({"image_key": "img_9"}), mentions=None, create_time=None)
            data = SimpleNamespace(event=SimpleNamespace(
                sender=SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_me")),
                message=message))
            with mock.patch.object(app_mod.asyncio, "run_coroutine_threadsafe",
                                   lambda coro, loop: calls.append(coro)):
                bridge.on_message(data)
            self.assertEqual(bridge.said, [])
            self.assertEqual(calls[0][0], "attach")
            self.assertEqual(calls[0][1][4], [("image", "img_9", "")])


if __name__ == "__main__":
    unittest.main()
