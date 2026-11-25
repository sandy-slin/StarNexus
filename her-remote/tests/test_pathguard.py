"""Bash 命令里的路径有没有伸出 workspace。

2026-11-03 实测：`policy` 给 Read/Glob/Grep 加了 `Read(<目录>/**)` 作用域（有效），
**但 Bash 绕得过去** —— `cat <目录外文件>` 照样读到，因为命令里的路径是
运行时才知道的，没法用工具作用域表达。用户的预期是"它只能看我这个项目"。

两组测试同等重要：
- 拦得住越界（安全）
- **不误伤正常命令**（可用性）—— 误伤会让每条命令都弹卡片，比漏判更快毁掉产品
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.pathguard import outside_workspace  # noqa: E402

HOME = os.path.expanduser("~")
WS = (os.path.join(HOME, "codes"),)


MUST_BLOCK = [
    "cat ~/.ssh/id_rsa",
    "cat ~/Documents/secret.txt",
    "ls ~/Desktop",
    "grep -r token ~/Library",
    "wc -l ~/.aws/credentials",
    "cat /Users/SomeoneElse/f.txt",
    f"cat {HOME}/.ssh/config",
    f"tail {HOME}/other-project/.env",
]

MUST_ALLOW = [
    "cat f.txt",
    "ls -la",
    "git status",
    "for d in */; do git -C \"$d\" status; done",
    "ls 2>/dev/null",
    "wc -l *.py",
    "cat /dev/null",
    "ps aux | grep /usr/bin/python",
    "python3 --version",
    "which git",
    "find . -name '*.py'",
    "du -sh /tmp/x",
    f"cat {HOME}/codes/carher-admin/README.md",
    f"ls {HOME}/codes",
]


@pytest.mark.parametrize("command", MUST_BLOCK)
def test_outside_paths_are_caught(command):
    assert outside_workspace(command, WS), f"没拦住：{command}"


@pytest.mark.parametrize("command", MUST_ALLOW)
def test_normal_commands_are_not_touched(command):
    """误伤会让每条命令都弹卡片 —— 比漏判更快毁掉产品。"""
    result = outside_workspace(command, WS)
    assert not result, f"误伤了：{command} -> {result}"


def test_symlink_escape_is_caught():
    """软链逃逸：`~/link -> ~/.ssh` 也要被 realpath 抹平后拦住。"""
    import tempfile

    with tempfile.TemporaryDirectory(dir=HOME) as tmp:
        link = os.path.join(tmp, "escape")
        os.symlink(os.path.join(HOME, ".ssh"), link)
        assert outside_workspace(f"ls {link}", WS)


def test_dotdot_escape_is_caught():
    assert outside_workspace(f"cat {HOME}/codes/../.ssh/id_rsa", WS)


def test_quoted_path_with_space_is_parsed():
    """带空格的路径要整体取出，不能被切成两半漏掉。"""
    assert outside_workspace(f'cat "{HOME}/My Docs/x.txt"', WS)


def test_flag_value_path_is_checked():
    """`--file=<路径>` 这种形式也要查。"""
    assert outside_workspace(f"somecmd --file={HOME}/.ssh/id_rsa", WS)


def test_empty_inputs_are_safe():
    assert outside_workspace("", WS) == ""
    assert outside_workspace("ls", ()) == ""


def test_multiple_workspaces_all_allowed():
    """阳性对照：配了多个目录时每个都该放过。"""
    multi = (os.path.join(HOME, "codes"), os.path.join(HOME, "work"))
    assert not outside_workspace(f"ls {HOME}/work/proj", multi)
    assert not outside_workspace(f"ls {HOME}/codes/proj", multi)
    assert outside_workspace(f"ls {HOME}/Documents", multi)


def test_hook_copy_matches_module():
    """钩子里嵌的那份必须和模块判一样 —— 漂移了会"卡片行为和预期不符"。"""
    from bridge.approval import HOOK_SOURCE

    namespace: dict = {}
    exec(HOOK_SOURCE.split("def main()")[0], namespace)  # noqa: S102
    hook_fn = namespace.get("_outside_workspace")
    assert callable(hook_fn), "钩子里没有 _outside_workspace"
    for command in MUST_BLOCK + MUST_ALLOW:
        assert bool(hook_fn(command, WS)) == bool(
            outside_workspace(command, WS)
        ), f"钩子和模块判定不一致：{command}"


def test_path_guard_runs_before_trusted_shortcut():
    """红线和「助手自己的家 / ~/.ssh」必须排在**任何一个**全授权放行之前。

    原判据是「读边界高于全授权」（2026-11-03 实测顺序写反 ⇒ trusted 下 `cat <目录外文件>` 直接读到）。
    第 39 轮（用户 11-19 拍板「都改成 bypass」，红线保留）全授权下项目外读写都放了，
    所以现在要守的顺序是：先判红线 / 受保护目录，再放行。顺序反了 ⇒ 全授权直接绕过红线。
    """
    from bridge.approval import HOOK_SOURCE

    body = HOOK_SOURCE[HOOK_SOURCE.index("def main()"):]
    first_trusted_allow = min(body.index('decide("allow", "全授权模式：项目外'),
                              body.index('decide("allow", "全授权模式")'))
    for guard in ("never_approvable(", "_is_protected("):
        assert body.index(guard) < first_trusted_allow, (
            f"{guard} 排在全授权放行之后 ⇒ 全授权会绕过它"
        )


# ---------- 相对路径 / 展开（2026-11-04 review 实测漏掉的） ----------

# 🔴 不能放 /tmp 下：/tmp 是系统豁免前缀，`/tmp/pg-ws/../.ssh` 解析成 `/tmp/.ssh`
# 会被当成系统路径放过 —— 测试就测不出相对路径逃逸。用家目录下的路径（不需要真存在）。
_WS_ROOT = os.path.expanduser("~/pg-test-ws")
_WS = (_WS_ROOT,)


@pytest.mark.parametrize("command", [
    "cat ../.ssh/id_rsa",
    "head -c 100 ../.aws/credentials",
    "cat ../../Documents/x.txt",
    "cd .. && cat .zsh_history",
    "cat ./x/../../.ssh/id_rsa",
    "cat $HOME/.ssh/id_rsa",
    "cat ${HOME}/.zsh_history",
    "cat `echo ~`/x",
])
def test_relative_and_expanded_escapes_are_caught(command):
    """🔴 原来只看 `/` `~` 开头的 token。workspace 是 ~/codes 时，只读模式下
    `cat ../.ssh/id_rsa`、`cat $HOME/.ssh/id_rsa` 都不弹卡片直接放行，内容进了飞书回复。"""
    assert outside_workspace(command, _WS, _WS_ROOT), f"漏了：{command}"


@pytest.mark.parametrize("command", [
    "ls", "cat src/main.py", "ls -la ./backend", "grep -rn foo .",
    "wc -l backend/*.py", "echo $?", "git log --oneline -5", "cat README.md",
])
def test_normal_relative_paths_inside_workspace_are_fine(command):
    """阳性对照：工作目录内的相对路径不能弹卡片（否则就回到"一堆询问同意"）。"""
    assert outside_workspace(command, _WS, _WS_ROOT) == ""
