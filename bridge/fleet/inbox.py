"""群里发来的图片 / 文件 ⇒ 存进会话项目的 `.starnexus/inbox/`，路径附在消息里发给 Agent（FC-09，§5.3）。

2026-11-17 实测：Codex 0.167 和 claude -p 拿到「附件已保存到 <绝对路径>」这句话都会自己去读图
（Codex 用 view_image，Claude 用 Read），所以不需要给两边各加一套图片参数，走同一条文字通道即可。
"""

from __future__ import annotations

import json
import os
import re
import time

INBOX_REL = os.path.join(".starnexus", "inbox")
MAX_BYTES = 50 * 1024 * 1024
MAX_ATTACHMENTS = 9
_UNSAFE = re.compile(r"[^\w.\-\u4e00-\u9fff]+")
_IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


def attachments_of(mtype: str, content: str) -> list:
    """消息里能下载的附件 ⇒ [(kind, key, name)]；kind 是飞书资源接口的 type（image / file）。"""
    try:
        body = json.loads(content or "{}")
    except (TypeError, ValueError):
        return []
    if not isinstance(body, dict):
        return []
    found: list = []
    if mtype == "image" and body.get("image_key"):
        found.append(("image", str(body["image_key"]), ""))
    elif mtype in ("file", "media") and body.get("file_key"):
        found.append(("file", str(body["file_key"]), str(body.get("file_name") or "")))
    elif mtype == "post":
        bodies = [body] + [v for v in body.values() if isinstance(v, dict)]
        for one in bodies:
            for line in one.get("content") or []:
                if not isinstance(line, list):
                    continue
                for el in line:
                    if not isinstance(el, dict):
                        continue
                    if el.get("tag") == "img" and el.get("image_key"):
                        found.append(("image", str(el["image_key"]), ""))
                    elif el.get("tag") == "media" and el.get("file_key"):
                        found.append(("file", str(el["file_key"]), str(el.get("file_name") or "")))
            if found:
                break
    seen: set = set()
    unique = []
    for item in found:
        if item[1] not in seen:
            seen.add(item[1])
            unique.append(item)
    return unique[:MAX_ATTACHMENTS]


def safe_name(name: str, key: str, kind: str, mime: str = "") -> str:
    """落盘文件名：时间前缀 + 去掉路径和怪字符的原名；没有原名的图片按 key 和类型起名。"""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = _UNSAFE.sub("_", base).strip("._")[:80]
    if not base:
        ext = _IMAGE_EXT.get(str(mime or "").split(";")[0].strip().lower(), ".png" if kind == "image" else "")
        base = f"{kind}-{_UNSAFE.sub('_', key)[-12:]}{ext}"
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{base}"


def inbox_dir(cwd: str) -> str:
    """建好 `<项目>/.starnexus/inbox/`，并放一个忽略全部的 .gitignore，附件不进项目的 Git。"""
    root = os.path.join(cwd, ".starnexus")
    path = os.path.join(cwd, INBOX_REL)
    os.makedirs(path, exist_ok=True)
    ignore = os.path.join(root, ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as handle:
            handle.write("*\n")
    return path


def save(directory: str, filename: str, data: bytes) -> str:
    """写进 inbox，不覆盖同名文件。超过上限抛 ValueError。"""
    if len(data) > MAX_BYTES:
        raise ValueError(f"附件超过 {MAX_BYTES // (1024 * 1024)}MB")
    target = os.path.join(directory, filename)
    stem, ext = os.path.splitext(target)
    n = 1
    while os.path.exists(target):
        target = f"{stem}-{n}{ext}"
        n += 1
    with open(target, "wb") as handle:
        handle.write(data)
    return target


def compose(text: str, paths: list) -> str:
    """发给 Agent 的那句话：原文 + 附件路径。只有附件时请它先看附件。"""
    lines = [f"- {p}" for p in paths]
    head = "我在飞书里发了附件，已保存到本机：" if len(paths) > 1 else "我在飞书里发了一个附件，已保存到本机："
    ask = str(text or "").strip() or "请先查看附件内容，告诉我里面是什么，再等我下一步指示。"
    return f"{ask}\n\n{head}\n" + "\n".join(lines)


def download(client, message_id: str, key: str, kind: str) -> tuple:
    """调飞书「获取消息中的资源文件」⇒ (bytes, 文件名, content-type)。失败抛 RuntimeError（不带 token）。"""
    from lark_oapi.api.im.v1 import GetMessageResourceRequest

    req = GetMessageResourceRequest.builder().message_id(message_id).file_key(key).type(kind).build()
    resp = client.im.v1.message_resource.get(req)
    if getattr(resp, "code", -1) != 0 or getattr(resp, "file", None) is None:
        raise RuntimeError(f"下载附件失败 code={getattr(resp, 'code', '?')} msg={getattr(resp, 'msg', '')}")
    raw = getattr(resp, "raw", None)
    headers = getattr(raw, "headers", None) or {}
    mime = headers.get("Content-Type") or headers.get("content-type") or ""
    return resp.file.read(), str(getattr(resp, "file_name", "") or ""), str(mime)
