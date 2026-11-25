"""一键脚本（安装器 / 自检）的回归测试。

这些坑 `bash -n` 一个都查不出来 —— 语法全对，只在**运行到那一行**时炸，
所以 build.sh 的语法检查是绿的，用户双击才翻车。

1. **`$VAR` 紧贴全角字符** ⇒ macOS 自带 bash 3.2 在 UTF-8 locale 下把全角字符首字节
   吞进变量名，`set -u` 下直接 `VAR\xef: unbound variable` 退出。
   实测命中两条用户必经路径：安装器「App ID 不以 cli_ 开头」的提示、
   自检第 5 项「凭据不对」的分支 —— 也就是**出错时要给人看的那句话本身会把脚本炸掉**。
   仓库里早有尺子：`scripts/lint-bash-cjk-vars.py`。

2. **`pgrep -fc`** 在 macOS 上不存在（`-c` 不是 BSD pgrep 的选项），整条命令
   usage 报错 + rc=2 ⇒ `|| echo 0` 把计数变成 0 ⇒ 自检第 3 项「实例唯一性」
   永远报「只有一个在跑」，而它恰恰是为了抓「跑了两个在抢消息」而写的。
"""

from __future__ import annotations

import os
import re
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick")
)

SHELL_FILES = (
    "src/install-mac.sh",
    "src/doctor-mac.sh",
    "feishu-agent-mac.command",
    "feishu-agent-doctor-mac.command",
)

# 和 scripts/lint-bash-cjk-vars.py 同一条判据：$VAR 后面紧跟非 ASCII。
ADJACENT_CJK = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")


def _paths():
    return [os.path.join(_ONECLICK, name) for name in SHELL_FILES]


def test_no_bare_var_before_fullwidth_char():
    """`"…（code $CODE）"` 这种写法在 bash 3.2 下会炸 ⇒ 必须写成 ${CODE}。"""
    offenders = []
    for path in _paths():
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                if line.lstrip().startswith("#"):
                    continue
                for name in ADJACENT_CJK.findall(line):
                    offenders.append(f"{os.path.basename(path)}:{lineno} ${name}")
    assert not offenders, "$VAR 紧贴全角字符（bash 3.2 会当成变量名的一部分）：\n" + "\n".join(
        offenders
    )


def test_fullwidth_adjacency_really_breaks_bash():
    """阳性对照：证明上面那条判据不是洁癖，没加花括号真的会退出非零。

    不验这个的话，test_no_bare_var_before_fullwidth_char 可能只是风格检查。
    """
    bad = 'set -u\nCODE=10014\nprintf "%s\\n" "（code $CODE）"\necho REACHED\n'
    good = bad.replace("$CODE", "${CODE}")
    env = {**os.environ, "LC_ALL": "en_US.UTF-8"}

    # 不用 text=True：炸的时候 bash 会把半个 UTF-8 字符写进 stderr，解码会抛。
    def run(script: str):
        return subprocess.run(
            ["/bin/bash", "-c", script], capture_output=True, env=env
        )

    broke = run(bad)
    fixed = run(good)
    assert broke.returncode != 0 and b"REACHED" not in broke.stdout, (
        "本机 bash 不复现这个坑，判据要重新想"
    )
    assert fixed.returncode == 0 and b"REACHED" in fixed.stdout


def test_no_pgrep_dash_c():
    """`pgrep -c` 在 BSD/macOS 上不存在 ⇒ 计数恒为 0，多实例检查形同虚设。"""
    offenders = []
    for path in _paths():
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as handle:
            for lineno, line in enumerate(handle, 1):
                if line.lstrip().startswith("#"):
                    continue
                if re.search(r"pgrep\s+-[a-zA-Z]*c", line):
                    offenders.append(f"{os.path.basename(path)}:{lineno}")
    assert not offenders, (
        "macOS pgrep 没有 -c（会 usage 报错 rc=2，被 || echo 0 吞成 0）：\n"
        + "\n".join(offenders)
    )


def test_pgrep_dash_c_really_fails_here():
    """阳性对照：确认这台机器的 pgrep 真的不认 -c。"""
    proc = subprocess.run(
        ["pgrep", "-fc", "definitely-no-such-process"], capture_output=True, text=True
    )
    assert proc.returncode == 2 and "usage" in proc.stderr.lower(), (
        "这台机器的 pgrep 支持 -c，判据要重新想"
    )


def test_instance_check_counts_two_processes():
    """自检第 3 项必须真能数出 2 个进程。用 pgrep -f + wc -l 的写法验。"""
    # 两个进程的命令行里都带这个标记串，让 pgrep -f 能认出来
    marker = "cfa_instance_selftest_marker"
    # 🔴 收尾按**自己记下的 pid**杀，不用 `pkill -f <标记>`：
    # 跑这段脚本的 bash 自己的命令行里也有这个标记串（实测 `ps` 里能看到），
    # 按名字杀会把父进程一起带走；更要紧的是这种写法会被人照抄到真代码里
    # （2026-11-04 一晚上两个 agent 踩了同一个坑）。见 tests/test_no_name_based_kill.py。
    script = (
        f'/bin/sh -c "MARKER={marker}; sleep 5" >/dev/null 2>&1 &\n'
        "P1=$!\n"
        f'/bin/sh -c "MARKER={marker}; sleep 5" >/dev/null 2>&1 &\n'
        "P2=$!\n"
        "sleep 0.8\n"
        f'N="$(pgrep -f {marker} 2>/dev/null | wc -l | tr -d " ")"\n'
        'echo "COUNT=$N"\n'
        'kill "$P1" "$P2" 2>/dev/null || true\n'
    )
    proc = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True
    )
    match = re.search(r"COUNT=(\d+)", proc.stdout)
    assert match, proc.stdout + proc.stderr
    assert int(match.group(1)) >= 2, (
        f"pgrep -f | wc -l 数不出两个进程（得到 {match.group(1)}）"
    )
