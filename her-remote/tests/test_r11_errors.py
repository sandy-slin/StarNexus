"""第 11 轮角色 B：出错时别把原始异常（带本机路径、英文异常名、两层前缀）发到手机上。

复现原文见 docs/iteration/review-r9.md 第 8 条：
    ❌ 没发进去：没发出去：[Errno 2] No such file or directory: '/Users/x/.claude/projects/abc.jsonl'
    总览 notes: ["Claude：读 Claude 出错了：'pids'"]

三件事一起盯：
1. 给用户的文字：没有 `/Users/`、没有 Errno / 英文异常类名、没有「没发进去：没发出去：」
2. 🔴 阳性对照：日志里**仍然有**原文（排查要用）—— 两边别弄反
3. 调用点还在：`bridge/fleet/` 那几个文件里 `except … as exc` 之后不许再把 `exc` 拼进字符串
"""

from __future__ import annotations

import ast
import asyncio
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import time
import types
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = pathlib.Path(os.path.dirname(_HERE))
sys.path.insert(0, str(_ROOT))

from bridge.fleet import ActionResult, Message, Session  # noqa: E402
from bridge.fleet.control import FleetControl  # noqa: E402
from bridge.fleet.model import (  # noqa: E402
    ERR_BUSY_DB,
    ERR_NETWORK,
    ERR_NOT_FOUND,
    ERR_OTHER,
    ERR_PERMISSION,
    ERR_TIMEOUT,
    SubprocessRunner,
    friendly_error,
)
from bridge.fleet.scan import Fleet  # noqa: E402

LEAK_PATH = "/Users/someone/.claude/projects/abc.jsonl"
CID = "44444444-4444-4444-8444-444444444444"
TID = "019a0000-0000-7000-8000-000000000001"
LOGGER = "feishu-bridge.fleet"

# 手机上不该出现的东西：本机路径、Errno、Python 异常类名、Traceback、两层前缀
_EXC_NAMES = re.compile(r"\b[A-Z][A-Za-z]*(Error|Exception|Expired)\b")


def _assert_clean(text: str) -> None:
    assert text, "报错时一个字都没给用户"
    assert "/Users/" not in text, f"本机路径漏到手机上了：{text!r}"
    assert "Errno" not in text and "Traceback" not in text, f"英文异常原文漏到手机上了：{text!r}"
    assert not _EXC_NAMES.search(text), f"英文异常类名漏到手机上了：{text!r}"
    assert "没发出去：没发出去" not in text and "没发进去：没发出去" not in text, f"两层前缀：{text!r}"


def _boom():
    return FileNotFoundError(2, "No such file or directory", LEAK_PATH)


@pytest.fixture
def here_dir():
    """落盘夹具放在测试目录旁边，**不放 tmp_path**（macOS 上 tmp_path 是 pathguard 豁免前缀，
    而且这条测试要的恰恰是「路径在 /Users/ 下面」—— tmp_path 在 /private/var 下会假绿）。"""
    base = pathlib.Path(_HERE).parent / f".r11-errors-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------- 一、统一函数本身 ----------


@pytest.mark.parametrize("exc, expected", [
    (TimeoutError("timed out after 30s"), ERR_TIMEOUT),
    (FileNotFoundError(2, "No such file or directory", LEAK_PATH), ERR_NOT_FOUND),
    (PermissionError(13, "Permission denied", LEAK_PATH), ERR_PERMISSION),
    (ConnectionResetError(54, "Connection reset by peer"), ERR_NETWORK),
    (sqlite3.OperationalError(f"unable to open database file {LEAK_PATH}"), ERR_BUSY_DB),
    (KeyError("pids"), ERR_OTHER),
    (RuntimeError(f"[Errno 2] No such file or directory: '{LEAK_PATH}'"), ERR_OTHER),
])
def test_friendly_error_by_kind(exc, expected, caplog):
    with caplog.at_level("ERROR", logger=LOGGER):
        text = friendly_error(exc, "读不到进程列表")
    assert text == f"读不到进程列表：{expected}"
    _assert_clean(text)
    assert "'pids'" not in text
    # 每一类都给下一步
    assert any(k in text for k in ("再试", "刷新", "电脑上")), text
    # 🔴 阳性对照：日志里原文还在（类型 + 内容）
    assert type(exc).__name__ in caplog.text and str(exc) in caplog.text


