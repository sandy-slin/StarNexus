"""第 14 轮：全授权下「看内存 / 看磁盘」还弹卡（主人截图 2026-11-06 16:57）。

现场数据（`approvals/57833cfc61b3e648/*.asked`）：TRUSTED 16:57:23 就在，钩子仍 defer，
因为越界检查排在全授权前面，而这两条被误判成「碰到了工作目录外」：
- awk 的程序文本 `{printf "%.0f GB\\n", $1/1024/1024/1024}` 带 `$` 和 `/` ⇒ 被当成路径
- `df -h /` 的 `/` ⇒ 被当成目录外路径（df 只读容量，不读文件内容）
修法只收窄误判，**不动**「全授权也守读边界」：真去读目录外文件的照样问。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge.approval import HOOK_SOURCE

# 钩子是独立进程跑的那份源码（HOOK_SOURCE），直接测它 —— 用户机器上跑的就是这份
_NS: dict = {}
exec(HOOK_SOURCE.split("if __name__ ==")[0], _NS)  # noqa: S102
_outside_workspace = _NS["_outside_workspace"]

WS = (os.path.realpath(os.path.expanduser("~/codes")),)


@pytest.mark.parametrize("cmd", [
    # 主人截图里那两条原文
    "sysctl -n hw.memsize | awk '{printf \"%.0f GB\\n\", $1/1024/1024/1024}'; vm_stat | head -8",
    "df -h / /System/Volumes/Data 2>/dev/null",
    # 同类高频写法
    "df -h",
    "ps aux | awk '{print $2/1024}' | sort -n",
    "git log --oneline | grep '/api/v1'",
    "cat package.json | jq '.scripts.build'",
    "echo a/b | sed 's/\\//-/g'",
])
def test_readonly_shapes_are_not_outside(cmd):
    assert _outside_workspace(cmd, WS, WS[0]) == ""


@pytest.mark.parametrize("cmd,needle", [
    # 读目录外的真文件 ⇒ 照样算越界（读边界不放松）
    ("awk '{print}' ~/.ssh/id_rsa", "~/.ssh/id_rsa"),
    ("grep -r password /Users", "/Users"),
    ("grep -e token ~/.aws/credentials", "~/.aws/credentials"),
    ("awk -f ~/evil.awk data.txt", "~/evil.awk"),
    ("sed -n 1p ~/.zshrc", "~/.zshrc"),
    ("jq . ~/.config/secret.json", "~/.config/secret.json"),
    ("cat $HOME/.netrc", "$HOME/.netrc"),
    ("ls /", "/"),
    ("df -h / && cat ~/.netrc", "~/.netrc"),
    # 对抗：程序位上的命令替换是 shell 先跑的 ⇒ 照样查
    ('awk "$(cat ~/.netrc)"', "$(cat ~/.netrc)"),
    ("grep `cat ~/.netrc` x", "~/.netrc`"),  # 和改之前同一个返回值
    ("df ~/.ssh; cat ~/.ssh/id_rsa", "~/.ssh/id_rsa"),
    ("grep pass < ~/.netrc", "~/.netrc"),
    ("awk '{print}' < ~/.netrc", "~/.netrc"),
    ("df -h / > ~/out.txt", "~/out.txt"),
    ("grep -efoo ~/.aws/credentials", "~/.aws/credentials"),
    ("grep --regexp=foo ~/.aws/credentials", "~/.aws/credentials"),
    ("awk -vX=1 '{print}' ~/.netrc", "~/.netrc"),
    ("sudo awk 1 ~/.netrc", "~/.netrc"),
])
def test_real_outside_reads_still_flagged(cmd, needle):
    assert _outside_workspace(cmd, WS, WS[0]) == needle
