"""会话总控底座。

重点是**不许停错进程**的那两道闸：pid 复用守卫（`same_process`）和登记簿（`FleetJobs.owns`）。
这两条破了，手机上点一下「停下」就可能给一个毫不相干的进程发 SIGINT。
测试全部用固定字符串和假回调，不起 ps 以外的任何东西，不碰任何真实会话。
"""

from __future__ import annotations

import calendar
import os
import signal
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import (  # noqa: E402
    ActionResult,
    Adapter,
    FleetJobs,
    Message,
    ProcTable,
    RunResult,
    Session,
    SubprocessRunner,
    ago,
    humanize_age,
    parse_etime,
    parse_lstart,
    parse_proc_start,
    short_title,
)

PS_SAMPLE = """\
    1     0 Ss   Mon Oct 26 00:36:24 2026     09-15:38:17 /sbin/launchd
13706 13650 S+   Tue Nov  3 19:02:35 2026        02:11:05 claude --model opus
22222  1 T    Tue Nov  3 21:00:00 2026           05:00 /usr/local/bin/codex exec resume abc  hi   there
  333     1 Z    Tue Nov  3 21:10:00 2026           00:07
garbage line here
 4444     1 S    Tue Xyz  3 21:10:00 2026           00:07 bad-month
"""


def _table() -> ProcTable:
    return ProcTable.parse(PS_SAMPLE)


# ---------- ps 解析 ----------


def test_ps_parse_fields():
    t = _table()
    assert len(t) == 4  # 垃圾行、月份不认识的行都丢掉
    p = t.get(13706)
    assert p.ppid == 13650
    assert p.stat == "S+"
    assert p.lstart == "Tue Nov 3 19:02:35 2026"  # 两个空格压成一个
    assert p.etime_sec == 2 * 3600 + 11 * 60 + 5
    assert p.args == "claude --model opus"
    assert not p.stopped


def test_ps_parse_keeps_args_spacing_and_flags_stopped():
    p = _table().get(22222)
    assert p.args == "/usr/local/bin/codex exec resume abc  hi   there"
    assert p.stopped
    assert p.etime_sec == 300


def test_ps_parse_zombie_without_args():
    p = _table().get("333")
    assert p is not None and p.args == ""


def test_ps_lookup_helpers():
    t = _table()
    assert 13706 in t and "13706" in t and 99999 not in t and "x" not in t
    assert [p.pid for p in t.children(1)] == [22222, 333]
    assert [p.pid for p in t.find(lambda p: p.args.startswith("claude"))] == [13706]
    assert t.lstart(99999) == ""


def test_etime_formats():
    assert parse_etime("00:07") == 7
    assert parse_etime("05:00") == 300
    assert parse_etime("1:02:03") == 3723
    assert parse_etime("09-15:38:17") == 9 * 86400 + 15 * 3600 + 38 * 60 + 17
    assert parse_etime("") == 0
    assert parse_etime("ab:cd") == 0


class _FakeRunner(SubprocessRunner):
    def __init__(self, result: RunResult = None, exc: Exception = None) -> None:
        self.result, self.exc, self.calls = result, exc, []

    def run(self, argv, **kw):
        self.calls.append((tuple(argv), kw))
        if self.exc:
            raise self.exc
        return self.result


def test_snapshot_uses_c_locale_and_timeout():
    r = _FakeRunner(RunResult(0, PS_SAMPLE))
    t = ProcTable.snapshot(r)
    assert len(t) == 4 and t.error == ""
    argv, kw = r.calls[0]
    assert argv[0] == "ps" and "pid=,ppid=,stat=,lstart=,etime=,args=" in argv
    assert kw["env"]["LC_ALL"] == "C"
    assert kw["timeout"] > 0