def test_friendly_error_without_doing_has_no_prefix():
    """发送失败那条：app.py 会套「❌ 没发进去：」，这里不能再带一层。"""
    text = friendly_error(_boom())
    assert text == ERR_NOT_FOUND and "：" not in text.split("，")[0][:3]


def test_friendly_error_logs_the_traceback_under_log_as(caplog):
    try:
        raise _boom()
    except FileNotFoundError as exc:
        with caplog.at_level("ERROR", logger=LOGGER):
            friendly_error(exc, log_as="会话总控发送失败 tool=claude")
    rec = [r for r in caplog.records if "会话总控发送失败 tool=claude" in r.getMessage()]
    assert rec and rec[0].exc_info, "日志里没留调用栈"
    assert LEAK_PATH in caplog.text


# ---------- 二、各个出口：send / relay / overview / refresh / detail / stop ----------


class Exploding:
    """什么都抛 FileNotFoundError(/Users/…) 的 adapter。"""

    def __init__(self, tool="claude"):
        self.tool = tool
        self.last_error = ""

    def list(self, limit=20):
        raise _boom()

    def running(self):
        raise _boom()

    def find(self, sid):
        raise _boom()

    def read(self, sid, n=4):
        raise _boom()

    def send(self, sid, text, **kw):
        raise _boom()

    def can_relay(self, sid):
        raise _boom()

    def relay(self, sid, text, **kw):
        raise _boom()

    def stop(self, sid):
        raise _boom()


def test_control_send_is_clean_and_logged(caplog):
    ctl = FleetControl(Fleet({"claude": Exploding()}))
    with caplog.at_level("ERROR", logger=LOGGER):
        res = ctl.send("claude", CID, "在吗")
    assert not res.ok and not res.delivered
    _assert_clean(res.text)
    assert not res.text.startswith("没发出去"), "前缀留给 app.py 那一层，这里再说就是两层"
    assert LEAK_PATH in caplog.text


def test_control_relay_check_is_clean(caplog):
    """relay=True：先走 can_relay —— 它抛了也只给人话。"""
    ctl = FleetControl(Fleet({"claude": Exploding()}))
    with caplog.at_level("ERROR", logger=LOGGER):
        res = ctl.send("claude", CID, "在吗", relay=True)
    _assert_clean(res.text)
    assert LEAK_PATH in caplog.text


def test_control_relay_codex_is_clean(caplog):
    ctl = FleetControl(Fleet({"codex": Exploding("codex")}))
    with caplog.at_level("ERROR", logger=LOGGER):
        res = ctl.send("codex", TID, "在吗", relay=True)
    _assert_clean(res.text)
    assert LEAK_PATH in caplog.text


def test_overview_notes_are_clean(caplog):
    """复现原文：`["Claude：读 Claude 出错了：'pids'"]`。"""

    class KeyErr(Exploding):
        def list(self, limit=20):
            raise KeyError("pids")

    with caplog.at_level("ERROR", logger=LOGGER):
        view = Fleet({"claude": Exploding(), "codex": KeyErr("codex")}).overview()
    assert len(view.notes) == 2, view.notes
    for note in view.notes:
        _assert_clean(note)
        assert "'pids'" not in note
    # 家名只出现一次（原来是「Claude：读 Claude 出错了」）
    assert all(note.count("Claude") + note.count("Codex") == 1 for note in view.notes), view.notes
    assert LEAK_PATH in caplog.text and "pids" in caplog.text


def test_overview_submit_failure_is_clean(caplog):
    fleet = Fleet({"claude": Exploding()})
    fleet._pool.shutdown(wait=False)  # 线程池关了 ⇒ submit 抛 RuntimeError
    with caplog.at_level("ERROR", logger=LOGGER):
        view = fleet.overview()
    assert view.notes
    for note in view.notes:
        _assert_clean(note)
    assert "RuntimeError" in caplog.text


