"""termsend（打进终端窗口，借鉴官方 20261123 第 41 轮）单测。

只验纯逻辑，不碰真 cmux：用假的 adapter（给出 `_table` / `_registry` / `find` / `runner`）
喂进程表和注册表，`_cmux_surfaces_of_session` 用 mock 顶掉。覆盖：
- `wants_terminal`：只有斜杠命令走这条路，普通话永远 None（退回 relay）。
- `can_send` 的三态：退回（空串 why）vs 真做不到（有 why）vs 能送。
- 认终端：父进程链命中 cmux 才算，认不出一律退回。
- 两源一致闸：进程身上的 surface 不在终端程序那张表里 ⇒ 一个字不送。
- 按键白名单：只有 esc / enter。
"""

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from bridge.fleet import termsend as ts
from bridge.fleet.model import Proc, ProcTable

SID = "11111111-2222-4333-8444-555555555555"
SID2 = "99999999-8888-4777-8666-555555555555"


def _proc(pid, ppid, args, stat="S"):
    return Proc(pid=pid, ppid=ppid, stat=stat, lstart="Sun Nov 23 10:00:00 2026",
                etime_sec=10, args=args)


class FakeAdapter:
    """TermSend 只用 adapter 的这四样：_table / _registry / find / runner。"""

    def __init__(self, procs, registry, sessions=None, run_result=None):
        self._procs = list(procs)
        self._reg = list(registry)
        self._sessions = sessions or {}
        self.run_calls = []
        self._run_result = run_result or SimpleNamespace(
            returncode=0, stdout="", stderr="", timed_out=False, error="")
        self.runner = SimpleNamespace(run=self._run)

    def _table(self):
        return ProcTable(self._procs)

    def _registry(self):
        return list(self._reg)

    def find(self, sid):
        return self._sessions.get(sid)

    def _run(self, argv, timeout=None, env=None, input_text=None):
        self.run_calls.append(SimpleNamespace(argv=list(argv), env=env, input_text=input_text))
        return self._run_result


def _registry_row(sid, pid):
    # same_process 对得上：lstart 原文一致即可
    return ("user", {"sessionId": sid, "pid": pid, "procStart": "Sun Nov 23 10:00:00 2026"})


class WantsTerminalTest(unittest.TestCase):
    def test_only_slash_commands(self):
        self.assertTrue(ts.wants_terminal("/stop"))
        self.assertTrue(ts.wants_terminal("/clear 现在"))
        self.assertTrue(ts.wants_terminal("/模型"))

    def test_plain_text_never(self):
        self.assertFalse(ts.wants_terminal("hello"))
        self.assertFalse(ts.wants_terminal("帮我改一下这个函数"))
        self.assertFalse(ts.wants_terminal(""))
        self.assertFalse(ts.wants_terminal("请 /stop 一下"))  # 斜杠不在开头


class CanSendTest(unittest.TestCase):
    def _cmux_chain(self, pid=5000, ppid=4000):
        # claude(pid) ← zsh(ppid) ← cmux
        return [
            _proc(pid, ppid, "/Users/x/.local/bin/claude --session-id " + SID),
            _proc(ppid, 3000, "-zsh"),
            _proc(3000, 1, "/Applications/cmux.app/Contents/MacOS/cmux"),
        ]

    def test_bad_id_is_a_real_no(self):
        a = FakeAdapter([], [])
        can, why = ts.TermSend(a).can_send("not-a-uuid")
        self.assertFalse(can)
        self.assertEqual(why, ts.WHY_BAD_ID)

    def test_no_running_process_falls_back_silently(self):
        # 没在跑 ⇒ 退回（空串 why），让 resume 那条路去
        a = FakeAdapter([], [])
        can, why = ts.TermSend(a).can_send(SID)
        self.assertFalse(can)
        self.assertEqual(why, "")

    def test_unknown_terminal_falls_back_silently(self):
        # 认不出开在哪种窗口 ⇒ 退回（空串），不把能用的 relay 说成坏的
        procs = [_proc(5000, 4000, "claude --session-id " + SID),
                 _proc(4000, 1, "/usr/bin/login")]  # 父链没有 cmux
        a = FakeAdapter(procs, [_registry_row(SID, 5000)])
        can, why = ts.TermSend(a).can_send(SID)
        self.assertFalse(can)
        self.assertEqual(why, "")

    def test_busy_is_a_real_no_for_text(self):
        # cmux 会话但正忙 ⇒ 真做不到（有 why），不能回空串（否则 relay 会说错误的那句）
        procs = self._cmux_chain()
        sessions = {SID: SimpleNamespace(status="running")}
        a = FakeAdapter(procs, [_registry_row(SID, 5000)], sessions=sessions)
        can, why = ts.TermSend(a).can_send(SID)
        self.assertFalse(can)
        self.assertEqual(why, ts.WHY_BUSY)

    def test_busy_ignored_for_key(self):
        # 按 Esc 要停的本来就是正跑的那一轮 ⇒ 不查「它正忙」
        procs = self._cmux_chain()
        sessions = {SID: SimpleNamespace(status="running")}
        a = FakeAdapter(procs, [_registry_row(SID, 5000)], sessions=sessions)
        can, why = ts.TermSend(a).can_send(SID, for_key=True)
        self.assertTrue(can)
        self.assertEqual(why, "")

    def test_idle_cmux_can_send(self):
        procs = self._cmux_chain()
        sessions = {SID: SimpleNamespace(status="idle")}
        a = FakeAdapter(procs, [_registry_row(SID, 5000)], sessions=sessions)
        can, why = ts.TermSend(a).can_send(SID)
        self.assertTrue(can)
        self.assertEqual(why, "")


