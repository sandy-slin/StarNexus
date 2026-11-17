"""报错自动上报：桥出错了，把**报错本身**送到 198，不用让同事截图。

## 为什么要有（第 38 轮，主人原话）

「你在新版本里增加搜集上传用户报错，方便问题定位，不要搜集其他信息；Windows 你要搞定。」

11-17 的现场：两台 Mac 有问题，一台一天里在新旧版之间来回切 12 次、一台 `u=crashloop`，
我们手上**一个字的报错都没有** —— 只能等人截图。而截图既慢又不全（看不到栈、看不到哪一行）。

## 只传报错，不传别的（隐私红线）

字段是**白名单**，多一个都算越线（`tests/test_r38_errreport.py` 钉着）：

| 字段 | 内容 | 为什么可以发 |
|---|---|---|
| `v` | BUILD_ID | 哪一版，已经天天在发 |
| `id` | install_id（16 位随机 hex，同 selfupdate） | 哪一台，随机、不推导自主机名 |
| `os` / `app` | mac/linux/windows、`cli_…` | 同 selfupdate 那 5 个字段，没有 app_secret |
| `at` | 发生时间（本机时区） | |
| `src` | log / uncaught / thread / crash | 从哪条路收到的 |
| `log` | logger 名（`feishu-bridge.*`） | 代码里的字符串 |
| `msg` | **日志模板** `record.msg`，**不是**格式化后的消息 | 模板是源码里的常量；格式化后的会带进用户原话 |
| `type` / `emsg` | 异常类型 / 异常消息（过 `redact` + 本文件再裁一次，≤300 字） | 定位用；路径/ID/引号里的原文都会抹掉 |
| `frames` | 只取 `bridge/` 里的帧：`bridge/app.py:123:func` | **不带**源码行、**不带**绝对路径 |
| `fp` / `n` | 同一指纹当天第几次 | 去重 + 频率 |

🔴 **绝不发**：会话内容、用户说的话、文件内容、绝对路径 / 用户名 / 电脑名、app_secret / token /
open_id；`record.args` 一个都不发（那里装着用户的话）。

## 报不出来怎么办（起不来的情况）

桥自己崩在 import 阶段时，本模块可能根本没跑起来 ⇒ 两道路：

1. **进程内**：`logger.error/exception` → 日志 handler；`sys.excepthook` / `threading.excepthook` /
   asyncio 的 loop 异常（SDK 默认会走 `asyncio` logger）→ 也收。
2. **起不来**：`launch_guard.sh` 数到「连续 5 次活不过 60 秒」时直接调本文件的 `--crash-log`
   模式（`python -m bridge.errreport --home <APP_HOME> --crash-log <agent.log>`）：模拟 import
   `bridge.app` 复现启动期异常，复现不了就读 `agent.log` 尾部最后一个 Traceback。

## 不许把桥搞挂 / 卡住

- `emit` 只做内存操作（裁剪 + 入队），不碰网络、不抛（全部 `try/except`）。
- 网络在后台守护线程里，超时 8 秒，失败静默丢；当天每个指纹只发 1 条、每台最多 30 条。
- 测试 / `CARHER_NO_REPORT=1` / 开发机（没有 BUILD_ID）不发。
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.request
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("feishu-bridge.errreport")

# 🔴 写死：和 selfupdate 一样，不从配置、不从环境变量读（这个 URL 决定报错去哪，也决定谁能收到）
_ALLOWED_HOST = "chat.auto-link.com.cn"
REPORT_URL = f"https://{_ALLOWED_HOST}/ai/api/install-report"

# 🔴 只许这几个键（顶层 + 每条）。加一个 = 越过隐私红线。
ITEM_KEYS = ("n", "src", "at", "log", "msg", "type", "emsg", "frames", "fp")
TOP_KEYS = ("kind", "v", "id", "os", "app", "items")
_MAX_ITEMS_PER_POST = 6
_MAX_TODAY = 30                 # 每台每天最多发这么多条（含 crash）
_MAX_QUEUE = 60                 # 内存里最多攒这么多条；超了丢最新的（旧的是现场）
_MAX_MSG = 200                  # 日志模板上限
_MAX_EMSG = 300                 # 异常消息上限（brief 要求 300）
_MAX_FRAMES = 12
_FLUSH_AFTER_SEC = 20.0         # 条进来了等这么久再发：同一指纹的连击能合成 n
_HEARTBEAT_SEC = 30.0           # 队列里有东西时，隔这么久重试一次
_SEND_TIMEOUT = 8.0
_TRY_MAX = 3                    # 同一个指纹最多试这么多次，再失败当天放弃
_STATE_FILE = "errreport_state.json"
_SPOOL_FILE = "errreport_pending.json"
_INSTALL_ID_FILE = "install_id"
_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_APP_ID_RE = re.compile(r"^cli_[A-Za-z0-9]{1,40}$")
_SAFE_BUILD_RE = re.compile(r"[^A-Za-z0-9._-]")
_LOGGER_RE = re.compile(r"[^A-Za-z0-9_.-]")
_TYPE_RE = re.compile(r"[^A-Za-z0-9_.<>]")
_FP_RE = re.compile(r"^[0-9a-f]{12}$")
# 栈帧的白名单形状：`bridge/app.py:123:_handle`（只认 bridge/ 里的相对路径）
_FRAME_RE = re.compile(r"^bridge/[A-Za-z0-9_./-]{1,80}\.py:\d{1,6}:<?[A-Za-z0-9_.<>]{1,60}>?$")
_SRC_WORDS = ("log", "uncaught", "thread", "asyncio", "crash")
# 代码目录 / 状态目录（测试里可整组替换，见 tests 的 `_paths` 夹具）
_BRIDGE_DIR = os.path.dirname(os.path.abspath(__file__))
_APP_HOME = os.path.dirname(_BRIDGE_DIR)

# ── 额外的打码（`redact.redact` 之后再过一遍）──────────────────────────
# 1) 家目录：`/Users/张三/…`、`C:\Users\xx\…` ⇒ `~/…`（把用户名去掉，尾巴留着还能看出是哪个文件）
_HOME_RE = re.compile(r"(?i)(?:/Users/[^/\s:'\"]+|/home/[^/\s:'\"]+|[A-Za-z]:\\Users\\[^\\\s:'\"]+)")
# 2) 其他绝对路径：带上项目名/客户名的，只留最后一段
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_./-])(?:[A-Za-z]:)?(?:/|\\\\)[^\s:'\"，。；、）)\]】]+[\\/][^\s:'\"，。；、）)\]】]*")
# 3) 飞书各种 ID（open_id / chat_id / union_id / message_id / app_id）+ 邮箱 + URL 里的参数
_ID_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:ou|oc|on|om|un|cli|cli_a|cig)_[A-Za-z0-9_-]{4,}"
    r"|(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
_URL_CRED_RE = re.compile(r"(?i)(https?://)([^\s/]+)@")
_LONG_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9+/=_-]{32,}")
# 4) 异常消息里「引号包着的东西」：正常报错引的是标识符（`'app_id'`），
#    带空格/中文的多半是用户原话/文件内容 ⇒ 只留引号形状
_QUOTED_RE = re.compile(r"(['\"`])([^'\"`\n]{0,400}?)\1")
# 5) 中文片段：只留「**我们源码里写过**的那几句」（异常消息里的中文若是运行时拼进去的
#    用户原话，源码里就没有这句 ⇒ 打码）。判据见 `_mask_unknown_cjk` / `_source_literals`。
_MASK_MARKS = ("[已脱敏]", "[ID]", "[长串]", "[路径]", "'[内容]'", "[内容]")
_CJK_RE = re.compile(r"[　-鿿＀-￯]")
_source_literals_cache: Optional[frozenset[str]] = None


def _source_literals() -> frozenset[str]:
    """`bridge/` 里所有字符串常量的集合。用来判「这句话是不是我们自己写的」。"""
    global _source_literals_cache
    if _source_literals_cache is not None:
        return _source_literals_cache
    out: set[str] = set()
    try:
        import ast

        paths = []
        for root, dirs, files in os.walk(_BRIDGE_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            paths += [os.path.join(root, f) for f in files if f.endswith(".py")]
        for path in paths:
            try:
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
            except (OSError, SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    value = node.value.strip()
                    if len(value) >= 3:
                        out.add(value)
    except Exception:  # noqa: BLE001
        pass
    _source_literals_cache = frozenset(out)
    return _source_literals_cache


def _literal_covers(token: str) -> bool:
    """这句话是不是「我们源码里写过」的：整句相等，或它是某个源码常量的一部分。"""
    if len(token) < 2:
        return False
    literals = _source_literals()
    if token in literals:
        return True
    # 短词（「客户」「预算」）在源码某处总能找到 ⇒ 只按子串认会把用户的词放过去（11-17 实测）。
    # 至少 4 个字才允许按「是某个常量的一部分」认。
    if len(token) < 4:
        return False
    return any(token in literal for literal in literals)


def _mask_unknown_cjk(text: str) -> str:
    """含中文、且**不是源码里写过的句子** ⇒ 换成 `[内容]`（防用户原话被拼进异常消息）。"""
    if not text or not _CJK_RE.search(text):
        return text
    # 先按打码标记切开（这些标记原样保留），再按常见分隔符切开逐个判
    parts = re.split(r"(" + "|".join(re.escape(mark) for mark in _MASK_MARKS) + r")", text)
    out: list[str] = []
    for part in parts:
        if not part or part in _MASK_MARKS or not _CJK_RE.search(part):
            out.append(part)
            continue
        out.append(_mask_part(part))
    return "".join(out)


def _mask_part(part: str) -> str:
    """一段含中文的文本：按分隔符拆成词，源码里出现过的词留着，其余抹掉。"""
    sep_re = r"[，。；、！？,;!?\s]+|[:：=]"
    chunks = re.split(f"({sep_re})", part)
    out: list[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        if re.fullmatch(sep_re, chunk) or not _CJK_RE.search(chunk):
            out.append(chunk)          # 纯 ASCII / 分隔符：留着（英文报错、标识符）
            continue
        out.append(chunk if _literal_covers(chunk.strip("'\"`()（）[]【】")) else "[内容]")
    joined = "".join(out)
    # 连续 [内容] 只留一个，读起来干净
    return re.sub(r"(?:\[内容\]){2,}", "[内容]", joined).strip()


def _path_mark(m: "re.Match[str]") -> str:
    tail = m.group(0).replace("\\", "/").rsplit("/", 1)[-1]
    ext = re.search(r"\.[A-Za-z0-9]{1,8}$", tail)
    return "[路径]" + (ext.group(0) if ext else "")


def _scrub(text: str, *, limit: int, trusted: bool = False) -> str:
    """先过 `redact`，再按本文件的规则裁一遍；先脱敏再截断（反过来会把密钥截半个发出去）。"""
    if not text:
        return ""
    s = str(text)
    try:
        from .redact import redact as _redact

        s, _hits = _redact(s)
    except Exception:  # noqa: BLE001 —— redact 自己坏了也照发，后面的规则仍然生效
        pass
    # 路径整段换成 `[路径]`，只留扩展名（文件名本身可能就是客户名/项目名）
    s = _HOME_RE.sub("[路径]", s)
    s = _ABS_PATH_RE.sub(_path_mark, s)
    s = re.sub(r"\[路径\](?:[\\/][^\s:'\"，。；、）)\]】]*)?", _path_mark, s)
    s = _URL_CRED_RE.sub(r"\1***@", s)
    s = _ID_TOKEN_RE.sub("[ID]", s)
    s = _LONG_TOKEN_RE.sub("[长串]", s)

    def _quote(m: "re.Match[str]") -> str:
        body = m.group(2)
        if not body:
            return m.group(0)
        if re.fullmatch(r"[A-Za-z0-9_.\-]{1,60}", body):
            return m.group(0)       # 标识符 / 短路径：留着才能定位
        return "'[内容]'"

    s = _QUOTED_RE.sub(_quote, s)
    if not trusted:
        s = _mask_unknown_cjk(s)   # 源码常量（日志模板）本来就是我们写的，不用再判
    s = s.replace("\r", " ").replace("\n", " ")
    # 控制字符（ESC/CSI 之类，防终端注入、也让 JSON 干净）
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s[:limit]


def _rel_frame(path: str) -> str:
    """绝对路径 ⇒ `bridge/xxx.py`。不在 bridge/ 里的返回空串（那些帧一概不发）。"""
    p = (path or "").replace("\\", "/")
    marker = _BRIDGE_DIR.replace("\\", "/").rstrip("/") + "/"
    if p.startswith(marker):
        return "bridge/" + p[len(marker):]
    # 别的机器/别的安装目录（从 agent.log 里读出来的旧路径）：只认 …/bridge/ 之后那段
    idx = p.rfind("/bridge/")
    if idx >= 0:
        return "bridge/" + p[idx + 8:]
    return ""


def _frames_of(exc: BaseException) -> list[str]:
    """异常栈里**只留 bridge/ 的帧**：`bridge/app.py:123:func`（不带源码行、不带绝对路径）。"""
    out: list[str] = []
    try:
        chain = traceback.extract_tb(exc.__traceback__)
    except Exception:  # noqa: BLE001
        return out
    for fr in chain:
        rel = _rel_frame(getattr(fr, "filename", "") or "")
        if not rel:
            continue
        func = re.sub(r"[^A-Za-z0-9_.<>]", "_", str(getattr(fr, "name", "") or "-"))[:60] or "-"
        out.append(f"{rel}:{int(getattr(fr, 'lineno', 0) or 0)}:{func}")
    return out[-_MAX_FRAMES:]


def _call_frame(record: logging.LogRecord) -> str:
    rel = _rel_frame(getattr(record, "pathname", "") or "")
    if not rel:
        return ""
    func = re.sub(r"[^A-Za-z0-9_.<>]", "_", str(getattr(record, "funcName", "") or "-"))[:60] or "-"
    return f"{rel}:{int(getattr(record, 'lineno', 0) or 0)}:{func}"


def _exc_type(exc: BaseException) -> str:
    name = getattr(exc, "__class__", type(exc)).__name__
    module = getattr(type(exc), "__module__", "") or ""
    value = name if module in ("", "builtins", "__main__") else f"{module}.{name}"
    return _TYPE_RE.sub("", value)[:80] or "Exception"


def _exc_text(exc: BaseException) -> str:
    """异常消息：脱敏 + 限长。`OSError` 带 errno，`KeyError` 带 repr（引号规则会处理）。"""
    try:
        if isinstance(exc, OSError) and getattr(exc, "strerror", None):
            raw = f"[Errno {exc.errno}] {exc.strerror}"
            if exc.filename:
                raw += f": {exc.filename}"
        elif isinstance(exc, KeyError) and exc.args:
            raw = repr(exc.args[0])
        else:
            raw = str(exc)
    except Exception:  # noqa: BLE001 —— 奇怪的异常 str() 会抛
        raw = ""
    return _scrub(raw, limit=_MAX_EMSG)


def _fingerprint(kind: str, log_name: str, msg: str, exc_type: str, frames: list[str]) -> str:
    """同一指纹 = 同一处代码的同一个错。**不含异常消息**（里面有 id、时间，天天变）。"""
    key = "\u0000".join((kind, log_name, msg, exc_type, "\u0001".join(frames)))
    return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def _now_iso(when: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(when if when is not None else time.time()))


def _today(when: Optional[float] = None) -> str:
    return time.strftime("%Y%m%d", time.localtime(when if when is not None else time.time()))


def _trim_item(item: dict) -> dict:
    """出生白名单：只留 `ITEM_KEYS`，值再按形状裁一次（bounded queue 里也全是干净数据）。"""
    out: dict[str, Any] = {}
    for key in ITEM_KEYS:
        val = item.get(key)
        if key in ("n",):
            try:
                out[key] = max(1, min(int(val or 1), 9999))
            except (TypeError, ValueError):
                out[key] = 1
        elif key == "frames":
            frames = [f for f in (val or []) if isinstance(f, str) and _FRAME_RE.match(f)]
            out[key] = frames[:_MAX_FRAMES]
        elif key == "src":
            out[key] = val if val in _SRC_WORDS else "log"
        elif key in ("msg", "emsg"):
            out[key] = str(val or "")[:_MAX_EMSG]
        elif key == "type":
            out[key] = _TYPE_RE.sub("", str(val or ""))[:80]
        elif key == "log":
            out[key] = _LOGGER_RE.sub("", str(val or ""))[:60]
        elif key == "fp":
            out[key] = val if isinstance(val, str) and _FP_RE.match(val) else ""
        else:  # at
            out[key] = str(val or "")[:32]
    return out


def item_from_record(record: logging.LogRecord) -> dict:
    """一条 logging 记录 ⇒ 一个上报条目。只用 `record.msg`（模板），**绝不** `getMessage()`。"""
    msg = record.msg
    if not isinstance(msg, str):
        # 有人 `logger.error(exc)` 直接传对象 ⇒ 只留类型名，内容一律不发
        template = f"<{type(msg).__name__}>"
    elif msg.strip() not in _source_literals():
        # 🔴 模板必须是**我们源码里的字符串常量**。不是 ⇒ 运行时拼出来的（f-string、
        # `logger.error(str(exc))`、asyncio 的 "Task exception …\n<coro 的参数 repr>"），
        # 里面什么都可能有 ⇒ 一个字都不发，靠 frames 定位。
        template = "<动态消息>"
    else:
        # 🔴 **绝不渲染**（不做 `%`）：`record.args` 里装的往往是用户原话
        # （实测 `"…开头=%s" % ("老板说的原话",)` 一渲染就把用户的话发出去了）。
        # 源码里的模板本身就够定位（哪条日志 + 哪个文件哪一行）。
        template = msg
    template = _scrub(template, limit=_MAX_MSG, trusted=True)
    exc: Optional[BaseException] = None
    if record.exc_info and record.exc_info[1] is not None:
        exc = record.exc_info[1]
    exc_type = _exc_type(exc) if exc is not None else ""
    frames = _frames_of(exc) if exc is not None else []
    call = _call_frame(record)
    if call and call not in frames:
        frames = (frames + [call])[-_MAX_FRAMES:]
    kind = "error"
    fp = _fingerprint(kind, record.name, template, exc_type, frames)
    return _trim_item(
        {
            # loop 里没人接的异常：asyncio 默认的 exception handler 打的是 `asyncio` logger 的 ERROR
            "src": "asyncio" if record.name == "asyncio" else "log",
            "at": _now_iso(),
            "log": record.name,
            "msg": template,
            "type": exc_type,
            "emsg": _exc_text(exc) if exc is not None else "",
            "frames": frames,
            "fp": fp,
            "n": 1,
        }
    )


def item_from_exception(exc: BaseException, src: str, drop_self: bool = False) -> dict:
    """未捕获异常 ⇒ 一个条目（`sys.excepthook` / `threading.excepthook` 用）。

    `drop_self`：复现 import 崩溃时栈底是本文件的 `_run`，那帧和崩溃无关，去掉
    （也让指纹和「读 agent.log 的 Traceback」那一路一致 ⇒ 两路报的同一个崩溃会去重）。
    """
    exc_type = _exc_type(exc)
    frames = _frames_of(exc)
    if drop_self:
        frames = [f for f in frames if not f.startswith("bridge/errreport.py:")]
    # 和 `item_from_traceback` 同一种指纹：进程内钩子报过的崩溃，守卫读日志再报时会被去重
    fp = _fingerprint("exc", "", "", exc_type, frames)
    return _trim_item(
        {
            "src": src if src in _SRC_WORDS else "uncaught",
            "at": _now_iso(),
            "log": "feishu-bridge",
            "msg": "",
            "type": exc_type,
            "emsg": _exc_text(exc),
            "frames": frames,
            "fp": fp,
            "n": 1,
        }
    )


# ── agent.log 里的 Traceback（起不来的那一路）────────────────────────────
_TB_HEAD = "Traceback (most recent call last):"
_TB_FRAME_RE = re.compile(r'^\s+File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<func>[^\s]+)\s*$')
_TB_TYPE_RE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Iteration))(?::\s?(?P<msg>.*))?$")
_CHAIN_MARKS = ("During handling of the above exception",
                "The above exception was the direct cause")


def _tb_blocks(text: str) -> list[list[str]]:
    """把文本切成一段段 Traceback（一段 = 一个最终异常的完整栈，含 chain）。"""
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if line.strip() == _TB_HEAD]
    if not heads:
        return []
    # 每条 Traceback 从它的 head 往前吃到上一个「代码行」（中间那些 "During handling" 和
    # 上一段的异常行属于这一段的前半部分）
    starts = []
    for idx in heads:
        start = idx
        j = idx - 1
        while j >= 0 and j >= idx - 80:
            line = lines[j]
            if line.strip() == _TB_HEAD or _TB_FRAME_RE.match(line) or line.startswith("    "):
                start = j
                j -= 1
                continue
            if line.startswith(_CHAIN_MARKS) or _TB_TYPE_RE.match(line.strip()):
                start = j
                j -= 1
                continue
            break
        starts.append(start)
    starts = sorted(set(starts))
    blocks = []
    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        blocks.append(lines[start:end])
    return blocks


def parse_traceback_block(lines: list[str]) -> dict:
    """一段 Traceback ⇒ {type, emsg, frames, chain}。只留 bridge/ 的帧。"""
    frames: list[str] = []
    exc_type = ""
    emsg = ""
    chain: list[str] = []
    for line in lines:
        m = _TB_FRAME_RE.match(line)
        if m:
            rel = _rel_frame(m.group("path"))
            if rel:
                func = re.sub(r"[^A-Za-z0-9_.<>]", "_", m.group("func"))[:60] or "-"
                frame = f"{rel}:{int(m.group('line'))}:{func}"
                if not frames or frames[-1] != frame:
                    frames.append(frame)
            continue
        stripped = line.strip()
        if not stripped or stripped == _TB_HEAD or "^^^^" in stripped or stripped.startswith("~~~"):
            continue
        if line.startswith(" "):
            continue    # 源码行 / SyntaxError 的插入符
        m2 = _TB_TYPE_RE.match(stripped)
        if m2:
            exc_type = _TYPE_RE.sub("", m2.group("type"))[:80]
            emsg = m2.group("msg") or ""
            if exc_type:
                chain.append(exc_type)
    return {"type": exc_type, "emsg": emsg, "frames": frames[-_MAX_FRAMES:], "chain": chain}


def item_from_traceback(parsed: dict, src: str = "crash") -> dict:
    exc_type = parsed.get("type") or ""
    frames = list(parsed.get("frames") or [])
    emsg = _scrub(parsed.get("emsg") or "", limit=_MAX_EMSG)
    fp = _fingerprint("exc", "", "", exc_type, frames)
    return _trim_item(
        {
            "src": src,
            "at": _now_iso(),
            "log": "feishu-bridge",
            "msg": "",
            "type": exc_type,
            "emsg": emsg,
            "frames": frames,
            "fp": fp,
            "n": 1,
        }
    )


def _read_log_tail(path: str, max_bytes: int = 256 * 1024) -> str:
    """读日志尾部。Windows 的 `*>>` 可能写成 UTF-16LE（有 NUL）⇒ 抹掉 NUL 再按 UTF-8 解。"""
    try:
        with open(path, "rb") as handle:
            try:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - max_bytes))
            except OSError:
                pass
            raw = handle.read()
    except OSError:
        return ""
    if raw.count(b"\x00") > len(raw) // 10:
        raw = raw.replace(b"\x00", b"")
    return raw.decode("utf-8", "replace")


def crash_item_from_log(log_path: str) -> Optional[dict]:
    """`agent.log` 尾部最后一段 Traceback ⇒ 上报条目。读不到 / 没有哪段像样的 ⇒ None。

    🔴 判据：Traceback **后面没有新的时间戳行** ⇒ 它是进程最后吐出来的东西（真崩了）。
    后面还有日志的，是 `logger.exception` 抓过的（那种进程内那一路已经报过了）。
    """
    text = _read_log_tail(log_path)
    if not text:
        return None
    blocks = _tb_blocks(text)
    if not blocks:
        return None
    lines = text.splitlines()
    stamp_re = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
    last_start = None
    # 从后往前：第一个「后面没有时间戳行」的块
    for block in reversed(blocks):
        # 定位这一块在全文里的位置：用它的最后一行做锚
        anchor = block[-1] if block else ""
        pos = len(lines) - 1
        if anchor:
            for idx in range(len(lines) - 1, -1, -1):
                if lines[idx].rstrip() == anchor.rstrip():
                    pos = idx
                    break
        last_start = pos
        if not any(stamp_re.match(line) for line in lines[pos + 1:]):
            parsed = parse_traceback_block(block)
            if parsed["type"] and (parsed["frames"] or parsed["emsg"]):
                return item_from_traceback(parsed)
    # 全都后面跟了日志：还是把最后一段发出去（比什么都不发强），但**不发时间戳行本身**
    if last_start is not None and blocks:
        parsed = parse_traceback_block(blocks[-1])
        if parsed["type"]:
            return item_from_traceback(parsed)
    return None


# ── 进程内复现启动期异常（比读日志准：拿得到真正的异常）─────────────────
def reproduce_import_crash(app_home: str) -> Optional[dict]:
    """在子线程里 import `bridge.app` 一次：**起不来的那一版**这里会抛出同一个异常。

    用子线程 + join 超时：万一 import 卡住（等网络/等锁），不能把上报也一起卡死。
    """
    box: dict[str, Any] = {}

    def _run() -> None:
        sys.path.insert(0, app_home) if app_home not in sys.path else None
        try:
            import importlib

            importlib.import_module("bridge.app")
        except BaseException as exc:  # noqa: BLE001 —— 就是要抓它
            box["exc"] = exc

    thread = threading.Thread(target=_run, name="errreport-import", daemon=True)
    try:
        thread.start()
        thread.join(15)
    except Exception:  # noqa: BLE001
        return None
    exc = box.get("exc")
    if exc is None:
        return None
    return item_from_exception(exc, "crash", drop_self=True)


class Reporter:
    """攒条目 → 指纹去重 → 每台每天上限 → 后台线程批量 POST。

    `send` 可替换（测试里给桩，绝不真打网络）；`clock` 可替换（测换天）。
    """

    def __init__(
        self,
        *,
        app_home: Optional[str] = None,
        package_dir: Optional[str] = None,
        send: Optional[Callable[[dict], bool]] = None,
        clock: Callable[[], float] = time.time,
        enabled: Optional[bool] = None,
        build_id: Optional[str] = None,
        app_id: Optional[str] = None,
    ) -> None:
        self.app_home = os.path.abspath(app_home or _APP_HOME)
        self.package_dir = os.path.abspath(package_dir or _BRIDGE_DIR)
        self.state_path = os.path.join(self.app_home, _STATE_FILE)
        self.spool_path = os.path.join(self.app_home, _SPOOL_FILE)
        self._send = send
        self._clock = clock
        self._build_override = build_id
        self._app_override = app_id
        self.enabled = self._decide_enabled() if enabled is None else bool(enabled)
        self._lock = threading.RLock()
        # 🔴 同一时间只许一个 flush 在发（11-17 端到端实测：后台线程和前台各 flush 一次，
        # 两边在锁里取到同一批、锁外各发一次 ⇒ 198 收到两条一模一样的）
        self._send_lock = threading.Lock()
        self._queue: list[Dict[str, Any]] = []          # [{item, day, tries}]
        self._sent_fp: dict[str, str] = {}               # fp -> day（当天已发过的）
        self._counts: dict[str, int] = {}                # fp -> 当天次数
        self._today_n = 0
        self._day = _today(self._clock())
        self._thread: Optional[threading.Thread] = None
        self._wake = threading.Event()
        self._stopped = False
        self._loaded = False

    # ---------- 开关 ----------
    @staticmethod
    def _decide_enabled() -> bool:
        if os.environ.get("CARHER_NO_REPORT", "").strip() not in ("", "0"):
            return False
        if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
            return False
        try:
            from .status import build_id as _build_id

            return bool(_build_id(_BRIDGE_DIR))
        except Exception:  # noqa: BLE001
            return False

    # ---------- 本机身份 ----------
    def build_id(self) -> str:
        if self._build_override is not None:
            return _SAFE_BUILD_RE.sub("", self._build_override or "")[:60]
        try:
            from .status import build_id as _build_id

            return _SAFE_BUILD_RE.sub("", _build_id(self.package_dir) or "")[:60]
        except Exception:  # noqa: BLE001
            return ""

    def install_id(self) -> str:
        """和 selfupdate 同一个文件、同一个格式；没有就**按它的规则生成**（只读自 update 那边会写）。"""
        path = os.path.join(os.path.dirname(self.package_dir), _INSTALL_ID_FILE)
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read(64).strip()
            if _INSTALL_ID_RE.match(value):
                return value
        except OSError:
            pass
        # 没有就建一个（新装的机器第一次启动就崩，selfupdate 还没跑到）。
        # 🔴 用 `os.link` 而不是 `os.replace`：selfupdate 可能同时在建 ⇒ 谁先建成就用谁的，
        # 绝不覆盖（覆盖 = 同一台机器这次启动里两边报的机器号不一样，fleet 表对不上）。
        value = secrets.token_hex(8)
        tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, value.encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
            finally:
                os.unlink(tmp)
            with open(path, encoding="utf-8") as handle:
                value = handle.read(64).strip()
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return ""
        return value if _INSTALL_ID_RE.match(value) else ""

    def app_id(self) -> str:
        if self._app_override is not None:
            value = str(self._app_override or "")
            return value if _APP_ID_RE.match(value) else ""
        try:
            path = os.environ.get("FEISHU_BRIDGE_CONFIG") or os.path.join(self.app_home, "config.toml")
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read(64 * 1024)
        except OSError:
            return ""
        match = re.search(r'(?m)^\s*app_id\s*=\s*"([^"\n]{1,60})"', text)
        value = match.group(1).strip() if match else ""
        return value if _APP_ID_RE.match(value) else ""

    # ---------- 去重 / 上限 ----------
    def _roll_day(self) -> None:
        day = _today(self._clock())
        if day == self._day:
            return
        self._day = day
        self._sent_fp = {}
        self._counts = {}
        self._today_n = 0

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                state = json.load(handle)
            day = str(state.get("day") or "")
            if day == self._day:
                sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
                counts = state.get("counts") if isinstance(state.get("counts"), dict) else {}
                self._sent_fp = {k: v for k, v in sent.items() if _FP_RE.match(k) and v == day}
                self._counts = {k: int(v) for k, v in counts.items() if _FP_RE.match(k)}
                self._today_n = int(state.get("n") or 0)
        except Exception:  # noqa: BLE001 —— 状态坏了当没有
            pass
        # 上次崩之前攒着没发出去的
        try:
            with open(self.spool_path, encoding="utf-8") as handle:
                spool = json.load(handle)
            for raw in (spool.get("items") or [])[:_MAX_QUEUE]:
                item = _trim_item(raw if isinstance(raw, dict) else {})
                if item.get("fp"):
                    self._queue.append({"item": item, "day": self._day, "tries": 0})
            os.unlink(self.spool_path)
        except Exception:  # noqa: BLE001
            pass

    def _save_state(self) -> None:
        state = {
            "day": self._day,
            "n": self._today_n,
            "sent": {fp: day for fp, day in self._sent_fp.items() if day == self._day},
            "counts": self._counts,
        }
        self._write_json(self.state_path, state)

    def _write_json(self, path: str, payload: dict) -> None:
        tmp = f"{path}.{os.getpid()}.{secrets.token_hex(3)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    def add(self, item: dict) -> bool:
        """收一条。返回是否真的入了队。**不碰网络、不抛**（调用方是 logging handler）。"""
        if not self.enabled:
            return False
        try:
            with self._lock:
                self._load()
                self._roll_day()
                item = _trim_item(item)
                fp = item.get("fp") or ""
                if not fp:
                    return False
                if self._sent_fp.get(fp) == self._day:
                    # 当天已经发过这个指纹：只加计数，稍后没人再发它
                    self._counts[fp] = self._counts.get(fp, 1) + 1
                    return False
                for pending in self._queue:
                    if pending["item"].get("fp") == fp:
                        pending["item"]["n"] = int(pending["item"].get("n") or 1) + 1
                        self._counts[fp] = self._counts.get(fp, 1) + 1
                        return False
                if self._today_n >= _MAX_TODAY or len(self._queue) >= _MAX_QUEUE:
                    logger.info("今天报错条数到上限 / 队列满，这一条不再发")
                    return False
                self._counts[fp] = self._counts.get(fp, 0) + 1
                self._queue.append({"item": item, "day": self._day, "tries": 0})
            # 不唤醒线程：让它按 `_FLUSH_AFTER_SEC` 等一会儿，连击的同一指纹合成一条 n
            self._ensure_thread()
            return True
        except Exception:  # noqa: BLE001 —— 上报绝不能把桥搞挂
            return False

    # ---------- 发送 ----------
    def _payload(self, batch: list[dict]) -> dict:
        return {
            "kind": "runtime",
            "v": self.build_id(),
            "id": self.install_id(),
            "os": "windows" if platform.system().lower().startswith(("win", "mingw", "cygwin"))
            else ("mac" if platform.system() == "Darwin" else "linux"),
            "app": self.app_id(),
            "items": [_trim_item(item) for item in batch],
        }

    def _post(self, payload: dict) -> bool:
        """真发。`CARHER_ERRREPORT_DUMP=<path>` 时只落盘不发（端到端手工验证 / 测试用）。"""
        dump = os.environ.get("CARHER_ERRREPORT_DUMP")
        if dump:
            try:
                path = dump.replace("{day}", _today(self._clock()))
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, indent=1)
                logger.info("报错已落盘（没发）：%s", path)
                return True
            except OSError:
                return False
        if os.environ.get("CARHER_NO_REPORT", "").strip() not in ("", "0"):
            return False
        if os.environ.get("PYTEST_CURRENT_TEST") or "pytest" in sys.modules:
            return False        # 双保险：测试里就算走到这里也不许发
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(data) > 64 * 1024:
            return False
        request = urllib.request.Request(
            REPORT_URL,
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "carher-errreport/1"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_SEND_TIMEOUT) as response:
                return 200 <= int(getattr(response, "status", 200)) < 300
        except Exception:  # noqa: BLE001 —— 网络坏了静默丢，绝不重试到把桥拖住
            return False

    def flush(self) -> int:
        """把队列里该发的发掉。返回发出去的条数。不抛。

        一批最多 `_MAX_ITEMS_PER_POST` 条；发成功 ⇒ 出队 + 记「这个指纹今天发过了」；
        发失败 ⇒ 次数 +1，到 `_TRY_MAX` 次就丢掉（绝不无限重试）。
        """
        if not self._send_lock.acquire(blocking=False):
            return 0        # 另一个 flush 正在发，这批归它
        try:
            return self._flush_locked()
        finally:
            self._send_lock.release()

    def _flush_locked(self) -> int:
        sent = 0
        try:
            while True:
                with self._lock:
                    self._load()
                    self._roll_day()
                    if not self._queue:
                        self._save_state()
                        return sent
                    batch = self._queue[:_MAX_ITEMS_PER_POST]
                    payload = self._payload([pending["item"] for pending in batch])
                if not payload.get("id") or not payload.get("v"):
                    # 没有机器号 / 没有版本号（开发机、install_id 写不进盘）⇒ 服务端也收不了，丢
                    with self._lock:
                        for pending in batch:
                            if pending in self._queue:
                                self._queue.remove(pending)
                        self._save_state()
                    return sent
                sender = self._send or self._post
                try:
                    ok = bool(sender(payload))
                except Exception:  # noqa: BLE001 —— 桩/网络抛了也不许往上冒
                    ok = False
                with self._lock:
                    for pending in batch:
                        fp = pending["item"].get("fp") or ""
                        if ok:
                            if pending in self._queue:
                                self._queue.remove(pending)
                            if fp:
                                self._sent_fp[fp] = self._day
                            self._today_n += 1
                            sent += 1
                        else:
                            pending["tries"] = int(pending.get("tries") or 0) + 1
                            if pending["tries"] >= _TRY_MAX and pending in self._queue:
                                self._queue.remove(pending)
                    self._save_state()
                if not ok:
                    return sent
        except Exception:  # noqa: BLE001
            return sent

    # ---------- 后台线程 ----------
    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="errreport", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        first = True
        while not self._stopped:
            wait = _FLUSH_AFTER_SEC if first else _HEARTBEAT_SEC
            self._wake.wait(timeout=wait)
            self._wake.clear()
            first = False
            if self._stopped:
                return
            try:
                with self._lock:
                    has = bool(self._queue)
                if has:
                    self.flush()
            except Exception:  # noqa: BLE001
                pass

    def drain_for_crash(self, max_sec: float = 4.0) -> None:
        """进程要死了：把攒着的东西同步发一次（限时），剩下的落盘等下次启动再补。"""
        timer = threading.Thread(target=self.flush, name="errreport-drain", daemon=True)
        timer.start()
        timer.join(max_sec)
        self.spool_pending()
        self._save_state()

    def spool_pending(self) -> None:
        try:
            with self._lock:
                items = [p["item"] for p in self._queue][:_MAX_QUEUE]
            if items:
                self._write_json(self.spool_path, {"items": items})
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self._stopped = True
        self._wake.set()


class _Handler(logging.Handler):
    """挂在 `feishu-bridge` 上（子 logger 会传播上来）；只在 ERROR 及以上收。"""

    def __init__(self, reporter: Reporter) -> None:
        super().__init__(level=logging.ERROR)
        self.reporter = reporter
        self._busy = threading.local()

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        if getattr(self._busy, "on", False):
            return
        # `Handler.handle()` 不看级别（只有 Logger 看）⇒ 这里自己再判一次
        if record.levelno < logging.ERROR:
            return
        name = record.name or ""
        if name == "feishu-bridge.errreport" or not (
            name == "feishu-bridge" or name.startswith("feishu-bridge.") or name == "asyncio"
        ):
            return
        self._busy.on = True
        try:
            self.reporter.add(item_from_record(record))
        except Exception:  # noqa: BLE001 —— 上报绝不能把桥搞挂
            pass
        finally:
            self._busy.on = False


_installed = False
_reporter: Optional[Reporter] = None


def install(app_home: Optional[str] = None, package_dir: Optional[str] = None) -> Optional[Reporter]:
    """`main()` 里调一次。从不抛：装不上只是没有报错上报，桥照跑。"""
    global _installed, _reporter
    if _installed:
        return _reporter
    _installed = True
    try:
        reporter = Reporter(app_home=app_home, package_dir=package_dir)
        _reporter = reporter
        if not reporter.enabled:
            logger.debug("报错上报没开（测试 / CARHER_NO_REPORT / 开发机没有 BUILD_ID）")
            return None
        logging.getLogger("feishu-bridge").addHandler(_Handler(reporter))
        logging.getLogger("asyncio").addHandler(_Handler(reporter))
        _install_excepthooks(reporter)
        atexit.register(reporter.drain_for_crash)
        reporter._load()
        return reporter
    except Exception:  # noqa: BLE001
        return None


def _install_excepthooks(reporter: Reporter) -> None:
    previous = sys.excepthook

    def _sys_hook(exc_type, exc, tb):  # noqa: ANN001
        try:
            if not issubclass(exc_type, KeyboardInterrupt):
                reporter.add(item_from_exception(exc, "uncaught"))
                reporter.drain_for_crash(3.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            previous(exc_type, exc, tb)
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = _sys_hook
    prev_thread = getattr(threading, "excepthook", None)

    def _thread_hook(args):  # noqa: ANN001
        try:
            exc = getattr(args, "exc_value", None)
            if exc is not None and not issubclass(getattr(args, "exc_type", type(exc)), KeyboardInterrupt):
                reporter.add(item_from_exception(exc, "thread"))
        except Exception:  # noqa: BLE001
            pass
        if prev_thread is not None:
            try:
                prev_thread(args)
            except Exception:  # noqa: BLE001
                pass

    if prev_thread is not None:
        threading.excepthook = _thread_hook


def _crash_log_main(argv: list[str]) -> int:
    """`--crash-log <agent.log>`：起不来的那一版由启动守卫调（或 Windows 的 runner 调）。

    先试着 import `bridge.app` 复现（拿得到真异常、真栈）；复现不了读日志尾部的 Traceback。
    **永远退出码 0**：上报不许影响启动/守卫。
    """
    home = _APP_HOME
    log_path = ""
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg == "--home" and args:
            home = os.path.abspath(args.pop(0))
        elif arg == "--crash-log" and args:
            log_path = args.pop(0)
    os.environ.setdefault("FEISHU_BRIDGE_CONFIG", os.path.join(home, "config.toml"))
    try:
        reporter = Reporter(app_home=home, package_dir=os.path.join(home, "bridge"))
        if not reporter.enabled:
            return 0
        reporter._load()
        item = reproduce_import_crash(home)
        if item is None and log_path:
            item = crash_item_from_log(log_path)
        if item is not None:
            reporter.add(item)
        reporter.flush()
    except Exception:  # noqa: BLE001
        pass
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--crash-log" in args:
        return _crash_log_main(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
