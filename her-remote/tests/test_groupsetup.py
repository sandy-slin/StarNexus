"""「每个会话一个群」调飞书的那一层 + 开权限体检。

这组测试**一个群都不真建**：HTTP 全部是注入的假的（spec 安全红线：建群是外部可见的不可逆动作）。
飞书的回包形状照 2026-11-05 对本机应用实测的原样抄（99991672 + permission_violations）。

重点不是「能不能调通」，而是这几条边：
- 体检只读：只打不存在的群，**绝不打建群接口**
- 查群在不在：只有飞书明确说没了才算没了（不然每次读失败多建一个群）
- 没权限要和别的失败分开（不然用户会去改一个没问题的地方）
- 群里那张确认卡：按钮走现成的白名单闸，文案说清「每一句都进去」「别拉人」
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import groupsetup as gs  # noqa: E402
from bridge.fleet.cards import resolve_relay_click  # noqa: E402

APP = "cli_test0000000000"
OWNER = "ou_" + "a" * 32
CHAT = "oc_" + "b" * 32


def _denied(*scopes):
    """飞书没权限时的原样回包（字段照实测）。"""
    return {
        "code": 99991672,
        "msg": "Access denied. One of the following scopes is required: [...]",
        "error": {"permission_violations": [{"type": "action_scope_required", "subject": s} for s in scopes]},
    }


class FakeHttp:
    """录下每一次调用；按 (method, 路径前缀) 回预设的包。"""

    def __init__(self, routes=None, token_out=None):
        self.calls = []
        self.routes = routes or {}
        self.token_out = token_out or {"code": 0, "tenant_access_token": "t-fake", "expire": 7200}

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, token, body))
        if path.startswith("/auth/v3/tenant_access_token"):
            return self.token_out
        bare = path.split("?", 1)[0]
        for (m, prefix), out in self.routes.items():
            if m == method and bare.startswith(prefix):
                return out(path, body) if callable(out) else out
        return {"code": 0, "data": {}}

    def hits(self, method, prefix):
        return [c for c in self.calls if c[0] == method and c[1].split("?", 1)[0].startswith(prefix)]


def _setup(http, now=lambda: 1_000_000.0):
    return gs.GroupSetup(APP, "secret", http=http, now=now)


@pytest.fixture
def here_dir():
    """落盘夹具放在测试目录旁边，**不放 tmp_path**。

    spec 安全红线：macOS 的 tmp_path 在 /private/var/folders/ 下，是 pathguard 的系统豁免前缀；
    这组测试本身不判路径，但统一不用它，免得以后有人往这里加路径相关的用例时假绿。
    """
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".groupsetup-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------- 建群 ----------


def test_create_puts_owner_in_as_group_owner_and_private():
    http = FakeHttp({("POST", "/im/v1/chats"): {"code": 0, "data": {"chat_id": CHAT}}})
    chat_id, code, _ = _setup(http).create("Claude · 修登录", OWNER)
    assert (chat_id, code) == (CHAT, 0)
    (method, path, token, body), = http.hits("POST", "/im/v1/chats")
    assert token == "t-fake"
    assert body["owner_id"] == OWNER and body["user_id_list"] == [OWNER]
    assert body["chat_type"] == "private", "公开群会被同事搜到"
    assert body["name"] == "Claude · 修登录"
    assert "user_id_type=open_id" in path and "set_bot_manager=true" in path
    assert "uuid=" in path


def test_create_without_owner_does_not_call_feishu():
    """不知道主人是谁就别建 —— 建出来是一个只有机器人的群，主人根本看不到。"""
    http = FakeHttp()
    chat_id, code, msg = _setup(http).create("x", "")
    assert chat_id == "" and code == 0 and msg
    assert not http.hits("POST", "/im/v1/chats")


def test_create_rejects_non_open_id_owner():
    http = FakeHttp()
    assert _setup(http).create("x", "oc_notaperson")[0] == ""
    assert not http.hits("POST", "/im/v1/chats")


def test_create_reports_permission_code_verbatim():
    """没权限要原样把码交给 chatbind（它靠码分「去开权限」和「等会儿再试」）。"""
    http = FakeHttp({("POST", "/im/v1/chats"): _denied("im:chat", "im:chat:create")})
    setup = _setup(http)
    chat_id, code, _ = setup.create("x", OWNER)
    assert chat_id == "" and code == 99991672
    assert setup.last_error["missing"] == ["im:chat", "im:chat:create"]


def test_create_success_without_chat_id_is_a_failure():
    http = FakeHttp({("POST", "/im/v1/chats"): {"code": 0, "data": {}}})
    chat_id, code, _ = _setup(http).create("x", OWNER)
    assert chat_id == "" and code != 0


def test_create_never_raises_when_http_explodes():
    def boom(*_a, **_k):
        raise RuntimeError("断网")

    chat_id, code, msg = gs.GroupSetup(APP, "s", http=boom).create("x", OWNER)
    assert chat_id == "" and code != 0


def test_group_name_is_scrubbed_of_zero_width_and_newlines():
    http = FakeHttp({("POST", "/im/v1/chats"): {"code": 0, "data": {"chat_id": CHAT}}})
    _setup(http).create("修​登录\n第二行‮", OWNER)
    body = http.hits("POST", "/im/v1/chats")[0][3]
    assert body["name"] == "修登录 第二行"


def test_empty_name_gets_a_fallback_not_untitled():
    assert gs.safe_group_name("​​") == "电脑上的会话"
    assert len(gs.safe_group_name("长" * 200)) == gs.MAX_GROUP_NAME


# ---------- 去重键 ----------


def test_dedupe_uuid_same_bucket_same_key():
    start = gs.DEDUPE_BUCKET_SEC * 16_000  # 桶的起点（固定分桶：跨桶边界的两下会拿到不同的键）
    a = gs.dedupe_uuid("n", OWNER, start)
    b = gs.dedupe_uuid("n", OWNER, start + gs.DEDUPE_BUCKET_SEC - 1)
    assert a == b and len(a) <= gs.UUID_MAX


def test_dedupe_uuid_changes_later_so_a_dissolved_group_can_be_rebuilt():
    """🔴 飞书同一个 uuid 10 小时只建一次 ⇒ 键要是不变，群被解散后就再也建不出来。"""
    a = gs.dedupe_uuid("n", OWNER, 1_000_000.0)
    later = gs.dedupe_uuid("n", OWNER, 1_000_000.0 + gs.DEDUPE_BUCKET_SEC * 3)
    assert a != later


def test_dedupe_uuid_differs_per_name_and_owner():
    base = gs.dedupe_uuid("n", OWNER, 0)
    assert base != gs.dedupe_uuid("m", OWNER, 0)
    assert base != gs.dedupe_uuid("n", "ou_" + "c" * 32, 0)


# ---------- 群还在不在 ----------


@pytest.mark.parametrize("code", sorted(gs.GONE_CODES))
def test_exists_false_only_when_feishu_says_gone(code):
    http = FakeHttp({("GET", "/im/v1/chats/"): {"code": code, "msg": "gone"}})
    assert _setup(http).exists(CHAT) is False


@pytest.mark.parametrize("status", ["dissolved", "dissolved_save"])
def test_exists_false_when_status_dissolved(status):
    http = FakeHttp({("GET", "/im/v1/chats/"): {"code": 0, "data": {"chat_status": status}}})
    assert _setup(http).exists(CHAT) is False


@pytest.mark.parametrize("out", [
    _denied("im:chat:read"),                 # 没开权限（现在就是这样）
    {"code": -1, "msg": "网络不通"},          # 断网
    {"code": 232019, "msg": "rate limited"},  # 限流
    {"code": 0, "data": {"chat_status": "normal"}},
])
def test_exists_true_when_unsure_so_we_never_rebuild_on_a_read_failure(out):
    """🔴 查不出来就当还在 —— 不然每次读失败都多建一个群。"""
    http = FakeHttp({("GET", "/im/v1/chats/"): out})
    assert _setup(http).exists(CHAT) is True


def test_exists_does_not_query_for_non_group_ids():
    http = FakeHttp()
    assert _setup(http).exists("ou_person") is True
    assert not http.hits("GET", "/im/v1/chats/")


def test_token_failure_reads_as_unsure_not_gone():
    http = FakeHttp(token_out={"code": 10014, "msg": "app secret invalid"})
    assert _setup(http).exists(CHAT) is True


def test_token_is_cached():
    http = FakeHttp({("GET", "/im/v1/chats/"): {"code": 0, "data": {}}})
    setup = _setup(http)
    setup.exists(CHAT)
    setup.exists(CHAT)
    assert len([c for c in http.calls if c[1].startswith("/auth/")]) == 1


# ---------- 改名 / 拉主人 ----------


def test_rename_only_sends_name():
    http = FakeHttp()
    ok, code, _ = _setup(http).rename(CHAT, "Claude · 新名字")
    assert ok and code == 0
    (_m, _p, _t, body), = http.hits("PUT", "/im/v1/chats/")
    assert body == {"name": "Claude · 新名字"}, "别的群设置一概不碰"


def test_add_owner_only_the_owner():
    http = FakeHttp()
    ok, _, _ = _setup(http).add_owner(CHAT, OWNER)
    assert ok
    (_m, path, _t, body), = http.hits("POST", "/im/v1/chats/")
    assert body == {"id_list": [OWNER]} and "member_id_type=open_id" in path


@pytest.mark.parametrize("chat,owner", [("ou_x", OWNER), (CHAT, "oc_x"), ("", OWNER), (CHAT, "")])
def test_add_owner_rejects_bad_ids_without_calling(chat, owner):
    http = FakeHttp()
    assert _setup(http).add_owner(chat, owner)[0] is False
    assert not http.hits("POST", "/im/v1/chats/")


def test_rename_reports_permission():
    http = FakeHttp({("PUT", "/im/v1/chats/"): _denied("im:chat", "im:chat:update")})
    ok, code, _ = _setup(http).rename(CHAT, "x")
    assert not ok and code == 99991672


# ---------- 群现在叫什么（「他自己改过没有」的唯一依据）----------


def test_name_of_reads_the_current_name():
    http = FakeHttp({("GET", "/im/v1/chats/"): {"code": 0, "data": {"name": "我自己改的名"}}})
    name, code, _ = _setup(http).name_of(CHAT)
    assert (name, code) == ("我自己改的名", 0)


@pytest.mark.parametrize("out", [
    _denied("im:chat:read"),                   # 没开「查看群信息」
    {"code": -1, "msg": "网络不通"},
    {"code": 0, "data": {}},                   # 飞书没给 name
    {"code": 0, "data": {"name": 12345}},      # 给了个不是字符串的
    # 🔴 **失败的信封里带的 name 不许当数据读**。限流 / 部分失败时回包里可能还挂着
    # 一截旧数据；采信它 = 拿一个不知道新旧的名字去和 `Bound.name` 比，
    # 比错一次就把主人起的群名冲掉。有 code 就只看 code。
    {"code": 232019, "msg": "rate limited", "data": {"name": "旧名字"}},
])
def test_name_of_unreadable_is_an_empty_string_not_a_guess(out):
    """🔴 读不出来要给空串：调用方据此**什么都不改**（不知道他动过没动过时别赌）。"""
    http = FakeHttp({("GET", "/im/v1/chats/"): out})
    assert _setup(http).name_of(CHAT)[0] == ""


@pytest.mark.parametrize("chat", ["ou_" + "a" * 32, "", "x", None])
def test_name_of_does_not_call_feishu_for_non_groups(chat):
    http = FakeHttp()
    assert _setup(http).name_of(chat)[0] == ""
    assert not http.hits("GET", "/im/v1/chats/")


def test_name_of_never_raises_when_http_explodes():
    def boom(*_a, **_k):
        raise RuntimeError("断网")

    assert gs.GroupSetup(APP, "s", http=boom).name_of(CHAT)[0] == ""


# ---------- 「点一下进群」的链接 ----------
#
# 回包形状照 2026-11-05 对真群实测的原样：{"is_permanent":..., "share_link": "https://applink.feishu.cn/..."}

LINK = "https://applink.feishu.cn/client/chat/chatter/add_by_link?link_token=286q2317-0000"


def _link_ok(path, body):
    return {"code": 0, "data": {"is_permanent": body.get("validity_period") == "permanently",
                                "share_link": LINK}}


def test_link_returns_the_share_link():
    http = FakeHttp({("POST", "/im/v1/chats/"): _link_ok})
    url, code, _ = _setup(http).link(CHAT)
    assert url == LINK and code == 0
    (_m, path, _t, body), = http.hits("POST", "/im/v1/chats/")
    assert path.endswith(f"/im/v1/chats/{CHAT}/link")
    assert body == {"validity_period": "week"}, "默认一周，不是永久（链接=进这个私有群的凭据）"


@pytest.mark.parametrize("asked,sent", [
    ("permanently", "permanently"), ("year", "year"), ("week", "week"),
    ("day", "week"), ("", "week"), ("'; drop", "week"),
])
def test_link_validity_is_whitelisted(asked, sent):
    """有效期只认文档里那三档；别的一律落回默认（不往飞书传没校验过的字符串）。"""
    http = FakeHttp({("POST", "/im/v1/chats/"): _link_ok})
    _setup(http).link(CHAT, validity=asked)
    (_m, _p, _t, body), = http.hits("POST", "/im/v1/chats/")
    assert body == {"validity_period": sent}


@pytest.mark.parametrize("chat", ["ou_" + "a" * 32, "", "x", None])
def test_link_does_not_call_feishu_for_non_groups(chat):
    http = FakeHttp()
    assert _setup(http).link(chat)[0] == ""
    assert not http.hits("POST", "/im/v1/chats/")


def test_link_permission_error_is_reported_not_raised():
    http = FakeHttp({("POST", "/im/v1/chats/"): _denied("im:chat")})
    url, code, _ = _setup(http).link(CHAT)
    assert url == "" and code == 99991672


@pytest.mark.parametrize("out", [
    {"code": 0, "data": {}},                                    # 说成了但没给链接
    {"code": 0, "data": {"share_link": ""}},
    {"code": 0, "data": {"share_link": "javascript:alert(1)"}},  # 不是 https ⇒ 当没拿到
    {"code": 0, "data": {"share_link": 12345}},
])
def test_link_rejects_unusable_payloads(out):
    """拿不到可用链接要**明确说没拿到**，不能把空串 / 怪东西往卡片按钮里放。"""
    http = FakeHttp({("POST", "/im/v1/chats/"): out})
    url, _code, msg = _setup(http).link(CHAT)
    assert url == "" and msg


def test_link_never_raises_when_http_explodes():
    def boom(*_a, **_k):
        raise RuntimeError("网络炸了")

    url, _c, _m = gs.GroupSetup(APP, "secret", http=boom).link(CHAT)
    assert url == ""


# ---------- 体检 ----------


def _today_http(app_info=None):
    """2026-11-05 本机应用实测的样子：只有发消息有权限。"""
    routes = {
        ("GET", "/im/v1/chats/"): _denied("im:chat:readonly", "im:chat", "im:chat:read"),
        ("PUT", "/im/v1/chats/"): _denied("im:chat", "im:chat:update"),
        ("POST", "/im/v1/chats/"): _denied("im:chat", "im:chat.members:write_only"),
        ("POST", "/im/v1/messages"): {"code": 230001, "msg": "invalid receive_id"},
        ("GET", "/application/v6/applications/"): app_info or _denied(
            "admin:app.info:readonly", "application:application:self_manage"),
    }
    return FakeHttp(routes)


def test_probe_matches_todays_measurement():
    report = gs.probe(_setup(_today_http()))
    states = {n.key: n.state for n in report.needs}
    assert states == {
        "create": gs.UNKNOWN, "rename": gs.MISSING, "add_owner": gs.MISSING,
        "read": gs.MISSING, "receive": gs.UNKNOWN, "send": gs.OK,
    }
    assert not report.ready
    assert report.get("read").feishu_says == ("im:chat:readonly", "im:chat", "im:chat:read")


def test_probe_never_calls_create_group():
    """🔴 体检绝不打建群接口 —— 有权限时一个合法请求就真建出一个群。"""
    http = _today_http()
    gs.probe(_setup(http))
    assert not [c for c in http.calls if c[0] == "POST" and c[1].split("?", 1)[0] == "/im/v1/chats"]


def test_probe_only_touches_the_nonexistent_chat():
    http = _today_http()
    gs.probe(_setup(http))
    for method, path, _t, body in http.calls:
        if "/im/v1/chats/" in path:
            assert gs.PROBE_CHAT_ID in path
        if path.startswith("/im/v1/messages"):
            assert body["receive_id"] == gs.PROBE_CHAT_ID
        if body and "id_list" in body:
            assert body["id_list"] == [gs.PROBE_OPEN_ID], "体检不许拉真人"
        assert method != "PATCH", "体检不改应用配置"
        assert "/publish" not in path, "体检不发版本"


def test_probe_uses_app_scope_list_when_readable():
    info = {"code": 0, "data": {"app": {"scopes": [
        {"scope": "im:chat:create", "token_types": ["tenant"]},
        {"scope": "im:message.group_msg", "token_types": ["user"]},  # 只有用户身份的那份 ⇒ 不算
    ]}}}
    report = gs.probe(_setup(_today_http(app_info=info)))
    assert report.get("create").state == gs.OK
    assert report.get("receive").state == gs.MISSING


def test_probe_all_green():
    ok_all = {"code": 232006, "msg": "invalid chat_id"}  # 有权限 ⇒ 轮到「群不存在」
    info = {"code": 0, "data": {"app": {"scopes": [
        {"scope": "im:chat", "token_types": ["tenant"]},
        {"scope": "im:message.group_msg", "token_types": ["tenant"]},
    ]}}}
    http = FakeHttp({
        ("GET", "/im/v1/chats/"): ok_all, ("PUT", "/im/v1/chats/"): ok_all,
        ("POST", "/im/v1/chats/"): ok_all,
        ("POST", "/im/v1/messages"): {"code": 230001, "msg": "invalid receive_id"},
        ("GET", "/application/v6/applications/"): info,
    })
    report = gs.probe(_setup(http))
    assert report.ready, report.render()
    assert report.to_open() == [] and report.grant_link() == ""
    assert "全齐了" in report.render()


def test_probe_bad_credentials_says_so():
    http = FakeHttp(token_out={"code": 10014, "msg": "invalid"})
    report = gs.probe(_setup(http))
    assert not report.ready and "app_id" in report.note
    assert all(n.state == gs.UNKNOWN for n in report.needs)


def test_probe_network_down_is_unknown_not_missing():
    """断网不能说成「没开权限」—— 用户会去后台找一个其实已经开了的东西。"""
    http = FakeHttp({("GET", "/im/v1/chats/"): {"code": -1, "msg": "网络不通"}})
    assert gs.probe(_setup(http)).get("read").state == gs.UNKNOWN


def test_grant_link_lists_every_scope_still_needed():
    report = gs.probe(_setup(_today_http()))
    link = report.grant_link()
    assert link.startswith(f"https://open.feishu.cn/app/{APP}/auth?q=")
    q = link.split("q=", 1)[1].split("&", 1)[0].split(",")
    assert q == ["im:chat:create", "im:chat:update", "im:chat.members:write_only",
                 "im:chat:read", "im:message.group_msg"]
    assert "token_type=tenant" in link


def test_render_is_plain_chinese_and_says_publish():
    text = gs.probe(_setup(_today_http())).render()
    assert "发一个新版本" in text
    assert "创建群" in text and "获取群组中所有消息" in text
    for word in ("agent", "None", "null", "exception", "traceback", "**"):
        assert word not in text


def test_report_is_cached_so_building_a_group_does_not_reprobe():
    """建群那张卡要按权限分叉文案 ⇒ 每次建群都体检一遍太慢（5 次飞书请求）。"""
    clock = [1_000_000.0]
    setup = _setup(_today_http(), now=lambda: clock[0])
    http = setup._http
    first = setup.report()
    calls = len(http.calls)
    assert first is not None and calls > 1
    assert setup.report() is first and len(http.calls) == calls, "缓存没生效"
    clock[0] += gs.REPORT_TTL_SEC + 1
    assert setup.report() is not first, "过期了还在用旧的 ⇒ 开完权限文案永远跟不上"


def test_report_on_a_dead_network_is_unsure_not_green():
    """断网 ⇒ 拿不到确定的答案 ⇒ 文案走保守那一支（说大了群里会直接失灵）。"""
    def boom(*_a, **_k):
        raise RuntimeError("断网")

    assert gs.receive_granted(gs.GroupSetup(APP, "s", http=boom).report()) is False


def test_report_never_raises(monkeypatch):
    """🔴 体检自己炸了也只能让文案保守，不能让建群这一下崩掉。

    （`_call` 已经把 HTTP 的异常包住了，所以这里直接让 `probe` 抛 —— 要测的是
    `report()` 外面那层 try，不是 HTTP 那层。）
    """
    def boom(_setup_obj):
        raise RuntimeError("体检自己炸了")

    monkeypatch.setattr(gs, "probe", boom)
    setup = _setup(_today_http())
    assert setup.report() is None
    assert gs.receive_granted(setup.report()) is False


def test_receive_scope_is_all_group_messages_not_at_only():
    """「群里说的每一句都直达」要的是群里**所有**消息；只开 @ 机器人的话，每句都得先 @ 它。"""
    need = next(n for n in gs.NEEDS if n[0] == "receive")
    assert need[2] == "im:message.group_msg"
    assert "group_at_msg" not in need[2]


# ---------- 群里的确认卡 ----------


class _Sess:
    tool = "claude"
    id = "11111111-2222-3333-4444-555555555555"
    title = "修登录 sk-ant-api03-" + "x" * 40


def _redact(text):
    from bridge.redact import redact

    return redact(text)[0]


def test_group_consent_card_buttons_go_through_relay_whitelist():
    card = json.loads(gs.build_group_consent_card(_Sess(), redact=_redact))
    values = [b["behaviors"][0]["value"] for b in _buttons(card)]
    known = {("claude", _Sess.id)}
    assert [resolve_relay_click(v, known)["op"] for v in values] == ["talk", "no"]
    # 伪造的 id 进不来（同 cards 那道闸）
    assert resolve_relay_click(dict(values[0], id="forged"), known) is None


def _granted_report(receive_ok: bool):
    """只在「收群消息」那一项上有区别的两份体检结果。"""
    needs = tuple(
        gs.Need(k, lbl, gs.OK if (k != "receive" or receive_ok) else gs.UNKNOWN, sc, con)
        for k, lbl, sc, con, _a in gs.NEEDS
    )
    return gs.Report(APP, needs)


def test_group_consent_card_copy():
    blob = gs.build_group_consent_card(_Sess(), redact=_redact, report=_granted_report(True))
    text = _texts(json.loads(blob))
    assert "每一句" in text and "别拉别人进来" in text
    assert "不问你就改" in text and "管不到它" in text
    assert "不确认" in text, "要说清没确认时群里的话不会过去"
    # 「算了」是这张卡上的一个按钮 ⇒ 正文要说清点它之后会怎样，不然他不知道该不该点
    assert "算了" in text and "不会发过去" in text
    assert "/cancel" not in text, "不叫用户打命令"
    assert "sk-ant-api03" not in blob, "会话名进群里之前要脱敏"
    assert len(blob.encode()) < 20 * 1024


@pytest.mark.parametrize("report", [None, _granted_report(False)])
def test_group_consent_card_does_not_promise_every_message_without_the_scope(report):
    """🔴 「每一句都直达」要看**真有没有** `im:message.group_msg`。

    没开的话飞书只推 @ 了机器人的消息 ⇒ 照说「每一句」，主人说一句没人理，
    他以为建群这功能坏了（2026-11-05 体验审查）。看不出来（report=None）也走保守那一支。
    """
    text = _texts(json.loads(gs.build_group_consent_card(_Sess(), redact=_redact, report=report)))
    assert gs.RECEIVE_SURE not in text, "权限没确认就承诺了「每一句」"
    assert "@ 一下机器人" in text, "没说清现在得先 @ 它"
    assert "获取群组中所有消息" in text, "没告诉他开哪一项就能不用 @"


def test_receive_granted_only_trusts_a_confirmed_ok():
    assert gs.receive_granted(_granted_report(True)) is True
    assert gs.receive_granted(_granted_report(False)) is False
    assert gs.receive_granted(None) is False
    # 实测那份（receive 看不出来）⇒ 不算
    assert gs.receive_granted(gs.probe(_setup(_today_http()))) is False


def test_need_permission_card_without_report():
    blob = gs.build_need_permission_card(app_id=APP)
    card = json.loads(blob)
    text = _texts(card)
    assert "权限管理" in text and "版本管理与发布" in text and "创建版本" in text
    assert "创建群" in text and "获取群组中所有消息" in text
    assert "以应用的身份发消息" not in text, "已经有的那项不该让用户再去开"
    urls = [b["behaviors"][0].get("default_url") for b in _buttons(card)]
    assert urls and urls[0].startswith(f"https://open.feishu.cn/app/{APP}/auth?q=")
    assert "secret" not in blob and "t-fake" not in blob


def test_need_permission_card_from_probe():
    report = gs.probe(_setup(_today_http()))
    text = _texts(json.loads(gs.build_need_permission_card(report)))
    assert "更新群信息" in text and "查看群信息" in text
    assert "接着聊" in text, "要告诉他不开群也能用"


def _buttons(card):
    return [e for e in _walk(card) if isinstance(e, dict) and e.get("tag") == "button"]


def _texts(card):
    return "\n".join(e.get("content", "") for e in _walk(card)
                     if isinstance(e, dict) and e.get("tag") in ("markdown", "plain_text"))


def _walk(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# ---------- 和 chatbind 的约定对得上 ----------


def test_satisfies_chatbind_group_client_contract(here_dir):
    """chatbind.ensure_group 拿它当 client：建成落盘、没权限报 NO_PERMISSION。"""
    chatbind = pytest.importorskip("bridge.fleet.chatbind")
    binds = chatbind.ChatBinds(str(here_dir / "binds.json"))

    denied = _setup(FakeHttp({("POST", "/im/v1/chats"): _denied("im:chat:create")}))
    result = chatbind.ensure_group(binds, denied, "claude", _Sess.id, "修登录", owner_open_id=OWNER)
    assert result.how == chatbind.NO_PERMISSION and binds.of_session("claude", _Sess.id) is None

    ok = _setup(FakeHttp({("POST", "/im/v1/chats"): {"code": 0, "data": {"chat_id": CHAT}}}))
    result = chatbind.ensure_group(binds, ok, "claude", _Sess.id, "修登录", owner_open_id=OWNER)
    assert result.how == chatbind.CREATED and result.bound.chat_id == CHAT
    # 再点一次：复用（没开 im:chat:read 时 exists 也是 True ⇒ 不会多建）
    again = _setup(FakeHttp({("GET", "/im/v1/chats/"): _denied("im:chat:read")}))
    assert chatbind.ensure_group(binds, again, "claude", _Sess.id, "修登录", owner_open_id=OWNER).how == chatbind.REUSED


def test_main_exit_codes(here_dir, monkeypatch, capsys):
    cfg = here_dir / "config.toml"
    cfg.write_text('[feishu]\napp_id = ""\napp_secret = ""\n', encoding="utf-8")
    monkeypatch.delenv("FEISHU_BRIDGE_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_BRIDGE_APP_SECRET", raising=False)
    assert gs.main(["x", str(cfg)]) == 2
    assert gs.main(["x", str(here_dir / "nope.toml")]) == 2

    cfg.write_text(f'[feishu]\napp_id = "{APP}"\napp_secret = "s3cr3t-value"\n', encoding="utf-8")
    assert gs.main(["x", str(cfg)], http=_today_http()) == 1
    out = capsys.readouterr().out
    assert "s3cr3t-value" not in out, "体检不打印凭据"
    assert "一键去开" in out
