"""群里的四句话（2026-11-06 第 3 轮，UX 侦察 Top 2）。

1. **前后说法一致**：确认卡说「@ 一下机器人再说话」（没开收群消息权限时），点确认后紧接着那句
   原来写死「直接打字发过来就行」⇒ 照后一句做，机器人一声不吭。两句必须按同一个判断
   （`groupsetup.receive_granted`）走。
2. **确认前那句不吞**：群里先说了一句 → 弹确认卡 → 点确认 ⇒ 原来那句没了，要他「再说一次」。
   现在先记着，**卡上引用原话明说**确认后会发过去，点确认后走正常管道发出去
   （身份复查、审查层照常）。点「算了」/ 隔太久 ⇒ 不发，而且照实说。
3. **不双前缀**：「Claude「Claude · 198 litellm 5x…」」—— 群名本身带工具前缀，又被当会话名套了一层。
4. **不叫人打「取消」**：群里那句「要停止转发就说「取消」」换成按钮。

夹具放测试目录旁边（`here_dir`），不放 tmp_path（macOS 上是 pathguard 的豁免前缀）。
一个群都不真建、一个飞书接口都不调。
"""

from __future__ import annotations

import ast
import os
import pathlib
import shutil
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.fleet.chatbind import Bound, group_name  # noqa: E402
from tests.test_chatbind import (  # noqa: E402
    CID,
    DM_CHAT,
    FakeClaude,
    FakeGroups,
    _bridge,
    _click,
    _confirm_in_group,
    _open_group,
    _PermGroups,
    _run,
    _sess,
)

APP_PY = pathlib.Path(_HERE).parent / "bridge" / "app.py"
TITLE = "198 litellm 5xx 归因"


@pytest.fixture
def here_dir():
    base = pathlib.Path(_HERE).parent / f".groupcopy-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _new_group(here_dir, groups=None, title=TITLE):
    claude = FakeClaude([_sess(title=title)])
    groups = groups if groups is not None else FakeGroups()
    b = _bridge(here_dir, claude, groups)
    b.fleet.overview()
    _open_group(b)
    return b, claude, groups.created[0][0]


def _say(b, gid, text):
    _run(b._handle(f"group:{gid}", gid, text))


def _texts(b, gid):
    return "\n".join(b.replier.to(gid))


def _no(b, gid):
    return _click(b, {"act": "relay", "op": "no", "tool": "claude", "id": CID}, chat_id=gid)


def _confirm_newest(b, gid):
    """点**最新那张**确认卡上真实的「确认」按钮值（带第 10 轮盖的序号，真飞书点下去回的就是它）。"""
    import json as _json

    def talk_values(node, out):
        if isinstance(node, dict):
            v = node.get("value")
            if isinstance(v, dict) and v.get("act") == "relay" and v.get("op") == "talk":
                out.append(v)
            for c in node.values():
                talk_values(c, out)
        elif isinstance(node, list):
            for c in node:
                talk_values(c, out)
        return out

    cards = [c for c in b.replier.cards_to(gid) if talk_values(_json.loads(c), [])]
    return _click(b, talk_values(_json.loads(cards[-1]), [])[0], chat_id=gid)


# ---------- 1 前后说法一致 ----------


@pytest.mark.parametrize("receive_ok", [False, True])
def test_after_confirm_the_group_hint_matches_the_consent_card(here_dir, receive_ok):
    """确认卡和确认后那句按**同一个判断**说「要不要 @」。"""
    b, _claude, gid = _new_group(here_dir, _PermGroups(receive_ok=receive_ok))
    # 取**确认卡**（含「确认，开始说话」那张），不是群里第一张（2026-11-06 起第一张是「现在就能说」的按钮卡）
    card = next(c for c in reversed(b.replier.cards_to(gid)) if "确认，开始说话" in c)
    before = len(b.replier.to(gid))
    _confirm_in_group(b, gid)
    after = "\n".join(b.replier.to(gid)[before:])
    assert after, "点确认后群里一句话都没有"
    if receive_ok:
        assert "每一句" in card and "@ 一下" not in card
        assert "@ 一下" not in after, f"权限开了还叫他 @：{after!r}"
    else:
        assert "@ 一下" in card, "前提：权限没开时确认卡叫他 @"
        assert "@ 一下" in after, f"确认卡叫他 @，确认后那句却没说：{after!r}"
        assert "直接打字" not in after, f"确认卡叫他 @，确认后那句叫他直接打字：{after!r}"


