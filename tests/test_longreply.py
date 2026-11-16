"""FC-10：回复超过 1500 字 ⇒ 生成飞书文档；群里只有摘要和链接。"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet import longreply

LONG = "# 结论\n\n" + "这一段是正文。" * 300 + "\n\n```\nprint(1)\n```\n"


class FakeFeishu:
    def __init__(self, fail_at=""):
        self.fail_at = fail_at
        self.calls = []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        if self.fail_at and self.fail_at in path:
            return {"code": 99991672, "msg": "denied"}
        if path == "/docx/v1/documents":
            return {"code": 0, "data": {"document": {"document_id": "doxcnABC"}}}
        if path.endswith("/blocks/convert"):
            return {"code": 0, "data": {"first_level_block_ids": ["b1", "b2"], "blocks": [
                {"block_id": "b1", "block_type": 3, "children": []},
                {"block_id": "b2", "block_type": 31, "children": ["c1"],
                 "table": {"property": {"row_size": 1, "merge_info": [{}]}}},
                {"block_id": "c1", "block_type": 32, "children": []}]}}
        return {"code": 0, "data": {}}


class PublishTest(unittest.TestCase):
    def test_threshold(self):
        self.assertFalse(longreply.is_long("字" * 1500))
        self.assertTrue(longreply.is_long("字" * 1501))

    def test_publish_order_and_owner(self):
        fake = FakeFeishu()
        url = longreply.publish(fake, "t", "标题", LONG, "ou_me")
        self.assertEqual(url, "https://feishu.cn/docx/doxcnABC")
        paths = [p for _m, p, _b in fake.calls]
        self.assertEqual(paths[0], "/docx/v1/documents")
        self.assertTrue(paths[2].endswith("/descendant"))
        write = fake.calls[2][2]
        self.assertEqual(write["children_id"], ["b1", "b2"])
        self.assertEqual({b["block_id"] for b in write["descendants"]}, {"b1", "b2", "c1"})
        table = next(b for b in write["descendants"] if b["block_id"] == "b2")
        self.assertNotIn("merge_info", table["table"]["property"])
        self.assertIn("ou_me", str(fake.calls[3][2]))
        self.assertIn("full_access", str(fake.calls[3][2]))

    def test_failure_raises(self):
        with self.assertRaises(RuntimeError):
            longreply.publish(FakeFeishu(fail_at="/docx/v1/documents"), "t", "x", LONG)

    def test_summary_short_and_no_code(self):
        text = longreply.summary(LONG)
        self.assertLessEqual(len(text), longreply.SUMMARY_CHARS + 20)
        self.assertNotIn("print(1)", text)
        self.assertTrue(text.startswith("结论"))

    def test_group_text_has_link_not_full_body(self):
        out = longreply.group_text("🖥 Codex · x", LONG, "https://feishu.cn/docx/d")
        self.assertIn("https://feishu.cn/docx/d", out)
        self.assertLess(len(out), 600)


class WiringTest(unittest.TestCase):
    def make(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.sent = []
        bridge.replier = SimpleNamespace(send=lambda chat, text: bridge.sent.append(text))
        return bridge

    def test_long_reply_goes_to_doc(self):
        bridge = self.make()
        bridge._publish_long_reply = lambda label, body: "https://feishu.cn/docx/d1"
        asyncio.run(bridge._send_reply_maybe_doc("oc_g", "🖥 Codex · x", LONG, "\n\n尾巴"))
        self.assertEqual(len(bridge.sent), 1)
        self.assertIn("https://feishu.cn/docx/d1", bridge.sent[0])
        self.assertTrue(bridge.sent[0].endswith("尾巴"))
        self.assertLess(len(bridge.sent[0]), 700)

    def test_doc_failure_falls_back_to_full_text(self):
        bridge = self.make()

        def boom(label, body):
            raise RuntimeError("建文档失败 code=99991672")

        bridge._publish_long_reply = boom
        asyncio.run(bridge._send_reply_maybe_doc("oc_g", "🖥 Codex · x", LONG, ""))
        self.assertIn("这一段是正文。" * 300, bridge.sent[0])

    def test_short_reply_unchanged(self):
        bridge = self.make()
        bridge._publish_long_reply = mock.Mock()
        asyncio.run(bridge._send_reply_maybe_doc("oc_g", "🖥 Codex · x", "好了", ""))
        self.assertEqual(bridge.sent, ["🖥 Codex · x 回复：\n\n好了"])
        bridge._publish_long_reply.assert_not_called()


if __name__ == "__main__":
    unittest.main()