def test_overview_future_failure_is_clean(caplog, monkeypatch):
    """`fut.result()` 自己抛（不是 adapter 抛）那条兜底。"""
    fleet = Fleet({"claude": Exploding()})

    def bad_one(tool, adapter, limit):
        raise PermissionError(13, "Permission denied", LEAK_PATH)

    monkeypatch.setattr(fleet, "_one", bad_one)
    with caplog.at_level("ERROR", logger=LOGGER):
        view = fleet.overview()
    assert view.notes
    for note in view.notes:
        _assert_clean(note)
    assert LEAK_PATH in caplog.text


def test_refresh_detail_stop_are_clean(caplog):
    ctl = FleetControl(Fleet({"claude": Exploding()}))
    with caplog.at_level("ERROR", logger=LOGGER):
        sess, why = ctl.refresh("claude", CID)
        _, messages, note = ctl.detail("claude", CID)
        stop = ctl.stop("claude", CID)
    assert sess is None and "读不出" in why  # 「读不出」≠「没了」那条约定还在
    for text in (why, note, stop.text):
        _assert_clean(text)
    assert caplog.text.count(LEAK_PATH) >= 3


def test_real_claude_adapter_send_and_relay_are_clean(caplog, monkeypatch, here_dir):
    from bridge.fleet.claude import ClaudeAdapter
    from bridge.fleet.model import FleetJobs

    ad = ClaudeAdapter(home=str(here_dir), bridge_home=str(here_dir / "b"), app_root=str(here_dir / "a"),
                       jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None))
    monkeypatch.setattr(ad, "_send_locked", lambda *a, **k: (_ for _ in ()).throw(_boom()))
    monkeypatch.setattr(ad, "_relay", lambda: (_ for _ in ()).throw(_boom()))
    with caplog.at_level("ERROR", logger=LOGGER):
        sent = ad.send(CID, "在吗")
        relayed = ad.relay(CID, "在吗")
        can, why = ad.can_relay(CID)
    for text in (sent.text, relayed.text, why):
        _assert_clean(text)
    assert not can
    assert caplog.text.count(LEAK_PATH) >= 3


def test_real_codex_adapter_send_and_relay_are_clean(caplog, monkeypatch, here_dir):
    from bridge.fleet.codex import CodexAdapter
    from bridge.fleet.model import FleetJobs

    ad = CodexAdapter(home=str(here_dir), jobs=FleetJobs(lstart_of=lambda p: "", kill=lambda *a: None),
                      codex_bin="/fake/codex", lsof_bin="/usr/sbin/lsof")
    monkeypatch.setattr(ad, "_send_locked", lambda *a, **k: (_ for _ in ()).throw(_boom()))
    monkeypatch.setattr(ad, "_relay_locked", lambda *a, **k: (_ for _ in ()).throw(_boom()))
    with caplog.at_level("ERROR", logger=LOGGER):
        sent = ad.send(TID, "在吗")
        relayed = ad.relay(TID, "在吗")
    for text in (sent.text, relayed.text):
        _assert_clean(text)
    assert caplog.text.count(LEAK_PATH) >= 2


def test_relay_send_is_clean(caplog, monkeypatch):
    """`Relay.send` 自己那层兜底（复审原文就是从这条路出来的）。"""
    from bridge.fleet.relay import Relay

    rl = Relay.__new__(Relay)
    import threading

    rl._lock = threading.Lock()
    rl._blocked_attempt = lambda sid, text: None
    rl._send_locked = lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError(f"[Errno 2] No such file or directory: '{LEAK_PATH}'"))
    with caplog.at_level("ERROR", logger=LOGGER):
        res = rl.send(CID, "在吗")
    _assert_clean(res.text)
    assert LEAK_PATH in caplog.text


def test_courier_write_failure_keeps_stderr_tail_in_the_log_only(caplog, here_dir):
    from bridge.fleet.relay import Courier

    class Pipe:
        def write(self, s):
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self):
            pass

    c = Courier.__new__(Courier)
    c._proc = types.SimpleNamespace(stdin=Pipe())
    c._stderr = [f"error: cannot open {LEAK_PATH}"]
    c.ensure = lambda: ""
    c.close = lambda: None
    with caplog.at_level("ERROR", logger=LOGGER):
        why = c.write("修登录", "在吗")
    assert why.startswith("信使没把这条写出去")
    _assert_clean(why)
    assert LEAK_PATH in caplog.text, "stderr 尾巴连日志里都没了，排查没得查"


