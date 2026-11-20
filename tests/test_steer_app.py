"""1.9.9：插话不再只对「桥自己起的那一轮」有效。

- Codex App 里开着的那一轮 ⇒ 走 App 的内部通道插进去（bridge/fleet/appsteer.py）
- 电脑终端里开着的 Claude 会话 ⇒ 走会话间消息送进它的输入队列，不等回复
"""
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import helpers  # noqa: F401

from bridge.fleet import appsteer
from bridge.fleet.claude import STEER_OK, WHY_STEER_NOT_RUNNING, ClaudeAdapter
from bridge.fleet.codex import CodexAdapter
from bridge.fleet.model import ActionResult

TID = "01a1dac3-9c25-7963-a9e2-fd7a615b0ea3"
SID = "7ad2f201-aaaa-bbbb-cccc-444444444444"


class FakeIpcServer:
    """照 App 那个 socket 的样子应答：4 字节小端长度 + JSON。"""

    def __init__(self, reply="ok"):
        self.reply = reply
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, "ipc"), exist_ok=True)
        self.path = os.path.join(self.home, "ipc", "ipc.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        self.sock.listen(1)
        self.seen = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _read(self, conn):
        head = conn.recv(4)
        if len(head) < 4:
            return None
        size = struct.unpack("<I", head)[0]
        body = b""
        while len(body) < size:
            chunk = conn.recv(size - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body)

    def _send(self, conn, obj):
        body = json.dumps(obj).encode()
        conn.sendall(struct.pack("<I", len(body)) + body)

    def _serve(self):
        conn, _ = self.sock.accept()
        try:
            while True:
                msg = self._read(conn)
                if msg is None:
                    return
                self.seen.append(msg)
                if msg.get("method") == "initialize":
                    self._send(conn, {"type": "response", "requestId": msg["requestId"],
                                      "resultType": "success", "method": "initialize",
                                      "result": {"clientId": "fake-client"}})
                    continue
                # 中间夹一条广播：调用方必须跳过它，不能当成回执
                self._send(conn, {"type": "broadcast", "method": "client-status-changed", "params": {}})
                if self.reply == "ok":
                    self._send(conn, {"type": "response", "requestId": msg["requestId"],
                                      "resultType": "success", "method": msg["method"],
                                      "result": {"result": {"turnId": "turn-1"}}})
                elif self.reply == "no-owner":
                    self._send(conn, {"type": "response", "requestId": msg["requestId"],
                                      "resultType": "error", "error": "no-client-found"})
                else:
                    conn.close()
                    return
        except OSError:
            pass

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class AppSteerTest(unittest.TestCase):
    def test_steers_into_running_turn(self):
        server = FakeIpcServer()
        self.addCleanup(server.close)
        got = appsteer.steer(TID, "改一下方向", codex_home=server.home)
        self.assertTrue(got.ok)
        self.assertEqual(got.turn_id, "turn-1")
        sent = [m for m in server.seen if m.get("method") == appsteer.STEER_METHOD][0]
        self.assertEqual(sent["params"]["conversationId"], TID)
        self.assertEqual(sent["params"]["input"][0]["text"], "改一下方向")

    def test_no_owner_is_not_ok(self):
        server = FakeIpcServer(reply="no-owner")
        self.addCleanup(server.close)
        got = appsteer.steer(TID, "x", codex_home=server.home)
        self.assertFalse(got.ok)
        self.assertIn("no-client-found", got.why)

    def test_socket_closed_midway_is_handled(self):
        server = FakeIpcServer(reply="drop")
        self.addCleanup(server.close)
        self.assertFalse(appsteer.steer(TID, "x", codex_home=server.home).ok)

    def test_missing_socket_and_empty_text(self):
        empty = tempfile.mkdtemp()
        self.assertFalse(appsteer.available(empty))
        self.assertFalse(appsteer.steer(TID, "x", codex_home=empty).ok)
        self.assertFalse(appsteer.steer(TID, "  ", codex_home=empty).ok)
        self.assertFalse(appsteer.steer("", "x", codex_home=empty).ok)


def codex_adapter(owner, live=None):
    ad = CodexAdapter.__new__(CodexAdapter)
    ad.home = "/tmp/sn-codex-home"
    ad._live = {TID: live} if live is not None else {}
    ad._live_guard = threading.Lock()
    ad._state = lambda ids: {TID: (owner, (1,), "running")} if owner != "none" else {}
    return ad


class CodexSteerRoutingTest(unittest.TestCase):
    def test_bridge_turn_still_uses_stdin_path(self):
        live = SimpleNamespace(steer=lambda text: True, steered=1)
        with mock.patch.object(appsteer, "steer") as app_path:
            self.assertTrue(codex_adapter("bridge", live).steer(TID, "改方向").ok)
            app_path.assert_not_called()

    def test_app_turn_goes_through_app_channel(self):
        with mock.patch.object(appsteer, "steer",
                               return_value=appsteer.SteerOutcome(True, "", "turn-9")) as app_path:
            got = codex_adapter("app").steer(TID, "改方向")
        self.assertTrue(got.ok)
        self.assertTrue(got.delivered)
        self.assertEqual(app_path.call_args[0][:2], (TID, "改方向"))

    def test_app_channel_failure_falls_back(self):
        with mock.patch.object(appsteer, "steer",
                               return_value=appsteer.SteerOutcome(False, "no-client-found")):
            got = codex_adapter("app").steer(TID, "改方向")
        self.assertFalse(got.ok)

    def test_terminal_owned_codex_not_app_channel(self):
        with mock.patch.object(appsteer, "steer") as app_path:
            self.assertFalse(codex_adapter("terminal").steer(TID, "改方向").ok)
            app_path.assert_not_called()


def claude_adapter(relay):
    ad = ClaudeAdapter.__new__(ClaudeAdapter)
    ad._live = {}
    ad._live_guard = threading.Lock()
    ad._relay = lambda: relay
    return ad


class ClaudeSteerViaRelayTest(unittest.TestCase):
    def test_terminal_session_is_steered_through_courier(self):
        relay = SimpleNamespace(
            check=lambda sid: SimpleNamespace(ok=True, why=""),
            deliver=mock.Mock(return_value=ActionResult(True, "已送进", SID, delivered=True)))
        got = claude_adapter(relay).steer(SID, "改方向")
        self.assertTrue(got.ok)
        self.assertEqual(got.text, STEER_OK)
        self.assertEqual(relay.deliver.call_args[0][:2], (SID, "改方向"))

    def test_not_relayable_falls_back(self):
        relay = SimpleNamespace(check=lambda sid: SimpleNamespace(ok=False, why="没开着"),
                                deliver=mock.Mock())
        got = claude_adapter(relay).steer(SID, "改方向")
        self.assertFalse(got.ok)
        self.assertEqual(got.text, WHY_STEER_NOT_RUNNING)
        relay.deliver.assert_not_called()

    def test_not_delivered_falls_back(self):
        relay = SimpleNamespace(check=lambda sid: SimpleNamespace(ok=True, why=""),
                                deliver=lambda sid, text: ActionResult(False, "没确认", sid))
        self.assertFalse(claude_adapter(relay).steer(SID, "改方向").ok)

    def test_relay_exception_falls_back(self):
        def boom(sid, text):
            raise RuntimeError("x")

        relay = SimpleNamespace(check=lambda sid: SimpleNamespace(ok=True, why=""), deliver=boom)
        self.assertFalse(claude_adapter(relay).steer(SID, "改方向").ok)

    def test_bridge_turn_still_preferred(self):
        relay = SimpleNamespace(check=lambda sid: SimpleNamespace(ok=True, why=""), deliver=mock.Mock())
        ad = claude_adapter(relay)
        ad._live = {SID: SimpleNamespace(write=lambda text: True, written=2, approval_dir="")}
        self.assertTrue(ad.steer(SID, "改方向").ok)
        relay.deliver.assert_not_called()


if __name__ == "__main__":
    unittest.main()
