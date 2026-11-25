"""第 38 轮 · 11-17 Windows 实机报错上报：

- 指纹 c72d89e0ffb8（程序=ps，model.py ProcTable.snapshot）/ faf1402711e3（程序=lsof，codex.py _holders）/
  c0141181d01e（friendly_error 包同一错）：Windows 上去起 ps / lsof ⇒ FileNotFoundError，每轮都报 ERROR。
  修法：Windows 上不起这两个命令。进程表走 kernel32（ctypes），查不了就安静返回「看不清」；lsof 那两处直接「看不清」。
- 指纹 7d634b74cd7d（FrozenInstanceError: cannot assign to field 'can_send'，claude.py list()）：ps 失败时对 frozen 的
  Session 直接赋值 ⇒ 抛出来被 except 吞掉 ⇒ 那条历史会话从列表里消失。修法：dataclasses.replace。
- Windows 版 Claude 注册表写 procStartFt（不是 procStart）：判活要认它（按 FILETIME 换算，⚠️ 推断，没见过真值）。

模拟 Windows：把 model._IS_WINDOWS 设成 True（on_windows() 每次现读），kernel32 那几个函数换成桩 —— ctypes 本身没在真 Windows 上跑过。
"""
import dataclasses
import logging
import time

import pytest

from bridge.fleet import claude as C
from bridge.fleet import codex as X
from bridge.fleet import cursor as U
from bridge.fleet import model as M
from bridge.fleet.model import FleetJobs, Proc, ProcTable, RunResult, SubprocessRunner
from tests.test_fleet_claude import _adapter, env  # noqa: F401  (env 是 fixture)
from tests.test_fleet_claude import SID_IDLE


class _NoPs(SubprocessRunner):
    """ps / lsof 一律起不来（和 Windows 上一样的 FileNotFoundError，走真 SubprocessRunner 的报错路径）。"""

    def __init__(self):
        self.argv0 = []

    def run(self, argv, **kw):
        self.argv0.append(argv[0])
        return SubprocessRunner.run(self, ["/nonexistent/" + str(argv[0]).rsplit("/", 1)[-1]], **kw)


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(M, "_IS_WINDOWS", True)
    monkeypatch.setattr(M, "_win_failure_noted", False)
    return monkeypatch


def _errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


# ---------- ps ----------

def test_windows_snapshot_never_runs_ps(win, caplog):
    now = time.time()
    win.setattr(M, "_win_procs", lambda: [Proc(4242, 1, "", M.format_lstart(now - 60), 60, "claude.exe")])
    runner = _NoPs()
    caplog.set_level(logging.WARNING)
    table = ProcTable.snapshot(runner)
    assert runner.argv0 == [] and not table.error and table.alive(4242), table.error
    assert not _errors(caplog)


def test_windows_snapshot_failure_is_quiet_unknown(win, caplog):
    def boom():
        raise OSError(5, "Access is denied")
    win.setattr(M, "_win_procs", boom)
    caplog.set_level(logging.WARNING)
    for _ in range(5):   # 每 10 秒一轮：不许每轮都报
        table = ProcTable.snapshot(_NoPs())
        assert table.error and len(table) == 0
    assert not _errors(caplog), [r.getMessage() for r in _errors(caplog)]
    assert len([r for r in caplog.records if "Windows 上读进程列表失败" in r.getMessage()]) == 1


def test_positive_control_mac_path_still_runs_ps_and_logs(caplog):
    """阳性对照：不在 Windows ⇒ 照旧去起 ps；起不来 ⇒ 照旧 ERROR（= 11-17 那几条指纹的形状）。"""
    runner = _NoPs()
    caplog.set_level(logging.WARNING)
    table = ProcTable.snapshot(runner)
    assert runner.argv0 == ["ps"] and table.error
    assert any("程序=ps" in r.getMessage() for r in _errors(caplog))


def test_windows_lstart_of_does_not_run_ps(win):
    calls = []
    win.setattr(M.subprocess, "run", lambda *a, **k: calls.append(a) or None)
    win.setattr(M, "_win_lstart_of", lambda pid: "Tue Nov 17 14:33:00 2026" if pid == 7 else "")
    assert M._ps_lstart_of(7) == "Tue Nov 17 14:33:00 2026" and M._ps_lstart_of(8) == ""
    win.setattr(M, "_win_lstart_of", lambda pid: (_ for _ in ()).throw(OSError("x")))
    assert M._ps_lstart_of(7) == ""
    assert calls == []


def test_format_lstart_matches_ps_shape():
    """Windows 上自己拼的 lstart 要和 ps 压空白后一个字不差，FleetJobs.owns 才比得上。"""
    stamp = time.mktime((2026, 11, 3, 19, 2, 35, 0, 0, -1))
    assert M.format_lstart(stamp) == "Tue Nov 3 19:02:35 2026"
    assert M.parse_lstart(M.format_lstart(stamp)) == stamp


# ---------- procStartFt ----------

