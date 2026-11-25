"""第 28 轮：总览每组折叠 +「还有 N 个」点了能展开。

用户原话（2026-11-08）：「如果没有在跑的任务就收缩，用户点击对应展开再展开，比如 codex 和 cursor
没有在跑的会话，那就收缩起来。」「『还有 28 个更早的没列出来』这个用户点上去要展开。
不能一句还没有列出来就什么都不管了。」

盯这几件事：
- 每组一个 `collapsible_panel`：有在跑 / 等你 ⇒ expanded；全闲着 / 挂起 ⇒ 收起
- 收起时标题自己说清楚：工具名 + 项目 + 个数 + 状态摘要
- 「等你」那几行照旧是主按钮、紧跟「它在问你…」
- 「还有 N 个」是按钮（op=more，只带 tool），白名单只认已知 tool、不认 id、不认多余键
- 点了 ⇒ 回调响应体带新卡（原地换），那一组全列出、别的组照旧；用的是上次扫描，不现扫
- 全列出来超 20KB / 200 组件 ⇒ 少放几行 + 写清放不下几个，但比原来多很多
"""

from __future__ import annotations

import json
import os
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import Session  # noqa: E402
from bridge.fleet import cards  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_CARD_BYTES,
    MAX_ROWS_PER_TOOL,
    build_overview_card,
    fleet_value,
    resolve_fleet_click,
)
from bridge.fleet.model import AskQuestion, PendingAsk  # noqa: E402
from bridge.fleet.scan import Overview, _sort_key  # noqa: E402

CA = "/Users/x/codes/carher-admin"


def _sess(tool="claude", sid="11111111-2222-4333-8444-555555555555", title="修登录", cwd=CA,
          status="idle", age=120, **kw):
    base = dict(tool=tool, surface="cli", id=sid, title=title, cwd=cwd, status=status,
                owner="none", updated_at=time.time() - age if age is not None else 0, can_send=True)
    base.update(kw)
    return Session(**base)


def _sid(i: int) -> str:
    return f"{i:08x}-0000-4000-8000-000000000000"


def _ask():
    return PendingAsk(tool="AskUserQuestion", questions=(
        AskQuestion(question="登录页要用哪种方案？", header="方案", multi=False,
                    options=(("短信验证码", "最快上线"), ("飞书扫码", "同事都有"))),))


def _check(blob: str) -> dict:
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    assert cards._components(card) <= cards.MAX_CARD_COMPONENTS
    return card


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


def _panels(card) -> dict:
    """工具名 → 那个折叠面板。"""
    out = {}
    for p in _walk(card, "collapsible_panel"):
        title = p["header"]["title"]["content"]
        for name, tool in (("Claude", "claude"), ("Codex", "codex"), ("Cursor", "cursor")):
            if f"**{name}**" in title:
                out[tool] = p
    return out


def _opens(node) -> list:
    return [b for b in _walk(node, "button") if b["behaviors"][0]["value"].get("op") == "open"]


def _more(node) -> list:
    return [b for b in _walk(node, "button") if b["behaviors"][0]["value"].get("op") == "more"]


def _view(*sessions, notes=()):
    return Overview(sessions=sorted(sessions, key=_sort_key), notes=list(notes),
                    ok_tools=["claude", "codex", "cursor"])


def _mixed():
    """Claude：1 个在跑 + 5 个闲着；Codex：5 个闲着；Cursor：2 个挂起。"""
    out = [_sess("claude", _sid(1), title="在跑的", status="running", pids=(9,), age=30)]
    out += [_sess("claude", _sid(10 + i), title=f"闲{i}", age=600 + i) for i in range(5)]
    out += [_sess("codex", _sid(20 + i), title=f"码{i}", cwd="/x/carher-admin", age=900 + i)
            for i in range(5)]
    out += [_sess("cursor", _sid(30 + i), title=f"光标{i}", cwd="/y/CarHer", status="stopped",
                  age=86400 + i) for i in range(2)]
    return _view(*out)


# ---------- 1. 折叠：有在跑的展开，全闲的收起 ----------


