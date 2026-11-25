"""「一个群 = 一个会话」：落盘绑定、幂等建群、长期直达、权限确认、会话没了、解绑。

🔴 **一个群都不真建**：建群是外部可见的不可逆动作（spec 安全红线）。这里全用假 client，
假 adapter、假 replier；夹具放 tmp_path（pytest 的临时目录），不碰 `~/.carher-feishu-agent`。

盯的是 spec 二 的 2、3、5、6、7、8：
- 2 幂等：同一个会话点两次只有一个群；群被解散了能重建；重启后还认得
- 3 长期：群里的话不走 5 分钟过期，查绑定表
- 4 权限确认：按群单独记，没确认只回说明、不转发
- 5 会话没了：明说 + 给按钮，不悄悄落进桥自己的 claude
- 6 群名：脱敏、截断、重名加短 id、没名字用短 id
- 7 不碰单聊：`dm:` scope 查不到绑定；`ou_` 开头的 id 绑不进来
- 8 解绑：只关转发，不解散群
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.fleet import ActionResult, Message, Session  # noqa: E402
from bridge.fleet.chatbind import (  # noqa: E402
    CREATED,
    FAILED,
    KEPT_OWN,
    MAX_NAME_CHARS,
    NO_PERMISSION,
    NO_RENAME,
    RENAMED,
    REUSED,
    SAME,
    ChatBinds,
    chat_of_scope,
    ensure_group,
    group_name,
    rename_group,
)
from bridge.fleet.consent import RelayConsent  # noqa: E402
from bridge.fleet.control import FleetControl  # noqa: E402
from bridge.fleet.scan import Fleet  # noqa: E402
from bridge.policy import Policy  # noqa: E402

OWNER = "ou_owner"
DM_CHAT = "oc_dm"
DM = f"dm:{OWNER}"

# 这几条原来打着 `xfail`，等的是 `bridge/app.py` 那份群功能收尾补丁
# （改绑改群名 / 群里「算了」解绑 / 作废的确认卡 / 文案按收群消息权限分叉）。
# **补丁 2026-11-05 已经落了，标记随之删掉** —— 留着 xfail 的话，
# 哪天这几条真坏了只会显示成「预期失败」，没人会看见。
CID = "44444444-4444-4444-8444-444444444444"
CID2 = "55555555-5555-4555-8555-555555555555"


# ---------- 假东西 ----------


class FakeGroups:
    """假建群接口。记下每一次 create，**不碰网络**。"""

    def __init__(self, code=0, msg="", raises=False):
        self.created: list = []
        self.alive: set = set()
        self.code = code
        self.msg = msg
        self.raises = raises
        self.exists_raises = False
        # 改群名那一组
        self.names: dict = {}  # chat_id → 飞书上现在的群名
        self.renamed: list = []
        self.name_fails = False
        self.name_raises = False
        self.rename_code = 0
        self.rename_raises = False

    def create(self, name, owner_open_id=""):
        if self.raises:
            raise RuntimeError("网络断了")
        if self.code:
            return "", self.code, self.msg
        chat_id = f"oc_g{len(self.created) + 1}"
        self.created.append((chat_id, name, owner_open_id))
        self.alive.add(chat_id)
        self.names[chat_id] = name
        return chat_id, 0, ""

    def exists(self, chat_id):
        if self.exists_raises:
            raise RuntimeError("查不了")
        return chat_id in self.alive

    # ---- 改群名那一组（真实那一层是 groupsetup.GroupSetup）----

    def name_of(self, chat_id):
        """飞书上现在的群名。`name_fails` = 读不出来（没开「查看群信息」/ 断网）。"""
        if self.name_raises:
            raise RuntimeError("读不了群名")
        if self.name_fails:
            return "", 99991672, "没权限"
        return self.names.get(chat_id, ""), 0, ""

    def rename(self, chat_id, name):
        if self.rename_raises:
            raise RuntimeError("改不了")
        if self.rename_code:
            return False, self.rename_code, "没改成"
        self.renamed.append((chat_id, name))
        self.names[chat_id] = name
        return True, 0, ""


class FakeClaude:
    tool = "claude"

    def __init__(self, sessions=()):
        self.sessions = list(sessions)
        self.relayed: list = []
        self.sent: list = []
        self.last_error = ""
        self.find_raises = False

    def list(self, limit=20):
        return self.sessions[:limit]

    def find(self, sid):
        if self.find_raises:
            raise RuntimeError("读超时")
        return next((s for s in self.sessions if s.id == sid), None)

    def read(self, sid, n=4):
        return [Message("assistant", "在跑")]

    def can_relay(self, sid):
        return True, ""

    def relay(self, sid, text, timeout=120.0, mode=None):
        self.relayed.append((sid, text))
        return ActionResult(True, "好，我看看", sid, delivered=True)

    def send(self, sid, text, timeout=600, **kw):
        self.sent.append((sid, text))
        return ActionResult(True, "resume 回复", sid)

    def stop(self, sid):
        return ActionResult(False, "到电脑上按 Esc", sid)


def _sess(sid=CID, title="修登录", **kw):
    base = dict(tool="claude", surface="cli", id=sid, title=title, cwd="/Users/x/codes/demo",
                status="running", pids=(4242,), owner="terminal", updated_at=time.time() - 60,
                can_send=False, why_not_send="会分叉", can_relay=True)
    base.update(kw)
    return Session(**base)


class FakeReplier:
    def __init__(self):
        self.texts: list = []  # (chat_id, text)
        self.cards: list = []  # (chat_id, card)

    def send(self, chat_id, text):
        from bridge.redact import redact_with_notice

        self.texts.append((chat_id, redact_with_notice(text)))
        return True

    def send_card(self, chat_id, card):
        self.cards.append((chat_id, card))
        return True

    def to(self, chat_id):
        return [t for c, t in self.texts if c == chat_id]

    def cards_to(self, chat_id):
        return [c for cid, c in self.cards if cid == chat_id]


def _bridge(tmp_path, adapter, groups=None, binds_path=None):
    from bridge.app import Bridge
    from bridge.approval import ApprovalGate
    from bridge.review import Reviewer

    b = Bridge.__new__(Bridge)
    pol = Policy(mode="readonly", workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                 model="claude-sonnet-5-5", turn_timeout_sec=60, ceiling_mode="trusted")
    # 🔴 config_path 一定要指到测试目录：为空时有些落盘状态（如群消息权限的证明）会退回真实的
    # ~/.carher-feishu-agent —— 那是正在服务的实例 ⇒ 测试读到线上状态（2026-11-06 撞上过）
    b.settings = types.SimpleNamespace(owner_open_ids=frozenset({OWNER}), policy=pol, custom_rules=(),
                                       config_path=str(tmp_path / "config.toml"))
    b.replier = FakeReplier()
    b._chat_ids = {DM: DM_CHAT}
    b._paused = {}
    b._awaiting = {}
    b._cancels = {}
    b._loop = None
    b._pending_consent = {}
    b.reviewer = Reviewer()
    b.gate = ApprovalGate(str(tmp_path / "approvals"))
    b.fleet = FleetControl(Fleet({adapter.tool: adapter}, timeout=1.0))
    b._fleet_inflight = {}
    b.relay_consent = RelayConsent(str(tmp_path / "relay_consent.json"))
    b.chat_binds = ChatBinds(str(binds_path or tmp_path / "chat_binds.json"))
    b.group_client = groups
    b.handled = []

    async def fake_run_one(session, chat_id, text):  # 桥自己的 claude
        b.handled.append((session.scope, text))

    b._run_one = fake_run_one
    b.sessions = types.SimpleNamespace(
        get=lambda scope: types.SimpleNamespace(scope=scope, workspace=str(tmp_path),
                                                lock=asyncio.Lock(), waiting=0),
        reset=lambda scope: None,
    )
    b.workspaces = types.SimpleNamespace(get=lambda scope: str(tmp_path))
    return b


def _run(coro):
    return asyncio.run(coro)


def _click(b, value, chat_id=DM_CHAT, clicker=OWNER):
    event = types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=chat_id))
    if value.get("act") == "relay":
        return b._on_relay_click(event, clicker, value)
    if value.get("act") == "do":
        return b._on_do_click(event, clicker, value)
    return b._on_fleet_click(event, clicker, value)


def _open_group(b, sid=CID, *, needs_confirm=True):
    """单聊里点 [接着聊] ⇒ 建 / 复用群（后台那一步在测试里同步跑）。

    🔴 2026-11-06 起「建群即进对话」（用户原话：建群后自动进入对话，而不是再让用户点一次确认）：
    点「接着聊」本身就记成了确认，群里不再弹确认卡。
    这一批老测试测的是**确认卡那套机制**（确认前那句记着、序号、过时卡、取消词）——
    那套在「身份变了 / 重启后」照样会弹卡，所以机制本身还要测。
    `needs_confirm=True`（默认，老测试用）：建完群把这次自动确认撤掉、并按老路弹一张确认卡，
    等于把群摆回「还没确认」的状态。新行为的测试传 `needs_confirm=False`（见 test_r11_auto_enter.py）。
    """
    _run(b._open_session_group(DM_CHAT, DM, OWNER, "claude", sid))
    if needs_confirm:
        bound = b.chat_binds.of_session("claude", sid)
        if bound is not None:
            if b.relay_consent is not None:  # 有的测试故意把确认存储拿掉
                b.relay_consent.revoke(bound.scope, "claude", sid)
            getattr(b, "_talk_idents", {}).pop(bound.scope, None)
            b.fleet.clear_pending(bound.scope)
            b._ask_relay_consent(bound.chat_id, bound.scope, "claude", sid)


def _confirm_in_group(b, chat_id, sid=CID):
    _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": sid}, chat_id=chat_id)


def _toast(resp):
    return str(getattr(getattr(resp, "toast", None), "content", ""))


# ---------- 6 群名 ----------


def test_group_name_is_tool_dot_title():
    assert group_name("claude", "修登录", CID) == "Claude · 修登录"


def test_group_name_fits_one_phone_line():
    name = group_name("codex", "把整个后端的鉴权中间件拆成三层并补齐所有的单元测试和文档", CID)
    assert len(name) <= MAX_NAME_CHARS and name.endswith("…")


def test_group_name_dedupes_with_short_id_and_still_fits():
    first = group_name("claude", "修登录", CID)
    second = group_name("claude", "修登录", CID2, taken={first})
    assert second != first and second.endswith("#" + CID2[:6])
    long_taken = group_name("claude", "很" * 40, CID)
    again = group_name("claude", "很" * 40, CID2, taken={long_taken})
    assert again != long_taken and len(again) <= MAX_NAME_CHARS


def test_group_name_without_title_uses_short_id():
    assert group_name("cursor", "", CID) == f"Cursor · 会话 {CID[:6]}"


def test_group_name_keeps_emoji_drops_newlines_and_zero_width():
    name = group_name("claude", "🔥修\n登录​", CID)
    assert "🔥" in name and "\n" not in name and "​" not in name


def test_group_name_is_redacted_before_truncation():
    """🔴 群里能看到的人更多 ⇒ 群名先脱敏整段，再截断（截断在前会让密钥半截漏出去）。"""
    from bridge.app import _redact_text

    secret = "sk-ant-api03-" + "Z" * 40
    name = group_name("claude", f"{secret} 修登录", CID, clean=_redact_text)
    assert "ZZZZ" not in name and "sk-ant" not in name


def test_group_name_survives_a_broken_redactor():
    def boom(_):
        raise ValueError("坏了")

    assert group_name("claude", "sk-secret", CID, clean=boom) == f"Claude · 会话 {CID[:6]}"


# ---------- 7 不碰单聊 ----------


def test_dm_scope_never_resolves_to_a_bind(tmp_path):
    store = ChatBinds(str(tmp_path / "b.json"))
    store.bind("oc_g1", "claude", CID, "x")
    assert store.of_scope(DM) is None and store.of_scope("dm:oc_g1") is None
    assert store.of_scope("group:oc_g1") is not None
    assert chat_of_scope(DM) == ""


@pytest.mark.parametrize("bad", ["ou_owner", "", " ", "x" * 200, None, 123])
def test_open_id_and_junk_never_bind(tmp_path, bad):
    """🔴 `ou_…` 是人的 open_id：绑进来就等于把单聊当群用（11-05 踩过一次）。"""
    path = tmp_path / "b.json"
    store = ChatBinds(str(path))
    assert store.bind(bad, "claude", CID) is None
    assert not path.exists(), "闸门没过还写了盘"


def test_unknown_tool_or_empty_id_never_bind(tmp_path):
    store = ChatBinds(str(tmp_path / "b.json"))
    assert store.bind("oc_g1", "vim", CID) is None
    assert store.bind("oc_g1", "claude", "") is None


def test_hand_edited_file_with_open_id_is_ignored_on_load(tmp_path):
    """磁盘不可信：旧版本 / 手改写进去的 `ou_` 行，加载时就丢掉。"""
    path = tmp_path / "b.json"
    path.write_text(json.dumps({"binds": {
        "ou_owner": {"tool": "claude", "id": CID},
        "oc_ok": {"tool": "claude", "id": CID2, "name": "好的"},
        "oc_badtool": {"tool": "rm", "id": CID},
    }}), encoding="utf-8")
    store = ChatBinds(str(path))
    assert [b.chat_id for b in store.all()] == ["oc_ok"]


def test_group_message_does_not_touch_the_dm_target(tmp_path):
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)
    _run(b._handle(f"group:{gid}", gid, "看看"))
    assert b.fleet.peek_pending(DM) is None, "群里的绑定把单聊也拉进了对话模式"
    _run(b._handle(DM, DM_CHAT, "单聊里的话"))
    assert (DM, "单聊里的话") in b.handled and [t for _, t in claude.relayed] == ["看看"]


# ---------- 2 幂等 + 落盘 ----------


def test_ensure_group_twice_creates_one_group(tmp_path):
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    first = ensure_group(store, groups, "claude", CID, "修登录", owner_open_id=OWNER)
    second = ensure_group(store, groups, "claude", CID, "修登录", owner_open_id=OWNER)
    assert first.how == CREATED and second.how == REUSED
    assert len(groups.created) == 1 and second.bound.chat_id == first.bound.chat_id
    assert groups.created[0][2] == OWNER, "建群没带上主人（群里只有机器人）"


def test_dissolved_group_is_rebuilt(tmp_path):
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    first = ensure_group(store, groups, "claude", CID, "修登录")
    groups.alive.discard(first.bound.chat_id)  # 主人把群解散了
    again = ensure_group(store, groups, "claude", CID, "修登录")
    assert again.how == CREATED and again.bound.chat_id != first.bound.chat_id
    assert store.of_chat(first.bound.chat_id) is None, "指向死群的旧记录没清掉"
    # 重建时群名不该因为「旧名字被占」而加上 #id（旧的已经忘了）
    assert again.bound.name == "Claude · 修登录"


def test_cannot_tell_if_group_exists_means_reuse_not_rebuild(tmp_path):
    """查不出来就当它还在：不然每次读失败都多一个群。"""
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    ensure_group(store, groups, "claude", CID, "修登录")
    groups.exists_raises = True
    assert ensure_group(store, groups, "claude", CID, "修登录").how == REUSED
    assert len(groups.created) == 1


def test_binds_survive_a_restart(tmp_path):
    path = tmp_path / "b.json"
    store, groups = ChatBinds(str(path)), FakeGroups()
    made = ensure_group(store, groups, "claude", CID, "修登录")
    reborn = ChatBinds(str(path))  # 看门狗重启
    assert reborn.of_scope(f"group:{made.bound.chat_id}") == made.bound
    assert ensure_group(reborn, groups, "claude", CID, "修登录").how == REUSED


def test_half_written_file_means_no_binds(tmp_path):
    path = tmp_path / "b.json"
    path.write_text("{ 半截 JSON", encoding="utf-8")
    assert ChatBinds(str(path)).all() == []


def test_save_uses_tempfile_and_replace(tmp_path, monkeypatch):
    """落盘写法抄 prefs.py：中途失败不能留下半截文件、也不能把原文件弄坏。"""
    path = tmp_path / "b.json"
    store = ChatBinds(str(path))
    store.bind("oc_g1", "claude", CID, "x")
    before = path.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("磁盘满")

    monkeypatch.setattr(os, "replace", boom)
    store.bind("oc_g2", "claude", CID2, "y")
    assert store.last_save_ok is False
    assert path.read_text(encoding="utf-8") == before


def test_one_session_occupies_one_group(tmp_path):
    """同一个会话改绑到别的群 ⇒ 旧群**关掉转发但记录留着**，并报出来让调用方善后。

    🔴 原来是直接删记录：旧群变成「谁都不绑」，而它的待发目标还在 ⇒ 主人在旧群继续说话，
    5 分钟内还进这个会话、5 分钟后悄悄落到桥自己的 claude（2026-11-05 两份审查都指出）。
    """
    store = ChatBinds(str(tmp_path / "b.json"))
    store.bind("oc_g1", "claude", CID, "x")
    store.bind("oc_g2", "claude", CID, "x")
    old = store.of_chat("oc_g1")
    assert old is not None and not old.on, "旧群记录被删了，善后代码就认不出它"
    assert store.of_chat("oc_g2").id == CID and store.of_chat("oc_g2").on
    assert store.last_displaced == ("oc_g1",), "没报出被挪走的群，调用方不会去清它的待发目标"


def test_displaced_group_survives_restart_as_off(tmp_path):
    """重启后旧群仍然是「记录在、转发关」，不会变回「谁都不绑」。"""
    path = str(tmp_path / "b.json")
    store = ChatBinds(path)
    store.bind("oc_g1", "claude", CID, "x")
    store.bind("oc_g2", "claude", CID, "x")
    again = ChatBinds(path)
    assert again.of_chat("oc_g1") is not None and not again.of_chat("oc_g1").on


# ---------- 改绑之后群名跟过去（2026-11-05 两份 review 的同一条）----------


def _bound_to(store, groups, sid, title):
    """把一个群绑到 sid 上（不改群名），返回那条 Bound。"""
    made = ensure_group(store, groups, "claude", CID, "修登录", owner_open_id=OWNER)
    if sid != CID:
        store.bind(made.bound.chat_id, "claude", sid, made.bound.name)
    return store.of_chat(made.bound.chat_id)


def test_rebind_renames_the_group_to_the_new_session(tmp_path):
    """🔴 群名还写着老会话的名字 ⇒ 主人看群名以为在跟老的说话，而每句都进了新的那个。"""
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    got = rename_group(store, groups, bound, "写文档")
    assert got.how == RENAMED and got.name == "Claude · 写文档"
    assert groups.renamed == [(bound.chat_id, "Claude · 写文档")]
    assert store.of_chat(bound.chat_id).name == "Claude · 写文档", "记的还是老名字 ⇒ 下次会误判"


def test_a_name_the_owner_changed_is_never_overwritten(tmp_path):
    """群主是主人，他随时能改群名。改群名是外部可见动作 ⇒ 他起的名字一个字都不碰。"""
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    groups.names[bound.chat_id] = "我的活儿"  # 他自己改了
    got = rename_group(store, groups, bound, "写文档")
    assert got.how == KEPT_OWN and got.name == "我的活儿"
    assert groups.renamed == []
    assert store.of_chat(bound.chat_id).name == "Claude · 修登录", \
        "把他改的名字记成了「我们起的」⇒ 下一次改绑就会冲掉它"


def test_unreadable_current_name_means_do_not_touch_it(tmp_path):
    """读不出群名（没开「查看群信息」/ 断网）⇒ 不知道他动过没动过 ⇒ 不赌，不改。"""
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    groups.name_fails = True
    assert rename_group(store, groups, bound, "写文档").how == NO_RENAME
    assert groups.renamed == []


def test_same_name_is_not_a_rename_call(tmp_path):
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID, "修登录")
    assert rename_group(store, groups, bound, "修登录").how == SAME
    assert groups.renamed == [], "名字没变还去调了一次改名接口"


def test_failed_rename_does_not_record_the_new_name(tmp_path):
    """🔴 先记后改的话：改名失败 ⇒ 下一次以为「现在这个名字是我们起的」⇒ 冲掉主人的名字。"""
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    groups.rename_code = 99991672  # 没开「更新群信息」
    got = rename_group(store, groups, bound, "写文档")
    assert got.how == NO_RENAME and got.code == 99991672
    assert store.of_chat(bound.chat_id).name == "Claude · 修登录"
    # 权限开了之后再点一次：这次该改成
    groups.rename_code = 0
    assert rename_group(store, groups, store.of_chat(bound.chat_id), "写文档").how == RENAMED


@pytest.mark.parametrize("flag", ["name_raises", "rename_raises"])
def test_rename_never_raises(tmp_path, flag):
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    setattr(groups, flag, True)
    assert rename_group(store, groups, bound, "写文档").how == NO_RENAME


def test_rename_without_the_capability_is_not_an_error(tmp_path):
    """没接改群名那一层（老版本的 client）⇒ NO_RENAME，不炸。"""
    class OnlyCreate:
        def create(self, name, owner_open_id=""):
            return "oc_x", 0, ""

    store = ChatBinds(str(tmp_path / "b.json"))
    store.bind("oc_x", "claude", CID, "Claude · 修登录")
    got = rename_group(store, OnlyCreate(), store.of_chat("oc_x"), "写文档")
    assert got.how == NO_RENAME and got.name == "Claude · 修登录"


def test_new_name_is_redacted_and_does_not_collide(tmp_path):
    """新群名照样过脱敏 + 避开别的群占掉的名字；**自己现在的名字不算被占**。"""
    from bridge.app import _redact_text

    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    store.bind("oc_other", "claude", "99999999-9999-4999-8999-999999999999", "Claude · 写文档")
    got = rename_group(store, groups, bound, "写文档 sk-ant-api03-" + "Z" * 40, clean=_redact_text)
    assert got.how == RENAMED
    assert "sk-ant" not in got.name and "ZZZ" not in got.name
    assert got.name != "Claude · 写文档", "和另一个群撞名了"


def test_the_new_name_survives_a_restart(tmp_path):
    """🔴 改完的名字必须落盘。

    看门狗重启是常态。不落盘的话重启后 `Bound.name` 还是老名字，而飞书上是新的 ⇒
    下一次改绑把这个**我们自己改的**名字误判成「主人改过的」⇒ 从此再也不改这个群的名，
    群名永远停在那一次。
    """
    path = str(tmp_path / "b.json")
    store, groups = ChatBinds(path), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    assert rename_group(store, groups, bound, "写文档").how == RENAMED
    again = ChatBinds(path)  # 重启
    assert again.of_chat(bound.chat_id).name == "Claude · 写文档"
    # 重启之后再改一次（会话又改名了）⇒ 照样认得出是我们起的名，不会判成「主人改过」
    assert rename_group(again, groups, again.of_chat(bound.chat_id), "写测试").how == RENAMED


def test_set_name_does_not_record_a_name_the_owner_chose(tmp_path):
    """`set_name` 只给 `rename_group` 在**改成之后**调。它是那把尺子，记错就失去保护。"""
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID2, "修登录")
    groups.names[bound.chat_id] = "我的活儿"
    rename_group(store, groups, bound, "写文档")   # KEPT_OWN
    rename_group(store, groups, store.of_chat(bound.chat_id), "再换一个")
    assert groups.renamed == [], "第二次就把主人的群名冲掉了（尺子被污染）"


def test_renaming_to_its_own_current_name_does_not_add_a_short_id(tmp_path):
    """🔴 算「这个群改个名」时要排除它自己，不然它现在的名字被当成被占 ⇒ 白加一截 #id。"""
    store, groups = ChatBinds(str(tmp_path / "b.json")), FakeGroups()
    bound = _bound_to(store, groups, CID, "修登录")
    assert "#" not in rename_group(store, groups, bound, "修登录").name


