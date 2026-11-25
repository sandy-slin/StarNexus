"""会话「结束」和「稍后提醒」的记录（第 33 轮）。

## 结束（settled）
主人在详情卡上点「✓ 结束」⇒ 记一条 `(tool, id, 结束时间)`，总览就不再列它。
- 它之后有新动静（会话的 `updated_at` 晚于结束时间），或者它又在跑 / 等你了 ⇒ 自动回来、删掉记录。
- 在跑 / 等你的会话不许结束（`can_settle`）。
- 🔴 **只记在我们这边**：不写回 Claude / Codex / Cursor 的库。点错了撤销就行，那边什么都没动过。

## 稍后提醒（snoozed）
「等你」推送卡上点「1 小时后再提醒」⇒ 这个会话的推送静音 1 小时。
- 只静音**这一个问题**：记的是那一刻它在问什么的指纹（`pending_sig`，只存哈希不存原文）。
  静音期间它问了**新的**（指纹变了）⇒ 照样推。

## 落盘
- config 同目录 `settled.json`，0600、原子写（同 `inflight.py`）。两样放在两个键里：
  `{"settled": {"tool\\tid": 结束时间}, "snoozed": {"tool\\tid": {"until": 秒, "sig": "…"}}}`
- 没有 config 路径 ⇒ 只留内存，**不退回去写 `~/.carher-feishu-agent`**（测试里造的桥会污染真目录）。
- 坏文件 / 读不了 ⇒ 当没有；写不进 ⇒ 记一行日志，内存里照样生效。
- 每样最多留 `MAX_ENTRIES` 条，按时间淘汰最旧的。

## 谁用哪一份
`store_for(config_path)` 按路径缓存一份。桥点按钮时用 `settings.config_path` 拿到它并 `bind()`；
总览卡 / 推送判断用 `active()`：绑过的那份 ⇒ 用它；还没绑过（刚重启、还没点过按钮）⇒
按 `FEISHU_BRIDGE_CONFIG`（安装器写进 launchd / 启动脚本的那个，`app.main` 也读它）找同一份；
都没有 ⇒ 只在内存的那份。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger("feishu-bridge.fleet")

SETTLED_FILE = "settled.json"
MAX_ENTRIES = 500
SNOOZE_SEC = 60 * 60
# 这两种状态不许结束：结束了它也会马上因为「有新动静」回来，等于按钮没用；而且「等你」被藏起来最危险
LIVE_STATUSES = ("running", "waiting")

WHY_NOT_SETTLE = {
    "running": "它还在跑，等它停下再结束。",
    "waiting": "它在等你回话，先处理了再结束。",
}


def _key(tool: str, sid: str) -> str:
    return f"{tool}\t{sid}"


def can_settle(sess: Any) -> tuple:
    """(能不能结束, 不能时的一句话)。"""
    status = getattr(sess, "status", "")
    if status in LIVE_STATUSES:
        return False, WHY_NOT_SETTLE[status]
    return True, ""


def pending_sig(sess: Any) -> str:
    """它这会儿在问什么的指纹。🔴 只存哈希：问题原文可能带项目里的东西，不落盘。"""
    pending = getattr(sess, "pending", None)
    if pending is None:
        return ""
    try:
        questions = [
            [str(getattr(q, "question", "")), str(getattr(q, "header", "")),
             [list(map(str, o)) for o in (getattr(q, "options", ()) or ())]]
            for q in (getattr(pending, "questions", ()) or ())
        ]
        blob = json.dumps([str(getattr(pending, "tool", "")), str(getattr(pending, "summary", "")),
                           questions], ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 —— 形状不对就当「读不出在问什么」
        return ""
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _num(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out > 0 else None  # NaN / 负数不要


class SettledStore:
    """一个文件两张表。path 为空 ⇒ 只在内存。线程安全（点按钮、造总览、后台推送三条线程都会用）。"""

    def __init__(self, path: str = "", now=time.time) -> None:
        self.path = path
        self._now = now
        self._lock = threading.Lock()
        self._settled: Optional[dict] = None  # key → 结束时间
        self._snoozed: dict = {}  # key → {"until", "sig"}

    # ---------- 读写 ----------

    def _ensure(self) -> None:
        """第一次用时读盘。坏文件 / 读不了 ⇒ 当没有。调用方持锁。"""
        if self._settled is not None:
            return
        self._settled = {}
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            logger.warning("settled.json 读不出来，当成没有记录")
            return
        if not isinstance(data, dict):
            return
        settled = data.get("settled")
        if isinstance(settled, dict):
            for key, at in settled.items():
                at = _num(at)
                if isinstance(key, str) and "\t" in key and at is not None:
                    self._settled[key] = at
        snoozed = data.get("snoozed")
        if isinstance(snoozed, dict):
            for key, rec in snoozed.items():
                if not (isinstance(key, str) and "\t" in key and isinstance(rec, dict)):
                    continue
                until = _num(rec.get("until"))
                sig = rec.get("sig")
                if until is not None and isinstance(sig, str):
                    self._snoozed[key] = {"until": until, "sig": sig}

    def _trim(self) -> None:
        if len(self._settled) > MAX_ENTRIES:
            keep = sorted(self._settled.items(), key=lambda kv: kv[1])[-MAX_ENTRIES:]
            self._settled = dict(keep)
        now = self._now()
        self._snoozed = {k: v for k, v in self._snoozed.items() if v["until"] > now}
        if len(self._snoozed) > MAX_ENTRIES:
            keep = sorted(self._snoozed.items(), key=lambda kv: kv[1]["until"])[-MAX_ENTRIES:]
            self._snoozed = dict(keep)

    def _save(self) -> None:
        """调用方持锁。没有路径 ⇒ 只在内存。写不进 ⇒ 留一行日志，内存里照样生效。"""
        self._trim()
        if not self.path:
            return
        directory = os.path.dirname(self.path) or "."
        name = ""
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             prefix=".settled-", delete=False) as tmp:
                name = tmp.name
                json.dump({"settled": self._settled, "snoozed": self._snoozed}, tmp, ensure_ascii=False)
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        except OSError:
            logger.warning("「结束 / 稍后提醒」记不下来（重启后会忘）", exc_info=True)
            if name:
                try:
                    os.unlink(name)
                except OSError:
                    pass

    # ---------- 结束 ----------

    def settle(self, tool: str, sid: str, at: Optional[float] = None) -> float:
        at = float(at) if at is not None else float(self._now())
        with self._lock:
            self._ensure()
            self._settled[_key(tool, sid)] = at
            self._save()
        return at

    def unsettle(self, tool: str, sid: str) -> bool:
        with self._lock:
            self._ensure()
            gone = self._settled.pop(_key(tool, sid), None) is not None
            if gone:
                self._save()
        return gone

    def settled_at(self, tool: str, sid: str) -> Optional[float]:
        with self._lock:
            self._ensure()
            return self._settled.get(_key(tool, sid))

    def count(self) -> int:
        with self._lock:
            self._ensure()
            return len(self._settled)

    def split(self, sessions: Iterable[Any]) -> tuple:
        """(要列出来的, 藏起来的个数)。有新动静 / 又在跑或等你了的 ⇒ 列出来，并删掉它的记录。"""
        shown: list = []
        hidden = 0
        with self._lock:
            self._ensure()
            changed = False
            for sess in sessions or ():
                key = _key(getattr(sess, "tool", ""), str(getattr(sess, "id", "")))
                at = self._settled.get(key)
                if at is None:
                    shown.append(sess)
                    continue
                updated = _num(getattr(sess, "updated_at", 0)) or 0
                if updated > at or getattr(sess, "status", "") in LIVE_STATUSES:
                    del self._settled[key]
                    changed = True
                    shown.append(sess)
                    continue
                hidden += 1
            if changed:
                self._save()
        return shown, hidden

    # ---------- 稍后提醒 ----------

    def snooze(self, tool: str, sid: str, sig: str, seconds: float = SNOOZE_SEC) -> float:
        until = float(self._now()) + float(seconds)
        with self._lock:
            self._ensure()
            self._snoozed[_key(tool, sid)] = {"until": until, "sig": str(sig or "")}
            self._save()
        return until

    def snoozed(self, sess: Any) -> bool:
        """这一刻这个会话的「等你」推送要不要静音。过期了 / 问的东西变了 ⇒ 不静音（并删掉记录）。"""
        key = _key(getattr(sess, "tool", ""), str(getattr(sess, "id", "")))
        with self._lock:
            self._ensure()
            rec = self._snoozed.get(key)
            if rec is None:
                return False
            if self._now() < rec["until"] and rec["sig"] == pending_sig(sess):
                return True
            del self._snoozed[key]
            self._save()
            return False


# ---------- 谁用哪一份 ----------

_REG_LOCK = threading.Lock()
_STORES: dict = {}
_BOUND: Optional[SettledStore] = None
CONFIG_ENV = "FEISHU_BRIDGE_CONFIG"


def path_for(config_path: str) -> str:
    """config 同目录的 settled.json；没有 config_path ⇒ 空串（只在内存）。"""
    if not config_path:
        return ""
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), SETTLED_FILE)


def store_for(config_path: str) -> SettledStore:
    path = path_for(config_path)
    with _REG_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = _STORES[path] = SettledStore(path)
        return store


def bind(store: SettledStore) -> SettledStore:
    global _BOUND
    with _REG_LOCK:
        _BOUND = store
    return store


def active() -> SettledStore:
    """总览卡 / 推送判断用的那一份（见模块头「谁用哪一份」）。"""
    with _REG_LOCK:
        bound = _BOUND
    if bound is not None:
        return bound
    return store_for(os.environ.get(CONFIG_ENV, "") or "")


def _reset() -> None:
    """只给测试用：清掉缓存和绑定。"""
    global _BOUND
    with _REG_LOCK:
        _STORES.clear()
        _BOUND = None
