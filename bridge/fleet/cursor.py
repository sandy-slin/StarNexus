"""Cursor：列 / 读 / 发 / 停。

## 能做什么（依据 `docs/agent-control-design.md`，2026-11-04 实测）

- **列**：命令行对话读 `~/.cursor/chats/<md5(cwd)>/<id>/meta.json`；编辑器里的对话读
  `state.vscdb` 的 composerHeaders（`?mode=ro` 只读打开）。**绝不碰 cursorDiskKV** ——
  那张表 8GB+，全扫一次 19 秒，机器人整个卡死。
- **读**：两种对话共用 `~/.cursor/projects/*/agent-transcripts/<id>/<id>.jsonl`，**按 id 找**。
  目录名不能自己算：命令行用的是 `--workspace` 原样字符串，chats 用的是 realpath，两边对不上。
- **发**：**只发命令行对话**，`cursor-agent --resume <id> -p`。实测同一个对话两条并发都报成功、
  第一条静默丢掉 ⇒ 按对话加锁串行；电脑上有别的 cursor-agent 开着它（或认不出开的是哪个）⇒ 不发。
- **停**：只停自己起的（`FleetJobs`）。

## 不做的

- 编辑器里的对话：发不了、停不了（Cursor 没给外部程序留口子），在不在跑也看不出 ⇒ 状态写 unknown
- 不读 `chats/*/*/store.db`：它的 meta 里有 blobEncryptionKey；标题改从 transcript 第一句话取
- 不写任何 Cursor 的文件；不用 `cursor-agent ls`（没 tty 会卡死，选择器里按退格还会删对话）

## worker-server

`cursor-agent -p` 回复完会留下一个 `index.js worker-server`。
发送期间盯着我们那个子进程的子孙，看到 worker 就记下 pid + 启动时间；发完再核一遍
（还是那个进程、cwd 就是这个对话的目录、启动不早于我们的进程）才收。哪一条对不上就留着、
记日志 —— 多留一个空转的 worker 只费点内存，收错了是把别人的进程杀了。

**2026-11-04 复核：这里会漏收，而且是故意留着漏的。** 实测两条：

- worker 是**每个 workspace 一个**，不是每次发送一个（7 轮发送只留下 1 个 worker）；
- 它的 `ppid` **一出生就是 1**，不是先挂在我们子进程下面再被收养 ⇒ 走子孙树**可能一个都抓不到**。

所以现在的净效果是「偏安全但会漏收」。**不改成按 cwd + 启动时间认领**：那是比「它是我们子进程的
子孙」弱得多的证据，同一个项目目录下别人也可能起 cursor-agent，认错一次就是把别人的进程杀了
（红线第一条）。漏收的代价是每个项目目录多一个空转进程（有上限、只费点内存），
而且实测杀不杀它对速度没影响（冷启 8.96 秒 vs 热 8.12~10.4 秒，在噪声内）。
两边一比，宁可漏收。

## 进会话聊天（`relay()`，2026-11-04 实测复核）

- **多轮续得上**：同一个 chat 连发 7 轮，`--resume` 每轮都是同一个 `session_id`，
  第 1 轮让它记 4719 → 第 2 轮答 4719 → 第 3 轮答 4720。延迟 5.8~10.4 秒
  （其中 `duration_ms` 只占 3.0~6.7 秒，固定开销约 3 秒）⇒ **不需要常驻信使**。
- **并发不再静默丢**（2026.10.22 实测）：同一个 chat 两条并发，输家 0.35~0.47 秒就 rc=1
  响亮失败，stderr 是 `Persistent chat ownership is currently changing for <id>`。
  **但 per-chat 锁照样留着**：旧版本是静默丢；而且输家那句报错给手机用户看莫名其妙，
  `WHY_BUSY_SEND` 比它好。不同 chat 可以并行（实测两个 chat 同时发，两条都 rc=0）⇒
  锁的粒度就该是 chatId。
- **取回复优先用 `--output-format json` 的 `result`**（同步就在 stdout 里）；
  它没给文字时才退回去读 transcript。

### 🔴 `turn_ended` 不能当「这一轮完了」的信号

- 假设：每轮结束追加一条 `turn_ended`，盯新增的就知道这轮完了。
- 证伪条件：若成立，7 轮之后该有 7 条 `turn_ended`，且文件只增不改。
- 数据：7 轮后 `grep -c turn_ended` = **1**。字节级轮询抓到 `turn_ended` 那一行**被下一轮的
  user 行覆盖**（`...\n{"type":"turn_ended",...}\n` → `...\n{"role":"user",...`）。

**CLI 的 transcript 是整文件重写的** ⇒ 两条纪律：(1) 不拿 `turn_ended` 当完成判据；
(2) **不能用字节偏移续读**（偏移会指到被重写过的内容里）。读回复一律重读尾部、
**按内容**找到我们这句的 user 回显，再取它后面的 assistant 文字。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
import signal as _signal
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .model import (
    ActionResult,
    Adapter,
    FleetJobs,
    Message,
    ProcTable,
    RunResult,
    Session,
    SubprocessRunner,
    friendly_error,
    parse_lstart,
    short_title,
    resolve_lsof,
)

logger = logging.getLogger("feishu-bridge.fleet.cursor")

DEFAULT_CURSOR_HOME = os.path.expanduser("~/.cursor")
DEFAULT_APP_USER = os.path.expanduser("~/Library/Application Support/Cursor/User")

SQLITE_TIMEOUT_SEC = 1.0
LSOF_TIMEOUT_SEC = 3.0
READ_TAIL_BYTES = 512 * 1024  # 读最近几条：尾部半兆足够，transcript 只有文字
TITLE_HEAD_BYTES = 64 * 1024  # 取标题：只看开头，第一句用户话就在最前面
MAX_SEND_CHARS = 20000
WORKER_JOB_TOOL = "cursor-worker"  # 回收 worker-server 时临时登记用，不是对话
WORKER_POLL_SEC = 1.0  # 发送期间多久看一次 ps 找 worker-server

# 对话 id：UUID 那样的字母数字和横线。它要进路径和 argv，别的字符一律不认（防 ../ 和奇怪参数）
_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{3,127}$")
# 外来进程：`~/.local/bin/cursor-agent` 是 `exec -a "$0" node .../versions/<ver>/index.js "$@"`
_AGENT_RE = re.compile(r"/cursor-agent/versions/[^/\s]+/index\.js(?:\s|$)")
# 不占对话的子命令（跑完就退）
_NONCHAT_WORDS = frozenset({
    "create-chat", "login", "logout", "status", "whoami", "update", "upgrade", "mcp",
    "--list-models", "-v", "--version", "-h", "--help", "install-shell-integration",
    "uninstall-shell-integration", "about",
})

# ---------- 给手机用户看的话 ----------

WHY_IDE_SEND = (
    "编辑器里的对话，手机这边送不进去 —— 要接着聊，在电脑上打开 Cursor 点开它，"
    "或者用 cursor-agent 开一个对话，那种手机上就能发。"
)
WHY_IDE_STOP = "编辑器里的对话请在 Cursor 里点停止，手机这边停不了它。"
WHY_TERMINAL_SEND = (
    "它正开在电脑上的 cursor-agent 里。这时候再发，两边会抢着写，其中一条会被悄悄丢掉；"
    "等那边关掉这个对话，再点 🔄 刷新就能从手机接着聊。"
)
WHY_TERMINAL_STOP = "它开在电脑上的终端里，从外面停会直接关掉整个会话，请在终端里停。"
WHY_MAYBE_SEND = (
    "电脑上有个 cursor-agent 开在同一个项目里，认不出它开的是不是这个对话。"
    "为了不把话弄丢，先不发；关掉它再试，或者新开一个对话。"
)
WHY_UNSEEN_SEND = "这会儿看不到电脑上的进程，没法确认这个对话有没有开着，先不发。"
WHY_BUSY_SEND = "手机上发的上一条还没回完，等它回完再发。"
WHY_IDLE_STOP = "它现在没在跑，不用停。"
WHY_NO_BIN = "这台电脑上找不到 cursor-agent，装好之后才能从手机上接着聊。"
WHY_NO_CWD = "这个对话的项目文件夹已经不在了，发不了。"
WHY_FOREIGN_UNKNOWN = "电脑上开着一个 cursor-agent，认不出是哪个对话，只能看不能动。"

UNKNOWN_IDE_NOTE = "编辑器里的对话现在在不在跑看不出来（Cursor 没把这个状态写到能读的地方）。"

# ---------- relay（进会话聊天） ----------

RELAY_TIMEOUT_SEC = 120.0  # 实测一轮 5.8~10.4 秒，120 秒够它干点活
RELAY_MIN_TIMEOUT_SEC = 20.0  # 比这还短等于必然超时（固定开销就约 3 秒）
RELAY_EMPTY_REPLY = "（Cursor 这一轮没有文字回复）"
# 上游并发输家的原话（实测 2026.10.22）。认出来 ⇒ 换成人话，别把这句甩给手机用户
_OWNERSHIP_CHANGING = "ownership is currently changing"
WHY_RELAY_RACE = (
    "电脑上有人同时在用这个对话，Cursor 把手机这条挡掉了（没发进去，也没弄乱）。"
    "等那边停下来，再点 🔄 刷新重发一次。"
)

# 🔴 编辑器（IDE）里的对话：外部送不进去，**这一轮也不去试**。
# 不写 `state.vscdb`、不做 UI 自动化、不用深链（深链只能开新对话，还要人点一下）。
#
# 🔴 2026-11-07 第 17 轮纠正：原来这句写的是「Cursor 没给其他程序留这个口子」，**那是假话**。
# 官方钩子（`stop` / `subagentStop`）的返回值 `followup_message` 原文是「Cursor will
# automatically submit it as the next user message」，而且每个钩子都收到 `conversation_id`
# —— 往已有对话追加一句的口子**是有的**，官方文档就写着。
# 真正的拦路理由是**时机**：那个钩子只在一轮快结束时由 Cursor 自己叫起来，
# 公开文档里没有任何定时 / 监听 / 推送入口能从外面叫它（timer、idle、poll、webhook、
# listen、daemon 一个都没有；应用级钩子只有「打开工作区」那一个）。
# ⇒ 对一个**闲着的**编辑器对话，手机这边依然送不进去。结论没变，理由变了。
# 把假话留着的代价：下一轮谁想做「手机催一句」，读到那句会直接判定无路可走。
WHY_RELAY_IDE = (
    "编辑器里的对话，手机这边送不进去（也不会去动它的记录或者替你点界面）。"
    "想接着聊：在电脑上打开 Cursor 点开这个对话，或者在终端里用 cursor-agent 开一个，"
    "那种手机上就能发。"
)
# `cursor-agent --resume <编辑器那种 id>`：第 17 轮用**一次性对话**实测出结论了（没碰用户的）。
# 两件事：(1) 接不上编辑器那份历史，会用同一个 id 另起一个空的命令行对话；
# (2) 会把两边共用的那份导出记录**整份重写**（哨兵 276 B → 139 B，sha 变回报错那一行）。
# 闸门仍然在 `_cli_meta`：编辑器的 id 不在 `~/.cursor/chats/` 下 ⇒ 认不出 ⇒ 根本不会去 resume。
WHY_RELAY_IDE_RESUME_UNTESTED = (
    "拿命令行去接编辑器里那个对话，接不上 —— 它只会另起一个空的，还会把那边的聊天记录重写掉。"
    "所以手机这边不碰它。"
)

# ---------- 小工具 ----------


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _ro_uri(path: str) -> str:
    """只读打开 sqlite。`mode=ro` 会读 WAL（看得到最近几秒）；`immutable=1` 会漏，所以不用。"""
    return "file:" + urllib.parse.quote(os.path.abspath(path)) + "?mode=ro"


def _read_json_file(path: str, limit: int = 64 * 1024) -> Optional[dict]:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(limit + 1)
    except OSError:
        return None
    if len(raw) > limit:  # meta.json 只有几行，大成这样肯定不是它
        return None
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _tail_lines(path: str, nbytes: int) -> list:
    """读文件尾部 nbytes；没从文件头开始读时丢掉第一行（多半是半截）。"""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - nbytes)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return []
    lines = raw.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]
    return [ln.decode("utf-8", errors="replace") for ln in lines if ln.strip()]


def _head_lines(path: str, nbytes: int) -> list:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(nbytes)
    except OSError:
        return []
    lines = raw.split(b"\n")
    if len(raw) >= nbytes and lines:
        lines = lines[:-1]  # 最后一行可能被截断
    return [ln.decode("utf-8", errors="replace") for ln in lines if ln.strip()]


def _ms_to_epoch(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num <= 0:
        return 0.0
    return num / 1000.0 if num > 1e11 else num


def _file_uri_to_path(uri: Any) -> str:
    """`file:///Users/x/My%20Proj` → `/Users/x/My Proj`；不是本机文件夹（ssh 远程等）返回空。"""
    if not isinstance(uri, str) or not uri.startswith("file://"):
        return ""
    parsed = urllib.parse.urlparse(uri)
    return urllib.parse.unquote(parsed.path or "")


def _same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return False


# ---------- transcript 解析 ----------

_TIMESTAMP_RE = re.compile(r"<timestamp>(.*?)</timestamp>", re.S)
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_TS_TEXT_RE = re.compile(
    r"^\s*\w+,\s*(\w{3})\w*\s+(\d{1,2}),\s*(\d{4}),\s*(\d{1,2}):(\d{2})\s*([AP]M)"
    r"(?:\s*\(UTC([+-]\d{1,2})(?::?(\d{2}))?\))?",
    re.I,
)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1
)}
# 不是人说的话：Cursor 自己塞进对话的东西（工具目录、上下文压缩的摘要）
_INJECTED_PREFIXES = ("<mcp_server_catalog>", "<mcp_meta_tool_server", "<system_reminder>", "<system-reminder>")
_SUMMARY_PREFIX = "This session is being continued from a previous conversation"
SUMMARY_NOTE = "（之前聊得太长，Cursor 自动压缩成了一段摘要）"
_TASK_NUDGE_PREFIX = "Briefly inform the user about the task result"


def _parse_ts_text(text: str) -> float:
    """`Sunday, Nov 1, 2026, 7:27 PM (UTC+8)` → epoch；认不出返回 0。"""
    m = _TS_TEXT_RE.match(text or "")
    if not m:
        return 0.0
    mon, day, year, hh, mm, ampm, off_h, off_m = m.groups()
    month = _MONTHS.get(mon[:3].lower())
    if month is None:
        return 0.0
    try:
        hour = int(hh) % 12 + (12 if ampm.upper() == "PM" else 0)
        offset = _dt.timedelta(hours=int(off_h or 0))
        if off_m:
            offset += _dt.timedelta(minutes=int(off_m)) * (1 if int(off_h or 0) >= 0 else -1)
        stamp = _dt.datetime(int(year), month, int(day), hour, int(mm), tzinfo=_dt.timezone(offset))
    except (ValueError, OverflowError):
        return 0.0
    return stamp.timestamp()


def _text_parts(record: dict) -> str:
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
    return "\n".join(t for t in texts if t.strip())


def clean_user_text(text: str) -> tuple:
    """去掉 `<timestamp>` / `<user_query>` 包装 → (正文, 时间)。不是人说的话 ⇒ 正文为空。"""
    at = 0.0
    stamp = _TIMESTAMP_RE.search(text)
    if stamp:
        at = _parse_ts_text(stamp.group(1))
    query = _USER_QUERY_RE.search(text)
    if query:
        body = query.group(1).strip()
        # Cursor 后台任务跑完后自己塞的一句「去告诉用户结果」，也包在 user_query 里（实测 201 次）
        return ("" if body.startswith(_TASK_NUDGE_PREFIX) else body), at
    body = _TIMESTAMP_RE.sub("", text).strip()
    if body.startswith(_INJECTED_PREFIXES):
        return "", at
    if body.startswith(_SUMMARY_PREFIX):
        return SUMMARY_NOTE, at
    return body, at


def parse_transcript_line(line: str) -> Optional[Message]:
    """一行 transcript → Message；工具调用、turn_ended、坏行 ⇒ None。"""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    role = record.get("role")
    if role not in ("user", "assistant"):
        return None
    text = _text_parts(record)
    at = 0.0
    if role == "user":
        text, at = clean_user_text(text)
    text = text.strip()
    if not text:
        return None
    return Message(role=role, text=text, at=at)


def reply_after_echo(lines, sent: str) -> str:
    """transcript 的行 → 我们这句（`sent`）之后的 assistant 文字；没找到回显 ⇒ 空串。

    回显之前的 assistant 行属于**上一轮**，一概不要 —— 不然用户会把上一轮的回答
    当成这句的答案。同一句话发过两次时取**最后一次**回显后面的。
    """
    wanted = " ".join(str(sent or "").split())
    if not wanted:
        return ""
    parsed = [parse_transcript_line(ln) for ln in lines or ()]
    start = -1
    for i, msg in enumerate(parsed):
        if msg is not None and msg.role == "user" and wanted in " ".join(msg.text.split()):
            start = i
    if start < 0:
        return ""
    out = []
    for msg in parsed[start + 1:]:
        if msg is None:
            continue
        if msg.role == "user":
            break  # 下一轮开始了
        out.append(msg.text)
    return "\n".join(out).strip()


def first_user_query(path: str) -> str:
    """transcript 开头第一句用户话，当标题用。"""
    for line in _head_lines(path, TITLE_HEAD_BYTES):
        msg = parse_transcript_line(line)
        if msg is not None and msg.role == "user" and msg.text != SUMMARY_NOTE:
            return msg.text
    return ""


# ---------- 进程 ----------


@dataclass(frozen=True)
class AgentProc:
    """电脑上一个 cursor-agent 对话进程（不是 worker-server）。"""

    pid: int
    chat_id: str  # 认不出 ⇒ ""
    printing: bool  # 带 -p：正在答一条；不带：交互界面，忙不忙看不出
    workspace: str  # argv 里的 --workspace；没有 ⇒ ""


def _agent_tokens(args: str) -> Optional[list]:
    """是 cursor-agent 主进程 ⇒ index.js 之后的参数；不是 ⇒ None。"""
    m = _AGENT_RE.search(args or "")
    if not m:
        return None
    return args[m.end():].split()


def is_worker_server(args: str) -> bool:
    tokens = _agent_tokens(args)
    return bool(tokens) and tokens[0] == "worker-server"


def parse_agent_args(pid: int, args: str) -> Optional[AgentProc]:
    """ps 的一行 args → AgentProc；worker-server、跑完就退的子命令 ⇒ None。

    参数是按空白拆的（ps 不保留引号），只用来认旗标；提示词里有空格不影响。
    """
    tokens = _agent_tokens(args)
    if tokens is None:
        return None
    if tokens and (tokens[0] == "worker-server" or tokens[0] in _NONCHAT_WORDS):
        return None
    if "--list-models" in tokens:
        return None
    chat_id = ""
    workspace = ""
    printing = False
    for i, tok in enumerate(tokens):
        if tok == "--":
            break
        if tok in ("-p", "--print"):
            printing = True
        elif tok == "--resume" and i + 1 < len(tokens) and valid_id(tokens[i + 1]):
            chat_id = tokens[i + 1]
        elif tok.startswith("--resume=") and valid_id(tok.split("=", 1)[1]):
            chat_id = tok.split("=", 1)[1]
        elif tok == "--workspace" and i + 1 < len(tokens):
            workspace = tokens[i + 1]
        elif tok.startswith("--workspace="):
            workspace = tok.split("=", 1)[1]
    return AgentProc(pid=int(pid), chat_id=chat_id, printing=printing, workspace=workspace)


def parse_lsof_cwd(text: str) -> dict:
    """`lsof -a -p 1,2 -d cwd -Fpn` 的输出 → {pid: cwd}。"""
    out: dict = {}
    pid = 0
    for line in (text or "").splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = 0
        elif line.startswith("n") and pid:
            out[pid] = line[1:]
    return out


def parse_result(stdout: str) -> Optional[dict]:
    """`--output-format json` 的结果对象（取最后一个能解析的 JSON 行）。"""
    for line in reversed((stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


# ---------- 按对话串行 ----------

_SEND_LOCKS: dict = {}
_SEND_LOCKS_GUARD = threading.Lock()


def _send_lock(key: str) -> threading.Lock:
    """全进程共用：同一个对话不管经过哪个 adapter 实例，同一时刻只有一条在发。"""
    with _SEND_LOCKS_GUARD:
        lock = _SEND_LOCKS.get(key)
        if lock is None:
            lock = _SEND_LOCKS[key] = threading.Lock()
        return lock


# 手机上的权限档位 → cursor-agent 旗标。`-p` 默认什么工具都能用（含 shell），
# 所以只读档必须显式收成 `--mode ask`；认不出的档位一律按只读（fail-closed，抄 policy.clamp_mode）。
MODE_FLAGS = {
    "readonly": ("--mode", "ask"),
    "write": (),
    "trusted": ("--force",),
}


@dataclass
class _ChatMeta:
    id: str
    cwd: str
    created_at: float
    updated_at: float
    has_conversation: bool
    path: str


@dataclass
class _WorkerWatch:
    """发送期间盯着我们那个子进程，记下它生出来的 worker-server。"""

    pid: int = 0
    lstart: str = ""
    seen: dict = field(default_factory=dict)  # worker pid -> Proc（第一次看到时）
    stop: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None


class CursorAdapter(Adapter):
    tool = "cursor"
    RUNNING_NOTE = UNKNOWN_IDE_NOTE

    def __init__(
        self,
        cursor_home: str = DEFAULT_CURSOR_HOME,
        app_user: str = DEFAULT_APP_USER,
        *,
        jobs: Optional[FleetJobs] = None,
        runner: Optional[SubprocessRunner] = None,
        agent_bin: Optional[str] = None,
        procs: Optional[Callable[[], ProcTable]] = None,
        mode: Any = "readonly",
        worker_poll_sec: float = WORKER_POLL_SEC,
    ) -> None:
        super().__init__(jobs=jobs, runner=runner)
        self.cursor_home = cursor_home
        self.app_user = app_user
        self._agent_bin = agent_bin
        self._procs_factory = procs
        self._mode = mode  # 字符串，或返回字符串的函数（跟着聊天里切的档位走）
        self.worker_poll_sec = worker_poll_sec

    # ---------- 路径 ----------

    @property
    def chats_dir(self) -> str:
        return os.path.join(self.cursor_home, "chats")

    @property
    def projects_dir(self) -> str:
        return os.path.join(self.cursor_home, "projects")

    @property
    def state_db(self) -> str:
        return os.path.join(self.app_user, "globalStorage", "state.vscdb")

    def agent_bin(self) -> str:
        """cursor-agent 在哪；后台服务的 PATH 常常很短，所以再看一眼默认安装位置。"""
        if self._agent_bin is not None:
            return self._agent_bin if os.path.isfile(self._agent_bin) else ""
        found = shutil.which("cursor-agent")
        if found:
            return found
        fallback = os.path.expanduser("~/.local/bin/cursor-agent")
        return fallback if os.path.isfile(fallback) else ""

    def _procs(self) -> ProcTable:
        if self._procs_factory is not None:
            try:
                return self._procs_factory()
            except Exception as exc:
                return ProcTable(error=friendly_error(exc, "读不到进程列表"))
        return ProcTable.snapshot(self.runner)

    def _current_mode(self, override: Optional[str] = None) -> str:
        """档位：字符串 / 返回字符串的函数 / 带 `.mode` 的 Policy 都行。认不出 ⇒ 只读。"""
        mode = override if override is not None else self._mode
        if callable(mode):
            try:
                mode = mode()
            except Exception:
                mode = "readonly"
        mode = getattr(mode, "mode", mode)
        return mode if isinstance(mode, str) and mode in MODE_FLAGS else "readonly"

    # ---------- 命令行对话：chats/*/<id>/meta.json ----------

    def _load_meta(self, path: str, chat_id: str) -> Optional[_ChatMeta]:
        data = _read_json_file(path)
        if data is None:
            return None
        cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
        return _ChatMeta(
            id=chat_id,
            cwd=cwd,
            created_at=_ms_to_epoch(data.get("createdAtMs")),
            updated_at=_ms_to_epoch(data.get("updatedAtMs")),
            has_conversation=data.get("hasConversation") is True,
            path=path,
        )

    def _cli_metas(self) -> list:
        """所有命令行对话（含没聊过的），不排序。两层 scandir，不递归。"""
        out = []
        try:
            with os.scandir(self.chats_dir) as groups:
                group_paths = [g.path for g in groups if g.is_dir(follow_symlinks=False)]
        except FileNotFoundError:
            return []
        except OSError as exc:
            self.last_error = friendly_error(exc, "读不到 Cursor 命令行对话")
            return []
        for group in group_paths:
            try:
                with os.scandir(group) as chats:
                    entries = [(c.name, c.path) for c in chats if c.is_dir(follow_symlinks=False)]
            except OSError:
                continue
            for name, path in entries:
                if not valid_id(name):
                    continue
                meta = self._load_meta(os.path.join(path, "meta.json"), name)
                if meta is not None:
                    out.append(meta)
        return out

    def _cli_meta(self, chat_id: str) -> Optional[_ChatMeta]:
        """按 id 找一个命令行对话（不知道它在哪个 md5 目录下，挨个看一眼）。"""
        if not valid_id(chat_id):
            return None
        try:
            with os.scandir(self.chats_dir) as groups:
                group_paths = [g.path for g in groups if g.is_dir(follow_symlinks=False)]
        except OSError:
            return None
        for group in group_paths:
            path = os.path.join(group, chat_id, "meta.json")
            if os.path.isfile(path):
                return self._load_meta(path, chat_id)
        return None

    # ---------- transcript：projects/*/agent-transcripts/<id>/<id>.jsonl ----------

    def find_transcripts(self, ids) -> dict:
        """按 id 找 transcript → {id: 路径}。不自己算目录名（命令行和 chats 用的路径写法不一样）。

        同一个 id 可能出现在两个项目目录下（`--workspace` 换过写法），取最近改过的那个。
        """
        wanted = {i for i in ids if valid_id(i)}
        if not wanted:
            return {}
        try:
            with os.scandir(self.projects_dir) as it:
                projects = [e.path for e in it if e.is_dir(follow_symlinks=False)]
        except OSError:
            return {}
        found: dict = {}
        # 每个项目目录列一次 agent-transcripts，和要找的 id 求交集：
        # 次数 = 项目数，不是 项目数 × id 数（list(200) 时差几十倍）
        for project in projects:
            try:
                with os.scandir(os.path.join(project, "agent-transcripts")) as it:
                    hits = [e.name for e in it if e.name in wanted]
            except OSError:
                continue
            for chat_id in hits:
                path = os.path.join(project, "agent-transcripts", chat_id, chat_id + ".jsonl")
                try:
                    mtime = os.stat(path).st_mtime
                except OSError:
                    continue
                if chat_id not in found or mtime > found[chat_id][1]:
                    found[chat_id] = (path, mtime)
        return {k: v[0] for k, v in found.items()}

    def find_transcript(self, chat_id: str) -> str:
        return self.find_transcripts([chat_id]).get(chat_id, "")

    # ---------- 编辑器对话：state.vscdb 的 composerHeaders（只读） ----------

    def _ide_rows(self, limit: int) -> list:
        """composerHeaders 最近 limit 条 → [dict]。库不在、表不在 ⇒ []；**不碰 cursorDiskKV**。"""
        if limit <= 0 or not os.path.isfile(self.state_db):
            return []
        try:
            conn = sqlite3.connect(_ro_uri(self.state_db), uri=True, timeout=SQLITE_TIMEOUT_SEC)
        except sqlite3.Error as exc:
            self.last_error = friendly_error(exc, "打不开 Cursor 的对话列表")
            return []
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(composerHeaders)")}
            if "composerId" not in cols:  # 表不在（老版本 / 没用过编辑器里的 agent）
                return []
            pick = [c for c in ("composerId", "workspaceId", "createdAt", "lastUpdatedAt",
                                "isArchived", "isSubagent", "value") if c in cols]
            where = ["composerId not like 'empty%'"]
            if "isSubagent" in cols:
                where.append("coalesce(isSubagent, 0) = 0")
            order = " order by lastUpdatedAt desc" if "lastUpdatedAt" in cols else ""
            sql = f"select {', '.join(pick)} from composerHeaders where {' and '.join(where)}{order} limit ?"
            rows = conn.execute(sql, (int(limit),)).fetchall()
        except sqlite3.Error as exc:
            self.last_error = friendly_error(exc, "读 Cursor 的对话列表失败")
            return []
        finally:
            conn.close()
        return [dict(zip(pick, row)) for row in rows]

    def _workspace_folder(self, workspace_id: Any) -> str:
        """workspaceStorage/<id>/workspace.json 里的 folder（点查一个小文件）。"""
        if not isinstance(workspace_id, str) or not valid_id(workspace_id):
            return ""
        data = _read_json_file(os.path.join(self.app_user, "workspaceStorage", workspace_id, "workspace.json"))
        return _file_uri_to_path((data or {}).get("folder"))

    def _ide_session(self, row: dict, transcript: str) -> Optional[Session]:
        chat_id = row.get("composerId")
        if not valid_id(chat_id):
            return None
        try:
            value = json.loads(row.get("value") or "{}")
        except (TypeError, ValueError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        if value.get("isDraft") and not value.get("name") and not transcript:
            return None  # 只开了个空输入框，什么都没聊
        cwd = ""
        for holder in (value.get("workspaceIdentifier"), (value.get("agentLocation") or {}).get("environment")
                       if isinstance(value.get("agentLocation"), dict) else None):
            uri = holder.get("uri") if isinstance(holder, dict) else None
            if isinstance(uri, dict):
                if uri.get("scheme", "file") == "file" and isinstance(uri.get("fsPath"), str):
                    cwd = uri["fsPath"]
                    break
        if not cwd:
            cwd = self._workspace_folder(row.get("workspaceId"))
        title = value.get("name") if isinstance(value.get("name"), str) else ""
        if not title and transcript:
            title = first_user_query(transcript)
        extra = {"source": "ide", "status_note": UNKNOWN_IDE_NOTE, "archived": bool(row.get("isArchived"))}
        if isinstance(value.get("unifiedMode"), str):
            extra["mode"] = value["unifiedMode"]
        return Session(
            tool="cursor",
            surface="app",
            id=chat_id,
            title=short_title(title or "（没有标题）"),
            cwd=cwd,
            status="unknown",
            owner="app",
            updated_at=_ms_to_epoch(row.get("lastUpdatedAt") or value.get("lastUpdatedAt")),
            can_send=False,
            can_stop=False,
            why_not_send=WHY_IDE_SEND,
            why_not_stop=WHY_IDE_STOP,
            extra=extra,
        )

    def _is_ide_chat(self, chat_id: str) -> bool:
        """点查一行：这个 id 是不是编辑器里的对话（只为了给出对的拒绝理由）。"""
        if not valid_id(chat_id) or not os.path.isfile(self.state_db):
            return False
        try:
            conn = sqlite3.connect(_ro_uri(self.state_db), uri=True, timeout=SQLITE_TIMEOUT_SEC)
        except sqlite3.Error:
            return False
        try:
            row = conn.execute("select 1 from composerHeaders where composerId = ? limit 1", (chat_id,)).fetchone()
        except sqlite3.Error:
            return False
        finally:
            conn.close()
        return row is not None

    # ---------- 谁开着哪个对话 ----------

    def _lsof_cwds(self, pids) -> Optional[dict]:
        """一次 lsof 拿这些进程的当前目录；读失败 ⇒ None（调用方按「看不清」处理）。"""
        pids = [str(int(p)) for p in pids]
        if not pids:
            return {}
        try:
            result = self.runner.run(
                [resolve_lsof(), "-a", "-p", ",".join(pids), "-d", "cwd", "-Fpn"], timeout=LSOF_TIMEOUT_SEC
            )
        except Exception as exc:
            logger.warning("lsof 失败：%s", exc)
            return None
        if result.error or result.timed_out:
            return None
        # 有的 pid 刚好退出时 lsof 退出码是 1，但别的行照样有用
        return parse_lsof_cwd(result.stdout)

    def _scan(self, table: ProcTable) -> dict:
        """一张 ps 表 → 我们的 / 外来认得出的 / 外来认不出的 cursor-agent。"""
        ours: dict = {}
        for job in self.jobs.jobs(tool=self.tool):
            if self.jobs.owns(job.pid, table):
                ours.setdefault(job.session_id, []).append(job.pid)
        foreign: dict = {}
        unknown: list = []
        for proc in table:
            if self.jobs.get(proc.pid) is not None and self.jobs.owns(proc.pid, table):
                continue
            agent = parse_agent_args(proc.pid, proc.args)
            if agent is None:
                continue
            if agent.chat_id:
                foreign.setdefault(agent.chat_id, []).append(agent)
            else:
                unknown.append(agent)
        cwds = self._lsof_cwds([a.pid for a in unknown]) if unknown else {}
        unknown_cwds = []
        for agent in unknown:
            cwd = (cwds or {}).get(agent.pid, "")
            if not cwd and agent.workspace.startswith("/"):
                cwd = agent.workspace
            unknown_cwds.append((agent, cwd))
        return {"ours": ours, "foreign": foreign, "unknown": unknown_cwds, "error": table.error}

    def _state_for(self, meta: _ChatMeta, scan: dict) -> dict:
        """一个命令行对话现在的状态 + 能不能发 / 停。"""
        ours = scan["ours"].get(meta.id, [])
        foreign = scan["foreign"].get(meta.id, [])
        if scan["error"]:
            return dict(status="unknown", owner="unknown", pids=(), can_send=False, can_stop=False,
                        why_not_send=WHY_UNSEEN_SEND, why_not_stop=WHY_IDLE_STOP)
        if ours:
            return dict(status="running", owner="bridge", pids=tuple(ours), can_send=False, can_stop=True,
                        why_not_send=WHY_BUSY_SEND, why_not_stop="")
        if foreign:
            # 外来进程忙不忙没实测过（只知道它开着）⇒ 状态 unknown，只说「开着」
            return dict(status="unknown", owner="terminal",
                        pids=tuple(a.pid for a in foreign), can_send=False, can_stop=False,
                        why_not_send=WHY_TERMINAL_SEND, why_not_stop=WHY_TERMINAL_STOP,
                        extra={"open_elsewhere": True, "printing": any(a.printing for a in foreign)})
        maybe = [a for a, cwd in scan["unknown"] if not cwd or _same_dir(cwd, meta.cwd)]
        if maybe:
            return dict(status="unknown", owner="unknown", pids=(), can_send=False, can_stop=False,
                        why_not_send=WHY_MAYBE_SEND, why_not_stop=WHY_TERMINAL_STOP,
                        extra={"maybe_pids": [a.pid for a in maybe]})
        if not meta.cwd or not os.path.isdir(meta.cwd):
            return dict(status="idle", owner="none", pids=(), can_send=False, can_stop=False,
                        why_not_send=WHY_NO_CWD, why_not_stop=WHY_IDLE_STOP)
        return dict(status="idle", owner="none", pids=(), can_send=True, can_stop=False,
                    why_not_send="", why_not_stop=WHY_IDLE_STOP)

    def _cli_session(self, meta: _ChatMeta, scan: dict, transcript: str) -> Session:
        state = dict(self._state_for(meta, scan))
        extra = {"source": "cli"}
        extra.update(state.pop("extra", {}))
        title = first_user_query(transcript) if transcript else ""
        return Session(
            tool="cursor",
            surface="cli",
            id=meta.id,
            title=short_title(title or "（命令行对话，还没有记录）"),
            cwd=meta.cwd,
            updated_at=meta.updated_at,
            extra=extra,
            **state,
        )

    # ---------- 公开方法 ----------

    def list(self, limit: int = 20) -> list:
        """最近的对话：命令行 + 编辑器，在跑的在前，其余按最后更新时间倒序。读失败返回已有的。"""
        self.last_error = ""
        try:
            limit = max(0, int(limit))
        except (TypeError, ValueError):
            limit = 20
        if limit == 0:
            return []
        metas = [m for m in self._cli_metas() if m.has_conversation]
        metas.sort(key=lambda m: m.updated_at, reverse=True)
        metas = metas[:limit]
        rows = self._ide_rows(limit * 2)  # 多拿一些：空草稿会被跳过
        cli_ids = {m.id for m in metas}
        rows = [r for r in rows if r.get("composerId") not in cli_ids]
        transcripts = self.find_transcripts([m.id for m in metas] + [
            r.get("composerId") for r in rows if isinstance(r.get("composerId"), str)
        ])
        sessions = []
        if metas:
            scan = self._scan(self._procs())
            if scan["error"]:
                self.last_error = scan["error"]
            sessions += [self._cli_session(m, scan, transcripts.get(m.id, "")) for m in metas]
        for row in rows:
            try:
                sess = self._ide_session(row, transcripts.get(row.get("composerId"), ""))
            except Exception as exc:  # 格式没文档、跨版本会变：一行坏了跳过这一行
                logger.warning("跳过一条读不懂的 Cursor 对话：%s", exc)
                continue
            if sess is not None:
                sessions.append(sess)
        # 在跑的、电脑上正开着的排前面，其余按最后更新时间
        sessions.sort(key=lambda s: (s.status != "running" and not s.pids, -s.updated_at))
        return sessions[:limit]

    def running(self) -> list:
        """现在电脑上开着的命令行对话。编辑器里的看不出来，不列（见 RUNNING_NOTE）。"""
        self.last_error = ""
        table = self._procs()
        if table.error:
            self.last_error = table.error
            return []
        scan = self._scan(table)
        ids = list(scan["ours"]) + [i for i in scan["foreign"] if i not in scan["ours"]]
        if not ids and not scan["unknown"]:
            return []
        transcripts = self.find_transcripts(ids)
        out = []
        for chat_id in ids:
            meta = self._cli_meta(chat_id) or _ChatMeta(
                id=chat_id, cwd="", created_at=0, updated_at=0, has_conversation=True, path=""
            )
            out.append(self._cli_session(meta, scan, transcripts.get(chat_id, "")))
        for agent, cwd in scan["unknown"]:
            out.append(Session(
                tool="cursor",
                surface="cli",
                id=f"pid-{agent.pid}",
                title=short_title(f"cursor-agent（{os.path.basename(cwd) or '不知道哪个项目'}）"),
                cwd=cwd,
                status="unknown",
                pids=(agent.pid,),
                owner="terminal",
                can_send=False,
                can_stop=False,
                why_not_send=WHY_FOREIGN_UNKNOWN,
                why_not_stop=WHY_TERMINAL_STOP,
                extra={"source": "cli", "unmatched": True},
            ))
        return out

    def read(self, id: str, n: int = 4) -> list:
        """最近 n 条（人说的 + Cursor 回的文字，不含工具调用），旧的在前。"""
        self.last_error = ""
        try:
            n = max(0, int(n))
        except (TypeError, ValueError):
            n = 4
        if n == 0 or not valid_id(id):
            return []
        path = self.find_transcript(id)
        if not path:
            self.last_error = "这个对话还没有能读的记录。"
            return []
        messages = []
        for line in _tail_lines(path, READ_TAIL_BYTES):
            msg = parse_transcript_line(line)
            if msg is not None:
                messages.append(msg)
        return messages[-n:]

    def stop(self, id: str) -> ActionResult:
        """只停手机上起的；终端里的、编辑器里的一律不碰。"""
        if not valid_id(id):
            return ActionResult(False, "这个对话认不出来。", str(id))
        if self.jobs.jobs(tool=self.tool, session_id=id):
            return self.stop_own(id)
        if self._cli_meta(id) is None and self._is_ide_chat(id):
            return ActionResult(False, WHY_IDE_STOP, id)
        table = self._procs()
        scan = self._scan(table)
        if scan["foreign"].get(id) or id.startswith("pid-"):
            return ActionResult(False, WHY_TERMINAL_STOP, id)
        return ActionResult(False, WHY_IDLE_STOP, id)

    def build_argv(self, binary: str, chat_id: str, cwd: str, text: str, mode: str) -> list:
        """`cursor-agent --resume <id> -p --output-format json --trust --workspace <cwd> [档位] -- <text>`。

        `--` 之后才是用户的话：手机上发一句以 `-` 开头的话，不能被当成旗标。
        """
        return [
            binary, "--resume", chat_id, "-p", "--output-format", "json", "--trust",
            "--workspace", cwd, *MODE_FLAGS.get(mode, MODE_FLAGS["readonly"]), "--", text,
        ]

    # ---------- relay：进会话聊天 ----------

    def relay(self, id: str, text: str, timeout: float = RELAY_TIMEOUT_SEC,
              mode: Any = None) -> ActionResult:
        """连续多轮地跟一个**命令行**对话说话（`--resume` 同一个 chat）。

        和 `send()` 的区别只在**取回复**和**拒绝话术**：
        - `--output-format json` 的 `result` 就是这一轮的回复（同步拿到，不用轮询文件）；
        - 它没给文字时，退回去读 transcript 的尾部，**按内容**找我们这句的回显再取后面的回复
          （不能用字节偏移：CLI 的 transcript 是整文件重写的，见模块头）；
        - 并发输家的上游原话换成人话；编辑器里的对话给出能做到的替代。

        同一个 chat 串行（`_send_lock`），不同 chat 可以并行 —— 实测锁的粒度就该是 chatId。
        """
        if not valid_id(id) or id.startswith("pid-"):
            return ActionResult(False, "这个对话认不出来。", str(id))
        text = (text or "").strip()
        if not text:
            return ActionResult(False, "要说的话是空的。", id)
        if len(text) > MAX_SEND_CHARS:
            return ActionResult(False, f"一次最多发 {MAX_SEND_CHARS} 个字，这条太长了。", id)
        meta = self._cli_meta(id)
        if meta is None:
            # 编辑器里的对话：这里就拦住，不去 resume、不写它的库、不碰界面
            if self._is_ide_chat(id):
                return ActionResult(False, WHY_RELAY_IDE, id)
            return ActionResult(False, "找不到这个 Cursor 命令行对话，可能已经删了。", id)
        binary = self.agent_bin()
        if not binary:
            return ActionResult(False, WHY_NO_BIN, id)
        lock = _send_lock(os.path.realpath(self.cursor_home) + "\0" + id)
        if not lock.acquire(blocking=False):
            return ActionResult(False, WHY_BUSY_SEND, id)
        try:
            state = self._state_for(meta, self._scan(self._procs()))
            if not state["can_send"]:
                return ActionResult(False, state["why_not_send"], id)
            spawned: list = []
            sure_not_sent: list = []
            res = self._relay_locked(meta, binary, text, timeout, self._current_mode(mode),
                                     spawned, sure_not_sent)
            # 起过子进程 ⇒ 默认当 delivered（宁可让用户去看一眼，也不能骗他「没发进去」
            # 然后他重发、在真实对话里跑第二遍）。**例外**：拿到了「这条确实没进去」的
            # 实测证据（并发输家）⇒ 照实说没进去，让他放心重发。
            if spawned and not res.delivered and not sure_not_sent:
                res = ActionResult(res.ok, res.text, res.session_id, delivered=True)
            return res
        finally:
            lock.release()

    def _relay_locked(self, meta: _ChatMeta, binary: str, text: str, timeout: float, mode: str,
                      spawned: Optional[list] = None,
                      sure_not_sent: Optional[list] = None) -> ActionResult:
        timeout = max(RELAY_MIN_TIMEOUT_SEC, float(timeout or RELAY_TIMEOUT_SEC))
        raw: list = []
        res = self._send_locked(meta, binary, text, timeout, mode, spawned, raw)
        if res.ok and res.text and res.text != RELAY_EMPTY_REPLY:
            return res
        # 认并发输家要看**未截断**的 stderr：`_to_action` 只放得下 200 字
        blob = (res.text or "") + "\n" + "\n".join(
            (r.stderr or "") + "\n" + (r.stdout or "") for r in raw
        )
        if _OWNERSHIP_CHANGING in blob:
            # 上游响亮失败：**没进去**（输家 0.35~0.47 秒就退，transcript 没坏行）
            if sure_not_sent is not None:
                sure_not_sent.append(True)
            return ActionResult(False, WHY_RELAY_RACE, meta.id, delivered=False)
        if res.ok:
            # 跑成功但 json 里没文字 ⇒ 去 transcript 里找（有时文字只落在记录里）
            found = self.reply_after(meta.id, text)
            if found:
                return ActionResult(True, found, meta.id, delivered=True)
            return ActionResult(True, RELAY_EMPTY_REPLY, meta.id, delivered=res.delivered)
        return res

    def reply_after(self, chat_id: str, sent: str) -> str:
        """transcript 尾部：找到我们这句的 user 回显，返回它后面的 assistant 文字。

        **按内容找，不用字节偏移** —— CLI 的 transcript 是整文件重写的（见模块头那条）。
        找不到回显就返回空串（宁可说「没有文字回复」，也不能把上一轮的回答当成这句的）。
        """
        if not valid_id(chat_id):
            return ""
        path = self.find_transcript(chat_id)
        if not path:
            return ""
        return reply_after_echo(_tail_lines(path, READ_TAIL_BYTES), sent)

    def relay_stop(self, id: str) -> ActionResult:
        """relay 这条路上的 `/stop`：只停手机上起的那一轮，别人的说清楚在哪停。"""
        return self.stop(id)

    def send(self, id: str, text: str, timeout: float = 600, mode: Any = None) -> ActionResult:
        """往命令行对话里接着说一句。只在**确认没人开着它**时发；同一个对话同一时刻只发一条。"""
        if not valid_id(id) or id.startswith("pid-"):
            return ActionResult(False, "这个对话认不出来。", str(id))
        text = (text or "").strip()
        if not text:
            return ActionResult(False, "要说的话是空的。", id)
        if len(text) > MAX_SEND_CHARS:
            return ActionResult(False, f"一次最多发 {MAX_SEND_CHARS} 个字，这条太长了。", id)
        meta = self._cli_meta(id)
        if meta is None:
            if self._is_ide_chat(id):
                return ActionResult(False, WHY_IDE_SEND, id)
            return ActionResult(False, "找不到这个 Cursor 命令行对话，可能已经删了。", id)
        binary = self.agent_bin()
        if not binary:
            return ActionResult(False, WHY_NO_BIN, id)
        lock = _send_lock(os.path.realpath(self.cursor_home) + "\0" + id)
        if not lock.acquire(blocking=False):
            return ActionResult(False, WHY_BUSY_SEND, id)
        try:
            # 锁里再查一次：等锁、点按钮的这几秒里，电脑上可能刚好打开了它
            state = self._state_for(meta, self._scan(self._procs()))
            if not state["can_send"]:
                return ActionResult(False, state["why_not_send"], id)
            spawned: list = []
            res = self._send_locked(meta, binary, text, timeout, self._current_mode(mode), spawned)
            # 起过子进程 ⇒ 消息已经进了会话（不管后面超时 / 被停 / 报错）
            if spawned and not res.delivered:
                res = ActionResult(res.ok, res.text, res.session_id, delivered=True)
            return res
        finally:
            lock.release()

    def _send_locked(self, meta: _ChatMeta, binary: str, text: str, timeout: float, mode: str,
                     spawned: Optional[list] = None, raw: Optional[list] = None) -> ActionResult:
        """`raw`：给它一个 list，会把原始 `RunResult` 放进去（relay 要看未截断的 stderr）。"""
        watch = _WorkerWatch()
        spawned = spawned if spawned is not None else []

        def on_spawn(pid: int) -> None:
            spawned.append(pid)
            job = self.jobs.register_spawned(pid, self.tool, meta.id)
            watch.pid = pid
            watch.lstart = job.lstart if job else ""
            watch.thread = threading.Thread(target=self._watch_workers, args=(watch,), daemon=True,
                                            name=f"cursor-worker-watch-{pid}")
            watch.thread.start()

        argv = self.build_argv(binary, meta.id, meta.cwd, text, mode)
        try:
            result = self.runner.run(argv, timeout=float(timeout), cwd=meta.cwd, on_spawn=on_spawn)
        except Exception as exc:  # runner 可注入，防它自己炸
            result = RunResult(returncode=-1, error=friendly_error(exc, "没能启动 cursor-agent"))
        finally:
            watch.stop.set()
            if watch.thread is not None:
                watch.thread.join(timeout=max(self.worker_poll_sec * 4, 2.0))
            if watch.pid:
                self.jobs.forget(watch.pid)
        try:
            self._reap_workers(watch, meta.cwd)
        except Exception:
            logger.exception("收 worker-server 时出错（不影响这次发送的结果）")
        if raw is not None:
            raw.append(result)
        return self._to_action(meta.id, result)

    def _to_action(self, chat_id: str, result: RunResult) -> ActionResult:
        return self._to_action_impl(chat_id, result)

    # ---------- start：手机上新建会话 ----------

    def start(self, cwd: str, text: str, timeout: float = 600, mode: Any = "readonly") -> ActionResult:
        """在 `cwd` 新开一个命令行对话：和 `build_argv` 同一套旗标，只是不带 `--resume`。

        🔴 control.start 一律传 readonly（`--mode ask`）。新对话的 id 从结果 JSON 的 `session_id` 拿；
        拿不到 ⇒ 当没开成。⚠️ 本机没装 cursor-agent，这条路没在真机上跑过（按 `--resume` 那条的形状推断）。
        """
        text = (text or "").strip()
        if not text:
            return ActionResult(False, "要说的话是空的。", "")
        if len(text) > MAX_SEND_CHARS:
            return ActionResult(False, f"一次最多发 {MAX_SEND_CHARS} 个字，这条太长了。", "")
        if not cwd or not os.path.isdir(cwd):
            return ActionResult(False, "这个项目目录不在了，换一个再开。", "")
        binary = self.agent_bin()
        if not binary:
            return ActionResult(False, WHY_NO_BIN, "")
        flags = MODE_FLAGS.get(mode if isinstance(mode, str) else "readonly", MODE_FLAGS["readonly"])
        argv = [binary, "-p", "--output-format", "json", "--trust", "--workspace", cwd, *flags, "--", text]
        spawned: list = []

        def on_spawn(pid: int) -> None:
            spawned.append(pid)
            self.jobs.register_spawned(pid, self.tool, "")

        try:
            result = self.runner.run(argv, timeout=float(timeout), cwd=cwd, on_spawn=on_spawn)
        except Exception as exc:
            result = RunResult(returncode=-1, error=friendly_error(exc, "没能启动 cursor-agent"))
        finally:
            for pid in spawned:
                self.jobs.forget(pid)
        data = parse_result(result.stdout) or {}
        sid = data.get("session_id") or data.get("chatId") or ""
        sid = sid if isinstance(sid, str) and valid_id(sid) else ""
        out = self._to_action_impl(sid, result)
        if not sid:
            return ActionResult(False, out.text if not out.ok else "Cursor 回了，但没给出新对话的编号，没法接着聊。", "")
        return ActionResult(out.ok, out.text, sid, delivered=bool(spawned))

    def _to_action_impl(self, chat_id: str, result: RunResult) -> ActionResult:
        if result.error:
            return ActionResult(False, result.error, chat_id)
        if result.timed_out:
            return ActionResult(False, "等太久没回完，已经把这一轮停掉了。可以再发一次试试。", chat_id)
        data = parse_result(result.stdout) or {}
        reply = data.get("result") if isinstance(data.get("result"), str) else ""
        if result.returncode == 130 or result.returncode == 143:
            return ActionResult(False, "这一轮被停下了，没有回复。", chat_id)
        if result.returncode != 0 or data.get("is_error") is True or (data and data.get("subtype") not in (None, "success")):
            # 🔴 原来连 stderr 原文一起发到手机（可能带本机路径）。原文只进日志（第 11 轮文案门禁）
            logger.warning("Cursor 没答上来 退出码=%s stderr=%s", result.returncode,
                           (result.stderr or "")[:500])
            detail = short_title(reply, 200) if reply else "它没说原因，再发一次试试"
            return ActionResult(False, f"Cursor 没答上来：{detail}", chat_id)
        if not reply.strip():
            return ActionResult(True, RELAY_EMPTY_REPLY, chat_id)
        return ActionResult(True, reply.strip(), chat_id)

    # ---------- worker-server ----------

    def _watch_workers(self, watch: _WorkerWatch) -> None:
        """发送期间：每隔一会儿看一眼我们那个进程的子孙里有没有 worker-server。"""
        while True:
            try:
                self._note_workers(watch, self._procs())
            except Exception:
                logger.exception("看 worker-server 时出错")
            if watch.stop.wait(self.worker_poll_sec):
                break
        try:  # 结束前再看最后一眼（它可能刚好在最后半秒里起来）
            self._note_workers(watch, self._procs())
        except Exception:
            pass

    @staticmethod
    def _note_workers(watch: _WorkerWatch, table: ProcTable) -> None:
        if not watch.pid or not watch.lstart or table.error:
            return
        if table.lstart(watch.pid) != watch.lstart:
            return  # 我们那个进程已经没了（或 pid 被复用），它名下的子孙认不准了
        family = {watch.pid}
        frontier = [watch.pid]
        while frontier:
            kids = [p for ppid in frontier for p in table.children(ppid) if p.pid not in family]
            family.update(p.pid for p in kids)
            frontier = [p.pid for p in kids]
            for proc in kids:
                if is_worker_server(proc.args) and proc.pid not in watch.seen:
                    watch.seen[proc.pid] = proc

    def _reap_workers(self, watch: _WorkerWatch, cwd: str) -> int:
        """只收证明是我们起的 worker：还是那个进程、cwd 对得上、启动不早于我们。拿不准就留着。"""
        if not watch.seen:
            return 0
        spawn_at = parse_lstart(watch.lstart)
        table = self._procs()
        if table.error:
            logger.info("读不到进程表，%d 个 worker-server 先留着", len(watch.seen))
            return 0
        candidates = []
        for pid, seen in watch.seen.items():
            if table.lstart(pid) != seen.lstart:
                continue  # 已经自己退了，或者 pid 被别人复用
            if not spawn_at or seen.started_at + 1 < spawn_at:
                logger.info("worker-server pid=%s 启动时间早于我们的进程，留着", pid)
                continue
            candidates.append(seen)
        if not candidates:
            return 0
        cwds = self._lsof_cwds([p.pid for p in candidates])
        if cwds is None:
            logger.info("lsof 读不到 cwd，%d 个 worker-server 先留着", len(candidates))
            return 0
        reaped = 0
        for proc in candidates:
            if not _same_dir(cwds.get(proc.pid, ""), cwd):
                logger.info("worker-server pid=%s 的 cwd 对不上（%s），留着", proc.pid, cwds.get(proc.pid, "?"))
                continue
            # 单独的 tool 名：登记的这一瞬间别的线程扫描时，不会把它当成一个对话列出来
            job = self.jobs.register(proc.pid, proc.lstart, WORKER_JOB_TOOL, str(watch.pid))
            if job is None:
                continue
            res = self.jobs.signal(proc.pid, _signal.SIGTERM)  # 唯一发信号的地方，lstart 再核一次
            self.jobs.forget(proc.pid)
            if res.ok:
                reaped += 1
                logger.info("收掉了我们起的 worker-server pid=%s", proc.pid)
            else:
                logger.info("worker-server pid=%s 没收掉：%s", proc.pid, res.text)
        return reaped
