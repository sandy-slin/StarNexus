"""进会话聊天（relay）：送进去 + 取回复 + 幂等 + 发之前再查一遍。

全部基于 `tmp_path` 下的假注册表 / 假 jsonl + 注入的假信使 + 假时钟；
**绝不起真 claude、绝不碰用户现有会话、绝不给任何进程发信号**。

重点盯住的几条：
- 目标不在跑 / 没名字 / 名字重了 / 被 Ctrl-Z 挂起 / 权限档不同类 ⇒ 拒，而且信使**一次都没被叫**
- 「真送达」两种形状都认：`queue-operation/enqueue`（目标正忙时只有这一条）和
  `user/isMeta` 的 `cross-session-message`（目标 idle 时的形状）
- 超时 ⇒ `ok=False` 但 `delivered=True`，文案说「发进去了」；**同一句话不自动重发**
- 落地那行的 `from-mode` 和目标不同类 ⇒ 当「被扣住」，不假装送到了
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import FleetJobs, ProcTable, RunResult, SubprocessRunner  # noqa: E402
from bridge.fleet import relay as relay_mod  # noqa: E402
from bridge.fleet.claude import ClaudeAdapter  # noqa: E402
from bridge.fleet.relay import Courier, Relay  # noqa: E402
from bridge.policy import Policy  # noqa: E402

SID_LIVE = "11111111-1111-4111-8111-111111111111"
SID_NONAME = "22222222-2222-4222-8222-222222222222"
SID_PLAIN = "33333333-3333-4333-8333-333333333333"   # 权限档 default（不同类）
SID_DEAD = "44444444-4444-4444-8444-444444444444"
SID_HUNG = "55555555-5555-4555-8555-555555555555"    # Ctrl-Z 挂起
SID_DUP = "66666666-6666-4666-8666-666666666666"      # 和 SID_LIVE 同名
SID_NOSOCK = "77777777-7777-4777-8777-777777777777"
SID_SDK = "88888888-8888-4888-8888-888888888888"      # 别的脚本起的 claude -p
SID_GONE = "99999999-9999-4999-8999-999999999999"     # 注册表活着但 jsonl 没了

L_LIVE = "Tue Nov  4 19:02:35 2026"
L_NONAME = "Tue Nov  4 19:03:35 2026"
L_PLAIN = "Tue Nov  4 19:04:35 2026"
L_HUNG = "Tue Nov  4 19:05:35 2026"
L_DUP = "Tue Nov  4 19:06:35 2026"
L_NOSOCK = "Tue Nov  4 19:07:35 2026"
L_SDK = "Tue Nov  4 19:08:35 2026"
L_GONE = "Tue Nov  4 19:09:35 2026"
L_COURIER = "Tue Nov  4 20:00:00 2026"

PS_TEXT = f"""\
  101     1 S+   {L_LIVE}    01:00:00 claude
  102     1 S+   {L_NONAME}    01:00:00 claude
  103     1 S+   {L_PLAIN}    01:00:00 claude
  105     1 T    {L_HUNG}    01:00:00 claude
  106     1 S+   {L_DUP}    01:00:00 claude
  107     1 S+   {L_NOSOCK}    01:00:00 claude
  108     1 S    {L_SDK}    01:00:00 claude -p something
  109     1 S+   {L_GONE}    01:00:00 claude
  900     1 S    {L_COURIER}    00:00:10 claude -p --input-format stream-json
