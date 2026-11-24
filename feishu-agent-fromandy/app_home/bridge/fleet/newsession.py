"""从手机新建一个会话（Claude Code / Codex / Cursor），拿到会话 id 就返回，第一轮在后台跑完再回群。

## 每家怎么「新建 + 拿 id」（2026-11-16 第 36 轮实测，见 docs/r36/newsession-backend.md）

| 家 | 命令 | id 从哪来 | 实测 |
|---|---|---|---|
| Claude | `claude -p <话> --session-id <我们生成的 uuid> -n <名字> --output-format stream-json --verbose` | **我们自己定**（`--session-id`），`system/init` 回显同一个 | init 0.75s，回完 6.9s |
| Codex | `codex exec --json --skip-git-repo-check -s read-only -- <话>` | 第一行 `thread.started.thread_id` | 0.39s 拿到 id，回完 6.3s |
| Cursor | 先 `cursor-agent create-chat`（stdout 一行 id），再 `--resume <id> -p … --mode ask` | `create-chat` 的 stdout | create-chat 1.71s；`-p` 自己 7.6s 才出 init ⇒ 不能靠它 |

## 不分叉的顺序（「同时在电脑上打开窗口」）

`claude -p` 在跑的时候电脑上再开一个 `claude --resume` ⇒ 两个进程写同一个 jsonl ⇒ 静默分叉。
所以 **第一轮在后台跑完、进程退出之后** 才去开 Terminal 窗口。窗口开着之后手机再说话：
- Claude：窗口里是 `claude --resume`（entrypoint=cli、注册表里有我们起的名字）⇒ `control.send(relay=True)`
  走会话间消息那条路（`relay.py`，要求会话有名字 ⇒ 新建时就用 `-n` 起好）；
- Codex：窗口占着写锁 ⇒ `relay()` 走 `codex queue`；
- Cursor：窗口里的 cursor-agent 占着 ⇒ 手机**发不进去**（cursor 没有 relay 进开着的对话的路）。

## 红线

- 原话只在脱敏后写日志（`bridge/redact.py`），也不写进 Terminal 脚本（脚本里只有 id）。
- cwd：`policy.resolve_workspace` + `pathguard.outside_workspace` 两道都过才开。
- 权限：Claude 按手机档位（非只读档要审批钩子 + 批准目录，和 `claude.py` 的 send 一致）；
  Codex / Cursor 一律只读（`-s read-only` / `--mode ask`）。
- 子进程一律登记进 `FleetJobs`（「⏹ 停下」能停、总览认得是桥起的），跑完就 forget。
- 给手机的话一律人话，原始异常进日志（`friendly_error`）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import signal as _signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .model import (TOOLS, ActionResult, FleetJobs, RunResult, SubprocessRunner, friendly_error,
                    on_windows, short_title, spawn_fail_log)

logger = logging.getLogger("feishu-bridge.fleet")

ID_WAIT_SEC = 2.5          # start() 等「起来了 / 拿到 id」最多这么久（卡片回调 3 秒上限）
CURSOR_CREATE_SEC = 2.2    # create-chat 实测 1.71s；没登录时会一直挂着（临时 HOME 实测 120s 不退）⇒ 必须有上限
MAX_PROMPT_CHARS = 8000
DEFAULT_TURN_SEC = 600.0

LABELS = {"claude": "Claude", "codex": "Codex", "cursor": "Cursor"}

# ---------- 给手机看的话 ----------
WHY_BAD_TOOL = "不认识这个工具，只能选 Claude / Codex / Cursor。"
WHY_NOT_INSTALLED = "这台电脑上没找到 {label}，装好之后再来。"
WHY_EMPTY = "第一句话是空的，说点什么再发。"
WHY_TOO_LONG = f"第一句话太长了（最多 {MAX_PROMPT_CHARS} 个字），拆短一点。"
WHY_NO_DIR = "这个项目文件夹不在了，换一个项目。"
WHY_OUTSIDE = "这个文件夹不在手机允许的项目范围里，换一个项目。"
WHY_NO_POLICY = "手机这边的权限设置没读到，先不开新会话。"
WHY_SPAWN_FAILED = "{label} 没能启动：{why}"
WHY_EXITED = "{label} 一启动就退出了：{why}"
WHY_NO_ID = "{label} 起来了，但几秒内没拿到会话编号，已经让它停下，没留下半截会话。稍后再试一次。"
WHY_CURSOR_CREATE = "Cursor 没能新建对话（{why}）。到电脑上打开一次 cursor-agent 看看是不是要重新登录。"
NOTE_STARTED = "已在电脑上新开了一个 {label} 会话，它正在回第一句，回完会发到群里。"
NOTE_READONLY = "{label} 从手机开的会话按只读跑：它只看、只回答，不改你的文件。"
NOTE_DOWNGRADED = "确认卡片那一层没接上，这次按「只读」档开的。"
NOTE_WINDOW_LATER = "第一句回完之后，会在电脑上打开一个终端窗口接着这个会话。"
NOTE_WINDOW_CURSOR = "注意：电脑上的窗口开着时，手机这边发不进这个 Cursor 对话，关掉窗口才行。"

# 从别的 Claude 会话里继承下来的标记：带着它起交互窗口会「Transcript saving is off」（11-16 实测）、
# 带着 ENTRYPOINT 会把新会话的入口记错。一律去掉。
_CLAUDE_INHERITED = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CONFIG_DIR", "CARHER_APPROVAL_DIR")


@dataclass
class NewSessionRequest:
    tool: str
    cwd: str
    prompt: str
    open_window: bool = False
    mode: str = "readonly"


@dataclass
class NewSessionResult:
    ok: bool
    tool: str
    session_id: str = ""
    note: str = ""
    # start() 返回时窗口一定还没开（要等第一轮跑完，防分叉）⇒ 这里恒为 False；
    # 真开没开看 FirstTurn.window_opened。
    window_opened: bool = False
    name: str = ""          # Claude 会话的名字（relay 的地址）；其他家空串
    mode: str = "readonly"  # 实际用的档位（可能比请求的低）


@dataclass
class FirstTurn:
    """第一轮跑完（或失败 / 超时）后交给 on_done 的东西。在**工作线程**里回调。"""

    tool: str
    session_id: str
    ok: bool
    text: str = ""          # 回复原文（未脱敏 —— 发飞书走 replier.send，那里统一脱敏）
    window_opened: bool = False
    window_note: str = ""


# ---------- 找可执行文件 ----------

_FALLBACKS = {
    "claude": ("/opt/homebrew/bin/claude", "/usr/local/bin/claude", "~/.local/bin/claude", "~/.claude/local/claude"),
    "codex": ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", "~/.local/bin/codex"),
    "cursor": ("~/.local/bin/cursor-agent", "/opt/homebrew/bin/cursor-agent", "/usr/local/bin/cursor-agent"),
}
# 🔴 11-20：Windows 的兜底位置原来**一个都没有**（上面全是 mac 路径）。
# 现场 liudewang …a355bd：桥在飞书里一干活就报「找不到要用的文件或程序」
# （relay.py:525 FileNotFoundError 起 claude），而他 11-18 装 Claude Code 是成功的
# ⇒ 真病根是**后台服务那份 PATH 失效了**（装机那一刻记的 $claudeDir，claude 后来换了位置），
# which 找不到、又没有 Windows 兜底 ⇒ 每次都抛。
# 这几个位置抄 install-windows.ps1 里挑 claudeBin 时试的同一组（改一处要改两处）。
# 🔴 **只放我们自己装的落点**，别凭猜加。
# 11-20 我原来还加了 `~\AppData\Local\Programs\claude\claude.exe`（猜的「桌面 App 装法」），
# 被「一键安装」会话按源码否掉：全仓 `AppData.Local.Programs` 在安装器里 0 命中，
# 我们装 claude 一律走 npm（`claude-oneclick/src/install-windows.ps1:266`
# `$npmPrefix = ~\.carher-npm-global`）⇒ 落点就是下面第一个。
# 留着的代价不是「多试一个空路径」，而是**万一那里真有别人装的 claude.exe，桥会去起它** ——
# 那份不带我们的 `npm_config_prefix`，`claude update` 会把包装进 `%APPDATA%\npm`
# 造一个 332MB 影子副本而且永远升不上去（同文件 :988 的注释）。
# 宁可报「找不到 claude」让人重跑安装器。
_FALLBACKS_WINDOWS = {
    "claude": (r"~\.carher-npm-global\claude.cmd", r"~\.local\bin\claude.exe",
               r"~\AppData\Roaming\npm\claude.cmd"),
    "codex": (r"~\.carher-npm-global\codex.cmd", r"~\.local\bin\codex.exe",
              r"~\AppData\Roaming\npm\codex.cmd"),
    "cursor": (r"~\.local\bin\cursor-agent.exe", r"~\AppData\Roaming\npm\cursor-agent.cmd"),
}
_EXE = {"claude": "claude", "codex": "codex", "cursor": "cursor-agent"}


# 转发脚本的特征：命令行里的 claude 可能是别的工具（cmux / 各种 wrapper）放的壳，
# 它会把自己所在目录从 PATH 里剔掉再去找真的 claude。
# 🔴 把壳的路径写死给后台服务 = 装机侧 11-18 修过的那个坑（install-windows.ps1 的
# Test-CarherClaudeRuns 注释：「照它写开机自启 ⇒ 任务那份 PATH 里没有真 claude ⇒ 起不动，
# 用户看到『飞书里能聊，一让它干活就答不出来』」）。本机实测 which 就返回了一个
# cmux shim（`~/.cmuxterm/cmux-cli-shims/<uuid>/claude`，内容是 bash 转发到
# cmux-claude-wrapper）⇒ 不排掉的话我这次修法会把同一个坑重新引进来。
_SHIM_MARKS = ("cmux-cli-shims", "/.cmuxterm/", "cmux-claude-wrapper")


def _looks_like_shim(path: str) -> bool:
    p = str(path).replace("\\", "/")
    return any(mark in p for mark in _SHIM_MARKS)


def find_bin(tool: str, which: Callable[[str], Optional[str]] = shutil.which) -> str:
    """后台服务的 PATH 很短 ⇒ which 找不到再看默认安装位置。找不到返回空串。

    `which` 命中一个**转发壳**时先看真实安装位置，都没有才退回那个壳
    （壳总比什么都没有好 —— 交互窗口里它是能跑的）。
    """
    if tool not in _EXE:
        return ""
    found = which(_EXE[tool])
    if found and not _looks_like_shim(found):
        return found
    cands = _FALLBACKS_WINDOWS[tool] if on_windows() else _FALLBACKS[tool]
    for cand in cands:
        path = os.path.expanduser(cand)
        # 🔴 Windows 上不查 X_OK：NTFS 没有执行位，`os.access(..., X_OK)` 对
        # `.cmd` / `.exe` 返回 False ⇒ 兜底全部失效（加了 Windows 路径也等于没加）。
        if os.path.isfile(path) and (on_windows() or os.access(path, os.X_OK)):
            return path
    return found or ""


def available_tools(which: Callable[[str], Optional[str]] = shutil.which) -> list:
    """本机装了哪几家，按 TOOLS 的顺序。"""
    return [t for t in TOOLS if find_bin(t, which)]


# ---------- 小工具 ----------

def _log_text(text: str) -> str:
    from ..redact import redact

    cleaned, _hits = redact(str(text or ""))
    return short_title(cleaned, 60)


def session_name(cwd: str, sid: str) -> str:
    """Claude 会话名 = relay 的唯一地址 ⇒ 只用 ASCII，带上 id 前几位防重名。`m` = mobile。"""
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", os.path.basename(os.path.normpath(cwd or ""))).strip("-.")
    return f"{(base or 'session')[:24]}-m{sid[:4]}"


def _check_cwd(cwd: str, policy: Any) -> tuple:
    """(真实路径, 不行的话一句人话)。C 校验过，这里再过一遍（不信任按钮回调）。"""
    from ..pathguard import outside_workspace

    if policy is None or not hasattr(policy, "resolve_workspace"):
        return "", WHY_NO_POLICY
    raw = str(cwd or "").strip()
    if not raw or not os.path.isdir(os.path.expanduser(raw)):
        return "", WHY_NO_DIR
    real = policy.resolve_workspace(raw)
    if real is None:
        return "", WHY_OUTSIDE
    if outside_workspace("cd " + shlex.quote(real), tuple(getattr(policy, "workspaces", ()) or ()), real):
        return "", WHY_OUTSIDE
    return real, ""


_HOME_PATH = re.compile(r"/(?:Users|home)/[^/\s'\"]+")


def _short_err(text: str) -> str:
    """stderr 最后一行 → 给手机看的短句。本机路径带真名（第 9 轮复审）⇒ 换成 ~。"""
    lines = [ln.strip() for ln in str(text or "").splitlines() if ln.strip()]
    return _HOME_PATH.sub("~", _log_text(lines[-1])) if lines else "没有输出"

def _safe_prompt(text: str) -> str:
    # 以「-」开头会被当成参数（claude.py 同一招）；单独一个「-」codex 会去读 stdin
    return " " + text if text.startswith("-") else text


def claude_env(auth_env: Optional[dict] = None, approval_dir: str = "", config_dir: str = "") -> dict:
    """新会话落在**用户自己的** `~/.claude`（电脑上 `claude -r` / 总览都看得到）。

    和 `ClaudeAdapter._send_env` 同一套：去掉 CLAUDE_CONFIG_DIR（桥那份绝不能漏进来），
    认证键从 settings.json 挑进来，审批目录按这次给。另外去掉从别的 Claude 会话继承来的标记。
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("CLAUDE_CODE_") and k not in _CLAUDE_INHERITED}
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    for key, value in (auth_env or {}).items():
        env[str(key)] = str(value)
    if approval_dir:
        env["CARHER_APPROVAL_DIR"] = approval_dir
    return env


