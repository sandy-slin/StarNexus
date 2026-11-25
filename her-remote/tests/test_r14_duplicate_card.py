"""第 14 轮：同一条命令连弹两张审批卡，点第二张提示「没存住你的批准」。

现场（领导的机器，2026-11-06 19:29，他自己的日志原文）：
    19:29:06 发出审批卡 digest=9988dab0
    19:29:11 一轮结束 stop=tool_deferred
    19:29:13 发出审批卡 digest=9988dab0   ← 同一个指纹，第二张
    19:29:57 同意已记下 digest=9988dab0
    19:29:59 同意没生效 digest=9988dab0 原因=没有未决项或写不进

成因：`_ask_approval` 对 `take_pending` 返回的每一项**无条件**发卡。他一口气批了
好几张 ⇒ 排了好几次「接着跑」⇒ 每次重跑都碰到同一条还没批的命令 ⇒ 钩子又落一条
`.pending` ⇒ 又发一张一样的卡。第一张批掉之后 `.asked` 被删，第二张就批不动了，
而提示却叫他「重新发一次你的要求」—— 那一步其实已经批过、已经在做了。

两条都修：同一轮不重复发同一个指纹的卡；真撞上已批过的卡，照实说「这一步已经批过了」。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from test_interference import (  # noqa: E402
    OWNER,
    SCOPE,
    _click,
    _fake_bridge,
    _patch_dispatch,
    _put_pending,
    _toast_text,
)

DIGEST = "9988dab0397d72bdd18b704a"


def _result(session_id="s1"):
    return types.SimpleNamespace(deferred=True, text="", session_id=session_id,
                                 ok=True, denials=[], error="")


def _cards(bridge):
    return bridge.replier.sent_cards


def _bridge_with_cards(tmp):
    b = _fake_bridge(tmp)
    b.replier.sent_cards = []

    def send_approval_card(chat_id, title, action, approve_value, deny_value):
        b.replier.sent_cards.append({"action": action, "approve": approve_value})
        return True

    b.replier.send_approval_card = send_approval_card
    b._chat_for = lambda scope: "chat1"
    return b


def test_same_command_is_not_asked_twice_in_one_turn():
    """第二次重跑碰到同一条命令 ⇒ 不再发第二张卡（他日志里 9988dab0 那一对）。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge_with_cards(tmp)
        session = types.SimpleNamespace(claude_session_id="s1")
        _put_pending(b, SCOPE, DIGEST, "df -h /")
        b._ask_approval(SCOPE, "chat1", session, _result())
        assert len(_cards(b)) == 1
        # 第二次重跑，钩子又落了同一条（指纹一样）
        _put_pending(b, SCOPE, DIGEST, "df -h /")
        b._ask_approval(SCOPE, "chat1", session, _result())
        assert len(_cards(b)) == 1, f"同一个指纹发了 {len(_cards(b))} 张卡"


def test_second_card_for_other_command_still_sent():
    """阳性对照：**不同**的命令照样要发卡，别把这条路一起堵死。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge_with_cards(tmp)
        session = types.SimpleNamespace(claude_session_id="s1")
        _put_pending(b, SCOPE, DIGEST, "df -h /")
        b._ask_approval(SCOPE, "chat1", session, _result())
        _put_pending(b, SCOPE, "aaaa1111bbbb2222", "sysctl -n hw.memsize")
        b._ask_approval(SCOPE, "chat1", session, _result())
        assert len(_cards(b)) == 2


def test_clicking_an_already_approved_card_says_so():
    """点一张已经批过的旧卡 ⇒ 说「已经批过、在做了」，不是「没存住你的批准」。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge_with_cards(tmp)
        _put_pending(b, SCOPE, DIGEST, "df -h /")
        b.gate.take_pending(SCOPE)          # 发过卡 ⇒ .asked
        token1 = b.approvals.issue(SCOPE, DIGEST, {"digest": DIGEST, "scope": SCOPE})
        token2 = b.approvals.issue(SCOPE, DIGEST, {"digest": DIGEST, "scope": SCOPE})
        scheduled, restore = _patch_dispatch(b)
        try:
            _click(b, token1)               # 第一张：真批掉
            text = _toast_text(_click(b, token2))   # 第二张：同一步
        finally:
            restore()
        assert "没存住" not in text, text
        assert "批过" in text or "在做" in text, text
