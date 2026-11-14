"""Bash 命令里的路径有没有伸到 workspace 外面。

## 为什么要单独一层

`policy.allowed_tools()` 给 Read/Glob/Grep 加了 `Read(<目录>/**)` 作用域，
实测有效（目录外被拒、目录内正常）。**但 Bash 绕得过去** ——
`cat /tmp/outside/secret.txt` 实测仍能读到，因为命令里的路径是运行时才知道的，
没法用工具作用域表达。

2026-11-03 实测现场：
    agent 读 workspace 之外的绝对路径 → 没有 denial、没有卡片、内容进了回复正文。
而用户的预期是"它只能看我这个项目"。

所以在 Bash 这条路上单独判一次：命令里出现的**绝对路径**必须落在
allowed workspaces 内，否则要审批。

## 判据与边界

看三类 token：
1. 以 `/` 或 `~` 开头的绝对路径
2. **相对路径**（含 `..`）—— 按 cwd 拼成绝对路径再判
3. **带 `$` 或反引号的 token**（`$HOME/.ssh/id_rsa`、`` `echo ~`/x ``）——
   展开结果运行时才知道，一律当越界，交给审批

🔴 2026-11-04 review 实测：原来只看第 1 类，注释写"`..` 逃逸由 realpath 收敛"——
那只对 `Policy.resolve_workspace`（决定 cwd）成立，**命令里的 token 从来没被解析**。
workspace 是 `~/codes` 时，只读模式下 `cat ../.ssh/id_rsa`、`cat $HOME/.ssh/id_rsa`、
`cat ../../Documents/x.txt` 全部**不弹卡片直接放行**，内容进了飞书回复。

cwd 不传时按第一个 workspace 算（runner 把 cwd 钉在 workspace 上）。
`cd ..` 之后的相对路径这一层仍然看不全 —— `cd` 本身带越界参数会被第 2 类抓到
（`cd ..` → 解析出 workspace 的父目录 ⇒ 越界）。

**刻意放过的**（它们不是用户数据，挡了会让大量正常命令要审批）：
- `/dev/null`、`/dev/stdin` 之类的设备
- `/tmp`、`/var/folders`（系统临时目录，agent 常拿来放中间结果）
- `/usr`、`/bin`、`/opt`、`/System`、`/Library`（系统只读区，命令本身就在这些路径下）
- `/proc`、`/sys`（Linux）

**刻意要挡的**就是用户的个人数据区：家目录下 workspace 之外的部分
（`~/Documents`、`~/Desktop`、别的项目、`~/.ssh`、`~/Library/...`）。
那才是"它不该看"的东西。

⚠️ 这一层和 `cmdclass` 是两件事：`cmdclass` 判"这条命令会不会改东西"，
这一层判"这条命令碰的路径在不在范围内"。两个都过才不弹卡片。
"""

from __future__ import annotations

import os
import re
import shlex

# 这些前缀下的路径不算"用户数据"，放过。
# 顺序无关，按前缀匹配（都以 / 结尾避免 `/usr` 误配 `/users`）。
_SYSTEM_PREFIXES = (
    "/dev/",
    "/tmp/",
    "/private/tmp/",
    "/var/tmp/",
    "/private/var/folders/",
    "/var/folders/",
    "/usr/",
    "/bin/",
    "/sbin/",
    "/opt/",
    "/System/",
    "/Library/",
    "/proc/",
    "/sys/",
    "/etc/",  # 配置可读；真正敏感的 /etc 文件另有 deny 清单兜
)

# 整个等于这些也放过（不带尾斜杠的写法）
_SYSTEM_EXACT = frozenset({"/dev/null", "/dev/stdin", "/dev/stdout", "/dev/stderr",
                           "/tmp", "/usr", "/bin", "/opt", "/etc"})

# 看起来像路径的 token：以 / 或 ~ 开头
_PATH_LIKE = re.compile(r"^(?:/|~/|~$)")
# 相对路径：含 `/`（`src/x.py`、`../a`），或就是 `.`/`..`
_RELATIVE_LIKE = re.compile(r"^(?:\.\.?(?:/|$)|[^-][^\s]*/)")
# 运行时展开：$VAR、${VAR}、$(...)、反引号
_EXPANDS = re.compile(r"\$|`")


def _is_system_path(path: str) -> bool:
    if path in _SYSTEM_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _SYSTEM_PREFIXES)


# ── 第 14 轮：「程序文本」不是路径 ──
# 🔴 2026-11-06 16:57 主人截图：全授权下「看内存」「看磁盘」照样弹卡。现场 `.asked` 原文：
#   `awk '{printf "%.0f GB\n", $1/1024/1024/1024}'` ⇒ outside=`{printf … $1/1024…};`
#   `df -h / /System/Volumes/Data` ⇒ outside=`/`
# 越界检查排在全授权前面（读边界优先，这条不动），所以误判 = 全授权也问。
# 收窄的只有两类，都不读文件内容：
# 1. awk/sed/grep/rg/jq 的**第一个位置参数是程序 / 正则 / 过滤器**，不是文件。
#    一旦出现从文件取程序的选项（-f/-e/--file/… 或拼在一起的短选项里带 e/f），
#    就不认第一个位置参数是程序 ⇒ 照旧全查（宁可多问）。
# 2. `df` 的参数只用来定位挂载点，输出只有容量。
_PG_PROGRAM_CMDS = frozenset({"awk", "gawk", "mawk", "nawk", "sed", "gsed", "grep", "egrep", "fgrep", "rg", "jq"})
_PG_PROGRAM_FROM_FILE = ("--file", "--regexp", "--expression", "--from-file", "--source", "--include")
_PG_VALUE_OPTS = {
    "awk": frozenset({"-F", "-v"}), "gawk": frozenset({"-F", "-v"}), "mawk": frozenset({"-F", "-v"}),
    "nawk": frozenset({"-F", "-v"}),
    "grep": frozenset({"-m", "-A", "-B", "-C", "--include", "--exclude", "--exclude-dir", "--color"}),
    "egrep": frozenset({"-m", "-A", "-B", "-C"}), "fgrep": frozenset({"-m", "-A", "-B", "-C"}),
    "rg": frozenset({"-g", "--glob", "-t", "--type", "-T", "-m", "-A", "-B", "-C", "-M"}),
    "jq": frozenset({"--arg", "--argjson", "--indent"}),
}
_PG_PATHS_NOT_READ = frozenset({"df"})
_PG_SUBSTITUTION = re.compile(r"\$\(|`|<\(|>\(")
_PG_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "|&", "(", ")", "{", "}", "!", "do", "then", "else"})


