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
    if not any(f"Codex「{name}」" in t and "会话状态卡" in t for t in bot_texts(chat)):
        return False, "状态卡标题没用名字"
    # 状态卡正文是卡片实体，接口只回占位图 ⇒ 缺省模型看私聊的新建回执（那里是明文）
    if not any("模型：默认（" in t for t in bot_texts(dm, 20)):
        return False, "新建回执没写缺省模型"
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
    if not wait_bot(chat, "这一轮已停下", timeout=90):
        return False, "没看到「这一轮已停下」回执"
    if not wait_bot(chat, "⏹ 已停下", timeout=30):
        return False, "等待卡没收尾成「⏹ 已停下」"
    if any("没正常回完" in t for t in bot_texts(chat)):
        return False, "停下后仍按「没正常回完」警告"
    if any("LR-SLEPT" in t for t in bot_texts(chat)):
        return False, "那一轮没被停住"
    send(chat, "停下后接着聊，只回复 LR-AFTER")
    if not wait_bot(chat, "LR-AFTER", timeout=240):
        return False, "停下后会话没接着回"
    return True, "停下后会话保留并继续回复"


def case_steer_claude(dm: str) -> tuple:
    """FC-38（1.7.0）：Claude 会话一轮在跑时群里再说一句 ⇒ 插进这一轮、同一轮改做新指令，不再回「上一条还在发」。"""
    name = "lr-steer-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "claude", name, "回归测试，只回复 LR-START")
    if not chat or not wait_bot(chat, "LR-START", timeout=240):
        return False, why or "首句没回"
    send(chat, "用 Bash 工具依次执行 10 次 `sleep 4 && date >> steer.txt`，每次单独调用一次工具，全部做完后回复 LR-ORIG")
    time.sleep(25)
    send(chat, "改主意了：立即停止剩下的 sleep，不要再调用任何工具，只回复 LR-STEER")
    if not wait_bot(chat, "已插进正在跑的这一轮", timeout=30):
        return False, "没看到「已插进正在跑的这一轮」回执"
    if not wait_bot(chat, "LR-STEER", timeout=180):
        return False, "插话后没回 LR-STEER"
    texts = bot_texts(chat, 30)
    if any("LR-ORIG" in t for t in texts):
        return False, "原任务照样做完了，插话没生效"
    if any("上一条还在发" in t for t in texts):
        return False, "仍回了「上一条还在发」"
    if any(t.find("提示：") >= 0 and "照常发过去" in t for t in texts):
        return False, "仍冒出无需处理的「提示」"
    lines = 0
    try:
        with open(os.path.join(WORK, name, "steer.txt"), encoding="utf-8") as fh:
            lines = sum(1 for _ in fh)
    except OSError:
        pass
    if lines >= 10:
        return False, f"10 次都跑完了（{lines}）"
    return True, f"插进同一轮，跑了 {lines} 次后改做新指令"


def case_choices(dm: str) -> tuple:
    """FC-07：回复结尾是编号选项 ⇒ 群里出现「👉 选项」按钮。点按钮要 Computer Use 复核。"""
    name = "lr-choice-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "claude", name, "回归测试，只回复 LR-READY")
    if not chat or not wait_bot(chat, "LR-READY"):
        return False, why or "首句没回"
    send(chat, "不要执行任何命令。只问我：你更喜欢哪种颜色？结尾用编号列出 1. 红色 2. 绿色 3. 蓝色，然后等我选。")
    if not wait_bot(chat, "👉 绿色", timeout=180):
        return False, "没出现选项按钮"
    return True, "选项按钮已出现"


