"""Cursor adapter。

全部基于 tmp_path 下的假文件 + 可注入的 runner / 进程表 / kill，**不起真 cursor-agent，不碰真实会话**。
重点是三条安全拒绝：
- 电脑上有进程开着这个对话（或认不出开的是哪个）⇒ send 拒绝，且**一个子进程都没起**
- 编辑器里的对话 ⇒ send 拒绝（Cursor 不让外部程序往里写）
- 不是我们起的进程 ⇒ stop 拒绝，且**没调用 kill**
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sqlite3
import sys
import threading

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import FleetJobs, ProcTable, RunResult, SubprocessRunner  # noqa: E402
from bridge.fleet import cursor as cur  # noqa: E402
from bridge.fleet.cursor import CursorAdapter  # noqa: E402

CHAT_A = "11111111-aaaa-4aaa-8aaa-000000000001"
CHAT_B = "22222222-bbbb-4bbb-8bbb-000000000002"
CHAT_EMPTY = "33333333-cccc-4ccc-8ccc-000000000003"
IDE_1 = "44444444-dddd-4ddd-8ddd-000000000004"
IDE_2 = "55555555-eeee-4eee-8eee-000000000005"

AGENT = "/Users/x/.local/share/cursor-agent/versions/2026.10.22-27dcf5f/index.js"
NODE = "/Users/x/.local/share/cursor-agent/versions/2026.10.22-27dcf5f/node --use-system-ca"

SPAWN_LSTART = "Wed Nov  4 10:00:00 2026"
WORKER_LSTART = "Wed Nov  4 10:00:02 2026"
OLD_LSTART = "Mon Nov  2 08:00:00 2026"


# ---------- 假环境 ----------


def _user(text: str, ts: str = "Sunday, Nov 1, 2026, 7:27 PM (UTC+8)") -> dict:
    return {"role": "user", "message": {"content": [
        {"type": "text", "text": f"<timestamp>{ts}</timestamp>\n<user_query>\n{text}\n</user_query>"}
    ]}}


def _assistant(text: str = "", tools: int = 0) -> dict:
    content = [{"type": "text", "text": text}] if text else []
    content += [{"type": "tool_use", "name": "Shell", "input": {"command": "ls"}}] * tools
    return {"role": "assistant", "message": {"content": content}}


TURN_ENDED = {"type": "turn_ended", "status": "success"}


def write_transcript(home, project_slug: str, chat_id: str, records) -> str:
    folder = home / "projects" / project_slug / "agent-transcripts" / chat_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{chat_id}.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
    return str(path)


def write_chat(home, chat_id: str, cwd: str, updated_ms: int, has_conv: bool = True) -> None:
    folder = home / "chats" / hashlib.md5(cwd.encode()).hexdigest() / chat_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "meta.json").write_text(json.dumps({
        "schemaVersion": 1, "createdAtMs": updated_ms - 1000, "updatedAtMs": updated_ms,
        "hasConversation": has_conv, "cwd": cwd,
    }), encoding="utf-8")
    # store.db 里有 blobEncryptionKey：放一个让测试证明我们从不打开它
    (folder / "store.db").write_bytes(b"SECRET-blobEncryptionKey")


def make_state_db(app_user, rows, *, with_table: bool = True) -> str:
    folder = app_user / "globalStorage"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "state.vscdb"
    conn = sqlite3.connect(str(path))
    conn.execute("create table cursorDiskKV (key text unique on conflict replace, value blob)")
    conn.execute("insert into cursorDiskKV values ('composerData:x', 'big')")
    if with_table:
        conn.execute(
            "create table composerHeaders (composerId text primary key, workspaceId text, createdAt integer,"
            " lastUpdatedAt integer, isArchived integer, isSubagent integer, recency integer,"
            " checkpointAt integer, value text, subagentTypeName text)"
        )
        for r in rows:
            conn.execute(
                "insert into composerHeaders (composerId, workspaceId, createdAt, lastUpdatedAt, isArchived,"
                " isSubagent, value) values (?,?,?,?,?,?,?)",
                (r["id"], r.get("ws", ""), r["ts"] - 5, r["ts"], r.get("archived", 0), r.get("sub", 0),
                 json.dumps(r.get("value", {}))),
            )
    conn.commit()
    conn.close()
    return str(path)


def ps_line(pid: int, ppid: int, lstart: str, args: str, stat: str = "S") -> str:
    return f"{pid:>6} {ppid:>6} {stat:<4} {lstart}    00:10 {args}"


class FakeRunner(SubprocessRunner):
    """记下每一次调用；cursor-agent 调用走 on_spawn 并返回预设结果；lsof 返回预设 cwd。"""

    def __init__(self, *, reply=None, lsof=None, spawn_pid: int = 4242, block: threading.Event = None):
        self.calls = []
        self.reply = reply if reply is not None else RunResult(
            0, stdout=json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "好的，改完了"})
        )
        self.lsof = lsof or {}
        self.lsof_fail = False
        self.spawn_pid = spawn_pid
        self.block = block
        self.started = threading.Event()

    def run(self, argv, *, timeout, cwd=None, env=None, input_text=None, on_spawn=None):
        argv = list(argv)
        self.calls.append({"argv": argv, "timeout": timeout, "cwd": cwd, "input_text": input_text})
        if os.path.basename(argv[0]) == "lsof":
            if self.lsof_fail:
                return RunResult(-1, error="没有 lsof")
            pids = [int(p) for p in argv[argv.index("-p") + 1].split(",")]
            out = "".join(f"p{p}\nfcwd\nn{self.lsof[p]}\n" for p in pids if p in self.lsof)
            return RunResult(0, stdout=out)
        if on_spawn is not None:
            on_spawn(self.spawn_pid)
        self.started.set()
        if self.block is not None:
            self.block.wait(5)
        return self.reply

    def agent_calls(self):
        return [c for c in self.calls if os.path.basename(c["argv"][0]) != "lsof"]


class FakeKill:
    def __init__(self):
        self.calls = []

    def __call__(self, pid, sig):
        self.calls.append((pid, sig))


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "cursor_home"
    app_user = tmp_path / "app_user"
    project = tmp_path / "proj"
    project.mkdir()
    (home / "chats").mkdir(parents=True)
    (home / "projects").mkdir(parents=True)
    app_user.mkdir()
    binary = tmp_path / "cursor-agent"
    binary.write_text("#!/bin/sh\nexit 99\n")  # 存在就行，测试绝不会真跑它

    state = {"ps": "", "ps_error": "", "lstarts": {}}
    kill = FakeKill()

    def lstart_of(pid):
        return state["lstarts"].get(pid, "")

    jobs = FleetJobs(lstart_of=lstart_of, kill=kill)
    runner = FakeRunner()

    def procs():
        return ProcTable.parse(state["ps"], error=state["ps_error"])

    def make(**kw):
        return CursorAdapter(
            str(home), str(app_user), jobs=kw.pop("jobs", jobs), runner=kw.pop("runner", runner),
            agent_bin=kw.pop("agent_bin", str(binary)), procs=kw.pop("procs", procs), **kw,
        )

    class Env:
        pass

    e = Env()
    e.home, e.app_user, e.project, e.binary = home, app_user, project, binary
    e.state, e.kill, e.jobs, e.runner, e.make = state, kill, jobs, runner, make
    return e


def seed_cli(e):
    """两个聊过的命令行对话 + 一个没聊过的。"""
    cwd = str(e.project)
    write_chat(e.home, CHAT_A, cwd, 1_793_000_000_000)
    write_chat(e.home, CHAT_B, cwd, 1_793_100_000_000)
    write_chat(e.home, CHAT_EMPTY, cwd, 1_793_200_000_000, has_conv=False)
    write_transcript(e.home, "tmp-proj", CHAT_A, [_user("修一下登录页的样式"), _assistant("好", 2), TURN_ENDED])
    # 目录名故意和 cwd 对不上：必须按 id 找，不能自己算
    write_transcript(e.home, "whatever-slug", CHAT_B, [_user("把 README 翻成英文"), _assistant("翻好了"), TURN_ENDED])


# ---------- list ----------


def test_list_cli_only_with_conversation_sorted_and_titled(env):
    seed_cli(env)
    sessions = env.make().list(10)
    assert [s.id for s in sessions] == [CHAT_B, CHAT_A]  # 没聊过的不列；新的在前
    b = sessions[0]
    assert b.tool == "cursor" and b.surface == "cli"
    assert b.title == "把 README 翻成英文"  # 名字都是 New Agent ⇒ 取第一句用户话
    assert b.cwd == str(env.project)
    assert b.updated_at == pytest.approx(1_793_100_000)
    assert b.status == "idle" and b.owner == "none"
    assert b.can_send and not b.can_stop and b.why_not_stop


def test_list_never_opens_store_db(env, monkeypatch):
    seed_cli(env)
    opened = []
    real_open = open

    def spy(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    monkeypatch.setattr("builtins.open", spy)
    real_connect = sqlite3.connect
    monkeypatch.setattr(cur.sqlite3, "connect", lambda p, *a, **kw: opened.append(str(p)) or real_connect(p, *a, **kw))
    env.make().list(10)
    assert not [p for p in opened if "store.db" in p]


def test_list_ide_from_composer_headers(env):
    ws_dir = env.app_user / "workspaceStorage" / "wsid0001"
    ws_dir.mkdir(parents=True)
    (ws_dir / "workspace.json").write_text(json.dumps({"folder": "file:///Users/x/My%20Proj"}))
    make_state_db(env.app_user, [
        {"id": IDE_1, "ts": 1_793_500_000_000, "value": {
            "name": "Chick eating rice video", "unifiedMode": "agent",
            "workspaceIdentifier": {"id": "c5", "uri": {"fsPath": "/Users/x/CarHer", "scheme": "file"}}}},
        {"id": IDE_2, "ts": 1_793_400_000_000, "ws": "wsid0001", "value": {"name": ""}},
        {"id": "empty-123", "ts": 1_793_900_000_000, "value": {"name": "x"}},
        {"id": "66666666-ffff-4fff-8fff-000000000006", "ts": 1_793_900_000_000, "sub": 1, "value": {"name": "sub"}},
        {"id": "77777777-ffff-4fff-8fff-000000000007", "ts": 1_793_800_000_000,
         "value": {"isDraft": True}},  # 只开了个空输入框
    ])
    write_transcript(env.home, "Users-x-My-Proj", IDE_2, [_user("没有名字时用这句当标题"), _assistant("嗯")])
    sessions = env.make().list(10)
    assert [s.id for s in sessions] == [IDE_1, IDE_2]
    one, two = sessions
    assert one.surface == "app" and one.owner == "app" and one.status == "unknown"
    assert one.title == "Chick eating rice video" and one.cwd == "/Users/x/CarHer"
    # 🔴 第 17 轮换过一次话术：旧的那句「Cursor 不允许外部程序」**是假的**（官方钩子确实能往
    # 已有对话追加一句，拦路的是时机不是口子，见 `docs/iteration/r17-cursor.md`）。
    # 所以这里不钉整句，只断行为 + 语义关键词。
    assert one.can_send is False and one.why_not_send
    assert "送不进去" in one.why_not_send  # 说清做不到
    assert "Cursor" in one.why_not_send  # 指路回电脑上
    assert not one.can_stop and one.why_not_stop
    assert one.extra["mode"] == "agent"
    assert two.title == "没有名字时用这句当标题"
    assert two.cwd == "/Users/x/My Proj"


def test_list_merges_cli_and_ide_by_time(env):
    seed_cli(env)
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_050_000_000, "value": {"name": "中间那个"}}])
    ids = [s.id for s in env.make().list(10)]
    assert ids == [CHAT_B, IDE_1, CHAT_A]
    assert [s.id for s in env.make().list(2)] == [CHAT_B, IDE_1]


def test_list_tolerates_missing_table_db_and_dirs(env, tmp_path):
    make_state_db(env.app_user, [], with_table=False)
    assert env.make().list(10) == []
    nothing = CursorAdapter(str(tmp_path / "nope"), str(tmp_path / "nope2"), jobs=env.jobs, runner=env.runner,
                            procs=lambda: ProcTable())
    assert nothing.list(10) == [] and nothing.running() == [] and nothing.read(CHAT_A) == []


def test_list_tolerates_corrupt_db_and_meta(env):
    seed_cli(env)
    (env.app_user / "globalStorage").mkdir(parents=True)
    (env.app_user / "globalStorage" / "state.vscdb").write_bytes(b"not a database at all" * 100)
    bad = env.home / "chats" / "abc" / "88888888-0000-4000-8000-000000000008"
    bad.mkdir(parents=True)
    (bad / "meta.json").write_text("{half")
    adapter = env.make()
    assert [s.id for s in adapter.list(10)] == [CHAT_B, CHAT_A]
    assert adapter.last_error  # 说明一句，不抛


def test_list_skips_weird_dir_names(env):
    seed_cli(env)
    weird = env.home / "chats" / "abc" / "..evil"
    weird.mkdir(parents=True)
    (weird / "meta.json").write_text(json.dumps({"hasConversation": True, "cwd": "/", "updatedAtMs": 9e12}))
    assert "..evil" not in [s.id for s in env.make().list(10)]


def test_list_bad_ide_value_json_skipped_not_raised(env):
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "ok"}}])
    conn = sqlite3.connect(str(env.app_user / "globalStorage" / "state.vscdb"))
    conn.execute("insert into composerHeaders (composerId, lastUpdatedAt, isSubagent, value) values (?,?,0,?)",
                 (IDE_2, 1_793_600_000_000, "{not json"))
    conn.commit()
    conn.close()
    sessions = env.make().list(10)
    assert IDE_1 in [s.id for s in sessions]


# ---------- read ----------


def test_read_strips_wrappers_and_skips_tools(env):
    seed_cli(env)
    msgs = env.make().read(CHAT_A, n=10)
    assert [(m.role, m.text) for m in msgs] == [("user", "修一下登录页的样式"), ("assistant", "好")]
    assert "<user_query>" not in msgs[0].text and "<timestamp>" not in msgs[0].text
    # 2026-11-01 19:27 UTC+8 = 11:27 UTC
    assert msgs[0].at == pytest.approx(1_793_532_420)


def test_read_last_n_and_found_by_id_not_by_path(env):
    seed_cli(env)
    records = []
    for i in range(10):
        records += [_user(f"第 {i} 句"), _assistant(f"回 {i}"), TURN_ENDED]
    write_transcript(env.home, "some-other-slug", CHAT_B, records)
    msgs = env.make().read(CHAT_B, n=3)
    assert [m.text for m in msgs] == ["回 8", "第 9 句", "回 9"]


def test_read_skips_injected_and_summarizes_compaction(env):
    write_transcript(env.home, "p", CHAT_A, [
        {"role": "user", "message": {"content": [{"type": "text", "text": "<mcp_server_catalog>\nlots</mcp_server_catalog>"}]}},
        {"role": "user", "message": {"content": [{"type": "text", "text": "This session is being continued from a previous conversation that ran out of context. Summary: ..."}]}},
        {"role": "user", "message": {"content": [{"type": "text", "text": "直接打的字，没有包装"}]}},
        "not a dict", {"role": "system", "message": {}}, {"role": "assistant", "message": {"content": "纯字符串"}},
    ])
    with open(env.home / "projects" / "p" / "agent-transcripts" / CHAT_A / f"{CHAT_A}.jsonl", "a") as fh:
        fh.write("{broken json\n")
    msgs = env.make().read(CHAT_A, n=10)
    assert [m.text for m in msgs] == [cur.SUMMARY_NOTE, "直接打的字，没有包装", "纯字符串"]


def test_read_skips_background_task_nudge(env):
    write_transcript(env.home, "p", CHAT_A, [
        _user("生成一个视频"), _assistant("在做了"),
        {"role": "user", "message": {"content": [{"type": "text", "text":
            "<timestamp>x</timestamp>\n\n<user_query>Briefly inform the user about the task result and "
            "perform any follow-up actions (if needed).</user_query>"}]}},
        _assistant("视频好了"),
    ])
    msgs = env.make().read(CHAT_A, n=10)
    assert [m.text for m in msgs] == ["生成一个视频", "在做了", "视频好了"]


def test_read_tail_drops_partial_first_line(env, monkeypatch):
    records = [_user("x" * 300)] + [_assistant(f"回 {i}") for i in range(5)]
    write_transcript(env.home, "p", CHAT_A, records)
    monkeypatch.setattr(cur, "READ_TAIL_BYTES", 200)
    msgs = env.make().read(CHAT_A, n=10)
    assert msgs and all(m.text.startswith("回") for m in msgs)


def test_read_picks_newest_when_id_in_two_projects(env):
    old = write_transcript(env.home, "tmp-proj", CHAT_A, [_assistant("旧的")])
    os.utime(old, (1, 1))
    write_transcript(env.home, "private-tmp-proj", CHAT_A, [_assistant("新的")])
    assert [m.text for m in env.make().read(CHAT_A)] == ["新的"]


def test_read_rejects_bad_ids(env):
    write_transcript(env.home, "p", CHAT_A, [_assistant("x")])
    adapter = env.make()
    for bad in ("", "../etc", "a/b", "*", None, "-rf"):
        assert adapter.read(bad) == []


def test_title_from_head_only(env):
    seed_cli(env)
    s = {x.id: x for x in env.make().list(10)}
    assert s[CHAT_A].title == "修一下登录页的样式"


# ---------- running ----------


def test_running_foreign_with_id_is_terminal_unknown(env):
    seed_cli(env)
    env.state["ps"] = "\n".join([
        ps_line(900, 800, OLD_LSTART, f"{NODE} {AGENT} --resume {CHAT_A}"),
        ps_line(901, 1, OLD_LSTART, f"{NODE} {AGENT} worker-server"),  # worker 不是对话
        ps_line(902, 1, OLD_LSTART, f"{NODE} {AGENT} create-chat"),
        ps_line(903, 1, OLD_LSTART, "/usr/bin/vim notes.txt"),
    ])
    sessions = env.make().running()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == CHAT_A and s.owner == "terminal" and s.status == "unknown"
    assert s.pids == (900,) and s.title == "修一下登录页的样式"
    assert not s.can_send and "cursor-agent" in s.why_not_send
    assert not s.can_stop


def test_list_puts_open_sessions_first(env):
    seed_cli(env)
    env.state["ps"] = ps_line(900, 800, OLD_LSTART, f"{NODE} {AGENT} --resume {CHAT_A}")
    assert [s.id for s in env.make().list(10)] == [CHAT_A, CHAT_B]


def test_running_foreign_without_id_listed_as_unmatched(env):
    env.runner.lsof = {905: "/Users/x/somewhere"}
    env.state["ps"] = ps_line(905, 800, OLD_LSTART, f"{NODE} {AGENT}")
    sessions = env.make().running()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.id == "pid-905" and s.status == "unknown" and s.extra.get("unmatched")
    assert s.cwd == "/Users/x/somewhere" and "somewhere" in s.title
    assert not s.can_send and not s.can_stop


def test_running_ours_only_from_fleet_jobs(env):
    seed_cli(env)
    env.state["ps"] = ps_line(4242, 100, SPAWN_LSTART, f"{NODE} {AGENT} --resume {CHAT_B} -p hi")
    env.state["lstarts"][4242] = SPAWN_LSTART
    env.jobs.register(4242, SPAWN_LSTART, "cursor", CHAT_B)
    sessions = env.make().running()
    assert [(s.id, s.owner, s.status, s.can_stop) for s in sessions] == [(CHAT_B, "bridge", "running", True)]
    # pid 被复用（lstart 变了）⇒ 不再算我们的，变成外来的
    env.state["ps"] = ps_line(4242, 100, OLD_LSTART, f"{NODE} {AGENT} --resume {CHAT_B} -p hi")
    s = env.make().running()[0]
    assert s.owner == "terminal" and not s.can_stop


def test_running_ps_failure_returns_empty_with_note(env):
    env.state["ps_error"] = "读进程列表超时了"
    adapter = env.make()
    assert adapter.running() == []
    assert "超时" in adapter.last_error


def test_running_ide_is_never_listed(env):
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "x"}}])
    assert env.make().running() == []
    assert "看不出来" in CursorAdapter.RUNNING_NOTE


def test_parse_agent_args_shapes():
    p = cur.parse_agent_args(1, f"{NODE} {AGENT} --resume {CHAT_A} -p --workspace /tmp/x hello there")
    assert p.chat_id == CHAT_A and p.printing and p.workspace == "/tmp/x"
    assert cur.parse_agent_args(1, f"{NODE} {AGENT} --resume={CHAT_A}").chat_id == CHAT_A
    assert cur.parse_agent_args(1, f"{NODE} {AGENT} -p -- --resume {CHAT_A}").chat_id == ""
    assert cur.parse_agent_args(1, f"{NODE} {AGENT} worker-server") is None
    assert cur.parse_agent_args(1, f"{NODE} {AGENT} --list-models") is None
    assert cur.parse_agent_args(1, "/usr/bin/python index.js") is None
    assert cur.is_worker_server(f"{NODE} {AGENT} worker-server")


# ---------- send：安全拒绝（必测：一个子进程都不许起） ----------


def test_send_refused_when_open_in_terminal_and_spawns_nothing(env):
    seed_cli(env)
    env.state["ps"] = ps_line(900, 800, OLD_LSTART, f"{NODE} {AGENT} --resume {CHAT_A}")
    res = env.make().send(CHAT_A, "你好")
    assert not res.ok and "cursor-agent" in res.text
    assert env.runner.agent_calls() == []


def test_send_refused_when_unidentified_agent_in_same_dir(env):
    seed_cli(env)
    env.runner.lsof = {905: str(env.project)}
    env.state["ps"] = ps_line(905, 800, OLD_LSTART, f"{NODE} {AGENT}")
    res = env.make().send(CHAT_A, "你好")
    assert not res.ok and "认不出" in res.text
    assert env.runner.agent_calls() == []


def test_send_refused_when_unidentified_agent_cwd_unreadable(env):
    seed_cli(env)
    env.runner.lsof_fail = True
    env.state["ps"] = ps_line(905, 800, OLD_LSTART, f"{NODE} {AGENT}")
    assert not env.make().send(CHAT_A, "你好").ok
    assert env.runner.agent_calls() == []


def test_send_allowed_when_unidentified_agent_elsewhere(env):
    seed_cli(env)
    env.runner.lsof = {905: "/somewhere/else"}
    env.state["ps"] = ps_line(905, 800, OLD_LSTART, f"{NODE} {AGENT}")
    assert env.make().send(CHAT_A, "你好").ok
    assert len(env.runner.agent_calls()) == 1


def test_send_refused_when_ps_unreadable(env):
    seed_cli(env)
    env.state["ps_error"] = "读不到进程列表"
    res = env.make().send(CHAT_A, "你好")
    assert not res.ok and env.runner.agent_calls() == []


def test_send_refused_for_ide_chat(env):
    """编辑器里的对话：拒绝，而且一个进程都不起。

    🔴 第 17 轮换过一次话术：旧的那句「Cursor 不允许外部程序」**是假的** —— 官方 `stop` 钩子
    的返回值确实能往已有对话追加一句，真正拦路的是时机（钩子只在一轮快结束时由 Cursor 自己
    叫起来，外面叫不起来）。详见 `docs/iteration/r17-cursor.md`。
    所以这里**不钉整句**：钉行为（拒绝 + 没起进程）+ 语义关键词。
    """
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "x"}}])
    res = env.make().send(IDE_1, "你好")
    assert res.ok is False and res.text
    assert "送不进去" in res.text
    assert env.runner.calls == []  # 🔴 一个进程都没起


def test_send_refused_for_unknown_bad_or_empty(env):
    seed_cli(env)
    adapter = env.make()
    for chat_id, text in ((CHAT_EMPTY + "x", "hi"), ("../x", "hi"), ("pid-905", "hi"), (CHAT_A, "  "),
                          (CHAT_A, "x" * (cur.MAX_SEND_CHARS + 1))):
        assert not adapter.send(chat_id, text).ok
    assert env.runner.agent_calls() == []


def test_send_refused_without_binary(env, tmp_path):
    seed_cli(env)
    res = env.make(agent_bin=str(tmp_path / "nope")).send(CHAT_A, "hi")
    assert not res.ok and "找不到 cursor-agent" in res.text and env.runner.calls == []


def test_send_refused_when_project_dir_gone(env, tmp_path):
    write_chat(env.home, CHAT_A, str(tmp_path / "deleted"), 1_793_000_000_000)
    assert not env.make().send(CHAT_A, "hi").ok
    assert env.runner.agent_calls() == []


# ---------- send：正常路径 ----------


def test_send_argv_cwd_stdin_timeout_and_reply(env):
    seed_cli(env)
    env.state["lstarts"][4242] = SPAWN_LSTART
    res = env.make().send(CHAT_A, "-rf 也只是一句话", timeout=42)
    assert res.ok and res.text == "好的，改完了" and res.session_id == CHAT_A
    (call,) = env.runner.agent_calls()
    argv = call["argv"]
    assert argv[:9] == [str(env.binary), "--resume", CHAT_A, "-p", "--output-format", "json", "--trust",
                        "--workspace", str(env.project)]
    assert argv[-2:] == ["--", "-rf 也只是一句话"]  # 用户的话在 -- 之后，不会被当成旗标
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "ask"  # 默认只读
    assert call["cwd"] == str(env.project) and call["timeout"] == 42
    assert call["input_text"] is None  # ⇒ stdin=DEVNULL
    assert env.jobs.jobs(tool="cursor") == []  # 跑完从登记簿里清掉


@pytest.mark.parametrize("mode,flags,absent", [
    ("readonly", ["--mode", "ask"], "--force"),
    ("write", [], "--mode"),
    ("trusted", ["--force"], "--mode"),
    ("bogus", ["--mode", "ask"], "--force"),
])
def test_send_mode_flags(env, mode, flags, absent):
    seed_cli(env)
    env.make(mode=mode).send(CHAT_A, "hi")
    argv = env.runner.agent_calls()[-1]["argv"]
    head = argv[: argv.index("--")]
    assert absent not in head
    for f in flags:
        assert f in head


def test_send_mode_follows_policy_object_and_callable(env):
    seed_cli(env)

    class P:
        mode = "trusted"

    env.make(mode=lambda: P()).send(CHAT_A, "hi")
    assert "--force" in env.runner.agent_calls()[-1]["argv"]
    env.make(mode=lambda: 1 / 0).send(CHAT_A, "hi")
    assert "ask" in env.runner.agent_calls()[-1]["argv"]


def test_send_failure_shapes(env):
    seed_cli(env)
    cases = [
        # 2026-11-06 第 11 轮：stderr 原文不再发到手机（可能带本机路径）⇒ 给固定那句，原文只进日志
        (RunResult(1, stdout="", stderr="ActionRequiredError: upgrade"), "它没说原因"),
        (RunResult(0, stdout=json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "坏了"})), "坏了"),
        (RunResult(-9, timed_out=True), "等太久"),
        (RunResult(130), "停下"),
        (RunResult(-1, error="没能启动 x"), "没能启动"),
    ]
    for reply, needle in cases:
        env.runner.reply = reply
        res = env.make().send(CHAT_A, "hi")
        assert not res.ok and needle in res.text


def test_send_runner_exception_does_not_raise(env, caplog):
    seed_cli(env)

    class Boom(FakeRunner):
        def run(self, argv, **kw):
            if os.path.basename(argv[0]) == "lsof":
                return RunResult(0)
            raise RuntimeError("炸了")

    with caplog.at_level("ERROR", logger="feishu-bridge.fleet"):
        res = env.make(runner=Boom()).send(CHAT_A, "hi")
    # 第 11 轮：异常原文只进日志，手机上是一句人话（tests/test_r11_errors.py）
    assert not res.ok and "没能启动" in res.text
    assert "炸了" not in res.text and "RuntimeError" not in res.text
    assert "炸了" in caplog.text


def test_send_serialized_per_chat(env):
    """并发发到同一个对话会静默丢第一条 ⇒ 第二条直接拒，不起第二个进程。"""
    seed_cli(env)
    gate = threading.Event()
    env.runner.block = gate
    env.state["lstarts"][4242] = SPAWN_LSTART
    adapter = env.make()
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("first", adapter.send(CHAT_A, "第一条")))
    t.start()
    assert env.runner.started.wait(5)
    second = env.make().send(CHAT_A, "第二条")  # 另一个实例也要被同一把锁挡住
    gate.set()
    t.join(5)
    assert results["first"].ok
    assert not second.ok and "上一条还没回完" in second.text
    assert [c["argv"][-1] for c in env.runner.agent_calls()] == ["第一条"]


def test_send_different_chats_not_blocked(env):
    seed_cli(env)
    gate = threading.Event()
    env.runner.block = gate
    adapter = env.make()
    t = threading.Thread(target=lambda: adapter.send(CHAT_A, "a"))
    t.start()
    assert env.runner.started.wait(5)
    r2 = FakeRunner()
    b = env.make(runner=r2).send(CHAT_B, "b")
    gate.set()
    t.join(5)
    assert b.ok and len(r2.agent_calls()) == 1


def test_list_shows_bridge_owned_while_sending(env):
    seed_cli(env)
    gate = threading.Event()
    env.runner.block = gate
    env.state["lstarts"][4242] = SPAWN_LSTART
    adapter = env.make()
    t = threading.Thread(target=lambda: adapter.send(CHAT_A, "a"))
    t.start()
    assert env.runner.started.wait(5)
    env.state["ps"] = ps_line(4242, os.getpid(), SPAWN_LSTART, f"{NODE} {AGENT} --resume {CHAT_A} -p a")
    s = {x.id: x for x in env.make().list(10)}[CHAT_A]
    assert s.owner == "bridge" and s.status == "running" and s.can_stop and not s.can_send
    gate.set()
    t.join(5)


# ---------- stop ----------


def test_stop_refuses_foreign_pid_and_never_kills(env):
    seed_cli(env)
    env.state["ps"] = ps_line(900, 800, OLD_LSTART, f"{NODE} {AGENT} --resume {CHAT_A}")
    env.state["lstarts"][900] = OLD_LSTART
    adapter = env.make()
    res = adapter.stop(CHAT_A)
    assert not res.ok and "终端" in res.text
    assert not adapter.stop("pid-900").ok
    assert env.kill.calls == []


def test_stop_refuses_ide_and_idle(env):
    seed_cli(env)
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "x"}}])
    adapter = env.make()
    assert "Cursor 里点停止" in adapter.stop(IDE_1).text
    assert "没在跑" in adapter.stop(CHAT_B).text
    assert not adapter.stop("../x").ok
    assert env.kill.calls == []


def test_stop_own_sends_sigint(env):
    seed_cli(env)
    env.state["lstarts"][4242] = SPAWN_LSTART
    env.jobs.register(4242, SPAWN_LSTART, "cursor", CHAT_A)
    res = env.make().stop(CHAT_A)
    assert res.ok and env.kill.calls == [(4242, signal.SIGINT)]


def test_stop_own_but_pid_reused_refuses(env):
    seed_cli(env)
    env.jobs.register(4242, SPAWN_LSTART, "cursor", CHAT_A)
    env.state["lstarts"][4242] = OLD_LSTART  # 我们的进程退了，pid 给了别人
    res = env.make().stop(CHAT_A)
    assert not res.ok and env.kill.calls == []


def test_stop_during_send_hits_our_child(env):
    seed_cli(env)
    gate = threading.Event()
    env.runner.block = gate
    env.state["lstarts"][4242] = SPAWN_LSTART
    adapter = env.make()
    t = threading.Thread(target=lambda: adapter.send(CHAT_A, "a"))
    t.start()
    assert env.runner.started.wait(5)
    assert env.make().stop(CHAT_A).ok
    gate.set()
    t.join(5)
    assert env.kill.calls == [(4242, signal.SIGINT)]


# ---------- worker-server 回收 ----------


def _send_with_worker(env, *, worker_lstart=WORKER_LSTART, worker_cwd=None, worker_ppid=4242,
                      after_lstart=None, lsof_fail=False):
    """假发送：发送期间 ps 里出现我们子进程生的 worker；发完它 ppid 变 1 还活着（实测形状）。"""
    seed_cli(env)
    env.state["lstarts"][4242] = SPAWN_LSTART
    env.state["lstarts"][5555] = after_lstart or worker_lstart  # FleetJobs 发信号前现查的 lstart
    during = "\n".join([
        ps_line(4242, os.getpid(), SPAWN_LSTART, f"{NODE} {AGENT} --resume {CHAT_A} -p hi"),
        ps_line(5555, worker_ppid, worker_lstart, f"{NODE} {AGENT} worker-server"),
    ])
    after = ps_line(5555, 1, after_lstart or worker_lstart, f"{NODE} {AGENT} worker-server")
    env.runner.lsof = {5555: worker_cwd if worker_cwd is not None else str(env.project)}
    env.runner.lsof_fail = lsof_fail
    looked = threading.Event()
    adapter = env.make(worker_poll_sec=0.01)
    real_note = adapter._note_workers

    def note(watch, table):
        real_note(watch, table)
        if "worker-server" in "".join(p.args for p in table):
            looked.set()

    adapter._note_workers = note
    orig_run = env.runner.run

    def run(argv, **kw):
        if os.path.basename(argv[0]) == "lsof":
            return orig_run(argv, **kw)
        env.state["ps"] = during
        out = orig_run(argv, **kw)
        looked.wait(2)  # 等盯梢线程至少看过一眼
        env.state["ps"] = after
        return out

    env.runner.run = run
    return adapter.send(CHAT_A, "hi")


def test_reaps_our_worker_with_matching_cwd(env):
    res = _send_with_worker(env)
    assert res.ok
    assert env.kill.calls == [(5555, signal.SIGTERM)]
    assert env.jobs.jobs() == []


def test_keeps_worker_with_other_cwd(env):
    _send_with_worker(env, worker_cwd="/somewhere/else")
    assert env.kill.calls == []


def test_keeps_worker_when_lsof_fails(env):
    _send_with_worker(env, lsof_fail=True)
    assert env.kill.calls == []


def test_keeps_worker_started_before_us(env):
    _send_with_worker(env, worker_lstart=OLD_LSTART)
    assert env.kill.calls == []


def test_keeps_worker_whose_pid_was_reused(env):
    _send_with_worker(env, after_lstart="Wed Nov  4 11:00:00 2026")
    assert env.kill.calls == []


def test_ignores_worker_not_in_our_family(env):
    _send_with_worker(env, worker_ppid=1)  # 别人的 worker（ppid 1，不是我们子进程生的）
    assert env.kill.calls == []


# ---------- 小工具 ----------


def test_parse_ts_text_and_clean():
    assert cur._parse_ts_text("Sunday, Nov 1, 2026, 7:27 PM (UTC+8)") == pytest.approx(1_793_532_420)
    assert cur._parse_ts_text("Monday, Oct 19, 2026, 12:05 AM") > 0
    assert cur._parse_ts_text("garbage") == 0
    text, at = cur.clean_user_text("<timestamp>x</timestamp>\n<user_query>\n  hi  \n</user_query>")
    assert text == "hi" and at == 0


def test_parse_lsof_cwd():
    assert cur.parse_lsof_cwd("p12\nfcwd\nn/a b\np13\nfcwd\nn/c\n") == {12: "/a b", 13: "/c"}
    assert cur.parse_lsof_cwd("pxx\nn/a\n") == {}


def test_never_queries_cursor_disk_kv(env, monkeypatch):
    """cursorDiskKV 8GB+，全扫 19 秒：list / send / stop 执行的每一句 SQL 都不许碰它。"""
    seed_cli(env)
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "x"}}])
    statements = []
    real_connect = sqlite3.connect

    def traced(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(cur.sqlite3, "connect", traced)
    adapter = env.make()
    adapter.list(50)
    adapter.send(IDE_1, "hi")
    adapter.stop(IDE_1)
    adapter.running()
    assert statements and not [q for q in statements if "cursordiskkv" in q.lower()]
    writes = re.compile(r"^\s*(insert|update|delete|replace|create|drop|alter|pragma\s+\w+\s*=)", re.I)
    assert not [q for q in statements if writes.match(q)]


def test_state_db_opened_read_only(env, monkeypatch):
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "x"}}])
    uris = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(cur.sqlite3, "connect", lambda p, *a, **kw: uris.append((p, kw)) or real_connect(p, *a, **kw))
    env.make().list(5)
    assert uris and all(p.startswith("file:") and p.endswith("?mode=ro") and kw.get("uri") for p, kw in uris)


def test_lsof_is_called_by_absolute_path():
    """launchd 的 PATH 里没有 /usr/sbin —— 裸写 lsof 会找不到，worker-server 永远收不掉。"""
    import os

    from bridge.fleet.model import resolve_lsof

    path = resolve_lsof()
    assert os.path.isabs(path)


def test_cursor_source_has_no_bare_lsof():
    import os
    import re

    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "bridge", "fleet", "cursor.py"), encoding="utf-8").read()
    assert not re.search(r'\[\s*"lsof"', src), "cursor.py 里还有裸的 \"lsof\" 调用"


# ---------- relay：进会话聊天 ----------
#
# 实测依据写在 cursor.py 模块头。全部用 tmp_path 下的假文件 + 假 runner，
# 不起真 cursor-agent、**不碰用户现有的对话**。

OWNERSHIP_ERR = (
    f"Chat {CHAT_A} may still be running, but Cursor could not verify its persistent session:\n"
    f"Persistent chat ownership is currently changing for {CHAT_A}"
)


def _json_reply(text, subtype="success", is_error=False):
    return RunResult(0, stdout=json.dumps(
        {"type": "result", "subtype": subtype, "is_error": is_error, "result": text}))


def test_relay_multi_turn_resumes_the_same_chat(env):
    """多轮：每轮都 `--resume` 同一个 chatId（实测 session_id 不变、上下文续得上）。"""
    seed_cli(env)
    adapter = env.make()
    env.runner.reply = _json_reply("第一轮答案")
    first = adapter.relay(CHAT_A, "记住 4719")
    env.runner.reply = _json_reply("4719")
    second = adapter.relay(CHAT_A, "刚让你记的数字是多少")
    assert first.ok and second.ok and second.text == "4719"
    calls = env.runner.agent_calls()
    assert len(calls) == 2
    for call in calls:
        argv = call["argv"]
        assert argv[1:3] == ["--resume", CHAT_A]  # 同一个 chat，不是新开
        assert argv[-2] == "--"  # 用户的话在 -- 之后
    assert [c["argv"][-1] for c in calls] == ["记住 4719", "刚让你记的数字是多少"]


def test_relay_message_starting_with_dash_is_not_a_flag(env):
    seed_cli(env)
    env.make().relay(CHAT_A, "--force 只是一句话")
    argv = env.runner.agent_calls()[-1]["argv"]
    assert argv[-2:] == ["--", "--force 只是一句话"]
    assert "--force" not in argv[:-1]  # 没被当成旗标塞进去


def test_relay_defaults_to_readonly_mode(env):
    """relay 和 send 一样 fail-closed：认不出的档位按只读。"""
    seed_cli(env)
    env.make(mode="bogus").relay(CHAT_A, "hi")
    argv = env.runner.agent_calls()[-1]["argv"]
    head = argv[: argv.index("--")]
    assert "--mode" in head and head[head.index("--mode") + 1] == "ask"
    assert "--force" not in head


def test_relay_timeout_has_a_floor(env):
    """固定开销就约 3 秒 ⇒ 不让调用方把超时配到必然失败。"""
    seed_cli(env)
    env.make().relay(CHAT_A, "hi", timeout=1)
    assert env.runner.agent_calls()[-1]["timeout"] >= cur.RELAY_MIN_TIMEOUT_SEC


def test_relay_concurrency_loser_is_told_it_did_not_land(env):
    """🔴 上游并发输家响亮失败 ⇒ 翻成人话，而且 **delivered=False**（可以放心重发）。"""
    seed_cli(env)
    env.state["lstarts"][4242] = SPAWN_LSTART
    env.runner.reply = RunResult(1, stdout="", stderr=OWNERSHIP_ERR)
    res = env.make().relay(CHAT_A, "hi")
    assert not res.ok
    assert res.text == cur.WHY_RELAY_RACE
    assert res.delivered is False  # 实测没进去、transcript 没坏行 ⇒ 别说「可能已经做了」
    assert "ownership" not in res.text and CHAT_A not in res.text  # 不甩上游原话


def test_relay_still_says_delivered_when_it_times_out(env):
    """超时 ≠ 没发进去（它可能已经动手了）⇒ delivered 要是 True。"""
    seed_cli(env)
    env.state["lstarts"][4242] = SPAWN_LSTART
    env.runner.reply = RunResult(-9, timed_out=True)
    res = env.make().relay(CHAT_A, "hi")
    assert not res.ok and res.delivered is True


def test_relay_serialized_per_chat_but_not_across_chats(env):
    """锁的粒度是 chatId：同一个 chat 第二条直接拒，不同 chat 可以并行。"""
    seed_cli(env)
    gate = threading.Event()
    env.runner.block = gate
    env.state["lstarts"][4242] = SPAWN_LSTART
    adapter = env.make()
    results = {}
    t = threading.Thread(target=lambda: results.setdefault("first", adapter.relay(CHAT_A, "第一条")))
    t.start()
    assert env.runner.started.wait(5)
    same = env.make().relay(CHAT_A, "第二条")
    other = env.make(runner=FakeRunner()).relay(CHAT_B, "别的对话")
    gate.set()
    t.join(5)
    assert results["first"].ok
    assert not same.ok and same.text == cur.WHY_BUSY_SEND
    assert other.ok  # 不同 chat 不挡
    assert [c["argv"][-1] for c in env.runner.agent_calls()] == ["第一条"]


def test_relay_refused_when_open_in_terminal_and_spawns_nothing(env):
    """电脑上开着它 ⇒ 不发（两边抢着写会丢话），一个子进程都不起。"""
    seed_cli(env)
    env.state["ps"] = ps_line(900, 1, OLD_LSTART, f"{NODE} {AGENT} --resume {CHAT_A}")
    res = env.make().relay(CHAT_A, "hi")
    assert not res.ok and res.text == cur.WHY_TERMINAL_SEND
    assert env.runner.agent_calls() == []


@pytest.mark.parametrize("chat_id,needle", [
    ("", "认不出"),
    ("pid-900", "认不出"),
    ("../../etc/passwd", "认不出"),
    ("99999999-9999-4999-8999-000000000099", "找不到"),
])
def test_relay_refuses_bad_ids_without_spawning(env, chat_id, needle):
    seed_cli(env)
    res = env.make().relay(chat_id, "hi")
    assert not res.ok and needle in res.text and not res.delivered
    assert env.runner.agent_calls() == []


def test_relay_refuses_empty_and_overlong(env):
    seed_cli(env)
    adapter = env.make()
    assert not adapter.relay(CHAT_A, "   ").ok
    assert not adapter.relay(CHAT_A, "x" * (cur.MAX_SEND_CHARS + 1)).ok
    assert env.runner.agent_calls() == []


def test_relay_refused_without_binary(env, tmp_path):
    seed_cli(env)
    res = env.make(agent_bin=str(tmp_path / "nope")).relay(CHAT_A, "hi")
    assert not res.ok and res.text == cur.WHY_NO_BIN
    assert env.runner.agent_calls() == []


# ---------- 编辑器（IDE）里的对话：发不进去，而且不去试 ----------


def test_relay_refuses_ide_chat_with_a_plain_alternative(env):
    """🔴 外部发不进去。给一句人话 + **能做到的替代**，不写 state.vscdb、不做 UI 自动化。"""
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "编辑器里的"}}])
    res = env.make().relay(IDE_1, "hi")
    assert not res.ok and not res.delivered
    assert res.text == cur.WHY_RELAY_IDE
    assert "在电脑上打开 Cursor" in res.text  # 替代方案
    assert "cursor-agent" in res.text  # 另一条替代：终端里新开一个
    assert env.runner.agent_calls() == []  # 🔴 压根没去 resume 它


def test_relay_never_resumes_an_ide_composer_id(env):
    """拿命令行去接编辑器那种 id：**实测接不上**，所以不拿用户的对话试。

    🔴 第 17 轮换过一次话术：这条路第 14 轮写的是「还没量出结论」，第 17 轮在**一次性对话**上
    量出来了 —— 接不上编辑器那份历史（只会用同一个 id 另起一个空的），而且会把两边共用的
    那份导出记录整份重写（哨兵 276 B → 139 B）。详见 `docs/iteration/r17-cursor.md`。
    所以这里**不钉整句**，也不再钉「没量出结论」：钉行为（一次 resume 都不发）+ 语义关键词。
    """
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "x"}}])
    seed_cli(env)
    env.make().relay(IDE_1, "hi")
    assert [c["argv"][2] for c in env.runner.agent_calls()] == []  # 一次 resume 都没发
    assert cur.WHY_RELAY_IDE_RESUME_UNTESTED  # 话术在，而且说的是实测结论
    assert "接不上" in cur.WHY_RELAY_IDE_RESUME_UNTESTED


def test_relay_never_touches_state_vscdb_or_ui(env, monkeypatch):
    """IDE 那条路上：不执行任何写语句，也不 shell out 到 open / osascript。"""
    make_state_db(env.app_user, [{"id": IDE_1, "ts": 1_793_500_000_000, "value": {"name": "x"}}])
    statements = []
    real_connect = sqlite3.connect

    def traced(*a, **kw):
        conn = real_connect(*a, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(cur.sqlite3, "connect", traced)
    env.make().relay(IDE_1, "hi")
    writes = re.compile(r"^\s*(insert|update|delete|replace|create|drop|alter)", re.I)
    assert not [q for q in statements if writes.match(q)]
    for call in env.runner.calls:
        assert os.path.basename(call["argv"][0]) in ("lsof", "ps", "cursor-agent"), call["argv"]


def test_cursor_source_has_no_ui_automation_or_deeplink():
    """源码级闸门：不许出现 osascript / 深链 / 写 state.vscdb 的痕迹。"""
    import inspect

    src = inspect.getsource(cur)
    for banned in ("osascript", "cursor://", "AppleScript", "System Events"):
        assert banned not in src, banned


# ---------- 取回复：turn_ended 不能当完成判据 ----------


def test_reply_after_echo_skips_the_previous_turn():
    """🔴 回显之前的 assistant 属于上一轮，不能当成这句的答案。"""
    lines = [json.dumps(r, ensure_ascii=False) for r in [
        _user("上一句"), _assistant("上一轮的回答"), TURN_ENDED,
        _user("这一句"), _assistant("这一轮的回答"),
    ]]
    assert cur.reply_after_echo(lines, "这一句") == "这一轮的回答"
    assert cur.reply_after_echo(lines, "上一句") == "上一轮的回答"
    assert cur.reply_after_echo(lines, "从没发过的话") == ""  # 找不到回显 ⇒ 空，不瞎猜
    assert cur.reply_after_echo(lines, "") == ""
    assert cur.reply_after_echo([], "x") == ""


def test_reply_after_echo_takes_the_last_occurrence():
    """同一句话发过两次 ⇒ 取最后一次回显后面的。"""
    lines = [json.dumps(r, ensure_ascii=False) for r in [
        _user("同一句"), _assistant("第一次的答案"),
        _user("同一句"), _assistant("第二次的答案"),
    ]]
    assert cur.reply_after_echo(lines, "同一句") == "第二次的答案"


def test_reply_after_echo_stops_at_the_next_user_turn():
    lines = [json.dumps(r, ensure_ascii=False) for r in [
        _user("这一句"), _assistant("这轮的"), _user("下一句"), _assistant("下轮的"),
    ]]
    assert cur.reply_after_echo(lines, "这一句") == "这轮的"


def test_relay_falls_back_to_transcript_when_json_has_no_text(env):
    """json 里没文字 ⇒ 回去读 transcript（按内容找回显），而不是报「没有回复」。"""
    seed_cli(env)
    write_transcript(env.home, "tmp-proj", CHAT_A, [
        _user("老问题"), _assistant("老回答"), TURN_ENDED,
        _user("新问题"), _assistant("记录里的新回答"),
    ])
    env.runner.reply = _json_reply("")  # 跑成功了，但 result 是空
    res = env.make().relay(CHAT_A, "新问题")
    assert res.ok and res.text == "记录里的新回答"


def test_relay_says_no_text_when_the_echo_is_not_there(env):
    """transcript 里找不到我们这句 ⇒ 说「没有文字回复」，**不拿上一轮的答案充数**。"""
    seed_cli(env)
    write_transcript(env.home, "tmp-proj", CHAT_A, [_user("老问题"), _assistant("老回答"), TURN_ENDED])
    env.runner.reply = _json_reply("")
    res = env.make().relay(CHAT_A, "刚发的新问题")
    assert res.ok and res.text == cur.RELAY_EMPTY_REPLY
    assert "老回答" not in res.text


def test_relay_does_not_use_turn_ended_as_the_done_signal():
    """🔴 实测：7 轮之后 transcript 里只有 1 条 turn_ended（整文件重写会覆盖它）。

    所以取回复的代码里不许把 turn_ended 当完成判据。
    """
    import inspect

    src = inspect.getsource(cur.reply_after_echo) + inspect.getsource(cur.CursorAdapter.reply_after)
    assert "turn_ended" not in src


def test_relay_reply_reader_handles_whole_file_rewrite(env):
    """整文件重写之后（内容全换、长度变短）照样读得对 —— 因为按内容找，不用字节偏移。"""
    seed_cli(env)
    adapter = env.make()
    write_transcript(env.home, "tmp-proj", CHAT_A, [_user("甲"), _assistant("甲的答案" * 50), TURN_ENDED])
    assert adapter.reply_after(CHAT_A, "甲")
    # Cursor 把整个文件重写了：短得多、turn_ended 被下一轮覆盖掉
    write_transcript(env.home, "tmp-proj", CHAT_A, [_user("乙"), _assistant("乙的答案")])
    assert adapter.reply_after(CHAT_A, "乙") == "乙的答案"
    assert adapter.reply_after(CHAT_A, "甲") == ""  # 旧的已经不在文件里了，不瞎编
    assert adapter.reply_after("pid-900", "x") == ""


def test_send_failure_keeps_stderr_in_the_log_not_on_the_phone(env, caplog):
    """🔴 阳性对照：stderr 原文不上屏，但**日志里必须还在**（排查要用）—— 两边别弄反。"""
    import logging

    seed_cli(env)
    env.runner.reply = RunResult(1, stdout="", stderr="FileNotFoundError: /Users/someone/.cursor/x")
    with caplog.at_level(logging.WARNING):
        res = env.make().send(CHAT_A, "hi")
    assert "/Users/" not in res.text and "FileNotFoundError" not in res.text
    assert "FileNotFoundError" in caplog.text, "原文没进日志 ⇒ 出了问题没法查"