"""


# ---------- 假时钟 ----------


class Clock:
    """sleep 直接推进时间；到点的回调就地执行（测试里不真等）。"""

    def __init__(self, start: float = 1_793_800_000.0) -> None:
        self.t = float(start)
        self._due: list = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += max(float(seconds or 0), 0.001)
        self._fire()

    def at(self, delay: float, fn) -> None:
        self._due.append((self.t + float(delay), fn))

    def _fire(self) -> None:
        ready = [(when, fn) for when, fn in self._due if when <= self.t]
        self._due = [(when, fn) for when, fn in self._due if when > self.t]
        for _when, fn in sorted(ready):
            fn()


# ---------- 假 jsonl ----------


def _append(path: str, records) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def enqueue_row(sid: str, text: str, *, name="carher-relay", mode="bypass", sock="/tmp/cc-socks/900.sock"):
    """目标**正忙**时唯一会落的那行（实测形状）。"""
    body = (
        f'<cross-session-message from="uds:{sock}" from-name="{name}" from-mode="{mode}">\n'
        f"{text}\n</cross-session-message>"
    )
    return {"type": "queue-operation", "operation": "enqueue", "sessionId": sid,
            "timestamp": "2026-11-04T12:06:08.467Z", "content": body}


def meta_row(sid: str, text: str, *, name="carher-relay", mode="bypass", sock="/tmp/cc-socks/900.sock"):
    """目标 idle 时的形状：type=user, isMeta=true。"""
    body = (
        "Another Claude session sent a message:\n"
        f'<cross-session-message from="uds:{sock}" from-name="{name}" from-mode="{mode}">\n'
        f"{text}\n</cross-session-message>\n\nThis came from another Claude session — …"
    )
    return {"type": "user", "isMeta": True, "isSidechain": False, "sessionId": sid,
            "message": {"role": "user", "content": body},
            "timestamp": "2026-11-04T12:06:09.000Z"}


def asst_row(text: str, msg_id="m1"):
    return {"type": "assistant", "isSidechain": False,
            "message": {"id": msg_id, "role": "assistant", "content": [{"type": "text", "text": text}]},
            "timestamp": "2026-11-04T12:06:12.000Z"}


def tool_row(msg_id="m0"):
    """只有工具调用、没有文字 ⇒ 不该被当成回复。"""
    return {"type": "assistant", "isSidechain": False,
            "message": {"id": msg_id, "role": "assistant",
                         "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
            "timestamp": "2026-11-04T12:06:10.000Z"}


def mode_row(sid: str, mode: str):
    return {"type": "permission-mode", "permissionMode": mode, "sessionId": sid}


# ---------- 假 runner / 假信使 ----------


class FakeRunner(SubprocessRunner):
    def __init__(self, ps_text=PS_TEXT):
        self.ps_text = ps_text
        self.other = []

    def run(self, argv, *, timeout, cwd=None, env=None, input_text=None, on_spawn=None):
        argv = list(argv)
        if argv and argv[0] == "ps":
            return RunResult(returncode=0, stdout=self.ps_text)
        self.other.append(argv)  # relay 不该走这里起 claude
        return RunResult(returncode=0, stdout="")


class FakeCourier:
    """记下每次 write；按脚本把「送达」/「回复」写进目标 jsonl。"""

    permission_mode = "bypassPermissions"

    def __init__(self, clock: Clock, *, jsonl: str, sid: str, ident=None):
        self.clock = clock
        self.jsonl = jsonl
        self.sid = sid
        self.writes: list = []
        self.closed = 0
        self.ident = {"name": "carher-relay", "socket": "/tmp/cc-socks/900.sock"} if ident is None else ident
        self.fail = ""
        # 脚本：(送达延迟, 送达行的 from-mode 或 None 表示不落地, 回复延迟, 回复文本或 None)
        self.plan = (1.0, "bypass", 2.0, "好，我看一下")
        self.delivery_shape = "enqueue"

    def identity(self, timeout=8.0):
        return dict(self.ident)

    def write(self, to_name, text):
        self.writes.append({"to": to_name, "text": text})
        if self.fail:
            return self.fail
        land_delay, land_mode, reply_delay, reply = self.plan
        if land_mode is not None:
            maker = enqueue_row if self.delivery_shape == "enqueue" else meta_row
            self.clock.at(land_delay, lambda: _append(
                self.jsonl, [maker(self.sid, text, name=self.ident.get("name", ""), mode=land_mode)]))
        if reply is not None:
            rows = [tool_row(), asst_row(reply)] if isinstance(reply, str) else list(reply)
            self.clock.at(land_delay + reply_delay, lambda: _append(self.jsonl, rows))
        return ""

    def close(self):
        self.closed += 1


# ---------- 环境 ----------


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "claude"
    bridge = tmp_path / "bridge"
    work = tmp_path / "work"
    work.mkdir()
    proj = home / "projects" / "-work"

    def reg(pid, sid, lstart, *, name="", entry="cli", sock=True, root=home, **kw):
        rec = {"pid": pid, "sessionId": sid, "cwd": str(work), "procStart": lstart,
               "kind": "interactive", "entrypoint": entry, "status": "idle", "version": "2.1.318"}
        if name:
            rec["name"] = name
            rec["nameSource"] = "user"
        if sock:
            rec["messagingSocketPath"] = f"/tmp/cc-socks/{pid}.sock"
        rec.update(kw)
        path = root / "sessions" / f"{pid}.json"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")

    reg(101, SID_LIVE, L_LIVE, name="修登录")
    reg(102, SID_NONAME, L_NONAME)
    reg(103, SID_PLAIN, L_PLAIN, name="默认档那个")
    reg(105, SID_HUNG, L_HUNG, name="挂起的")
    reg(106, SID_DUP, L_DUP, name="另一个")  # 重名那条测试里改成和 101 一样
    reg(107, SID_NOSOCK, L_NOSOCK, name="老版本", sock=False)
    reg(108, SID_SDK, L_SDK, name="脚本起的", entry="sdk-cli")
    reg(109, SID_GONE, L_GONE, name="记录没了")
    # 死进程留下的注册表：pid 不在 ps 里
    reg(999, SID_DEAD, L_LIVE, name="早关了")
    # 签名密钥文件：绝不能被当成注册表打开
    (home / "sessions" / "101.abcdef.key").write_text("SECRET-DO-NOT-READ", encoding="utf-8")

    for sid, mode in ((SID_LIVE, "bypassPermissions"), (SID_NONAME, "bypassPermissions"),
                      (SID_PLAIN, "default"), (SID_HUNG, "bypassPermissions"),
                      (SID_DUP, "bypassPermissions"), (SID_NOSOCK, "bypassPermissions"),
                      (SID_SDK, "bypassPermissions")):
        _append(str(proj / f"{sid}.jsonl"), [
            {"type": "user", "isSidechain": False, "cwd": str(work), "entrypoint": "cli",
             "message": {"role": "user", "content": "hi"},
             "timestamp": "2026-11-04T11:00:00.000Z"},
            mode_row(sid, mode),
            asst_row("历史上的回复", msg_id="old"),
        ])
    policy = Policy(mode="readonly", workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                    model="claude-sonnet-5-5", turn_timeout_sec=60)
    return {"home": home, "bridge": bridge, "work": work, "proj": proj, "policy": policy, "tmp": tmp_path}


def _jsonl_of(env, sid):
    return str(env["proj"] / f"{sid}.jsonl")


def _build(env, *, sid=SID_LIVE, ps_text=PS_TEXT, kills=None, courier=None, clock=None):
    clock = clock or Clock()
    kills = kills if kills is not None else []
    runner = FakeRunner(ps_text=ps_text)
    table = ProcTable.parse(ps_text)
    jobs = FleetJobs(lstart_of=lambda pid: table.lstart(pid), kill=lambda pid, sig: kills.append((pid, sig)))
    adapter = ClaudeAdapter(
        home=str(env["home"]), bridge_home=str(env["bridge"]), app_root=str(env["tmp"] / "app"),
        jobs=jobs, runner=runner, policy=env["policy"],
    )
    courier = courier if courier is not None else FakeCourier(clock, jsonl=_jsonl_of(env, sid), sid=sid)
    rl = Relay(adapter, courier=courier, jobs=jobs, now=clock.now, sleep=clock.sleep)
    return rl, courier, clock, runner, kills


# ---------- 一、发之前的拒绝（信使一次都没被叫） ----------


def test_refuse_when_target_not_live(env):
    """注册表里还有它，但 pid 不在 ps 里 ⇒ 没开着。"""
    rl, courier, _clock, _runner, kills = _build(env)
    out = rl.send(SID_DEAD, "在吗")
    assert not out.ok and not out.delivered
    assert out.text == relay_mod.WHY_NOT_LIVE
    assert courier.writes == []  # 没起信使、没发任何东西
    assert kills == []


def test_refuse_when_session_unknown(env):
    """id 根本不在注册表里 ⇒ 同一句「没开着」。"""
    rl, courier, *_ = _build(env)
    out = rl.send("abcdabcd-0000-4000-8000-000000000000", "在吗")
    assert not out.ok and out.text == relay_mod.WHY_NOT_LIVE
    assert courier.writes == []


def test_refuse_when_bad_id(env):
    rl, courier, *_ = _build(env)
    out = rl.send("not-a-uuid", "在吗")
    assert not out.ok and out.text == relay_mod.WHY_BAD_ID
    assert courier.writes == []


def test_refuse_when_text_empty(env):
    rl, courier, *_ = _build(env)
    out = rl.send(SID_LIVE, "   ")
    assert not out.ok and out.text == relay_mod.WHY_EMPTY
    assert courier.writes == []


def test_refuse_when_no_name(env):
    """没名字 ⇒ SendMessage 没地址可发。文案要告诉用户去 /name。"""
    rl, courier, *_ = _build(env, sid=SID_NONAME)
    out = rl.send(SID_NONAME, "在吗")
    assert not out.ok and not out.delivered
    assert out.text == relay_mod.WHY_NO_NAME
    assert "/name" in out.text
    assert courier.writes == []


def test_duplicate_name_no_longer_blocks(env):
    """🔴 2026-11-22 行为反转：两个活会话同名**照样能发**。

    现场：主人在群里发 `/goal 看看天气`，回「电脑上有两个会话都叫这个名字，
    发过去可能进错一个。先在电脑上给其中一个改个名」。他的原话是
    「不应该是用 session id 去发送消息吗？」—— 对的：我们手里有这个会话自己的
    `messagingSocketPath`，一对一，压根不会进错。拿名字当地址是弯路，
    重名那道闸是这个弯路的副作用。

    `SendMessage` 的 `to` 收显式地址，bundle 原文（第 361200 行）：
      「, or an explicit `uds:<socket>` / `bridge:<session id>` address.」
    """
    reg = env["home"] / "sessions" / "106.json"
    data = json.loads(reg.read_text(encoding="utf-8"))
    data["name"] = "修登录"  # 和 101 撞名
    reg.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    rl, courier, *_ = _build(env)
    out = rl.send(SID_LIVE, "在吗")
    # 发出去了（送达/回复那几步由别的用例管），关键是**没被重名挡住**
    assert courier.writes, "重名又把它挡住了 —— 地址该用 socket，不是名字"
    assert getattr(relay_mod, "WHY_NAME_DUP", None) is None or \
        out.text != relay_mod.WHY_NAME_DUP


def test_address_is_the_socket_not_the_name(env):
    """🔴 发出去的地址必须是 `uds:<socket>`。

    这是上面那条的**正判据**：只断言「没被挡住」的话，哪天又退回用名字发
    （而恰好没重名）这条照样绿。
    """
    rl, courier, *_ = _build(env)
    rl.send(SID_LIVE, "在吗")
    assert courier.writes, "一条都没发出去"
    to = courier.writes[-1]["to"]
    assert to.startswith("uds:"), f"地址用的不是 socket：{to!r}"
    assert to.endswith(".sock"), to


def test_refuse_when_permission_mode_class_differs(env):
    """目标是 default、信使是 bypass ⇒ 不同类，发过去会被扣住 ⇒ 先不发。"""
    rl, courier, *_ = _build(env, sid=SID_PLAIN)
    out = rl.send(SID_PLAIN, "在吗")
    assert not out.ok and not out.delivered
    assert out.text == relay_mod.WHY_MODE_DIFFERS
    assert courier.writes == []


def test_refuse_when_permission_mode_unreadable(env):
    """jsonl 里读不到 permission-mode ⇒ 不猜，拒。"""
    path = _jsonl_of(env, SID_LIVE)
    rows = [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]
    with open(path, "w", encoding="utf-8") as fh:
        for rec in rows:
            if rec.get("type") != "permission-mode":
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    rl, courier, *_ = _build(env)
    out = rl.check(SID_LIVE)
    assert not out.ok and out.why == relay_mod.WHY_MODE_UNKNOWN
    assert courier.writes == []


def test_refuse_when_suspended(env):
    """ps STAT=T（Ctrl-Z 挂起）⇒ 发进去没人理。"""
    rl, courier, *_ = _build(env, sid=SID_HUNG)
    out = rl.send(SID_HUNG, "在吗")
    assert not out.ok and out.text == relay_mod.WHY_SUSPENDED
    assert courier.writes == []


def test_refuse_when_no_messaging_socket(env):
    """老版本 Claude 没有 messagingSocketPath ⇒ 这条路不通。"""
    rl, courier, *_ = _build(env, sid=SID_NOSOCK)
    out = rl.send(SID_NOSOCK, "在吗")
    assert not out.ok and out.text == relay_mod.WHY_NO_SOCKET
    assert courier.writes == []


def test_refuse_when_not_a_terminal_session(env):
    """别的脚本起的 `claude -p`（entrypoint=sdk-cli）不是终端会话。"""
    rl, courier, *_ = _build(env, sid=SID_SDK)
    out = rl.send(SID_SDK, "在吗")
    assert not out.ok and out.text == relay_mod.WHY_NOT_TERMINAL
    assert courier.writes == []


def test_refuse_when_jsonl_missing(env):
    rl, courier, *_ = _build(env, sid=SID_GONE)
    out = rl.send(SID_GONE, "在吗")
    assert not out.ok and out.text == relay_mod.WHY_NO_RECORD
    assert courier.writes == []


def test_refuse_when_ps_unreadable(env):
    """看不出死活 ⇒ 不发（ps 空表 + error）。"""
    rl, courier, *_ = _build(env, ps_text="")
    rl.adapter._procs = lambda: ProcTable(error="读进程列表超时了")
    out = rl.send(SID_LIVE, "在吗")
    assert not out.ok and out.text.startswith(relay_mod.WHY_PS_FAILED)
    assert courier.writes == []


def test_bridge_own_sessions_are_not_relay_targets(env, tmp_path):
    """桥自己起的会话写在 bridge_home 里，不该成为 relay 的目标。"""
    reg = env["bridge"] / "sessions" / "900.json"
    os.makedirs(os.path.dirname(reg), exist_ok=True)
    reg.write_text(json.dumps({
        "pid": 900, "sessionId": SID_DEAD, "cwd": str(env["work"]), "procStart": L_COURIER,
        "entrypoint": "cli", "name": "桥自己的", "messagingSocketPath": "/tmp/cc-socks/900.sock",
    }), encoding="utf-8")
    rl, courier, *_ = _build(env)
    out = rl.send(SID_DEAD, "在吗")
    assert not out.ok and out.text == relay_mod.WHY_NOT_LIVE
    assert courier.writes == []


def test_check_passes_for_a_clean_target(env):
    rl, _courier, *_ = _build(env)
    out = rl.check(SID_LIVE)
    assert out.ok and out.why == ""
    assert out.target.name == "修登录"
    assert out.target.pid == 101
    assert out.target.mode == "bypassPermissions"
    assert out.target.mode_class == "bypass"
    assert out.target.jsonl == _jsonl_of(env, SID_LIVE)


# ---------- 一·五、身份声明（「这是主人本人说的」）----------
#
# 🔴 用户 2026-11-05 报的：手机上发「hi」，目标回的是「另一个会话『carher-relay』发来一句『hi』，
# 没有具体请求，我没做任何处理」。消息**没发错会话**（日志 + 目标 jsonl 都对得上），
# 错的是身份 —— 会话间消息必然盖上信使的名字，Claude Code 还会在外面套一段
# 「not typed by your user」，目标于是当成同事闲聊，转述一下就完了。
# 能改的只有正文 ⇒ 在最前面说清这是主人的原话。


def test_wire_text_declares_the_owner_and_keeps_the_words():
    wire = relay_mod.wire_text("把测试跑一遍")
    assert wire.endswith("把测试跑一遍"), "主人的原话一字不动，而且在最后（前缀只能加在前面）"
    assert wire != "把测试跑一遍", "没有身份声明 ⇒ 目标会当成别的会话在闲聊"
    head = wire[: -len("把测试跑一遍")]
    assert "主人" in head and "原话" in head


def test_framing_says_act_on_it_not_just_read_it():
    """只说「这是主人发的」不够 —— 目标上次就是「知道了但没做」。要明说让它动手。"""
    assert any(w in relay_mod.OWNER_FRAMING for w in ("来做", "去做", "执行")), \
        relay_mod.OWNER_FRAMING


def test_framing_is_not_an_instruction_the_target_can_mistake_for_payload():
    """声明和正文要分得开：声明用【】括起来，不会和主人自己打的字混在一起。"""
    assert relay_mod.OWNER_FRAMING.startswith("【") and "】" in relay_mod.OWNER_FRAMING
    assert relay_mod.OWNER_FRAMING.endswith("\n")


def test_resend_guard_keys_on_the_owner_words_not_the_wire_text(env):
    """去重键用主人的原话 ⇒ 加了前缀不影响「同一句别发两遍」那道闸。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 0.0, None)
    rl.send(SID_LIVE, "把测试跑一遍", timeout=30)
    again = rl.send(SID_LIVE, "把测试跑一遍", timeout=30)
    assert "已经送进去过了" in again.text
    assert len(courier.writes) == 1


