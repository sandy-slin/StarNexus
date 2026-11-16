import unittest

from bridge.botmenu import MENU_TREE, build_bot_menus, validate_tree


class BotMenuStructureTest(unittest.TestCase):
    def test_user_flow_groups_session_entry_points(self):
        self.assertEqual(validate_tree(), [])
        self.assertEqual(len(build_bot_menus()), 10)
        self.assertEqual([item[0] for item in MENU_TREE], ["🖥 会话", "🧠 模型", "⚡ 常用", "⏹ 停下"])

        session = MENU_TREE[0]
        self.assertEqual(session[1], "")
        self.assertEqual(session[2], (("➕ 新建会话", "/newsession"), ("📋 会话总览（含历史）", "/sessions")))

        common = MENU_TREE[2]
        self.assertEqual([item[0] for item in common[2]], ["🔑 权限", "📂 换项目", "🌐 网页版", "全部按钮"])


if __name__ == "__main__":
    unittest.main()
