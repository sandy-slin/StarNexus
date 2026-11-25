"""11-16 总览的会话行从按钮换成了左对齐的 `interactive_container`（`cards._tap_row`）。

老测试按 `tag == "button"` 找行、读 `text.content` / `type == "primary"`。这里把一个交互容器
**看成**原来那个按钮：文字 = 里面那段 markdown（实体还原回原字）、醒目（蓝边）= primary、
behaviors 原样 ⇒ 老断言不用改意思，只改找节点的那一处。
"""

from __future__ import annotations

import html


def tap_as_button(node):
    """交互容器 → 按钮的样子；不是交互容器 ⇒ None。"""
    if not isinstance(node, dict) or node.get("tag") != "interactive_container":
        return None
    md = next((e for e in node.get("elements") or () if isinstance(e, dict) and e.get("tag") == "markdown"), {})
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": html.unescape(md.get("content", ""))},
        "type": "primary" if node.get("border_color") == "blue" else "default",
        "behaviors": node.get("behaviors") or [],
        "_container": node,
    }
