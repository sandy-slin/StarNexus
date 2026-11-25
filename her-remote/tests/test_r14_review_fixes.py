"""第 14 轮：review-r13 的 3 个 🔴（同事那条真实路径）。

1. App 分叉 / 回退后的旧 id（`priorCliSessionIds`、`rewindEdges[].parent`）不再出现在总览、不让发
   —— 点旧的 = 后台另起一条分支，App 里永远看不到（本机实测 afad5500 → 18cd70e4）
2. 桥自己的信使（`<bridge_home>/relay` 里跑的 carher-relay*）不进主人的总览
3. App 关了要走后台、但项目不在白名单 ⇒ 不弹一张点了必失败的「确认，接着跑」卡
"""
from __future__ import annotations

import inspect
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import test_fleet_claude as base
from test_fleet_claude import _adapter, _jsonl, _user, _write, env  # noqa: F401  (fixture)

OLD_FORK = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OLD_REWIND = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
COURIER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _fork_setup(env):
    old = time.time() - 3000
    for sid in (OLD_FORK, OLD_REWIND):
        _jsonl(str(env["proj"] / f"{sid}.jsonl"),
               [_user("App 里问的（分叉前）", cwd=str(env["work"]), entrypoint="claude-desktop-3p")], mtime=old)
    _write(str(env["app"] / "claude-code-sessions" / "acct" / "org" / "local_a.json"),
           {"cliSessionId": base.SID_APP, "title": "App 标题", "isArchived": False, "cwd": str(env["work"]),
            "lastActivityAt": int(old * 1000), "priorCliSessionIds": [OLD_FORK],
            "rewindEdges": [{"parent": OLD_REWIND, "child": base.SID_APP}]})


def test_superseded_app_ids_are_not_listed(env):
    _fork_setup(env)
    ad, *_ = _adapter(env)
    ids = [s.id for s in ad.list(20)]
    assert base.SID_APP in ids
    assert OLD_FORK not in ids and OLD_REWIND not in ids


def test_superseded_app_id_is_not_sendable(env):
    _fork_setup(env)
    ad, runner, *_ = _adapter(env)
    res = ad.send(OLD_FORK, "你好")
    assert not res.ok
    assert runner.claude_calls == []


def test_courier_history_and_live_rows_are_hidden(env):
    relay_cwd = env["bridge"] / "relay"
    relay_cwd.mkdir(parents=True)
    _jsonl(str(env["home"] / "projects" / "-relay" / f"{COURIER}.jsonl"),
           [_user("<cross-session-message>hi</cross-session-message>", cwd=str(relay_cwd), entrypoint="sdk-cli")],
           mtime=time.time() - 100)
    # 活着的信使：注册表里 cwd = relay 目录（用终端会话那个 pid，ps 里对得上）
    _write(str(env["home"] / "sessions" / "101.json"),
           {"pid": 101, "sessionId": COURIER, "cwd": str(relay_cwd), "procStart": base.L_TERM,
            "entrypoint": "sdk-cli", "status": "idle", "name": "carher-relay"})
    ad, *_ = _adapter(env)
    ids = [s.id for s in ad.list(20)]
    assert COURIER not in ids


def test_courier_job_with_non_uuid_id_is_not_a_row():
    from bridge.fleet import claude as mod

    src = inspect.getsource(mod.ClaudeAdapter._live_holders)
    assert "_is_session_id(job.session_id)" in src
    assert "_is_courier_cwd(" in src


def test_background_consent_checks_can_send_first():
    """调用点还在：弹后台确认卡之前先看 can_send，不能发就说原因 + 换项目按钮。"""
    from bridge import app as app_mod

    src = inspect.getsource(app_mod.FeishuBridge._ask_relay_consent) \
        if hasattr(app_mod, "FeishuBridge") else ""
    if not src:
        cls = next(v for v in vars(app_mod).values()
                   if inspect.isclass(v) and hasattr(v, "_ask_relay_consent"))
        src = inspect.getsource(cls._ask_relay_consent)
    i_check = src.index('not getattr(sess, "can_send", True)')
    i_card = src.index("self._build_background_consent_card(")
    assert i_check < i_card
    assert 'do_button("dirs"' in src


def test_whitelist_reason_names_the_real_button():
    from bridge.fleet import claude as mod
    from bridge.workspace import DIRS_BUTTON

    src = inspect.getsource(mod.ClaudeAdapter._why_not_send_idle)
    assert DIRS_BUTTON in src and "📁 项目" not in src