# ---------- 建不了群：照实说 ----------


def test_no_permission_is_reported_separately(tmp_path):
    store = ChatBinds(str(tmp_path / "b.json"))
    got = ensure_group(store, FakeGroups(code=99991672, msg="Access denied"), "claude", CID, "x")
    assert got.how == NO_PERMISSION and not got.ok and store.all() == []


def test_other_failures_and_exceptions_are_failed(tmp_path):
    store = ChatBinds(str(tmp_path / "b.json"))
    assert ensure_group(store, FakeGroups(code=230002), "claude", CID, "x").how == FAILED
    assert ensure_group(store, FakeGroups(raises=True), "claude", CID, "x").how == FAILED
    assert store.all() == [], "没建成也落了盘 ⇒ 绑定指向一个不存在的群"


def test_no_permission_falls_back_to_talking_in_the_dm(tmp_path):
    """没权限：说清楚要开权限，然后退回老路（就在单聊里聊），不是死路。"""
    claude = FakeClaude([_sess()])
    b = _bridge(tmp_path, claude, FakeGroups(code=99991672))
    b.fleet.overview()
    _open_group(b)
    said = "\n".join(b.replier.to(DM_CHAT))
    assert "权限" in said and "发布" in said
    assert b._pending_consent.get(DM) == ("claude", CID), "退回老路时没弹单聊的确认卡"
    assert b.chat_binds.all() == []


