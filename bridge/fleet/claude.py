"""Claude Code 会话：列 / 看在跑的 / 读最近消息 / 没人占着时接着发 / 只停自己起的。

## 数据从哪来（都是 2026-11-04 实测过的，见 docs/agent-control-groundtruth-raw.txt）

- **谁在跑**：`~/.claude/sessions/<pid>.json`，每个活着的 claude 进程一份（终端 TUI、`claude -p`
  都写）。桥自己的子进程在 `~/.claude-feishu-bridge/sessions/`，分开读。
  ⚠️ 同目录下还有 `<pid>.<hash>.key` —— 那是会话间消息的签名密钥，**只认 `<数字>.json`，别的一律不碰**。
- **判活**：pid 在 **且** procStart 对得上 ps 的 lstart（pid 会复用；procStart 是 UTC，lstart 是本地时间，
  换算交给 `ProcTable.same_process`）。注册表的 status 会过期：Ctrl-Z 挂起的进程 STAT=T 但写着 idle，
  所以 STAT 带 T 一律算 stopped。
- **历史会话**：`projects/<目录>/<sessionId>.jsonl`。每个项目目录 os.scandir 一层，按 mtime 取最近的；
  **绝不整文件读**（大的有 70MB+），只读头 64KB（cwd、入口、第一句话）和尾部（标题、消息）。
- **App（Claude-3p）标题**：`Claude-3p/claude-code-sessions/**/local_*.json` 的 cliSessionId → title。只读。

## 发消息的红线

`claude -p --resume <id>` 在**有进程占着**的会话上不报错、不加锁，而是在同一个 jsonl 里**静默分叉**，
终端那边永远看不到（实测）。所以：
- 有活进程（终端 / App / 别的脚本 / 我们自己上一条还没回完）⇒ 不发，说清为什么
- 真正 spawn 之前**再查一次**（用户可能刚在终端里打开它）
- 同一个会话同一时间只发一条（两条并发 = 自己跟自己分叉）
- App 里**关着**的会话：第 14 轮起照样后台接着跑（主人拍板）；App 界面要重开那个会话才看得到新的几轮
- App 里的会话也**不走** relay（会话间消息）。2026-11-06 第 12 轮实测（一次性会话，用 App 自带的
  2.1.308 二进制、`CLAUDE_CODE_ENTRYPOINT=claude-desktop-3p`、`--permission-mode=auto`，和本机 App 进程同参）：
  - 桥现在的信使是 bypass 类，App 会话的 `auto` 归 **prompting** 类 ⇒ 3 句全被扣住
    （宿主收到 `system/peer_message_hold cause=mode-mismatch`，jsonl 里一行都不落、没回复）
  - 信使换成 `auto`：第 1 句送到并回了，第 2 句起被信使自己的 auto 分类器拦下
    （判 OWNER_FRAMING「主人从手机发来的」是伪造授权）⇒ 1/3
  - 信使换成 `default` + 只放行 SendMessage：3/3 送到（3.3~3.8s）、回复从 jsonl 取回（6~11s）
    ⇒ 路是通的，但要**第二个信使**（prompting 类）+ 从 user 行读权限档（App 的 jsonl 没有
    `permission-mode` 行，`relay.permission_mode_of` 读出空串 ⇒ 现在会判「看不出权限档」）
    + 在真 App 界面上确认用户看得见那句话（没测：没法从命令行开 App 会话）。这三样都没做 ⇒ 不给按钮。

## 停的红线

终端里的会话收到 SIGINT 会**整个退出**（实测），不是停当前这一轮。所以只停 FleetJobs 里登记的、
桥自己起的那个 `claude -p`。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import threading
import time
from typing import Any, Iterable, Optional

from .model import (
    ActionResult,
    Adapter,
    AskQuestion,
    FleetJobs,
    Message,
    PendingAsk,
    ProcTable,
    Session,
    SubprocessRunner,
    friendly_error,
    short_title,
)

logger = logging.getLogger("feishu-bridge.fleet")

TOOL = "claude"

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 2 * 1024 * 1024
TAIL_BYTES_WIDE = 8 * 1024 * 1024  # 大 tool_result 把 2MB 吃满、凑不够 n 条时放宽一次
TITLE_TAIL_BYTES = 256 * 1024
# 「等你」时找它卡在哪一步：等你的那条 tool_use 一定是**最后几行**之一（它停在那儿了，后面没东西）
# ⇒ 读尾巴 512KB 足够，别像 read() 那样读 2MB（每次扫描每个等你的会话都要读一遍）。
PENDING_TAIL_BYTES = 512 * 1024
# jsonl 这么近还在变、却找不到占着它的进程 ⇒ 当作「可能有人在用」，不发
RECENT_WRITE_SEC = 30
DEFAULT_SEND_TIMEOUT = 600

_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_REGISTRY_RE = re.compile(r"^\d+\.json$")

# 注册表 status → 总控 status
_STATUS_MAP = {"busy": "running", "idle": "idle", "waiting": "waiting", "shell": "idle"}
# 一个会话多个 pid 时取最「要紧」的那个
_STATUS_RANK = {"running": 0, "waiting": 1, "idle": 2, "stopped": 3, "unknown": 4}

# 读消息时整条跳过的「用户」行：都是 Claude Code 自己塞进去的，不是人说的话
_NOISE_PREFIXES = (
    "<task-notification>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<system-reminder>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "Caveat: The messages below",
)
_CROSS_SESSION_RE = re.compile(
    r"<cross-session-message\b([^>]*)>(.*?)(?:</cross-session-message>|$)", re.S
)
_FROM_NAME_RE = re.compile(r'from-name="([^"]*)"')
_COMMAND_NAME_RE = re.compile(r"<command-name>(.*?)</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.S)

# ---------- 给手机用户看的话 ----------

WHY_SEND_TERMINAL = "它开在电脑的终端里，从手机发进去会分叉成两段对话（终端那边看不到）。在终端里退出它（/exit）后点 🔄 刷新，就能从手机接着聊。"
WHY_SEND_SCRIPT = "电脑上有个 claude 程序正在用它（脚本或别的程序），现在发进去会分叉成两段对话；等它跑完再发。"
# 🔴 第 12 轮：原来那句「这条路也还没验证过」是写给我们看的，同事拿着它什么都做不了。
# 现在只说两件他能做的事：回 App 里说；或者就在这个飞书对话里直接说（走桥自己的 claude，
# 但它看不到 App 里聊过的内容 ⇒ 要他先把事说清楚）。卡片上一条最多 NOTE_CHARS=160 字。
WHY_SEND_APP = (
    "它开在电脑上的 Claude App 里，手机发不进去。要接着聊，就到 App 里说；"
    "想在手机上做，直接在这个对话里跟我说（我看不到它之前聊过什么，先把事情说清楚）。"
)
# 第 14 轮复审：App 里回退 / 分叉过的会话，旧的那一份（列表里不出现，防有人拿旧卡点）
WHY_SEND_SUPERSEDED = "这是它在 App 里回退之前的旧版本，现在用的是新的那一份。点「🖥 回到总览」重新选。"
WHY_SEND_COURIER = "这是手机助手自己用来传话的会话，不是你的。"
WHY_SEND_BRIDGE_BUSY = "上一条还没回完，等它回完再发。"
WHY_SEND_BRIDGE_CHAT = "这是飞书对话自己的会话，直接在那个飞书对话里接着说就行。"
WHY_SEND_RECENT = "它刚刚还在写入，可能正开在别的地方；过一会儿再发。"
WHY_SEND_UNKNOWN = "看不出现在谁占着它，为了不分叉先不发。"
WHY_SEND_JUST_OPENED = "电脑上好像刚打开了一个 Claude，可能就是这个会话；为了不分叉先不发，过几秒点 🔄 刷新再试。"
# 新起的 claude 写注册表的空窗（实测 0.2~1.7 秒），留足余量
_FRESH_PROC_SEC = 5
WHY_SEND_NO_POLICY = "手机发消息的权限档位没配好，先不发。"
WHY_SEND_NO_HOOK = "确认卡片那一层没接上，现在只能在「只读」档从手机接着聊。"
WHY_SEND_DEFERRED = "它想改文件或跑命令，这一步要你确认，可从手机接着聊时弹不了确认卡，所以**这一步没做**。要它动手：在电脑上打开这个会话，或者在「🔑 权限」里切到「全授权」。"

# relay（进会话聊天）那条路的拒绝理由。和上面 `WHY_SEND_*` 不是一回事：
# 那些是「桥能不能起 `claude -p --resume` 接着跑」，这些是「能不能把一句话送进它」。
WHY_RELAY_NOT_TERMINAL = "这条路只对电脑终端里开着的 Claude 会话管用。"
WHY_RELAY_NO_NAME = "这个会话没有名字，送不进去。在电脑上那个会话里用 /name 起个名字，再点 🔄 刷新。"
WHY_RELAY_OLD_VERSION = "这个会话的 Claude 版本还不支持从别处送消息进去。在电脑上和它说吧。"
WHY_RELAY_SUSPENDED = "它在电脑上被挂起了（按过 Ctrl-Z），送进去不会有人理。到电脑上敲 fg 让它继续。"

WHY_STOP_TERMINAL = "终端里的会话只能在终端里停，从外面停会直接关掉整个会话。"
# 和 WHY_SEND_APP 在详情卡上紧挨着出现 ⇒ 不再重复「这是 App 里的会话」，只说停的那一步
WHY_STOP_APP = "要停它，也只能在 App 里点停止。"
WHY_STOP_BRIDGE_CHAT = "这是飞书对话里正在跑的那一轮，回到那个对话点「⏹ 停下」。"
WHY_STOP_IDLE = "它现在没在跑，不用停。"


# ---------- 小工具 ----------


def _is_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _read_head(path: str, size: int = HEAD_BYTES) -> list:
    """读文件头 size 字节，按行解析；最后半行丢掉。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read(size)
    except OSError:
        return []
    lines = data.split(b"\n")
    if len(data) >= size:
        lines = lines[:-1]
    return _parse_lines(lines)


