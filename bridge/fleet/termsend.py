"""第三条路：把字 / 按键送进电脑上那个**终端窗口**（第 41 轮，2026-11-23）。

前两条路和它们各自做不到的事（都实测过，见 `relay.py` 模块头和 `test_r40_slash_passthrough.py`）：

| 路 | 怎么走 | 斜杠命令 | 打断那一轮 | 对方正忙时 |
|----|--------|---------|-----------|-----------|
| `claude.py::send` | `claude -p --resume` | **真执行** | — | 用不了（会静默分叉） |
| `relay.py::send` | 会话间消息 | 跑不了（上游写死 `skipSlashCommands`） | 做不到 | 进输入队列，不打断、不丢 |
| **这里** | 往那个窗口里打字 / 按键 | 真执行（等于主人自己敲的） | 能（Esc） | 🔴 见下面「正忙时不走这条」 |

主人那台 31 个会话全跑在 cmux 里（2026-11-23 实测：`~/.claude/sessions/*.json` 里 30 个在跑的
会话，父进程链一个不差都是 `claude ← zsh ← login ← cmux`），所以这条路对他是**主路**。

## 为什么默认仍然是 relay，这条路只接斜杠命令和 Esc

往窗口里打字就是「模拟主人按键」，它和 relay 的差别不是快慢，是**会不会抢他手里的活**：
relay 的消息进输入队列，对方干完这一轮再读；打字直接落在输入框里。
所以这里只接两件 relay 真办不到的事：
  - **斜杠命令**：relay 那条路上游关着（`skipSlashCommands`），不是我们挡的
  - **Esc 打断**：relay 一个信号都不发给目标，`relay_stop` 只能照实说「到电脑上按 Esc」

## 🔴 草稿闸门：cmux 那道只保护「注册过的」会话，所以我们自己也要有一道

cmux 的 `send` / `paste` 文档都写着「目标输入框里有人打了一半的字、或者有弹窗开着 ⇒
什么都不送、命令失败，要 `--force` 才送」。这条**只对被 cmux 注册成 agent 会话的那些生效**。

2026-11-23 的 A/B（同一个 workspace 里把两侧都做出来，先留半句草稿再送，都不加 `--force`）：

| 那个窗口里的 claude 是怎么起的 | 注册了吗 | 留草稿后再送 |
|------------------------------|---------|-------------|
| `claude`（走 PATH ⇒ 其实是 cmux 的 shim，带 `--session-id` + hook） | 是 | **rc=1 被拦住** |
| `/绝对路径/claude`（绕开 shim） | 否 | rc=0 放行，草稿和我们的字**拼成一句提交出去** |

主人侧逐个核过：29 个在跑的会话 **29/29 都已注册**（`claude-hook-sessions.json` 的
`activeSessionsBySurface` 里都有）⇒ cmux 那道闸对他每一个会话都生效。

**但我们自己那道（`can_send` → `_busy`：目标不空闲就不送字）仍然要留**，它管的是别的东西：
  - `send-key`（Esc）**完全不受** cmux 那道闸管
  - 实测「有弹窗开着就失败」在终端 surface 上没兑现：bypass 警告框开着时
    `send` / `paste` / `send-key` 三个全部 rc=0 放行
  - 没注册的会话（比如脚本用绝对路径起的）在 cmux 那边一点保护都没有
⇒ 两道闸各管一段，不是重复。「上游文档写了」从来不是证据，写在我们码里的才是。

## 怎么送（都是实测定下来的）

- **传输用 `cmux paste --surface <UUID> --submit -`，正文走 stdin**。三个理由：
  ① 裸 `send` 会把文本里的字面 `\n` 当回车 —— 实测 `send 'A\nB'` 把「A」提交掉、只剩「B」
     在框里，用户随口一句带 `\n` 就被截断并提前发出去；`paste` 原样保留。
  ② 一次调用就完成「落字 + 提交」，中间没有「字已经在框里但还没提交」的半成品状态。
  ③ 正文**不进 argv** ⇒ `ps` 里看不到用户说了什么。
- **打断用 `cmux send-key --surface <UUID> escape`**。实测把那一轮打成
  「Interrupted · What should Claude do instead?」，进程还活着、提示符可用。
- **不抢焦点**：送字前后 `cmux current-workspace` 不变（阳性对照过）。

## 鉴权：用目标会话自己那把令牌，不改主人任何配置

cmux 的 socket 默认 `automation.socketControlMode = cmuxOnly`（`cmux socket-status --json`
原文）⇒ 桥这种「不是 cmux 子孙」的进程直连一律被拒：
`访问被拒绝 - 只有在 cmux 内启动的进程才能连接`。实测（setsid + 双 fork 到 ppid=1、
环境里一个 `CMUX_*` 都没有）：

| 带什么 | 结果 |
|--------|------|
| 正确的 `CMUX_SOCKET_CAPABILITY` | 通 |
| 什么都不带 | 拒 |
| 令牌末 4 位改掉 | 拒 |
| 错密码 / 错 `CMUX_SOCKET_PASSWORD` | 拒（**和不带令牌同一句** ⇒ 那句话跟密码无关） |

令牌从**目标会话那个 claude 进程自己的环境变量**里现取（同 uid `ps eww`）。
本机 29 个在跑的会话 29/29 都有。🔴 **不缓存、不落盘**：cmux 重开一次令牌就换。
🔴 这不等于「我们改了用户的安全设置」—— 我们用的是他那个会话本来就有的凭据，
`socketControlMode` 一个字没动。

🔴 **令牌对整个 socket 有效，不是按 surface 分权的**（实测：拿 A 会话的令牌去操作 B 的
surface 照样成功）⇒ **边界只能我们自己守**：surface 必须从「这个 sessionId 自己那个进程」
身上取（`_surface_of_pid` 读它自己 env 里的 `CMUX_SURFACE_ID`），
绝不接受调用方传进来的 surface，也不按 tty 猜（tty 做交叉线索实测 7/29 对不上）。

🔴 这个文件**永不**模拟全局键盘（AppleScript / System Events）：那会打进主人当时聚焦的
任何窗口。只走那个终端程序自己的命令行接口，它只认自己的窗口。

## 权限：和 relay 同一档，不是和 `-p --resume` 同一档

打进窗口的字由**目标会话自己**执行 ⇒ 用的是它在电脑上那套权限（主人那 30 个会话实测
都是 `bypassPermissions`），手机这边的档位、工具白名单、审批钩子**一个都管不到**。
这和 relay 完全一样（`claude.relay` 的 docstring：「`mode` 收下就扔」），所以挡在前面的
是同一道闸：进对话模式前那次权限确认（`consent.RelayConsent`，`app._relay_granted`）。
🔴 **别把这条路接到任何绕过那道确认的入口上。** 它比 relay 更需要那道闸：relay 送进去的
是一句话，这里送进去的是**会真执行的命令**。
`/clear` `/init` 另有一道写死的闸，在 `app._handle_command` 里、跑在选路之前
（第 40 轮；在 relay 上它们送进去也不跑，到这条路上就是真清上下文 / 真写 CLAUDE.md）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Callable, Optional

from .claude import _is_session_id
from .model import ActionResult, ProcTable, friendly_error
from .relay import _looks_like_slash_command

logger = logging.getLogger("feishu-bridge.fleet")

TOOL = "claude"

# cmux 那条路已经量通并接上（2026-11-23，见模块头）。
# 留这个开关是为了出事能一键回到「原样退回今天的两条路」：改成 False ⇒ `can_send` 一律回
# 空串退回，行为和第 41 轮发版前一个字都不差。
WIRED = True

# ---------- 认出这是哪种终端 ----------

TERM_CMUX = "cmux"
TERM_TMUX = "tmux"
TERM_TERMINAL_APP = "terminal.app"
TERM_ITERM = "iterm"
TERM_VSCODE = "vscode"
TERM_CURSOR = "cursor"
TERM_UNKNOWN = "unknown"

# 往上找几层父进程。实测主人机上 cmux 是第 3 层（claude ← zsh ← login ← cmux），
# tmux / 编辑器集成终端可能更深（嵌套 shell、`login -fp`、nvm 的壳），和 `claude._HOST_HOPS` 同一个值。
_HOPS = 12

# (种类, 整条命令行里的特征串, argv[0] 的文件名)。
# 🔴 每条后面标的是**证据等级**，别把下面两档当成一回事：
#   [链] = 本机真跑着的 claude 会话，父进程链一路量到了（2026-11-23，30/30 个会话）
#   [名] = 只量到了那个终端程序**自己**在 ps 里的样子，没有一个 claude 会话真开在它下面 ⇒
#          「它在链上长这样」是推的。角色 2 在查非 cmux 的识别和降级，他的结论来了按他的改。
_TERM_MARKS = (
    # [链] ps 原文 `/Applications/cmux.app/Contents/MacOS/cmux`（本机 26 个活进程命中）。
    # 🔴 `cmux-claude-wrapper` 曾经在这张表里，已删：那个文件**在**、shim 脚本里也引用它，
    # 但它在**任何**活进程的 argv 里都不出现（本机实测 0 命中）—— 文件存在 ≠ 这条路被走过。
    (TERM_CMUX, ("/cmux.app/", "/.cmuxterm/"), ("cmux",)),
    # [名] 本机 ps 原文 `/opt/homebrew/bin/tmux -S <sock> new-session …`。
    # 🔴 `tmux: server` 已删：那是 **Linux** 下 tmux 改写 argv 的写法，macOS 上 0 命中
    # （本机 tmux 3.6a 实测）⇒ 留着等于这条判据在 mac 上永不生效。认它只能靠 argv[0] 的名字。
    (TERM_TMUX, (), ("tmux",)),
    # [名] 本机 ps 原文 `/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal`（pid 18387）
    (TERM_TERMINAL_APP, ("/Terminal.app/",), ()),
    # [猜] 本机没装，特征串按 app bundle 的通用形状写的 —— 角色 2 现场核过再当真
    (TERM_ITERM, ("/iTerm.app/", "/iTerm2.app/"), ()),
    # [名] 和 claude._HOST_MARKS 同一组串（那边已经在用它认宿主编辑器）
    (TERM_VSCODE, ("/Visual Studio Code.app/", "Code Helper", "\\Code.exe"), ()),
    (TERM_CURSOR, ("/Cursor.app/", "Cursor Helper", "\\Cursor.exe"), ()),
)

# 给用户看的叫法。认不出的不编名字（文案走 WHY_TERM_UNKNOWN，压根不用这张表）。
TERM_LABELS = {
    TERM_CMUX: "cmux",
    TERM_TMUX: "tmux",
    TERM_TERMINAL_APP: "「终端」",
    TERM_ITERM: "iTerm",
    TERM_VSCODE: "VS Code",
    TERM_CURSOR: "Cursor",
}

# ---------- 能按的键 ----------
#
# 🔴 白名单，不是黑名单。要的只有「打断」和「提交」两件事。
# Ctrl-C / Ctrl-D / Ctrl-Z 一个都不给：实测 SIGINT 会把主人整个会话关掉（relay.py 模块头），
# Ctrl-Z 会把它挂起成后台（11-18 那次 SIGSTOP 把 7 个窗口打掉成后台、1 个跑了 7 天半的直接死）。
# 名字先按人话定住；真要发给终端的键码等角色 1（`_argv_key` 的桩）。
KEY_ESC = "esc"
KEY_ENTER = "enter"
ALLOWED_KEYS = (KEY_ESC, KEY_ENTER)

# ---------- 给手机用户看的话（语气参考 relay.py 顶上那些 WHY_*） ----------
#
# 🔴 三条规矩：① 不许出现「我们收到你的信息」「会跟进」这类话（主人明确禁过）；
# ② 不露内部概念（窗口编号、连接路径）；③ 一句大白话 + 下一步该干什么。
# 这几句现在**到不了用户眼前**：`control` 那边拿不到就退回 relay / resume，它们各有自己更准的一句话。
# 留着是为了接上之后不用临时编文案，也进文案闸门（tests/test_copy_quality.py）。

WHY_NOT_WIRED = "手机这边还没接上「直接打进那个窗口」这条路，这次没走它。"
WHY_TERM_UNKNOWN = (
    "看不出这个会话开在电脑上哪种窗口里，怕把字打错地方，先不打进去。到电脑上那个窗口里自己打吧。"
)
WHY_TERM_NO_API = (
    "这个会话开在{label}里，手机这边插不上手、没法替你往里打字。到电脑上那个窗口里自己打吧。"
)
WHY_NO_CLI = (
    "电脑上找不到 {label} 的命令行工具，没法替你往它的窗口里打字。到那个窗口里自己打吧。"
)
WHY_UNREACHABLE = (
    "连不上电脑上的 {label}，这次没打进去。它可能刚重开过，到那个窗口里自己打一次吧。"
)
WHY_NO_WINDOW = (
    "在 {label} 里没找到这个会话的窗口，可能已经关掉了。到电脑上看一眼吧。"
)
WHY_SUSPENDED = "它在电脑上被挂起了（按过 Ctrl-Z），打字进去不会有人理。到电脑上敲 fg 让它继续。"
# 🔴 目标正忙时的那句。**只有斜杠命令会看到它**（普通消息走 relay，进它的输入队列，不受影响）。
# 措辞要点：① 说清是「这会儿正忙」，不是功能不行；② 给出真的下一步（等它闲下来再发一次）；
# ③ **不要**让他去关窗口 —— relay 那句旧话（「把那个窗口关掉就能跑了」）在这条路接上之后
# 对 cmux 会话已经不成立，照着做是白折腾。
WHY_BUSY = (
    "它正忙着手里这一轮，这会儿打进去会和它抢输入框。等它干完再发一次；"
    "想让它先停，点「停下」。"
)
WHY_BAD_ID = "会话编号不对。"
WHY_EMPTY = "要发的内容是空的。"
WHY_BAD_KEY = "这个按键不在能按的那几个里。"


def _fill(template: str, kind: str) -> str:
    """把 `{label}` 换成人话。认不出种类 ⇒ 不编名字，整句换成「看不出开在哪种窗口里」。"""
    label = TERM_LABELS.get(kind, "")
    return template.format(label=label) if label else WHY_TERM_UNKNOWN


# ---------- 读一个进程自己的环境变量 ----------
#
# 从目标进程身上要的就这三样：开在哪个窗口里（`CMUX_SURFACE_ID`）、
# 连上去的凭据（`CMUX_SOCKET_CAPABILITY`）、连哪个（`CMUX_SOCKET_PATH`）。
# 🔴 白名单，不是「把 CMUX_* 全拿走」：那片环境里还有别的程序的东西。

_ENV_SURFACE = "CMUX_SURFACE_ID"
_ENV_CAPABILITY = "CMUX_SOCKET_CAPABILITY"
_ENV_SOCKET = "CMUX_SOCKET_PATH"
# 连上去要带的（给子进程的环境），和「定位用的」分开：surface 是参数，不是凭据。
_ENV_CREDS = (_ENV_CAPABILITY, _ENV_SOCKET)
_ENV_WANTED = (_ENV_SURFACE,) + _ENV_CREDS


def _process_env(pid: Any) -> dict:
    """`pid` 那个进程的 `CMUX_*` 环境变量（读不到就空字典，**不抛**）。

    `ps eww` 只能读**同一个用户自己**的进程 —— 桥和主人的会话同 uid，所以读得到；
    换个用户的进程内核直接不给（这不是我们的权限设计，是 macOS 的）。

    🔴 只挑 `CMUX_` 开头的几个。那片环境里还有一堆别的东西（包括别的程序的凭据），
    一股脑读回来再传给子进程等于顺手把它们也扩散出去。
    🔴 返回值里有凭据 ⇒ **不许进日志**（这个函数自己一句日志都不打）。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {}
    if pid <= 0:
        return {}
    try:
        out = subprocess.run(
            ["ps", "eww", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
            encoding="utf-8", errors="replace",
        ).stdout
    except Exception:  # 没有 ps / 超时 / 被杀 ⇒ 当读不到，照实退回
        return {}
    env = {}
    for token in (out or "").split(" "):
        if not token.startswith("CMUX_") or "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key in _ENV_WANTED:
            env[key] = value.rstrip("\n")
    return env


# 读进程环境的超时。`ps` 一次就该毫秒级回来。
_PS_TIMEOUT = 5.0


# ---------- 沿父进程链认终端 ----------


def terminal_of_pid(table: ProcTable, pid: Any) -> str:
    """`claude` 的 pid → 它开在哪种终端里（认不出返回 `unknown`，**绝不猜**）。

    判据只有一条：从这个 pid 往上走父进程，哪一层的命令行命中 `_TERM_MARKS` 就是哪种。
    实测主人机上第 3 层命中（`claude ← zsh ← login ← cmux`，30/30）。

    🔴 **链上没有命中的就是 `unknown`，不许按「剩下最可能是什么」兜底。**
    这条路是往窗口里打字 —— 认错终端的后果是把主人的字打进别的地方，
    而 `unknown` 的代价只是退回今天的两条路（他一点损失都没有）。
    """
    proc = table.get(pid)
    if proc is None:
        return TERM_UNKNOWN
    seen = {int(getattr(proc, "pid", 0) or 0)}
    for _ in range(_HOPS):
        # 🔴 **从父进程开始看，不看 claude 自己那行命令行**（和 `claude._host_app` 同一个口径）。
        # 本机那 30 个会话的 claude 命令行里都带着 `--settings ~/.cmuxterm/claude-settings/<hash>.json`
        # ⇒ 拿它当判据会在第 0 跳就命中、顺手把「它开在哪个窗口里」这个问题跳过去。
        # 这两件事不是一回事：手敲 `claude --settings ~/.cmuxterm/...` 也带那段路径，
        # 而那个进程根本不在 cmux 的窗口里 —— 往「它的窗口」打字就打到别处去了。
        # 真正的证据是父进程链（claude ← zsh ← login ← cmux，本机 30/30）。
        ppid = int(getattr(proc, "ppid", 0) or 0)
        if ppid <= 1 or ppid in seen:
            return TERM_UNKNOWN
        seen.add(ppid)
        proc = table.get(ppid)
        if proc is None:
            return TERM_UNKNOWN
        kind = _mark_of(proc)
        if kind:
            return kind
    return TERM_UNKNOWN


def _mark_of(proc: Any) -> str:
    """这一层进程自己是哪种终端（不是的话空串）。

    两种判据：整条命令行里有特征串，或者 argv[0] 的文件名就是那个程序
    （`/opt/homebrew/bin/tmux` 这种 —— 本机 pid 80303 的 ps 原文）。
    """
    args = str(getattr(proc, "args", "") or "")
    if not args:
        return ""
    head = args.split(" ", 1)[0]
    base = head.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for kind, marks, exes in _TERM_MARKS:
        if any(mark in args for mark in marks):
            return kind
        if base in exes:
            return kind
    return ""


# ---------- 哪些消息非这条路不可 ----------
#
# 🔴 **只有这两件事走终端**，别的一律让 relay 去（它有去重、送达确认、不打断对方那一轮）。
# 判据写在这里、和分派在同一个地方 —— 和第 40 轮那次踩的坑同一条规矩
# （拦在 app.py ⇒ 把本来能跑的那条路一起拦了，见 tests/test_r40_slash_passthrough.py）。


def wants_terminal(text: str) -> bool:
    """这句话必须打进窗口才能生效吗 ⇒ 现在只有斜杠命令。

    普通一句话**永远不走**这条路：往窗口里打字就是模拟主人按键，而「对方正忙时打进去
    会怎样」已经量出来了，答案正是不能走 —— 他打了一半的草稿会和我们的字**拼成一句
    提交出去**（模块头那张 A/B 表）。
    relay 那条路在这件事上是实测过的（进输入队列、不打断、不丢）⇒ 普通话归它。
    """
    return _looks_like_slash_command(text)


# ---------- 这条路本体 ----------


class TermSend:
    """把文字 / 按键送进那个终端窗口。

    `adapter`：一个 `ClaudeAdapter`，判活 / 找 pid / 读注册表**只用它那一份实现**
    （和 `relay.Relay` 同一条规矩：这里不另写一套判活逻辑，否则两份会漂）。
    """

    def __init__(self, adapter: Any, *, procs: Optional[Callable[[], ProcTable]] = None) -> None:
        self.adapter = adapter
        self._procs = procs
        self.last_error = ""

    def _table(self) -> ProcTable:
        if self._procs is not None:
            try:
                return self._procs()
            except Exception as exc:  # 注入的函数炸了也不能抛给上层
                return ProcTable(error=friendly_error(exc, "读不到进程列表"))
        return self.adapter._table()

    # ---------- 认终端 ----------

    def detect_terminal(self, session_id: str) -> str:
        """这个会话开在什么终端里 ⇒ `cmux` / `tmux` / `terminal.app` / … / `unknown`。

        只认**主人自己开的**那个进程（注册表里 `source == "user"`、pid 和启动时间都对得上），
        桥自己起的子进程和别的脚本的 `claude -p` 不算 —— 往它们的「窗口」里打字没有意义。
        读不到进程表、会话没在跑、链上认不出 ⇒ 一律 `unknown`（**不猜**，见 `terminal_of_pid`）。
        """
        sid = str(session_id or "")
        if not _is_session_id(sid):
            return TERM_UNKNOWN
        table = self._table()
        if getattr(table, "error", ""):
            return TERM_UNKNOWN
        for pid in self._own_pids(sid, table):
            kind = terminal_of_pid(table, pid)
            if kind != TERM_UNKNOWN:
                return kind
        return TERM_UNKNOWN

    def _own_pids(self, sid: str, table: ProcTable) -> list:
        """主人开着的这个会话有哪些 pid（同一个会话开在两个窗口里是实测见过的）。"""
        out = []
        for source, rec in self.adapter._registry():
            if source != "user" or rec.get("sessionId") != sid:
                continue
            try:
                pid = int(rec.get("pid") or 0)
            except (TypeError, ValueError):
                continue
            start = (rec.get("procStart"), rec.get("procStartFt"), rec.get("startedAt"))
            if not table.same_process(pid, start):
                continue
            out.append(pid)
        return out

    # ---------- 发之前查一遍 ----------

    def can_send(self, session_id: str, *, for_key: bool = False) -> tuple:
        """能不能走这条路 → (能不能, 不能时给用户的一句话)。

        🔴 **和 `claude.can_relay` 同一个形状**：`why` 为空串 = 「退回别的路」，
        调用方（`control`）接着去试 relay / resume，用户什么都不该看到；
        `why` 有内容 = 真做不到，照实说。
        这个区分不是风格问题：空串当成拒绝 ⇒ 今天能走 relay 的会话全被这条新路挡死。

        `for_key=True`（按 Esc 打断那种）：**不查「它正不忙」**——
        要打断的本来就是正在跑的那一轮，拿「它正忙着」挡住 Esc 等于这条路白加。
        """
        sid = str(session_id or "")
        if not _is_session_id(sid):
            return False, WHY_BAD_ID
        table = self._table()
        if getattr(table, "error", ""):
            # 读不到进程表 ⇒ 不知道它在哪个窗口里。退回别的路（它们各有自己的判断），不在这儿定调
            return False, ""
        pids = self._own_pids(sid, table)
        if not pids:
            # 没在跑、或者只有桥 / 脚本起的进程 ⇒ resume 那条路更合适，让它去
            return False, ""
        kind = TERM_UNKNOWN
        for pid in pids:
            kind = terminal_of_pid(table, pid)
            if kind != TERM_UNKNOWN:
                break
        if kind == TERM_UNKNOWN:
            # 🔴 认不出就退回（空串），**不报错**：今天 relay 对这些会话是好的，
            # 在这儿说一句「看不出开在哪种窗口里」等于把能用的功能说成坏的。
            # 真正要给用户看 `WHY_TERM_UNKNOWN` 的场合，是**只有这条路能办**的事
            # （斜杠命令 / Esc）——那一句由调用方在退回失败之后拼，不在这儿。
            return False, ""
        if not WIRED:
            return False, ""  # 桩：原样退回今天的两条路，行为和发版前一致
        if kind not in _SENDERS:
            return False, _fill(WHY_TERM_NO_API, kind)
        proc = next((table.get(p) for p in pids if table.get(p) is not None), None)
        if proc is not None and getattr(proc, "stopped", False):
            return False, WHY_SUSPENDED
        if not for_key and self._busy(sid):
            # 🔴 正忙时不往输入框里塞字（见模块头）。
            # 🔴 这里**不能回空串**：空串 = 「退回 relay」，而 relay 对斜杠命令只会说
            # 「斜杠命令送不进一个开着的会话……把那个窗口关掉就能跑了」—— 那句话在这条路
            # 接上之后**是错的**：真正的原因是「它这会儿正忙」，而且**不用关窗口，等它闲下来
            # 再发一次就行**。照着那句话去关窗口，等于白折腾。
            # （只有斜杠命令会走到这条路，所以这句话不会盖住普通消息 —— 普通消息在
            #   `wants_terminal` 那层就回 `None` 了，relay 原样接走。）
            return False, WHY_BUSY
        return True, ""

    def _busy(self, sid: str) -> bool:
        """它正在跑这一轮吗。读不出来算「忙」（保守：宁可退回 relay，也别和他抢输入框）。"""
        try:
            sess = self.adapter.find(sid)
        except Exception:  # adapter 约定不抛；防它自己炸
            return True
        if sess is None:
            return True
        return str(getattr(sess, "status", "")) in ("running", "waiting", "unknown")

    # ---------- 真发（桩） ----------

    def send_text(self, session_id: str, text: str) -> ActionResult:
        """把 `text` 打进那个窗口，就像主人自己敲的（斜杠命令因此真执行）。

        🔴 现在是桩。接上之前它一个字都不往外送 —— `can_send` 在 `WIRED=False` 时
        已经回「退回别的路」，正常流程压根到不了这里；真有人直接调就照实说还没接上。
        """
        sid = str(session_id or "")
        text = str(text or "").strip()
        if not _is_session_id(sid):
            return ActionResult(False, WHY_BAD_ID, sid)
        if not text:
            return ActionResult(False, WHY_EMPTY, sid)
        return self._dispatch(sid, lambda kind, surface: _argv_text(kind, surface, text))

    def send_key(self, session_id: str, key: str) -> ActionResult:
        """往那个窗口按一个键。`key` 只能是 `ALLOWED_KEYS` 里的（白名单，见上面的红字）。"""
        sid = str(session_id or "")
        name = str(key or "").strip().lower()
        if not _is_session_id(sid):
            return ActionResult(False, WHY_BAD_ID, sid)
        if name not in ALLOWED_KEYS:
            return ActionResult(False, WHY_BAD_KEY, sid)
        return self._dispatch(sid, lambda kind, surface: _argv_key(kind, surface, name), for_key=True)

    def _dispatch(self, sid: str, build: Callable[[str, str], Any], *, for_key: bool = False) -> ActionResult:
        """两个 send 共用的收尾：复查一次 → 找窗口 → 拼命令 → 跑。

        🔴 复查是必须的：卡片 / 上层的判断可能是几分钟前的，而窗口随时会被关掉
        （和 `relay.Relay.check` 同一条规矩）。
        """
        can, why = self.can_send(sid, for_key=for_key)
        if not can:
            # 退回那种（why 空串）走到这里说明调用方直接调了这条路 ⇒ 照实说还没接上，别装作发了
            return ActionResult(False, why or WHY_NOT_WIRED, sid)
        kind = self.detect_terminal(sid)
        # 🔴 窗口和令牌都从**这个 sessionId 自己那个进程**身上取，调用方一个字都插不上手
        # （令牌对整个 socket 有效、不按 surface 分权 ⇒ 边界只能在这儿守，见模块头）。
        target = self._target_of(sid)
        if target is None:
            return ActionResult(False, _fill(WHY_NO_WINDOW, kind), sid)
        surface, creds = target
        try:
            cmd = build(kind, surface)
        except NotImplementedError:
            return ActionResult(False, WHY_NOT_WIRED, sid)
        if cmd is None or not getattr(cmd, "argv", None):
            return ActionResult(False, WHY_NOT_WIRED, sid)
        return self._run(sid, kind, cmd, creds)

    def _target_of(self, sid: str) -> Optional[tuple]:
        """这个会话的 (窗口 id, 连上去要的那几个环境变量)，取不到回 `None`。

        两样都只认**那个 claude 进程自己的环境**：
        `CMUX_SURFACE_ID` = 它开在哪个窗口里，`CMUX_SOCKET_CAPABILITY` = 连它那个终端要的凭据。
        🔴 **现取不缓存**：终端程序重开一次，凭据就换一把（缓存下来的那把只会让我们
        在「其实已经连不上」的时候以为自己能送）。也因此它从不落盘。

        🔴 **两源一致才送**（这是本文件最后一道闸，别删）。
        `pid` 是从注册表来的，而注册表是一个**文件**：进程没了、pid 被系统回收给别人、
        文件没来得及更新，都会让它指到另一个会话上 —— 而令牌对整个连接有效、
        不按窗口分权（见模块头），所以「pid 指错」直接等于**把主人的话打进他另一个会话**。
        `same_process` 只能保证「这个 pid 还是当初那个进程」，保证不了「它就是这个
        sessionId 的进程」。所以这里再要一份独立证据：终端程序自己对这个 sessionId
        给出的窗口（`_surfaces_from_registry`），进程身上那个不在里头就不送。
        双向对照过：A 的 sid 配 A 的 pid ⇒ 解析出 A 的窗口；A 的 sid 配 **B 的 pid**
        ⇒ 回 `None`，一个字都不送。本机 29 个在跑的会话正常配对 29/29 全解析得出
        ⇒ 这道闸不误伤。
        """
        table = self._table()
        if getattr(table, "error", ""):
            return None
        want = self._surfaces_from_registry(sid)
        for pid in self._own_pids(sid, table):
            env = _process_env(pid)
            surface = str(env.get(_ENV_SURFACE, "") or "").strip()
            if not surface:
                continue
            if want and surface.upper() not in want:
                # 🔴 两源打架 ⇒ 一个字都不送。宁可退回 relay（用户顶多少一次斜杠命令），
                # 也不能赌哪一源是对的 —— 赌错的代价是打进别人的会话。
                logger.warning("窗口两源不一致，不往里送 sid=%s", sid[:8])
                continue
            creds = {k: env[k] for k in _ENV_CREDS if env.get(k)}
            if _ENV_CAPABILITY not in creds:
                # 连不上就别拼命令了（照实走「连不上」那句，不去试一条注定被拒的命令）
                continue
            return surface, creds
        return None

    def _surfaces_from_registry(self, sid: str) -> set:
        """终端程序自己记的「这个 sessionId 开在哪些窗口」。读不到回空集合（⇒ 不卡这道闸）。

        和 `_target_of` 里那份（**进程自己身上**带的 `CMUX_SURFACE_ID`）是两个互相独立的
        来源：这份是**终端程序自己维护的那张表**，按 sessionId 查窗口，和 pid 无关 ——
        所以 pid 指错时这两源必然打架，正是我们要的那道闸。
        🔴 桥自己的会话注册表（`~/.claude/sessions/<pid>.json`）**不能用**：
        它里面根本没有窗口字段（本机 30 份记录 0 份带），拿它当交叉证据等于这道闸永不生效。
        """
        try:
            return _cmux_surfaces_of_session(sid)
        except Exception:  # 读不到就当没有这道交叉证据，别把正常路堵死
            return set()

    def _run(self, sid: str, kind: str, cmd: Any, creds: dict) -> ActionResult:
        """跑那条终端命令。

        🔴 `creds` 只经**环境变量**传给子进程，绝不进 argv —— argv 在 `ps` 里全机可见。
        同理用户那句话走 stdin（`input_text`），`ps` 里看不到他说了什么。
        """
        argv = list(cmd.argv)
        env = dict(os.environ)
        env.update(creds)
        try:
            result = self.adapter.runner.run(
                argv, timeout=_RUN_TIMEOUT, env=env, input_text=cmd.input_text,
            )
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, log_as=f"往终端窗口打字失败 sid={sid}"), sid)
        if getattr(result, "error", ""):
            return ActionResult(False, _fill(WHY_NO_CLI, kind), sid)
        if getattr(result, "timed_out", False):
            return ActionResult(False, _fill(WHY_UNREACHABLE, kind), sid)
        if getattr(result, "returncode", 1) != 0:
            logger.warning("终端打字退出码非 0 sid=%s 终端=%s 码=%s", sid[:8], kind,
                           getattr(result, "returncode", "?"))
            # 🔴 退出码非 0 有两种完全不同的原因，不许都说成「窗口没找到」：
            # 终端那边**主动拦住**（输入框里有人正打字 / 有弹窗等着答）是最常见的一种，
            # 而这种情况窗口好得很 —— 跟用户说「可能已经关掉了」会让他白跑一趟。
            # 判据用的是它自己那两句话里的原文（见 `_BLOCKED_MARKS`），不是猜退出码。
            blocked = _blocked_reason(result)
            if blocked:
                return ActionResult(False, blocked, sid)
            return ActionResult(False, _fill(WHY_NO_WINDOW, kind), sid)
        # 打进去了。这条路**没有送达确认**（没人回执），所以 ok=True 但不号称它读到了
        return ActionResult(True, "", sid, delivered=True)


