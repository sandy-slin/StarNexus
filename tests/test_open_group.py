import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from tests.helpers import sess
from bridge import app as app_mod
from bridge.fleet.chatbind import CREATED, REUSED, ChatBinds, ensure_group


class SlowClient:
    def __init__(self):
        self.created = []
        self._lock = threading.Lock()

    def create(self, name, owner):
        time.sleep(0.05)
        with self._lock:
            cid = f"oc_{len(self.created) + 1:04d}abcdef"
            self.created.append(name)
        return cid, 0, ""

    def exists(self, chat_id):
        return True

    @staticmethod
    def open_link(chat_id):
        return f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}"


class EnsureTwiceTest(unittest.TestCase):
    def test_double_click_creates_one_group(self):
        binds = ChatBinds(os.path.join(tempfile.mkdtemp(), "b.json"))
        client = SlowClient()
        results = []

        def click():
            results.append(ensure_group(binds, client, "codex", "c1", "修登录",
                                        owner_open_id="ou_me", project="/x/remote_control"))

        threads = [threading.Thread(target=click) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(client.created), 1)
        self.assertEqual(sorted(r.how for r in results), sorted([CREATED, REUSED]))
        self.assertEqual(results[0].bound.chat_id, results[1].bound.chat_id)
        self.assertTrue(client.created[0].startswith("SN·Codex·remote_control"))


def make_bridge(binds):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.chat_binds = binds
    bridge.group_client = SlowClient()
    bridge._chat_ids = {"dm:ou_me": "oc_dm"}
    return bridge


class RowButtonTest(unittest.TestCase):
    def setUp(self):
        self.binds = ChatBinds(os.path.join(tempfile.mkdtemp(), "b.json"))

    def test_unbound_session_row_creates_group(self):
        make = make_bridge(self.binds)._overview_row_button("oc_dm")
        button = make(sess(tool="codex", sid="c1", can_send=True), "修登录", False)
        value = button["behaviors"][0]["value"]
        self.assertEqual(value, {"act": "fleet", "op": "send", "tool": "codex", "id": "c1"})

    def test_bound_session_row_jumps_into_group(self):
        self.binds.bind("oc_0001abcdef", "codex", "c1", "SN·Codex·x·修登录")
        make = make_bridge(self.binds)._overview_row_button("oc_dm")
        button = make(sess(tool="codex", sid="c1", can_send=True), "修登录", False)
        kinds = [b["type"] for b in button["behaviors"]]
        self.assertEqual(kinds, ["open_url", "callback"])
        self.assertIn("oc_0001abcdef", button["behaviors"][0]["default_url"])
        self.assertEqual(button["behaviors"][1]["value"]["op"], "go")

    def test_session_that_cannot_talk_keeps_detail(self):
        make = make_bridge(self.binds)._overview_row_button("oc_dm")
        self.assertIsNone(make(sess(tool="codex", sid="c1"), "修登录", False))

    def test_inside_a_session_group_no_direct_rows(self):
        self.binds.bind("oc_0001abcdef", "codex", "c1", "SN·Codex·x·修登录")
        self.assertIsNone(make_bridge(self.binds)._overview_row_button("oc_0001abcdef"))

    def test_overview_card_uses_row_button(self):
        from bridge.fleet.cards import build_overview_card
        from bridge.fleet.scan import Overview

        make = make_bridge(self.binds)._overview_row_button("oc_dm")
        card = build_overview_card(Overview(sessions=[sess(tool="codex", sid="c1", can_send=True)],
                                            ok_tools=["codex"]),
                                   seen_at=None, settled=None, row_button=make)
        self.assertIn('"op": "send"', card)
        self.assertNotIn('"op": "open"', json.dumps(json.loads(card)["body"]["elements"][1]))


if __name__ == "__main__":
    unittest.main()