def _read_tail(path: str, size: int) -> tuple:
    """读文件尾 size 字节，丢掉第一行半截 → (记录列表, 是否读到了文件头)。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            total = fh.tell()
            start = max(0, total - size)
            fh.seek(start)
            data = fh.read()
    except OSError:
        return [], True
    lines = data.split(b"\n")
    if start > 0:
        lines = lines[1:]
    return _parse_lines(lines), start == 0


def _parse_lines(lines: Iterable[bytes]) -> list:
    out = []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _iso_to_epoch(value: Any) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        stamp = _dt.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=_dt.timezone.utc)
    return stamp.timestamp()


def _ms_to_epoch(value: Any) -> float:
    try:
        num = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if num <= 0:
        return 0.0
    return num / 1000.0 if num > 1e11 else num


def _content_text(content: Any) -> str:
    """message.content → 纯文本：字符串原样；列表只拼 text 块（跳过 tool_use / tool_result / thinking）。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n".join(parts)


def _clean_user_text(text: str) -> str:
    """用户行 → 给人看的样子；是噪音就返回空串。

    - 跨会话消息：`Another Claude session sent a message: <cross-session-message from-name=...>正文`
      ⇒ 「〔另一个会话 xxx 发来〕正文」
    - 斜杠命令：`<command-name>/model</command-name><command-args>x</command-args>` ⇒ `/model x`
    - `[Request interrupted by user...]` ⇒ 「（打断了）」
    """
    stripped = text.strip()
    if not stripped:
        return ""
    if "<cross-session-message" in stripped:
        match = _CROSS_SESSION_RE.search(stripped)
        if match:
            name = _FROM_NAME_RE.search(match.group(1) or "")
            who = f"另一个会话 {name.group(1)}" if name and name.group(1) else "另一个会话"
            return f"〔{who}发来〕{match.group(2).strip()}"
    if stripped.startswith("<command-name>") or stripped.startswith("<command-message>"):
        name = _COMMAND_NAME_RE.search(stripped)
        if not name:
            return ""
        args = _COMMAND_ARGS_RE.search(stripped)
        tail = (args.group(1).strip() if args else "")
        return (name.group(1).strip() + (" " + tail if tail else "")).strip()
    if stripped.startswith("[Request interrupted by user"):
        return "（打断了）"
    for prefix in _NOISE_PREFIXES:
        if stripped.startswith(prefix):
            return ""
    return stripped


def _first_user_text(records: list) -> str:
    """头部记录里第一句人说的话（给没名字的会话当标题）。"""
    for rec in records:
        if rec.get("type") != "user" or rec.get("isSidechain") or rec.get("isMeta"):
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        text = _clean_user_text(_content_text(message.get("content")))
        if text and not text.startswith(("〔", "（", "/")):
            return text
    return ""


def _scan_head(path: str) -> dict:
    """头 64KB：cwd、入口（cli / sdk-cli / claude-desktop-3p）、标题候选、第一句话。"""
    records = _read_head(path)
    info: dict = {"cwd": "", "entrypoint": "", "custom_title": "", "ai_title": "", "first": "",
                  "last_turn": "", "last_turn_at": 0.0}
    for rec in records:
        if not info["cwd"] and isinstance(rec.get("cwd"), str):
            info["cwd"] = rec["cwd"]
        if not info["entrypoint"] and isinstance(rec.get("entrypoint"), str):
            info["entrypoint"] = rec["entrypoint"]
        _take_titles(rec, info)
    info["first"] = _first_user_text(records)
    return info


def _take_titles(rec: dict, info: dict) -> None:
    kind = rec.get("type")
    if kind == "custom-title" and isinstance(rec.get("customTitle"), str) and rec["customTitle"].strip():
        info["custom_title"] = rec["customTitle"].strip()
    elif kind == "ai-title" and isinstance(rec.get("aiTitle"), str) and rec["aiTitle"].strip():
        info["ai_title"] = rec["aiTitle"].strip()


def _tail_titles(path: str, info: dict) -> None:
    """改名会在后面追加新的 custom-title / ai-title；尾部的比头部的新。

    第 32 轮：同一份尾巴顺手看「最后一轮怎么结束的」（`_last_turn`），别为它再读一遍文件。
    """
    records, _ = _read_tail(path, TITLE_TAIL_BYTES)
    for rec in records:
        _take_titles(rec, info)
    info["last_turn"], info["last_turn_at"] = _last_turn(records)