def test_each_group_is_a_collapsible_panel():
    card = _check(build_overview_card(_mixed()))
    panels = _panels(card)
    assert set(panels) == {"claude", "codex", "cursor"}
    for p in panels.values():
        assert p["header"]["title"]["tag"] == "markdown"
        assert p["header"]["icon"]["token"] == "down-small-ccm_outlined"
        assert p["header"]["icon_position"] == "right"
        assert p["header"]["icon_expanded_angle"] == -180
    # 会话按钮全在面板里，刷新按钮在面板外
    top = card["body"]["elements"]
    assert not _opens(top[0]) and all(
        not _opens(el) for el in top if el.get("tag") != "collapsible_panel")
    assert top[-1]["behaviors"][0]["value"] == {"act": "fleet", "op": "refresh"}


def test_group_with_a_running_session_is_expanded():
    panels = _panels(_check(build_overview_card(_mixed())))
    assert panels["claude"]["expanded"] is True
    assert panels["claude"]["header"]["title"]["content"] == \
        "🤖 **Claude** · carher-admin（6 个）· 🟢 1 个在跑"


def test_all_idle_group_is_collapsed_and_says_so():
    panels = _panels(_check(build_overview_card(_mixed())))
    assert panels["codex"]["expanded"] is False
    assert panels["codex"]["header"]["title"]["content"] == \
        "🤖 **Codex** · carher-admin（5 个）· 都闲着"
    assert panels["cursor"]["expanded"] is False
    assert panels["cursor"]["header"]["title"]["content"].endswith("（2 个）· 都挂起了")
    # 收起的那组，行照样在面板里（点开就能看）
    assert len(_opens(panels["codex"])) == 5


def test_mixed_idle_and_stopped_group_summary():
    view = _view(_sess("codex", _sid(1)), _sess("codex", _sid(2), status="stopped"))
    title = _panels(_check(build_overview_card(view)))["codex"]["header"]["title"]["content"]
    assert title.endswith("· 没有在跑的"), title


def test_waiting_group_is_expanded_and_keeps_its_style():
    waiting = _sess("codex", _sid(1), title="问方案", status="waiting", pending=_ask(), age=10)
    idle = _sess("codex", _sid(2), title="写文档", age=900)
    card = _check(build_overview_card(_view(waiting, idle, _sess("cursor", _sid(3)))))
    panel = _panels(card)["codex"]
    assert panel["expanded"] is True
    # 第 32 轮：读得出它在问什么（AskUserQuestion）⇒「❓ 等你回答」，不再是笼统的「🟡 在等你」
    assert "❓ 1 个等你回答" in panel["header"]["title"]["content"]
    rows = _opens(panel)
    assert rows[0]["type"] == "primary" and rows[0]["text"]["content"].startswith("❓")
    assert rows[1]["type"] == "default"
    # 「它在问你」紧跟在它那一行后面（面板里的下一个元素）
    elements = panel["elements"]
    i = elements.index(rows[0].get("_container", rows[0]))  # 11-16 行是交互容器（tests/_overview_rows.py）
    assert "它在问你" in elements[i + 1]["content"]
    assert _panels(card)["cursor"]["expanded"] is False


def test_hidden_running_session_still_opens_the_group():
    """列出来的 8 行都闲着、藏着的那几个里有在跑的（几乎不会，排序把它们放前面）——
    判断展开看的是整组，不能只看列出来的那几行。"""
    items = [_sess(sid=_sid(i), title=f"闲{i}", age=i + 1) for i in range(MAX_ROWS_PER_TOOL + 2)]
    items.append(_sess(sid=_sid(99), title="在跑", status="running", age=9999))
    view = Overview(sessions=items, ok_tools=["claude"])  # 故意不排序
    panel = _panels(_check(build_overview_card(view)))["claude"]
    assert panel["expanded"] is True


# ---------- 2. 「还有 N 个」是按钮 ----------


def _many(n=30, tool="claude"):
    return [_sess(tool, _sid(i + (0 if tool == "claude" else 1000)), title=f"会话{i}", age=60 * (i + 1))
            for i in range(n)]


def test_hidden_rows_line_is_a_button_now():
    card = _check(build_overview_card(_view(*_many(30))))
    panel = _panels(card)["claude"]
    more = _more(panel)
    assert len(more) == 1, "「还有 N 个」应该是一个能点的按钮"
    assert more[0]["behaviors"][0]["value"] == {"act": "fleet", "op": "more", "tool": "claude"}
    label = more[0]["text"]["content"]
    assert f"还有 {30 - MAX_ROWS_PER_TOOL} 个" in label and "点开" in label
    assert more[0] is panel["elements"][-1], "按钮是面板里最后一个"
    # 不再是死字
    assert not any("没列出来" in m["content"] for m in _walk(panel, "markdown"))