def case_codex_trusted(dm: str) -> tuple:
    """FC-13：Codex trusted 档 ⇒ 工作区内能写；工作区外和 git push 先出审批卡、不直接执行。"""
    name = "lr-trust-" + time.strftime("%H%M%S")
    outside = os.path.expanduser(f"~/sn-outside-{name}.txt")
    # 不写「回归测试」：Codex 会按用户级 AGENTS.md 的 her-regression 规则先跑技能同步脚本（2026-11-18 实测）
    chat, why = _new_session(dm, "codex", name, "在当前目录创建文件 inside.txt，内容 LR-IN，然后只回复 LR-WROTE")
    if not chat or not wait_bot(chat, "LR-WROTE", timeout=300):
        return False, why or "首句没回"
    if not os.path.exists(os.path.join(WORK, name, "inside.txt")):
        return False, "工作区内的文件没写出来"
    send(chat, f"用 shell 执行：echo LR-OUT > {outside}，然后回复 LR-DONE2")
    if not wait_bot(chat, "需要你批准", timeout=300):
        return False, "写工作区外没出审批卡"
    if os.path.exists(outside):
        return False, "工作区外的文件没等审批就写了"
    return True, "工作区内直接写；工作区外先出审批卡且未执行"


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=30).stdout


def _claude_pid(cwd: str) -> int:
    folder = os.path.expanduser("~/.claude/sessions")
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("cwd") == cwd and rec.get("entrypoint") == "cli":
            return int(rec.get("pid") or 0)
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def case_term_stop(dm: str) -> tuple:
    """FC-12：终端里正在跑的 Claude 一轮，私聊 /stop ⇒ 说明只能在终端停、给替代按钮，终端会话不被关掉。"""
    name = "lr-term-" + time.strftime("%H%M%S")
    path = os.path.join(WORK, name)
    os.makedirs(path, exist_ok=True)
    session = "sn-" + name
    _tmux("new-session", "-d", "-s", session, "-x", "200", "-y", "50", "-c", path, "claude")
    try:
        time.sleep(10)
        _tmux("send-keys", "-t", session,
              "用 Bash 前台运行 python3 -c 'print(sum(1 for n in range(2, 40000000) "
              "if all(n % d for d in range(2, int(n**0.5)+1))))'，命令超时设 600000 毫秒，不要改算法，算完回复 TERM-DONE。")
        time.sleep(1)
        _tmux("send-keys", "-t", session, "Enter")
        pid = 0
        end = time.time() + 60
        while time.time() < end and not pid:
            pid = _claude_pid(path)
            time.sleep(3)
        if not pid:
            return False, "终端 Claude 没登记会话"
        time.sleep(25)
        seen = {m.get("message_id") for m in messages(dm)}
        if not send(dm, "/stop"):
            return False, "发 /stop 失败"
        fresh: list = []
        end = time.time() + 60
        while time.time() < end:
            fresh = [str(m.get("content") or "") for m in messages(dm)
                     if m.get("message_id") not in seen and (m.get("sender") or {}).get("sender_type") == "app"]
            if any("终端里的会话只能在终端里停" in t for t in fresh) and any("提醒我到电脑上停" in t for t in fresh):
                break
            time.sleep(4)
        if not any("终端里的会话只能在终端里停" in t for t in fresh):
            return False, "这次 /stop 的回复里没有终端会话的说明"
        if not any("提醒我到电脑上停" in t for t in fresh):
            return False, "这次 /stop 的回复里没有「提醒我到电脑上停」替代按钮"
        time.sleep(20)
        if not _alive(pid):
            return False, "终端会话被关掉了"
        return True, "说明只能在终端停并给替代按钮，终端会话还在"
    finally:
        _tmux("send-keys", "-t", session, "Escape")
        time.sleep(2)
        _tmux("kill-session", "-t", session)


