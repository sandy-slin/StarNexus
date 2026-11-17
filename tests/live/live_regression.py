"""StarNexus 真机回归：以林森身份通过 lark-cli 给 Her remote · Pro 发命令，读回群消息判定。

只覆盖能从服务端读回的结果；卡片按钮点击和客户端画面仍需 Computer Use 复核（见 docs/computer-use.md）。

用法：
    SN_DM_CHAT=oc_xxx python3 tests/live/live_regression.py [用例 ...]
SN_DM_CHAT 是林森和 Her remote · Pro 的私聊 chat_id。不传用例就全跑。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~/.carher-feishu-agent")
WORK = os.path.expanduser("~/work/AI_native/live-regression")


def lark(*args: str) -> dict:
    out = subprocess.run(["lark-cli", *args, "--as", "user", "--format", "json"],
                         capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "raw": out.stdout[:300] + out.stderr[:300]}


def send(chat: str, text: str) -> bool:
    return bool(lark("im", "+messages-send", "--chat-id", chat, "--text", text).get("ok"))


def messages(chat: str, n: int = 20) -> list:
    d = lark("im", "+chat-messages-list", "--chat-id", chat, "--page-size", str(n), "--sort", "desc")
    return [m for m in (d.get("data") or {}).get("messages") or []]


def bot_texts(chat: str, n: int = 20) -> list:
    return [str(m.get("content") or "") for m in messages(chat, n)
            if (m.get("sender") or {}).get("sender_type") == "app"]


def group_name(chat: str) -> str:
    d = lark("im", "chats", "get", "--params", json.dumps({"chat_id": chat}))
    return str((d.get("data") or {}).get("name") or "")


def binds() -> dict:
    with open(os.path.join(HOME, "chat_binds.json"), encoding="utf-8") as fh:
        return json.load(fh).get("binds") or {}


def wait_new_group(before: set, timeout: float = 90) -> str:
    end = time.time() + timeout
    while time.time() < end:
        new = [k for k in binds() if k not in before]
        if new:
            return new[0]
        time.sleep(3)
    return ""


def wait_bot(chat: str, needle: str, timeout: float = 120) -> str:
    end = time.time() + timeout
    while time.time() < end:
        for text in bot_texts(chat):
            if needle in text:
                return text
        time.sleep(4)
    return ""


def _new_session(dm: str, tool: str, name: str, first: str) -> tuple:
    """发 /ns 工具 名字 路径 + 第一句话，返回 (新群 chat_id, 失败原因)。"""
    path = os.path.join(WORK, name)
    os.makedirs(path, exist_ok=True)
    before = set(binds())
    if not send(dm, f"/ns {tool} {name} {path}"):
        return "", "发 /ns 失败"
    time.sleep(6)
    if not send(dm, first):
        return "", "发第一句话失败"
    chat = wait_new_group(before)
    return (chat, "") if chat else ("", "90 秒内没建群")


def case_ns_claude(dm: str) -> tuple:
    """5.1 / FC-01：/ns 建 Claude 会话 ⇒ SN· 群名、置顶状态卡标题用名字、首句有回复。"""
    name = "lr-claude-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "claude", name, "回归测试，只回复 LR-OK")
    if not chat:
        return False, why
    gname = group_name(chat)
    if not gname.startswith("SN·Claude·") or name not in gname:
        return False, f"群名不对：{gname}"
    if not wait_bot(chat, "LR-OK"):
        return False, "群里没等到 LR-OK"
    if not any(f"Claude「{name}」" in t and "会话状态卡" in t for t in bot_texts(chat)):
        return False, "状态卡标题没用 /ns 名字"
    return True, f"{gname}"


def case_ns_codex(dm: str) -> tuple:
    """5.1 / FC-01：Codex 同样流程，不指定模型 ⇒ 状态卡写工具默认。"""
    name = "lr-codex-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "codex", name, "回归测试，只回复 LR-OK")
    if not chat:
        return False, why
    gname = group_name(chat)
    if not gname.startswith("SN·Codex·"):
        return False, f"群名不对：{gname}"
    if not wait_bot(chat, "LR-OK", timeout=240):
        return False, "群里没等到 LR-OK"
    if not any("工具默认" in t for t in bot_texts(chat)):
        return False, "状态卡没写工具默认模型"
    return True, gname


def case_group_relay(dm: str) -> tuple:
    """5.2：已绑定的群里直接说话 ⇒ 进会话并回复，不弹确认卡。"""
    name = "lr-relay-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "claude", name, "回归测试，只回复 LR-ONE")
    if not chat or not wait_bot(chat, "LR-ONE"):
        return False, why or "首句没回"
    send(chat, "接着说，只回复 LR-TWO")
    if not wait_bot(chat, "LR-TWO"):
        return False, "群里第二句没回"
    if any("确认，开始说话" in t for t in bot_texts(chat)):
        return False, "出现了多余的确认卡"
    return True, "群里两轮都回了"


def case_find(dm: str) -> tuple:
    """5.7：/find 关键字 ⇒ 列出命中的会话。"""
    send(dm, "/find lr-")
    text = wait_bot(dm, "lr-", timeout=60)
    return (True, "搜到 lr- 会话") if text else (False, "/find 没列出 lr- 会话")


CASES = {
    "ns_claude": case_ns_claude,
    "ns_codex": case_ns_codex,
    "group_relay": case_group_relay,
    "find": case_find,
}


def main(argv: list) -> int:
    dm = os.environ.get("SN_DM_CHAT", "")
    if not dm.startswith("oc_"):
        print("先设置 SN_DM_CHAT（林森和 Her remote · Pro 的私聊 chat_id）")
        return 2
    names = argv or list(CASES)
    failed = 0
    for key in names:
        try:
            ok, note = CASES[key](dm)
        except Exception as exc:  # noqa: BLE001
            ok, note = False, f"异常：{exc}"
        failed += not ok
        print(f"{'PASS' if ok else 'FAIL'} {key}: {note}", flush=True)
    print(f"共 {len(names)} 条，失败 {failed} 条")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
