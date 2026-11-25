"""「正在跑的那一轮」落盘：桥被看门狗 / 开机 / 崩溃重启后，接着把它做完。

第 32 轮（照 T3 Code 的 RestartContinuation，借思路不抄代码）：
- 开跑时按会话记一条；拿到 claude 的会话 id 立刻补上（`runner.run_turn(on_session=…)`）。
- 这一轮**不管怎么结束**（跑完 / 失败 / 被 /stop / 停在等确认）都删掉这条。
- 进程被杀删不掉 ⇒ 下次启动时还在的，就是被重启打断、要续的那一轮。
- 续的时候 resume 同一个 claude 会话，发一句固定提示（**不重发原话**：原话可能是「删掉 X」，
  重发一遍等于让它再做一次）。
- 续出来的轮次记「我续的是第几次」；5 分钟内连着续了 3 次还没完 ⇒ 多半是这一轮本身把桥弄崩，停下。

## 规矩
- 存在 config 同目录的 `inflight.json`，0600、原子写（同 `history.py`）。
- 没有 config 路径 ⇒ 只留在内存里，**不退回去写 `~/.carher-feishu-agent`**（测试里造的桥会污染真目录）。
- 原话先脱敏再落盘，只留前 `TEXT_KEEP` 个字（只用来在飞书里提一句「上一轮『…』」）。
- 坏文件 / 写不进 ⇒ 当没有，绝不让收消息那条路出错。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Optional

logger = logging.getLogger("feishu-bridge.inflight")

INFLIGHT_FILE = "inflight.json"
TEXT_KEEP = 200            # 原话（脱敏后）最多留几个字
MAX_AGE_SEC = 30 * 60      # 起跑超过 30 分钟的不续（他早就不在等了，或者已经换了说法）
CHAIN_MAX = 3              # 连着续了几次还没完就停
CHAIN_WINDOW_SEC = 5 * 60  # 「连着」= 从这条链最初那一次算起 5 分钟内

# 续的时候发给 claude 的那句话。🔴 不是原话 —— 见文件头。
CONTINUE_PROMPT = (
    "上一轮被重启打断了（不是用户让你停的），接着把它做完，别从头重来。"
    "先看看会话里已经做到哪一步；之前改过东西的命令（写文件、装依赖、跑脚本、发请求）"
    "先确认生没生效、是不是还在跑，确认不了就直接告诉用户，别重复执行。"
)

# 飞书里说的两句
RESUMING_NOTE = "⟳ 电脑上的助手刚重启，上一轮『{head}』被打断了，我让它接着做。"
CHAIN_STOP_NOTE = "上一轮被重启打断了好几次还没做完，先停下。要继续就再说一句。"

# plan() 的结论
RESUME = "resume"
CHAIN_STOP = "chain_stop"   # 要跟他说一声的那种不续
SKIP = "skip"               # 安静地不续


def head_of(text: str, n: int = 20) -> str:
    """飞书里提一句用的原话开头。"""
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[:n] + "…"


def plan(entry: dict, now: float, *, has_pending: bool, paused: bool) -> tuple[str, str]:
    """一条残留该不该续 → (结论, 原因)。纯函数，单独测。"""
    if not entry.get("claude_session_id"):
        return SKIP, "没有会话 id（claude 还没起来就被打断了）"
    if has_pending:
        return SKIP, "这个会话有确认卡在等"
    if paused:
        return SKIP, "这个会话已暂停"
    started = float(entry.get("started_at") or 0)
    if now - started > MAX_AGE_SEC:
        return SKIP, "起跑超过 30 分钟"
    chain_started = float(entry.get("chain_started_at") or started)
    if int(entry.get("continue_of") or 0) >= CHAIN_MAX and now - chain_started < CHAIN_WINDOW_SEC:
        return CHAIN_STOP, "5 分钟内连着续了 3 次"
    return RESUME, ""


def next_chain(entry: dict, now: float) -> tuple[int, float]:
    """续出来那一轮的 (continue_of, chain_started_at)。

    链只数 5 分钟窗口里的（同 T3）：窗口外的旧链从这一次重新数，
    否则一个每 6 分钟崩一次的轮次会永远续下去、计数却一直往上涨。
    """
    started = float(entry.get("started_at") or now)
    chain_started = float(entry.get("chain_started_at") or started)
    if now - chain_started >= CHAIN_WINDOW_SEC:
        return 1, now
    return int(entry.get("continue_of") or 0) + 1, chain_started


class InflightStore:
    """一个文件、按 scope 一条「正在跑的那一轮」。path 为空 ⇒ 只在内存。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._memory: dict = {}

    def _load(self) -> dict:
        if not self.path:
            return dict(self._memory)
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        if not self.path:
            self._memory = dict(data)
            return
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             delete=False) as tmp:
                json.dump(data, tmp, ensure_ascii=False)
                name = tmp.name
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        except OSError:
            logger.warning("在跑的轮次记不下来（只影响重启后接着干）", exc_info=True)

    def start(self, scope: str, chat_id: str, workspace: str, text: str,
              claude_session_id: Optional[str] = None, continue_of: int = 0,
              chain_started_at: Optional[float] = None) -> dict:
        """开跑时写一条（同一会话的旧条被覆盖：用户新说一句 ⇒ 链清零）。"""
        from .redact import redact

        now = time.time()
        entry = {
            "scope": scope,
            "chat_id": chat_id,
            "claude_session_id": claude_session_id or "",
            "workspace": workspace,
            "text": redact(str(text or ""))[0][:TEXT_KEEP],
            "started_at": now,
            "continue_of": int(continue_of or 0),
            "chain_started_at": float(chain_started_at) if chain_started_at else now,
        }
        with self._lock:
            data = self._load()
            data[scope] = entry
            self._save(data)
        return entry

    def set_session(self, scope: str, claude_session_id: str, started_at: float) -> None:
        """拿到 claude 会话 id 就补上。只改**这一轮**那条（started_at 对得上）。"""
        if not claude_session_id:
            return
        with self._lock:
            data = self._load()
            entry = data.get(scope)
            if not isinstance(entry, dict) or entry.get("started_at") != started_at:
                return
            if entry.get("claude_session_id") == claude_session_id:
                return
            entry["claude_session_id"] = claude_session_id
            self._save(data)

    def finish(self, scope: str, started_at: Optional[float] = None) -> None:
        """这一轮结束了 ⇒ 删掉。给了 started_at 就只删那一条（别误删之后新开的那轮）。"""
        with self._lock:
            data = self._load()
            entry = data.get(scope)
            if entry is None:
                return
            if started_at is not None and isinstance(entry, dict) \
                    and entry.get("started_at") != started_at:
                return
            data.pop(scope, None)
            self._save(data)

    def leftovers(self) -> list[dict]:
        """现在盘上还留着的（启动时调 = 被重启打断的那些）。"""
        with self._lock:
            data = self._load()
        return [dict(e) for e in data.values()
                if isinstance(e, dict) and isinstance(e.get("scope"), str) and e.get("scope")]
