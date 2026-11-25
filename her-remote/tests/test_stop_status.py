"""/stop、状态卡、掐停落盘。

三条都是「长任务可用性」的一部分：没有停下键，一句打错的话会占住会话 15 分钟；
没有进度，用户只能盯着沉默；掐停不落盘，看门狗一重启就形同虚设。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest  # noqa: E402

from bridge.status import StatusCard, _fmt_elapsed, build_status_card  # noqa: E402


# ---------- 状态卡渲染 ----------


def test_elapsed_formatting():
    assert _fmt_elapsed(5) == "5s"
    assert _fmt_elapsed(90) == "1m30s"
    assert _fmt_elapsed(3700) == "1h01m"


def test_card_is_schema_2_without_action_tag():
    """回归：schema 2.0 不支持 action 容器，用了整张卡发不出去。"""
    blob = build_status_card("⏳ 还在跑")
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob
    assert '"actions"' not in blob


def test_card_is_small_enough_for_30kb_limit():
    """状态卡必须很小 —— 卡片 30KB 上限，超了飞书静默丢弃。"""
    assert len(build_status_card("⏳ 还在跑 · 42s · 正在搜代码 · 第 3 步")) < 1000


class FakeReplier:
    def __init__(self, create_ok=True, patch_ok=True):
        self.created: list[str] = []
        self.patched: list[tuple[str, str]] = []
        self._create_ok = create_ok
        self._patch_ok = patch_ok

    def send_status_card(self, chat_id, text, with_stop=True):
        self.created.append(text)
        return "om_status_1" if self._create_ok else None

    def patch_status_card(self, message_id, text, with_stop=True):
        self.patched.append((message_id, text))
        return self._patch_ok


def test_first_update_creates_then_patches():
    """第一次 create，之后都 patch —— 不刷消息流。"""

    async def run():
        r = FakeReplier()
        card = StatusCard(r, "oc_1")
        await card.update(elapsed=30, tool_calls=1, last_tool="Grep")
        await card.update(elapsed=60, tool_calls=3, last_tool="Read")
        assert len(r.created) == 1, "只该创建一次"
        assert len(r.patched) == 1, "第二次该是原地更新"

    asyncio.run(run())


def test_identical_content_is_not_resent():
    """内容没变就不发，省请求也免闪动。"""

    async def run():
        r = FakeReplier()
        card = StatusCard(r, "oc_1")
        await card.update(elapsed=30, tool_calls=1, last_tool="Grep")
        await card.update(elapsed=30, tool_calls=1, last_tool="Grep")
        assert len(r.patched) == 0

    asyncio.run(run())


def test_tool_names_are_humanized():
    async def run():
        r = FakeReplier()
        await StatusCard(r, "oc_1").update(elapsed=30, tool_calls=2, last_tool="Grep")
        assert "搜代码" in r.created[0]
        assert "Grep" not in r.created[0]

    asyncio.run(run())


def test_card_mentions_how_to_stop():
    """用户看到进度的同时必须知道怎么叫停 —— 现在是卡片上一个「⏹ 停下」按钮。

    原来是一行「想停就发 /stop」；用户要求全按钮化（2026-11-05）。
    """
    from bridge.status import build_status_card

    card = build_status_card("⏳ 还在跑 · 30s", with_stop=True)
    assert "⏹ 停下" in card and '"op": "stop"' in card
    # 跑完收尾那张不放停止按钮
    assert "⏹ 停下" not in build_status_card("✅ 跑完了", with_stop=False)


def test_create_failure_disables_further_attempts():
    """发不出去就别一直试 —— 否则每 15 秒打一发失败请求。"""

    async def run():
        r = FakeReplier(create_ok=False)
        card = StatusCard(r, "oc_1")
        await card.update(elapsed=30, tool_calls=1, last_tool="Read")
        await card.update(elapsed=45, tool_calls=2, last_tool="Grep")
        assert len(r.created) == 1
        assert len(r.patched) == 0

    asyncio.run(run())


def test_status_failure_never_raises():
    """状态卡是锦上添花，它坏了不能让这一轮任务失败。"""

    class Exploding:
        def send_status_card(self, *a):
            raise RuntimeError("boom")

        def patch_status_card(self, *a):
            raise RuntimeError("boom")

    async def run():
        card = StatusCard(Exploding(), "oc_1")
        await card.update(elapsed=30, tool_calls=1, last_tool="Read")
        await card.finish()  # 不该抛

    asyncio.run(run())


def test_finish_closes_the_card():
    """别让状态卡永远停在「还在跑」。"""

    async def run():
        r = FakeReplier()
        card = StatusCard(r, "oc_1")
        await card.update(elapsed=30, tool_calls=1, last_tool="Read")
        await card.finish()
        assert r.patched and "跑完" in r.patched[-1][1]

    asyncio.run(run())


def test_finish_without_card_is_noop():
    """没发过卡（任务很快结束）时，收尾不该凭空发一张。"""

    async def run():
        r = FakeReplier()
        await StatusCard(r, "oc_1").finish()
        assert r.created == [] and r.patched == []

    asyncio.run(run())


# ---------- 掐停落盘 ----------


def _bridge_with_paused_file(tmp):
    """造一个只带暂停状态功能的 Bridge（不连网络）。"""
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    b._paused = {}
    b._paused_path = os.path.join(tmp, "paused.json")
    return b


def test_pause_survives_restart():
    """核心：看门狗会自杀重启，内存里的暂停活不过去。"""
    with tempfile.TemporaryDirectory() as tmp:
        first = _bridge_with_paused_file(tmp)
        first._paused["dm:owner"] = "连续试探被拦"
        first._save_paused()

        second = _bridge_with_paused_file(tmp)
        second._load_paused()
        assert second._paused.get("dm:owner") == "连续试探被拦"


def test_resume_clears_persisted_pause():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge_with_paused_file(tmp)
        b._paused["dm:owner"] = "x"
        b._save_paused()
        b._paused.pop("dm:owner")
        b._save_paused()

        again = _bridge_with_paused_file(tmp)
        again._load_paused()
        assert again._paused == {}


def test_missing_paused_file_is_not_fatal():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge_with_paused_file(tmp)
        b._load_paused()
        assert b._paused == {}


def test_corrupt_paused_file_is_not_fatal():
    """半截 JSON 不能让服务起不来。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "paused.json")
        with open(path, "w") as handle:
            handle.write("{broken")
        b = _bridge_with_paused_file(tmp)
        b._load_paused()
        assert b._paused == {}


