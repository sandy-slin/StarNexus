"""第 32 轮 FLEETSTATUS：会话总览的状态分得更细（照 T3 手机列表）。

盯这几件事（tasks-r32「## FLEETSTATUS」）：
1. 「🟡 等你」拆成「🟡 等你批准」「❓ 等你回答」；读不出在问什么 ⇒ 还是「🟡 等你」
2. 「✅ 跑完了，你还没看」：最后一轮在上一次给这个人发总览卡之后才结束；记不到上一次 ⇒ 不显示
3. 「⚠️ 出错了」：只认读得到的失败信号（Claude 的 API 报错收尾、桥发的 Codex 那一轮 turn.failed）
4. 排序：等你批准 > 等你回答 > 出错了 > 在跑 > 跑完没看 > 闲着；折叠 / 图例 / 标题 / 文字兜底 /
   「还有 N 个」都认识新状态
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.fleet import Session  # noqa: E402
from bridge.fleet import cards  # noqa: E402
from bridge.fleet import claude as claude_mod  # noqa: E402
from bridge.fleet import codex as codex_mod  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_ROWS_PER_TOOL,
    OverviewSeen,
    build_detail_card,
    build_overview_card,
    display_status,
    group_summary,
    more_button_label,
    overview_fallback_lines,
    overview_headline,
    row_label,
    status_legend,
)
from bridge.fleet.model import AskQuestion, PendingAsk, RunResult  # noqa: E402
from bridge.fleet.scan import Overview, _sort_key  # noqa: E402

CA = "/Users/x/codes/carher-admin"
NOW = time.time()


def _sid(i: int) -> str:
    return f"{i:08x}-0000-4000-8000-000000000000"


def _sess(tool="claude", sid=None, title="修登录", status="idle", age=120, **kw):
    base = dict(tool=tool, surface="cli", id=sid or _sid(1), title=title, cwd=CA, status=status,
                owner="none", updated_at=NOW - age, can_send=True)
    base.update(kw)
    return Session(**base)


ASK = PendingAsk(tool="AskUserQuestion", questions=(
    AskQuestion(question="用哪种方案？", options=(("短信", ""), ("扫码", ""))),))
APPROVE = PendingAsk(tool="Bash", summary="rm -rf build")


def _approve(i=1, **kw):
    return _sess(sid=_sid(i), title="要跑命令", status="waiting", pending=APPROVE, **kw)


def _ask(i=2, **kw):
    return _sess(sid=_sid(i), title="问方案", status="waiting", pending=ASK, **kw)


def _failed(i=3, at=NOW - 60, **kw):
    return _sess(sid=_sid(i), title="报错了", last_turn="failed", last_turn_at=at, **kw)


def _done(i=4, at=NOW - 30, **kw):
    return _sess(sid=_sid(i), title="写完了", last_turn="done", last_turn_at=at, **kw)


def _running(i=5, **kw):
    return _sess(sid=_sid(i), title="在干活", status="running", pids=(9,), **kw)


def _idle(i=6, **kw):
    return _sess(sid=_sid(i), title="闲着的", **kw)


SEEN = NOW - 300  # 上一次给他发总览是 5 分钟前


@pytest.fixture(autouse=True)
def _fresh_seen(monkeypatch):
    """每个测试一份新的「上次发卡时间」，别串。"""
    monkeypatch.setattr(cards, "OVERVIEW_SEEN", OverviewSeen())


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


def _check(blob: str) -> dict:
    card = json.loads(blob)
    assert len(blob.encode("utf-8")) <= cards.MAX_CARD_BYTES
    return card


def _panel(card, name="Claude"):
    return next(p for p in _walk(card, "collapsible_panel") if f"**{name}**" in p["header"]["title"]["content"])


def _rows(panel):
    return [b["text"]["content"] for b in _walk(panel, "button") if b["behaviors"][0]["value"].get("op") == "open"]


# ---------- 1. 每种新状态都判得出来 ----------


def test_waiting_split_into_approve_and_ask():
    assert display_status(_approve()) == "approve"
    assert display_status(_ask()) == "ask"
    # 读不出在问什么 ⇒ 不乱拆，还是笼统的「等你」
    assert display_status(_sess(status="waiting")) == "waiting"
    assert cards.display_label(_approve()) == "🟡 等你批准"
    assert cards.display_label(_ask()) == "❓ 等你回答"


def test_ask_without_readable_questions_is_still_an_approval_shape_not_a_guess():
    # AskUserQuestion 的题读不出来时 claude.py 退成 summary 形状（tool 还是 AskUserQuestion）⇒ 仍算「等你回答」
    assert display_status(_sess(status="waiting", pending=PendingAsk(tool="AskUserQuestion"))) == "ask"


def test_failed_only_when_signal_read():
    assert display_status(_failed()) == "failed"
    assert cards.display_label(_failed()) == "⚠️ 出错了"
    assert display_status(_idle()) == "idle"  # 没信号 ⇒ 不标


def test_old_failure_does_not_stay_red_forever():
    old = _failed(at=NOW - cards.FAILED_FRESH_SEC - 60)
    assert display_status(old) == "idle"


def test_unseen_needs_known_seen_time():
    done = _done(at=NOW - 30)
    assert display_status(done, seen_at=SEEN) == "unseen"
    assert cards.display_label(done, SEEN) == "✅ 跑完了，你还没看"
    # 记不到上一次发卡 ⇒ 不显示这个状态（不猜）
    assert display_status(done, seen_at=None) == "idle"
    # 在上一次发卡**之前**就跑完了 ⇒ 他看过了
    assert display_status(_done(at=SEEN - 10), seen_at=SEEN) == "idle"


def test_running_and_waiting_ignore_last_turn():
    # 开着在跑的，最后一轮信号不该盖住「在跑」
    assert display_status(_running(last_turn="failed", last_turn_at=NOW)) == "running"


def test_session_drops_last_turn_without_time_or_bad_value():
    assert _sess(last_turn="done").last_turn == ""  # 没时间 ⇒ 没法比先后 ⇒ 当读不到
    assert _sess(last_turn="weird", last_turn_at=NOW).last_turn == ""
    assert _sess(last_turn="failed", last_turn_at="nan").last_turn == ""
    s = _sess(last_turn="done", last_turn_at=NOW)
    assert (s.last_turn, s.last_turn_at) == ("done", NOW)


# ---------- 2. Claude：从会话记录里读最后一轮 ----------


def _u(text, ts="2026-11-09T01:00:00.000Z", **kw):
    rec = {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts}
    rec.update(kw)
    return rec


def _a(blocks, stop="end_turn", ts="2026-11-09T01:00:05.000Z", **kw):
    rec = {"type": "assistant", "timestamp": ts,
           "message": {"role": "assistant", "stop_reason": stop, "content": blocks}}
    rec.update(kw)
    return rec


TEXT = [{"type": "text", "text": "好了"}]
API_ERR = dict(isApiErrorMessage=True, error="server_error")


def test_claude_last_turn_shapes():
    lt = claude_mod._last_turn
    assert lt([_u("hi"), _a(TEXT)])[0] == "done"
    assert lt([_u("hi"), _a(TEXT)])[1] == claude_mod._iso_to_epoch("2026-11-09T01:00:05.000Z")
    # 本机实测形状：API 报错是 model=<synthetic>、stop_sequence、isApiErrorMessage
    err = _a([{"type": "text", "text": "Request timed out"}], stop="stop_sequence", **API_ERR)
    assert lt([_u("hi"), err])[0] == "failed"
    # 报错后人又说了话（重试了）⇒ 那一轮不算失败；现在是在跑或被打断，说不准
    assert lt([_u("hi"), err, _u("再试一次")]) == ("", 0)
    # 一轮跑到一半
    assert lt([_u("hi"), _a([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], stop="tool_use")]) == ("", 0)
    assert lt([_u("hi"), _a([{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], stop="tool_use"),
               _u([{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])]) == ("", 0)
    # max_tokens / 空 stop_reason：不当成跑完了
    assert lt([_u("hi"), _a(TEXT, stop="max_tokens")]) == ("", 0)
    assert lt([_u("hi"), _a(TEXT, stop=None)]) == ("", 0)
    # 子 agent 的、系统塞的提醒不影响主线
    assert lt([_u("hi"), _a(TEXT), _a(TEXT, stop="tool_use", isSidechain=True),
               _u("<system-reminder>x</system-reminder>", isMeta=True)])[0] == "done"
    assert lt([]) == ("", 0)


from test_fleet_claude import _adapter as _claude_adapter, _jsonl, env  # noqa: E402,F401


def test_claude_history_session_carries_last_turn(env):
    sid_err = "99999999-9999-4999-8999-999999999999"
    work = str(env["work"])
    old = time.time() - 600
    _jsonl(str(env["proj"] / f"{sid_err}.jsonl"), [
        _u("跑一下", cwd=work, entrypoint="cli"),
        _a([{"type": "text", "text": "Please run /login"}], stop="stop_sequence",
           ts="2026-11-09T01:00:05.000Z", **API_ERR),
    ], mtime=old + 100)
    ad, _, _, _ = _claude_adapter(env)
    by_id = {s.id: s for s in ad.list(20)}
    assert by_id[sid_err].last_turn == "failed"
    # fixture 里 SID_IDLE 最后是「改好了」但没写 stop_reason ⇒ 不标
    from test_fleet_claude import SID_IDLE

    assert by_id[SID_IDLE].last_turn == ""


# ---------- 3. Codex：rollout 收尾 + 桥自己那一轮的 turn.failed ----------


from test_fleet_codex import (  # noqa: E402,F401
    T_CLI,
    FakeRunner as CodexRunner,
    _adapter as _codex_adapter,
    _make_db,
    _rollout,
    home,
)


def _ev(kind, ts):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": kind}}


def test_codex_turn_end_shapes():
    te = codex_mod._turn_end
    s, c = "2026-11-09T01:00:00.000Z", "2026-11-09T01:00:09.000Z"
    state, at, started = te([_ev("task_started", s), _ev("task_complete", c)])
    assert state == "done" and at == codex_mod._epoch(c) and started == codex_mod._epoch(s)
    assert te([_ev("task_started", s), _ev("turn_aborted", c)])[:2] == ("", 0)  # 被打断 ≠ 跑完
    assert te([_ev("task_complete", c), _ev("task_started", s)])[:2] == ("", 0)  # 在跑
    assert te([{"type": "event_msg", "payload": {"type": "task_complete"}}])[:2] == ("", 0)  # 没时间


def _codex_home_with_rollout(tmp_path, records):
    h = tmp_path / "ch"
    h.mkdir()
    proj = tmp_path / "p"
    proj.mkdir()
    path = _rollout(str(h), T_CLI, records=records)
    _make_db(str(h), [{"id": T_CLI, "source": "cli", "cwd": str(proj), "title": "t", "archived": 0,
                       "updated_at": 1793600945, "recency_at_ms": 1793600945000, "rollout_path": path}])
    return h


def test_codex_list_reads_done_from_rollout_path(tmp_path):
    h = _codex_home_with_rollout(tmp_path, [_ev("task_started", "2026-11-09T01:00:00.000Z"),
                                            _ev("task_complete", "2026-11-09T01:00:09.000Z")])
    ad, _, _ = _codex_adapter(h)
    sess = {s.id: s for s in ad.list(10)}[T_CLI]
    assert sess.last_turn == "done"
    assert sess.last_turn_at == codex_mod._epoch("2026-11-09T01:00:09.000Z")


def test_codex_without_rollout_path_is_not_marked(home):
    ad, _, _ = _codex_adapter(home)
    assert all(s.last_turn == "" for s in ad.list(10))


def test_codex_turn_failed_from_bridge_send_marks_failed_until_next_turn(tmp_path):
    h = _codex_home_with_rollout(tmp_path, [_ev("task_started", "2026-11-09T01:00:00.000Z"),
                                            _ev("task_complete", "2026-11-09T01:00:09.000Z")])
    runner = CodexRunner(codex_result=RunResult(1, stdout=json.dumps(
        {"type": "turn.failed", "error": {"message": "额度用完"}})))
    ad, _, _ = _codex_adapter(h, runner)
    assert not ad.send(T_CLI, "hi").ok
    sess = {s.id: s for s in ad.list(10)}[T_CLI]
    assert sess.last_turn == "failed" and display_status(sess) == "failed"
    # 之后（电脑上）又开了新的一轮 ⇒ 那次失败作废
    later = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() + 60))
    _rollout(str(h), T_CLI, records=[_ev("task_started", later), _ev("task_complete", later)])
    assert {s.id: s for s in ad.list(10)}[T_CLI].last_turn == "done"


def test_codex_busy_writer_is_not_a_model_failure(tmp_path):
    h = _codex_home_with_rollout(tmp_path, [])
    runner = CodexRunner(codex_result=RunResult(1, stdout=json.dumps(
        {"type": "error", "message": "thread has an active writer"})))
    ad, _, _ = _codex_adapter(h, runner)
    ad.send(T_CLI, "hi")
    assert {s.id: s for s in ad.list(10)}[T_CLI].last_turn == ""


# ---------- 4. 排序 ----------


def _all_kinds():
    return [_idle(6, age=1), _done(4, age=2), _running(5, age=3), _failed(3, age=4), _ask(2, age=5),
            _approve(1, age=6)]


def test_rows_sorted_by_priority():
    view = Overview(sessions=sorted(_all_kinds(), key=_sort_key), at=NOW)
    rows = _rows(_panel(_check(build_overview_card(view, seen_at=SEEN))))
    heads = [r.split(" ", 1)[0] for r in rows]
    assert heads == ["🟡", "❓", "⚠️", "🟢", "✅", "⚪"], rows
    assert [r.split(" ", 1)[1].split(" · ")[0] for r in rows] == \
        ["要跑命令", "问方案", "报错了", "在干活", "写完了", "闲着的"]


def test_unseen_and_failed_are_not_hidden_behind_idle_rows():
    """一组里 20 个闲着的比它们新：跑完没看 / 出错了 也得排进前 8 行。"""
    items = [_idle(100 + i, age=i) for i in range(20)] + [_done(4, age=999), _failed(3, age=999)]
    view = Overview(sessions=sorted(items, key=_sort_key), at=NOW)
    rows = _rows(_panel(_check(build_overview_card(view, seen_at=SEEN))))
    assert len(rows) == MAX_ROWS_PER_TOOL
    assert rows[0].startswith("⚠️") and rows[1].startswith("✅")


def test_only_urgent_rows_are_primary():
    view = Overview(sessions=_all_kinds(), at=NOW)
    btns = [b for b in _walk(_check(build_overview_card(view, seen_at=SEEN)), "button")
            if b["behaviors"][0]["value"].get("op") == "open"]
    primary = [b["text"]["content"].split(" ", 1)[0] for b in btns if b["type"] == "primary"]
    assert primary == ["🟡", "❓"]


# ---------- 5. 折叠 ----------


@pytest.mark.parametrize("make,expanded", [
    (lambda: _done(at=NOW - 30), True),       # 跑完没看 ⇒ 展开
    (lambda: _failed(), True),                # 出错了 ⇒ 展开
    (lambda: _approve(), True),
    (lambda: _ask(), True),
    (lambda: _done(at=SEEN - 10), False),     # 看过了 ⇒ 就是闲着 ⇒ 收起
    (lambda: _idle(), False),
])
def test_fold_rules(make, expanded):
    view = Overview(sessions=[make()], at=NOW)
    assert _panel(_check(build_overview_card(view, seen_at=SEEN)))["expanded"] is expanded


def test_unseen_group_collapsed_when_seen_time_unknown():
    view = Overview(sessions=[_done(at=NOW - 30)], at=NOW)
    assert _panel(_check(build_overview_card(view, seen_at=None)))["expanded"] is False


def test_group_summary_names_new_states():
    title = group_summary([_approve(), _ask(), _failed(), _done(), _idle()], SEEN)
    assert title == "🟡 1 个等你批准，❓ 1 个等你回答，⚠️ 1 个出错了，✅ 1 个跑完了你还没看"
    assert group_summary([_idle(), _idle(7)], SEEN) == "都闲着"


# ---------- 6. 图例 / 标题 / 按钮 / 文字兜底 ----------


def test_legend_lists_only_seen_states_in_priority_order():
    assert status_legend([_idle(), _failed()], SEEN) == "⚠️ 出错了 · ⚪ 闲着"
    assert status_legend([_done(), _ask()], SEEN) == "❓ 等你回答 · ✅ 跑完了，你还没看"
    assert status_legend([_done()], None) == "⚪ 闲着"  # 不知道上次发卡 ⇒ 不出现「没看」
    assert "等你批准" not in status_legend([_ask()], SEEN)


def test_legend_on_card():
    view = Overview(sessions=[_idle(), _approve()], at=NOW)
    card = _check(build_overview_card(view, seen_at=SEEN))
    legends = [m["content"] for m in _walk(card, "markdown") if m.get("text_size") == "notation"
               and "⚪ 闲着" in m["content"]]
    assert legends == ["🟡 等你批准 · ⚪ 闲着"]


def test_headline_counts_new_states():
    line = overview_headline([_approve(), _ask(), _failed(), _running(), _done()], SEEN)
    assert line == ("🟡 **1 个等你批准**，❓ **1 个等你回答**，⚠️ 1 个出错了，🟢 1 个在跑，"
                    "✅ 1 个跑完了你还没看。")
    assert overview_headline([_idle()], SEEN) == "电脑上现在没有开着的会话。"
    # 老的「在等你」说法：读不出在问什么时照旧
    assert "🟡 **1 个在等你**" in overview_headline([_sess(status="waiting")])


def test_more_button_names_hidden_new_states():
    hidden = [_failed(i) for i in range(10, 13)] + [_done(i) for i in range(20, 22)]
    # 两半放不下一行（48 格）⇒ 留优先级高的那半（出错了 > 跑完没看）
    label = more_button_label(hidden, SEEN)
    assert label == "⚠️ 还有 5 个（3 个出错了），点开看全部"
    assert cards.display_width(label) <= cards.OVERVIEW_ROW_CELLS
    # 只藏着跑完没看的 ⇒ 点名（短说法）
    assert more_button_label([_done(20), _done(21)], SEEN) == "⚠️ 还有 2 个（2 个跑完没看），点开看全部"
    # 不知道上次发卡 ⇒ 就是更早的闲着的
    assert more_button_label([_done(20)], None) == "还有 1 个更早的没列出来，点开看全部"
    # 太长 ⇒ 从优先级低的那头丢，「点开看全部」留着
    many = [_approve(i) for i in range(30, 40)] + [_ask(i) for i in range(40, 50)] + hidden
    label = more_button_label(many, SEEN)
    assert label.endswith("点开看全部") and "等你批准" in label
    assert cards.display_width(label) <= cards.OVERVIEW_ROW_CELLS


def test_hidden_rows_and_overflow_notes_name_new_states():
    assert "1 个出错了" in cards.hidden_rows_note([_failed()], SEEN)
    assert "1 个跑完了你还没看" in cards.overflow_note([_done()], SEEN)


def test_text_fallback_writes_states_as_words():
    lines = overview_fallback_lines([_idle(), _done(), _failed(), _approve(), _ask()], seen_at=SEEN)
    rows = [ln for ln in lines if not ln.startswith("  ")]
    assert ["等你批准" in rows[0], "等你回答" in rows[1], "出错了" in rows[2],
            "跑完了，你还没看" in rows[3], "闲着" in rows[4]] == [True] * 5
    # 状态是字，不是光一个点
    assert not any(r.startswith(("🟡", "❓", "⚠️", "✅", "⚪")) for r in rows)
    # 等你的紧跟它在问什么
    assert lines[1].strip().startswith("⏸ 它在等你确认") and lines[3].strip().startswith("❓ 它在问你")
    assert "出错了" in row_label(_failed())


def test_detail_card_uses_finer_label():
    blob = build_detail_card(_approve(), [])
    assert "等你批准" in blob
    assert "出错了" in build_detail_card(_failed(), [])


# ---------- 7. 「上一次发卡」只记在内存，自动接上 ----------


def test_seen_time_recorded_per_card_and_expand_reuses_it():
    first = Overview(sessions=[_done(at=NOW - 30)], at=NOW - 60)
    # 第一张：从没发过 ⇒ 不知道 ⇒ 不标「没看」
    assert "跑完了" not in build_overview_card(first)
    # 第二张：那一轮在第一张之后结束 ⇒ 标出来
    second = Overview(sessions=[_done(at=NOW - 30)], at=NOW)
    assert "跑完了，你还没看" in build_overview_card(second)
    # 「看全部」原地换卡：同一次扫描，还是标着（不会因为刚发过一次就没了）
    assert "跑完了，你还没看" in build_overview_card(second, expand="claude")
    # 第三张：他已经在第二张上看过了
    third = Overview(sessions=[_done(at=NOW - 30)], at=NOW + 60)
    assert "跑完了" not in build_overview_card(third)


def test_overview_seen_store():
    seen = OverviewSeen()
    assert seen.before("a") is None
    seen.mark("a", 100)
    seen.mark("a", 200)
    seen.mark("a", 0)  # 没时间的不记
    assert seen.before("a") == 200
    assert seen.before("a", 200) == 100
    assert seen.before("a", 100) is None
    assert seen.before("b") is None and seen.before("") is None