def test_proc_start_ft_matches_live_process():
    start = time.mktime((2026, 11, 17, 14, 33, 0, 0, 0, -1))
    ft = str(int((start + 11644473600) * 10**7))
    table = ProcTable([Proc(4242, 1, "", M.format_lstart(start), 60, "claude.exe")])
    assert table.same_process(4242, (None, ft, None))
    # 对不上（pid 被复用）⇒ False；三种都空 ⇒ False
    assert not table.same_process(4242, (None, str(int(ft) + 10**8), None))
    assert not table.same_process(4242, (None, None, None))
    # 阳性对照：原来只认 procStart / startedAt ⇒ Windows 注册表那条一律判死
    assert not table.same_process(4242, None)


def test_claude_registry_with_proc_start_ft_is_live(env, win):  # noqa: F811
    start = time.time() - 120
    ft = int((int(start) + 11644473600) * 10**7)
    C_reg = env["home"] / "sessions" / "4242.json"
    import json
    C_reg.write_text(json.dumps({"pid": 4242, "sessionId": SID_IDLE, "cwd": str(env["work"]), "procStartFt": str(ft),
                                 "kind": "interactive", "entrypoint": "cli", "status": "idle"}), encoding="utf-8")
    win.setattr(M, "_win_procs", lambda: [Proc(4242, 1, "", M.format_lstart(int(start)), 120, "claude.exe")])
    ad, *_ = _adapter(env)
    live = {s.id: s for s in ad.running()}
    assert SID_IDLE in live and live[SID_IDLE].owner == "terminal", (live, ad.last_error)


# ---------- lsof ----------

def test_codex_holders_skip_lsof_on_windows(tmp_path, win, caplog):
    tid = "019a0000-0000-7000-8000-000000000001"
    locks = tmp_path / "codex" / "sessions" / "locks"
    ad = X.CodexAdapter(home=str(tmp_path / "codex"), runner=_NoPs(),
                        jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None))
    win.setattr(type(ad), "locks_dir", property(lambda self: str(locks)), raising=False)
    locks.mkdir(parents=True)
    (locks / f"{tid}.lock").write_text("")
    caplog.set_level(logging.WARNING)
    held, err = ad._holders()
    assert held == {tid: None} and err == "", (held, err)
    assert ad.runner.argv0 == [] and not _errors(caplog)


def test_codex_holders_positive_control_mac_runs_lsof(tmp_path, caplog):
    tid = "019a0000-0000-7000-8000-000000000001"
    locks = tmp_path / "locks"
    locks.mkdir()
    (locks / f"{tid}.lock").write_text("")
    ad = X.CodexAdapter(home=str(tmp_path / "codex"), runner=_NoPs(), lsof_bin="lsof",
                        jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None))
    type(ad).locks_dir  # noqa: B018 —— 确认是属性
    orig = type(ad).locks_dir
    try:
        type(ad).locks_dir = property(lambda self: str(locks))
        caplog.set_level(logging.WARNING)
        held, err = ad._holders()
    finally:
        type(ad).locks_dir = orig
    assert held == {tid: None} and err and ad.runner.argv0 == ["lsof"]
    assert any("程序=lsof" in r.getMessage() for r in _errors(caplog))


def test_cursor_lsof_cwds_skip_on_windows(win, caplog):
    runner = _NoPs()
    ad = U.CursorAdapter(runner=runner, procs=lambda: ProcTable(),
                         jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None))
    caplog.set_level(logging.WARNING)
    assert ad._lsof_cwds([123]) is None
    assert runner.argv0 == [] and not _errors(caplog)


# ---------- FrozenInstanceError ----------

def _ps_broken_runner():
    from tests.test_fleet_claude import PS_TEXT

    class R(SubprocessRunner):
        ps_text = PS_TEXT   # _adapter 用它给 FleetJobs 造 lstart 表；ps 本身照样起不来

        def run(self, argv, **kw):
            if argv and argv[0] == "ps":
                return RunResult(returncode=-1, error="没能启动 ps：系统找不到这个程序")
            return RunResult(returncode=0, stdout="")
    return R()


def test_ps_failure_keeps_history_session_not_sendable(env, caplog):  # noqa: F811
    """ps 读不到 ⇒ 历史会话照样列出来，只是不能发（原因 = 看不出谁占着）。原来这里抛 FrozenInstanceError，会话直接消失。"""
    ad, *_ = _adapter(env, runner=_ps_broken_runner())
    caplog.set_level(logging.ERROR)
    sessions = {s.id: s for s in ad.list(20)}
    assert SID_IDLE in sessions, (list(sessions), ad.last_error)
    s = sessions[SID_IDLE]
    assert s.can_send is False and s.why_not_send == C.WHY_SEND_UNKNOWN, s
    assert not [r for r in caplog.records if "FrozenInstanceError" in (r.exc_text or "") + r.getMessage()]


def test_positive_control_same_session_is_sendable_when_ps_works(env):  # noqa: F811
    """阳性对照：ps 正常时同一条历史会话 can_send=True ⇒ 上面那条确实走到了「改成不能发」那一步。"""
    ad, *_ = _adapter(env)
    s = {x.id: x for x in ad.list(20)}[SID_IDLE]
    assert s.can_send is True, s


def test_session_is_frozen():
    """前提：Session 真是 frozen（不是的话上面那个修法没意义，也测不出原来的 bug）。"""
    sess = M.Session(tool="claude", surface="cli", id=SID_IDLE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        sess.can_send = False  # type: ignore[misc]
