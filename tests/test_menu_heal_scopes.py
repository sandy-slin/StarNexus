"""常驻菜单自愈：权限清单变了 ⇒ 发版前先把缺的权限申请上（2026-11-17，FC-09 撞出来）。"""
import os
import tempfile
import unittest

from tests import helpers  # noqa: F401

from bridge import menu_heal, register


class FakeFeishu:
    def __init__(self, have, add_code=0):
        self.have = have
        self.add_code = add_code
        self.calls = []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, body))
        if path.startswith("/auth/"):
            return {"code": 0, "tenant_access_token": "t"}
        if path == "/application/v6/scopes":
            return {"code": 0, "data": {"scopes": [
                {"scope_name": s, "scope_type": "tenant", "grant_status": 1} for s in self.have]}}
        if path.endswith("/config"):
            return {"code": self.add_code, "msg": "x"}
        if path.endswith("/publish"):
            return {"code": 0, "data": {"version": "1.2.3"}}
        return {"code": 0}

    def paths(self):
        return ["SCOPE-ADD" if isinstance(b, dict) and "scope" in b else p for _m, p, b in self.calls]


class HealScopesTest(unittest.TestCase):
    def heal(self, fake):
        state = os.path.join(tempfile.mkdtemp(), "menu_heal.json")
        return menu_heal.MenuHeal("cli_x", "s", state, http=fake, clock=lambda: 1794850000.0)

    def test_missing_scope_added_before_publish(self):
        have = [s for s in register.WANT_SCOPES if s != "im:message:readonly"]
        fake = FakeFeishu(have)
        self.assertTrue(self.heal(fake).attempt())
        paths = fake.paths()
        add = paths.index("SCOPE-ADD")
        self.assertLess(add, paths.index("/application/v7/applications/cli_x/publish"))
        body = fake.calls[add][2]
        self.assertEqual([x["scope_name"] for x in body["scope"]["add_scopes"]], ["im:message:readonly"])

    def test_scope_not_effective_logged(self):
        missing = register.WANT_SCOPES[-1]
        have = [sc for sc in register.WANT_SCOPES if sc != missing]
        fake = FakeFeishu(have)
        with self.assertLogs("feishu-bridge", level="WARNING") as logs:
            self.assertTrue(self.heal(fake).attempt())
        self.assertIn("还没生效 " + missing, "\n".join(logs.output))

    def test_nothing_missing_no_add(self):
        fake = FakeFeishu(list(register.WANT_SCOPES))
        self.assertTrue(self.heal(fake).attempt())
        self.assertNotIn("SCOPE-ADD", fake.paths())

    def test_add_failure_does_not_publish(self):
        fake = FakeFeishu([], add_code=99991672)
        self.assertFalse(self.heal(fake).attempt())
        self.assertNotIn("/application/v7/applications/cli_x/publish", fake.paths())

    def test_readonly_scope_wanted(self):
        self.assertIn("im:message:readonly", register.WANT_SCOPES)
        self.assertNotIn("im:message", register.WANT_SCOPES)

    def test_urgent_scope_not_wanted(self):
        self.assertFalse(any("urgent" in sc for sc in register.WANT_SCOPES))


if __name__ == "__main__":
    unittest.main()