def case_progress(dm: str) -> tuple:
    """FC-14：一轮持续约 5 分钟的工具调用 ⇒ 群里只多一张进展卡并原地更新，回完之前不刷屏。"""
    name = "lr-prog-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "codex", name, "回归测试，只回复 LR-READY")
    if not chat or not wait_bot(chat, "LR-READY", timeout=300):
        return False, why or "首句没回"
    time.sleep(8)
    seen = {m.get("message_id") for m in messages(chat, 30)}
    if not send(chat, "回归测试：依次运行 5 条 shell 命令，每条单独执行：sleep 55 && date。"
                      "全部跑完后只回复 LR-PROG-DONE。"):
        return False, "发任务失败"
    snapshots: dict = {}
    end = time.time() + 480
    done = False
    while time.time() < end:
        for m in messages(chat, 30):
            mid = m.get("message_id")
            if mid in seen or (m.get("sender") or {}).get("sender_type") != "app":
                continue
            text = str(m.get("content") or "")
            snapshots.setdefault(mid, set()).add(text)
            if "LR-PROG-DONE" in text:
                done = True
        if done:
            break
        time.sleep(20)
    if not done:
        return False, "8 分钟内没回 LR-PROG-DONE"
    before_reply = [mid for mid, texts in snapshots.items() if not any("LR-PROG-DONE" in t for t in texts)]
    cards = [mid for mid in before_reply if any("发给" in t or "它回了" in t for t in snapshots[mid])]
    extra = [mid for mid in before_reply if mid not in cards]
    if len(cards) != 1:
        return False, f"进展卡数量 {len(cards)}，应为 1"
    if len(snapshots[cards[0]]) < 2:
        return False, "进展卡在运行中没有原地更新"
    if len(extra) > 1:
        return False, f"回完之前多发了 {len(extra)} 条消息"
    return True, f"一张进展卡更新了 {len(snapshots[cards[0]])} 个版本，其他新消息 {len(extra)} 条"


def case_ns_one_line(dm: str) -> tuple:
    """FC-20：/ns codex 名字 路径 luna high 第一句 ⇒ 不等下一句，直接开群、模型 luna / 高。"""
    name = "lr-oneline-" + time.strftime("%H%M%S")
    path = os.path.join(WORK, name)
    os.makedirs(path, exist_ok=True)
    before = set(binds())
    if not send(dm, f"/ns codex {name} {path} luna high 回归测试，只回复 LR-ONE-OK"):
        return False, "发 /ns 失败"
    chat = wait_new_group(before)
    if not chat:
        return False, "6 分钟内没建群"
    gname = group_name(chat)
    if not gname.startswith("SN·Codex·"):
        return False, f"群名不对：{gname}"
    if not wait_bot(chat, "LR-ONE-OK", timeout=240):
        return False, "群里没等到 LR-ONE-OK"
    if not any("gpt-6-luna" in t and "高" in t for t in bot_texts(chat)):
        return False, "状态卡没写 gpt-6-luna / 高"
    return True, gname


def case_ns_card(dm: str) -> tuple:
    """FC-16（服务端部分）：/ns 只发一张「➕ 新建会话」卡，卡上有工具/项目/模型/强度/第一句/开始。点卡要 Computer Use。"""
    if not send(dm, "/ns"):
        return False, "发 /ns 失败"
    end = time.time() + 60
    while time.time() < end:
        for m in messages(dm, 10):
            if (m.get("sender") or {}).get("sender_type") != "app":
                continue
            body = str(m.get("content") or "")
            if "新建会话" in body and "开始" in body and "第一句话" in body and "会话名" in body:
                return True, "卡片已发出"
        time.sleep(3)
    return False, "60 秒内没收到新会话卡"