def test_dm_click_queues_group_creation_only_when_client_exists(tmp_path):
    claude = FakeClaude([_sess()])
    b = _bridge(tmp_path, claude, FakeGroups())
    b.fleet.overview()
    resp = _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert "建群" in _toast(resp)
    assert b.fleet.peek_pending(DM) is None
    # 没有建群接口（权限还没开）⇒ 老样子：弹单聊的确认卡
    b2 = _bridge(tmp_path / "b2", FakeClaude([_sess()]), None)
    b2.fleet.overview()
    resp = _click(b2, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert "确认" in _toast(resp) and b2._pending_consent.get(DM) == ("claude", CID)


# ---------- 4 权限确认 + 3 长期直达 ----------


def test_new_group_asks_consent_in_the_group_and_forwards_nothing_before(tmp_path):
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    assert b.replier.cards_to(gid), "群里没先发权限说明卡"
    # 2026-11-06 建群即进对话后，群里第一张卡是「现在就能说」那句的按钮卡；这条测的是撤掉自动确认后补的那张确认卡
    assert any("管不到" in c for c in b.replier.cards_to(gid)), "确认卡没说清手机这边管不到它"
    assert b._pending_consent.get(f"group:{gid}") == ("claude", CID)
    _run(b._handle(f"group:{gid}", gid, "把登录改掉"))
    assert claude.relayed == [] and claude.sent == [], "没确认就转发了"
    assert b.handled == [], "没确认的话落进了桥自己的 claude"
    assert any("还没确认" in t for t in b.replier.to(gid))


def test_dm_consent_does_not_count_for_the_group(tmp_path):
    """群里能看到的人更多 ⇒ 单聊里确认过不等于群里确认过。"""
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    b.relay_consent.grant(DM, "claude", CID, b._relay_ident("claude", CID))
    _open_group(b)
    gid = groups.created[0][0]
    assert b.replier.cards_to(gid), "单聊的确认被当成了群的确认"


def test_after_consent_every_group_message_goes_to_the_session(tmp_path):
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)
    for line in ("一", "二", "三"):
        _run(b._handle(f"group:{gid}", gid, line))
    assert [t for _, t in claude.relayed] == ["一", "二", "三"]
    assert b.handled == []