# 桥起 claude 用的权限档。🔴 **只在这里写一次**。
# 11-20：原来 `claude_argv` 里写死 bypassPermissions，而窗口那条 `--resume` 不带权限参数
# ⇒ jsonl 里记 bypass、窗口里的真进程跑用户自己那档 ⇒ relay 按 jsonl 选信使就选错了，
# 消息被「权限档不同类」扣住（用户看到「📨 发给 Claude 了…」然后「❌ 没发进去」）。
# 两处共用这一个常量，改档位只改这里。
BRIDGE_PERMISSION_MODE = "bypassPermissions"


def claude_argv(binary: str, sid: str, name: str, prompt: str, policy: Any,
                hook: Optional[dict] = None, auth_top: Optional[dict] = None) -> list:
    """和 `ClaudeAdapter._send_argv` 同一套权限：`BRIDGE_PERMISSION_MODE` 档、
    一个 settings 来源都不加载、不起用户的 MCP、白名单 / 红线来自 policy、钩子用 --settings 单独带。
    区别只在「新建」：`--session-id` 我们自己定，`-n` 起好名字（relay 要它），stream-json 拿 init。"""
    extra = dict(auth_top or {})
    extra.pop("permissions", None)
    if hook:
        extra["hooks"] = hook["hooks"]
    argv = [
        binary, "-p", _safe_prompt(prompt),
        "--session-id", sid, "-n", name,
        "--output-format", "stream-json", "--verbose",
        "--permission-mode", BRIDGE_PERMISSION_MODE,
        "--setting-sources", "",
        "--strict-mcp-config",
    ]
    if extra:
        argv += ["--settings", json.dumps(extra, ensure_ascii=False)]
    for tool in policy.allowed_tools():
        argv += ["--allowed-tools", str(tool)]
    for tool in policy.disallowed_tools():
        argv += ["--disallowed-tools", str(tool)]
    return argv


