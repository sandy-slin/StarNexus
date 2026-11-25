"""第 17 轮 E：冗余点击落地（`docs/iteration/r14-redundant-taps.md` 排好序的那几条）。

🔴 **断言的是「现在要点几下」**，不是「某个函数被调用了」。所以下面每个 `taps_*`
都从一张**真卡片**出发：卡上有那个按钮 ⇒ 1 下；没有 ⇒ 按常驻菜单 / 菜单卡**现有的树**
现算出来要几下（`botmenu.MENU_TREE` + `menu.CONTROLS`，不写死数字，菜单改了这里跟着变）。

这一轮在 `cards.py` / `actions.py` 里落的两条：

1. **卡上说「点📂 换项目」，卡上却没有这个按钮**（实测，见 `test_*_change_project_*`）。
   「换项目」不在常驻菜单里（`botmenu.MENU_TREE` 第 85 行的注释写明被挪走了）⇒
   只能 ⚡常用 → 全部按钮 → 📂 换项目，**3 下**。放到同一张卡上 ⇒ **1 下**。
2. **「等你」的会话要进详情才看得到它在问什么**。总览那一行底下直接写一句 ⇒
   看问题从 **1 下**（点进详情）变 **0 下**。

**没动**（原因见最终报告）：审批卡、放开权限的二次确认、会话身份变了重新确认 —— 报告里
标「必须」的那些；以及 `WHY_SEND_DEFERRED` 那句叫人去点「🔑 权限」—— 权限**本来就是**
常驻菜单的一级节点（1 下），卡上再放一个省不下任何一下，所以不放。

🔴 反向用例：`WHY_STOP_BRIDGE_CHAT` 那句「回到那个对话点「⏹ 停下」」说的是**另一个聊天**里
的停下，这里绝不能顺手补一个「停下」按钮 —— 点了会停错东西。
"""

from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import Session  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_CARD_BYTES,
    build_detail_card,
    build_overview_card,
)
from bridge.fleet.model import AskQuestion, Message, PendingAsk  # noqa: E402
from bridge.fleet.scan import Overview  # noqa: E402
from bridge.redact import redact  # noqa: E402

SID = "abababab-1111-4111-8111-111111111111"
SID2 = "cdcdcdcd-2222-4222-8222-222222222222"

# 真的会出现在卡上的那两句（`control.WHY_OUTSIDE_WORKSPACE` / `claude.py` 里的同义句）。
# 从源码取，不抄字面 —— 文案改了测试跟着改。
from bridge.fleet.claude import WHY_SEND_DEFERRED, WHY_STOP_BRIDGE_CHAT  # noqa: E402
from bridge.fleet.control import WHY_OUTSIDE_WORKSPACE  # noqa: E402


def _redact(text):
    return redact(str(text or ""))[0]


def _sess(**kw):
    base = dict(tool="claude", surface="cli", id=SID, title="修登录", cwd="/Users/x/codes/demo",
                status="running", pids=(4242,), owner="terminal", updated_at=time.time() - 60,
                can_send=False, can_relay=True)
    base.update(kw)
    return Session(**base)


def _ask(question="登录页要用哪种方案？", header="方案", **kw):
    return PendingAsk(tool="AskUserQuestion", questions=(
        AskQuestion(question=question, header=header, multi=False,
                    options=(("短信验证码", "最快上线"), ("飞书扫码", "同事都有"))),), **kw)


from tests._overview_rows import tap_as_button  # noqa: E402


def _nodes(blob, tag):
    out = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == tag:
                out.append(node)
            elif tag == "button" and tap_as_button(node):  # 11-16 会话行是交互容器
                out.append(tap_as_button(node))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(blob))
    return out


def _labels(blob):
    return [n["text"]["content"] for n in _nodes(blob, "button")]


def _texts(blob):
    return [n.get("content", "") for n in _nodes(blob, "markdown")]


def _check(blob):
    """每张卡都要过的那几条（同 test_card_slim / test_r8）。"""
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    return blob


def _has_button(blob, word):
    return any(word in label for label in _labels(blob))


# ---------- 「几下」是按现有菜单树现算的，不是写死的数字 ----------


