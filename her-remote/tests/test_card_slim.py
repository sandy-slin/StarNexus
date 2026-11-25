"""总览瘦身（11-05 下午 spec 第一节）。

用户原话：「claude 和 codex 和 cursor 会话列表 前面部分提取到上面成为公共部分，文字太多 手机上显示不全。」
原来一行是 `Claude · carher-admin · 闲着 · 14 分钟前｜Chat.auto-link.co…`，
工具名和项目每行都重复一遍，名字反而被挤到 18 个字。

盯这几件事：
- 工具名、项目只在分组标题上说一次；行里只剩 状态点 + 会话名 + 多久前
- 一行的**可见宽度**不超过 48 格（= 24 个汉字，手机竖屏一行）——这个数钉在这里
- 跨项目、没标题、撞名、emoji、超长目录名、20KB 自缩，各有一条
- 标题脱敏在截断之前（同详情卡的红字）

全部用假会话，不读真实 store。
"""

from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import Session  # noqa: E402
from bridge.fleet import cards  # noqa: E402
from bridge.fleet.cards import (  # noqa: E402
    MAX_CARD_BYTES,
    MAX_ROWS_PER_TOOL,
    NAME_TARGET_CELLS,
    OVERVIEW_ROW_CELLS,
    build_overview_card,
    clip_cells,
    display_width,
    row_label,
)
from bridge.fleet.scan import Overview  # noqa: E402

CA = "/Users/x/codes/carher-admin"


def _sess(tool="claude", sid="11111111-2222-4333-8444-555555555555", title="修登录", cwd=CA,
          status="idle", age=120, **kw):
    base = dict(tool=tool, surface="cli", id=sid, title=title, cwd=cwd, status=status,
                owner="none", updated_at=time.time() - age if age is not None else 0, can_send=True)
    base.update(kw)
    return Session(**base)


def _sid(i: int) -> str:
    return f"{i:08x}-0000-4000-8000-000000000000"


def _card(view, redact=None):
    blob = build_overview_card(view, redact=redact)
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    return blob, card


from tests._overview_rows import tap_as_button  # noqa: E402


def _walk(node, tag):
    out = []
    if isinstance(node, dict):
        if node.get("tag") == tag:
            out.append(node)
        elif tag == "button" and tap_as_button(node):  # 11-16 会话行是交互容器（tests/_overview_rows.py）
            out.append(tap_as_button(node))
        for v in node.values():
            out.extend(_walk(v, tag))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v, tag))
    return out


def _rows(card):
    """会话那几行按钮的文字（不含最后那个刷新）。"""
    return [b["text"]["content"] for b in _walk(card, "button")
            if b["behaviors"][0]["value"].get("op") == "open"]


def _headings(card):
    """分组标题里 `group_heading` 那一截。

    第 28 轮起每组是折叠面板，标题末尾多了「· 都闲着 / · 🟢 1 个在跑」（`cards.group_summary`，
    在 test_r28_overview 里测）⇒ 这里只比前半截（工具名 + 项目 + 个数），这个文件管的是那一截。
    """
    out = []
    for m in _walk(card, "markdown"):
        text = m["content"]
        if text.startswith("🤖"):
            out.append(text.split("）· ", 1)[0] + "）" if "）· " in text else text)
    return out


# ---------- 行宽：钉死 ----------


def test_row_budget_is_pinned_to_phone_width():
    """手机竖屏一行看得见 22~24 个汉字 ⇒ 48 格。改这个数要先在真手机上看过。"""
    assert OVERVIEW_ROW_CELLS == 48
    assert display_width("汉" * 24) == OVERVIEW_ROW_CELLS


def test_display_width_counts_cjk_and_emoji_as_two():
    assert display_width("abc") == 3
    assert display_width("修登录") == 6
    assert display_width("🟢") == 2
    assert display_width("⏸") == 2  # 挂起那个点：east_asian_width 是 N，但手机上是 emoji
    assert display_width(" · ") == 4  # 中间那个点是 Ambiguous，按两格（写死 3 实测会多剪一刀）
    assert display_width("👨‍👩‍👧‍👦") == 2  # 一家四口：7 个码位，一个字
    assert display_width("🇨🇳") == 2
    assert display_width("👍🏽") == 2


