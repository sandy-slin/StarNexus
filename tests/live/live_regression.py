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


def wait_new_group(before: set, timeout: float = 360) -> str:
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
    return (chat, "") if chat else ("", "6 分钟内没建群")


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


def _red_png(path: str) -> None:
    import struct
    import zlib

    width = height = 64
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def _upload_image_as_bot(path: str) -> str:
    """林森的用户授权没有 im:resource:upload ⇒ 用 bot 身份上传拿 image_key，再以林森身份发进群。"""
    out = subprocess.run(["lark-cli", "im", "images", "create", "--as", "bot", "--data", '{"image_type":"message"}',
                          "--file", f"image={path}", "--format", "json"], capture_output=True, text=True, timeout=60)
    try:
        return str((json.loads(out.stdout or "{}").get("data") or {}).get("image_key") or "")
    except json.JSONDecodeError:
        return ""


def _new_session_args(dm: str, tool: str, name: str, extra: str, first: str) -> tuple:
    path = os.path.join(WORK, name)
    os.makedirs(path, exist_ok=True)
    before = set(binds())
    if not send(dm, f"/ns {tool} {name} {path} {extra}".strip()):
        return "", "发 /ns 失败"
    time.sleep(6)
    if not send(dm, first):
        return "", "发第一句话失败"
    chat = wait_new_group(before)
    return (chat, "") if chat else ("", "6 分钟内没建群")


def case_model_effort(dm: str) -> tuple:
    """FC-02：/ns claude 名字 路径 opus high ⇒ 状态卡写 claude-opus-5-5 / 高。"""
    name = "lr-model-" + time.strftime("%H%M%S")
    chat, why = _new_session_args(dm, "claude", name, "opus high", "回归测试，只回复 LR-MODEL")
    if not chat:
        return False, why
    if not wait_bot(chat, "LR-MODEL"):
        return False, "首句没回"
    if not any("claude-opus-5-5 / 高" in t for t in bot_texts(chat)):
        return False, "状态卡没写 claude-opus-5-5 / 高"
    return True, "状态卡 claude-opus-5-5 / 高"


def case_long_reply(dm: str) -> tuple:
    """FC-10：回复超过 1500 字 ⇒ 群里只发摘要和飞书文档链接。"""
    name = "lr-long-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "claude", name,
                             "回归测试：直接用中文写一篇不少于 2500 字的说明文，主题是如何整理家里的书架。不要用任何工具。")
    if not chat:
        return False, why
    text = wait_bot(chat, "全文已放进飞书文档", timeout=300)
    return (True, "群里是摘要 + 文档链接") if text else (False, "5 分钟内没出现文档链接")


def case_image(dm: str) -> tuple:
    """FC-09：群里发一张纯红色图片 ⇒ Agent 读到并说出红色。"""
    name = "lr-image-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "claude", name, "回归测试：下一条我会发一张图，你看图后只回复它的主要颜色。这一句只回复 LR-READY")
    if not chat or not wait_bot(chat, "LR-READY"):
        return False, why or "首句没回"
    here = os.getcwd()
    tmp = os.path.join(WORK, name)
    os.chdir(tmp)
    try:
        _red_png("red.png")
        key = _upload_image_as_bot("red.png")
        ok = bool(key) and bool(lark("im", "+messages-send", "--chat-id", chat, "--image", key).get("ok"))
    finally:
        os.chdir(here)
    if not ok:
        return False, "发图片失败"
    text = wait_bot(chat, "红", timeout=180)
    return (True, "Agent 回答红色") if text else (False, "3 分钟内没回答红色")


def case_stop(dm: str) -> tuple:
    """FC-11：群里 /stop 停当前一轮，会话保留，接着能聊。"""
    name = "lr-stop-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "codex", name, "回归测试，只回复 LR-START")
    if not chat or not wait_bot(chat, "LR-START", timeout=240):
        return False, why or "首句没回"
    send(chat, "用 shell 运行 sleep 120，结束后回复 LR-SLEPT")
    time.sleep(25)
    send(chat, "/stop")
    if not wait_bot(chat, "停下", timeout=90):
        return False, "没看到停下回执"
    if any("LR-SLEPT" in t for t in bot_texts(chat)):
        return False, "那一轮没被停住"
    send(chat, "停下后接着聊，只回复 LR-AFTER")
    if not wait_bot(chat, "LR-AFTER", timeout=240):
        return False, "停下后会话没接着回"
    return True, "停下后会话保留并继续回复"


CASES = {
    "ns_claude": case_ns_claude,
    "ns_codex": case_ns_codex,
    "group_relay": case_group_relay,
    "find": case_find,
    "model_effort": case_model_effort,
    "long_reply": case_long_reply,
    "image": case_image,
    "stop": case_stop,
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
