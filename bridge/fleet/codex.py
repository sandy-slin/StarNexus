"""Codex 的会话：列（state_5.sqlite 只读）/ 谁占着（写锁 + lsof）/ 读（rollout 尾部）/ 发 / 停。

## 为什么要有这个文件

手机上想看「电脑上的 Codex 在干嘛」、顺手接着聊一句。Codex App 和命令行写的是同一套东西：
`~/.codex/state_5.sqlite` 的 threads 表（元数据）+ `~/.codex/sessions/**/rollout-*.jsonl`（对话）。

## 边界（实测出来的，别改松）

- **只读**：sqlite 一律 `file:...?mode=ro`；rollout 只从尾部读；绝不写任何 Codex 的库。
- **谁占着 = 写锁**：`thread-writer-locks/<threadId>.lock` 被谁打开，谁就是主人。
  App 开着的那个对话，闲着也占锁。`exec resume` 这条路有人占着就**不发**：它会等 10 秒后报
  `already has an active writer`，与其让用户等着看失败，不如先说清楚。
- **线程 id = 文件名里时间戳后面的第一个 UUID**（后面那个是分段 id，复核纠正过）。
- **只停自己起的**：别人的进程一个信号都不发，闸门在 `FleetJobs`。
- 读失败不抛：返回已有的，说明放 `self.last_error`。

## 进会话聊天（`relay()`，2026-11-04 实测复核）

上一轮写的「`codex queue` 等 40 秒没人取、写进去等于石沉大海」**是错的** —— 那次是在一个
**没有主人**的线程上量的。队列行要有人持写锁才会被取走：

| 实测场景（都是一次性新建的线程） | 结果 | 取走用了多久 |
|---|---|---|
| App（ChatGPT / app-server）开着它 | 取走，App 自己跑完这一轮 | 6.0 / 5.9 / 8.2 秒 |
| 终端 TUI 开着它 | 取走，TUI 当成正常一轮回了 | 1.5 秒 |
| 命令行（0.153 / App 内嵌 0.166-alpha）开着它 | 取走 | 4.1~8.3 秒 |
| 没人占锁 | 行一直躺着不动 | 70 秒没动静；等有人 load 时 0.4 秒排干 |
| 对方正在跑一轮 | 等它那一轮跑完，再当**自己一轮**跑（不是插话） | 排进去 +8.1s，开跑 +12.5s |
| 一口气两条 | 按 `queue_order` 两轮都跑了 | 一起 6.9 秒排干 |

所以 `relay()` 分两条路：**别人（App / 终端）占着** ⇒ `codex queue`；**没人占着** ⇒ 还是
`exec resume`（能同步拿回复，不用等那 10 秒轮询）。取走是**约 10 秒一次的轮询，不是事件**
⇒ Codex 这条路的现实下限是「10 秒 + 它自己跑一轮」，比 Claude 那条（约 5 秒）慢，
所以超时别设得比 30 秒还短。

**入队成功 ≠ 已送达**：`codex queue` 0.09~0.31 秒就退 0，那只代表行写进了
`queue_1.sqlite`。真送达的判据是**目标自己的 rollout 里出现我们这句的 user 回显**，
之后才是 assistant 回复 + `event_msg task_complete`（都在目标线程自己的 rollout 里，不分叉）。

两个坑：
- **还没有 rollout 的线程排不进去**：报 `no rollout found for thread id <id> (code -32603)`、
  退出码 1（刚开还没说过一句话的 TUI 就是这个状态）⇒ 先看有没有分段，没有就直接说清楚。
- 行还在 `queued_items` 里、线程又没人占锁 ⇒ 是「排上了但这会儿没人在听」，不是失败。
  `pending_queued()` 只读看一眼就知道（`?mode=ro`，和 `state_5.sqlite` 一样绝不写）。

## 两条**故意不做**的路

- **`ipc/ipc.sock` 的写方法**（`thread-follower-start-turn` / `steer-turn` / `interrupt-turn`）：
  一次都没执行过，也不实现。queue 这条官方路在 App / 终端 / 无主三种线程上都通了，
  没有理由去碰那条按方法分版本、App 一升级就会变的私有协议。
- **深链 `codex://threads/wake/<id>`**：实测它真能让 App 在 0.5 秒内把躺着的队列行取走
  （比等 10 秒轮询快），但副作用是 **App 会抢走这个线程的写锁并一直占着** —— 实测占到
  `codex delete --force` 都删不掉那个线程。而「没人占锁」本来就走 `exec resume` 直接发，
  不值得为了快十秒去动用户的 App 状态。
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import signal as _signal
import sqlite3
import threading
import time
import urllib.parse
from typing import Any, Callable, Optional

from .model import (
    ActionResult,
    Adapter,
    FleetJobs,
    Message,
    ProcTable,
    Session,
    SubprocessRunner,
    friendly_error,
    parse_proc_start,
    short_title,
)

logger = logging.getLogger("feishu-bridge.fleet.codex")

_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_UUID_RE = re.compile(rf"^{_UUID}$")
# rollout-2026-11-04T15-55-55-<threadId>[_<segmentId>].jsonl
_ROLLOUT_RE = re.compile(rf"^rollout-\d{{4}}-\d{{2}}-\d{{2}}T\d{{2}}-\d{{2}}-\d{{2}}-({_UUID})(?:_({_UUID}))?\.jsonl$")

SQLITE_TIMEOUT_SEC = 2.0
LSOF_TIMEOUT_SEC = 2.0
BUSY_TAIL_BYTES = 256 * 1024  # 一条记录可能很大，256KB 是实测给的量
READ_TAIL_BYTES = 1024 * 1024
READ_TAIL_MAX_BYTES = 4 * 1024 * 1024
READ_MAX_SEGMENTS = 3
STOP_GRACE_SEC = 15.0  # 软超时先 SIGINT，再等这么久才让 runner 硬杀
MAX_LOCKS = 200

# ---------- relay（进会话聊天） ----------

QUEUE_CMD_TIMEOUT_SEC = 30.0  # `codex queue` 实测 0.09~0.31 秒就退；30 秒是给卡住的兜底
RELAY_TIMEOUT_SEC = 120.0  # 等回复的默认上限（spec 一 定的 120 秒）
RELAY_POLL_SEC = 1.0
# 取走是约 10 秒一次的轮询（实测 1.5~8.3 秒落在这个周期里）。低于这个值等于必然超时 ⇒
# 不让调用方把 relay 的超时配到「连一次轮询都等不到」。
RELAY_MIN_TIMEOUT_SEC = 30.0
RELAY_TAIL_BYTES = 512 * 1024

# Codex 往 user 消息里塞的上下文，不是用户说的话
_USER_NOISE_PREFIXES = (
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md instructions",
    "<permissions",
    "<turn_aborted>",
    "<codex_",
)
_TEXT_PART_TYPES = ("input_text", "output_text", "text")

# 当前权限档位 → `-c sandbox_mode=…`。手机入口和终端入口共用同一档位，
# 明确给 trusted 一个工作区写入边界，避免依赖用户 Codex 配置。
# `readonly` 只读；`write` / `trusted` 可写当前工作区，不能越过工作区边界。
SANDBOX_BY_MODE = {"readonly": "read-only", "write": "workspace-write", "trusted": "workspace-write"}

WHY_NOT_SEND_APP ="它正开在 Codex App 里，现在从手机发会失败。请在 App 里接着聊。"
WHY_NOT_SEND_TERMINAL = "它正开在电脑的终端里，现在从手机发会失败。等那边关掉这个会话再发。"
WHY_NOT_SEND_BRIDGE = "手机上发的上一条还在跑，等它回完再发。"
WHY_NOT_SEND_UNKNOWN = "说不准它现在有没有被别的程序占着，为了不出错先不发。"
WHY_NOT_SEND_ARCHIVED = "这个会话已经归档了，先在 Codex 里取消归档再发。"
WHY_NOT_STOP_APP = "它开在 Codex App 里，只能在 App 里停。"
WHY_NOT_STOP_TERMINAL = "它开在终端里，只能在终端里停；从外面停会直接关掉整个会话。"
WHY_NOT_STOP_IDLE = "它现在没有在跑从手机上发的任务，没有可停的。"

# relay 的拒绝话术（每条都要有「能做到的替代」）
WHY_RELAY_BAD_ID = "会话编号不对，发不了。"
WHY_RELAY_EMPTY = "要发的话是空的。"
WHY_RELAY_ARCHIVED = "这个会话已经归档了，先在 Codex 里取消归档再发。"
WHY_RELAY_NOT_FOUND = "没找到这个 Codex 会话，可能已经删了。"
WHY_RELAY_NO_ROLLOUT = (
    "这个会话还没说过一句话，现在排不进去（Codex 要有记录才收）。"
    "先在电脑上跟它说一句，再从手机接着聊。"
)
WHY_RELAY_NO_CWD = "这个会话的项目文件夹不在了，没法接着聊。"
WHY_RELAY_BUSY = "手机上发的上一条还在排，等它回完再发。"
# 排进去了但没等到回复：**不能说没发进去**，用户会重发 ⇒ 在他真实的会话里跑第二遍
RELAY_QUEUED_NO_REPLY = (
    "话已经排进去了，但到点还没等到回复。它可能正忙着上一轮，干完就会处理这句。"
    "先点进去看看最新情况，别急着重发。"
)
RELAY_QUEUED_NOBODY_HOME = (
    "话已经排进去了，但这会儿电脑上没人开着这个会话，所以没人处理。"
    "等在 Codex App 或终端里打开它，这句会自动接上。"
)
RELAY_DELIVERED_NO_TEXT = "它处理完了这句，但没有写文字回复。"
RELAY_ABORTED = "它把这一轮取消了（电脑上按了停，或者它自己中断了）。"
# 停：实测只能在对方自己那边停，别装；排进去的那句也撤不回来
WHY_RELAY_STOP_APP = "这一轮在 Codex App 里跑着，只能在 App 里点停止，手机上停不了。"
WHY_RELAY_STOP_TERMINAL = "这一轮在电脑的终端里跑着，只能在那边按 Esc 停，手机上停不了。"
WHY_RELAY_NO_UNSEND = (
    "排进去的话撤不回来（Codex 没给撤回的口子，手机这边也不会去改它的队列文件）。"
    "不想让它做的话，在电脑上打开这个会话跟它说一声。"
)

# ---------- 故意不实现的两条路（spec B 第 2、3 项） ----------
# 每条都给手机用户一句大白话，而不是悄悄不做。
WHY_NO_IPC_WRITE = (
    "没有走 Codex App 那条没公开的内部通道（它一升级就会变，容易把 App 搞乱）。"
    "手机上这句是按 Codex 自己的排队功能送进去的，效果一样，就是要多等几秒。"
)
WHY_NO_WAKE_DEEPLINK = (
    "没有用深链去「叫醒」Codex App 接这句话：实测那会让 App 抢住这个会话不放，"
    "连在电脑上删都删不掉。现在的做法是排进队列，等你在电脑上打开它就自动接上。"
)


def thread_id_from_rollout(path: str) -> str:
    """rollout 文件名 → 线程 id（时间戳后的**第一个** UUID）；不像 rollout 就返回空串。"""
    m = _ROLLOUT_RE.match(os.path.basename(str(path or "")))
    return m.group(1).lower() if m else ""


def is_thread_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _epoch(value: Any) -> float:
    cands = parse_proc_start(value)
    return cands[0] if cands else 0.0


def _is_subagent(row: dict) -> bool:
    source = str(row.get("source") or "")
    if source.lstrip().startswith("{"):
        return True
    if str(row.get("thread_source") or "") == "subagent":
        return True
    return bool(row.get("attached_thread_parent_id") or row.get("parent_thread_id"))


def _surface(source: str) -> str:
    # vscode = Codex App（桌面）/ IDE 插件；cli / exec / 其他 = 命令行
    return "app" if source == "vscode" else "cli"


def _tail_lines(path: str, nbytes: int) -> tuple:
    """读文件最后 nbytes，丢掉第一行半截。返回 (行列表, 是否读到了文件头)。"""
    size = os.path.getsize(path)
    start = max(0, size - nbytes)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read(nbytes)
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    return lines, start == 0


def _records(lines) -> list:
    out = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _turn_state(records) -> str:
    """task_started / task_complete / turn_aborted 里最后一个是 task_started ⇒ running。"""
    last = ""
    for rec in records:
        if rec.get("type") != "event_msg":
            continue
        kind = (rec.get("payload") or {}).get("type")
        if kind in ("task_started", "task_complete", "turn_aborted"):
            last = kind
    if not last:
        return ""
    return "running" if last == "task_started" else "idle"


def _turn_end(records) -> tuple:
    """rollout 记录 → (最后一轮怎么结束的, 结束时间, 最后一次 task_started 的时间)。

    本机 214 个 rollout 实测（codex-cli 0.153.4，2026-11-09）：轮次事件只有 task_started /
    task_complete / turn_aborted 三种，**rollout 里一条报错事件都没有**（`turn.failed` 只出现在
    `codex exec --json` 的 stdout 里，见 `parse_exec_output`）。所以这里只判得出「跑完了」：
    - 最后是 task_complete 且带时间 ⇒ ("done", 时间)
    - turn_aborted（被打断）/ task_started（在跑）/ 没有时间 ⇒ ("", 0)，不猜
    """
    last, at, started = "", 0.0, 0.0
    for rec in records:
        if rec.get("type") != "event_msg":
            continue
        kind = (rec.get("payload") or {}).get("type")
        if kind not in ("task_started", "task_complete", "turn_aborted"):
            continue
        stamp = _epoch(rec.get("timestamp"))
        last, at = kind, stamp
        if kind == "task_started":
            started = max(started, stamp)
    if last == "task_complete" and at:
        return "done", at, started
    return "", 0.0, started


def _message_of(rec: dict) -> Optional[Message]:
    if rec.get("type") != "response_item":
        return None
    payload = rec.get("payload") or {}
    if payload.get("type") != "message":
        return None  # reasoning / function_call / function_call_output 都跳过
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None  # developer 是系统塞的
    parts = []
    for part in payload.get("content") or []:
        if not isinstance(part, dict) or part.get("type") not in _TEXT_PART_TYPES:
            continue
        text = str(part.get("text") or "").strip()
        if not text:
            continue
        if role == "user" and text.startswith(_USER_NOISE_PREFIXES):
            continue
        parts.append(text)
    if not parts:
        return None
    return Message(role=role, text="\n".join(parts), at=_epoch(rec.get("timestamp")))


class CodexAdapter(Adapter):
    tool = "codex"

    def __init__(
        self,
        home: Optional[str] = None,
        jobs: Optional[FleetJobs] = None,
        runner: Optional[SubprocessRunner] = None,
        codex_bin: Optional[str] = None,
        lsof_bin: Optional[str] = None,
        lsof_timeout: float = LSOF_TIMEOUT_SEC,
    ) -> None:
        super().__init__(jobs=jobs, runner=runner)
        self._default_home = home is None
        # 默认跟 codex 自己一样：有 CODEX_HOME 用它，没有就是 ~/.codex
        self.home = os.path.abspath(os.path.expanduser(home or os.environ.get("CODEX_HOME") or "~/.codex"))
        self.codex_bin = codex_bin
        self.lsof_bin = lsof_bin
        self.lsof_timeout = lsof_timeout
        self._send_guard = threading.Lock()
        self._sending: set = set()
        # 第 32 轮：桥自己发的那一轮读到 `turn.failed` / 顶层 error 的时间（线程 id → epoch 秒）。
        # rollout 里没有失败事件（见 `_turn_end`），这是唯一读得到的失败信号 ⇒ 只记在内存；
        # 那之后 rollout 里又开了新的一轮（task_started 更晚）就作废。
        self._failed_at: dict = {}

    # ---------- 路径 ----------

    @property
    def db_path(self) -> str:
        return os.path.join(self.home, "state_5.sqlite")

    @property
    def locks_dir(self) -> str:
        return os.path.join(self.home, "thread-writer-locks")

    def _inside_home(self, path: str) -> bool:
        try:
            real = os.path.realpath(path)
            root = os.path.realpath(self.home)
        except (OSError, ValueError):
            return False
        return real == root or real.startswith(root + os.sep)

    # ---------- sqlite（只读） ----------

    def _connect(self) -> Optional[sqlite3.Connection]:
        if not os.path.exists(self.db_path):
            self.last_error = "没找到 Codex 的会话记录（这台电脑可能没用过 Codex）。"
            return None
        try:
            uri = "file:" + urllib.parse.quote(self.db_path) + "?mode=ro"
            return sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SEC)
        except sqlite3.Error as exc:
            self.last_error = friendly_error(exc, "打不开 Codex 的会话记录")
            return None

    @staticmethod
    def _columns(conn: sqlite3.Connection) -> set:
        return {row[1] for row in conn.execute("PRAGMA table_info(threads)")}

    _WANTED = (
        "id", "source", "thread_source", "attached_thread_parent_id", "cwd", "name", "title",
        "preview", "first_user_message", "archived", "updated_at", "updated_at_ms",
        "recency_at_ms", "rollout_path", "model",
    )

    def _query(
        self, where: str = "", params: tuple = (), limit: int = 0,
        include_archived: bool = False, skip_subagents: bool = True,
    ) -> list:
        conn = self._connect()
        if conn is None:
            return []
        try:
            cols = self._columns(conn)
            if "id" not in cols:
                self.last_error = "Codex 的会话表格式变了，读不出来。"
                return []
            picked = [c for c in self._WANTED if c in cols]
            clauses = [where] if where else []
            if "archived" in cols and not include_archived:
                clauses.append("coalesce(archived, 0) = 0")
            if "source" in cols and skip_subagents:
                clauses.append("coalesce(source, '') not like '{%'")  # 子 agent 行先在 SQL 里筛一道
            order = next((c for c in ("recency_at_ms", "updated_at_ms", "updated_at") if c in cols), "rowid")
            sql = f"select {', '.join(picked)} from threads"
            if clauses:
                sql += " where " + " and ".join(f"({c})" for c in clauses)
            sql += f" order by {order} desc"
            if limit > 0:
                sql += f" limit {int(limit)}"
            rows = [dict(zip(picked, r)) for r in conn.execute(sql, params)]
        except sqlite3.Error as exc:
            self.last_error = friendly_error(exc, "读 Codex 会话记录失败")
            return []
        finally:
            conn.close()
        return [r for r in rows if r.get("id") and not (skip_subagents and _is_subagent(r))]

    def _row(self, thread_id: str, include_archived: bool = True) -> Optional[dict]:
        rows = self._query("id = ?", (thread_id,), limit=1, include_archived=include_archived)
        return rows[0] if rows else None

    # ---------- rollout ----------

    def _segments(self, thread_id: str, row: Optional[dict] = None) -> list:
        """这个线程的 rollout 分段，最新的在前（按 mtime）。只认文件名第一个 UUID 等于 id 的。"""
        if not is_thread_id(thread_id):
            return []
        tid = thread_id.lower()
        found = set()
        base = os.path.join(self.home, "sessions")
        for pattern in (f"rollout-*-{tid}.jsonl", f"rollout-*-{tid}_*.jsonl"):
            for path in glob.glob(os.path.join(base, "*", "*", "*", pattern)):
                if thread_id_from_rollout(path) == tid:
                    found.add(path)
        rollout = str((row or {}).get("rollout_path") or "")
        if rollout and rollout.endswith(".jsonl") and os.path.isfile(rollout) and self._inside_home(rollout):
            found.add(rollout)

        def mtime(p: str) -> float:
            try:
                return os.path.getmtime(p)
            except OSError:
                return 0.0

        return sorted(found, key=mtime, reverse=True)

    def _turn_status(self, thread_id: str, row: Optional[dict] = None) -> str:
        segs = self._segments(thread_id, row)
        if not segs:
            return ""
        try:
            lines, _ = _tail_lines(segs[0], BUSY_TAIL_BYTES)
            state = _turn_state(_records(lines))
            if not state:  # 尾部恰好是一条超大记录，放宽再看一次
                lines, _ = _tail_lines(segs[0], READ_TAIL_MAX_BYTES)
                state = _turn_state(_records(lines))
            return state
        except OSError as exc:
            self.last_error = friendly_error(exc, "读 Codex 对话记录失败")
            return ""

    def _last_turn(self, thread_id: str, row: Optional[dict]) -> tuple:
        """这个线程最后一轮怎么结束的 → ("done"|"failed"|"", 时间)。读不到 ⇒ ("", 0)，绝不抛。

        🔴 只看 threads 行里的 `rollout_path`，不 glob：历史会话一次扫 30 个，每个 glob 一遍
        `sessions/*/*/*` 会拖慢总览（`scan.DEFAULT_TIMEOUT_SEC` 才 1.2 秒）。没有这个字段的就不标。
        """
        tid = str(thread_id or "").lower()
        rollout = str((row or {}).get("rollout_path") or "")
        started = 0.0
        state, at = "", 0.0
        try:
            if rollout.endswith(".jsonl") and os.path.isfile(rollout) and self._inside_home(rollout):
                lines, _ = _tail_lines(rollout, BUSY_TAIL_BYTES)
                state, at, started = _turn_end(_records(lines))
        except Exception:  # noqa: BLE001 —— 锦上添花，读不到就不标
            logger.debug("读 Codex 最后一轮失败 tid=%s", tid, exc_info=True)
            state, at = "", 0.0
        failed = self._failed_at.get(tid, 0.0)
        if failed and failed >= started and failed >= at:
            return "failed", failed
        return state, at

    def _note_turn_failed(self, tid: str) -> None:
        self._failed_at[str(tid or "").lower()] = time.time()

    # ---------- 谁占着 ----------

    def _lsof(self) -> str:
        if self.lsof_bin:
            return self.lsof_bin
        for cand in ("/usr/sbin/lsof", "/usr/bin/lsof"):
            if os.path.exists(cand):
                return cand
        return shutil.which("lsof") or "lsof"

    def _holders(self, thread_ids: Optional[list] = None) -> tuple:
        """{线程 id: [持锁 pid]}，以及一句错误（查不了时）。

        没有锁文件 ⇒ ({}, "")，不用起 lsof。lsof 查不了 ⇒ 有锁文件的线程都记成 `None`（说不准）。
        """
        try:
            names = sorted(os.listdir(self.locks_dir))
        except FileNotFoundError:
            return {}, ""
        except OSError as exc:
            return {}, friendly_error(exc, "读不到 Codex 的写锁目录")
        paths = {}
        for name in names:
            tid = name[:-5] if name.endswith(".lock") else ""
            if not is_thread_id(tid):
                continue
            if thread_ids is not None and tid.lower() not in thread_ids:
                continue
            paths[os.path.join(self.locks_dir, name)] = tid.lower()
        if not paths:
            return {}, ""
        files = list(paths)[:MAX_LOCKS]
        try:
            res = self.runner.run([self._lsof(), "-F", "pn", *files], timeout=self.lsof_timeout)
        except Exception as exc:
            res = None
            err = friendly_error(exc, "查不了谁占着 Codex 会话")
        else:
            err = ""
            if res.error:
                err = "查不了谁占着 Codex 会话：" + res.error
            elif res.timed_out:
                err = "查谁占着 Codex 会话超时了"
            elif res.returncode not in (0, 1):
                # 0 = 有人开着；1 = 列出的文件没人开着（这是常态）；别的都是 lsof 自己出错
                # 退出码只进日志：给用户看的话不带内部细节（第 11 轮文案门禁）
                logger.warning("lsof 退出码 %s，查不了谁占着 Codex 会话", res.returncode)
                err = "查不了谁占着 Codex 会话（系统命令没跑成）"
        if err:
            return {tid: None for tid in paths.values()}, err
        held: dict = {}
        pid = 0
        for line in (res.stdout or "").splitlines():
            if line.startswith("p"):
                try:
                    pid = int(line[1:])
                except ValueError:
                    pid = 0
            elif line.startswith("n") and pid:
                tid = paths.get(line[1:])
                if tid is None:  # lsof 可能把路径规范化（/private/...）
                    tid = thread_id_from_lock(line[1:])
                if tid:
                    held.setdefault(tid, [])
                    if pid not in held[tid]:
                        held[tid].append(pid)
        return held, ""

    def _owned(self, pid: int, table: ProcTable) -> bool:
        """pid 或它的祖先（codex 是 node 包装 + 原生子进程）是桥自己起的。"""
        cur, seen = pid, set()
        while cur and cur not in seen and len(seen) < 6:
            seen.add(cur)
            if self.jobs.owns(cur, table):
                return True
            proc = table.get(cur)
            cur = proc.ppid if proc else 0
        return False

    @staticmethod
    def _is_app(pid: int, table: ProcTable) -> bool:
        cur, seen = pid, set()
        while cur and cur not in seen and len(seen) < 6:
            seen.add(cur)
            proc = table.get(cur)
            if proc is None:
                return False
            if "Codex.app/" in proc.args or "CodexCLI.app/" in proc.args:
                return True
            cur = proc.ppid
        return False

    def _owner(self, pids: list, table: ProcTable) -> str:
        if any(self._owned(p, table) for p in pids):
            return "bridge"
        if table.error:
            return "unknown"
        if any(self._is_app(p, table) for p in pids):
            return "app"
        return "terminal"

    def _state(self, thread_ids: Optional[list] = None) -> dict:
        """{线程 id: (owner, pids, status)}，只含被占着的（含桥自己正在跑的）。"""
        held, err = self._holders(thread_ids)
        if err:
            self.last_error = err
        own = {}
        for job in self.jobs.jobs(tool=self.tool):
            if thread_ids is None or job.session_id in thread_ids:
                own.setdefault(job.session_id, []).append(job.pid)
        if not held and not own:
            return {}
        table = ProcTable.snapshot(runner=self.runner)
        out = {}
        for tid, pids in held.items():
            if pids is None:
                out[tid] = ("unknown", (), "unknown")
                continue
            if not pids:
                continue
            owner = self._owner(pids, table)
            procs = [table.get(p) for p in pids]
            if procs and all(p is not None and p.stopped for p in procs):
                status = "stopped"
            else:
                status = self._turn_status(tid) or "unknown"
            out[tid] = (owner, tuple(pids), status)
        for tid, pids in own.items():
            alive = [p for p in pids if self.jobs.owns(p, table)]
            if alive and (tid not in out or out[tid][0] != "bridge"):
                out[tid] = ("bridge", tuple(alive), "running")
        return out

    # ---------- 组装 Session ----------

    def _session(self, row: dict, state: Optional[tuple]) -> Session:
        tid = str(row.get("id") or "").lower()
        source = str(row.get("source") or "")
        title = ""
        for key in ("name", "title", "preview", "first_user_message"):
            if row.get(key):
                title = short_title(row[key], 40)
                break
        updated = 0.0
        for key in ("recency_at_ms", "updated_at_ms", "updated_at"):
            if row.get(key):
                updated = _epoch(row[key])
                if updated:
                    break
        archived = bool(row.get("archived"))
        owner, pids, status = state if state else ("none", (), "idle")
        if owner == "app":
            why_send, why_stop = WHY_NOT_SEND_APP, WHY_NOT_STOP_APP
        elif owner == "terminal":
            why_send, why_stop = WHY_NOT_SEND_TERMINAL, WHY_NOT_STOP_TERMINAL
        elif owner == "bridge":
            why_send, why_stop = WHY_NOT_SEND_BRIDGE, ""
        elif owner == "unknown":
            why_send, why_stop = WHY_NOT_SEND_UNKNOWN, WHY_NOT_STOP_IDLE
        else:
            why_send, why_stop = "", WHY_NOT_STOP_IDLE
        if archived and not why_send:
            why_send = WHY_NOT_SEND_ARCHIVED
        extra = {"source": source}
        if row.get("model"):
            extra["model"] = str(row["model"])
        if archived:
            extra["archived"] = True
        # 第 32 轮：闲着的才看最后一轮（在跑 / 等你 / 挂起的读了也不显示）
        last_turn, last_turn_at = self._last_turn(tid, row) if status == "idle" else ("", 0.0)
        return Session(
            tool=self.tool,
            surface=_surface(source),
            id=tid,
            title=title or "（没有标题）",
            cwd=str(row.get("cwd") or ""),
            status=status,
            pids=pids,
            owner=owner,
            updated_at=updated,
            can_send=not why_send,
            # 🔴 2026-11-05 用户报「点了几下没建群」：App 开着的 Codex 会话详情卡上**没有
            # [接着聊]**（卡片按 `can_send or can_relay` 决定给不给），而这里从来没填 can_relay
            # ⇒ 永远 False ⇒ 只剩「请在 App 里接着聊」那句拒绝。
            # 可 `relay()` 本来就是为这种会话做的：App / 终端占着 ⇒ `codex queue`，
            # 模块头那张表实测 App 6~8 秒取走、终端 1.5 秒取走。能力在，只是没告诉卡片。
            # 归档了的不开（App 里已经看不到它了）；`bridge` 是我们自己正在发，别叠一条。
            can_relay=owner in ("app", "terminal") and not archived,
            can_stop=owner == "bridge",
            why_not_send=why_send,
            why_not_stop=why_stop,
            extra=extra,
            last_turn=last_turn,
            last_turn_at=last_turn_at,
        )

    # ---------- 公开方法 ----------

    def list(self, limit: int = 20) -> list:
        """最近的会话：在跑的在前，其余按最近活动排。不按模型提供方过滤；跳过子 agent 和已归档的。"""
        self.last_error = ""
        try:
            limit = max(1, int(limit))
            rows = self._query(limit=limit)
            state = self._state()
            seen = {str(r["id"]).lower() for r in rows}
            missing = [tid for tid in state if tid not in seen]
            for tid in missing:
                row = self._row(tid)
                if row is not None:
                    rows.append(row)
            sessions = [self._session(r, state.get(str(r["id"]).lower())) for r in rows]
        except Exception as exc:  # 兜底：任何意外都不抛给上层
            self.last_error = friendly_error(exc, "读 Codex 会话失败（codex list）")
            return []
        live = [s for s in sessions if s.owner != "none"]
        rest = [s for s in sessions if s.owner == "none"]
        live.sort(key=lambda s: s.updated_at, reverse=True)
        rest.sort(key=lambda s: s.updated_at, reverse=True)
        # 在跑的全部留着，limit 只截历史（和 Claude 一致；原来在跑的超过 limit 也会被截掉）
        return live + rest[:limit]

    def running(self) -> list:
        """被占着的会话（App / 终端 / 桥自己）。没有 threads 行的也列出来，标题留空。"""
        self.last_error = ""
        try:
            state = self._state()
            out = []
            for tid, st in state.items():
                rows = self._query("id = ?", (tid,), limit=1, include_archived=True, skip_subagents=False)
                row = rows[0] if rows else {"id": tid}
                if _is_subagent(row):
                    continue  # 子 agent 跟着父会话走，不单独列
                out.append(self._session(row, st))
        except Exception as exc:
            self.last_error = friendly_error(exc, "读 Codex 会话失败（codex running）")
            return []
        out.sort(key=lambda s: s.updated_at, reverse=True)
        return out

    def read(self, id: str, n: int = 4) -> list:
        """最近 n 条用户 / 助手的话（跳过推理、工具调用、系统塞的上下文）。"""
        self.last_error = ""
        if not is_thread_id(id):
            self.last_error = "会话编号不对。"
            return []
        n = max(1, int(n))
        try:
            row = self._row(id.lower())
            segs = self._segments(id.lower(), row)
            if not segs:
                self.last_error = "没找到这个会话的对话记录（可能已归档或删掉了）。"
                return []
            collected: list = []
            for seg in segs[:READ_MAX_SEGMENTS]:
                size = READ_TAIL_BYTES
                while True:
                    lines, whole = _tail_lines(seg, size)
                    msgs = [m for m in (_message_of(r) for r in _records(lines)) if m is not None]
                    if len(msgs) >= n or whole or size >= READ_TAIL_MAX_BYTES:
                        break
                    size *= 4
                collected = msgs + collected
                if len(collected) >= n:
                    break
            return collected[-n:]
        except Exception as exc:
            self.last_error = friendly_error(exc, "读 Codex 对话失败")
            return []

    def _codex(self) -> str:
        if self.codex_bin:
            return self.codex_bin
        found = shutil.which("codex")
        if found:
            return found
        for cand in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", os.path.expanduser("~/.local/bin/codex")):
            if os.path.exists(cand):
                return cand
        return "codex"

    def _env(self, codex: str) -> dict:
        env = dict(os.environ)
        # codex 是 `#!/usr/bin/env node` 脚本；后台服务的 PATH 常常没有 node 所在目录
        extra = [os.path.dirname(codex)] if os.path.isabs(codex) else []
        extra += ["/opt/homebrew/bin", "/usr/local/bin"]
        parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        env["PATH"] = os.pathsep.join(parts + [p for p in extra if p and p not in parts])
        if not self._default_home:
            env["CODEX_HOME"] = self.home  # 读和发看同一份数据
        return env

    def send(self, id: str, text: str, timeout: float = 600, mode: Optional[str] = None) -> ActionResult:
        """没人占着才发：`codex exec resume --json --skip-git-repo-check [-c sandbox_mode=…] -- <id> <text>`。

        `mode`：当前权限档位。给了就按档位压 sandbox（见 SANDBOX_BY_MODE）；认不出 ⇒ 只读。
        不给（None）= 用用户 config.toml 里那份（老调用方式，测试在用）。
        """
        if not is_thread_id(id):
            return ActionResult(False, "会话编号不对，发不了。", str(id or ""))
        tid = id.lower()
        text = str(text or "").strip()
        if not text or text == "-":  # 单独一个 `-` 会被 codex 当成「从 stdin 读」
            return ActionResult(False, "要发的话是空的。", tid)
        with self._send_guard:
            if tid in self._sending:
                return ActionResult(False, WHY_NOT_SEND_BRIDGE, tid)
            self._sending.add(tid)
        try:
            spawned: list = []
            res = self._send_locked(tid, text, timeout, mode, spawned)
            # 起过子进程 ⇒ 消息已经进了会话（不管后面超时 / 被停 / 报错）
            if spawned and not res.delivered:
                res = ActionResult(res.ok, res.text, res.session_id, delivered=True)
            return res
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, log_as=f"codex send 失败 tid={tid}"), tid)
        finally:
            with self._send_guard:
                self._sending.discard(tid)

    def _send_locked(self, tid: str, text: str, timeout: float, mode: Optional[str] = None,
                     spawned: Optional[list] = None) -> ActionResult:
        row = self._row(tid)
        if row is None:
            return ActionResult(False, self.last_error or "没找到这个 Codex 会话。", tid)
        if row.get("archived"):
            return ActionResult(False, WHY_NOT_SEND_ARCHIVED, tid)
        # 发之前现查一次：五分钟里 App / 终端可能刚打开它
        self.last_error = ""
        state = self._state([tid]).get(tid)
        if state is not None:
            sess = self._session(row, state)
            return ActionResult(False, sess.why_not_send or WHY_NOT_SEND_UNKNOWN, tid)
        cwd = str(row.get("cwd") or "")
        if not cwd or not os.path.isdir(cwd):
            return ActionResult(False, "这个会话的项目文件夹不在了，没法接着聊。", tid)

        codex = self._codex()
        argv = [codex, "exec", "resume", "--json", "--skip-git-repo-check"]
        if mode is not None:
            sandbox = SANDBOX_BY_MODE.get(mode, SANDBOX_BY_MODE["readonly"])
            if sandbox:
                argv += ["-c", f'sandbox_mode="{sandbox}"']
        argv += ["--", tid, text]
        spawned = spawned if spawned is not None else []
        timer_box: list = []
        fired = threading.Event()

        def on_spawn(pid: int) -> None:
            spawned.append(pid)
            self.jobs.register_spawned(pid, self.tool, tid)

            # 软超时：走 FleetJobs 发 SIGINT（包装脚本会转给原生进程，不留孤儿、不留写锁）
            def soft_stop() -> None:
                fired.set()
                self.jobs.signal(pid, _signal.SIGINT)

            timer = threading.Timer(max(1.0, float(timeout)), soft_stop)
            timer.daemon = True
            timer_box.append(timer)
            timer.start()

        try:
            res = self.runner.run(
                argv,
                timeout=max(1.0, float(timeout)) + STOP_GRACE_SEC,
                cwd=cwd,
                env=self._env(codex),
                input_text=None,  # stdin=DEVNULL，不关会卡在 Reading additional input from stdin
                on_spawn=on_spawn,
            )
        finally:
            for timer in timer_box:
                timer.cancel()
            for pid in spawned:
                self.jobs.forget(pid)

        reply, failure = parse_exec_output(res.stdout)
        if failure and not reply and "active writer" not in failure:
            # 第 32 轮：读到真失败（turn.failed / 顶层 error）⇒ 总览上这个会话标「⚠️ 出错了」。
            # 「被 App / 终端占着」不算它出错，是我们没发进去。
            self._note_turn_failed(tid)
        if res.error:
            # SubprocessRunner 的 error 自己就以「没能启动 …」开头 ⇒ 别再套一层
            return ActionResult(False, res.error if res.error.startswith("没能启动") else "没能启动 Codex：" + res.error, tid)
        if res.timed_out or (fired.is_set() and not (res.returncode == 0 and reply)):
            return ActionResult(False, "Codex 太久没回完，已经让它停下了。", tid)
        blob = (res.stderr or "") + "\n" + (failure or "")
        if "active writer" in blob:
            return ActionResult(False, "它刚刚被 Codex App 或终端打开了，现在发不进去。", tid)
        if res.returncode != 0 or (failure and not reply):
            err_lines = (res.stderr or "").strip().splitlines()
            detail = short_title(failure or (err_lines[-1] if err_lines else ""), 120)
            if res.returncode in (-2, 130) and not detail:
                return ActionResult(False, "已经停下了。", tid)
            return ActionResult(False, "Codex 没回成功" + (f"：{detail}" if detail else "。"), tid)
        if not reply:
            return ActionResult(True, "Codex 跑完了，但没有文字回复。", tid)
        return ActionResult(True, reply, tid)

    # ---------- start：手机上新建会话 ----------

    def start(self, cwd: str, text: str, timeout: float = 600, mode: Optional[str] = "trusted",
              model: str = "", on_pid: Optional[Callable[[int], None]] = None) -> ActionResult:
        """在 `cwd` 新开一个 Codex 会话：`codex exec --json --skip-git-repo-check -c sandbox_mode=… -- <text>`。

        手机入口和终端入口共用当前权限档位；默认 trusted，对应工作区内可写。
        新会话的 id 从 `thread.started` 事件里拿；拿不到就当没开成（没法接着聊）。
        """
        text = str(text or "").strip()
        if not text or text == "-":
            return ActionResult(False, "要发的话是空的。", "")
        if not cwd or not os.path.isdir(cwd):
            return ActionResult(False, "这个项目目录不在了，换一个再开。", "")
        codex = self._codex()
        sandbox = SANDBOX_BY_MODE.get(mode or "trusted", SANDBOX_BY_MODE["trusted"])
        argv = [codex, "exec", "--json", "--skip-git-repo-check", "-c", f'sandbox_mode="{sandbox}"']
        if model:
            if not safe_model_name(model):
                return ActionResult(False, f"模型名不对：{short_title(str(model), 40)}", "")
            argv += ["-m", model]
        argv += ["--", text]
        spawned: list = []
        timer_box: list = []
        fired = threading.Event()

        def on_spawn(pid: int) -> None:
            spawned.append(pid)
            self.jobs.register_spawned(pid, self.tool, "")
            if on_pid is not None:
                try:
                    on_pid(pid)
                except Exception:  # noqa: BLE001
                    pass

            def soft_stop() -> None:
                fired.set()
                self.jobs.signal(pid, _signal.SIGINT)

            timer = threading.Timer(max(1.0, float(timeout)), soft_stop)
            timer.daemon = True
            timer_box.append(timer)
            timer.start()

        try:
            res = self.runner.run(argv, timeout=max(1.0, float(timeout)) + STOP_GRACE_SEC, cwd=cwd,
                                  env=self._env(codex), input_text=None, on_spawn=on_spawn)
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, "没能启动 Codex"), "")
        finally:
            for timer in timer_box:
                timer.cancel()
            for pid in spawned:
                self.jobs.forget(pid)
        if res.error:
            return ActionResult(False, res.error if res.error.startswith("没能启动") else "没能启动 Codex：" + res.error, "")
        tid = thread_id_from_exec_output(res.stdout)
        reply, failure = parse_exec_output(res.stdout)
        if not tid:
            err_lines = (res.stderr or "").strip().splitlines()
            detail = short_title(failure or (err_lines[-1] if err_lines else ""), 120)
            return ActionResult(False, "Codex 没开成新会话" + (f"：{detail}" if detail else "。"), "")
        if res.timed_out or (fired.is_set() and not (res.returncode == 0 and reply)):
            return ActionResult(False, "Codex 太久没回完，已经让它停下了。会话已经开了，可以接着说。", tid, delivered=True)
        if res.returncode != 0 or (failure and not reply):
            self._note_turn_failed(tid)
            detail = short_title(failure or "", 120)
            return ActionResult(False, "Codex 没回成功" + (f"：{detail}" if detail else "。"), tid, delivered=True)
        return ActionResult(True, reply or "Codex 跑完了，但没有文字回复。", tid, delivered=True)

    # ---------- relay：进会话聊天 ----------

    @property
    def queue_db_path(self) -> str:
        return os.path.join(self.home, "queue_1.sqlite")

    def pending_queued(self, thread_id: str) -> Optional[int]:
        """这个线程还有几条排在 `queue_1.sqlite` 里（只读）。读不了返回 None。

        用处只有一个：等不到回复时判断「排上了但没人在听」还是「有人在听只是慢」。
        和 `state_5.sqlite` 一样 `?mode=ro`，**绝不写**。
        """
        if not is_thread_id(thread_id) or not os.path.exists(self.queue_db_path):
            return None
        try:
            uri = "file:" + urllib.parse.quote(self.queue_db_path) + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SEC)
        except sqlite3.Error:
            return None
        try:
            row = conn.execute(
                "select count(*) from queued_items where thread_id = ?", (thread_id.lower(),)
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            conn.close()
        return int(row[0]) if row else 0

    def _tail_marks(self, segs: list) -> dict:
        """{分段路径: 当前字节数}，发之前记一次，之后只看新增的部分。"""
        marks = {}
        for path in segs:
            try:
                marks[path] = os.path.getsize(path)
            except OSError:
                marks[path] = 0
        return marks

    def _new_records(self, tid: str, marks: dict) -> list:
        """自 `marks` 以来新写进去的记录（含新开的分段），按时间顺序（旧分段在前）。"""
        out: list = []

        def mtime(path: str) -> float:
            try:
                return os.path.getmtime(path)
            except OSError:
                return 0.0

        for path in sorted(self._segments(tid), key=mtime):
            start = marks.get(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if start is None:  # 发之后新开的分段：整段都是新的
                start = 0
            if size <= start:
                continue
            # 🔴 续读时**不能无条件丢第一行**：mark 多半正好落在换行之后（上一次读到的是
            # 完整的最后一行），丢掉就等于把回复的第一条记录吃掉。往前退一个字节看一眼：
            # 是换行 ⇒ 正好在行首，一行都不丢；不是 ⇒ 前面那行被切断了，丢掉这半截。
            seek = max(0, start - 1)
            try:
                with open(path, "rb") as fh:
                    fh.seek(seek)
                    data = fh.read(min(size - seek, RELAY_TAIL_BYTES * 8))
            except OSError:
                continue
            if start > 0:
                at_boundary = data[:1] == b"\n"
                data = data[1:]
                lines = data.split(b"\n")
                if not at_boundary and lines:
                    lines = lines[1:]
            else:
                lines = data.split(b"\n")
            out.extend(_records(lines))
        return out

    def relay(self, id: str, text: str, timeout: float = RELAY_TIMEOUT_SEC,
              mode: Optional[str] = None, poll: float = RELAY_POLL_SEC,
              min_timeout: float = RELAY_MIN_TIMEOUT_SEC) -> ActionResult:
        """把一句话送进一个**活着的** Codex 会话，并把回复取回来。

        - 别人（App / 终端）占着它 ⇒ `codex queue`，然后盯目标自己的 rollout 等回复。
        - 没人占着 ⇒ 还是 `exec resume`（同步拿回复，不用等那约 10 秒的轮询）。
        - **幂等**：超时绝不自动重发（对方可能已经在做了），只如实说「排进去了、还没回」。

        `min_timeout`：等回复的下限，默认 `RELAY_MIN_TIMEOUT_SEC`（约 10 秒的轮询周期决定的，
        再短等于必然超时）。**只有测试会把它调小**，线上别传。
        """
        if not is_thread_id(id):
            return ActionResult(False, WHY_RELAY_BAD_ID, str(id or ""))
        tid = id.lower()
        text = str(text or "").strip()
        if not text or text == "-":
            return ActionResult(False, WHY_RELAY_EMPTY, tid)
        with self._send_guard:
            if tid in self._sending:
                return ActionResult(False, WHY_RELAY_BUSY, tid)
            self._sending.add(tid)
        try:
            spawned: list = []
            res = self._relay_locked(tid, text, timeout, mode, spawned, poll, min_timeout)
            if spawned and not res.delivered:
                res = ActionResult(res.ok, res.text, res.session_id, delivered=True)
            return res
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, log_as=f"codex relay 失败 tid={tid}"), tid)
        finally:
            with self._send_guard:
                self._sending.discard(tid)

    def _relay_locked(self, tid: str, text: str, timeout: float, mode: Optional[str],
                      spawned: list, poll: float,
                      min_timeout: float = RELAY_MIN_TIMEOUT_SEC) -> ActionResult:
        self.last_error = ""
        row = self._row(tid)
        if row is None:
            return ActionResult(False, self.last_error or WHY_RELAY_NOT_FOUND, tid)
        if row.get("archived"):
            return ActionResult(False, WHY_RELAY_ARCHIVED, tid)
        state = self._state([tid]).get(tid)
        owner = state[0] if state else "none"
        if owner == "bridge":
            return ActionResult(False, WHY_RELAY_BUSY, tid)
        if owner == "none":
            # 没人占着 ⇒ 直接 exec resume，同步拿回复（比排队等轮询快）
            return self._send_locked(tid, text, timeout, mode, spawned)
        segs = self._segments(tid, row)
        if not segs:
            # 实测：没有 rollout 的线程 `codex queue` 直接报 -32603，先说清楚别白跑
            return ActionResult(False, WHY_RELAY_NO_ROLLOUT, tid)
        cwd = str(row.get("cwd") or "")
        marks = self._tail_marks(segs)
        codex = self._codex()
        # `--thread=` / `--message=` 用等号形式：手机上发一句以 `-` 开头的话不能被当成旗标
        argv = [codex, "queue", f"--thread={tid}", f"--message={text}"]
        try:
            res = self.runner.run(
                argv,
                timeout=QUEUE_CMD_TIMEOUT_SEC,
                cwd=cwd if cwd and os.path.isdir(cwd) else None,
                env=self._env(codex),
                input_text=None,
            )
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, "没能把这句排进去"), tid)
        if res.error:
            # SubprocessRunner 的 error 自己就以「没能启动 …」开头 ⇒ 别再套一层
            return ActionResult(False, res.error if res.error.startswith("没能启动") else "没能启动 Codex：" + res.error, tid)
        if res.timed_out:
            return ActionResult(False, "把这句排进 Codex 队列时卡住了，没排上。", tid)
        blob = (res.stderr or "") + "\n" + (res.stdout or "")
        if res.returncode != 0:
            if "no rollout found" in blob:
                return ActionResult(False, WHY_RELAY_NO_ROLLOUT, tid)
            detail = short_title(blob.strip().splitlines()[-1] if blob.strip() else "", 120)
            return ActionResult(False, "没排进去" + (f"：{detail}" if detail else "。"), tid)
        # 行写进去了 ⇒ 从这里开始，一律 delivered=True：绝不说「没发进去」（用户会重发）
        return self._await_reply(tid, text, marks, timeout, poll, min_timeout)

    def _await_reply(self, tid: str, text: str, marks: dict, timeout: float,
                     poll: float, min_timeout: float = RELAY_MIN_TIMEOUT_SEC) -> ActionResult:
        """盯目标自己的 rollout：先等我们这句的 user 回显（真送达），再等它后面的回复。"""
        limit = max(float(min_timeout or 0.0), float(timeout or RELAY_TIMEOUT_SEC))
        step = max(0.05, float(poll or RELAY_POLL_SEC))
        deadline = time.monotonic() + limit
        echoed = False
        reply = ""
        while True:
            # 🔴 每轮都从 marks 整段重算，不留上一轮的 echoed / reply：目标可能先把**它自己
            # 上一轮**写完（assistant + task_complete）才来取我们这句。状态跨轮累积的话，
            # 上一轮的回复会被当成这句的回答返回给用户。
            echoed, reply, done = scan_relay_records(self._new_records(tid, marks), text)
            if done == "aborted":
                return ActionResult(False, RELAY_ABORTED, tid, delivered=True)
            if done == "complete" and reply:
                return ActionResult(True, reply, tid, delivered=True)
            if time.monotonic() >= deadline:
                break
            time.sleep(min(step, max(0.0, deadline - time.monotonic())))
        if reply:  # 看到回复了但没等到它说完：把看到的给出去，并说实话
            return ActionResult(True, reply, tid, delivered=True)
        if echoed:
            return ActionResult(False, RELAY_DELIVERED_NO_TEXT, tid, delivered=True)
        pending = self.pending_queued(tid)
        if pending:
            return ActionResult(False, RELAY_QUEUED_NOBODY_HOME, tid, delivered=True)
        return ActionResult(False, RELAY_QUEUED_NO_REPLY, tid, delivered=True)

    def relay_stop(self, id: str) -> ActionResult:
        """relay 这条路上的 `/stop`：实测停不了，说清楚在哪停，别假装能停。"""
        tid = str(id or "").lower()
        if not is_thread_id(tid):
            return ActionResult(False, WHY_RELAY_BAD_ID, str(id or ""))
        if self.jobs.jobs(tool=self.tool, session_id=tid):
            return self.stop_own(tid)  # 桥自己起的 exec resume 能停
        try:
            state = self._state([tid]).get(tid)
        except Exception:
            state = None
        owner = state[0] if state else "none"
        if owner == "app":
            return ActionResult(False, WHY_RELAY_STOP_APP, tid)
        if owner == "terminal":
            return ActionResult(False, WHY_RELAY_STOP_TERMINAL, tid)
        if self.pending_queued(tid):
            return ActionResult(False, WHY_RELAY_NO_UNSEND, tid)
        return ActionResult(False, WHY_NOT_STOP_IDLE, tid)

    def stop(self, id: str) -> ActionResult:
        """只停桥自己起的进程；别人的说清楚为什么不能停。"""
        if not is_thread_id(id):
            return ActionResult(False, "会话编号不对。", str(id or ""))
        tid = id.lower()
        if self.jobs.jobs(tool=self.tool, session_id=tid):
            return self.stop_own(tid)
        try:
            state = self._state([tid]).get(tid)
        except Exception:
            state = None
        owner = state[0] if state else "none"
        if owner == "app":
            return ActionResult(False, WHY_NOT_STOP_APP, tid)
        if owner == "terminal":
            return ActionResult(False, WHY_NOT_STOP_TERMINAL, tid)
        return ActionResult(False, WHY_NOT_STOP_IDLE, tid)


def thread_id_from_lock(path: str) -> str:
    name = os.path.basename(str(path or ""))
    tid = name[:-5] if name.endswith(".lock") else ""
    return tid.lower() if is_thread_id(tid) else ""


def scan_relay_records(records: list, sent: str) -> tuple:
    """rollout 新增记录 → (我们这句回显到了吗, 它之后的回复, ""|"complete"|"aborted")。

    判据顺序是实测给的：**先等 user 回显**（这才是「真送达」），回显之前出现的 assistant /
    `task_complete` 都属于目标**上一轮**，一概不算我们的答案。
    """
    wanted = " ".join(str(sent or "").split())
    echoed = False
    reply = ""
    done = ""
    for rec in records:
        if not isinstance(rec, dict):
            continue
        msg = _message_of(rec)
        if not echoed:
            if msg is not None and msg.role == "user" and wanted and wanted in " ".join(msg.text.split()):
                echoed = True
            continue
        if msg is not None and msg.role == "assistant":
            reply = msg.text
            continue
        if rec.get("type") == "event_msg":
            kind = (rec.get("payload") or {}).get("type")
            if kind == "turn_aborted":
                return echoed, reply, "aborted"
            if kind == "task_complete":
                done = "complete"
    return echoed, reply, done


def parse_exec_output(stdout: str) -> tuple:
    """`codex exec --json` 的 JSONL → (最后一条 agent_message 文本, 失败说明)。

    `item.completed` 里 type=error 的是警告（服务档位、skills 预算），不算失败。
    真失败看 `turn.failed` / 顶层 `error`。
    """
    reply, failure = "", ""
    for raw in (stdout or "").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message" and str(item.get("text") or "").strip():
                reply = str(item["text"]).strip()
        elif kind == "turn.failed":
            err = ev.get("error") or {}
            failure = str(err.get("message") if isinstance(err, dict) else err or "回合失败")
        elif kind == "error":
            failure = str(ev.get("message") or "出错了")
    return reply, failure


def thread_id_from_exec_output(stdout: str) -> str:
    """`codex exec --json` 第一条 `thread.started` 里的 thread_id；没有 / 形状不对 ⇒ 空串。"""
    for raw in (stdout or "").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if isinstance(ev, dict) and ev.get("type") == "thread.started":
            tid = ev.get("thread_id")
            return tid.lower() if is_thread_id(tid) else ""
    return ""


_MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")


def safe_model_name(name: Any) -> bool:
    """模型名只认字母数字和 `._:/-`，不以 `-` 开头（不会被当成参数）。"""
    return isinstance(name, str) and bool(_MODEL_NAME_RE.match(name))