def test_every_row_fits_even_with_hostile_titles():
    titles = [
        "汉" * 80,
        "a" * 200,
        "🔥" * 60,
        "👨‍👩‍👧‍👦" * 30,
        "Chat.auto-link.com.cn API 透传" * 4,
        "",
        "  \n\t ",
    ]
    sessions = [_sess(sid=_sid(i), title=t, status=st, age=age)
                for i, t in enumerate(titles)
                for st, age in (("running", 30), ("stopped", 86400 * 400))]
    _, card = _card(Overview(sessions=sessions))
    rows = _rows(card)
    assert rows
    for row in rows:
        assert display_width(row) <= OVERVIEW_ROW_CELLS, row


def test_clip_never_splits_an_emoji():
    family = "👨‍👩‍👧‍👦"
    for cells in range(1, 20):
        out = clip_cells(family * 10, cells)
        body = out[:-1] if out.endswith("…") else out
        # 剪下来的只能是整个整个的「一家四口」
        assert body.replace(family, "") == "", (cells, out)
        assert display_width(out) <= max(cells, 2)
    flags = clip_cells("🇨🇳🇯🇵🇺🇸🇬🇧", 5)
    assert flags in ("🇨🇳…",), flags


def test_ascii_title_uses_the_width_it_saves():
    """原来按字符数截 18 个，`slot-allocation-aimd`（20 个字母，20 格）被剪成 `slot-allocation-a…`。"""
    _, card = _card(Overview(sessions=[_sess(title="slot-allocation-aimd")]))
    assert _rows(card) == ["⚪ slot-allocation-aimd · 2 分钟前"]


# ---------- 公共部分提到分组标题 ----------


def test_shared_parts_move_to_heading():
    sessions = [_sess(sid=_sid(1), title="修登录", status="running", pids=(9,), age=180),
                _sess(sid=_sid(2), title="文档更新，适配最新版本 (2)", age=10)]
    blob, card = _card(Overview(sessions=sessions))
    assert _headings(card) == ["🤖 **Claude** · carher-admin（2 个）"]
    rows = _rows(card)
    assert rows == ["🟢 修登录 · 3 分钟前", "⚪ 文档更新，适配最新版本 (2) · 刚刚"]
    for row in rows:
        assert "Claude" not in row and "carher-admin" not in row
        assert "在跑" not in row and "闲着" not in row  # 字挪到图例里了
    # 只留点 ⇒ 底下必须有图例，而且只列这张卡上出现过的状态
    # 图例 = 那行**小字**（text_size=notation）。2026-11-06 标题改成「🟢 1 个在跑」之后，
    # 原来按「含 在跑」找图例会把标题也抓进来 ⇒ 按小字找，别按字面量猜。
    legends = [m["content"] for m in _walk(card, "markdown")
               if m.get("text_size") == "notation" and "在跑" in m["content"]]
    assert legends == ["🟢 在跑 · ⚪ 闲着"]
    assert "看不出" not in blob


def test_each_tool_gets_its_own_heading():
    sessions = [_sess("claude", _sid(1)), _sess("codex", _sid(2), cwd="/x/carher-admin"),
                _sess("cursor", _sid(3), cwd="/y/CarHer")]
    _, card = _card(Overview(sessions=sessions))
    assert _headings(card) == [
        "🤖 **Claude** · carher-admin（1 个）",
        "🤖 **Codex** · carher-admin（1 个）",
        "🤖 **Cursor** · CarHer（1 个）",
    ]


def test_tool_across_several_projects():
    """同一工具跨项目：标题写最多的那个 +「等 N 个项目」，别的项目的行自己带项目名。"""
    sessions = [_sess(sid=_sid(1), title="甲"), _sess(sid=_sid(2), title="乙"),
                _sess(sid=_sid(3), title="丙", cwd="/private/tmp/peerprobe"),
                _sess(sid=_sid(4), title="丁", cwd="")]
    _, card = _card(Overview(sessions=sessions))
    assert _headings(card) == ["🤖 **Claude** · carher-admin 等 3 个项目（4 个）"]
    rows = _rows(card)
    assert rows[0] == "⚪ 甲 · 2 分钟前"
    assert rows[1] == "⚪ 乙 · 2 分钟前"
    assert rows[2] == "⚪ 丙 · peerprobe · 2 分钟前"
    # 没有工作目录的那行：行里用短说法（整句「不知道哪个项目」14 格，名字一长就被剪成
    # 「不知道…」—— 说了等于没说还白占地方）。文字兜底那条路照旧用整句。
    assert rows[3] == "⚪ 丁 · 项目不明 · 2 分钟前"


