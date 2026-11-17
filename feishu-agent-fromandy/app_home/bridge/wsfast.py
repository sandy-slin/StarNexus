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

import asyncio
import logging
import sys
import time
from typing import Any, Callable

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


# ---------- 第 34 轮：比「多久没业务事件」更准的两个活性信号 ----------
#
# 看门狗原来只有「连接对象在不在 + 多久没收到消息」。用户刚聊完就撞上 lark-oapi #169
# （重连路径 `_get_conn_url()` 里的 `requests.post` 没超时）⇒ 要等满 15 分钟。
# 读本机 lark-oapi 1.7.5 源码（`lark_oapi/ws/client.py`）得到两条更直接的证据：
#
# 1. **SDK 的事件循环是不是卡死了**：`requests.post` 是同步调用，跑在 SDK 自己的 asyncio
#    循环上（模块级 `loop`，`start()` 里 `run_until_complete`）。卡住它 = 整个循环停摆：
#    ping 发不出、pong 收不到、收消息也停。从看门狗线程往那个循环投一个空回调
#    （`call_soon_threadsafe`），多久没被执行，就是它卡了多久 —— **不依赖飞书服务端任何行为**。
# 2. **飞书的 pong 还来不来**：SDK 每 `_ping_interval` 秒发一个 ping（默认 120，
#    2026-11-10 实测服务端下发 `PingInterval=90`），服务端回 pong（payload 里是 ClientConfig）。
#    SDK 只 `logger.debug("receive pong")`，**不记时间** ⇒ 这里在子类里记下来。
#    🔴 只有在**当前这条连接**上真收到过 pong 才启用（服务端不回 pong 时这条信号直接不参与，
#    退回原来的判据）—— 服务端到底回不回 pong，本机 INFO 日志里看不到，没实测。
#
# 两条都按 `time.monotonic()` 计时：Mac 睡眠期间它不走 ⇒ 睡一夜醒来不会被算成「卡了 8 小时」。

# 当前连接上次收到 pong 之后，错过几个 ping 周期才算死（再加一点余量）
PONG_MISSED = 2
PONG_SLACK_SEC = 30
# 阈值下限：服务端把 ping 间隔调得很小时，也不至于网络抖一下就判死
PONG_MIN_SEC = 120

_first_pong_logged = False


def _note_pong(client: Any, frame: Any) -> None:
    """控制帧里是 pong ⇒ 记下时间和它属于哪条连接。任何异常都吞掉（这是 SDK 的收包路径）。"""
    global _first_pong_logged
    try:
        for header in frame.headers:
            if header.key == "type" and header.value == "pong":
                client._r34_pong_at = time.monotonic()
                client._r34_pong_conn = getattr(client, "_conn", None)
                if not _first_pong_logged:
                    _first_pong_logged = True
                    logger.info("收到飞书心跳回包（每 %s 秒一次），卡死检测已启用",
                                getattr(client, "_ping_interval", "?"))
                return
    except Exception:  # noqa: BLE001
        pass


def pong_limit(client: Any) -> float:
    """当前连接多少秒没收到 pong 就算死：错过 `PONG_MISSED` 个 ping + 余量，不低于下限。"""
    try:
        interval = float(getattr(client, "_ping_interval", 120) or 120)
    except (TypeError, ValueError):
        interval = 120.0
    return max(PONG_MIN_SEC, PONG_MISSED * interval + PONG_SLACK_SEC)


def pong_age(client: Any, now: float | None = None) -> float | None:
    """**当前这条连接**上次收到 pong 到现在多少秒（monotonic）。

    没收到过、或者收到的是上一条连接的 ⇒ None（不知道 ≠ 死了，交给别的判据）。
    """
    conn = getattr(client, "_conn", None)
    at = getattr(client, "_r34_pong_at", None)
    if conn is None or at is None or getattr(client, "_r34_pong_conn", None) is not conn:
        return None
    now = time.monotonic() if now is None else now
    return max(0.0, now - at)


def sdk_loop() -> Any:
    """lark-oapi 收发长连接用的那个事件循环（`lark_oapi/ws/client.py` 模块级的 `loop`）。"""
    mod = sys.modules.get("lark_oapi.ws.client")
    return getattr(mod, "loop", None)


class LoopProbe:
    """从别的线程探「SDK 的事件循环还转不转」。

    每次 `tick()`：没有在途的探针就投一个空回调进去；有在途的就返回它等了多久。
    循环没在跑（长连接线程已经退出）/ 拿不到 ⇒ None：不知道，交给静默兜底。
    """

    def __init__(self, loop_getter: Callable[[], Any] = sdk_loop,
                 clock: Callable[[], float] = time.monotonic) -> None:
        import threading

        self._get_loop = loop_getter
        self._clock = clock
        self._lock = threading.Lock()
        self._sent_at: float | None = None
        self._token = 0

    def reset(self) -> None:
        """丢掉在途的探针（刚睡醒时用：睡前投的那个没被执行，不代表循环卡了）。"""
        with self._lock:
            self._token += 1
            self._sent_at = None

    def _ack(self, token: int) -> None:
        with self._lock:
            if token == self._token:
                self._sent_at = None

    def tick(self) -> float | None:
        loop = self._get_loop()
        try:
            running = (loop is not None and isinstance(loop, asyncio.AbstractEventLoop)
                       and not loop.is_closed() and loop.is_running())
        except Exception:  # noqa: BLE001
            running = False
        now = self._clock()
        with self._lock:
            if not running:
                self._sent_at = None
                return None
            if self._sent_at is not None:
                return max(0.0, now - self._sent_at)
            self._token += 1
            token = self._token
            self._sent_at = now
        try:
            loop.call_soon_threadsafe(self._ack, token)
        except Exception:  # noqa: BLE001 —— 循环刚好关了
            with self._lock:
                self._sent_at = None
            return None
        return 0.0


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

            async def _handle_control_frame(self, frame: Any) -> None:  # noqa: D102
                # 第 34 轮：记下「哪条连接、什么时候」收到过飞书的 pong（见 `pong_age`）
                _note_pong(self, frame)
                await super()._handle_control_frame(frame)

        client = _FastReconnect(app_id, app_secret, **kwargs)
    except Exception:  # noqa: BLE001
        logger.info("造快速重连客户端失败，用 SDK 原版（断线后恢复会慢一些）")
        return base(app_id, app_secret, **kwargs)

    # 构造时 `_configure` 还没被调用过 ⇒ 这里先夹一次（实测构造完仍是 30/120）
    return _clamp(client)
