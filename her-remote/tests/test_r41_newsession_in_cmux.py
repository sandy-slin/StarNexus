"""手机「新建会话」开的窗口要开在 cmux 里（2026-11-23）。

主人现场：手机新建会话 ⇒ 窗口开在 Terminal.app ⇒ 那个会话是 31 个里唯一认不出的
（`session-lanes.sh` 30/31）⇒ 在群里发 `/goal 东京天气` 回「这个会话开在终端里，
手机这边插不上手」。另外 30 个都能打进去（cmux 有程序接口，见 `termsend.py`）。

修法：`_open_terminal_default` 在 mac 上**先**试 `cmux workspace create`，
开不了（没装 / 连不上 / 拿不到凭据）才退回 `open -a Terminal`。

端到端实测（走 `open_window_for` 真代码、窗口里真跑 claude）：
  ① 窗口开在 cmux 里、不抢焦点
  ② 进程身上带 `CMUX_SURFACE_ID` + 凭据 ⇒ `detect_terminal` = cmux、`can_send` = True
  ③ `send_text("/goal …")` ⇒ jsonl 里出现 `<command-name>/goal</command-name>`
阴性对照：主人那个开在 Terminal.app 的 ma54c ⇒ `detect_terminal` = terminal.app、`can_send` = False。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import newsession as ns  # noqa: E402


class _R:
    def __init__(self, rc=0, out="OK workspace:1\n"):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def test_mac_tries_cmux_first(monkeypatch):
    """cmux 开得了 ⇒ 不碰 Terminal.app。"""
    calls = []
    monkeypatch.setattr(ns, "on_windows", lambda: False)
    monkeypatch.setattr(ns, "_open_in_cmux", lambda path: calls.append(path) or True)
    monkeypatch.setattr(ns.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该走到 open -a Terminal")))
    ok, why = ns._open_terminal_default("/tmp/x/claude-abc.command")
    assert ok and why == ""
    assert calls == ["/tmp/x/claude-abc.command"]


def test_falls_back_to_terminal_when_cmux_unavailable(monkeypatch):
    """cmux 开不了 ⇒ 原样走 Terminal.app（行为和改之前一样，没装 cmux 的同事不受影响）。"""
    seen = {}
    monkeypatch.setattr(ns, "on_windows", lambda: False)
    monkeypatch.setattr(ns, "_open_in_cmux", lambda path: False)
    monkeypatch.setattr(ns.subprocess, "run", lambda argv, **k: seen.setdefault("argv", argv) and _R())
    ok, _ = ns._open_terminal_default("/tmp/x/claude-abc.command")
    assert ok
    assert seen["argv"][:3] == ["/usr/bin/open", "-a", "Terminal"]


def test_windows_never_touches_cmux(monkeypatch):
    """Windows 上压根没有 cmux，那条腿不许被调到。"""
    monkeypatch.setattr(ns, "on_windows", lambda: True)
    monkeypatch.setattr(ns, "_open_in_cmux",
                        lambda path: (_ for _ in ()).throw(AssertionError("Windows 不该试 cmux")))
    monkeypatch.setattr(ns.subprocess, "run", lambda argv, **k: _R())
    ok, _ = ns._open_terminal_default(r"C:\tmp\x.cmd")
    assert ok


def test_open_in_cmux_needs_credentials(monkeypatch, tmp_path):
    """拿不到凭据 ⇒ False（退回 Terminal），**不去跑一条注定被拒的命令**。"""
    import bridge.fleet.termsend as ts

    monkeypatch.setattr(ts, "_cmux_bin", lambda: "/bin/echo")
    monkeypatch.setattr(ts, "_process_env", lambda pid: {})
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    (tmp_path / ".claude" / "sessions" / "1.json").write_text('{"pid": 1}')
    ran = []
    monkeypatch.setattr(ns.subprocess, "run", lambda *a, **k: ran.append(a) or _R())
    assert ns._open_in_cmux("/tmp/x.command") is False
    assert ran == [], "没凭据还去跑了 cmux"


def test_open_in_cmux_passes_creds_by_env_never_argv(monkeypatch, tmp_path):
    """🔴 凭据只走环境变量：argv 在 `ps` 里全机可见。也不抢焦点（--focus false）。"""
    import bridge.fleet.termsend as ts

    secret = "v1.SECRETTOKEN.xyz"
    monkeypatch.setattr(ts, "_cmux_bin", lambda: "/usr/bin/cmux-fake")
    monkeypatch.setattr(ts, "_process_env", lambda pid: {"CMUX_SOCKET_CAPABILITY": secret})
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    (tmp_path / ".claude" / "sessions" / "1.json").write_text('{"pid": 1}')
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env") or {}
        return _R()

    monkeypatch.setattr(ns.subprocess, "run", fake_run)
    assert ns._open_in_cmux("/tmp/x.command") is True
    assert secret not in " ".join(seen["argv"]), "凭据进了 argv"
    assert seen["env"].get("CMUX_SOCKET_CAPABILITY") == secret
    i = seen["argv"].index("--focus")
    assert seen["argv"][i + 1] == "false", "会抢焦点"


def test_open_in_cmux_rejects_non_ok_output(monkeypatch, tmp_path):
    """rc=0 但没回 OK（比如只打了一句提示）⇒ 不算开成了。"""
    import bridge.fleet.termsend as ts

    monkeypatch.setattr(ts, "_cmux_bin", lambda: "/usr/bin/cmux-fake")
    monkeypatch.setattr(ts, "_process_env", lambda pid: {"CMUX_SOCKET_CAPABILITY": "v1.a.b"})
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude" / "sessions").mkdir(parents=True)
    (tmp_path / ".claude" / "sessions" / "1.json").write_text('{"pid": 1}')
    monkeypatch.setattr(ns.subprocess, "run", lambda *a, **k: _R(rc=0, out="Error: 访问被拒绝"))
    assert ns._open_in_cmux("/tmp/x.command") is False
