"""第 25 轮 P-guard：红线（点同意也不许）被几种常见写法绕过 + 输入重定向误弹卡 + 脱敏漏几种常见写法。

P-guard 用真 claude（haiku）+ 桥的真参数 + 真钩子实测：全授权档 `git -C . push origin main`
**真推了出去**（远端出现 refs/heads/main），而菜单承诺「推代码永远不做」。
这里对**真钩子子进程**和真 `never_approvable` / `classify` / `redact` 喂输入。
"""
import json
import os
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.approval import HOOK_SOURCE  # noqa: E402
from bridge.cmdclass import classify, never_approvable  # noqa: E402
from bridge.redact import MASK, redact  # noqa: E402

MUST_BLOCK = [
    "git -C . push origin main", "git --no-pager push",
    "kubectl -n default delete pod x", "helm -n prod uninstall x", "chmod -fR 777 /",
    "cd ~ && rm -rf .", "cd ~ ; rm -rf *", "cd ~; rm -rf ./*", "cd && rm -rf *",
    "cat ~/.s''sh/config", 'cat ~/."ssh"/config', "cat ~/.ss?/config",
    "cat ~/.a''ws/credentials", "cat ~/.config/g''h/hosts.yml", "cat .e''nv",
    "cat ~/.pgpass", "cat ~/.my.cnf", "cat ~/.net''rc",
]
MUST_STAY_APPROVABLE = [
    "git -C webapp branch --show-current", "git -C . status", "git --no-pager log -5",
    "kubectl -n default get pods", "helm -n prod list", "cd ~/codes/proj && rm -rf build",
    "cd /tmp && rm -rf .", "ls ~/Desktop", "echo ssh", "which ssh", "cd ~ && ls",
    "rm -rf ./build", "cat ~/.sshrc", "ls ~/.config/nvim", "chmod 644 a.txt",
    "cd ~ && rm -f notes.txt",
]


@pytest.mark.parametrize("cmd", MUST_BLOCK)
def test_red_line_variants_are_never_approvable(cmd):
    assert never_approvable(cmd), cmd


@pytest.mark.parametrize("cmd", MUST_STAY_APPROVABLE)
def test_everyday_commands_are_not_red_lined(cmd):
    assert never_approvable(cmd) == "", (cmd, never_approvable(cmd))


@pytest.fixture
def hook(tmp_path):
    path = tmp_path / "hook.py"
    path.write_text(HOOK_SOURCE, encoding="utf-8")
    appr = tmp_path / "appr"
    appr.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (appr / "workspaces.json").write_text(json.dumps([str(ws)]), encoding="utf-8")
    (appr / "trusted").write_text("1", encoding="utf-8")       # 全授权档

    def run(cmd):
        env = dict(os.environ, CARHER_APPROVAL_DIR=str(appr))
        proc = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, env=env,
                              input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                                                "cwd": str(ws)}))
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    return run


@pytest.mark.parametrize("cmd", ["git -C . push origin main", "cat ~/.s''sh/config", "cd ~ && rm -rf ."])
def test_real_hook_denies_variants_in_full_control(hook, cmd):
    """钩子是独立进程、用的是内嵌副本 —— 不同步的话 cmdclass 修了也白修。"""
    assert hook(cmd) == "deny", cmd


@pytest.mark.parametrize("cmd", ["git -C . status", "ls ~/Desktop", "wc -l < README.md"])
def test_real_hook_still_allows_everyday_in_full_control(hook, cmd):
    assert hook(cmd) == "allow", cmd


@pytest.mark.parametrize("cmd", ["wc -l < README.md", "sort < a.txt | uniq -c",
                                 "grep -c ERROR < app.log", "head -5 < notes.txt"])
def test_input_redirect_is_a_read(cmd):
    """领导那条抱怨的同一类：`<` 只是把文件喂给标准输入，不写东西。"""
    assert classify(cmd).readonly, classify(cmd).reason


@pytest.mark.parametrize("cmd", ["sh < x.sh", "bash < x.sh", "python3 < x.py", "wc -l < a > b",
                                 "cat < a; rm -rf b", "tee < a b"])
def test_input_redirect_does_not_open_a_hole(cmd):
    assert not classify(cmd).readonly, cmd


@pytest.mark.parametrize("text,secret", [
    ("密码：Tr0ub4dor&3xyz", "Tr0ub4dor"), ("数据库密码是Tr0ub4dor&3xyz", "Tr0ub4dor"),
    ("machine x login me password zxcv1234plain", "zxcv1234"),
    ("passphrase: swordfish123", "swordfish123"),
    ("curl -u admin:MyP@ssw0rd123 https://a.b", "MyP@ssw0rd123"),
    ("mysql -uroot -pS3cretPass1 db", "S3cretPass1"),
])
def test_common_password_shapes_are_masked(text, secret):
    out, hits = redact(text)
    assert hits and secret not in out and MASK in out, out


@pytest.mark.parametrize("text", ["密码是什么意思", "忘记密码怎么办", "the password field is required",
                                  "密钥管理很重要", "curl -u admin https://a", "mysql -u root -p db",
                                  "password reset flow", "把密码改成更长的"])
def test_password_talk_is_not_masked(text):
    assert redact(text) == (text, 0)