def codex_argv(binary: str, cwd: str, prompt: str) -> list:
    """只读：`-s read-only`（实测 threads.sandbox_policy 记成 root 只读、approval never）。"""
    return [binary, "exec", "--json", "--skip-git-repo-check", "-s", "read-only",
            "-C", cwd, "--", _safe_prompt(prompt)]


def codex_env(binary: str, codex_home: str = "") -> dict:
    env = dict(os.environ)
    # codex 是 node 脚本；后台服务的 PATH 常常没有 node（抄 CodexAdapter._env）
    extra = [os.path.dirname(binary)] if os.path.isabs(binary) else []
    extra += ["/opt/homebrew/bin", "/usr/local/bin"]
    parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    env["PATH"] = os.pathsep.join(parts + [p for p in extra if p and p not in parts])
    if codex_home:
        env["CODEX_HOME"] = codex_home
    return env


def cursor_argv(binary: str, chat_id: str, cwd: str, prompt: str) -> list:
    """和 `CursorAdapter.build_argv(mode="readonly")` 同一个形状：只读 = `--mode ask`。"""
    return [binary, "--resume", chat_id, "-p", "--output-format", "json", "--trust",
            "--workspace", cwd, "--mode", "ask", "--", _safe_prompt(prompt)]


_CURSOR_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_UUID = _CURSOR_ID


def _window_argv(tool: str, binary: str, sid: str, cwd: str,
                 permission_mode: str = "") -> list:
    """窗口里要跑的那条命令。两个平台共用，别各写一份（会漂）。

    - Claude：`claude --resume <id> --permission-mode <桥用的那档>`
    - Codex：`codex resume <id> -s read-only`（手机开的会话是只读的，窗口里也别变成整机全开）
    - Cursor：`cursor-agent --resume <id> --mode ask`

    🔴 11-20：`--permission-mode` 原来**不带**（注释写着「= 用户自己那套设置」），
    而桥起第一轮用的是 `bypassPermissions`（今天 14c1f705 改的）⇒ jsonl 里那行
    `user.permissionMode` 记的是 bypass，窗口里的真进程却跑在用户自己那档（manual）。
    会话间消息要「收发双方权限档同类」，`relay.permission_mode_of()` 读 jsonl 得到 bypass
    ⇒ 选了 bypass 信使 ⇒ 真进程是 prompting 类 ⇒ **消息被扣住等人点**，
    用户在群里看到「📨 发给 Claude 了，等它回…」然后「❌ 没发进去」（本轮现场）。
    改成把桥这一档一起传进去：jsonl 记什么、进程跑什么，是同一个值。
    改 permission-mode 的默认档时，这里会跟着走（它从 policy 来），不用再记得改第二处。
    """
    if tool == "claude":
        argv = [binary, "--resume", sid]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        return argv
    if tool == "codex":
        return [binary, "resume", "-s", "read-only", "-C", cwd, sid]
    return [binary, "--resume", sid, "--workspace", cwd, "--mode", "ask"]


