"""合并转发（merge_forward）：把里面的几条消息展开成一段文字记录，当成一条消息交给会话（1.9.6）。

飞书推过来的 merge_forward 正文只有一句「Merged and Forwarded Message」。
要拿到里面的每一条，得用「获取指定消息」接口取这个 message_id：返回第一项是合并转发本身，
其余每一项是一条子消息（`upper_message_id` 指回它），带发送人、时间、类型和正文。
2026-11-20 用真实的合并转发消息（om_…9c019dda，两条文字）实测过这个形状。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Optional

MAX_ITEMS = 50
MAX_CHARS = 12000

TYPE_LABELS = {
    "image": "[图片]", "file": "[文件]", "media": "[视频]", "audio": "[语音]", "sticker": "[表情]",
    "interactive": "[卡片]", "share_chat": "[群名片]", "share_user": "[个人名片]",
    "merge_forward": "[又一层合并转发]", "location": "[位置]", "video_chat": "[视频会议]",
    "todo": "[任务]", "vote": "[投票]", "system": "[系统消息]",
}


def _post_text(body: dict) -> str:
    bodies = [body] + [v for v in body.values() if isinstance(v, dict)]
    for one in bodies:
        parts = []
        title = one.get("title")
        if isinstance(title, str) and title.strip():
            parts.append(title.strip())
        for line in one.get("content") or []:
            if not isinstance(line, list):
                continue
            seg = []
            for el in line:
                if not isinstance(el, dict):
                    continue
                tag = el.get("tag")
                if tag in ("text", "a", "md"):
                    seg.append(str(el.get("text") or ""))
                elif tag == "at":
                    seg.append("@" + str(el.get("user_name") or el.get("user_id") or ""))
                elif tag == "img":
                    seg.append("[图片]")
                elif tag == "media":
                    seg.append("[视频]")
                elif tag == "code_block":
                    seg.append(str(el.get("text") or ""))
            text = "".join(seg).strip()
            if text:
                parts.append(text)
        if parts:
            return "\n".join(parts)
    return ""


def item_text(msg_type: str, content: str) -> str:
    """一条子消息的正文 ⇒ 一段文字。读不懂的类型给一个方括号占位，别整条丢掉。"""
    try:
        body = json.loads(content or "{}")
    except (TypeError, ValueError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    if msg_type == "text":
        return str(body.get("text") or "").strip()
    if msg_type == "post":
        return _post_text(body) or "[富文本]"
    if msg_type == "file" and body.get("file_name"):
        return f"[文件：{body['file_name']}]"
    return TYPE_LABELS.get(msg_type, f"[{msg_type or '消息'}]")


def render(items: list, *, names: Optional[dict] = None, me: str = "",
           clock: Callable[[float], str] = None) -> str:
    """子消息列表 ⇒ 「时间 发送人：正文」一行一条的记录。超长截断并说明。"""
    names = names or {}
    clock = clock or (lambda ts: time.strftime("%m-%d %H:%M", time.localtime(ts)))
    lines = []
    for it in items[:MAX_ITEMS]:
        sender = it.get("sender") or {}
        sid = str(sender.get("id") or "")
        who = (names.get(sid) or str(sender.get("name") or "").strip()
               or ("我" if sid and sid == me else ("机器人" if sender.get("sender_type") == "app" else "某人")))
        try:
            ts = int(it.get("create_time") or 0) / 1000
        except (TypeError, ValueError):
            ts = 0
        when = clock(ts) if ts else ""
        body = item_text(str(it.get("msg_type") or ""), str((it.get("body") or {}).get("content") or ""))
        saved = it.get("saved") or []
        if saved:
            body += "（已保存到本机：" + "，".join(saved) + "）"
        lines.append(f"{when} {who}：{body}".strip())
    out = "\n".join(lines)
    if len(items) > MAX_ITEMS:
        out += f"\n…（还有 {len(items) - MAX_ITEMS} 条没列出）"
    if len(out) > MAX_CHARS:
        out = out[:MAX_CHARS] + f"\n…（太长，后面 {len(out) - MAX_CHARS} 个字没列出）"
    return out


def compose(transcript: str, count: int) -> str:
    """交给会话的那句话。"""
    return (f"我在飞书里合并转发了 {count} 条消息给你，内容如下：\n\n{transcript}\n\n"
            "请先读懂这些消息，告诉我它们在说什么，再等我下一步指示。")


def fetch(client: Any, message_id: str) -> list:
    """取合并转发里的子消息（不含它自己）。失败抛 RuntimeError（不带 token）。"""
    from lark_oapi.api.im.v1 import GetMessageRequest

    req = GetMessageRequest.builder().message_id(message_id).with_sender_name(True).build()
    resp = client.im.v1.message.get(req)
    if getattr(resp, "code", -1) != 0:
        raise RuntimeError(f"读合并转发失败 code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '')}")
    out = []
    for m in getattr(getattr(resp, "data", None), "items", None) or []:
        if getattr(m, "message_id", "") == message_id or not getattr(m, "upper_message_id", ""):
            continue
        sender = getattr(m, "sender", None)
        body = getattr(m, "body", None)
        name = getattr(sender, "sender_name", "") or ""
        i18n = getattr(sender, "sender_i18n_names", None)
        if not name and i18n is not None:
            name = getattr(i18n, "zh_cn", "") or getattr(i18n, "en_us", "") or ""
        out.append({
            "message_id": getattr(m, "message_id", ""),
            "msg_type": getattr(m, "msg_type", ""),
            "create_time": getattr(m, "create_time", ""),
            "sender": {"id": getattr(sender, "id", ""), "sender_type": getattr(sender, "sender_type", ""),
                       "name": name},
            "body": {"content": getattr(body, "content", "") or ""},
        })
    return out
