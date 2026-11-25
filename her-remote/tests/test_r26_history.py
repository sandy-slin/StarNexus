"""「↑ 最近问过」demo：记、翻、改了再发、按会话分、脱敏、陌生人点不动。"""
import asyncio
import json
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bridge.history import (FORM_NAME, INPUT_NAME, LIST, InputHistory, build_card,  # noqa: E402
                            fingerprint, submitted_text)
from test_interference import OWNER, _fake_bridge  # noqa: E402

SCOPE = f"dm:{OWNER}"      # 卡片点击按点击人的单聊认 scope（见 app._click_scope）


def test_records_newest_first_dedups_and_skips_commands(tmp_path):
    h = InputHistory(str(tmp_path / "h.json"))
    for t in ["看看桌面", "/status", "菜单", "列一下项目", "看看桌面"]:
        h.add(SCOPE, t)
    assert h.list(SCOPE) == ["看看桌面", "列一下项目"]


def test_scopes_are_separate_and_secrets_masked(tmp_path):
    h = InputHistory(str(tmp_path / "h.json"))
    h.add("dm:a", "密码：Tr0ub4dor&3xyz 帮我登一下")
    h.add("group:b", "群里的话")
    assert h.list("group:b") == ["群里的话"]
    assert "Tr0ub4dor" not in h.list("dm:a")[0]
    raw = (tmp_path / "h.json").read_text(encoding="utf-8")
    assert "Tr0ub4dor" not in raw
    assert oct(os.stat(tmp_path / "h.json").st_mode & 0o777) == "0o600"


def test_corrupt_file_is_empty_history(tmp_path):
    p = tmp_path / "h.json"
    p.write_text("{坏", encoding="utf-8")
    h = InputHistory(str(p))
    assert h.list(SCOPE) == []
    h.add(SCOPE, "第一句话")
    assert h.list(SCOPE) == ["第一句话"]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _picker(card):
    return next(e for e in card["body"]["elements"] if e.get("tag") == "select_static")


def _box(card):
    return next(n for n in _walk(card) if n.get("tag") == "input")


def test_enter_to_send_box_obeys_the_client_rules():
    """🔴 用户实测：上一版下拉里回车没起作用。本机 Lark 卡片 input：单行 + 不在 form 里 + 带回调 ⇒ 回车才发。"""
    card = build_card(["第三句", "第二句", "第一句"])
    assert not any(n.get("tag") == "form" for n in _walk(card)), "在表单里回车什么都不做"
    box = _box(card)
    assert box.get("input_type", "text") == "text", "多行框的回车只插空格"
    assert box["behaviors"][0]["value"] == {"act": "hist", "op": "send"}
    assert box["default_value"] == "第三句", "默认填最近那句（像按一下 ↑）"
    assert box["max_length"] <= 1000
    picker = _picker(card)
    assert "initial_index" not in picker and picker["options"][0]["text"]["content"].startswith("第三句")
    json.dumps(card, ensure_ascii=False)


def test_options_show_when():
    import time as _t

    now = _t.time()
    card = build_card([("刚问的", int(now - 30)), ("早一点", int(now - 600))], now=now)
    labels = [o["text"]["content"] for o in _picker(card)["options"]]
    assert "刚刚" in labels[0] and "10 分钟前" in labels[1], labels


def test_long_sentence_label_is_cut():
    long = "帮我看看" * 40
    assert len(_picker(build_card([long]))["options"][0]["text"]["content"]) <= 41


def test_empty_history_card_says_so():
    card = build_card([])
    assert "还没有记录" in json.dumps(card, ensure_ascii=False)


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.settings.config_path = os.path.join(tmp, "config.toml")
    b._chat_ids = {SCOPE: "oc_1"}
    b._loop = object()
    return b


def _click(b, value, form=None, clicker=OWNER, option=None):
    ev = types.SimpleNamespace(event=types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=clicker),
        context=types.SimpleNamespace(open_chat_id="oc_1"),
        action=types.SimpleNamespace(value=value, form_value=form, input_value=None, option=option)))
    return b.on_card_action(ev)


def _body(resp):
    out = {}
    if getattr(resp, "toast", None) is not None:
        out["toast"] = {"content": resp.toast.content, "type": resp.toast.type}
    if getattr(resp, "card", None) is not None:
        out["card"] = resp.card.data
    return out


def _capture(monkeypatch):
    import bridge.app as app_mod

    sent = []

    def fake(coro, loop):
        sent.append(coro)
    monkeypatch.setattr(app_mod.asyncio, "run_coroutine_threadsafe", fake)
    return sent


def _run_sent(b, sent):
    """把派出去的协程真跑一遍，返回 (回显的话, 进 _handle 的话)。"""
    said, handled = [], []
    b._say = lambda chat, text: said.append(text) or True

    async def handle(scope, chat, text):
        handled.append(text)
    b._handle = handle
    for coro in sent:
        asyncio.run(coro)
    return said, handled


def _sent_card_text(r):
    return json.dumps(r.get("card"), ensure_ascii=False)


