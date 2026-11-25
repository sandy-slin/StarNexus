"""第 8 轮：「等你」的会话点进去，详情卡上要看得到它在问什么。

用户原话（Happy #635）：「the actual question text and multiple-choice options aren't rendered」。
主人掏手机看到「🟡 等你」点进去，原来卡上只有几条旧消息 —— tool_use 一律不显示。

盯这几件事：
- 会话在等你 + jsonl 尾部最后一条 tool_use 没有 tool_result ⇒ 卡上在最近消息**之前**多一段
  - AskUserQuestion ⇒「❓ 它在问你」+ 题目 + ①②③ 选项
  - 别的工具 ⇒「⏸ 它在等你确认」+ 人话工具名 + 一行摘要
  - 末尾小字「要在电脑上点」；**不给按钮**（手机上点选项回答没验过，第 2 步不做）
- 阳性对照：不在等你 / 已经有 tool_result / 人后来又说了话 / 读失败 ⇒ 不加这段，卡照常出
- 先脱敏整段再截断（命令里可能带凭据）
- 20KB / schema 2.0 / 没有 `tag: action`
- 调用点还在：走 `ClaudeAdapter.find` → `FleetControl.detail` → `build_detail_card` 整条路（app.py 就是这么调的）

落盘夹具放在测试目录旁边，不放 tmp_path（brief-common 测试纪律）。
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import Session  # noqa: E402
from bridge.fleet import cards  # noqa: E402
from bridge.fleet import claude as claude_mod  # noqa: E402
from bridge.fleet.cards import MAX_CARD_BYTES, build_detail_card  # noqa: E402
from bridge.fleet.claude import ClaudeAdapter  # noqa: E402
from bridge.fleet.control import FleetControl  # noqa: E402
from bridge.fleet.model import FleetJobs, Message, ProcTable  # noqa: E402
from bridge.fleet.scan import Fleet  # noqa: E402
from bridge.redact import redact  # noqa: E402

SID = "abababab-1111-4111-8111-111111111111"
LSTART = "Tue Nov  3 19:02:35 2026"
PS = f"  101     1 S+   {LSTART}    02:11:05 claude --model opus\n"
SECRET = "sk-ant-" + "A" * 40  # redact.py 认得的形状


def _redact(text):
    return redact(str(text or ""))[0]


@pytest.fixture
def here_dir():
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".r8-pending-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------- 假 jsonl 记录 ----------


def _user(text):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text},
            "timestamp": "2026-11-06T01:00:00.000Z"}


def _asst_text(text, mid="m0"):
    return {"type": "assistant", "isSidechain": False,
            "message": {"id": mid, "role": "assistant", "content": [{"type": "text", "text": text}]},
            "timestamp": "2026-11-06T01:00:02.000Z"}


def _tool_use(name, inp, tid="toolu_1", mid="m1"):
    return {"type": "assistant", "isSidechain": False,
            "message": {"id": mid, "role": "assistant",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]},
            "timestamp": "2026-11-06T01:00:05.000Z"}


def _tool_result(tid="toolu_1", text="ok"):
    return {"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
            "timestamp": "2026-11-06T01:00:09.000Z"}


ASK_INPUT = {"questions": [{
    "question": "登录页要用哪种方案？",
    "header": "方案",
    "multiSelect": False,
    "options": [
        {"label": "短信验证码", "description": "最快上线，依赖短信网关", "preview": "x"},
        {"label": "飞书扫码", "description": "公司同事都有飞书"},
        {"label": "账号密码", "description": "老办法"},
    ],
}]}


def _env(root, status="waiting", records=None):
    home = root / "claude"
    work = root / "work"
    work.mkdir(exist_ok=True)
    reg = {"pid": 101, "sessionId": SID, "cwd": str(work), "procStart": LSTART, "kind": "interactive",
           "entrypoint": "cli", "status": status, "version": "2.1.317", "name": "修登录", "nameSource": "user",
           "updatedAt": int(time.time() * 1000)}
    (home / "sessions").mkdir(parents=True, exist_ok=True)
    (home / "sessions" / "101.json").write_text(json.dumps(reg), encoding="utf-8")
    proj = home / "projects" / "-work"
    proj.mkdir(parents=True, exist_ok=True)
    recs = records if records is not None else [_user("帮我修登录"), _asst_text("我先问一下"),
                                                _tool_use("AskUserQuestion", ASK_INPUT)]
    with open(proj / f"{SID}.jsonl", "w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    adapter = ClaudeAdapter(home=str(home), bridge_home=str(root / "bridge"), app_root=str(root / "app"),
                            jobs=FleetJobs(lstart_of=lambda pid: LSTART, kill=lambda *a: None),
                            procs=lambda: ProcTable.parse(PS), policy=None)
    return adapter, proj / f"{SID}.jsonl"


def _detail_blob(adapter):
    """和 app.py `_fleet_detail` 同一条路：FleetControl.detail → build_detail_card(redact=…)。"""
    control = FleetControl(Fleet({"claude": adapter}))
    sess, messages, note = control.detail("claude", SID)
    assert sess is not None
    blob = build_detail_card(sess, messages, redact=_redact, note=note)
    _check_card(blob)
    return sess, blob


def _check_card(blob):
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    return card


def _texts(blob):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "markdown":
                out.append(node.get("content", ""))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(blob))
    return out


def _buttons(blob):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                out.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(json.loads(blob))
    return out


# ---------- 整条路（红在现码上）----------


def test_waiting_ask_question_shows_question_and_options(here_dir):
    adapter, _ = _env(here_dir)
    sess, blob = _detail_blob(adapter)
    texts = _texts(blob)
    joined = "\n".join(texts)
    assert "它在问你" in joined
    assert "登录页要用哪种方案" in joined
    for i, label in zip("①②③", ("短信验证码", "飞书扫码", "账号密码")):
        assert f"{i} {label}" in joined
    assert "要在电脑上点" in joined
    # 在最近消息**之前**
    ask_at = next(i for i, t in enumerate(texts) if "它在问你" in t)
    msg_at = next(i for i, t in enumerate(texts) if "我先问一下" in t)
    assert ask_at < msg_at
    # 🔴 不给「点了没用的」选项按钮：按钮还是原来那几个
    labels = [b["text"]["content"] for b in _buttons(blob)]
    assert not any(lbl in " ".join(labels) for lbl in ("短信验证码", "飞书扫码", "账号密码"))


def test_waiting_bash_shows_confirm_with_human_tool_name(here_dir):
    adapter, _ = _env(here_dir, records=[
        _user("发个版"), _tool_use("Bash", {"command": "npm publish --tag next", "description": "发版"}),
    ])
    _, blob = _detail_blob(adapter)
    joined = "\n".join(_texts(blob))
    assert "它在等你确认" in joined
    assert "跑命令" in joined  # 人话工具名照 status._TOOL_LABEL
    assert "npm publish --tag next" in joined
    assert "要在电脑上点" in joined


def test_adapter_fills_pending_only_when_waiting(here_dir):
    adapter, _ = _env(here_dir)
    sess = adapter.find(SID)
    assert sess.status == "waiting"
    assert sess.pending is not None and sess.pending.questions
    q = sess.pending.questions[0]
    assert q.question == "登录页要用哪种方案？"
    assert [o[0] for o in q.options] == ["短信验证码", "飞书扫码", "账号密码"]


# ---------- 阳性对照 ----------


def test_not_waiting_no_section(here_dir):
    adapter, _ = _env(here_dir, status="busy")
    sess, blob = _detail_blob(adapter)
    assert sess.status == "running"
    assert getattr(sess, "pending", None) is None
    joined = "\n".join(_texts(blob))
    assert "它在问你" not in joined and "它在等你确认" not in joined


def test_resolved_tool_use_no_section(here_dir):
    adapter, _ = _env(here_dir, records=[
        _user("发个版"), _tool_use("Bash", {"command": "npm publish"}), _tool_result(),
        _asst_text("发好了", mid="m2"),
    ])
    sess, blob = _detail_blob(adapter)
    assert sess.status == "waiting" and sess.pending is None
    joined = "\n".join(_texts(blob))
    assert "它在等你确认" not in joined and "发好了" in joined


def test_user_spoke_after_stale_tool_use_no_section(here_dir):
    """人后来又说了话 ⇒ 前面没回的那条是旧账（比如被打断时没落 tool_result），不显示。"""
    adapter, _ = _env(here_dir, records=[
        _tool_use("Bash", {"command": "rm -rf build"}), _user("算了，换个思路"),
    ])
    sess = adapter.find(SID)
    assert sess.pending is None


def test_last_unresolved_wins_over_earlier_resolved(here_dir):
    adapter, _ = _env(here_dir, records=[
        _tool_use("Read", {"file_path": "/a.txt"}, tid="t1"), _tool_result("t1"),
        _tool_use("Edit", {"file_path": "/src/login.py", "old_string": "a", "new_string": "b"}, tid="t2", mid="m2"),
    ])
    sess = adapter.find(SID)
    assert sess.pending.tool == "Edit" and "/src/login.py" in sess.pending.summary


def test_sidechain_tool_use_ignored():
    recs = [dict(_tool_use("Bash", {"command": "ls"}), isSidechain=True)]
    assert claude_mod._pending_ask(recs) is None


def test_read_failure_card_still_ships(here_dir, monkeypatch):
    adapter, _ = _env(here_dir)

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(claude_mod, "_pending_ask", boom)
    sess, blob = _detail_blob(adapter)  # 不抛、卡照常
    assert sess.status == "waiting" and sess.pending is None
    assert "它在问你" not in "\n".join(_texts(blob))


def test_jsonl_missing_card_still_ships(here_dir):
    adapter, path = _env(here_dir)
    os.remove(path)
    sess = adapter.find(SID)
    assert sess is not None and sess.pending is None
    _check_card(build_detail_card(sess, [], redact=_redact))


def test_card_section_survives_bad_redact():
    """脱敏函数炸了 ⇒ 这一段不显示，卡照常出（不能让详情卡发不出去）。"""
    from bridge.fleet.model import PendingAsk

    sess = Session(tool="claude", surface="cli", id=SID, title="t", status="waiting",
                   pending=PendingAsk(tool="Bash", summary="ls"))
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if text == "ls":
            raise RuntimeError("redact broke")
        return text

    blob = build_detail_card(sess, [Message("assistant", "hi")], redact=flaky)
    _check_card(blob)
    assert "它在等你确认" not in blob


def test_reads_tail_not_whole_file(here_dir, monkeypatch):
    """读尾部用现成的 `_read_tail`，有上限，不整个文件读（本机有 200MB 的 jsonl）。"""
    adapter, _ = _env(here_dir)
    sizes = []
    real = claude_mod._read_tail

    def spy(path, size):
        sizes.append(size)
        return real(path, size)

    monkeypatch.setattr(claude_mod, "_read_tail", spy)
    sess = adapter.find(SID)
    assert sess.pending is not None
    assert sizes and max(sizes) <= 8 * 1024 * 1024
    assert claude_mod.PENDING_TAIL_BYTES <= 1024 * 1024


# ---------- 脱敏 / 截断 / 大小 ----------


def test_redact_whole_command_before_truncating(here_dir):
    """凭据在命令的**后半截**：先截断再脱敏的话，被截在 sk-ant- 中间就认不出来了。"""
    # 摘要截在 160 字：前缀 142 字 ⇒ 截完只剩 `sk-ant-` + 10 个字符，redact 认不出（要 ≥16）
    # 🔴 前面不带 `key:` 这种字样：带了的话「赋值」那条规则会兜住半截，测不出顺序
    prefix = "echo " + "x" * 137 + " "
    assert len(prefix) == 143
    cmd = prefix + SECRET
    adapter, _ = _env(here_dir, records=[_tool_use("Bash", {"command": cmd})])
    _, blob = _detail_blob(adapter)
    assert "sk-ant-A" not in blob
    assert "已脱敏" in blob


def test_redact_question_and_options(here_dir):
    inp = {"questions": [{"question": f"用哪个 key：{SECRET}？", "header": "k", "multiSelect": False,
                          "options": [{"label": f"用 {SECRET}", "description": f"老的 {SECRET}"},
                                      {"label": "换新的", "description": ""}]}]}
    adapter, _ = _env(here_dir, records=[_tool_use("AskUserQuestion", inp)])
    _, blob = _detail_blob(adapter)
    assert "AAAAAAAAAAAAAAAA" not in blob
    assert "换新的" in blob


def test_redact_long_question_before_truncating(here_dir):
    """题目 / 选项各自截在 120 / 30 字，凭据正好跨过截断点 ⇒ 先截断的话半截凭据漏出去。"""
    q = "x" * 105 + " " + SECRET  # 120 字处截在 sk-ant- 后面第 7 个字符
    label = "y" * 15 + " " + SECRET
    inp = {"questions": [{"question": q, "header": "", "multiSelect": False,
                          "options": [{"label": label, "description": ""}]}]}
    adapter, _ = _env(here_dir, records=[_tool_use("AskUserQuestion", inp)])
    _, blob = _detail_blob(adapter)
    assert "sk-ant" not in blob
    assert blob.count("已脱敏") >= 2  # 方括号在卡片里转成了实体


def test_parallel_tool_uses_last_unresolved_wins(here_dir):
    """一条回复里并行两个 tool_use、都没回 ⇒ 显示最后那个（它停在那儿）。"""
    rec = _tool_use("Read", {"file_path": "/a.txt"}, tid="t1")
    rec["message"]["content"].append({"type": "tool_use", "id": "t2", "name": "Bash",
                                      "input": {"command": "make deploy"}})
    adapter, _ = _env(here_dir, records=[rec])
    sess = adapter.find(SID)
    assert sess.pending.tool == "Bash" and sess.pending.summary == "make deploy"


def test_huge_question_still_fits_20kb():
    from bridge.fleet.model import AskQuestion, PendingAsk

    big = "很长的题目" * 4000
    qs = tuple(AskQuestion(question=big, header=big, options=tuple((big, big) for _ in range(30)))
               for _ in range(10))
    sess = Session(tool="claude", surface="cli", id=SID, title="t", status="waiting",
                   pending=PendingAsk(tool="AskUserQuestion", questions=qs))
    msgs = [Message("assistant", "x" * 5000) for _ in range(4)]
    blob = build_detail_card(sess, msgs, redact=_redact)
    _check_card(blob)
    assert "它在问你" in blob


def test_markdown_in_question_is_escaped():
    from bridge.fleet.model import AskQuestion, PendingAsk

    q = AskQuestion(question="[点我](https://evil.example) <at id=all></at>",
                    options=(("**粗**", "<b>x</b>"),))
    sess = Session(tool="claude", surface="cli", id=SID, title="t", status="waiting",
                   pending=PendingAsk(tool="AskUserQuestion", questions=(q,)))
    blob = build_detail_card(sess, [], redact=_redact)
    section = next(t for t in _texts(blob) if "它在问你" in t)
    assert "](https://evil.example)" not in section
    assert "<at" not in section and "<b>" not in section


def test_card_ignores_pending_when_not_waiting():
    from bridge.fleet.model import PendingAsk

    sess = Session(tool="claude", surface="cli", id=SID, title="t", status="idle",
                   pending=PendingAsk(tool="Bash", summary="ls"))
    assert "它在等你确认" not in build_detail_card(sess, [], redact=_redact)


def test_unknown_tool_name_falls_back_to_code():
    from bridge.fleet.model import PendingAsk

    sess = Session(tool="claude", surface="cli", id=SID, title="t", status="waiting",
                   pending=PendingAsk(tool="mcp__lark__send", summary=""))
    joined = "\n".join(_texts(build_detail_card(sess, [], redact=_redact)))
    assert "它在等你确认" in joined and "mcp__lark__send" in joined


# ---------- 调用点还在 ----------


def test_call_sites_present():
    """查调用形状 `f(x)`，不查名字（只查名字会被 import 行喂饱）。"""
    card_src = inspect.getsource(cards.build_detail_card)
    assert "pending_elements(sess, clean)" in card_src
    run_src = inspect.getsource(ClaudeAdapter._running_session)
    assert "self._pending_for(path)" in run_src
    assert "pending=pending" in run_src
    reader_src = inspect.getsource(ClaudeAdapter._pending_for)
    assert "_read_tail(path, PENDING_TAIL_BYTES)" in reader_src
    assert "_pending_ask(records)" in reader_src


# ---------- 文字兜底那条路也要说清（主会话 2026-11-06 接线）----------


def _fb_sess(pending, status="waiting"):
    import types
    return types.SimpleNamespace(status=status, pending=pending)


def test_text_fallback_shows_the_question_and_options():
    from bridge.app import _pending_fallback
    from bridge.fleet.model import AskQuestion, PendingAsk

    p = PendingAsk(tool="AskUserQuestion", summary="",
                   questions=(AskQuestion(question="用哪个库？", options=(("A 库", ""), ("B 库", ""))),))
    out = _pending_fallback(_fb_sess(p))
    assert "它在问你" in out and "用哪个库" in out and "1. A 库" in out and "电脑上" in out


def test_text_fallback_redacts_the_command_before_cutting():
    """🔴 卡片发不出去时这段走纯文本 ⇒ 同样要脱敏，不能把凭据明文发出去。"""
    from bridge.app import _pending_fallback
    from bridge.fleet.model import PendingAsk

    secret = "sk-ant-api03-" + "Z" * 40
    out = _pending_fallback(_fb_sess(PendingAsk(tool="跑命令", summary=f"curl -H {secret} x", questions=())))
    assert "等你确认" in out and secret not in out


def test_text_fallback_silent_when_not_waiting():
    from bridge.app import _pending_fallback
    from bridge.fleet.model import PendingAsk

    assert _pending_fallback(_fb_sess(PendingAsk(tool="x", summary="y", questions=()), status="running")) == ""


def test_fleet_detail_wires_the_text_fallback():
    import inspect

    from bridge.app import Bridge

    assert "_pending_fallback(sess)" in inspect.getsource(Bridge._fleet_detail)