def test_hidden_waiting_ones_are_still_named_on_the_button():
    items = [_sess(sid=_sid(i), title=f"等{i}", status="waiting", age=i + 1) for i in range(10)]
    more = _more(_check(build_overview_card(_view(*items))))
    assert "2 个在等你" in more[0]["text"]["content"]


def test_more_button_label_fits_one_phone_line():
    from bridge.fleet.cards import OVERVIEW_ROW_CELLS, display_width, more_button_label

    mixes = [[_sess(status="idle")] * 128,
             [_sess(status="waiting")] * 120 + [_sess(status="running")] * 130,
             [_sess(status="running")] * 3]
    for hidden in mixes:
        label = more_button_label(hidden)
        assert display_width(label) <= OVERVIEW_ROW_CELLS, label
        assert label.endswith("点开看全部"), label
    assert "120 个在等你" in more_button_label(mixes[1])


def test_no_more_button_when_everything_fits():
    assert not _more(_check(build_overview_card(_mixed())))


# ---------- 3. 白名单 ----------


def test_more_op_whitelist():
    ok = resolve_fleet_click({"act": "fleet", "op": "more", "tool": "codex"}, set())
    assert ok == {"op": "more", "tool": "codex", "id": ""}
    assert fleet_value("more", "codex") == {"act": "fleet", "op": "more", "tool": "codex"}
    bad = [
        {"act": "fleet", "op": "more"},                                   # 没 tool
        {"act": "fleet", "op": "more", "tool": "vim"},                    # 不认识的工具
        {"act": "fleet", "op": "more", "tool": ["claude"]},               # 不是字符串
        {"act": "fleet", "op": "more", "tool": "claude", "id": _sid(1)},  # 不该带 id
        {"act": "fleet", "op": "more", "tool": "claude", "text": "rm"},   # 夹带
        {"act": "fleet", "op": "more", "tool": "claude", "expand": "x"},  # 多余键
        {"act": "fleet", "op": "more_all", "tool": "claude"},             # 伪造的 op
        {"act": "menu", "op": "more", "tool": "claude"},
    ]
    for value in bad:
        assert resolve_fleet_click(value, {("claude", _sid(1))}) is None, value


# ---------- 4. 展开后的那张卡 ----------


def test_expanded_card_lists_the_whole_group_and_keeps_others():
    sessions = _many(30) + _many(12, "codex")
    view = _view(*sessions)
    card = _check(build_overview_card(view, expand="claude"))
    panels = _panels(card)
    assert panels["claude"]["expanded"] is True
    assert len(_opens(panels["claude"])) == 30, "那一组要全列出来"
    assert not _more(panels["claude"])
    # 别的组照旧：还是收起、还是 8 行 +「看全部」按钮
    assert panels["codex"]["expanded"] is False
    assert len(_opens(panels["codex"])) == MAX_ROWS_PER_TOOL
    assert len(_more(panels["codex"])) == 1


def test_expand_unknown_or_empty_tool_is_the_plain_card():
    view = _view(*_many(12))
    plain = build_overview_card(view)
    assert build_overview_card(view, expand="vim") == plain
    assert build_overview_card(view, expand="cursor") == plain  # 这一组没会话


def test_expanded_card_over_budget_does_not_blow_up_and_says_how_many_are_left():
    sessions = [_sess(sid=_sid(i), title="汉" * 200, cwd=f"/x/项目{i % 7}", age=60 * (i + 1))
                for i in range(400)]
    sessions += [_sess("codex", _sid(5000 + i), title="码" * 200) for i in range(40)]
    view = _view(*sessions, notes=["读得太慢" * 2000] * 6)
    card = _check(build_overview_card(view, expand="claude"))
    panel = _panels(card)["claude"]
    shown = len(_opens(panel))
    assert shown > MAX_ROWS_PER_TOOL * 3, f"展开后只多列了这么几行：{shown}"
    tail = panel["elements"][-1]
    assert tail["tag"] == "markdown"
    assert f"还有 {400 - shown} 个这张卡放不下" in tail["content"] and "电脑上看" in tail["content"]
    assert not _more(panel), "已经是全部了，别再给一个点了还是同一张卡的按钮"
    # 刷新还在
    assert card["body"]["elements"][-1]["behaviors"][0]["value"] == {"act": "fleet", "op": "refresh"}


