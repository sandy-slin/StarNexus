"""给用户看的文案必须是人话 —— 这条测试是**闸门**。

这个产品面向不写代码的同事，他们不知道什么是 agent、看不到本机日志、
不会在手机上敲 `/cancel`。文案里出现这些东西，用户的反应是"这产品不行"。

## 第 11 轮：从正则换成扫 AST（`docs/iteration/review-r9.md` 第 9 条）

原来只用正则抓 `_say(chat, "…")` 这一种写法，实测 `app.py` 74 处出口漏了 52 处：
`_say_with_buttons` / `_say_later*` / `toast(` 名字不匹配、跨行 `+` 拼接只取第一段、
f-string 里的 `{exc}` 看不见，而且**根本不看 `bridge/fleet/`、`menu.py`、`workspace.py`**。

现在的判据（`scan_source`）：

1. **扫哪些文件**：`bridge/**/*.py` 全部（`rglob`，不是列表）。`test_r11_copy.py` 断言
   条数下限和"fleet 也扫到了"，范围被改窄会红。
2. **哪些字符串算"给用户看的"**：AST 里每一个字符串表达式（`Constant`、f-string 的常量段、
   `"…" + x + "…"` 整条拼起来算一条），满足下面任一条就收：
   - 含汉字 —— 这个产品所有给人看的话都是中文；不含汉字的字面量是键名、tag、命令表；
   - 是出口调用（`_say` / `_say_with_buttons` / `_say_later` / `_say_later_with_buttons` /
     `toast`）的直接实参 —— 纯英文的 `_say(chat, "/cancel")` 也逃不掉；
   - 里面有一个桥认得的斜杠命令（`menu.known_commands()`）—— 比如 `f"  /cd {path}"`
     这种没有汉字、但会被 `"\\n".join` 进中文回复里的行。
3. **哪些明确不算**（每条都是"不会发到飞书"的位置，不是"看起来像技术细节"）：
   docstring、`logger.*(…)` 的实参、`raise` / `assert` 里的、比较 / 集合 / 字典键 /
   下标 / `re.*` / `startswith` 这类**拿来匹配**的、以及整条就是一个命令（`"/诊断"`，命令表的值）。
   `raise` 的文字只有被 `{exc}` 透传才会上屏 —— 那一头单独查（下面第 4 条）。
4. **安装器终端里的话**（`print(`、`OK=/WARN=/FATAL=/NOTE=` 开头、`main()`、
   只被安装器调用的脚本和函数，见 `TERMINAL_*`）照样扫、照样计数，但不按飞书的规矩查斜杠命令 ——
   终端里没有按钮可点。`test_r11_copy.py` 盯着：被判成终端的那些，没有一条是飞书出口的实参。

对收进来的每一条查四件事（`CHECKS`）：
- **斜杠命令**：叫用户打 `/xxx`。兜底那几句在 `ALLOWED` 里，每条写了理由；
- **内部名词**：`.py` / `.toml` 文件名、`_` 开头的标识符、`snake_case`、`f()`、绝对路径、
  老词表（agent、stderr、本机日志、None……）；
- **透传异常**：f-string / 拼接里直接塞了 `except … as exc` 的那个 `exc`（含 `str(exc)`、`exc.reason`）；
- **说改不了**：「改不了」「不能改」「只能看」…… 只读档的真实语义是「会先问你」（用户 11-05 拍板）。

## 还没修的（`PENDING`）

不在本角色独占文件里的问题按 (文件, 查哪项, 抓到的词, 文中一段) 记在 `PENDING`，
写清谁去修、改成什么。闸门的规矩：
- 出现 `ALLOWED` / `PENDING` 都没记的 ⇒ 红（**新加的文案被挡住**）；
- `PENDING` 里某条已经修好了还留着 ⇒ 也红（`test_pending_entries_are_still_real`），
  提示把那条删掉 —— 不然这张表会慢慢变成"随便放过"的白名单。
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import sys
from dataclasses import dataclass, field

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = pathlib.Path(os.path.dirname(_HERE))
sys.path.insert(0, str(_ROOT))

BRIDGE_DIR = _ROOT / "bridge"

# 拼接 / f-string 里运行时才有的那一段，用这个占位。故意选一个所有判据正则都不会命中的字符。
HOLE = "‹…›"

CJK = re.compile(r"[\u4e00-\u9fff]")

OUTLETS = frozenset({"_say", "_say_with_buttons", "_say_later", "_say_later_with_buttons", "toast"})
LOG_METHODS = frozenset({"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})
LOGGERS = frozenset({"logger", "logging", "log", "_log", "LOG"})
# 拿字面量去**匹配**用户输入的方法。`get` / `setdefault` 不在里面：`LABELS.get(x, "默认那句")` 的默认值是上屏的。
MATCH_METHODS = frozenset({
    "startswith", "endswith", "split", "rsplit", "replace", "strip", "lstrip", "rstrip",
    "partition", "rpartition", "index", "find", "rfind", "count", "removeprefix", "removesuffix",
})

# ---------- 终端（安装器 / 命令行）里的话 ----------
# 整个文件只被安装器用 `python bridge/xxx.py` 跑、输出进终端（install-mac.sh 里 REG_OUT / BOOTSTRAP_OUT / CARRY_OUT）。
TERMINAL_FILES = frozenset({"bridge/bootstrap.py", "bridge/carryover.py", "bridge/register.py"})
# (文件, 符号)：只进终端或只进日志的函数 / 常量。每条写了去向。
TERMINAL_SYMBOLS = {
    ("bridge/botmenu.py", "validate_tree"): "只给测试和加菜单的人看",
    ("bridge/botmenu.py", "setup"): "安装器 MENU_OUT 打到终端",
    ("bridge/botmenu.py", "_human"): "拼进 setup() 的终端输出",
    ("bridge/botmenu.py", "PUBLISH_ERRORS"): "拼进 setup() 的终端输出",
    ("bridge/botmenu.py", "CONFIG_ERRORS"): "拼进 setup() 的终端输出",
    ("bridge/botmenu.py", "_http"): "错误包进 setup() 的终端输出",
    ("bridge/fleet/groupsetup.py", "Report.render"): "只被 groupsetup.main() print",
    ("bridge/fleet/groupsetup.py", "probe"): "Report.note 只进 render()（终端）",
    ("bridge/pairing.py", "PairingGate.reissue"): "`python -m bridge.pairing reissue` 的终端输出",
    ("bridge/app.py", "explain_ws_failure"): "只被 logger.error 调用（长连接握手失败原因）",
    ("bridge/approval.py", "HOOK_SOURCE"): "钩子脚本的源码本身（另一个进程，输出给 Claude Code 不给飞书）",
    # 11-20：退出码**故意**编进常量模板里 —— errreport 只发 `record.msg`（模板），
    # `%s` 的实参传不回来，所以 4 台 Windows 连着几天装失败、退出码谁都不知道。
    # 这两个只被 `logger.error(installer_rc_log(...))` 用；给用户的话另走
    # 「更新没装上，这一版先跳过了……」那句（不带码）。
    ("bridge/selfupdate.py", "_INSTALLER_RC_LOGS"): "只被 installer_rc_log() 取值、进 logger.error（上报要按码分桶）",
    ("bridge/selfupdate.py", "_INSTALLER_RC_EXTRA"): "同上，带数字的「没单独归类」那批（11-20 加）",
    ("bridge/selfupdate.py", "_INSTALLER_RC_OTHER"): "同上，没列到的码的兜底",
    ("bridge/selfupdate.py", "installer_rc_log"): "只被 logger.error 调用，用户看不到",
}
TERMINAL_PREFIX = re.compile(r"^(OK|WARN|FATAL|NOTE)=")


@dataclass(frozen=True)
class Unit:
    """一条会给人看的话。"""

    file: str
    line: int
    text: str  # 拼好的全文，运行时那段是 HOLE
    symbol: str  # 在哪个函数 / 常量里
    channel: str  # "feishu" / "terminal"
    outlet: str = ""  # 直接是哪个出口调用的实参（空 = 不是直接实参）
    exc_holes: tuple = ()  # 透传进来的异常变量（源码形状）


@dataclass(frozen=True)
class Hit:
    check: str
    token: str
    unit: Unit = field(compare=False)

    def where(self) -> str:
        return f"{self.unit.file}:{self.unit.line} [{self.check}:{self.token}] {self.unit.text[:80]!r}"


# ---------- 收集 ----------


def _call_name(call: ast.Call) -> tuple[str, str]:
    func = call.func
    if isinstance(func, ast.Attribute):
        recv = func.value.id if isinstance(func.value, ast.Name) else ""
        return func.attr, recv
    if isinstance(func, ast.Name):
        return func.id, ""
    return "", ""


def _is_str_node(node) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _is_str_node(node.left) or _is_str_node(node.right)
    return False


def _flatten(node) -> tuple[str, list]:
    """字符串表达式 → (全文, 运行时那几段的表达式)。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value, []
    if isinstance(node, ast.JoinedStr):
        parts, holes = [], []
        for value in node.values:
            if isinstance(value, ast.Constant):
                parts.append(str(value.value))
            else:
                parts.append(HOLE)
                holes.append(value.value)
        return "".join(parts), holes
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _flatten(node.left) if _is_str_node(node.left) else (HOLE, [node.left])
        right = _flatten(node.right) if _is_str_node(node.right) else (HOLE, [node.right])
        return left[0] + right[0], left[1] + right[1]
    return HOLE, [node]


