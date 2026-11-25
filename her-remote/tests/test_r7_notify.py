"""第 7 轮：Claude 会话进入「等你」时，主动往主人单聊推一张卡。

外部侦察 Top2（CC #29438 👍59、#28765 👍44）：「它在等我，手机却不响」。
规则（brief-r7.md）：
1. 同一次 waiting 只推一次；回到别的状态再进 waiting 才算新的一次
2. 持续 ≥20 秒才推（秒级的不推）
3. 人在电脑前（键鼠空闲 < 120 秒）不推；读不出来 ⇒ 当人不在、照推
4. 一个会话 10 分钟最多一次；全部加起来 1 分钟最多 3 张
5. 推到主人单聊：标题「🟡 Claude「名」在等你」+ 项目一行 + 「👀 看看它」按钮，不放消息内容
6. `[notify] waiting = false` 一张都不推
7. 复用总控扫描（FleetControl.overview），后台每 10 秒；扫描失败不能让桥崩

全部用假时钟、假 adapter、假 replier；不真睡、不碰真实会话、不调飞书。
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import types
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import Session  # noqa: E402
from bridge.fleet.cards import fleet_value, resolve_fleet_click  # noqa: E402
from bridge.fleet.control import FleetControl  # noqa: E402
from bridge.fleet.scan import Fleet, Overview  # noqa: E402

OWNER = "ou_owner"
SID_A = "aaaaaaaa-1111-4111-8111-111111111111"
SID_B = "bbbbbbbb-2222-4222-8222-222222222222"

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
MAC_TEMPLATE = os.path.join(_REPO, "feishu-agent-oneclick", "src", "install-mac.sh")
WIN_TEMPLATE = os.path.join(_REPO, "feishu-agent-oneclick", "src", "install-windows.ps1")


# ---------- 夹具 ----------


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def tick(self, sec: float) -> None:
        self.t += sec


def sess(sid=SID_A, status="waiting", tool="claude", owner="terminal", title="修登录",
         cwd="/Users/x/codes/demo", **kw):
    return Session(tool=tool, surface="cli", id=sid, title=title, cwd=cwd, status=status,
                   owner=owner, pids=(4242,), updated_at=time.time(), **kw)


def view(*sessions, ok=("claude", "codex", "cursor")):
    return Overview(sessions=list(sessions), ok_tools=list(ok), at=time.time())


def notifier(clock, idle=None, **kw):
    from bridge.fleet.notify import WaitingNotifier

    return WaitingNotifier(enabled=kw.pop("enabled", True), now=clock,
                           idle=(lambda: idle) if not callable(idle) else idle, **kw)


def run_for(n, clock, seconds, make_view, step=10):
    """按 10 秒一拍跑 `seconds` 秒，返回每拍推出来的 key 列表（拉平）。"""
    pushed = []
    for _ in range(int(seconds // step)):
        clock.tick(step)
        for s in n.due(make_view()):
            pushed.append(s.key)
            n.record(s.key, True)
    return pushed


# ---------- 规则 2：持续 ≥20 秒才推 ----------


def test_waiting_under_20_seconds_is_not_pushed_and_at_20_it_is():
    clock = Clock()
    n = notifier(clock)
    assert n.due(view(sess())) == []          # t=0 第一次看到
    clock.tick(19)
    assert n.due(view(sess())) == []          # 19 秒：还不推
    clock.tick(1)
    due = n.due(view(sess()))                 # 20 秒：推
    assert [s.key for s in due] == [("claude", SID_A)]


def test_short_flaps_reset_the_timer():
    """等 10 秒 → 继续干活 → 又等 10 秒：两段都不够 20 秒，不推。"""
    clock = Clock()
    n = notifier(clock)
    n.due(view(sess()))
    clock.tick(10)
    assert n.due(view(sess())) == []
    clock.tick(1)
    assert n.due(view(sess(status="running"))) == []
    clock.tick(1)
    n.due(view(sess()))
    clock.tick(15)
    assert n.due(view(sess())) == []


# ---------- 规则 1：同一次等待只推一次 ----------


def test_same_waiting_episode_is_pushed_only_once():
    clock = Clock()
    n = notifier(clock)
    n.due(view(sess()))
    pushed = run_for(n, clock, 60 * 60, lambda: view(sess()))  # 一直等了一小时
    assert pushed == [("claude", SID_A)]


def test_leaving_and_reentering_waiting_is_a_new_episode():
    clock = Clock()
    n = notifier(clock)
    n.due(view(sess()))
    assert run_for(n, clock, 30, lambda: view(sess())) == [("claude", SID_A)]
    clock.tick(10)
    n.due(view(sess(status="running")))       # 回去干活了
    clock.tick(11 * 60)                       # 过了每会话 10 分钟的限
    n.due(view(sess()))                       # 又进 waiting
    assert run_for(n, clock, 30, lambda: view(sess())) == [("claude", SID_A)]


def test_session_gone_from_a_good_scan_ends_the_episode():
    clock = Clock()
    n = notifier(clock)
    n.due(view(sess()))
    run_for(n, clock, 30, lambda: view(sess()))
    clock.tick(10)
    n.due(view())                             # 进程没了（扫描是好的，里面没它）
    clock.tick(11 * 60)
    n.due(view(sess()))
    assert run_for(n, clock, 30, lambda: view(sess())) == [("claude", SID_A)]


def test_claude_scan_missing_keeps_state_instead_of_resetting():
    """这次 Claude 没读到（超时 / 上一次还没读完）≠ 会话不等了：计时不清零，也不当成新的一次。"""
    clock = Clock()
    n = notifier(clock)
    n.due(view(sess()))
    clock.tick(15)
    assert n.due(view(ok=("codex",))) == []   # Claude 这次没读到
    clock.tick(5)
    assert [s.key for s in n.due(view(sess()))] == [("claude", SID_A)]  # 从第一次看到算满 20 秒
    clock.tick(10)
    assert n.due(view(ok=("codex",))) == []
    clock.tick(10)
    assert n.due(view(sess())) == []          # 还是同一次，不再推


# ---------- 规则 3：人在电脑前不推 ----------


def test_human_at_the_computer_suppresses_then_push_when_he_leaves():
    clock = Clock()
    idle = {"v": 0.02}
    n = notifier(clock, idle=lambda: idle["v"])
    n.due(view(sess()))
    clock.tick(30)
    assert n.due(view(sess())) == []          # 刚动过鼠标：不推
    idle["v"] = 119.0
    clock.tick(10)
    assert n.due(view(sess())) == []          # 119 秒：还算在电脑前
    idle["v"] = 120.0
    clock.tick(10)
    assert [s.key for s in n.due(view(sess()))] == [("claude", SID_A)]  # 走开了：推


def test_unreadable_idle_counts_as_away():
    """读不出来（不是 Mac / 命令失败）⇒ 当人不在、照推。漏推比多推一次更糟。"""
    clock = Clock()

    def broken():
        raise OSError("ioreg 跑不起来")

    for idle in (None, broken):
        n = notifier(clock, idle=idle)
        n.due(view(sess()))
        clock.tick(20)
        assert [s.key for s in n.due(view(sess()))] == [("claude", SID_A)], idle


def test_idle_probe_not_run_when_nothing_is_due():
    """ioreg 只在真有一张要推时才跑（每 10 秒白跑一次没必要）。"""
    calls = []
    clock = Clock()
    n = notifier(clock, idle=lambda: calls.append(1) or None)
    for _ in range(5):
        n.due(view(sess(status="running")))
        clock.tick(10)
    assert calls == []


# ---------- 规则 4：限流 ----------


def test_one_session_at_most_once_per_10_minutes():
    clock = Clock()
    n = notifier(clock)
    n.due(view(sess()))
    clock.tick(20)
    assert len(n.due(view(sess()))) == 1     # t=20 推了
    n.record(("claude", SID_A), True)
    for _ in range(3):                        # 等 → 跑 → 等，每段都 > 20 秒，但都在 10 分钟里
        clock.tick(30)
        n.due(view(sess(status="running")))
        clock.tick(1)
        n.due(view(sess()))
        clock.tick(30)
        assert n.due(view(sess())) == []
    clock.tick(10 * 60)                       # 过了 10 分钟，这一次还在等 ⇒ 推
    assert [s.key for s in n.due(view(sess()))] == [("claude", SID_A)]


def test_all_sessions_together_at_most_3_per_minute_longest_first():
    clock = Clock()
    n = notifier(clock)
    sids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(6)]
    # 按顺序先后进入 waiting（越前面的等得越久）
    for i in range(6):
        n.due(view(*[sess(sid) for sid in sids[: i + 1]]))
        clock.tick(1)
    clock.tick(20)
    first = [s.id for s in n.due(view(*[sess(sid) for sid in sids]))]
    assert first == sids[:3]                  # 3 张，等得最久的先
    for _ in range(5):                        # 一分钟内再扫：一张都不加
        clock.tick(10)
        assert n.due(view(*[sess(sid) for sid in sids])) == []
    clock.tick(10)                            # 第一批推出 60 秒后
    assert [s.id for s in n.due(view(*[sess(sid) for sid in sids]))] == sids[3:]


def test_failed_send_retries_after_cooldown_not_every_tick():
    clock = Clock()
    n = notifier(clock)
    n.due(view(sess()))
    clock.tick(20)
    assert len(n.due(view(sess()))) == 1
    n.record(("claude", SID_A), False)        # 飞书没送达
    for _ in range(6):
        clock.tick(10)
        assert n.due(view(sess())) == []      # 不是每 10 秒重打一次
    clock.tick(10 * 60)
    assert [s.key for s in n.due(view(sess()))] == [("claude", SID_A)]  # 冷却完再试


# ---------- 只做 Claude ----------


def test_only_claude_and_not_the_bridges_own_runs():
    clock = Clock()
    n = notifier(clock)
    others = (sess("c0dex", tool="codex"), sess("curs0r", tool="cursor"),
              sess(SID_B, owner="bridge"))
    n.due(view(*others))
    clock.tick(60)
    assert n.due(view(*others)) == []


# ---------- 规则 6：开关 ----------


def test_switched_off_pushes_nothing():
    clock = Clock()
    n = notifier(clock, enabled=False)
    n.due(view(sess()))
    assert run_for(n, clock, 60 * 60, lambda: view(sess())) == []


@pytest.fixture
def here_dir():
    """落盘夹具放在测试目录旁边，不放 tmp_path（macOS 上是 pathguard 豁免前缀）。"""
    base = pathlib.Path(_HERE).parent / f".r7-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


_CFG = """
[feishu]
app_id = "cli_x"
app_secret = "s"
owner_open_ids = ["ou_owner"]