def test_group_binding_does_not_expire_after_five_minutes(tmp_path):
    """🔴 绑定是长期的：5 分钟待发目标过期了，群里的下一句照样直达。"""
    clock = [1000.0]
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet._now = lambda: clock[0]
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)
    _run(b._handle(f"group:{gid}", gid, "第一句"))
    clock[0] += 3600  # 一小时后
    _run(b._handle(f"group:{gid}", gid, "一小时后"))
    assert [t for _, t in claude.relayed] == ["第一句", "一小时后"]
    assert not any("5 分钟" in t for t in b.replier.to(gid)), "群里冒出了「过了 5 分钟」"


def test_consent_still_expires_after_24h_in_the_group(tmp_path):
    """绑定长期，确认不长期：隔天再说话要重新确认一次。"""
    now = [1000.0]
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.relay_consent = RelayConsent(str(tmp_path / "rc.json"), now=lambda: now[0])
    b.fleet._now = lambda: now[0]
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)
    _run(b._handle(f"group:{gid}", gid, "今天"))
    now[0] += 25 * 3600
    _run(b._handle(f"group:{gid}", gid, "明天"))
    assert [t for _, t in claude.relayed] == ["今天"]


def test_group_binding_survives_restart_end_to_end(tmp_path):
    """看门狗重启：内存全空（_chat_ids、待发目标、扫描缓存），群里说话照样直达。"""
    path = tmp_path / "binds.json"
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups, binds_path=path)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)

    b2 = _bridge(tmp_path, claude, groups, binds_path=path)  # 重启：同一个家目录
    b2._chat_ids = {}
    assert b2.fleet.known_keys() == set()
    _run(b2._handle(f"group:{gid}", gid, "重启后"))
    assert [t for _, t in claude.relayed] == ["重启后"]
    # 重启后在群里点卡片：不用先发一句话也认得出是群
    _, scope = b2._click_scope(types.SimpleNamespace(context=types.SimpleNamespace(open_chat_id=gid)), OWNER)
    assert scope == f"group:{gid}"


