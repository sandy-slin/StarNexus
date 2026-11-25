"""第 15 轮：「这张卡已经发过了」那条路**一个字都不发** ⇒ 他发了消息，完全没反应。

## 怎么来的

第 14 轮我修「同一条命令弹两张卡」时，加了一条「已经发过就不重发」的分支（`app.py`
`_ask_approval` 开头）。R1 用真钩子跑完整链发现它是条**静默路径**：

    点同意 → 写盘失败 `approve()=False` → toast「没存住你的批准，重新发一次」
    → 他照做 → 这一轮又 defer 到同一条命令 → `take_pending()` 返回空、
      `last_skipped_asked` 非空 → **只写一行日志就 return**，持续 `_ASKED_FRESH_SEC`=30 分钟

也就是说：我为了修「多一张卡」，造出了一个「完全没反应」。这正是今天领导那句
「什么消息都没有反应」的同一种形状，而我自己刚亲手又加了一个。

修法：那条分支要**说一句话**，并把他能做的下一步给出来（往上翻那张卡 / 点新话题重开）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import SCOPE, _fake_bridge, _put_pending  # noqa: E402

DIGEST = "9988dab0397d72bdd18b704a"


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.sent = []
    b.cards = []
    b._say = lambda chat_id, text: b.sent.append(text) or True
    b._say_with_buttons = lambda chat_id, text, buttons: b.sent.append(text) or True
    b.replier.send_approval_card = lambda *a, **kw: b.cards.append(a) or True
    b._chat_for = lambda scope: "chat1"
    return b


def _result():
    return types.SimpleNamespace(deferred=True, text="", session_id="s1",
                                 ok=True, denials=[], error="")


def test_already_asked_still_says_something():
    """🔴 这一条是重点：不许静默。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        session = types.SimpleNamespace(claude_session_id="s1")
        _put_pending(b, SCOPE, DIGEST, "df -h /")
        b._ask_approval(SCOPE, "chat1", session, _result())   # 第一张卡
        assert len(b.cards) == 1 and b.sent == []
        _put_pending(b, SCOPE, DIGEST, "df -h /")
        b._ask_approval(SCOPE, "chat1", session, _result())   # 又碰到同一条
        assert len(b.cards) == 1, "重复发卡了"
        assert b.sent, "静默了 —— 他发了消息什么都没收到"


def test_the_message_tells_him_what_to_do():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        session = types.SimpleNamespace(claude_session_id="s1")
        for _ in range(2):
            _put_pending(b, SCOPE, DIGEST, "df -h /")
            b._ask_approval(SCOPE, "chat1", session, _result())
        text = b.sent[-1]
        assert "卡" in text, text
        # 不许出现内部名词
        for bad in ("digest", "scope", "指纹", "退出码"):
            assert bad not in text, text
