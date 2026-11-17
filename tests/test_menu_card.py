import json
import re
import unittest

from bridge.menu import build_menu_card

_BROKEN_BOLD = re.compile(r"[：，。、；！？）」:,.;!?)]\*\*[^\s*：，。、；！？）」:,.;!?)]")


def _markdown_texts(node):
    if isinstance(node, dict):
        if node.get("tag") == "markdown" and isinstance(node.get("content"), str):
            yield node["content"]
        for value in node.values():
            yield from _markdown_texts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _markdown_texts(item)


class MenuCardMarkdownTest(unittest.TestCase):
    def test_bold_markers_render_in_feishu(self):
        card = json.loads(build_menu_card("~"))
        texts = list(_markdown_texts(card))
        self.assertTrue(any(text.startswith("**更多**：") for text in texts))
        broken = [text for text in texts if _BROKEN_BOLD.search(text)]
        self.assertEqual(broken, [])


if __name__ == "__main__":
    unittest.main()