def _exc_refs(expr, names: set) -> str:
    """这一段是不是把异常原文塞进来了。是 ⇒ 返回源码形状。

    只认直接透传：`exc`、`exc.reason`、`str(exc)`、`repr(exc)`。
    `friendly_error(exc, …)` 这种过了一道翻译的不算。
    """
    target = expr
    if isinstance(target, ast.Call) and isinstance(target.func, ast.Name) and target.func.id in {"str", "repr"} \
            and len(target.args) == 1:
        target = target.args[0]
    while isinstance(target, ast.Attribute):
        target = target.value
    if isinstance(target, ast.Name) and target.id in names:
        return ast.unparse(expr)
    return ""


# 只认**没有运行时段**的整条命令。带 `‹…›` 的（`f"  /cd {path}"`）是给人看的候选行，不能在这里放过。
_COMMAND_ONLY = re.compile(r"^\s*/\S+\s*$")


def scan_source(source: str, relative: str, commands=frozenset()) -> list[Unit]:
    """一个文件 → 里面所有会给人看的话。判据见文件头。"""
    tree = ast.parse(source)
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def symbol_of(node) -> str:
        names = []
        cur = parents.get(node)
        child = node
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(cur.name)
            elif isinstance(cur, (ast.Assign, ast.AnnAssign)) and isinstance(parents.get(cur), ast.Module):
                targets = cur.targets if isinstance(cur, ast.Assign) else [cur.target]
                if targets and isinstance(targets[0], ast.Name):
                    names.append(targets[0].id)
            child, cur = cur, parents.get(cur)
        return ".".join(reversed(names))

    def context(node) -> tuple[str, str, bool, set]:
        """→ (跳过的原因, 直接出口名, 在 print 里, 外层 except 绑定的名字)。"""
        skip, outlet, printed = "", "", False
        exc_names: set = set()
        child, cur = node, parents.get(node)
        statement_done = False
        while cur is not None:
            if isinstance(cur, ast.ExceptHandler) and cur.name:
                exc_names.add(cur.name)
            if not statement_done:
                if isinstance(cur, ast.Expr) and cur.value is node:
                    skip = "docstring"
                elif isinstance(cur, (ast.Raise, ast.Assert)):
                    skip = "raise"
                elif isinstance(cur, ast.Compare) or isinstance(cur, ast.Set):
                    skip = skip or "matcher"
                elif isinstance(cur, ast.Subscript) and child is cur.slice:
                    skip = skip or "matcher"
                elif isinstance(cur, ast.Dict) and child in cur.keys:
                    skip = skip or "matcher"
                elif isinstance(cur, ast.Call) and child is not cur.func:
                    name, recv = _call_name(cur)
                    if name in LOG_METHODS and recv in LOGGERS:
                        skip = "log"
                    elif name in {"frozenset", "set"} or recv == "re":
                        skip = skip or "matcher"
                    elif name in MATCH_METHODS and isinstance(cur.func, ast.Attribute):
                        skip = skip or "matcher"
                    elif name == "print":
                        printed = True
                    # 只认**字符串本身**就是实参：`_say_later(waiting["chat_id"], …)` 里的 "chat_id" 不算
                    if name in OUTLETS and not outlet and child is node and child in cur.args:
                        outlet = name
                if isinstance(cur, ast.stmt):
                    statement_done = True
            child, cur = cur, parents.get(cur)
        return skip, outlet, printed, exc_names

    units: list[Unit] = []
    for node in ast.walk(tree):
        if not _is_str_node(node):
            continue
        parent = parents.get(node)
        # 只收最外层那条：f-string 的常量段、拼接里的每一段都并进外层，不重复计。
        if isinstance(parent, (ast.JoinedStr, ast.FormattedValue)):
            continue
        if isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add) and _is_str_node(parent):
            continue
        text, holes = _flatten(node)
        skip, outlet, printed, exc_names = context(node)
        if skip and not outlet:
            continue
        exc_holes = tuple(r for r in (_exc_refs(h, exc_names) for h in holes) if r)
        has_command = any(re.search(rf"(?<![\w/]){re.escape(c)}(?![\w])", text) for c in commands)
        # 纯 `f"{exc}"` 不单独收：它们落进 GroupResult.msg 之类的字段，那些字段今天没人读（AST 查过
        # `.msg` 的 Load 只在 reply.py 里、是另一个对象）。一旦被拼进中文句子或直接进出口，照样会被抓。
        if not (CJK.search(text) or outlet or has_command):
            continue
        if _COMMAND_ONLY.match(text) and not outlet:
            continue  # 整条就是一条命令（命令表的值、resolve_click 拼出来交给分派的），不是句子
        symbol = symbol_of(node)
        top_symbol = symbol.split(".")[0] if symbol else ""
        terminal = (
            relative in TERMINAL_FILES
            or printed
            or bool(TERMINAL_PREFIX.match(text))
            or symbol.split(".")[-1:] == ["main"]
            or any((relative, s) in TERMINAL_SYMBOLS for s in (symbol, top_symbol))
            or any(relative == f and symbol.startswith(s + ".") for f, s in TERMINAL_SYMBOLS)
        )
        units.append(Unit(relative, node.lineno, text, symbol,
                          "terminal" if terminal else "feishu", outlet, exc_holes))
    return units


