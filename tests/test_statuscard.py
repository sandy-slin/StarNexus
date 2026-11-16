import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers import sess
from bridge import app as app_mod
from bridge.fleet.statuscard import (
    CardInfo,
    StatusCardKeeper,
    StatusCards,
    build_card,
    resolve_click,
)


class FakeFeishu:
    def __init__(self, pin_ok=True, patch_ok=True):
        self.sent, self.patched, self.pinned = [], [], []
        self.pin_ok, self.patch_ok = pin_ok, patch_ok

    def send(self, chat_id, card):
        mid = f"om_{len(self.sent) + 1}"
        self.sent.append((chat_id, card, mid))
        return mid

    def patch(self, mid, card):
        self.patched.append((mid, card))
        return self.patch_ok

    def pin(self, mid):
        self.pinned.append(mid)
        return (True, 0, "") if self.pin_ok else (False, 99991672, "no scope")


def info(**kw):
    base = dict(tool="codex", sid="c1", title="修登录", project="remote_control",
                model="gpt-5 · high", mode="全授权", status="running")
    base.update(kw)
    return CardInfo(**base)


class CardTest(unittest.TestCase):
    def test_card_shows_fields_and_session_buttons(self):
        card = json.loads(build_card(info()))
        text = card["body"]["elements"][0]["content"]
        for want in ("Codex", "gpt-5 · high", "remote_control", "全授权", "正在跑"):
            self.assertIn(want, text)
        blob = json.dumps(card, ensure_ascii=False)
        for want in ("🧠 模型", "⏹ 停下", "✅ 完成", "🔄 刷新"):
            self.assertIn(want, blob)
        self.assertIn('"act": "mdl"', blob)
        self.assertNotIn("c1", json.dumps([e for e in card["body"]["elements"][1:]]))

    def test_finished_card(self):
        blob = build_card(info(finished=True))
        self.assertIn("已完成", blob)
        self.assertIn("重新打开", blob)

    def test_click_whitelist(self):
        self.assertEqual(resolve_click({"act": "sc", "op": "done"}), "done")
        self.assertEqual(resolve_click({"act": "sc", "op": "rm"}), "")
        self.assertEqual(resolve_click({"act": "sc", "op": "done", "id": "x"}), "")
        self.assertEqual(resolve_click("x"), "")


class KeeperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "sc.json")

    def test_first_send_pins_then_patch_in_place(self):
        fs = FakeFeishu()
        keeper = StatusCardKeeper(StatusCards(self.path), fs.send, fs.patch, fs.pin)
        self.assertEqual(keeper.ensure("oc_g", info()), "sent_pinned")
        self.assertEqual(fs.pinned, ["om_1"])
        self.assertEqual(keeper.ensure("oc_g", info()), "same")
        self.assertEqual(keeper.ensure("oc_g", info(status="waiting")), "patched")
        self.assertEqual(len(fs.sent), 1)
        self.assertEqual(fs.patched[0][0], "om_1")

    def test_survives_restart(self):
        fs = FakeFeishu()
        StatusCardKeeper(StatusCards(self.path), fs.send, fs.patch, fs.pin).ensure("oc_g", info())
        again = StatusCardKeeper(StatusCards(self.path), fs.send, fs.patch, fs.pin)
        self.assertEqual(again.ensure("oc_g", info(model="luna")), "patched")
        self.assertEqual(len(fs.sent), 1)

    def test_pin_missing_scope_degrades(self):
        fs = FakeFeishu(pin_ok=False)
        keeper = StatusCardKeeper(StatusCards(self.path), fs.send, fs.patch, fs.pin)
        self.assertEqual(keeper.ensure("oc_g", info()), "sent")
        self.assertFalse(StatusCards(self.path).get("oc_g")["pinned"])

    def test_patch_failure_resends(self):
        fs = FakeFeishu(patch_ok=False)
        keeper = StatusCardKeeper(StatusCards(self.path), fs.send, fs.patch, fs.pin)
        keeper.ensure("oc_g", info())
        self.assertEqual(keeper.ensure("oc_g", info(status="idle")), "sent_pinned")
        self.assertEqual(len(fs.sent), 2)