def test_clicking_the_same_session_again_reuses_and_says_so(tmp_path):
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    _open_group(b)
    assert len(groups.created) == 1
    assert any("已经有群" in t for t in b.replier.to(DM_CHAT))


# ---------- 5 会话没了 ----------


def test_gone_session_is_said_plainly_with_buttons(tmp_path):
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)
    claude.sessions = []  # 电脑上关掉了
    _run(b._handle(f"group:{gid}", gid, "还在吗"))
    assert claude.relayed == [] and b.handled == [], "死群里的话悄悄落进了桥自己的 claude"
    said = "\n".join(b.replier.to(gid))
    assert "不在了" in said
    cards = "\n".join(b.replier.cards_to(gid))
    assert '"op": "sessions"' in cards and '"op": "quit"' in cards
    assert groups.alive == {gid}, "不该自动解散群"


def test_unreadable_session_with_no_cache_is_not_forced(tmp_path):
    """读不出来 ≠ 没了；但连上次都没见过就别硬发。"""
    path = tmp_path / "b.json"
    ChatBinds(str(path)).bind("oc_g1", "claude", CID, "x")
    claude = FakeClaude([_sess()])
    claude.find_raises = True
    b = _bridge(tmp_path, claude, FakeGroups(), binds_path=path)
    _run(b._handle("group:oc_g1", "oc_g1", "hi"))
    assert claude.relayed == [] and b.handled == []
    assert any("读不出" in t for t in b.replier.to("oc_g1"))