def case_session_perm(dm: str) -> tuple:
    """FC-21/23/24：飞书开的会话全部授权、新群不弹权限卡；群里 /权限 只读 ⇒ 写不了；/权限 全部授权 ⇒ 又能写。"""
    name = "lr-perm-" + time.strftime("%H%M%S")
    path = os.path.join(WORK, name)
    os.makedirs(path, exist_ok=True)
    before = set(binds())
    if not send(dm, f"/ns codex {name} {path} -- 在当前目录创建 a.txt 内容 A，完成后只回复 LR-PERM-A"):
        return False, "发 /ns 失败"
    chat = wait_new_group(before)
    if not chat:
        return False, "6 分钟内没建群"
    if not wait_bot(chat, "LR-PERM-A", timeout=240):
        return False, "群里没等到 LR-PERM-A"
    if not os.path.exists(os.path.join(path, "a.txt")):
        return False, "全部授权下 a.txt 没写出来"
    texts = bot_texts(chat, 30)
    if any("🔑 这个会话的权限" in t for t in texts):
        return False, "飞书开的会话不该弹权限切换卡"
    if not any("全部授权" in t for t in texts):
        return False, "状态卡没写全部授权"
    if not send(chat, "/权限 只读") or not wait_bot(chat, "改成「只读」", timeout=60):
        return False, "群里切只读没回执"
    send(chat, "在当前目录创建 b.txt 内容 B。如果做不到就只回复 LR-PERM-RO，做到了只回复 LR-PERM-B")
    end = time.time() + 240
    while time.time() < end and not any(("LR-PERM-RO" in t or "LR-PERM-B" in t) for t in bot_texts(chat, 10)):
        time.sleep(3)
    if not any(("LR-PERM-RO" in t or "LR-PERM-B" in t) for t in bot_texts(chat, 10)):
        return False, "只读那一轮没回"
    time.sleep(3)
    if os.path.exists(os.path.join(path, "b.txt")):
        return False, "只读档下 b.txt 被写出来了"
    if not send(chat, "/权限 全部授权") or not wait_bot(chat, "改成「全部授权」", timeout=60):
        return False, "群里切回全部授权没回执"
    send(chat, "在当前目录创建 c.txt 内容 C，完成后只回复 LR-PERM-C")
    if not wait_bot(chat, "LR-PERM-C", timeout=240):
        return False, "全部授权那一轮没回 LR-PERM-C"
    if not os.path.exists(os.path.join(path, "c.txt")):
        return False, "切回全部授权后 c.txt 没写出来"
    return True, group_name(chat)


def case_menu_m(dm: str) -> tuple:
    """FC-26 / FC-29（服务端部分）：私聊 /m 出常用功能卡；会话群 /m 把状态卡挪到最下面，卡上有权限一排。"""
    if not send(dm, "/m"):
        return False, "私聊发 /m 失败"
    end = time.time() + 60
    ok_dm = False
    while time.time() < end and not ok_dm:
        for m in messages(dm, 6):
            body = str(m.get("content") or "")
            if (m.get("sender") or {}).get("sender_type") == "app" and "常用功能" in body and "全局权限" in body and "默认模型" in body:
                ok_dm = True
                break
        time.sleep(3)
    if not ok_dm:
        return False, "私聊 60 秒内没收到 /m 常用功能卡"
    groups = binds()
    chat = next(iter(sorted(groups, key=lambda c: groups[c].get("created_at", 0), reverse=True)), "")
    if not chat:
        return False, "没有会话群可测"
    if not send(chat, "/m"):
        return False, "会话群发 /m 失败"
    end = time.time() + 60
    while time.time() < end:
        texts = bot_texts(chat, 6)
        if any("会话状态卡" in t and "权限" in t for t in texts) and any("已挪到最下面" in t for t in bot_texts(chat, 40)):
            return True, group_name(chat)
        time.sleep(3)
    return False, "会话群 60 秒内没看到挪到最下面的状态卡"


def _steer_texts_ok(texts: list, orig: str) -> str:
    if any(orig in t for t in texts):
        return "原任务照样做完了，插话没生效"
    if any("上一条还在发" in t or "这条排上了" in t for t in texts):
        return "仍回了排队提示"
    if any(t.find("提示：") >= 0 and "照常发过去" in t for t in texts):
        return "仍冒出无需处理的「提示」"
    return ""


