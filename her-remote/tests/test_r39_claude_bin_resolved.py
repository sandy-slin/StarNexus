"""桥起 claude 必须用**解析过**的路径，不能留裸名靠 PATH。

现场 a-user …<机器>（Windows）：飞书里一让它干活就报「找不到要用的文件或程序」
（11:03 relay.py:525、11:43 newsession.py:285 两条 FileNotFoundError），
而他 11-18 装 Claude Code 是成功的（上报 `√ 汇总:全部装好 (Claude Code 2.1.326)`）。

真病根：**后台服务那份 PATH 是装机那一刻写死的**（run-agent.ps1 里的 $claudeDir），
claude 后来换了位置 ⇒ 裸名 `"claude"` 解析不到 ⇒ 每次干活都抛。
`find_bin` 本来就是为这个写的（"后台服务的 PATH 很短 ⇒ which 找不到再看默认安装位置"），
但起 claude 的三条腿（runner.py 主路 / claude.py / relay.py 信使）一直都用裸名。

同时补两个洞：
- `_FALLBACKS` 原来**全是 mac 路径**，Windows 一个兜底都没有 ⇒ 他那台根本没救。
- Windows 上不能查 `X_OK`：NTFS 没有执行位，对 `.cmd` / `.exe` 返回 False
  ⇒ 加了 Windows 路径也等于没加。

🔴 以及我自己差点引入的回归：`which` 可能命中**转发壳**（本机实测返回 cmux shim），
把壳写死给后台服务正是装机侧 11-18 修过的坑。
"""

from __future__ import annotations

import os
import sys

import pytest

from bridge.fleet import newsession as ns
from bridge.fleet.newsession import _FALLBACKS, _FALLBACKS_WINDOWS, find_bin
from bridge.runner import _claude_bin


@pytest.fixture()
def win(monkeypatch):
    monkeypatch.setattr(ns, "on_windows", lambda: True)


# ---------- 兜底清单本身 ----------

def test_windows_fallbacks_exist_for_every_tool():
    """每个工具都要有 Windows 兜底 —— 原来一个都没有。"""
    assert set(_FALLBACKS_WINDOWS) == set(_FALLBACKS)
    for tool, cands in _FALLBACKS_WINDOWS.items():
        assert cands, tool
        for c in cands:
            assert "\\" in c, f"{tool} 的 {c!r} 不像 Windows 路径"
            assert c.endswith((".cmd", ".exe")), f"{tool} 的 {c!r} 少扩展名（Windows 上必须带）"