def test_heading_counts_only_the_projects_you_can_see():
    """🔴 「等 N 个项目」只数列出来的行。

    数全部的话：8 行全在主项目、更早的 6 个在别处 ⇒ 标题写「等 7 个项目」，
    而屏幕上一个别的项目都看不见 —— 这个数字找不到出处（2026-11-05 体验审查）。
    """
    sessions = [_sess(sid=_sid(i), title=f"会话{i}", age=i + 1) for i in range(MAX_ROWS_PER_TOOL)]
    sessions += [_sess(sid=_sid(20 + i), title=f"更早{i}", cwd=f"/x/proj-{i}", age=9000 + i)
                 for i in range(6)]
    _, card = _card(Overview(sessions=sessions))
    assert _headings(card) == [f"🤖 **Claude** · carher-admin（{len(sessions)} 个）"]
    assert len(_rows(card)) == MAX_ROWS_PER_TOOL
    # 总数（14 个）照旧是全部，没列出来的那些由「还有 N 个」兜着
    assert "还有 6 个更早的没列出来" in json.dumps(card, ensure_ascii=False)


def test_heading_project_is_from_the_rows_shown_and_tie_is_stable():
    """并列最多时取先出现的（`set` 顺序不稳，同一份数据两次刷新标题不能跳）。"""
    sessions = [_sess(sid=_sid(1), cwd="/a/beta"), _sess(sid=_sid(2), cwd="/a/alpha")]
    heads = {tuple(_headings(_card(Overview(sessions=sessions))[1])) for _ in range(20)}
    assert heads == {("🤖 **Claude** · beta 等 2 个项目（2 个）",)}


def test_long_project_dir_does_not_eat_the_name():
    """🔴 名字先拿够 10 个汉字，项目名只分剩下的（2026-11-05 体验审查改的方向）。

    原来项目名先拿满 14 格 ⇒ 这一行的名字被剪到 7 个汉字
    （`⚪ 生成一个视频：… · a-very-long-… · 2 分钟前`，实测）。用户是按名字找会话的。
    """
    sessions = [_sess(sid=_sid(1)), _sess(sid=_sid(2)),
                _sess(sid=_sid(3), title="生成一个视频：秋天的森林",
                      cwd="/Users/x/codes/a-very-long-directory-name-for-video-output")]
    _, card = _card(Overview(sessions=sessions))
    row = _rows(card)[2]
    name = row.split(" · ")[0][2:]  # 去掉状态点
    assert display_width(name) >= NAME_TARGET_CELLS - 2, f"名字被项目名挤掉了：{row!r}"
    assert row.startswith("⚪ 生成一个视频：秋天"), row
    assert " · a-very-" in row and "…" in row.split(" · ")[1], row
    assert display_width(row) <= OVERVIEW_ROW_CELLS


def test_short_project_leaves_the_rest_to_the_name():
    """项目名短 ⇒ 富余全给名字（不是死守 20 格）。"""
    sessions = [_sess(sid=_sid(1)), _sess(sid=_sid(2)),
                _sess(sid=_sid(3), title="汉" * 40, cwd="/x/ab")]
    _, card = _card(Overview(sessions=sessions))
    row = _rows(card)[2]
    name = row.split(" · ")[0][2:]
    assert display_width(name) > NAME_TARGET_CELLS, row
    assert display_width(row) <= OVERVIEW_ROW_CELLS


# ---------- 没标题 / 撞名 ----------


def test_missing_title_falls_back_to_short_id():
    _, card = _card(Overview(sessions=[_sess(sid="abcdef0123456789", title=""),
                                       _sess(sid="99999999aaaa", title="   ")]))
    assert _rows(card) == ["⚪ abcdef01 · 2 分钟前", "⚪ 99999999 · 2 分钟前"]


def test_no_age_means_no_trailing_separator():
    _, card = _card(Overview(sessions=[_sess(age=None)]))
    assert _rows(card) == ["⚪ 修登录"]


def test_duplicate_titles_get_a_short_id():
    """实测 Cursor 里三个会话都叫 `Chick eating rice video`，三行一模一样，点不准。"""
    sessions = [_sess("cursor", s, title="Chick eating rice video", cwd="/y/CarHer", age=3 * 86400)
                for s in ("8633aaaa", "ad6bbbbb", "b0dfcccc")]
    sessions.append(_sess("cursor", "eeee0000", title="General chat", cwd="/y/CarHer"))
    _, card = _card(Overview(sessions=sessions))
    rows = _rows(card)
    assert rows[:3] == [
        "⚪ Chick eating rice video · #8633 · 3 天前",
        "⚪ Chick eating rice video · #ad6b · 3 天前",
        "⚪ Chick eating rice video · #b0df · 3 天前",
    ]
    assert rows[3] == "⚪ General chat · 2 分钟前"  # 不撞的不加
    assert len(set(rows)) == len(rows)


