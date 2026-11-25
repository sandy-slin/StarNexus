"""第 33 轮 FLEETACT：会话「结束」+「稍后提醒」。

盯这几件事：
- 结束：详情卡上有「✓ 结束」（在跑 / 等你的没有）；点了原地换「已结束 · 撤销」；总览不再列、底部写「已结束 N 个」
- 有新动静（updated_at 晚于结束时间 / 又在跑或等你了）⇒ 自己回来、删记录；撤销 ⇒ 回来
- 伪造：op 不认识、id 不在扫描里、多夹带键、snooze 的 q 不是十六进制 ⇒ 拒；伪造点「结束」在跑的 ⇒ toast 说明
- 稍后提醒：推送卡上有按钮；静音期间不推；问了新的照样推；一小时到了再推
- 落盘：config 同目录 settled.json、0600；文件损坏当没有；没有 config_path 不写盘；最多 500 条
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import Session  # noqa: E402
from bridge.fleet import settled as settled_mod  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_CARD_BYTES,
    build_detail_card,
    build_overview_card,
    resolve_fleet_click,
)
from bridge.fleet.model import AskQuestion, PendingAsk  # noqa: E402
from bridge.fleet.notify import WaitingNotifier, build_waiting_card  # noqa: E402
from bridge.fleet.scan import Overview, _sort_key  # noqa: E402
from bridge.fleet.settled import SettledStore, pending_sig  # noqa: E402

SID = "11111111-2222-4333-8444-555555555555"
SID2 = "22222222-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # 🔴 别让 `active()` 退到真机的 FEISHU_BRIDGE_CONFIG（那是 ~/.carher-feishu-agent）
    monkeypatch.delenv(settled_mod.CONFIG_ENV, raising=False)
    settled_mod._reset()
    yield
    settled_mod._reset()


def _sess(sid=SID, status="idle", age=600, title="修登录", tool="claude", pending=None, **kw):
    base = dict(tool=tool, surface="cli", id=sid, title=title, cwd="/Users/x/codes/demo",
                status=status, owner="none", updated_at=time.time() - age, can_send=True,
                pending=pending)
    base.update(kw)
    return Session(**base)


def _view(*sessions):
    return Overview(sessions=sorted(sessions, key=_sort_key), ok_tools=["claude", "codex", "cursor"],
                    at=time.time())


from tests._overview_rows import tap_as_button  # noqa: E402


def _walk(node, tag):
    out = []
    if isinstance(node, dict):
        if node.get("tag") == tag:
            out.append(node)
        elif tag == "button" and tap_as_button(node):  # 11-16 会话行是交互容器（tests/_overview_rows.py）
            out.append(tap_as_button(node))
        for v in node.values():
            out.extend(_walk(v, tag))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v, tag))
    return out


def _ops(card) -> list:
    return [b["behaviors"][0]["value"] for b in _walk(card, "button")]


def _open_ids(card) -> list:
    return [v.get("id") for v in _ops(card) if v.get("op") == "open"]


def _ask(question="用哪个方案？"):
    return PendingAsk(tool="AskUserQuestion", questions=(
        AskQuestion(question=question, options=(("A", ""), ("B", ""))),))


# ---------- 存储 ----------


def test_settle_hides_until_new_activity(tmp_path):
    store = SettledStore(str(tmp_path / "settled.json"))
    old = _sess(age=600)
    store.settle("claude", SID)
    shown, hidden = store.split([old])
    assert shown == [] and hidden == 1
    # 结束之后它又动了 ⇒ 回来、记录删掉
    newer = _sess(age=0)
    newer = Session(**{**newer.__dict__, "updated_at": time.time() + 5})
    shown, hidden = store.split([newer])
    assert shown == [newer] and hidden == 0
    assert store.settled_at("claude", SID) is None


def test_settled_session_comes_back_when_it_runs_again(tmp_path):
    store = SettledStore(str(tmp_path / "settled.json"))
    store.settle("claude", SID)
    # 时间戳没变但又在跑了（有的 adapter 在跑时不刷 updated_at）⇒ 回来
    running = _sess(status="running", age=600)
    shown, _ = store.split([running])
    assert shown == [running]
    assert store.count() == 0


def test_unsettle_brings_it_back(tmp_path):
    store = SettledStore(str(tmp_path / "settled.json"))
    store.settle("claude", SID)
    assert store.unsettle("claude", SID) is True
    assert store.split([_sess()])[1] == 0


def test_file_is_0600_and_survives_restart(tmp_path):
    path = tmp_path / "settled.json"
    SettledStore(str(path)).settle("claude", SID)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = SettledStore(str(path))
    assert again.settled_at("claude", SID) is not None
    assert not [p for p in os.listdir(tmp_path) if p != "settled.json"], "临时文件没清掉"


@pytest.mark.parametrize("blob", ["{坏的", "[]", '{"settled": "x"}', '{"settled": {"claude\\tx": "NaN"}}', ""])
def test_corrupt_file_reads_as_empty(tmp_path, blob):
    path = tmp_path / "settled.json"
    path.write_text(blob, encoding="utf-8")
    store = SettledStore(str(path))
    assert store.count() == 0
    shown, hidden = store.split([_sess()])
    assert len(shown) == 1 and hidden == 0
    store.settle("claude", SID)  # 坏文件上照样能记
    assert json.loads(path.read_text())["settled"]


def test_no_config_path_never_writes_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    store = settled_mod.store_for("")
    assert store.path == ""
    store.settle("claude", SID)
    store.snooze("claude", SID, "ab")
    assert store.settled_at("claude", SID) is not None
    assert list(tmp_path.iterdir()) == [], "没有 config_path 就不该写盘"


def test_path_is_next_to_config(tmp_path):
    cfg = tmp_path / "conf" / "config.toml"
    assert settled_mod.path_for(str(cfg)) == str(tmp_path / "conf" / "settled.json")


def test_keeps_at_most_500_newest(tmp_path):
    store = SettledStore(str(tmp_path / "settled.json"))
    for i in range(520):
        store.settle("claude", f"id-{i:04d}", at=1000 + i)
    data = json.loads((tmp_path / "settled.json").read_text())
    assert len(data["settled"]) == 500
    assert "claude\tid-0000" not in data["settled"] and "claude\tid-0519" in data["settled"]


def test_live_sessions_cannot_settle():
    assert settled_mod.can_settle(_sess(status="running"))[0] is False
    assert settled_mod.can_settle(_sess(status="waiting"))[0] is False
    assert settled_mod.can_settle(_sess(status="idle"))[0] is True


# ---------- 卡片 ----------


def test_detail_card_has_settle_only_when_not_live():
    idle = json.loads(build_detail_card(_sess(), []))
    assert {"act": "fleet", "op": "settle", "tool": "claude", "id": SID} in _ops(idle)
    for status in ("running", "waiting"):
        card = json.loads(build_detail_card(_sess(status=status, pids=(9,)), []))
        assert not [v for v in _ops(card) if v.get("op") == "settle"], status


def test_overview_drops_settled_and_says_how_many(tmp_path):
    store = SettledStore(str(tmp_path / "settled.json"))
    store.settle("claude", SID)
    view = _view(_sess(SID, title="结束了的"), _sess(SID2, title="还在的", age=700))
    blob = build_overview_card(view, settled=store, seen_at=None)
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    card = json.loads(blob)
    assert _open_ids(card) == [SID2]
    assert "已结束 1 个（有新动静会自己回来）" in blob
    # 不藏 ⇒ 都在、没有那句
    blob = build_overview_card(view, settled=None, seen_at=None)
    assert set(_open_ids(json.loads(blob))) == {SID, SID2}
    assert "已结束" not in blob


def test_overview_only_settled_does_not_say_nothing_read(tmp_path):
    store = SettledStore(str(tmp_path / "settled.json"))
    store.settle("claude", SID)
    blob = build_overview_card(_view(_sess()), settled=store, seen_at=None)
    assert "一个会话都没读到" not in blob
    assert "已结束 1 个" in blob


def test_overview_uses_bound_store_by_default(tmp_path):
    store = settled_mod.bind(settled_mod.store_for(str(tmp_path / "config.toml")))
    store.settle("claude", SID)
    card = json.loads(build_overview_card(_view(_sess()), seen_at=None))
    assert _open_ids(card) == []


def test_waiting_card_has_snooze_with_question_sig():
    sess = _sess(status="waiting", pending=_ask())
    values = _ops(json.loads(build_waiting_card(sess)))
    snooze = [v for v in values if v.get("op") == "snooze"]
    assert snooze == [{"act": "fleet", "op": "snooze", "tool": "claude", "id": SID, "q": pending_sig(sess)}]


# ---------- 白名单 ----------


def test_whitelist_new_ops():
    known = {("claude", SID)}
    assert resolve_fleet_click({"act": "fleet", "op": "settle", "tool": "claude", "id": SID}, known)
    assert resolve_fleet_click({"act": "fleet", "op": "unsettle", "tool": "claude", "id": SID}, known)
    got = resolve_fleet_click({"act": "fleet", "op": "snooze", "tool": "claude", "id": SID, "q": "ab12"}, known)
    assert got == {"op": "snooze", "tool": "claude", "id": SID, "q": "ab12"}
    for bad in (
        {"act": "fleet", "op": "settle", "tool": "claude", "id": SID2},  # 不在扫描里
        {"act": "fleet", "op": "settle", "tool": "vim", "id": SID},
        {"act": "fleet", "op": "settle", "tool": "claude", "id": SID, "q": "ab"},  # 只有 snooze 带 q
        {"act": "fleet", "op": "delete", "tool": "claude", "id": SID},
        {"act": "fleet", "op": "snooze", "tool": "claude", "id": SID, "q": "../x"},
        {"act": "fleet", "op": "snooze", "tool": "claude", "id": SID, "q": "a" * 17},
        {"act": "fleet", "op": "snooze", "tool": "claude", "id": SID, "q": 5},
        {"act": "fleet", "op": "snooze", "tool": "claude", "id": SID, "text": "x"},
    ):
        assert resolve_fleet_click(bad, known) is None, bad


# ---------- 点按钮（走真的 `_on_fleet_click`）----------


class _Claude:
    tool = "claude"

    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.last_error = ""

    def list(self, limit=20):
        return list(self.sessions)

    def running(self):
        return [s for s in self.sessions if s.pids]

    def find(self, sid):
        return next((s for s in self.sessions if s.id == sid), None)

    def read(self, sid, n=4):
        return []


def _bridge(tmp_path, sessions, config_path=""):
    from tests.test_relay_integration import _bridge as base

    b = base(tmp_path, _Claude(sessions))
    b.settings.config_path = config_path
    b.fleet.overview()
    return b


def _click(b, value, clicker=None):
    from tests.test_relay_integration import CHAT, OWNER

    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=CHAT))
    return b._on_fleet_click(event, clicker or OWNER, value)


def test_click_settle_swaps_card_and_hides_from_overview(tmp_path):
    cfg = tmp_path / "config.toml"
    b = _bridge(tmp_path, [_sess(), _sess(SID2, age=700)], str(cfg))
    resp = _click(b, {"act": "fleet", "op": "settle", "tool": "claude", "id": SID})
    assert resp.toast.type == "info"
    assert resp.card is not None and resp.card.type == "raw"
    blob = json.dumps(resp.card.data, ensure_ascii=False)
    assert "已结束" in blob and "电脑上那个会话没动" in blob
    assert {"act": "fleet", "op": "unsettle", "tool": "claude", "id": SID} in _ops(resp.card.data)
    assert not b.replier.cards and not b.replier.texts, "原地换卡就别再发一条"
    assert (tmp_path / "settled.json").exists()
    # 总览（默认用绑上的那份）不再列它
    card = json.loads(build_overview_card(b.fleet.last, seen_at=None))
    assert _open_ids(card) == [SID2]
    # 撤销 ⇒ 回来
    resp = _click(b, {"act": "fleet", "op": "unsettle", "tool": "claude", "id": SID})
    assert resp.card is not None and "已撤销" in json.dumps(resp.card.data, ensure_ascii=False)
    card = json.loads(build_overview_card(b.fleet.last, seen_at=None))
    assert set(_open_ids(card)) == {SID, SID2}


def test_forged_settle_on_running_session_is_refused(tmp_path):
    b = _bridge(tmp_path, [_sess(status="running", pids=(9,))])
    resp = _click(b, {"act": "fleet", "op": "settle", "tool": "claude", "id": SID})
    assert resp.toast.type == "error"
    assert "还在跑" in resp.toast.content
    assert resp.card is None
    assert settled_mod.active().count() == 0


def test_forged_settle_unknown_id_or_stranger_is_refused(tmp_path):
    b = _bridge(tmp_path, [_sess()])
    resp = _click(b, {"act": "fleet", "op": "settle", "tool": "claude", "id": SID2})
    assert resp.toast.type == "error" and resp.card is None
    resp = _click(b, {"act": "fleet", "op": "settle", "tool": "claude", "id": SID}, clicker="ou_stranger")
    assert resp.toast.type == "error" and resp.card is None
    assert settled_mod.active().count() == 0


def test_click_without_config_path_stays_in_memory(tmp_path):
    b = _bridge(tmp_path, [_sess()], config_path="")
    before = set(os.listdir(tmp_path))
    _click(b, {"act": "fleet", "op": "settle", "tool": "claude", "id": SID})
    assert set(os.listdir(tmp_path)) == before
    assert settled_mod.active().settled_at("claude", SID) is not None


def test_click_snooze_swaps_card(tmp_path):
    sess = _sess(status="waiting", pending=_ask())
    b = _bridge(tmp_path, [sess], str(tmp_path / "config.toml"))
    resp = _click(b, {"act": "fleet", "op": "snooze", "tool": "claude", "id": SID, "q": pending_sig(sess)})
    assert resp.toast.type == "info" and resp.card is not None
    assert "1 小时" in json.dumps(resp.card.data, ensure_ascii=False)
    assert settled_mod.active().snoozed(sess) is True
    data = json.loads((tmp_path / "settled.json").read_text())
    assert f"claude\t{SID}" in data["snoozed"]
    assert "用哪个方案" not in json.dumps(data, ensure_ascii=False), "问题原文不落盘"


# ---------- 稍后提醒：推送判断 ----------


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def _notifier(clock, store):
    return WaitingNotifier(now=clock, idle=lambda: None, snoozes=store)


def _wview(*sessions):
    return Overview(sessions=list(sessions), ok_tools=["claude"])


def test_snoozed_session_is_not_pushed_then_pushed_after_an_hour():
    clock = _Clock()
    store = SettledStore("", now=clock)
    n = _notifier(clock, store)
    sess = _sess(status="waiting", pending=_ask())
    n.due(_wview(sess))
    clock.t += 30
    assert [s.id for s in n.due(_wview(sess))] == [SID]
    n.record(sess.key, True)
    store.snooze("claude", SID, pending_sig(sess))
    # 静音期间：每会话 10 分钟冷却过了也不推
    for _ in range(5):
        clock.t += 11 * 60
        assert n.due(_wview(sess)) == []
    # 一小时到了还在等同一个问题 ⇒ 再推一次
    clock.t += 10 * 60
    n.due(_wview(sess))
    clock.t += 30
    assert [s.id for s in n.due(_wview(sess))] == [SID]


def test_new_question_during_snooze_is_pushed():
    clock = _Clock()
    store = SettledStore("", now=clock)
    n = _notifier(clock, store)
    first = _sess(status="waiting", pending=_ask("用哪个方案？"))
    n.due(_wview(first))
    clock.t += 30
    assert n.due(_wview(first))
    n.record(first.key, True)
    store.snooze("claude", SID, pending_sig(first))
    clock.t += 11 * 60
    assert n.due(_wview(first)) == []
    second = _sess(status="waiting", pending=_ask("要不要顺便改测试？"))
    n.due(_wview(second))
    clock.t += 30
    assert [s.id for s in n.due(_wview(second))] == [SID]


def test_snooze_store_failure_means_push():
    class Broken:
        def snoozed(self, sess):
            raise OSError("盘坏了")

    clock = _Clock()
    n = _notifier(clock, Broken())
    sess = _sess(status="waiting", pending=_ask())
    n.due(_wview(sess))
    clock.t += 30
    assert n.due(_wview(sess))