def _turn_mark(rec: dict) -> Optional[tuple]:
    """一条记录对「最后一轮怎么结束的」说了什么 → (状态, 时间)；跟这件事无关 ⇒ None。

    本机 211 个 jsonl 实测（2026-11-09）：
    - 好好跑完 = 最后一条主线 assistant 是 `stop_reason: end_turn`、里面没有 tool_use（181 个）
    - 出错 = `isApiErrorMessage: true` 的那条 assistant（model=`<synthetic>`，`error` 字段是
      server_error / invalid_request / authentication_failed …，正文是「Request timed out」
      「Prompt is too long」「Please run /login」这类）；只有它是**最后一条**才算（3 个）——
      后面人又说了话的 31 个都是「出错后重试过」，那一轮不算失败
    - 别的（最后是人说的话 = 正在跑 / 被打断；是 tool_result / tool_use = 一轮跑到一半；
      stop_reason 是空的或 max_tokens）⇒ 说不准，返回 ("", 0)，卡上不标
    """
    kind = rec.get("type")
    if kind not in ("user", "assistant") or rec.get("isSidechain"):
        return None
    message = rec.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    at = _iso_to_epoch(rec.get("timestamp"))
    if kind == "user":
        if rec.get("isMeta"):
            return None
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            return ("", 0.0)  # 一轮还在中间
        # 噪音（系统塞的提醒之类）不算人说了话；真说了话 ⇒ 后面要么在跑、要么被打断，都不是「跑完了」
        return ("", 0.0) if _clean_user_text(_content_text(content)) else None
    if rec.get("isApiErrorMessage"):
        return ("failed", at)
    blocks = content if isinstance(content, list) else ()
    if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
        return ("", 0.0)
    if message.get("stop_reason") == "end_turn":
        return ("done", at)
    return ("", 0.0)


def _last_turn(records: list) -> tuple:
    """jsonl 记录（旧 → 新）→ 最后一轮怎么结束的：("done"|"failed"|"", 时间)。读不出来一律 ("", 0)。"""
    last = ("", 0.0)
    for rec in records:
        try:
            mark = _turn_mark(rec)
        except Exception:  # noqa: BLE001 —— 形状怪的记录跳过，别让总览出不来
            continue
        if mark is not None:
            last = mark
    return last


def _app_index(app_root: str) -> dict:
    """Claude-3p 的 `local_*.json` → {cliSessionId: {title, archived, cwd, updated_at, summary}}。只读。"""
    base = os.path.join(app_root, "claude-code-sessions")
    out: dict = {}
    superseded: set = set()
    if not os.path.isdir(base):
        return out
    for dirpath, _dirs, files in os.walk(base):
        for name in files:
            if not (name.startswith("local_") and name.endswith(".json")):
                continue
            data = _read_json(os.path.join(dirpath, name))
            if not data:
                continue
            sid = data.get("cliSessionId")
            if not _is_session_id(sid):
                continue
            summary = data.get("postTurnSummary")
            # 🔴 第 14 轮复审：App 回退 / 分叉后旧 id 的 jsonl 还在 ⇒ 总览里两个同名会话，点旧的 = 后台另起分支，
            # App 里永远看不到。旧 id 记在 `priorCliSessionIds` 和 `rewindEdges[].parent`（本机实测 afad5500→18cd70e4）。
            for old in data.get("priorCliSessionIds") or []:
                if _is_session_id(old) and old != sid:
                    superseded.add(old)
            for edge in data.get("rewindEdges") or []:
                parent = edge.get("parent") if isinstance(edge, dict) else None
                if _is_session_id(parent) and parent != sid:
                    superseded.add(parent)
            out[sid] = {
                "title": data.get("title") if isinstance(data.get("title"), str) else "",
                "archived": bool(data.get("isArchived")),
                "cwd": data.get("cwd") if isinstance(data.get("cwd"), str) else "",
                "updated_at": _ms_to_epoch(data.get("lastActivityAt")),
                "status_category": summary.get("status_category", "") if isinstance(summary, dict) else "",
            }
    # 被分叉掉的旧 id：当成已归档（不列、不让发）。它要是自己也是一条现役 App 会话（不该发生）就不动它。
    for old in superseded:
        if old not in out:
            out[old] = {"title": "", "archived": True, "cwd": "", "updated_at": 0.0,
                        "status_category": "", "superseded": True}
    return out