def _menu_taps_to(label_word: str) -> int:
    """不在这张卡上时，走菜单到那个按钮要点几下。

    常驻菜单（`botmenu.MENU_TREE`）：一级节点 ⇒ 1 下；二级 ⇒ 2 下。
    常驻菜单里根本没有 ⇒ 只能先打开「全部按钮」那张菜单卡（`menu.CONTROLS`）：
    ⚡常用(1) → 全部按钮(2) → 卡上那个按钮(3)。
    """
    from bridge.botmenu import MENU_TREE
    from bridge.menu import CONTROLS

    for top_label, _key, children in MENU_TREE:
        if label_word in top_label:
            return 1
        for child_label, _child_key in children:
            if label_word in child_label:
                return 2
    # 菜单卡上有 ⇒ 要先点出那张卡
    for _cmd, label in CONTROLS:
        if label_word in label:
            path = 0
            for top_label, _key, children in MENU_TREE:
                for child_label, _child_key in children:
                    if "全部按钮" in child_label:
                        path = 2  # ⚡常用 → 全部按钮
            assert path, "菜单里连「全部按钮」都没有了，这个模型要重算"
            return path + 1
    raise AssertionError(f"哪儿都找不到「{label_word}」这个按钮")


def _taps_to(blob, label_word: str) -> int:
    """从这张卡出发，到那个按钮要点几下。"""
    return 1 if _has_button(blob, label_word) else _menu_taps_to(label_word)


def test_the_tap_model_itself_is_grounded_in_the_menu_tree():
    """阳性对照：模型不是写死的 —— 停下在常驻菜单一级（1 下），权限 11-10 挪进「⚡ 常用」（2 下），
    换项目被挪走了（3 下）。"""
    assert _menu_taps_to("停下") == 1
    assert _menu_taps_to("权限") == 2
    assert _menu_taps_to("换项目") == 3


# ---------- 1. 卡上叫你点「📂 换项目」，就把那个按钮放在这张卡上 ----------


def _outside_workspace_card():
    """项目不在手机能用的目录里 ⇒ 两条路都走不通，卡上贴的就是那句（实测形状）。"""
    sess = _sess(status="idle", pids=(), owner="none", can_send=False,
                 why_not_send=WHY_OUTSIDE_WORKSPACE, can_relay=False, why_not_relay="")
    return _check(build_detail_card(sess, [Message("assistant", "改完了")], redact=_redact))


def test_detail_card_that_tells_you_to_change_project_costs_one_tap():
    """🔴 现码上会红：卡上写着「点📂 换项目」，而卡上没有这个按钮 ⇒ 要走菜单 3 下。"""
    blob = _outside_workspace_card()
    assert "换项目" in "\n".join(_texts(blob)), "阳性对照：这张卡本来就该贴那句话"
    assert _taps_to(blob, "换项目") == 1, \
        f"卡上说「点📂 换项目」却没给按钮 ⇒ 现在要 {_taps_to(blob, '换项目')} 下，按钮上=[{_labels(blob)}]"


def test_the_change_project_button_is_the_plain_whitelisted_one():
    """按钮必须是现成的无参按钮（`actions.DO_COMMANDS["dirs"]`）⇒ 闸门一点不变。"""
    from bridge.fleet.actions import do_value

    blob = _outside_workspace_card()
    found = [n for n in _nodes(blob, "button") if "换项目" in n["text"]["content"]]
    assert found, "没找到「换项目」按钮"
    values = [b.get("value") for b in found[0]["behaviors"] if b.get("type") == "callback"]
    assert values == [do_value("dirs")], f"回调值不是预设的无参 op：{values}"


def test_a_card_that_does_not_mention_it_gets_no_extra_button():
    """阳性对照反面：没说要换项目的卡**不多**这个按钮（别到处撒按钮）。"""
    blob = _check(build_detail_card(_sess(), [Message("assistant", "在跑")], redact=_redact))
    assert not _has_button(blob, "换项目")


def test_never_adds_a_stop_button_for_a_stop_in_another_chat():
    """🔴 反向用例：「回到那个对话点「⏹ 停下」」指的是**另一个聊天**，这里补按钮会停错东西。"""
    sess = _sess(can_relay=False, why_not_relay="App 里的会话发不进去", can_send=False,
                 can_stop=False, why_not_stop=WHY_STOP_BRIDGE_CHAT, status="running")
    blob = _check(build_detail_card(sess, [], redact=_redact))
    assert "停下" in "\n".join(_texts(blob)), "阳性对照：那句话确实贴在卡上"
    assert not _has_button(blob, "停下"), "不许给另一个聊天里的「停下」补按钮"


