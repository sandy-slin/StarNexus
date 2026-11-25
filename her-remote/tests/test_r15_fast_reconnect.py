"""第 15 轮：断线之后要等 18~21 分钟才回来 —— 压到半分钟内。

## 现场（主人这台真实日志，三次）

首连/断线之后到重新连上：**634 秒 / 1155 秒 / 1291 秒**。这期间点按钮 = 飞书回
「目标回调服务当前未在线」，发消息没反应。今天领导那边多半撞的就是这个。

## 原因（实测，不是读码推断）

`lark.ws.Client` 的重连节奏：首次重连前随机等 `0~_reconnect_nonce`（默认 30）秒，
之后每 `_reconnect_interval`（默认 120）秒一次，`_reconnect_count=-1` 无限重试。
最坏窗口 30s → 150s → 270s…

构造参数传不进去（R1 实测签名里没有，SDK 注释写明故意不暴露）；
改实例属性也会被 `_configure` 盖回 30/120（本机实测：服务端下发配置时它重置）。
**继承 + 覆盖 `_configure` 夹小**有效，多次下发不反弹。

## 这一轮

抖动 ≤3 秒、间隔 ≤15 秒 ⇒ 最坏窗口 3s / 18s / 33s。
🔴 `_reconnect_count` 保持 -1（无限重试）—— 夹成有限次会变成「断够几次就彻底不连了」。
🔴 不 fork SDK、不改 site-packages。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.wsfast import MAX_INTERVAL, MAX_NONCE, make_client


class _Conf:
    ReconnectCount = -1
    ReconnectInterval = 120
    ReconnectNonce = 30
    PingInterval = 120


def _client():
    return make_client("cli_x", "s", event_handler=None)


def test_reconnect_window_is_clamped_right_after_construction():
    c = _client()
    assert c._reconnect_nonce <= MAX_NONCE
    assert c._reconnect_interval <= MAX_INTERVAL


def test_server_pushed_config_cannot_undo_the_clamp():
    """🔴 实测过的坑：只在构造后设一次，服务端下发配置时会被盖回 30/120。"""
    c = _client()
    c._configure(_Conf())
    assert c._reconnect_nonce <= MAX_NONCE, c._reconnect_nonce
    assert c._reconnect_interval <= MAX_INTERVAL, c._reconnect_interval


def test_repeated_pushes_do_not_bounce_back():
    c = _client()
    for _ in range(3):
        c._configure(_Conf())
    assert c._reconnect_interval <= MAX_INTERVAL


def test_infinite_retry_is_preserved():
    """🔴 夹成有限次 = 断够几次就彻底不连了，比慢更糟。"""
    c = _client()
    c._configure(_Conf())
    assert c._reconnect_count == -1


def test_smaller_server_values_are_respected():
    """服务端给的比我们还小 ⇒ 用它的（只夹上限，不强行放大）。"""
    class Small(_Conf):
        ReconnectInterval = 5
        ReconnectNonce = 1

    c = _client()
    c._configure(Small())
    assert c._reconnect_interval == 5 and c._reconnect_nonce == 1


def test_worst_case_window_is_under_a_minute():
    """把这一轮的收益钉成数字：最坏「断线到第三次重试」要小于 60 秒。"""
    c = _client()
    c._configure(_Conf())
    worst = c._reconnect_nonce + c._reconnect_interval * 2
    assert worst < 60, worst


def test_bridge_uses_it():
    """调用点还在：run() 里要用 make_client，不是裸的 lark.ws.Client。"""
    import inspect

    from bridge import app as app_mod

    cls = next(v for v in vars(app_mod).values()
               if inspect.isclass(v) and hasattr(v, "_watchdog_loop"))
    src = inspect.getsource(cls.run)
    assert "make_client(" in src, "还在用裸的 SDK 客户端，重连窗口没压短"


def test_falls_back_when_sdk_shape_changes():
    """SDK 换版本、属性名变了 ⇒ 不许抛异常（宁可慢，不能起不来）。"""
    import bridge.wsfast as wsfast

    class Weird:
        def __init__(self, *a, **kw):
            pass

    got = wsfast._clamp(Weird())
    assert got is not None