class ClaudeAdapter(Adapter):
    """Claude Code（终端 TUI、`claude -p`、Claude-3p App 的 Code 页）。

    构造参数里的三个目录默认是真实家目录；测试一律传 tmp_path 下的假目录。
    `policy`：桥的 Policy（或返回 Policy 的函数，因为聊天里能换档）。只用它的
    `allowed_tools()` / `disallowed_tools()` / `resolve_workspace()`。没给 ⇒ 一律不发。
    `procs`：返回 ProcTable 的函数；默认走 `self.runner` 跑一次 ps。
    """

    tool = TOOL

    def __init__(
        self,
        home: Optional[str] = None,
        bridge_home: Optional[str] = None,
        app_root: Optional[str] = None,
        *,
        jobs: Optional[FleetJobs] = None,
        runner: Optional[SubprocessRunner] = None,
        policy: Any = None,
        procs: Any = None,
        claude_bin: str = "claude",
        now: Any = time.time,
        user_settings: Optional[str] = None,
        hook_settings: Any = None,
    ) -> None:
        super().__init__(jobs=jobs, runner=runner)
        self.home = os.path.expanduser(home or "~/.claude")
        # 认证键从这里挑（默认 <home>/settings.json）。只挑认证那几个，权限规则 / hooks 不带。
        self.user_settings = os.path.expanduser(user_settings or os.path.join(self.home, "settings.json"))
        # 桥的审批钩子那段 settings（dict 或返回 dict 的函数）。没有 ⇒ 只在只读档发。
        self._hook_settings = hook_settings
        self.bridge_home = os.path.expanduser(bridge_home or "~/.claude-feishu-bridge")
        self.app_root = os.path.expanduser(app_root or "~/Library/Application Support/Claude-3p")
        self._policy = policy
        self._procs = procs
        self.claude_bin = claude_bin
        self._now = now
        self._send_locks: dict = {}
        self._send_locks_guard = threading.Lock()
        self._last_own_write: dict = {}  # sessionId → 我们最后一次发完的时间（别把自己的写入当成「有人在用」）

    # ---------- 进程 / 注册表 ----------

    def _table(self) -> ProcTable:
        try:
            if self._procs is not None:
                return self._procs()
            return ProcTable.snapshot(self.runner)
        except Exception as exc:  # 注入的函数炸了也不能抛给上层
            return ProcTable(error=friendly_error(exc, "读不到进程列表"))

    def _registry(self) -> list:
        """两个注册表目录里的 `<pid>.json` → [(来源, 记录)]。`.key` 等其他文件绝不打开。"""
        out = []
        for source, root in (("user", self.home), ("bridge", self.bridge_home)):
            folder = os.path.join(root, "sessions")
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if not _REGISTRY_RE.match(entry.name):
                    continue
                data = _read_json(entry.path)
                if not data or not _is_session_id(data.get("sessionId")):
                    continue
                try:
                    if int(data.get("pid") or 0) != int(entry.name[:-5]):
                        continue
                except (TypeError, ValueError):
                    continue
                out.append((source, data))
        return out

    def _unregistered_claude_near(self, table: ProcTable, sid: str) -> bool:
        """ps 里有没有一个**没写注册表**的 claude 可能正在打开这个会话。

        判据（任一即算）：
        - 命令行里出现这个 session id（`claude -r <id>` / `--resume <id>`）
        - 是 claude、启动不到 `_FRESH_PROC_SEC` 秒、且注册表里还没有它
        桥自己起的不算（FleetJobs 认得）。宁可多拦一次，也不分叉。
        """
        registered = set()
        for _source, rec in self._registry():
            try:
                registered.add(int(rec["pid"]))
            except (KeyError, TypeError, ValueError):
                continue
        for proc in table:
            if proc.pid in registered or self.jobs.owns(proc.pid, table):
                continue
            args = proc.args or ""
            head = args.split(" ", 1)[0]
            if not (head == "claude" or head.endswith("/claude")):
                continue
            if sid and sid in args:
                return True
            if 0 <= proc.etime_sec < _FRESH_PROC_SEC:
                return True
        return False

    def _is_courier_cwd(self, cwd: str) -> bool:
        """桥的信使（relay）在 `<bridge_home>/relay` 里跑。它的注册表行和 jsonl 都落在用户的 ~/.claude 里，
        🔴 第 14 轮复审：不滤掉就会以「carher-relay」排进主人的总览，点进去「接着聊」是在跟我们自己的信使说话。"""
        if not cwd:
            return False
        root = os.path.join(self.bridge_home, "relay")
        try:
            real, base = os.path.realpath(cwd), os.path.realpath(root)
        except OSError:
            real, base = cwd, root
        return real == base or real.startswith(base + os.sep)

    def _live_holders(self, table: ProcTable) -> dict:
        """sessionId → [持有者]。持有者 = {pid, owner, status, entrypoint, rec}，只收**活着且对得上**的。"""
        holders: dict = {}
        for source, rec in self._registry():
            if self._is_courier_cwd(str(rec.get("cwd") or "")):
                continue
            pid = int(rec["pid"])
            start = rec.get("procStart") or rec.get("startedAt")
            if not table.same_process(pid, start):
                continue  # 进程没了，或者 pid 被别人复用了
            proc = table.get(pid)
            entry = str(rec.get("entrypoint") or "")
            if self.jobs.owns(pid, table):
                owner = "bridge"
            elif source == "bridge":
                owner = "bridge"
            elif entry == "cli":
                owner = "terminal"
            elif entry == "claude-desktop-3p":
                owner = "app"
            else:
                owner = "unknown"  # sdk-cli：别的脚本 / agent 起的 claude -p
            raw = str(rec.get("status") or "")
            status = "stopped" if proc is not None and proc.stopped else _STATUS_MAP.get(raw, "unknown")
            holders.setdefault(rec["sessionId"], []).append(
                {"pid": pid, "owner": owner, "status": status, "raw": raw, "entrypoint": entry,
                 "source": source, "rec": rec}
            )
        # 桥自己起的、还没来得及写注册表的
        for job in self.jobs.jobs(tool=TOOL):
            if not self.jobs.owns(job.pid, table):
                continue
            if not _is_session_id(job.session_id):
                # 🔴 第 14 轮复审：信使登记成 "relay-courier"，原来在这里被补成一行没名字的「🟢 relay-co」
                continue
            items = holders.setdefault(job.session_id, [])
            if not any(h["pid"] == job.pid for h in items):
                items.append({"pid": job.pid, "owner": "bridge", "status": "running", "raw": "",
                              "entrypoint": "sdk-cli", "source": "jobs", "rec": {}})
        return holders

    # ---------- 找文件 ----------

    def _project_dirs(self, root: str = "") -> list:
        try:
            return [e.path for e in os.scandir(os.path.join(root or self.home, "projects")) if e.is_dir()]
        except OSError:
            return []

    def _jsonl_path(self, sid: str, include_bridge: bool = False) -> str:
        """按 id 找 `projects/*/<id>.jsonl`；不自己拼目录名（编码规则有坑：/tmp 会变 /private/tmp）。

        `include_bridge`：飞书对话自己的会话在桥的配置目录里，**只读**时也去那边找；发消息绝不带它。
        """
        if not _is_session_id(sid):
            return ""
        roots = [self.home] + ([self.bridge_home] if include_bridge else [])
        for root in roots:
            for folder in self._project_dirs(root):
                path = os.path.join(folder, sid + ".jsonl")
                if os.path.isfile(path):
                    return path
        return ""

    def _recent_jsonls(self, want: int) -> list:
        """所有项目目录下的 `<uuid>.jsonl`（只看一层，子 agent 在子目录里）按 mtime 倒序取 want 个。"""
        found = []
        for folder in self._project_dirs():
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                name = entry.name
                if not name.endswith(".jsonl") or not _is_session_id(name[:-6]):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                found.append((mtime, name[:-6], entry.path))
        found.sort(reverse=True)
        return found[:want]

    # ---------- 能不能发 ----------

    def _get_policy(self, override: Any = None) -> Any:
        policy = override if override is not None else self._policy
        if callable(policy) and not hasattr(policy, "allowed_tools"):
            try:
                policy = policy()
            except Exception:
                return None
        if policy is None or not hasattr(policy, "allowed_tools"):
            return None
        return policy

    def _why_not_send_idle(self, sid: str, cwd: str, path: str, app_meta: Optional[dict],
                           entrypoint: str, policy: Any) -> str:
        """没有活进程占着的会话，还有哪些理由不能发。空串 = 能发。

        🔴 第 14 轮（主人拍板「能，后台接着跑」）：App 里**关着**的会话和终端里关着的一样，
        走 `claude -p --resume` 后台接着跑、按手机这边的档位。App **开着**时有进程占着，
        走不到这里（`_why_not_send_held` / relay 那条路管）。`app_meta` / `entrypoint` 留着签名不动。
        """
        if app_meta is not None and app_meta.get("superseded"):
            return WHY_SEND_SUPERSEDED
        if self._is_courier_cwd(cwd):
            return WHY_SEND_COURIER
        if policy is None:
            return WHY_SEND_NO_POLICY
        if not cwd or not os.path.isdir(cwd):
            return "它原来的项目目录已经不在了，没法接着跑。"
        try:
            inside = policy.resolve_workspace(cwd)
        except Exception:
            inside = None
        if not inside:
            return "这个项目不在手机能用的目录里，先不发。点「📂 换项目」看现在能用哪些；要放开得在电脑上改这个助手的设置。"
        if path:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            own = self._last_own_write.get(sid, 0)
            if mtime and self._now() - mtime < RECENT_WRITE_SEC and mtime > own + 2:
                return WHY_SEND_RECENT
        return ""

    @staticmethod
    def _why_not_send_held(items: list) -> str:
        owners = {h["owner"] for h in items}
        if "terminal" in owners:
            return WHY_SEND_TERMINAL
        if "app" in owners:
            return WHY_SEND_APP
        if "unknown" in owners:
            return WHY_SEND_SCRIPT
        if any(h["source"] == "bridge" for h in items):
            return WHY_SEND_BRIDGE_CHAT
        return WHY_SEND_BRIDGE_BUSY

    def _why_not_stop_held(self, items: list, table: ProcTable) -> str:
        if any(self.jobs.owns(h["pid"], table) for h in items):
            return ""
        owners = {h["owner"] for h in items}
        if "app" in owners and "terminal" not in owners:
            return WHY_STOP_APP
        if owners == {"bridge"}:
            return WHY_STOP_BRIDGE_CHAT
        return WHY_STOP_TERMINAL

    # ---------- running ----------

    def running(self) -> list:
        self.last_error = ""
        table = self._table()
        if table.error:
            # 没有 ps 就分不清死活（注册表里常有死进程留下的文件），宁可一个都不列
            self.last_error = "看不出 Claude 哪些在跑：" + table.error
            return []
        try:
            holders = self._live_holders(table)
        except Exception as exc:
            self.last_error = friendly_error(exc, "读 Claude 运行列表失败")
            return []
        out = []
        for sid, items in holders.items():
            try:
                out.append(self._running_session(sid, items, table))
            except Exception as exc:
                self.last_error = friendly_error(exc, "有个 Claude 会话读不出来")
        out.sort(key=lambda s: (_STATUS_RANK.get(s.status, 9), -s.updated_at))
        return out

    def _running_session(self, sid: str, items: list, table: ProcTable) -> Session:
        items = sorted(items, key=lambda h: _STATUS_RANK.get(h["status"], 9))
        status = items[0]["status"]
        # 归属按「谁最不能碰」排：终端 > App > 来路不明 > 桥
        present = {h["owner"] for h in items}
        owner = next((o for o in ("terminal", "app", "unknown", "bridge") if o in present), "unknown")
        recs = [h["rec"] for h in items if h["rec"]]
        cwd = next((r.get("cwd") for r in recs if isinstance(r.get("cwd"), str) and r.get("cwd")), "")
        updated = 0.0
        for r in recs:
            updated = max(updated, _ms_to_epoch(r.get("updatedAt")), _ms_to_epoch(r.get("statusUpdatedAt")))
        path = self._jsonl_path(sid, include_bridge=True)
        if path:
            try:
                updated = max(updated, os.path.getmtime(path))
            except OSError:
                pass
        title = self._running_title(recs, path)
        surface = "app" if owner == "app" else ("bridge" if owner == "bridge" else "cli")
        why_stop = self._why_not_stop_held(items, table)
        extra = {
            "pids_status": {str(h["pid"]): h["raw"] or h["status"] for h in items},
            "entrypoint": items[0]["entrypoint"],
        }
        if any(h["raw"] == "shell" for h in items):
            extra["shell"] = True
        if len(items) > 1:
            extra["copies"] = len(items)  # 同一个会话开在好几个终端里（实测见过）
        version = next((r.get("version") for r in recs if r.get("version")), "")
        if version:
            extra["version"] = str(version)
        # 「等你」⇒ 它在问什么 / 等你确认哪一步（详情卡上显示）。只有等你时才读，别的状态不花这次 IO
        pending = self._pending_for(path) if status == "waiting" else None
        # 第 32 轮：开着但闲着的（终端里跑完一轮停在那儿）⇒ 看最后一轮是跑完了还是报错了。
        # 在跑 / 等你的不读：那两个状态排在前面，读了也不显示。
        last_turn, last_turn_at = self._last_turn_for(path) if status == "idle" else ("", 0.0)
        can_relay, why_relay = self._relay_capability(items, owner, status)
        if can_relay:
            # 卡片上要显示「正在和谁说话」，而 relay 的地址就是这个 name
            extra["relay_name"] = next(
                (str(h["rec"].get("name") or "").strip() for h in items if h["rec"].get("name")), ""
            )
        return Session(
            tool=TOOL, surface=surface, id=sid, title=title, cwd=cwd, status=status,
            pids=tuple(h["pid"] for h in items), owner=owner, updated_at=updated,
            can_send=False, why_not_send=self._why_not_send_held(items),
            can_stop=not why_stop, why_not_stop=why_stop,
            can_relay=can_relay, why_not_relay=why_relay, extra=extra, pending=pending,
            last_turn=last_turn, last_turn_at=last_turn_at,
        )

    @staticmethod
    def _last_turn_for(path: str) -> tuple:
        """jsonl 尾巴 → 最后一轮怎么结束的；读不到 / 出任何错 ⇒ ("", 0)（卡上不标，同 `_pending_for`）。"""
        if not path:
            return "", 0.0
        try:
            records, _ = _read_tail(path, TITLE_TAIL_BYTES)
            return _last_turn(records)
        except Exception:  # noqa: BLE001
            logger.debug("读最后一轮失败 path=%s", path, exc_info=True)
            return "", 0.0

    @staticmethod
    def _pending_for(path: str) -> Optional[PendingAsk]:
        """jsonl 尾部 → 它卡在哪一步；读不到 / 找不到 / 出任何错 ⇒ None（卡上那段不显示，卡照常出）。

        🔴 只读尾巴（`_read_tail`，有上限）：本机有 200MB 的 jsonl，整个读一次就卡住扫描。
        """
        if not path:
            return None
        try:
            records, _ = _read_tail(path, PENDING_TAIL_BYTES)
            return _pending_ask(records)
        except Exception:  # noqa: BLE001 —— 这一段是锦上添花，绝不能让会话列表 / 详情卡出不来
            logger.debug("读「等你」那一步失败 path=%s", path, exc_info=True)
            return None

    def _relay_capability(self, items: list, owner: str, status: str) -> tuple:
        """卡片上该不该出现 [接着聊]（relay 那条路）→ (能不能, 不能的话为什么)。

        🔴 这里是**便宜的预判**，不是闸门。真正的闸门在 `relay.Relay.check()` ——
        它还会比权限档同类、查名字有没有重。卡片上的状态可能是几分钟前的，
        所以发之前必须再查一次（见 `Relay.check` 的红字）。这里只做能从注册表一眼看出的几项：
        少画一个点了必然被拒的按钮，比画出来让人点了失望好。
        """
        # 🔴 2026-11-06 第 13 轮：Claude App 会话**也给**。第 12 轮实测的阻塞是「桥的信使是 bypass、App 会话是 auto
        # （prompting 类）⇒ 被扣住」—— 现在 relay 按目标权限档挑同类的信使（`Relay.courier_for`），这一条解开了。
        # App 会话的其余条件和终端一样：有 socket、有名字（下面那几行照查）。
        # 同事 11-06 原话：「悬浮按钮有了，点了某个session 会话之后 为啥不能像我一样 有接着聊的按钮呢」。
        if owner not in ("terminal", "app"):
            # 别的脚本 / 桥自己的会话都不走这条路：会话间消息只在终端 TUI 上实测过
            return False, WHY_RELAY_NOT_TERMINAL
        if status == "stopped":
            return False, WHY_RELAY_SUSPENDED
        recs = [h["rec"] for h in items if h["rec"]]
        if not any(str(r.get("messagingSocketPath") or "") for r in recs):
            # 老版本的 claude 不写这个字段 ⇒ 没有会话间消息这回事
            return False, WHY_RELAY_OLD_VERSION
        if not any(str(r.get("name") or "").strip() for r in recs):
            return False, WHY_RELAY_NO_NAME
        return True, ""

    def _running_title(self, recs: list, path: str) -> str:
        # 注册表 name：用户起的 / 自动起的直接用；derived 是「目录名-两位 hex」，没信息量，往后排
        derived = ""
        for r in recs:
            name = r.get("name")
            if isinstance(name, str) and name.strip():
                if r.get("nameSource") == "derived":
                    derived = derived or name.strip()
                else:
                    return short_title(name)
        if path:
            info = _scan_head(path)
            _tail_titles(path, info)
            best = info["custom_title"] or info["ai_title"] or info["first"]
            if best:
                return short_title(best)
        return short_title(derived)

    # ---------- list ----------

    def list(self, limit: int = 20) -> list:
        """活的在前，再按 jsonl 的 mtime 补最近的历史会话。App 里归档了的不列。"""
        limit = max(int(limit or 0), 0)
        if limit == 0:
            return []
        live = self.running()
        # ps 读不到时看不出谁占着 ⇒ 历史会话也一律不让发（下面标记），否则卡上会出现
        # 一个点了必然被拒的 [接着聊]（2026-11-04 稳定性审查）。
        ps_failed = bool(self.last_error) and not live
        errors = [self.last_error] if self.last_error else []
        # 🔴 **在跑的全部列出来**，`limit` 只管历史会话。原来 live[:limit] ⇒ 本机 18 个在跑、
        # 总览只显示 6 个，/status 说「在跑 6 个」，其余 12 个点不开（2026-11-04 稳定性审查）。
        out = list(live)
        seen = {s.id for s in out}
        history_budget = limit
        try:
            app = _app_index(self.app_root)
        except Exception as exc:
            app = {}
            errors.append(friendly_error(exc, "读不到 Claude App 的会话标题"))
        try:
            recent = self._recent_jsonls(limit + len(seen) + 10)
        except Exception as exc:
            recent = []
            errors.append(friendly_error(exc, "读不到 Claude 历史会话"))
        policy = self._get_policy()
        added = 0
        for mtime, sid, path in recent:
            if added >= history_budget:
                break
            if sid in seen:
                continue
            meta = app.get(sid)
            if meta and meta["archived"]:
                continue
            try:
                sess = self._history_session(sid, path, mtime, meta, policy)
                if sess is None:
                    seen.add(sid)
                    continue
                if ps_failed and sess.can_send:
                    sess.can_send = False
                    sess.why_not_send = WHY_SEND_UNKNOWN
                out.append(sess)
                added += 1
            except Exception as exc:
                errors.append(friendly_error(exc, "有个 Claude 历史会话读不出来"))
            seen.add(sid)
        self.last_error = "；".join(e for e in errors if e)
        return out

    def _history_session(self, sid: str, path: str, mtime: float, meta: Optional[dict], policy: Any) -> Optional[Session]:
        info = _scan_head(path)
        _tail_titles(path, info)
        cwd = info["cwd"] or (meta or {}).get("cwd", "")
        if self._is_courier_cwd(cwd):
            return None
        entry = info["entrypoint"]
        is_app = meta is not None or entry == "claude-desktop-3p"
        title = ((meta or {}).get("title") or info["custom_title"] or info["ai_title"] or info["first"])
        why = self._why_not_send_idle(sid, cwd, path, meta, entry, policy)
        extra: dict = {"live": False}
        if entry:
            extra["entrypoint"] = entry
        if meta and meta.get("status_category"):
            extra["app_status"] = meta["status_category"]
        return Session(
            tool=TOOL, surface="app" if is_app else "cli", id=sid, title=short_title(title), cwd=cwd,
            status="idle", owner="none", updated_at=mtime,
            can_send=not why, why_not_send=why, can_stop=False, why_not_stop=WHY_STOP_IDLE, extra=extra,
            last_turn=info.get("last_turn", ""), last_turn_at=info.get("last_turn_at", 0),
        )

    # ---------- read ----------

    def read(self, id: str, n: int = 4) -> list:
        self.last_error = ""
        n = max(int(n or 0), 0)
        if n == 0:
            return []
        path = self._jsonl_path(id, include_bridge=True)
        if not path:
            self.last_error = "找不到这个 Claude 会话的记录。"
            return []
        try:
            records, whole = _read_tail(path, TAIL_BYTES)
            messages = _messages(records)
            if len(messages) < n and not whole:
                records, _ = _read_tail(path, TAIL_BYTES_WIDE)
                messages = _messages(records)
        except Exception as exc:
            self.last_error = friendly_error(exc, "读 Claude 会话失败")
            return []
        return messages[-n:]

    # ---------- send ----------

    def _lock_for(self, sid: str) -> threading.Lock:
        with self._send_locks_guard:
            return self._send_locks.setdefault(sid, threading.Lock())

    def send(self, id: str, text: str, timeout: float = DEFAULT_SEND_TIMEOUT, policy: Any = None,
             approval_dir: str = "") -> ActionResult:
        """只有**没进程占着**才发：`claude -p --resume <id> --output-format json`。

        `policy` 不传就用构造时给的。工具白名单 = `policy.allowed_tools()`，红线 = `disallowed_tools()`。

        🔴 **不加载用户的 settings**（`--setting-sources project`）：用户 settings.json 里的
        allow 规则和 `defaultMode` 会叠加在白名单上（主会话 16:50 实测：不加时读工作目录外的文件
        0 次拒绝）。认证键另外从 settings.json 的 env 里只挑出来塞进子进程环境（launchd 起的桥
        环境里没有它们，丢了就是「Not logged in」）。审批钩子用 `--settings` 单独带进来，
        `approval_dir` 是这次发送的批准目录（钩子靠 CARHER_APPROVAL_DIR 找它）。
        """
        if not _is_session_id(id):
            return ActionResult(False, "会话编号不对。", str(id or ""))
        text = str(text or "").strip()
        if not text:
            return ActionResult(False, "要发的内容是空的。", id)
        lock = self._lock_for(id)
        if not lock.acquire(blocking=False):
            return ActionResult(False, WHY_SEND_BRIDGE_BUSY, id)
        try:
            return self._send_locked(id, text, timeout, self._get_policy(policy), approval_dir)
        except Exception as exc:  # 绝不抛给上层
            return ActionResult(False, friendly_error(exc), id)
        finally:
            lock.release()

    def _send_locked(self, sid: str, text: str, timeout: float, policy: Any, approval_dir: str = "") -> ActionResult:
        # 🔴 spawn 之前现查一次：用户可能刚在终端里打开它（卡片上的 can_send 可能是几分钟前的）
        table = self._table()
        if table.error:
            return ActionResult(False, "看不出现在有没有人占着它（" + table.error + "），为了不分叉先不发。", sid)
        held = self._live_holders(table).get(sid)
        if held:
            return ActionResult(False, self._why_not_send_held(held), sid)
        # 🔴 注册表有空窗：终端里刚敲 `claude -r <id>`，进程已经在 ps 里，
        # `~/.claude/sessions/<pid>.json` 要 0.2~1.7 秒后才写（2026-11-04 安全审查实测）。
        # 这个窗口里只看注册表会判「没人占着」⇒ 起 `-p --resume` ⇒ 两个进程写同一个会话 ⇒ 静默分叉。
        # 所以再看一眼 ps：命令行里带着这个 id，或刚起不久、还没写注册表的 claude，都当占着。
        if self._unregistered_claude_near(table, sid):
            return ActionResult(False, WHY_SEND_JUST_OPENED, sid)
        path = self._jsonl_path(sid)
        if not path:
            return ActionResult(False, "找不到这个会话的记录，可能已经删掉了。", sid)
        info = _scan_head(path)
        meta = _app_index(self.app_root).get(sid)
        cwd = info["cwd"] or (meta or {}).get("cwd", "")
        why = self._why_not_send_idle(sid, cwd, path, meta, info["entrypoint"], policy)
        if why:
            return ActionResult(False, why, sid)

        hook = self._hook()
        if getattr(policy, "mode", "readonly") != "readonly" and not (hook and approval_dir):
            # 可改 / 全授权档靠审批钩子挡越界写和红线命令；钩子没带上就只给只读档发
            return ActionResult(False, WHY_SEND_NO_HOOK, sid)

        argv = self._send_argv(sid, text, policy, hook)
        env = self._send_env(approval_dir if hook else "")
        spawned: list = []

        def on_spawn(pid: int) -> None:
            spawned.append(pid)
            self.jobs.register_spawned(pid, TOOL, sid)

        try:
            result = self.runner.run(
                argv, timeout=float(timeout or DEFAULT_SEND_TIMEOUT), cwd=cwd, env=env,
                on_spawn=on_spawn,  # stdin 默认 DEVNULL
            )
        finally:
            for pid in spawned:
                self.jobs.forget(pid)
            self._last_own_write[sid] = self._now()
        out = _send_result(sid, result)
        if spawned:
            out = ActionResult(out.ok, out.text, out.session_id, delivered=True)
        return out

    # ---------- relay：进会话聊天（送进终端里开着的那个会话） ----------

    def _relay(self) -> Any:
        """这家的 relay 单例（懒建）。

        `relay.py` 反过来 import 这个模块（它用这里的 jsonl 解析和判活），所以**在函数里 import**。
        单例是必须的：relay 里有一个常驻信使进程和一张「发过哪些话」的表（挡自动重发），
        每次新建一个 = 每句话起一个 claude（慢 7~40 秒）+ 幂等保护失效。
        """
        existing = getattr(self, "_relay_obj", None)
        if existing is not None:
            return existing
        from .relay import Relay

        with self._send_locks_guard:  # 复用这把锁，别让两个线程各建一个信使
            existing = getattr(self, "_relay_obj", None)
            if existing is None:
                existing = Relay(self)
                self._relay_obj = existing
        return existing

    def can_relay(self, id: str) -> tuple:
        """发之前的那次复查 → (能不能, 不能的话一句大白话)。给卡片和 app.py 用。"""
        try:
            decision = self._relay().check(str(id or ""))
        except Exception as exc:
            return False, friendly_error(exc, "看不出能不能送进去")
        return bool(decision.ok), ("" if decision.ok else decision.why)

    def relay(self, id: str, text: str, timeout: float = 120.0, mode: Any = None) -> ActionResult:
        """把一句话送进电脑终端里**正开着**的这个会话，并把回复取回来。

        `mode` 收下就扔：relay 不起目标进程，目标用的是**它自己在电脑上那套权限**，
        手机这边的档位管不到它（所以进对话模式前要让用户确认一次，见 `consent.py`）。
        签名和 Codex / Cursor 的 `relay()` 对齐，`control.send(relay=True)` 才能一视同仁地分派。
        """
        try:
            return self._relay().send(str(id or ""), str(text or ""), timeout=float(timeout or 120.0))
        except Exception as exc:  # 绝不抛给上层
            return ActionResult(False, friendly_error(exc), str(id or ""))

    def relay_stop(self, id: str) -> ActionResult:
        """对话模式里的 `/stop`：**停不了**，照实说在哪停。

        实测 SIGINT 会把用户整个会话关掉（不是停这一轮），所以 relay 一个信号都不发给目标。
        先看一眼有没有桥自己起的进程（那种能停），没有就给那句大白话。
        """
        from .relay import WHY_STOP_TARGET

        sid = str(id or "")
        if self.jobs.jobs(tool=TOOL, session_id=sid):
            return self.stop_own(sid)
        return ActionResult(False, WHY_STOP_TARGET, sid)

    def close_relay(self) -> None:
        """收掉常驻信使（服务退出 / 测试收尾用）。没起过就什么都不做。"""
        existing = getattr(self, "_relay_obj", None)
        if existing is not None:
            try:
                existing.close()
            except Exception:
                logger.exception("收信使时出错")

    def _hook(self) -> dict:
        hook = self._hook_settings
        if callable(hook):
            try:
                hook = hook()
            except Exception:
                return {}
        return hook if isinstance(hook, dict) and hook.get("hooks") else {}

    def _auth(self) -> tuple:
        """settings.json 里的认证键：(env 键, 顶层键)。清单和桥自己那份共用（approval.inherited_auth）。"""
        from ..approval import inherited_auth

        return inherited_auth(self.user_settings)

    def _send_env(self, approval_dir: str = "") -> dict:
        """会话在哪个配置目录里，就用哪个目录的配置续写。

        默认 `~/.claude` ⇒ **去掉** CLAUDE_CONFIG_DIR（桥自己那份绝不能漏进来，否则会在桥的目录里
        找不到会话、另起一个）；别的目录（测试）⇒ 显式指过去。
        认证键从 settings.json 挑进来（`--setting-sources project` 不加载它，见 send 的红字）。
        """
        env = dict(os.environ)
        if os.path.realpath(self.home) == os.path.realpath(os.path.expanduser("~/.claude")):
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = self.home
        auth_env, _ = self._auth()
        for key, value in auth_env.items():
            env[key] = str(value)
        env.pop("CARHER_APPROVAL_DIR", None)
        if approval_dir:
            env["CARHER_APPROVAL_DIR"] = approval_dir
        return env

    def _send_argv(self, sid: str, text: str, policy: Any, hook: Optional[dict] = None) -> list:
        """和 runner.py 同一个形状：prompt 紧跟 `-p`，白名单 / 红线放最后（这两个参数吃多个值）。

        以「-」开头的话会被当成参数 ⇒ 前面垫一个空格（模型看不出差别）。
        """
        prompt = " " + text if text.startswith("-") else text
        # --settings 只带钩子 + 认证用的顶层键（apiKeyHelper 只能走 settings）；不带任何权限规则
        _, auth_top = self._auth()
        extra = dict(auth_top)
        extra.pop("permissions", None)
        if hook:
            extra["hooks"] = hook["hooks"]
        argv = [
            self.claude_bin, "-p", prompt,
            "--resume", sid,
            "--output-format", "json",
            # manual = 没批准的工具直接拒，不挂在那儿等人（和 runner.py 同一档）
            "--permission-mode", "manual",
            # 不加载 user / local：用户的 allow 规则、defaultMode、hooks 都不进来
            # 🔴 **一个来源都不加载**（空串），不是 "project"。
            # 2026-11-04 安全审查：project 级 settings 的 allow 规则也会叠加在 Policy 上，
            # 而手机能接着聊的正是别人的仓库（比如 carher-admin 自带 `Bash(lark-cli im *)`），
            # 克隆来的第三方仓库也能自带 allow。实测（一次性目录，已清理）：
            # 空串 ⇒ 读白名单外文件被拒（denials=1）；project ⇒ 已信任的目录里会放行。
            # 审批钩子仍由 --settings 单独带进来，不受影响。
            "--setting-sources", "",
            # 用户的 MCP 服务也不起（白名单里本来就没有它们的工具）
            "--strict-mcp-config",
        ]
        if extra:
            argv += ["--settings", json.dumps(extra, ensure_ascii=False)]
        for tool in policy.allowed_tools():
            argv += ["--allowed-tools", str(tool)]
        for tool in policy.disallowed_tools():
            argv += ["--disallowed-tools", str(tool)]
        return argv

    # ---------- start：手机上新建会话 ----------

    def start(self, cwd: str, text: str, timeout: float = DEFAULT_SEND_TIMEOUT, policy: Any = None,
              approval_dir: str = "", model: str = "") -> ActionResult:
        """在 `cwd` 新开一个会话，第一句是 `text`。参数和 `send()` 同一套（档位、白名单、红线、审批钩子），
        只是把 `--resume <id>` 换成 `--session-id <新 uuid>`：id 由我们先定好，起进程前就能登记，
        这样「停下」和占用判断从第一秒起就认得它。
        """
        import uuid

        text = str(text or "").strip()
        if not text:
            return ActionResult(False, "要发的内容是空的。", "")
        policy = self._get_policy(policy)
        if policy is None:
            return ActionResult(False, WHY_SEND_NO_POLICY, "")
        if not cwd or not os.path.isdir(cwd):
            return ActionResult(False, "这个项目目录不在了，换一个再开。", "")
        try:
            inside = policy.resolve_workspace(cwd)
        except Exception:
            inside = None
        if not inside:
            return ActionResult(False, "这个项目不在手机能用的目录里，先不开。点「📂 换项目」看现在能用哪些。", "")
        hook = self._hook()
        if getattr(policy, "mode", "readonly") != "readonly" and not (hook and approval_dir):
            return ActionResult(False, WHY_SEND_NO_HOOK, "")
        sid = str(uuid.uuid4())
        argv = self._send_argv(sid, text, policy, hook)
        at = argv.index("--resume")
        argv[at:at + 2] = ["--session-id", sid]
        if model:
            from .codex import safe_model_name

            if not safe_model_name(model):
                return ActionResult(False, f"模型名不对：{short_title(str(model), 40)}", "")
            argv[at + 2:at + 2] = ["--model", model]
        env = self._send_env(approval_dir if hook else "")
        spawned: list = []

        def on_spawn(pid: int) -> None:
            spawned.append(pid)
            self.jobs.register_spawned(pid, TOOL, sid)

        try:
            result = self.runner.run(argv, timeout=float(timeout or DEFAULT_SEND_TIMEOUT), cwd=inside,
                                     env=env, on_spawn=on_spawn)
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, "没能启动 claude"), "")
        finally:
            for pid in spawned:
                self.jobs.forget(pid)
            self._last_own_write[sid] = self._now()
        out = _send_result(sid, result)
        if not spawned:
            return ActionResult(out.ok, out.text, "" if not out.ok else out.session_id)
        got = out.session_id or sid
        if not out.ok and not self._jsonl_path(got):
            # 进程起来了但会话记录没落盘（比如没登录就退出）⇒ 没有能接着聊的会话
            return ActionResult(False, out.text, "")
        return ActionResult(out.ok, out.text, got, delivered=True)

    # ---------- stop ----------

    def stop(self, id: str) -> ActionResult:
        if self.jobs.jobs(tool=TOOL, session_id=str(id or "")):
            return self.stop_own(id)
        table = self._table()
        held = self._live_holders(table).get(id) if not table.error else None
        if not held:
            return ActionResult(False, WHY_STOP_IDLE, id)
        return ActionResult(False, self._why_not_stop_held(held, table) or WHY_STOP_TERMINAL, id)


