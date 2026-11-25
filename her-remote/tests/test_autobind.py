"""装完到绑定主人之间那段窗口 —— 从「第一条消息自动绑定」换成一次性配对码。

## 换掉的那个洞

旧行为（`autobind_first_sender`）：白名单为空时，**第一条消息的发送者直接成为主人**，
open_id 落盘、重启仍在。自建应用默认对全员可见 ⇒ 同租户任何人私聊这个机器人并抢先
发一条，就拿下了这台电脑（只读也能读 home 目录，输出全部回传到飞书）。受害者本人
之后发消息走「静默丢弃」，只会觉得机器人坏了。

而这条路不是边缘情况：安装器只在拿不到创建者 open_id 时降级到 autobind，
`bootstrap.py` 自己写明「拿不到是常态」（权限没审批就取不到）—— 手工建应用那条路
**几乎必然**落到 autobind。

## 现在的行为

码的生成/校验/落盘全在 `bridge/pairing.py`，性质由 `tests/test_pairing.py` 盯。
这个文件只盯两件事：
1. **配置里再也没有能打开那个窗口的开关**（安装器 + 示例配置）。
2. **接入层**：白名单为空时，不是码的消息一律不执行（fail-closed 没被新功能破坏）。

最后两条走完整 `on_message` 闭环，验证配对码真的接进了鉴权路径
（接入前它们是 xfail，证明那个洞当时确实开着）。
"""

from __future__ import annotations

import os
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.pairing import PairingGate  # noqa: E402
from test_auth import make_event  # noqa: E402

OWNER = "ou_owner"
STRANGER = "ou_stranger"
WS = "/tmp/bridge-test-ws"
CODE = "7KMQ3F"

CONFIG_TEMPLATE = """\
[feishu]
app_id = "cli_test"
app_secret = "s3cret"
owner_open_ids = []
pairing_code = "{code}"
pairing_issued_at = {issued_at}
pairing_ttl_min = 30
pairing_max_attempts = 5

[agent]
workspaces = ["{ws}"]
default_workspace = "{ws}"
mode = "readonly"
model = "claude-haiku-5-5"
turn_timeout_sec = 60
session_idle_reset_min = 30

[limits]
max_reply_chars = 5000
"""

_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
EXAMPLE_CONFIG = os.path.normpath(os.path.join(_HERE, "..", "config.example.toml"))


