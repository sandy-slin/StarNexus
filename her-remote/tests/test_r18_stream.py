"""第 18 轮：流式回答（用户催过多次的那件事）。

判据都落在「坏了不许拖垮这一轮」和「中间帧不许漏凭据」上 —— 流式是增益，
缺权限/限频/接口变了都只该让它安静退回「跑完发一条」。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge.stream import MAX_BODY, MIN_CHARS, StreamReply, hold_tail


class _Resp:
    def __init__(self, ok=True, code=0, data=None):
        self._ok, self.code, self.data = ok, code, data

    def success(self):
        return self._ok


class _FakeClient:
    """按「哪一步失败」造场景。记下每次调用，便于断言序号和次数。"""

    def __init__(self, fail_at=None, code=0, clock=None):
        self.fail_at = fail_at
        self.code = code
        self.calls = []
        # 第 19 轮：记下每一帧**推上去的正文**和每次 settings 的原文，断言卡上最后长什么样
        self.bodies = []
        self.settings_sent = []
        # 每次 content 调用依次弹一个返回码（0 = 成功），弹完就一直成功。造「终态被限频」用
        self.content_codes = []
        # 拒绝带 summary 的 settings（「改预览」这个字段没在真飞书上验过 ⇒ 被拒时要能退回）
        self.reject_summary = False
        # 每次卡片调用的时刻（假时钟），算「任意 1 秒内几次」
        self._clock = clock
        self.stamps = []

    def _stamp(self):
        if self._clock is not None:
            self.stamps.append(self._clock())

    def _resp(self, step, data=None):
        self.calls.append(step)
        if self.fail_at == step:
            return _Resp(False, self.code or 99991672)
        return _Resp(True, 0, data)

    # cardkit
    @property
    def cardkit(self):
        outer = self

        class _CardElement:
            def content(self, request):
                outer._stamp()
                outer.calls.append(("content", request.request_body.sequence))
                if outer.fail_at == "content":
                    return _Resp(False, outer.code or 500)
                code = outer.content_codes.pop(0) if outer.content_codes else 0
                if code:
                    return _Resp(False, code)
                outer.bodies.append(request.request_body.content)
                return _Resp(True)

        class _Card:
            def create(self, request):
                return outer._resp("create", type("D", (), {"card_id": "card_1"})())

            def settings(self, request):
                outer._stamp()
                outer.calls.append(("settings", request.request_body.sequence))
                raw = request.request_body.settings
                if outer.reject_summary and "summary" in raw:
                    return _Resp(False, 10002)
                outer.settings_sent.append(raw)
                return _Resp(outer.fail_at != "settings")

        class _V1:
            card = _Card()
            card_element = _CardElement()

        return type("CK", (), {"v1": _V1()})()

    @property
    def im(self):
        outer = self

        class _Message:
            def create(self, request):
                return outer._resp("send", type("D", (), {"message_id": "om_1"})())

        class _V1:
            message = _Message()

        return type("IM", (), {"v1": _V1()})()


def _prose(n: int) -> str:
    """造 n 个字左右的**带标点**中文。

    🔴 第 19 轮：扣尾改成「从最后一个分隔处之后全扣」之后，原来用的 `"写" * 40`
    （40 个字、一个标点都没有）会被整段扣住 ⇒ 一帧都推不出去 ⇒ 3 条测试红。
    那不是代码错，是 fixture 不像真话：真实中文大约每 8~15 个字就有一个逗号或句号。
    """
    unit = "这一段在说明，"
    return (unit * (n // len(unit) + 1))[:n]


def _run(coro):
    return asyncio.run(coro)


# ---------- 坏了要安静退回 ----------

def test_no_permission_breaks_quietly():
    """🔴 老用户大概率没 cardkit 权限 ⇒ 建卡被拒 ⇒ broken，调用方回到老路。"""
    s = StreamReply(_FakeClient(fail_at="create"), "oc_1")
    assert _run(s.start()) is False
    assert s.broken is True
    assert s.live is False


def test_send_failure_breaks_quietly():
    s = StreamReply(_FakeClient(fail_at="send"), "oc_1")
    assert _run(s.start()) is False
    assert s.broken is True


def test_disabled_never_touches_the_api():
    s = StreamReply(_FakeClient(), "oc_1", enabled=False)
    assert _run(s.start()) is False
    assert s.broken is True
    assert _run(s.push("x" * 100)) is False


def test_push_failure_breaks_but_does_not_raise():
    client = _FakeClient(fail_at="content", code=500)
    s = StreamReply(client, "oc_1", now=lambda: 100.0)
    assert _run(s.start()) is True
    assert _run(s.push(_prose(50))) is False
    assert s.broken is True


@pytest.mark.parametrize("code", [230020, 200810])
def test_rate_limit_skips_the_frame_without_breaking(code):
    """限频 / 用户正在点卡片 ⇒ 跳这一帧，**不许**降级（下一帧带全量追上）。"""
    client = _FakeClient(fail_at="content", code=code)
    s = StreamReply(client, "oc_1", now=lambda: 100.0)
    assert _run(s.start()) is True
    _run(s.push(_prose(50)))
    assert s.broken is False, "限频不该让整轮退回老路"


# ---------- 节流 ----------

def test_throttle_needs_enough_new_text():
    clock = {"t": 100.0}
    s = StreamReply(_FakeClient(), "oc_1", now=lambda: clock["t"])
    _run(s.start())
    assert s.should_push("短") is False, "才几个字就推 ⇒ 刷屏"
    assert s.should_push("写" * (MIN_CHARS + 1)) is True


def test_throttle_respects_the_interval():
    clock = {"t": 100.0}
    s = StreamReply(_FakeClient(), "oc_1", now=lambda: clock["t"])
    _run(s.start())
    assert _run(s.push(_prose(40))) is True
    assert s.should_push(_prose(80)) is False, "同一秒内连推两帧"
    clock["t"] += 1.0
    assert s.should_push(_prose(80)) is True


def test_paragraph_end_pushes_early():
    """段末提前推一次 —— 手感更像在写。"""
    s = StreamReply(_FakeClient(), "oc_1", now=lambda: 100.0)
    _run(s.start())
    assert s.should_push("一句话。\n") is True


def test_no_growth_no_push():
    s = StreamReply(_FakeClient(), "oc_1", now=lambda: 100.0)
    _run(s.start())
    _run(s.push(_prose(40)))
    assert s.should_push(_prose(40)) is False


# ---------- 🔴 中间帧不许漏凭据 ----------

def test_frames_never_leak_a_credential_prefix():
    from bridge.redact import redact

    full = "密钥是 " + "sk-" + "proj-" + "A" * 40
    secret = full.split()[-1]
    for i in range(1, len(full) + 1):
        frame = hold_tail(full[:i])
        got = redact(frame)
        shown = got[0] if isinstance(got, (tuple, list)) else got
        assert not (secret[:6] in frame and shown == frame), f"第 {i} 帧带着凭据片段"


def test_body_is_redacted_before_pushing():
    from bridge.stream import _safe_body

    body = _safe_body("凭据 " + "AKIA" + "IOSFODNN7EXAMPLE")
    assert "AKIA" not in body or "已脱敏" in body


def test_oversized_body_is_cut():
    from bridge.stream import _safe_body

    assert len(_safe_body("写" * (MAX_BODY + 500))) <= MAX_BODY + 40


# ---------- 序号：不能撞 ----------

def test_sequence_always_increases():
    client = _FakeClient()
    clock = {"t": 100.0}
    s = StreamReply(client, "oc_1", now=lambda: clock["t"])
    _run(s.start())
    for i in range(4):
        clock["t"] += 1.0
        _run(s.push("写" * (40 * (i + 1))))
    _run(s.finish("写" * 40))
    seqs = [c[1] for c in client.calls if isinstance(c, tuple)]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs)), f"序号撞了：{seqs}"


# ---------- 收尾 ----------

def test_finish_rewrites_the_full_text_and_closes():
    client = _FakeClient()
    s = StreamReply(client, "oc_1", now=lambda: 100.0)
    _run(s.start())
    assert _run(s.finish("最终全文")) is True
    assert any(c[0] == "settings" for c in client.calls if isinstance(c, tuple))


def test_finish_gives_up_when_text_is_too_long():
    """装不下 ⇒ False，让调用方照旧用消息发全文（宁可没有打字效果，也不能丢正文）。"""
    s = StreamReply(_FakeClient(), "oc_1", now=lambda: 100.0)
    _run(s.start())
    assert _run(s.finish("写" * (MAX_BODY + 1))) is False


def test_finish_on_a_broken_card_is_false():
    s = StreamReply(_FakeClient(fail_at="create"), "oc_1")
    _run(s.start())
    assert _run(s.finish("x")) is False


# ======================================================================
# 第 19 轮 S1：流式卡和别的东西并存时（实测见 docs/iteration/r19-stream.md）
# ======================================================================

import json  # noqa: E402
import stat  # noqa: E402
import tempfile  # noqa: E402

from bridge import stream as stream_mod  # noqa: E402
from bridge.stream import MAX_BYTES, MIN_GAP, _safe_final, _size  # noqa: E402


def _clocked(**kw):
    clock = {"t": 100.0}
    client = _FakeClient(clock=lambda: clock["t"], **kw)
    s = StreamReply(client, "oc_1", now=lambda: clock["t"])
    s._final_retry_delays = (0.0, 0.0, 0.0)     # 测试里不真睡
    return clock, client, s


def _max_per_second(stamps):
    return max((sum(1 for u in stamps if t <= u < t + 1.0) for t in stamps), default=0)


# ---------- §2 工具调用穿插：用真 runner 读一段「先说 → 干活 → 再说」的事件流 ----------

def _delta(text):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                              "delta": {"type": "text_delta", "text": text}}}


def _tool_json(part):
    return {"type": "stream_event", "event": {"type": "content_block_delta", "index": 1,
                                              "delta": {"type": "input_json_delta",
                                                        "partial_json": part}}}


def _text_block(chunks):
    out = [{"type": "stream_event", "event": {"type": "content_block_start",
                                              "content_block": {"type": "text", "text": ""}}}]
    out += [_delta(c) for c in chunks]
    out.append({"type": "assistant", "message": {"content": [{"type": "text",
                                                               "text": "".join(chunks)}]}})
    out.append({"type": "stream_event", "event": {"type": "content_block_stop"}})
    return out


def _tool_block(name, args):
    raw = json.dumps(args, ensure_ascii=False)
    out = [{"type": "stream_event", "event": {"type": "content_block_start",
                                              "content_block": {"type": "tool_use", "name": name}}}]
    out += [_tool_json(raw[i:i + 6]) for i in range(0, len(raw), 6)]
    out.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name,
                                                               "input": args}]}})
    out.append({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
    return out


# 形状照实测那一轮（r19-stream.md §2：text 9 段 → 3 次工具 → text 7 段 → 1 次工具 → text 56 段）
_BLOCK_A = ["我先列目录，", "再读 notes.txt，", "最后看一下日期并总结。"]
_BLOCK_B = ["看到了：", "notes.txt 只有三行，", "说明这是个测试目录。"]
_BLOCK_C = ["这个目录很小，", "只有两个文本文件。\n\n", "当前时间是 20:17，", "和文件的创建时间一致。\n\n",
            "要看 todo.txt 的话告诉我。"]


def _fake_claude(tmp):
    events = (_text_block(_BLOCK_A)
              + _tool_block("Bash", {"command": "ls -la"})
              + _tool_block("Read", {"file_path": "notes.txt"})
              + _text_block(_BLOCK_B)
              + _tool_block("Bash", {"command": "date"})
              + _text_block(_BLOCK_C)
              + [{"type": "result", "subtype": "success", "stop_reason": "end_turn",
                  "session_id": "s1"}])
    data = os.path.join(tmp, "events.jsonl")
    with open(data, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")
    exe = os.path.join(tmp, "claude")
    with open(exe, "w") as fh:
        fh.write(f"#!{sys.executable}\nimport sys\nsys.stdout.write(open({data!r}, encoding='utf-8').read())\n")
    os.chmod(exe, os.stat(exe).st_mode | stat.S_IEXEC)


def test_tool_calls_in_between_never_wipe_what_was_already_said(monkeypatch):
    """🔴 一轮里有三段话、中间夹着工具调用：卡上**每一帧都是下一帧的前缀**（前面说的不会被清掉），
    工具参数一个字都不上卡，终态 == runner 交出的全文。真跑 `ClaudeRunner.run_turn`。"""
    from bridge.policy import Policy
    from bridge.runner import ClaudeRunner

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        _fake_claude(tmp)
        monkeypatch.setenv("PATH", tmp + os.pathsep + os.environ.get("PATH", ""))
        policy = Policy(mode="trusted", workspaces=(tmp,), default_workspace=tmp,
                        model="claude-sonnet-5-5", turn_timeout_sec=30)
        seen = []
        result = _run(ClaudeRunner(policy, config_dir=tmp).run_turn(
            "x", tmp, on_text=seen.append))

    assert result.ok and result.text, result
    clock, client, s = _clocked()
    _run(s.start())
    for full in seen:
        clock["t"] += 1.0
        _run(s.push(full))
    assert _run(s.finish(result.text)) is True

    frames = client.bodies
    assert len(frames) >= 3, f"三段话只推了 {len(frames)} 帧：{frames}"
    for a, b in zip(frames, frames[1:]):
        assert b.startswith(a), f"后一帧把前面说的清掉了：{a!r} → {b!r}"
    assert not any("command" in f or "file_path" in f for f in frames), "工具参数推上了卡"
    assert frames[-1] == result.text
    for block in (_BLOCK_A, _BLOCK_B, _BLOCK_C):
        assert "".join(block).strip() in frames[-1]


# ---------- §4 长答案 ----------

def test_overflow_card_ends_on_its_opening_plus_a_pointer_not_a_half_frame():
    """🔴 装不下 ⇒ finish 返回 False（调用方另发全文），而卡上最后一帧必须是
    「开头 + 指向下面那条消息」，不能停在流到一半的中间帧上。"""
    clock, client, s = _clocked()
    _run(s.start())
    para = "远程看电脑要先想清楚需求，再选工具。" * 5 + "\n\n"
    full = ""
    while len(full) < MAX_BODY + 2000:
        full += para
        clock["t"] += 1.0
        _run(s.push(full))
    assert _run(s.finish(full)) is False
    last = client.bodies[-1]
    assert last.endswith(stream_mod._NOTE_FINAL_OVERFLOW), "卡上没告诉他全文在下面"
    head = last[: -len(stream_mod._NOTE_FINAL_OVERFLOW)].rstrip()
    assert head and full.startswith(head), "卡上留的不是开头"
    assert any('"streaming_mode": false' in raw for raw in client.settings_sent), "装不下也要关流"


@pytest.mark.parametrize("text", [
    "写字，" * 3000,                        # 9000 个字符：字数没超 MAX_BODY，字节 27KB 超 MAX_BYTES
    ("代码\n" + "x = \"a\\b\"\n" * 2500),     # 转义后变长
])
def test_every_frame_fits_the_30kb_card_limit(text):
    """🔴 飞书限的是 30KB，中文 3 字节一个：只按字数限会把 36KB 的帧推上去（实测 35527 字节）。"""
    clock, client, s = _clocked()
    _run(s.start())
    for i in range(200, len(text) + 200, 400):
        clock["t"] += 1.0
        _run(s.push(text[:i]))
    _run(s.finish(text))
    worst = max(_size(b) for b in client.bodies)
    assert worst <= MAX_BYTES < 30 * 1024, f"有一帧 {worst} 字节"


def test_final_that_fits_by_chars_but_not_bytes_falls_back_to_a_message():
    clock, client, s = _clocked()
    _run(s.start())
    text = "写字，" * 3000
    assert len(text) <= MAX_BODY and _size(text) > MAX_BYTES     # 前提
    assert _run(s.finish(text)) is False, "36KB 的正文说成「卡片交付了」⇒ 飞书拒掉就没人发全文"


# ---------- §5 限频 ----------

def test_worst_case_short_english_paragraphs_stay_under_the_card_rate_limit():
    """🔴 纯英文、每段一句、段末 \\n\\n：段末捷径原来绕过 0.6 秒节流，
    轮询一密就是 22 次/秒（单卡硬限 10 次/秒）。含收尾的两发，任意 1 秒都要 ≤ 10。"""
    clock, client, s = _clocked()
    _run(s.start())
    text = ""
    for n in range(400):
        clock["t"] += 0.01                       # 轮询 10ms 一次
        if n % 5 == 0:
            text += "Keep your desk clear today.\n\n"   # 每 50ms 一段
        _run(s.push(text))
    _run(s.finish(text))
    assert _max_per_second(client.stamps) <= 10, f"任意 1 秒最多 {_max_per_second(client.stamps)} 次"
    assert client.bodies[-1] == text


def test_paragraph_end_still_pushes_early_once_the_gap_has_passed():
    """阳性对照：硬下限不能把「段末提前推」整个弄没。"""
    clock, client, s = _clocked()
    _run(s.start())
    assert _run(s.push("第一段说完了。\n")) is True
    clock["t"] += MIN_GAP + 0.01
    assert _run(s.push("第一段说完了。\n第二句。\n")) is True, "过了硬下限，段末还是该推"


def test_a_frame_that_shows_nothing_new_is_not_sent():
    """新长出来的字全被扣尾扣住 ⇒ 推上去也是同一段 ⇒ 不发（实测一轮里出现过 1 次）。"""
    clock, client, s = _clocked()
    _run(s.start())
    base = "我先看一下目录，确认文件都在，再读那份笔记，"
    assert _run(s.push(base + "然后再说。")) is True
    sent = len(client.bodies)
    clock["t"] += 1.0
    _run(s.push(base + "然后再说。" + "sk_live_" + "Z" * 30))
    assert len(client.bodies) == sent, "同一段话又推了一次"


def test_a_rate_limited_frame_is_not_counted_as_shown():
    clock, client, s = _clocked()
    client.content_codes = [230020]
    _run(s.start())
    text = "这一段在说明，" * 5
    assert _run(s.push(text)) is False
    assert s.pushes == 0 and s.broken is False
    assert _run(s.push(text)) is True, "被跳过的那帧，下一拍同样的全文要能补上"


def test_final_frame_rate_limited_is_retried_not_reported_as_delivered():
    """🔴 终态被限频：原来 `_put_content` 当成功 ⇒ finish 报「交付了」⇒ app 不再发消息 ⇒
    卡上停在扣过尾的中间帧。终态要重试，重试仍不行就返回 False 让调用方发消息。"""
    clock, client, s = _clocked()
    _run(s.start())
    client.content_codes = [230020, 200810]
    assert _run(s.finish("All done. See README.md")) is True
    assert client.bodies[-1] == "All done. See README.md"

    clock, client, s = _clocked()
    _run(s.start())
    client.content_codes = [230020] * 10
    assert _run(s.finish("All done. See README.md")) is False


# ---------- §1/§3 收尾：卡不许停在「正在回答」 ----------

def test_closing_replaces_the_chat_list_preview():
    """飞书文档：「关闭流式更新模式并不会改变开发者自定义的摘要文本」⇒ 建卡时那句
    「正在回答…」会一直挂在聊天列表里，收尾时必须换掉（换成正文开头）。"""
    clock, client, s = _clocked()
    _run(s.start())
    _run(s.finish("答案：测试都过了。\n\n细节在下面。"))
    closing = [json.loads(raw)["config"] for raw in client.settings_sent]
    assert closing and closing[-1]["streaming_mode"] is False
    preview = closing[-1].get("summary", {}).get("content", "")
    assert preview and preview in "答案：测试都过了。", preview


def test_closing_still_happens_when_the_preview_field_is_rejected():
    """改预览没在真飞书上验过 ⇒ 被拒时退回只关流，关流本身不能丢。"""
    clock, client, s = _clocked()
    client.reject_summary = True
    _run(s.start())
    assert _run(s.finish("答案。")) is True
    assert any(json.loads(raw)["config"] == {"streaming_mode": False}
               for raw in client.settings_sent), client.settings_sent


def test_empty_final_keeps_what_was_shown_and_closes():
    """被停下 / 没跑完 / 等确认时 `result.text` 可能是空的：卡上已经显示的那段要留着、
    补上调用方给的那句、关流 —— 不能变成空卡，也不能一直停在「正在回答」。"""
    clock, client, s = _clocked()
    _run(s.start())
    _run(s.push("我先说一下打算，然后动手，改完再跑一遍测试给你看。\n"))
    shown = client.bodies[-1]
    assert _run(s.finish("", footer="FOOTER-X")) is True
    assert client.bodies[-1].startswith(shown) and "FOOTER-X" in client.bodies[-1]
    assert json.loads(client.settings_sent[-1])["config"]["streaming_mode"] is False


def test_finish_does_not_rewrite_an_identical_final_frame():
    clock, client, s = _clocked()
    _run(s.start())
    _run(s.push("全文就这一句。\n"))
    before = len(client.bodies)
    assert _run(s.finish("全文就这一句。\n")) is True
    assert len(client.bodies) == before, "卡上已经是全文，还写了一遍"


def test_final_text_is_still_redacted_on_overflow():
    out = _safe_final("密钥是 " + "sk-" + "proj-" + "A" * 40 + "，" + "写字，" * 5000)
    assert "AAAAAAAA" not in out and _size(out) <= MAX_BYTES


def test_idle_sec_reports_how_long_the_card_has_been_still():
    """给 app.py 判「回答卡还在动 ⇒ 状态卡先别冒出来」用。"""
    clock, client, s = _clocked()
    assert s.idle_sec() is None
    _run(s.start())
    assert s.idle_sec() is None, "一帧都没推过不算「在动」"
    _run(s.push("这一段在说明，" * 5))
    clock["t"] += 7.0
    assert s.idle_sec() == pytest.approx(7.0)


def test_text_blocks_split_by_tools_are_not_glued_together(monkeypatch):
    """第 19 轮 S1-A5：工具调用前后两段话原来直接粘成「…并总结。看到了：…」。
    流式累加和最终文本**同一个分隔**（不然终态重写时卡上的字会整体跳一下）。"""
    from bridge.policy import Policy
    from bridge.runner import ClaudeRunner

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        _fake_claude(tmp)
        monkeypatch.setenv("PATH", tmp + os.pathsep + os.environ.get("PATH", ""))
        policy = Policy(mode="trusted", workspaces=(tmp,), default_workspace=tmp,
                        model="claude-sonnet-5-5", turn_timeout_sec=30)
        seen = []
        result = _run(ClaudeRunner(policy, config_dir=tmp).run_turn(
            "x", tmp, on_text=seen.append))

    a, b = "".join(_BLOCK_A), "".join(_BLOCK_B)
    assert a + b not in result.text, "两段话粘在一起了"
    assert a + "\n\n" + b in result.text, result.text
    assert seen[-1].strip() == result.text, "流式累加和最终文本分隔不一致"
