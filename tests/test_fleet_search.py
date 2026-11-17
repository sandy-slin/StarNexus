"""§5.7 搜索会话：/find 关键字 ⇒ 按标题 / 项目 / 编号列出，按钮进群。"""
import asyncio
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod
from bridge.fleet.cards import resolve_fleet_click
from bridge.fleet.control import FleetControl
from bridge.fleet.model import Session


def sess(tool, sid, title, cwd, at):
    return Session(tool=tool, surface="cli", id=sid, title=title, cwd=cwd, status="idle", updated_at=at,
                   can_send=True)


class SearchTest(unittest.TestCase):
    def control(self):
        control = FleetControl(fleet=SimpleNamespace())
        for s in (sess("codex", "a1", "修登录", "/u/work/remote_control", 1),
                  sess("claude", "b2", "写周报", "/u/work/notes", 3),
                  sess("codex", "c3", "登录页样式", "/u/work/web", 2)):
            control._remember(s)
        return control

    def test_matches_title_project_and_all_words(self):
        control = self.control()
        self.assertEqual([s.id for s in control.search("登录")], ["c3", "a1"])
        self.assertEqual([s.id for s in control.search("remote_control")], ["a1"])
        self.assertEqual([s.id for s in control.search("登录 web")], ["c3"])
        self.assertEqual(control.search("  "), [])

    def test_command_lists_buttons(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.fleet = self.control()
        bridge.fleet.overview = lambda: None
        bridge._fleet_scanner = lambda: None
        bridge.settings = SimpleNamespace(policy=SimpleNamespace(workspaces=("/u",), default_workspace="/u",
                                                                 resolve_workspace=lambda c: c))
        out = []
        bridge._say_with_buttons = lambda chat, text, buttons: out.append((text, buttons))
        bridge._say = lambda chat, text: out.append((text, []))
        asyncio.run(bridge._fleet_search("dm:ou_me", "oc_dm", "登录"))
        text, buttons = out[0]
        self.assertIn("找到 2 个", text)
        values = [b["behaviors"][0]["value"] for b in buttons if b["behaviors"][0]["value"].get("act") == "fleet"]
        self.assertEqual([v["id"] for v in values], ["c3", "a1"])
        for v in values:
            self.assertIsNotNone(resolve_fleet_click(v, bridge.fleet.known_keys()))

    def test_existing_group_button_jumps_in_one_click(self):
        bridge = app_mod.Bridge.__new__(app_mod.Bridge)
        bridge.fleet = self.control()
        bridge.fleet.overview = lambda: None
        bridge._fleet_scanner = lambda: None
        bridge.settings = SimpleNamespace(policy=SimpleNamespace(workspaces=("/u",), default_workspace="/u",
                                                                 resolve_workspace=lambda c: c))
        bound = SimpleNamespace(chat_id="oc_group_a1")
        bridge.chat_binds = SimpleNamespace(
            of_chat=lambda chat_id: None,
            of_session=lambda tool, sid: bound if sid == "a1" else None,
        )
        bridge.group_client = SimpleNamespace(
            open_link=lambda chat_id: f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}")
        bridge._chat_ids = {}
        out = []
        bridge._say_with_buttons = lambda chat, text, buttons: out.append((text, buttons))
        bridge._say = lambda chat, text: out.append((text, []))
        asyncio.run(bridge._fleet_search("dm:ou_me", "oc_dm", "登录"))
        _, buttons = out[0]
        by_id = {}
        for button in buttons:
            callback = [b for b in button["behaviors"] if b["type"] == "callback"][0]["value"]
            if callback.get("act") == "fleet":
                by_id[callback["id"]] = button
        jump = [b for b in by_id["a1"]["behaviors"] if b["type"] == "open_url"]
        self.assertEqual(len(jump), 1)
        self.assertIn("oc_group_a1", jump[0]["default_url"])
        a1_callback = [b for b in by_id["a1"]["behaviors"] if b["type"] == "callback"][0]["value"]
        self.assertIsNotNone(resolve_fleet_click(a1_callback, bridge.fleet.known_keys()))
        self.assertFalse([b for b in by_id["c3"]["behaviors"] if b["type"] == "open_url"])

    def test_word_routes_to_command(self):
        self.assertTrue(app_mod._is_fleet_word("找会话 登录"))
        self.assertFalse(app_mod._is_fleet_word("找会话登录的问题"))


if __name__ == "__main__":
    unittest.main()
