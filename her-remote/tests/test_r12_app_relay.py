"""Claude App 里的会话能「接着聊」（第 13 轮起；第 12 轮的决定是「不给」，见下）。

第 12 轮 R 实测（一次性会话，见 `bridge/fleet/claude.py` 文件头）：桥的信使是 bypass 类，
App 会话是 `auto`（归 prompting）⇒ 3/3 被扣住；**换 default 档 + 只给 SendMessage 的信使 3/3 送达、3/3 取回**。
第 13 轮（同事原话「点了某个session 会话之后 为啥不能像我一样 有接着聊的按钮呢」）：
relay 按目标权限档挑同类信使（`Relay.courier_for`），App 会话照常给按钮、走 prompting 那个信使。
另：App 的 jsonl 没有 `permission-mode` 行，档位在 user 行上 ⇒ `permission_mode_of` 两种都认。
全部走真实调用点（`running()` / `Relay.check` / `Relay.send` / `build_detail_card`）。

夹具放在测试目录旁边（不放 tmp_path，同 test_groupsetup.here_dir 的理由）。
**绝不起真 claude、不碰真实会话**：ps 是固定字符串，信使是假的。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import FleetJobs, ProcTable, RunResult, SubprocessRunner  # noqa: E402
from bridge.fleet import claude as claude_mod  # noqa: E402
from bridge.fleet import relay as relay_mod  # noqa: E402
from bridge.fleet.cards import build_detail_card  # noqa: E402
from bridge.fleet.claude import ClaudeAdapter  # noqa: E402
from bridge.fleet.relay import Relay  # noqa: E402

SID_APP = "aaaaaaaa-1111-4111-8111-111111111111"
SID_TERM = "bbbbbbbb-2222-4222-8222-222222222222"
L_APP = "Fri Nov  6 10:56:13 2026"
L_TERM = "Fri Nov  6 11:00:00 2026"

PS_TEXT = f"""\
  8294     1 S    {L_APP}    01:00:00 /Users/x/Library/Application Support/Claude-3p/claude-code/2.1.308/h/claude.app/Contents/MacOS/claude --output-format stream-json
  8300     1 S+   {L_TERM}    01:00:00 claude
