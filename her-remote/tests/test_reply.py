"""回复层测试：重点是「超长时不许假装发完了」。

背景：飞书超过上限会**静默丢弃**，不报错。真实案例里 agent 干完活花了钱，
卡片没更新，用户盯着「正在思考」等到永远。所以这层的失败必须是可见的。
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.reply import Replier, chunk_text  # noqa: E402


class FakeResponse:
    def __init__(self, ok: bool = True) -> None:
        self._ok = ok
        self.code = 0 if ok else 230025
        self.msg = "ok" if ok else "message too long"

    def success(self) -> bool:
        return self._ok


class FakeClient:
    """够用的桩：记下每次发送的内容，可被设成失败。"""

    def __init__(self, ok: bool = True) -> None:
        self.sent: list[str] = []
        self._ok = ok
        self.im = self

    @property
    def v1(self):
        return self

    @property
    def message(self):
        return self

    def create(self, request):
        import json

        body = request.request_body
        content = json.loads(body.content)
        # 三种形状都要能记下正文，否则改了消息类型测试就全废：
        #   text         -> {"text": "..."}
        #   post（富文本）-> {"zh_cn": {"content": [[{"tag":"md","text":"..."}]]}}
        #   interactive  -> 卡片 JSON（没有 text 字段，记整个 content）
        if "text" in content:
            self.sent.append(content["text"])
        elif "zh_cn" in content:
            blocks = content["zh_cn"].get("content") or []
            self.sent.append(
                "".join(
                    seg.get("text", "")
                    for line in blocks
                    for seg in line
                )
            )
        else:
            self.sent.append(body.content)
        return FakeResponse(self._ok)


# ---------- chunk_text ----------


def test_short_text_is_single_chunk():
    assert chunk_text("hello", 100) == ["hello"]


def test_splits_on_line_boundaries():
    text = "\n".join(f"line{i}" for i in range(100))
    chunks = chunk_text(text, 60)
    assert len(chunks) > 1
    # 关键：拼回来必须和原文完全一致，不能丢字符
    assert "".join(chunks) == text


def test_single_overlong_line_is_hard_split():
    text = "x" * 250
    chunks = chunk_text(text, 100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == text


def test_no_content_lost_with_mixed_lines():
    text = "short\n" + "y" * 300 + "\nshort again\n"
    chunks = chunk_text(text, 80)
    assert "".join(chunks) == text


# ---------- Replier ----------


def test_sends_short_message_once():
    client = FakeClient()
    assert Replier(client, 1000).send("oc_x", "hi") is True
    assert client.sent == ["hi"]


def test_empty_text_still_says_something():
    """静默不发 = 用户以为机器人死了。"""
    client = FakeClient()
    Replier(client, 1000).send("oc_x", "   ")
    assert len(client.sent) == 1
    assert client.sent[0].strip()


def test_overlong_output_admits_truncation():
    """超长时必须明说有多少没发出，不许假装发完。"""
    client = FakeClient()
    Replier(client, 100).send("oc_x", "z" * 10000)
    joined = "\n".join(client.sent)
    assert "没发出" in joined
    assert len(client.sent) <= 4


def test_send_failure_is_reported_not_swallowed():
    client = FakeClient(ok=False)
    assert Replier(client, 1000).send("oc_x", "hi") is False


def test_send_success_returns_true_positive_control():
    """阳性对照：上一条的 False 必须来自真失败，而不是这层永远返回 False。"""
    client = FakeClient(ok=True)
    assert Replier(client, 1000).send("oc_x", "hi") is True


def test_max_chars_is_capped_by_platform_limit():
    """配置里写得再大也不能超过平台安全线。"""
    from bridge.reply import TEXT_SAFE_CHARS

    client = FakeClient()
    replier = Replier(client, 10_000_000)
    assert replier._max_chars <= TEXT_SAFE_CHARS


# ---------- limit <= 0：会死循环的那一类输入 ----------
#
# `max_reply_chars = 0`（手抖、或者想"关掉限制"写了 0）能原样通过 Settings.load，
# 一路传到 chunk_text。那里 `while len(line) > limit` 每轮切 `line[:0]` = 空串，
# 行长度永远不减 ⇒ **整个事件循环卡死在这一轮**，进程还活着、日志不动、
# 看门狗 15 分钟后才会把它打死。所以这里必须收敛而不是信任调用方。


def test_zero_limit_does_not_hang():
    """回归：limit=0 曾经让 chunk_text 无限循环。"""
    chunks = chunk_text("hello\nworld", 0)
    assert "".join(chunks) == "hello\nworld"


def test_negative_limit_does_not_hang():
    chunks = chunk_text("abc", -5)
    assert "".join(chunks) == "abc"


def test_replier_floors_bad_limit_from_config():
    """配置里写 0 不能让回复层卡死，要落到一个能用的下限。"""
    client = FakeClient()
    replier = Replier(client, 0)
    assert replier._max_chars > 0
    assert replier.send("oc_x", "hi") is True
    assert client.sent == ["hi"]


# ---------- 审批卡片的 schema 2.0 形状 ----------
#
# 2026-11-03 真实故障：用了 schema 1.0 的 `{"tag":"action","actions":[...]}` 容器，
# 飞书回 code=230099 / ErrCode: 200861 /「cards of schema V2 no longer support this
# capability; ErrorValue: unsupported tag action」。
# 后果是 agent 停在 defer 等一个永远不会来的点击。三种候选形状实打飞书验过。


def _approval_card(replier_client):
    """抓出 send_approval_card 真正发出去的卡片 JSON。"""
    from bridge.reply import Replier

    r = Replier(replier_client, 20000)
    r.send_approval_card(
        "oc_1", "标题", "跑这条命令：ls", {"act": "approve"}, {"act": "deny"}
    )
    return json.loads(replier_client.sent[-1])


class CardCapturingClient(FakeClient):
    """复用 FakeClient，但记下 interactive 消息的 content。"""


def test_card_declares_schema_2():
    client = FakeClient()
    card = _approval_card(client)
    assert card["schema"] == "2.0"


def test_card_has_no_action_tag_container():
    """硬回归：schema 2.0 不支持 action 容器，用了就整张卡发不出去。"""
    client = FakeClient()
    card = _approval_card(client)
    blob = json.dumps(card, ensure_ascii=False)
    assert '"tag": "action"' not in blob
    assert '"actions"' not in blob


def test_buttons_use_behaviors_not_top_level_value():
    """schema 2.0 的回调参数走 behaviors；写顶层 value 点了没反应。"""
    client = FakeClient()
    card = _approval_card(client)
    buttons = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                buttons.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(card)
    assert len(buttons) == 2, f"应该有两个按钮，实际 {len(buttons)}"
    for b in buttons:
        assert "behaviors" in b, "按钮必须用 behaviors 声明回调"
        assert b["behaviors"][0]["type"] == "callback"
        assert isinstance(b["behaviors"][0]["value"], dict), "value 必须是 object"
        assert "value" not in b, "顶层 value 是 schema 1.0 的写法"


def test_card_carries_both_decisions():
    client = FakeClient()
    card = _approval_card(client)
    blob = json.dumps(card, ensure_ascii=False)
    assert "approve" in blob and "deny" in blob


def test_card_body_is_truncated_for_30kb_limit():
    """卡片 30KB 上限，超了飞书静默丢弃。"""
    from bridge.reply import Replier

    client = FakeClient()
    Replier(client, 20000).send_approval_card(
        "oc_1", "t", "x" * 50000, {"act": "approve"}, {"act": "deny"}
    )
    assert len(client.sent[-1]) < 8000


def test_card_send_failure_is_reported():
    from bridge.reply import Replier

    client = FakeClient(ok=False)
    ok = Replier(client, 20000).send_approval_card(
        "oc_1", "t", "body", {"act": "approve"}, {"act": "deny"}
    )
    assert ok is False, "发送失败必须返回 False，上层才能退化成纯文本"


# ---------- receive_id_type 按 id 前缀选 ----------


def test_open_id_receiver_uses_open_id_type():
    """🔴 菜单点击只带 open_id。按 chat_id 发 ⇒ 230001，用户点了看不到任何回应。"""
    from bridge.reply import _id_type

    assert _id_type("ou_abc123") == "open_id"


def test_chat_id_receiver_uses_chat_id_type():
    from bridge.reply import _id_type

    assert _id_type("oc_abc123") == "chat_id"


def test_every_send_path_uses_the_router():
    """五条发送路径都要走 `_id_type`，不能有一条还写死 chat_id。"""
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge", "reply.py")
    source = open(path, encoding="utf-8").read()
    assert '.receive_id_type("chat_id")' not in source
    assert source.count(".receive_id_type(_id_type(") >= 5


def test_approval_card_body_is_redacted():
    """🔴 审批卡正文原来不过脱敏：agent 读了 .env 再把 key 拼进命令，key 就上了卡片。"""
    import json as _json
    import types

    from bridge.reply import Replier

    sent = {}

    class _Msg:
        def create(self, request):
            sent["content"] = request.request_body.content
            return types.SimpleNamespace(success=lambda: True, code=0, msg="ok")

    client = types.SimpleNamespace(
        im=types.SimpleNamespace(v1=types.SimpleNamespace(message=_Msg()))
    )
    key = "sk" + "-proj-" + "A" * 32
    Replier(client, 2000).send_approval_card(
        "oc_x", "要动手了", f"执行命令：curl -H 'Authorization: Bearer {key}' x", {}, {}
    )
    assert key not in sent["content"], "密钥原样上了审批卡"
    assert "要动手了" in _json.loads(sent["content"])["header"]["title"]["content"]