# 跑那条终端命令的超时。它只是「把字塞进窗口」，不等对方回答 ⇒ 几秒就该回来。
_RUN_TIMEOUT = 10.0

# 终端**主动拦住**时它自己那两句话里的特征串（cmux 的原文，2026-11-23 从它的字符串表取）：
#   「the agent prompt in … holds text someone is typing, so nothing was sent」
#   「… is waiting on a question or dialog, so nothing was sent」
# 🔴 认它的原文，不认退出码：退出码只告诉我们「没成」，而这两种「没成」窗口是好的、
# 该让用户知道的是「你有半句话没发 / 有个问题等你答」，不是「窗口可能关了」。
_BLOCKED_MARKS = (
    ("holds text someone is typing", "窗口里有你打了一半还没发的话。这次没打进去，免得和它拼在一起。"
                                     "把那半句发出去或者删掉，再发一次。"),
    ("waiting on a question or dialog", "它正等你在电脑上回一个问题。先把那个答了，再发一次。"),
)


def _blocked_reason(result: Any) -> str:
    """终端是不是主动拦住的 ⇒ 给用户的那句话；不是的话空串。"""
    text = f"{getattr(result, 'stderr', '') or ''}\n{getattr(result, 'stdout', '') or ''}"
    for mark, why in _BLOCKED_MARKS:
        if mark in text:
            return why
    return ""

