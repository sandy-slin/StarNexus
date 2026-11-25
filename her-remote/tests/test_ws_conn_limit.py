"""长连接**被飞书明确拒绝**时，要能说出原因，而不是静悄悄地不理人。

## 这组测试在守什么

2026-11-05 实测的两件事，合起来是一个真实故障：

1. **一个应用最多 50 条长连接。** 拿现有应用一条条连，前 49 条全成功，
   第 50 条起 `POST /callback/ws/endpoint` 回
   `code=1000040350 msg=the number of connections exceeded the limit`
   （加上当时在线的真桥正好 50）。
2. **SDK 对这种拒绝不重连。** `lark_oapi/ws/client.py:187-189` 里
   `ClientException` 是直接 `raise e`。它跑在 daemon 线程里，所以
   **线程死了、主进程照常活着** —— monkeypatch 复现过：
   `ws 线程还活着吗: False / 连接对象 _conn: None`。

于是用户看到的是「服务明明在跑，机器人就是不理我」，而日志里只有一行 SDK 的
`connect failed`。看门狗会在静默 15 分钟后退出等重启，但**重启治不好**
连接数到顶 —— 只会每 15 分钟重来一次，把真正的原因埋进噪音。

所以下面守两件事：这个死法必须被记下来（不能把异常吞掉成静默），
以及给出的原因必须是实测认过的码才下结论、认不出就说认不出。
"""

from __future__ import annotations

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest  # noqa: E402

from bridge import app as app_mod  # noqa: E402
from bridge.app import Bridge, explain_ws_failure  # noqa: E402


# ---------- 拒绝码 → 下一步 ----------


def test_conn_limit_code_is_the_measured_one():
    """这个常量是实打出来的，改了它就等于改了判据。"""
    assert app_mod.WS_CONN_LIMIT_CODE == 1000040350


def test_conn_limit_says_one_app_per_machine():
    """连接数到顶最可能的原因是"多台电脑共用一个应用" —— 必须把这条说出来。

    光说"连接数到顶"会让人去加连接数，而真正的下一步是"每台电脑用自己的应用"：
    集群模式下共用一个应用，消息只会随机进其中一台，加连接数只会让随机更均匀。
    """
    text = explain_ws_failure(1000040350)
    assert "50" in text
    assert "随机" in text  # 集群模式这件事不能漏
    assert "自己的应用" in text


def test_unknown_code_does_not_invent_a_reason():
    """🔴 认不出就说认不出，并把码带上。

    编一个原因比不给原因更贵 —— 它会把排查带错方向，而对方要自己反证才能拉回来。
    """
    text = explain_ws_failure(424242)
    assert "认不出" in text
    assert "424242" in text


def test_empty_credential_code_points_at_reinstall():
    text = explain_ws_failure(app_mod.WS_NO_CREDENTIAL_CODE)
    assert "app_id" in text


# ---------- 线程边界：死法必须留痕 ----------


class _BoomClient:
    """复刻 SDK 的行为：`start()` 抛带 `code` 的 ClientException 且不重连。"""

    def __init__(self, code: int, msg: str) -> None:
        self._code = code
        self._msg = msg

    def start(self) -> None:
        exc = RuntimeError(self._msg)
        exc.code = self._code  # SDK 的 ClientException 就是这个形状
        raise exc


def _bare_bridge() -> Bridge:
    """不跑 __init__（它要起会话总控、装钩子）。只要被测的那两个属性。"""
    bridge = Bridge.__new__(Bridge)
    bridge._ws_fatal = ""
    bridge._ws_client = None
    return bridge


def test_ws_thread_records_the_refusal(caplog):
    bridge = _bare_bridge()
    with caplog.at_level("ERROR"):
        bridge._ws_thread(_BoomClient(1000040350, "the number of connections exceeded the limit"))
    # 1) 原话留痕，诊断能区分"被拒"和"正在重连"
    assert "1000040350" in bridge._ws_fatal
    # 2) 日志里有"下一步"，不只有 SDK 那句英文
    assert "随机" in caplog.text


def test_ws_thread_never_lets_the_exception_escape():
    """🔴 线程边界上什么都不能往外抛。

    抛了会在 daemon 线程里打印 traceback 然后什么也不做，正是原来的行为。
    """
    bridge = _bare_bridge()

    class _Nasty:
        def start(self):
            raise KeyboardInterrupt("even this")

    bridge._ws_thread(_Nasty())  # 不抛就算过
    assert bridge._ws_fatal


def test_fresh_bridge_has_no_fatal_recorded():
    """没被拒过的时候这个字段必须是空的 —— 否则诊断会把健康的桥报成坏的。"""
    bridge = _bare_bridge()
    assert bridge._ws_fatal == ""
