"""第 16 轮：全授权下「看一眼电脑上的东西」不该弹卡（终端历史、桌面、下载、别的项目）。

## 用户原话

> 「领导要远程看电脑，终端历史、桌面、下载、别的项目都在外面」这些都是基本的查阅操作
> 为啥还有弹卡呢 要做放开到整台电脑

## 🔴 放开之前必须先堵的口子（主会话 2026-11-07 实测）

目录外读凭据，现在**只靠工作目录这道边界**挡着：

    cat ~/.ssh/id_rsa       只读判定=True   红线=无
    cat ~/.aws/credentials  只读判定=True   红线=无
    cat ~/.netrc            只读判定=True   红线=无

也就是说：一旦把读范围放到整台电脑，这几条就直接放行了 —— 而回复会进飞书，
等于把私钥发布出去。所以这一轮**先加凭据路径红线，再放开读范围**，两件事必须同一轮上。

## 放开的边界

- 全授权 + **只读命令** + 目录外 ⇒ 放行（不弹卡）
- 全授权 + 目录外 + **会写** ⇒ 照旧（写只许在工作目录里）
- 任何档位 + 凭据路径 ⇒ **拒**（不是弹卡，是不许做：点同意也不行）
- 只读 / 可改档 + 目录外 ⇒ 照旧弹卡（那两档的语义就是「先问你」）
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge.approval import HOOK_SOURCE
from bridge.policy import Policy

OUTSIDE_READS = [
    "cat ~/.zsh_history",
    "tail -50 ~/.bash_history",
    "ls ~/Desktop",
    "ls -la ~/Downloads",
    "cat ~/Documents/notes.txt",
    "ls ~/other-project",
    "grep -r TODO ~/other-project",
    "tail -100 /var/log/system.log",
    "cat ~/Library/Logs/somelog.log",
    "git -C ~/other-project log --oneline -5",
]

CRED_READS = [
    "cat ~/.ssh/id_rsa",
    "cat ~/.ssh/id_ed25519",
    "cat ~/.aws/credentials",
    "cat ~/.netrc",
    "cat ~/.env",
    "cat ~/project/.env.local",
    "grep -r password ~/.config",
    "tail ~/.ssh/config",
    "cat /etc/shadow",
    "cat ~/.docker/config.json",
    "cat ~/.kube/config",
    "cat ~/Library/Keychains/login.keychain-db",
]

OUTSIDE_WRITES = [
    "echo x > ~/Desktop/pwned.txt",
    "rm ~/Downloads/something",
    "mv ~/Desktop/a ~/Desktop/b",
    "touch ~/.zshrc",
]


def _run_hook(command: str, *, trusted: bool, workspace: str, directory: str) -> dict:
    """把钩子写成文件真跑一遍（和线上同一份 HOOK_SOURCE）。"""
    hook = os.path.join(directory, "hook.py")
    with open(hook, "w") as handle:
        handle.write(HOOK_SOURCE)
    approvals = os.path.join(directory, "approvals")
    os.makedirs(approvals, exist_ok=True)
    with open(os.path.join(approvals, "workspaces.json"), "w") as handle:
        json.dump([workspace], handle)
    if trusted:
        with open(os.path.join(approvals, "TRUSTED"), "w") as handle:
            handle.write("1")
    event = {"tool_name": "Bash", "tool_input": {"command": command},
             "cwd": workspace, "session_id": "s1"}
    proc = subprocess.run([sys.executable, hook], input=json.dumps(event),
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "CARHER_APPROVAL_DIR": approvals})
    try:
        return json.loads(proc.stdout)["hookSpecificOutput"]
    except Exception:  # noqa: BLE001
        return {"permissionDecision": f"解析不出来: {proc.stdout[:120]}{proc.stderr[:120]}"}


@pytest.fixture
def box():
    with tempfile.TemporaryDirectory() as tmp:
        ws = os.path.join(tmp, "ws")
        os.makedirs(ws)
        yield ws, tmp


# ---------- 全授权：目录外的查阅要放行 ----------

@pytest.mark.parametrize("cmd", OUTSIDE_READS)
def test_trusted_reads_anywhere_without_a_card(cmd, box):
    ws, tmp = box
    got = _run_hook(cmd, trusted=True, workspace=ws, directory=tmp)
    assert got["permissionDecision"] == "allow", f"{cmd} → {got}"


# ---------- 任何档位：凭据一律不许 ----------

@pytest.mark.parametrize("cmd", CRED_READS)
def test_credentials_are_denied_even_in_trusted(cmd, box):
    ws, tmp = box
    got = _run_hook(cmd, trusted=True, workspace=ws, directory=tmp)
    assert got["permissionDecision"] == "deny", f"🔴 凭据被放行：{cmd} → {got}"


@pytest.mark.parametrize("cmd", CRED_READS[:4])
def test_credentials_denied_in_readonly_too(cmd, box):
    ws, tmp = box
    got = _run_hook(cmd, trusted=False, workspace=ws, directory=tmp)
    assert got["permissionDecision"] == "deny", f"🔴 凭据被放行：{cmd} → {got}"


# ---------- 写边界：第 39 轮起全授权下放开（用户 11-19 拍板「都改成 bypass」，红线保留）----------

@pytest.mark.parametrize("cmd", OUTSIDE_WRITES)
def test_writes_outside_go_through_in_trusted(cmd, box):
    ws, tmp = box
    got = _run_hook(cmd, trusted=True, workspace=ws, directory=tmp)
    assert got["permissionDecision"] == "allow", f"全授权下项目外的写还在拦：{cmd} → {got}"


@pytest.mark.parametrize("cmd", OUTSIDE_WRITES)
def test_writes_outside_still_ask_when_not_trusted(cmd, box):
    """只读 / 可改档的语义是「先问你」，不跟着全授权一起放开。"""
    ws, tmp = box
    got = _run_hook(cmd, trusted=False, workspace=ws, directory=tmp)
    assert got["permissionDecision"] != "allow", f"🔴 非全授权下项目外的写被放行：{cmd} → {got}"


@pytest.mark.parametrize("cmd", [
    "echo x >> ~/.carher-feishu-agent/prefs.json",
    "cp /tmp/x ~/.carher-feishu-agent/config.toml",
    "sed -i '' s/a/b/ $HOME/.carher-feishu-agent/chat_binds.json",
    "cat ~/.carher-feishu-agent/config.toml",
    "cat ~/.carher-feishu-agent/approval.secret",
    "cat ~/.claude-feishu-bridge/.claude.json",
    "cp /tmp/k ~/.ssh/authorized_keys",
])
def test_bridge_home_and_ssh_stay_off_limits_in_trusted(cmd, box):
    """🔴 放开之后还挡着的：改助手自己的配置（换主人、伪造批准）、读它的 app_secret / 令牌、写 ~/.ssh。
    读 config.toml 那条**改前改后都漏**（全授权下 `cat` 直接 allow，app_secret 会跟着回复进飞书），第 39 轮补上。"""
    ws, tmp = box
    got = _run_hook(cmd, trusted=True, workspace=ws, directory=tmp)
    assert got["permissionDecision"] == "deny", f"🔴 全授权下放行了：{cmd} → {got}"


# ---------- 只读档语义不变 ----------

@pytest.mark.parametrize("cmd", ["cat ~/.zsh_history", "ls ~/Desktop"])
def test_readonly_mode_still_asks_for_outside(cmd, box):
    """只读档的语义就是「先问你」，不跟着全授权一起放开。"""
    ws, tmp = box
    got = _run_hook(cmd, trusted=False, workspace=ws, directory=tmp)
    assert got["permissionDecision"] == "defer", f"{cmd} → {got}"


# ---------- Read 工具（不走钩子那条路）----------

def test_trusted_read_tool_covers_whole_machine():
    p = Policy(mode="trusted", workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
               model="claude-sonnet-5-5", turn_timeout_sec=60)
    allowed = " ".join(p.allowed_tools())
    assert "Read(//**)" in allowed, "全授权下 Read 工具还只能看工作目录"


def test_readonly_read_tool_stays_scoped():
    p = Policy(mode="readonly", workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
               model="claude-sonnet-5-5", turn_timeout_sec=60)
    allowed = " ".join(p.allowed_tools())
    assert "Read(//**)" not in allowed, "只读档不该跟着放开"