def test_expanded_card_redacts_titles():
    secret = "sk-ant-api03-" + "A" * 40
    sessions = [_sess(sid=_sid(i), title=f"{secret} 第{i}个", age=i + 1) for i in range(20)]
    blob = build_overview_card(_view(*sessions), redact=lambda s: s.replace(secret, "[已脱敏]"),
                               expand="claude")
    _check(blob)
    # 11-16 行是 markdown ⇒ 方括号转成了实体（&#91;），还原回来再数
    import html as _html

    assert "sk-ant" not in blob and _html.unescape(blob).count("[已脱敏]") >= 20


def test_expanded_card_duplicate_titles_get_short_ids():
    sessions = [_sess(sid=f"{i:04x}aaaa-0000", title="同一个名字", age=i + 1) for i in range(12)]
    card = _check(build_overview_card(_view(*sessions), expand="claude"))
    rows = [b["text"]["content"] for b in _opens(card)]
    assert len(rows) == 12 and len(set(rows)) == 12, rows


# ---------- 5. 点一下：原地换卡 ----------


@pytest.fixture
def here_dir():
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".r28-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _ManyClaude:
    tool = "claude"

    def __init__(self, sessions):
        self.sessions = list(sessions)
        self.last_error = ""
        self.list_calls = 0

    def list(self, limit=20):
        self.list_calls += 1
        return list(self.sessions)

    def running(self):
        return []

    def find(self, sid):
        return next((s for s in self.sessions if s.id == sid), None)

    def read(self, sid, n=4):
        return []


def _click(b, value):
    from tests.test_relay_integration import CHAT, OWNER

    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=CHAT))
    return b._on_fleet_click(event, OWNER, value)


def test_click_more_swaps_the_card_in_place(here_dir):
    from tests.test_relay_integration import _bridge

    adapter = _ManyClaude(_many(30))
    b = _bridge(here_dir, adapter)
    b.fleet.overview()  # 先有一次扫描（主人先点过「会话」）
    calls = adapter.list_calls
    resp = _click(b, {"act": "fleet", "op": "more", "tool": "claude"})
    assert adapter.list_calls == calls, "回调里不许现扫（3 秒内必须返回）"
    assert resp.card is not None, "没有原地换卡"
    assert resp.card.type == "raw"
    data = resp.card.data
    blob = json.dumps(data, ensure_ascii=False)
    _check(blob)
    panel = _panels(data)["claude"]
    assert panel["expanded"] is True
    assert len(_opens(panel)) == 30
    assert not b.replier.cards and not b.replier.texts, "原地换卡就别再新发一条"


def test_click_more_without_a_previous_scan_falls_back_with_a_toast(here_dir):
    from tests.test_relay_integration import _bridge

    b = _bridge(here_dir, _ManyClaude(_many(30)))
    queued = []
    b._loop = object()  # 有事件循环 ⇒ 走「丢后台」那条
    import asyncio

    def fake_submit(coro, loop):
        queued.append(coro.cr_code.co_name)
        coro.close()
        return types.SimpleNamespace(add_done_callback=lambda f: None)

    orig = asyncio.run_coroutine_threadsafe
    asyncio.run_coroutine_threadsafe = fake_submit
    try:
        resp = _click(b, {"act": "fleet", "op": "more", "tool": "claude"})
    finally:
        asyncio.run_coroutine_threadsafe = orig
    assert resp.card is None
    assert "重新看一眼" in resp.toast.content
    assert queued == ["_fleet_overview"]


def test_forged_more_click_is_rejected(here_dir):
    from tests.test_relay_integration import _bridge

    b = _bridge(here_dir, _ManyClaude(_many(30)))
    b.fleet.overview()
    for value in ({"act": "fleet", "op": "more", "tool": "vim"},
                  {"act": "fleet", "op": "more", "tool": "claude", "id": _sid(1)},
                  {"act": "fleet", "op": "more", "tool": "claude", "text": "x"}):
        resp = _click(b, value)
        assert resp.card is None, value
        assert resp.toast.type == "error", value


def test_stranger_cannot_expand(here_dir):
    from tests.test_relay_integration import CHAT, _bridge

    b = _bridge(here_dir, _ManyClaude(_many(30)))
    b.fleet.overview()
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=CHAT))
    resp = b._on_fleet_click(event, "ou_stranger", {"act": "fleet", "op": "more", "tool": "claude"})
    assert resp.card is None and resp.toast.type == "error"
