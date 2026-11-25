"""第 32 轮：桥被重启打断的那一轮，重启后用同一个 claude 会话接着做（照 T3 的 RestartContinuation）。

真跑 `_run_locally` / `_run_one` / `_resume_after_restart`，只把 claude 换掉；看盘上的 `inflight.json`、
发给 claude 的是什么、聊天里说了什么。`on_session` 那条用假 claude 脚本真起子进程。
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from test_interference import SCOPE, _fake_bridge, _put_pending  # noqa: E402

from bridge import inflight as inflight_mod  # noqa: E402
from bridge.inflight import CONTINUE_PROMPT  # noqa: E402
from bridge.runner import TurnResult  # noqa: E402

ORIGINAL = "帮我把 build 目录清掉再重新打包一次"


def _bridge(tmp, runner=None):
    b = _fake_bridge(tmp)
    b.settings.config_path = os.path.join(tmp, "config.toml")
    b.sent, b.ran = [], []
    b._say = lambda chat_id, text: b.sent.append(text) or True
    b._say_with_buttons = lambda chat_id, text, buttons: b.sent.append(text) or True
    b._send_buttons = lambda chat_id, buttons: True
    b._chat_for = lambda scope: "chat1"
    b.workspaces = types.SimpleNamespace(get=lambda scope: "/tmp")
    from bridge.review import Reviewer

    b.reviewer = Reviewer(custom_rules=())
    b.settings.progress_first_notice_sec = 90
    b.settings.progress_escalate_sec = 420
    b.settings.streaming = False

    class Runner:
        async def run_turn(self, text, workspace, *, resume=None, on_session=None, **_):
            b.ran.append((text, resume))
            if runner is not None:
                return await runner(b, text, resume, on_session)
            if on_session:
                on_session("s-new")
            return TurnResult(ok=True, text="好的", session_id="s-new")

    b.runner = Runner()
    return b


def _disk(tmp) -> dict:
    path = os.path.join(tmp, inflight_mod.INFLIGHT_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _leftover(tmp, **over) -> None:
    """造一条「上次被杀时留下的」记录。"""
    now = time.time()
    entry = {"scope": SCOPE, "chat_id": "chat1", "claude_session_id": "s-old",
             "workspace": "/tmp", "text": ORIGINAL, "started_at": now - 60,
             "continue_of": 0, "chain_started_at": now - 60}
    entry.update(over)
    with open(os.path.join(tmp, inflight_mod.INFLIGHT_FILE), "w", encoding="utf-8") as handle:
        json.dump({SCOPE: entry}, handle, ensure_ascii=False)


async def _hang_until_killed(b, text, resume, on_session):
    """claude 跑到一半进程被杀：拿到会话 id 之后一直不回，外面把这一轮 cancel 掉。"""
    if on_session:
        on_session("s-live")
    await asyncio.sleep(3600)


def _kill_mid_turn(b, text="x"):
    """跑一轮、在 claude 跑到一半时把它 cancel 掉（= 进程退出时事件循环拆掉这个任务）。"""
    async def go():
        task = asyncio.ensure_future(b._run_locally(SCOPE, "chat1", text))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


# ---------- 开跑写、跑完删 ----------


def test_written_at_start_with_session_id_and_deleted_after():
    with tempfile.TemporaryDirectory() as tmp:
        seen = {}

        async def runner(b, text, resume, on_session):
            on_session("s-mid")
            seen.update(_disk(tmp))           # claude 还在跑时盘上的样子
            return TurnResult(ok=True, text="好的", session_id="s-mid")

        b = _bridge(tmp, runner)
        asyncio.run(b._run_locally(SCOPE, "chat1", "token sk-ant-api03-" + "A" * 40 + " 看看"))
        entry = seen.get(SCOPE)
        assert entry, f"开跑没写在跑记录：{seen}"
        assert entry["claude_session_id"] == "s-mid", "拿到会话 id 没补上"
        assert entry["chat_id"] == "chat1" and entry["workspace"] == "/tmp"
        assert entry["continue_of"] == 0
        assert "A" * 40 not in entry["text"], "原话没脱敏就落盘了"
        assert _disk(tmp) == {}, "跑完没删"
        mode = stat.S_IMODE(os.stat(os.path.join(tmp, inflight_mod.INFLIGHT_FILE)).st_mode)
        assert mode == 0o600, oct(mode)


@pytest.mark.parametrize("result", [
    TurnResult(ok=False, text="", error="坏了"),
    TurnResult(ok=False, text="半截", cancelled=True, error="已按你的要求停下", session_id="s1"),
    TurnResult(ok=True, text="", deferred=True, session_id="s1"),
], ids=["failed", "stopped", "deferred"])
def test_deleted_on_fail_stop_and_waiting_for_approval(result):
    with tempfile.TemporaryDirectory() as tmp:
        async def runner(b, text, resume, on_session):
            on_session("s1")
            if result.deferred:
                _put_pending(b, SCOPE, "ab12cd34ef56ab12cd34ef56", "echo hi > out.txt")
            return result

        b = _bridge(tmp, runner)
        b.replier.send_approval_card = lambda *a, **kw: True
        asyncio.run(b._run_locally(SCOPE, "chat1", "做点事"))
        assert _disk(tmp) == {}, "这一轮结束了还留着在跑记录"


def test_runner_exception_also_deletes():
    with tempfile.TemporaryDirectory() as tmp:
        async def runner(b, text, resume, on_session):
            raise RuntimeError("boom")

        b = _bridge(tmp, runner)
        with pytest.raises(RuntimeError):
            asyncio.run(b._run_locally(SCOPE, "chat1", "做点事"))
        assert _disk(tmp) == {}


def test_killed_mid_turn_leaves_the_entry():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _hang_until_killed)
        _kill_mid_turn(b, ORIGINAL)
        entry = _disk(tmp).get(SCOPE)
        assert entry and entry["claude_session_id"] == "s-live", f"被杀后没留下能续的记录：{entry}"
        assert entry["text"] == ORIGINAL


def test_no_config_path_stays_in_memory():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _hang_until_killed)
        b.settings.config_path = ""
        _kill_mid_turn(b)
        assert not os.path.exists(os.path.join(tmp, inflight_mod.INFLIGHT_FILE))
        assert b._inflight_store().path == ""
        assert [e["scope"] for e in b._inflight_store().leftovers()] == [SCOPE]


# ---------- 重启后续 ----------


def test_restart_resumes_same_session_with_the_hint_not_the_original():
    with tempfile.TemporaryDirectory() as tmp:
        _leftover(tmp)
        b = _bridge(tmp)                       # 新进程
        asyncio.run(b._resume_after_restart())
        assert b.ran == [(CONTINUE_PROMPT, "s-old")], f"续跑发给 claude 的：{b.ran}"
        assert all(ORIGINAL not in text for text, _ in b.ran), "把原话又发了一遍"
        assert any(t.startswith("⟳") and ORIGINAL[:20] in t for t in b.sent), b.sent
        assert _disk(tmp) == {}, "续完没删"


def test_resumed_turn_records_chain_and_original_text():
    with tempfile.TemporaryDirectory() as tmp:
        _leftover(tmp, continue_of=1, chain_started_at=time.time() - 100)
        chain_started = _disk(tmp)[SCOPE]["chain_started_at"]
        b = _bridge(tmp, _hang_until_killed)

        async def go():
            task = asyncio.ensure_future(b._resume_after_restart())
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(go())
        entry = _disk(tmp)[SCOPE]
        assert entry["continue_of"] == 2 and entry["chain_started_at"] == chain_started, entry
        assert entry["text"] == ORIGINAL, "续出来那条记的应该还是他的原话（下次提示里要用）"


def test_chain_of_three_within_five_minutes_stops():
    """真走三次「续 → 又被杀」，第四次启动不续、说一声。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp, _hang_until_killed)
        _kill_mid_turn(b, ORIGINAL)                       # 他的原话那一轮被杀
        for n in range(1, 4):
            b = _bridge(tmp, _hang_until_killed)          # 重启

            async def go():
                task = asyncio.ensure_future(b._resume_after_restart())
                await asyncio.sleep(0.2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            asyncio.run(go())
            assert b.ran == [(CONTINUE_PROMPT, "s-live")], (n, b.ran)
            assert _disk(tmp)[SCOPE]["continue_of"] == n
        b = _bridge(tmp)
        asyncio.run(b._resume_after_restart())
        assert b.ran == [], "连着续了 3 次还在续"
        assert any("好几次" in t for t in b.sent), b.sent
        assert _disk(tmp) == {}, "停下以后下次启动还会再看到它"


def test_old_chain_outside_window_starts_counting_again():
    with tempfile.TemporaryDirectory() as tmp:
        _leftover(tmp, continue_of=3, chain_started_at=time.time() - 400)
        b = _bridge(tmp)
        asyncio.run(b._resume_after_restart())
        assert b.ran == [(CONTINUE_PROMPT, "s-old")]


def test_user_message_resets_the_chain():
    with tempfile.TemporaryDirectory() as tmp:
        _leftover(tmp, continue_of=2)
        seen = {}

        async def runner(b, text, resume, on_session):
            seen.update(_disk(tmp))
            return TurnResult(ok=True, text="好的", session_id="s-old")

        b = _bridge(tmp, runner)
        asyncio.run(b._run_locally(SCOPE, "chat1", "换个事：看看日志"))
        assert seen[SCOPE]["continue_of"] == 0, seen


def test_user_spoke_first_after_restart_wins():
    """重启后续跑排在他新说的那句后面 ⇒ 他那句跑完，续跑不再跑。"""
    with tempfile.TemporaryDirectory() as tmp:
        _leftover(tmp)
        gate = {}

        async def runner(b, text, resume, on_session):
            await gate["go"].wait()                       # 他那句跑着的时候续跑来排队
            return TurnResult(ok=True, text="好的", session_id="s-new")

        b = _bridge(tmp, runner)

        async def go():
            gate["go"] = asyncio.Event()
            user = asyncio.ensure_future(b._run_locally(SCOPE, "chat1", "别管了，先看看日志"))
            await asyncio.sleep(0.05)
            resume = asyncio.ensure_future(b._resume_after_restart())
            await asyncio.sleep(0.05)
            gate["go"].set()
            await user
            await resume

        asyncio.run(go())
        assert [t for t, _ in b.ran] == ["别管了，先看看日志"], b.ran
        assert _disk(tmp) == {}


@pytest.mark.parametrize("case", ["no_session", "pending", "asked", "paused", "too_old"])
def test_not_resumed(case):
    with tempfile.TemporaryDirectory() as tmp:
        over = {}
        if case == "no_session":
            over["claude_session_id"] = ""
        if case == "too_old":
            over["started_at"] = time.time() - 31 * 60
        _leftover(tmp, **over)
        b = _bridge(tmp)
        if case == "pending":
            _put_pending(b, SCOPE, "ab12cd34ef56ab12cd34ef56", "echo hi > out.txt")
        if case == "asked":
            with open(os.path.join(b.gate.dir_for(SCOPE), "ab12cd34ef56ab12cd34ef56.asked"), "w") as f:
                json.dump({"tool_name": "Bash", "tool_input": {"command": "echo hi"}}, f)
        if case == "paused":
            b._paused[SCOPE] = "反复试探"
        asyncio.run(b._resume_after_restart())
        assert b.ran == [], f"{case} 不该续：{b.ran}"
        assert not any(t.startswith("⟳") for t in b.sent), b.sent
        assert _disk(tmp) == {}


def test_plan_pure():
    now = time.time()
    base = {"claude_session_id": "s", "started_at": now - 10, "chain_started_at": now - 10}
    assert inflight_mod.plan(base, now, has_pending=False, paused=False)[0] == "resume"
    assert inflight_mod.plan({**base, "continue_of": 3}, now, has_pending=False,
                             paused=False)[0] == "chain_stop"
    assert inflight_mod.plan({**base, "continue_of": 2}, now, has_pending=False,
                             paused=False)[0] == "resume"


# ---------- runner.on_session ----------


def _fake_claude(tmp, sleep_sec: float) -> None:
    script = f"""#!{sys.executable}
import json, sys, time
def out(e):
    sys.stdout.write(json.dumps(e) + "\\n"); sys.stdout.flush()
out({{"type": "system", "subtype": "init", "session_id": "sess-init"}})
time.sleep({sleep_sec})
out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "完成"}}]}}}})
out({{"type": "result", "subtype": "success", "stop_reason": "end_turn", "session_id": "sess-init"}})
"""
    exe = os.path.join(tmp, "claude")
    with open(exe, "w") as fh:
        fh.write(script)
    os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)