def _messages(records: list) -> list:
    """jsonl 记录 → 给人看的 Message 列表（旧 → 新）。

    只留非 sidechain 的 user / assistant 文本；同一条 assistant 回复被拆成多行（一块一行）时合并。
    """
    out: list = []
    last_msg_id = None
    for rec in records:
        kind = rec.get("type")
        if kind not in ("user", "assistant") or rec.get("isSidechain"):
            last_msg_id = None if kind in ("user", "assistant") else last_msg_id
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        raw = _content_text(message.get("content"))
        at = _iso_to_epoch(rec.get("timestamp"))
        if kind == "user":
            last_msg_id = None
            if rec.get("isMeta") and "<cross-session-message" not in raw:
                continue  # 系统塞的说明，不是人说的
            text = _clean_user_text(raw)
            if text:
                out.append(Message("user", text, at))
            continue
        text = raw.strip()
        msg_id = message.get("id")
        if not text:
            continue
        if msg_id and msg_id == last_msg_id and out and out[-1].role == "assistant":
            prev = out.pop()
            out.append(Message("assistant", prev.text + "\n" + text, prev.at or at))
        else:
            out.append(Message("assistant", text, at))
        last_msg_id = msg_id
    return out


# 别的工具在等你确认时，摘要从 input 的哪个字段取（按顺序取第一个有值的）。
# 🔴 只取一个短字段，不把整个 input dump 成 JSON：Write 的 content 动辄几百 KB，而且这里取的是**原文**
# （脱敏在卡片那边整段做，见 `cards.pending_elements`）。
_PENDING_SUMMARY_KEYS = ("command", "file_path", "notebook_path", "url", "query", "pattern",
                         "description", "path", "prompt")