class FakeBinds:
    def __init__(self, bound):
        self.bound = bound
        self.names = []

    def of_chat(self, chat_id):
        return self.bound if chat_id == self.bound.chat_id else None

    def all(self):
        return [self.bound]

    def set_name(self, chat_id, name):
        self.bound.name = name
        self.names.append(name)


class FakeGroup:
    def __init__(self, name):
        self.name = name

    def name_of(self, chat_id):
        return self.name, 0, ""

    def rename(self, chat_id, name):
        self.name = name
        return True, 0, ""


def make_bridge(tmp, fs, session):
    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bound = SimpleNamespace(chat_id="oc_g", tool="codex", id="c1", on=True,
                            name="SN·Codex·remote_control·修登录")
    bridge.chat_binds = FakeBinds(bound)
    bridge.group_client = FakeGroup(bound.name)
    bridge.fleet = SimpleNamespace(cached=lambda tool, sid: session)
    bridge.settings = SimpleNamespace(policy=SimpleNamespace(mode="trusted"), owner_open_ids={"ou_me"})
    bridge.model_prefs = None
    bridge.status_cards = StatusCardKeeper(StatusCards(os.path.join(tmp, "sc.json")),
                                           fs.send, fs.patch, fs.pin)
    return bridge


class WiringTest(unittest.TestCase):
    def test_refresh_sends_pinned_card_with_session_facts(self):
        fs = FakeFeishu()
        bridge = make_bridge(tempfile.mkdtemp(), fs,
                             sess(tool="codex", sid="c1", status="running", cwd="/x/remote_control"))
        self.assertEqual(bridge._refresh_status_card("oc_g"), "sent_pinned")
        text = fs.sent[0][1]
        self.assertIn("remote_control", text)
        self.assertIn("全授权", text)
        self.assertIn("工具默认", text)
        self.assertEqual(bridge._refresh_status_card("not_a_group"), "not_group")

    def test_tick_updates_in_place(self):
        fs = FakeFeishu()
        holder = {"s": sess(tool="codex", sid="c1", status="running")}
        bridge = make_bridge(tempfile.mkdtemp(), fs, None)
        bridge.fleet = SimpleNamespace(cached=lambda tool, sid: holder["s"])
        bridge._status_cards_tick()
        holder["s"] = sess(tool="codex", sid="c1", status="idle", last_turn="done", last_turn_at=1.0)
        bridge._status_cards_tick()
        bridge._status_cards_tick()
        self.assertEqual(len(fs.sent), 1)
        self.assertEqual(len(fs.patched), 1)
        self.assertIn("上一轮完成", fs.patched[0][1])

    def test_done_prefixes_group_name_and_reopen_restores(self):
        fs = FakeFeishu()
        bridge = make_bridge(tempfile.mkdtemp(), fs, sess(tool="codex", sid="c1"))
        bridge._refresh_status_card("oc_g")
        bridge._mark_session_done("oc_g", True)
        self.assertEqual(bridge.group_client.name, "✅SN·Codex·remote_control·修登录")
        self.assertIn("已完成", fs.patched[-1][1])
        bridge._mark_session_done("oc_g", False)
        self.assertEqual(bridge.group_client.name, "SN·Codex·remote_control·修登录")

    def test_done_keeps_owner_renamed_group(self):
        fs = FakeFeishu()
        bridge = make_bridge(tempfile.mkdtemp(), fs, sess(tool="codex", sid="c1"))
        bridge.group_client.name = "我自己起的名"
        bridge._mark_session_done("oc_g", True)
        self.assertEqual(bridge.group_client.name, "我自己起的名")


if __name__ == "__main__":
    unittest.main()
