"""改动之间的**互相干扰**审计（2026-11-03 一天塞进去十几处改动之后补的）。

每处改动单独都有测试、单独都是绿的。这个文件只问一件事：**合在一起有没有互相拆台。**

每条测试的 docstring 都标了「实测」还是「读码推断」：
  - 实测 = 真跑过 claude / 真起过进程 / 真落过盘，有可复现的命令
  - 读码推断 = 只读了代码路径，没有现场证据

标 `🔴 RED` 的是**当前实现有问题**、故意让它红的用例。不要改弱断言凑绿 ——
要么修源码，要么把结论写进 docstring 再改。

真起 claude 的用例默认跳过（慢 + 花钱）：

    CARHER_LIVE_GATE=1 ./.venv/bin/python -m pytest tests/test_interference.py -v
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import pytest  # noqa: E402

from bridge.app import Bridge  # noqa: E402
from bridge.approval import ApprovalGate  # noqa: E402
from bridge.cmdclass import classify  # noqa: E402
from bridge.policy import Policy, readonly_command_prefixes  # noqa: E402
from bridge.review import ApprovalStore  # noqa: E402
from bridge.sessions import SessionStore  # noqa: E402

LIVE = os.environ.get("CARHER_LIVE_GATE") == "1"
live_only = pytest.mark.skipif(
    not LIVE, reason="要真打 claude，设 CARHER_LIVE_GATE=1 才跑"
)

MODEL = "claude-haiku-5-5"
OWNER = "ou_owner"
SCOPE = "dm:owner"


# ---------- 公用构造（全部在 /tmp，绝不碰 ~/.carher-feishu-agent 和 ~/.claude*）----------


def _fake_bridge(tmp: str, owners=(OWNER,)):
    """造一个不碰网络、不碰用户目录的 Bridge。

    只装审批/掐停/取消这几条路径需要的字段 —— 照 tests/test_auth.py 的做法，
    用真实对象而不是 MagicMock，免得 DENY 用例因为桩不完整而假通过。
    """
    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(
        owner_open_ids=frozenset(owners),
        policy=Policy(
            mode="readonly",
            workspaces=("/tmp",),
            default_workspace="/tmp",
            model=MODEL,
            turn_timeout_sec=60,
        ),
        custom_rules=(),
    )
    b.sessions = SessionStore("/tmp", 30)
    b.gate = ApprovalGate(os.path.join(tmp, "approvals"))
    b.approvals = ApprovalStore(b"k" * 32)
    b._awaiting = {}
    b._cancels = {}
    b._paused = {}
    b._paused_path = os.path.join(tmp, "paused.json")
    b._ws_client = None
    b._last_event_at = time.monotonic()
    b._loop = object()  # 非 None，让 on_card_action 走到派发那步
    b.said = []
    b._say = lambda chat_id, text: b.said.append(text)
    b.replier = types.SimpleNamespace(
        send=lambda chat_id, text: b.said.append(text) or True
    )
    return b


def _patch_dispatch(bridge):
    """换掉 run_coroutine_threadsafe，只记录「本来会不会续跑」。"""
    import bridge.app as app_module

    original = app_module.asyncio.run_coroutine_threadsafe
    scheduled: list = []

    def fake(coro, loop):
        scheduled.append(coro)
        coro.close()  # 不真跑，避免 warning
        return None

    app_module.asyncio.run_coroutine_threadsafe = fake

    def restore():
        app_module.asyncio.run_coroutine_threadsafe = original

    return scheduled, restore


def _put_pending(bridge, scope: str, digest: str, command: str) -> None:
    """模拟钩子落下一条待办（钩子是独立进程，它就是这么写的）。"""
    directory = bridge.gate.dir_for(scope)
    with open(os.path.join(directory, digest + ".pending"), "w") as handle:
        json.dump(
            {"tool_name": "Bash", "tool_input": {"command": command}}, handle
        )


def _suffixes(bridge, scope: str) -> list[str]:
    directory = bridge.gate.dir_for(scope)
    return sorted(
        name
        for name in os.listdir(directory)
        if name.endswith((".pending", ".asked", ".ok"))
    )


def _click(bridge, token, act="approve", clicker=OWNER):
    """造一次飞书卡片回调。"""
    event = types.SimpleNamespace(
        event=types.SimpleNamespace(
            operator=types.SimpleNamespace(open_id=clicker),
            action=types.SimpleNamespace(value={"act": act, **token.to_value()}),
        )
    )
    return bridge.on_card_action(event)


def _toast_text(response) -> str:
    """从飞书回调响应里把 toast 文案抠出来（SDK 包了一层对象）。"""
    blob = json.dumps(getattr(response, "__dict__", {}), default=str, ensure_ascii=False)
    toast = getattr(response, "toast", None)
    for attr in ("content", "_content"):
        text = getattr(toast, attr, None)
        if text:
            return str(text)
    return blob


# ======================================================================
# 干扰 1：改动 4（继承用户网关配置）× 改动 2（自己写 hooks.PreToolUse）
# ======================================================================
#
# install_hook 一边从 ~/.claude/settings.json 继承认证键，一边写自己的审批钩子。
# 问三件事：用户的 hooks 会不会被带进来打架？permissions 会不会放宽闸门？
# env 里会不会残留 CARHER_APPROVAL_DIR（注释说那会让所有会话共用批准目录）？


def _install_with_user_settings(tmp: str, user_settings: dict, bridge_settings=None):
    """在隔离目录里跑一次 install_hook，把「用户主配置」换成我们造的。

    绝不读真实 ~/.claude/settings.json —— 那是用户正在用的文件。
    """
    import bridge.approval as approval_module

    user_path = os.path.join(tmp, "user_settings.json")
    with open(user_path, "w", encoding="utf-8") as handle:
        json.dump(user_settings, handle)

    settings_path = os.path.join(tmp, "bridge_cc", "settings.json")
    if bridge_settings is not None:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        with open(settings_path, "w", encoding="utf-8") as handle:
            json.dump(bridge_settings, handle)

    original = approval_module.user_settings_path
    approval_module.user_settings_path = lambda: user_path
    try:
        approval_module.install_hook(
            os.path.join(tmp, "hooks", "approval_hook.py"), settings_path
        )
    finally:
        approval_module.user_settings_path = original

    with open(settings_path, encoding="utf-8") as handle:
        return json.load(handle)


_HOSTILE_USER_SETTINGS = {
    "env": {
        "ANTHROPIC_BASE_URL": "https://gw.example/x",
        "ANTHROPIC_AUTH_TOKEN": "tok",
        # 用户历史上可能自己配过（或被旧版桥写过）—— 继承进来等于所有会话共用目录
        "CARHER_APPROVAL_DIR": "/tmp/interference-shared-approvals",
    },
    "hooks": {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "echo user-own-hook"}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "Bash",
                "hooks": [{"type": "command", "command": "echo user-post-hook"}],
            }
        ],
    },
    "permissions": {"allow": ["Bash"], "defaultMode": "bypassPermissions"},
    "apiKeyHelper": "/bin/echo key",
    "model": "opus",
    "statusLine": {"type": "command", "command": "x"},
}


def test_inherited_auth_actually_lands():
    """阳性对照放最前：继承必须真的发生。

    这条过不了，下面所有「没被继承」的断言都可能只是因为 install_hook 没跑到。
    实测（本测试即实测）。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        out = _install_with_user_settings(tmp, _HOSTILE_USER_SETTINGS)
        assert out["env"]["ANTHROPIC_BASE_URL"] == "https://gw.example/x"
        assert out["env"]["ANTHROPIC_AUTH_TOKEN"] == "tok"
        assert out["apiKeyHelper"] == "/bin/echo key"