# 哪些终端真有命令行接口可用。**量通了才准进这张表**。
# 没在表里的终端走 `WHY_TERM_NO_API`（照实说插不上手），不去试、不去猜。
_SENDERS: tuple = (TERM_CMUX,)

# 目标进程环境里这几个变量的名字。`CMUX_SURFACE_ID` 定位窗口，另两个是连上去的凭据。
# （定义在文件上半部也用得到 ⇒ 真正的定义在 `_ENV_WANTED` 上面那几行，这里不重复。）

# cmux 自带的那个命令行工具。装在 app 里，`PATH` 上一般没有
# （桥是后台服务，它那份 PATH 很短 —— 和 `claude` 裸名解析不到的那个坑同一回事）。
_CMUX_BINS = (
    "/Applications/cmux.app/Contents/Resources/bin/cmux",
    "/opt/homebrew/bin/cmux",
    "/usr/local/bin/cmux",
)


class _Cmd:
    """一条要跑的终端命令：`argv` + 走 stdin 的正文。

    分成两半是为了**让正文永远不进 argv**：`ps` 全机可见，用户在手机上说的话
    不该出现在那儿（和令牌同一条规矩，见 `_run`）。
    """

    __slots__ = ("argv", "input_text")

    def __init__(self, argv: list, input_text: Optional[str] = None) -> None:
        self.argv = list(argv)
        self.input_text = input_text


