import unittest
from types import SimpleNamespace

from tests.helpers import Clock, FakeBinds, FakeBound, View, sess
from bridge import app as app_mod
from bridge.fleet.escalate import at_owner_line
from bridge.fleet.grouppush import GroupPusher
from bridge.fleet.model import PendingAsk


class AtOwnerTest(unittest.TestCase):
    def test_at_line_only_open_ids(self):
        self.assertEqual(at_owner_line({"ou_a", "bad"}), '<at user_id="ou_a"></at>')
        self.assertEqual(at_owner_line(()), "")


class FakeReplier:
    def __init__(self):
        self.md = []

    def send_md_id(self, chat_id, text):
        self.md.append((chat_id, text))
        return f"om_{len(self.md)}"


class FakeControl:
    def detail(self, tool, sid, n=4):
        return None, [], ""


class FakeScan:
    def __init__(self, bridge):
        self.bridge = bridge

    def overview(self):
        return self.bridge.view


def make_bridge(clock):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.fleet = FakeControl()
    bridge.chat_binds = FakeBinds([FakeBound("oc_g", "claude", "s1")])
    bridge.group_pusher = GroupPusher(now=clock)
    bridge.waiting_notifier = SimpleNamespace(enabled=False, due=lambda v: [])
    bridge.settings = SimpleNamespace(owner_open_ids=frozenset({"ou_me"}))
    bridge.replier = FakeReplier()
    bridge.urged = []
    bridge.group_client = SimpleNamespace(
        urgent=lambda mid, ids: bridge.urged.append((mid, sorted(ids))) or (True, 0, ""))
    bridge.sent = []
    bridge._say = lambda chat_id, text: bridge.sent.append((chat_id, text)) or True
    bridge._fleet_scanner = lambda: FakeScan(bridge)
    bridge.status_cards = None
    return bridge


def waiting(question="要删 build/ 目录吗？"):
    ask = PendingAsk(tool="Bash", summary=question)
    return sess(status="waiting", pending=ask)


class WiringTest(unittest.TestCase):
    def test_waiting_push_mentions_owner_and_never_urges(self):
        clock = Clock()
        bridge = make_bridge(clock)
        bridge.view = View([waiting()])
        bridge._notify_tick()
        clock.tick(25)
        bridge._notify_tick()
        self.assertEqual(len(bridge.replier.md), 1)
        self.assertIn('<at user_id="ou_me"></at>', bridge.replier.md[0][1])
        self.assertIn("需要人介入", bridge.replier.md[0][1])
        clock.tick(601)
        bridge._notify_tick()
        clock.tick(1200)
        bridge._notify_tick()
        self.assertEqual(bridge.urged, [])
        self.assertFalse(any("10 分钟" in t for _c, t in bridge.sent))

    def test_failed_push_mentions_owner(self):
        clock = Clock()
        bridge = make_bridge(clock)
        bridge.view = View([sess(status="running")])
        bridge._notify_tick()
        bridge.view = View([sess(status="idle", last_turn="failed", last_turn_at=clock() + 1)])
        clock.tick(5)
        bridge._notify_tick()
        self.assertEqual(len(bridge.replier.md), 1)
        self.assertIn("出错", bridge.replier.md[0][1])
        self.assertIn('<at user_id="ou_me"></at>', bridge.replier.md[0][1])
        clock.tick(1200)
        bridge._notify_tick()
        self.assertEqual(bridge.urged, [])

    def test_no_urgent_api_on_group_client(self):
        from bridge.fleet import groupsetup

        self.assertFalse(any(hasattr(c, "urgent") for c in vars(groupsetup).values() if isinstance(c, type)))


if __name__ == "__main__":
    unittest.main()