def test_titles_that_collide_only_after_clipping_get_ids():
    long = "汉" * 30
    sessions = [_sess(sid="aaaa1111", title=long + "甲"), _sess(sid="bbbb2222", title=long + "乙")]
    _, card = _card(Overview(sessions=sessions))
    rows = _rows(card)
    assert "#aaaa" in rows[0] and "#bbbb" in rows[1]
    assert len(set(rows)) == 2


def test_same_title_in_different_tools_is_not_a_collision():
    """不同工具在不同分组里，标题已经分开了 ⇒ 不补 id。"""
    _, card = _card(Overview(sessions=[_sess("claude", "aaaa1111", title="say OK"),
                                       _sess("codex", "bbbb2222", title="say OK")]))
    assert _rows(card) == ["⚪ say OK · 2 分钟前", "⚪ say OK · 2 分钟前"]


# ---------- 脱敏 / 转义 ----------


def test_title_redacted_before_clipping():
    secret = "sk-ant-api03-" + "A" * 40
    view = Overview(sessions=[_sess(title="看看 " + secret + " 为啥不能用")])
    blob, card = _card(view, redact=lambda s: s.replace(secret, "[已脱敏]"))
    assert "sk-ant" not in blob
    assert _rows(card)[0].startswith("⚪ 看看 [已脱敏] 为啥不能用")


def test_project_name_in_heading_goes_through_redact_and_escape():
    view = Overview(sessions=[_sess(cwd="/x/**粗体**")])
    _, card = _card(view, redact=lambda s: s)
    head = _headings(card)[0]
    assert "**粗体**" not in head.split("·", 1)[1]


def test_redact_crash_does_not_kill_the_card():
    def boom(_s):
        raise RuntimeError("坏了")

    view = Overview(sessions=[_sess(sid="abcdef0123", title="修登录")], notes=["Cursor 读得太慢"])
    _, card = _card(view, redact=boom)
    assert _rows(card) == ["⚪ abcdef01 · 2 分钟前"]  # 宁可不显示标题
    assert _headings(card) == ["🤖 **Claude**（1 个）"]  # 项目也不显示，不留空的「 · 」


# ---------- 20KB 自缩 / 行数 ----------


def test_more_rows_fit_than_before():
    """瘦身后每组列 8 行（原来 6 行）。"""
    assert MAX_ROWS_PER_TOOL == 8
    sessions = [_sess(t, _sid(i), title=f"会话{i}") for t in ("claude", "codex", "cursor") for i in range(20)]
    blob, card = _card(Overview(sessions=sessions))
    assert len(_rows(card)) == 3 * MAX_ROWS_PER_TOOL
    assert blob.count("还有 12 个更早的没列出来") == 3


def test_detail_card_note_and_why_cannot_blow_the_budget():
    """🔴 `note` / `why` 是外来长文本，而详情卡的收缩循环只缩消息正文。

    一条 4 万字的报错（adapter 把整段 stderr 塞进 last_error、control.detail 把异常
    原文拼进 note）原来能让整张卡 12 万字节、消息缩到 40 字也下不来 —— 飞书对超大卡片
    是**静默丢弃**，用户点了 [看看它] 什么都收不到（2026-11-04 稳定性 + 安全审查）。
    """
    from bridge.fleet import Message
    from bridge.fleet.cards import build_detail_card

    sess = _sess(status="running", pids=(1,), can_send=False, can_relay=False,
                 why_not_send="读不出" + "报错" * 10000,
                 why_not_relay="也不行" + "原因" * 10000,
                 why_not_stop="停不了" * 5000, owner="terminal")
    for messages in ([], [Message("user", "汉" * 50000)] * 4):
        blob = build_detail_card(sess, messages, note="读不出来：" + "报错" * 10000)
        assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES, len(blob.encode("utf-8"))
        # 剪了但不是剪没了：那句说明还认得出来
        assert "读不出来" in blob and "读不出" in blob


def test_shrink_loop_still_holds_with_huge_notes():
    sessions = [_sess(t, _sid(i), title="汉" * 200) for t in ("claude", "codex", "cursor") for i in range(60)]
    notes = ["读得太慢" * 2000] * 6
    blob, card = _card(Overview(sessions=sessions, notes=notes))
    assert len(blob.encode("utf-8")) <= MAX_CARD_BYTES
    assert _rows(card)  # 缩完至少还有会话
    assert _walk(card, "button")[-1]["behaviors"][0]["value"] == {"act": "fleet", "op": "refresh"}


