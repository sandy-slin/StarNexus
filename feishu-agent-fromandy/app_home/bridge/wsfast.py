"""把飞书长连接的重连窗口从 18~21 分钟压到半分钟内。

## 为什么要有这个文件

主人这台的真实日志，断线/首连失败之后到重新连上：**634 秒 / 1155 秒 / 1291 秒**。
这期间点卡片按钮，飞书直接回「目标回调服务当前未在线」；发消息没有任何反应。
2026-11-06 领导那句「什么消息都没有反应」，撞的多半就是这个窗口。

## 节奏是怎么来的（实测，不是读码推断）

`lark.ws.Client` 的重连：首次重连前随机等 `0~_reconnect_nonce`（默认 **30**）秒，
之后每 `_reconnect_interval`（默认 **120**）秒一次，`_reconnect_count=-1` 无限重试。
最坏窗口 30s → 150s → 270s …

- **构造参数传不进去**：`Client.__init__` 的签名里没有这几项（SDK 注释写明故意不暴露）。
- **只在构造后改实例属性不够**：服务端下发配置时 `_configure()` 会把它们**盖回 30/120**
  （本机实测）。所以必须连 `_configure` 一起覆盖。
- 不 fork SDK、不改 `site-packages` —— 那两样升级一次就没了，而且别人看不出来。

## 红线

- 🔴 `_reconnect_count` 保持 **-1**（无限重试）。夹成有限次 = 断够几次就彻底不连了，比慢更糟。
- 🔴 只夹**上限**：服务端给的值比我们还小就用它的，不强行放大。
- 🔴 SDK 换版本、属性改名 ⇒ 照原样返回，不抛异常。宁可慢，不能起不来。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("feishu-bridge.wsfast")

# 首次重连前的随机抖动上限（秒）。留一点点抖动是对的：所有客户端同时重连会打在同一秒。
MAX_NONCE = 3
# 之后每次重试的间隔上限（秒）。最坏窗口 = MAX_NONCE + MAX_INTERVAL * 2 ≈ 33 秒。
MAX_INTERVAL = 15


def _clamp(client: Any) -> Any:
    """把这个客户端的重连节奏夹小。认不出就原样返回（见文件头红线）。"""
    try:
        nonce = getattr(client, "_reconnect_nonce", None)
        interval = getattr(client, "_reconnect_interval", None)
        if nonce is not None:
            client._reconnect_nonce = min(int(nonce), MAX_NONCE)
        if interval is not None:
            client._reconnect_interval = min(int(interval), MAX_INTERVAL)
    except Exception:  # noqa: BLE001 —— 慢一点可以，起不来不行
        logger.info("这个版本的飞书 SDK 认不出重连节奏，断线后恢复会慢一些")
    return client


def make_client(app_id: str, app_secret: str, **kwargs: Any) -> Any:
    """造一个重连快的 `lark.ws.Client`。

    用法和 `lark.ws.Client(app_id, app_secret, event_handler=…, log_level=…)` 一样。
    造不出子类（SDK 结构变了）就退回原版客户端 —— 功能照旧，只是恢复慢。
    """
    import lark_oapi as lark

    base = lark.ws.Client

    try:
        class _FastReconnect(base):  # type: ignore[misc,valid-type]
            """覆盖 `_configure`：服务端每次下发配置之后都重新夹一遍（见文件头）。"""

            def _configure(self, conf: Any) -> None:  # noqa: D102
                super()._configure(conf)
                _clamp(self)

        client = _FastReconnect(app_id, app_secret, **kwargs)
    except Exception:  # noqa: BLE001
        logger.info("造快速重连客户端失败，用 SDK 原版（断线后恢复会慢一些）")
        return base(app_id, app_secret, **kwargs)

    # 构造时 `_configure` 还没被调用过 ⇒ 这里先夹一次（实测构造完仍是 30/120）
    return _clamp(client)