@pytest.mark.parametrize(
    "runner",
    [
        _FakeRunner(RunResult(-1, timed_out=True)),
        _FakeRunner(RunResult(-1, error="找不到 ps")),
        _FakeRunner(RunResult(1, "")),
        _FakeRunner(exc=RuntimeError("boom")),
    ],
    ids=["timeout", "spawn-fail", "exit1", "runner-raises"],
)
def test_snapshot_failure_is_empty_table_with_reason(runner):
    t = ProcTable.snapshot(runner)
    assert len(t) == 0
    assert t.error  # 有一句说明
    assert not t.same_process(13706, "Tue Nov  3 19:02:35 2026")


def test_real_ps_snapshot_smoke():
    """真 ps 只读一次：至少能看到自己。"""
    t = ProcTable.snapshot()
    if t.error:
        pytest.skip(t.error)
    assert os.getpid() in t
    me = t.get(os.getpid())
    assert t.same_process(os.getpid(), me.lstart)


# ---------- pid 复用守卫 ----------

LOCAL_LSTART = "Tue Nov  3 19:02:35 2026"


def _epoch_local() -> float:
    return parse_lstart(LOCAL_LSTART)


def test_same_process_exact_lstart_string():
    assert _table().same_process(13706, LOCAL_LSTART)
    assert _table().same_process(13706, "Tue Nov 3 19:02:35 2026")


def test_same_process_millisecond_and_second_timestamps():
    t = _table()
    ep = _epoch_local()
    assert t.same_process(13706, int(ep * 1000) + 538)  # 毫秒，带零头
    assert t.same_process(13706, str(int(ep * 1000)))  # 毫秒字符串
    assert t.same_process(13706, ep)  # 秒


def test_same_process_utc_iso():
    ep = _epoch_local()
    iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ep))
    t = _table()
    assert t.same_process(13706, iso + ".538Z")
    assert t.same_process(13706, iso + "+00:00")
    assert t.same_process(13706, iso)  # 不带时区按 UTC


def test_same_process_utc_ctime_like_claude_registry():
    """Claude 2.1.317 的 procStart 是 UTC 的 ctime 串，ps 给的是本地时间。"""
    ep = _epoch_local()
    utc_text = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(ep))
    assert _table().same_process(13706, utc_text)


@pytest.mark.parametrize(
    "proc_start",
    [
        "Tue Nov  3 19:02:50 2026",  # 差 15 秒 ⇒ 是另一个进程
        "Tue Nov  4 19:02:35 2026",
        lambda: _epoch_local() + 60,
        lambda: int((_epoch_local() - 3600) * 1000),
        "",
        None,
        "not a time",
        True,
        0,
    ],
)
def test_same_process_rejects_reused_pid(proc_start):
    value = proc_start() if callable(proc_start) else proc_start
    assert not _table().same_process(13706, value)


def test_same_process_missing_pid():
    assert not _table().same_process(99999, LOCAL_LSTART)
    assert not _table().same_process("abc", LOCAL_LSTART)


def test_parse_proc_start_shapes():
    assert parse_proc_start(1793703755538) == (1793703755.538,)
    assert parse_proc_start("1793703755") == (1793703755.0,)
    utc = parse_proc_start("Tue Nov  3 11:02:35 2026")
    assert utc[0] == calendar.timegm((2026, 11, 3, 11, 2, 35, 0, 0, 0))
    assert parse_proc_start("") == () and parse_proc_start(None) == ()
    assert parse_proc_start(-5) == ()


# ---------- 文案工具 ----------


@pytest.mark.parametrize(
    "sec,text",
    [
        (0, "刚刚"),
        (59, "刚刚"),
        (-30, "刚刚"),
        (None, "刚刚"),
        ("x", "刚刚"),
        (float("nan"), "刚刚"),
        (60, "1 分钟前"),
        (180, "3 分钟前"),
        (3599, "59 分钟前"),
        (3600, "1 小时前"),
        (7200 + 59, "2 小时前"),
        (86399, "23 小时前"),
        (86400 * 2 + 5, "2 天前"),
    ],
)
def test_humanize_age(sec, text):
    assert humanize_age(sec) == text


def test_ago():
    assert ago(0) == "" and ago(None) == ""
    assert ago(1000, now=1000 + 180) == "3 分钟前"