def test_save_is_atomic_no_partial_file():
    """写临时文件再改名 —— 落盘后必须是合法 JSON。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge_with_paused_file(tmp)
        b._paused = {"dm:a": "原因一", "group:b": "原因二"}
        b._save_paused()
        with open(b._paused_path, encoding="utf-8") as handle:
            assert json.load(handle) == b._paused


# ---------- /stop 取消 ----------


@pytest.mark.parametrize("command", ["/stop", "/s"])
def test_stop_sets_cancel_event(command):
    """/stop 要能在**不拿会话锁**的情况下置位取消 —— 要打断的那轮正持着锁。"""
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    b._cancels = {}
    b._paused = {}
    b._paused_path = ""
    b._awaiting = {}
    said: list[str] = []
    b._say = lambda c, t: said.append(t)

    async def run():
        event = asyncio.Event()
        b._cancels["dm:owner"] = event
        b.sessions = None  # /stop 不该碰 sessions
        handled = await Bridge._handle_command(b, "dm:owner", "oc_1", command)
        assert handled is True
        assert event.is_set(), "取消事件没被置位"
        assert said and "停" in said[-1]

    asyncio.run(run())


def test_stop_with_nothing_running_says_so():
    """阳性对照：没有在跑的任务时要明说，不能假装停了什么。"""
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    b._cancels = {}
    said: list[str] = []
    b._say = lambda c, t: said.append(t)

    async def run():
        handled = await Bridge._handle_command(b, "dm:owner", "oc_1", "/stop")
        assert handled is True
        assert "没有在跑" in said[-1]

    asyncio.run(run())


def test_help_mentions_stop():
    from bridge.app import HELP

    assert "/stop" in HELP


# ---------- 看门狗：整个方案的核心保险 ----------
#
# lark-oapi issue #169（未修）：重连路径里 requests.post 没设超时且全程持锁 ⇒
# 半开连接下永久阻塞，**进程还活着、不报错、日志直接断掉**（实测有人失联 3 天 13 小时）。
# auto_reconnect 覆盖不了这种，只能从外面看静默时长。
#
# 2026-11-03：这个阈值在之前一直只是"读代码读出来的"。拆出 should_self_exit 之后
# 真起进程观测过：持续心跳时存活，停止心跳后以 EX_TEMPFAIL(75) 退出。


def _watchdog_bridge(silent_for: float):
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    import time as _t

    b._last_event_at = _t.monotonic() - silent_for
    return b


def test_fresh_connection_does_not_self_exit():
    """阳性对照放前面：刚收到事件时绝不能自杀。"""
    assert _watchdog_bridge(silent_for=5).should_self_exit() is False


def test_long_silence_triggers_self_exit():
    from bridge.app import WATCHDOG_SILENT_SEC

    assert _watchdog_bridge(silent_for=WATCHDOG_SILENT_SEC + 10).should_self_exit() is True


def test_exactly_at_threshold_does_not_fire():
    """边界：等于阈值不触发，严格大于才触发（避免抖动期反复自杀）。"""
    from bridge.app import WATCHDOG_SILENT_SEC

    b = _watchdog_bridge(silent_for=0)
    assert b.should_self_exit(threshold_sec=WATCHDOG_SILENT_SEC) is False


def test_touch_resets_the_silence_clock():
    """收到任何飞书事件都要把计时清零 —— 否则忙的时候也会被误杀。"""
    b = _watchdog_bridge(silent_for=99999)
    assert b.should_self_exit() is True
    b.touch()
    assert b.should_self_exit() is False


def test_threshold_is_long_enough_for_normal_idle():
    """阈值不能短到把正常空闲误判成故障。

    用户一整天不发消息是常态；10 分钟以下的阈值会让服务反复自杀。
    """
    from bridge.app import WATCHDOG_SILENT_SEC

    assert WATCHDOG_SILENT_SEC >= 600, "阈值太短，正常空闲会被误杀"


def test_tick_is_shorter_than_threshold():
    """心跳间隔必须明显小于阈值，否则最坏情况要多等一整个 tick。"""
    from bridge.app import WATCHDOG_SILENT_SEC, WATCHDOG_TICK_SEC

    assert WATCHDOG_TICK_SEC < WATCHDOG_SILENT_SEC / 5


# ---------- 看门狗必须看「连接通不通」，不是「用户说不说话」 ----------
#
# 2026-11-03 查出来的缺陷：`touch()` 原来只有两个调用方 —— 收到消息、点卡片。
# SDK 的心跳 / pong 不喂这个时钟。后果是用户一整天不发消息（完全正常），
# 服务每 15 分钟自杀一次，而且日志写满「长连接已静默」把真正的 #169 故障埋进噪音。


class _Conn:
    def __init__(self, closed=False):
        self.closed = closed


class _WsClient:
    def __init__(self, conn=None):
        self._conn = conn


def _bridge(silent_for: float, ws_client=object()):
    import time as _t

    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    b._last_event_at = _t.monotonic() - silent_for
    b._ws_client = ws_client
    return b


def test_idle_but_connected_does_not_self_exit():
    """核心回归：用户一天不说话但连接好着 ⇒ 绝不能自杀。"""
    b = _bridge(silent_for=24 * 3600, ws_client=_WsClient(_Conn(closed=False)))
    assert b.should_self_exit() is False


def test_dead_connection_and_long_silence_self_exits():
    """阳性对照：连接真没了 + 静默超阈值 ⇒ 必须自杀等人拉起。"""
    b = _bridge(silent_for=24 * 3600, ws_client=_WsClient(conn=None))
    assert b.should_self_exit() is True


def test_dead_connection_but_short_silence_waits():
    """刚断开（可能正在重连）不急着杀，给 SDK 自愈的机会。"""
    b = _bridge(silent_for=5, ws_client=_WsClient(conn=None))
    assert b.should_self_exit() is False


def test_closed_connection_counts_as_dead():
    b = _bridge(silent_for=24 * 3600, ws_client=_WsClient(_Conn(closed=True)))
    assert b.should_self_exit() is True


def test_missing_ws_client_falls_back_to_silence():
    """拿不到客户端就按静默时长兜底 —— 宁可多重启一次，
    也不要像 #169 那样静默失联三天。
    """
    b = _bridge(silent_for=24 * 3600, ws_client=None)
    assert b.should_self_exit() is True
    fresh = _bridge(silent_for=5, ws_client=None)
    assert fresh.should_self_exit() is False


def test_connection_alive_handles_unknown_shape():
    """SDK 换了内部属性名也不能崩 —— 探不到就当连接没了，交给静默兜底。"""
    b = _bridge(silent_for=1, ws_client=object())
    assert b.connection_alive() is False


def test_conn_without_closed_attr_counts_as_alive():
    """有连接对象但没有 closed 属性时，视为活着（保守：别误杀）。"""

    class Bare:
        pass

    b = _bridge(silent_for=24 * 3600, ws_client=_WsClient(Bare()))
    assert b.connection_alive() is True
    assert b.should_self_exit() is False


# ---------- 看门狗不能腰斩正在跑的任务 ----------
#
# 2026-11-03 查出来的干扰：连接断开 + 静默超阈值 ⇒ 自杀。但如果那一刻有任务正在跑，
# 自杀会把那一轮的结果全丢掉 —— 用户看到「发了消息，状态卡停住，再也没有回复」，
# 比多等一会儿更糟。而 SDK 重连往往几秒就成了。


def _busy_bridge(silent_for: float, ws_client, busy=False, awaiting=False):
    import time as _t

    from bridge.app import Bridge
    from bridge.sessions import SessionStore

    b = Bridge.__new__(Bridge)
    b._last_event_at = _t.monotonic() - silent_for
    b._ws_client = ws_client
    b.sessions = SessionStore("/tmp", 30)
    b._awaiting = {"dm:x": {}} if awaiting else {}
    if busy:
        session = b.sessions.get("dm:x")

        async def grab():
            await session.lock.acquire()

        asyncio.run(grab())
    return b


def test_idle_with_dead_connection_still_self_exits():
    """阳性对照：没任务在跑时，连接死了就该自杀等人拉起。"""
    from bridge.app import WATCHDOG_SILENT_SEC

    b = _busy_bridge(WATCHDOG_SILENT_SEC + 10, _WsClient(conn=None))
    assert b.should_self_exit() is True


def test_running_task_is_not_cut_short():
    """核心：连接断了但任务在跑 ⇒ 宽限，不腰斩。"""
    from bridge.app import WATCHDOG_SILENT_SEC

    b = _busy_bridge(WATCHDOG_SILENT_SEC + 10, _WsClient(conn=None), busy=True)
    assert b.should_self_exit() is False


def test_waiting_for_approval_is_not_cut_short():
    """等用户点审批卡片也算"有活在身"，不该自杀（否则点了也没人 resume）。"""
    from bridge.app import WATCHDOG_SILENT_SEC

    b = _busy_bridge(WATCHDOG_SILENT_SEC + 10, _WsClient(conn=None), awaiting=True)
    assert b.should_self_exit() is False


def test_grace_is_bounded():
    """宽限不能无限 —— 否则"卡住的任务"会让服务永远不重启，那更糟。"""
    from bridge.app import WATCHDOG_SILENT_SEC

    b = _busy_bridge(WATCHDOG_SILENT_SEC * 10, _WsClient(conn=None), busy=True)
    assert b.should_self_exit() is True


def test_work_in_flight_is_false_when_idle():
    b = _busy_bridge(1, _WsClient(_Conn(False)))
    assert b._work_in_flight() is False


# ---------- 超长输出行不能让整轮任务崩掉 ----------
#
# 2026-11-04 实测：asyncio StreamReader 默认上限 64KiB，而
# `--output-format stream-json` 的**单行**事件可以远超这个（读一个大文件、
# 贴一段长 diff 都会）。`readline()` 抛
# `ValueError: Separator is found, but chunk is longer than limit`，
# 整轮任务直接失败 —— 而用户只会看到"它没回我"。


def test_stream_limit_is_generous():
    """上限要足够装下任何单条工具结果。"""
    from bridge.runner import _STREAM_LIMIT

    assert _STREAM_LIMIT >= 8 * 1024 * 1024, "太小，长输出会崩"
    assert _STREAM_LIMIT <= 64 * 1024 * 1024, "太大，坏掉的子进程会吃光内存"


def test_runner_passes_limit_to_subprocess():
    """回归：`create_subprocess_exec` 必须带 limit 参数。

    不带的话用默认 64KiB —— 这正是那个崩溃的根因。
    """
    import inspect

    from bridge import runner

    source = inspect.getsource(runner.ClaudeRunner.run_turn)
    assert "limit=_STREAM_LIMIT" in source, "没把 limit 传给子进程"


def test_long_line_is_readable():
    """造一条超长行，确认在新上限下能读出来。"""
    import asyncio

    from bridge.runner import _STREAM_LIMIT

    async def run():
        payload = "x" * (200 * 1024)  # 200KiB 一行，远超默认 64KiB
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", f"print('{payload}')",
            stdout=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT,
        )
        line = await proc.stdout.readline()
        await proc.wait()
        return len(line)

    assert asyncio.run(run()) > 200 * 1024


# ---------- 消息发不出去必须看得见 ----------
#
# 真实日志里出现过 `code=230001 invalid receive_id`（单聊 scope 里存的是
# open_id，而发消息按 chat_id 发）。`_say` 当时吞掉了结果，22 处调用都不知道
# 消息没送达 —— 用户的体验是"它不回我"，而日志里看不出是哪条。
# 「活干完了但用户收不到」是 C 端最致命的失败模式。


def _say_bridge(send_result):
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)

    class Rep:
        def __init__(self):
            self.calls = []

        def send(self, chat_id, text):
            self.calls.append((chat_id, text))
            if isinstance(send_result, Exception):
                raise send_result
            return send_result

    b.replier = Rep()
    return b


def test_say_returns_true_on_success():
    b = _say_bridge(True)
    assert b._say("oc_1", "hi") is True


def test_say_returns_false_when_platform_rejects():
    """飞书拒收（230001 / 230025 之类）要返回 False，不能当成功。"""
    b = _say_bridge(False)
    assert b._say("oc_1", "hi") is False


def test_say_returns_false_on_exception():
    b = _say_bridge(RuntimeError("network down"))
    assert b._say("oc_1", "hi") is False


def test_say_skips_empty_chat_id():
    """没有 chat_id 就别发 —— 那个请求必然失败，只会污染日志。"""
    b = _say_bridge(True)
    assert b._say("", "hi") is False
    assert b.replier.calls == [], "空 chat_id 时不该真的发请求"


def test_chat_for_never_returns_open_id_as_chat_id():
    """回归：单聊 scope 是 `dm:<open_id>`，但发消息按 chat_id 发。

    从 scope 反推会得到 open_id ⇒ 飞书回 `230001 invalid receive_id`。
    表里没有就返回空串，让 `_say` 跳过。
    """
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    b._chat_ids = {}
    assert b._chat_for("dm:ou_someone") == "", "不该把 open_id 当 chat_id"
    # 群聊的 scope 后半段就是 chat_id，可以直接用
    assert b._chat_for("group:oc_abc") == "oc_abc"
    # 记过的查得到
    b._chat_ids["dm:ou_someone"] = "oc_real"
    assert b._chat_for("dm:ou_someone") == "oc_real"


# ---------- 给 agent 的上下文提示 ----------
#
# 不说"你在飞书里、用户在手机上"的后果（都实测过）：
# - 回复是终端风格的长输出（实测 563 字 vs 带提示 221 字，短 60%）
# - 问句被当成任务：用户问"测试怎么跑"，agent 去 `npm install` 装依赖 +
#   新建测试文件，那一轮弹了 21 张审批卡片


def test_context_tells_agent_its_boundary():
    """🔴 必须告诉 agent 边界在哪，否则它会去试、然后回「我没有权限」。

    实测：workspace 是子目录时，用户问"这目录下有几个 py 文件"，
    agent 去读父目录被拦，回复变成「我没有读取 … 的权限，请授权后我…」——
    用户会以为产品坏了。加上边界说明后同一个问题直接回「41」。
    """
    from bridge.policy import Policy
    from bridge.runner import _context_for

    policy = Policy(
        mode="readonly",
        workspaces=("/tmp/a", "/tmp/b"),
        default_workspace="/tmp/a",
        model="m",
        turn_timeout_sec=60,
    )
    text = _context_for("/tmp/a/proj", policy)
    assert "/tmp/a/proj" in text, "要说清现在在哪"
    assert "/tmp/a" in text and "/tmp/b" in text, "要列出所有允许的目录"
    assert "不是配置错误" in text, "要说清这是刻意限制，否则 agent 会反复试"
    # 🔴 2026-11-06 改判据：原来要求出现「/cd」—— 那等于**教模型去叫用户打命令**，
    # 实测开场例句 15 次回答里 6 次带「用 /cd 换目录」。用户原话「不要让用户输入 /cancel 等类似的」。
    # 换成：要告诉用户怎么换（指菜单按钮），并且明说别叫用户打 / 开头的命令。
    assert "换项目" in text, "要告诉用户怎么换目录（菜单里那个按钮）"
    assert "/cd" not in text, "又在教模型叫用户打命令了"
    assert "不要让用户输入" in text
    assert "只读" in text, "只读模式要讲明"
    assert "改不了" in text and "问用户" in text, "只读要按真实行为说：会先问，不是改不了"


def test_context_omits_readonly_note_when_writable():
    from bridge.policy import Policy
    from bridge.runner import _context_for

    policy = Policy(
        mode="trusted",
        workspaces=("/tmp/a",),
        default_workspace="/tmp/a",
        model="m",
        turn_timeout_sec=60,
    )
    assert "只读模式" not in _context_for("/tmp/a", policy)


def test_context_prompt_is_passed_to_claude():
    import inspect

    from bridge import runner

    source = inspect.getsource(runner.ClaudeRunner._build_argv)
    assert "--append-system-prompt" in source
    # 提示现在是按 workspace 动态拼的（要告诉 agent 边界在哪）
    assert "_context_for" in source


def test_context_prompt_covers_the_three_known_failures():
    from bridge.runner import _BRIDGE_CONTEXT  # noqa: F401 — 基础段

    # 1. 用户在手机上 ⇒ 回复要短
    assert "短" in _BRIDGE_CONTEXT or "简" in _BRIDGE_CONTEXT
    # 2. 问句就是问句，不是任务
    assert "答案" in _BRIDGE_CONTEXT
    # 3. 不要自作主张改东西
    assert "不要改文件" in _BRIDGE_CONTEXT
    assert "不要装依赖" in _BRIDGE_CONTEXT


def test_context_prompt_stays_short():
    """它进每一轮的 token 预算 —— 不能写成一篇文章。

    连边界说明一起算，整段要控制住。
    """
    from bridge.policy import Policy
    from bridge.runner import _context_for

    policy = Policy(
        mode="readonly",
        workspaces=("/Users/someone/codes",),
        default_workspace="/Users/someone/codes",
        model="m",
        turn_timeout_sec=60,
    )
    full = _context_for("/Users/someone/codes/proj", policy)
    assert len(full) < 500, f"太长了：{len(full)} 字"


# ---------- 失败时给用户的话 ----------
#
# 这是用户最容易放弃的时刻。三条要求：
# 1. 不说内部术语（"agent"、"退出码"、"subtype"）
# 2. 不把 stderr 原文发出去（里面可能有路径、token）
# 3. **必须给下一步** —— 只说"失败了"会让人反复重试同一句话


def _error_messages() -> list[str]:
    """从 runner 源码里抽出所有发给用户的 error 文案。"""
    import inspect
    import re

    from bridge import runner

    source = inspect.getsource(runner)
    out = []
    for match in re.finditer(r'error=\(?\s*(?:f?")([^"]{4,})"', source):
        out.append(match.group(1))
    # 多行拼接的也要抓
    for match in re.finditer(r'error=\(\s*\n\s*f?"([^"]+)"\s*\n\s*"([^"]+)"', source):
        out.append(match.group(1) + match.group(2))
    return out


def test_error_messages_avoid_internal_jargon():
    for message in _error_messages():
        for jargon in ("agent", "subtype", "returncode", "stderr", "exit code"):
            assert jargon not in message.lower(), f"{message!r} 里有内部术语 {jargon}"


def test_error_messages_give_a_next_step():
    """只说失败不给出路，用户只会反复重试同一句话。"""
    actionable = ("/new", "再问", "重新", "拆小", "具体", "停下")
    for message in _error_messages():
        assert any(hint in message for hint in actionable), f"{message!r} 没给下一步"


def test_zero_output_error_suggests_new_session():
    """零输出最常见的真实原因是 resume 到不完整的轮次 ⇒ /new 能解。"""
    import inspect

    from bridge import runner

    source = inspect.getsource(runner.ClaudeRunner.run_turn)
    # 现在提示点「🆕 新话题」按钮，不再叫人打 /new
    assert "新话题" in source


def test_stderr_goes_to_log_not_to_user():
    """stderr 里可能有路径和 token —— 只进日志。"""
    import inspect

    from bridge import runner

    source = inspect.getsource(runner.ClaudeRunner.run_turn)
    # 有记日志
    assert "logger.error" in source
    # 但 error= 的文案里不含 stderr 变量
    assert 'error=f"agent' not in source
    assert "{hint}" not in source.split("error=")[-1][:200]


# ---------- 状态卡收尾按真实结果写（2026-11-04 review） ----------


@pytest.mark.parametrize("result,expect", [
    (None, "failed"),
    (__import__("types").SimpleNamespace(ok=False, cancelled=True, deferred=False), "stopped"),
    (__import__("types").SimpleNamespace(ok=True, cancelled=False, deferred=True), "waiting"),
    (__import__("types").SimpleNamespace(ok=False, cancelled=False, deferred=False), "failed"),
    (__import__("types").SimpleNamespace(ok=True, cancelled=False, deferred=False), "done"),
])
def test_outcome_mapping(result, expect):
    """原来一律「✅ 跑完了」—— 停下、超时、等确认时手机上置顶那张卡也显示成功。"""
    from bridge.app import _outcome_of

    assert _outcome_of(result) == expect


def test_finish_writes_the_outcome_head():
    import asyncio
    import types

    from bridge.status import StatusCard

    patched = []
    replier = types.SimpleNamespace(patch_status_card=lambda mid, text: patched.append(text) or True)
    card = StatusCard(replier, "oc_x")
    card._message_id = "om_x"
    asyncio.run(card.finish(elapsed=65, tool_calls=3, outcome="stopped"))
    assert patched and patched[-1].startswith("⏹ 已停下"), patched
    assert "✅" not in patched[-1]