# ---------- 二、送达 + 取回复 ----------


def test_delivery_confirmed_by_enqueue_row_then_reply(env):
    """目标正忙时只落 `queue-operation/enqueue` —— 这就是「真送达」。"""
    rl, courier, _clock, runner, kills = _build(env)
    out = rl.send(SID_LIVE, "看一眼登录那个 bug")
    assert out.ok and out.delivered
    assert out.text == "好，我看一下"
    assert out.session_id == SID_LIVE
    # 发出去的是**带身份声明**的那一份（见 relay_mod.OWNER_FRAMING）：主人的原话一字不动地跟在后面
    write, = courier.writes
    # 🔴 11-22：地址从**名字**换成 `uds:<socket>`（名字会重，socket 一对一）。
    # 见 test_address_is_the_socket_not_the_name 和 relay.Target.address。
    assert write["to"].startswith("uds:") and write["to"].endswith(".sock"), write["to"]
    assert write["text"] == relay_mod.OWNER_FRAMING + "看一眼登录那个 bug"
    assert runner.other == []  # relay 没有自己起过 claude（只走注入的信使）
    assert kills == []         # 一个信号都没发


def test_delivery_confirmed_by_meta_row(env):
    """目标 idle 时的形状（type=user, isMeta=true）也要认。"""
    rl, courier, *_ = _build(env)
    courier.delivery_shape = "meta"
    out = rl.send(SID_LIVE, "在吗")
    assert out.ok and out.delivered and out.text == "好，我看一下"