def write_config(code: str = CODE) -> str:
    import time

    os.makedirs(WS, exist_ok=True)
    text = CONFIG_TEMPLATE.format(code=code, issued_at=int(time.time()), ws=WS)
    directory = tempfile.mkdtemp(prefix="bridge-autobind-")
    path = os.path.join(directory, "config.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def make_bridge(config_path: str):
    """造一个不碰网络的 Bridge，派发和回复都替换成记录。"""
    import bridge.app as app_module
    from bridge.app import Bridge, Settings

    settings = Settings.load(config_path)
    bridge = Bridge.__new__(Bridge)
    bridge.settings = settings
    bridge._seen_messages = {}
    bridge._last_event_at = 0.0
    bridge._loop = object()
    # 接入后 app.py 自己会在 __init__ 里建这个；测试里先放好，
    # 这样无论它是在 __init__ 建还是按需建，这个桩都能跑。
    bridge.pairing = PairingGate.load(config_path)

    said: list[tuple[str, str]] = []
    bridge._say = lambda chat_id, text: said.append((chat_id, text))

    dispatched: list = []
    original = app_module.asyncio.run_coroutine_threadsafe

    def fake_dispatch(coro, loop):
        dispatched.append(coro)
        coro.close()
        return None

    app_module.asyncio.run_coroutine_threadsafe = fake_dispatch
    bridge._restore = lambda: setattr(
        app_module.asyncio, "run_coroutine_threadsafe", original
    )
    return bridge, dispatched, said


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------- 那个开关必须彻底消失 ----------


def test_example_config_has_no_autobind_switch():
    """🔴 回归：示例配置里不能再有 autobind_first_sender。

    留着它 = 留着一条「照着示例抄一份就把窗口打开」的路。
    """
    lines = [
        line
        for line in read(EXAMPLE_CONFIG).splitlines()
        if "autobind" in line and not line.lstrip().startswith("#")
    ]
    assert not lines, "示例配置还能打开首条消息绑定：\n" + "\n".join(lines)


def test_example_config_documents_pairing_fields():
    """阳性对照：示例配置得真的给出配对码字段，否则上面那条只是「删干净了」。"""
    text = read(EXAMPLE_CONFIG)
    for key in ("pairing_code", "pairing_issued_at", "pairing_ttl_min", "pairing_max_attempts"):
        assert key in text, f"示例配置缺 {key}"


def test_installers_do_not_write_autobind():
    offenders = []
    for name in ("src/install-mac.sh", "src/install-windows.ps1"):
        path = os.path.join(_ONECLICK, name)
        if not os.path.exists(path):
            continue
        for lineno, line in enumerate(read(path).splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "autobind_first_sender" in line:
                offenders.append(f"{name}:{lineno}")
    assert not offenders, "安装器还在写 autobind_first_sender：\n" + "\n".join(offenders)


# ---------- 接入层：空白名单 = 不执行 ----------


def test_ordinary_first_message_is_not_executed():
    """🔴 这就是原来的洞：随便一句话过去就成主人、还顺带干活。

    现在白名单为空时任何不是码的消息都不执行 —— 不管发的人是谁。
    """
    path = write_config()
    bridge, dispatched, said = make_bridge(path)
    try:
        bridge.on_message(make_event(open_id=STRANGER, text="这个目录里有什么项目？"))
        assert dispatched == [], "白名单为空时不该执行任何活"
        assert bridge.settings.owner_open_ids == frozenset()
        assert "owner_open_ids = []" in read(path), "陌生人不该被写进配置"
    finally:
        bridge._restore()


def test_ordinary_first_message_gets_no_reply():
    """静默丢弃：回「你没权限」就等于向陌生人确认这个机器人背后有东西。"""
    path = write_config()
    bridge, _, said = make_bridge(path)
    try:
        bridge.on_message(make_event(open_id=STRANGER, text="在吗"))
        assert said == [], f"不该回话，却回了 {said}"
    finally:
        bridge._restore()


def test_bot_sender_is_still_dropped():
    """别的机器人发言也会进事件流，哪怕正文是码也不能让它绑。"""
    path = write_config()
    bridge, dispatched, _ = make_bridge(path)
    try:
        bridge.on_message(make_event(open_id=OWNER, sender_type="bot", text=CODE))
        assert dispatched == []
        assert bridge.settings.owner_open_ids == frozenset()
        assert "owner_open_ids = []" in read(path)
    finally:
        bridge._restore()


def test_owner_still_works_after_binding_landed():
    """阳性对照：配置里已经有主人时，他的消息必须照常执行。

    没有这条的话，上面几条「不执行」可能只是因为桩根本跑不到派发那步。
    """
    path = write_config()
    text = read(path).replace("owner_open_ids = []", f'owner_open_ids = ["{OWNER}"]')
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    bridge, dispatched, _ = make_bridge(path)
    try:
        bridge.on_message(make_event(open_id=OWNER, text="看一下 git status"))
        assert len(dispatched) == 1
    finally:
        bridge._restore()


# ---------- 走完整 on_message 的闭环（app.py 已接入）----------


def test_pairing_code_binds_through_on_message():
    path = write_config()
    bridge, dispatched, said = make_bridge(path)
    try:
        bridge.on_message(make_event(open_id=OWNER, text=CODE))
        assert bridge.settings.owner_open_ids == frozenset({OWNER})
        assert f'owner_open_ids = ["{OWNER}"]' in read(path), "必须落盘，重启仍在"
        # 绑定成功要回一条消息，并且**带上示例提示** ——
        # 用户不知道能说什么的话，第一印象就浪费了（竞品都给 suggested prompts）。
        reply = said[0][1]
        assert said and ("绑好了" in reply or "已绑定" in reply), "要回一条消息告诉新主人"
        assert "试试" in reply, "要给示例提示，不能只说一句「直接说要干什么」"
            # 2026-11-06 第 11 轮：指路改成按钮（「❓ 帮助」），不再叫人打 /help
        assert "/cd" in reply or "/help" in reply or "帮助" in reply, "要指路"
        assert dispatched == [], "🔴 码只是个码，不能被当成任务执行"
    finally:
        bridge._restore()


def test_legacy_autobind_key_in_config_is_ignored():
    """老用户的 config.toml 里可能还留着 autobind_first_sender = true。

    重装只会覆盖程序、不会改他那份配置 ⇒ app.py 必须**不再认这个键**，
    否则洞对老用户照样开着。
    """
    path = write_config()
    text = read(path) + "\n"
    text = text.replace(
        'pairing_code = "7KMQ3F"', 'autobind_first_sender = true\npairing_code = ""'
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    bridge, dispatched, _ = make_bridge(path)
    try:
        bridge.on_message(make_event(open_id=STRANGER, text="随便一句话"))
        assert dispatched == [], "老配置里的 autobind 不能再被认"
        assert "owner_open_ids = []" in read(path)
    finally:
        bridge._restore()