[agent]
workspaces = ["{ws}"]
default_workspace = "{ws}"
mode = "readonly"
{extra}
"""


@pytest.mark.parametrize("extra,expected", [
    ("", True),                                   # 没写 ⇒ 默认开
    ("[notify]\nwaiting = true\n", True),
    ("[notify]\nwaiting = false\n", False),
    ('[notify]\nwaiting = "false"\n', False),     # 写成字符串也认
])
def test_settings_reads_notify_switch(here_dir, extra, expected, monkeypatch):
    from bridge.app import Settings

    monkeypatch.delenv("FEISHU_BRIDGE_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_BRIDGE_APP_SECRET", raising=False)
    path = here_dir / "config.toml"
    path.write_text(_CFG.format(ws=str(here_dir), extra=extra), encoding="utf-8")
    assert Settings.load(str(path)).notify_waiting is expected


# ---------- 规则 3 的量具：ioreg ----------

_IOREG_SAMPLE = """+-o IOHIDSystem  <class IOHIDSystem, id 0x100000465, registered, matched, active, busy 0 (0 ms), retain 23>
    {
      "IOClass" = "IOHIDSystem"
      "HIDIdleTime" = 7977400000000
      "IOProviderClass" = "IOResources"
    }
"""


def test_parse_hid_idle_reads_nanoseconds():
    from bridge.fleet.notify import parse_hid_idle

    assert parse_hid_idle(_IOREG_SAMPLE) == pytest.approx(7977.4)
    assert parse_hid_idle('  "HIDIdleTime" = 20000000\n') == pytest.approx(0.02)
    assert parse_hid_idle("没有那一行") is None
    assert parse_hid_idle("") is None


def test_idle_probe_argv_no_shell_timeout_2():
    from bridge.fleet.notify import human_idle_seconds

    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, stdout=_IOREG_SAMPLE, stderr="")

    assert human_idle_seconds(run=fake_run, platform="darwin") == pytest.approx(7977.4)
    assert isinstance(seen["argv"], list) and "IOHIDSystem" in seen["argv"]
    assert seen["kw"].get("timeout") == 2
    assert not seen["kw"].get("shell")


@pytest.mark.parametrize("behaviour", ["raise", "timeout", "nonzero", "noline"])
def test_idle_probe_failures_read_as_unknown(behaviour):
    from bridge.fleet.notify import human_idle_seconds

    def fake_run(argv, **kw):
        if behaviour == "raise":
            raise FileNotFoundError("ioreg")
        if behaviour == "timeout":
            raise subprocess.TimeoutExpired(argv, 2)
        if behaviour == "nonzero":
            return subprocess.CompletedProcess(argv, 1, stdout=_IOREG_SAMPLE, stderr="boom")
        return subprocess.CompletedProcess(argv, 0, stdout="nothing here", stderr="")

    assert human_idle_seconds(run=fake_run, platform="darwin") is None


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_idle_probe_off_mac_is_unknown_without_running_anything(platform):
    from bridge.fleet.notify import human_idle_seconds

    def fake_run(*a, **kw):
        raise AssertionError("不是 Mac 就不该跑命令")

    assert human_idle_seconds(run=fake_run, platform=platform) is None


# ---------- 规则 5：卡片 ----------


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _buttons(card: dict) -> list:
    return [n for n in _walk(card) if n.get("tag") == "button"]


def test_waiting_card_shape():
    from bridge.fleet.notify import build_waiting_card

    blob = build_waiting_card(sess(cwd="/Users/x/codes/carher-admin"))
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "🟡 Claude「修登录」在等你"
    assert len(blob.encode("utf-8")) < 30 * 1024
    assert not any(n.get("tag") == "action" for n in _walk(card))
    assert "carher-admin" in blob             # 在哪个项目
    buttons = _buttons(card)
    # 第 33 轮：多了「1 小时后再提醒」（op=snooze，见 tests/test_r33_fleet_act.py）
    assert len(buttons) == 2
    assert buttons[0]["text"]["content"] == "👀 看看它"
    assert buttons[0]["behaviors"][0]["value"] == fleet_value("open", "claude", SID_A)
    assert buttons[1]["behaviors"][0]["value"]["op"] == "snooze"


def test_waiting_card_has_no_message_content_and_title_is_redacted():
    from bridge.fleet.notify import build_waiting_card, waiting_fallback_text
    from bridge.app import _redact_text

    secret = "sk-ant-api03-" + "A" * 40
    s = sess(title=f"用 {secret} 调接口", extra={"last_text": "它最后说的一句话"})
    for blob in (build_waiting_card(s, redact=_redact_text), waiting_fallback_text(s, redact=_redact_text)):
        assert secret not in blob
        assert "它最后说的一句话" not in blob


# ---------- 接进 app ----------


class FakeAdapter:
    def __init__(self, tool, sessions=(), boom=False):
        self.tool = tool
        self.sessions = list(sessions)
        self.boom = boom
        self.last_error = ""
        self.calls = 0

    def list(self, limit=20):
        self.calls += 1
        if self.boom:
            raise RuntimeError("坏了")
        return list(self.sessions)


class FakeReplier:
    def __init__(self, card_ok=True):
        self.texts, self.cards, self.card_ok = [], [], card_ok

    def send(self, chat_id, text):
        self.texts.append((chat_id, text))
        return True

    def send_card(self, chat_id, card):
        self.cards.append((chat_id, card))
        return self.card_ok


def make_bridge(adapter, clock, *, enabled=True, card_ok=True):
    from bridge.app import Bridge
    from bridge.fleet.notify import WaitingNotifier

    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(owner_open_ids=frozenset({OWNER}), notify_waiting=enabled)
    b.replier = FakeReplier(card_ok)
    b._chat_ids = {}
    b._loop = None
    b.fleet = FleetControl(Fleet({adapter.tool: adapter}, timeout=1.0))
    b.waiting_notifier = WaitingNotifier(enabled=enabled, now=clock, idle=lambda: None)
    return b


def test_tick_pushes_one_card_to_owner_and_its_button_resolves():
    clock = Clock()
    adapter = FakeAdapter("claude", [sess()])
    b = make_bridge(adapter, clock)
    b._notify_tick()
    assert b.replier.cards == []
    clock.tick(20)
    b._notify_tick()
    assert len(b.replier.cards) == 1
    to, blob = b.replier.cards[0]
    assert to == OWNER                        # 主人单聊（open_id 发，replier 按 ou_ 选 receive_id_type）
    value = _buttons(json.loads(blob))[0]["behaviors"][0]["value"]
    # 按钮真能点：value 过得了总控那道白名单闸（会话在最近一次扫描里）
    assert resolve_fleet_click(value, b.fleet.known_keys()) == {"op": "open", "tool": "claude", "id": SID_A}
    clock.tick(10)
    b._notify_tick()
    assert len(b.replier.cards) == 1          # 同一次等待不再推


def test_tick_card_failure_falls_back_to_text():
    clock = Clock()
    b = make_bridge(FakeAdapter("claude", [sess()]), clock, card_ok=False)
    b._notify_tick()
    clock.tick(20)
    b._notify_tick()
    assert len(b.replier.texts) == 1 and "在等你" in b.replier.texts[0][1]


def test_tick_switched_off_does_not_even_scan():
    clock = Clock()
    adapter = FakeAdapter("claude", [sess()])
    b = make_bridge(adapter, clock, enabled=False)
    for _ in range(5):
        b._notify_tick()
        clock.tick(20)
    assert adapter.calls == 0 and b.replier.cards == [] and b.replier.texts == []


def test_tick_never_raises_when_scan_blows_up():
    clock = Clock()
    b = make_bridge(FakeAdapter("claude", boom=True), clock)
    b._notify_tick()                          # adapter 抛
    b.fleet = None
    b._notify_tick()                          # 总控没起来

    class Exploding:
        def overview(self):
            raise RuntimeError("扫描整个炸了")

    b.fleet = Exploding()
    b._notify_tick()                          # 不往外抛就算过


def test_loop_keeps_going_after_a_failing_tick(monkeypatch):
    """后台循环里一拍失败，下一拍照样跑（循环本身不死）。"""
    from bridge import app as app_mod

    b = app_mod.Bridge.__new__(app_mod.Bridge)
    ticks = []

    def bad_tick():
        ticks.append(1)
        raise RuntimeError("这一拍炸了")

    b._notify_tick = bad_tick

    class Stop(BaseException):
        pass

    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > 3:
            raise Stop()

    monkeypatch.setattr(app_mod.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        b._notify_waiting_loop()
    assert len(ticks) == 3
    assert set(sleeps) == {10}                # 每 10 秒一拍


# ---------- 调用点还在 ----------


def test_run_starts_the_notify_thread():
    from bridge.app import Bridge

    src = inspect.getsource(Bridge.run)
    assert re.search(r"threading\.Thread\(\s*target=self\._notify_waiting_loop\b[^)]*\)\.start\(\)", src), \
        "run() 里没起「等你」推送的后台线程"


def test_init_builds_the_notifier_from_settings():
    from bridge.app import Bridge

    src = inspect.getsource(Bridge.__init__)
    assert re.search(r"WaitingNotifier\(\s*enabled=settings\.notify_waiting", src), \
        "Bridge.__init__ 没按配置开关造 WaitingNotifier"


def test_tick_uses_the_shared_fleet_scan():
    """复用总控现成的扫描（会顺带刷新按钮白名单），不另起一套读注册表的代码。"""
    from bridge.app import Bridge

    src = inspect.getsource(Bridge._notify_tick)
    assert re.search(r"\.overview\(\)", src) and re.search(r"\.due\(", src) and re.search(r"\.record\(", src)
    loop_src = inspect.getsource(Bridge._notify_waiting_loop)
    assert re.search(r"self\._notify_tick\(\)", loop_src)


@pytest.mark.parametrize("template,needle", [
    (MAC_TEMPLATE, 'write_file "$APP_HOME/bridge/fleet/notify.py"'),
    (WIN_TEMPLATE, "Write-PyFile 'fleet\\notify.py'"),
], ids=["mac", "windows"])
def test_notify_module_is_embedded_in_both_installers(template, needle):
    if not os.path.exists(template):
        pytest.skip("模板不在")
    with open(template, encoding="utf-8") as handle:
        blob = handle.read()
    assert needle in blob
    assert ("@@" + "EMBED bridge/fleet/notify.py@@") in blob