def test_dm_enter_text_is_unchanged(here_dir):
    """阳性对照：单聊里收消息不用 @，那句照旧「直接打字」。"""
    claude = FakeClaude([_sess()])
    b = _bridge(here_dir, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    said = "\n".join(b.replier.to(DM_CHAT))
    assert "直接打字" in said and "@ 一下" not in said


# ---------- 2 确认前那句不吞 ----------


def test_the_sentence_before_confirm_is_quoted_on_the_card_and_sent_after(here_dir):
    b, claude, gid = _new_group(here_dir)
    _say(b, gid, "把登录改掉")
    assert claude.relayed == [] and b.handled == [], "没确认就发出去了"
    said = _texts(b, gid)
    assert "再说一次" not in said, f"还在叫他重打：{said!r}"
    card = b.replier.cards_to(gid)[-1]
    assert "把登录改掉" in card, "卡上没写确认后要发的是哪句（他点确认时看不到）"
    _confirm_newest(b, gid)
    assert [t for _, t in claude.relayed] == ["把登录改掉"], "确认后那句没发出去"
    assert b.handled == []


def test_several_sentences_before_confirm_go_out_together_once(here_dir):
    b, claude, gid = _new_group(here_dir)
    _say(b, gid, "一")
    _say(b, gid, "二")
    assert "2 句" in b.replier.cards_to(gid)[-1]
    assert "一" in b.replier.cards_to(gid)[-1] and "二" in b.replier.cards_to(gid)[-1], "卡上没列出每一句"
    _confirm_newest(b, gid)
    assert [t for _, t in claude.relayed] == ["一\n二"]
    _say(b, gid, "三")
    assert [t for _, t in claude.relayed] == ["一\n二", "三"], "记着的那几句被发了两次"


def test_tapping_no_drops_the_held_sentence_and_says_so(here_dir):
    b, claude, gid = _new_group(here_dir)
    _say(b, gid, "把登录改掉")
    before = len(b.replier.to(gid))
    _no(b, gid)
    assert claude.relayed == []
    after = "\n".join(b.replier.to(gid)[before:])
    assert "没发" in after, f"点了「算了」，没说那句没发：{after!r}"


def test_held_sentence_is_not_sent_after_cancel_then_reopen(here_dir):
    """说了取消 ⇒ 记着的那句作废；之后重新接上再确认，不许把旧话发过去。"""
    b, claude, gid = _new_group(here_dir)
    _say(b, gid, "旧话")
    _say(b, gid, "取消")
    _open_group(b)               # 重新接上（复用这个群）⇒ 又弹卡
    _confirm_in_group(b, gid)
    assert claude.relayed == [], "取消过的话在重新确认后被发了出去"


def test_a_stale_held_sentence_is_not_sent_and_is_named(here_dir, monkeypatch):
    import bridge.app as app

    monkeypatch.setattr(app, "HELD_TTL_SEC", -1)
    b, claude, gid = _new_group(here_dir)
    _say(b, gid, "把登录改掉")
    _confirm_newest(b, gid)
    assert claude.relayed == [], "隔太久的话不该自动发"
    said = _texts(b, gid)
    assert "把登录改掉" in said and "没发" in said, f"没照实说那句没发：{said[-200:]!r}"


def test_held_sentence_still_goes_through_the_identity_recheck(here_dir):
    """确认后发那句走的是正常管道：卡弹完之后改了名 ⇒ 这次确认不算数，那句不发。"""
    claude = FakeClaude([_sess(title="修登录")])
    groups = FakeGroups()
    b = _bridge(here_dir, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _say(b, gid, "删掉旧表")
    claude.sessions = [_sess(title="删库跑路")]
    b.fleet.overview()
    _confirm_in_group(b, gid)
    assert claude.relayed == [], "卡是按旧名字弹的，改名后那句不该凭这次确认发出去"


# ---------- 3 不双前缀 ----------


def test_bound_label_strips_the_tool_prefix_of_the_group_name():
    name = group_name("claude", "198 litellm 5xx 归因 再长一点点点点", CID)
    assert name.startswith("Claude · ")
    label = Bound("oc_x", "claude", CID, name, 0.0).label
    assert label.startswith("Claude「") and "Claude「Claude" not in label, label
    # 没名字 ⇒ 短 id
    assert Bound("oc_x", "claude", CID, "", 0.0).label == f"Claude「{CID[:6]}」"


def test_group_lines_use_the_session_name_not_the_group_name(here_dir):
    b, _claude, gid = _new_group(here_dir)
    _say(b, gid, "hi")
    said = _texts(b, gid)
    assert "Claude「Claude" not in said, f"双前缀：{said!r}"
    assert f"Claude「{TITLE}」" in said


# ---------- 4 不叫人打「取消」 ----------


def test_reopened_group_offers_a_quit_button_not_a_typed_word(here_dir):
    b, _claude, gid = _new_group(here_dir, _PermGroups(receive_ok=False))
    _confirm_in_group(b, gid)
    texts_before = len(b.replier.to(gid))
    cards_before = len(b.replier.cards_to(gid))
    _open_group(b)  # 24 小时内再点 ⇒ 走「确认过」那一支
    said = "\n".join(b.replier.to(gid)[texts_before:])
    assert said, "确认过那一支群里没说话"
    assert "「取消」" not in said, f"又叫人打字：{said!r}"
    assert "直接说就行" not in said and "@ 一下" in said, f"和确认卡说法不一致：{said!r}"
    new_cards = "\n".join(b.replier.cards_to(gid)[cards_before:])
    assert '"op": "quit"' in new_cards, "没给「退出对话」按钮"


# ---------- 调用点还在（查调用形状，不查名字） ----------


def _calls(func_name):
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Call)]
    raise AssertionError(f"app.py 里没有 {func_name}")