# ---------- 8 解绑 ----------


def test_cancel_in_bound_group_unbinds_but_keeps_the_group(tmp_path):
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)
    _run(b._handle(f"group:{gid}", gid, "取消"))
    assert b.chat_binds.of_chat(gid).on is False
    assert groups.alive == {gid}, "解绑不该解散群"
    assert any("不再转发" in t for t in b.replier.to(gid))
    _run(b._handle(f"group:{gid}", gid, "之后的话"))
    assert claude.relayed == [] and (f"group:{gid}", "之后的话") in b.handled
    # 再点那个会话 ⇒ 复用这个群、重新接上
    _open_group(b)
    assert len(groups.created) == 1 and b.chat_binds.of_chat(gid).on is True


def test_quit_button_in_bound_group_unbinds(tmp_path):
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _run(b._handle_command(f"group:{gid}", gid, "/cancel"))
    assert b.chat_binds.of_chat(gid).on is False


def test_clicking_another_session_in_a_bound_group_rebinds(tmp_path):
    claude = FakeClaude([_sess(), _sess(CID2, title="写文档")])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID2}, chat_id=gid)
    assert b.chat_binds.of_chat(gid).id == CID2
    assert len(groups.created) == 1, "在群里点会话不该再建群"
    assert b._pending_consent.get(f"group:{gid}") == ("claude", CID2), "改绑后没重新问权限"


