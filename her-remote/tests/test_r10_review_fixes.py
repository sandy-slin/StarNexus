"""第 10 轮：修第 9 轮复审（`docs/iteration/review-r9.md`）找到的 4 个 🔴。

每一组开头那条就是复审报告里的复现代码，原样变成断言（在第 9 轮的代码上红）。

1. 说了取消又被发出去：取消词原来只认精确的「取消」两个字（「取消。」「退出」「退出对话」「算了」都漏）
2. 卡上只写了一句，点确认却发两句：两张确认卡按钮值一模一样，点旧卡发出了新卡才引用的那句
3. 单聊里确认后会话变样照样发：身份复查原来只接在群那条路上
4. 审批卡发出后重启，/stop 不管用、旧卡照样能批：/stop 原来只在内存 `_awaiting` 有记录时才清待批

夹具放测试目录旁边（`here_dir`），不放 tmp_path（macOS 上是 pathguard 的豁免前缀）。
一个群都不真建、一个飞书接口都不调。
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import shutil
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from bridge.review import ApprovalStore  # noqa: E402
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
    _sess,
)
from tests.test_token_lifecycle import (  # noqa: E402
    PUBLISH,
    click as card_click,
    make_bridge,
    oks_in,
    send_card,
    write_hook,
)

APP_PY = pathlib.Path(_HERE).parent / "bridge" / "app.py"


@pytest.fixture
def here_dir():
    base = pathlib.Path(_HERE).parent / f".r10-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _group(here_dir, title="修登录"):
    claude = FakeClaude([_sess(title=title)])
    groups = FakeGroups()
    b = _bridge(here_dir, claude, groups)
    b.fleet.overview()
    _open_group(b)
    gid = groups.created[0][0]
    return b, claude, gid, f"group:{gid}"


def _relay_values(card_json: str, op: str) -> list:
    """一张卡上某个 op 的按钮回调值（真飞书点下去回给我们的就是它）。"""
    found = []

    def walk(node):
        if isinstance(node, dict):
            value = node.get("value")
            if isinstance(value, dict) and value.get("act") == "relay" and value.get("op") == op:
                found.append(dict(value))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(json.loads(card_json))
    return found


def _consent_cards(b, chat_id) -> list:
    return [c for c in b.replier.cards_to(chat_id) if _relay_values(c, "talk")]


# ======================================================================
# 1. 取消词
# ======================================================================


@pytest.mark.parametrize("word", ["取消。", "退出", "退出对话", "算了", "停止转发",
                                  " 取消 ", "取消！", "取消!", "退出对话。", "算了。"])
def test_cancel_words_in_a_confirmed_group_unbind_and_are_not_relayed(here_dir, word):
    """复审 S5 原样：已确认的群里打这些 ⇒ 原来被转进会话、绑定照开。"""
    b, claude, gid, sc = _group(here_dir)
    _confirm_in_group(b, gid)
    _run(b._handle(sc, gid, word))
    assert claude.relayed == [], f"打「{word}」被转进了会话"
    assert b.chat_binds.active_of_scope(sc) is None, f"打「{word}」之后绑定还开着"


@pytest.mark.parametrize("word", ["取消", "/cancel"])
def test_the_old_cancel_words_still_work_positive_control(here_dir, word):
    b, claude, gid, sc = _group(here_dir)
    _confirm_in_group(b, gid)
    _run(b._handle(sc, gid, word))
    assert claude.relayed == [] and b.chat_binds.active_of_scope(sc) is None


@pytest.mark.parametrize("text", ["取消订单那个 bug 修一下", "退出登录的逻辑有问题", "算了算了先看日志",
                                  "停止转发的开关在哪"])
def test_sentences_that_merely_contain_a_cancel_word_are_relayed(here_dir, text):
    """阳性对照：只认整句。「取消订单那个 bug」是正常的一句话，不许被吃掉。"""
    b, claude, gid, sc = _group(here_dir)
    _confirm_in_group(b, gid)
    _run(b._handle(sc, gid, text))
    assert [t for _, t in claude.relayed] == [text]
    assert b.chat_binds.active_of_scope(sc) is not None


def test_cancel_word_before_confirm_is_not_held_and_drops_what_was(here_dir):
    """复审 S4 原样：先说一句再打「算了」⇒ 原来两句都被记着、确认后一起发出去。"""
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "把 main 分支强推一下"))
    _run(b._handle(sc, gid, "算了"))
    held = [t for _, t in getattr(b, "_held", {}).get(sc, (None, None, []))[2]]
    assert "算了" not in held, f"取消词被记进了 _held：{held}"
    assert held == [], f"说了「算了」，之前记着的话没作废：{held}"
    said = "\n".join(b.replier.to(gid)[-1:])
    assert "没发过去" in said, f"没照实说那几句没发：{said!r}"
    # 之后重新接上再确认：旧话不许冒出去
    _open_group(b)
    _confirm_in_group(b, gid)
    assert claude.relayed == [], f"取消过的话被发了出去：{claude.relayed}"


def test_cancel_word_in_dm_talk_mode_exits(here_dir):
    """单聊对话模式里打「退出对话」（按钮上的字）⇒ 退出，不转发。"""
    claude = FakeClaude([_sess()])
    b = _bridge(here_dir, claude, None)
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    _run(b._handle(DM, DM_CHAT, "退出对话。"))
    assert claude.relayed == []
    assert b.fleet.peek_pending(DM) is None, "打了「退出对话」还在对话模式里"


@pytest.mark.parametrize("text", ["算了", "退出", "取消订单那个 bug 修一下"])
def test_dm_with_nothing_to_cancel_treats_these_as_normal_text(here_dir, text):
    """阳性对照：单聊里没有对话模式、没有待确认的卡 ⇒「算了」「退出」就是一句普通的话，交给桥自己的 claude。"""
    claude = FakeClaude([_sess()])
    b = _bridge(here_dir, claude, None)
    _run(b._handle(DM, DM_CHAT, text))
    assert b.handled == [(DM, text)], f"「{text}」被当成命令吃掉了：{b.replier.to(DM_CHAT)}"


def test_dm_plain_cancel_with_nothing_still_answers_positive_control(here_dir):
    claude = FakeClaude([_sess()])
    b = _bridge(here_dir, claude, None)
    _run(b._handle(DM, DM_CHAT, "取消"))
    assert b.handled == [] and "没有要取消的" in b.replier.to(DM_CHAT)[-1]


def test_cancel_word_normalizer_unit():
    from bridge.app import _is_cancel_word

    for yes in ("取消", "取消。", " 取消 ", "取消！", "退出", "退出对话", "🚪 退出对话", "算了", "算了。",
                "停止转发", "取消~", "「取消」", "/cancel", "/cancel。", "　取消　"):
        assert _is_cancel_word(yes), yes
    for no in ("取消订单那个 bug", "退出登录", "算了算了先看日志", "", "  ", "取 消", "不要取消",
               "/cancel 一下", "取消订单"):
        assert not _is_cancel_word(no), no


# ======================================================================
# 2. 两张确认卡：点哪张发哪张写的
# ======================================================================


def test_tapping_the_first_card_does_not_send_what_only_the_second_card_shows(here_dir):
    """复审 #2 原样：两句、两张卡，点第一张 ⇒ 原来两句一起发出去。"""
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "先看看日志"))
    _run(b._handle(sc, gid, "删掉 prod 的旧表"))
    cards = _consent_cards(b, gid)
    first = cards[-2]
    assert "先看看日志" in first and "删掉 prod 的旧表" not in first, "前提：第一张卡只引用第一句"
    _click(b, _relay_values(first, "talk")[0], chat_id=gid)
    sent = "\n".join(t for _, t in claude.relayed)
    assert "删掉 prod 的旧表" not in sent, f"点第一张卡发出了它没写的那句：{claude.relayed}"