def test_dropdown_pick_only_fills_the_box(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        for t in ["第一句", "看看桌面", "第三句"]:
            b._remember_input(SCOPE, t)
        sent = _capture(monkeypatch)
        r = _body(_click(b, {"act": "hist", "op": "pick"}, option=fingerprint("看看桌面")))
        assert sent == [], "下拉选中不发，发送交给框的回车"
        assert _box(r["card"])["default_value"] == "看看桌面" and "回车" in r["toast"]["content"]


def test_enter_in_box_sends_edited_text_with_echo_and_receipt(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._remember_input(SCOPE, "看看桌面")
        sent = _capture(monkeypatch)
        ev = types.SimpleNamespace(event=types.SimpleNamespace(
            operator=types.SimpleNamespace(open_id=OWNER), context=types.SimpleNamespace(open_chat_id="oc_1"),
            action=types.SimpleNamespace(value={"act": "hist", "op": "send"}, form_value=None,
                                         input_value="看看桌面和下载", option=None)))
        r = _body(b.on_card_action(ev))
        card = _sent_card_text(r)
        assert "已发" in card and "看看桌面和下载" in card and '"tag": "input"' not in card
        said, handled = _run_sent(b, sent)
        assert handled == ["看看桌面和下载"] and said[0].startswith("↑ 你问：看看桌面和下载")


def test_same_sentence_twice_within_10s_sends_once(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        sent = _capture(monkeypatch)
        for _ in range(2):
            ev = types.SimpleNamespace(event=types.SimpleNamespace(
                operator=types.SimpleNamespace(open_id=OWNER), context=types.SimpleNamespace(open_chat_id="oc_1"),
                action=types.SimpleNamespace(value={"act": "hist", "op": "send"}, form_value=None,
                                             input_value="看看桌面", option=None)))
            r = _body(b.on_card_action(ev))
        assert len(sent) == 1 and "刚发过" in r["toast"]["content"]


def test_risky_sentence_gets_a_warning_above_the_box():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.settings.policy = b.settings.policy.__class__(**{**b.settings.policy.__dict__, "mode": "trusted",
                                                            "ceiling_mode": "trusted"})
        b.prefs = None
        b._remember_input(SCOPE, "帮我把 build 目录删掉")
        r = _body(_click(b, {"act": "hist", "op": "pick"}, option=fingerprint("帮我把 build 目录删掉")))
        assert "⚠️" in _sent_card_text(r) and "会改东西" in _sent_card_text(r)
        b._remember_input(SCOPE, "看看桌面上有什么")
        r = _body(_click(b, {"act": "hist", "op": "pick"}, option=fingerprint("看看桌面上有什么")))
        assert "⚠️" not in _sent_card_text(r)


def test_pending_approval_and_talk_mode_warn():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._remember_input(SCOPE, "看看桌面上有什么")
        d = b.gate.dir_for(SCOPE)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "abc.asked"), "w").write("{}")
        r = _body(_click(b, {"act": "hist", "op": "list"}))
        assert "确认卡在等你" in _sent_card_text(r)
        os.remove(os.path.join(d, "abc.asked"))
        b.fleet = types.SimpleNamespace(peek_pending_fresh=lambda scope: (object(), False))
        r = _body(_click(b, {"act": "hist", "op": "list"}))
        assert "接着聊" in _sent_card_text(r)


def test_receipt_buttons_edit_and_list():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._remember_input(SCOPE, "看看桌面")
        r = _body(_click(b, {"act": "hist", "op": "edit", "h": fingerprint("看看桌面")}))
        assert _box(r["card"])["default_value"] == "看看桌面"
        r = _body(_click(b, {"act": "hist", "op": "list"}))
        assert "select_static" in _sent_card_text(r)


def test_pick_unknown_gives_fresh_card(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._remember_input(SCOPE, "第一句")
        sent = _capture(monkeypatch)
        r = _body(_click(b, {"act": "hist", "op": "pick"}, option="deadbeef0000"))
        assert sent == [] and "更新" in r["toast"]["content"] and "select_static" in _sent_card_text(r)


def _held(b, monkeypatch, text):
    b._remember_input(SCOPE, text)
    sent = _capture(monkeypatch)
    r = _body(_click(b, {"act": "hist", "op": "pick"}, option=fingerprint(text)))
    return sent, r


def test_typing_up_arrow_opens_the_card():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        shown = []
        b._send_history_card = lambda scope, chat: shown.append(scope)
        b._sync_talk_marker = lambda *a: None
        asyncio.run(b._handle(SCOPE, "oc_1", "↑"))
        assert shown == [SCOPE] and b._history().list(SCOPE) == []


def test_stranger_cannot_read_or_send(monkeypatch):
    import bridge.app as app_mod

    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b._remember_input(SCOPE, "主人的私话")
        sent = []
        monkeypatch.setattr(app_mod.asyncio, "run_coroutine_threadsafe", lambda c, l: sent.append(c))
        for value, form, opt in [({"act": "hist", "op": "pick"}, None, fingerprint("主人的私话")),
                                 ({"act": "hist", "op": "edit", "h": fingerprint("主人的私话")}, None, None),
                                 ({"act": "hist", "op": "send"}, {INPUT_NAME: "rm -rf ~"}, None)]:
            r = _body(_click(b, value, form, clicker="ou_stranger", option=opt))
            assert "card" not in r and "主人的私话" not in json.dumps(r, ensure_ascii=False)
        assert sent == []


def test_handle_records_what_he_typed():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)

        async def nothing(*a, **k):
            return None
        b._maybe_send_to_fleet = nothing
        b._run_locally = nothing
        b._sync_talk_marker = lambda *a: None
        asyncio.run(b._handle(SCOPE, "oc_1", "帮我看看终端在跑什么"))
        assert b._history().list(SCOPE) == ["帮我看看终端在跑什么"]


def test_submitted_text_reads_form_value():
    a = types.SimpleNamespace(form_value={INPUT_NAME: "  改过的话 "}, input_value=None)
    assert submitted_text(a) == "改过的话"
    assert submitted_text(types.SimpleNamespace(form_value={}, input_value="")) is None