def test_reply_ignores_tool_calls_and_merges_segments(env):
    """只转文字；多段文字拼起来，工具调用那几行跳过。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 1.0, [tool_row(), asst_row("第一段", "m1"),
                                          tool_row("m2"), asst_row("第二段", "m3")])
    out = rl.send(SID_LIVE, "说两段")
    assert out.ok and out.text == "第一段\n第二段"


def test_only_rows_after_send_count_as_delivery(env):
    """历史上同一句话的落地记录不算这次 —— 只认发之前记下的文件位置之后的新行。"""
    path = _jsonl_of(env, SID_LIVE)
    _append(path, [enqueue_row(SID_LIVE, "在吗"), asst_row("上次的回复", "prev")])
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, None, 0.0, None)  # 这次什么都不落地
    out = rl.send(SID_LIVE, "在吗", delivery_timeout=5)
    assert not out.ok and not out.delivered
    assert out.text == relay_mod.TEXT_NO_CONFIRM


def test_reply_cut_off_when_target_starts_a_new_turn(env):
    """它开始下一轮（又来一条 user）⇒ 这段回复就是全部，不等 settle 窗口。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 1.0, [
        asst_row("就这些"),
        {"type": "user", "isSidechain": False, "message": {"role": "user", "content": "下一句"},
         "timestamp": "2026-11-04T12:07:00.000Z"},
    ])
    out = rl.send(SID_LIVE, "说一句")
    assert out.ok and out.text == "就这些"


