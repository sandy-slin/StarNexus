"""FC-07：回复结尾的编号选项 ⇒ 按钮；点一下把那一项发回同一个会话。"""
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.choices import (choice_value, extract_choices, multi_value, resolve_choice_click,
                                  resolve_multi_click, submit_value, wants_multi)

MEASURED = ("I don't have an AskUserQuestion tool in this session, so I can't show you a selection prompt. "
            "I'll ask here instead:\n\n**Which color do you prefer?**\n1. Red\n2. Blue\n\nReply with your choice.")


class ExtractTest(unittest.TestCase):
    def test_measured_claude_p_reply(self):
        self.assertEqual(extract_choices(MEASURED), ["Red", "Blue"])

    def test_letters_and_descriptions(self):
        self.assertEqual(extract_choices("要用哪个方案？\nA. **方案一** — 快\nB. 方案二：稳"), ["方案一", "方案二"])

    def test_plain_steps_are_not_choices(self):
        self.assertEqual(extract_choices("步骤：\n1. 装依赖\n2. 跑测试\n完成了。"), [])

    def test_single_or_too_many_options_ignored(self):
        self.assertEqual(extract_choices("选哪个？\n1. 只有一个"), [])
        many = "选哪个？\n" + "\n".join(f"{i}. x{i}" for i in range(1, 9))
        self.assertEqual(extract_choices(many), [])

    # #3 做厚：覆盖 Codex / 纪要类更多编号形状
    def test_dash_prefixed_numbers(self):
        self.assertEqual(extract_choices("选哪个？\n- 1. 方案甲\n- 2. 方案乙"), ["方案甲", "方案乙"])

    def test_parenthesized_numbers(self):
        self.assertEqual(extract_choices("which?\n(1) foo\n(2) bar"), ["foo", "bar"])

    def test_circled_numbers(self):
        self.assertEqual(extract_choices("选项：\n① 红\n② 蓝\n请选"), ["红", "蓝"])

    def test_bold_numbers(self):
        self.assertEqual(extract_choices("选哪个方案？\n**1.** 快\n**2.** 稳"), ["快", "稳"])

    def test_lowercase_alpha(self):
        self.assertEqual(extract_choices("pick one:\na) apple\nb) pear"), ["apple", "pear"])

    def test_plain_bulleted_list_without_numbers_not_choices(self):
        # 无编号的纯 markdown 列表不能当选项（否则到处误报）
        self.assertEqual(extract_choices("买这些：\n- 苹果\n- 梨\n- 香蕉"), [])


class MultiSelectTest(unittest.TestCase):
    def test_wants_multi_detects_multi(self):
        self.assertTrue(wants_multi("这几个都要吗？可多选\n1. A\n2. B\n3. C"))
        self.assertTrue(wants_multi("select all that apply:\n1. A\n2. B"))

    def test_wants_multi_false_for_single(self):
        self.assertFalse(wants_multi("选一个\n1. A\n2. B"))

    def test_wants_multi_false_when_no_options(self):
        self.assertFalse(wants_multi("可多选，但这里没有选项"))

    def test_multi_resolve_toggle_and_submit(self):
        self.assertEqual(resolve_multi_click(multi_value("abc", 2)), ("t", "abc", 2))
        self.assertEqual(resolve_multi_click(submit_value("abc")), ("g", "abc", -1))

    def test_multi_resolve_rejects_forgery(self):
        self.assertEqual(resolve_multi_click({"act": "ch", "op": "t", "n": "a", "i": 99}), ("", "", -1))
        self.assertEqual(resolve_multi_click({"act": "ch", "op": "g", "n": "a", "x": 1}), ("", "", -1))
        self.assertEqual(resolve_multi_click({"act": "ch", "op": "z", "n": "a"}), ("", "", -1))

    def test_single_resolve_rejects_multi_shapes(self):
        self.assertEqual(resolve_choice_click(multi_value("a", 1)), ("", -1))
        self.assertEqual(resolve_choice_click(submit_value("a")), ("", -1))