def test_user_pretooluse_hook_is_not_inherited():
    """用户自己的 PreToolUse 不能被带进来 —— 两个钩子都返回决定会打架。

    实测：继承后 hooks.PreToolUse 只有一条，就是我们的审批钩子。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        out = _install_with_user_settings(tmp, _HOSTILE_USER_SETTINGS)
        entries = out["hooks"]["PreToolUse"]
        assert len(entries) == 1, f"PreToolUse 有 {len(entries)} 条，用户的钩子被带进来了"
        command = entries[0]["hooks"][0]["command"]
        assert "approval_hook.py" in command
        assert "user-own-hook" not in json.dumps(out, ensure_ascii=False)


def test_user_other_hook_events_are_not_inherited():
    """PostToolUse 之类也不该继承 —— 它们会在桥的会话里跑用户的任意命令。

    实测。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        out = _install_with_user_settings(tmp, _HOSTILE_USER_SETTINGS)
        assert list(out["hooks"].keys()) == ["PreToolUse"], (
            f"继承了多余的 hook 事件：{list(out['hooks'].keys())}"
        )


def test_user_permissions_cannot_loosen_our_gate():
    """用户的 permissions（含 defaultMode=bypassPermissions）绝不能继承。

    继承了就等于把整个闸门拆了 —— policy.py 的红线是「提权只能改 config.toml」。
    实测。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        out = _install_with_user_settings(tmp, _HOSTILE_USER_SETTINGS)
        assert "permissions" not in out
        assert "bypassPermissions" not in json.dumps(out, ensure_ascii=False)


def test_user_approval_dir_never_lands_in_settings():
    """settings.json 的 env **覆盖**进程环境（approval.py 的红字，实测结论）⇒
    CARHER_APPROVAL_DIR 落进去就会盖掉 run_turn 按会话传的值，
    A 会话的批准会让 B 会话的同样操作直接放行。

    实测。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        out = _install_with_user_settings(tmp, _HOSTILE_USER_SETTINGS)
        assert "CARHER_APPROVAL_DIR" not in out.get("env", {})
        assert "interference-shared-approvals" not in json.dumps(out)


def test_legacy_pinned_approval_dir_is_scrubbed_on_upgrade():
    """桥自己的 settings.json 是**上个版本**写的、里面有 CARHER_APPROVAL_DIR ⇒
    升级后必须被清掉，否则这台机器会一直共用批准目录。

    实测：install_hook 里有 env.pop。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        out = _install_with_user_settings(
            tmp,
            {"env": {"ANTHROPIC_AUTH_TOKEN": "tok"}},
            bridge_settings={"env": {"CARHER_APPROVAL_DIR": "/tmp/legacy-shared"}},
        )
        assert "CARHER_APPROVAL_DIR" not in out.get("env", {})


def test_preexisting_bridge_permissions_survive_install_hook():
    """🔴 RED（干扰）：install_hook 清 env 里的 CARHER_APPROVAL_DIR，
    但**不清**桥自己 settings.json 里已有的 `permissions` 和别的 hook 事件。

    继承那条路是干净的（上面几条绿的证明了），但「桥的 settings.json 里本来就有
    permissions」这条路没人管。实测：写进去的 defaultMode=bypassPermissions 原样留着，
    而 Claude Code 的 settings 权限层优先于命令行白名单的宽松方向。

    谁会往里写？目前仓库里没有写入方（grep `claude-feishu-bridge` 只有 runner.py 的
    路径常量）—— 所以这条是**可达性未证实**的潜在干扰：用户手动编辑过那个目录、
    或将来加个安装步骤，就会踩到。install_hook 既然已经在替用户清 env，
    就该用同样的力度清 permissions。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        out = _install_with_user_settings(
            tmp,
            {"env": {"ANTHROPIC_AUTH_TOKEN": "tok"}},
            bridge_settings={
                "permissions": {"defaultMode": "bypassPermissions", "allow": ["Bash"]},
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo stale"}],
                        }
                    ]
                },
            },
        )
        assert "permissions" not in out, (
            "桥自己 settings.json 里的 permissions 没被清掉 ⇒ 闸门可被放宽"
        )
        assert list(out["hooks"].keys()) == ["PreToolUse"], (
            f"遗留的 hook 事件没清：{list(out['hooks'].keys())}"
        )


def test_install_hook_does_not_read_real_user_home():
    """纪律性测试：user_settings_path 必须指向 ~/.claude/settings.json 而不是
    CLAUDE_CONFIG_DIR（桥运行时那个变量已经指向桥自己的目录了）。

    读码推断 + 字符串断言，不真读那个文件。
    """
    from bridge.approval import user_settings_path

    path = user_settings_path()
    assert path.endswith(os.path.join(".claude", "settings.json"))
    assert "feishu" not in path


# ======================================================================
# 干扰 2：改动 9（看门狗先探活连接）× 改动 7（/stop + 状态卡 + 长任务）
# ======================================================================
#
# 问题原话：「一个跑了 20 分钟的长任务期间，看门狗会不会判成静默把进程杀掉？」
#
# touch() 的调用方只有「收到消息」和「点卡片」，所以长任务跑着的时候静默时钟在涨。
# 结论（实测，见下面每条）：**不会被腰斩**，有两道独立的保险：
#   1. connection_alive() —— 连接好着就直接 return False，静默时钟根本不参与判断
#   2. _work_in_flight() + _INFLIGHT_GRACE —— 连接断了也宽限到 阈值×4 = 60 分钟
# 而 runner 自己的 turn_timeout_sec 默认 900s(15min)，**比看门狗先动手** ——
# 真正会腰斩 20 分钟任务的是它，不是看门狗。


def _watchdog_bridge(silent_for: float, conn_alive: bool, busy=False, awaiting=False):
    """造一个只带看门狗字段的 Bridge。"""

    class _Conn:
        closed = False

    class _Ws:
        def __init__(self, conn):
            self._conn = conn

    b = Bridge.__new__(Bridge)
    b._last_event_at = time.monotonic() - silent_for
    b._ws_client = _Ws(_Conn() if conn_alive else None)
    b.sessions = SessionStore("/tmp", 30)
    b._awaiting = {SCOPE: {"chat_id": "oc"}} if awaiting else {}
    if busy:
        session = b.sessions.get(SCOPE)
        asyncio.run(session.lock.acquire())
    return b


def test_20min_task_survives_when_connection_is_alive():
    """**干扰 2 的主结论**：长任务跑 20 分钟、用户期间一句话都没说 ⇒ 不自杀。

    连接好着时 connection_alive() 先 return False，静默时钟完全不参与。
    实测（本测试即实测；探活逻辑同时被 tests/test_stop_status.py 的
    test_idle_but_connected_does_not_self_exit 覆盖）。
    """
    b = _watchdog_bridge(silent_for=20 * 60, conn_alive=True, busy=True)
    assert b.should_self_exit() is False


def test_20min_task_survives_even_if_connection_drops():
    """第二道保险：连接在长任务期间断了，也不腰斩 —— 宽限到 阈值×4。

    SDK 重连往往几秒就成了，这个窗口里动手只会把结果全丢掉。
    实测。
    """
    from bridge.app import _INFLIGHT_GRACE, WATCHDOG_SILENT_SEC

    b = _watchdog_bridge(silent_for=20 * 60, conn_alive=False, busy=True)
    assert b.should_self_exit() is False
    assert 20 * 60 < WATCHDOG_SILENT_SEC * _INFLIGHT_GRACE, "宽限窗口不足 20 分钟"


def test_inflight_grace_covers_the_whole_turn_timeout():
    """宽限窗口必须大于 turn_timeout 上限，否则「跑到超时」的那一轮仍会被腰斩。

    读码推断 + 常量断言：config.example.toml 给的是 900s。
    """
    import tomllib

    from bridge.app import _INFLIGHT_GRACE, WATCHDOG_SILENT_SEC

    with open(os.path.join(_ROOT, "config.example.toml"), "rb") as handle:
        configured = int(tomllib.load(handle)["agent"]["turn_timeout_sec"])
    assert WATCHDOG_SILENT_SEC * _INFLIGHT_GRACE > configured, (
        "看门狗的宽限比一轮的超时还短 ⇒ 跑满超时的任务会被腰斩"
    )


