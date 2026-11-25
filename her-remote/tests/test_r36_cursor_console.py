"""第 36 轮：「Cursor 的控制台里开 claude，飞书总览看不到」。

全部用 tmp_path 假目录 + 固定 ps 文本，不起真 claude。注册表字段照第 36 轮在临时 CLAUDE_CONFIG_DIR 里
真跑 2.1.325 写出来的样子（见 docs/r36/cursor-console.md）。每条修复配一条阳性对照（原来就对的那种照旧对）。
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import FleetJobs, ProcTable  # noqa: E402
from bridge.fleet import claude as claude_mod  # noqa: E402
from bridge.fleet.claude import ClaudeAdapter, norm_entrypoint  # noqa: E402
from bridge.fleet.relay import Relay  # noqa: E402
from bridge.policy import Policy  # noqa: E402

SID_CLI = "a1111111-1111-4111-8111-111111111111"
SID_IDE = "a2222222-2222-4222-8222-222222222222"
SID_DIRTY = "a3333333-3333-4333-8333-333333333333"
SID_SDK = "a4444444-4444-4444-8444-444444444444"
SID_HIST_APP = "a5555555-5555-4555-8555-555555555555"
SID_HIST_CLI = "a6666666-6666-4666-8666-666666666666"

L1 = "Mon Nov 16 09:00:00 2026"
L2 = "Mon Nov 16 09:10:00 2026"
L3 = "Mon Nov 16 09:20:00 2026"
L4 = "Mon Nov 16 09:30:00 2026"
L5 = "Mon Nov 16 09:40:00 2026"

CURSOR_PTY = "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper.app/Contents/MacOS/Cursor Helper --type=utility"


def _ps(rows):
    return "\n".join(f"{pid:>6} {ppid:>6} S+   {lstart}    {etime} {args}" for pid, ppid, lstart, etime, args in rows) + "\n"


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)


def _jsonl(path, records):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _reg(home, pid, sid, lstart, entry, cwd, **kw):
    rec = {"pid": pid, "sessionId": sid, "cwd": cwd, "procStart": lstart, "kind": "interactive",
           "entrypoint": entry, "status": "idle", "version": "2.1.325",
           "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock", "name": f"work-{pid}", "nameSource": "derived"}
    rec.update(kw)
    _write(os.path.join(home, "sessions", f"{pid}.json"), rec)


def _make(tmp_path, ps_rows, regs=(), hist=()):
    home = str(tmp_path / "claude")
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    for pid, sid, lstart, entry in regs:
        _reg(home, pid, sid, lstart, entry, str(work))
    for sid, entry in hist:
        _jsonl(os.path.join(home, "projects", "-work", f"{sid}.jsonl"),
               [{"type": "user", "isSidechain": False, "cwd": str(work), "entrypoint": entry,
                 "message": {"role": "user", "content": "你好"}, "timestamp": "2026-11-16T01:00:00.000Z"}])
    table = ProcTable.parse(_ps(ps_rows))
    jobs = FleetJobs(lstart_of=lambda pid: table.lstart(pid), kill=lambda pid, sig: None)
    policy = Policy(mode="readonly", workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                    model="claude-sonnet-5-5", turn_timeout_sec=60)
    adapter = ClaudeAdapter(home=home, bridge_home=str(tmp_path / "bridge"), app_root=str(tmp_path / "app"),
                            jobs=jobs, policy=policy, procs=lambda: table)
    return adapter, table


# ---------- entrypoint 归一化 ----------

def test_norm_entrypoint_takes_first_word():
    assert norm_entrypoint("cli") == "cli"
    assert norm_entrypoint("cli FOO=1") == "cli"
    assert norm_entrypoint("claude-desktop-3p CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 X=2") == "claude-desktop-3p"
    assert norm_entrypoint("  claude-vscode ") == "claude-vscode"
    assert norm_entrypoint(None) == "" and norm_entrypoint(3) == "" and norm_entrypoint("") == ""


def test_dirty_cli_entry_is_terminal_not_script(tmp_path):
    """实测：CLAUDE_CODE_ENTRYPOINT="cli FOO=1" 原样写进注册表。原来归成「脚本或别的程序」。"""
    rows = [(201, 1, L1, "10:00", "claude"), (202, 1, L2, "10:00", "claude")]
    adapter, _ = _make(tmp_path, rows, regs=[(201, SID_DIRTY, L1, "cli FOO=1"), (202, SID_CLI, L2, "cli")])
    got = {s.id: s for s in adapter.list(20)}
    assert got[SID_DIRTY].owner == "terminal"
    assert got[SID_DIRTY].why_not_send == claude_mod.WHY_SEND_TERMINAL
    assert got[SID_DIRTY].can_relay is True
    # 阳性对照：干净的 cli 照旧
    assert got[SID_CLI].owner == "terminal" and got[SID_CLI].can_relay is True


def test_dirty_entry_in_history_head(tmp_path):
    """jsonl 头里 `claude-desktop-3p <ENV=...>`（本机实测 2 个）原来被标成终端会话。"""
    adapter, _ = _make(tmp_path, [], hist=[
        (SID_HIST_APP, "claude-desktop-3p CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 MCP_CONNECTION_NONBLOCKING=true"),
        (SID_HIST_CLI, "cli"),
    ])
    got = {s.id: s for s in adapter.list(20)}
    assert got[SID_HIST_APP].surface == "app"
    assert got[SID_HIST_APP].extra["entrypoint"] == "claude-desktop-3p"
    assert got[SID_HIST_CLI].surface == "cli"  # 阳性对照


def test_relay_check_accepts_dirty_cli(tmp_path):
    """relay.check 原来也是精确匹配 ⇒ `cli FOO=1` 被拒成「不是终端里的会话」。"""
    from bridge.fleet.relay import WHY_NOT_TERMINAL
    rows = [(201, 1, L1, "10:00", "claude"), (401, 1, L4, "10:00", "claude -p hi")]
    adapter, _ = _make(tmp_path, rows, regs=[(201, SID_DIRTY, L1, "cli FOO=1"), (401, SID_SDK, L4, "sdk-cli")])
    relay = Relay(adapter)
    assert relay.check(SID_DIRTY).why != WHY_NOT_TERMINAL
    # 阳性对照：sdk-cli 照旧被拒
    assert relay.check(SID_SDK).why == WHY_NOT_TERMINAL


# ---------- 编辑器里的官方扩展面板（entrypoint=claude-vscode）----------

def test_ide_panel_is_held_and_labelled(tmp_path):
    """claude-vscode 原来是「来路不明 / 脚本」。它有人占着：不能 --resume 发，不能从外面停。"""
    rows = [(301, 1, L3, "10:00", "/Users/x/.cursor/extensions/anthropic.claude-code/resources/native-binary/claude"),
            (401, 1, L4, "10:00", "claude -p hi")]
    adapter, _ = _make(tmp_path, rows, regs=[(301, SID_IDE, L3, "claude-vscode"), (401, SID_SDK, L4, "sdk-cli")])
    got = {s.id: s for s in adapter.list(20)}
    ide = got[SID_IDE]
    assert ide.owner == "terminal"  # 合法取值里最不能碰的那档
    assert ide.extra["opened_in"] == "ide_panel"
    assert ide.can_send is False and ide.why_not_send == claude_mod.WHY_SEND_IDE
    assert ide.can_stop is False and ide.why_not_stop == claude_mod.WHY_STOP_IDE
    assert ide.can_relay is False and ide.why_not_relay == claude_mod.WHY_RELAY_IDE
    # 阳性对照：sdk-cli 照旧是脚本
    sdk = got[SID_SDK]
    assert sdk.owner == "unknown" and sdk.why_not_send == claude_mod.WHY_SEND_SCRIPT
    assert "opened_in" not in sdk.extra


def test_ide_panel_send_refused_without_spawning(tmp_path):
    rows = [(301, 1, L3, "10:00", "claude")]
    adapter, _ = _make(tmp_path, rows, regs=[(301, SID_IDE, L3, "claude-vscode")])
    calls = []
    adapter.runner.run = lambda *a, **k: calls.append(a)  # 一旦被调就记下
    res = adapter.send(SID_IDE, "hi")
    assert res.ok is False
    assert not [c for c in calls if c and c[0] and c[0][0] != "ps"]


def test_history_ide_entry_marked(tmp_path):
    adapter, _ = _make(tmp_path, [], hist=[(SID_HIST_CLI, "claude-vscode")])
    (sess,) = adapter.list(20)
    assert sess.extra["opened_in"] == "ide_panel" and sess.surface == "cli"


# ---------- Cursor 集成终端：照旧是 terminal，额外标宿主 ----------

def test_cursor_integrated_terminal_host_app(tmp_path):
    """实测：TERM_PROGRAM=vscode / CLAUDECODE=1 不改 entrypoint（仍是 cli）。沿父进程认出 Cursor。"""
    rows = [(900, 1, L1, "1-00:00:00", CURSOR_PTY),
            (901, 900, L1, "10:00", "/bin/zsh -il"),
            (201, 901, L2, "10:00", "claude"),
            (950, 1, L1, "10:00", "login -pf x"),
            (951, 950, L1, "10:00", "-zsh"),
            (202, 951, L3, "10:00", "claude")]
    adapter, _ = _make(tmp_path, rows, regs=[(201, SID_CLI, L2, "cli"), (202, SID_DIRTY, L3, "cli")])
    got = {s.id: s for s in adapter.list(20)}
    assert got[SID_CLI].owner == "terminal" and got[SID_CLI].extra.get("host_app") == "cursor"
    assert "host_app" not in got[SID_DIRTY].extra  # 阳性对照：Terminal.app 里的不标


# ---------- 在跑却没留注册表的 ----------

def test_unregistered_interactive_claude_is_noted(tmp_path):
    """继承 CLAUDE_CODE_CHILD_SESSION=1 的交互 claude 不写注册表（实测 + 官方文档）⇒ 总览底下说一句。"""
    rows = [(201, 1, L1, "10:00", "claude"),
            (202, 1, L2, "05:00", "claude --permission-mode auto"),   # 没注册表、交互 ⇒ 算
            (203, 1, L3, "05:00", "claude -p hi"),                    # -p ⇒ 不算
            (204, 1, L4, "05:00", "claude mcp serve"),                # 子命令 ⇒ 不算
            (205, 1, L5, "00:03", "claude")]                          # 刚起 ⇒ 不算
    adapter, _ = _make(tmp_path, rows, regs=[(201, SID_CLI, L1, "cli")])
    sessions = adapter.list(20)
    assert [s.id for s in sessions] == [SID_CLI]
    assert adapter.last_note == claude_mod.NOTE_UNREGISTERED.format(n=1)
    assert adapter.last_note in adapter.last_error
    # find() 找不到时，这句不能被当成「读不出来」
    assert adapter.find("ffffffff-ffff-4fff-8fff-ffffffffffff") is None
    assert claude_mod.NOTE_UNREGISTERED.format(n=1) not in adapter.last_error


def test_no_note_when_all_registered(tmp_path):
    rows = [(201, 1, L1, "10:00", "claude")]
    adapter, _ = _make(tmp_path, rows, regs=[(201, SID_CLI, L1, "cli")])
    adapter.list(20)
    assert adapter.last_note == "" and adapter.last_error == ""


def test_note_fits_card_note_limit():
    from bridge.fleet.cards import NOTE_CHARS
    assert len("Claude：" + claude_mod.NOTE_UNREGISTERED.format(n=12)) <= NOTE_CHARS


def test_detail_card_says_where_it_is_open():
    """第 36 轮：详情卡上「终端」细分成「终端（Cursor）」/「编辑器面板（VS Code）」。"""
    from types import SimpleNamespace
    from bridge.fleet.cards import surface_label

    s = lambda surface, **extra: SimpleNamespace(surface=surface, extra=extra)
    assert surface_label(s("cli")) == "终端"
    assert surface_label(s("cli", host_app="cursor")) == "终端（Cursor）"
    assert surface_label(s("cli", opened_in="ide_panel", host_app="vscode")) == "编辑器面板（VS Code）"
    assert surface_label(s("cli", host_app="evil")) == "终端"
    assert surface_label(s("app")) == "App"
