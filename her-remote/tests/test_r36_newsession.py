"""第 36 轮：从手机新建会话的后端（bridge/fleet/newsession.py）。全部用假进程，不起真 CLI。"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import threading
import time

import pytest

from bridge.fleet import newsession as ns
from bridge.fleet.model import FleetJobs, RunResult
from bridge.policy import Policy


SECRET = "sk-ant-api03-" + "A" * 40


def _policy(tmp_path, mode="readonly", ceiling="trusted"):
    return Policy(mode=mode, workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                  model="sonnet", turn_timeout_sec=30, ceiling_mode=ceiling)


class FakeProc:
    _next = 41000

    def __init__(self, lines, *, rc=0, err="", hold=None, exit_early=False):
        FakeProc._next += 1
        self.pid = FakeProc._next
        self._lines = lines
        self._rc = rc
        self._hold = hold or threading.Event()
        if exit_early:
            self._hold.set()
        self.stdout = self._gen()
        self.stderr = io.StringIO(err)
        self.returncode = None
        self._done = threading.Event()
        self.signals = []

    def _gen(self):
        for line in self._lines:
            yield line + "\n"
        self._hold.wait(10)
        self.returncode = self._rc
        self._done.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._done.wait(timeout if timeout is not None else 10):
            import subprocess
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class FakePopen:
    def __init__(self, procs):
        self.procs = list(procs)
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return self.procs.pop(0)


def _jobs(procs_by_pid=None):
    sent = []

    def kill(pid, sig):
        sent.append((pid, sig))
        for p in (procs_by_pid or {}).values():
            if p.pid == pid:
                p.signals.append(sig)
                p._hold.set()

    return FleetJobs(lstart_of=lambda pid: "Mon Nov 16 00:00:00 2026", kill=kill), sent


def _which(name):
    return f"/fake/bin/{name}"


def _done_box():
    box, ev = [], threading.Event()

    def on_done(ft):
        box.append(ft)
        ev.set()

    return box, ev, on_done


CLAUDE_OK = [
    json.dumps({"type": "system", "subtype": "session_title_changed", "session_id": "x"}),
    json.dumps({"type": "system", "subtype": "init", "session_id": "PLACEHOLDER"}),
    json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "好", "session_id": "PLACEHOLDER"}),
]


def _req(tool, cwd, prompt="只回一个字：好", open_window=False, mode="readonly"):
    return ns.NewSessionRequest(tool=tool, cwd=str(cwd), prompt=prompt, open_window=open_window, mode=mode)


# ---------- available_tools ----------

def test_available_tools_only_lists_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "_FALLBACKS", {"claude": (), "codex": (), "cursor": ()})
    got = ns.available_tools(which=lambda n: "/x/claude" if n == "claude" else None)
    assert got == ["claude"]


def test_find_bin_falls_back_to_default_install(tmp_path, monkeypatch):
    exe = tmp_path / "cursor-agent"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(ns, "_FALLBACKS", {"claude": (), "codex": (), "cursor": (str(exe),)})
    assert ns.find_bin("cursor", which=lambda n: None) == str(exe)
    assert ns.find_bin("nope", which=_which) == ""


# ---------- Claude ----------

def test_claude_start_returns_id_fast_and_finishes_in_background(tmp_path, caplog):
    proc = FakeProc(CLAUDE_OK, exit_early=True)
    popen = FakePopen([proc])
    jobs, _ = _jobs()
    box, ev, on_done = _done_box()
    caplog.set_level(logging.INFO, logger="feishu-bridge.fleet")
    t0 = time.monotonic()
    res = ns.start(_req("claude", tmp_path, prompt="看看 " + SECRET), policy=_policy(tmp_path), jobs=jobs,
                   popen=popen, which=_which, auth=lambda: ({"ANTHROPIC_BASE_URL": "http://gw"}, {}),
                   on_done=on_done)
    assert time.monotonic() - t0 < 3
    assert res.ok, res.note
    assert ns._UUID.match(res.session_id)
    assert res.name.endswith("-m" + res.session_id[:4]) and res.name.isascii()
    assert res.window_opened is False
    argv, kw = popen.calls[0]
    assert argv[argv.index("--session-id") + 1] == res.session_id
    assert argv[argv.index("-n") + 1] == res.name
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv and "stream-json" in argv
    assert "CLAUDE_CONFIG_DIR" not in kw["env"]  # 落在用户自己的 ~/.claude
    assert not any(k.startswith("CLAUDE_CODE_") for k in kw["env"])
    assert kw["env"]["ANTHROPIC_BASE_URL"] == "http://gw"
    assert kw["cwd"] == os.path.realpath(str(tmp_path))
    assert ev.wait(5)
    assert box[0].ok and box[0].text == "好" and box[0].session_id == res.session_id
    assert jobs.jobs() == []  # 跑完就 forget
    assert ns.started_from_phone("claude", res.session_id)
    assert SECRET not in caplog.text  # 原话只脱敏后进日志


def test_claude_registered_in_fleetjobs_while_running(tmp_path):
    hold = threading.Event()
    proc = FakeProc(CLAUDE_OK[:2], hold=hold)
    jobs, _ = _jobs({"p": proc})
    res = ns.start(_req("claude", tmp_path), policy=_policy(tmp_path), jobs=jobs, popen=FakePopen([proc]),
                   which=_which, auth=lambda: ({}, {}))
    assert res.ok
    own = jobs.jobs(tool="claude", session_id=res.session_id)
    assert [j.pid for j in own] == [proc.pid]
    stopped = jobs.stop_session("claude", res.session_id)  # 「⏹ 停下」能停
    assert stopped.ok
    hold.set()


def test_claude_write_mode_without_hook_downgrades_to_readonly(tmp_path):
    proc = FakeProc(CLAUDE_OK, exit_early=True)
    popen = FakePopen([proc])
    jobs, _ = _jobs()
    res = ns.start(_req("claude", tmp_path, mode="write"), policy=_policy(tmp_path), jobs=jobs, popen=popen,
                   which=_which, auth=lambda: ({}, {}))
    assert res.ok and res.mode == "readonly"
    assert ns.NOTE_DOWNGRADED in res.note
    argv, kw = popen.calls[0]
    assert not any(a.startswith("Write(") or a.startswith("Edit(") for a in argv)
    assert "CARHER_APPROVAL_DIR" not in kw["env"]


def test_claude_write_mode_with_hook_uses_phone_mode(tmp_path):
    proc = FakeProc(CLAUDE_OK, exit_early=True)
    popen = FakePopen([proc])
    jobs, _ = _jobs()
    hook = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}}
    dirs = []
    res = ns.start(_req("claude", tmp_path, mode="write"), policy=_policy(tmp_path), jobs=jobs, popen=popen,
                   which=_which, auth=lambda: ({}, {}), hook=lambda: hook,
                   approval_dir=lambda sid: dirs.append(sid) or str(tmp_path / "appr"))
    assert res.ok and res.mode == "write"
    argv, kw = popen.calls[0]
    assert any(a.startswith("Write(") for a in argv)
    assert json.loads(argv[argv.index("--settings") + 1])["hooks"] == hook["hooks"]
    assert kw["env"]["CARHER_APPROVAL_DIR"] == str(tmp_path / "appr")
    assert dirs == [res.session_id]


def test_claude_mode_clamped_to_ceiling(tmp_path):
    proc = FakeProc(CLAUDE_OK, exit_early=True)
    jobs, _ = _jobs()
    res = ns.start(_req("claude", tmp_path, mode="trusted"), policy=_policy(tmp_path, ceiling="readonly"),
                   jobs=jobs, popen=FakePopen([proc]), which=_which, auth=lambda: ({}, {}),
                   hook={"hooks": {"x": []}}, approval_dir="/tmp/a")
    assert res.ok and res.mode == "readonly"


def test_claude_exits_immediately_gives_plain_words(tmp_path):
    proc = FakeProc([], rc=1, err="Error: /Users/someone/.claude/x: Not logged in\n", exit_early=True)
    jobs, _ = _jobs()
    res = ns.start(_req("claude", tmp_path), policy=_policy(tmp_path), jobs=jobs, popen=FakePopen([proc]),
                   which=_which, auth=lambda: ({}, {}))
    assert not res.ok and res.session_id == ""
    assert res.note.startswith("Claude 一启动就退出了") and "/Users/" not in res.note and "Not logged in" in res.note
    assert jobs.jobs() == []


def test_spawn_oserror_is_plain_words(tmp_path):
    def boom(argv, **kw):
        raise FileNotFoundError(2, "No such file", "/Users/someone/bin/claude")

    jobs, _ = _jobs()
    res = ns.start(_req("claude", tmp_path), policy=_policy(tmp_path), jobs=jobs, popen=boom, which=_which,
                   auth=lambda: ({}, {}))
    assert not res.ok and "/Users/" not in res.note and "Errno" not in res.note


# ---------- Codex ----------

CODEX_TID = "01a1e003-4bc5-7fe2-a5f4-b8bc2da479b7"
CODEX_OK = [
    json.dumps({"type": "thread.started", "thread_id": CODEX_TID}),
    json.dumps({"type": "item.completed", "item": {"type": "error", "message": "skills budget"}}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "好"}}),
    json.dumps({"type": "turn.completed"}),
]


def test_codex_id_from_thread_started_and_readonly(tmp_path):
    hold = threading.Event()
    proc = FakeProc(CODEX_OK[:1], hold=hold)
    popen = FakePopen([proc])
    jobs, _ = _jobs()
    res = ns.start(_req("codex", tmp_path, mode="trusted"), policy=_policy(tmp_path, mode="trusted"), jobs=jobs,
                   popen=popen, which=_which)
    assert res.ok and res.session_id == CODEX_TID
    assert ns.NOTE_READONLY.format(label="Codex") in res.note
    argv, kw = popen.calls[0]
    assert argv[1:3] == ["exec", "--json"]
    assert argv[argv.index("-s") + 1] == "read-only"  # 不跟手机档位，一律只读
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[-2:] == ["--", "只回一个字：好"]
    assert [j.session_id for j in jobs.jobs(tool="codex")] == [CODEX_TID]  # 拿到 id 后改登记到会话名下
    hold.set()


def test_codex_reply_collected(tmp_path):
    proc = FakeProc(CODEX_OK, exit_early=True)
    jobs, _ = _jobs()
    box, ev, on_done = _done_box()
    res = ns.start(_req("codex", tmp_path), policy=_policy(tmp_path), jobs=jobs, popen=FakePopen([proc]),
                   which=_which, on_done=on_done)
    assert res.ok and ev.wait(5)
    assert box[0].ok and box[0].text == "好"


def test_codex_no_id_in_time_is_stopped(tmp_path):
    hold = threading.Event()
    proc = FakeProc([json.dumps({"type": "turn.started"})], hold=hold)
    jobs, sent = _jobs({"p": proc})
    res = ns.start(_req("codex", tmp_path), policy=_policy(tmp_path), jobs=jobs, popen=FakePopen([proc]),
                   which=_which, id_wait=0.3)
    assert not res.ok and res.note == ns.WHY_NO_ID.format(label="Codex")
    deadline = time.time() + 3
    while time.time() < deadline and not sent:
        time.sleep(0.05)
    assert sent and sent[0][0] == proc.pid  # 没拿到 id 的进程不留着
    hold.set()


def test_dash_prompt_not_taken_as_flag(tmp_path):
    assert ns.codex_argv("codex", "/w", "-rf")[-1] == " -rf"
    jobs, _ = _jobs()
    res = ns.start(_req("codex", tmp_path, prompt="-"), policy=_policy(tmp_path), jobs=jobs,
                   popen=FakePopen([]), which=_which)
    assert not res.ok and res.note == ns.WHY_EMPTY


# ---------- Cursor ----------

CURSOR_ID = "54676aa5-a315-4327-ad4b-94e7758de967"


class FakeRunner:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return self.result


def test_cursor_create_chat_then_resume_ask_mode(tmp_path):
    runner = FakeRunner(RunResult(0, CURSOR_ID + "\n", ""))
    proc = FakeProc([json.dumps({"type": "result", "subtype": "success", "result": "好"})], exit_early=True)
    popen = FakePopen([proc])
    jobs, _ = _jobs()
    box, ev, on_done = _done_box()
    t0 = time.monotonic()
    res = ns.start(_req("cursor", tmp_path, mode="trusted"), policy=_policy(tmp_path, mode="trusted"), jobs=jobs,
                   runner=runner, popen=popen, which=_which, on_done=on_done)
    assert time.monotonic() - t0 < 3
    assert res.ok and res.session_id == CURSOR_ID
    assert runner.calls[0][0][1:] == ["create-chat"]
    assert runner.calls[0][1]["timeout"] <= 2.5
    argv, _kw = popen.calls[0]
    assert argv[argv.index("--resume") + 1] == CURSOR_ID
    assert argv[argv.index("--mode") + 1] == "ask"
    assert "--force" not in argv and "--yolo" not in argv
    assert ev.wait(5) and box[0].text == "好"


def test_cursor_create_chat_hangs_gives_plain_words(tmp_path):
    runner = FakeRunner(RunResult(-1, "", "", timed_out=True))
    jobs, _ = _jobs()
    res = ns.start(_req("cursor", tmp_path), policy=_policy(tmp_path), jobs=jobs, runner=runner,
                   popen=FakePopen([]), which=_which)
    assert not res.ok and "重新登录" in res.note


def test_cursor_garbage_id_rejected(tmp_path):
    runner = FakeRunner(RunResult(0, "../../etc\n", ""))
    jobs, _ = _jobs()
    res = ns.start(_req("cursor", tmp_path), policy=_policy(tmp_path), jobs=jobs, runner=runner,
                   popen=FakePopen([]), which=_which)
    assert not res.ok


# ---------- 公共闸门 ----------

def test_cwd_outside_workspace_rejected(tmp_path):
    inside = tmp_path / "ws"
    inside.mkdir()
    outside = tmp_path / "other"
    outside.mkdir()
    pol = Policy(mode="readonly", workspaces=(str(inside),), default_workspace=str(inside), model="sonnet",
                 turn_timeout_sec=30)
    jobs, _ = _jobs()
    for cwd in (outside, inside / ".." / "other"):
        res = ns.start(_req("claude", cwd), policy=pol, jobs=jobs, popen=FakePopen([]), which=_which)
        assert not res.ok and res.note == ns.WHY_OUTSIDE
    link = inside / "escape"
    link.symlink_to(outside)
    res = ns.start(_req("claude", link), policy=pol, jobs=jobs, popen=FakePopen([]), which=_which)
    assert not res.ok and res.note == ns.WHY_OUTSIDE


@pytest.mark.parametrize("req_kw,expect", [
    ({"tool": "vim"}, ns.WHY_BAD_TOOL),
    ({"prompt": "   "}, ns.WHY_EMPTY),
    ({"prompt": "x" * (ns.MAX_PROMPT_CHARS + 1)}, ns.WHY_TOO_LONG),
])
def test_bad_requests(tmp_path, req_kw, expect):
    kw = {"tool": "claude", "cwd": str(tmp_path), "prompt": "hi", "open_window": False, "mode": "readonly"}
    kw.update(req_kw)
    jobs, _ = _jobs()
    res = ns.start(ns.NewSessionRequest(**kw), policy=_policy(tmp_path), jobs=jobs, popen=FakePopen([]),
                   which=_which)
    assert not res.ok and res.note == expect


def test_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, "_FALLBACKS", {"claude": (), "codex": (), "cursor": ()})
    jobs, _ = _jobs()
    res = ns.start(_req("codex", tmp_path), policy=_policy(tmp_path), jobs=jobs, popen=FakePopen([]),
                   which=lambda n: None)
    assert not res.ok and res.note == ns.WHY_NOT_INSTALLED.format(label="Codex")


def test_no_policy(tmp_path):
    jobs, _ = _jobs()
    res = ns.start(_req("claude", tmp_path), policy=None, jobs=jobs, popen=FakePopen([]), which=_which)
    assert not res.ok and res.note == ns.WHY_NO_POLICY


# ---------- 电脑上开窗口：第一轮跑完之后才开 ----------

def test_window_opens_only_after_first_turn_exits(tmp_path):
    hold = threading.Event()
    proc = FakeProc(CLAUDE_OK, hold=hold)
    opened = []

    def open_terminal(path):
        opened.append((path, proc.poll(), open(path).read(), oct(os.stat(path).st_mode & 0o777)))
        return True, ""

    jobs, _ = _jobs()
    box, ev, on_done = _done_box()
    res = ns.start(_req("claude", tmp_path, prompt="秘密原话" + SECRET, open_window=True), policy=_policy(tmp_path),
                   jobs=jobs, popen=FakePopen([proc]), which=_which, auth=lambda: ({}, {}), on_done=on_done,
                   open_terminal=open_terminal, script_dir=str(tmp_path))
    assert res.ok and not res.window_opened and ns.NOTE_WINDOW_LATER in res.note
    time.sleep(0.3)
    assert opened == []  # 第一轮还在跑 ⇒ 不开（开了就分叉）
    hold.set()
    assert ev.wait(5)
    assert box[0].window_opened
    path, rc_at_open, script, mode = opened[0]
    assert rc_at_open == 0
    assert mode == "0o700"
    assert f"--resume {res.session_id}" in script
    assert "秘密原话" not in script and SECRET not in script  # 脚本里只有 id
    assert "unset" in script and "CLAUDE_CONFIG_DIR" in script


def test_window_scripts_per_tool():
    s = ns.window_script("codex", "/b/codex", CODEX_TID, "/w/p q")
    assert "resume -s read-only -C '/w/p q' " + CODEX_TID in s
    c = ns.window_script("cursor", "/b/cursor-agent", CURSOR_ID, "/w")
    assert f"--resume {CURSOR_ID}" in c and "--mode ask" in c
    assert 'rm -f "$0"' in s


def test_window_open_failure_is_reported_not_raised(tmp_path):
    proc = FakeProc(CLAUDE_OK, exit_early=True)
    jobs, _ = _jobs()
    box, ev, on_done = _done_box()
    res = ns.start(_req("claude", tmp_path, open_window=True), policy=_policy(tmp_path), jobs=jobs,
                   popen=FakePopen([proc]), which=_which, auth=lambda: ({}, {}), on_done=on_done,
                   open_terminal=lambda p: (False, "被系统拦了"), script_dir=str(tmp_path))
    assert res.ok and ev.wait(5)
    assert not box[0].window_opened and "没打开" in box[0].window_note
    assert not [p for p in os.listdir(tmp_path) if p.startswith("carher-newsession-")]  # 没打开就清掉脚本


def test_session_name_marks_phone_origin():
    sid = "7a47792f-a829-45f3-a70c-c964616d7698"
    name = ns.session_name("/Users/x/codes/车载 her", sid)
    assert name.isascii() and name.endswith("-m7a47")
    assert ns.started_from_phone("claude", "deadbeef-0000-0000-0000-000000000000", "foo-mdead")
    assert not ns.started_from_phone("claude", "deadbeef-0000-0000-0000-000000000000", "carher-admin-de")


def test_send_is_refused_right_after_a_window_was_opened(monkeypatch):
    """🔴 复审 Y1：开窗后窗口里的 claude 还没写注册表 ⇒ 这段时间手机发一律拦（不然 -p --resume 分叉）。"""
    from types import SimpleNamespace

    from bridge.fleet import control as control_mod
    from bridge.fleet import newsession as ns

    sid = "11111111-2222-3333-4444-555555555555"
    calls = []
    adapter = SimpleNamespace(send=lambda *a, **k: calls.append(a) or SimpleNamespace(ok=True))
    ctl = control_mod.FleetControl.__new__(control_mod.FleetControl)
    ctl.fleet = SimpleNamespace(adapter=lambda tool: adapter)
    ns.note_window_opened(sid)
    try:
        res = ctl.send("claude", sid, "hi")
        # 🔴 11-20：原来断言文案里有「分叉」，而那句话改成了「会和窗口里那个撞上」
        #（给用户看的不该出现「分叉」这种内部词）。这条测试钉的是**行为**：
        # 没起来就不许发、adapter 一次都不许被调。用词交给 test_copy_quality 那套闸门管。
        assert not res.ok and calls == [], res
        assert res.text == ns.WHY_WINDOW_JUST_OPENED, res.text
        assert ns.window_just_opened(sid, now=time.monotonic() + ns.WINDOW_GRACE_SEC + 1) is False
        res = ctl.send("claude", sid, "hi")       # 阳性对照：过了宽限就照常走 adapter
        assert calls
    finally:
        ns._WINDOW_OPENED.pop(sid, None)


# ---------- ⑤ 「📱 从手机开的」落盘：桥重启后不丢（11-16 补丁） ----------


@pytest.fixture(autouse=True)
def _phone_mark_state_in_tmp(tmp_path, monkeypatch):
    """本文件所有用例（含既有的 start 用例）一律把标记文件重定向到 tmp，绝不写真 ~/.carher-feishu-agent。"""
    monkeypatch.setenv(ns.STATE_ENV, str(tmp_path / "state" / "newsession-started.json"))
    monkeypatch.setattr(ns, "_DISK_CACHE", {"key": None, "data": {}})


def _new_process():
    """模拟桥重启：内存字典 + 读盘缓存都清空。"""
    with ns._STARTED_LOCK:
        ns._STARTED.clear()
    ns._DISK_CACHE.update(key=None, data={})


def test_phone_mark_survives_restart_for_codex_and_cursor(tmp_path):
    path = tmp_path / "state" / "newsession-started.json"
    codex_sid, cursor_sid = "019a0000-aaaa-bbbb-cccc-000000000001", "c0ffee00-1111-2222-3333-444444444444"
    ns.mark_started("codex", codex_sid)
    ns.mark_started("cursor", cursor_sid)
    assert path.exists()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert not [p for p in os.listdir(path.parent) if p != path.name]  # 没留临时文件
    _new_process()
    # Codex/Cursor 没有会话名尾巴 ⇒ 只能靠盘认出来
    assert ns.started_from_phone("codex", codex_sid)
    assert ns.started_from_phone("cursor", cursor_sid, "")
    assert not ns.started_from_phone("codex", cursor_sid)  # 家不对不算
    assert not ns.started_from_phone("codex", "019a0000-aaaa-bbbb-cccc-999999999999")  # 阴性对照


def test_phone_mark_survives_a_real_new_process(tmp_path):
    """真·新进程：另起一个 python 只 import 模块，内存是空的，只能靠读盘认出来。"""
    import subprocess
    import sys

    sid = "abcdef01-0000-0000-0000-000000000000"
    ns.mark_started("codex", sid)
    code = ("import sys; from bridge.fleet import newsession as n; "
            "print(n._STARTED == {}, n.started_from_phone('codex', sys.argv[1]), "
            "n.started_from_phone('codex', 'not-' + sys.argv[1]))")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    res = subprocess.run([sys.executable, "-c", code, sid], cwd=root, capture_output=True, text=True,
                         timeout=60, env={**os.environ, ns.STATE_ENV: os.environ[ns.STATE_ENV]})
    assert res.returncode == 0, res.stderr
    assert res.stdout.split() == ["True", "True", "False"]


@pytest.mark.parametrize("content", [None, "", "{坏了", "[1,2,3]", "null", '{"started": "x"}',
                                     '{"started": {"codex:abc": "昨天", ":x": 1, "codex": 2}}'])
def test_started_from_phone_never_raises_on_missing_or_broken_file(tmp_path, content):
    path = tmp_path / "state" / "newsession-started.json"
    if content is not None:
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
    _new_process()
    assert ns.started_from_phone("codex", "abc") is False
    assert ns.started_from_phone("claude", "deadbeef-0000", "foo-mdead") is True  # 会话名兜底照常
    ns.mark_started("codex", "abc")  # 坏文件上写也不抛，且写完能认
    _new_process()
    assert ns.started_from_phone("codex", "abc")


def test_unwritable_state_path_does_not_raise(tmp_path, monkeypatch):
    blocker = tmp_path / "is-a-file"
    blocker.write_text("x")
    monkeypatch.setenv(ns.STATE_ENV, str(blocker / "sub" / "s.json"))  # 父目录是个文件 ⇒ makedirs 必失败
    ns.mark_started("codex", "zzz")
    assert ns.started_from_phone("codex", "zzz")  # 内存那份还在
    _new_process()
    assert ns.started_from_phone("codex", "zzz") is False


def test_state_file_pruned_by_age_and_count(tmp_path, monkeypatch):
    path = tmp_path / "state" / "newsession-started.json"
    now = 2_000_000_000.0
    old = {f"codex:old{i}": now - ns.STATE_MAX_AGE_SEC - 10 for i in range(3)}
    many = {f"codex:s{i:04d}": now - 1000 + i for i in range(ns.STATE_MAX_ITEMS + 20)}
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"v": 1, "started": {**old, **many}}), encoding="utf-8")
    monkeypatch.setattr(ns.time, "time", lambda: now)
    ns.mark_started("cursor", "newest")
    got = json.loads(path.read_text(encoding="utf-8"))["started"]
    assert len(got) == ns.STATE_MAX_ITEMS
    assert "cursor:newest" in got
    assert not [k for k in got if k.startswith("codex:old")]
    assert "codex:s0000" not in got and f"codex:s{ns.STATE_MAX_ITEMS + 19:04d}" in got  # 丢最旧的