class DetectTerminalTest(unittest.TestCase):
    def test_cmux_chain_detected(self):
        procs = [_proc(5000, 4000, "claude --session-id " + SID),
                 _proc(4000, 3000, "-zsh"),
                 _proc(3000, 1, "/Applications/cmux.app/Contents/MacOS/cmux")]
        a = FakeAdapter(procs, [_registry_row(SID, 5000)])
        self.assertEqual(ts.TermSend(a).detect_terminal(SID), ts.TERM_CMUX)

    def test_claude_settings_path_is_not_cmux(self):
        # claude 自己命令行里带 ~/.cmuxterm/... 不算 —— 真证据是父进程链
        procs = [_proc(5000, 1, "claude --settings /Users/x/.cmuxterm/claude-settings/ab.json "
                                "--session-id " + SID)]
        a = FakeAdapter(procs, [_registry_row(SID, 5000)])
        self.assertEqual(ts.TermSend(a).detect_terminal(SID), ts.TERM_UNKNOWN)


class TwoSourceGateTest(unittest.TestCase):
    """进程身上的 surface 必须在终端程序那张表里，否则一个字不送。"""

    def _setup(self, env_surface, registry_surfaces):
        procs = [_proc(5000, 4000, "claude --session-id " + SID),
                 _proc(4000, 3000, "-zsh"),
                 _proc(3000, 1, "/Applications/cmux.app/Contents/MacOS/cmux")]
        a = FakeAdapter(procs, [_registry_row(SID, 5000)],
                        sessions={SID: SimpleNamespace(status="idle")})
        ts_obj = ts.TermSend(a)
        # 顶掉两个外部读取：进程环境 + 终端程序那张表
        self._env_patch = mock.patch.object(
            ts, "_process_env",
            return_value={ts._ENV_SURFACE: env_surface,
                          ts._ENV_CAPABILITY: "cap-token",
                          ts._ENV_SOCKET: "/tmp/ipc.sock"})
        self._reg_patch = mock.patch.object(
            ts, "_cmux_surfaces_of_session",
            return_value={s.upper() for s in registry_surfaces})
        self._argv_patch = mock.patch.object(
            ts, "_argv_text",
            side_effect=lambda kind, surface, text: ts._Cmd(["cmux", "paste", "--surface", surface,
                                                             "--submit", "-"], input_text=text))
        return ts_obj, a

    def test_sources_agree_sends(self):
        ts_obj, a = self._setup("surf-A", ["surf-A"])
        with self._env_patch, self._reg_patch, self._argv_patch:
            out = ts_obj.send_text(SID, "/stop")
        self.assertTrue(out.ok, out.text)
        self.assertEqual(len(a.run_calls), 1)
        # 正文走 stdin，不进 argv
        self.assertEqual(a.run_calls[0].input_text, "/stop")
        self.assertNotIn("/stop", " ".join(a.run_calls[0].argv))
        # 凭据经环境传
        self.assertEqual(a.run_calls[0].env.get(ts._ENV_CAPABILITY), "cap-token")

    def test_sources_disagree_sends_nothing(self):
        ts_obj, a = self._setup("surf-A", ["surf-B"])  # 进程说 A，终端表只有 B
        with self._env_patch, self._reg_patch, self._argv_patch:
            out = ts_obj.send_text(SID, "/stop")
        self.assertFalse(out.ok)
        self.assertEqual(a.run_calls, [])  # 一个字都没送


class SendKeyWhitelistTest(unittest.TestCase):
    def test_rejects_unknown_key(self):
        a = FakeAdapter([], [])
        out = ts.TermSend(a).send_key(SID, "ctrl-c")
        self.assertFalse(out.ok)
        self.assertEqual(out.text, ts.WHY_BAD_KEY)

    def test_allowed_keys(self):
        self.assertEqual(ts.ALLOWED_KEYS, (ts.KEY_ESC, ts.KEY_ENTER))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