def _policy(tmp):
    from bridge.policy import Policy

    return Policy(mode="trusted", workspaces=(tmp,), default_workspace=tmp,
                  model="claude-haiku-5-5", turn_timeout_sec=30)


def test_on_session_fires_at_init_not_at_the_end(monkeypatch):
    from bridge.runner import ClaudeRunner

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        _fake_claude(tmp, 1.5)
        monkeypatch.setenv("PATH", tmp + os.pathsep + os.environ.get("PATH", ""))
        got = []
        started = time.monotonic()
        result = asyncio.run(ClaudeRunner(_policy(tmp), config_dir=tmp).run_turn(
            "x", tmp, on_session=lambda sid: got.append((sid, time.monotonic() - started))))
        total = time.monotonic() - started
    assert result.ok and result.text == "完成", result
    assert [sid for sid, _ in got] == ["sess-init"], got
    assert got[0][1] < total - 1.0, f"会话 id 到结尾才回调：{got[0][1]:.2f}s / 总 {total:.2f}s"


def test_on_session_error_does_not_break_the_turn(monkeypatch):
    from bridge.runner import ClaudeRunner

    def bad(_sid):
        raise RuntimeError("callback boom")

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        _fake_claude(tmp, 0)
        monkeypatch.setenv("PATH", tmp + os.pathsep + os.environ.get("PATH", ""))
        result = asyncio.run(ClaudeRunner(_policy(tmp), config_dir=tmp).run_turn(
            "x", tmp, on_session=bad))
    assert result.ok and result.text == "完成", result
