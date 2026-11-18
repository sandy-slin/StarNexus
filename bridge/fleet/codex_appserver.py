"""Codex 接着聊的一轮走 `codex app-server`（stdio JSON-RPC），这样一轮在跑时能中途插话（1.9.0）。

为什么：`codex exec resume` 是一次性进程，跑起来以后外面插不进话。app-server 的 `turn/steer`
能把一句话插进**正在跑的同一轮**（docs/product/steering.md §2，2026-11-18 实测）。

生命周期和 Claude 一样**按轮开关**：一轮 = 起一个 app-server → `thread/resume` → `turn/start`
→ 等 `turn/completed` → 关掉。不做常驻，崩了也不用重连：下一轮重新起、重新 resume。

2026-11-18 实测（codex-cli 0.167.0）：
- 顶层 `--dangerously-bypass-hook-trust` 对 app-server **不生效**（钩子登记了但是 untrusted，不跑）；
  `-c hooks.PreToolUse=…` 加上 `thread/resume|start` 的 `config.bypass_hook_trust=true` 才会跑，
  审批钩子照样能拦命令。
- `thread/resume` 续接 `codex exec` 开的会话可以，`threads.source` 不变。
- `turn/steer` 的 `expectedTurnId` 不是正在跑的那一轮就报错 ⇒ 正好用来判断“这一轮已经结束”。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("feishu-bridge.fleet.codex")

RPC_TIMEOUT_SEC = 60.0
STEER_RPC_TIMEOUT_SEC = 15.0
INTERRUPT_GRACE_SEC = 15.0
CLIENT_INFO = {"name": "starnexus", "title": "StarNexus", "version": "1.9.0"}


class RpcError(Exception):
    pass


class AppServer:
    """一个 app-server 子进程的 JSON-RPC 客户端。请求可以从任何线程发；通知进 `events`。"""

    def __init__(self, proc: Any) -> None:
        self.proc = proc
        self.events: "queue.Queue[Optional[dict]]" = queue.Queue()
        self._wlock = threading.Lock()
        self._next = 0
        self._pending: dict = {}
        self._plock = threading.Lock()
        threading.Thread(target=self._read, daemon=True, name="codex-app-server-out").start()
        threading.Thread(target=self._drain_err, daemon=True, name="codex-app-server-err").start()

    def _drain_err(self) -> None:
        try:
            for _line in self.proc.stderr:
                pass
        except (OSError, ValueError):
            pass

    def _read(self) -> None:
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    self._server_request(msg)
                elif "id" in msg and ("result" in msg or "error" in msg):
                    with self._plock:
                        box = self._pending.pop(msg.get("id"), None)
                    if box is not None:
                        box["msg"] = msg
                        box["done"].set()
                else:
                    self.events.put(msg)
        except (OSError, ValueError):
            pass
        finally:
            with self._plock:
                boxes = list(self._pending.values())
                self._pending.clear()
            for box in boxes:
                box["done"].set()
            self.events.put(None)

    def _server_request(self, msg: dict) -> None:
        """approvalPolicy=never 时不该来审批请求；万一来了一律拒，别让这一轮卡住等人。"""
        method = str(msg.get("method") or "")
        logger.warning("Codex app-server 发来请求 %s，按拒绝处理", method)
        if "requestApproval" in method or method in ("applyPatchApproval", "execCommandApproval"):
            self._write({"id": msg["id"], "result": {"decision": "decline" if "requestApproval" in method
                                                      else "denied"}})
        else:
            self._write({"id": msg["id"], "error": {"code": -32601, "message": "StarNexus 不处理这个请求"}})

    def _write(self, obj: dict) -> bool:
        try:
            with self._wlock:
                self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self.proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        self._write({"method": method, **({"params": params} if params is not None else {})})

    def request(self, method: str, params: dict, timeout: float = RPC_TIMEOUT_SEC) -> dict:
        box = {"done": threading.Event(), "msg": None}
        with self._plock:
            self._next += 1
            rid = self._next
            self._pending[rid] = box
        if not self._write({"id": rid, "method": method, "params": params}):
            with self._plock:
                self._pending.pop(rid, None)
            raise RpcError("Codex 进程已经退出")
        if not box["done"].wait(timeout):
            with self._plock:
                self._pending.pop(rid, None)
            raise RpcError(f"{method} 超时")
        msg = box["msg"]
        if msg is None:
            raise RpcError("Codex 进程已经退出")
        if "error" in msg:
            err = msg.get("error") or {}
            raise RpcError(str(err.get("message") if isinstance(err, dict) else err))
        return msg.get("result") or {}

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except (OSError, ValueError, AttributeError):
            pass


def _text(text: str) -> dict:
    return {"type": "text", "text": text, "text_elements": []}


class LiveTurn:
    """桥自己起的、正在跑的那一轮。`steer()` / `interrupt()` 可以从别的线程调。"""

    def __init__(self, server: AppServer, thread_id: str) -> None:
        self.server = server
        self.thread_id = thread_id
        self.turn_id = ""
        self.done = threading.Event()
        self.steered = 0
        self.interrupted = False

    def open(self) -> bool:
        return bool(self.turn_id) and not self.done.is_set()

    def steer(self, text: str) -> bool:
        if not self.open():
            return False
        try:
            res = self.server.request("turn/steer", {"threadId": self.thread_id, "expectedTurnId": self.turn_id,
                                                     "input": [_text(text)]}, timeout=STEER_RPC_TIMEOUT_SEC)
        except RpcError as exc:
            logger.info("Codex 插话没插进 会话=%s 原因=%s", self.thread_id[:8], str(exc)[:120])
            return False
        ok = str(res.get("turnId") or "") == self.turn_id
        if ok:
            self.steered += 1
        return ok

    def interrupt(self) -> bool:
        if not self.open():
            return False
        try:
            self.server.request("turn/interrupt", {"threadId": self.thread_id, "turnId": self.turn_id},
                                timeout=STEER_RPC_TIMEOUT_SEC)
        except RpcError as exc:
            logger.info("Codex 停这一轮失败 会话=%s 原因=%s", self.thread_id[:8], str(exc)[:120])
            return False
        self.interrupted = True
        return True


class TurnOutcome:
    def __init__(self, reply: str = "", failure: str = "", status: str = "", timed_out: bool = False,
                 interrupted: bool = False) -> None:
        self.reply = reply
        self.failure = failure
        self.status = status
        self.timed_out = timed_out
        self.interrupted = interrupted


def run_turn(proc: Any, thread_id: str, text: str, *, cwd: str, sandbox: str = "", bypass_hook_trust: bool = False,
             model: str = "", effort: str = "", timeout: float = 600,
             on_live: Optional[Callable[[LiveTurn], None]] = None) -> TurnOutcome:
    """在已起好的 app-server 进程上续接 `thread_id` 跑一轮，等到这一轮结束。不抛异常。"""
    server = AppServer(proc)
    live = LiveTurn(server, thread_id)
    try:
        server.request("initialize", {"clientInfo": CLIENT_INFO})
        server.notify("initialized")
        params: dict = {"threadId": thread_id, "cwd": cwd, "approvalPolicy": "never"}
        if sandbox:
            params["sandbox"] = sandbox
        if bypass_hook_trust:
            params["config"] = {"bypass_hook_trust": True}
        server.request("thread/resume", params)
        turn_params: dict = {"threadId": thread_id, "input": [_text(text)]}
        if model:
            turn_params["model"] = model
        if effort:
            turn_params["effort"] = effort
        res = server.request("turn/start", turn_params)
        live.turn_id = str((res.get("turn") or {}).get("id") or "")
        if not live.turn_id:
            return TurnOutcome(failure="Codex 没开始这一轮")
        if on_live is not None:
            try:
                on_live(live)
            except Exception:  # noqa: BLE001
                logger.exception("登记 Codex 这一轮失败")
        return _collect(server, live, timeout)
    except RpcError as exc:
        return TurnOutcome(failure=str(exc))
    finally:
        live.done.set()
        server.close()


def _collect(server: AppServer, live: LiveTurn, timeout: float) -> TurnOutcome:
    deadline = time.time() + max(1.0, float(timeout))
    reply, failure, timed_out, grace = "", "", False, False
    while True:
        now = time.time()
        if now >= deadline:
            if grace:
                live.done.set()
                return TurnOutcome(reply, failure, "", True, live.interrupted)
            timed_out, grace = True, True
            live.interrupt()
            deadline = now + INTERRUPT_GRACE_SEC
            continue
        try:
            msg = server.events.get(timeout=min(1.0, max(0.05, deadline - now)))
        except queue.Empty:
            continue
        if msg is None:
            live.done.set()
            return TurnOutcome(reply, failure or "Codex 进程中途退出了", "", timed_out, live.interrupted)
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "item/completed":
            item = params.get("item") or {}
            if item.get("type") == "agentMessage" and str(item.get("text") or "").strip():
                reply = str(item["text"]).strip()
        elif method == "error" and not params.get("willRetry"):
            err = params.get("error") or {}
            failure = str(err.get("message") if isinstance(err, dict) else err or "出错了")
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if str(turn.get("id") or "") != live.turn_id:
                continue
            live.done.set()
            status = str(turn.get("status") or "")
            if status == "failed" and not failure:
                err = turn.get("error") or {}
                failure = str((err.get("message") if isinstance(err, dict) else err) or "回合失败")
            return TurnOutcome(reply, failure, status, timed_out, live.interrupted or status == "interrupted")


def reap(proc: Any) -> None:
    """关 stdin 让它自己退；不退再 terminate / kill。"""
    for step in ("wait", "terminate", "kill"):
        try:
            if step == "terminate":
                proc.terminate()
            elif step == "kill":
                proc.kill()
            proc.wait(timeout=10 if step == "wait" else 5)
            return
        except Exception:  # noqa: BLE001
            continue
