"""1.9.6：合并转发展开成一段记录交给会话。样例是 2026-11-20 从真实合并转发里取的形状。"""
import asyncio
import json
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet import mergefwd

ME = "ou_me"
BOT = "cli_bot"


def item(mtype, content, sender=ME, kind="user", name="林森", ts="1795132600000", mid="om_c"):
    return {"message_id": mid, "msg_type": mtype, "create_time": ts,
            "sender": {"id": sender, "sender_type": kind, "name": name},
            "body": {"content": json.dumps(content, ensure_ascii=False)}}


CLOCK = lambda ts: "11-20 07:56"  # noqa: E731


class ItemTextTest(unittest.TestCase):
    def test_text(self):
        self.assertEqual(mergefwd.item_text("text", '{"text":" 用/h 代替/m "}'), "用/h 代替/m")

    def test_post_with_at_and_image(self):
        body = {"title": "", "content": [[{"tag": "text", "text": "看这个 "}, {"tag": "at", "user_name": "柏君"}],
                                         [{"tag": "img", "image_key": "k"}]]}
        self.assertEqual(mergefwd.item_text("post", json.dumps(body)), "看这个 @柏君\n[图片]")

    def test_unknown_types_get_placeholder(self):
        self.assertEqual(mergefwd.item_text("interactive", "{}"), "[卡片]")
        self.assertEqual(mergefwd.item_text("image", '{"image_key":"k"}'), "[图片]")
        self.assertEqual(mergefwd.item_text("file", '{"file_name":"a.pdf"}'), "[文件：a.pdf]")
        self.assertEqual(mergefwd.item_text("weird", "{}"), "[weird]")


class RenderTest(unittest.TestCase):
    def test_two_texts(self):
        out = mergefwd.render([item("text", {"text": "用/h 代替/m"}), item("text", {"text": "/m 暂时空下来"})],
                              me=ME, clock=CLOCK)
        self.assertEqual(out, "11-20 07:56 林森：用/h 代替/m\n11-20 07:56 林森：/m 暂时空下来")

    def test_mixed_with_bot_and_saved_image(self):
        img = item("image", {"image_key": "k"})
        img["saved"] = ["/p/.starnexus/inbox/a.png"]
        bot = item("interactive", {"elements": []}, sender=BOT, kind="app", name="")
        out = mergefwd.render([img, bot, item("text", {"text": "会报上面截图的问题"})], me=ME, clock=CLOCK)
        self.assertIn("林森：[图片]（已保存到本机：/p/.starnexus/inbox/a.png）", out)
        self.assertIn("机器人：[卡片]", out)
        self.assertIn("林森：会报上面截图的问题", out)

    def test_too_many_items_noted(self):
        many = [item("text", {"text": "x"}) for _ in range(60)]
        self.assertIn("还有 10 条没列出", mergefwd.render(many, me=ME, clock=CLOCK))

    def test_too_long_cut_with_note(self):
        many = [item("text", {"text": "x" * 400}) for _ in range(40)]
        out = mergefwd.render(many, me=ME, clock=CLOCK)
        self.assertIn("个字没列出", out)
        self.assertLessEqual(len(out), mergefwd.MAX_CHARS + 60)

    def test_compose_mentions_count(self):
        self.assertIn("合并转发了 2 条消息", mergefwd.compose("a\nb", 2))


class FetchTest(unittest.TestCase):
    def test_skips_parent_and_reads_sender_name(self):
        def m(mid, upper, mtype, content, name):
            return SimpleNamespace(message_id=mid, upper_message_id=upper, msg_type=mtype, create_time="1",
                                   sender=SimpleNamespace(id=ME, sender_type="user", sender_name=name,
                                                          sender_i18n_names=SimpleNamespace(zh_cn="林森", en_us="")),
                                   body=SimpleNamespace(content=content))
        resp = SimpleNamespace(code=0, data=SimpleNamespace(items=[
            m("om_p", "", "merge_forward", "Merged and Forwarded Message", ""),
            m("om_1", "om_p", "text", '{"text":"a"}', ""),
        ]))
        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(get=lambda req: resp))))
        got = mergefwd.fetch(client, "om_p")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["sender"]["name"], "林森")
        self.assertEqual(got[0]["message_id"], "om_1")

    def test_error_raises(self):
        resp = SimpleNamespace(code=230002, msg="no permission", data=None)
        client = SimpleNamespace(im=SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(get=lambda req: resp))))
        with self.assertRaises(RuntimeError):
            mergefwd.fetch(client, "om_p")


class BridgeWiringTest(unittest.TestCase):
    def test_merge_forward_is_expanded_and_handled(self):
        br = app_mod.Bridge.__new__(app_mod.Bridge)
        br.client = object()
        handled, said = [], []
        br._say = lambda chat, text: said.append(text) or True
        br._inbox_cwd = lambda scope: "/tmp"

        async def handle(scope, chat, text, mid, late_note=""):
            handled.append(text)
        br._handle_then_unack = handle
        orig = mergefwd.fetch
        mergefwd.fetch = lambda client, mid: [item("text", {"text": "用/h 代替/m"}), item("text", {"text": "/m 空下来"})]
        try:
            asyncio.run(br._merge_forward_then_handle("dm:ou_me", "oc_dm", "om_p", ME))
        finally:
            mergefwd.fetch = orig
        self.assertEqual(said, [])
        self.assertEqual(len(handled), 1)
        self.assertIn("合并转发了 2 条消息", handled[0])
        self.assertIn("用/h 代替/m", handled[0])
        self.assertNotIn("只看得懂文字", handled[0])

    def test_fetch_failure_tells_user(self):
        br = app_mod.Bridge.__new__(app_mod.Bridge)
        br.client = object()
        said = []
        br._say = lambda chat, text: said.append(text) or True

        def boom(client, mid):
            raise RuntimeError("code=1")
        orig = mergefwd.fetch
        mergefwd.fetch = boom
        try:
            asyncio.run(br._merge_forward_then_handle("dm:ou_me", "oc_dm", "om_p", ME))
        finally:
            mergefwd.fetch = orig
        self.assertTrue(any("没读出来" in t for t in said))


if __name__ == "__main__":
    unittest.main()
