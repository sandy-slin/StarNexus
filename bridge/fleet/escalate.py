"""需要人介入的群推送：@ 本人，10 分钟没处理再加急一次（产品定义 §5.8，验收 FC-08）。

- 推「在等你 / 出错」时消息里 @ 主人（`at_owner_line`）。
- 「在等你」那条记下 message_id；之后每拍看：同一件事（会话 + 问题指纹）还在等、
  已经过了 `delay` 秒、还没加急过 ⇒ 调加急一次。同一件事绝不加急第二次。
- 会话不再等了，或者问的换成了别的事 ⇒ 这一件作废（换的那件会重新推、重新计时）。

这里只做判断；发消息和调加急接口由 app 注入。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

URGENT_DELAY_SEC = 10 * 60


def at_owner_line(owner_ids: Iterable[str]) -> str:
    """post 富文本 md 里的 @：`<at user_id="ou_xxx"></at>`。只认 ou_ 开头的 id。"""
    ids = sorted({str(i) for i in owner_ids or () if str(i).startswith("ou_")})
    return " ".join(f'<at user_id="{i}"></at>' for i in ids)


@dataclass
class Pending:
    chat_id: str
    message_id: str
    sig: str
    at: float
    urged: bool = False


class Escalator:
    def __init__(self, *, delay: float = URGENT_DELAY_SEC, now: Callable[[], float] = time.time) -> None:
        self.delay = delay
        self._now = now
        self._lock = threading.Lock()
        self._items: dict = {}

    def track(self, key: tuple, chat_id: str, message_id: str, sig: str) -> None:
        if not message_id:
            return
        with self._lock:
            old = self._items.get(key)
            if old is not None and old.sig == sig:
                return
            self._items[key] = Pending(chat_id, message_id, sig, self._now())

    def due(self, waiting: dict) -> list:
        """`waiting` = {key: 现在的问题指纹}（此刻还在等的会话）。返回该加急的 Pending。"""
        out = []
        now = self._now()
        with self._lock:
            for key in list(self._items):
                item = self._items[key]
                sig = waiting.get(key)
                if sig is None or sig != item.sig:
                    self._items.pop(key, None)
                    continue
                if not item.urged and now - item.at >= self.delay:
                    item.urged = True
                    out.append(item)
        return out

    def pending(self) -> dict:
        with self._lock:
            return dict(self._items)