def test_long_task_is_killed_by_turn_timeout_not_the_watchdog():
    """🔴 腰斩长任务的是 runner 的 `turn_timeout_sec`，不是看门狗。

    原来默认 900s（15 分钟）⇒ 20 分钟的活跑不完，这条测试钉住 900 并留话
    「默认超时变了，重新评估」。**2026-11-20 改成 1800s（30 分钟）**：
    用户原话「15 分钟 timeout 这个有点太短了，设成默认 30 分钟吧」。

    **2026-11-22 又改成 3600s（1 小时）**：主人原话「你再改成 1 个小时」。

    按那句留话重新评估过的两件事（这次真撞上了一条）：
    1. 20 分钟的活跑得完（3600 > 1200），余量更大了。
    2. 🔴 **看门狗差点先把桥干掉**：原来 `_INFLIGHT_GRACE = 4` ⇒ 900×4 = 3600s，
       和新的单轮上限 3600s **正好相等、不大于** ⇒ 跑满 1 小时的那一轮会被看门狗
       掐掉（用户看到的是状态卡停住、再也没有回复）。这条测试当场报红，
       所以把倍数提到 6（900×6 = 5400 > 3600，留 30 分钟余量）。
       **这就是这条测试存在的意义** —— 它钉的不是某个数字，是
       「宽限 > 单轮上限」这个方向。以后谁再调任一个数，它还会红一次。
    """
    import tomllib

    from bridge.app import _INFLIGHT_GRACE, DEFAULT_TURN_TIMEOUT_SEC, WATCHDOG_SILENT_SEC

    with open(os.path.join(_ROOT, "config.example.toml"), "rb") as handle:
        configured = int(tomllib.load(handle)["agent"]["turn_timeout_sec"])
    assert configured == 3600, "默认超时变了，重新评估长任务能不能跑完 + 看门狗会不会先杀"
    assert configured == DEFAULT_TURN_TIMEOUT_SEC, "模板和代码兜底不一致（原来就是这么分叉的）"
    assert configured >= 20 * 60, "20 分钟的长任务应该跑得完"
    # 看门狗的容忍必须明显大于单轮上限，否则桥会在活干完之前自杀
    assert WATCHDOG_SILENT_SEC * _INFLIGHT_GRACE > configured, (
        f"看门狗干活时只容忍 {WATCHDOG_SILENT_SEC * _INFLIGHT_GRACE}s 静默，"
        f"却允许一轮跑 {configured}s ⇒ 会在活干完前把桥杀了"
    )


def test_watchdog_still_fires_when_truly_stuck():
    """阳性对照：宽限不能无限 —— 否则一个卡死的任务会让服务永远不重启，
    那正是 lark-oapi #169 那种「静默失联三天」。

    实测。
    """
    from bridge.app import _INFLIGHT_GRACE, WATCHDOG_SILENT_SEC

    b = _watchdog_bridge(
        silent_for=WATCHDOG_SILENT_SEC * (_INFLIGHT_GRACE + 1),
        conn_alive=False,
        busy=True,
    )
    assert b.should_self_exit() is True


def test_waiting_for_approval_counts_as_work_in_flight():
    """等人点审批卡也算有活在身 —— 自杀会把 _awaiting 连同内存里的待办一起丢掉
    （见干扰 7：重启后点旧卡片拿到的是「写批准记录失败」）。

    实测。
    """
    b = _watchdog_bridge(silent_for=20 * 60, conn_alive=False, awaiting=True)
    assert b._work_in_flight() is True
    assert b.should_self_exit() is False


def test_work_in_flight_never_raises_in_watchdog_thread():
    """_work_in_flight 跑在看门狗线程里，抛异常会让这条唯一的保险线程死掉。

    实测：喂一个会在属性访问时爆的 sessions。
    """

    class Exploding:
        @property
        def _sessions(self):
            raise RuntimeError("boom")

    b = Bridge.__new__(Bridge)
    b.sessions = Exploding()
    b._awaiting = {}
    assert b._work_in_flight() is False


def test_touch_is_only_fed_by_inbound_feishu_events():
    """把「长任务期间时钟在涨」这个前提钉死 —— 它是干扰 2 的根因。

    `touch()` 只能由**飞书推进来的事件**喂（收到消息、点了卡片、点了菜单）。
    runner 的事件流**不**喂它 ⇒ 长任务期间时钟一路涨 ⇒ 必须靠连接探活 + 宽限兜。

    🔴 这里查的是**哪些函数在喂**，不是喂的次数。
    原来断言 `count("self.touch()") == 2`，加一个事件处理器（菜单点击）就红 ——
    而那一下其实完全符合这条不变量。数数的断言会在"正确的改动"上报警，
    于是真正该看的那条性质反而被当成噪音绕过去。
    """
    with open(os.path.join(_ROOT, "bridge", "app.py"), encoding="utf-8") as handle:
        lines = handle.readlines()

    # 每个 self.touch() 往上找最近的 def，记下它在谁体内
    feeders = []
    for index, line in enumerate(lines):
        if "self.touch()" not in line:
            continue
        for back in range(index, -1, -1):
            match = re.match(r"    def (\w+)", lines[back])
            if match:
                feeders.append(match.group(1))
                break

    assert feeders, "一个 touch() 调用方都没找到 —— 文件结构变了？"
    # 允许的喂食者：飞书事件入口，以及 touch 自己的定义不算
    inbound = {"on_message", "on_card_action", "on_bot_menu"}
    unexpected = set(feeders) - inbound
    assert not unexpected, (
        f"{sorted(unexpected)} 在喂 touch() —— 只有飞书推进来的事件才能喂。"
        "如果是 runner / ticker / 看门狗里的调用，长任务期间时钟就不再涨，"
        "「静默 15 分钟自杀」这条保险会失效（lark-oapi #169 的唯一兜底）"
    )

    # 反向：runner 那条路绝不能碰它
    source = "".join(lines)
    turn_body = source[source.index("async def _run_one") : source.index("def _ask_approval")]
    assert "self.touch()" not in turn_body, "跑一轮的过程里喂了 touch()"


# ======================================================================
# 干扰 3：改动 7（/stop）× 改动 2（审批 defer）
# ======================================================================
#
# defer 的要点就是**进程退出了**（不占资源、不会超时）。但 /stop 认的是
# `_cancels` 里那个 asyncio.Event，而那个 event 在 run_turn 的 finally 里被 pop 掉了。
# 所以「停在等审批」这个状态下，/stop 什么都停不掉。


def test_stop_while_waiting_for_approval_cancels_it():
    """任务停在等审批时发 /stop ⇒ 那一步要被取消，卡片按钮失效。

    修之前：只回「现在没有在跑的任务」，而且**不清**等待状态 ——
    用户说了停，过一会儿手滑点到那张卡片，那个操作照样执行（实测会写下 .ok）。
    「停」必须停全部。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _fake_bridge(tmp)
        _put_pending(b, SCOPE, "dig00001", "npm publish")
        b.gate.take_pending(SCOPE)  # 发过卡 ⇒ .asked
        b._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "sid-1", "count": 1}

        handled = asyncio.run(Bridge._handle_command(b, SCOPE, "oc_1", "/stop"))
        assert handled is True
        last = b.said[-1][1] if isinstance(b.said[-1], tuple) else b.said[-1]
        assert "取消" in last or "停了" in last, last
        assert SCOPE not in b._awaiting, "/stop 要清掉待续跑的审批"
        # 不崩是底线
        assert SCOPE not in b._cancels


def test_stop_does_not_clear_the_pending_approval():
    """🔴 RED（干扰）：/stop 不清 `_awaiting`、不清磁盘上的 `.asked`。

    后果（实测，下一条测试给出完整链路）：用户以为「我已经停了」，但那张卡片
    **仍然可以点**，点下去会真的批准并 resume 那一步。
    「停下键没真的停下」是改动 7 的全部意义所在。

    /stop 的处理应该和 /new 一样：没有在跑的轮次但有 _awaiting / `.asked` 时，
    把它们一起清掉再回话。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _fake_bridge(tmp)
        _put_pending(b, SCOPE, "dig00001", "npm publish")
        b.gate.take_pending(SCOPE)
        b._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "sid-1", "count": 1}

        asyncio.run(Bridge._handle_command(b, SCOPE, "oc_1", "/stop"))

        assert SCOPE not in b._awaiting, "/stop 之后还留着待续跑的审批"
        assert _suffixes(b, SCOPE) == [], (
            f"/stop 之后磁盘上还留着未决记录：{_suffixes(b, SCOPE)}"
        )