def test_shrink_loop_trims_rows_when_needed(monkeypatch):
    """把上限压低，逼 `_fit` 真的去少放几行（不然 8 行永远放得下，这条路测不到）。"""
    sessions = [_sess("claude", _sid(i), title=f"会话{i}") for i in range(20)]
    full = len(build_overview_card(Overview(sessions=sessions)).encode("utf-8"))
    monkeypatch.setattr(cards, "MAX_CARD_BYTES", full - 400)
    blob = build_overview_card(Overview(sessions=sessions))
    assert len(blob.encode("utf-8")) <= full - 400
    shown = len(_rows(json.loads(blob)))
    assert 1 <= shown < MAX_ROWS_PER_TOOL
    assert f"还有 {20 - shown} 个更早的没列出来" in blob


def test_card_with_nothing_still_renders():
    blob, card = _card(Overview())
    assert "一个会话都没读到" in blob
    assert _headings(card) == []
    assert not any("在跑 ·" in m["content"] for m in _walk(card, "markdown"))


# ---------- 文字兜底那条路 ----------


def test_standalone_row_keeps_tool_project_and_status_word():
    """卡片发不出去时的纯文字消息：没有分组标题、没有图例 ⇒ 一行要自己说清楚。"""
    label = row_label(_sess("codex", status="running"))
    assert label.startswith("Codex · carher-admin · 在跑 · 2 分钟前｜修登录")
    label = row_label(_sess("claude", title="slot-allocation-aimd"))
    assert label.endswith("｜slot-allocation-aimd")  # 原来按字符数截成了 `slot-allocation-a…`
    assert display_width(row_label(_sess(title="汉" * 100))) <= cards.FALLBACK_ROW_CELLS


# ---------- 「等你」不许被藏起来（2026-11-06 第 1 轮）----------
#
# UX 侦察实测：10 个在跑 + 2 个等你 ⇒ 列出的 8 行全是 🟢，两个在等主人的会话被压进
# 「还有 11 个更早的没列出来」—— 恰恰是他掏出手机最想找的。排序和标题一起修。


def _fleet_of(running: int, waiting: int, idle: int = 0):
    """用真实的排序（scan._sort_key）排出一个总览，跟线上走同一条路。"""
    from bridge.fleet.scan import _sort_key

    out, i = [], 0
    for status, n in (("running", running), ("waiting", waiting), ("idle", idle)):
        for _ in range(n):
            i += 1
            out.append(_sess(sid=_sid(i), title=f"{status}-{i}", status=status, age=i * 60,
                             pids=(1000 + i,) if status != "idle" else ()))
    return Overview(sessions=sorted(out, key=_sort_key))


def test_waiting_sessions_are_listed_even_when_many_are_running():
    blob, card = _card(_fleet_of(running=10, waiting=2))
    rows = [b["text"]["content"] for b in _walk(card, "button") if "behaviors" in b
            and b["behaviors"][0]["value"].get("op") == "open"]
    assert sum(r.startswith("🟡") for r in rows) == 2, f"等你的会话被藏起来了：{rows}"
    assert rows[0].startswith("🟡") and rows[1].startswith("🟡"), "等你的要排在最前"


def test_waiting_rows_are_the_only_primary_buttons():
    """一屏全是蓝色主按钮 = 没有重点。只有等你的才醒目。"""
    _blob, card = _card(_fleet_of(running=3, waiting=1, idle=2))
    opens = [b for b in _walk(card, "button") if "behaviors" in b
             and b["behaviors"][0]["value"].get("op") == "open"]
    primary = [b["text"]["content"] for b in opens if b.get("type") == "primary"]
    assert len(primary) == 1 and primary[0].startswith("🟡"), primary


def test_headline_leads_with_how_many_are_waiting():
    blob, _card_ = _card(_fleet_of(running=3, waiting=2, idle=4))
    assert "2 个在等你" in blob and "3 个在跑" in blob
    assert blob.index("在等你") < blob.index("个在跑"), "先说等你的，再说在跑的"


def test_headline_without_waiting_does_not_mention_it():
    blob, _ = _card(_fleet_of(running=2, waiting=0))
    assert "等你" not in blob.split("点一行")[0], "没有等你的就别在标题里提"


def test_hidden_rows_note_names_waiting_ones():
    """「还有 N 个没列」里藏着等你的 ⇒ 必须点名，不能叫「更早的」。"""
    from bridge.fleet.cards import hidden_rows_note

    note = hidden_rows_note([_sess(status="waiting"), _sess(status="idle")])
    assert "等你" in note and "更早" not in note
    assert hidden_rows_note([_sess(status="idle")]) == "还有 1 个更早的没列出来"