def test_every_enter_text_call_passes_the_scope():
    src = APP_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    calls = [ast.unparse(n) for n in ast.walk(tree)
             if isinstance(n, ast.Call) and ast.unparse(n.func) == "self._enter_talk_text"]
    assert len(calls) >= 4, calls
    assert all(c.endswith(", scope)") for c in calls), f"有调用点没把 scope 传进去：{calls}"


def test_enter_text_branches_on_the_same_receive_judgment():
    # 2026-11-06 多了 proven=（现场收到过不 @ 的群消息 ⇒ 按「直接说」）—— 同一个判断，多一个依据
    assert ("_group_how_to_talk(self._group_perm_report(), here=True, proven=self._group_msg_proven())"
            in _calls("_enter_talk_text"))


def test_group_how_to_talk_uses_receive_granted():
    assert "receive_granted(report)" in _calls("_group_how_to_talk")


def test_arm_holds_the_text_and_the_card_gets_it():
    calls = _calls("_group_bind_arm")
    assert "self._hold_for_consent(scope, bound.tool, bound.id, text)" in calls
    assert "self._ask_relay_consent(chat_id, scope, bound.tool, bound.id)" in calls
    consent = _calls("_ask_relay_consent")
    assert "self._held_note(scope, tool, sid)" in consent
    assert "self._with_held_note(card, note)" in consent


def test_maybe_send_passes_the_text_to_arm():
    assert "self._group_bind_arm(scope, chat_id, text)" in _calls("_maybe_send_to_fleet")


def test_confirm_click_releases_the_held_text():
    for name in ("_on_relay_click", "_confirm_talk_by_text"):
        calls = _calls(name)
        assert "self._release_held(scope, chat_id, tool, sid)" in calls, name
        assert "self._send_held(scope, chat_id, target, held)" in calls, name
    # 发出去走正常管道（身份复查、审查层都在里面），不另开一条直发的路
    assert "self._handle(scope, chat_id, text)" in _calls("_send_held")
    assert "self._enter_talk_text(target, scope)" in _calls("_send_held")


def test_held_text_is_dropped_on_no_and_cancel():
    assert "self._drop_held(scope)" in _calls("_group_bind_quit")
    assert "self._drop_held(scope)" in _calls("_handle_command")


def test_group_lines_use_bound_label_helper():
    for name in ("_group_bind_arm", "_group_bind_quit"):
        assert "self._bound_label(bound)" in _calls(name), name
