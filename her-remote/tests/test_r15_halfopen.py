"""第 15 轮：连接已经死了，`connection_alive()` 还说活着 ⇒ 看门狗这把保险是空的。

## 实测（本机 websockets 15.0.1）

    >>> from websockets.asyncio.client import ClientConnection
    >>> hasattr(ClientConnection, "closed")
    False
    >>> [a for a in dir(ClientConnection) if "clos" in a or a == "state"]
    ['close', 'close_code', 'close_reason', 'state', 'wait_closed']

`app.py` 原来的写法是「取 `closed`，取不到就当活着」：

    closed = getattr(conn, "closed", None)
    if closed is None:
        return True          # ← 这一行在 15.0.1 上是**永真**

于是 `should_self_exit()` 第一行 `if self.connection_alive(): return False` 永远成立，
看门狗（lark-oapi #169 静默卡死的唯一兜底）从来没有真正生效过。
R1 用真连接验过：关闭之后它仍返回 True，`state` 是 `State.CLOSED`(3)。

## 这一轮改成

按 `state` 判（`State.OPEN == 1`）；两样都没有再退回「有对象就算活着」。
🔴 判据要同时认 enum 和 int：SDK/websockets 换版本时 `state` 可能是裸 int。
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import _fake_bridge  # noqa: E402


class _Conn:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _b(tmp, conn):
    b = _fake_bridge(tmp)
    b._ws_client = types.SimpleNamespace(_conn=conn)
    return b


def test_closed_state_is_not_alive():
    """websockets 15.0.1 的真实形状：没有 .closed，只有 state。"""
    from websockets.protocol import State

    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp, _Conn(state=State.CLOSED))
        assert b.connection_alive() is False


def test_open_state_is_alive():
    from websockets.protocol import State

    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp, _Conn(state=State.OPEN))
        assert b.connection_alive() is True


def test_closing_state_is_not_alive():
    from websockets.protocol import State

    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp, _Conn(state=State.CLOSING))
        assert b.connection_alive() is False


def test_plain_int_state_also_works():
    """换版本后 state 可能是裸 int（1 = OPEN）。"""
    with tempfile.TemporaryDirectory() as tmp:
        assert _b(tmp, _Conn(state=1)).connection_alive() is True
        assert _b(tmp, _Conn(state=3)).connection_alive() is False


def test_old_closed_attribute_still_honoured():
    """老版本 websockets 有 .closed ⇒ 照旧认它（别把老环境弄坏）。"""
    with tempfile.TemporaryDirectory() as tmp:
        assert _b(tmp, _Conn(closed=True)).connection_alive() is False
        assert _b(tmp, _Conn(closed=False)).connection_alive() is True


def test_watchdog_can_actually_fire_on_a_dead_connection():
    """整条链：连接死了 + 静默超阈值 ⇒ 必须判「该退出等重启」。

    这是这个文件存在的理由 —— 原来第一行就 return False，整条链走不到。
    """
    import time

    from websockets.protocol import State

    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp, _Conn(state=State.CLOSED))
        b._last_event_at = time.monotonic() - 40 * 60
        b._last_event_wall = time.time() - 40 * 60
        assert b.should_self_exit() is True


def test_live_connection_never_self_exits():
    """阳性对照：连着的时候不许自杀（哪怕用户一天没说话）。"""
    import time

    from websockets.protocol import State

    with tempfile.TemporaryDirectory() as tmp:
        b = _b(tmp, _Conn(state=State.OPEN))
        b._last_event_at = time.monotonic() - 10 * 3600
        b._last_event_wall = time.time() - 10 * 3600
        assert b.should_self_exit() is False
