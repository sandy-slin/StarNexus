"""建群即进对话（2026-11-06 用户原话：「建群后自动进入对话，而不是再让用户点一次确认开始对话」）。

+ 截图里那个错：卡上说「要先 @ 一下机器人」，他没 @ 也发进去了、也回了 ⇒
  「收群消息」权限其实开着，只是体检没法主动试（恒为 unknown）⇒ 文案永远走「要先 @」。
  现在：真收到过一条没 @ 的群消息就落盘记下，之后按「直接说」说。

夹具复用 test_chatbind 的假 bridge（一个群都不真建）。
"""

from __future__ import annotations

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from test_chatbind import (  # noqa: E402
    CID,
    DM,
    DM_CHAT,
    OWNER,
    FakeClaude,
    FakeGroups,
    _bridge,
    _open_group,
    _run,
    _sess,
)


def _fresh(tmp_path, **sess_kw):
    claude = FakeClaude([_sess(**sess_kw)])
    groups = FakeGroups()
    b = _bridge(tmp_path, claude, groups)
    # 🔴 一定要指到测试自己的目录：config_path 为空时证明路径会退回真实的 ~/.carher-feishu-agent ——
    # 那是正在服务的实例，里面可能真有证明文件 ⇒ 测试读到线上的状态（第一版就这么假红过）
    b.settings.config_path = str(tmp_path / "config.toml")
    b.fleet.overview()
    _open_group(b, needs_confirm=False)  # 新行为：不撤掉自动确认
    return b, claude, groups.created[0][0]


def test_new_group_needs_no_confirm_click(tmp_path):
    """建群后群里**不弹**「确认，开始说话」卡，第一句直接进会话。"""
    b, claude, gid = _fresh(tmp_path)
    assert not any("确认，开始说话" in c for c in b.replier.cards_to(gid)), "又让他点一次确认了"
    _run(b._handle(f"group:{gid}", gid, "把登录改掉"))
    assert claude.relayed or claude.sent, "建群后第一句没进会话"


def test_group_opening_message_keeps_the_safety_notes(tmp_path):
    """不要确认了，但原来确认卡上的提示**一条不少**：权限是哪套、别拉别人进来、怎么退出。"""
    b, _claude, gid = _fresh(tmp_path)
    said = "\n".join(b.replier.to(gid))
    assert "现在就能说" in said
    assert "别拉别人进来" in said
    assert "退出对话" in said
    assert "权限" in said


def test_dm_message_no_longer_says_confirm_first(tmp_path):
    b, _claude, _gid = _fresh(tmp_path)
    dm = "\n".join(b.replier.to(DM_CHAT))
    assert "先确认一下权限" not in dm, "单聊里还在说「第一次进去要先确认」"


def test_identity_recheck_still_applies_after_auto_enter(tmp_path):
    """🔴 自动进对话不等于永久放行：之后会话在电脑上改了名，群里照样重新问（F 那套）。"""
    b, claude, gid = _fresh(tmp_path)
    claude.sessions = [_sess(title="完全换了一个名字")]
    b.fleet.overview()
    _run(b._handle(f"group:{gid}", gid, "继续"))
    assert claude.relayed == [] and claude.sent == [], "改名之后照样发进去了"
    assert any("确认" in c for c in b.replier.cards_to(gid)), "改名之后没重新问"


def test_codex_session_rights_line_says_readonly(tmp_path):
    """权限那一行按真实走的路说：Codex / Cursor 一律只读，不能说成「电脑上那套、不问就改」。"""
    b, _claude, _gid = _fresh(tmp_path)
    assert "只读" in b._rights_line("codex", "x")
    assert "不问就改" not in b._rights_line("codex", "x")


def test_grant_happens_through_the_shared_path():
    """调用点：建群那条路真的调了自动确认，而且确认走的是和点确认按钮同一套记法。"""
    import inspect

    from bridge.app import Bridge

    src = inspect.getsource(Bridge._open_session_group)
    assert "self._grant_by_open_click(group_scope, tool, sid)" in src
    grant = inspect.getsource(Bridge._grant_by_open_click)
    assert "self._remember_talk_ident(scope, tool, sid, ident)" in grant
    assert "store.grant(scope, tool, sid, ident)" in grant


# ---------- 「不用 @」靠现场证明 ----------


def _group_msg(text, mentions=None):
    return types.SimpleNamespace(
        message_id=f"om_{abs(hash(text))}", chat_id="oc_g1", chat_type="group",
        content='{"text": "%s"}' % text, mentions=mentions, message_type="text")


def test_unmentioned_group_message_proves_the_permission(tmp_path):
    """落盘位置 = config.toml 同目录（线上 settings.config_path 一定有）。"""
    b, _claude, _gid = _fresh(tmp_path)
    b.settings.config_path = str(tmp_path / "config.toml")
    assert not b._group_msg_proven()
    b._mark_group_msg_proven()
    assert b._group_msg_proven()
    # 落盘：重启后的新 bridge 也认
    b2 = _bridge(tmp_path, FakeClaude([_sess()]), FakeGroups())
    b2.settings.config_path = str(tmp_path / "config.toml")
    assert b2._group_msg_proven(), "证明没落盘，重启后又叫人 @"


def test_once_proven_the_wording_stops_asking_for_at():
    from bridge.app import _group_how_to_talk

    assert "@" in _group_how_to_talk(None, here=True, proven=False), "阳性对照：没证明时保守说法"
    assert "@" not in _group_how_to_talk(None, here=True, proven=True)
    assert "每一句" in _group_how_to_talk(None, here=True, proven=True)


def test_message_entry_marks_proof_only_for_unmentioned_group_messages():
    """调用点：消息入口按「群消息 + 没 @」记证明；@ 了的、单聊的都不算（证明不了不 @ 也收得到）。"""
    import inspect

    from bridge.app import Bridge

    src = inspect.getsource(Bridge.on_message)
    assert 'chat_type == "group" and not (getattr(message, "mentions", None) or [])' in src
    assert "self._mark_group_msg_proven()" in src


def test_proof_path_works_when_config_path_is_relative(tmp_path):
    """🔴 线上 launchd 起服务时 config_path 可能就是「config.toml」（相对路径）⇒ dirname 是空串。
    原来那样就返回空路径 ⇒ 证明永远读不到 ⇒ 永远叫人 @（主会话拿部署那份代码模拟时撞上）。
    要和审批目录 / paused.json 同一套兜底：退回 ~/.carher-feishu-agent。
    """
    import os

    b, _claude, _gid = _fresh(tmp_path)
    b.settings.config_path = "config.toml"
    path = b._group_msg_proof_path()
    assert path, "相对 config_path 时证明路径是空的"
    assert path == os.path.join(os.path.expanduser("~/.carher-feishu-agent"), "group_msg_proven.json")
