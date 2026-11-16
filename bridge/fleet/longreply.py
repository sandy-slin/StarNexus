"""长回复 ⇒ 飞书文档，群里只放摘要和链接（FC-10，§5.3）。

用应用身份建文档：建空文档 → Markdown 转块 → 写进文档 → 把主人加成可管理的协作者。
2026-11-17 实测各步要的权限（都挑最窄的那一项，清单在 `register._BASE_SCOPES`）：
建文档 `docx:document:create`，转块 `docx:document.block:convert`，写块 `docx:document:write_only`，
加协作者 `docs:permission.member:create`。任何一步失败 ⇒ 抛 RuntimeError，调用方退回分段发原文，不丢内容。
"""

from __future__ import annotations

import re

THRESHOLD = 1500
SUMMARY_CHARS = 300
TITLE_CHARS = 60
DOC_HOST = "https://feishu.cn/docx/"
BLOCKS_PER_CALL = 900


def is_long(text: str) -> bool:
    return len(str(text or "")) > THRESHOLD


def summary(text: str) -> str:
    """开头几段（不切断代码块），最多 SUMMARY_CHARS 字。"""
    out: list = []
    size = 0
    in_code = False
    for line in str(text or "").splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code or not line.strip():
            continue
        clean = re.sub(r"^#+\s*", "", line).strip()
        if size + len(clean) > SUMMARY_CHARS:
            room = SUMMARY_CHARS - size
            if room > 20:
                out.append(clean[:room] + "…")
            break
        out.append(clean)
        size += len(clean)
    return "\n".join(out) or str(text or "")[:SUMMARY_CHARS]


def title_for(label: str, text: str) -> str:
    first = ""
    for line in str(text or "").splitlines():
        line = re.sub(r"^[#>*\-\s]+", "", line).strip(" *`")
        if line:
            first = line
            break
    head = f"{label} · {first}" if first else str(label)
    return head[:TITLE_CHARS]


def _ok(out: dict, step: str) -> dict:
    if not isinstance(out, dict) or out.get("code"):
        code = out.get("code") if isinstance(out, dict) else "?"
        msg = str(out.get("msg") if isinstance(out, dict) else "")[:160]
        raise RuntimeError(f"{step}失败 code={code} {msg}")
    return out.get("data") or {}


def _strip_merge_info(blocks: list) -> list:
    """转换接口给的表格块带只读字段 merge_info，原样写回会被拒（官方文档的提示）。"""
    for block in blocks:
        table = block.get("table") if isinstance(block, dict) else None
        if isinstance(table, dict) and isinstance(table.get("property"), dict):
            table["property"].pop("merge_info", None)
    return blocks


def publish(http, token: str, title: str, markdown: str, owner_open_id: str = "") -> str:
    """建文档并写入 markdown ⇒ 文档链接。`http(method, path, token, body)` 同 botmenu._http。"""
    doc = _ok(http("POST", "/docx/v1/documents", token, {"title": title}), "建文档")
    doc_id = ((doc.get("document") or {}).get("document_id")) or ""
    if not doc_id:
        raise RuntimeError("建文档失败：没拿到文档 id")
    conv = _ok(http("POST", "/docx/v1/documents/blocks/convert", token,
                    {"content_type": "markdown", "content": markdown}), "转换正文")
    first = list(conv.get("first_level_block_ids") or [])
    blocks = _strip_merge_info(list(conv.get("blocks") or []))
    if first:
        by_id = {b.get("block_id"): b for b in blocks if isinstance(b, dict)}
        index = 0
        for start in range(0, len(first), BLOCKS_PER_CALL):
            batch = first[start:start + BLOCKS_PER_CALL]
            wanted: list = []
            stack = list(batch)
            while stack:
                bid = stack.pop()
                block = by_id.get(bid)
                if block is None:
                    continue
                wanted.append(block)
                stack.extend(block.get("children") or [])
            _ok(http("POST", f"/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant", token,
                     {"children_id": batch, "descendants": wanted, "index": index}), "写入正文")
            index += len(batch)
    if owner_open_id:
        _ok(http("POST", f"/drive/v1/permissions/{doc_id}/members?type=docx&need_notification=false", token,
                 {"member_type": "openid", "member_id": owner_open_id, "perm": "full_access"}), "授权给你")
    return DOC_HOST + doc_id


EXPORT_MESSAGES = 200


def transcript_markdown(title: str, messages: list) -> str:
    """会话记录 ⇒ 导出文档的 Markdown：每一轮一个小节，「你」和「它」分开。"""
    lines = [f"# {title}", ""]
    turn = 0
    for msg in messages:
        role = getattr(msg, "role", "")
        text = str(getattr(msg, "text", "") or "").strip()
        if not text:
            continue
        if role == "user":
            turn += 1
            lines += [f"## 第 {turn} 轮", "", "**你**：", "", text, ""]
        else:
            lines += ["**它**：", "", text, ""]
    if turn == 0 and len(lines) == 2:
        lines.append("（这个会话还没有可导出的对话）")
    return "\n".join(lines)


def group_text(label: str, text: str, url: str) -> str:
    return (f"{label} 回复较长（{len(text)} 字），全文已放进飞书文档：\n[打开全文]({url})\n\n"
            f"**摘要**\n{summary(text)}")
