"""「➕ 新开一个」勾了「在电脑上开窗口」：Windows 上要真能开，别写 bash 脚本去 open -a Terminal。

现场（11-20 11:43，a-user 机器 …<机器>，Windows，20261119-7af89c47，重装过一次仍报）：
`newsession.py:285:_open_terminal_default` 抛 FileNotFoundError ⇒ 用户看到
「电脑上找不到要用的文件或程序（可能删掉或挪走了），刷新一下再试。」

根因：这条腿整个只有 mac 实现 ——
  - `window_script` 写死 `#!/bin/bash` + `unset` + `shlex.quote`
  - 脚本扩展名写死 `.command`（Windows 上没有关联程序）
  - `os.chmod(0o700)`（NTFS 上没意义）
  - `_open_terminal_default` 写死 `/usr/bin/open -a Terminal`（Windows 根本没这个文件）
`newsession.py` 里一次都没出现过 `on_windows()`，而同目录 `model.py` 早就导出了它。
"""

from __future__ import annotations

import os

import pytest

from bridge.fleet import newsession as ns

SID = "abc12345-0000-0000-0000-000000000000"


# ---------- 命令本身：两个平台共用一个真相 ----------

@pytest.mark.parametrize(("tool", "binary", "want_tail"), [
    ("claude", "claude", ["--resume", SID]),
    ("codex", "codex", ["resume", "-s", "read-only", "-C", "/tmp/x", SID]),
    ("cursor", "cursor-agent", ["--resume", SID, "--workspace", "/tmp/x", "--mode", "ask"]),
])
def test_window_argv_shared(tool, binary, want_tail):
    """两份脚本都从 `_window_argv` 取命令 —— 各写一份必漂。"""
    assert ns._window_argv(tool, binary, SID, "/tmp/x") == [binary] + want_tail


# ---------- Windows 那份 .cmd ----------

def _win(tool="claude", binary=r"C:\Users\x\.carher-npm-global\claude.cmd", cwd=r"D:\我的 项目\web"):
    return ns.window_script_windows(tool, binary, SID, cwd)


def test_windows_script_is_cmd_not_bash():
    s = _win()
    assert s.startswith("@echo off"), s[:40]
    assert "#!/bin/bash" not in s
    assert "unset " not in s, "cmd 里没有 unset，要用 set \"VAR=\""


def test_windows_script_uses_crlf():
    """cmd 对单独的 LF 行尾会出奇怪的解析错误。"""
    assert "\r\n" in _win()
    assert "\n" not in _win().replace("\r\n", "")


def test_windows_script_deletes_itself():
    assert 'del "%~f0"' in _win()


def test_windows_script_clears_inherited_env():
    s = _win()
    for name in ns._CLAUDE_INHERITED:
        assert f'set "{name}="' in s, name


def test_windows_script_cd_crosses_drives():
    """`cd` 不带 /d 换不了盘符 —— 脚本在 C:，目标在 D: 就留在原地。"""
    s = _win(cwd=r"D:\我的 项目\web")
    assert 'cd /d "D:\\我的 项目\\web" || exit /b 1' in s


def test_windows_program_path_always_quoted():
    """🔴 程序路径一律加引号：`C:\\Users\\张 三\\…` 这种机器很常见。

    「这台没空格所以不用引号」会在别人机器上裂成两个参数。
    """
    s = ns.window_script_windows("claude", r"C:\Users\张 三\.local\bin\claude.exe", SID, r"C:\w")
    line = [ln for ln in s.split("\r\n") if "claude.exe" in ln][0]
    assert line.startswith('"C:\\Users\\张 三\\.local\\bin\\claude.exe"'), line
    # 路径里没空格时也要带引号
    s2 = ns.window_script_windows("claude", r"C:\p\claude.cmd", SID, r"C:\w")
    line2 = [ln for ln in s2.split("\r\n") if "claude.cmd" in ln][0]
    assert line2.startswith('"C:\\p\\claude.cmd"'), line2


@pytest.mark.parametrize(("raw", "want"), [
    ("plain", "plain"),
    ("has space", '"has space"'),
    (r"C:\p\x.cmd", r"C:\p\x.cmd"),
    ('q"uote', '"q""uote"'),
    ("a&b", '"a&b"'),
    ("pct%x", '"pct%x"'),
    ("", '""'),          # 裸空串在命令行里会消失 ⇒ 必须留成 ""
])
def test_cmd_quote(raw, want):
    assert ns._cmd_quote(raw) == want