def _pg_skipped_indexes(tokens: list[str]) -> set:
    """哪几个 token 是程序文本 / 只用来定位挂载点的参数 ⇒ 不当路径查。判不准的一律不跳。"""
    skip: set = set()
    i = 0
    while i < len(tokens):
        if tokens[i] in _PG_SEPARATORS or tokens[i].startswith((">", "<")):
            i += 1
            continue
        head = tokens[i].rsplit("/", 1)[-1]
        j = i + 1
        end = j
        while end < len(tokens) and tokens[end] not in _PG_SEPARATORS:
            end += 1
        if head in _PG_PATHS_NOT_READ:
            for k in range(j, end):
                if not tokens[k].startswith("-") and not tokens[k - 1].startswith((">", "<")) \
                        and not tokens[k].startswith((">", "<")):
                    skip.add(k)
        elif head in _PG_PROGRAM_CMDS:
            values = _PG_VALUE_OPTS.get(head, frozenset())
            from_file = False
            k = j
            while k < end:
                tok = tokens[k]
                if tok == "--":
                    k += 1
                    break
                if tok.startswith("--"):
                    if tok.split("=", 1)[0] in _PG_PROGRAM_FROM_FILE:
                        from_file = True
                    k += 2 if tok in values else 1
                    continue
                if tok.startswith("-") and len(tok) > 1:
                    if tok in values:
                        k += 2
                        continue
                    letters = tok[1:]
                    if "e" in letters or "f" in letters:
                        from_file = True
                    k += 1
                    continue
                break
            if not from_file and k < end and not tokens[k].startswith((">", "<")):
                skip.add(k)
        i = end
    # 🔴 `awk "$(cat ~/.netrc)"`：程序位上的命令替换是 shell **先跑**的，那才是读文件 ⇒ 不跳
    return {k for k in skip if not _PG_SUBSTITUTION.search(tokens[k])}


def _candidate_paths(command: str) -> list[str]:
    """抽出命令里看起来是路径的 token（绝对、相对、带展开的）。

    🔴 用 `posix=False` 分词保留引号原样？不 —— 用 posix 分词拿到去引号后的值，
    但**展开符号要在原始命令上判**：单引号里的 `$HOME` 不会被 shell 展开，
    却会被我们当成展开。宁可多问一次（误判成越界 = 弹卡片），不要漏放。
    分词失败就退化成按空白切：宁可多抓几个候选，也别漏掉一个真路径。
    """
    skip: set = set()
    try:
        # 带标点分词：`;` `|` 单独成 token，才分得出每一段的命令词（见 `_pg_skipped_indexes`）
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        skip = _pg_skipped_indexes(tokens)
    except ValueError:
        tokens = command.split()
    found = []
    for index, token in enumerate(tokens):
        if index in skip:
            continue
        # 剥掉常见的包裹：`--file=/path`、`-o/path`
        cleaned = token
        if "=" in token:
            cleaned = token.split("=", 1)[1]
        if not cleaned:
            continue
        if _EXPANDS.search(cleaned):
            # 带 $ 或反引号的 token 只在**像路径**时才算 —— `echo $?`、`$((1+1))`
            # 这种不碰文件的不该弹卡片。判据：展开后面跟着 `/`，或整个就是 $HOME 类变量
            if "/" in cleaned or re.fullmatch(r"\$\{?HOME\}?|~", cleaned):
                found.append(cleaned)
            continue
        if _PATH_LIKE.match(cleaned) or _RELATIVE_LIKE.match(cleaned):
            found.append(cleaned)
    return found


def outside_workspace(
    command: str, workspaces: tuple[str, ...], cwd: str = ""
) -> str:
    """命令里有没有伸出 workspace 的路径。返回越界的那个，空串表示没有。

    workspaces 是 `Policy.workspaces`（绝对路径元组）。
    cwd 是这条命令执行时的工作目录（钩子事件里带 `cwd`）；不给就按第一个 workspace。
    """
    if not command or not workspaces:
        return ""

    roots = []
    for workspace in workspaces:
        try:
            roots.append(os.path.realpath(os.path.expanduser(workspace)))
        except OSError:
            continue
    if not roots:
        return ""
    base = cwd or roots[0]

    for raw in _candidate_paths(command):
        if _EXPANDS.search(raw):
            # 展开结果运行时才知道 —— 当越界，交给审批（宁可多问一次）
            return raw
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            expanded = os.path.join(base, expanded)
        if _is_system_path(expanded):
            continue
        # realpath 会跟软链：`~/link-to-ssh` 这种逃逸也抹平
        try:
            resolved = os.path.realpath(expanded)
        except OSError:
            resolved = expanded
        if _is_system_path(resolved):
            continue
        if any(
            resolved == root or resolved.startswith(root + os.sep) for root in roots
        ):
            continue
        return raw
    return ""
