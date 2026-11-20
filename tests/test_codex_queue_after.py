"""一轮在跑时再说一句 ⇒ 排在这一轮后面，不再回「上一条还在发」。

1.9.8 只对 Codex、且只在 App / 终端占着时排队。2026-11-21 主人口径「整体都走 steering」
⇒ 凡声明了 `steer` 能力的工具都走这条兜底，owner 不再限制（见 tests/test_steer_fallback_queue.py）。
没有排队通道的工具（cursor）照旧走原来的提示。
"""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.codex import CodexAdapter
from bridge.fleet.model import ActionResult

TID = "01a1dac3-9c25-7963-a9e2-fd7a615b0ea3"


def adapter(owner):
    ad = CodexAdapter.__new__(CodexAdapter)
    ad._row = lambda tid: {"id": tid, "cwd": "/tmp"}
    ad._state = lambda ids: {TID: (owner, (1,), "running")} if owner != "none" else {}
    ad._queue_locked = mock.Mock(return_value=ActionResult(True, "回复B", TID, delivered=True))
    return ad


class RelayQueuedTest(unittest.TestCase):
    def test_app_owned_session_is_queued(self):
        ad = adapter("app")
        got = ad.relay_queued(TID, "第二句")
        self.assertTrue(got.ok)
        self.assertEqual(ad._queue_locked.call_args[0][:2], (TID, "第二句"))

    def test_terminal_owned_session_is_queued(self):
        self.assertTrue(adapter("terminal").relay_queued(TID, "第二句").ok)

    def test_bridge_or_nobody_not_applicable(self):
        for owner in ("bridge", "none"):
            ad = adapter(owner)
            got = ad.relay_queued(TID, "第二句")
            self.assertFalse(got.ok)
            self.assertEqual(got.text, "")
            ad._queue_locked.assert_not_called()

    def test_bad_id_or_empty_text(self):
        ad = adapter("app")
        self.assertFalse(ad.relay_queued("not-a-thread", "x").ok)
        self.assertFalse(ad.relay_queued(TID, "  ").ok)


def make_bridge(result, owner="app"):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.said = []
    bridge.replies = []
    bridge._paused = {}
    bridge.reviewer = None
    bridge._session_policy = lambda tool, sid: SimpleNamespace(mode="trusted", turn_timeout_sec=60)
    bridge._say = lambda chat_id, text: bridge.said.append(text)

    async def reply(chat_id, label, body, tail):
        bridge.replies.append(body)

    bridge._send_reply_maybe_doc = reply
    bridge.fleet = SimpleNamespace(relay_queued=mock.Mock(return_value=result),
                                   refresh=lambda tool, sid: None,
                                   cached=lambda tool, sid: SimpleNamespace(owner=owner))
    return bridge


TARGET = SimpleNamespace(tool="codex", id=TID, label="Codex「her remote」")


class TryQueueAfterTest(unittest.TestCase):
    def run_it(self, bridge, n=1):
        async def go():
            outs = [await bridge._try_queue_after("group:oc", "oc", TARGET, f"第{i}句") for i in range(n)]
            await asyncio.sleep(0.2)
            return outs
        return asyncio.run(go())

    def test_queued_and_reply_posted(self):
        bridge = make_bridge(ActionResult(True, "回复B", TID, delivered=True))
        self.assertEqual(self.run_it(bridge), [True])
        self.assertIn("排在它后面", bridge.said[0])
        self.assertNotIn("上一条还在发", "".join(bridge.said))
        self.assertEqual(bridge.replies, ["回复B"])

    def test_any_owner_is_queued(self):
        """owner 不再限制：桥自己起的那一轮结束后也排得上（11-20 现场就是这一种）。"""
        for owner in ("bridge", "none", "unknown"):
            bridge = make_bridge(ActionResult(True, "r", TID, delivered=True), owner=owner)
            self.assertEqual(self.run_it(bridge), [True])
            bridge.fleet.relay_queued.assert_called()

    def test_claude_queued_here_too(self):
        """Claude 也声明了 steer ⇒ 同样走这条兜底，不再在这里被挡掉。"""
        bridge = make_bridge(ActionResult(True, "r", TID, delivered=True))
        claude = SimpleNamespace(tool="claude", id=TID, label="Claude")
        self.assertTrue(asyncio.run(bridge._try_queue_after("group:oc", "oc", claude, "x")))

    def test_cursor_not_queued_here(self):
        """cursor 没有 steer 能力、也没有排队通道 ⇒ 退回原来的提示。"""
        bridge = make_bridge(ActionResult(True, "r", TID, delivered=True))
        cursor = SimpleNamespace(tool="cursor", id="c1", label="Cursor")
        self.assertFalse(asyncio.run(bridge._try_queue_after("group:oc", "oc", cursor, "x")))
        self.assertEqual(bridge.said, [])

    def test_queued_but_no_reply_says_why(self):
        bridge = make_bridge(ActionResult(False, "话已经排进去了。", TID, delivered=True))
        self.run_it(bridge)
        self.assertEqual(bridge.replies, [])
        self.assertIn("话已经排进去了。", bridge.said)

    def test_cap_on_queued_messages(self):
        import threading
        hold = threading.Event()

        def slow(*a, **k):
            hold.wait(2)
            return ActionResult(True, "r", TID, delivered=True)

        bridge = make_bridge(None)
        bridge.fleet.relay_queued.side_effect = slow

        async def go():
            outs = [await bridge._try_queue_after("group:oc", "oc", TARGET, f"第{i}句")
                    for i in range(app_mod.QUEUE_AFTER_MAX + 1)]
            hold.set()
            await asyncio.sleep(0.3)
            return outs
        outs = asyncio.run(go())
        self.assertEqual(outs[-1], False)
        self.assertTrue(all(outs[:-1]))

    def test_paused_not_queued(self):
        bridge = make_bridge(ActionResult(True, "r", TID, delivered=True))
        bridge._paused = {"group:oc": "x"}
        self.assertEqual(self.run_it(bridge), [False])
        bridge.fleet.relay_queued.assert_not_called()


if __name__ == "__main__":
    unittest.main()