def test_rebinding_in_a_group_also_renames_it(tmp_path):
    """🔴 改绑了但群名还写着老会话 ⇒ 主人看群名以为在跟老的说话，而每句都进了新的。"""
    claude = FakeClaude([_sess(), _sess(CID2, title="写文档")])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    assert groups.names[gid] == "Claude · 修登录"
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID2}, chat_id=gid)
    assert groups.names[gid] == "Claude · 写文档", "改绑之后群名没跟过去"
    assert b.chat_binds.of_chat(gid).name == "Claude · 写文档"
    said = "\n".join(b.replier.to(gid))
    assert "写文档" in said and "群名也改" in said, f"没告诉他群名改了：{said!r}"


def test_rebinding_keeps_a_name_the_owner_picked_and_says_so(tmp_path):
    """主人自己改过群名 ⇒ 不覆盖，但要在群里说清「现在直达的是另一个会话」。"""
    claude = FakeClaude([_sess(), _sess(CID2, title="写文档")])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    groups.names[gid] = "我的活儿"  # 他在飞书里自己改了
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID2}, chat_id=gid)
    assert groups.names[gid] == "我的活儿", "把主人起的群名冲掉了"
    said = "\n".join(b.replier.to(gid))
    assert "写文档" in said, "没说清现在直达哪个会话"
    assert "你自己改过的名字我不动" in said


def test_rename_failure_does_not_break_the_rebind(tmp_path):
    """没开「更新群信息」权限：绑定照样改好，只是群名没跟过去 —— 不能让这一下点击崩掉。"""
    claude = FakeClaude([_sess(), _sess(CID2, title="写文档")])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    groups.rename_code = 99991672
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID2}, chat_id=gid)
    assert b.chat_binds.of_chat(gid).id == CID2, "改名失败把改绑也带崩了"
    assert groups.names[gid] == "Claude · 修登录"
    assert any("写文档" in t for t in b.replier.to(gid))


# ---------- 「算了」= 解绑 ----------


def test_tapping_no_in_a_bound_group_unbinds_it(tmp_path):
    """🔴 点了「算了」只清待发目标的话，下一句又按绑定接回去、再弹一次卡 ——
    用户以为断了，其实没断（2026-11-05 体验审查）。"""
    claude = FakeClaude([_sess()])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    def consent_cards():
        return [c for c in b.replier.cards_to(gid) if "这个群直达电脑上的会话" in c]

    assert len(consent_cards()) == 1
    resp = _click(b, {"act": "relay", "op": "no", "tool": "claude", "id": CID}, chat_id=gid)
    assert "不转发" in _toast(resp), _toast(resp)
    assert b.chat_binds.of_chat(gid).on is False, "点了算了绑定还开着"
    assert groups.alive == {gid}, "算了不该解散群"
    assert any("不再转发" in t for t in b.replier.to(gid))
    # 下一句：按平常处理，**不再弹确认卡**（原来会按绑定接回去、再问一次）
    _run(b._handle(f"group:{gid}", gid, "下一句"))
    assert claude.relayed == [] and (f"group:{gid}", "下一句") in b.handled
    assert len(consent_cards()) == 1, "点了算了之后还在弹确认卡"
    # 再点那个会话 ⇒ 复用这个群、重新接上
    _open_group(b)
    assert len(groups.created) == 1 and b.chat_binds.of_chat(gid).on is True