class ResolveTest(unittest.TestCase):
    def test_round_trip_and_forgery(self):
        self.assertEqual(resolve_choice_click(choice_value("abc", 1)), ("abc", 1))
        self.assertEqual(resolve_choice_click({"act": "ch", "n": "abc", "i": 1, "text": "rm"}), ("", -1))
        self.assertEqual(resolve_choice_click({"act": "ch", "n": "abc", "i": 99}), ("", -1))
        self.assertEqual(resolve_choice_click({"act": "ch", "n": "abc", "i": True}), ("", -1))


class WiringTest(unittest.TestCase):
    def make(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.settings = SimpleNamespace(owner_open_ids=frozenset({"ou_me"}))
        bridge.pending = []
        bridge.fleet = SimpleNamespace(set_pending=lambda *a: bridge.pending.append(a))
        bridge._loop = object()
        return bridge

    def test_buttons_then_click_sends_answer_to_same_session(self):
        bridge = self.make()
        buttons = bridge._choice_buttons("group:oc_g", "oc_g", "claude", "s1", MEASURED)
        self.assertEqual(len(buttons), 2)
        value = buttons[1]["behaviors"][0]["value"]
        sent = []

        async def fake_send(scope, chat_id, text):
            sent.append((scope, chat_id, text))

        bridge._maybe_send_to_fleet = fake_send

        def schedule(coro, loop):
            import asyncio
            asyncio.run(coro)

        with mock.patch.object(app_mod.asyncio, "run_coroutine_threadsafe", schedule):
            resp = bridge._on_choice_click(None, "ou_me", value)
        self.assertIn("Blue", resp.toast.content)
        self.assertEqual(bridge.pending, [("group:oc_g", "claude", "s1")])
        self.assertEqual(sent, [("group:oc_g", "oc_g", "Blue")])
        again = bridge._on_choice_click(None, "ou_me", value)
        self.assertIn("过期", again.toast.content)

    def test_non_owner_rejected(self):
        bridge = self.make()
        value = bridge._choice_buttons("group:oc_g", "oc_g", "claude", "s1", MEASURED)[0]["behaviors"][0]["value"]
        bridge._on_choice_click(None, "ou_other", value)
        self.assertEqual(bridge.pending, [])

    def test_no_choices_no_buttons(self):
        self.assertEqual(self.make()._choice_buttons("g", "oc", "claude", "s1", "做完了。"), [])

    def test_multi_toggle_then_submit_sends_joined_answer(self):
        bridge = self.make()
        prompt = "这几个都要哪些？可多选\n1. Red\n2. Blue\n3. Green"
        buttons = bridge._choice_buttons("group:oc_g", "oc_g", "claude", "s1", prompt)
        # 3 个选项 + 1 个「就这些」
        self.assertEqual(len(buttons), 4)
        v = lambda b: b["behaviors"][0]["value"]
        sent = []

        async def fake_send(scope, chat_id, text):
            sent.append((scope, chat_id, text))
        bridge._maybe_send_to_fleet = fake_send

        def schedule(coro, loop):
            import asyncio
            asyncio.run(coro)

        # 勾第 1、第 3 项（toggle 不发、不记 pending）
        bridge._on_choice_click(None, "ou_me", v(buttons[0]))
        bridge._on_choice_click(None, "ou_me", v(buttons[2]))
        self.assertEqual(sent, [])
        self.assertEqual(bridge.pending, [])
        # 点「就这些」= buttons[3]
        with mock.patch.object(app_mod.asyncio, "run_coroutine_threadsafe", schedule):
            resp = bridge._on_choice_click(None, "ou_me", v(buttons[3]))
        self.assertEqual(sent, [("group:oc_g", "oc_g", "Red、Green")])
        self.assertEqual(bridge.pending, [("group:oc_g", "claude", "s1")])
        self.assertIn("Red", resp.toast.content)

    def test_multi_submit_without_pick_is_rejected(self):
        bridge = self.make()
        buttons = bridge._choice_buttons("group:oc_g", "oc_g", "claude", "s1",
                                         "都要哪些？可多选\n1. A\n2. B")
        submit = buttons[-1]["behaviors"][0]["value"]
        resp = bridge._on_choice_click(None, "ou_me", submit)
        self.assertIn("还没勾", resp.toast.content)
        self.assertEqual(bridge.pending, [])


if __name__ == "__main__":
    unittest.main()
