"""第 32 轮 DENYREPLY：确认卡挂着时打字 = 回答它（没批），原话交给模型。

实验（2026-11-09，真 claude-haiku-5-5 + 真钩子，只读档）：
- 第一轮 `touch hello.txt` ⇒ 钩子 defer ⇒ `stop_reason=tool_deferred`
- `--resume <id>` 加一句「不行，别建这个文件，改成只打印出来」⇒ Claude Code **用同一个
  tool_use_id 再调了一次钩子**（钩子日志 3 次调用，前两次 tool_use_id 相同）。
  - 钩子照旧 defer 的话：会话 jsonl 里那条 tool_result =
    `[Tool result missing due to internal error]`（is_error=true），模型说「刚才那次 touch 的结果没有返回」。
  - resume 前写 `<指纹>.deny`、钩子查到就 deny 带原话：tool_result =
    `PreToolUse:Bash hook error: <我们的话>`，模型回「没有创建 hello.txt…按你的意思…」。
⇒ 走 A 路：钩子查 `.deny`。

这里用真钩子进程 + 真 `_run_locally` / `_ask_approval` / `on_card_action`，只把 claude 换掉。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_interference import SCOPE, _click, _fake_bridge, _suffixes  # noqa: E402

from bridge.approval import HOOK_SOURCE, action_digest  # noqa: E402

CMD_A = "echo hi > out.txt"
CMD_B = "echo two > two.txt"
SECRET = "sk-ant-api03-" + "A" * 40


def _write_hook(tmp):
    path = os.path.join(tmp, "hook.py")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(HOOK_SOURCE)
    return path


def _hook(hook, directory, command, session_id="sess-1", tool_use_id="toolu_1"):
    """照 Claude Code 的方式起钩子（stdin 一份 JSON）。"""
    env = dict(os.environ, CARHER_APPROVAL_DIR=directory)
    proc = subprocess.run(
        [sys.executable, hook],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                          "session_id": session_id, "tool_use_id": tool_use_id, "cwd": "/tmp"}),
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]


def _bridge(tmp):
    b = _fake_bridge(tmp)
    b.sent, b.cards, b.ran, b.patched = [], [], [], []
    b._say = lambda chat_id, text: b.sent.append(text) or True
    b._say_with_buttons = lambda chat_id, text, buttons: b.sent.append(text) or True

    def send_card(*a, **kw):
        b.cards.append(a)
        return f"om_{len(b.cards)}"            # 真 Replier：成功时回 message_id

    b.replier.send_approval_card = send_card
    b.replier.patch_settled_card = lambda mid, outcome, note="": b.patched.append(
        (mid, outcome, note)) or True
    b._chat_for = lambda scope: "chat1"
    b.workspaces = types.SimpleNamespace(get=lambda scope: "/tmp")

    from bridge.review import Reviewer
    from bridge.runner import TurnResult

    b.reviewer = Reviewer(custom_rules=())
    b.settings.progress_first_notice_sec = 90
    b.settings.progress_escalate_sec = 420
    b.settings.streaming = False

    class Runner:
        async def run_turn(self, text, workspace, approval_dir=None, **_):
            b.ran.append(text)
            # 模拟 Claude Code resume 时**再调一次钩子**（实验结论），把钩子说的话记下来
            if getattr(b, "hook", None):
                b.hook_said = [_hook(b.hook, approval_dir, c) for c in getattr(b, "replay", [])]
            return TurnResult(ok=True, text="好的", session_id="sess-1")

    b.runner = Runner()
    return b


def _defer(b, hook, commands):
    """真钩子落 .pending → 一轮 defer → _ask_approval 出卡。"""
    directory = b.gate.dir_for(SCOPE)
    for c in commands:
        assert _hook(hook, directory, c)["permissionDecision"] == "defer"
    result = types.SimpleNamespace(deferred=True, text="", session_id="sess-1", ok=True,
                                   denials=[], error="")
    b._ask_approval(SCOPE, "chat1", b.sessions.get(SCOPE), result)
    assert len(b.cards) == len(commands), "前提没成立：卡没发出去"
    out = []
    for card in b.cards[-len(commands):]:
        v = card[3]
        out.append(types.SimpleNamespace(to_value=lambda v=v: {k: x for k, x in v.items() if k != "act"}))
    return out


def _toast(resp) -> str:
    body = getattr(resp, "toast", None) or {}
    if not body and isinstance(getattr(resp, "__dict__", None), dict):
        body = resp.__dict__.get("toast") or {}
    return (body.get("content") if isinstance(body, dict) else getattr(body, "content", "")) or ""


def test_typing_while_card_waits_hands_the_words_to_the_model():
    """打字 ⇒ resume 时钩子 deny 并带上原话；卡片原地换成「没让它做 · 你说：…」；不再说老话。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.hook = _write_hook(tmp)
        _defer(b, b.hook, [CMD_A])
        b.replay = [CMD_A]
        asyncio.run(b._run_locally(SCOPE, "chat1", "不行，别建这个文件，改成只打印出来"))

        assert b.ran == ["不行，别建这个文件，改成只打印出来"]
        said = b.hook_said[0]
        assert said["permissionDecision"] == "deny", said
        assert "不行，别建这个文件，改成只打印出来" in said["permissionDecisionReason"]
        assert "没批" in said["permissionDecisionReason"]
        assert b.patched and b.patched[0][0] == "om_1" and b.patched[0][1] == "replied", b.patched
        assert "你说：不行，别建这个文件" in b.patched[0][2]
        assert not any("等这条回完再说一次" in t for t in b.sent), b.sent
        assert not any(n.endswith(".deny") for n in os.listdir(b.gate.dir_for(SCOPE))), "deny 没被兑换掉"


