"""第 33 轮：点模型 / 权限卡上的一档，**这张卡**要原地刷新。

用户原话：「选中模型后，比如原来是最快，我点上最聪明，下面说变换了，但是上面四个按钮没有变，依然是最快的被选中」。
原来点了只是丢后台当命令跑 ⇒ 聊天里多一条「换成了…」，卡上的 ✅ 还停在旧的那个上。
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_mode_switch_e2e import OWNER, _bridge  # noqa: E402

FAST, SMART = "claude-haiku-5-5", "claude-opus-5-5"


def _click(b, value):
    b._chat_ids = {f"dm:{OWNER}": "oc_1"}
    b._loop = None
    ev = types.SimpleNamespace(event=types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=OWNER),
        context=types.SimpleNamespace(open_chat_id="oc_1"),
        action=types.SimpleNamespace(value=value)))
    return b.on_card_action(ev)


def _selected(card: dict) -> list:
    """卡上标了 ✅ / 主按钮的那几个按钮文字。"""
    out = []
    def walk(n):
        if isinstance(n, dict):
            if n.get("tag") == "button" and (n.get("type") == "primary" or
                                              "✅" in n.get("text", {}).get("content", "")):
                out.append(n["text"]["content"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(card)
    return out


def test_picking_a_model_refreshes_the_card_in_place(tmp_path):
    from bridge.policy import MODEL_LABELS

    b = _bridge(tmp_path)
    b.prefs.set_model(FAST)
    resp = _click(b, {"act": "menu", "model": SMART})
    assert resp.card is not None, "点了模型，卡没换 ⇒ ✅ 还停在旧的上面"
    sel = _selected(resp.card.data)
    assert sel and all(MODEL_LABELS[SMART] in t for t in sel), sel
    assert not any(MODEL_LABELS[FAST] in t for t in sel), sel
    assert b.policy.model == SMART, "卡换了但模型没真换"
    assert MODEL_LABELS[SMART] in resp.toast.content


def test_tightening_mode_refreshes_the_card_in_place(tmp_path):
    from bridge.policy import MODE_LABELS

    b = _bridge(tmp_path, "trusted")
    resp = _click(b, {"act": "menu", "mode": "readonly"})
    assert resp.card is not None
    sel = _selected(resp.card.data)
    assert sel and all(MODE_LABELS["readonly"] in t for t in sel), sel
    assert b.policy.mode == "readonly"


def test_loosening_mode_still_asks_first_and_does_not_switch(tmp_path):
    """放开要二次确认 —— 不许在回调里当场放开。"""
    b = _bridge(tmp_path, "trusted")
    b.prefs.set_mode("readonly")
    resp = _click(b, {"act": "menu", "mode": "trusted"})
    assert resp.card is None
    assert b.policy.mode == "readonly", "点一下就放开了，跳过了二次确认"


def test_unknown_model_does_not_refresh_or_switch(tmp_path):
    b = _bridge(tmp_path)
    before = b.policy.model
    resp = _click(b, {"act": "menu", "model": "gpt-not-real"})
    assert resp.card is None and b.policy.model == before


def test_chat_confirmation_line_is_still_sent(tmp_path):
    """用户：「『🧠 换成 稳（claude-opus-5）。下一条消息开始生效』这样的提示要依然留着」。"""
    from bridge.policy import MODEL_LABELS

    b = _bridge(tmp_path)
    b._say_later = lambda chat, text: b.said.append(text)
    _click(b, {"act": "menu", "model": SMART})
    assert any(t.startswith("🧠 换成") and MODEL_LABELS[SMART] in t and SMART in t and "下一条消息开始生效" in t
               for t in b.said), b.said
    _click(b, {"act": "menu", "mode": "readonly"})
    assert any(t.startswith("🔑 现在是") and "下一条消息开始生效" in t for t in b.said), b.said
