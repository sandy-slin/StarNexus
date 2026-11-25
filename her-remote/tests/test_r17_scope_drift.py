"""第 17 轮：以后新增的权限，**永远到不了老用户手上**。

## 怎么发现的

角色 F 调研流式输出时撞上：流式要 `cardkit:card:write`，而主会话实测本机生产应用
现有 **13 项 scope、含 cardkit 的 0 项**；`menu_heal.json` 是：

    {"apps": {"cli_aa…": {"done": true, "version": "1.20261107.151153", ...}}}

`done: true` 之后 `MenuHeal` 直接返回（`menu_heal.py:151`、`:174`），不再碰权限。
而装机申请权限只发生在安装器那一次 ⇒ **老机器上没有任何代码路径会再补一次**。

## 为什么这是机制问题，不是流式一个功能的问题

今天这一轮就新增过两次「要新权限」的想法（流式、以后可能的别的卡片能力）。
每次都会撞上同一件事：代码发出去了、权限没跟上，而且**两边都看不出来** ——
用户那边是「新功能没反应」，我们这边是「我发版了啊」。这和
`bridge/app.py` / `bridge/policy.py` 红字里记的「两边都收到了『已经锁住』的信号」是同一个形状。

## 这一轮加的判据

`done` 不再只是一个布尔，要记**那一次装上的是哪一套权限清单**（指纹）。
清单变了（我们新增了 scope）⇒ `done` 不再成立 ⇒ 自愈会再跑一次把新权限补上。
🔴 清单没变就绝不重跑（原来「成功一次永不再写」的设计是对的，不许退回「每次启动都申请」）。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge import menu_heal

APP = "cli_test0000000001"


def _state(tmp_path, **rec):
    path = tmp_path / "menu_heal.json"
    path.write_text(json.dumps({"apps": {APP: rec}}), encoding="utf-8")
    return str(path)


def test_scope_fingerprint_is_stable():
    """同一套清单每次算出来一样（不然每次启动都会判成「清单变了」）。"""
    assert menu_heal.scopes_fingerprint() == menu_heal.scopes_fingerprint()
    assert len(menu_heal.scopes_fingerprint()) >= 8


def test_done_with_current_fingerprint_stays_done(tmp_path):
    """阳性对照：清单没变 ⇒ 照旧「成功一次永不再写」。"""
    path = _state(tmp_path, done=True, version="1.0", scopes=menu_heal.scopes_fingerprint())
    assert menu_heal.is_done(path, APP) is True


def test_done_without_fingerprint_is_stale(tmp_path):
    """🔴 老机器上的记录里没有这个字段（现场就是这样）⇒ 要再跑一次补新权限。"""
    path = _state(tmp_path, done=True, version="1.20261107.151153")
    assert menu_heal.is_done(path, APP) is False


def test_done_with_old_fingerprint_is_stale(tmp_path):
    """我们新增了 scope ⇒ 指纹变了 ⇒ 要再跑一次。"""
    path = _state(tmp_path, done=True, version="1.0", scopes="deadbeef")
    assert menu_heal.is_done(path, APP) is False


def test_mark_done_records_the_fingerprint(tmp_path):
    path = str(tmp_path / "m.json")
    menu_heal.mark_done(path, APP, "1.2.3")
    rec = json.loads(open(path, encoding="utf-8").read())["apps"][APP]
    assert rec["scopes"] == menu_heal.scopes_fingerprint()
    assert menu_heal.is_done(path, APP) is True


def test_unknown_app_is_not_done(tmp_path):
    path = _state(tmp_path, done=True, scopes=menu_heal.scopes_fingerprint())
    assert menu_heal.is_done(path, "cli_someone_else") is False