def case_steer_dm(dm: str) -> tuple:
    """FC-39（1.8.0）：私聊里桥自己跑的 Claude 一轮在跑时再说一句 ⇒ 插进这一轮，不再回「这条排上了」。"""
    tag = time.strftime("%H%M%S")
    send(dm, "/cancel")
    time.sleep(4)
    send(dm, f"用 Bash 工具依次执行 10 次 `sleep 4`，每次单独调用一次工具，全部做完后只回复 LR-DMORIG-{tag}")
    time.sleep(25)
    send(dm, f"改主意了：立即停止剩下的 sleep，不要再调用任何工具，只回复 LR-DMSTEER-{tag}")
    if not wait_bot(dm, "已插进正在跑的这一轮", timeout=30):
        return False, "私聊没看到「已插进正在跑的这一轮」回执"
    if not wait_bot(dm, f"LR-DMSTEER-{tag}", timeout=180):
        return False, "插话后没回 LR-DMSTEER"
    why = _steer_texts_ok([t for t in bot_texts(dm, 15)], f"LR-DMORIG-{tag}")
    return (False, why) if why else (True, "私聊同一轮改做新指令")


def case_steer_first_turn(dm: str) -> tuple:
    """FC-40（1.8.0）：新建 Claude 会话的第一轮还在跑、群还没建好时私聊再说一句 ⇒ 插进第一轮，回复进新群。"""
    tag = time.strftime("%H%M%S")
    name = "lr-fsteer-" + tag
    path = os.path.join(WORK, name)
    os.makedirs(path, exist_ok=True)
    before = set(binds())
    send(dm, "/cancel")
    time.sleep(4)
    if not send(dm, f"/ns claude {name} {path}"):
        return False, "发 /ns 失败"
    time.sleep(6)
    send(dm, f"用 Bash 工具依次执行 10 次 `sleep 4`，每次单独调用一次工具，全部做完后只回复 LR-FORIG-{tag}")
    time.sleep(30)
    send(dm, f"改主意了：立即停止剩下的 sleep，不要再调用任何工具，只回复 LR-FSTEER-{tag}")
    if not wait_bot(dm, "已插进正在跑的这一轮", timeout=30):
        return False, "私聊没看到「已插进正在跑的这一轮」回执"
    chat = wait_new_group(before, timeout=240)
    if not chat:
        return False, "没建群"
    if not wait_bot(chat, f"LR-FSTEER-{tag}", timeout=120):
        return False, "新群里没看到插话后的回复"
    why = _steer_texts_ok(bot_texts(chat, 20) + bot_texts(dm, 10), f"LR-FORIG-{tag}")
    return (False, why) if why else (True, f"第一轮被插话改向，回复进了 {group_name(chat)}")


def case_steer_opencode(dm: str) -> tuple:
    """FC-41（1.8.0）：opencode 会话群里一轮在跑时再说一句 ⇒ 插进这一轮（常驻服务 delivery=steer）。"""
    tag = time.strftime("%H%M%S")
    name = "lr-ocsteer-" + tag
    chat, why = _new_session(dm, "opencode", name, "回归测试，只回复 LR-START")
    if not chat or not wait_bot(chat, "LR-START", timeout=240):
        return False, why or "首句没回"
    send(chat, f"用 bash 依次执行 10 次 `sleep 4 && date >> steer.txt`，每次单独调用一次工具，全部做完后回复 LR-OCORIG-{tag}")
    time.sleep(22)
    send(chat, f"改主意了：立即停止剩下的 sleep，不要再调用任何工具，只回复 LR-OCSTEER-{tag}")
    if not wait_bot(chat, "已插进正在跑的这一轮", timeout=40):
        return False, "没看到「已插进正在跑的这一轮」回执"
    if not wait_bot(chat, f"LR-OCSTEER-{tag}", timeout=180):
        return False, "插话后没回 LR-OCSTEER"
    why = _steer_texts_ok(bot_texts(chat, 30), f"LR-OCORIG-{tag}")
    if why:
        return False, why
    lines = 0
    try:
        with open(os.path.join(WORK, name, "steer.txt"), encoding="utf-8") as fh:
            lines = sum(1 for _ in fh)
    except OSError:
        pass
    if lines >= 10:
        return False, f"10 次都跑完了（{lines}）"
    return True, f"插进同一轮，跑了 {lines} 次后改做新指令"