def test_reply_of_only_tool_calls_says_so(env):
    """送到了、这一轮没有文字 ⇒ 照实说，不编一句回复。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 1.0, [tool_row(), tool_row("m2")])
    out = rl.send(SID_LIVE, "跑一下测试", timeout=20)
    assert not out.ok and out.delivered
    assert out.text == relay_mod.TEXT_TIMEOUT


def test_delivery_matched_by_body_even_if_identity_unknown(env):
    """读不到信使自己的注册表（新起的有 0.2~1.7s 空窗）⇒ 退回按正文一字不差比对。"""
    rl, courier, *_ = _build(env)
    courier.ident = {}
    courier.plan = (1.0, "bypass", 1.0, "收到")
    out = rl.send(SID_LIVE, "按正文认我")
    assert out.ok and out.text == "收到"


def test_other_peers_message_is_not_mistaken_for_ours(env):
    """别的会话同时给目标发消息 ⇒ 不能当成我们这条的送达。"""
    rl, courier, _clock, *_ = _build(env)
    path = _jsonl_of(env, SID_LIVE)
    courier.plan = (1.0, None, 0.0, None)
    _clock.at(0.5, lambda: _append(path, [
        enqueue_row(SID_LIVE, "别人发的", name="someone-else", sock="/tmp/cc-socks/555.sock")]))
    out = rl.send(SID_LIVE, "我发的", delivery_timeout=5)
    assert not out.ok and not out.delivered and out.text == relay_mod.TEXT_NO_CONFIRM


def test_held_when_landed_from_mode_differs(env):
    """落地了但 `from-mode` 不同类 ⇒ 被扣住等人点，不能说「它收到了」。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "prompting", 1.0, "不该读到这句")
    out = rl.send(SID_LIVE, "在吗")
    assert not out.ok
    assert out.delivered  # 已经在它门口了，用户别重发
    assert out.text == relay_mod.TEXT_HELD


def test_courier_write_failure_is_not_delivered(env):
    rl, courier, *_ = _build(env)
    courier.fail = "信使没把这条写出去：broken pipe"
    out = rl.send(SID_LIVE, "在吗")
    assert not out.ok and not out.delivered
    assert "broken pipe" in out.text


# ---------- 三、超时 + 幂等 ----------


def test_timeout_says_it_was_delivered(env):
    """送到了、它没回 ⇒ ok=False 但 delivered=True，文案必须说「发进去了」。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 0.0, None)  # 落地，但永远不回
    out = rl.send(SID_LIVE, "慢慢想", timeout=30)
    assert not out.ok
    assert out.delivered
    assert out.text == relay_mod.TEXT_TIMEOUT
    assert "发进去了" in out.text and "不会自动重发" in out.text


def test_no_auto_resend_of_the_same_text_after_timeout(env):
    """🔴 同一句话超时之后再发 ⇒ 挡住，信使只被叫过一次。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 0.0, None)
    first = rl.send(SID_LIVE, "把测试跑一遍", timeout=30)
    assert first.delivered and not first.ok
    again = rl.send(SID_LIVE, "把测试跑一遍", timeout=30)
    assert not again.ok
    assert again.delivered  # 它已经在那边了
    assert "已经送进去过了" in again.text
    assert len(courier.writes) == 1  # 真的没发第二遍


