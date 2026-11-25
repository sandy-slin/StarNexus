"""Claude adapter。

全部基于 tmp_path 下的假目录 + 固定 ps 字符串 + 假 runner；**绝不起真 claude、不碰真实会话**。
重点是两道安全拒绝：
- 有进程占着的会话 ⇒ send 拒绝，而且 runner 一次都没被调（没起任何子进程）
- 不是桥起的 pid ⇒ stop 拒绝，而且 kill 一次都没被调
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import FleetJobs, ProcTable, RunResult, SubprocessRunner  # noqa: E402
from bridge.fleet import claude as claude_mod  # noqa: E402
from bridge.fleet.claude import ClaudeAdapter  # noqa: E402
from bridge.policy import Policy  # noqa: E402

SID_TERM = "11111111-1111-4111-8111-111111111111"
SID_TWO = "22222222-2222-4222-8222-222222222222"
SID_IDLE = "33333333-3333-4333-8333-333333333333"
SID_APP = "44444444-4444-4444-8444-444444444444"
SID_BRIDGE = "55555555-5555-4555-8555-555555555555"
SID_DEAD = "66666666-6666-4666-8666-666666666666"
SID_SCRIPT = "77777777-7777-4777-8777-777777777777"
SID_OLD = "88888888-8888-4888-8888-888888888888"

L_TERM = "Tue Nov  3 19:02:35 2026"
L_A = "Wed Nov  4 09:00:00 2026"
L_B = "Wed Nov  4 09:30:00 2026"
L_STOP = "Wed Nov  4 10:00:00 2026"
L_BRIDGE = "Wed Nov  4 11:00:00 2026"
L_SCRIPT = "Wed Nov  4 11:30:00 2026"
L_REUSED = "Wed Nov  4 12:00:00 2026"
L_CHILD = "Wed Nov  4 12:30:00 2026"

PS_TEXT = f"""\
  101     1 S+   {L_TERM}    02:11:05 claude --model opus
  201     1 S+   {L_A}    01:00:00 claude
  202     1 T    {L_B}    00:30:00 claude
  301     1 S    {L_BRIDGE}    00:10:00 claude -p hi
  401     1 S    {L_SCRIPT}    00:05:00 claude -p --resume x
  501     1 S    {L_REUSED}    00:01:00 /usr/bin/some-other-thing
