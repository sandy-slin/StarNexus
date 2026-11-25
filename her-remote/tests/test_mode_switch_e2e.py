"""切档走的是**真实处理函数**，不是只测 resolve_click。

这组测试的意义：`resolve_click` 只负责"认不认这个点击"，真正提权发生在
`_handle_mode` ⇒ `PrefsStore.set_mode`。只测前者等于只测了门牌号。

🔴 判据是**档位本身变没变**（`bridge.policy.mode`），不是回话里说了什么。
参照记忆里那条教训：线程池吞异常时任务显示"跑完"而结果 0 条，动作却已真执行 ——
判做没做只认被操作对象自身的状态。
"""

from __future__ import annotations

import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.app import Bridge  # noqa: E402
from bridge.menu import resolve_click  # noqa: E402
from bridge.policy import Policy  # noqa: E402
from bridge.prefs import PrefsStore  # noqa: E402
from bridge.sessions import SessionStore  # noqa: E402

OWNER = "ou_owner"


def _bridge(tmp_path, config_mode: str = "trusted"):
    """造一个只装切档这条路所需字段的 Bridge。不碰网络、不碰用户目录。"""
    base = Policy(
        mode=config_mode,
        workspaces=("/tmp",),
        default_workspace="/tmp",
        model="claude-sonnet-5-5",
        turn_timeout_sec=60,
        ceiling_mode=config_mode,
    )
    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(
        owner_open_ids=frozenset({OWNER}), policy=base, custom_rules=()
    )
    b.prefs = PrefsStore(str(tmp_path / "prefs.json"), base)
    b.sessions = SessionStore("/tmp", 30)
    b.workspaces = None  # 换目录分支用 getattr 兜底，None 时跳过
    b._paused = {}
    b._awaiting = {}
    b._cancels = {}
    b._loop = None
    b.said = []
    b.cards = []
    b._say = lambda chat_id, text: (b.said.append(text), True)[1]
    b.replier = types.SimpleNamespace(
        send=lambda chat_id, text: (b.said.append(text), True)[1],
        send_card=lambda chat_id, card: (b.cards.append(card), True)[1],
    )
    return b


def _click(bridge, value: dict) -> None:
    """模拟一次卡片点击：resolve_click → **真实的** `_handle_command`。

    🔴 原来这里抄了一份分派表直接调 `_handle_mode` —— 2026-11-04 review 实测：
    把 app.py 里 `/mode`、`/mode!`、`/model` 三个分支全删掉，1178 个测试照样全绿。
    现在走真正的命令分派，那三个分支坏了这里就红。
    """
    import asyncio

    text = resolve_click(value)
    if not text:
        return
    handled = asyncio.run(bridge._handle_command("dm:" + OWNER, "c1", text))
    assert handled, f"{text!r} 没被命令分派接住 —— 会被当成问题发给 claude"


# ---------- 放开要两次 ----------


def test_one_click_does_not_escalate(tmp_path):
    """🔴 核心断言：选择卡上点一下「全授权」**不能**真的提权。"""
    b = _bridge(tmp_path)
    b.prefs.set_mode("readonly")
    assert b.policy.mode == "readonly"

    _click(b, {"act": "menu", "mode": "trusted"})
    assert b.policy.mode == "readonly", "一次点击就提权了 —— 二次确认没生效"
    assert b.cards, "应该发出一张确认卡"
    assert "确认放开" in b.cards[-1]


def test_second_click_applies(tmp_path):
    b = _bridge(tmp_path)
    b.prefs.set_mode("readonly")
    _click(b, {"act": "menu", "mode": "trusted"})
    _click(b, {"act": "menu", "mode2": "trusted"})
    assert b.policy.mode == "trusted"
    assert any("不再逐步问你" in t for t in b.said), "放开之后要给出能看见的提醒"


def test_escalation_survives_restart(tmp_path):
    """提权要落盘 —— 看门狗会自杀重启，内存态活不过一次重启。"""
    b = _bridge(tmp_path)
    b.prefs.set_mode("readonly")
    _click(b, {"act": "menu", "mode2": "trusted"})
    assert b.policy.mode == "trusted"
    fresh = _bridge(tmp_path)
    assert fresh.policy.mode == "trusted"


def test_tightening_takes_one_click(tmp_path):
    """收紧不该多一步 —— 用户要收回权限时不能有摩擦。"""
    b = _bridge(tmp_path)
    assert b.policy.mode == "trusted"
    _click(b, {"act": "menu", "mode": "readonly"})
    assert b.policy.mode == "readonly"
    assert not b.cards, "收紧不该弹确认卡"