def test_subprocess_runner_missing_binary_under_users_is_clean(caplog, here_dir):
    """真起一次：不存在的程序放在 /Users/… 下（here_dir 就在仓库里）。
    这句 error 会经 `_send_result` / codex / cursor 直接成为 ActionResult.text。"""
    missing = here_dir / "no-such-cli"
    assert str(missing).startswith("/Users/"), "夹具不在 /Users 下，这条测试会假绿"
    with caplog.at_level("ERROR", logger=LOGGER):
        res = SubprocessRunner().run([str(missing)], timeout=2)
    assert res.error and not res.ok
    assert res.error.startswith("没能启动 no-such-cli：")
    _assert_clean(res.error)
    assert str(missing) in caplog.text


def test_launch_failure_is_not_double_prefixed(here_dir):
    """`没能启动 claude：没能启动 /Users/…/claude：…` 也是两层前缀（还漏路径）。"""
    from bridge.fleet.claude import _send_result
    from bridge.fleet.model import RunResult

    err = SubprocessRunner().run([str(here_dir / "claude")], timeout=2).error
    text = _send_result(CID, RunResult(returncode=-1, error=err)).text
    assert text.count("没能启动") == 1, text
    _assert_clean(text)
    # 阳性对照：别的 error（不是 runner 给的）照旧带上「没能启动 claude：」
    assert _send_result(CID, RunResult(returncode=-1, error="找不到")).text == "没能启动 claude：找不到"


# ---------- 三、到手机上的那一句（走 app.py 真路径） ----------


def _bridge(base, adapter):
    """和 tests/test_relay_integration.py 的 `_bridge` 同形（假 replier、不起真 CLI）。"""
    from bridge.app import Bridge
    from bridge.approval import ApprovalGate
    from bridge.fleet.consent import RelayConsent
    from bridge.policy import Policy
    from bridge.review import Reviewer

    owner, chat = "ou_owner", "oc_chat"
    scope = f"dm:{owner}"

    class Replier:
        def __init__(self):
            self.texts, self.cards = [], []

        def send(self, chat_id, text):
            self.texts.append(text)
            return True

        def send_card(self, chat_id, card):
            self.cards.append(card)
            return True

    b = Bridge.__new__(Bridge)
    pol = Policy(mode="readonly", workspaces=(str(base),), default_workspace=str(base),
                 model="claude-sonnet-5-5", turn_timeout_sec=60, ceiling_mode="trusted")
    b.settings = types.SimpleNamespace(owner_open_ids=frozenset({owner}), policy=pol, custom_rules=())
    b.replier = Replier()
    b._chat_ids = {scope: chat}
    b._paused, b._awaiting, b._cancels, b._pending_consent = {}, {}, {}, {}
    b._loop = None
    b.reviewer = Reviewer()
    b.gate = ApprovalGate(str(base / "approvals"))
    b.fleet = FleetControl(Fleet({adapter.tool: adapter}, timeout=1.0))
    b._fleet_inflight = {}
    b.relay_consent = RelayConsent(str(base / "relay_consent.json"))

    async def fake_run_one(session, chat_id, text):
        raise AssertionError("不该落到桥自己的 claude")

    b._run_one = fake_run_one
    b.sessions = types.SimpleNamespace(get=lambda s: types.SimpleNamespace(
        scope=s, workspace=str(base), lock=asyncio.Lock(), waiting=0))
    b.workspaces = types.SimpleNamespace(get=lambda s: str(base))
    return b, scope, chat, owner