def test_approval_card_still_works_after_stop():
    """🔴 RED（干扰 3 的真实后果）：发完 /stop 之后点那张卡 ⇒ 仍然批准并续跑。

    实测链路：
      1. 任务 defer，发了卡片，_awaiting 记上
      2. 用户发 /stop，被告知「现在没有在跑的任务」
      3. 用户点「同意，去做」⇒ gate.approve 写下 .ok，并调度 _continue_after_approval

    用户的心智模型是「我已经停了」，实际那一步照样会执行。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _fake_bridge(tmp)
        scheduled, restore = _patch_dispatch(b)
        try:
            _put_pending(b, SCOPE, "dig00001", "npm publish")
            b.gate.take_pending(SCOPE)
            token = b.approvals.issue(
                SCOPE, "dig00001", {"digest": "dig00001", "scope": SCOPE}
            )
            b._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "sid", "count": 1}

            asyncio.run(Bridge._handle_command(b, SCOPE, "oc_1", "/stop"))
            _click(b, token, act="approve")

            granted = [n for n in _suffixes(b, SCOPE) if n.endswith(".ok")]
            assert granted == [], f"/stop 之后点卡片仍然写下了批准：{granted}"
            assert scheduled == [], "/stop 之后点卡片仍然调度了续跑"
        finally:
            restore()


def test_approving_while_the_turn_is_still_running_does_not_double_run():
    """反方向：任务**正在跑**的时候用户先点了同意会怎样？

    实测：`on_card_action` 用的是 `_awaiting.get`（不是 pop），pop 发生在
    `_continue_after_approval` 里。调度出来的那个协程去抢 `session.lock`，
    锁被正在跑的那一轮持着 ⇒ 它**排队等**，不会并发。
    这是对的（同一会话串行是 sessions.py 的设计），这条测试把它钉住。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _fake_bridge(tmp)
        scheduled, restore = _patch_dispatch(b)
        try:
            _put_pending(b, SCOPE, "dig00002", "pip install x")
            b.gate.take_pending(SCOPE)
            token = b.approvals.issue(
                SCOPE, "dig00002", {"digest": "dig00002", "scope": SCOPE}
            )
            b._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "sid", "count": 1}

            session = b.sessions.get(SCOPE)
            asyncio.run(session.lock.acquire())  # 模拟「这一轮还在跑」
            _click(b, token, act="approve")

            assert len(scheduled) == 1, "点击应该只调度一次续跑"
            assert session.lock.locked(), "续跑必须排队等锁，不能抢着并发"
            # `.ok` 已经落盘，钩子 resume 时才会看到它 —— 批准和执行是解耦的
            assert [n for n in _suffixes(b, SCOPE) if n.endswith(".ok")] == [
                "dig00002.ok"
            ]
        finally:
            restore()


def test_second_card_clicked_after_first_already_resumed():
    """🔴 RED（干扰）：一轮 defer 两个操作 ⇒ 两张卡。点完第 1 张（已 pop _awaiting）
    再点第 2 张会怎样？

    实测：`_continue_after_approval` 里 `_awaiting.pop` 是**一次性**的，第 1 张卡的
    续跑一跑起来就把它取走了。第 2 张卡点下去时 `gate.approve` 仍然成功写下 `.ok`
    （磁盘上确实还有 `.asked`），toast 回「好，继续」，但 `_awaiting` 已经没了
    ⇒ **一个 resume 都不会被调度**。
    用户看到「好，继续」然后永远没有下文，而磁盘上留下一张 10 分钟内有效的
    通行证 —— 下一轮跑到同样的命令会被直接放行、没有卡片、用户不知道。

    approval.py 的红字说「判据是磁盘上有没有未决记录」，这条守住了「拒绝/新会话之后
    不能批准」，但守不住「这一轮的 resume 已经被别的点击消费掉了」。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _fake_bridge(tmp)
        scheduled, restore = _patch_dispatch(b)
        try:
            _put_pending(b, SCOPE, "dig00001", "npm publish")
            _put_pending(b, SCOPE, "dig00002", "pip install y")
            b.gate.take_pending(SCOPE)
            first = b.approvals.issue(
                SCOPE, "dig00001", {"digest": "dig00001", "scope": SCOPE}
            )
            second = b.approvals.issue(
                SCOPE, "dig00002", {"digest": "dig00002", "scope": SCOPE}
            )
            b._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "sid", "count": 2}

            _click(b, first, act="approve")
            # 真实里 _continue_after_approval 一跑起来就 pop ⇒ 这里照样模拟那一步
            b._awaiting.pop(SCOPE, None)
            before = len(scheduled)
            response = _click(b, second, act="approve")

            assert len(scheduled) > before, (
                "第 2 张卡点了「同意」，但没有任何续跑被调度 ⇒ 用户看到「好，继续」后再无下文"
            )
            assert "失效" not in _toast_text(response)
        finally:
            restore()


# ======================================================================
# 干扰 5：改动 8（掐停落盘）× /new × 改动 2（批准清理）
# ======================================================================


def test_new_clears_all_three_states():
    """/new 必须同时清：gate（磁盘）、_awaiting（内存）、_paused（内存+磁盘）。

    实测：三者都清了。这条是阳性对照，也防止将来有人只清两个。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _fake_bridge(tmp)
        _put_pending(b, SCOPE, "dig00003", "npm publish")
        b.gate.take_pending(SCOPE)
        b._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "sid", "count": 1}
        b._paused[SCOPE] = "连续试探被拦"
        b._save_paused()

        asyncio.run(Bridge._handle_command(b, SCOPE, "oc_1", "/new"))

        assert _suffixes(b, SCOPE) == [], "gate 没清干净"
        assert SCOPE not in b._awaiting, "_awaiting 没清"
        assert SCOPE not in b._paused, "_paused 没清"
        reloaded = _fake_bridge(tmp)
        reloaded._paused_path = b._paused_path
        reloaded._load_paused()
        assert SCOPE not in reloaded._paused, "_paused 的落盘没跟着清 ⇒ 重启后又暂停了"


def test_new_then_clicking_an_old_card_gives_a_misleading_toast():
    """🔴 RED（干扰）：/new 之后点旧卡片 ⇒ toast「写批准记录失败」。

    实测：令牌本身仍然有效（签名过、nonce 没用过、没过期）⇒ redeem 成功；
    但 gate.clear 已经把 `.asked` 删了 ⇒ gate.approve 返回 False ⇒ 走到
    「写批准记录失败」那个 error toast。

    这句话是**错的且有害的**：用户会以为磁盘坏了、服务出故障，而真相是
    「这一轮已经结束了，这张卡作废了」。应该是一句 info：「这张卡已经过期了，
    发条新消息重新来」。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _fake_bridge(tmp)
        scheduled, restore = _patch_dispatch(b)
        try:
            _put_pending(b, SCOPE, "dig00004", "pip install z")
            b.gate.take_pending(SCOPE)
            token = b.approvals.issue(
                SCOPE, "dig00004", {"digest": "dig00004", "scope": SCOPE}
            )
            b._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "sid", "count": 1}

            asyncio.run(Bridge._handle_command(b, SCOPE, "oc_1", "/new"))
            toast = _toast_text(_click(b, token, act="approve"))

            assert "失败" not in toast, (
                f"/new 之后点旧卡片得到「{toast}」—— 这是把「卡片作废」说成了「系统故障」"
            )
            assert scheduled == [], "/new 之后不该再续跑"
        finally:
            restore()


def test_paused_restored_from_disk_blocks_the_turn_not_the_card():
    """掐停后重启：paused.json 恢复了，但 _awaiting 没了。用户点旧卡片会怎样？

    实测：
      - `_paused` 从磁盘恢复 ⇒ 新消息被挡住（对的，掐停有效）
      - 但 `.asked` 也在磁盘上、令牌密钥也落盘了 ⇒ 卡片能被 redeem，
        gate.approve 会写下 `.ok`，而 `_awaiting` 空 ⇒ 没有 resume
      - 也就是说：**被掐停的会话里，点旧卡片仍然能在磁盘上留下一张放行证**，
        等 /resume 之后的下一轮跑到同样命令时被直接放行。

    这条是干扰 5 和干扰 7 的交点。断言写成「掐停的会话不许写 .ok」。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        first = _fake_bridge(tmp)
        _put_pending(first, SCOPE, "dig00005", "npm publish")
        first.gate.take_pending(SCOPE)
        token = first.approvals.issue(
            SCOPE, "dig00005", {"digest": "dig00005", "scope": SCOPE}
        )
        first._paused[SCOPE] = "连续试探被拦"
        first._save_paused()
        secret = first.approvals._secret

        # 重启：同一个 home、同一把密钥（改动 11 让它跨重启有效），内存全空
        second = _fake_bridge(tmp)
        second.approvals = ApprovalStore(secret)
        second._load_paused()
        assert second._paused.get(SCOPE), "掐停没从磁盘恢复，这轮不可信"
        assert second._awaiting == {}, "重启后 _awaiting 本应是空的"

        scheduled, restore = _patch_dispatch(second)
        try:
            # 这张卡是重启**前**签发的：密钥对得上，但 _pending 在新进程里没有记录
            _click(second, token, act="approve")
            granted = [n for n in _suffixes(second, SCOPE) if n.endswith(".ok")]
            assert granted == [], (
                f"被掐停的会话里点旧卡片写下了放行证：{granted} ⇒ /resume 之后会被直接放行"
            )
            assert scheduled == []
        finally:
            restore()