def test_short_title():
    assert short_title("  修一下\n 登录页  ") == "修一下 登录页"
    long = "字" * 50
    out = short_title(long, 40)
    assert len(out) == 40 and out.endswith("…")
    assert short_title("a" * 40, 40) == "a" * 40
    assert short_title(None) == ""
    assert short_title("abc", 0) == ""


# ---------- 数据结构 ----------


def test_session_defaults_fill_why_not():
    s = Session(tool="claude", surface="cli", id="x", pids=["12", "bad", 3], updated_at=-1)
    assert s.pids == (12, 3)
    assert s.updated_at == 0
    assert s.status == "unknown" and s.owner == "unknown"
    assert s.why_not_send and s.why_not_stop  # 不能做 ⇒ 必有一句原因
    assert s.key == ("claude", "x")
    assert s.age_text == ""


def test_session_can_do_clears_why_not():
    s = Session(
        tool="codex", surface="app", id="y", status="running", owner="bridge",
        can_send=True, can_stop=True, why_not_send="残留", why_not_stop="残留",
    )
    assert s.why_not_send == "" and s.why_not_stop == ""


def test_session_bad_status_degrades_bad_tool_raises():
    assert Session(tool="cursor", surface="cli", id="z", status="busy", owner="me").status == "unknown"
    with pytest.raises(ValueError):
        Session(tool="vim", surface="cli", id="z")
    with pytest.raises(ValueError):
        Session(tool="claude", surface="web", id="z")


def test_message_and_result():
    assert Message("user", "hi").at == 0
    r = ActionResult(False, "不行")
    assert r.session_id == "" and not r.ok


def test_adapter_defaults_refuse_in_chinese():
    a = Adapter(jobs=FleetJobs(lstart_of=lambda p: ""))
    assert a.list(5) == [] and a.running() == [] and a.read("x", 3) == []
    for r in (a.send("x", "hi", 10), a.stop("x")):
        assert not r.ok and r.session_id == "x"
        assert any("一" <= ch <= "鿿" for ch in r.text)
    assert a.find("x") is None
    assert not a.stop_own("x").ok


# ---------- FleetJobs ----------


class _Ps:
    """可变的假进程表：pid → lstart。"""

    def __init__(self, mapping=None):
        self.mapping = dict(mapping or {})

    def __call__(self, pid):
        return self.mapping.get(pid, "")


class _Kills:
    def __init__(self):
        self.sent = []

    def __call__(self, pid, sig):
        self.sent.append((pid, sig))


def test_jobs_unregistered_pid_is_refused_and_not_signalled():
    ps, kills = _Ps({500: LOCAL_LSTART}), _Kills()
    jobs = FleetJobs(lstart_of=ps, kill=kills)
    assert not jobs.owns(500)
    r = jobs.signal(500)
    assert not r.ok and "不是从手机上开的" in r.text
    assert kills.sent == []


def test_jobs_registered_and_unchanged_is_owned():
    ps, kills = _Ps({500: LOCAL_LSTART}), _Kills()
    jobs = FleetJobs(lstart_of=ps, kill=kills)
    assert jobs.register_spawned(500, "claude", "s1") is not None
    assert jobs.owns(500)
    assert jobs.signal(500).ok
    assert kills.sent == [(500, signal.SIGINT)]


def test_jobs_lstart_changed_is_refused():
    """进程退出、pid 被别人复用 ⇒ lstart 变了 ⇒ 绝不发信号。"""
    ps, kills = _Ps({500: LOCAL_LSTART}), _Kills()
    jobs = FleetJobs(lstart_of=ps, kill=kills)
    jobs.register(500, LOCAL_LSTART, "codex", "t1")
    ps.mapping[500] = "Wed Nov  4 08:00:00 2026"
    assert not jobs.owns(500)
    r = jobs.signal(500)
    assert not r.ok and r.session_id == "t1"
    assert kills.sent == []
    assert jobs.get(500) is None  # 顺手清掉


