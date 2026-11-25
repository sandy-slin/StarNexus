"""第 17 轮 D：编辑器里的对话，到底能不能从手机送一句话进去。

第 14 轮的报告在这一项上是**断的**：它只写了「`--resume` 这条路没敢试」，
于是 `bridge/fleet/cursor.py` 里留下两句话，现在都不准：

1. 「Cursor 没给其他程序留这个口子」—— **这句是错的**。官方 `stop` 钩子的
   `followup_message` 原文就是「Cursor will automatically submit it as the next
   user message」，而且每个钩子都收到对话 id。口子是有的。
2. 「这条路还没量出结论」—— 这一轮量了（用一次性对话，没碰用户的）：拿编辑器那种 id
   去接，接不上它、会另起一个空对话，还会把两边共用的那份导出记录整份重写。

**但「有口子」不等于「手机能送进去」**：那个钩子只在一轮快结束时才被叫到，
外面没有任何定时 / 监听 / 推送的入口能把它叫起来（公开文档里 timer / idle / poll /
webhook / listen / daemon 一个都没有，应用级钩子只有「打开工作区」那一个）。
所以对一个**闲着的**编辑器对话，手机这边依然送不进去 —— 结论没变，变的是**理由**，
而给用户看的那句话必须跟着改准，不然下一轮又有人照着那句假话去设计功能。

这个文件钉住的是**话术的准确性**，不是「能发」：IDE 那条路上一行都不许去 resume、
不许写它的库、不许碰界面 —— 那几条在 `tests/test_fleet_cursor.py` 里已经钉过，这里只复钉入口。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge.fleet import cursor as cur

# `tests/test_copy_quality.py::CANT_EDIT` 的词表（那个文件不归这一轮改）。
# 新写的话一旦用上这里任何一个词，文案闸门会当成一条没记理由的新命中 ⇒ 全量测试红。
CANT_EDIT_WORDS = ("改不了", "不能改", "没法改", "改不动", "不能编辑", "无法修改", "不会改", "只能看")

IDE_COPY = ("WHY_RELAY_IDE", "WHY_RELAY_IDE_RESUME_UNTESTED", "WHY_IDE_SEND", "WHY_IDE_STOP")


# ---------- 1. 把那句错的绝对话收回 ----------


def test_ide_copy_drops_the_claim_that_cursor_left_no_door():
    """🔴「Cursor 没给其他程序留这个口子」是假话 —— 官方钩子能往已有对话追加一句。

    留着它的代价不是嘴瓢：下一轮谁想做「手机催一句」，读到这句会直接判定无路可走，
    而真正的拦路理由（钩子只在一轮结束时被叫到，外面叫不起来）就被这句假话盖住了。
    """
    for name in IDE_COPY:
        text = getattr(cur, name)
        assert "没给其他程序留这个口子" not in text, f"{name} 还在说 Cursor 没留口子"
        assert "不允许外部程序" not in text, f"{name} 还在把「做不到」说成「官方不允许」"


def test_ide_send_copy_points_at_the_one_that_works():
    """送不进去的时候，必须同时告诉他**哪种对话手机上能发** —— 不然他只知道此路不通。"""
    for name in ("WHY_IDE_SEND", "WHY_RELAY_IDE"):
        text = getattr(cur, name)
        assert "cursor-agent" in text, f"{name} 没说「命令行开的对话手机上能发」"
        assert "Cursor" in text, f"{name} 没指路回电脑上的 Cursor"


# ---------- 2. resume 那条路：量过了，写上结论 ----------


def test_resume_copy_states_a_measured_verdict_not_a_pending_one():
    """这条路这一轮量出结论了（一次性对话上实测）⇒ 话术不许再说「还没量出结论」。"""
    text = cur.WHY_RELAY_IDE_RESUME_UNTESTED
    assert "还没量出结论" not in text, "量过了，这句还挂着「没量出结论」"
    assert "没验过的路" not in text, "同上：理由换了，不该再说「没验过」"
    # 实测到的两件事，至少说清一件：接不上（另起一个空的）/ 会把导出记录覆盖掉
    assert re.search(r"空|另起|新的|覆盖|重写", text), "没写实测结论：接不上 + 会覆盖导出记录"


# ---------- 3. 话术本身要过闸门 ----------


def test_ide_copy_has_no_internal_words_and_no_cant_edit_phrasing():
    """🔴 内部名词 + `CANT_EDIT` 词表两道一起查。

    `tests/test_copy_quality.py` 不归这一轮改 ⇒ 新话术里不许出现它那张词表里的词，
    否则闸门会抓到一条「没记理由的新命中」，而豁免表我这边改不动。
    """
    for name in IDE_COPY:
        text = getattr(cur, name)
        for word in CANT_EDIT_WORDS:
            assert word not in text, f"{name} 用了闸门词「{word}」"
        for word in ("composerId", "composerData", "state.vscdb", "transcript", "followup_message",
                     "stop 钩子", "hooks.json", "cursorDiskKV"):
            assert word not in text, f"{name} 露了内部名词「{word}」"


def test_ide_copy_is_plain_and_short():
    """手机上看的一句话：别写成段落，也别空着。"""
    for name in IDE_COPY:
        text = getattr(cur, name)
        assert text.strip(), f"{name} 空的"
        assert len(text) <= 160, f"{name} 太长了（{len(text)} 字），手机上看不完"


# ---------- 4. 入口复钉：话术改了，行为一行都不许松 ----------


def test_ide_relay_still_refuses_and_never_resumes(tmp_path, monkeypatch):
    """🔴 改的是嘴，不是闸：IDE 的 id 进来，照旧拒绝，而且**一次 resume 都不许发**。"""
    import sqlite3

    home = tmp_path / "cursor_home"
    app_user = tmp_path / "app_user"
    (home / "chats").mkdir(parents=True)
    (home / "projects").mkdir(parents=True)
    (app_user / "globalStorage").mkdir(parents=True)
    ide_id = "11111111-2222-3333-4444-555555555555"
    db = app_user / "globalStorage" / "state.vscdb"
    conn = sqlite3.connect(str(db))
    conn.execute("create table cursorDiskKV (key text unique on conflict replace, value blob)")
    conn.execute(
        "create table composerHeaders (composerId text primary key, workspaceId text, createdAt integer,"
        " lastUpdatedAt integer, isArchived integer, isSubagent integer, value text)"
    )
    conn.execute(
        "insert into composerHeaders (composerId, workspaceId, createdAt, lastUpdatedAt, isArchived,"
        " isSubagent, value) values (?,?,?,?,?,?,?)",
        (ide_id, "", 1, 2, 0, 0, "{}"),
    )
    conn.commit()
    conn.close()

    binary = tmp_path / "cursor-agent"
    binary.write_text("#!/bin/sh\nexit 99\n")
    spawned: list = []

    class Runner:
        def run(self, argv, **kw):  # noqa: D401 - 只记账，绝不真跑
            spawned.append(argv)
            raise AssertionError("IDE 那条路上不许起任何进程：%r" % (argv,))

    adapter = cur.CursorAdapter(
        str(home), str(app_user), runner=Runner(), agent_bin=str(binary), procs=lambda: None
    )
    res = adapter.relay(ide_id, "接着干")
    assert not res.ok and not getattr(res, "delivered", False)
    assert res.text == cur.WHY_RELAY_IDE
    assert spawned == []  # 🔴 一次 resume 都没发