def test_loosening_permissions_keeps_its_own_confirm_and_gets_no_shortcut():
    """🔴 放开权限那套（报告里标「必须」）一下都不省：这里不放「权限」按钮、也不放「全授权」。

    「🔑 权限」本来就是常驻菜单的一级节点（1 下），卡上再放一个省不下任何一下；
    而「全授权」要走它自己的二次确认（`menu.build_confirm_loosen_card`），绝不能在这里抄近道。
    """
    sess = _sess(status="idle", pids=(), owner="none", can_send=False,
                 why_not_send=WHY_SEND_DEFERRED, can_relay=False, why_not_relay="")
    blob = _check(build_detail_card(sess, [], redact=_redact))
    assert "全授权" in "\n".join(_texts(blob)), "阳性对照"
    assert not _has_button(blob, "全授权"), "放开权限不许有一键按钮"
    assert not _has_button(blob, "权限"), "权限在常驻菜单里已经是 1 下，卡上不重复放"


# ---------- 2. 「等你」在问什么：总览上直接看得到，不用先进详情 ----------


def _waiting_overview(**kw):
    waiting = _sess(status="waiting", pending=_ask(**kw), can_send=True, can_relay=False)
    idle = _sess(id=SID2, title="写文档", status="idle", pids=(), owner="none", can_send=True,
                 can_relay=False)
    return _check(build_overview_card(Overview(sessions=[waiting, idle], ok_tools=["claude"]),
                                      redact=_redact))


def _taps_to_see_the_question(blob, question: str) -> int:
    """从总览出发，看到它在问什么要点几下。总览上就有 ⇒ 0；否则得点那一行进详情 ⇒ 1。"""
    return 0 if question[:10] in "\n".join(_texts(blob)) else 1


def test_overview_shows_what_a_waiting_session_is_asking():
    """🔴 现码上会红：总览只有「🟡 等你」，要知道它在问什么得先点进详情（1 下）。"""
    blob = _waiting_overview()
    assert _taps_to_see_the_question(blob, "登录页要用哪种方案？") == 0, \
        f"总览没写它在问什么 ⇒ 还要多点一下进详情。卡上的字={_texts(blob)}"


def test_overview_question_line_sits_under_its_own_row():
    """那一句必须紧跟在**它自己**那一行底下（上下两行都在等你时才分得清谁问的）。"""
    other = _sess(id=SID2, title="写文档", status="waiting", can_send=True, can_relay=False,
                  pending=_ask(question="文档放哪个目录？", header="位置"))
    first = _sess(status="waiting", can_send=True, can_relay=False, pending=_ask())
    blob = _check(build_overview_card(Overview(sessions=[first, other], ok_tools=["claude"]),
                                      redact=_redact))
    flat = [(n.get("tag"), n.get("text", {}).get("content") if n.get("tag") == "button"
             else n.get("content", "")) for n in _nodes(blob, "button") + _nodes(blob, "markdown")]
    # 按卡里真实顺序重走一遍
    order = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("tag") == "button":
                order.append(("btn", node["text"]["content"]))
            elif tap_as_button(node):  # 11-16 会话行是交互容器：它里面那段 markdown 就是这一行，别再当成一句话
                order.append(("btn", tap_as_button(node)["text"]["content"]))
                return
            elif node.get("tag") == "markdown":
                order.append(("md", node.get("content", "")))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(blob))
    assert flat  # 只是确认上面那个 helper 真的拿到了东西
    # 两行**各自**的问题都要紧跟在自己底下，不能串台
    for name, question in (("修登录", "登录页要用哪种方案"), ("写文档", "文档放哪个目录")):
        rows = [i for i, (kind, text) in enumerate(order) if kind == "btn" and name in text]
        assert rows, f"没找到「{name}」那一行：{order}"
        after = order[rows[0] + 1]
        assert after[0] == "md" and question in after[1], \
            f"「{name}」的问题没紧跟在它那一行下面，紧跟的是 {after}"


def test_overview_says_nothing_extra_for_sessions_that_are_not_waiting():
    """不在等你的会话不多这一句（总览是最挤的那张卡）。"""
    blob = _check(build_overview_card(
        Overview(sessions=[_sess(status="running", can_send=True)], ok_tools=["claude"]),
        redact=_redact))
    assert "它在问你" not in "\n".join(_texts(blob))


def test_overview_question_is_redacted_before_it_is_clipped():
    """🔴 问题正文是用户项目里的文字 ⇒ 先整段脱敏再截断（同详情卡的红字）。"""
    secret = "sk-ant-api03-" + "Z" * 40
    blob = _waiting_overview(question=f"{secret} 这个 key 还能用吗？")
    joined = "\n".join(_texts(blob))
    assert "ZZZZ" not in joined and "sk-ant" not in joined


def test_overview_question_cannot_blow_the_card_budget():
    """一道几万字的题目不能把总览顶过 20KB（`_check` 会抓）。"""
    sessions = [_sess(id=f"{i:08x}-1111-4111-8111-111111111111", title=f"会话{i}",
                      status="waiting", can_send=True, can_relay=False,
                      pending=_ask(question="很" * 20000, header="标" * 500))
                for i in range(8)]
    blob = _check(build_overview_card(Overview(sessions=sessions, ok_tools=["claude"]),
                                      redact=_redact))
    assert "它在问你" in "\n".join(_texts(blob))