def test_tapping_the_first_card_after_cancel_like_text(here_dir):
    """复审 #2 第二行：「把 main 强推一下」+「取消。」⇒ 原来点第一张卡发出两句。"""
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "把 main 强推一下"))
    _run(b._handle(sc, gid, "取消。"))
    for card in _consent_cards(b, gid):
        for value in _relay_values(card, "talk"):
            _click(b, value, chat_id=gid)
    assert claude.relayed == [], f"说了「取消。」之后点卡，话被发了出去：{claude.relayed}"


def test_stale_card_says_so_and_offers_the_newest(here_dir):
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "先看看日志"))
    _run(b._handle(sc, gid, "删掉 prod 的旧表"))
    first = _consent_cards(b, gid)[-2]
    before = len(_consent_cards(b, gid))
    resp = _click(b, _relay_values(first, "talk")[0], chat_id=gid)
    toast = str(getattr(getattr(resp, "toast", None), "content", ""))
    assert "过时" in toast, toast
    assert claude.relayed == []
    # 再弹一张最新的（落在聊天最底下，不用往回翻），上面列着全部要发的话
    newest = _consent_cards(b, gid)
    assert len(newest) == before + 1, "点了过时的卡，没给一张能点的新卡"
    assert "先看看日志" in newest[-1] and "删掉 prod 的旧表" in newest[-1]
    # 点最新那张 ⇒ 两句都发（卡上写着两句）
    _click(b, _relay_values(newest[-1], "talk")[0], chat_id=gid)
    assert [t for _, t in claude.relayed] == ["先看看日志\n删掉 prod 的旧表"]


