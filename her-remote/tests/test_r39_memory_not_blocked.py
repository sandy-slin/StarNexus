"""agent 自己的记忆不能被「挡桥凭据」那条规则连坐。

现场（某同事 11-21 截图）：「记忆目录被权限挡住了，我读不到固定检查表，这次没法逐项打勾」。

根因：桥给 claude 的是独立配置目录 `~/.claude-feishu-bridge`（不和用户自己的 `~/.claude` 混），
agent 的记忆就落在 `<那个目录>/projects/<项目>/memory/` 下。
而 11-20 我加的 `Read(//**/.claude-feishu-bridge/**)`（07317017，本意只挡桥的凭据）
把**整个目录**挡了 ⇒ 连自己的记忆都读不到。

真 claude 复现 + 对比（同一条命令、同一个文件，只换 deny 规则）：
- 改前：`denials=1`，claude 原话「系统的权限设置拒绝访问 .claude-feishu-bridge/ 目录，
  所以我没有看到 MEMORY.md 的内容」—— 和他截图里那句一字对上。
- 改后：`denials=0`，原样读出了文件内容。
- 阳性对照：`settings.json` / `.claude.json` / `.carher-feishu-agent/config.toml`
  三个凭据文件**仍然全部被拒**（各 denials=1）⇒ 只放开记忆、没放开凭据。
"""

from __future__ import annotations

import fnmatch

import pytest

from bridge.policy import ALWAYS_DENY

_BRIDGE_HOME = "/Users/someone/.claude-feishu-bridge"


def _denied(path: str) -> list:
    """这个路径被 ALWAYS_DENY 里哪几条 Read 规则命中。"""
    hit = []
    for rule in ALWAYS_DENY:
        if not rule.startswith("Read("):
            continue
        pat = rule[len("Read("):-1]
        for cand in (pat, pat.lstrip("/"), "*" + pat.lstrip("/")):
            if fnmatch.fnmatch(path, cand):
                hit.append(rule)
                break
    return hit


# ---------- 记忆不许被挡 ----------

@pytest.mark.parametrize("path", [
    f"{_BRIDGE_HOME}/projects/-Users-x-codes-y/memory/MEMORY.md",
    f"{_BRIDGE_HOME}/projects/-Users-x-codes-y/memory/feedback_a.md",
    f"{_BRIDGE_HOME}/projects/-Users-x-codes-y/memory/checklist.md",
])
def test_agent_memory_is_readable(path):
    """🔴 他的现场：读固定检查表被拒，一轮活没法逐项打勾。"""
    assert _denied(path) == [], f"记忆被这些规则挡住了：{_denied(path)}"


def test_session_transcripts_are_readable():
    """会话记录也在同一个目录下，同样不该被整目录规则连坐。"""
    assert _denied(f"{_BRIDGE_HOME}/projects/-Users-x-codes-y/abc.jsonl") == []


# ---------- 凭据照旧要挡（阳性对照） ----------

@pytest.mark.parametrize("path", [
    f"{_BRIDGE_HOME}/settings.json",
    f"{_BRIDGE_HOME}/.claude.json",
    "/Users/someone/.carher-feishu-agent/config.toml",
    "/Users/someone/.carher-feishu-agent/approval.secret",
    "/Users/someone/.ssh/id_rsa",
    "/Users/someone/.aws/credentials",
])
def test_credentials_still_denied(path):
    """放开记忆**不许**顺带放开凭据 —— 真 claude 实测这三个仍 denials=1。"""
    assert _denied(path), f"{path} 没被挡住了！"


def test_no_whole_directory_rule_left():
    """盯住：别有人把整目录那条加回来（那样记忆又会被连坐）。"""
    bad = [r for r in ALWAYS_DENY if r == "Read(//**/.claude-feishu-bridge/**)"]
    assert not bad, "整目录规则回来了 ⇒ agent 又读不到自己的记忆"


def test_bridge_config_dir_is_separate():
    """这条测试的前提：桥用的确实是独立配置目录（不是 ~/.claude）。"""
    import inspect

    from bridge.runner import ClaudeRunner
    src = inspect.getsource(ClaudeRunner.__init__)
    assert ".claude-feishu-bridge" in src
