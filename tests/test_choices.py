"""FC-07：回复结尾的编号选项 ⇒ 按钮；点一下把那一项发回同一个会话。"""
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.choices import choice_value, extract_choices, resolve_choice_click

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


if __name__ == "__main__":
    unittest.main()
