"""Codex adapter：全部用 tmp_path 下的假 ~/.codex + 假 runner，绝不起真 codex / lsof / ps。

重点是两道安全闸：
- 有人占着写锁（App / 终端 / 说不准）⇒ send 拒绝，而且**一个 codex 子进程都没起**
- 不是桥自己起的 pid ⇒ stop 拒绝，而且**没调 kill**
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import FleetJobs, RunResult, SubprocessRunner  # noqa: E402
from bridge.fleet.codex import (  # noqa: E402
    RELAY_ABORTED,
    RELAY_DELIVERED_NO_TEXT,
    RELAY_QUEUED_NO_REPLY,
    RELAY_QUEUED_NOBODY_HOME,
    WHY_NOT_SEND_APP,
    WHY_NOT_SEND_BRIDGE,
    WHY_NOT_SEND_TERMINAL,
    WHY_NOT_SEND_UNKNOWN,
    WHY_NO_IPC_WRITE,
    WHY_NO_WAKE_DEEPLINK,
    WHY_RELAY_ARCHIVED,
    WHY_RELAY_BUSY,
    WHY_RELAY_NO_ROLLOUT,
    WHY_RELAY_NO_UNSEND,
    WHY_RELAY_STOP_APP,
    WHY_RELAY_STOP_TERMINAL,
    CodexAdapter,
    parse_exec_output,
    scan_relay_records,
    thread_id_from_rollout,
)

T_APP = "01a03cf2-1673-7f03-81eb-831cd49fe4d6"
T_CLI = "01a19af2-6177-7dc2-b1bd-3fe88d51929c"
T_EXEC = "01a196db-3498-7b42-940b-e6fa19d02de8"
T_SUB = "01a1b000-0000-7000-8000-000000000001"
T_ARCH = "01a1b000-0000-7000-8000-000000000002"
T_KIRO = "01a1b000-0000-7000-8000-000000000003"
SEG = "01a1a58e-b329-7383-a117-145137260e17"

LSTART = "Tue Nov 3 19:02:35 2026"
LSTART_PS = "Tue Nov  3 19:02:35 2026"

APP_ARGS = ("/Applications/OpenAI Codex.app/Contents/Resources/codex-cli/CodexCLI.app/"
            "Contents/MacOS/codex -c features.code_mode_host=true app-server")


# ---------- 假数据 ----------


def _make_db(home, rows, columns=None):
    columns = columns or [
        "id", "rollout_path", "updated_at", "source", "model_provider", "cwd", "title",
        "archived", "name", "recency_at_ms", "thread_source", "model",
    ]
    conn = sqlite3.connect(os.path.join(home, "state_5.sqlite"))
    conn.execute(f"create table threads ({', '.join(columns)})")
    for row in rows:
        vals = [row.get(c) for c in columns]
        conn.execute(f"insert into threads values ({', '.join('?' * len(columns))})", vals)
    conn.commit()
    conn.close()


def _rollout(home, tid, stamp="2026-11-04T15-55-55", seg=None, records=(), day="2026/11/04", pad=0):
    d = os.path.join(home, "sessions", day)
    os.makedirs(d, exist_ok=True)
    name = f"rollout-{stamp}-{tid}" + (f"_{seg}" if seg else "") + ".jsonl"
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as fh:
        if pad:
            fh.write(json.dumps({"type": "world_state", "payload": {"blob": "x" * pad}}) + "\n")
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def _msg(role, text, ts="2026-11-04T07:55:56.000Z", ctype=None):
    ctype = ctype or ("output_text" if role == "assistant" else "input_text")
    return {"timestamp": ts, "type": "response_item",
            "payload": {"type": "message", "role": role, "content": [{"type": ctype, "text": text}]}}


def _ev(kind):
    return {"timestamp": "2026-11-04T07:55:57.000Z", "type": "event_msg", "payload": {"type": kind}}


def _lock(home, tid):
    d = os.path.join(home, "thread-writer-locks")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{tid}.lock")
    open(path, "w").close()
    return path


class FakeRunner(SubprocessRunner):
    """按 argv 分派：lsof / ps / codex。记下每次调用，绝不起真进程。"""

    def __init__(self, holders=None, ps_rows=(), lsof_result=None, codex_result=None, codex_hook=None,
                 queue_result=None, queue_hook=None):
        self.calls = []
        self.holders = holders or {}  # lock path -> [pid]
        self.ps_rows = list(ps_rows)  # (pid, ppid, stat, args)
        self.lsof_result = lsof_result
        self.codex_result = codex_result or RunResult(0, stdout="")
        self.codex_hook = codex_hook
        self.queue_result = queue_result  # 没给就用 rc=0（`codex queue` 实测立刻退 0）
        self.queue_hook = queue_hook

    def codex_calls(self):
        return [c for c in self.calls if os.path.basename(c["argv"][0]) == "codex"]

    def exec_calls(self):
        return [c for c in self.codex_calls() if "exec" in c["argv"]]

    def queue_calls(self):
        return [c for c in self.codex_calls() if "queue" in c["argv"]]

    def lsof_calls(self):
        return [c for c in self.calls if os.path.basename(c["argv"][0]) == "lsof"]

    def run(self, argv, *, timeout, cwd=None, env=None, input_text=None, on_spawn=None):
        argv = list(argv)
        self.calls.append({"argv": argv, "timeout": timeout, "cwd": cwd, "env": env,
                           "input_text": input_text})
        assert timeout and timeout > 0
        exe = os.path.basename(argv[0])
        if exe == "lsof":
            if self.lsof_result is not None:
                return self.lsof_result
            out = []
            any_held = False
            for path in argv[3:]:
                for pid in self.holders.get(path, []):
                    any_held = True
                    out += [f"p{pid}", "f36", f"n{path}"]
            return RunResult(0 if any_held else 1, stdout="\n".join(out) + ("\n" if out else ""))
        if exe == "ps":
            lines = [f"{pid:>5} {ppid:>5} {stat:<4} {LSTART_PS}     01:00 {args}"
                     for pid, ppid, stat, args in self.ps_rows]
            return RunResult(0, stdout="\n".join(lines) + "\n")
        if exe == "codex":
            if "queue" in argv:
                # `codex queue` 不该被登记成「桥在跑这个会话」（它立刻就退了）
                assert on_spawn is None, "queue 不用 on_spawn"
                if self.queue_hook is not None:
                    self.queue_hook()
                return self.queue_result if self.queue_result is not None else RunResult(0)
            if on_spawn is not None:
                on_spawn(4242)
            if self.codex_hook is not None:
                self.codex_hook()
            return self.codex_result
        raise AssertionError(f"意外的子进程 {argv}")


class Kills:
    def __init__(self):
        self.calls = []

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))


def _jobs(kills, known=(4242,)):
    known = set(known)
    return FleetJobs(lstart_of=lambda pid: LSTART if pid in known else "", kill=kills)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "codex-home"
    h.mkdir()
    proj = tmp_path / "proj"
    proj.mkdir()
    rows = [
        {"id": T_APP, "source": "vscode", "cwd": str(proj), "title": "重置下所有的 cursor key",
         "archived": 0, "name": None, "updated_at": 1793779042, "recency_at_ms": 1793779042000,
         "thread_source": "user", "model_provider": "openai", "model": "gpt-5.5"},
        {"id": T_CLI, "source": "cli", "cwd": str(proj), "title": "cli 的标题", "name": "我改过的名字",
         "archived": 0, "updated_at": 1793600945, "recency_at_ms": 1793600945000, "thread_source": "user"},
        {"id": T_EXEC, "source": "exec", "cwd": str(proj), "title": "生成一个视频", "archived": 0,
         "updated_at": 1793532661, "recency_at_ms": 1793532661000, "thread_source": None},
        {"id": T_KIRO, "source": "exec", "cwd": str(proj), "title": "别的提供方", "archived": 0,
         "updated_at": 1793500000, "recency_at_ms": 1793500000000, "model_provider": "kiro"},
        {"id": T_SUB, "source": '{"subagent":{"thread_spawn":{"parent_thread_id":"x"}}}', "cwd": str(proj),
         "title": "子 agent", "archived": 0, "updated_at": 1793800000, "recency_at_ms": 1793800000000,
         "thread_source": "subagent"},
        {"id": T_ARCH, "source": "cli", "cwd": str(proj), "title": "归档的", "archived": 1,
         "updated_at": 1793900000, "recency_at_ms": 1793900000000},
    ]
    _make_db(str(h), rows)
    return h


def _adapter(home, runner=None, kills=None, known=(4242,)):
    kills = kills if kills is not None else Kills()
    runner = runner or FakeRunner()
    return CodexAdapter(home=str(home), jobs=_jobs(kills, known), runner=runner,
                        codex_bin="/fake/bin/codex", lsof_bin="/usr/sbin/lsof"), runner, kills


# ---------- 纯函数 ----------


def test_thread_id_is_first_uuid():
    name = f"rollout-2026-11-04T15-55-55-{T_APP}_{SEG}.jsonl"
    assert thread_id_from_rollout("/x/" + name) == T_APP
    assert thread_id_from_rollout(f"rollout-2026-11-02T14-28-58-{T_CLI}.jsonl") == T_CLI
    assert thread_id_from_rollout(f"rollout-2026-11-02T14-28-58-{T_CLI}.jsonl.zst") == ""
    assert thread_id_from_rollout("random.jsonl") == ""


def test_parse_exec_output_warnings_are_not_failures():
    out = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": T_CLI}),
        json.dumps({"type": "item.completed", "item": {"type": "error", "message": "service tier"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "第一段"}}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "PONG"}}),
        "not json",
        json.dumps({"type": "turn.completed", "usage": {}}),
    ])
    assert parse_exec_output(out) == ("PONG", "")
    failed = json.dumps({"type": "turn.failed", "error": {"message": "额度用完"}})
    assert parse_exec_output(failed) == ("", "额度用完")


# ---------- list ----------


def test_list_filters_and_maps(home):
    ad, runner, _ = _adapter(home)
    sessions = ad.list(10)
    ids = [s.id for s in sessions]
    assert T_SUB not in ids and T_ARCH not in ids  # 子 agent、归档的不列
    assert T_KIRO in ids  # 不按模型提供方过滤
    assert ids == [T_APP, T_CLI, T_EXEC, T_KIRO]  # 按最近活动
    by = {s.id: s for s in sessions}
    assert by[T_APP].surface == "app" and by[T_CLI].surface == "cli" and by[T_EXEC].surface == "cli"
    assert by[T_CLI].title == "我改过的名字"  # name 优先于 title
    assert by[T_APP].updated_at == pytest.approx(1793779042.0)
    assert by[T_APP].owner == "none" and by[T_APP].can_send and not by[T_APP].can_stop
    assert by[T_APP].extra["model"] == "gpt-5.5"
    assert runner.calls == []  # 没有锁文件 ⇒ lsof / ps 都不用起
    assert ad.list(2) == sessions[:2]


def test_list_tolerates_missing_columns(tmp_path):
    h = tmp_path / "old"
    h.mkdir()
    _make_db(str(h), [{"id": T_CLI, "source": "cli", "cwd": "/x", "updated_at": 1793600945},
                      {"id": T_SUB, "source": '{"subagent":{}}', "cwd": "/x", "updated_at": 1793700000}],
             columns=["id", "source", "cwd", "updated_at"])
    ad, _, _ = _adapter(h)
    sessions = ad.list(5)
    assert [s.id for s in sessions] == [T_CLI]
    assert sessions[0].title == "（没有标题）"
    assert ad.last_error == ""


def test_list_without_db_or_broken_db(tmp_path):
    ad, _, _ = _adapter(tmp_path)
    assert ad.list(5) == [] and "没找到" in ad.last_error
    (tmp_path / "state_5.sqlite").write_bytes(b"not a database at all" * 100)
    assert ad.list(5) == [] and ad.last_error
    h2 = tmp_path / "noth"
    h2.mkdir()
    sqlite3.connect(str(h2 / "state_5.sqlite")).execute("create table other (x)").connection.close()
    ad2, _, _ = _adapter(h2)
    assert ad2.list(5) == [] and ad2.last_error


def test_list_does_not_write_db(home):
    db = os.path.join(str(home), "state_5.sqlite")
    before = (os.path.getmtime(db), os.path.getsize(db))
    ad, _, _ = _adapter(home)
    ad.list(10)
    ad.read(T_APP)
    assert (os.path.getmtime(db), os.path.getsize(db)) == before
    assert not os.path.exists(db + "-wal")


# ---------- running ----------


def test_running_app_owner_and_busy(home):
    lock = _lock(str(home), T_APP)
    _rollout(str(home), T_APP, seg=SEG, records=[_ev("task_started"), _ev("task_complete"), _ev("task_started")])
    runner = FakeRunner(holders={lock: [22059]},
                        ps_rows=[(21927, 1, "S", "/Applications/OpenAI Codex.app/Contents/MacOS/ChatGPT"),
                                 (22059, 21927, "S", APP_ARGS)])
    ad, _, _ = _adapter(home, runner)
    live = ad.running()
    assert len(live) == 1
    s = live[0]
    assert (s.id, s.owner, s.status, s.pids, s.surface) == (T_APP, "app", "running", (22059,), "app")
    assert not s.can_send and s.why_not_send == WHY_NOT_SEND_APP
    # 🔴 2026-11-05 用户报「点了几下没建群」：App 开着的会话**不能 send、但能 relay**
    # （`codex queue`，App 6~8 秒取走）。can_relay 没填 ⇒ 详情卡上没有 [接着聊] ⇒ 建群那步根本点不到。
    assert s.can_relay, "App 开着的会话要能接着聊（走 codex queue），不然详情卡上没有那个按钮"
    assert not s.can_stop and "App" in s.why_not_stop
    assert len(runner.lsof_calls()) == 1  # 一次 lsof
    assert runner.lsof_calls()[0]["timeout"] > 0


def test_idle_unowned_thread_sends_but_does_not_relay(home):
    """没人占着 ⇒ 走 send（exec resume），不标 can_relay —— 那是给「别人开着」的那条路。"""
    _rollout(str(home), T_APP, seg=SEG, records=[_ev("task_complete")])
    ad, _, _ = _adapter(home, FakeRunner())
    s = next(x for x in ad.list() if x.id == T_APP)
    assert s.owner == "none"
    assert s.can_send and not s.can_relay


def test_running_terminal_owner_idle_and_list_puts_live_first(home):
    lock = _lock(str(home), T_EXEC)
    _rollout(str(home), T_EXEC, stamp="2026-11-01T19-25-10", day="2026/11/01",
             records=[_ev("task_started"), _ev("task_complete")])
    runner = FakeRunner(holders={lock: [700]},
                        ps_rows=[(699, 1, "S", "node /opt/homebrew/bin/codex"),
                                 (700, 699, "S", "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex")])
    ad, _, _ = _adapter(home, runner)
    s = ad.running()[0]
    assert (s.owner, s.status) == ("terminal", "idle")
    assert s.why_not_send == WHY_NOT_SEND_TERMINAL
    assert "关掉整个会话" in s.why_not_stop
    assert ad.list(2)[0].id == T_EXEC  # 在跑的排最前


def test_running_ctrl_z_is_stopped(home):
    lock = _lock(str(home), T_CLI)
    runner = FakeRunner(holders={lock: [700]}, ps_rows=[(700, 1, "T", "codex")])
    ad, _, _ = _adapter(home, runner)
    assert ad.running()[0].status == "stopped"


def test_stale_lock_file_without_holder_is_free(home):
    _lock(str(home), T_CLI)
    ad, runner, _ = _adapter(home)
    assert ad.running() == []
    assert len(runner.lsof_calls()) == 1
    assert [c for c in runner.calls if c["argv"][0] == "ps"] == []


def test_lsof_failure_marks_unknown_not_free(home):
    _lock(str(home), T_CLI)
    runner = FakeRunner(lsof_result=RunResult(-1, timed_out=True))
    ad, _, _ = _adapter(home, runner)
    live = ad.running()
    assert [(s.id, s.owner, s.status) for s in live] == [(T_CLI, "unknown", "unknown")]
    assert not live[0].can_send and live[0].why_not_send == WHY_NOT_SEND_UNKNOWN
    assert "超时" in ad.last_error


def test_subagent_lock_not_listed_in_running(home):
    lock = _lock(str(home), T_SUB)
    runner = FakeRunner(holders={lock: [22059]}, ps_rows=[(22059, 1, "S", APP_ARGS)])
    ad, _, _ = _adapter(home, runner)
    assert ad.running() == []


# ---------- read ----------


def test_read_keeps_user_assistant_text_only(home):
    _rollout(str(home), T_APP, seg=SEG, records=[
        {"type": "session_meta", "payload": {"id": T_APP}},
        _msg("user", "<environment_context>\n  <cwd>/x</cwd>"),
        _msg("developer", "<codex_apps_client_time_context>"),
        _msg("user", "# AGENTS.md instructions for /x"),
        _msg("user", "重置下所有的cursor key"),
        {"type": "response_item", "payload": {"type": "reasoning", "summary": [{"text": "想想"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "arguments": "{}"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "ok"}},
        _msg("assistant", "我按流程执行"),
        _msg("assistant", "已完成所有 Cursor key 的余额重置"),
        _ev("task_complete"),
    ])
    ad, _, _ = _adapter(home)
    msgs = ad.read(T_APP, n=3)
    assert [(m.role, m.text) for m in msgs] == [
        ("user", "重置下所有的cursor key"),
        ("assistant", "我按流程执行"),
        ("assistant", "已完成所有 Cursor key 的余额重置"),
    ]
    assert msgs[0].at > 0
    assert len(ad.read(T_APP, n=50)) == 3


def test_read_spans_segments_and_drops_partial_first_line(home):
    old = _rollout(str(home), T_APP, stamp="2026-11-03T10-00-00", seg="01a1a000-0000-7000-8000-00000000000a",
                   day="2026/11/03", records=[_msg("user", "旧分段的问题"), _msg("assistant", "旧分段的回答")])
    new = _rollout(str(home), T_APP, seg=SEG, pad=1_200_000, records=[_msg("user", "新问题"), _msg("assistant", "新回答")])
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    os.utime(new, None)
    ad, _, _ = _adapter(home)
    texts = [m.text for m in ad.read(T_APP, n=4)]
    assert texts == ["旧分段的问题", "旧分段的回答", "新问题", "新回答"]


def test_read_ignores_other_threads_and_bad_ids(home):
    _rollout(str(home), T_CLI, records=[_msg("user", "别人的")])
    # 分段 id 恰好是 T_APP 的文件不算 T_APP 的
    _rollout(str(home), T_CLI, seg=T_APP, records=[_msg("user", "也是别人的")])
    ad, _, _ = _adapter(home)
    assert ad.read(T_APP) == [] and ad.last_error
    assert ad.read("../../etc/passwd") == []
    assert ad.read("*") == []


def test_read_rollout_path_outside_home_is_ignored(home, tmp_path):
    outside = tmp_path / "evil.jsonl"
    outside.write_text(json.dumps(_msg("user", "不该读到")) + "\n")
    conn = sqlite3.connect(str(home / "state_5.sqlite"))
    conn.execute("update threads set rollout_path=? where id=?", (str(outside), T_CLI))
    conn.commit()
    conn.close()
    ad, _, _ = _adapter(home)
    assert ad.read(T_CLI) == []


# ---------- send ----------


def _ok_output(text="PONG"):
    return RunResult(0, stdout="\n".join([
        json.dumps({"type": "thread.started", "thread_id": T_CLI}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}),
        json.dumps({"type": "turn.completed"}),
    ]))


def test_send_free_thread(home):
    runner = FakeRunner(codex_result=_ok_output("PONG"))
    seen = {}
    ad, _, _ = _adapter(home, runner)

    def during():
        seen["owned"] = ad.jobs.owns(4242)
        seen["refuse"] = ad.send(T_CLI, "第二条")  # 同一会话并发 ⇒ 拒

    runner.codex_hook = during
    res = ad.send(T_CLI, "说 PONG")
    assert res.ok and res.text == "PONG" and res.session_id == T_CLI
    calls = runner.codex_calls()
    assert len(calls) == 1
    c = calls[0]
    assert c["argv"] == ["/fake/bin/codex", "exec", "resume", "--json", "--skip-git-repo-check", "--", T_CLI, "说 PONG"]
    assert c["input_text"] is None  # stdin=DEVNULL
    assert c["cwd"] == os.path.join(os.path.dirname(str(home)), "proj")
    assert c["timeout"] > 0
    assert c["env"]["CODEX_HOME"] == str(home)
    assert "queue" not in c["argv"]
    assert seen["owned"] is True  # 跑的时候已经登记
    assert not seen["refuse"].ok and seen["refuse"].text == WHY_NOT_SEND_BRIDGE
    assert ad.jobs.jobs() == []  # 跑完就清掉


@pytest.mark.parametrize("owner_rows,why", [
    ([(22059, 1, "S", APP_ARGS)], WHY_NOT_SEND_APP),
    ([(700, 1, "S", "codex")], WHY_NOT_SEND_TERMINAL),
])
def test_send_refused_when_lock_held_and_no_codex_spawned(home, owner_rows, why):
    lock = _lock(str(home), T_CLI)
    runner = FakeRunner(holders={lock: [owner_rows[0][0]]}, ps_rows=owner_rows)
    ad, _, _ = _adapter(home, runner)
    res = ad.send(T_CLI, "hi")
    assert not res.ok and res.text == why
    assert runner.codex_calls() == []


def test_send_refused_when_lock_state_unknown(home):
    _lock(str(home), T_CLI)
    runner = FakeRunner(lsof_result=RunResult(-1, error="没有 lsof"))
    ad, _, _ = _adapter(home, runner)
    res = ad.send(T_CLI, "hi")
    assert not res.ok and res.text == WHY_NOT_SEND_UNKNOWN
    assert runner.codex_calls() == []


def test_send_rechecks_lock_after_list(home):
    ad, runner, _ = _adapter(home)
    assert next(s for s in ad.list(10) if s.id == T_CLI).can_send
    lock = _lock(str(home), T_CLI)  # 列完之后终端打开了它
    runner.holders = {lock: [700]}
    runner.ps_rows = [(700, 1, "S", "codex")]
    res = ad.send(T_CLI, "hi")
    assert not res.ok and runner.codex_calls() == []


@pytest.mark.parametrize("tid,text", [
    (T_ARCH, "hi"), (T_SUB, "hi"), ("not-a-uuid", "hi"), (T_CLI, ""), (T_CLI, "  "), (T_CLI, "-"),
    ("01a1ffff-0000-7000-8000-000000000009", "hi"),
])
def test_send_refuses_bad_targets(home, tid, text):
    ad, runner, _ = _adapter(home)
    res = ad.send(tid, text)
    assert not res.ok and res.text
    assert runner.codex_calls() == []


def test_send_missing_cwd(home):
    conn = sqlite3.connect(str(home / "state_5.sqlite"))
    conn.execute("update threads set cwd='/definitely/not/here' where id=?", (T_CLI,))
    conn.commit()
    conn.close()
    ad, runner, _ = _adapter(home)
    res = ad.send(T_CLI, "hi")
    assert not res.ok and "文件夹" in res.text and runner.codex_calls() == []


def test_send_active_writer_race(home):
    runner = FakeRunner(codex_result=RunResult(1, stderr="Error: thread already has an active writer"))
    ad, _, _ = _adapter(home, runner)
    res = ad.send(T_CLI, "hi")
    assert not res.ok and "刚刚被" in res.text


def test_send_failures(home):
    runner = FakeRunner(codex_result=RunResult(-1, error="No such file"))
    ad, _, _ = _adapter(home, runner)
    assert "没能启动" in ad.send(T_CLI, "hi").text
    runner.codex_result = RunResult(-9, timed_out=True)
    assert "太久" in ad.send(T_CLI, "hi").text
    runner.codex_result = RunResult(1, stdout=json.dumps({"type": "turn.failed", "error": {"message": "额度用完"}}))
    res = ad.send(T_CLI, "hi")
    assert not res.ok and "额度用完" in res.text
    runner.codex_result = RunResult(0, stdout="")
    res = ad.send(T_CLI, "hi")
    assert res.ok and "没有文字回复" in res.text


def test_send_soft_timeout_sends_sigint_to_own_child(home):
    kills = Kills()
    runner = FakeRunner(codex_result=RunResult(130, stderr=""))
    runner.codex_hook = lambda: time.sleep(1.4)
    ad, _, _ = _adapter(home, runner, kills)
    res = ad.send(T_CLI, "hi", timeout=1)
    assert kills.calls == [(4242, signal.SIGINT)]
    assert not res.ok and "停下" in res.text
    assert runner.codex_calls()[0]["timeout"] > 1  # runner 的硬超时留了余量


# ---------- stop ----------


def test_stop_foreign_pid_refused_without_kill(home):
    lock = _lock(str(home), T_CLI)
    kills = Kills()
    runner = FakeRunner(holders={lock: [700]}, ps_rows=[(700, 1, "S", "codex")])
    ad, _, _ = _adapter(home, runner, kills, known=(700, 4242))
    res = ad.stop(T_CLI)
    assert not res.ok and "终端" in res.text
    lock2 = _lock(str(home), T_APP)
    runner.holders[lock2] = [22059]
    runner.ps_rows.append((22059, 1, "S", APP_ARGS))
    res = ad.stop(T_APP)
    assert not res.ok and "App" in res.text
    res = ad.stop(T_EXEC)
    assert not res.ok
    assert kills.calls == []


def test_stop_own_child_during_send(home):
    kills = Kills()
    box = {}
    runner = FakeRunner(codex_result=RunResult(130),
                        ps_rows=[(4242, 1, "S", "node /opt/homebrew/bin/codex exec resume"),
                                 (4243, 4242, "S", "/opt/homebrew/lib/node_modules/@openai/codex/bin/codex")])
    ad, _, _ = _adapter(home, runner, kills)

    def during():
        # 真实情况：持锁的是 node 包装下面的原生子进程 ⇒ 顺着 ppid 认出是我们的
        runner.holders = {_lock(str(home), T_CLI): [4243]}
        live = {s.id: s for s in ad.running()}
        box["sess"] = live.get(T_CLI)
        box["stop"] = ad.stop(T_CLI)

    runner.codex_hook = during
    ad.send(T_CLI, "写一篇长文")
    sess = box["sess"]
    assert sess is not None and sess.owner == "bridge" and sess.can_stop and not sess.can_send
    assert box["stop"].ok
    assert kills.calls == [(4242, signal.SIGINT)]


def test_stop_refuses_reused_pid(home):
    kills = Kills()
    lstarts = {4242: LSTART}
    jobs = FleetJobs(lstart_of=lambda pid: lstarts.get(pid, ""), kill=kills)
    ad = CodexAdapter(home=str(home), jobs=jobs, runner=FakeRunner(), codex_bin="/fake/bin/codex")
    jobs.register_spawned(4242, "codex", T_CLI)
    lstarts[4242] = "Wed Nov 4 08:00:00 2026"  # 退出后 pid 被别人复用
    res = ad.stop(T_CLI)
    assert not res.ok and kills.calls == []


def test_methods_never_raise_on_garbage(tmp_path):
    h = tmp_path / "h"
    (h / "thread-writer-locks").mkdir(parents=True)
    (h / "thread-writer-locks" / "junk.lock").write_text("")
    (h / "sessions" / "2026" / "11" / "04").mkdir(parents=True)
    (h / "sessions" / "2026" / "11" / "04" / f"rollout-2026-11-04T00-00-00-{T_CLI}.jsonl").write_bytes(
        b"\xff\xfe garbage\n{not json}\n[1,2]\n")
    ad, runner, _ = _adapter(h)
    assert ad.list(5) == []
    assert ad.running() == []
    assert ad.read(T_CLI) == []
    assert not ad.send(T_CLI, "hi").ok
    assert not ad.stop(T_CLI).ok
    assert runner.codex_calls() == [] and runner.lsof_calls() == []


def test_default_home_is_real_codex_dir():
    ad = CodexAdapter(runner=FakeRunner())
    assert ad.home == os.path.abspath(os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex"))
    # 默认目录时不改用户的 CODEX_HOME
    assert ad._env("/opt/homebrew/bin/codex").get("CODEX_HOME") == os.environ.get("CODEX_HOME")


# ---------- relay：进会话聊天 ----------
#
# 实测依据写在 codex.py 模块头。这里全部用 tmp_path 下的假 ~/.codex + 假 runner，
# 不起真 codex，**一个真实会话都不碰**。


def _queue_db(home, rows=()):
    """假的 queue_1.sqlite（真 schema，`codex queue` 自己建的那套）。"""
    conn = sqlite3.connect(os.path.join(str(home), "queue_1.sqlite"))
    conn.execute(
        "create table queued_items (id text primary key not null, thread_id text not null,"
        " payload_json text not null, queue_order integer not null,"
        " created_at_ms integer not null, updated_at_ms integer not null)"
    )
    for i, tid in enumerate(rows):
        conn.execute("insert into queued_items values (?,?,?,?,?,?)",
                     (f"row{i}", tid, "{}", i, 1, 1))
    conn.commit()
    conn.close()


def _app_held(home, tid, runner=None):
    """让 tid 看起来被 Codex App 占着（写锁 + App 进程树）。"""
    lock = _lock(str(home), tid)
    runner = runner or FakeRunner()
    runner.holders = {lock: [22059]}
    runner.ps_rows = [(21927, 1, "S", "/Applications/OpenAI Codex.app/Contents/MacOS/ChatGPT"),
                      (22059, 21927, "S", APP_ARGS)]
    return runner


def _append(path, records):
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_scan_relay_records_ignores_the_previous_turn():
    """🔴 回显之前的 assistant / task_complete 属于目标**上一轮**，不能当成这句的答案。"""
    prev = [_msg("assistant", "上一轮的回答"), _ev("task_complete")]
    assert scan_relay_records(prev, "我这句") == (False, "", "")
    full = prev + [_msg("user", "我这句"), _msg("assistant", "这句的回答"), _ev("task_complete")]
    assert scan_relay_records(full, "我这句") == (True, "这句的回答", "complete")
    # 回显到了、还没答完 ⇒ 不算完成
    assert scan_relay_records(prev + [_msg("user", "我这句")], "我这句") == (True, "", "")
    # 取消
    aborted = prev + [_msg("user", "我这句"), _ev("turn_aborted")]
    assert scan_relay_records(aborted, "我这句")[2] == "aborted"
    assert scan_relay_records([], "x") == (False, "", "")
    assert scan_relay_records([{"type": "x"}, "not a dict"], "") == (False, "", "")


def test_relay_queues_when_app_holds_the_thread_and_waits_for_reply(home):
    """App 占着 ⇒ 走 `codex queue`（不是 exec resume），然后盯 rollout 等回复。"""
    seg = _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "老问题"), _msg("assistant", "老回答"),
                                                       _ev("task_complete")])
    runner = _app_held(home, T_APP)

    def after_queue():
        # App 约 10 秒后取走：回显 + 回复 + task_complete 都写进**目标自己的** rollout
        _append(seg, [_msg("user", "手机上这句"), _msg("assistant", "收到，办好了"), _ev("task_complete")])

    runner.queue_hook = after_queue
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "手机上这句", timeout=30, poll=0.01)
    assert res.ok and res.text == "收到，办好了" and res.delivered
    assert runner.exec_calls() == []  # 有人占着，绝不 exec resume
    queued = runner.queue_calls()
    assert len(queued) == 1
    assert queued[0]["argv"] == ["/fake/bin/codex", "queue", f"--thread={T_APP}", "--message=手机上这句"]
    assert queued[0]["input_text"] is None and queued[0]["timeout"] > 0


def test_relay_message_starting_with_dash_is_not_a_flag(home):
    """`--message=` 等号形式：一句以 `-` 开头的话不能被 codex 当成旗标。"""
    seg = _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    runner.queue_hook = lambda: _append(seg, [_msg("user", "--help 是什么意思"),
                                              _msg("assistant", "它是帮助"), _ev("task_complete")])
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "--help 是什么意思", timeout=30, poll=0.01)
    assert res.ok and res.text == "它是帮助"
    argv = runner.queue_calls()[0]["argv"]
    assert argv[-1] == "--message=--help 是什么意思"
    assert "--help" not in argv[:-1]


def test_relay_uses_exec_resume_when_nobody_holds_the_thread(home):
    """没人占着 ⇒ 还是 exec resume（同步拿回复，不用等约 10 秒的轮询）。"""
    runner = FakeRunner(codex_result=_ok_output("PONG"))
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_CLI, "说 PONG", timeout=30, poll=0.01)
    assert res.ok and res.text == "PONG"
    assert len(runner.exec_calls()) == 1 and runner.queue_calls() == []


def test_relay_terminal_owner_also_queues(home):
    """终端 TUI 占着也能排进去（实测 1.5 秒就被取走）。"""
    seg = _rollout(str(home), T_EXEC, records=[_msg("user", "x")])
    lock = _lock(str(home), T_EXEC)
    runner = FakeRunner(holders={lock: [700]}, ps_rows=[(700, 1, "S", "codex")])
    runner.queue_hook = lambda: _append(seg, [_msg("user", "终端那句"), _msg("assistant", "TUI 回了"),
                                              _ev("task_complete")])
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_EXEC, "终端那句", timeout=30, poll=0.01)
    assert res.ok and res.text == "TUI 回了"
    assert len(runner.queue_calls()) == 1 and runner.exec_calls() == []


def test_relay_reads_a_mark_sitting_exactly_on_a_newline(home):
    """🔴 续读的 mark 正好落在换行后 ⇒ 一行都不许丢（丢了就把回复第一条吃掉）。"""
    seg = _rollout(str(home), T_CLI, records=[_msg("user", "第一句")])
    ad, _, _ = _adapter(home)
    marks = ad._tail_marks([seg])
    _append(seg, [_msg("assistant", "紧跟着这条不许丢"), _ev("task_complete")])
    recs = ad._new_records(T_CLI, marks)
    assert [r.get("payload", {}).get("type") for r in recs] == ["message", "task_complete"]
    assert _message_text(recs[0]) == "紧跟着这条不许丢"


def test_relay_half_written_line_is_dropped(home):
    """mark 落在一行中间（上次读到半截）⇒ 那半截丢掉，别当成记录。"""
    seg = _rollout(str(home), T_CLI, records=[_msg("user", "第一句")])
    with open(seg, "a", encoding="utf-8") as fh:
        fh.write('{"type":"response_item","payload":{"type":"mess')  # 半截，没换行
    ad, _, _ = _adapter(home)
    marks = ad._tail_marks([seg])
    with open(seg, "a", encoding="utf-8") as fh:
        fh.write('age"}}\n')  # 半截补完
    _append(seg, [_msg("assistant", "这条要读到")])
    recs = ad._new_records(T_CLI, marks)
    assert [_message_text(r) for r in recs if _message_text(r)] == ["这条要读到"]


def _message_text(rec):
    payload = (rec or {}).get("payload") or {}
    parts = payload.get("content") or []
    return "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))


def test_relay_queued_but_nobody_consumed_says_so_and_stays_delivered(home):
    """排上了、没人取 ⇒ **不能说没发进去**（用户会重发，在真实会话里跑第二遍）。"""
    _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    _queue_db(home, rows=[T_APP])  # 行还躺在队列里
    runner = _app_held(home, T_APP)
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "没人取的那句", timeout=1, poll=0.01, min_timeout=0)
    assert not res.ok
    assert res.delivered is True  # 🔴 关键：已送达
    assert res.text == RELAY_QUEUED_NOBODY_HOME
    assert "排进去了" in res.text and "没人" in res.text
    assert len(runner.queue_calls()) == 1  # 只排一次，绝不自动重发


def test_relay_timeout_does_not_resend(home):
    """幂等：到点没回复，一次也不重发。"""
    _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "慢慢答", timeout=1, poll=0.01, min_timeout=0)
    assert not res.ok and res.delivered
    assert res.text == RELAY_QUEUED_NO_REPLY
    assert len(runner.queue_calls()) == 1
    # 再发一次是用户自己又点的，那才允许排第二条
    ad.relay(T_APP, "慢慢答", timeout=1, poll=0.01, min_timeout=0)
    assert len(runner.queue_calls()) == 2


def test_relay_sends_the_owner_words_with_no_framing_prefix(home):
    """🔴 codex 这条路**不许**加「这是主人发的」前缀 —— 加了就是每句话白多一行废话。

    为什么 Claude 那条路要加、这条不要（2026-11-05 实测）：
    Claude 走的是会话间消息，必然盖上信使的名字（`from-name="carher-relay"`），
    Claude Code 还在外面套一段「not typed by your user」⇒ 目标当成同事闲聊，
    转述一下就不动手了（用户 11-05 报的就是这个）。所以那边要在正文里声明身份
    （`bridge/fleet/relay.py` 的 `OWNER_FRAMING`）。

    codex 不一样：`codex queue` / `codex exec resume` 落进目标 rollout 时就是主人的原话。
    实测 11-05 18:21 桥发的那句，在目标 rollout 里是 `'你好啊'` —— 一个字没多、没有外包装。
    ⇒ 这里不需要前缀。这条测试就是防着以后有人「顺手统一一下」把前缀加到这条路上。
    """
    from bridge.fleet import relay as relay_mod

    _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    ad, _, _ = _adapter(home, runner)
    ad.relay(T_APP, "把测试跑一遍", timeout=1, poll=0.01, min_timeout=0)
    call, = runner.queue_calls()
    argv = call["argv"]
    # 真实形状（实测）：['codex', 'queue', '--thread=…', '--message=把测试跑一遍']
    msg, = [a for a in argv if str(a).startswith("--message=")]
    assert msg == "--message=把测试跑一遍", msg
    assert relay_mod.OWNER_FRAMING.strip() not in msg, "codex 这条路不该带身份声明前缀"


def test_relay_echo_without_text_reply(home):
    """回显到了（真送达）但它没写文字 ⇒ 说实话，仍然 delivered。"""
    seg = _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    runner.queue_hook = lambda: _append(seg, [_msg("user", "办一下"), _ev("task_complete")])
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "办一下", timeout=1, poll=0.01, min_timeout=0)
    assert not res.ok and res.delivered and res.text == RELAY_DELIVERED_NO_TEXT


def test_relay_turn_aborted(home):
    seg = _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    runner.queue_hook = lambda: _append(seg, [_msg("user", "停一下"), _ev("turn_aborted")])
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "停一下", timeout=1, poll=0.01, min_timeout=0)
    assert not res.ok and res.delivered and res.text == RELAY_ABORTED


def test_relay_refuses_thread_without_rollout(home):
    """实测：没有 rollout 的线程 `codex queue` 直接报 -32603 ⇒ 先说清楚，别白跑。"""
    runner = _app_held(home, T_APP)  # 占着，但一条 rollout 都没有
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "hi")
    assert not res.ok and res.text == WHY_RELAY_NO_ROLLOUT and not res.delivered
    assert runner.queue_calls() == [] and runner.exec_calls() == []


def test_relay_maps_upstream_no_rollout_error(home):
    """真撞上了（排的瞬间线程没了）⇒ 也翻成同一句大白话，不甩 -32603。"""
    _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    runner.queue_result = RunResult(1, stderr=(
        "Error: failed to queue session message: thread/queue/add failed: failed to read thread: "
        "invalid thread-store request: no rollout found for thread id x (code -32603)"))
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(T_APP, "hi", timeout=1, poll=0.01, min_timeout=0)
    assert not res.ok and res.text == WHY_RELAY_NO_ROLLOUT
    assert res.delivered is False  # 没排进去 ⇒ 照实说，用户可以放心重发


@pytest.mark.parametrize("tid,text,why", [
    (T_ARCH, "hi", WHY_RELAY_ARCHIVED),
    ("not-a-uuid", "hi", None),
    (T_CLI, "", None),
    (T_CLI, "   ", None),
    (T_CLI, "-", None),
    ("01a1ffff-0000-7000-8000-000000000009", "hi", None),
])
def test_relay_refuses_bad_targets_without_any_subprocess(home, tid, text, why):
    runner = FakeRunner()
    ad, _, _ = _adapter(home, runner)
    res = ad.relay(tid, text)
    assert not res.ok and res.text and not res.delivered
    if why:
        assert res.text == why
    assert runner.codex_calls() == []


def test_relay_refuses_second_message_while_first_is_in_flight(home):
    """同一个会话同一时刻只排一条（幂等的前提）。"""
    seg = _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    seen = {}
    ad, _, _ = _adapter(home, runner)

    def during():
        seen["refuse"] = ad.relay(T_APP, "第二条")
        _append(seg, [_msg("user", "第一条"), _msg("assistant", "好"), _ev("task_complete")])

    runner.queue_hook = during
    res = ad.relay(T_APP, "第一条", timeout=30, poll=0.01)
    assert res.ok
    assert not seen["refuse"].ok and seen["refuse"].text == WHY_RELAY_BUSY
    assert len(runner.queue_calls()) == 1  # 第二条一个子进程都没起


def test_relay_never_writes_the_queue_db(home):
    """只读 `queue_1.sqlite`：relay 全程不许对它执行任何写语句。"""
    _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    _queue_db(home, rows=[T_APP])
    db = os.path.join(str(home), "queue_1.sqlite")
    before = (os.path.getmtime(db), os.path.getsize(db))
    runner = _app_held(home, T_APP)
    ad, _, _ = _adapter(home, runner)
    ad.relay(T_APP, "hi", timeout=1, poll=0.01, min_timeout=0)
    assert (os.path.getmtime(db), os.path.getsize(db)) == before
    assert not os.path.exists(db + "-wal")
    assert ad.pending_queued(T_APP) == 1
    assert ad.pending_queued(T_CLI) == 0
    assert ad.pending_queued("not-a-uuid") is None


def test_pending_queued_without_db_is_none(home):
    ad, _, _ = _adapter(home)
    assert ad.pending_queued(T_APP) is None
    (home / "queue_1.sqlite").write_bytes(b"not a database" * 50)
    assert ad.pending_queued(T_APP) is None


# ---------- relay 的 /stop：停不了的照实说 ----------


def test_relay_stop_cannot_stop_other_peoples_turn(home):
    """App / 终端那一轮**停不了**（从外面发信号会关掉用户整个会话）⇒ 说清楚在哪停，不发信号。"""
    kills = Kills()
    runner = _app_held(home, T_APP)
    ad, _, _ = _adapter(home, runner, kills, known=(22059, 4242))
    res = ad.relay_stop(T_APP)
    assert not res.ok and res.text == WHY_RELAY_STOP_APP
    lock = _lock(str(home), T_EXEC)
    runner.holders[lock] = [700]
    runner.ps_rows.append((700, 1, "S", "codex"))
    res = ad.relay_stop(T_EXEC)
    assert not res.ok and res.text == WHY_RELAY_STOP_TERMINAL
    assert kills.calls == []  # 🔴 一个信号都没发


def test_relay_stop_says_queued_message_cannot_be_unsent(home):
    """排进去的那句撤不回来（Codex 没给口子，我们也不去改它的队列文件）。"""
    _queue_db(home, rows=[T_CLI])
    kills = Kills()
    ad, _, _ = _adapter(home, FakeRunner(), kills)
    res = ad.relay_stop(T_CLI)
    assert not res.ok and res.text == WHY_RELAY_NO_UNSEND
    assert "撤不回来" in res.text and kills.calls == []


def test_relay_stop_bad_id_and_idle(home):
    ad, runner, kills = _adapter(home)
    assert not ad.relay_stop("not-a-uuid").ok
    assert not ad.relay_stop(T_CLI).ok  # 没排队、没人占着 ⇒ 没什么可停的
    assert kills.calls == [] and runner.codex_calls() == []


def test_relay_stop_can_stop_our_own_exec_resume(home):
    """桥自己起的那条 exec resume 还是能停的。"""
    kills = Kills()
    runner = FakeRunner(codex_result=RunResult(130))
    ad, _, _ = _adapter(home, runner, kills)
    box = {}
    runner.codex_hook = lambda: box.setdefault("stop", ad.relay_stop(T_CLI))
    ad.relay(T_CLI, "写一篇长文")
    assert box["stop"].ok and kills.calls == [(4242, signal.SIGINT)]


# ---------- 两条故意不做的路：有话术，没有尝试 ----------


def test_ipc_write_methods_are_never_attempted():
    """🔴 `ipc/ipc.sock` 的写方法一次都不执行，源码里也不许出现它们。"""
    import inspect

    from bridge.fleet import codex as mod

    src = inspect.getsource(mod)
    for banned in ("thread-follower-start-turn", "steer-turn", "interrupt-turn"):
        # 只许出现在「我们不做这个」的说明里，不许作为被发出去的字符串
        assert f'"{banned}"' not in src and f"'{banned}'" not in src
    assert "ipc.sock" not in src.replace("`ipc/ipc.sock`", "")
    assert WHY_NO_IPC_WRITE and "没公开" in WHY_NO_IPC_WRITE


def test_wake_deeplink_is_never_opened():
    """深链会让 App 抢住线程不放（实测删都删不掉）⇒ 不用它，也不许 shell out 到 open。"""
    import inspect

    from bridge.fleet import codex as mod

    src = inspect.getsource(mod)
    assert "codex://" not in src.replace("`codex://threads/wake/<id>`", "")
    assert "threads/wake" not in src.replace("`codex://threads/wake/<id>`", "")
    assert WHY_NO_WAKE_DEEPLINK and "深链" in WHY_NO_WAKE_DEEPLINK


def test_relay_only_runs_codex_queue_and_exec_never_anything_else(home):
    """relay 起的子进程只有 `codex queue` / `codex exec resume`，没有别的花活。"""
    seg = _rollout(str(home), T_APP, seg=SEG, records=[_msg("user", "x")])
    runner = _app_held(home, T_APP)
    runner.queue_hook = lambda: _append(seg, [_msg("user", "q"), _msg("assistant", "a"), _ev("task_complete")])
    ad, _, _ = _adapter(home, runner)
    ad.relay(T_APP, "q", timeout=30, poll=0.01)
    ad.relay(T_CLI, "说 PONG", timeout=30, poll=0.01)
    for call in runner.codex_calls():
        assert call["argv"][1] in ("queue", "exec"), call["argv"]
