"""群里「确认过之后」的两道复查（2026-11-06 角色 F，修上一轮角色 D 实测复现的两条缺口）。

1. **确认过之后会话改名 / 换项目**（sessionId 不变）⇒ 群里下一句要重新问，不许直接转发。
   原来 `_group_bind_arm` 里「这个群的待发目标已经是它」那条捷径无限期跳过身份复查。
2. **电脑上关掉 Claude**（owner 从 terminal 变成 none，jsonl 还在）⇒ 下一句不再悄悄换路。
   关掉之后还能发，但走的是另一条路（后台接着跑、权限按手机这边的档位），
   所以要**明说 + 再确认一次**。反方向（关着的会话又在终端里打开 ⇒ 变成电脑那套权限）同样重问。

阳性对照：名字、目录、开没开着都没变时（状态 running→waiting、pid 变、更新时间变）**一张卡都不多弹**。

夹具放测试目录旁边（`here_dir`），不放 tmp_path（macOS 上是 pathguard 的豁免前缀）。
一个群都不真建、一个飞书接口都不调。
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys
import time
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.fleet import Session  # noqa: E402
from bridge.fleet.consent import RelayConsent  # noqa: E402
from tests.test_chatbind import (  # noqa: E402
    CID,
    DM,
    DM_CHAT,
    FakeClaude,
    FakeGroups,
    _bridge,
    _click,
    _confirm_in_group,
    _open_group,
    _run,
)

APP_PY = pathlib.Path(_HERE).parent / "bridge" / "app.py"


@pytest.fixture
def here_dir():
    base = pathlib.Path(_HERE).parent / f".groupguard-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class HonestClaude(FakeClaude):
    """`can_relay` 跟着会话现在的样子走（真 adapter 就是这样）：关掉了就 relay 不了。"""

    def can_relay(self, sid):
        sess = next((s for s in self.sessions if s.id == sid), None)
        if sess is not None and sess.can_relay:
            return True, ""
        return False, "这个会话现在没开着。"


def _open(sid=CID, title="修登录", cwd="/Users/x/codes/demo", **kw):
    """电脑终端里开着的会话。"""
    base = dict(tool="claude", surface="cli", id=sid, title=title, cwd=cwd,
                status="running", pids=(4242,), owner="terminal", updated_at=time.time() - 60,
                can_send=False, why_not_send="会分叉", can_relay=True)
    base.update(kw)
    return Session(**base)


def _closed(sid=CID, title="修登录", cwd="/Users/x/codes/demo", **kw):
    """电脑上关掉了（jsonl 还在）：没人占着 ⇒ 能 resume、不能 relay。"""
    base = dict(tool="claude", surface="cli", id=sid, title=title, cwd=cwd,
                status="idle", pids=(), owner="none", updated_at=time.time() - 60,
                can_send=True, can_relay=False)
    base.update(kw)
    return Session(**base)


def _group(here_dir, sessions, **kw):
    """建好群、在群里点过确认。返回 (bridge, adapter, 群 chat_id)。"""
    claude = HonestClaude(sessions)
    groups = FakeGroups()
    b = _bridge(here_dir, claude, groups)
    for key, value in kw.items():
        setattr(b, key, value)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)
    return b, claude, gid


def _say(b, gid, text):
    _run(b._handle(f"group:{gid}", gid, text))


def _forwarded(claude):
    return [t for _, t in claude.relayed] + [t for _, t in claude.sent]


# ---------- 缺口 1：确认过之后改名 / 换项目 ----------


def test_rename_after_consent_asks_again_in_the_group(here_dir):
    """D 的复现：确认 → 说过话 → 电脑上改名 ⇒ 下一句不许直接 relay，要再弹卡。"""
    b, claude, gid = _group(here_dir, [_open()])
    _say(b, gid, "一")
    assert _forwarded(claude) == ["一"]
    cards_before = len(b.replier.cards_to(gid))

    claude.sessions = [_open(title="删库跑路")]  # 同一个 sessionId，改了名
    _say(b, gid, "二")
    assert _forwarded(claude) == ["一"], "改了名照样转发过去了（确认绑的是旧名字）"
    assert b.handled == [], "没转发的话落进了桥自己的 claude"
    assert len(b.replier.cards_to(gid)) == cards_before + 1, "没重新弹确认卡"
    said = "\n".join(b.replier.to(gid))
    assert "改了名" in said, "没告诉他为什么又要确认"

    # 再确认一次 ⇒ 照常通。卡弹出来之前说的「二」先记着、卡上引用过原话，
    # 确认后发过去（第 3 轮：不再吞掉要他重打）
    assert "二" in b.replier.cards_to(gid)[-1], "卡上没写确认后要发的那句"
    _confirm_in_group(b, gid)
    _say(b, gid, "三")
    assert _forwarded(claude) == ["一", "二", "三"]


def test_project_change_after_consent_asks_again_in_the_group(here_dir):
    """名字没变、换了项目目录（sessionId 不变）⇒ 一样要重新问。"""
    b, claude, gid = _group(here_dir, [_open()])
    _say(b, gid, "一")
    claude.sessions = [_open(cwd="/Users/x/codes/payments")]
    _say(b, gid, "二")
    assert _forwarded(claude) == ["一"], "换了项目照样转发过去了"
    assert b._pending_consent.get(f"group:{gid}") == ("claude", CID)


def test_rename_is_caught_even_when_consent_cannot_be_stored(here_dir):
    """确认记录起不来（store=None）时靠的就是那条捷径 —— 捷径本身也要复查身份。"""
    b, claude, gid = _group(here_dir, [_open()], relay_consent=None)
    _say(b, gid, "一")
    claude.sessions = [_open(title="别的事")]
    _say(b, gid, "二")
    assert _forwarded(claude) == ["一"]


def test_without_a_consent_store_the_group_still_does_not_ask_every_line(here_dir):
    """阳性对照：确认记录存不下来时，确认一次之后不能每句都弹卡（捷径要还在）。"""
    b, claude, gid = _group(here_dir, [_open()], relay_consent=None)
    for line in ("一", "二", "三"):
        _say(b, gid, line)
    assert _forwarded(claude) == ["一", "二", "三"]
    consent_cards = [c for c in b.replier.cards_to(gid) if "确认，" in c]
    assert len(consent_cards) == 1, "确认记录存不下来时每句都弹了卡"


# ---------- 阳性对照：正常路径不多弹卡 ----------


def test_status_pid_and_time_changes_do_not_ask_again(here_dir):
    """在跑 / 在等你 / 换了个 pid / 更新时间变了 —— 都还是你确认的那个会话，不许多弹卡。"""
    b, claude, gid = _group(here_dir, [_open()])
    cards_before = len(b.replier.cards_to(gid))
    texts_before = len(b.replier.to(gid))
    shapes = [
        _open(status="waiting"),
        _open(status="idle", pids=(5151,)),
        _open(updated_at=time.time()),
        _open(),
    ]
    for n, sess in enumerate(shapes):
        claude.sessions = [sess]
        _say(b, gid, f"第{n}句")
    assert _forwarded(claude) == [f"第{n}句" for n in range(len(shapes))]
    assert len(b.replier.cards_to(gid)) == cards_before + len(shapes), \
        "多弹了卡（每句只该有回复下面那张按钮小卡）"
    assert not any("确认" in t for t in b.replier.to(gid)[texts_before:]), "正常路径又要他确认"


def test_a_long_normal_conversation_shows_exactly_one_consent_card(here_dir):
    b, claude, gid = _group(here_dir, [_open()])
    for n in range(8):
        _say(b, gid, f"第{n}句")
    assert len(_forwarded(claude)) == 8
    consent_cards = [c for c in b.replier.cards_to(gid) if "确认，" in c]
    assert len(consent_cards) == 1, f"一段正常对话弹了 {len(consent_cards)} 张确认卡"


def test_closed_session_that_stays_closed_does_not_ask_every_line(here_dir):
    """确认时就是关着的、一直关着 ⇒ 确认一次就够。"""
    b, claude, gid = _group(here_dir, [_closed()])
    for line in ("一", "二", "三"):
        _say(b, gid, line)
    assert [t for _, t in claude.sent] == ["一", "二", "三"]
    consent_cards = [c for c in b.replier.cards_to(gid) if "确认，" in c]
    assert len(consent_cards) == 1


# ---------- 缺口 2：电脑上关掉了 ----------


def test_closing_on_the_computer_stops_and_says_so(here_dir):
    """D 的复现：terminal → none（jsonl 还在）⇒ 下一句不许悄悄换路进去，要明说。"""
    b, claude, gid = _group(here_dir, [_open()])
    _say(b, gid, "一")
    assert [t for _, t in claude.relayed] == ["一"]
    texts_before = len(b.replier.to(gid))

    claude.sessions = [_closed()]
    _say(b, gid, "二")
    assert _forwarded(claude) == ["一"], "关掉之后悄悄换了一条路接着发"
    assert b.handled == []
    said = "\n".join(b.replier.to(gid)[texts_before:])
    assert "关" in said and "手机这边" in said, f"群里没说清：{said!r}"
    assert b._pending_consent.get(f"group:{gid}") == ("claude", CID)


def test_the_closed_card_describes_the_phone_side_permission(here_dir):
    """关着的会话走后台接着跑 ⇒ 卡上要说「权限按手机这边的」，不能还说「电脑上那套」。"""
    b, claude, gid = _group(here_dir, [_open()])
    _say(b, gid, "一")
    claude.sessions = [_closed()]
    _say(b, gid, "二")
    card = b.replier.cards_to(gid)[-1]
    assert "手机这边" in card and "只读" in card, card
    assert "不问你就改" not in card, "关着的会话卡上还在说电脑那套权限"
    assert "确认，" in card and '"op": "talk"' in card and '"op": "no"' in card
    for word in ("claude -p", "--resume", "jsonl", "/Users/"):
        assert word not in card, f"卡上露了 {word}"


def test_after_reconfirming_a_closed_session_it_runs_in_the_background(here_dir):
    """再确认一次之后照常能聊：走 resume（手机这边的档位），不走 relay。"""
    b, claude, gid = _group(here_dir, [_open()])
    claude.sessions = [_closed()]
    _say(b, gid, "一")
    assert _forwarded(claude) == []
    _confirm_in_group(b, gid)   # 确认前那句「一」记着，确认后按新确认的那条路（后台）发过去
    _say(b, gid, "二")
    _say(b, gid, "三")
    assert [t for _, t in claude.sent] == ["一", "二", "三"] and claude.relayed == []


def test_reopening_in_the_terminal_asks_again(here_dir):
    """反方向更要紧：确认时关着（手机这边的权限），后来终端里打开了 ⇒ 下一句会用电脑那套权限。"""
    b, claude, gid = _group(here_dir, [_closed()])
    _say(b, gid, "一")
    assert [t for _, t in claude.sent] == ["一"]
    claude.sessions = [_open()]
    _say(b, gid, "二")
    assert _forwarded(claude) == ["一"], "重新打开之后没问就按电脑那套权限转进去了"
    card = b.replier.cards_to(gid)[-1]
    assert "不问你就改" in card, "开着的会话要用说电脑那套权限的那张卡"


def test_closing_is_caught_after_a_restart(here_dir):
    """看门狗重启：内存全空、确认记录在盘上（当时开着）⇒ 现在关着，照样要重问。"""
    claude = HonestClaude([_open()])
    groups = FakeGroups()
    binds = here_dir / "binds.json"
    b = _bridge(here_dir, claude, groups, binds_path=binds)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    _confirm_in_group(b, gid)

    claude.sessions = [_closed()]
    b2 = _bridge(here_dir, claude, groups, binds_path=binds)
    b2._chat_ids = {}
    _say(b2, gid, "重启后")
    assert _forwarded(claude) == []
    assert any("手机这边" in t for t in b2.replier.to(gid))


def test_dm_click_on_a_now_closed_session_asks_again(here_dir):
    """单聊那条路也一样：确认时开着、现在关了 ⇒ 点 [接着聊] 要重新问。"""
    claude = HonestClaude([_open()])
    b = _bridge(here_dir, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    b.fleet.clear_pending(DM)
    claude.sessions = [_closed()]
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(DM) is None, "关掉之后没问就进了对话模式"
    assert b._pending_consent.get(DM) == ("claude", CID)
    assert "手机这边" in b.replier.cards[-1][1]
    assert b.replier.cards[-1][0] == DM_CHAT


def test_dm_click_on_an_unchanged_session_does_not_ask_again(here_dir):
    """阳性对照：单聊里同一个会话、什么都没变 ⇒ 24 小时内不再问。"""
    claude = HonestClaude([_open()])
    b = _bridge(here_dir, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    b.fleet.clear_pending(DM)
    n_cards = len(b.replier.cards)
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    assert b.fleet.peek_pending(DM) is not None
    assert not any("确认，" in c for _, c in b.replier.cards[n_cards:])


def test_old_consent_records_still_count_for_open_sessions(here_dir):
    """升级不该让所有人重新确认一遍：老格式（名字|目录）的记录对开着的会话照样有效。"""
    from bridge.fleet.consent import session_ident

    claude = HonestClaude([_open()])
    b = _bridge(here_dir, claude, None)
    b.fleet.overview()
    b.relay_consent = RelayConsent(str(here_dir / "rc.json"))
    b.relay_consent.grant(DM, "claude", CID, session_ident("修登录", "/Users/x/codes/demo"))
    assert b._relay_granted(DM, "claude", CID)
    claude.sessions = [_closed()]
    b.fleet.overview()
    assert not b._relay_granted(DM, "claude", CID), "关掉之后老记录还算数"


# ---------- 调用点还在（查调用形状，不查名字） ----------


def _body(name: str) -> str:
    src = APP_PY.read_text(encoding="utf-8")
    head = re.search(rf"\n    (?:async )?def {re.escape(name)}\(", src)
    assert head is not None, f"app.py 里没有 {name}"
    start = head.start() + 1
    nxt = re.search(r"\n    (?:async )?def ", src[start + 10:])
    return src[start: start + 10 + (nxt.start() if nxt else len(src))]


def test_both_consent_paths_remember_what_was_confirmed():
    """两个写确认的地方（点卡 / 打 /talk!）都要把「确认时它是什么样」记下来。"""
    for name in ("_on_relay_click", "_confirm_talk_by_text"):
        body = _body(name)
        assert "self._remember_talk_ident(scope, tool, sid, ident)" in body, f"{name} 没记下确认时的样子"
        assert "ident = self._consented_ident(scope, tool, sid)" in body, f"{name} 没按卡上那个样子记确认"
        assert "store.grant(scope, tool, sid, ident)" in body, f"{name} 记确认用的不是卡上那个样子"


def test_group_arm_rechecks_identity_before_the_shortcut():
    body = _body("_group_bind_arm")
    assert "self._talk_ident_same(scope, bound.tool, bound.id)" in body
    assert "self._talk_changed_why(scope, bound.tool, bound.id)" in body
    assert "self._ask_relay_consent(chat_id, scope, bound.tool, bound.id)" in body


def test_consent_card_branches_on_the_route():
    body = _body("_ask_relay_consent")
    assert "self._build_background_consent_card(sess, in_group)" in body


# ---------- 卡片看的时候和点的时候不一样 ----------


def test_confirm_records_what_the_card_showed_not_what_it_is_at_click_time(here_dir):
    """卡是在会话关着时弹的（写的是手机这边的权限），点之前终端里又打开了 ⇒
    这次确认只算「关着的那个」，下一句要按电脑那套权限再问一次。"""
    b, claude, gid = _group(here_dir, [_open()])
    claude.sessions = [_closed()]
    _say(b, gid, "一")                       # 弹「后台 + 手机这边」那张卡
    claude.sessions = [_open()]
    b.fleet.overview()                       # 点之前缓存已经是「开着」了
    _confirm_in_group(b, gid)
    _say(b, gid, "二")
    assert _forwarded(claude) == [], "他确认的是手机这边的权限，却按电脑那套权限转进去了"
    assert "不问你就改" in b.replier.cards_to(gid)[-1]