def test_write_is_also_a_loosening(tmp_path):
    """readonly → write 也是放开（能改文件了），同样要确认。"""
    b = _bridge(tmp_path)
    b.prefs.set_mode("readonly")
    _click(b, {"act": "menu", "mode": "write"})
    assert b.policy.mode == "readonly"
    assert b.cards and "确认放开" in b.cards[-1]


def test_card_send_failure_fails_closed(tmp_path):
    """🔴 确认卡发不出去 ⇒ **不提权**，并告诉用户怎么继续。

    宁可多一步，不要在他没看到任何警告的情况下放开。
    """
    b = _bridge(tmp_path)
    b.prefs.set_mode("readonly")
    b.replier.send_card = lambda chat_id, card: False
    _click(b, {"act": "menu", "mode": "trusted"})
    assert b.policy.mode == "readonly", "卡片发失败却提权了"
    assert any("/mode!" in t for t in b.said), "要给用户一条能继续的路"


# ---------- 天花板 ----------


def test_ceiling_blocks_even_confirmed_clicks(tmp_path):
    """🔴 确认过的点击**也不能**越过本机天花板。

    `mode2` 是"用户已表达意图"，不是"绕过策略"。IT 锁成 readonly 时，
    连点确认也只能停在 readonly。
    """
    b = _bridge(tmp_path, config_mode="readonly")
    _click(b, {"act": "menu", "mode2": "trusted"})
    assert b.policy.mode == "readonly"
    assert any("最多放到" in t or "改本机" in t for t in b.said)


def test_clamped_switch_says_who_blocked_it(tmp_path):
    """被压住要说清是谁挡的、怎么才能真放开 —— 否则用户只觉得坏了。"""
    b = _bridge(tmp_path, config_mode="write")
    b.prefs.set_mode("readonly")
    _click(b, {"act": "menu", "mode2": "trusted"})
    assert b.policy.mode == "write"
    said = " ".join(b.said)
    # 2026-11-06 第 11 轮：不再叫手机用户去改 config.toml（他改不了），改成找给他装的同事
    assert "同事" in said and "config.toml" not in said


# ---------- 切档真的改变闸门 ----------


def test_switch_changes_the_tool_allowlist(tmp_path):
    """切档必须真的改变传给 claude 的工具白名单，不能只改显示。"""
    b = _bridge(tmp_path)
    _click(b, {"act": "menu", "mode": "readonly"})
    tools = b.policy.allowed_tools()
    assert "Bash" not in tools and "Write" not in tools
    _click(b, {"act": "menu", "mode2": "trusted"})
    tools = b.policy.allowed_tools()
    assert "Bash" in tools


def test_read_boundary_survives_every_mode(tmp_path):
    """任何档位下读边界都还在 —— "不问就做"不等于"能翻遍整台电脑"。"""
    b = _bridge(tmp_path)
    for value in (
        {"act": "menu", "mode": "readonly"},
        {"act": "menu", "mode2": "write"},
        {"act": "menu", "mode2": "trusted"},
    ):
        _click(b, value)
        tools = b.policy.allowed_tools()
        assert any(t.startswith("Read(") for t in tools), f"{b.policy.mode} 下读边界丢了"
        # 硬红线在任何档位下都在
        assert "Read(**/.env*)" in b.policy.disallowed_tools()


def test_runner_argv_uses_the_switched_mode(tmp_path):
    """🔴 验到 argv —— 这才是真正递给 claude 的东西。

    中间任何一层忘了传 policy，这条就会红（记忆里那条"改完源码≠上线"）。
    """
    from bridge.runner import ClaudeRunner

    b = _bridge(tmp_path)
    runner = ClaudeRunner(b.settings.policy)
    _click(b, {"act": "menu", "mode": "readonly"})
    argv = runner._build_argv("hi", None, "/tmp", b.policy)
    assert "Bash" not in argv, "只读档位却把 Bash 发给了 claude"
    _click(b, {"act": "menu", "mode2": "trusted"})
    argv = runner._build_argv("hi", None, "/tmp", b.policy)
    assert "Bash" in argv


def test_runner_argv_uses_the_switched_model(tmp_path):
    from bridge.runner import ClaudeRunner

    b = _bridge(tmp_path)
    runner = ClaudeRunner(b.settings.policy)
    _click(b, {"act": "menu", "model": "claude-haiku-5-5"})
    argv = runner._build_argv("hi", None, "/tmp", b.policy)
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"


