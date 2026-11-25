"""第 37 轮：快捷工具栏供给 + 补绑 + 轮询（bridge/fleet/toolkit.py）。

HTTP 全部注入假的：一个按钮都不真建、一个群都不真绑。回包形状照 11-16 实测（contract-toolkit.md）。
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import toolkit as tk  # noqa: E402
from bridge.fleet import groupsetup as gs  # noqa: E402

APP = "cli_test0000000000"
OWNER = "ou_" + "a" * 32
CLAUDE_ON = "oc_" + "1" * 32
CLAUDE_OFF = "oc_" + "2" * 32
CODEX_ON = "oc_" + "3" * 32
NOW = 1_800_000_000.0


def _denied():
    return {"code": 99991672, "msg": "Access denied. [im:chat]",
            "error": {"permission_violations": [{"subject": "im:chat"}]}}


class FakeFeishu:
    """GET /im/v1/toolkits 列出 self.items；POST 建一个；PUT 群记下来。"""

    def __init__(self):
        self.calls = []
        self.items = []
        self.next_id = 7000000000000000001
        self.deny = set()  # {"GET", "POST", "PUT"}
        self.put_fail = set()

    def __call__(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method in self.deny:
            return _denied()
        bare = path.split("?", 1)[0]
        if method == "GET" and bare == "/im/v1/toolkits":
            return {"code": 0, "data": {"items": list(self.items), "has_more": False}}
        if method == "POST" and bare == "/im/v1/toolkits":
            tid = str(self.next_id)
            self.next_id += 1
            self.items.append({"toolkit_id": tid, "toolkit_name": body["toolkit_name"],
                               "toolkit_type": body["toolkit_type"], "callback": dict(body["callback"])})
            return {"code": 0, "data": {"toolkit_id": tid}}
        if method == "PUT" and bare.startswith("/im/v1/chats/"):
            if bare.rsplit("/", 1)[1] in self.put_fail:
                return {"code": 232011, "msg": "bot not in chat"}
            return {"code": 0, "data": {}}
        return {"code": 0, "data": {}}

    def hits(self, method, prefix):
        return [c for c in self.calls if c[0] == method and c[1].split("?", 1)[0].startswith(prefix)]


class FakeInbox:
    def __init__(self, polls=None):
        self.calls = []
        self.polls = list(polls or [])
        self.register_status = 200

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body))
        if url.endswith("/register"):
            return self.register_status, ({"ok": True} if self.register_status == 200 else {})
        if self.polls:
            return self.polls.pop(0)
        return 0, {}

    def n(self, suffix):
        return len([c for c in self.calls if suffix in c[1]])


def _write_binds(home):
    rows = {
        CLAUDE_ON: {"tool": "claude", "id": "s1", "name": "Claude · a", "created_at": 1, "on": True},
        CLAUDE_OFF: {"tool": "claude", "id": "s2", "name": "Claude · b", "created_at": 2, "on": False},
        CODEX_ON: {"tool": "codex", "id": "s3", "name": "Codex · c", "created_at": 3, "on": True},
    }
    with open(os.path.join(home, "chat_binds.json"), "w", encoding="utf-8") as fh:
        json.dump({"binds": rows}, fh)


def _kit(home, feishu=None, inbox=None, clicks=None):
    return tk.Toolkit(APP, str(home), feishu=feishu or FakeFeishu(), inbox=inbox or FakeInbox(),
                      on_click=(clicks.append if clicks is not None else None),
                      now=lambda: NOW, sleep=lambda s: None)


# ---------- 供给 ----------

def test_first_provision_posts_five_call_back_buttons_and_persists_0600(tmp_path):
    fs = FakeFeishu()
    kit = _kit(tmp_path, fs)
    assert kit.provision() == tk.OK
    posts = fs.hits("POST", "/im/v1/toolkits")
    assert [p[2]["callback"]["action_key"] for p in posts] == [a for a, _ in tk.TOOLKIT_BUTTONS]
    hook = kit.webhook()
    assert hook.startswith("https://chat.auto-link.com.cn/her-tk/hook/") and len(hook.rsplit("/", 1)[1]) == 32
    for p in posts:
        assert p[2]["toolkit_type"] == "call_back" and p[2]["callback"]["webhook"] == hook
    path = tmp_path / tk.STATE_NAME
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    saved = json.loads(path.read_text())[APP]
    assert saved["fingerprint"] == tk.fingerprint() and len(saved["ids"]) == 5
    assert len(kit.ids()) == 5


def test_existing_buttons_are_not_posted_again(tmp_path):
    fs = FakeFeishu()
    assert _kit(tmp_path, fs).provision() == tk.OK
    # 新进程：状态在盘上 ⇒ 一个请求都不发
    fs.calls.clear()
    kit2 = _kit(tmp_path, fs)
    assert kit2.provision() == tk.OK and fs.calls == []
    # 盘上 ids 丢了但 hook 还在 ⇒ GET 认回来，不 POST
    state = json.loads((tmp_path / tk.STATE_NAME).read_text())
    state[APP]["ids"] = {}
    (tmp_path / tk.STATE_NAME).write_text(json.dumps(state))
    kit3 = _kit(tmp_path, fs)
    assert kit3.provision() == tk.OK
    assert fs.hits("POST", "/im/v1/toolkits") == [] and len(kit3.ids()) == 5


def test_other_webhooks_are_not_adopted(tmp_path):
    fs = FakeFeishu()
    fs.items.append({"toolkit_id": "1", "toolkit_name": "⏹ 停下", "toolkit_type": "call_back",
                     "callback": {"webhook": tk.WEBHOOK_BASE + "f" * 32, "action_key": "stop"}})
    kit = _kit(tmp_path, fs)
    assert kit.provision() == tk.OK
    assert "1" not in kit.ids() and len(fs.hits("POST", "/im/v1/toolkits")) == 5


def test_fingerprint_change_rebuilds_and_rebinds(tmp_path, monkeypatch):
    _write_binds(tmp_path)
    fs = FakeFeishu()
    kit = _kit(tmp_path, fs)
    assert kit.provision() == tk.OK
    kit.bind_all()
    old = kit.ids()
    monkeypatch.setattr(tk, "TOOLKIT_BUTTONS", (("stop", "⏹ 停"),) + tk.TOOLKIT_BUTTONS[1:])
    fs.calls.clear()
    kit2 = _kit(tmp_path, fs)
    assert kit2.ids() == []           # 指纹变了 ⇒ 不给旧的
    assert kit2.provision() == tk.OK
    posts = fs.hits("POST", "/im/v1/toolkits")
    assert [p[2]["toolkit_name"] for p in posts] == ["⏹ 停"]   # 只重建名字变了的那个
    new = kit2.ids()
    assert new[0] != old[0] and new[1:] == old[1:]
    kit2.bind_all()
    assert len(fs.hits("PUT", "/im/v1/chats/")) == 1          # 绑过的群按新指纹重绑


def test_no_permission_does_not_raise_and_writes_nothing_to_feishu(tmp_path):
    fs = FakeFeishu()
    fs.deny = {"GET", "POST", "PUT"}
    kit = _kit(tmp_path, fs)
    assert kit.provision() == tk.NO_PERM
    assert fs.hits("POST", "/im/v1/toolkits") == [] and kit.ids() == []
    assert kit.bind_all() == {}


def test_feishu_raising_never_escapes(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("x")
    kit = _kit(tmp_path, boom)
    assert kit.provision() == tk.FAILED
    assert kit.bind_all() == {} and kit.ensure_bound(CLAUDE_ON) == tk.FAILED


def test_provision_loop_retries_after_ten_minutes(tmp_path):
    fs = FakeFeishu()
    fs.deny = {"GET"}
    slept = []

    def sleep(s):
        slept.append(s)
        if len(slept) == 2:
            fs.deny = set()
    kit = tk.Toolkit(APP, str(tmp_path), feishu=fs, inbox=FakeInbox(), now=lambda: NOW, sleep=sleep)
    kit.provision_loop(first_delay=0)
    assert slept == [0, tk.RETRY_EVERY_SEC] and len(kit.ids()) == 5


# ---------- 补绑 ----------

def test_bind_all_only_claude_on_with_full_list_and_skips_bound(tmp_path):
    _write_binds(tmp_path)
    fs = FakeFeishu()
    kit = _kit(tmp_path, fs)
    kit.provision()
    got = kit.bind_all()
    puts = fs.hits("PUT", "/im/v1/chats/")
    assert [p[1] for p in puts] == [f"/im/v1/chats/{CLAUDE_ON}"]
    assert puts[0][2] == {"toolkit_ids": [int(x) for x in kit.ids()]}
    assert got == {CLAUDE_ON: tk.OK}
    fs.calls.clear()
    _kit(tmp_path, fs).bind_all()
    assert fs.hits("PUT", "/im/v1/chats/") == []   # 同指纹已绑 ⇒ 跳过


def test_bind_failure_of_one_chat_is_skipped(tmp_path):
    _write_binds(tmp_path)
    rows = json.loads((tmp_path / "chat_binds.json").read_text())
    other = "oc_" + "4" * 32
    rows["binds"][other] = {"tool": "claude", "id": "s4", "name": "Claude · d", "created_at": 4, "on": True}
    (tmp_path / "chat_binds.json").write_text(json.dumps(rows))
    fs = FakeFeishu()
    fs.put_fail = {CLAUDE_ON}
    kit = _kit(tmp_path, fs)
    kit.provision()
    assert kit.bind_all() == {CLAUDE_ON: tk.FAILED, other: tk.OK}


def test_ensure_bound_ignores_non_claude(tmp_path):
    _write_binds(tmp_path)
    fs = FakeFeishu()
    kit = _kit(tmp_path, fs)
    kit.provision()
    assert kit.ensure_bound(CODEX_ON) == tk.FAILED
    assert kit.ensure_bound(CLAUDE_OFF) == tk.FAILED
    assert fs.hits("PUT", "/im/v1/chats/") == []
    assert kit.ensure_bound(CLAUDE_ON) == tk.OK


def test_create_group_carries_toolkit_ids_only_for_claude(monkeypatch):
    monkeypatch.setattr(tk, "toolkit_ids", lambda: ["7000000000000000001", "7000000000000000002"])
    seen = []

    def http(method, path, token, body=None):
        if path.startswith("/auth/"):
            return {"code": 0, "tenant_access_token": "t", "expire": 7200}
        seen.append(body)
        return {"code": 0, "data": {"chat_id": "oc_x"}}
    g = gs.GroupSetup(APP, "s", http=http)
    g.create("Claude · 修登录", OWNER)
    g.create("Codex · 修登录", OWNER)
    assert seen[0]["toolkit_ids"] == [7000000000000000001, 7000000000000000002]
    assert "toolkit_ids" not in seen[1]
    monkeypatch.setattr(tk, "toolkit_ids", lambda: [])
    g.create("Claude · 别的", OWNER)
    assert "toolkit_ids" not in seen[2]


# ---------- 轮询 + payload 校验 ----------

def _item(action="stop", log_id="L1", at=NOW, **drop):
    body = {"action_key": action,
            "action_info": {"open_chat_id": CLAUDE_ON, "open_id": OWNER, "union_id": "on_x",
                            "tenant_key": "1088406f1c155740", "locale": "zh_cn", "plat_form": "mac"},
            "extra": {"log_id": log_id}}
    for key in drop:
        body["action_info"].pop(key, None)
    return {"at": at, "body": body}


def test_click_validation(tmp_path):
    clicks = []
    kit = _kit(tmp_path, clicks=clicks)
    c = kit.accept(_item())
    assert c == tk.ToolbarClick("stop", CLAUDE_ON, OWNER, "1088406f1c155740", "L1", NOW)
    assert kit.accept(_item()) is None                         # 重复 log_id
    assert kit.accept(_item(action="rm_rf", log_id="L2")) is None   # action 不在表
    assert kit.accept(_item(log_id="L3", open_id=1)) is None   # 缺字段
    assert kit.accept(_item(log_id="L4", at=NOW - 121)) is None     # 过期
    assert kit.accept({"at": NOW, "body": "x"}) is None
    assert kit.accept(_item(action="go_on", log_id="L5")) is not None
    assert [x.log_id for x in clicks] == ["L1", "L5"]


def test_on_click_raising_does_not_escape(tmp_path):
    def bad(_):
        raise RuntimeError("x")
    kit = tk.Toolkit(APP, str(tmp_path), feishu=FakeFeishu(), inbox=FakeInbox(), on_click=bad,
                     now=lambda: NOW, sleep=lambda s: None)
    assert kit.accept(_item()) is not None


def test_poll_401_stops_and_registers_again(tmp_path):
    clicks = []
    inbox = FakeInbox(polls=[(401, {}), (200, {"clicks": [_item()]})])
    kit = _kit(tmp_path, inbox=inbox, clicks=clicks)
    kit.poll_loop(rounds=2)   # 一轮 = (必要时 register) + 一次 poll
    assert inbox.n("/register") == 2 and inbox.n("/poll") == 2
    assert [c.action for c in clicks] == ["stop"]
    reg = [c for c in inbox.calls if c[1].endswith("/register")][0]
    import hashlib
    assert reg[3]["hook_id"] == kit._mine["hook_id"]
    assert reg[3]["key_hash"] == hashlib.sha256(kit._mine["poll_key"].encode()).hexdigest()
    poll = [c for c in inbox.calls if "/poll" in c[1]][0]
    assert poll[2]["Authorization"] == "Bearer " + kit._mine["poll_key"]


def test_poll_backoff_is_exponential_and_capped(tmp_path):
    slept = []
    inbox = FakeInbox(polls=[(0, {})] * 12)
    kit = tk.Toolkit(APP, str(tmp_path), feishu=FakeFeishu(), inbox=inbox, now=lambda: NOW,
                     sleep=slept.append)
    kit.poll_loop(rounds=13)
    assert slept[:4] == [2, 4, 8, 16] and max(slept) == tk.BACKOFF_MAX_SEC


def test_start_only_once_and_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(tk, "_started", False)
    monkeypatch.setattr(tk, "_active", None)
    started = []
    monkeypatch.setattr(tk.Toolkit, "provision_loop", lambda self, first_delay=0: started.append("p"))
    monkeypatch.setattr(tk.Toolkit, "poll_loop", lambda self, rounds=None: started.append("q"))
    tk.start(object(), APP, str(tmp_path), lambda c: None)
    tk.start(object(), APP, str(tmp_path), lambda c: None)
    import time as _t
    _t.sleep(0.2)
    assert sorted(started) == ["p", "q"]
    assert tk.toolkit_ids() == []
    tk.ensure_bound(CLAUDE_ON)   # 按钮没建好 ⇒ 什么都不做，不抛