# ======================================================================
# 干扰 4：改动 6（一次性配对码）× 改动 3（审查层）/ 改动 2（审批）
# ======================================================================
#
# 还没绑定主人时，有人发的消息正好长得像配对码**又**包含危险内容。
# 问：会不会被当配对码处理？审查层在配对阶段跑不跑？


def test_pairing_phase_never_executes_anything():
    """核心：配对阶段的消息**永远不当任务执行** —— 所以审查层跑不跑都不影响安全。

    实测：`on_message` 在白名单为空那条分支里直接 `return`，不走 `_handle`，
    `PairingResult.should_run` 恒为 False。
    """
    from bridge.pairing import Outcome, PairingResult

    assert PairingResult(Outcome.BOUND, owner_open_id="x").should_run is False
    assert PairingResult(Outcome.WRONG).should_run is False

    with open(os.path.join(_ROOT, "bridge", "app.py"), encoding="utf-8") as handle:
        source = handle.read()
    branch = source.split("if not self.settings.owner_open_ids:")[1].split(
        "if open_id not in self.settings.owner_open_ids:"
    )[0]
    assert "self._handle(" not in branch, "配对分支里出现了任务派发 ⇒ 陌生人能干活"
    assert "self.reviewer" not in branch


def test_code_shaped_message_with_injection_is_not_a_code():
    """`ABC123; rm -rf /` 会不会被当成配对码？

    实测：`_normalize` 把非字母数字全删掉 ⇒ `ABC123RMRF`（10 字符），
    长度和 6 位码不符 ⇒ 判 NOT_A_CODE、**不计尝试次数**、不绑定。
    危险内容也不执行（上一条测试证明了配对阶段不执行任何东西）。
    """
    from bridge.pairing import Outcome, PairingGate, _normalize

    assert _normalize("ABC123; rm -rf /") == "ABC123RMRF"

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        config = os.path.join(tmp, "config.toml")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write("[feishu]\nowner_open_ids = []\n")
        gate = PairingGate(
            config, code="ABC123", issued_at=time.time(), state_path=os.path.join(tmp, "s")
        )
        result = gate.consider("ou_stranger", "ABC123; rm -rf /", "om_1")
        assert result.outcome is Outcome.NOT_A_CODE
        assert result.bound is False
        assert gate.failed_attempts == 0, "不是码的消息不该烧掉主人的尝试次数"


def test_separator_padded_code_still_binds_and_does_not_execute():
    """🔴 注意这条**不是** RED，是把一个真实的宽容点钉住：
    `ABC 123` / `abc-123` 归一化之后等于 `ABC123` ⇒ **会绑定**。

    实测：这是 `_normalize` 刻意的宽容（用户从终端复制粘贴，可能带空格/全角）。
    和注入无关 —— 绑定之后那条消息照样不执行。但它意味着「长得像码」的判据比
    6 个字符宽：任何归一化后等于码的串都算码。写下来是为了下次改 _normalize 时
    知道这个面有多大。
    """
    from bridge.pairing import Outcome, PairingGate

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        config = os.path.join(tmp, "config.toml")
        with open(config, "w", encoding="utf-8") as handle:
            handle.write("[feishu]\nowner_open_ids = []\n")
        gate = PairingGate(
            config,
            code="ABC123",
            issued_at=time.time(),
            state_path=os.path.join(tmp, "s"),
        )
        result = gate.consider("ou_stranger", "abc-123", "om_1")
        assert result.outcome is Outcome.BOUND
        assert result.should_run is False, "绑定消息也绝不能被执行"


def test_review_layer_denies_the_same_text_once_bound():
    """阳性对照：绑定之后，同一句危险内容必须被审查层挡住。

    这条过不了，上面「配对阶段不执行」就没有意义 —— 得证明审查层本身是活的。
    实测。
    """
    from bridge.review import Reviewer, ReviewContext, Verdict

    decision = Reviewer().review_request(ReviewContext(text="ABC123; rm -rf /"))
    assert decision.verdict is Verdict.DENY


# ======================================================================
# 干扰 6：改动 10（cmdclass 结构化判定）× 既有 policy.allowed_tools()
# ======================================================================
#
# 两份清单各写一份。问题原话：「cmdclass 说只读不用问，但 policy 可能根本没给
# 这个命令权限 ⇒ 用户点了同意也执行不了（点了没反应）」。
#
# 🔴 实测结论和这个担心**方向相反**，而且更严重：钩子返回 `allow` 会
# **覆盖** `--allowed-tools`。所以漂移的后果不是「点了没反应」，
# 而是「没有卡片、直接执行」。见 test_hook_allow_overrides_allowed_tools_live。


def _policy_prefix_allows(command: str) -> bool:
    """policy.READONLY_BASH 的前缀清单放不放这条命令。"""
    return any(
        command == prefix or command.startswith(prefix + " ")
        for prefix in readonly_command_prefixes()
    )


# cmdclass 判「只读、不用问」，但 policy 的 Bash 前缀清单里没有的命令。
# 实测得到 31 条，这里取有代表性的一组。
_DRIFTED = (
    "cat /etc/hosts",
    "head -5 x.txt",
    "grep foo x.txt",
    "rg foo",
    "find . -name x",
    "jq . x.json",
    "ps aux",
    "env",
    "whoami",
    "sort x.txt",
    "which git",
    "kubectl get pods",
    "docker ps",
    "brew list",
    "npm ls",
    "launchctl list",
    "defaults read com.apple.dock",
    "sysctl -a",
    "git tag",
    "git branch",
)


