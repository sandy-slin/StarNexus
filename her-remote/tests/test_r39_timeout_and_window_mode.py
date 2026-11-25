"""两件 11-20 的用户反馈。

**一、超时（buyitian：「15 分钟 timeout 这个有点太短了，设成默认 30 分钟吧」）**
- 抬到 30 分钟。这道闸**不管挂死**（卡住本来就会等满全程），放大只影响真在干长活的；
  中途要停有 `/stop`。
- 🔴 顺带修一个 bug：原来有**两个不一样的兜底** —— 读配置那处 900（15 分钟），
  群里干活那两处 600（10 分钟）⇒ 群里的实际上限比用户以为的还短 5 分钟。
- 🔴 超时 / 被停那两支**不带 session_id**（`final` 这时还是空的、第一轮 `resume` 也空）
  ⇒ 上层记不住会话 ⇒ 用户下一句开新轮，做到一半的东西全丢。
  改成从 `system/init` 那条记一份（`progress.session_id`）。
  **这条是台架真跑出来的**：我第一版只把 `(final or {}).get(...)` 搬进超时分支，
  以为就好了，实测 session_id 还是 None。
- 文案：原来「可以把问题拆小一点再问，或者说得更具体些」= 把原因推给用户，
  而干长活的人问得再清楚也会满 ⇒ 改成「发『继续』就接着做」。

**二、新建会话后进群发消息，一堆「❌ 没发进去」**
`_window_argv` 原来**不带** `--permission-mode`（注释写着「= 用户自己那套设置」），
而桥起第一轮用的是 `bypassPermissions`（今天 14c1f705 改的）
⇒ jsonl 里 `user.permissionMode` 记 bypass，窗口里的真进程跑用户自己那档（manual）
⇒ 会话间消息要求「收发双方权限档同类」，relay 按 jsonl 选了 bypass 信使、
  目标实际是 prompting 类 ⇒ **消息被扣住等人点**。
现场：那个会话 jsonl 里 `type=permission-mode` 0 条、只有 1 行 user 带 bypassPermissions；
终端里显示「The sending session's permission mode class doesn't match this session's」。
⇒ 档位抽成 `BRIDGE_PERMISSION_MODE` 一处定义，窗口那条跟着用。
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.normpath(os.path.join(_HERE, ".."))
_ONECLICK = os.path.normpath(os.path.join(_PKG, "..", "..", "feishu-agent-oneclick"))


# ---------- 一、超时值 ----------

def test_default_is_one_hour():
    """🔴 11-22 主人「你再改成 1 个小时」⇒ 1800 → 3600（第三代，见 r40 那组测试）。

    判据不写死数字：`DEFAULT_TURN_TIMEOUT_SEC` 是**唯一**的源，
    模板和它比（下面那条），别让两边各写一个常量。
    """
    from bridge.app import DEFAULT_TURN_TIMEOUT_SEC
    assert DEFAULT_TURN_TIMEOUT_SEC == 3600


@pytest.mark.parametrize("rel", [
    "config.example.toml",
    "../../feishu-agent-oneclick/src/install-mac.sh",
    "../../feishu-agent-oneclick/src/install-windows.ps1",
])
def test_templates_match_the_code_default(rel):
    """用户机器上那份 config.toml 是安装器写的 ⇒ 模板必须和代码兜底一致。

    🔴 比的是**常量**不是写死的数：改默认值时只要动一处，这条自动跟上。
    r17 那次就是模板和代码各改一半，用户拿到的不是我们以为的那个数。
    """
    from bridge.app import DEFAULT_TURN_TIMEOUT_SEC
    text = pathlib.Path(os.path.join(_PKG, rel)).read_text(encoding="utf-8")
    assert f"turn_timeout_sec = {DEFAULT_TURN_TIMEOUT_SEC}" in text
    for stale in (900, 1800):
        if stale != DEFAULT_TURN_TIMEOUT_SEC:
            assert f"turn_timeout_sec = {stale}" not in text, f"模板里还有上一代的 {stale}"


def test_no_second_fallback_value():
    """🔴 原来群里那条路兜底 600、读配置那处 900 —— 两个值不一致就是个 bug。"""
    app = pathlib.Path(os.path.join(_PKG, "bridge", "app.py")).read_text(encoding="utf-8")
    code = "\n".join(l for l in app.splitlines() if not l.strip().startswith("#"))
    assert '"turn_timeout_sec", 600' not in code, "群里那条路还在用 600"
    assert '"turn_timeout_sec", 900' not in code, "还有写死的 900"
    assert code.count("DEFAULT_TURN_TIMEOUT_SEC") >= 3, "三处都该走同一个常量"


# ---------- 超时返回值 ----------

class _FakeProc:
    returncode = None

    def __init__(self, sid: str = "SID-123"):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        if sid:
            self.stdout.feed_data(
                json.dumps({"type": "system", "subtype": "init", "session_id": sid}).encode() + b"\n")

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return -9


def _run(monkeypatch, *, timeout: float, cancel=None, sid: str = "SID-123"):
    from bridge.policy import Policy
    from bridge.runner import ClaudeRunner
    import bridge.runner as rr

    async def fake_exec(*a, **k):
        return _FakeProc(sid)

    monkeypatch.setattr(rr.asyncio, "create_subprocess_exec", fake_exec)
    d = tempfile.mkdtemp()
    pol = Policy(mode="trusted", workspaces=(d,), default_workspace=d,
                 model="claude-sonnet-5-5", turn_timeout_sec=int(timeout) or 1)
    return asyncio.run(ClaudeRunner(pol, config_dir=d).run_turn("x", d, cancel=cancel))


def test_timeout_returns_session_id(monkeypatch):
    """没有它，用户发「继续」会开新轮 —— 我第一版就漏了这个（实测 None）。"""
    res = _run(monkeypatch, timeout=1)
    assert res.timed_out
    assert res.session_id == "SID-123", res


def test_cancel_returns_session_id(monkeypatch):
    """`/stop` 那支同病，一起修（修完一腿当场搜另一腿）。"""
    ev = asyncio.Event()
    ev.set()
    res = _run(monkeypatch, timeout=60, cancel=ev)
    assert res.session_id == "SID-123", res


def test_timeout_copy_does_not_blame_the_user(monkeypatch):
    res = _run(monkeypatch, timeout=1)
    assert "发「继续」" in (res.error or "")
    assert "拆小" not in (res.error or "")


def test_no_session_id_still_works(monkeypatch):
    """阴性对照：init 没给 session_id 也不许炸，只是接不上。"""
    res = _run(monkeypatch, timeout=1, sid="")
    assert res.timed_out and not res.session_id


# ---------- 二、窗口里的权限档 ----------

def test_bridge_permission_mode_is_single_source():
    from bridge.fleet import newsession as ns
    src = pathlib.Path(ns.__file__).read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert code.count('"bypassPermissions"') == 1, "档位写了不止一处（会漂）"
    assert "BRIDGE_PERMISSION_MODE" in code


def test_window_argv_carries_the_mode():
    """窗口里的真进程要和 jsonl 记的是同一档，否则 relay 选错信使、消息被扣。"""
    from bridge.fleet.newsession import BRIDGE_PERMISSION_MODE, _window_argv
    argv = _window_argv("claude", "claude", "SID", "/tmp/x", BRIDGE_PERMISSION_MODE)
    assert "--permission-mode" in argv
    assert argv[argv.index("--permission-mode") + 1] == BRIDGE_PERMISSION_MODE


def test_window_mode_matches_what_the_bridge_ran():
    """两边同类 —— 这就是「没发进去」的判据（relay.mode_class）。"""
    from bridge.fleet.newsession import BRIDGE_PERMISSION_MODE
    from bridge.fleet.relay import mode_class
    assert mode_class(BRIDGE_PERMISSION_MODE) == "bypass"


def test_non_claude_tools_unchanged():
    """codex / cursor 不传档位（它们有自己的 -s / --mode）。"""
    from bridge.fleet.newsession import _window_argv
    assert "--permission-mode" not in _window_argv("codex", "codex", "SID", "/tmp/x", "")
    assert "--permission-mode" not in _window_argv("cursor", "cursor-agent", "SID", "/tmp/x", "")


@pytest.mark.parametrize("win", [False, True])
def test_both_scripts_embed_the_mode(win):
    from bridge.fleet.newsession import (BRIDGE_PERMISSION_MODE, window_script,
                                         window_script_windows)
    fn = window_script_windows if win else window_script
    body = fn("claude", "claude", "SID", "/tmp/x", BRIDGE_PERMISSION_MODE)
    assert "--permission-mode" in body and BRIDGE_PERMISSION_MODE in body


def test_open_window_passes_the_mode():
    """调用处真的传了 —— 不传的话上面几条全绿但线上还是坏的。"""
    from bridge.fleet import newsession as ns
    src = pathlib.Path(ns.__file__).read_text(encoding="utf-8")
    assert "permission_mode=BRIDGE_PERMISSION_MODE if tool == \"claude\" else \"\"" in src
