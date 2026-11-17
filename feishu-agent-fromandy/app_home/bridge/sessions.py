"""会话与排队。

三条都来自调研里的真实故障：

1. **同一个会话串行，不同会话并发。** 所有项目的一致做法。
2. **入队必须立刻告诉用户，带上排在第几。** lark-channel-bridge #232/#294 和
   cc-connect #1091：消息静静进队列、没有任何回音，用户以为机器人崩了。
3. **空闲超时就开新会话，别无条件 --resume。** cc-connect 默认 30 分钟，理由写得很清楚：
   旧 history（失败的命令、调试噪音）被反复重读，会开始主导模型的注意力。

另外刻意**不做**的一件事：不去比对「磁盘上的 session id」和「活着的 session id」然后
在不一致时回收。cc-connect #1470 就是这么干的，结果每条消息都杀掉正确的会话、resume
一个错的空会话，再把错的 id 写回磁盘，形成永久乒乓。这里只有一个权威：内存里这份。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    scope: str
    workspace: str
    claude_session_id: str | None = None
    last_active: float = field(default_factory=time.monotonic)
    turns: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    waiting: int = 0

    def resume_target(self, idle_reset_sec: int) -> str | None:
        """该不该接着上一轮。空闲太久就从头开，返回 None。"""
        if not self.claude_session_id:
            return None
        if time.monotonic() - self.last_active > idle_reset_sec:
            return None
        return self.claude_session_id


class SessionStore:
    def __init__(self, default_workspace: str, idle_reset_min: int) -> None:
        self._sessions: dict[str, Session] = {}
        self._default_workspace = default_workspace
        self._idle_reset_sec = idle_reset_min * 60

    @staticmethod
    def scope_for(chat_id: str, chat_type: str, sender_open_id: str) -> str:
        """一个聊天 = 一个会话。

        单聊按人、群按群。和调研里各家一致（`dm_{open_id}` / `group_{chat_id}`）。
        """
        if chat_type == "p2p":
            return f"dm:{sender_open_id}"
        return f"group:{chat_id}"

    def get(self, scope: str) -> Session:
        session = self._sessions.get(scope)
        if session is None:
            session = Session(scope=scope, workspace=self._default_workspace)
            self._sessions[scope] = session
        return session

    @property
    def idle_reset_sec(self) -> int:
        return self._idle_reset_sec

    def reset(self, scope: str) -> None:
        """开新会话：丢掉 claude session 指针，保留工作目录。"""
        session = self._sessions.get(scope)
        if session is not None:
            session.claude_session_id = None
            session.turns = 0

    def stats(self) -> dict[str, int]:
        return {"sessions": len(self._sessions)}