def test_reply_is_redacted_before_it_reaches_disk_model_and_card():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.hook = _write_hook(tmp)
        _defer(b, b.hook, [CMD_A])
        b.replay = []                                       # 先不 resume，看盘上
        asyncio.run(b._run_locally(SCOPE, "chat1", f"别跑，用这个 key：{SECRET}"))
        directory = b.gate.dir_for(SCOPE)
        deny = [n for n in os.listdir(directory) if n.endswith(".deny")]
        assert deny, "没写 .deny"
        raw = open(os.path.join(directory, deny[0]), encoding="utf-8").read()
        assert SECRET not in raw and "已脱敏" in raw, raw
        assert SECRET not in b.patched[0][2], b.patched
        said = _hook(b.hook, directory, CMD_A)
        assert said["permissionDecision"] == "deny"
        assert SECRET not in said["permissionDecisionReason"]


def test_red_line_still_denied_even_with_a_reply_and_new_wording():
    """红线照旧：.deny 只会更严。另查新拒绝语：说清是安全规则、不许绕过。"""
    with tempfile.TemporaryDirectory() as tmp:
        hook = _write_hook(tmp)
        from bridge.approval import ApprovalGate

        gate = ApprovalGate(os.path.join(tmp, "root"))
        directory = gate.dir_for(SCOPE)
        out = _hook(hook, directory, "rm -rf ~")
        assert out["permissionDecision"] == "deny"
        reason = out["permissionDecisionReason"]
        assert "安全规则" in reason and "不是用户拒的" in reason and "绕过" in reason, reason
        # 盘上有一条「同意」样的 .ok 也没用（红线在前），有 .deny 也只是 deny
        key = action_digest("Bash", {"command": "rm -rf ~"})
        open(os.path.join(directory, key + ".ok"), "w").write("1")
        assert _hook(hook, directory, "rm -rf ~")["permissionDecision"] == "deny"
        with open(os.path.join(directory, key + ".deny"), "w", encoding="utf-8") as fh:
            json.dump({"text": "做吧", "session_id": "sess-1"}, fh)
        assert _hook(hook, directory, "rm -rf ~")["permissionDecision"] == "deny"


def test_two_cards_only_the_first_is_answered():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.hook = _write_hook(tmp)
        _defer(b, b.hook, [CMD_A, CMD_B])
        first_digest = next(iter(b._issued_cards[SCOPE].values()))   # 第一张出的卡
        b.replay = []
        asyncio.run(b._run_locally(SCOPE, "chat1", "都先别做"))
        directory = b.gate.dir_for(SCOPE)
        deny = [n for n in os.listdir(directory) if n.endswith(".deny")]
        assert len(deny) == 1, deny
        # 第一张出的卡 ⇒ 被回答；第二张 ⇒ 照旧作废
        outcomes = {mid: outcome for mid, outcome, _ in b.patched}
        assert outcomes == {"om_1": "replied", "om_2": "superseded"}, b.patched
        assert deny[0] == first_digest + ".deny", (deny, first_digest)
        # 第一张那条命令 ⇒ deny 带原话；第二张 ⇒ 没有 .deny，照常 defer（会重新出卡）
        reasons = {}
        for c in (CMD_A, CMD_B):
            reasons[c] = _hook(b.hook, directory, c)
        answered = [c for c, r in reasons.items() if r["permissionDecision"] == "deny"]
        assert len(answered) == 1, reasons
        assert "都先别做" in reasons[answered[0]]["permissionDecisionReason"]
        other = [c for c in reasons if c not in answered][0]
        assert reasons[other]["permissionDecision"] == "defer"