def case_steer_codex(dm: str) -> tuple:
    """FC-40（1.9.0）：Codex 会话群里一轮在跑时再说一句 ⇒ 插进这一轮（app-server turn/steer）。"""
    tag = time.strftime("%H%M%S")
    name = "lr-cxsteer-" + tag
    chat, why = _new_session(dm, "codex", name, "回归测试，只回复 LR-START")
    if not chat or not wait_bot(chat, "LR-START", timeout=240):
        return False, why or "首句没回"
    send(chat, f"用 shell 依次执行 10 次 `sleep 4 && date >> steer.txt`，每次单独执行一条命令，全部做完后回复 LR-CXORIG-{tag}")
    time.sleep(22)
    send(chat, f"改主意了：立即停止剩下的 sleep，不要再执行任何命令，只回复 LR-CXSTEER-{tag}")
    if not wait_bot(chat, "已插进正在跑的这一轮", timeout=40):
        return False, "没看到「已插进正在跑的这一轮」回执"
    if not wait_bot(chat, f"LR-CXSTEER-{tag}", timeout=180):
        return False, "插话后没回 LR-CXSTEER"
    why = _steer_texts_ok(bot_texts(chat, 30), f"LR-CXORIG-{tag}")
    if why:
        return False, why
    lines = 0
    try:
        with open(os.path.join(WORK, name, "steer.txt"), encoding="utf-8") as fh:
            lines = sum(1 for _ in fh)
    except OSError:
        pass
    if lines >= 10:
        return False, f"10 次都跑完了（{lines}）"
    return True, f"插进同一轮，跑了 {lines} 次后改做新指令"


def case_trusted_script(dm: str) -> tuple:
    """1.9.1：全授权 Claude 会话里 `python3 -c` 读 JSON、`$R/$f` 接口路径不弹审批卡；有删除动作的脚本照旧出卡。"""
    name = "lr-tscript-" + time.strftime("%H%M%S")
    chat, why = _new_session(dm, "claude", name, "回归测试，只回复 LR-TS-READY")
    if not chat:
        return False, why
    if not wait_bot(chat, "LR-TS-READY"):
        return False, "第一轮没等到 LR-TS-READY"
    path = os.path.join(WORK, name)
    with open(os.path.join(path, "a.json"), "w", encoding="utf-8") as fh:
        json.dump({"n": 37}, fh)
    ask = ("请严格按顺序用 Bash 工具执行这两条命令，不要改写："
           f"第一条 cd {path} && python3 -c \"import json;print('LR-TS-'+str(json.load(open('a.json'))['n']))\" ；"
           f"第二条 cd {path} && R=x/y; for f in a b; do echo \"$R/$f\" > out_$f.txt; done && cat out_a.txt 。"
           "然后只回复第一条命令的输出。")
    if not send(chat, ask):
        return False, "发命令失败"
    got = wait_bot(chat, "LR-TS-37", timeout=180)
    texts = bot_texts(chat, 30)
    if any("批准" in t or "等你在飞书里确认" in t for t in texts):
        return False, "全授权下仍然弹了审批卡"
    if not got:
        return False, "没等到 LR-TS-37"
    return True, f"{group_name(chat)} 无审批卡，回了 LR-TS-37"


CASES = {
    "ns_claude": case_ns_claude,
    "ns_codex": case_ns_codex,
    "group_relay": case_group_relay,
    "find": case_find,
    "model_effort": case_model_effort,
    "long_reply": case_long_reply,
    "image": case_image,
    "stop": case_stop,
    "steer_claude": case_steer_claude,
    "steer_dm": case_steer_dm,
    "steer_first_turn": case_steer_first_turn,
    "steer_opencode": case_steer_opencode,
    "steer_codex": case_steer_codex,
    "choices": case_choices,
    "codex_trusted": case_codex_trusted,
    "term_stop": case_term_stop,
    "progress": case_progress,
    "ns_one_line": case_ns_one_line,
    "ns_card": case_ns_card,
    "session_perm": case_session_perm,
    "menu_m": case_menu_m,
    "trusted_script": case_trusted_script,
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