def test_two_whitelists_drift_is_benign():
    """两份白名单确实不一致，但**不会**让用户"点了同意却没反应"。

    `cmdclass` 放行的命令里有 18 条不在 `policy` 的 `Bash(前缀:*)` 清单里
    （`cat`/`head`/`grep`/`ps`/`whoami`/`sort`/`which`/`git tag`/`find` …）。
    原本担心的后果是：钩子说"不用问"，但权限层拒绝 ⇒ 不弹卡片、也干不了活。

    2026-11-03 逐条真打 claude 实测：**9/9 全部放行**。原因是 Claude Code
    自带只读启发式，这些命令在空 allowlist 下本来就自动跑 ——
    也就是说 `policy.READONLY_BASH` 那份清单基本是废票（它匹配的是本来
    不会被问的命令）。

    这条测试锁住的结论是：漂移存在但无害，**不要**为了"对齐两份清单"去
    给 policy 加一长串前缀 —— 那只会让人以为闸门更严，实际没变。
    真正的边界是 `--disallowed-tools` 和 cmdclass 的白名单制。
    """
    from bridge.cmdclass import classify
    from bridge.policy import READONLY_BASH

    prefixes = tuple(
        entry[len("Bash(") : -1].removesuffix(":*")
        for entry in READONLY_BASH
        if entry.startswith("Bash(")
    )
    samples = [
        "cat /etc/hosts", "head -5 x.txt", "grep foo x.txt", "ps aux",
        "whoami", "sort x.txt", "which git", "git tag", "find . -name x",
    ]
    drifted = [
        cmd
        for cmd in samples
        if classify(cmd).readonly
        and not any(cmd == pre or cmd.startswith(pre + " ") for pre in prefixes)
    ]
    # 漂移本身是预期的（见 docstring），这里只断言它确实存在、
    # 以免有人"修好"了之后这条测试变成死票。
    assert drifted, "漂移消失了？确认是真对齐了，还是 cmdclass 变严了"

def test_always_deny_is_never_classified_readonly():
    """阳性对照（这条是**绿**的，很重要）：policy.ALWAYS_DENY 覆盖的那些命令，
    cmdclass 一条都不放行。

    也就是说漂移只发生在「allow 的宽窄」上，硬底线（rm/sudo/push/curl…）两层一致。
    实测。
    """
    for command in (
        "rm -rf /tmp/x",
        "sudo ls",
        "git push",
        "git reset --hard",
        "git clean -fd",
        "kubectl delete pod x",
        "curl http://example.com",
        "wget http://example.com",
    ):
        assert classify(command).readonly is False, f"cmdclass 放行了硬底线命令：{command}"


_DESTRUCTIVE_GIT = (
    "git stash",                                      # 把未提交的改动全收走
    "git stash clear",                                # 直接丢掉 stash
    "git tag -d v1",                                  # 删 tag
    "git branch -D main",                             # 删分支
    "git config --global user.email x@example.com",   # 改全局 git 身份
)


@pytest.mark.parametrize("command", _DESTRUCTIVE_GIT)
def test_cmdclass_module_gates_destructive_git(command):
    """阳性对照（**绿**）：cmdclass 模块已经把这几条判成「要问」。

    实测：`stash`/`config` 已从 GIT_SAFE 移出，`tag -d`/`branch -D` 按选项判。
    这条绿是下一条红的前提 —— 它证明「正确答案」是什么。
    """
    assert classify(command).readonly is False, (
        f"cmdclass 模块把会毁东西的 git 子命令判成只读：{command!r}"
    )


@pytest.mark.parametrize("command", _DESTRUCTIVE_GIT)
def test_hook_embedded_copy_gates_destructive_git(command):
    """真正跑的是**钩子内嵌的那份副本**，不是 cmdclass 模块。这条盯着它。

    为什么单独测：这一条在本次审计期间**真的红过**。
    22:18 有人修了 `cmdclass.py`（把 stash/config 从 GIT_SAFE 移出、
    给 tag/branch 加按选项判），但 `approval.py` 的 `HOOK_SOURCE` 里那份副本
    直到 22:19 才跟上。中间那一分钟实测可复现：

        $ echo '{"tool_name":"Bash","tool_input":{"command":"git stash"}}' \\
            | CARHER_APPROVAL_DIR=/tmp/appr python3 /tmp/h.py
        {"permissionDecision": "allow", "permissionDecisionReason": "只读操作，不需要确认"}

    现在两份都对了（22:19 之后实测：`git stash` 在只读模式下 defer、
    工作区 f.txt 没被动、批准目录里出现 `.pending`）。
    留着这条是因为**第三份清单的存在本身就是这个干扰的根因** ——
    改 cmdclass 而忘了 HOOK_SOURCE，测试照样全绿、线上行为不变。
    """
    from bridge.approval import HOOK_SOURCE

    namespace: dict = {}
    exec(compile(HOOK_SOURCE, "<hook>", "exec"), namespace)  # noqa: S102
    assert namespace["_classify_readonly"](command) is False, (
        f"钩子内嵌副本仍放行：{command!r} —— 模块改了，HOOK_SOURCE 漏改"
    )


def test_hook_copy_and_module_lists_are_identical():
    """三份清单里，cmdclass 模块和钩子内嵌副本必须**字面一致**。

    审计期间实测到的漂移（22:18~22:19 那一分钟窗口）：
      GIT_SAFE   钩子多了 {stash, config}，模块多了 {diff-tree, diff-index,
                 check-ignore, var}
      SUBCOMMAND_GATED["git"] 同上；READERS 一致
    现已修复。断言用集合比较并把差集打进消息，下次漂移时一眼看出方向。
    """
    from bridge.approval import HOOK_SOURCE
    from bridge.cmdclass import GIT_SAFE, READERS, SUBCOMMAND_GATED

    namespace: dict = {}
    exec(compile(HOOK_SOURCE, "<hook>", "exec"), namespace)  # noqa: S102
    assert namespace["READERS"] == READERS
    assert set(namespace["GIT_SAFE"]) == set(GIT_SAFE), (
        "GIT_SAFE 漂移，仅钩子有："
        f"{set(namespace['GIT_SAFE']) - set(GIT_SAFE)}，仅模块有："
        f"{set(GIT_SAFE) - set(namespace['GIT_SAFE'])}"
    )
    assert namespace["SUBCOMMAND_GATED"] == SUBCOMMAND_GATED


# ---------- 干扰 6 的决定性实测：钩子的 allow 盖不盖得过 policy ----------


def _run_claude(prompt: str, workspace: str, allowed, denied, config_dir, approval_dir=""):
    """起一发真 claude，返回 (result dict)。只在 CARHER_LIVE_GATE=1 时被调用。"""
    argv = [
        "claude", "-p", prompt,
        "--permission-mode", "manual",
        "--output-format", "json",
        "--model", MODEL,
    ]
    for tool in allowed:
        argv += ["--allowed-tools", tool]
    for tool in denied:
        argv += ["--disallowed-tools", tool]

    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = config_dir
    if approval_dir:
        env["CARHER_APPROVAL_DIR"] = approval_dir
    proc = subprocess.run(
        argv, cwd=workspace, capture_output=True, text=True, env=env,
        stdin=subprocess.DEVNULL, timeout=300,
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"claude 没返回 JSON：{proc.stdout[:300]} {proc.stderr[:300]}")


def _hook_env(tmp: str, decision: str):
    """装一个**固定返回某个决定**的 PreToolUse 钩子，用来单独测优先级。"""
    hook = os.path.join(tmp, "probe_hook.py")
    with open(hook, "w", encoding="utf-8") as handle:
        handle.write(
            "import json,sys\n"
            "json.load(sys.stdin)\n"
            "print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse',"
            f"'permissionDecision':{decision!r},'permissionDecisionReason':'probe'}}}}))\n"
        )
    config_dir = os.path.join(tmp, "cc")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "settings.json"), "w", encoding="utf-8") as handle:
        json.dump(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash|Write|Edit|NotebookEdit",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"/usr/bin/env python3 {hook}",
                                }
                            ],
                        }
                    ]
                }
            },
            handle,
        )
    return config_dir