def test_runner_without_policy_falls_back_to_startup(tmp_path):
    """不传 policy 时用启动那份 —— 老调用方行为不变。"""
    from bridge.runner import ClaudeRunner

    b = _bridge(tmp_path, config_mode="readonly")
    runner = ClaudeRunner(b.settings.policy)
    argv = runner._build_argv("hi", None, "/tmp")
    assert "Bash" not in argv


# ---------- 模型 ----------


def test_bad_model_is_rejected_with_options(tmp_path):
    b = _bridge(tmp_path)
    b._handle_model("c1", "/model gpt-4o")
    assert b.policy.model == "claude-sonnet-5-5"
    assert any("能选" in t for t in b.said)


def test_model_by_short_label(tmp_path):
    """手机上用户会打「最快」而不是全名。"""
    b = _bridge(tmp_path)
    b._handle_model("c1", "/model 最快")
    assert b.policy.model == "claude-haiku-5-5"


def test_mode_by_chinese_alias(tmp_path):
    b = _bridge(tmp_path)
    b._handle_mode("c1", "/mode 只读")
    assert b.policy.mode == "readonly"


def test_unknown_mode_arg_does_not_change_anything(tmp_path):
    b = _bridge(tmp_path)
    b._handle_mode("c1", "/mode 随便")
    assert b.policy.mode == "trusted"
    assert any("不认识" in t for t in b.said)


# ---------- 没有 prefs 时不能崩 ----------


def test_missing_prefs_falls_back_to_config(tmp_path):
    """prefs 缺失时回落到配置那份，方向是更严而不是更松。"""
    b = _bridge(tmp_path, config_mode="readonly")
    del b.prefs
    assert b.policy.mode == "readonly"
    b._handle_mode("c1", "/mode write")
    assert b.policy.mode == "readonly"


# ---------- 常驻菜单点击：真事件对象 → 真分派 ----------


def _menu_event(event_key: str, open_id: str = OWNER):
    """用 SDK 自己的模型造事件 —— 不用 SimpleNamespace，免得字段名写错也能过。"""
    from lark_oapi.api.application.v6.model.p2_application_bot_menu_v6 import (
        P2ApplicationBotMenuV6,
    )

    return P2ApplicationBotMenuV6(
        {
            "schema": "2.0",
            "header": {"event_type": "application.bot.menu_v6"},
            "event": {
                "event_key": event_key,
                "operator": {"operator_id": {"open_id": open_id}},
                "timestamp": 1,
            },
        }
    )


def _capture_dispatch(bridge):
    import bridge.app as app_module

    sent = []
    original = app_module.asyncio.run_coroutine_threadsafe

    def fake(coro, loop):
        sent.append(coro.cr_frame.f_locals.get("text"))
        sent.append(coro.cr_frame.f_locals.get("chat_id"))
        coro.close()

    app_module.asyncio.run_coroutine_threadsafe = fake
    bridge._loop = object()
    return sent, lambda: setattr(app_module.asyncio, "run_coroutine_threadsafe", original)


def test_bot_menu_click_dispatches_preset_text(tmp_path):
    b = _bridge(tmp_path)
    b.touch = lambda: None
    sent, restore = _capture_dispatch(b)
    try:
        b.on_bot_menu(_menu_event("q:projects"))
    finally:
        restore()
    assert sent[0] == "我有哪些项目？各自是什么"


def test_bot_menu_click_without_known_chat_falls_back_to_open_id(tmp_path):
    """🔴 菜单事件只带 open_id。没发过消息时只能拿 open_id 当收件人，
    reply.py 按 `ou_` 前缀选 receive_id_type=open_id —— 两边对上才收得到回复。"""
    from bridge.reply import _id_type

    owner = "ou_realowner"
    b = _bridge(tmp_path)
    b.settings.owner_open_ids = frozenset({owner})
    b.touch = lambda: None
    sent, restore = _capture_dispatch(b)
    try:
        b.on_bot_menu(_menu_event("/sessions", owner))
    finally:
        restore()
    assert sent == ["/sessions", owner]
    assert _id_type(sent[1]) == "open_id"


def test_bot_menu_click_from_stranger_is_dropped(tmp_path):
    b = _bridge(tmp_path)
    b.touch = lambda: None
    sent, restore = _capture_dispatch(b)
    try:
        b.on_bot_menu(_menu_event("/sessions", "ou_stranger"))
    finally:
        restore()
    assert sent == []


def test_bot_menu_unknown_key_is_dropped(tmp_path):
    b = _bridge(tmp_path)
    b.touch = lambda: None
    sent, restore = _capture_dispatch(b)
    try:
        b.on_bot_menu(_menu_event("rm -rf /"))
    finally:
        restore()
    assert sent == []