def test_the_newest_card_shows_every_sentence_it_will_send(here_dir):
    """底线：主人点确认时屏幕上看到的，就是会发出去的全部。"""
    b, claude, gid, sc = _group(here_dir)
    words = ["一号句子", "二号句子", "三号句子"]
    for w in words:
        _run(b._handle(sc, gid, w))
    newest = _consent_cards(b, gid)[-1]
    for w in words:
        assert w in newest, f"最新那张卡上没有「{w}」，点了却会发出去"
    _click(b, _relay_values(newest, "talk")[0], chat_id=gid)
    assert [t for _, t in claude.relayed] == ["\n".join(words)]


def test_a_long_held_sentence_is_shown_in_full_on_the_card(here_dir):
    b, claude, gid, sc = _group(here_dir)
    long = "把这几个文件里的旧接口全部换成新接口，" * 5 + "最后删掉 prod 的旧表"
    _run(b._handle(sc, gid, long))
    card = _consent_cards(b, gid)[-1]
    assert "最后删掉 prod 的旧表" in card, "长句子在卡上被截掉了尾巴，点确认却整句发出去"


def test_no_on_a_stale_card_still_cancels(here_dir):
    """往「更少发生」走永远安全：旧卡上的「算了」照样作废、解绑。"""
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "先看看日志"))
    _run(b._handle(sc, gid, "删掉 prod 的旧表"))
    first = _consent_cards(b, gid)[-2]
    _click(b, _relay_values(first, "no")[0], chat_id=gid)
    assert b.chat_binds.active_of_scope(sc) is None
    assert getattr(b, "_held", {}).get(sc) is None
    assert claude.relayed == []


def test_single_card_confirm_positive_control(here_dir):
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "把登录改掉"))
    card = _consent_cards(b, gid)[-1]
    _click(b, _relay_values(card, "talk")[0], chat_id=gid)
    assert [t for _, t in claude.relayed] == ["把登录改掉"]


def test_card_value_without_nonce_is_refused_when_several_cards_are_out(here_dir):
    """拿不出是哪张卡的点击（没带序号）+ 外面有好几张 ⇒ 不猜，按过时处理。"""
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "先看看日志"))
    _run(b._handle(sc, gid, "删掉 prod 的旧表"))
    _confirm_in_group(b, gid)  # 不带序号
    assert claude.relayed == []


def test_card_nonce_is_stripped_before_the_whitelist(here_dir):
    """序号只在 app.py 里加、在白名单之前摘掉：别的多余键照旧被拒。"""
    b, claude, gid, sc = _group(here_dir)
    _run(b._handle(sc, gid, "把登录改掉"))
    value = _relay_values(_consent_cards(b, gid)[-1], "talk")[0]
    value["evil"] = "1"
    _click(b, value, chat_id=gid)
    assert claude.relayed == []


# ======================================================================
# 3. 单聊里确认后会话变样
# ======================================================================


def _dm_confirmed(here_dir, **kw):
    claude = FakeClaude([_sess(**kw)])
    b = _bridge(here_dir, claude, None)
    b.group_client = None
    b.fleet.overview()
    _click(b, {"act": "fleet", "op": "send", "tool": "claude", "id": CID})
    _click(b, {"act": "relay", "op": "talk", "tool": "claude", "id": CID})
    return b, claude


