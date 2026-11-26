"""品牌图标（borrow 自官方 20261123 第 36 轮）单测。

全离线：上传用注入的假函数，不碰飞书、不碰网络。覆盖：
- `IconStore`：按 app_id 存取、换 app_id 不串、坏 key 不收、保留别的 app_id 那一格、0600。
- `key_for` / `disable` / `bind` 的三态：没绑 ⇒ ""；绑了有 key ⇒ key；disable 后 ⇒ ""。
- `ensure`：只传缺的、已有的跳过、上传失败不落盘。
- `card_uses_icons`：认 `custom_icon`。
- 回落红线：拿不到 key，总览标题照旧 🤖（见 test_cards 侧，这里只验 key_for 回 ""）。
"""

import json
import os
import stat
import sys
import tempfile
import unittest

from bridge.fleet import brand_icons as bi

APP = "cli_app_one"
APP2 = "cli_app_two"
# 一个合法形状的 img_key（`img_v\d+_...`）
K_CLAUDE = "img_v3_0238_073f1823-df2b-4a00-aaaa-000000000001"
K_CODEX = "img_v3_0238_073f1823-df2b-4a00-aaaa-000000000002"
BAD_KEY = "not-an-img-key"


class IconStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sn-brand-")
        self.path = os.path.join(self.dir, "brand_icons.json")

    def test_put_and_reload(self):
        s = bi.IconStore(self.path, APP)
        s.put("claude", K_CLAUDE)
        self.assertEqual(s.keys().get("claude"), K_CLAUDE)
        # 重新读一份：落盘了
        self.assertEqual(bi.IconStore(self.path, APP).keys().get("claude"), K_CLAUDE)

    def test_file_is_0600(self):
        s = bi.IconStore(self.path, APP)
        s.put("claude", K_CLAUDE)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_rejects_bad_key(self):
        s = bi.IconStore(self.path, APP)
        s.put("claude", BAD_KEY)
        self.assertNotIn("claude", s.keys())

    def test_rejects_unknown_tool(self):
        s = bi.IconStore(self.path, APP)
        s.put("notatool", K_CLAUDE)
        self.assertNotIn("notatool", s.keys())

    def test_other_app_id_not_leaked_and_preserved(self):
        bi.IconStore(self.path, APP).put("claude", K_CLAUDE)
        # 换个 app_id：读不到别人的 key
        other = bi.IconStore(self.path, APP2)
        self.assertEqual(other.keys(), {})
        other.put("codex", K_CODEX)
        # 两个 app 各自那一格都在
        with open(self.path, encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["apps"][APP]["claude"], K_CLAUDE)
        self.assertEqual(raw["apps"][APP2]["codex"], K_CODEX)

    def test_corrupt_cache_is_treated_as_empty(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertEqual(bi.IconStore(self.path, APP).keys(), {})


class KeyForStateTest(unittest.TestCase):
    def tearDown(self):
        bi.bind(None)  # 别把绑定泄漏给别的测试

    def test_unbound_returns_empty(self):
        bi.bind(None)
        self.assertEqual(bi.key_for("claude"), "")

    def test_bound_returns_key(self):
        d = tempfile.mkdtemp(prefix="sn-brand-")
        s = bi.IconStore(os.path.join(d, "c.json"), APP)
        s.put("claude", K_CLAUDE)
        bi.bind(s)
        self.assertEqual(bi.key_for("claude"), K_CLAUDE)
        self.assertEqual(bi.key_for("codex"), "")  # 没传过的回落

    def test_disable_makes_key_for_empty(self):
        d = tempfile.mkdtemp(prefix="sn-brand-")
        s = bi.IconStore(os.path.join(d, "c.json"), APP)
        s.put("claude", K_CLAUDE)
        bi.bind(s)
        bi.disable("飞书拒了")
        self.assertEqual(bi.key_for("claude"), "")
        # bind 一份新的会清掉 disabled
        bi.bind(s)
        self.assertEqual(bi.key_for("claude"), K_CLAUDE)


class EnsureTest(unittest.TestCase):
    def test_uploads_only_missing(self):
        d = tempfile.mkdtemp(prefix="sn-brand-")
        s = bi.IconStore(os.path.join(d, "c.json"), APP)
        s.put("claude", K_CLAUDE)  # 已有 claude
        uploaded = []

        def fake_upload(tool, data):
            uploaded.append(tool)
            return {"claude": K_CLAUDE, "codex": K_CODEX, "cursor": ""}.get(tool) or \
                "img_v3_0238_073f1823-df2b-4a00-bbbb-%012d" % len(uploaded)

        bi.ensure(s, fake_upload)
        self.assertNotIn("claude", uploaded)  # 已有的跳过
        self.assertIn("codex", uploaded)

    def test_upload_failure_not_stored(self):
        d = tempfile.mkdtemp(prefix="sn-brand-")
        s = bi.IconStore(os.path.join(d, "c.json"), APP)
        bi.ensure(s, lambda tool, data: None)  # 全失败
        self.assertEqual(s.keys(), {})


class CardDetectTest(unittest.TestCase):
    def test_detects_custom_icon(self):
        self.assertTrue(bi.card_uses_icons('{"tag":"custom_icon","img_key":"x"}'))
        self.assertFalse(bi.card_uses_icons('{"tag":"standard_icon"}'))
        self.assertFalse(bi.card_uses_icons(None))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