def bridge_files() -> list[pathlib.Path]:
    return sorted(BRIDGE_DIR.rglob("*.py"))


def known_commands() -> frozenset:
    from bridge.menu import known_commands as _known

    return _known()


def scan_bridge() -> list[Unit]:
    commands = known_commands()
    units: list[Unit] = []
    for path in bridge_files():
        relative = path.relative_to(_ROOT).as_posix()
        units.extend(scan_source(path.read_text(encoding="utf-8"), relative, commands))
    return units


# ---------- 查 ----------

# 叫人打的斜杠命令。前面不能是字母数字 / `.` / `~` / `/` / `:`（排除路径和 URL），
# 后面紧跟第二个 `/` 的是路径（`/Users/x`），也不算。
SLASH = re.compile(r"(?<![\w.~/:\\-])/[A-Za-z\u4e00-\u9fff][\w\u4e00-\u9fff!-]*(?!/)")

INTERNAL = (
    ("文件名", re.compile(r"\b[\w-]+\.(?:py|pyc|toml|jsonl?|log|sh|ps1|plist|sqlite|db)\b")),
    ("下划线标识符", re.compile(r"(?<![\w‹])_[A-Za-z]\w*")),
    ("snake_case", re.compile(r"(?<![\w-])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w-])")),
    ("函数调用", re.compile(r"\b[A-Za-z_]\w*\(\)")),
    ("绝对路径", re.compile(r"/Users/|/home/\w|/private/|/var/folders|~/\.|[A-Za-z]:\\\\Users")),
    # 老词表。`cursor-agent` 是同事要自己装的那个命令的名字（「找不到 cursor-agent，装好之后…」），不算。
    ("黑话", re.compile(
        r"(?<![\w-])(?:agent|subtype|returncode|stderr|traceback|exception|null|None)(?![\w-])|退出码|本机日志",
        re.IGNORECASE)),
)