def test_dm_closed_then_opened_reasks(here_dir):
    """复审 #1 原样：关着时确认 → 电脑上打开后说一句 ⇒ 原来直接进终端会话。"""
    b, claude = _dm_confirmed(here_dir, owner="none", pids=(), status="idle")
    card = _consent_cards(b, DM_CHAT)[-1]
    assert "关了" in card, "前提：确认卡写的是「电脑上那个会话关了」"
    cards_before = len(_consent_cards(b, DM_CHAT))
    claude.sessions = [_sess(owner="terminal")]
    b.fleet.overview()
    _run(b._handle(DM, DM_CHAT, "把旧表删了"))
    assert claude.relayed == [] and claude.sent == [], f"变样之后照样发了：{claude.relayed} {claude.sent}"
    assert len(_consent_cards(b, DM_CHAT)) == cards_before + 1, "没重新弹确认卡"
    said = "\n".join(b.replier.to(DM_CHAT)[-2:])
    assert "又打开了" in said and "没发" in said, said
    assert b.handled == [], "这句落进了桥自己的 claude"


def test_dm_renamed_and_moved_reasks(here_dir):
    b, claude = _dm_confirmed(here_dir)
    claude.sessions = [_sess(title="删库跑路", cwd="/Users/x/codes/prod")]
    b.fleet.overview()
    _run(b._handle(DM, DM_CHAT, "删掉旧表"))
    assert claude.relayed == [] and claude.sent == []
    assert "改了名" in "\n".join(b.replier.to(DM_CHAT)[-2:])


def test_dm_open_then_closed_reasks(here_dir):
    b, claude = _dm_confirmed(here_dir)
    claude.sessions = [_sess(owner="none", pids=(), status="idle")]
    b.fleet.overview()
    _run(b._handle(DM, DM_CHAT, "删掉旧表"))
    assert claude.relayed == [] and claude.sent == []
    assert "关了" in "\n".join(b.replier.to(DM_CHAT)[-2:])


def test_dm_change_seen_even_before_the_next_background_scan(here_dir):
    """后台扫描 10 秒一拍：变了还没扫到也要认出来（发之前现查一次）。"""
    b, claude = _dm_confirmed(here_dir, owner="none", pids=(), status="idle")
    claude.sessions = [_sess(owner="terminal")]
    _run(b._handle(DM, DM_CHAT, "把旧表删了"))   # 不调 overview
    assert claude.relayed == [] and claude.sent == []


def test_dm_status_pid_changes_do_not_reask_positive_control(here_dir):
    b, claude = _dm_confirmed(here_dir)
    cards_before = len(_consent_cards(b, DM_CHAT))
    claude.sessions = [_sess(status="waiting", pids=(9999,))]
    b.fleet.overview()
    _run(b._handle(DM, DM_CHAT, "看一下日志"))
    assert [t for _, t in claude.relayed] == ["看一下日志"]
    assert len(_consent_cards(b, DM_CHAT)) == cards_before


def test_dm_reconfirm_after_change_then_sends(here_dir):
    """重新确认之后照常能聊（阳性对照：不是把路堵死）。"""
    b, claude = _dm_confirmed(here_dir, owner="none", pids=(), status="idle")
    claude.sessions = [_sess(owner="terminal")]
    b.fleet.overview()
    _run(b._handle(DM, DM_CHAT, "把旧表删了"))
    newest = _consent_cards(b, DM_CHAT)[-1]
    _click(b, _relay_values(newest, "talk")[0])
    _run(b._handle(DM, DM_CHAT, "看一下日志"))
    assert [t for _, t in claude.relayed] == ["看一下日志"], "单聊里不记确认前的话：只发确认之后说的"


# ======================================================================
# 4. 审批卡发出后重启，/stop
# ======================================================================


def _approval_bridge(home):
    b = make_bridge(str(home))
    # 和 Bridge.__init__ 一致：用过的 nonce 落盘
    b.approvals = ApprovalStore(b.approvals._secret, used_path=os.path.join(str(home), "approvals-used.txt"))
    return b


SC = "dm:ou_owner"