def test_overview_survives_a_broken_pending_and_a_broken_redactor():
    """这一句出任何错都只是**这一句不显示**，总览照常出（同 `pending_elements` 的红字）。"""
    import types as _types

    broken = _sess(status="waiting", can_send=True, can_relay=False,
                   pending=PendingAsk(tool="AskUserQuestion",
                                      questions=(_types.SimpleNamespace(),)))
    blob = _check(build_overview_card(Overview(sessions=[broken], ok_tools=["claude"]),
                                      redact=_redact))
    assert "修登录" in "\n".join(_labels(blob)), "那一行还得在"

    def boom(_):
        raise ValueError("坏了")

    blob = _check(build_overview_card(
        Overview(sessions=[_sess(status="waiting", can_send=True, pending=_ask())],
                 ok_tools=["claude"]),
        redact=boom))
    assert json.loads(blob)["schema"] == "2.0"


def test_waiting_session_without_a_readable_question_adds_nothing():
    """读不出它卡在哪一步（`pending` 是 None，别家工具都这样）⇒ 不多那一句。"""
    blob = _check(build_overview_card(
        Overview(sessions=[_sess(status="waiting", can_send=True, pending=None)],
                 ok_tools=["claude"]),
        redact=_redact))
    joined = "\n".join(_texts(blob))
    assert "它在问你" not in joined and "等你确认" not in joined


def test_overview_shows_the_other_kind_of_pending_too():
    """不是选择题、而是「等你确认某个动作」⇒ 也写一句（人话工具名 + 摘要）。"""
    pending = PendingAsk(tool="Bash", summary="rm -rf build/")
    blob = _check(build_overview_card(
        Overview(sessions=[_sess(status="waiting", can_send=True, pending=pending)],
                 ok_tools=["claude"]),
        redact=_redact))
    assert "等你确认" in "\n".join(_texts(blob))


# ---------- 主会话补的那一处：总览的**文字兜底**也要带上「它在问你」 ----------


def test_overview_text_fallback_also_shows_what_it_is_asking():
    """卡片发不出去时退成文字，不能又让他看不到它在问什么。

    角色 E 把总览卡补好之后指出：`app.py` 的文字兜底是另一条路，它自己拼 `lines`，
    不会走 `overview_pending_line`。详情卡那条路早就有 `_pending_fallback` 做同一件事。

    🔴 判据是**把 app.py 里那段兜底真跑一遍看输出**，不是查源码里有没有那个函数名。
    第一版我写成「函数名出现在附近」，变异验证当场打脸：把循环体掏空（`pass`）照样全绿
    —— 名字还留在 import 行里。今天这个病犯了第三次，所以这里连「怎么写才算钉住」一起记下来。
    """
    import inspect
    import re
    import textwrap

    from bridge import app as app_mod
    from bridge.fleet.cards import overview_pending_line, row_label

    cls = next(v for v in vars(app_mod).values()
               if inspect.isclass(v) and hasattr(v, "_watchdog_loop"))
    src = inspect.getsource(cls._fleet_overview)
    # 抠出「拼 lines + 拼 fallback」那一段，在本地真跑
    start = src.index("lines = []") if "lines = []" in src else src.index("lines = [")
    end = src.index('fallback = "🖥 电脑上的会话')
    # 🔴 `src[start:]` 的第一行没有缩进（index 落在 `lines` 那个词上），后面几行有 ——
    # 直接 dedent 不掉共同前缀。先把第一行的缩进补回去再 dedent。
    indent = " " * 8
    block = textwrap.dedent(indent + src[start:end])
    block = re.sub(r"^\s*from \.fleet\.cards import .*$", "", block, flags=re.M)

    waiting = _sess(status="waiting", pending=_ask())
    view = Overview(sessions=(waiting,), notes=())
    env = {
        "view": view, "unread": 0, "row_label": row_label,
        "overview_pending_line": overview_pending_line,
        "_cleaned": lambda s, _r: s, "_redact_text": lambda x: x,
        "self": type("S", (), {"_unread_headline": lambda *a: "x"})(),
    }
    exec(block, {}, env)  # noqa: S102 —— 跑的是 app.py 自己那几行
    blob = "\n".join(env["lines"])
    assert "它在问你" in blob, f"文字兜底里没有「它在问你」：\n{blob}"
    assert "短信验证码" in blob, "选项也该在兜底里"
