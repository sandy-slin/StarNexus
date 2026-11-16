"""FC-15：旧会话群补 `SN·` 前缀（只改我们起的、没人动过的群名）。"""
import unittest

from tests import helpers  # noqa: F401

from bridge.fleet.chatbind import (KEPT_OWN, NO_RENAME, RENAMED, ChatBinds, migrate_sn_prefix, sn_prefixed)


class NameTest(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(sn_prefixed("Codex · 修登录"), "SN·Codex·修登录")
        self.assertEqual(sn_prefixed("群推送测试"), "SN·群推送测试")
        self.assertEqual(sn_prefixed("SN·Codex·x"), "SN·Codex·x")


class FakeClient:
    def __init__(self, current, fail=False):
        self.current = current
        self.fail = fail
        self.renamed = []

    def name_of(self, chat_id):
        return self.current.get(chat_id, ""), 0, ""

    def rename(self, chat_id, name):
        if self.fail:
            return False, 99991672, "denied"
        self.renamed.append((chat_id, name))
        self.current[chat_id] = name
        return True, 0, ""


class MigrateTest(unittest.TestCase):
    def make(self, rows):
        import tempfile, os
        binds = ChatBinds(os.path.join(tempfile.mkdtemp(), "chat_binds.json"))
        for chat_id, tool, sid, name in rows:
            binds.bind(chat_id, tool, sid, name)
        return binds

    def test_renames_only_ours(self):
        binds = self.make([("oc_a", "codex", "s1", "Codex · 甲"), ("oc_b", "claude", "s2", "Claude·乙"),
                           ("oc_c", "codex", "s3", "SN·Codex·丙")])
        client = FakeClient({"oc_a": "Codex · 甲", "oc_b": "我自己改的名字"})
        out = {r[0]: r[1] for r in migrate_sn_prefix(binds, client)}
        self.assertEqual(out, {"oc_a": RENAMED, "oc_b": KEPT_OWN})
        self.assertEqual(client.renamed, [("oc_a", "SN·Codex·甲")])

    def test_failure_keeps_recorded_name(self):
        binds = self.make([("oc_a", "codex", "s1", "Codex · 甲")])
        client = FakeClient({"oc_a": "Codex · 甲"}, fail=True)
        self.assertEqual(migrate_sn_prefix(binds, client)[0][1], NO_RENAME)
        self.assertEqual(binds.of_chat("oc_a").name, "Codex · 甲")

    def test_idempotent(self):
        binds = self.make([("oc_a", "codex", "s1", "Codex · 甲")])
        client = FakeClient({"oc_a": "Codex · 甲"})
        self.assertEqual(migrate_sn_prefix(binds, client)[0][1], RENAMED)
        self.assertEqual(migrate_sn_prefix(binds, client), [])
        self.assertEqual(len(client.renamed), 1)


if __name__ == "__main__":
    unittest.main()