# 老词表的名字留着：`tests/test_buttons.py::test_button_messages_pass_the_copy_gate` 还 import 它。
# 判据以上面 INTERNAL 的「黑话」那条为准（同一组词，按词边界匹配）。
FORBIDDEN = ("agent", "subtype", "returncode", "退出码", "stderr", "traceback", "本机日志",
             "exception", "null", "None")

CANT_EDIT = re.compile(r"改不了|不能改|没法改|改不动|不能编辑|无法修改|不会改|只能看")


def check_unit(unit: Unit) -> list[Hit]:
    hits: list[Hit] = []
    if unit.channel == "feishu":
        for match in SLASH.finditer(unit.text):
            hits.append(Hit("斜杠命令", match.group(0), unit))
        for name, pattern in INTERNAL:
            for match in pattern.finditer(unit.text):
                hits.append(Hit("内部名词", match.group(0), unit))
        for shape in unit.exc_holes:
            hits.append(Hit("透传异常", shape, unit))
        # 终端里那几句「改不了配置」说的是飞书应用的配置，跟只读档无关 ⇒ 只查飞书这边
        for match in CANT_EDIT.finditer(unit.text):
            hits.append(Hit("说改不了", match.group(0), unit))
    return hits


# (文件, 查哪项, 抓到的词, 文中一段)：一条命中要四样都对上才算被放过 ——
# 只按词放过的话，同一个文件里新加一句「请发 /talk!」也会被这条吃掉。
ALLOWED = {
    # ---- 斜杠命令：卡片发不出去时的文字兜底（那一刻没有按钮可点）----
    ("bridge/app.py", "斜杠命令", "/talk!", "卡片发不出去"):
        "后台接着聊的确认卡发不出去时唯一的确认通道（app._talk_confirm）",
    ("bridge/app.py", "斜杠命令", "/mode!", "确认卡片发不出去"):
        "放开权限的二次确认卡发不出去时唯一的确认通道（menu.resolve_click 的 mode2）",
    ("bridge/fleet/actions.py", "斜杠命令", "/mode", "收回"):
        "typed_fallback：只在按钮卡发不出去时才发",
    # ---- 斜杠命令：换项目要带参数，按钮带不了（actions.do_value 规定不带参数）----
    ("bridge/workspace.py", "斜杠命令", "/cd", "换到哪个项目"):
        "list_dirs：在出现逐项目按钮之前，这是切到某个具体项目的唯一办法",
    ("bridge/workspace.py", "斜杠命令", "/cd", "要去哪个项目"):
        "回的是他刚打的 `/cd`（没带名字）—— 他已经在打字了，告诉他怎么补全",
    ("bridge/workspace.py", "斜杠命令", "/dirs", "要去哪个项目"):
        "同上；按钮名字放在前面（test_workspace 要求这句里有 /dirs）",
    ("bridge/workspace.py", "斜杠命令", "/cd", "  /cd "):
        "模糊匹配到多个时列出来的候选，每行能原样粘回去（test_workspace 的 round-trip 用例）",
    # ---- 斜杠命令：不上屏，是交给命令分派的 ----
    ("bridge/menu.py", "斜杠命令", "/mode", "/mode ‹…›"): "resolve_click 的返回值，走 _handle_command，不发给用户",
    ("bridge/menu.py", "斜杠命令", "/mode!", "/mode! ‹…›"): "resolve_click 的返回值（放开确认卡上那一下），不发给用户",
    ("bridge/menu.py", "斜杠命令", "/model", "/model ‹…›"): "resolve_click 的返回值（换模型），不发给用户",
    # ---- 斜杠命令：帮助最后那一行兜底 ----
    ("bridge/menu.py", "斜杠命令", "/stop", "按钮出不来的时候"): "帮助末行：按钮卡发不出去时的文字通道",
    ("bridge/menu.py", "斜杠命令", "/sessions", "按钮出不来的时候"): "帮助末行兜底：按钮卡发不出去时看会话",
    ("bridge/menu.py", "斜杠命令", "/cd", "按钮出不来的时候"): "帮助末行兜底：换到具体项目要带名字，按钮带不了",
    # ---- 透传异常：不上屏的 ----
    ("bridge/cmdclass.py", "透传异常", "exc", "解析不了这条命令"):
        "cmdclass.classify 的 reason 不上屏：bridge 里只 import 了 never_approvable（approval.py:977），"
        "钩子用的是 HOOK_SOURCE 里自己那份",
    # ---- 斜杠命令：电脑上 Claude Code 自己的命令，叫他在电脑的终端里打，不是在飞书里 ----
    ("bridge/fleet/claude.py", "斜杠命令", "/exit", "在终端里退出它"): "电脑上的终端命令",
    ("bridge/fleet/claude.py", "斜杠命令", "/name", "在电脑上那个会话里"): "电脑上 Claude Code 的命令",
    ("bridge/fleet/relay.py", "斜杠命令", "/name", "在电脑上那个会话里"): "电脑上 Claude Code 的命令",
    ("bridge/fleet/cards.py", "斜杠命令", "/model", "在电脑上敲 /model"): "电脑上 Claude Code 的命令（第 37 轮：终端开着的会话手机换不了模型）",
    # ---- 说改不了：说的不是"只读档改不了文件" ----
    ("bridge/runner.py", "说改不了", "改不了", "不要告诉用户"):
        "给模型的指令，内容恰恰是「别说改不了」",
    ("bridge/fleet/chatbind.py", "说改不了", "改不了", "群名"):
        "说的是群名（client 不支持改名），不是文件；RenameResult.msg 也不上屏",
    ("bridge/fleet/cursor.py", "说改不了", "只能看", "认不出是哪个对话"):
        "说的是认不出的外部会话不往里发，不是只读档（review-r9 第 9 条复查过）",
}