def test_resend_guard_is_per_text_not_per_session(env):
    """换一句话照样能发（挡的是「同一句」，不是「这个会话」）。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 0.0, None)
    rl.send(SID_LIVE, "第一句", timeout=30)
    rl.send(SID_LIVE, "第二句", timeout=30)
    assert [w["text"] for w in courier.writes] == [
        relay_mod.OWNER_FRAMING + "第一句", relay_mod.OWNER_FRAMING + "第二句",
    ]


def test_resend_guard_ignores_whitespace_differences(env):
    """「同一句」按压空白之后比：多敲个空格不算新的一句。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, "bypass", 0.0, None)
    rl.send(SID_LIVE, "把测试跑一遍", timeout=30)
    again = rl.send(SID_LIVE, "  把测试跑一遍 ", timeout=30)
    assert "已经送进去过了" in again.text
    assert len(courier.writes) == 1


def test_resend_allowed_after_guard_window(env):
    """过了保护窗口（默认 10 分钟）就可以再说一遍。"""
    rl, courier, clock, *_ = _build(env)
    courier.plan = (1.0, "bypass", 0.0, None)
    rl.send(SID_LIVE, "同一句", timeout=30)
    clock.t += relay_mod.RESEND_GUARD_SEC + 1
    rl.send(SID_LIVE, "同一句", timeout=30)
    assert len(courier.writes) == 2


def test_resend_allowed_after_a_reply_came_back(env):
    """回过了就不是「可能已经在做」，再问一遍同样的话是正常的。"""
    rl, courier, *_ = _build(env)
    out = rl.send(SID_LIVE, "现在几点")
    assert out.ok
    rl.send(SID_LIVE, "现在几点")
    assert len(courier.writes) == 2


def test_identical_resend_after_a_reply_is_reported_as_deduped(env):
    """🔴 实测：接收端 inbox 会把一模一样的重复消息丢掉（发送方收到 delivery notice）。

    这时候「可能没送到、去电脑上看」是错的话 —— 上一条真送到了、也回过了。
    """
    rl, courier, *_ = _build(env)
    first = rl.send(SID_LIVE, "一模一样的一句")
    assert first.ok
    courier.plan = (1.0, None, 0.0, None)  # 这次不落地（被 inbox 丢了）
    again = rl.send(SID_LIVE, "一模一样的一句", delivery_timeout=5)
    assert not again.ok
    assert again.delivered  # 上一条已经在它那儿了
    assert again.text == relay_mod.TEXT_DEDUPED
    assert "一模一样" in again.text and "换个说法" in again.text


def test_a_fresh_text_that_never_lands_is_not_called_deduped(env):
    """没送过的一句话等不到落地 ⇒ 是「没确认」，不是「被去重」。"""
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, None, 0.0, None)
    out = rl.send(SID_LIVE, "头一回说这句", delivery_timeout=5)
    assert out.text == relay_mod.TEXT_NO_CONFIRM and not out.delivered


def test_unconfirmed_send_blocks_identical_resend(env):
    """🔴 交给信使、没等到落地 ⇒ **挡住重发**（2026-11-04 稳定性审查）。

    原来这一格允许再发一遍：那一条可能正在路上，重发会让目标把同一件事做两次。
    挡的话术也不能说「已经送进去了」—— 我们并不知道，所以用单独一句。
    """
    rl, courier, *_ = _build(env)
    courier.plan = (1.0, None, 0.0, None)
    first = rl.send(SID_LIVE, "不确定有没有到", delivery_timeout=5)
    assert first.text == relay_mod.TEXT_NO_CONFIRM
    second = rl.send(SID_LIVE, "不确定有没有到", delivery_timeout=5)
    assert "没等到它收到的记录" in second.text and not second.delivered
    assert len(courier.writes) == 1, "没确认的那条又发了一遍 ⇒ 目标可能做两次"


def test_concurrent_send_on_the_same_relay_is_refused(env):
    """一个信使串行发：上一句还在送的时候，第二句直接告诉用户等一下。

    在信使的 write 里重入 send（真实世界里是另一个线程同时点了发送）。
    """
    rl, courier, _clock, *_ = _build(env)
    seen = []
    original = courier.write

    def reentrant(to_name, text):
        if not seen:
            seen.append(rl.send(SID_LIVE, "插队的"))
        return original(to_name, text)

    courier.write = reentrant
    out = rl.send(SID_LIVE, "第一句")
    assert out.ok
    assert seen and not seen[0].ok
    assert seen[0].text == relay_mod.WHY_BUSY_RELAY


# ---------- 四、停不了 ----------


def test_stop_wording_never_pretends_it_can_stop(env):
    """目标那一轮只能在电脑上按 Esc；这句文案不许暗示手机能停。"""
    assert "Esc" in relay_mod.WHY_STOP_TARGET
    assert "停不了" in relay_mod.WHY_STOP_TARGET


# ---------- 五、判据函数本身 ----------


def test_mode_class_is_binary_and_conservative():
    assert relay_mod.mode_class("bypassPermissions") == "bypass"
    assert relay_mod.mode_class("bypass") == "bypass"
    for other in ("default", "manual", "plan", "acceptEdits", "dontAsk", "auto"):
        assert relay_mod.mode_class(other) == "prompting", other
    assert relay_mod.mode_class("") == ""
    assert relay_mod.mode_class(None) == ""