def test_windows_fallbacks_match_installer():
    """和 install-windows.ps1 挑 claudeBin 时试的那组一致（改一处要改两处）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    ins = os.path.normpath(os.path.join(here, "..", "..", "..",
                                        "feishu-agent-oneclick", "src", "install-windows.ps1"))
    text = open(ins, encoding="utf-8").read()
    for frag in (".carher-npm-global\\claude.cmd", ".local\\bin\\claude.exe"):
        assert frag in text, f"安装器不再试 {frag}，两处口径要一起改"
        assert any(frag.replace("\\", "\\") in c for c in _FALLBACKS_WINDOWS["claude"]), frag


# ---------- 解析行为 ----------

def test_which_hit_is_used(monkeypatch):
    assert find_bin("claude", which=lambda n: "/usr/local/bin/claude") == "/usr/local/bin/claude"


def test_windows_fallback_when_which_misses(win, monkeypatch):
    want = os.path.expanduser(r"~\.carher-npm-global\claude.cmd")
    monkeypatch.setattr(os.path, "isfile", lambda p: p == want)
    assert find_bin("claude", which=lambda n: None) == want


def test_windows_does_not_require_exec_bit(win, monkeypatch):
    """🔴 NTFS 没有执行位 ⇒ 查 X_OK 会让所有 Windows 兜底失效。"""
    want = os.path.expanduser(r"~\.local\bin\claude.exe")
    monkeypatch.setattr(os.path, "isfile", lambda p: p == want)
    monkeypatch.setattr(os, "access", lambda *a, **k: False)   # 模拟 NTFS
    assert find_bin("claude", which=lambda n: None) == want


def test_mac_still_requires_exec_bit(monkeypatch):
    """阴性对照：mac 上照旧要可执行位，别把闸门一起撤了。"""
    monkeypatch.setattr(ns, "on_windows", lambda: False)
    monkeypatch.setattr(os.path, "isfile", lambda p: True)
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    assert find_bin("claude", which=lambda n: None) == ""


def test_nothing_found_returns_empty(win, monkeypatch):
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    assert find_bin("claude", which=lambda n: None) == ""


# ---------- 转发壳（我差点引入的回归） ----------

_SHIMS = [
    "/Users/x/.cmuxterm/cmux-cli-shims/UUID/claude",
    r"C:\Users\x\.cmuxterm\cmux-cli-shims\UUID\claude.cmd",
    "/opt/cmux-claude-wrapper",
]


@pytest.mark.parametrize("shim", _SHIMS)
def test_shim_loses_to_a_real_install(shim, monkeypatch):
    """which 命中壳、真实位置也存在 ⇒ 要真的那个。

    把壳写死给后台服务 = 装机侧 11-18 修过的坑（壳会把自己所在目录从 PATH 剔掉
    再找真 claude ⇒ 服务那份 PATH 里没有真的 ⇒ 起不动）。
    """
    monkeypatch.setattr(ns, "on_windows", lambda: False)
    real = "/opt/homebrew/bin/claude"
    monkeypatch.setattr(os.path, "isfile", lambda p: p == real)
    monkeypatch.setattr(os, "access", lambda *a, **k: True)
    assert find_bin("claude", which=lambda n: shim) == real


@pytest.mark.parametrize("shim", _SHIMS)
def test_shim_is_better_than_nothing(shim, monkeypatch):
    """只有壳、没有真装的 ⇒ 还是退回壳（交互窗口里它能跑）。"""
    monkeypatch.setattr(ns, "on_windows", lambda: False)
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    assert find_bin("claude", which=lambda n: shim) == shim


def test_real_machine_does_not_resolve_to_a_shim():
    """本机实测：which 返回的是 cmux shim，find_bin 必须给出非壳的那个。

    这条是真现场（不是构造的）——我第一版就是把这个壳写死进桥的。
    """
    got = find_bin("claude")
    if not got:
        pytest.skip("这台没装 claude")
    assert not ns._looks_like_shim(got), got


# ---------- 三条腿都得用它 ----------

def test_runner_uses_resolved_path():
    assert _claude_bin()
    import inspect
    from bridge import runner
    src = inspect.getsource(runner.ClaudeRunner.run_turn) if hasattr(runner.ClaudeRunner, "run_turn") \
        else open(runner.__file__, encoding="utf-8").read()
    assert '"claude",\n' not in src.replace("_claude_bin()", ""), "argv 里还留着裸名"


def test_runner_falls_back_to_bare_name(monkeypatch):
    """解析不到就还是 "claude" —— 行为不比以前差。"""
    monkeypatch.setattr(ns, "find_bin", lambda *a, **k: "")
    import importlib
    from bridge import runner
    importlib.reload(runner)
    monkeypatch.setattr("bridge.fleet.newsession.find_bin", lambda *a, **k: "")
    assert runner._claude_bin() == "claude"


def test_control_passes_resolved_bin():
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "bridge", "fleet", "control.py"), encoding="utf-8").read()
    assert 'find_bin("claude")' in src
    assert "claude_bin=claude_bin" in src, "解析了但没传给 adapter"


def test_relay_inherits_from_adapter():
    """信使从 adapter 取 claude_bin ⇒ control 改了它自动跟上。"""
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, "..", "bridge", "fleet", "relay.py"), encoding="utf-8").read()
    assert 'getattr(adapter, "claude_bin", "claude")' in src