@live_only
def test_hook_allow_overrides_allowed_tools_live():
    """🔴 **干扰 6 的真正结论，真打 claude 测出来的。**

    钩子返回 `allow` 的工具调用会执行，**即使 `--allowed-tools` 里没有它**。
    也就是说 `cmdclass`（钩子那层）是**更宽的一侧，而且它赢**。

    实测命令与结果（2026-11-03，claude-haiku-5-5）：
        --allowed-tools 'Bash(git status:*)' --allowed-tools Read
        prompt: 用 Bash 原样跑这一条命令：whoami
        ⇒ permission_denials: []   结果里给出了真实用户名

    所以问题原话里的担心（「cmdclass 放行但 policy 不允许 ⇒ 点了同意也执行不了」）
    方向是反的：漂移的后果是**不该放的被放了**，不是「点了没反应」。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        workspace = os.path.join(tmp, "ws")
        os.makedirs(workspace)
        config_dir = _hook_env(tmp, "allow")
        data = _run_claude(
            "用 Bash 原样跑这一条命令：whoami",
            workspace,
            ["Bash(git status:*)", "Read"],
            [],
            config_dir,
        )
        denials = [d.get("tool_name") for d in data.get("permission_denials", [])]
        assert denials, (
            "钩子的 allow 盖过了 --allowed-tools：whoami 不在白名单里却执行了 "
            f"(result={data.get('result', '')[:120]!r})"
        )


@live_only
def test_hook_allow_cannot_override_disallowed_tools_live():
    """阳性对照（**绿**，真打过）：`--disallowed-tools` 挡得住钩子的 allow。

    实测：`--allowed-tools Bash --disallowed-tools 'Bash(rm:*)'` + 钩子 allow
    ⇒ `rm -f victim.txt` 被拒，文件还在。

    所以 policy.ALWAYS_DENY 那条硬底线是**真底线**，钩子穿不过去。
    这也是为什么干扰 6 的后果是「只读模式被放宽」，而不是「rm 能跑」。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        workspace = os.path.join(tmp, "ws")
        os.makedirs(workspace)
        victim = os.path.join(workspace, "victim.txt")
        with open(victim, "w") as handle:
            handle.write("victim")
        config_dir = _hook_env(tmp, "allow")
        data = _run_claude(
            "用 Bash 原样跑这一条命令：rm -f victim.txt",
            workspace,
            ["Bash"],
            ["Bash(rm:*)"],
            config_dir,
        )
        assert os.path.exists(victim), "🔴 钩子的 allow 穿过了 disallowed-tools"
        assert data.get("permission_denials"), "既没拒也没删？这轮不可信"


@live_only
def test_readonly_mode_write_executes_once_approved_live():
    """🔴 **最该让人知道的一条，真打 claude 验过。**

    `mode = "readonly"` 下，`policy.allowed_tools()` **不含** Write/Edit。
    但审批钩子挂在 `Write` 上 ⇒ 用户在飞书点一下「同意，去做」，
    Write 就真的执行了。

    实测全链路（2026-11-03，用真实 install_hook 装的钩子 + 真实 readonly 工具集）：
      1. 第一次跑 ⇒ stop_reason=tool_deferred，canary.txt 没变，
         批准目录里出现 `<digest>.pending`
      2. 把 `.pending` 换成 `.ok`（模拟用户点同意）
      3. 再跑一次 ⇒ stop_reason=end_turn，**canary.txt 内容变成 CHANGED**

    产品含义：审批卡把「只读模式」变成了「只读 + 问一句就能写」。
    这可能是故意的（审批层的意义就是能批准危险操作），但 `/status` 告诉用户的是
    「只读：能看代码、查 git、搜文件；**不能改文件**」—— 那句话现在是错的。
    这条测试断言「只读模式下批准也不该能写」，**要么改文案，要么改行为**。
    """
    import bridge.approval as approval_module

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        workspace = os.path.realpath(os.path.join(tmp, "ws"))
        approval_dir = os.path.join(tmp, "appr")
        config_dir = os.path.join(tmp, "cc")
        os.makedirs(workspace)
        os.makedirs(approval_dir)
        target = os.path.join(workspace, "canary.txt")
        with open(target, "w") as handle:
            handle.write("READONLY-ORIG")

        original = approval_module.user_settings_path
        approval_module.user_settings_path = lambda: os.path.join(tmp, "no-user-settings")
        try:
            approval_module.install_hook(
                os.path.join(tmp, "hooks", "approval_hook.py"),
                os.path.join(config_dir, "settings.json"),
            )
        finally:
            approval_module.user_settings_path = original

        policy = Policy(
            mode="readonly",
            workspaces=(workspace,),
            default_workspace=workspace,
            model=MODEL,
            turn_timeout_sec=120,
        )
        assert "Write" not in policy.allowed_tools(), "前提变了：只读模式给了 Write"

        prompt = f"用 Write 工具把 CHANGED 写进 {target}"
        first = _run_claude(
            prompt, workspace, policy.allowed_tools(), policy.disallowed_tools(),
            config_dir, approval_dir,
        )
        assert first.get("stop_reason") == "tool_deferred", (
            f"第一次应该 defer，拿到 {first.get('stop_reason')!r}"
        )
        pending = [n for n in os.listdir(approval_dir) if n.endswith(".pending")]
        assert pending, "钩子没留下待办，这轮不可信"

        # 模拟用户点「同意，去做」
        for name in pending:
            digest = name[: -len(".pending")]
            with open(os.path.join(approval_dir, digest + ".ok"), "w") as handle:
                handle.write(str(int(time.time())))
            os.unlink(os.path.join(approval_dir, name))

        _run_claude(
            prompt, workspace, policy.allowed_tools(), policy.disallowed_tools(),
            config_dir, approval_dir,
        )
        with open(target) as handle:
            content = handle.read()
        assert content == "READONLY-ORIG", (
            "🔴 只读模式下点一次「同意」就把文件改了 —— "
            f"policy.describe() 说的「不能改文件」不成立（内容={content!r}）"
        )


@live_only
def test_git_stash_defers_instead_of_destroying_work_live():
    """干扰 6 的最坏后果，真打过两次 —— 修复前后各一次。

    **修复前**（22:18，钩子内嵌副本还判 `git stash` 只读）：
        f.txt = "UNCOMMITTED-WORK-DO-NOT-LOSE"
        ⇒ stop_reason=end_turn, denials=[], 批准目录空的
        ⇒ f.txt 变回 "v1" —— 未提交的改动被收走，一张卡片都没弹

    **修复后**（22:19 之后，同一条命令、同一个只读工具集）：
        ⇒ stop_reason=tool_deferred, denials=[],
          批准目录出现 6caaad81b14ac3b54d74c7c7.pending
        ⇒ f.txt 仍是 "UNCOMMITTED-WORK"

    「丢了一天的未提交改动」用户会归因成 agent 乱改代码，所以这条留作回归。
    """
    import bridge.approval as approval_module

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        workspace = os.path.realpath(os.path.join(tmp, "ws"))
        approval_dir = os.path.join(tmp, "appr")
        config_dir = os.path.join(tmp, "cc")
        os.makedirs(workspace)
        os.makedirs(approval_dir)
        run = lambda *args: subprocess.run(args, cwd=workspace, check=True,
                                           capture_output=True)
        run("git", "init", "-q")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "t")
        target = os.path.join(workspace, "f.txt")
        with open(target, "w") as handle:
            handle.write("v1\n")
        run("git", "add", "f.txt")
        run("git", "commit", "-qm", "init")
        precious = "UNCOMMITTED-WORK-DO-NOT-LOSE\n"
        with open(target, "w") as handle:
            handle.write(precious)

        original = approval_module.user_settings_path
        approval_module.user_settings_path = lambda: os.path.join(tmp, "no-user-settings")
        try:
            approval_module.install_hook(
                os.path.join(tmp, "hooks", "approval_hook.py"),
                os.path.join(config_dir, "settings.json"),
            )
        finally:
            approval_module.user_settings_path = original

        policy = Policy(
            mode="readonly", workspaces=(workspace,), default_workspace=workspace,
            model=MODEL, turn_timeout_sec=120,
        )
        data = _run_claude(
            "用 Bash 原样跑这一条命令：git stash",
            workspace, policy.allowed_tools(), policy.disallowed_tools(),
            config_dir, approval_dir,
        )
        with open(target) as handle:
            content = handle.read()
        assert content == precious, (
            "🔴 只读模式下 git stash 把未提交的改动收走了，而且一张卡片都没弹"
        )
        assert data.get("stop_reason") == "tool_deferred", (
            f"没有走审批就放过去了（stop_reason={data.get('stop_reason')!r}）"
        )
        assert [n for n in os.listdir(approval_dir) if n.endswith(".pending")], (
            "没有留下待办 ⇒ 用户收不到卡片"
        )