def test_permission_mode_reads_the_last_one(env):
    """档位在会话里改过 ⇒ 要读到**最后**那条。"""
    path = _jsonl_of(env, SID_LIVE)
    _append(path, [mode_row(SID_LIVE, "plan")])
    assert relay_mod.permission_mode_of(path) == "plan"
    _append(path, [mode_row(SID_LIVE, "bypassPermissions")])
    assert relay_mod.permission_mode_of(path) == "bypassPermissions"


def test_permission_mode_of_missing_file_is_empty(tmp_path):
    assert relay_mod.permission_mode_of(str(tmp_path / "nope.jsonl")) == ""


def test_delivery_row_recognizes_both_shapes():
    enq = relay_mod._delivery_row(enqueue_row(SID_LIVE, "正文"))
    assert enq["body"].strip() == "正文" and enq["from_mode"] == "bypass"
    meta = relay_mod._delivery_row(meta_row(SID_LIVE, "正文"))
    assert meta["body"].strip() == "正文" and meta["from_name"] == "carher-relay"
    # 别的行一律不是送达
    assert relay_mod._delivery_row(asst_row("hi")) is None
    assert relay_mod._delivery_row({"type": "queue-operation", "operation": "enqueue",
                                     "content": "<task-notification>x</task-notification>"}) is None
    assert relay_mod._delivery_row({"type": "user", "message": {"content": "普通的话"}}) is None


def test_read_from_leaves_half_written_lines_for_next_round(tmp_path):
    """jsonl 一行写一半很常见 ⇒ 半截行要留到下一轮，不许解析失败就丢。"""
    path = tmp_path / "x.jsonl"
    path.write_text(json.dumps({"type": "a"}) + "\n" + '{"type": "b"', encoding="utf-8")
    recs, offset = relay_mod._read_from(str(path), 0)
    assert [r["type"] for r in recs] == ["a"]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("}\n")
    recs2, _ = relay_mod._read_from(str(path), offset)
    assert [r["type"] for r in recs2] == ["b"]


def test_read_from_handles_a_shrinking_file(tmp_path):
    """文件被换掉（变短）⇒ 跳到现在的末尾，不回头整文件读（这边有 200MB 的 jsonl）。"""
    path = tmp_path / "x.jsonl"
    path.write_text("x" * 100, encoding="utf-8")
    recs, offset = relay_mod._read_from(str(path), 500)
    assert recs == [] and offset == 100


# ---------- 六、信使本身（红线） ----------


class FakeProc:
    """假子进程：记下写进 stdin 的行，kill 只记一笔。"""

    def __init__(self, pid=900):
        self.pid = pid
        self.lines: list = []
        self.killed = 0
        self.waited = 0
        self._alive = True
        self.stdin = self
        self.stdout = []
        self.stderr = []

    # stdin 的样子
    def write(self, text):
        self.lines.append(text)

    def flush(self):
        pass

    def close(self):
        pass

    def poll(self):
        return None if self._alive else 0

    def kill(self):
        self.killed += 1
        self._alive = False

    def wait(self, timeout=None):
        self.waited += 1
        return 0


def _courier(env, clock=None, *, pid=900, kills=None):
    clock = clock or Clock()
    kills = kills if kills is not None else []
    table = ProcTable.parse(PS_TEXT)
    jobs = FleetJobs(lstart_of=lambda p: table.lstart(p), kill=lambda p, s: kills.append((p, s)))
    spawned: list = []

    def spawn(argv, cwd, envv):
        proc = FakeProc(pid)
        spawned.append({"argv": list(argv), "cwd": cwd, "env": dict(envv), "proc": proc})
        return proc

    c = Courier(home=str(env["home"]), cwd=str(env["tmp"] / "relay"), jobs=jobs,
                spawn=spawn, now=clock.now, sleep=clock.sleep)
    return c, spawned, jobs, kills, clock


def test_courier_argv_is_the_measured_shape(env):
    """实测最快那条路：常驻 stream-json + haiku + 只给 SendMessage。"""
    c, spawned, *_ = _courier(env)
    assert c.ensure() == ""
    argv = spawned[0]["argv"]
    assert argv[:2] == ["claude", "-p"]
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    i = argv.index("--tools")
    tools = []
    for a in argv[i + 1:]:
        if a.startswith("--"):
            break
        tools.append(a)
    # 🔴 只给 SendMessage。ListAgents 能枚举用户其他会话的名字 —— 越狱文本让信使改投时
    # 正好用得上，所以不给（2026-11-04 安全审查）。
    assert tools == ["SendMessage"]
    # 用户的 settings（allow 规则 / defaultMode / hooks）一个都不加载
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    # `--bare` 会把 SendMessage 一起关掉（实测），绝不能出现
    assert "--bare" not in argv
    assert "--resume" not in argv  # relay 不 resume 目标会话（那条路会分叉）


def test_courier_env_points_at_the_targets_config_dir(env):
    """🔴 CLAUDE_CONFIG_DIR 指错 ⇒ SendMessage 看不见用户的会话，一个都找不到。"""
    c, spawned, *_ = _courier(env)
    c.ensure()
    got = spawned[0]["env"]
    assert got["CLAUDE_CONFIG_DIR"] == str(env["home"])
    assert str(env["bridge"]) not in got["CLAUDE_CONFIG_DIR"]
    assert "CARHER_APPROVAL_DIR" not in got  # 信使没有审批钩子，别继承


def test_courier_registers_itself_so_stop_can_tell_it_apart(env):
    c, spawned, jobs, *_ = _courier(env)
    c.ensure()
    assert c.pid == 900
    job = jobs.get(900)
    assert job is not None and job.tool == "claude" and job.session_id == "relay-courier"