def _pending_summary(inp: Any) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in _PENDING_SUMMARY_KEYS:
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _ask_questions(inp: Any) -> tuple:
    """AskUserQuestion 的 input → (AskQuestion, …)。实测形状（本机 jsonl）：
    `questions[{question, header, multiSelect, options[{label, description, preview}]}]`。形状不对的题跳过。
    """
    out = []
    questions = inp.get("questions") if isinstance(inp, dict) else None
    for q in questions if isinstance(questions, list) else ():
        if not isinstance(q, dict) or not isinstance(q.get("question"), str) or not q["question"].strip():
            continue
        options = []
        for opt in q.get("options") if isinstance(q.get("options"), list) else ():
            if isinstance(opt, dict) and isinstance(opt.get("label"), str) and opt["label"].strip():
                desc = opt.get("description")
                options.append((opt["label"], desc if isinstance(desc, str) else ""))
        header = q.get("header")
        out.append(AskQuestion(question=q["question"], header=header if isinstance(header, str) else "",
                               multi=bool(q.get("multiSelect")), options=tuple(options)))
    return tuple(out)


def _pending_ask(records: list) -> Optional[PendingAsk]:
    """jsonl 记录（旧 → 新）→ 最后一条**还没有 tool_result 的 tool_use**；没有 ⇒ None。

    - 只看主线（sidechain 是子 agent 的，它等的不是你）
    - 人在后面又说了话（文字，不是 tool_result）⇒ 前面没回的都是旧账（被打断时可能不落 tool_result），清掉
    - 一条回复里并行好几个 tool_use、只回了一部分 ⇒ 取最后一个没回的
    """
    open_uses: dict = {}  # tool_use_id → (name, input)；dict 保插入顺序 ⇒ 最后一个 = 最新
    for rec in records:
        kind = rec.get("type")
        if kind not in ("user", "assistant") or rec.get("isSidechain"):
            continue
        message = rec.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if kind == "assistant":
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("id"):
                    open_uses[str(block["id"])] = (str(block.get("name") or ""), block.get("input"))
            continue
        if rec.get("isMeta"):
            continue
        if isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        ):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    open_uses.pop(str(block.get("tool_use_id") or ""), None)
            continue
        if _clean_user_text(_content_text(content)):
            open_uses.clear()
    if not open_uses:
        return None
    name, inp = list(open_uses.values())[-1]
    if not name:
        return None
    if name == "AskUserQuestion":
        questions = _ask_questions(inp)
        if questions:
            return PendingAsk(tool=name, questions=questions)
    return PendingAsk(tool=name, summary=_pending_summary(inp))