# ======================================================================
# 干扰 7：改动 11（密钥落盘）× 改动 3（ApprovalStore._pending 在内存）
# ======================================================================
#
# 问题原话：「那么『修好了旧卡片能点』到底成立吗？这会不会让用户得到比之前
# 更令人困惑的提示？」


def test_secret_survives_restart():
    """阳性对照：密钥确实跨重启稳定（改动 11 的目标）。实测。"""
    from bridge.app import _load_or_create_secret

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        first = _load_or_create_secret(tmp)
        second = _load_or_create_secret(tmp)
        assert first == second and len(first) >= 32
        mode = os.stat(os.path.join(tmp, "approval.secret")).st_mode & 0o777
        assert mode == 0o600, f"密钥文件权限是 {oct(mode)}，应该是 0600"


def test_old_card_after_restart_is_still_unredeemable():
    """🔴 RED（干扰 7 的核心）：密钥落盘了，但 `_pending` 是内存的 ⇒
    重启后旧卡片的签名能验过，**却查不到待办记录**，`redeem` 照样返回 None。

    实测：`redeem_detailed` 返回 `(None, "stale")` —— 走的是
    「签名有效但没有待办记录」那个分支。

    所以改动 11 声称修的那个问题（「服务重启 14 次，用户点任何一张早先的卡片
    都得到『这个按钮已失效或你无权操作』」）**并没有被修好**：
    密钥不再是失效的原因了，但 `_pending` 丢失成了新的原因，
    用户看到的那句话一字不差。

    真要修，`_pending` 也得落盘（磁盘上的 `.asked` 已经有这个信息了，
    `redeem` 可以改成查它）。
    """
    from bridge.app import _load_or_create_secret

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        secret = _load_or_create_secret(tmp)
        before = ApprovalStore(secret)
        token = before.issue(SCOPE, "dig00006", {"digest": "dig00006", "scope": SCOPE})

        after = ApprovalStore(_load_or_create_secret(tmp))  # 重启
        payload, reason = after.redeem_detailed(
            token.to_value(), OWNER, frozenset({OWNER})
        )
        assert payload is not None, (
            f"重启后旧卡片仍然点不动（reason={reason!r}）—— 密钥落盘没有达到目的"
        )


def test_same_process_redeem_works():
    """阳性对照：同一个进程里 redeem 必须成功。

    这条过不了，上面那条红可能只是因为令牌本身就坏的。实测。
    """
    store = ApprovalStore(b"k" * 32)
    token = store.issue(SCOPE, "dig00007", {"digest": "dig00007", "scope": SCOPE})
    payload, reason = store.redeem_detailed(
        token.to_value(), OWNER, frozenset({OWNER})
    )
    assert payload is not None and reason == ""


def test_restart_gives_the_user_the_same_opaque_message():
    """🔴 RED（干扰 7 的用户可见面）：`redeem_detailed` 专门区分了
    USED/EXPIRED/STALE/FORBIDDEN 四种原因（注释写明「不同原因要给用户不同的话」），
    但 `app.on_card_action` 调的是 `redeem()`，**把原因丢掉了**，
    四种情况一律回「这个按钮已失效或你无权操作」。

    实测：`grep redeem_detailed bridge/` 只有 review.py 自己调。

    干扰角度：改动 11 让密钥跨重启有效，于是「重启后点旧卡」从
    「签名对不上」变成了「没有待办记录」—— 两者都落在 STALE。
    用户拿到的提示和改动 11 之前**完全一样**，所以他会认为这个 bug 没修，
    而我们会认为修了。这是比之前更糟的地方：问题被当成已解决。
    """
    with open(os.path.join(_ROOT, "bridge", "app.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert "redeem_detailed" in source, (
        "on_card_action 仍在用丢掉原因的 redeem() ⇒ 四种失败同一句话"
    )


# ======================================================================
# 干扰 8：改动 1（osenv 单实例锁）× 改动 9（看门狗 os._exit(75) 自杀重启）
# ======================================================================


def _lock_child_source(lock_path: str) -> str:
    return (
        "import os,sys\n"
        f"sys.path.insert(0, {_ROOT!r})\n"
        "from bridge.osenv import acquire_single_instance\n"
        f"handle = acquire_single_instance({lock_path!r})\n"
        "print('acquired', os.getpid(), flush=True)\n"
        "os._exit(75)\n"  # 照看门狗那样走 EX_TEMPFAIL
    )


def test_lock_is_released_after_watchdog_self_exit():
    """核心：看门狗 `os._exit(75)` 之后，launchd 立刻拉起的新进程必须拿得到锁。

    实测（本测试真起两个子进程）：第一个拿锁后 `os._exit(75)`，
    第二个**立刻**启动也能拿到 —— 内核在进程退出时释放 flock，
    `os._exit` 绕过 Python 清理但不绕过内核。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        lock = os.path.join(tmp, "bridge.lock")
        source = _lock_child_source(lock)

        first = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True, timeout=60
        )
        assert first.returncode == 75 and "acquired" in first.stdout

        second = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True, timeout=60
        )
        assert "acquired" in second.stdout, (
            f"自杀重启后新进程拿不到锁：{second.stdout!r} {second.stderr[-400:]!r}"
        )


def test_stale_lock_file_with_live_unrelated_pid_does_not_block_startup():
    """锁文件里残留着**另一个还活着的进程**的 pid（pid 被复用、或上次写了别人的号）
    ⇒ 不能靠「pid 还在」就拒绝启动，否则服务永远起不来。

    实测：`acquire_single_instance` 先试 flock，拿到就放行；
    pid 只用来给拒绝时的提示文案。这个顺序是对的。
    """
    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        lock = os.path.join(tmp, "bridge.lock")
        with open(lock, "w") as handle:
            handle.write(str(os.getpid()))  # 本测试进程，确定活着
        child = subprocess.run(
            [sys.executable, "-c", _lock_child_source(lock)],
            capture_output=True, text=True, timeout=60,
        )
        assert "acquired" in child.stdout, (
            f"残留 pid 挡住了启动：{child.stdout!r} {child.stderr[-400:]!r}"
        )


def test_second_live_instance_is_refused():
    """阳性对照：真有两个进程时必须拒绝第二个（飞书长连接随机投递，
    两个在线消息会漂走）。

    实测：子进程持锁不退，父进程这边再 acquire 必须抛 SingleInstanceError。
    """
    from bridge.osenv import SingleInstanceError, acquire_single_instance

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        lock = os.path.join(tmp, "bridge.lock")
        holder = subprocess.Popen(
            [
                sys.executable, "-c",
                "import os,sys\n"
                f"sys.path.insert(0, {_ROOT!r})\n"
                "from bridge.osenv import acquire_single_instance\n"
                f"h = acquire_single_instance({lock!r})\n"
                "print('held', flush=True)\n"
                "sys.stdin.read()\n",
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        try:
            assert holder.stdout.readline().strip() == "held"
            with pytest.raises(SingleInstanceError) as caught:
                acquire_single_instance(lock)
            assert "已经有一个桥在跑" in str(caught.value)
        finally:
            holder.stdin.close()
            holder.wait(timeout=30)


def test_lock_default_path_is_not_inside_the_app_home():
    """纪律性（读码推断）：默认锁文件在 `~/.feishu-agent-bridge.lock`，
    不在 app home 里 —— 清理 app home（重装）不该顺手释放/删掉锁。

    这条只断言路径形状，**不创建也不触碰**那个文件（用户的服务正在用）。
    """
    import inspect

    from bridge.osenv import acquire_single_instance

    source = inspect.getsource(acquire_single_instance)
    assert "~/.feishu-agent-bridge.lock" in source


def test_windows_restart_loop_is_documented():
    """读码推断：launchd 的 KeepAlive 自带「退出就拉起」，计划任务没有等价物 ⇒
    Windows 侧必须自己包一层重启循环，否则 `os._exit(75)` 之后再也起不来。

    这里只钉住 osenv.py 里那条注释还在（它是唯一记录这个坑的地方）。
    """
    with open(os.path.join(_ROOT, "bridge", "osenv.py"), encoding="utf-8") as handle:
        source = handle.read()
    assert "重启循环" in source and "75" in source