def window_script(tool: str, binary: str, sid: str, cwd: str, permission_mode: str = "") -> str:
    """电脑上那个窗口跑的脚本。**只有 id 和目录**，不含第一句话。跑起来先删掉自己。

    🔴 11-20：Windows 要另一份（见 `window_script_windows`）。原来只有这一份 bash，
    于是 Windows 上写出 `#!/bin/bash` 的 `.command`、再 `/usr/bin/open -a Terminal`
    ⇒ `FileNotFoundError`，手机上「➕ 新开一个」勾了「在电脑上开窗口」就报
    「电脑上找不到要用的文件或程序……刷新一下再试」（liudewang 机器 …a355bd
    11-20 11:43 现场，newsession.py:285）。
    """
    return "\n".join([
        "#!/bin/bash",
        'rm -f "$0"; rmdir "$(dirname "$0")" 2>/dev/null',
        "unset " + " ".join(_CLAUDE_INHERITED),
        "cd " + shlex.quote(cwd) + " || exit 1",
        "exec " + " ".join(shlex.quote(a) for a in _window_argv(tool, binary, sid, cwd, permission_mode)),
        "",
    ])


def window_script_windows(tool: str, binary: str, sid: str, cwd: str, permission_mode: str = "") -> str:
    """同上，`.cmd` 版。

    - `@echo off` 之后**先删自己**：`.cmd` 删掉自己照样能跑完（cmd 按偏移续读，但
      `start` 之后这行就是最后一行了）⇒ 和 mac 那份一样不留垃圾。
      🔴 `del "%~f0"` 要放在真正启动**之前**那一刻，不能放最后 —— `exec` 之后不会回来。
    - 清掉继承来的 CLAUDE_*：`set "VAR="` 是 cmd 的删法（不是 `unset`）。
    - 路径里有空格/中文一律加引号；cwd 用 `cd /d` 才能跨盘符。
    """
    argv = _window_argv(tool, binary, sid, cwd, permission_mode)
    lines = ["@echo off", 'del "%~f0" >nul 2>&1']
    lines += [f'set "{name}="' for name in _CLAUDE_INHERITED]
    lines.append(f'cd /d "{cwd}" || exit /b 1')
    # 🔴 程序路径**一律加引号**，哪怕当前这个路径里没有空格：
    # 它是 `C:\Users\<用户名>\…` 开头的，用户名里有空格的机器很常见（「张 三」），
    # 而「这台没空格所以不用引号」会在别人机器上变成两个参数。后面的参数照常按需加。
    lines.append(" ".join([_cmd_quote(argv[0], always=True)]
                          + [_cmd_quote(a) for a in argv[1:]]))
    lines.append("")
    return "\r\n".join(lines)


def _cmd_quote(arg: str, *, always: bool = False) -> str:
    """cmd 的引号规则：有空格或特殊字符就用双引号包起来；`always` = 无条件包。

    🔴 不能用 `shlex.quote`（它出单引号，cmd 不认单引号，会把它当成文件名的一部分）。
    """
    s = str(arg)
    if not always and s and not any(c in s for c in ' \t"&|<>^()%!'):
        return s
    return '"' + s.replace('"', '""') + '"'


def _open_in_cmux(script_path: str) -> bool:
    """在 cmux 里新开一个工作区跑这个脚本。开成了 True；任何一步不行 False（调用方退回 Terminal.app）。

    凭据取法和 `termsend` 同一个：cmux 只认它自己的子孙，桥不是 ⇒ 从一个活着的 cmux 会话
    身上取 `CMUX_SOCKET_CAPABILITY`，**只经环境变量**传给子进程（不进 argv、不落盘、不打日志）。
    """
    try:
        from .termsend import _ENV_CAPABILITY, _ENV_SOCKET, _cmux_bin, _process_env
    except Exception:  # noqa: BLE001
        return False
    exe = _cmux_bin()
    if not exe:
        return False
    creds = {}
    try:
        reg_dir = os.path.join(os.path.expanduser("~/.claude"), "sessions")
        for name in sorted(os.listdir(reg_dir)):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(reg_dir, name), encoding="utf-8") as fh:
                    pid = (json.load(fh) or {}).get("pid")
            except Exception:  # noqa: BLE001
                continue
            env = _process_env(pid)
            if env.get(_ENV_CAPABILITY):
                creds = {k: env[k] for k in (_ENV_CAPABILITY, _ENV_SOCKET) if env.get(k)}
                break
    except OSError:
        return False
    if not creds:
        return False
    env = dict(os.environ)
    env.update(creds)
    folder = os.path.dirname(script_path) or "."
    # 工作区名给人看：不给的话标题是那个一次性脚本的临时路径（`/var/folders/…/carher-newsession-xxx`）。
    # 真正的工作目录由脚本第一行 `cd` 决定，`--cwd` 只是 cmux 开壳时的起点。
    argv = [exe, "workspace", "create", "--name", "📱 手机新开的会话", "--cwd", folder,
            "--command", script_path, "--focus", "false"]
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0 and "OK" in (res.stdout or "")