"""


class FakeRunner(SubprocessRunner):
    def __init__(self):
        self.calls = []

    def run(self, argv, *, timeout, cwd=None, env=None, input_text=None, on_spawn=None):
        argv = list(argv)
        if argv and argv[0] == "ps":
            return RunResult(returncode=0, stdout=PS_TEXT)
        self.calls.append(argv)
        return RunResult(returncode=1, stdout="")


class FakeCourier:
    permission_mode = "bypassPermissions"

    def __init__(self):
        self.writes = []

    def write(self, name, text):
        self.writes.append((name, text))
        return ""

    def identity(self, timeout=0):
        return {}

    def close(self):
        pass


@pytest.fixture
def here_dir():
    base = pathlib.Path(_HERE).parent / f".r12-app-relay-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))


@pytest.fixture
def env(here_dir):
    home = here_dir / "claude"
    work = here_dir / "work"
    work.mkdir()
    proj = home / "projects" / "-work"

    def reg(pid, sid, lstart, entry, mode_in_user_row):
        # 形状照本机 App 会话 8294.json 抄的：有 socket、有名字、peerProtocol=1
        _write(str(home / "sessions" / f"{pid}.json"), {
            "pid": pid, "sessionId": sid, "cwd": str(work), "procStart": lstart, "kind": "interactive",
            "entrypoint": entry, "status": "idle", "version": "2.1.308", "name": "Greeting",
            "peerProtocol": 1, "peerFeatures": ["notify_idle"],
            "messagingSocketPath": f"/tmp/cc-socks/{pid}.sock",
        })
        rows = [
            {"type": "user", "isSidechain": False, "permissionMode": mode_in_user_row, "cwd": str(work),
             "entrypoint": entry, "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "isSidechain": False,
             "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "hello"}]}},
        ]
        if entry == "cli":
            rows.append({"type": "permission-mode", "permissionMode": "bypassPermissions", "sessionId": sid})
        _write(str(proj / f"{sid}.jsonl"), "".join(json.dumps(r) + "\n" for r in rows))

    reg(8294, SID_APP, L_APP, "claude-desktop-3p", "auto")
    reg(8300, SID_TERM, L_TERM, "cli", "bypassPermissions")
    # 两个会话同名会触发重名闸 ⇒ 终端那个改个名
    rec = json.loads((home / "sessions" / "8300.json").read_text())
    rec["name"] = "终端那个"
    _write(str(home / "sessions" / "8300.json"), rec)

    runner = FakeRunner()
    jobs = FleetJobs(lstart_of=lambda pid: ProcTable.parse(PS_TEXT).lstart(pid), kill=lambda *a: None)
    adapter = ClaudeAdapter(home=str(home), bridge_home=str(here_dir / "bridge"),
                            app_root=str(here_dir / "Claude-3p"), jobs=jobs, runner=runner)
    return {"adapter": adapter, "runner": runner, "jobs": jobs}


def _running(adapter, sid):
    found = [s for s in adapter.running() if s.id == sid]
    assert len(found) == 1, adapter.last_error
    return found[0]


# ---------- 卡片上的能力（真实调用点：running() → _running_session → _relay_capability） ----------


def test_app_session_gets_the_relay_button(env):
    """🔴 第 13 轮：App 会话**给**「接着聊」（推广对象的会话基本全在 App 里）。"""
    sess = _running(env["adapter"], SID_APP)
    assert sess.owner == "app"
    assert sess.can_relay and sess.why_not_relay == "", sess.why_not_relay


def test_terminal_session_still_gets_relay(env):
    """对照：同一套夹具里的终端会话照旧能接着聊（防止把 App 那一刀砍到终端上）。"""
    sess = _running(env["adapter"], SID_TERM)
    assert sess.owner == "terminal" and sess.can_relay and sess.why_not_relay == ""


def test_detail_card_has_the_talk_button_for_app_sessions(env):
    sess = _running(env["adapter"], SID_APP)
    card = build_detail_card(sess, [])
    assert ('"op": "send"' in card or '"op":"send"' in card) and "💬 接着聊" in card
    assert "还没验证过" not in card


# ---------- 发之前那道闸（真实调用点：Relay.check / Relay.send） ----------


class _ModeCourier(FakeCourier):
    def __init__(self, mode):
        super().__init__()
        self.permission_mode = mode


def test_app_session_goes_through_the_prompting_courier(env):
    """🔴 App 会话（auto = prompting）必须交给 **prompting 那个**信使；交给 bypass 的会被扣住（R 实测 0/3）。"""
    relay = Relay(env["adapter"], jobs=env["jobs"])
    bypass, prompting = _ModeCourier("bypassPermissions"), _ModeCourier("default")
    relay.courier, relay.courier_prompting = bypass, prompting
    decision = relay.check(SID_APP)
    assert decision.ok, decision.why
    assert decision.target.mode == "auto" and decision.target.mode_class == "prompting"
    assert relay.courier_for(decision.target.mode_class) is prompting
    relay.send(SID_APP, "在吗", timeout=0.01, delivery_timeout=0.01)
    assert prompting.writes and not bypass.writes, "App 会话交给了 bypass 信使 ⇒ 会被扣住"


def test_terminal_session_still_goes_through_the_bypass_courier(env):
    """阳性对照：终端会话（bypass）照旧交给 bypass 信使。"""
    relay = Relay(env["adapter"], jobs=env["jobs"])
    bypass, prompting = _ModeCourier("bypassPermissions"), _ModeCourier("default")
    relay.courier, relay.courier_prompting = bypass, prompting
    relay.send(SID_TERM, "在吗", timeout=0.01, delivery_timeout=0.01)
    assert bypass.writes and not prompting.writes


def test_default_couriers_are_one_per_class(env):
    """没注入信使时，真建两个：bypass 一个、default 一个（R 实测通的那个形状），名字不同。"""
    relay = Relay(env["adapter"], jobs=env["jobs"])
    assert relay_mod.mode_class(relay.courier.permission_mode) == "bypass"
    assert relay.courier_prompting.permission_mode == "default"
    assert relay.courier.name != relay.courier_prompting.name
    argv = relay.courier_prompting.argv()
    assert "--tools" in argv and "SendMessage" in argv
    # 🔴 default 档下只列工具不等于预先批准 —— 不加这一项，无人值守的信使用 SendMessage 会被挡（R 实测形状）
    assert argv[argv.index("--allowedTools") + 1] == "SendMessage", argv


def test_app_jsonl_mode_is_read_from_the_user_row(env, here_dir):
    """App 的 jsonl 没有 permission-mode 行（本机实测），档位只在 user 行上 —— 要读得出来。"""
    path = env["adapter"]._jsonl_path(SID_APP)
    assert relay_mod.permission_mode_of(path) == "auto"


def test_relay_still_refuses_sdk_cli_with_old_reason():
    """别的脚本起的 claude -p 照旧是 WHY_NOT_TERMINAL（App 那句不能漏到它身上）。"""
    assert relay_mod.WHY_NOT_TERMINAL != claude_mod.WHY_SEND_APP


# ---------- 权限档：auto 归 prompting（实测），_BYPASS_MODES 不动 ----------


def test_auto_is_prompting_class():
    assert relay_mod.mode_class("auto") == "prompting"
    assert relay_mod.mode_class("bypassPermissions") == "bypass"
    assert relay_mod._BYPASS_MODES == ("bypasspermissions", "bypass")


def test_wording_is_plain_and_fits_card_note():
    from bridge.fleet.cards import NOTE_CHARS

    why = claude_mod.WHY_SEND_APP
    assert len(why) <= NOTE_CHARS
    assert "验证" not in why and "这条路" not in why
    assert "App" not in claude_mod.WHY_STOP_APP.replace("App 里点停止", "")