# 本角色（C）改不了的文件里抓到的。谁修、改成什么写在值里；修好了就删掉这条（否则 test_pending_entries_are_still_real 红）。
_OLD_HELP = "不想打字就发「菜单」** — 一排按钮"  # app.HELP 独有的一句；换成 `from .menu import HELP` 后这批自动对不上 ⇒ 删
PENDING: dict = {
    # 2026-11-06 第 11 轮：角色 C 列的 11 处（app.py HELP 整段 + 10 句）主会话全落了，清空。
    # 以后新抓到「不在你独占文件里」的，照原格式加进来：(文件, 检查项, 命中的词, 那句里的一段): 谁去改、改成什么
}


def _key_matches(hit: Hit, key: tuple) -> bool:
    file, check, token, anchor = key
    return hit.unit.file == file and hit.check == check and hit.token == token and anchor in hit.unit.text


def all_hits(units=None) -> list[Hit]:
    units = scan_bridge() if units is None else units
    return [hit for unit in units for hit in check_unit(unit)]


def unexplained(hits) -> list[Hit]:
    keys = list(ALLOWED) + list(PENDING)
    return [h for h in hits if not any(_key_matches(h, k) for k in keys)]


# ---------- 闸门 ----------


@pytest.fixture(scope="module")
def scanned():
    units = scan_bridge()
    return units, all_hits(units)