def _open_terminal_default(script_path: str) -> tuple:
    """打开一个真窗口跑那个脚本。mac 用 `open -a Terminal`，Windows 用 `cmd /c start`。"""
    if on_windows():
        # `start` 是 cmd 的内建，不是 exe ⇒ 必须经 `cmd /c`。
        # 第一个 "" 是窗口标题占位：不给的话 `start "C:\有空格\x.cmd"` 会把路径当标题、什么都不跑。
        # /k 不用（跑完留着窗口会积一堆）；脚本自己 exec 进 CLI，CLI 退出窗口就关。
        argv = ["cmd", "/c", "start", "", "/d", os.path.dirname(script_path) or ".", script_path]
        doing = "没能打开命令行窗口"
    else:
        # 🔴 11-23：**先开在 cmux 里**，开不了才退回 Terminal.app。
        # 主人现场：手机「新建会话」开出来的窗口在 Terminal.app ⇒ 那个会话是唯一一个
        # 认不出的（session-lanes 30/31）⇒ 在群里发 `/goal` 回「插不上手」，而另外 30 个都行。
        # cmux 里的窗口才有程序接口（termsend：斜杠命令真执行、Esc 真打断）。
        # 实测（干净环境、非 cmux 子孙、只带一把从活会话身上取的凭据）：
        # `cmux workspace create --cwd <目录> --command <脚本> --focus false` ⇒ OK、
        # 不抢焦点、tree 里立刻出现。没装 cmux / 连不上 / 没有凭据 ⇒ 原样走 Terminal.app。
        if _open_in_cmux(script_path):
            return True, ""
        argv = ["/usr/bin/open", "-a", "Terminal", script_path]
        doing = "没能打开终端窗口"
    try:
        res = subprocess.run(argv, stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # 🔴 先打一句带程序名的**源码常量**日志：上报只发常量模板，光靠 friendly_error
        # 那句通用模板查不出起的是什么（11-20 这条就是这样卡住的，同 relay.py 的修法）。
        try:
            logger.error(spawn_fail_log(argv[0]), exc_info=exc)
        except Exception:  # noqa: BLE001
            pass
        return False, friendly_error(exc, doing)
    if res.returncode != 0:
        return False, _short_err(res.stderr) or "窗口没打开"
    return True, ""

# ---------- 「这是从手机开的」：给总览标 📱 用 ----------

_STARTED_LOCK = threading.Lock()
_STARTED: dict = {}  # (tool, sid) -> epoch 秒。内存一份 + 落盘一份（桥重启后读盘；Codex/Cursor 没有会话名尾巴可兜）

# 落盘：一个小 JSON {"v":1,"started":{"<tool>:<sid>": epoch秒}}。标记是锦上添花 ⇒ 读写任何异常都吞掉只记 debug，
# 绝不能让总览卡因此挂掉。`CARHER_NEWSESSION_STATE` 覆盖路径（测试用）。
STATE_ENV = "CARHER_NEWSESSION_STATE"
STATE_MAX_ITEMS = 500
STATE_MAX_AGE_SEC = 30 * 86400
_DISK_CACHE: dict = {"key": None, "data": {}}  # key=(路径, mtime_ns, size)，文件没变就不重读


def _state_path() -> str:
    return os.environ.get(STATE_ENV) or os.path.expanduser("~/.carher-feishu-agent/newsession-started.json")


def _prune(items: dict, now: float) -> dict:
    keep = {k: t for k, t in items.items() if now - t <= STATE_MAX_AGE_SEC}
    if len(keep) > STATE_MAX_ITEMS:
        keep = dict(sorted(keep.items(), key=lambda kv: kv[1], reverse=True)[:STATE_MAX_ITEMS])
    return keep


def _read_disk() -> dict:
    """{(tool, sid): epoch秒}。文件不存在 / 坏了 ⇒ {}（不抛）。调用方持 `_STARTED_LOCK`。"""
    path = _state_path()
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (path, st.st_mtime_ns, st.st_size)
    if _DISK_CACHE["key"] == key:
        return _DISK_CACHE["data"]
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        for k, t in ((raw or {}).get("started") or {}).items():
            tool, _, sid = str(k).partition(":")
            if tool and sid and isinstance(t, (int, float)):
                out[(tool, sid)] = float(t)
    except Exception:  # noqa: BLE001 —— 标记丢了只是少一个 📱，不能连累总览
        logger.debug("读「从手机开的」标记文件失败 %s", path, exc_info=True)
        out = {}
    _DISK_CACHE.update(key=key, data=out)
    return out


def _write_disk(items: dict) -> None:
    """原子写（临时文件 + os.replace），目录 0700、文件 0600。异常吞掉。调用方持 `_STARTED_LOCK`。"""
    path = _state_path()
    tmp = ""
    try:
        d = os.path.dirname(path) or "."
        os.makedirs(d, mode=0o700, exist_ok=True)
        body = {"v": 1, "started": {f"{t}:{s}": ts for (t, s), ts in items.items()}}
        fd, tmp = tempfile.mkstemp(prefix=".newsession-started.", dir=d)  # mkstemp 本身就是 0600
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        tmp = ""
    except Exception:  # noqa: BLE001
        logger.debug("写「从手机开的」标记文件失败 %s", path, exc_info=True)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def mark_started(tool: str, sid: str) -> None:
    now = time.time()
    with _STARTED_LOCK:
        _STARTED[(str(tool), str(sid))] = now
        try:
            merged = dict(_read_disk())
            merged[(str(tool), str(sid))] = now
            _write_disk(_prune(merged, now))
        except Exception:  # noqa: BLE001
            logger.debug("落盘「从手机开的」标记失败", exc_info=True)


def started_from_phone(tool: str, sid: str, name: str = "") -> bool:
    """总览打「📱 从手机开的」用。内存 → 盘 → 会话名（Claude 的 `-m<id前4位>` 尾巴）三层，任何一层认出就算。"""
    key = (str(tool), str(sid))
    with _STARTED_LOCK:
        if key in _STARTED:
            return True
        try:
            if key in _read_disk(): return True  # noqa: E701 —— 桥重启后靠这一行认回来
        except Exception:  # noqa: BLE001
            logger.debug("读「从手机开的」标记失败", exc_info=True)
    return bool(name) and str(name).endswith("-m" + str(sid)[:4])


# ---------- 起进程 + 边读边找 id ----------

class _Proc:
    """一个后台子进程：stdout 按行读（找 id、攒回复），stderr 只留尾巴（防管道写满卡死）。"""

    def __init__(self, popen: Callable[..., Any], argv: list, cwd: str, env: dict,
                 find_id: Callable[[dict], str]) -> None:
        self.lines: list = []
        self.err_tail = ""
        self.id_found = ""
        self.got_id = threading.Event()
        self.done = threading.Event()
        self._find_id = find_id
        self.proc = popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        self.pid = int(self.proc.pid)
        self._out = threading.Thread(target=self._read_out, daemon=True, name=f"newsession-out-{self.pid}")
        self._err = threading.Thread(target=self._read_err, daemon=True, name=f"newsession-err-{self.pid}")
        self._out.start()
        self._err.start()

    def _read_out(self) -> None:
        try:
            for line in self.proc.stdout:
                self.lines.append(line)
                if self.got_id.is_set():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                found = self._find_id(rec) if isinstance(rec, dict) else ""
                if found:
                    self.id_found = found
                    self.got_id.set()
        except Exception:  # noqa: BLE001 —— 读管道出错只影响回复，不能让线程崩
            logger.debug("读子进程输出失败 pid=%s", self.pid, exc_info=True)
        finally:
            self.done.set()

    def _read_err(self) -> None:
        try:
            for line in self.proc.stderr:
                self.err_tail = (self.err_tail + line)[-4000:]
        except Exception:  # noqa: BLE001
            pass

    def exited(self) -> Optional[int]:
        return self.proc.poll()

    def wait_reader(self, timeout: float) -> None:
        self._out.join(timeout)
        self._err.join(min(timeout, 2.0))

def _claude_id(rec: dict) -> str:
    if rec.get("type") == "system" and rec.get("subtype") == "init":
        return str(rec.get("session_id") or "")
    return ""


def _codex_id(rec: dict) -> str:
    if rec.get("type") == "thread.started":
        tid = str(rec.get("thread_id") or "").lower()
        return tid if _UUID.match(tid) else ""
    return ""


def _never(rec: dict) -> str:
    return ""


def parse_reply(tool: str, lines: list) -> tuple:
    """子进程 stdout → (成没成, 回复原文)。读不懂就 (False, "")。"""
    recs = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            recs.append(rec)
    if tool == "codex":
        texts, failed = [], ""
        for rec in recs:
            item = rec.get("item") if isinstance(rec.get("item"), dict) else {}
            if rec.get("type") == "item.completed" and item.get("type") == "agent_message":
                texts.append(str(item.get("text") or ""))
            elif rec.get("type") in ("turn.failed", "error"):
                err = rec.get("error") if isinstance(rec.get("error"), dict) else {}
                failed = str(err.get("message") or rec.get("message") or "这一轮失败了")
        if failed and not texts:
            return False, failed
        return bool(texts), "\n\n".join(t for t in texts if t)
    # claude（stream-json 最后一条 result）/ cursor（json 一整个对象）同一个形状
    for rec in reversed(recs):
        if rec.get("type") == "result" or "result" in rec:
            text = str(rec.get("result") or "")
            bad = bool(rec.get("is_error")) or str(rec.get("subtype") or "success") != "success"
            return (not bad and bool(text)), text
    return False, ""

# 🔴 第 36 轮复审 Y1：电脑上刚开窗口 ⇒ Terminal + shell 启动、`claude -r` 写注册表之前有一段空窗，
# 这时手机再发一句，`send()` 看不到有人占着 ⇒ 起 `-p --resume` ⇒ 分叉。所以开窗后这段时间一律当「有人占着」。
WINDOW_GRACE_SEC = 90
_WINDOW_OPENED: dict = {}   # sid → time.monotonic()
_WINDOW_LOCK = threading.Lock()


def note_window_opened(sid: str) -> None:
    with _WINDOW_LOCK:
        _WINDOW_OPENED[sid] = time.monotonic()


def clear_window_opened(sid: str) -> None:
    """窗口里的进程确认起来了 ⇒ 这段空窗期的意义就没了（11-20）。

    不清的话，`window_just_opened` 在剩下的几十秒里还会回 True，
    每个调用点都得自己再判一次「其实已经起来了」。
    """
    with _WINDOW_LOCK:
        _WINDOW_OPENED.pop(sid, None)


def window_just_opened(sid: str, now: Optional[float] = None) -> bool:
    """这个会话是不是刚在电脑上开了窗口（WINDOW_GRACE_SEC 内）。"""
    with _WINDOW_LOCK:
        at = _WINDOW_OPENED.get(sid)
        if at is None:
            return False
        if (time.monotonic() if now is None else now) - at > WINDOW_GRACE_SEC:
            _WINDOW_OPENED.pop(sid, None)
            return False
        return True


# 🔴 11-20：原来写「等一分钟再说」—— 而真正要等的是窗口里那个 claude 起来（通常几秒），
# 不是一分钟。现在起来了就立刻能发（见 control.send 里那段），所以这句话只在「真的还没起来」时出现。
WHY_WINDOW_JUST_OPENED = "电脑上那个窗口还在启动，几秒后再发一次就行（现在发会和窗口里那个撞上）。"


def open_window_for(tool: str, binary: str, sid: str, cwd: str,
                    open_terminal: Optional[Callable[[str], tuple]] = None,
                    script_dir: Optional[str] = None,
                    permission_mode: str = "") -> tuple:
    """写一个一次性脚本（跑起来先删自己）再开一个真窗口 → (开没开, 一句话)。

    mac：`.command` + 0700 + `open -a Terminal`；Windows：`.cmd` + `cmd /c start`。
    🔴 Windows 上别 `chmod 0700`：NTFS 上这个调用不报错但也不起作用，而真正要紧的是
    扩展名必须是 `.cmd`（`.command` 在 Windows 上没有关联程序，start 会弹「用什么打开」）。
    """
    win = on_windows()
    folder = tempfile.mkdtemp(prefix="carher-newsession-", dir=script_dir)
    ext = "cmd" if win else "command"
    path = os.path.join(folder, f"{tool}-{sid[:8]}.{ext}")
    try:
        body = (window_script_windows if win else window_script)(tool, binary, sid, cwd, permission_mode)
        # Windows 的 cmd 按当前代码页读脚本；中文路径用 UTF-8 会乱码 ⇒ 用 mbcs（系统默认，GBK）。
        # 写不出去的字符退回 UTF-8，总比整条命令废掉好。
        if win:
            try:
                with open(path, "w", encoding="mbcs", newline="") as fh:
                    fh.write(body)
            except (LookupError, UnicodeEncodeError):
                with open(path, "w", encoding="utf-8", newline="") as fh:
                    fh.write(body)
        else:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.chmod(path, 0o700)
        ok, why = (open_terminal or _open_terminal_default)(path)
    except Exception as exc:  # noqa: BLE001
        ok, why = False, friendly_error(exc, "没能打开终端窗口")
    if not ok:
        shutil.rmtree(folder, ignore_errors=True)
        return False, "电脑上的窗口没打开：" + str(why or "原因不明")
    note_window_opened(sid)
    return True, f"已在电脑上打开了一个终端窗口，接着这个 {LABELS.get(tool, tool)} 会话。"


def _reap(p: _Proc, jobs: FleetJobs, wait: float = 15.0) -> None:
    """让一个没用了的子进程停下并收尸（不留僵尸、不留登记）。"""
    def run() -> None:
        try:
            if p.exited() is None:
                jobs.signal(p.pid, _signal.SIGINT)
                try:
                    p.proc.wait(timeout=wait)
                except subprocess.TimeoutExpired:
                    jobs.signal(p.pid, _signal.SIGKILL)
                    p.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            logger.debug("收子进程失败 pid=%s", p.pid, exc_info=True)
        finally:
            jobs.forget(p.pid)
    threading.Thread(target=run, daemon=True, name=f"newsession-reap-{p.pid}").start()


def _bg_finish(tool: str, sid: str, p: _Proc, jobs: FleetJobs, *, timeout: float, open_window: bool,
               binary: str, cwd: str, on_done: Optional[Callable[[FirstTurn], None]],
               open_terminal: Optional[Callable[[str], tuple]], script_dir: Optional[str]) -> None:
    """工作线程：等第一轮跑完 → 解析回复 → （要的话）开窗口 → on_done。

    🔴 顺序就是防分叉：**进程退出之后**才开窗口。第一轮还在跑时开 `--resume` = 两个进程写同一个会话。
    """
    ok, text = False, ""
    try:
        try:
            p.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            jobs.signal(p.pid, _signal.SIGINT)
            try:
                p.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                jobs.signal(p.pid, _signal.SIGKILL)
                p.proc.wait(timeout=5)
        p.wait_reader(5.0)
        ok, text = parse_reply(tool, p.lines)
        if not ok:
            why = text or _short_err(p.err_tail)
            text = f"第一句没跑完：{why}"
    except Exception as exc:  # noqa: BLE001
        text = friendly_error(exc, "等第一句的回复时出错", log_as=f"新建会话收尾失败 tool={tool}")
    finally:
        jobs.forget(p.pid)
    logger.info("新建会话第一轮结束 tool=%s 会话=%s ok=%s 回复=%s", tool, sid[:8], ok, _log_text(text))
    opened, note = False, ""
    if open_window:
        if p.exited() is None:
            note = "它还没停下来，为了不分叉，这次没在电脑上开窗口。"
        else:
            # 🔴 带上桥这一档：jsonl 记什么、窗口里的进程跑什么，必须是同一个值（见 _window_argv 红字）
            opened, note = open_window_for(tool, binary, sid, cwd, open_terminal, script_dir,
                                           permission_mode=BRIDGE_PERMISSION_MODE if tool == "claude" else "")
    if on_done is not None:
        try:
            on_done(FirstTurn(tool, sid, ok, text, opened, note))
        except Exception:  # noqa: BLE001
            logger.exception("新建会话 on_done 回调失败 tool=%s", tool)

# ---------- 入口 ----------

def _fail(tool: str, note: str, mode: str = "readonly") -> NewSessionResult:
    return NewSessionResult(False, tool, "", note, False, "", mode)


def _wait_id(p: _Proc, limit: float) -> None:
    deadline = time.monotonic() + max(0.1, limit)
    while time.monotonic() < deadline:
        if p.got_id.wait(0.05) or p.exited() is not None:
            # 退出了也再给读线程一点时间把最后几行读完
            p.got_id.wait(0.2)
            return


def start(req: NewSessionRequest, *, policy: Any, jobs: FleetJobs, runner: Optional[SubprocessRunner] = None,
          hook: Any = None, approval_dir: Any = "", on_done: Optional[Callable[[FirstTurn], None]] = None,
          popen: Callable[..., Any] = subprocess.Popen, which: Callable[[str], Optional[str]] = shutil.which,
          claude_config_dir: str = "", codex_home: str = "", auth: Optional[Callable[[], tuple]] = None,
          open_terminal: Optional[Callable[[str], tuple]] = None, script_dir: Optional[str] = None,
          id_wait: float = ID_WAIT_SEC, turn_timeout: float = 0) -> NewSessionResult:
    """新开一个会话跑第一句，**拿到 id 就返回**（≤3 秒）；第一轮在后台跑完后调 `on_done(FirstTurn)`。

    - `hook`：审批钩子 settings（dict 或返回 dict 的函数，= app 的 `_fleet_hook`）。
    - `approval_dir`：批准目录，字符串或 `f(sid) -> str`（= `lambda sid: app._fleet_approval_dir("claude", sid, policy)`）。
      Claude 非只读档两样都要有，缺一样就降成只读（和 `ClaudeAdapter.send` 同一条规矩）。
    - `on_done` 在**工作线程**里调；要回飞书得自己切回事件循环（`run_coroutine_threadsafe`）。
    - 其余参数给测试注入（popen / which / 目录 / 开窗口），线上不用传。
    绝不抛：任何意外都变成 ok=False + 一句人话。
    """
    tool = str(getattr(req, "tool", "") or "")
    try:
        return _start(req, tool, policy, jobs, runner or SubprocessRunner(), hook, approval_dir, on_done,
                      popen, which, claude_config_dir, codex_home, auth, open_terminal, script_dir,
                      id_wait, turn_timeout)
    except Exception as exc:  # noqa: BLE001
        return _fail(tool, friendly_error(exc, "没能新开会话", log_as=f"新建会话失败 tool={tool}"))


def _start(req, tool, policy, jobs, runner, hook, approval_dir, on_done, popen, which,
           claude_config_dir, codex_home, auth, open_terminal, script_dir, id_wait, turn_timeout):
    if tool not in TOOLS:
        return _fail(tool, WHY_BAD_TOOL)
    label = LABELS[tool]
    binary = find_bin(tool, which)
    if not binary:
        return _fail(tool, WHY_NOT_INSTALLED.format(label=label))
    prompt = str(getattr(req, "prompt", "") or "").strip()
    if not prompt or prompt == "-":
        return _fail(tool, WHY_EMPTY)
    if len(prompt) > MAX_PROMPT_CHARS:
        return _fail(tool, WHY_TOO_LONG)
    cwd, why = _check_cwd(getattr(req, "cwd", ""), policy)
    if why:
        return _fail(tool, why)
    timeout = float(turn_timeout or getattr(policy, "turn_timeout_sec", 0) or DEFAULT_TURN_SEC)
    open_window = bool(getattr(req, "open_window", False))
    logger.info("新建会话 tool=%s 目录=%s 开窗口=%s 第一句=%s", tool, os.path.basename(cwd), open_window,
                _log_text(prompt))
    finish = dict(jobs=jobs, timeout=timeout, open_window=open_window, binary=binary, cwd=cwd,
                  on_done=on_done, open_terminal=open_terminal, script_dir=script_dir)
    if tool == "claude":
        return _start_claude(req, binary, cwd, prompt, policy, jobs, hook, approval_dir, popen,
                             claude_config_dir, auth, id_wait, finish)
    if tool == "codex":
        return _start_codex(binary, cwd, prompt, jobs, popen, codex_home, id_wait, finish)
    return _start_cursor(binary, cwd, prompt, jobs, runner, popen, finish)

def _launch(tool: str, argv: list, cwd: str, env: dict, jobs: FleetJobs, popen: Callable[..., Any],
            find_id: Callable[[dict], str], known_sid: str, id_wait: float, finish: dict,
            notes: list, extra: Optional[dict] = None) -> NewSessionResult:
    label = LABELS[tool]
    try:
        p = _Proc(popen, argv, cwd, env, find_id)
    except (OSError, ValueError) as exc:
        return _fail(tool, WHY_SPAWN_FAILED.format(label=label, why=friendly_error(exc)))
    jobs.register_spawned(p.pid, tool, known_sid)
    if known_sid:
        p.id_found = p.id_found or known_sid
    _wait_id(p, id_wait)
    sid = known_sid or p.id_found
    code = p.exited()
    if code is not None and code != 0:
        p.wait_reader(1.0)
        jobs.forget(p.pid)
        logger.warning("新建会话 子进程退出 tool=%s 退出码=%s stderr=%s", tool, code, _log_text(p.err_tail))
        return _fail(tool, WHY_EXITED.format(label=label, why=_short_err(p.err_tail)))
    if not sid:
        _reap(p, jobs)
        return _fail(tool, WHY_NO_ID.format(label=label))
    if not known_sid:
        jobs.register_spawned(p.pid, tool, sid)  # 拿到 id 后改登记到这个会话名下（「⏹ 停下」按会话找）
    mark_started(tool, sid)
    threading.Thread(target=_bg_finish, args=(tool, sid, p), kwargs=finish, daemon=True,
                     name=f"newsession-{tool}-{sid[:8]}").start()
    res = NewSessionResult(True, tool, sid, "".join(notes), False)
    for key, value in (extra or {}).items():
        setattr(res, key, value)
    return res


def _notes(tool: str, open_window: bool, downgraded: bool = False) -> list:
    label = LABELS[tool]
    notes = [NOTE_STARTED.format(label=label)]
    if tool in ("codex", "cursor"):
        notes.append(NOTE_READONLY.format(label=label))
    if downgraded:
        notes.append(NOTE_DOWNGRADED)
    if open_window:
        notes.append(NOTE_WINDOW_LATER)
        if tool == "cursor":
            notes.append(NOTE_WINDOW_CURSOR)
    return notes


def _start_claude(req, binary, cwd, prompt, policy, jobs, hook, approval_dir, popen,
                  config_dir, auth, id_wait, finish) -> NewSessionResult:
    # 档位：请求的档位压到本机上限以内（policy.with_mode 自带 clamp）；读不懂 ⇒ 只读
    want = str(getattr(req, "mode", "") or "readonly")
    try:
        pol = policy.with_mode(want) if hasattr(policy, "with_mode") else policy
    except Exception:  # noqa: BLE001
        pol = policy
    hook_d = hook() if callable(hook) else hook
    hook_d = hook_d if isinstance(hook_d, dict) and hook_d.get("hooks") else {}
    sid = str(uuid.uuid4())
    adir = approval_dir(sid) if callable(approval_dir) else str(approval_dir or "")
    downgraded = False
    if getattr(pol, "mode", "readonly") != "readonly" and not (hook_d and adir):
        # 可改 / 全授权档靠审批钩子挡越界写和红线命令；没带上就只读（claude.py send 同一条）
        pol = pol.with_mode("readonly") if hasattr(pol, "with_mode") else pol
        downgraded = True
    if auth is None:
        from ..approval import inherited_auth

        settings = os.path.join(config_dir or os.path.expanduser("~/.claude"), "settings.json")
        auth_env, auth_top = inherited_auth(settings)
    else:
        auth_env, auth_top = auth()
    name = session_name(cwd, sid)
    # 和 ClaudeAdapter._send_locked 一样：钩子有就带（只读档也带），批准目录只在带钩子时给
    argv = claude_argv(binary, sid, name, prompt, pol, hook_d or None, auth_top)
    env = claude_env(auth_env, adir if hook_d else "", config_dir)
    mode = getattr(pol, "mode", "readonly")
    return _launch("claude", argv, cwd, env, jobs, popen, _claude_id, sid, id_wait, finish,
                   _notes("claude", finish["open_window"], downgraded), {"name": name, "mode": mode})


def _start_codex(binary, cwd, prompt, jobs, popen, codex_home, id_wait, finish) -> NewSessionResult:
    return _launch("codex", codex_argv(binary, cwd, prompt), cwd, codex_env(binary, codex_home), jobs, popen,
                   _codex_id, "", id_wait, finish, _notes("codex", finish["open_window"]))


def _start_cursor(binary, cwd, prompt, jobs, runner, popen, finish) -> NewSessionResult:
    # 第一步：create-chat 拿 id（实测 1.71s）。`-p` 自己的 init 要 7.6s 才出来，等不起。
    res = runner.run([binary, "create-chat"], timeout=CURSOR_CREATE_SEC, cwd=cwd, env=dict(os.environ))
    chat_id = (res.stdout or "").strip().splitlines()[-1].strip() if (res.stdout or "").strip() else ""
    if res.error:
        return _fail("cursor", WHY_SPAWN_FAILED.format(label="Cursor", why=res.error))
    if res.timed_out or res.returncode != 0 or not _CURSOR_ID.match(chat_id):
        why = "等太久没反应" if res.timed_out else _short_err(res.stderr or res.stdout)
        logger.warning("cursor create-chat 失败 退出码=%s 超时=%s %s", res.returncode, res.timed_out, why)
        return _fail("cursor", WHY_CURSOR_CREATE.format(why=why))
    # 第二步：后台 `--resume <id> -p --mode ask`。id 已经知道，起得来就返回
    return _launch("cursor", cursor_argv(binary, chat_id, cwd, prompt), cwd, dict(os.environ), jobs, popen,
                   _never, chat_id, 0.3, finish, _notes("cursor", finish["open_window"]))
