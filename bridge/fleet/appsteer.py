"""插进 Codex App 正在跑的那一轮（1.9.9）。

## 为什么要有这个模块

`codex queue` 只能排到**下一轮**：App 这一轮做完才被取走。用户原话（11-20）：
「群里发出的每一句话都能加入到 steering 中，不能等上一个任务做完才处理 …… 很多时候任务描述
并不完整，需要后面持续来修正，如果等上一句话的任务做完，反而不是我希望的结果。」
等一轮做完再处理，纠偏就失去意义了。

## 走哪条路

Codex App（ChatGPT.app）各个窗口之间本来就用一个本机 Unix socket 互相转发请求，
其中就有 `thread-follower-steer-turn` —— App 自己插话用的就是它。我们以一个普通客户端的身份
连上去，发同一个请求，App 那边由**拥有这个会话的那个窗口**处理，等于替用户按了一次「插话」。

- socket：`$CODEX_HOME/ipc/ipc.sock`（只认当前用户自己的，chmod 600）
- 帧格式：4 字节小端长度 + JSON
- 先 `initialize` 领一个 clientId，再发 `thread-follower-steer-turn`
- 没有窗口认领这个会话 ⇒ 回 `no-client-found`；这一轮已经结束 ⇒ 回
  `Cannot steer conversation … because its active turn already ended`。两种都退回排队，不丢话。

2026-11-20 本机实测：插进正在跑的那一轮，目标会话当场看到新消息（rollout 里出现我们这句的
user 回显，turnId 还是原来那一轮）。

## 边界

- **只发插话这一个请求**。不碰启动、打断、改设置、改权限。
- 请求超时 15 秒，连不上 / 超时 / 任何异常都安静退回，调用方照旧排队。
- <span>这是 App 内部协议，App 升级可能变</span>⇒ 失败一律当「不支持」处理，并记进日志。
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import uuid
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("feishu-bridge.fleet.appsteer")

CLIENT_TYPE = "CODEX_UNKNOWN"
STEER_METHOD = "thread-follower-steer-turn"
STEER_VERSION = 1
CONNECT_TIMEOUT_SEC = 3.0
REQUEST_TIMEOUT_SEC = 15.0
MAX_FRAME_BYTES = 8 * 1024 * 1024

# App 回的两种「这条路不通」：没人认领这个会话、这一轮已经结束。都退回排队。
WHY_NO_OWNER = "no-client-found"
WHY_TURN_ENDED = "active turn already ended"


@dataclass(frozen=True)
class SteerOutcome:
    """`ok=True` 才是插进去了。`retryable=False` 的意思是「这条路对这个会话不通」。"""

    ok: bool
    why: str = ""
    turn_id: str = ""


def socket_path(codex_home: str = "") -> str:
    home = str(codex_home or os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
    return os.path.join(home, "ipc", "ipc.sock")


def available(codex_home: str = "") -> bool:
    path = socket_path(codex_home)
    try:
        return os.path.exists(path) and os.stat(path).st_uid == os.getuid()
    except OSError:
        return False


def _frame(obj: dict) -> bytes:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return struct.pack("<I", len(body)) + body


def _read_exact(sock: socket.socket, want: int) -> bytes:
    buf = b""
    while len(buf) < want:
        chunk = sock.recv(want - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def _read_frame(sock: socket.socket) -> Optional[dict]:
    head = _read_exact(sock, 4)
    if len(head) < 4:
        return None
    size = struct.unpack("<I", head)[0]
    if size <= 0 or size > MAX_FRAME_BYTES:
        return None
    body = _read_exact(sock, size)
    if not body:
        return None
    try:
        out = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _await_response(sock: socket.socket, request_id: str) -> Optional[dict]:
    """等自己这条请求的回执。中间的广播（别的窗口状态变化）一律跳过。"""
    while True:
        msg = _read_frame(sock)
        if msg is None:
            return None
        if msg.get("type") == "response" and msg.get("requestId") == request_id:
            return msg


def steer(thread_id: str, text: str, *, codex_home: str = "",
          timeout: float = REQUEST_TIMEOUT_SEC) -> SteerOutcome:
    """把一句话插进 Codex App 正在跑的那一轮。插不进 ⇒ `ok=False`，调用方退回排队。"""
    thread_id = str(thread_id or "").strip().lower()
    text = str(text or "").strip()
    if not thread_id or not text:
        return SteerOutcome(False, "要发的内容是空的。")
    path = socket_path(codex_home)
    if not available(codex_home):
        return SteerOutcome(False, "电脑上的 Codex App 没在跑。")
    sock = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(CONNECT_TIMEOUT_SEC)
        sock.connect(path)
        sock.settimeout(max(1.0, float(timeout or REQUEST_TIMEOUT_SEC)))
        hello_id = str(uuid.uuid4())
        sock.sendall(_frame({"type": "request", "requestId": hello_id,
                             "sourceClientId": "initializing-client", "version": 0,
                             "method": "initialize", "params": {"clientType": CLIENT_TYPE}}))
        hello = _await_response(sock, hello_id)
        client_id = str(((hello or {}).get("result") or {}).get("clientId") or "")
        if not client_id:
            return SteerOutcome(False, "连上了 Codex App 但没领到身份。")
        request_id = str(uuid.uuid4())
        sock.sendall(_frame({
            "type": "request", "requestId": request_id, "sourceClientId": client_id,
            "version": STEER_VERSION, "method": STEER_METHOD,
            "timeoutMs": int(max(1.0, float(timeout or REQUEST_TIMEOUT_SEC)) * 1000),
            "params": {"conversationId": thread_id,
                       "input": [{"type": "text", "text": text, "text_elements": []}],
                       "restoreMessage": {"context": {}},
                       "attachments": [], "clientUserMessageId": None},
        }))
        got = _await_response(sock, request_id)
        if got is None:
            return SteerOutcome(False, "Codex App 没给回执。")
        if got.get("resultType") != "success":
            why = str(got.get("error") or "")
            logger.info("App 插话 没插进 会话=%s 原因=%s", thread_id[:8], why[:120])
            return SteerOutcome(False, why)
        turn = str((((got.get("result") or {}).get("result")) or {}).get("turnId") or "")
        logger.info("App 插话 插进 会话=%s 字数=%d 这一轮=%s", thread_id[:8], len(text), turn[:8])
        return SteerOutcome(True, "", turn)
    except (OSError, socket.timeout) as exc:
        logger.info("App 插话 连不上 会话=%s %s", thread_id[:8], type(exc).__name__)
        return SteerOutcome(False, "连不上电脑上的 Codex App。")
    except Exception:  # noqa: BLE001 —— 私有协议，任何意外都当「这条路不通」
        logger.exception("App 插话 出错 会话=%s", thread_id[:8])
        return SteerOutcome(False, "插话没成功。")
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