def test_stop_after_restart_voids_the_old_card(here_dir):
    """复审 #4 原样：发卡 → 重启 → /stop → 点同意 ⇒ 原来 .ok 写下、待续跑。"""
    hook = write_hook(str(here_dir))
    b = _approval_bridge(here_dir)
    value = send_card(b, SC, PUBLISH, hook)
    b2 = _approval_bridge(here_dir)                          # 重启
    asyncio.run(b2._handle_command(SC, "oc_1", "/stop"))
    assert "卡片按钮已失效" in b2.said[-1], f"/stop 回：{b2.said[-1]!r}"
    kind, content = card_click(b2, value)
    assert oks_in(b2.gate.dir_for(SC)) == [], f"说了停，旧卡照样批了：{kind} {content}"
    assert SC not in b2._awaiting, "说了停，还挂着待续跑"


def test_stop_in_the_same_process_positive_control(here_dir):
    hook = write_hook(str(here_dir))
    b = _approval_bridge(here_dir)
    value = send_card(b, SC, PUBLISH, hook)
    asyncio.run(b._handle_command(SC, "oc_1", "/stop"))
    assert "卡片按钮已失效" in b.said[-1]
    card_click(b, value)
    assert oks_in(b.gate.dir_for(SC)) == []


def test_stop_with_nothing_pending_after_restart_says_nothing_running(here_dir):
    b = _approval_bridge(here_dir)
    asyncio.run(b._handle_command(SC, "oc_1", "/stop"))
    assert "现在没有在跑的任务" in b.said[-1]


def test_card_without_stop_after_restart_still_approves_positive_control(here_dir):
    """阳性对照：没说停 ⇒ 重启后点旧卡照样能批（第 2 轮修的那条不能被改回去）。"""
    hook = write_hook(str(here_dir))
    b = _approval_bridge(here_dir)
    value = send_card(b, SC, PUBLISH, hook)
    b2 = _approval_bridge(here_dir)
    card_click(b2, value)
    assert len(oks_in(b2.gate.dir_for(SC))) == 1


def test_paused_gate_survives_restart_and_stop(here_dir):
    """顺带核第 2 轮的掐停闸：它读的是落盘的 paused.json，重启后还在（不是同一个形状）。"""
    hook = write_hook(str(here_dir))
    b = _approval_bridge(here_dir)
    value = send_card(b, SC, PUBLISH, hook)
    b._paused[SC] = "连续试探被拦"
    b._save_paused()
    b2 = _approval_bridge(here_dir)
    assert SC in b2._paused
    card_click(b2, value)
    assert oks_in(b2.gate.dir_for(SC)) == [], "掐停期间重启后点旧卡写下了 .ok"


# ======================================================================
# 调用点还在（查调用形状，不查名字）
# ======================================================================


def _func(name):
    tree = ast.parse(APP_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"app.py 里没有 {name}")


def _calls(name):
    return [ast.unparse(n) for n in ast.walk(_func(name)) if isinstance(n, ast.Call)]


def test_cancel_check_is_wired_into_both_gates():
    assert "_is_cancel_word(text)" in _calls("_handle_command")
    assert "_is_cancel_word(text)" in _calls("_is_fleet_word")


def test_dm_talk_path_uses_the_same_identity_judgment():
    calls = _calls("_maybe_send_to_fleet")
    assert "self._talk_ident_same(scope, target.tool, target.id)" in calls
    assert "self._relay_granted(scope, target.tool, target.id)" in calls
    assert "self._talk_changed_why(scope, target.tool, target.id)" in calls
    assert "self._ask_relay_consent(chat_id, scope, target.tool, target.id)" in calls


def test_consent_card_is_stamped_and_the_click_checks_the_stamp():
    assert "self._stamp_consent_card(scope, card)" in _calls("_ask_relay_consent")
    assert "self._consent_card_current(scope, stamp)" in _calls("_on_relay_click")


def test_stop_clears_the_gate_unconditionally():
    """`/stop` 那一支里 `gate.clear(scope)` 不能再套在 `if had_pending` 里。"""
    func = _func("_handle_command")
    stop = None
    for node in ast.walk(func):
        if isinstance(node, ast.If) and "'/stop'" in ast.unparse(node.test):
            stop = node
            break
    assert stop is not None
    top_level = [ast.unparse(s) for s in stop.body]
    assert any("self._clear_approvals_for_stop(scope)" in s for s in top_level), top_level
    assert "gate.clear(scope)" in _calls("_clear_approvals_for_stop")
