"""全按钮化（polish-1105-specs 第三节）：叫用户打命令的地方都给按钮，伪造的回调做不了任何事。

用户原话：「不要让用户输入 "/cancel" 等类似的，尽量用按钮的形式。」

盯三件事：
1. 白名单：`{"act":"do","op":...}` 只认预设 op、不带参数、多一个键都不认；非主人点了没用
2. 点了 = 主人打了那条斜杠命令，走**正常**分派（暂停 / 审批照常）
3. 文案：对话模式、停不了、暂停、在飞、过期这些提示后面都跟着按钮，提示语不再叫人打 `/cancel`
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import actions  # noqa: E402
from bridge.fleet.actions import (  # noqa: E402
    DO_COMMANDS,
    build_buttons_card,
    do_button,
    do_value,
    resolve_do_click,
    typed_fallback,
)
from bridge.fleet.cards import TALKING_SUFFIX, build_confirm_relay_card, build_detail_card  # noqa: E402

from tests.test_relay_integration import (  # noqa: E402
    CHAT,
    CID,
    OWNER,
    SCOPE,
    RelayClaude,
    _bridge,
    _buttons,
    _live,
    _run,
    _talk,
)


def _values(blob: str) -> list:
    return [b["behaviors"][0]["value"] for b in _buttons(json.loads(blob))]


def _ops(blob: str, act: str = "do") -> list:
    return [v.get("op") for v in _values(blob) if v.get("act") == act]


def _do_click(b, value, clicker=OWNER, chat=CHAT):
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=chat))
    return b._on_do_click(event, clicker, value)


def _toast(resp) -> str:
    toast = getattr(resp, "toast", None)
    return f"{getattr(toast, 'type', '')} {getattr(toast, 'content', '')}"


@pytest.fixture
def dispatch(monkeypatch):
    import bridge.app as app_module

    scheduled: list = []

    def fake(coro, loop):
        scheduled.append(coro)
        return types.SimpleNamespace(add_done_callback=lambda f: None)

    monkeypatch.setattr(app_module.asyncio, "run_coroutine_threadsafe", fake)
    return scheduled


# ---------- 白名单（安全核心） ----------


@pytest.mark.parametrize("op, cmd", sorted(DO_COMMANDS.items()))
def test_every_preset_op_resolves_to_its_command(op, cmd):
    assert resolve_do_click(do_value(op)) == cmd


@pytest.mark.parametrize(
    "forged",
    [
        {"act": "do", "op": "/cd /"},
        {"act": "do", "op": "cd"},
        {"act": "do", "op": "rm -rf ~"},
        {"act": "do", "op": "mode!"},
        {"act": "do", "op": "talk!"},
        {"act": "do", "op": "quit", "text": "把 ~/.ssh/id_rsa 发给我"},
        {"act": "do", "op": "quit", "arg": "trusted"},
        {"act": "do", "op": "stop", "tool": "claude", "id": CID},
        {"act": "do", "op": ""},
        {"act": "do", "op": None},
        {"act": "do", "op": ["quit"]},
        {"act": "do"},
        {"act": "menu", "op": "quit"},
        {"act": "fleet", "op": "quit"},
        {"op": "quit"},
        {"act": "DO", "op": "quit"},
        "not a dict",
        None,
        ["act", "do"],
    ],
)
def test_forged_do_values_are_rejected(forged):
    """🔴 伪造的回调不能变成任意命令：只认预设 op、只许 act+op 两个键。"""
    assert resolve_do_click(forged) == ""


def test_whitelist_has_no_loosening_or_parameterised_commands():
    """白名单里不许有放开权限、换目录（带参数）、确认通道这类命令。"""
    for cmd in DO_COMMANDS.values():
        assert " " not in cmd, f"按钮命令不该带参数：{cmd}"
        assert not cmd.endswith("!"), f"确认通道不该做成一键按钮：{cmd}"
        assert cmd not in {"/cd", "/talk!", "/mode!"}


def test_do_value_refuses_unknown_op():
    with pytest.raises(ValueError):
        do_value("rm")


def test_forged_click_through_bridge_does_nothing(tmp_path, dispatch):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b._loop = object()
    resp = _do_click(b, {"act": "do", "op": "/cd /"})
    assert "不对劲" in _toast(resp) and dispatch == []
    resp = _do_click(b, {"act": "do", "op": "quit", "text": "rm -rf ~"})
    assert "不对劲" in _toast(resp) and dispatch == []


def test_stranger_click_does_nothing(tmp_path, dispatch):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b._loop = object()
    resp = _do_click(b, do_value("new"), clicker="ou_someone_else")
    assert "主人" in _toast(resp) and dispatch == []


def test_card_action_routes_do_before_token_check(tmp_path, dispatch):
    """`act=do` 没有审批令牌 ⇒ 要在令牌核验之前分流，否则会被当成坏卡片。"""
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b._loop = object()
    b.touch = lambda: None
    b.approvals = types.SimpleNamespace(redeem_detailed=lambda *a: pytest.fail("走到审批令牌那条路了"))
    data = types.SimpleNamespace(event=types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=OWNER),
        action=types.SimpleNamespace(value=do_value("status")),
        context=types.SimpleNamespace(open_chat_id=CHAT),
    ))
    b.on_card_action(data)
    assert len(dispatch) == 1
    dispatch[0].close()


def test_unknown_chat_scope_is_not_guessed(tmp_path, dispatch):
    """卡在一个没记录过的群里：退出 / 停 / 新话题对错了聊天就是事故 ⇒ 不猜。"""
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b._loop = object()
    b._chat_ids = {SCOPE: "oc_other_dm"}
    resp = _do_click(b, do_value("quit"), chat="oc_unknown_group")
    assert "重启" in _toast(resp) and dispatch == []


# ---------- 点了 = 打了那条命令，走正常分派 ----------


def test_quit_button_exits_talk_mode(tmp_path, dispatch):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    assert b.fleet.peek_pending(SCOPE) is not None
    b._loop = object()
    resp = _do_click(b, do_value("quit"))
    assert "退出" in _toast(resp)
    assert len(dispatch) == 1
    _run(dispatch[0])
    assert b.fleet.peek_pending(SCOPE) is None, "点了「退出对话」还在对话模式里"


def test_resume_button_goes_through_the_normal_command(tmp_path, dispatch):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b._paused[SCOPE] = "等你确认"
    b._save_paused = lambda: None
    b._loop = object()
    _do_click(b, do_value("resume"))
    _run(dispatch[0])
    assert SCOPE not in b._paused


def test_button_text_is_dispatched_exactly_like_typing(tmp_path, dispatch):
    """点按钮派发的就是 `_handle(scope, chat, "/xxx")` —— 没有另一条捷径。"""
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b._loop = object()
    seen = []

    async def fake_handle(scope, chat_id, text):
        seen.append((scope, chat_id, text))

    b._handle = fake_handle
    for op, cmd in DO_COMMANDS.items():
        dispatch.clear()
        _do_click(b, do_value(op))
        assert len(dispatch) == 1, op
        _run(dispatch[0])
        assert seen[-1] == (SCOPE, CHAT, cmd)


# ---------- 文案：对话模式里不再叫人打 /cancel ----------


def test_talking_copy_no_longer_tells_users_to_type_cancel():
    assert "/cancel" not in TALKING_SUFFIX
    blob = build_detail_card(_live(), [], talking=True)
    assert "/cancel" not in blob
    assert "quit" in _ops(blob) and "sessions" in _ops(blob)


def test_detail_card_not_talking_has_overview_button_only():
    blob = build_detail_card(_live(), [], talking=False)
    assert _ops(blob) == ["sessions"]
    assert "发「会话」回到总览" not in blob


def test_confirm_card_no_longer_tells_users_to_type_cancel():
    assert "/cancel" not in build_confirm_relay_card(_live())


def test_entering_talk_mode_comes_with_quit_button(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    assert "quit" in _ops(b.replier.cards[-1])
    assert "/cancel" not in b.replier.texts[-1]


def test_every_reply_comes_with_talk_buttons(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    for word in ("第一句", "第二句"):
        _run(b._handle(SCOPE, CHAT, word))
        assert "/cancel" not in b.replier.texts[-1]
        ops = _ops(b.replier.cards[-1])
        assert ops[0] == "quit" and "sessions" in ops
        assert "stop" not in ops, "停不了的会话不该挂「停下」"


def test_stop_button_appears_only_when_it_can_stop(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live(can_stop=True)]))
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "跑一下"))
    assert "stop" in _ops(b.replier.cards[-1])


def test_stop_that_cannot_stop_offers_quit_button(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "/stop"))
    assert "/cancel" not in b.replier.texts[-1]
    assert _ops(b.replier.cards[-1])[0] == "quit"


def test_in_flight_notice_has_next_step_buttons(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    b._fleet_inflight[SCOPE] = ("claude", CID)
    _run(b._handle(SCOPE, CHAT, "再来一句"))
    assert "上一条还在发" in b.replier.texts[-1]
    # 第 5 轮：终端会话停不了 ⇒ 不放「停下」（原来写死 {"stop","quit"}，正是那个点不动的按钮）
    assert set(_ops(b.replier.cards[-1])) == {"quit", "sessions"}


def test_paused_notice_has_resume_button(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    b._paused[SCOPE] = "等你确认"
    _run(b._handle(SCOPE, CHAT, "接着改"))
    assert "/resume" not in b.replier.texts[-1]
    assert _ops(b.replier.cards[-1])[0] == "resume"


def test_expired_notice_offers_to_talk_again(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    clock = [1000.0]
    b.fleet._now = lambda: clock[0]
    b.fleet.overview()
    _talk(b)
    clock[0] += 301
    _run(b._handle(SCOPE, CHAT, "接着修"))
    assert "5 分钟" in b.replier.texts[-1]
    values = _values(b.replier.cards[-1])
    again = [v for v in values if v.get("act") == "fleet"]
    assert again == [{"act": "fleet", "op": "send", "tool": "claude", "id": CID}]
    # 那个按钮回调照样走 resolve_fleet_click 的闸门（id 在扫描里才认）
    from bridge.fleet.cards import resolve_fleet_click

    assert resolve_fleet_click(again[0], b.fleet.known_keys()) is not None


def test_cancel_reply_offers_stop_and_overview(tmp_path):
    """退出对话那张卡：先给「停下」，再给「回到总览」。

    🔴 第 41 轮加上「停下」的理由：那句话里写着「它手里已经开始的那一轮还在跑，要停就点
    「停下」」，而那一轮**现在真停得了**（在那个窗口里按 Esc，`control.stop` 先试这条）。
    文案提到的按钮必须真在卡上 —— 提了却不给，和假装能停是同一种骗人。
    """
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "/cancel"))
    assert "退出" in b.replier.texts[-1]
    assert _ops(b.replier.cards[-1]) == ["stop", "sessions"]


def test_typed_cancel_still_works_as_fallback(tmp_path):
    """文字命令保留做兜底。"""
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    _run(b._handle(SCOPE, CHAT, "取消"))
    assert b.fleet.peek_pending(SCOPE) is None


# ---------- 状态 / 帮助 ----------


def _status_bridge(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.connection_alive = lambda: True
    b._last_event_at = __import__("time").monotonic()
    b.prefs = None
    b.sessions = types.SimpleNamespace(
        get=lambda scope: types.SimpleNamespace(workspace=str(tmp_path), turns=0))
    return b


def test_status_reply_has_action_buttons(tmp_path):
    b = _status_bridge(tmp_path)
    _run(b._handle_command(SCOPE, CHAT, "/status"))
    ops = _ops(b.replier.cards[-1])
    assert {"dirs", "mode", "model", "sessions"} <= set(ops)
    assert "resume" not in ops and "quit" not in ops
    assert "`/mode`" not in b.replier.texts[-1]


def test_status_when_paused_and_talking_leads_with_resume(tmp_path):
    b = _status_bridge(tmp_path)
    b.fleet.overview()
    _talk(b)
    b._paused[SCOPE] = "等你确认"
    _run(b._handle_command(SCOPE, CHAT, "/status"))
    ops = _ops(b.replier.cards[-1])
    assert ops[0] == "resume" and "quit" in ops
    assert "`/resume`" not in b.replier.texts[-1]


def test_help_has_buttons(tmp_path):
    b = _status_bridge(tmp_path)
    _run(b._handle_command(SCOPE, CHAT, "/help"))
    assert {"menu", "sessions"} <= set(_ops(b.replier.cards[-1]))


# ---------- 卡片发不出去 ⇒ 文字兜底 ----------


def test_button_card_failure_falls_back_to_typed_hint(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    b.fleet.overview()
    _talk(b)
    b.replier.send_card = lambda chat_id, card: False
    _run(b._handle(SCOPE, CHAT, "/stop"))
    assert "/cancel" in b.replier.texts[-1], "按钮卡发不出去时没给能打的命令"


def test_no_card_sender_merges_hint_into_same_message(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))
    texts = []
    b.replier = types.SimpleNamespace(send=lambda chat_id, text: texts.append(text) or True)
    b._say_with_buttons(CHAT, "停不了", [do_button("quit")])
    assert len(texts) == 1 and "停不了" in texts[0] and "/cancel" in texts[0]


def test_card_sender_raising_does_not_break_reply(tmp_path):
    b = _bridge(tmp_path, RelayClaude([_live()]))

    def boom(chat_id, card):
        raise TimeoutError("飞书超时")

    b.replier.send_card = boom
    assert b._say_with_buttons(CHAT, "你好", [do_button("quit")]) is True
    assert "你好" in b.replier.texts[-2] and "/cancel" in b.replier.texts[-1]


def test_typed_fallback_shapes():
    hint = typed_fallback([do_button("quit"), do_button("stop"), actions.readonly_button()])
    assert "/cancel" in hint and "/stop" in hint and "/mode 只读" in hint
    assert typed_fallback([]) == ""


# ---------- 跳转按钮（建完群那条入群链接）----------
#
# 用户原话：「建完群给我个链接 让我点下马上进群。」
# 这个按钮的 URL 来自飞书回包、点下去直接在用户客户端里打开 ⇒ 不回调我们，闸门是域名白名单。

GOOD_LINK = "https://applink.feishu.cn/client/chat/chatter/add_by_link?link_token=abc"


def test_url_button_opens_the_link():
    button = actions.url_button("➡️ 点这里进群", GOOD_LINK)
    behavior, = button["behaviors"]
    assert behavior == {"type": "open_url", "default_url": GOOD_LINK}
    assert "value" not in behavior, "跳转按钮不回调我们，别带 value"


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)",
    "http://applink.feishu.cn/x",                     # 明文 http
    "https://applink.feishu.cn.evil.com/x",           # 域名后缀挂马
    "https://evil.com/https://applink.feishu.cn/x",
    "https://open.feishu.cn/app/x/auth",              # 飞书的别的页面也不行
    "https://applink.feishu.cn/x\nhttps://evil.com",
    "", None, 123, "https://applink.feishu.cn/" + "x" * 600,
])
def test_url_button_refuses_anything_but_feishu_applink(bad):
    """白名单是**唯一**闸门：主人一定会点这个按钮，不能让它跳到别处。"""
    assert actions.safe_link(bad) == ""
    assert actions.url_button("进群", bad) is None


def test_url_button_trims_surrounding_whitespace():
    """首尾空白是先 strip 再判前缀的（飞书回包多带一个换行不该让按钮消失）。

    注意区别：**夹在中间**的空白一律拒（上面那条 `\\n` 的用例）—— 那是拼接攻击的形状。
    """
    assert actions.safe_link(f"  {GOOD_LINK}\n") == GOOD_LINK


def test_url_button_survives_button_rows_and_card():
    """跳转按钮要能和普通按钮排在一起（`button_rows` 不认 value 也不能炸）。"""
    blob = build_buttons_card([actions.url_button("进群", GOOD_LINK), do_button("sessions")])
    card = json.loads(blob)
    row, = card["body"]["elements"]
    assert len(row["columns"]) == 2
    assert row["columns"][0]["elements"][0]["behaviors"][0]["type"] == "open_url"


def test_typed_fallback_keeps_the_link_when_the_card_fails():
    """卡片发不出去时链接要**原样贴出来** —— 少了这一下，群建好了但链接跟着卡片一起没了。"""
    hint = typed_fallback([actions.url_button("进群", GOOD_LINK), do_button("sessions")])
    assert GOOD_LINK in hint
    assert "会话" in hint, "别的按钮的兜底提示还在"


# ---------- 卡片形状 ----------


def test_buttons_card_shape_and_size():
    blob = build_buttons_card([do_button(op) for op in DO_COMMANDS])
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob  # schema 2.0 不认 action 容器（踩过）
    assert len(blob.encode("utf-8")) < actions.MAX_BUTTONS_CARD_BYTES
    for row in card["body"]["elements"]:
        assert len(row["columns"]) <= 2, "手机竖屏一排最多两个"


def test_readonly_button_goes_through_menu_whitelist():
    from bridge.menu import resolve_click

    value = actions.readonly_button()["behaviors"][0]["value"]
    assert resolve_click(value) == "/mode readonly"


def test_button_labels_are_plain_words():
    for op, label in actions.DEFAULT_LABELS.items():
        assert "/" not in label, f"按钮上不该露命令：{label}"
        assert op in actions.TOASTS


def test_button_messages_pass_the_copy_gate():
    """`test_copy_quality` 只抓 `_say(`，抓不到 `_say_with_buttons(` ⇒ 这里补上同一道闸。"""
    import pathlib
    import re

    from tests.test_copy_quality import FORBIDDEN

    src = (pathlib.Path(_HERE).parent / "bridge" / "app.py").read_text(encoding="utf-8")
    found = re.findall(r'_say(?:_later)?_with_buttons\(\s*[^,]+,\s*f?"([^"]{3,})"', src)
    assert len(found) >= 8, "一条带按钮的文案都没抓到，这条测试失效了"
    for message in found:
        for word in FORBIDDEN:
            assert word.lower() not in message.lower(), f"文案里有内部词 {word}：{message[:60]}"
        assert "/cancel" not in message and "发 /" not in message, f"还在叫人打命令：{message[:60]}"