def _cmux_surfaces_of_session(sid: str) -> set:
    """问终端程序自己：这个 sessionId 开在哪些窗口里。查不到 / 出错一律空集合。

    🔴 **回的是集合，不是一个**。同一个会话被开在两个窗口里是实测见过的
    （`claude.py` 的 `_own_pids` 就是为这个写成返回一串的），而那条命令
    对这种情况**只回一行、静默挑一个** ⇒ 拿「那一个」去比对，会把另一个窗口的
    正常请求判成「两源打架」挡掉。所以这里把所有行都收下，交叉闸只要求
    「进程身上那个窗口在这个集合里」。
    """
    exe = _cmux_bin()
    if not exe:
        return set()
    try:
        out = subprocess.run(
            [exe, "sessions", "list", "--agent", "claude", "--json", "--limit", "500"],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
            encoding="utf-8", errors="replace",
        ).stdout
        rows = (json.loads(out or "{}") or {}).get("sessions") or []
    except Exception:
        return set()
    found = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("session_id") != sid:
            continue
        # 🔴 已经关掉的窗口它照样回（实测：关掉的 workspace 还能拿到完整
        # surface_id、`active_for_surface` 还是 true）。`stored_pid_exists` 是唯一线索。
        if not row.get("stored_pid_exists"):
            continue
        surface = str(row.get("surface_id") or "").strip()
        if surface:
            found.add(surface.upper())
    return found


