"""第 6 轮：审批卡点完原地换成「已处理」卡，按钮消失。

外部侦察 Top1（CC #92528「I hit it daily」）+ 竞品对标 E Top1 指到同一处：
点了「同意 / 不行」原来只弹 toast，卡片原样挂着、两个按钮还亮着 ⇒ 手机上往回翻分不清哪张批过。

修法：卡片回调响应体带 `card: {"type": "raw", "data": <卡片 dict>}`，飞书用它原地替换。
- 同意成功 / 拒绝成功 / 被掐停拒 ⇒ 换
- 已处理过 / 过期 / 签名不对 / 不是主人 ⇒ **不换**（非主人不许改主人的卡；过期卡换了像刚批过）

替换卡**只写结果 + 时间**，不放命令原文（回调里拿不到原卡正文，要放就得多落盘一份可能带凭据的东西）。
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import re
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import pytest  # noqa: E402

from bridge.review import ApprovalStore  # noqa: E402
from tests.test_token_lifecycle import (  # noqa: E402
    OWNER,
    PUBLISH,
    SCOPE,
    STRANGER,
    make_bridge,
    oks_in,
    send_card,
    write_hook,
)

SECRET_CMD = "npm publish"  # PUBLISH 的命令原文：替换卡上不许出现


@pytest.fixture
def home():
    """落盘夹具放在测试目录旁边，不放 tmp_path（macOS 上是 pathguard 豁免前缀）。"""
    base = pathlib.Path(_HERE).parent / f".r6-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield str(base)
    finally:
        shutil.rmtree(base, ignore_errors=True)


def respond(bridge, value: dict, clicker: str = OWNER):
    """模拟飞书卡片回调，返回完整响应对象（要看 card，不只 toast）。"""
    import types

    event = types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=clicker),
        action=types.SimpleNamespace(value=value),
    )
    return bridge.on_card_action(types.SimpleNamespace(event=event))


def wire(response) -> dict:
    """SDK 真正发回飞书的那段 JSON（`lark.JSON.marshal`），不是我们以为的对象。"""
    import lark_oapi as lark

    return json.loads(lark.JSON.marshal(response))


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def assert_settled_card(body: dict, title: str, template: str) -> dict:
    """替换卡的共同约束：raw + dict、schema 2.0、无按钮、无 action 容器、< 30KB、带 HH:MM、不带命令。"""
    assert "card" in body, f"响应体里没有替换卡：{body}"
    card = body["card"]
    assert card["type"] == "raw"
    data = card["data"]
    assert isinstance(data, dict), "data 用卡片 JSON 对象（不是字符串）"
    blob = json.dumps(data, ensure_ascii=False)
    assert data.get("schema") == "2.0"
    assert '"tag": "action"' not in blob  # schema 2.0 不认 action 容器（踩过）
    assert len(blob.encode("utf-8")) < 30 * 1024
    tags = {n.get("tag") for n in _walk(data)}
    assert "button" not in tags, "替换卡上不许再有按钮"
    assert not any("behaviors" in n for n in _walk(data)), "替换卡上不许再有回调"
    assert data["header"]["title"]["content"] == title
    assert data["header"]["template"] == template
    assert re.search(r"\b\d{2}:\d{2}\b", blob), "要有一行 HH:MM 时间"
    assert SECRET_CMD not in blob, "替换卡上不许出现命令原文"
    return data


# ---------- 换卡的三种结果 ----------


def test_approve_replaces_card_with_approved(home):
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    body = wire(respond(bridge, value))
    assert body["toast"]["type"] == "info"
    assert_settled_card(body, "✅ 已同意", "green")
    assert oks_in(bridge.gate.dir_for(SCOPE)), "换卡不能顶掉批准本身"


def test_approve_without_awaiting_also_replaces_card(home):
    """同意的第二条 return（`_awaiting` 已被消费、从会话记录续）也要换卡。"""
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    bridge._awaiting.clear()
    bridge.sessions = None  # 取会话 id 会抛 ⇒ 走容错，批准仍生效
    bridge._chat_ids = {}
    body = wire(respond(bridge, value))
    assert body["toast"]["type"] == "info"
    assert_settled_card(body, "✅ 已同意", "green")


def test_deny_replaces_card_with_denied(home):
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    deny = dict(value, act="deny")
    bridge._say_later = lambda chat_id, text: None
    body = wire(respond(bridge, deny))
    assert body["toast"]["type"] == "info"
    assert_settled_card(body, "🚫 没让它做", "grey")


def test_paused_approve_replaces_card_with_paused_and_writes_no_ok(home):
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    bridge._paused[SCOPE] = "这一轮有 3 次操作被挡"
    body = wire(respond(bridge, value))
    assert body["toast"]["type"] == "error"
    assert_settled_card(body, "⏸ 会话已暂停，没批", "red")
    assert not oks_in(bridge.gate.dir_for(SCOPE)), "掐停那一闸不许因换卡被绕过"


def test_settled_card_time_is_beijing_hh_mm():
    from bridge.reply import build_settled_card

    utc_noon = datetime(2026, 11, 6, 4, 5, tzinfo=timezone.utc)
    blob = json.dumps(build_settled_card("approved", now=utc_noon), ensure_ascii=False)
    assert "12:05" in blob, "北京时间 = UTC+8"
    # 换个时区的同一时刻 ⇒ 同一个北京时间
    same = utc_noon.astimezone(timezone(timedelta(hours=-5)))
    assert "12:05" in json.dumps(build_settled_card("approved", now=same), ensure_ascii=False)


# ---------- 阳性对照：这些不许换卡 ----------


def test_stranger_click_does_not_replace_card(home):
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    body = wire(respond(bridge, value, clicker=STRANGER))
    assert body["toast"]["type"] == "error"
    assert "card" not in body, "非主人点了不许改主人的卡"
    # 同一张卡主人再点照样能换 ⇒ 上面的「没换」不是因为换卡根本没接上
    assert "card" in wire(respond(bridge, value))


def test_expired_click_does_not_replace_card(home):
    from bridge.app import _load_or_create_secret

    hook = write_hook(home)
    bridge = make_bridge(home)
    bridge.approvals = ApprovalStore(_load_or_create_secret(home), ttl_sec=-60)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    body = wire(respond(bridge, value))
    assert body["toast"]["content"] == __import__("bridge.app", fromlist=["x"])._CARD_TOASTS[
        ApprovalStore.EXPIRED
    ]
    assert "card" not in body, "过期卡换了反而像刚批过"


def test_already_handled_click_does_not_replace_card(home):
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    first = wire(respond(bridge, value))
    assert "card" in first  # 阳性对照：第一次换了
    second = wire(respond(bridge, value))
    assert second["toast"]["type"] == "error"
    assert "card" not in second, "已处理过的点击不许再换一次卡"


def test_bad_signature_does_not_replace_card(home):
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    forged = dict(value, sig="0" * 32)
    body = wire(respond(bridge, forged))
    assert body["toast"]["type"] == "error"
    assert "card" not in body


# ---------- 调用点还在 ----------


def test_on_card_action_wires_settled_card_into_all_three_outcomes():
    """查调用形状 `build_settled_card("approved"…)` 等，不查名字（import 行会喂饱只查名字的断言）。"""
    from bridge.app import Bridge

    src = inspect.getsource(Bridge.on_card_action)
    for outcome in ("approved", "denied", "paused"):
        assert re.search(r'_settled\(\s*"%s"' % outcome, src), f"on_card_action 里没有换卡调用：{outcome}"
    # 同意有两条成功 return，两条都要带
    assert len(re.findall(r'_settled\(\s*"approved"', src)) >= 2
    helper = inspect.getsource(Bridge._settled)
    assert re.search(r"build_settled_card\(", helper)
    assert re.search(r'"card"\s*:\s*\{\s*"type"\s*:\s*"raw"', helper)