class LiveButBroken:
    """复审原样：终端里开着的会话，relay 抛 `[Errno 2] … '/Users/x/…'`。"""

    tool = "claude"

    def __init__(self, cwd):
        self.last_error = ""
        self.sess = Session(tool="claude", surface="cli", id=CID, title="修登录", cwd=cwd,
                            status="running", pids=(4242,), owner="terminal",
                            updated_at=time.time() - 60, can_send=False, can_relay=True)

    def list(self, limit=20):
        return [self.sess]

    def running(self):
        return [self.sess]

    def find(self, sid):
        return self.sess if sid == CID else None

    def read(self, sid, n=4):
        return [Message("assistant", "之前答的")]

    def can_relay(self, sid):
        return True, ""

    def relay(self, sid, text, timeout=120.0, mode=None):
        # 真 adapter 的 relay 自己会兜住 ⇒ 这里模拟「没兜住」，最外层 control.send 那层接
        raise RuntimeError(f"[Errno 2] No such file or directory: '{LEAK_PATH}'")

    def send(self, sid, text, **kw):
        raise AssertionError("终端开着的会话不该走 send")

    def stop(self, sid):
        return ActionResult(False, "到电脑上停", sid)


def test_phone_never_sees_the_raw_exception(caplog, here_dir):
    """review-r9 第 8 条原样：原来回的是
    `❌ 没发进去：没发出去：[Errno 2] No such file or directory: '/Users/x/.claude/…'`"""
    b, scope, chat, owner = _bridge(here_dir, LiveButBroken(str(here_dir)))
    b.fleet.overview()
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=chat))
    b._on_fleet_click(event, owner, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    b._on_relay_click(event, owner, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    with caplog.at_level("ERROR", logger=LOGGER):
        asyncio.run(b._handle(scope, chat, "继续"))
    reply = b.replier.texts[-1]
    assert reply.startswith("❌ 没发进去：")
    _assert_clean(reply)
    assert LEAK_PATH in caplog.text, "原文连日志里都没了"


# ---------- 四、调用点还在（查调用形状，不查名字） ----------

FILES = ("claude.py", "codex.py", "cursor.py", "control.py", "scan.py", "relay.py", "model.py")
# 每个文件里至少有这么多处 `friendly_error(exc…)` 调用 —— 少了就是有一处被改回去了
MIN_CALLS = {"claude.py": 9, "codex.py": 9, "cursor.py": 5, "control.py": 6, "scan.py": 3, "relay.py": 4,
             "model.py": 4}


def _tree(name):
    return ast.parse((_ROOT / "bridge" / "fleet" / name).read_text(encoding="utf-8"))


def _is_exc_ref(node, names):
    """`exc` / `str(exc)` / `repr(exc)` / `exc.args` …"""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
    return False


@pytest.mark.parametrize("name", FILES)
def test_no_exception_text_spliced_into_strings(name):
    """`except … as exc` 里不许把 exc 拼进 f-string / 用 + 接进字符串 / str(exc)。
    logger 的 `%s` 参数不算（那是日志）。"""
    offenders = []
    for handler in ast.walk(_tree(name)):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        names = {handler.name}
        for node in ast.walk(handler):
            if isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.FormattedValue) and _is_exc_ref(part.value, names):
                        offenders.append((node.lineno, "f-string"))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("str", "repr", "format") and any(_is_exc_ref(a, names) for a in node.args):
                offenders.append((node.lineno, node.func.id))
            elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)) \
                    and (_is_exc_ref(node.left, names) or _is_exc_ref(node.right, names)):
                offenders.append((node.lineno, "concat"))
    assert not offenders, f"bridge/fleet/{name} 还在把异常原文拼给用户：{offenders}"


@pytest.mark.parametrize("name", FILES)
def test_except_handlers_route_through_friendly_error(name):
    """调用形状：`friendly_error(<except 绑定的那个名字>, …)` 真的在 except 块里被调到。"""
    calls = 0
    for handler in ast.walk(_tree(name)):
        if not isinstance(handler, ast.ExceptHandler) or not handler.name:
            continue
        for node in ast.walk(handler):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "friendly_error" and node.args \
                    and isinstance(node.args[0], ast.Name) and node.args[0].id == handler.name:
                calls += 1
    assert calls >= MIN_CALLS[name], f"bridge/fleet/{name} 只有 {calls} 处走 friendly_error，应 ≥ {MIN_CALLS[name]}"