def test_no_unexplained_copy_problems(scanned):
    """🔴 闸门本体：bridge/ 里任何一句给人看的话，不许叫人打命令、露内部名词、透传异常、说改不了。

    红了 ⇒ 改文案（首选：换成「点菜单里的「…」」）；真的是卡片发不出去时的兜底 ⇒ 进 `ALLOWED` 并写理由。
    """
    _, hits = scanned
    bad = unexplained(hits)
    assert not bad, "文案闸门抓到 %d 条：\n%s" % (len(bad), "\n".join(h.where() for h in bad))


def test_pending_entries_are_still_real(scanned):
    """`ALLOWED` / `PENDING` 里每一条都必须还对得上一处真实命中。

    对不上 = 那句已经改掉了（或换了说法）⇒ 把这条删掉。留着的话，下次有人写出同样的词，
    会被这条早就没用的豁免静默放过。
    """
    _, hits = scanned
    stale = [k for k in list(ALLOWED) + list(PENDING) if not any(_key_matches(h, k) for h in hits)]
    assert not stale, "这些豁免已经对不上任何一句了，删掉：\n" + "\n".join(map(repr, stale))


# ---------- 原来就有、照留的几条 ----------


def test_help_has_examples_and_limits():
    """帮助必须有示例（用户不知道能说什么）和边界（不然会反复试做不到的事）。"""
    from bridge.menu import HELP

    assert "试试" in HELP or "比如" in HELP or "·" in HELP, "要给示例"
    assert "做不到" in HELP or "看不到" in HELP, "要说清边界"


def test_binding_reply_onboards_the_user():
    """绑定成功是第一印象 —— 必须给示例和指路。"""
    source = (_ROOT / "bridge/pairing.py").read_text(encoding="utf-8")
    match = re.search(r'"✅[^)]+\)', source, re.DOTALL)
    assert match, "找不到绑定成功的回复"
    reply = match.group(0)
    assert "试试" in reply, "要给示例提示"
    assert "/cd" in reply or "/help" in reply or "菜单" in reply, "要指路"