def test_cmd_quote_never_uses_single_quotes():
    """shlex.quote 出单引号，cmd 不认 —— 会把引号当成文件名的一部分。"""
    assert "'" not in ns._cmd_quote("has space")


# ---------- mac 那份不许被改坏 ----------

def test_mac_script_unchanged():
    m = ns.window_script("claude", "/opt/homebrew/bin/claude", SID, "/Users/x/有 空格")
    assert m.startswith("#!/bin/bash")
    assert "unset CLAUDECODE" in m
    assert "'/Users/x/有 空格'" in m          # shlex.quote 还在管事
    assert 'rm -f "$0"' in m


# ---------- 真正开窗口那一步 ----------

def test_open_terminal_uses_cmd_start_on_windows(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(ns, "on_windows", lambda: True)
    monkeypatch.setattr(ns.subprocess, "run", fake_run)
    ok, why = ns._open_terminal_default(r"C:\tmp\carher\claude-abc.cmd")
    assert ok and not why
    assert seen["argv"][:3] == ["cmd", "/c", "start"]
    assert "/usr/bin/open" not in seen["argv"]
    # 窗口标题占位：不给的话 start 会把带空格的路径当标题，什么都不跑
    assert seen["argv"][3] == ""


def test_open_terminal_uses_open_on_mac(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"] = argv

        class R:
            returncode = 0
            stderr = ""
        return R()

    monkeypatch.setattr(ns, "on_windows", lambda: False)
    monkeypatch.setattr(ns.subprocess, "run", fake_run)
    # 11-23 起 mac 先试 cmux；这条测的是「cmux 开不了 ⇒ 退回 Terminal.app」那条腿
    monkeypatch.setattr(ns, "_open_in_cmux", lambda path: False)
    ok, _ = ns._open_terminal_default("/tmp/x/claude-abc.command")
    assert ok
    assert seen["argv"][:3] == ["/usr/bin/open", "-a", "Terminal"]


def test_spawn_failure_logs_the_program_name(monkeypatch):
    """起不来时日志要带程序名（上报只发源码常量模板）。"""
    import logging
    got: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            got.append(record.getMessage())

    lg = logging.getLogger("feishu-bridge.fleet")
    h = H()
    lg.addHandler(h)
    lg.setLevel(logging.ERROR)
    try:
        monkeypatch.setattr(ns, "on_windows", lambda: True)
        monkeypatch.setattr(ns.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError(2, "nope")))
        ok, why = ns._open_terminal_default(r"C:\tmp\x.cmd")
        assert not ok and why
        # 第一条是带程序名的源码常量（上报认的就是它），后面还有 friendly_error 那句通用的
        assert any("程序=cmd" in m for m in got), got
        assert "C:\\" not in why and "nope" not in why, f"给用户的话泄露了路径/原文：{why}"
    finally:
        lg.removeHandler(h)


# ---------- 落盘：扩展名 / 不 chmod ----------

def test_window_file_is_cmd_on_windows(monkeypatch, tmp_path):
    seen = {}

    def fake_open_terminal(path):
        seen["path"] = path
        # 🔴 这里不能写 encoding="mbcs"：mac/Linux 上没有这个编码，LookupError。
        # 代码那边正是因此带了「mbcs 不行就退 UTF-8」的兜底（本轮台架实测走到了这条）。
        seen["body"] = open(path, encoding="utf-8", newline="").read()
        return True, ""

    monkeypatch.setattr(ns, "on_windows", lambda: True)
    chmod_calls = []
    monkeypatch.setattr(ns.os, "chmod", lambda *a, **k: chmod_calls.append(a))
    ok, note = ns.open_window_for("claude", r"C:\p\claude.cmd", SID, str(tmp_path),
                                  open_terminal=fake_open_terminal, script_dir=str(tmp_path))
    assert ok, note
    assert seen["path"].endswith(".cmd"), seen["path"]
    assert seen["body"].startswith("@echo off")
    assert not chmod_calls, "Windows 上不该 chmod（NTFS 上没意义）"


def test_window_file_is_command_on_mac(monkeypatch, tmp_path):
    seen = {}

    def fake_open_terminal(path):
        seen["path"] = path
        seen["mode"] = os.stat(path).st_mode & 0o777
        return True, ""

    monkeypatch.setattr(ns, "on_windows", lambda: False)
    ok, note = ns.open_window_for("claude", "/usr/local/bin/claude", SID, str(tmp_path),
                                 open_terminal=fake_open_terminal, script_dir=str(tmp_path))
    assert ok, note
    assert seen["path"].endswith(".command")
    assert seen["mode"] == 0o700, oct(seen["mode"])