def test_tapping_no_in_a_dm_is_unchanged(tmp_path):
    """单聊没有绑定 ⇒ 「算了」照旧只是「不进对话模式」。"""
    b = _bridge(tmp_path, FakeClaude([_sess()]), None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    resp = _click(b, {"act": "relay", "op": "no", "tool": "claude", "id": CID})
    assert "不进" in _toast(resp)
    assert b.fleet.peek_pending(DM) is None
    assert any("不进对话模式" in t for t in b.replier.to(DM_CHAT))


# ---------- 作废的确认卡 ----------


def test_a_stale_consent_card_cannot_enter_talk_mode_after_cancel(tmp_path):
    """🔴 说了「取消」之后，聊天记录里那张旧确认卡照样能进对话模式，还顺手写下 24 小时
    免确认 —— 他以为退出了，下一句直接进了那个「不问就改」的会话（2026-11-05 安全审查）。
    文字通道 `_confirm_talk_by_text` 本来就是这么拒的，两条路要一致。"""
    claude = FakeClaude([_sess()])
    b = _bridge(tmp_path, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b._pending_consent.get(DM) == ("claude", CID)
    _run(b._handle_command(DM, DM_CHAT, "取消"))
    resp = _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    assert "失效" in _toast(resp), _toast(resp)
    assert b.fleet.peek_pending(DM) is None, "作废的卡片把他送进了对话模式"
    assert not b.relay_consent.granted(DM, "claude", CID, b._relay_ident("claude", CID)), \
        "作废的卡片换来了 24 小时免确认"


def test_a_card_for_another_session_is_refused(tmp_path):
    """等的是 A 的确认，点到 B 的旧卡 ⇒ 拒（不然点一下就进了一个没问过的会话）。"""
    claude = FakeClaude([_sess(), _sess(CID2, title="写文档")])
    b = _bridge(tmp_path, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    resp = _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID2})
    assert "失效" in _toast(resp)
    assert b.fleet.peek_pending(DM) is None


def test_after_a_restart_the_stale_card_also_explains_itself(tmp_path):
    """🔴 这道闸最常见的触发原因不是攻击，是**服务重启过**（看门狗自杀等拉起，本机已 49 次）。

    toast 一闪而过 ⇒ 只弹一下「失效了」，用户看到的是「按钮点了没反应」
    （2026-11-05 他已经报过一次这个形状）。所以除了 toast，还要有一条**带按钮**的消息。
    """
    claude = FakeClaude([_sess()])
    b = _bridge(tmp_path, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})

    # 模拟重启：内存里那张「正在等」的记录没了，落盘的东西还在
    b2 = _bridge(tmp_path, FakeClaude([_sess()]), None)
    b2.fleet.overview()
    assert not getattr(b2, "_pending_consent", {}), "这条测试要的前提是重启后内存态为空"

    resp = _click(b2, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    assert "失效" in _toast(resp)
    assert b2.fleet.peek_pending(DM) is None, "重启后那张旧卡不许换来对话模式"
    said = "\n".join(b2.replier.to(DM_CHAT))
    assert "重启" in said, f"只给了 toast，没给下一步：{said!r}"


def test_the_card_we_are_waiting_for_still_works(tmp_path):
    """阳性对照：正常那条路不许被这道闸挡住。"""
    claude = FakeClaude([_sess()])
    b = _bridge(tmp_path, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    resp = _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    assert "说吧" in _toast(resp)
    assert b.fleet.peek_pending(DM) is not None


# ---------- 「每一句都直达」这句话要看权限 ----------


class _PermGroups(FakeGroups):
    """带权限体检的假 client（真实那层是 `groupsetup.GroupSetup.report`）。"""

    def __init__(self, receive_ok=False, report_raises=False):
        super().__init__()
        self.receive_ok = receive_ok
        self.report_raises = report_raises

    def report(self, **_kw):
        if self.report_raises:
            raise RuntimeError("体检炸了")
        from bridge.fleet import groupsetup as gs

        return gs.Report("app", tuple(
            gs.Need(k, lbl, gs.OK if (k != "receive" or self.receive_ok) else gs.UNKNOWN, sc, con)
            for k, lbl, sc, con, _a in gs.NEEDS))


@pytest.mark.parametrize("groups,promise", [
    (_PermGroups(receive_ok=True), True),
    (_PermGroups(receive_ok=False), False),
    (_PermGroups(report_raises=True), False),  # 查不出来 ⇒ 保守
    (FakeGroups(), False),                     # 没有体检这个能力 ⇒ 保守
])
def test_group_copy_promises_every_message_only_when_the_scope_is_on(tmp_path, groups, promise):
    """🔴 没开 `im:message.group_msg` 时飞书只推 @ 了机器人的消息。照说「每一句都直达」，
    主人在群里说一句没人理，他以为建群这功能坏了（2026-11-05 体验审查）。"""
    b = _bridge(tmp_path, FakeClaude([_sess()]), groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    said = "\n".join(b.replier.to(DM_CHAT)) + "\n" + "\n".join(b.replier.cards_to(gid))
    if promise:
        assert "每一句都直达" in said and "@ 一下机器人" not in said
    else:
        assert "@ 一下机器人" in said, said[-400:]
        assert "每一句都直达它" not in said, "权限没确认就承诺了「每一句」"


def test_read_failure_is_not_reported_as_deleted():
    """🔴 Codex 的库被 App 锁住时 find() 也返回 None ⇒ 原来群里报「会话已经关掉或删了」，
    而它还在电脑上跑着（2026-11-05 体验审查）。读失败要走「读不出来」那条分支。"""
    import types

    from bridge.fleet.control import FleetControl
    from bridge.fleet.scan import Fleet

    class Broken:
        tool = "codex"
        last_error = "state db 被锁住了"

        def find(self, sid):
            return None

        def list(self, limit=20):
            return []

    ctl = FleetControl(Fleet({"codex": Broken()}))
    sess, why = ctl.refresh("codex", "x" * 8)
    assert sess is None and "读不出" in why, f"读失败被当成了「没了」：{why!r}"

    class Gone(Broken):
        last_error = ""

    ctl2 = FleetControl(Fleet({"codex": Gone()}))
    assert ctl2.refresh("codex", "x" * 8) == (None, ""), "真没了时不该编一个原因"


def test_group_gets_the_group_consent_card(tmp_path):
    """🔴 群里要发群专用确认卡：单聊那张没有「群里其他人也能看到回复」这句（2026-11-05 体验审查）。"""
    from bridge.fleet.groupsetup import build_group_consent_card
    from bridge.fleet.model import Session

    sess = Session(tool="claude", surface="cli", id="c" * 8, title="修登录", cwd="/x",
                   status="idle", owner="terminal", updated_at=0, can_send=False)
    card = build_group_consent_card(sess, redact=lambda s: s)
    assert "群里" in card and ("别" in card or "其他人" in card)