def test_first_message_has_no_hardcoded_repo():
    """开场消息和帮助里**不许出现我们自己的仓库名/文件名**。

    同事装完，机器上只有他自己的项目。原来 `BOUND_MESSAGE` 的第二条例句是
    「carher-admin 有什么没提交的改动？」，`HELP` 里还有 `feishu_bridge.py`
    和「backend 的测试」—— 他照着问会得到「那个目录不在范围内」，
    第一印象就是"这不是给我用的"。例句必须在任何仓库里都成立。
    """
    from bridge.app import HELP as APP_HELP
    from bridge.menu import HELP
    from bridge.pairing import BOUND_MESSAGE, GREETING

    ours = ("carher-admin", "carher-bot", "feishu_bridge", "backend 的测试",
            "operator-go", "carher-operator")
    for name, copy in (("BOUND_MESSAGE", BOUND_MESSAGE), ("GREETING", GREETING),
                       ("menu.HELP", HELP), ("app.HELP", APP_HELP)):
        hits = [word for word in ours if word in copy]
        assert not hits, f"{name} 里写死了我们自己的仓库/文件名：{hits}"


def test_greeting_leaks_no_absolute_path():
    """🔴 开场消息会被转发（装完第一件事就是截图问人），绝对路径里带着同事真名。"""
    from bridge.pairing import GREETING

    assert "/Users/" not in GREETING
    assert not re.search(r"/home/\w", GREETING)
    assert "C:\\Users" not in GREETING


def test_greeting_states_the_permission_level_too():
    """扫码那条路的开场也必须说清「现在是哪一档」和「挡什么」。

    🔴 第 17 轮换过判据。默认档从只读改成**全授权**，所以旧断言要求的
    「只读」「问你」「同意」三个词在默认情况下都说不出来 —— 全授权就是**不问**，
    硬要文案出现「点同意才动手」等于逼它说假话（这正是 2026-11-05 栽过的那种事故：
    只读档承诺「不能改文件」，而实测点同意就能改）。
    现在钉三件：说清当前哪一档、说清永远不做什么、给出怎么变安全。
    """
    from bridge.pairing import GREETING

    _assert_states_mode(GREETING, "扫码那条路的开场")


def _assert_states_mode(text: str, where: str) -> None:
    assert "全授权" in text or "只读" in text or "可改" in text, f"{where}没说清现在是哪一档"
    for guard in ("删家目录", "推代码", "私钥"):
        assert guard in text, f"{where}没说清永远不做什么：缺「{guard}」"
    assert "权限" in text, f"{where}没告诉他去哪儿改档位"


def test_unknown_command_copy_names_what_the_user_typed():
    """打错命令那句话要把他打的东西原样回出来，否则他不知道哪儿错了。"""
    from bridge.menu import unknown_command_reply

    line, buttons = unknown_command_reply("/sessoins")
    assert "/sessoins" in line
    assert buttons, "认不出命令时必须给出口按钮，不能只说一句"
    for jargon in ("命令表", "白名单", "分派", "handler"):
        assert jargon not in line, f"文案里有内部术语：{jargon}"


def test_first_message_states_the_permission_level():
    """开场消息必须说清「现在是只读」和「要改东西会先问你」（只读档点同意真的会改，11-05 实测）。"""
    from bridge.pairing import BOUND_MESSAGE

    # 判据同 `test_greeting_states_the_permission_level_too`（见那里的红字）：
    # 默认全授权之后不许再逼文案说「点同意才动手」。
    _assert_states_mode(BOUND_MESSAGE, "配对码那条路的开场")


def test_error_copy_offers_a_next_step(scanned):
    """失败时只说"出错了"会让用户反复重试同一句话。

    第 11 轮起从 AST 收集的出口实参里取（原来是正则，只看 5 个文件）。范围限在
    **直接作为出口实参**的那些：fleet 里的 WHY_* 常量常常是「为什么不行」本身，下一步在按钮上。
    """
    units, _ = scanned
    actionable = ("再试", "再问", "重新", "直接说", "直接打字", "拆小", "具体", "点", "发「", "过一会儿",
                  "/new", "/cd", "/help", "找")
    checked = 0
    weak = []
    for unit in units:
        if unit.channel != "feishu" or not unit.outlet:
            continue
        if not any(k in unit.text for k in ("出错", "失败", "没能", "没出结果")):
            continue
        checked += 1
        if not any(hint in unit.text for hint in actionable):
            weak.append(f"{unit.file}:{unit.line} {unit.text[:70]!r}")
    assert checked >= 3, "没抓到足够的失败文案，这条测试可能失效了"
    assert not weak, "这些失败文案没给下一步：\n" + "\n".join(weak)