def test_first_card_is_the_first_one_issued_not_the_alphabetical_one():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.hook = _write_hook(tmp)
        a, c = sorted([CMD_A, CMD_B], key=lambda x: action_digest("Bash", {"command": x}))
        _defer(b, b.hook, [a, c])
        # 让「出卡顺序」和指纹排序相反：把内存里记的出卡顺序倒过来
        issued = b._issued_cards[SCOPE]
        b._issued_cards[SCOPE] = dict(reversed(list(issued.items())))
        b.replay = []
        asyncio.run(b._run_locally(SCOPE, "chat1", "先别"))
        deny = [n for n in os.listdir(b.gate.dir_for(SCOPE)) if n.endswith(".deny")]
        assert deny == [action_digest("Bash", {"command": c}) + ".deny"], deny


def test_clicking_the_answered_card_afterwards_does_nothing():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.hook = _write_hook(tmp)
        (token,) = _defer(b, b.hook, [CMD_A])
        b.replay = []
        asyncio.run(b._run_locally(SCOPE, "chat1", "不要"))
        resp = _click(b, token)
        assert "继续" not in _toast(resp), _toast(resp)
        assert not any(n.endswith(".ok") for n in _suffixes(b, SCOPE)), "回答过的卡点同意还写了放行证"


def test_patch_failure_falls_back_to_one_line():
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.hook = _write_hook(tmp)
        _defer(b, b.hook, [CMD_A])
        b.replier.patch_settled_card = lambda *a, **kw: False
        b.replay = []
        asyncio.run(b._run_locally(SCOPE, "chat1", "不要"))
        assert any("没批" in t for t in b.sent), b.sent
        assert not any("等这条回完再说一次" in t for t in b.sent), b.sent


def test_stale_or_foreign_deny_does_not_block_a_later_session():
    """.deny 只在同一个 claude 会话里、10 分钟内兑现 —— 一句旧话不能把以后同样的命令也拦了。"""
    with tempfile.TemporaryDirectory() as tmp:
        hook = _write_hook(tmp)
        from bridge.approval import ApprovalGate

        gate = ApprovalGate(os.path.join(tmp, "root"))
        directory = gate.dir_for(SCOPE)
        key = action_digest("Bash", {"command": CMD_A})
        path = os.path.join(directory, key + ".deny")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"text": "不要", "session_id": "sess-OLD"}, fh)
        assert _hook(hook, directory, CMD_A, session_id="sess-NEW")["permissionDecision"] == "defer"
        assert not os.path.exists(path), "别人的 .deny 没清掉"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"text": "不要", "session_id": "sess-1"}, fh)
        os.utime(path, (1, 1))
        assert _hook(hook, directory, CMD_A)["permissionDecision"] == "defer"


def test_positive_control_approve_chain_unchanged():
    """阳性对照：没打字 ⇒ 点同意 ⇒ .ok ⇒ 钩子 allow。"""
    with tempfile.TemporaryDirectory() as tmp:
        b = _bridge(tmp)
        b.hook = _write_hook(tmp)
        (token,) = _defer(b, b.hook, [CMD_A])
        import bridge.app as app_mod

        original = app_mod.asyncio.run_coroutine_threadsafe
        scheduled = []
        app_mod.asyncio.run_coroutine_threadsafe = lambda coro, loop: (scheduled.append(coro), coro.close())
        try:
            resp = _click(b, token)
        finally:
            app_mod.asyncio.run_coroutine_threadsafe = original
        assert "继续" in _toast(resp) and scheduled
        assert _hook(b.hook, b.gate.dir_for(SCOPE), CMD_A)["permissionDecision"] == "allow"
        assert not b.patched