def test_jobs_process_gone_is_refused():
    ps, kills = _Ps({500: LOCAL_LSTART}), _Kills()
    jobs = FleetJobs(lstart_of=ps, kill=kills)
    jobs.register(500, LOCAL_LSTART, "codex", "t1")
    del ps.mapping[500]
    assert not jobs.owns(500)
    assert not jobs.signal(500).ok and kills.sent == []


def test_jobs_register_without_lstart_is_skipped():
    jobs = FleetJobs(lstart_of=_Ps(), kill=_Kills())
    assert jobs.register_spawned(500, "claude", "s1") is None
    assert jobs.register(500, "", "claude", "s1") is None
    assert jobs.register("x", LOCAL_LSTART, "claude", "s1") is None
    assert not jobs.owns(500)


def test_jobs_owns_with_snapshot_table():
    jobs = FleetJobs(lstart_of=_Ps(), kill=_Kills())
    jobs.register(13706, LOCAL_LSTART, "claude", "s1")
    assert jobs.owns(13706, _table())
    jobs.register(22222, "Tue Nov  3 20:00:00 2026", "codex", "t1")  # 与表里不同
    assert not jobs.owns(22222, _table())
    assert jobs.prune(_table()) == 1
    assert [j.pid for j in jobs.jobs()] == [13706]


def test_jobs_stop_session_only_touches_that_session():
    ps, kills = _Ps({500: LOCAL_LSTART, 501: LOCAL_LSTART, 600: LOCAL_LSTART}), _Kills()
    jobs = FleetJobs(lstart_of=ps, kill=kills)
    jobs.register_spawned(500, "claude", "s1")
    jobs.register_spawned(501, "claude", "s1")
    jobs.register_spawned(600, "claude", "s2")
    assert jobs.stop_session("claude", "s1").ok
    assert sorted(p for p, _ in kills.sent) == [500, 501]
    assert not jobs.stop_session("codex", "s1").ok  # 同 id 不同工具不算
    assert not jobs.stop_session("claude", "nope").ok


def test_jobs_kill_race_process_lookup_error():
    def kill(pid, sig):
        raise ProcessLookupError

    jobs = FleetJobs(lstart_of=_Ps({500: LOCAL_LSTART}), kill=kill)
    jobs.register_spawned(500, "claude", "s1")
    r = jobs.signal(500)
    assert not r.ok and jobs.get(500) is None


def test_jobs_thread_safe_register():
    import threading

    ps = _Ps({i: LOCAL_LSTART for i in range(1, 401)})
    jobs = FleetJobs(lstart_of=ps, kill=_Kills())

    def worker(start):
        for pid in range(start, start + 100):
            jobs.register_spawned(pid, "claude", f"s{pid}")
            jobs.owns(pid)

    threads = [threading.Thread(target=worker, args=(s,)) for s in (1, 101, 201, 301)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(jobs.jobs()) == 400


def test_runner_on_spawn_registers_before_exit(tmp_path):
    """真跑一个 python 子进程：on_spawn 时能拿到 lstart 并登记；stdin 默认是 DEVNULL。"""
    jobs = FleetJobs()
    seen = []

    def on_spawn(pid):
        seen.append(jobs.register_spawned(pid, "claude", "s1"))

    r = SubprocessRunner().run(
        [sys.executable, "-c", "import sys,time; print(repr(sys.stdin.read())); time.sleep(0.3)"],
        timeout=10,
        cwd=str(tmp_path),
        on_spawn=on_spawn,
    )
    assert r.ok and r.stdout.strip() == "''"
    assert seen and seen[0] is not None and seen[0].pid == r.pid
    assert not jobs.owns(r.pid)  # 退出后不再认


def test_runner_timeout_and_missing_binary(tmp_path):
    r = SubprocessRunner().run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.3)
    assert r.timed_out and not r.ok
    r = SubprocessRunner().run([str(tmp_path / "no-such-cli")], timeout=1)
    assert r.error and not r.ok
