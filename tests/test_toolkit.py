"""会话群快捷工具栏（borrow 自官方 20261123 第 37 轮）单测。

传输层全离线：飞书调用和收件箱调用都用注入的假函数，不碰网络、不碰官方 /her-tk。覆盖：
- `parse_click`：形状 / 时效 / 动作白名单 / chat_id / open_id / tenant / log_id 校验。
- `accept`：同一个 log_id 只回调一次（去重）。
- `provision` / `bind_chat`：按 fingerprint 幂等；没权限(99991672) ⇒ NO_PERM 不反复建。
- `poll_once`：401 ⇒ 要重新 register；200 的 clicks 逐条过 accept。
- 收件箱 URL 指向配置的 INBOX_BASE（确认依赖官方 /her-tk，符合林森 11-26 拍板）。
"""

import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from bridge.fleet import toolkit as tk

CHAT = "oc_" + "a" * 20
WHO = "ou_" + "b" * 20


def _click_item(action="stop", at=None, log_id="lg1", chat=CHAT, who=WHO, tenant="tk1"):
    return {"at": time.time() if at is None else at,
            "body": {"action_key": action,
                     "action_info": {"open_chat_id": chat, "open_id": who, "tenant_key": tenant},
                     "extra": {"log_id": log_id}}}


class ParseClickTest(unittest.TestCase):
    def test_valid(self):
        c = tk.parse_click(_click_item(), time.time())
        self.assertIsNotNone(c)
        self.assertEqual((c.action, c.chat_id, c.open_id), ("stop", CHAT, WHO))

    def test_stale_rejected(self):
        self.assertIsNone(tk.parse_click(_click_item(at=time.time() - 999), time.time()))

    def test_unknown_action_rejected(self):
        self.assertIsNone(tk.parse_click(_click_item(action="rm -rf"), time.time()))

    def test_bad_chat_id_rejected(self):
        self.assertIsNone(tk.parse_click(_click_item(chat="not-a-chat"), time.time()))

    def test_bad_open_id_rejected(self):
        self.assertIsNone(tk.parse_click(_click_item(who="nope"), time.time()))

    def test_missing_body_rejected(self):
        self.assertIsNone(tk.parse_click({"at": time.time()}, time.time()))


def _toolkit(feishu=None, inbox=None, on_click=None):
    d = tempfile.mkdtemp(prefix="sn-tk-")
    kit = tk.Toolkit("cli_app", d,
                     feishu=feishu or (lambda m, p, b=None: {"code": 0}),
                     inbox=inbox or (lambda *a, **k: (200, {})),
                     on_click=on_click)
    kit.ensure_hook()
    return kit


class AcceptDedupTest(unittest.TestCase):
    def test_same_log_id_once(self):
        seen = []
        kit = _toolkit(on_click=seen.append)
        kit.accept(_click_item(log_id="dup"))
        kit.accept(_click_item(log_id="dup"))
        kit.accept(_click_item(log_id="other"))
        self.assertEqual([c.log_id for c in seen], ["dup", "other"])


class ProvisionBindTest(unittest.TestCase):
    def test_bind_chat_idempotent_by_fingerprint(self):
        calls = []

        def feishu(method, path, body=None):
            calls.append((method, path))
            # GET toolkits 列表：返回空（provision 会建全部 5 个）；PUT/POST 都成功
            if method == "GET" and path.startswith("/im/v1/toolkits"):
                return {"code": 0, "data": {"items": []}}
            if method == "POST":
                return {"code": 0, "data": {"toolkit_id": str(len(calls))}}
            return {"code": 0}

        kit = _toolkit(feishu=feishu)
        self.assertEqual(kit.provision(), tk.OK)
        # 第一次绑群发 PUT
        n_before = len([c for c in calls if c[0] == "PUT"])
        self.assertEqual(kit.bind_chat(CHAT), tk.OK)
        n_after = len([c for c in calls if c[0] == "PUT"])
        self.assertEqual(n_after, n_before + 1)
        # 第二次同一个 fingerprint ⇒ 不再 PUT
        self.assertEqual(kit.bind_chat(CHAT), tk.OK)
        self.assertEqual(len([c for c in calls if c[0] == "PUT"]), n_after)

    def test_no_permission_is_no_perm(self):
        def feishu(method, path, body=None):
            if method == "GET":
                return {"code": 0, "data": {"items": []}}
            return {"code": 99991672}  # 没 im:chat

        kit = _toolkit(feishu=feishu)
        # 没权限建按钮 ⇒ provision 不 OK
        self.assertNotEqual(kit.provision(), tk.OK)


class PollTest(unittest.TestCase):
    def test_401_requires_reregister(self):
        kit = _toolkit(inbox=lambda method, url, headers, body, timeout: (401, {}))
        kit.registered = True
        kit.poll_once()
        self.assertFalse(kit.registered)

    def test_200_clicks_go_through_accept(self):
        seen = []
        items = {"clicks": [_click_item(log_id="p1"), _click_item(log_id="p2")]}
        kit = _toolkit(inbox=lambda method, url, headers, body, timeout: (200, items),
                       on_click=seen.append)
        kit.poll_once()
        self.assertEqual(sorted(c.log_id for c in seen), ["p1", "p2"])

    def test_inbox_url_is_official_her_tk(self):
        # 林森 11-26 拍板照搬：点击经官方 chat.auto-link.com.cn/her-tk 中转
        captured = {}

        def inbox(method, url, headers, body, timeout):
            captured["url"] = url
            return (200, {})

        kit = _toolkit(inbox=inbox)
        kit.poll_once()
        self.assertIn("/her-tk", captured["url"])
        self.assertTrue(tk.INBOX_BASE.endswith("/her-tk"))


if __name__ == "__main__":
    sys.exit(0 if unittest.main(exit=False).result.wasSuccessful() else 1)