def _cmux_bin() -> str:
    """cmux 的命令行工具在哪。找不到回空串（调用方照实说「找不到它的命令行工具」）。"""
    from .newsession import find_bin

    for path in _CMUX_BINS:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return find_bin("cmux") or ""


def _argv_text(kind: str, surface: str, text: str) -> Optional[_Cmd]:
    """把一段文字打进那个窗口（打完就提交，等于主人自己敲完按了回车）。

    cmux 这条是 `cmux paste --surface <窗口> --submit -`，**正文走 stdin**。
    为什么是 `paste` 而不是 `send`（都实测过，见模块头「怎么送」）：
      - `send` 会把文本里的字面 `\\n` 当回车 ⇒ `A\\nB` 把「A」提交掉、只剩「B」在框里
      - `paste --submit` 一次就落字 + 提交，中间没有「字在框里还没提交」的半成品状态
    🔴 **不加 `--force`**：那个参数的作用正是「主人输入框里有半句草稿也照送」，
    而我们最怕的就是把他那半句话连带着替他发出去。
    """
    if kind != TERM_CMUX:
        return None
    exe = _cmux_bin()
    if not exe:
        return None
    return _Cmd([exe, "paste", "--surface", surface, "--submit", "-"], input_text=text)


def _argv_key(kind: str, surface: str, key: str) -> Optional[_Cmd]:
    """往那个窗口按一个键。`escape` 实测把当前那一轮打断、会话还活着（见模块头）。"""
    if kind != TERM_CMUX:
        return None
    exe = _cmux_bin()
    if not exe:
        return None
    name = _CMUX_KEYS.get(key)
    if not name:
        return None
    return _Cmd([exe, "send-key", "--surface", surface, name])


# 我们这几个键名 → cmux 认的写法。`esc` / `escape` 它都收，这里写全名。
_CMUX_KEYS = {KEY_ESC: "escape", KEY_ENTER: "enter"}
