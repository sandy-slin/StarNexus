"""第 36 轮「➕ 新会话」界面 + 接线（工程师 C）。

全部假的：`newsession.start` / `available_tools` 用 monkeypatch 的桩，假建群接口、假 replier、tmp_path。
**不起真 CLI、不开窗口、不发飞书。**

盯的几件事：卡片形状（单行输入框不在 form 里、下拉无默认值）、回调路由、桩成功 / 失败、
非主人点击、超长输入、按钮值里没有路径和原话、目录越界拦住、连按两下回车只开一个。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import newsession  # noqa: E402
from bridge.fleet.actions import newsession_value, resolve_newsession_click  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    NEWSESSION_INPUT,
    build_newsession_card,
    build_overview_card,
    newsession_open_window,
)
from bridge.fleet.chatbind import ChatBinds  # noqa: E402
from bridge.fleet.consent import RelayConsent  # noqa: E402
from bridge.fleet.control import FleetControl  # noqa: E402
from bridge.fleet.model import Session  # noqa: E402
from bridge.fleet.scan import Fleet, Overview  # noqa: E402
from bridge.policy import Policy  # noqa: E402

from tests.test_chatbind import FakeClaude, FakeGroups, FakeReplier  # noqa: E402

OWNER = "ou_owner"
DM_CHAT = "oc_dm"
DM = f"dm:{OWNER}"
NEW_SID = "66666666-6666-4666-8666-666666666666"
SECRET_PROMPT = "帮我看看 token=sk-ant-abcdefghijklmnopqrstuvwxyz0123456789 为什么不行"


def _walk(node, tag=None):
    if isinstance(node, dict):
        if tag is None or node.get("tag") == tag:
            yield node
        for v in node.values():
            yield from _walk(v, tag)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, tag)


def _values(card):
    """卡上所有回调值（按钮 / 下拉 / 输入框）。"""
    for node in _walk(card):
        for b in node.get("behaviors", []) or []:
            if b.get("type") == "callback":
                yield b["value"]
        for opt in node.get("options", []) or []:
            yield {"option": opt.get("value")}


# ---------- 桥 + 桩 ----------


def _policy(tmp_path, mode="readonly"):
    return Policy(mode=mode, workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                  model="claude-sonnet-5-5", turn_timeout_sec=60, ceiling_mode="trusted")


def _bridge(tmp_path, adapter=None, groups=None):
    from bridge.app import Bridge
    from bridge.approval import ApprovalGate
    from bridge.review import Reviewer

    adapter = adapter or FakeClaude()
    b = Bridge.__new__(Bridge)
    pol = _policy(tmp_path)
    b.settings = types.SimpleNamespace(owner_open_ids=frozenset({OWNER}), policy=pol, custom_rules=(),
                                       config_path=str(tmp_path / "config.toml"))
    b.replier = FakeReplier()
    b._chat_ids = {DM: DM_CHAT}
    b._paused, b._awaiting, b._cancels, b._pending_consent = {}, {}, {}, {}
    b._loop = None
    b.reviewer = Reviewer()
    b.gate = ApprovalGate(str(tmp_path / "approvals"))
    b.fleet = FleetControl(Fleet({adapter.tool: adapter}, timeout=1.0))
    b._fleet_inflight = {}
    b.relay_consent = RelayConsent(str(tmp_path / "relay_consent.json"))
    b.chat_binds = ChatBinds(str(tmp_path / "chat_binds.json"))
    b.group_client = groups or FakeGroups()
    b.workspaces = types.SimpleNamespace(get=lambda scope: str(proj_a(tmp_path)))
    return b


def proj_a(tmp_path):
    p = tmp_path / "proj-a"
    p.mkdir(exist_ok=True)
    (p / ".git").mkdir(exist_ok=True)
    return p


def proj_b(tmp_path):
    p = tmp_path / "proj-b"
    p.mkdir(exist_ok=True)
    (p / ".git").mkdir(exist_ok=True)
    return p


class Stub:
    """`newsession.start` 的桩：记下请求，按设定返回成功 / 失败。"""

    def __init__(self, ok=True, note="已在电脑上新开了一个会话。", sid=NEW_SID):
        self.ok, self.note, self.sid = ok, note, sid
        self.calls: list = []

    def __call__(self, req, *, policy, jobs, **kw):
        self.calls.append((req, kw))
        if not self.ok:
            return newsession.NewSessionResult(False, req.tool, "", self.note)
        return newsession.NewSessionResult(True, req.tool, self.sid, self.note)


@pytest.fixture
def stub(monkeypatch):
    s = Stub()
    monkeypatch.setattr(newsession, "start", s)
    monkeypatch.setattr(newsession, "available_tools", lambda which=None: ["claude", "codex", "cursor"])
    return s


def _event(value, option=None, input_value=None, chat_id=DM_CHAT, clicker=OWNER):
    action = types.SimpleNamespace(value=value, form_value=None, input_value=input_value, option=option)
    return types.SimpleNamespace(operator=types.SimpleNamespace(open_id=clicker),
                                 context=types.SimpleNamespace(open_chat_id=chat_id), action=action)


def _click(b, value, **kw):
    """走 `on_card_action` 的总入口（路由也一起测）。返回 {"toast": {...}, "card": dict}。"""
    resp = b.on_card_action(types.SimpleNamespace(event=_event(value, **kw)))
    out = {}
    if getattr(resp, "toast", None) is not None:
        out["toast"] = {"content": resp.toast.content, "type": resp.toast.type}
    if getattr(resp, "card", None) is not None:
        out["card"] = resp.card.data
    return out


def _nonce(card):
    for v in _values(card):
        if v.get("act") == "nsess" and v.get("n"):
            return v["n"]
    raise AssertionError("卡上没有序号")


def _drain(b, value, **kw):
    """回车那一下会把后台协程交给 `_newsession_later`；测试里直接同步跑掉它。"""
    ran = []

    def later(chat_id, coro):
        ran.append(coro)

    b._newsession_later = later
    resp = _click(b, value, **kw)
    for coro in ran:
        asyncio.run(coro)
    return resp, len(ran)


# ---------- 卡片形状 ----------


def test_card_shape_single_line_input_outside_form_with_callback():
    card = build_newsession_card("ab12", ["claude", "codex"], "claude",
                                 [("~/codes/a", True), ("~/codes/b", False)], 0)
    assert card["schema"] == "2.0"
    assert not list(_walk(card, "form")), "输入框放进 form 里回车就不发了"
    boxes = list(_walk(card, "input"))
    assert len(boxes) == 1
    box = boxes[0]
    assert box["name"] == NEWSESSION_INPUT
    assert box.get("input_type", "text") == "text", "多行框的回车只插空格"
    assert box["behaviors"][0]["value"] == {"act": "nsess", "op": "send", "n": "ab12"}
    assert 0 < box["max_length"] <= 1000
    assert "default_value" not in box


def _selects(card):
    sels = list(_walk(card, "select_static"))
    by_op = {sel["behaviors"][0]["value"]["op"]: sel for sel in sels}
    return sels, by_op


def test_card_is_one_row_of_two_dropdowns_plus_input_nothing_else():
    """11-16 简化：一行【工具 ▾】【项目 ▾】+ 输入框 + 一行小字。没有工具按钮、没有开窗口开关、没有「算了」。"""
    card = build_newsession_card("ab12", ["claude", "codex", "cursor"], "codex",
                                 [("~/codes/a", True), ("~/codes/b", False)], 1)
    elems = card["body"]["elements"]
    assert elems[0]["tag"] == "column_set" and len(elems[0]["columns"]) == 2, "两个下拉并排在第一行"
    assert [e["tag"] for e in elems] == ["column_set", "input", "markdown"]
    assert not list(_walk(card, "button")), "卡上一个按钮都不要（工具按钮 / 开关 / 算了都拿掉了）"
    vals = list(_values(card))
    assert not [v for v in vals if v.get("op") in ("win", "tool", "no")], "没有 win 按钮、没有老的工具按钮"
    assert "打开窗口：" not in json.dumps(card, ensure_ascii=False)
    (box,) = list(_walk(card, "input"))
    assert box["placeholder"]["content"] == "第一句话，回车就开"


def test_both_dropdowns_show_current_default_via_initial_option():
    # 不设 initial_option ⇒ 手机上只显示 placeholder，看不出默认是谁。值填选项的 value（客户端按 value 匹配）
    card = build_newsession_card("ab12", ["claude", "codex"], "codex", [("~/codes/a", True), ("~/codes/b", False)], 1)
    sels, by_op = _selects(card)
    assert set(by_op) == {"tsel", "proj"} and len(sels) == 2
    assert by_op["tsel"]["initial_option"] == "codex"
    assert by_op["proj"]["initial_option"] == "p1"
    for sel in sels:
        assert sel["initial_option"] in [o["value"] for o in sel["options"]]
        assert sel["behaviors"][0]["value"] == {"act": "nsess", "op": sel["behaviors"][0]["value"]["op"], "n": "ab12"}


def test_project_dropdown_carries_only_indexes():
    card = build_newsession_card("ab12", ["claude"], "claude", [("~/codes/a", True), ("~/codes/b", False)], 0)
    _, by_op = _selects(card)
    assert [o["value"] for o in by_op["proj"]["options"]] == ["p0", "p1"]
    assert by_op["proj"]["initial_option"] == "p0"


def test_tool_dropdown_skips_uninstalled():
    card = build_newsession_card("ab12", ["claude", "cursor"], "cursor", [("~/a", True)], 0)
    _, by_op = _selects(card)
    assert [o["value"] for o in by_op["tsel"]["options"]] == ["claude", "cursor"], "没装的 Codex 不显示"
    assert by_op["tsel"]["initial_option"] == "cursor"


def test_hint_is_one_line_and_cursor_says_background():
    for tool in ("claude", "codex", "cursor"):
        card = build_newsession_card("ab12", ["claude", "codex", "cursor"], tool, [("~/a", True)], 0)
        md = [e for e in card["body"]["elements"] if e["tag"] == "markdown"]
        assert len(md) == 1 and "\n" not in md[0]["content"] and md[0].get("text_size") == "notation"
        if tool == "cursor":
            assert "Cursor 在后台跑" in md[0]["content"] and "窗口" not in md[0]["content"]
        else:
            assert "打开一个窗口" in md[0]["content"]
    assert "只读" in json.dumps(build_newsession_card("ab12", ["codex"], "codex", [("~/a", True)], 0),
                                ensure_ascii=False), "Codex 从手机只读这句还在"


def test_open_window_rule_claude_codex_yes_cursor_no():
    assert newsession_open_window("claude") is True
    assert newsession_open_window("codex") is True
    assert newsession_open_window("cursor") is False


def test_no_tools_installed_card_has_no_input():
    card = build_newsession_card("ab12", [], "", [], None)
    assert not list(_walk(card, "input")) and "没找到" in json.dumps(card, ensure_ascii=False)


def test_card_stays_small_with_twenty_long_projects():
    from bridge.fleet.cards import MAX_CARD_BYTES

    projects = [("~/" + "很长的项目名" * 20 + str(i), i == 0) for i in range(40)]
    card = build_newsession_card("ab12", ["claude", "codex", "cursor"], "claude", projects, 0)
    assert len(json.dumps(card, ensure_ascii=False).encode()) < MAX_CARD_BYTES
    _, by_op = _selects(card)
    assert len(by_op["proj"]["options"]) == 20


def test_overview_has_only_one_new_session_entry_on_top(monkeypatch):
    # 11-16 主人改主意：「新会话放在每个分类会话的上方」⇒ 顶上那个通用入口拿掉，每个面板一个（工具已定）
    from bridge.fleet import cards as _cards

    monkeypatch.setattr(_cards, "_startable_tools", lambda: ["claude", "codex", "cursor"])
    sess = [Session(tool=t, surface="cli", id=f"s{i}", title="修登录", cwd="/x", status="idle",
                    owner="none", updated_at=time.time() - 60) for i, t in enumerate(("claude", "codex", "cursor"))]
    card = json.loads(build_overview_card(Overview(sessions=sess)))
    ns = [v for v in _values(card) if v.get("act") == "nsess"]
    assert ns == [{"act": "nsess", "op": "begin", "t": t} for t in ("claude", "codex", "cursor")], \
        "每个面板一个「➕ 新开一个 X」，顶上不再有通用的"
    assert {"act": "nsess", "op": "begin"} not in ns
    assert all(resolve_newsession_click(v) is not None for v in ns)


# ---------- 值的白名单 ----------


@pytest.mark.parametrize("bad", [
    {"act": "nsess", "op": "begin", "n": "ab12"},           # begin 不带序号
    {"act": "nsess", "op": "send"},                          # send 必须带序号
    {"act": "nsess", "op": "send", "n": "ZZ"},               # 序号不是小写十六进制
    {"act": "nsess", "op": "send", "n": "ab12", "p": "/etc"},  # 多一个键
    {"act": "nsess", "op": "tool", "n": "ab12"},             # 换工具没说换成哪个
    {"act": "nsess", "op": "tool", "n": "ab12", "t": "bash"},  # 不认识的工具
    {"act": "nsess", "op": "proj", "n": "ab12", "t": "claude"},  # t 只给 begin/tool
    {"act": "nsess", "op": "rm", "n": "ab12"},
    "nsess",
])
def test_resolver_rejects_anything_off_shape(bad):
    assert resolve_newsession_click(bad) is None


def _start_flow(b, tmp_path, tool=""):
    proj_a(tmp_path)
    proj_b(tmp_path)
    r = _click(b, newsession_value("begin", tool=tool))
    assert "card" in r, r
    return r["card"], _nonce(r["card"])


def test_no_value_on_any_card_carries_a_path_or_the_prompt(tmp_path, stub):
    b = _bridge(tmp_path)
    card, n = _start_flow(b, tmp_path)
    cards = [card]
    cards.append(_click(b, newsession_value("proj", n), option="p1")["card"])
    cards.append(_click(b, newsession_value("tsel", n), option="codex")["card"])
    resp, _ = _drain(b, newsession_value("send", n), input_value=SECRET_PROMPT)
    cards.append(resp["card"])
    for c in cards:
        for v in _values(c):
            raw = json.dumps(v, ensure_ascii=False)
            assert str(tmp_path) not in raw and "/" not in raw.replace("\\/", ""), raw
            assert SECRET_PROMPT not in raw


# ---------- 回调路由 + 接线 ----------


def test_begin_preselects_first_tool_and_current_project(tmp_path, stub):
    b = _bridge(tmp_path)
    card, n = _start_flow(b, tmp_path)
    draft = b._newsession_drafts[DM]
    assert draft["tool"] == "claude" and "win" not in draft, "开窗口不再存草稿：按工具定"
    assert draft["projects"][0][0] == os.path.realpath(proj_a(tmp_path)), "现在这个聊天的项目排第一"
    assert draft["proj"] == 0
    assert stub.calls == [], "发卡不起任何进程"


def test_group_entry_preselects_its_tool(tmp_path, stub):
    b = _bridge(tmp_path)
    _start_flow(b, tmp_path, tool="codex")
    assert b._newsession_drafts[DM]["tool"] == "codex"


def test_tool_and_project_dropdowns_update_draft_in_place(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    r = _click(b, newsession_value("tsel", n), option="codex")
    assert "card" in r and b._newsession_drafts[DM]["tool"] == "codex"
    assert _selects(r["card"])[1]["tsel"]["initial_option"] == "codex"
    r = _click(b, newsession_value("proj", n), option="p1")
    assert "card" in r and b._newsession_drafts[DM]["proj"] == 1
    assert _selects(r["card"])[1]["proj"]["initial_option"] == "p1"
    # 老卡上的工具按钮（value.t）照样认
    r = _click(b, newsession_value("tool", n, "claude"))
    assert "card" in r and b._newsession_drafts[DM]["tool"] == "claude"


def test_tool_dropdown_rejects_uninstalled_or_forged_option(tmp_path, stub, monkeypatch):
    b = _bridge(tmp_path)
    monkeypatch.setattr(newsession, "available_tools", lambda which=None: ["claude", "codex"])
    _, n = _start_flow(b, tmp_path)
    for opt in ("cursor", "bash", "/etc", None, 3):
        r = _click(b, newsession_value("tsel", n), option=opt)
        assert r["toast"]["type"] == "error" and "card" not in r
    assert b._newsession_drafts[DM]["tool"] == "claude"


def test_old_card_window_toggle_changes_nothing(tmp_path, stub):
    """之前发出去的老卡上还有开关：点了不改草稿，回车照样按工具定开不开。"""
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    before = dict(b._newsession_drafts[DM])
    r = _click(b, newsession_value("win", n))
    assert "card" in r and not [v for v in _values(r["card"]) if v.get("op") == "win"]
    assert b._newsession_drafts[DM] == before


def test_bad_project_index_is_refused(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    for opt in ("p99", "/etc", "p-1", None, 3):
        r = _click(b, newsession_value("proj", n), option=opt)
        assert r["toast"]["type"] == "error" and "card" not in r
    assert b._newsession_drafts[DM]["proj"] == 0


def test_send_success_starts_session_builds_group_and_posts_first_reply(tmp_path, stub):
    adapter = FakeClaude()
    b = _bridge(tmp_path, adapter)
    _, n = _start_flow(b, tmp_path)
    _click(b, newsession_value("proj", n), option="p1")
    # 新会话起来后总览能认出它（refresh → find）
    adapter.sessions.append(Session(tool="claude", surface="cli", id=NEW_SID, title=SECRET_PROMPT,
                                    cwd=str(proj_b(tmp_path)), status="running", owner="bridge",
                                    updated_at=time.time()))
    resp, ran = _drain(b, newsession_value("send", n), input_value=SECRET_PROMPT)
    assert ran == 1 and "正在" in json.dumps(resp["card"], ensure_ascii=False)
    assert not list(_walk(resp["card"], "input")), "开了之后的卡上不能再有输入框（再回车就是又开一个）"
    (req, kw), = stub.calls
    assert req.tool == "claude" and req.prompt == SECRET_PROMPT and req.open_window is True, "Claude 一律开窗口"
    assert req.cwd == os.path.realpath(proj_b(tmp_path)) and req.mode == "readonly"
    assert callable(kw["on_done"]) and callable(kw["approval_dir"])
    bound = b.chat_binds.of_session("claude", NEW_SID)
    assert bound is not None, "复用「接着聊」那条路建了群"
    assert b._relay_granted(bound.scope, "claude", NEW_SID), "点这一下就是确认，群里第一句不再问"
    assert any("现在就能说" in t for t in b.replier.to(bound.chat_id))
    # 第一轮回完（B 在工作线程里调 on_done）⇒ 回复贴进那个群
    ft = newsession.FirstTurn(tool="claude", session_id=NEW_SID, ok=True, text="颜色改好了")
    asyncio.run(b._newsession_first_turn(ft))
    assert any("颜色改好了" in t for t in b.replier.to(bound.chat_id))
    assert DM not in b._newsession_drafts


def test_first_reply_before_group_is_ready_is_held_then_posted(tmp_path, stub):
    b = _bridge(tmp_path)
    b._newsession_firsts = {("codex", NEW_SID): {"dm": DM_CHAT, "ready": False, "ft": None}}
    ft = newsession.FirstTurn(tool="codex", session_id=NEW_SID, ok=True, text="先到的回复")
    asyncio.run(b._newsession_first_turn(ft))
    assert not any("先到的回复" in t for _, t in b.replier.texts)
    b._newsession_firsts[("codex", NEW_SID)]["ready"] = True
    asyncio.run(b._newsession_first_turn(b._newsession_firsts[("codex", NEW_SID)]["ft"]))
    assert any("先到的回复" in t for t in b.replier.to(DM_CHAT))


def test_send_failure_says_one_plain_sentence_and_offers_retry(tmp_path, stub):
    stub.ok, stub.note = False, "Codex 一启动就退出了：没登录"
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path, tool="codex")
    _drain(b, newsession_value("send", n), input_value="看看")
    texts = [t for _, t in b.replier.texts]
    cards = json.dumps([c for _, c in b.replier.cards], ensure_ascii=False)
    assert "没登录" in cards or any("没登录" in t for t in texts)
    assert "再开一次" in cards
    assert b.chat_binds.of_session("codex", "") is None and not b.group_client.created


def test_non_owner_click_does_nothing(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    before = dict(b._newsession_drafts[DM])
    for value, opt in ((newsession_value("begin"), None), (newsession_value("send", n), None),
                       (newsession_value("tsel", n), "cursor"), (newsession_value("proj", n), "p1")):
        r = _click(b, value, clicker="ou_stranger", input_value="删库", option=opt)
        assert r["toast"]["type"] == "error" and "card" not in r
    assert stub.calls == [] and b._newsession_drafts[DM] == before, "非主人点下拉不改草稿"


def test_overlong_or_empty_input_is_bounced_not_truncated(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    r, ran = _drain(b, newsession_value("send", n), input_value="字" * 1001)
    assert ran == 0 and "太长" in json.dumps(r["card"], ensure_ascii=False)
    r, ran = _drain(b, newsession_value("send", n), input_value="   ")
    assert ran == 0 and "空" in json.dumps(r["card"], ensure_ascii=False)
    assert stub.calls == [] and DM in b._newsession_drafts, "草稿还在，改短了直接再回车"


def test_project_that_left_the_whitelist_is_refused_at_send(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    b._newsession_drafts[DM]["projects"][0] = (str(outside), "outside", True)
    r, ran = _drain(b, newsession_value("send", n), input_value="看看")
    assert ran == 0 and stub.calls == [] and "范围" in json.dumps(r["card"], ensure_ascii=False)


def test_symlink_out_of_workspace_is_refused(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-out2")
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside)
    b._newsession_drafts[DM]["projects"][0] = (str(link), "link", True)
    _, ran = _drain(b, newsession_value("send", n), input_value="看看")
    assert ran == 0 and stub.calls == []


def test_double_enter_only_starts_once(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    _drain(b, newsession_value("send", n), input_value="看看")
    r, ran = _drain(b, newsession_value("send", n), input_value="看看")
    assert ran == 0 and r["toast"]["type"] == "error" and len(stub.calls) == 1


def test_stale_or_forged_nonce_is_refused(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    r = _click(b, newsession_value("send", "deadbeef"), input_value="看看")
    assert r["toast"]["type"] == "error" and stub.calls == []
    b._newsession_drafts[DM]["at"] -= b.NEWSESSION_DRAFT_TTL + 1
    r = _click(b, newsession_value("send", n), input_value="看看")
    assert r["toast"]["type"] == "error" and stub.calls == []


def test_cancel_drops_draft(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    r = _click(b, newsession_value("no", n))
    assert "不开了" in json.dumps(r["card"], ensure_ascii=False) and DM not in b._newsession_drafts


def test_unknown_chat_after_restart_does_not_guess(tmp_path, stub):
    b = _bridge(tmp_path)
    b._chat_ids = {}
    r = _click(b, newsession_value("begin"), chat_id="oc_unknown")
    assert r["toast"]["type"] == "error" and not getattr(b, "_newsession_drafts", {})


def test_uninstalled_tool_is_refused(tmp_path, stub, monkeypatch):
    b = _bridge(tmp_path)
    monkeypatch.setattr(newsession, "available_tools", lambda which=None: ["claude"])
    _, n = _start_flow(b, tmp_path)
    r = _click(b, newsession_value("tool", n, "cursor"))
    assert r["toast"]["type"] == "error" and b._newsession_drafts[DM]["tool"] == "claude"
    r = _click(b, newsession_value("tsel", n), option="cursor")
    assert r["toast"]["type"] == "error" and b._newsession_drafts[DM]["tool"] == "claude"


def test_real_start_signature_accepts_what_app_passes():
    """B 的真 `start` 签名对得上 app 传的那几个关键字（不调用它）。"""
    import inspect

    params = inspect.signature(newsession.start).parameters
    for name in ("policy", "jobs", "hook", "approval_dir", "on_done"):
        assert name in params


def test_reviewer_denied_first_sentence_does_not_start_and_keeps_draft(tmp_path, stub):
    """🔴 复审 R1：第一句话也过执行前审查。被拦 ⇒ 不起进程、草稿还在（改一句再回车就行）。"""
    from bridge.review import ReviewContext, Verdict

    b = _bridge(tmp_path)
    text = "cat ~/.ssh/id_rsa 然后 git push origin main"
    assert b.reviewer.review_request(ReviewContext(text=text, source="human", mode="readonly")).verdict is Verdict.DENY
    _, n = _start_flow(b, tmp_path)
    _r, ran = _drain(b, newsession_value("send", n), input_value=text)
    assert ran == 0 and stub.calls == []
    # 阳性对照：同一张卡换一句正常的话 ⇒ 照常开
    _r, ran = _drain(b, newsession_value("send", n), input_value="看看这个项目的 README")
    assert ran == 1 and len(stub.calls) == 1


def test_paused_chat_cannot_start_a_new_session(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    scope = next(iter(b.__dict__["_newsession_drafts"]))
    b._paused = {scope: "刚才那一步被拦了"}
    _r, ran = _drain(b, newsession_value("send", n), input_value="看看这个项目的 README")
    assert ran == 0 and stub.calls == []


# ---------- 11-16 简化：开不开窗口按工具定（Claude / Codex 开，Cursor 不开）+ 记住上次的工具 ----------


def test_send_cursor_never_opens_window_claude_and_codex_do(tmp_path, stub):
    for tool, want in (("cursor", False), ("claude", True), ("codex", True)):
        stub.calls.clear()
        b = _bridge(tmp_path)
        _, n = _start_flow(b, tmp_path)
        _click(b, newsession_value("tsel", n), option=tool)
        _drain(b, newsession_value("send", n), input_value="看看")
        (req, _kw), = stub.calls
        assert req.tool == tool and req.open_window is want, tool


def test_old_card_toggle_cannot_open_window_for_cursor(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    _click(b, newsession_value("tsel", n), option="cursor")
    b._newsession_drafts[DM]["win"] = True   # 老草稿 / 被改坏了也不认
    _click(b, newsession_value("win", n))
    _drain(b, newsession_value("send", n), input_value="看看")
    (req, _kw), = stub.calls
    assert req.tool == "cursor" and req.open_window is False


def test_next_begin_remembers_last_used_tool_per_scope(tmp_path, stub):
    b = _bridge(tmp_path)
    _, n = _start_flow(b, tmp_path)
    assert b._newsession_drafts[DM]["tool"] == "claude", "没用过 ⇒ 第一家"
    _click(b, newsession_value("tsel", n), option="codex")
    # 只换了没回车 ⇒ 不算「用过」
    card, n = _start_flow(b, tmp_path)
    assert b._newsession_drafts[DM]["tool"] == "claude"
    _click(b, newsession_value("tsel", n), option="codex")
    _drain(b, newsession_value("send", n), input_value="看看")
    card, n = _start_flow(b, tmp_path)
    assert b._newsession_drafts[DM]["tool"] == "codex", "上次用的是 Codex"
    assert _selects(card)[1]["tsel"]["initial_option"] == "codex"
    # 别的聊天不受影响
    assert b._newsession_last_tool == {DM: "codex"}


def test_remembered_tool_that_got_uninstalled_falls_back_to_first(tmp_path, stub, monkeypatch):
    b = _bridge(tmp_path)
    b._newsession_last_tool = {DM: "cursor"}
    monkeypatch.setattr(newsession, "available_tools", lambda which=None: ["claude", "codex"])
    _start_flow(b, tmp_path)
    assert b._newsession_drafts[DM]["tool"] == "claude"


def test_default_project_is_first_session_in_that_tool_panel(tmp_path, monkeypatch):
    """11-16 主人「建立新会话的时候，默认走第一个会话的目录」：面板第一行那个会话在 proj-b ⇒ 默认 proj-b，
    哪怕这个聊天现在的项目是 proj-a（阳性对照：没有会话时还是 proj-a）。"""
    monkeypatch.setattr(newsession, "available_tools", lambda: ["claude", "codex"])
    b = _bridge(tmp_path)
    pa, pb = proj_a(tmp_path), proj_b(tmp_path)
    def default_path():
        _start_flow(b, tmp_path, tool="claude")
        d = b._newsession_drafts[next(iter(b._newsession_drafts))]
        return os.path.basename(d["projects"][d["proj"]][0])

    assert default_path() == "proj-a"             # 阳性对照
    now = time.time()
    b.fleet.last = Overview(sessions=[
        Session(tool="codex", surface="cli", id="c1", cwd=str(pa), status="idle", updated_at=now),
        Session(tool="claude", surface="cli", id="a1", cwd=str(pb), status="idle", updated_at=now - 10),
        Session(tool="claude", surface="cli", id="a2", cwd=str(pa), status="idle", updated_at=now - 99),
    ], at=now)
    assert default_path() == "proj-b"