def test_courier_reuses_one_process(env):
    """常驻：多条消息走同一个进程（这正是 10.6s → 4.8s 的来源）。"""
    c, spawned, *_ = _courier(env)
    assert c.write("修登录", "第一句") == ""
    assert c.write("修登录", "第二句") == ""
    assert len(spawned) == 1
    lines = [json.loads(l) for l in spawned[0]["proc"].lines]
    assert len(lines) == 2
    assert all(rec["type"] == "user" for rec in lines)
    assert "第一句" in lines[0]["message"]["content"]
    assert "第二句" in lines[1]["message"]["content"]


def test_courier_turn_carries_target_and_text_verbatim(env):
    """喂给信使的那一轮里，`to` 和 `message` 必须是原文（它只负责照抄）。"""
    c, spawned, *_ = _courier(env)
    c.write("修登录", '带引号的"一句"和\n换行')
    content = json.loads(spawned[0]["proc"].lines[0])["message"]["content"]
    payload = json.loads(content[content.index("{"):])
    assert payload == {"to": "修登录", "message": '带引号的"一句"和\n换行'}


def test_courier_close_only_touches_its_own_pid(env):
    """🔴 只杀自己 Popen 出来的那个进程；不按名字杀、不给别人发信号。"""
    c, spawned, jobs, kills, _clock = _courier(env)
    c.ensure()
    proc = spawned[0]["proc"]
    c.close()
    assert proc.killed == 1
    assert kills == []           # FleetJobs.signal 没被用到（close 直接 kill 自己的 Popen）
    assert jobs.get(900) is None  # 登记也清掉了
    assert c.pid == 0


def test_courier_restarts_after_idle_ttl(env):
    """空闲太久就收掉，不在用户机器上留常驻进程。"""
    c, spawned, *_, clock = _courier(env)
    c.idle_ttl = 60
    c.write("修登录", "a")
    clock.t += 61
    c.write("修登录", "b")
    assert len(spawned) == 2
    assert spawned[0]["proc"].killed == 1


def test_courier_identity_waits_for_the_registry_then_reads_name(env):
    """新起的 claude 要 0.2~1.7s 才写 sessions/<pid>.json ⇒ 轮询一小会儿。"""
    c, spawned, *_, clock = _courier(env)
    c.ensure()
    path = env["home"] / "sessions" / "900.json"
    clock.at(0.5, lambda: path.write_text(json.dumps({
        "pid": 900, "sessionId": SID_DEAD, "name": "carher-relay",
        "messagingSocketPath": "/tmp/cc-socks/900.sock",
    }), encoding="utf-8"))
    ident = c.identity(timeout=5)
    assert ident["name"] == "carher-relay"
    assert ident["socket"] == "/tmp/cc-socks/900.sock"


def test_courier_identity_gives_up_without_blocking_forever(env):
    c, *_ = _courier(env)
    c.ensure()
    assert c.identity(timeout=2) == {}


def test_courier_spawn_failure_is_a_sentence_not_an_exception(env, caplog):
    clock = Clock()
    table = ProcTable.parse(PS_TEXT)
    jobs = FleetJobs(lstart_of=lambda p: table.lstart(p), kill=lambda p, s: None)

    def boom(argv, cwd, envv):
        raise OSError("command not found: claude")

    c = Courier(home=str(env["home"]), cwd=str(env["tmp"] / "relay"), jobs=jobs,
                spawn=boom, now=clock.now, sleep=clock.sleep)
    with caplog.at_level("ERROR", logger="feishu-bridge.fleet"):
        why = c.write("修登录", "在吗")
    # 第 11 轮：原文（可能带路径）只进日志
    assert "没能启动 claude" in why and "command not found" not in why
    assert "command not found" in caplog.text


def test_relay_close_closes_the_courier(env):
    rl, courier, *_ = _build(env)
    rl.close()
    assert courier.closed == 1


def test_relay_never_signals_anything(env):
    """整条路跑完（成功、超时、被拒三种）都不许发一个信号。"""
    rl, courier, _clock, _runner, kills = _build(env)
    rl.send(SID_LIVE, "成功的一句")
    courier.plan = (1.0, "bypass", 0.0, None)
    rl.send(SID_LIVE, "超时的一句", timeout=20)
    rl.send(SID_DEAD, "被拒的一句")
    assert kills == []


def test_courier_cannot_enumerate_other_sessions(env):
    """🔴 信使没有 ListAgents ⇒ 就算被正文说服要改投，它也不知道别的会话叫什么。"""
    rl, *_ = _build(env)
    argv = rl.courier._argv() if hasattr(rl.courier, "_argv") else None
    if argv is None:
        import inspect
        argv = inspect.getsource(type(rl.courier))
        assert "ListAgents" not in argv
        return
    assert "ListAgents" not in argv


def test_delivery_requires_the_target_own_jsonl(env):
    """送达只认**这个** sid 的 jsonl 里命中 —— 信使改投到别的会话时等不到落地，
    用户看到的是「没等到它收到的记录」，不会被当成成功。"""
    import inspect

    src = inspect.getsource(relay_mod.Relay._wait_delivery)
    assert "_is_ours" in src
    src2 = inspect.getsource(relay_mod.Relay._send_locked)
    assert "target.jsonl" in src2, "送达判据没有绑定目标自己的记录文件"


def test_framing_asks_for_a_phone_friendly_reply():
    """🔴 2026-11-06：主人在手机上看 ⇒ 要它先给结论、回短一点（Happy #726、cc-connect #85 用户原话抱怨）。"""
    assert "手机" in relay_mod.OWNER_FRAMING
    assert "先给结论" in relay_mod.OWNER_FRAMING
