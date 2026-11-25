"""第 37 轮：Claude 会话群工具栏「点击之后做什么」（`docs/r37/contract-toolkit.md` 角色 C 那一节）。

🔴 一个群都不真建、一条飞书都不真发：夹具复用 test_chatbind 的假 bridge / 假 adapter / 假 replier。
toolkit 模块（角色 B）一律用假的：点击直接构造同形的 `ToolbarClick`。

盯的：
- 闸门：非主人 / 不在绑定表 / 非 Claude 群 / 解绑过的群 / 不认识的按钮 ⇒ 什么都不做（阳性对照：主人 + 开着的 Claude 群 ⇒ 做）
- 五个动作的正路，回复都进**那个群**
- 🧠：选择落 `Bound`（落盘、兼容旧文件）、出现在下一轮 `-p --resume` 的 argv；relay 会话只出说明
- ▶️：走群消息同一条路（reviewer / 暂停闸），文案带时间（避开 10 分钟同句去重）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import types
from dataclasses import dataclass

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.fleet import ActionResult  # noqa: E402
from bridge.fleet.chatbind import ChatBinds  # noqa: E402
from bridge.review import Decision, Verdict  # noqa: E402
from test_fleet_claude import env  # noqa: E402,F401 —— 夹具（假 ~/.claude 目录）
from test_chatbind import (  # noqa: E402
    CID,
    OWNER,
    FakeClaude,
    FakeGroups,
    _bridge,
    _open_group,
    _run,
    _sess,
    _toast,
)


@dataclass(frozen=True)
class ToolbarClick:  # 和 toolkit.ToolbarClick 同形（契约）
    action: str
    chat_id: str
    open_id: str
    tenant_key: str = "t"
    log_id: str = "l"
    at: float = 0.0


class IdleClaude(FakeClaude):
    """没人占着的会话：不走 relay，走 `send()`（= `claude -p --resume`），记下带了哪些参数。"""

    def __init__(self, sessions=()):
        super().__init__(sessions)
        self.sent_kw: list = []

    def can_relay(self, sid):
        return False, ""

    def send(self, sid, text, timeout=600, **kw):
        self.sent.append((sid, text))
        self.sent_kw.append(kw)
        return ActionResult(True, "resume 回复", sid)


def _prefs(kw):
    return {k: v for k, v in kw.items() if k in ("model", "effort")}


@pytest.fixture
def loop():
    lp = asyncio.new_event_loop()
    th = threading.Thread(target=lp.run_forever, daemon=True)
    th.start()
    yield lp
    lp.call_soon_threadsafe(lp.stop)
    th.join(2)
    lp.close()


def _setup(tmp_path, loop, *, idle=False, cwd=None):
    kw = {"cwd": str(cwd)} if cwd else {}
    if idle:
        kw.update(owner="none", pids=(), status="idle", can_send=True, why_not_send="", can_relay=False)
    claude = (IdleClaude if idle else FakeClaude)([_sess(**kw)])
    b = _bridge(tmp_path, claude, FakeGroups())
    b.fleet.overview()
    _open_group(b, needs_confirm=False)
    gid = b.chat_binds.of_session("claude", CID).chat_id
    b._loop = loop
    return b, claude, gid


def _click(b, action, gid, who=OWNER, wait=True):
    fut = b._on_toolbar_click(ToolbarClick(action=action, chat_id=gid, open_id=who))
    if fut is not None and wait:
        fut.result(timeout=10)
    return fut


def _said(b, gid):
    return "\n".join(b.replier.to(gid))


def _cards(b, gid):
    return [c if isinstance(c, str) else json.dumps(c, ensure_ascii=False) for c in b.replier.cards_to(gid)]


def _model_click(b, gid, op, option, nonce=None, who=OWNER):
    nonce = nonce if nonce is not None else b._toolbar_model_cards[gid][0]
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=gid),
                                  action=types.SimpleNamespace(option=option))
    return b._on_toolbar_model_click(event, who, {"act": "tkm", "op": op, "n": nonce})


# ---------- 闸门 ----------


def test_positive_control_owner_in_bound_claude_group_runs(tmp_path, loop):
    """阳性对照：下面那几条「不执行」只有在这条能执行时才有意义。"""
    b, _c, gid = _setup(tmp_path, loop)
    before = len(b.replier.to(gid))
    assert _click(b, "stop", gid) is not None
    assert len(b.replier.to(gid)) > before


@pytest.mark.parametrize("case", ["not_owner", "unknown_chat", "unbound", "codex_group", "bad_action"])
def test_gate_drops_click_and_says_nothing(tmp_path, loop, case):
    b, claude, gid = _setup(tmp_path, loop)
    who, chat, action = OWNER, gid, "go_on"
    if case == "not_owner":
        who = "ou_stranger"
    elif case == "unknown_chat":
        chat = "oc_nobody"
    elif case == "unbound":
        b.chat_binds.turn_off(gid)
    elif case == "codex_group":
        chat = "oc_codexgroup"
        b.chat_binds.bind(chat, "codex", "codex-sid-1", "Codex · x")
    elif case == "bad_action":
        action = "rm_rf"
    texts_before, cards_before = len(b.replier.texts), len(b.replier.cards)
    assert _click(b, action, chat, who=who) is None
    assert claude.relayed == [] and claude.sent == []
    assert len(b.replier.texts) == texts_before and len(b.replier.cards) == cards_before


def test_click_before_loop_is_dropped(tmp_path, loop):
    b, claude, gid = _setup(tmp_path, loop)
    b._loop = None
    assert _click(b, "go_on", gid) is None and claude.relayed == []


# ---------- ⏹ 停下 ----------


def test_stop_on_terminal_session_says_it_cannot(tmp_path, loop):
    b, _c, gid = _setup(tmp_path, loop)
    b.fleet.clear_pending(f"group:{gid}")  # 5 分钟待发目标过了 / 刚重启：照样能停（问 adapter）
    _click(b, "stop", gid)
    assert "Esc" in _said(b, gid)


def test_stop_while_talking_uses_group_stop_command(tmp_path, loop):
    b, _c, gid = _setup(tmp_path, loop)
    _click(b, "stop", gid)  # 建群即进对话 ⇒ 有待发目标 ⇒ 走 /stop 那一套（给「退出对话」）
    assert "Esc" in _said(b, gid) and "退出对话" in _said(b, gid)


# ---------- ▶️ 接着干 ----------


def test_go_on_relays_timestamped_text_and_tells_group(tmp_path, loop):
    b, claude, gid = _setup(tmp_path, loop)
    _click(b, "go_on", gid)
    assert len(claude.relayed) == 1
    sent = claude.relayed[0][1]
    assert re.fullmatch(r"接着干（\d\d:\d\d:\d\d）", sent), sent  # 带时间 ⇒ 不撞 10 分钟同句去重
    assert "▶️ 已让它接着干" in _said(b, gid)
    assert b.handled == [], "落进了桥自己的 claude"


def test_go_on_goes_through_reviewer(tmp_path, loop):
    b, claude, gid = _setup(tmp_path, loop)
    seen = []

    class Deny:
        def review_request(self, ctx):
            seen.append(ctx.text)
            return Decision(Verdict.DENY, "测试拦下")

    b.reviewer = Deny()
    _click(b, "go_on", gid)
    assert seen and seen[0].startswith("接着干"), "没过 reviewer"
    assert claude.relayed == [] and "测试拦下" in _said(b, gid)


def test_go_on_respects_pause(tmp_path, loop):
    b, claude, gid = _setup(tmp_path, loop)
    b._paused[f"group:{gid}"] = "刚才那一步可疑"
    _click(b, "go_on", gid)
    assert claude.relayed == [] and "已暂停" in _said(b, gid)
    assert "▶️ 已让它接着干" not in _said(b, gid)


# ---------- 🧠 模型 ----------


def test_model_on_relay_session_only_explains(tmp_path, loop):
    b, _c, gid = _setup(tmp_path, loop)
    _click(b, "model", gid)
    cards = _cards(b, gid)
    assert cards and "在电脑上敲 /model" in cards[-1]
    assert "select_static" not in cards[-1], "终端开着的会话不该给下拉"
    assert gid not in getattr(b, "_toolbar_model_cards", {})


def test_model_pick_lands_in_bound_and_next_argv(tmp_path, loop):
    b, claude, gid = _setup(tmp_path, loop, idle=True)
    _click(b, "model", gid)
    card = _cards(b, gid)[-1]
    assert card.count("select_static") == 2 and "claude-opus-5-5" in card
    assert '"tkm"' in card and "claude-opus-5-5" not in json.dumps(
        [o for o in re.findall(r'"value": \{[^}]*\}', card)]), "模型名进了回调值"
    r1 = _model_click(b, gid, "model", "m1")
    r2 = _model_click(b, gid, "effort", "e2")
    assert "记下了" in _toast(r1) and "记下了" in _toast(r2)
    bound = b.chat_binds.of_chat(gid)
    assert (bound.model, bound.effort) == ("claude-opus-5-5", "high")
    # 落盘：重启后还在
    again = ChatBinds(b.chat_binds._path).of_chat(gid)
    assert (again.model, again.effort) == ("claude-opus-5-5", "high")
    # 下一句（群消息）带上
    _run(b._handle(f"group:{gid}", gid, "看看测试"))
    assert _prefs(claude.sent_kw[-1]) == {"model": "claude-opus-5-5", "effort": "high"}
    # 改回「不指定」⇒ 一个键都不带
    _model_click(b, gid, "model", "d")
    _model_click(b, gid, "effort", "d")
    _run(b._handle(f"group:{gid}", gid, "再看看"))
    assert _prefs(claude.sent_kw[-1]) == {}


@pytest.mark.parametrize("bad", ["stale_nonce", "not_owner", "bad_option", "other_chat"])
def test_model_click_gate(tmp_path, loop, bad):
    b, _c, gid = _setup(tmp_path, loop, idle=True)
    _click(b, "model", gid)
    if bad == "stale_nonce":
        resp = _model_click(b, gid, "model", "m1", nonce="deadbeef")
    elif bad == "not_owner":
        resp = _model_click(b, gid, "model", "m1", who="ou_stranger")
    elif bad == "bad_option":
        resp = _model_click(b, gid, "model", "m99")
    else:
        nonce = b._toolbar_model_cards[gid][0]
        resp = _model_click(b, "oc_nobody", "model", "m1", nonce=nonce)
    assert "记下了" not in _toast(resp)
    assert b.chat_binds.of_chat(gid).model == ""


def test_model_value_shape_rejects_extra_keys():
    from bridge.fleet.actions import resolve_toolbar_model_click, resolve_toolbar_model_option

    assert resolve_toolbar_model_click({"act": "tkm", "op": "model", "n": "ab12"}) == {"op": "model", "n": "ab12"}
    assert resolve_toolbar_model_click({"act": "tkm", "op": "model", "n": "ab12", "m": "x"}) is None
    assert resolve_toolbar_model_click({"act": "tkm", "op": "mode", "n": "ab12"}) is None
    assert resolve_toolbar_model_option("model", "claude-opus-5-5") is None  # 只认下标
    assert resolve_toolbar_model_option("effort", "e4") == "max"
    assert resolve_toolbar_model_option("effort", "m0") is None


def test_effort_choices_match_claude_help():
    """取值来自本机 `claude --help`（11-16，CLI 2.1.325）：(low, medium, high, xhigh, max)。装了 claude 就现查一次。"""
    from bridge.fleet.chatbind import EFFORT_LEVELS

    assert EFFORT_LEVELS == ("low", "medium", "high", "xhigh", "max")
    try:
        out = subprocess.run(["claude", "--help"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("本机没有 claude")
    m = re.search(r"--effort <level>.*?\(([^)]*)\)", out, re.S)
    if not m:
        pytest.skip("这个版本的 claude --help 里没有 --effort")
    assert tuple(x.strip() for x in m.group(1).split(",")) == EFFORT_LEVELS


def test_chatbind_old_file_and_junk_values(tmp_path):
    path = tmp_path / "chat_binds.json"
    path.write_text(json.dumps({"binds": {
        "oc_old": {"tool": "claude", "id": CID, "name": "x", "created_at": 1, "on": True},
        "oc_junk": {"tool": "claude", "id": "other", "name": "y", "created_at": 2, "on": True,
                    "model": "evil --dangerously-skip-permissions", "effort": "ultra"},
    }}), encoding="utf-8")
    store = ChatBinds(str(path))
    assert (store.of_chat("oc_old").model, store.of_chat("oc_old").effort) == ("", "")
    assert (store.of_chat("oc_junk").model, store.of_chat("oc_junk").effort) == ("", "")
    store.set_prefs("oc_old", model="claude-haiku-5-5")
    store.bind("oc_old", "claude", "another-sid", "x")  # 改绑：选择跟着群走
    assert store.of_chat("oc_old").model == "claude-haiku-5-5"
    assert "model" not in json.loads(path.read_text())["binds"]["oc_junk"]


def test_send_argv_carries_model_and_effort_only_when_whitelisted(tmp_path):
    from bridge.fleet.claude import ClaudeAdapter
    from bridge.policy import Policy

    ad = ClaudeAdapter(home=str(tmp_path / "h"), bridge_home=str(tmp_path / "b"), app_root=str(tmp_path / "a"),
                       user_settings=str(tmp_path / "none.json"))
    pol = Policy(mode="readonly", workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                 model="claude-sonnet-5-5", turn_timeout_sec=60)
    argv = ad._send_argv(CID, "hi", pol, model="claude-opus-5-5", effort="xhigh")
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--effort") + 1] == "xhigh"
    plain = ad._send_argv(CID, "hi", pol)
    assert "--model" not in plain and "--effort" not in plain
    bogus = ad._send_argv(CID, "hi", pol, model="opus; rm -rf ~", effort="ultra")
    assert "--model" not in bogus and "--effort" not in bogus


def test_real_send_puts_prefs_into_spawned_argv(env):
    """走**真的** `ClaudeAdapter.send()`（假 runner）：去掉 _send_locked 往 argv 传参那一步，这条要红。"""
    from test_fleet_claude import SID_IDLE, _adapter

    ad, runner, *_ = _adapter(env)
    res = ad.send(SID_IDLE, "继续", model="claude-haiku-5-5", effort="low")
    assert res.ok, res.text
    argv = runner.claude_calls[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--effort") + 1] == "low"


# ---------- 📜 进度 / 📂 改动 ----------


def test_progress_reads_tail_without_sending(tmp_path, loop):
    b, claude, gid = _setup(tmp_path, loop)
    _click(b, "progress", gid)
    cards = _cards(b, gid)
    assert cards and "在跑" in cards[-1] and "没打扰它" in cards[-1]
    assert claude.relayed == [] and claude.sent == []


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def test_changes_shows_diff_stat_in_group(tmp_path, loop):
    repo = tmp_path / "demo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "init")
    (repo / "a.py").write_text("x = 2\ny = 3\n")
    (repo / "new.txt").write_text("n")
    b, claude, gid = _setup(tmp_path, loop, cwd=repo)
    _click(b, "changes", gid)
    said = _said(b, gid)
    assert "📂 demo 的改动" in said and "a.py" in said and "1 file changed" in said
    assert "new.txt" in said
    assert claude.relayed == []


def test_changes_outside_workspace_or_not_git(tmp_path, loop):
    b, _c, gid = _setup(tmp_path, loop, cwd="/")
    _click(b, "changes", gid)
    assert "不在手机能看的范围" in _said(b, gid)
    plain = tmp_path / "plain"
    plain.mkdir()
    b2, _c2, gid2 = _setup(tmp_path / "x", loop, cwd=plain)
    b2.settings.policy = b2.settings.policy.__class__(**{**b2.settings.policy.__dict__,
                                                          "workspaces": (str(tmp_path),)})
    _click(b2, "changes", gid2)
    assert "不是 git 仓库" in _said(b2, gid2)


def test_git_changes_times_out(tmp_path, monkeypatch):
    from bridge import app as app_mod

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(a[0], k.get("timeout"))

    monkeypatch.setattr(subprocess, "run", slow)
    assert "超过 5 秒" in app_mod._git_changes(str(tmp_path), 5.0)


# ---------- 接线：启动调 toolkit.start、建群调 ensure_bound ----------


def test_new_claude_group_calls_ensure_bound(tmp_path, monkeypatch):
    import bridge.fleet as fleet_pkg

    calls = []
    fake = types.SimpleNamespace(ensure_bound=lambda chat_id: calls.append(chat_id),
                                 start=lambda *a, **k: None, toolkit_ids=lambda: [])
    monkeypatch.setattr(fleet_pkg, "toolkit", fake, raising=False)
    monkeypatch.setitem(sys.modules, "bridge.fleet.toolkit", fake)
    b = _bridge(tmp_path, FakeClaude([_sess()]), FakeGroups())
    b.fleet.overview()
    _open_group(b, needs_confirm=False)
    assert calls == [b.chat_binds.of_session("claude", CID).chat_id]
    _open_group(b, needs_confirm=False)  # 复用 ⇒ 不再调
    assert len(calls) == 1


def test_ensure_bound_failure_does_not_break_group(tmp_path, monkeypatch):
    import bridge.fleet as fleet_pkg

    def boom(chat_id):
        raise RuntimeError("toolkit 坏了")

    fake = types.SimpleNamespace(ensure_bound=boom)
    monkeypatch.setattr(fleet_pkg, "toolkit", fake, raising=False)
    monkeypatch.setitem(sys.modules, "bridge.fleet.toolkit", fake)
    b = _bridge(tmp_path, FakeClaude([_sess()]), FakeGroups())
    b.fleet.overview()
    _open_group(b, needs_confirm=False)
    gid = b.chat_binds.of_session("claude", CID).chat_id
    assert "现在就能说" in _said(b, gid)


def test_run_starts_toolkit_with_click_handler():
    """`run()` 起不了真长连接，只钉住接线：调了 toolkit.start 且回调是 `_on_toolbar_click`，包在 try 里。"""
    import inspect

    from bridge.app import Bridge

    src = inspect.getsource(Bridge.run)
    i = src.index("toolkit.start(")
    assert "self._on_toolbar_click" in src[i:i + 300]
    assert "try:" in src[max(0, i - 200):i]