def _send_result(sid: str, result: Any) -> ActionResult:
    if getattr(result, "error", ""):
        err = str(result.error)
        # SubprocessRunner 的 error 自己就以「没能启动 …」开头 ⇒ 别再套一层
        return ActionResult(False, err if err.startswith("没能启动") else "没能启动 claude：" + err, sid)
    if getattr(result, "timed_out", False):
        return ActionResult(False, "等太久没回完，已经把这次停下了；会话还在，可以再发。", sid)
    payload = None
    for line in reversed((result.stdout or "").strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            payload = data
            break
    if payload is None:
        tail = short_title((result.stderr or result.stdout or "").strip(), 120)
        return ActionResult(False, "claude 没给出回复" + (f"：{tail}" if tail else "。"), sid)
    reply = payload.get("result")
    reply = reply if isinstance(reply, str) else ""
    if payload.get("stop_reason") == "tool_deferred":
        # 审批钩子 defer 了：进程已经退出，那一步**没做**。不能装成正常回复
        lead = (reply.strip() + "\n\n") if reply.strip() else ""
        return ActionResult(False, lead + WHY_SEND_DEFERRED, sid)
    if result.returncode != 0 or payload.get("is_error"):
        return ActionResult(False, "claude 报错了：" + (short_title(reply, 200) or "没有说明"), sid)
    got = payload.get("session_id")
    if isinstance(got, str) and got and got != sid:
        # 正常不会发生（没带 --fork-session）。真发生了要让人知道，不能装成接上了
        return ActionResult(True, (reply or "（没有文字回复）") + f"\n\n⚠️ 这次落到了新会话 {got[:8]}，没接在原来那段后面。", got)
    return ActionResult(True, reply or "（没有文字回复）", sid)
