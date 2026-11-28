"""工具栏点击 → StarNexus 已有处理器的路由（app.py，借鉴官方 20261123 第 37 轮，适配移植）。

用 `Bridge.__new__` 装一个最小 Bridge，验证闸门和每个按钮路由到的处理器，不碰飞书 / asyncio loop：
- `_toolbar_bound` 闸门：主人 + 群绑的是 claude + on。
- 5 个 `_toolbar_*` 分派到：/stop、relay「接着干」、已有模型卡、详情卡、git 改动。
  （只验「调到了哪条已有的路」，不重测那些路自己的逻辑 —— 它们各有单测。）
"""

import asyncio
import sys
import unittest
from types import SimpleNamespace

from tests import helpers  # noqa: F401

from bridge import app as app_mod

CHAT = "oc_" + "a" * 20
OWNER = "ou_me"
SID = "11111111-2222-4333-8444-555555555555"


def _bound(tool="claude", on=True):
    return SimpleNamespace(chat_id=CHAT, tool=tool, id=SID, name="Claude · 修登录",
                           on=on, scope="group:" + CHAT)


def _bridge(bound=None):
    b = app_mod.Bridge.__new__(app_mod.Bridge)
    b.settings = SimpleNamespace(owner_open_ids=frozenset({OWNER}))
    b.chat_binds = SimpleNamespace(of_chat=lambda cid: bound)
    return b


class ToolbarGateTest(unittest.TestCase):
    def test_rejects_non_owner(self):
        b = _bridge(_bound())
        self.assertIsNone(b._toolbar_bound(CHAT, "ou_someone_else", "stop"))

    def test_rejects_non_claude_group(self):
        b = _bridge(_bound(tool="codex"))
        self.assertIsNone(b._toolbar_bound(CHAT, OWNER, "stop"))

    def test_rejects_unbound_group(self):
        b = _bridge(None)
        self.assertIsNone(b._toolbar_bound(CHAT, OWNER, "stop"))

    def test_rejects_turned_off(self):
        b = _bridge(_bound(on=False))
        self.assertIsNone(b._toolbar_bound(CHAT, OWNER, "stop"))

    def test_accepts_owner_claude_on(self):
        b = _bridge(_bound())
        self.assertIsNotNone(b._toolbar_bound(CHAT, OWNER, "stop"))


class OnToolbarClickTest(unittest.TestCase):
    def test_unknown_action_dropped(self):
        b = _bridge(_bound())
        b._loop = SimpleNamespace()
        self.assertIsNone(b._on_toolbar_click(SimpleNamespace(action="danger", chat_id=CHAT, open_id=OWNER)))

    def test_no_loop_yet_dropped(self):
        b = _bridge(_bound())
        b._loop = None
        self.assertIsNone(b._on_toolbar_click(SimpleNamespace(action="stop", chat_id=CHAT, open_id=OWNER)))


class ToolbarDispatchTest(unittest.IsolatedAsyncioTestCase):
    """每个按钮路由到哪条已有的路（记录调用，不真执行）。"""

    def _bridge_with_routes(self, calls):
        b = _bridge(_bound())
        b._say = lambda chat, text: calls.append(("say", text[:12]))
        b._sync_talk_marker = lambda scope: None
        b._talking_to = lambda scope: None

        async def handle_command(scope, chat, text):
            calls.append(("/cmd", text))
            return True
        b._handle_command = handle_command

        async def maybe_send(scope, chat, text):
            calls.append(("relay", text.split("（")[0]))
            return True
        b._maybe_send_to_fleet = maybe_send
        b._relay_granted = lambda *a: True
        b._talk_ident_same = lambda *a: False
        b._paused = {}
        b._fleet_inflight = {}

        async def fleet_detail(chat, tool, sid, scope=""):
            calls.append(("detail", tool))
        b._fleet_detail = fleet_detail

        b.fleet = SimpleNamespace(
            stop=lambda tool, sid: SimpleNamespace(ok=True, text="停了"),
            refresh=lambda tool, sid: (SimpleNamespace(cwd="/tmp/x"), ""),
            cached=lambda tool, sid: SimpleNamespace(cwd="/tmp/x"))
        # policy 是只读属性，派生自 settings.policy。目录不在白名单 ⇒ changes 走「不在范围」
        b.settings.policy = SimpleNamespace(resolve_workspace=lambda p: None)
        b.prefs = None
        b.model_prefs = SimpleNamespace()
        b._send_card_or_text = lambda chat, card, fb: calls.append(("card", "sent"))
        return b

    async def test_stop_routes_to_slash_stop_when_talking(self):
        calls = []
        b = self._bridge_with_routes(calls)
        b._talking_to = lambda scope: ("claude", SID)  # 有在说话的记录 ⇒ 走 /stop
        await b._toolbar_dispatch("stop", _bound())
        self.assertIn(("/cmd", "/stop"), calls)

    async def test_stop_routes_to_fleet_stop_when_idle(self):
        calls = []
        b = self._bridge_with_routes(calls)
        await b._toolbar_dispatch("stop", _bound())
        self.assertTrue(any(c[0] == "say" for c in calls))  # 直接问 adapter 停、回一句

    async def test_go_on_routes_to_relay(self):
        calls = []
        b = self._bridge_with_routes(calls)
        await b._toolbar_dispatch("go_on", _bound())
        self.assertIn(("relay", b.TOOLBAR_GO_ON_TEXT), calls)

    async def test_progress_routes_to_detail(self):
        calls = []
        b = self._bridge_with_routes(calls)
        await b._toolbar_dispatch("progress", _bound())
        self.assertIn(("detail", "claude"), calls)

    async def test_changes_respects_workspace_whitelist(self):
        calls = []
        b = self._bridge_with_routes(calls)
        await b._toolbar_dispatch("changes", _bound())
        # 目录不在白名单 ⇒ 回一句「不在范围」，不跑 git
        self.assertTrue(any(c[0] == "say" for c in calls))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
