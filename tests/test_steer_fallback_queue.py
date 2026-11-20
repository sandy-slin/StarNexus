"""插话插不进时排队，不再回「上一条还在发」（2026-11-21 主人口径：整体都走 steering）。

现场证据（11-20 agent.log）：22:51 插话成功一次，之后 23:00、23:00、23:01、23:07、23:37
五次全是「中途插话 结果=没插进」⇒ 紧跟一条「对话模式 拒收：上一条还在发」，`同一个=True`。
也就是同一个 Claude 会话、桥自己起的那一轮已经结束（stdin 关了），而终端没占着它
⇒ `steer()` 两条路都不通；而排队兜底（`_try_queue_after`）当时只对 codex 开着。

主人要的是「整体都是 steering 的方式」：插不进也不该让人重发，排在这一轮后面。
"""
import asyncio
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.model import ActionResult

SID = "11111111-2222-3333-4444-555555555555"


class FakeControl:
    """steer 一律失败（复现 11-20 现场），relay_queued 记录被排进来的话。"""

    def __init__(self, sess):
        self.sess = sess
        self.pending = {}
        self.queued = []

    def refresh(self, tool, sid):
        return self.sess, ""

    def cached(self, tool, sid):
        return self.sess

    def peek_pending_fresh(self, scope):
        return self.pending.get(scope), False

    def peek_pending(self, scope):
        return self.pending.get(scope)

    def set_pending(self, scope, tool, sid):
        target = SimpleNamespace(tool=tool, id=sid, label="Claude「x」")
        self.pending[scope] = target
        return target

    def clear_pending(self, scope):
        self.pending.pop(scope, None)

    def steer(self, tool, sid, text):
        return ActionResult(False, "", sid)

    def relay_queued(self, tool, sid, text, timeout=600):
        self.queued.append((tool, sid, text))
        return ActionResult(True, "排在后面那句的回复", sid)


def bridge_with(sess):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.fleet = FakeControl(sess)
    bridge.reviewer = None
    bridge._group_bind_arm = lambda *a: asyncio.sleep(0, result=False)
    bridge._restart_talk_offer = lambda *a: False
    bridge._session_policy = lambda tool, sid: SimpleNamespace(mode="trusted", turn_timeout_sec=600)
    bridge._talk_buttons = lambda tool, sid: []
    said = []
    bridge._say = lambda chat_id, text: said.append(text) or True
    bridge._say_with_buttons = lambda chat_id, text, buttons: said.append(text) or True
    sent = []

    async def send_reply(chat_id, title, body, tail):
        sent.append(body)

    bridge._send_reply_maybe_doc = send_reply
    return bridge, said, sent


class SteerFallbackQueueTest(unittest.TestCase):
    def claude_session(self, owner):
        return SimpleNamespace(tool="claude", id=SID, owner=owner, title="x", cwd="/tmp")

    async def run_busy(self, bridge):
        bridge.fleet.set_pending("group:oc_g", "claude", SID)
        bridge._fleet_inflight = {"group:oc_g": ("claude", SID)}
        handled = await bridge._maybe_send_to_fleet("group:oc_g", "oc_g", "再说一句")
        # 排队的回复在后台任务里等，让它跑完
        await asyncio.sleep(0)
        for task in asyncio.all_tasks() - {asyncio.current_task()}:
            await task
        return handled

    def test_claude_terminal_queued_instead_of_refused(self):
        """终端占着的 Claude：插不进 ⇒ 排在后面，不再回「上一条还在发」。"""
        bridge, said, _sent = bridge_with(self.claude_session("terminal"))
        handled = asyncio.run(self.run_busy(bridge))
        self.assertTrue(handled)
        self.assertEqual(bridge.fleet.queued, [("claude", SID, "再说一句")])
        self.assertNotIn("上一条还在发", "".join(said))

    def test_claude_bridge_owned_queued_too(self):
        """桥自己起的那一轮已经结束（stdin 关了）⇒ 也排在后面，这正是 11-20 的现场。"""
        bridge, said, _sent = bridge_with(self.claude_session("bridge"))
        handled = asyncio.run(self.run_busy(bridge))
        self.assertTrue(handled)
        self.assertEqual(bridge.fleet.queued, [("claude", SID, "再说一句")])
        self.assertNotIn("上一条还在发", "".join(said))

    def test_queue_reply_comes_back(self):
        """排进去那句的回复要贴回聊天里，不能静默。"""
        bridge, _said, sent = bridge_with(self.claude_session("terminal"))
        asyncio.run(self.run_busy(bridge))
        self.assertIn("排在后面那句的回复", "".join(sent))

    def test_cursor_still_refused(self):
        """不支持 steering 也没有排队通道的工具（cursor）⇒ 照旧提示，不假装排上了。"""
        sess = SimpleNamespace(tool="cursor", id="c1", owner="none", title="x", cwd="/tmp")
        bridge, said, _sent = bridge_with(sess)
        bridge.fleet.set_pending("group:oc_g", "cursor", "c1")
        bridge._fleet_inflight = {"group:oc_g": ("cursor", "c1")}
        handled = asyncio.run(bridge._maybe_send_to_fleet("group:oc_g", "oc_g", "x"))
        self.assertTrue(handled)
        self.assertEqual(bridge.fleet.queued, [])
        self.assertIn("上一条还在发", "".join(said))


if __name__ == "__main__":
    unittest.main()
