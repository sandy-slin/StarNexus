"""第 18 轮：发消息后立刻给个「收到」的表情回复 + 流式输出，都要先把权限要到手。

## 为什么这两件事卡了很久

用户原话：「我发消息后最后有个反应 比如 get 符号 你可以参考 openclaw」
「流式信息发送还没有吗？我都提了好多次了」

两件都要**新权限**，而老用户拿不到新权限 —— 这是第 17 轮才查清的机制问题：
装机时申请权限只发生一次，`menu_heal` 的 `done: true` 之后再也不碰权限。
第 17 轮已经修了（`menu_heal.scopes_fingerprint()`：清单变了就重跑），
所以**现在往 `WANT_SCOPES` 里加东西，老用户下次重启就会自动补上**。

## 实测：表情回复确实要新权限（文档写得不准）

文档「权限要求」那段写的是「获取与发送单聊、群组消息」/「发送、删除消息表情回复」
二者任一即可，而我们已经有前者那一组（`im:message.p2p_msg:readonly` + `im:message:send_as_bot`）。
2026-11-07 真打一发（假 message_id）拿回的是：

    code=99991672
    Access denied. One of the following scopes is required:
    [im:message, im:message.reactions:write_only]

⇒ 服务端的判据是 `im:message`（全量读写）或 `im:message.reactions:write_only`。
我们两个都没有。**按服务端的原话要，不按文档那句中文要。**
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge import menu_heal
from bridge.register import WANT_SCOPES


def test_reaction_scope_is_requested():
    """🔴 按服务端 99991672 的原话要最小那一项，不要 `im:message`（那是全量读写，过宽）。"""
    assert "im:message.reactions:write_only" in WANT_SCOPES, (
        "没申请表情回复权限 ⇒ 「收到」那个表情发不出去"
    )
    assert "im:message" not in WANT_SCOPES, (
        "别要 im:message —— 那是单聊群聊的全量读写，比我们需要的宽得多"
    )


def test_cardkit_scope_is_requested():
    """流式输出要的那一项（第 17 轮角色 F 实测：现有 13 项里 cardkit 一项都没有）。"""
    assert any("cardkit" in s for s in WANT_SCOPES), "没申请卡片流式权限 ⇒ 流式做不了"


def test_adding_scopes_invalidates_old_done_marks(tmp_path):
    """🔴 这条是整件事能成的关键：清单变了 ⇒ 老用户的「已装好」标记失效 ⇒ 自愈重跑补权限。

    没有这一条，上面两项加了也白加（老用户永远拿不到）。
    """
    import json

    path = tmp_path / "menu_heal.json"
    # 老机器上的记录：有 done、没有 scopes 字段（现场就是这样）
    path.write_text(json.dumps({"apps": {"cli_x": {"done": True, "version": "1.0"}}}),
                    encoding="utf-8")
    assert menu_heal.is_done(str(path), "cli_x") is False

    # 按**当前**清单装好的 ⇒ 不重跑
    menu_heal.mark_done(str(path), "cli_x", "1.1")
    assert menu_heal.is_done(str(path), "cli_x") is True


def test_fingerprint_changes_when_scopes_change(monkeypatch):
    """阳性对照：指纹真的跟着清单走（不然上面那条是空的）。"""
    before = menu_heal.scopes_fingerprint()
    import bridge.register as reg

    monkeypatch.setattr(reg, "WANT_SCOPES", list(reg.WANT_SCOPES) + ["im:fake:probe"])
    assert menu_heal.scopes_fingerprint() != before