"""


class FakeRunner(SubprocessRunner):
    """记下每次调用；ps 返回固定表，claude 返回给定结果。"""

    def __init__(self, ps_text=PS_TEXT, result=None, child_pid=900, on_run=None):
        self.ps_text = ps_text
        self.result = result or RunResult(
            returncode=0, stdout=json.dumps({"type": "result", "result": "收到", "session_id": SID_IDLE,
                                             "is_error": False}))
        self.child_pid = child_pid
        self.calls = []
        self.on_run = on_run

    def run(self, argv, *, timeout, cwd=None, env=None, input_text=None, on_spawn=None):
        argv = list(argv)
        if argv and argv[0] == "ps":
            return RunResult(returncode=0, stdout=self.ps_text)
        self.calls.append({"argv": argv, "timeout": timeout, "cwd": cwd, "env": env, "input": input_text})
        if on_spawn is not None:
            on_spawn(self.child_pid)
        if self.on_run is not None:
            self.on_run(self)
        return self.result

    @property
    def claude_calls(self):
        return self.calls


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        if isinstance(data, (dict, list)):
            json.dump(data, fh, ensure_ascii=False)
        else:
            fh.write(data)


def _jsonl(path, records, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _user(text, **kw):
    rec = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text},
           "timestamp": "2026-11-04T01:00:00.000Z"}
    rec.update(kw)
    return rec


def _asst(text, msg_id="m1", **kw):
    rec = {"type": "assistant", "isSidechain": False,
           "message": {"id": msg_id, "role": "assistant", "content": [{"type": "text", "text": text}]},
           "timestamp": "2026-11-04T01:00:05.000Z"}
    rec.update(kw)
    return rec


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "claude"
    bridge = tmp_path / "bridge"
    app = tmp_path / "Claude-3p"
    work = tmp_path / "work"
    work.mkdir()
    proj = home / "projects" / "-work"
    old = time.time() - 3600

    def reg(root, pid, sid, lstart, status="idle", entry="cli", **kw):
        rec = {"pid": pid, "sessionId": sid, "cwd": str(work), "procStart": lstart, "kind": "interactive",
               "entrypoint": entry, "status": status, "version": "2.1.317",
               "updatedAt": int(old * 1000), "statusUpdatedAt": int(old * 1000)}
        rec.update(kw)
        _write(str(root / "sessions" / f"{pid}.json"), rec)

    # 终端里开着、有名字
    reg(home, 101, SID_TERM, L_TERM, status="busy", name="修登录", nameSource="user")
    # 同一个会话开在两个终端里，其中一个被 Ctrl-Z 挂起（注册表还写着 idle）
    reg(home, 201, SID_TWO, L_A, status="shell", name="work-1a", nameSource="derived")
    reg(home, 202, SID_TWO, L_B, status="idle", name="work-1a", nameSource="derived")
    # 别的脚本起的 claude -p
    reg(home, 401, SID_SCRIPT, L_SCRIPT, status="busy", entry="sdk-cli")
    # 死进程留下的注册表；以及 pid 被复用（501 现在是别的程序）
    reg(home, 999, SID_DEAD, L_A)
    reg(home, 501, SID_DEAD, L_A)
    # 签名密钥文件：绝不能被当成注册表打开
    _write(str(home / "sessions" / "101.abcdef.key"), "SECRET-DO-NOT-READ")
    # 桥自己的飞书对话会话
    reg(bridge, 301, SID_BRIDGE, L_BRIDGE, status="busy", entry="sdk-cli")

    _jsonl(str(proj / f"{SID_TERM}.jsonl"), [
        {"type": "ai-title", "aiTitle": "旧标题", "sessionId": SID_TERM},
        _user("帮我修登录", cwd=str(work), entrypoint="cli"),
        _asst("好的"),
    ], mtime=old)
    _jsonl(str(proj / f"{SID_TWO}.jsonl"), [
        {"type": "custom-title", "customTitle": "两个终端", "sessionId": SID_TWO},
        _user("hi", cwd=str(work), entrypoint="cli"),
    ], mtime=old)
    _jsonl(str(proj / f"{SID_IDLE}.jsonl"), [
        {"type": "mode", "mode": "normal"},
        _user("<local-command-caveat>Caveat: ...</local-command-caveat>", isMeta=True, cwd=str(work),
              entrypoint="cli"),
        _user("把 README 的安装步骤写清楚一点", cwd=str(work)),
        _asst("改好了"),
    ], mtime=old - 10)
    _jsonl(str(proj / f"{SID_APP}.jsonl"), [
        _user("App 里问的", cwd=str(work), entrypoint="claude-desktop-3p"),
    ], mtime=old - 20)
    _jsonl(str(proj / f"{SID_OLD}.jsonl"), [_user("很久以前", cwd=str(work), entrypoint="cli")],
           mtime=old - 86400 * 3)
    _jsonl(str(bridge / "projects" / "-work" / f"{SID_BRIDGE}.jsonl"), [
        _user("飞书里问的", cwd=str(work), entrypoint="sdk-cli"),
    ], mtime=old)
    # 子 agent 的 jsonl 在子目录里，不该被列出来
    _jsonl(str(proj / SID_IDLE / "subagents" / "agent-x.jsonl"), [_user("sub")])

    _write(str(app / "claude-code-sessions" / "acct" / "org" / "local_a.json"),
           {"cliSessionId": SID_APP, "title": "App 标题", "isArchived": False, "cwd": str(work),
            "lastActivityAt": int(old * 1000), "postTurnSummary": {"status_category": "review_ready"}})
    _write(str(app / "claude-code-sessions" / "acct" / "org" / "local_b.json"),
           {"cliSessionId": SID_OLD, "title": "归档的", "isArchived": True})

    policy = Policy(mode="readonly", workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                    model="claude-sonnet-5-5", turn_timeout_sec=60)
    return {"home": home, "bridge": bridge, "app": app, "work": work, "proj": proj, "policy": policy,
            "tmp": tmp_path}


def _adapter(env, runner=None, jobs=None, kills=None, ps_text=None, policy="default"):
    runner = runner or FakeRunner(ps_text=ps_text or PS_TEXT)
    table_text = ps_text or runner.ps_text
    kills = kills if kills is not None else []
    if jobs is None:
        lookup = {"900": L_CHILD}
        jobs = FleetJobs(
            lstart_of=lambda pid: lookup.get(str(pid), ProcTable.parse(table_text).lstart(pid)),
            kill=lambda pid, sig: kills.append((pid, sig)),
        )
    return ClaudeAdapter(
        home=str(env["home"]), bridge_home=str(env["bridge"]), app_root=str(env["app"]),
        jobs=jobs, runner=runner, policy=env["policy"] if policy == "default" else policy,
    ), runner, jobs, kills


# ---------- running ----------


def test_running_merges_pids_and_maps_status(env):
    ad, *_ = _adapter(env)
    live = {s.id: s for s in ad.running()}
    assert set(live) == {SID_TERM, SID_TWO, SID_SCRIPT, SID_BRIDGE}  # 死的、pid 复用的都不算
    term = live[SID_TERM]
    assert term.status == "running" and term.owner == "terminal" and term.surface == "cli"
    assert term.pids == (101,)
    assert term.title == "修登录"  # 注册表里用户起的名字
    two = live[SID_TWO]
    assert sorted(two.pids) == [201, 202]
    assert two.extra["copies"] == 2 and two.extra["shell"] is True
    assert two.extra["pids_status"]["202"] == "idle"
    assert two.status == "idle"  # shell→idle；挂起的那个排在后面
    assert two.title == "两个终端"  # derived 名字没信息量 ⇒ 用 jsonl 里的标题
    assert live[SID_SCRIPT].owner == "unknown"
    assert live[SID_BRIDGE].owner == "bridge" and live[SID_BRIDGE].surface == "bridge"
    assert ad.last_error == ""


def test_running_stopped_when_all_pids_suspended(env):
    ps = PS_TEXT.replace(f"  201     1 S+   {L_A}", f"  201     1 T    {L_A}")
    ad, *_ = _adapter(env, ps_text=ps)
    two = {s.id: s for s in ad.running()}[SID_TWO]
    assert two.status == "stopped"


def test_running_accepts_utc_ctime_procstart(env):
    # Claude 2.1.317 实测：procStart 是 UTC 的 ctime 串，ps lstart 是本地时间
    local = time.mktime(time.strptime(L_A, "%a %b %d %H:%M:%S %Y"))
    utc_ctime = time.strftime("%a %b %d %H:%M:%S %Y", time.gmtime(local))
    rec = json.load(open(env["home"] / "sessions" / "201.json"))
    rec["procStart"] = utc_ctime
    _write(str(env["home"] / "sessions" / "201.json"), rec)
    ad, *_ = _adapter(env)
    assert 201 in {s.id: s for s in ad.running()}[SID_TWO].pids


def test_running_never_opens_key_files(env, monkeypatch):
    opened = []
    real = claude_mod._read_json
    monkeypatch.setattr(claude_mod, "_read_json", lambda p: (opened.append(p), real(p))[1])
    ad, *_ = _adapter(env)
    ad.running()
    assert opened and not any(p.endswith(".key") for p in opened)


def test_running_ps_failure_returns_empty_with_reason(env):
    class Broken(FakeRunner):
        def run(self, argv, **kw):
            return RunResult(returncode=-1, error="没有 ps")

    ad, *_ = _adapter(env, runner=Broken())
    assert ad.running() == []
    assert "ps" in ad.last_error


def test_running_includes_own_child_before_registry(env):
    ps = PS_TEXT + f"  900     1 S    {L_CHILD}    00:00:05 claude -p --resume {SID_IDLE}\n"
    ad, _, jobs, _ = _adapter(env, ps_text=ps)
    jobs.register(900, L_CHILD, "claude", SID_IDLE)
    s = {x.id: x for x in ad.running()}[SID_IDLE]
    assert s.owner == "bridge" and s.status == "running" and s.can_stop and not s.can_send


# ---------- list ----------


def test_list_live_first_then_recent_history(env):
    ad, *_ = _adapter(env)
    items = ad.list(10)
    ids = [s.id for s in items]
    assert set(ids[:4]) == {SID_TERM, SID_TWO, SID_SCRIPT, SID_BRIDGE}
    assert ids[4:] == [SID_IDLE, SID_APP]  # 归档的 SID_OLD 不列；子 agent 不列
    idle = items[4]
    assert idle.owner == "none" and idle.status == "idle" and idle.can_send
    assert idle.title == "把 README 的安装步骤写清楚一点"  # 跳过 isMeta 那行
    assert idle.cwd == str(env["work"])
    app = items[5]
    # 第 14 轮：App 里关着的会话也能后台接着跑（主人拍板「能，后台接着跑」）
    assert app.surface == "app" and app.title == "App 标题" and app.can_send
    assert app.why_not_send == ""
    assert app.extra["app_status"] == "review_ready"


def test_list_limit_and_live_cannot_send(env):
    ad, *_ = _adapter(env)
    # 🔴 在跑的全部列出，limit 只管历史（原来 live[:limit] ⇒ 本机 18 个在跑只显示 6 个）
    live_ids = {s.id for s in ad.running()}
    listed = ad.list(1)
    assert live_ids <= {s.id for s in listed}, "在跑的会话被 limit 截掉了"
    assert len([s for s in listed if s.id not in live_ids]) <= 1, "历史会话超过了 limit"
    term = next(s for s in ad.list(10) if s.id == SID_TERM)
    assert not term.can_send and "分叉" in term.why_not_send
    assert not term.can_stop and "终端" in term.why_not_stop


def test_list_tolerates_missing_dirs(tmp_path):
    ad = ClaudeAdapter(home=str(tmp_path / "x"), bridge_home=str(tmp_path / "y"), app_root=str(tmp_path / "z"),
                       runner=FakeRunner(ps_text=""), jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None))
    assert ad.list(5) == [] and ad.running() == []
    assert ad.read(SID_IDLE) == []


def test_list_without_policy_cannot_send(env):
    ad, *_ = _adapter(env, policy=None)
    idle = next(s for s in ad.list(10) if s.id == SID_IDLE)
    assert not idle.can_send


def test_list_cwd_outside_workspaces_cannot_send(env):
    other = Policy(mode="readonly", workspaces=("/nonexistent-ws",), default_workspace="/nonexistent-ws",
                   model="claude-sonnet-5-5", turn_timeout_sec=60)
    ad, *_ = _adapter(env, policy=other)
    idle = next(s for s in ad.list(10) if s.id == SID_IDLE)
    assert not idle.can_send and "目录" in idle.why_not_send
    # 手机用户改不了 config.toml，提示里不该出现配置术语（2026-11-04 体验审查）
    assert "config.toml" not in idle.why_not_send and "workspaces" not in idle.why_not_send


# ---------- read ----------


def test_read_filters_noise_and_shortens_wrappers(env):
    path = env["proj"] / f"{SID_IDLE}.jsonl"
    _jsonl(str(path), [
        _user("第一句"),
        _asst("思考中", msg_id="a"),
        {"type": "assistant", "isSidechain": False, "message": {"id": "a", "content": [
            {"type": "tool_use", "name": "Read", "input": {}}]}},
        _asst("接着说", msg_id="a"),  # 同一条回复拆成多行（text / tool_use / text）
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "x" * 100}]}},
        {"type": "attachment"}, {"type": "api-request"}, {"type": "queue-operation"},
        {"type": "system", "subtype": "stop_hook_summary"},
        _user("<task-notification>done</task-notification>"),
        _user("侧链", isSidechain=True),
        _user("Another Claude session sent a message:\n<cross-session-message from=\"uds:/tmp/x.sock\" "
              "from-name=\"peer-a\" from-mode=\"prompting\">\n帮我看下日志\n</cross-session-message>", isMeta=True),
        _user("[Request interrupted by user]"),
        _user("<command-name>/model</command-name>\n<command-args>opus</command-args>"),
        _asst("最后", msg_id="b"),
    ])
    msgs = ad_read(env, SID_IDLE, 10)
    assert [(m.role, m.text) for m in msgs] == [
        ("user", "第一句"),
        ("assistant", "思考中\n接着说"),
        ("user", "〔另一个会话 peer-a发来〕帮我看下日志"),
        ("user", "（打断了）"),
        ("user", "/model opus"),
        ("assistant", "最后"),
    ]
    assert ad_read(env, SID_IDLE, 2)[-1].text == "最后"
    assert msgs[0].at > 0


def ad_read(env, sid, n):
    ad, *_ = _adapter(env)
    return ad.read(sid, n)


def test_read_tail_drops_partial_first_line(env, monkeypatch):
    monkeypatch.setattr(claude_mod, "TAIL_BYTES", 200)
    monkeypatch.setattr(claude_mod, "TAIL_BYTES_WIDE", 200)
    path = env["proj"] / f"{SID_IDLE}.jsonl"
    _jsonl(str(path), [_user("很长的开头" * 50), _asst("尾巴")])
    msgs = ad_read(env, SID_IDLE, 5)
    assert [m.text for m in msgs] == ["尾巴"]


def test_read_widens_when_window_too_small(env, monkeypatch):
    monkeypatch.setattr(claude_mod, "TAIL_BYTES", 300)
    path = env["proj"] / f"{SID_IDLE}.jsonl"
    _jsonl(str(path), [_user("早先的问题"), {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "x" * 2000}]}}, _asst("回答")])
    msgs = ad_read(env, SID_IDLE, 2)
    assert [m.text for m in msgs] == ["早先的问题", "回答"]


def test_read_bridge_session_and_unknown_id(env):
    assert [m.text for m in ad_read(env, SID_BRIDGE, 3)] == ["飞书里问的"]
    ad, *_ = _adapter(env)
    assert ad.read("../../etc/passwd", 3) == [] and ad.last_error
    assert ad.read("99999999-9999-4999-8999-999999999999", 3) == []


# ---------- send：安全拒绝 ----------


@pytest.mark.parametrize("sid,needle", [
    (SID_TERM, "终端"),
    (SID_TWO, "终端"),
    (SID_SCRIPT, "分叉"),
    (SID_BRIDGE, "飞书"),
])
def test_send_refuses_held_or_app_without_spawning(env, sid, needle):
    ad, runner, *_ = _adapter(env)
    res = ad.send(sid, "你好")
    assert not res.ok and needle in res.text
    assert runner.claude_calls == []  # 一个子进程都没起


def test_closed_app_session_resumes_in_background(env):
    """第 14 轮：App 里关着的会话 ⇒ 桥起 claude -p --resume <它的 id> 接着跑（不再拒）。"""
    ad, runner, *_ = _adapter(env)
    res = ad.send(SID_APP, "你好")
    assert res.ok
    assert len(runner.claude_calls) == 1
    argv = runner.claude_calls[0]["argv"]
    assert argv[argv.index("--resume") + 1] == SID_APP


def test_send_rechecks_right_before_spawn(env):
    # 列表时它还空着（能发）；用户随后在终端里打开了它 ⇒ 发之前现查要拦住
    ad, runner, *_ = _adapter(env)
    assert next(s for s in ad.list(10) if s.id == SID_IDLE).can_send
    rec = {"pid": 202, "sessionId": SID_IDLE, "procStart": L_B, "entrypoint": "cli", "status": "idle",
           "cwd": str(env["work"])}
    _write(str(env["home"] / "sessions" / "202.json"), rec)
    res = ad.send(SID_IDLE, "你好")
    assert not res.ok and "终端" in res.text
    assert runner.claude_calls == []


def test_send_refuses_when_ps_unreadable(env):
    class NoPs(FakeRunner):
        def run(self, argv, **kw):
            if argv[0] == "ps":
                return RunResult(returncode=-1, timed_out=True)
            return super().run(argv, **kw)

    runner = NoPs()
    ad, *_ = _adapter(env, runner=runner)
    res = ad.send(SID_IDLE, "你好")
    assert not res.ok and runner.claude_calls == []


def test_send_refuses_recently_written(env):
    os.utime(env["proj"] / f"{SID_IDLE}.jsonl", None)  # 刚刚有人写过
    ad, runner, *_ = _adapter(env)
    res = ad.send(SID_IDLE, "你好")
    assert not res.ok and "刚刚" in res.text and runner.claude_calls == []


def test_send_refuses_without_policy_and_bad_input(env):
    ad, runner, *_ = _adapter(env, policy=None)
    assert not ad.send(SID_IDLE, "你好").ok
    assert not ad.send("not-a-uuid", "你好").ok
    assert not ad.send(SID_IDLE, "   ").ok
    assert runner.claude_calls == []


def test_send_one_at_a_time_per_session(env):
    ad, *_ = _adapter(env)
    inner = []

    def reenter(runner):
        inner.append(ad.send(SID_IDLE, "第二条"))

    ad.runner.on_run = reenter
    res = ad.send(SID_IDLE, "第一条")
    assert res.ok
    assert len(ad.runner.claude_calls) == 1
    assert not inner[0].ok and "上一条" in inner[0].text


# ---------- send：能发时 ----------


def test_send_idle_runs_resume_with_user_config_and_policy(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/should/not/leak")
    seen_jobs = []
    ad, runner, jobs, _ = _adapter(env)
    runner.on_run = lambda r: seen_jobs.append([j.pid for j in jobs.jobs("claude", SID_IDLE)])
    res = ad.send(SID_IDLE, "-继续", timeout=42)
    assert res.ok and res.text == "收到" and res.session_id == SID_IDLE
    call = runner.claude_calls[0]
    argv = call["argv"]
    assert argv[:3] == ["claude", "-p", " -继续"]  # 以 - 开头的话垫了空格，不会被当成参数
    assert argv[argv.index("--resume") + 1] == SID_IDLE
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    allowed = [argv[i + 1] for i, a in enumerate(argv) if a == "--allowed-tools"]
    denied = [argv[i + 1] for i, a in enumerate(argv) if a == "--disallowed-tools"]
    assert allowed == list(env["policy"].allowed_tools())
    assert denied == list(env["policy"].disallowed_tools())
    assert "--fork-session" not in argv and "--dangerously-skip-permissions" not in argv
    assert call["cwd"] == str(env["work"]) and call["timeout"] == 42
    assert call["input"] is None  # stdin=DEVNULL
    # 测试的 home 不是 ~/.claude ⇒ 显式指过去；绝不是外面漏进来的那个
    assert call["env"]["CLAUDE_CONFIG_DIR"] == str(env["home"])
    assert seen_jobs == [[900]]  # 跑的时候登记着（能被停）
    assert jobs.jobs("claude") == []  # 跑完清掉


def test_send_env_drops_bridge_config_dir_for_real_home(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/bridge/dir")
    ad = ClaudeAdapter(runner=FakeRunner(), jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None))
    assert "CLAUDE_CONFIG_DIR" not in ad._send_env()


def test_send_reports_errors_plainly(env):
    for result, needle in [
        (RunResult(returncode=1, stdout=json.dumps({"result": "API Error", "is_error": True})), "报错"),
        (RunResult(returncode=-9, timed_out=True), "等太久"),
        (RunResult(returncode=-1, error="找不到 claude"), "没能启动"),
        (RunResult(returncode=0, stdout="not json", stderr=""), "没给出回复"),
    ]:
        ad, *_ = _adapter(env, runner=FakeRunner(result=result))
        res = ad.send(SID_IDLE, "你好")
        assert not res.ok and needle in res.text


def test_send_flags_unexpected_new_session(env):
    other = "99999999-9999-4999-8999-999999999999"
    runner = FakeRunner(result=RunResult(returncode=0, stdout=json.dumps({"result": "嗯", "session_id": other})))
    ad, *_ = _adapter(env, runner=runner)
    res = ad.send(SID_IDLE, "你好")
    assert res.ok and res.session_id == other and "新会话" in res.text


def test_own_write_does_not_block_next_send(env):
    ad, runner, *_ = _adapter(env)

    def touch(_r):
        os.utime(env["proj"] / f"{SID_IDLE}.jsonl", None)  # claude -p 往 jsonl 里写了回复

    runner.on_run = touch
    assert ad.send(SID_IDLE, "一").ok
    runner.on_run = None
    assert ad.send(SID_IDLE, "二").ok
    assert len(runner.claude_calls) == 2


# ---------- stop ----------


def test_stop_refuses_foreign_pids_without_kill(env):
    ad, _, _, kills = _adapter(env)
    for sid in (SID_TERM, SID_TWO, SID_SCRIPT):
        res = ad.stop(sid)
        assert not res.ok and "终端" in res.text
    assert not ad.stop(SID_BRIDGE).ok
    assert not ad.stop(SID_IDLE).ok
    assert kills == []


def test_stop_refuses_registered_but_reused_pid(env):
    ad, _, jobs, kills = _adapter(env)
    jobs.register(501, "Mon Jan  1 00:00:00 2026", "claude", SID_IDLE)  # 登记时的进程早没了，pid 被复用
    res = ad.stop(SID_IDLE)
    assert not res.ok and kills == []


def test_stop_own_child_sends_sigint(env):
    ps = PS_TEXT + f"  900     1 S    {L_CHILD}    00:00:05 claude -p\n"
    ad, _, jobs, kills = _adapter(env, ps_text=ps)
    jobs.register(900, L_CHILD, "claude", SID_IDLE)
    res = ad.stop(SID_IDLE)
    assert res.ok and kills == [(900, signal.SIGINT)]


# ---------- 注册表空窗（2026-11-04 安全审查） ----------


def _fresh_claude_ps(args, etime="00:01"):
    from bridge.fleet.model import ProcTable

    line = f"99999 1 S Tue Nov  4 10:00:00 2026 {etime} {args}"
    return ProcTable([ProcTable.parse_line(line)])


def test_unregistered_claude_resuming_this_id_blocks_send(env):
    """终端刚敲 `claude -r <id>`、还没写注册表 —— 原来判「没人占着」就起 -p --resume ⇒ 分叉。"""
    ad, *_ = _adapter(env)
    table = _fresh_claude_ps(f"claude -r {SID_IDLE}", etime="05:00")
    assert ad._unregistered_claude_near(table, SID_IDLE)


def test_young_unregistered_claude_blocks_send(env):
    """不带 id 的 `claude` 刚起 1 秒（可能马上在 picker 里选中这个会话）也拦。"""
    ad, *_ = _adapter(env)
    assert ad._unregistered_claude_near(_fresh_claude_ps("claude"), SID_IDLE)


def test_old_unrelated_claude_does_not_block(env):
    """阳性对照：跑了很久、命令行里没这个 id 的 claude 不该拦 —— 否则手机永远发不了。"""
    ad, *_ = _adapter(env)
    assert not ad._unregistered_claude_near(_fresh_claude_ps("claude --model haiku", etime="02:00:00"), SID_IDLE)


def test_non_claude_process_with_id_does_not_block(env):
    ad, *_ = _adapter(env)
    assert not ad._unregistered_claude_near(_fresh_claude_ps(f"tail -f /x/{SID_IDLE}.jsonl"), SID_IDLE)


@pytest.mark.parametrize("extra_line", [
    # 终端里刚敲 `claude -r <id>`，已经在 ps 里，注册表还没写
    f"  777     1 S+   Wed Nov  4 12:59:59 2026    05:00 claude -r {SID_IDLE}",
    # 刚起 1 秒、不带 id 的 claude（可能马上在选择器里选中这个会话）
    "  778     1 S+   Wed Nov  4 12:59:59 2026    00:01 claude",
])
def test_send_refused_during_registry_gap_without_spawning(env, extra_line):
    """🔴 走**真的 send()**，不只测 helper —— 去掉 _send_locked 里那一行调用，这条必须红。"""
    runner = FakeRunner(ps_text=PS_TEXT + extra_line + "\n")
    ad, *_ = _adapter(env, runner=runner)
    res = ad.send(SID_IDLE, "继续")
    assert not res.ok and "刚打开" in res.text, res.text
    assert runner.calls == [], "注册表空窗里照样起了 -p --resume ⇒ 会分叉"
