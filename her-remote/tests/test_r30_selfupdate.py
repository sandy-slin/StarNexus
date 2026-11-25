"""第 30 轮：全公司版本可见 + 全自动更新（用户原话「不可能靠人来报」）。

盯的几条：
1. 查 latest.json 时自动上报，**只有** v / id / os / u / app 这 5 个字段（隐私红线）。
2. `install_id` 随机、落盘后不变；写不进也照样能查新版。
3. Windows 第 38 轮起和 Mac 一样自动装（原来不下载、只给链接）。
4. 服务端回滚到以前装成功过的版本 ⇒ 照样装。
5. 一张没人点的审批卡不再让这台永远不更新；重启后点旧卡仍能续跑。
6. 启动 60 秒先查一次。
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
import types
import urllib.parse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from bridge import selfupdate  # noqa: E402
from test_r14_selfupdate import BASE, FILES, PKG, _done, _latest, _opener  # noqa: E402

APP_ID = "cli_a1b2c3d4e5f60708"
SECRET = "SuperSecret" + "Value0123456789abcdef"


def _up(tmp_path, files=None, *, seen=None, current="20261106-oldbuild", app_id=APP_ID,
        runner=None, now=None, idle=True):
    pkg = tmp_path / "bridge"
    pkg.mkdir(exist_ok=True)
    if current:
        (pkg / "buildinfo.py").write_text(f'BUILD_ID = "{current}"\n', encoding="utf-8")
    return selfupdate.SelfUpdater(
        str(pkg), str(tmp_path / "update_state.json"),
        is_idle=lambda: idle, opener=_opener(files if files is not None else FILES, seen),
        runner=runner or (lambda p: _done(0)), now=now or (lambda: 1000.0), app_id=app_id,
    )


def _latest_query(seen):
    urls = [u for u in seen if u.split("?", 1)[0].endswith("/latest.json")]
    assert urls, seen
    parsed = urllib.parse.urlsplit(urls[-1])
    return parsed.query, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)


# ---------- 1. 上报字段 ----------


def test_report_has_exactly_the_five_fields(tmp_path):
    seen = []
    up = _up(tmp_path, seen=seen)
    up.check()
    raw, q = _latest_query(seen)
    assert sorted(q) == sorted(["v", "id", "os", "u", "app"]), q
    assert q["v"] == ["20261106-oldbuild"]
    assert len(q["id"][0]) == 16 and int(q["id"][0], 16) >= 0
    assert q["os"][0] in ("mac", "windows")
    assert q["u"] == [""]
    assert q["app"] == [APP_ID]
    assert len(raw.encode()) <= 300


def test_u_is_only_the_outcome_word_of_the_latest_attempt(tmp_path):
    """第 32 轮起 `u` 取**最近一次尝试**（不再是当前版本那条），仍然只许一个词、不带 detail。"""
    seen = []
    clock = {"t": 1000.0}
    up = _up(tmp_path, seen=seen, now=lambda: clock["t"])
    up._remember("20261106-other", "ok")
    clock["t"] += 60
    up._remember("20261106-oldbuild", "fail", "installer said: token=abc /Users/zhangsan")
    up.check()
    raw, q = _latest_query(seen)
    assert q["u"] == ["fail"]
    for leak in ("token", "zhangsan", "installer", "Users"):
        assert leak not in urllib.parse.unquote(raw), raw


def test_app_never_carries_the_secret(tmp_path):
    """桥传进来的只是 app_id；就算哪天传错成 secret，形状对不上也不发。"""
    seen = []
    _up(tmp_path, seen=seen, app_id=SECRET).check()
    raw, q = _latest_query(seen)
    assert SECRET not in urllib.parse.unquote(raw)
    assert q["app"] == [""]


def test_bridge_gives_updater_app_id_but_not_secret(tmp_path):
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(app_id=APP_ID, app_secret=SECRET)
    b.updater = _up(tmp_path, app_id="")
    b._updater_learn_app_id()
    assert b.updater.app_id == APP_ID
    assert SECRET not in repr(vars(b.updater))


def test_values_are_url_encoded(tmp_path):
    seen = []
    up = _up(tmp_path, seen=seen, current="20261106-abc&x=1 y")
    up.check()
    raw, q = _latest_query(seen)
    assert sorted(q) == sorted(["v", "id", "os", "u", "app"]), f"值里的 & 拼出了多余字段：{raw}"
    assert " " not in raw


def test_query_drops_unknown_keys_and_caps_length():
    assert "secret" not in selfupdate._query({"v": "x", "secret": "s"})
    assert selfupdate._query({"v": "x" * 400}) == ""


def test_dev_machine_does_not_report_or_create_id(tmp_path):
    seen = []
    _up(tmp_path, seen=seen, current="").check()
    assert all("?" not in u for u in seen), seen
    assert not (tmp_path / "install_id").exists()


# ---------- 2. install_id ----------


def test_install_id_is_persisted_private_and_random(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    first = _up(a).install_id()
    again = _up(a).install_id()          # 新对象 = 重启
    other = _up(b).install_id()
    assert first == again, "重启后编号变了 ⇒ 一台机器被数成好几台"
    assert first != other, "两台机器编号一样 ⇒ 不是随机的"
    path = a / "install_id"
    assert path.read_text() == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(first) == 16 and set(first) <= set("0123456789abcdef")
    # 不从主机名 / 用户名推导
    for ident in (os.uname().nodename, os.environ.get("USER", "") or "zz-none"):
        assert ident.lower() not in first


def test_install_id_lives_next_to_update_state(tmp_path):
    up = _up(tmp_path)
    up.install_id()
    assert (tmp_path / "install_id").exists()
    assert not (tmp_path / "bridge" / "install_id").exists(), "放进 bridge/ 重装就被覆盖"


def test_unwritable_install_id_still_checks_for_updates(tmp_path, monkeypatch):
    seen = []
    up = _up(tmp_path, seen=seen)
    real_open = os.open

    def deny(path, *a, **kw):
        if "install_id" in str(path):
            raise PermissionError("read-only")
        return real_open(path, *a, **kw)

    monkeypatch.setattr(selfupdate.os, "open", deny)
    assert up.check() is not None, "编号写不进把更新也弄失败了"
    _raw, q = _latest_query(seen)
    assert "id" not in q
    assert not [n for n in os.listdir(tmp_path) if n.endswith(".tmp")]


def test_garbage_install_id_file_is_replaced(tmp_path):
    (tmp_path / "install_id").write_text("not-hex\n")
    value = _up(tmp_path).install_id()
    assert len(value) == 16 and (tmp_path / "install_id").read_text() == value


# ---------- 3. Windows ----------


def _windows(monkeypatch):
    monkeypatch.setattr(selfupdate.platform, "system", lambda: "Windows")


WIN_FILES = {f"{BASE}/latest.json": _latest(key="windows"),
             f"{BASE}/feishu-agent-mac.command": PKG}


def test_windows_now_auto_updates_like_mac(tmp_path, monkeypatch):
    """第 38 轮起 Windows 不再 manual_only：下载、核 sha256、跑安装器（细节在 test_r38_win_selfupdate.py）。"""
    _windows(monkeypatch)
    seen = []
    ran = []
    up = _up(tmp_path, WIN_FILES, seen=seen, runner=lambda p: ran.append(p) or _done(0))
    info = up.check()
    assert info is not None and not info.get("manual_only")
    ok, _ = up.update_now(info)
    assert ok and len(ran) == 1
    _raw, q = _latest_query(seen)
    assert q["os"] == ["windows"]


def test_windows_on_demand_installs_instead_of_link(tmp_path, monkeypatch):
    from bridge.app import Bridge

    _windows(monkeypatch)
    ran = []
    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(app_id=APP_ID)
    b.updater = _up(tmp_path, WIN_FILES, runner=lambda p: ran.append(p) or _done(0))
    said = []
    b._say = lambda chat, text: said.append(text) or True
    b._update_on_demand("oc_1")
    assert len(ran) == 1, said
    assert not any("Windows 上我还不能自己装" in t for t in said), said


# ---------- 4. 回滚 ----------


def test_rollback_to_previously_installed_build_is_installed(tmp_path):
    clock = {"t": 1000.0}
    ran = []
    files = dict(FILES)
    files[f"{BASE}/latest.json"] = _latest(build="20261106-oldbuild")   # 服务端回滚
    up = _up(tmp_path, files, current="20261107-newer", now=lambda: clock["t"],
             runner=lambda p: ran.append(p) or _done(0))
    up._remember("20261106-oldbuild", "ok")           # 以前装成功过
    clock["t"] += 3600
    up._remember("20261107-newer", "ok")              # 后来升到了新版
    clock["t"] += 3600
    info = up.check()
    assert info is not None and info["build_id"] == "20261106-oldbuild", up.last_error
    assert up.tick()[0] is True and len(ran) == 1


def test_just_installed_build_is_not_reinstalled_in_a_loop(tmp_path):
    """阳性对照：刚装成功、盘上版本号还没跟上 ⇒ 不每 10 分钟重装。"""
    ran = []
    up = _up(tmp_path, runner=lambda p: ran.append(p) or _done(0))
    assert up.tick()[0] is True
    assert up.check() is None
    assert "装失败" not in up.last_error
    assert len(ran) == 1


# ---------- 5. 审批卡挂着 ----------


def _approval_bridge(tmp):
    from test_r20_superseded_approval import _bridge

    return _bridge(tmp)


def test_pending_approval_card_does_not_block_update():
    from test_r20_superseded_approval import _defer_with_session

    with tempfile.TemporaryDirectory() as tmp:
        b = _approval_bridge(tmp)
        _defer_with_session(b)
        assert b._awaiting, "前提：卡挂着"
        assert b._work_in_flight() is False, "一张没人点的卡让这台永远不更新"


def test_pending_card_without_hook_session_still_counts_as_busy():
    """接不上的那种（钩子没记 session_id）⇒ 重启会把续跑弄丢 ⇒ 仍算有活，别装。"""
    from test_r20_superseded_approval import _defer

    with tempfile.TemporaryDirectory() as tmp:
        b = _approval_bridge(tmp)
        _defer(b)
        assert b._work_in_flight() is True


def test_update_while_card_pending_then_click_old_card_still_resumes():
    """端到端：卡挂着 → 判空闲 → 「装新版重启」（换一个新的 Bridge）→ 点旧卡 ⇒ 接着那一轮跑。"""
    from test_r20_superseded_approval import _capture_continue, _defer_with_session, _toast
    from test_interference import SCOPE, _click

    with tempfile.TemporaryDirectory() as tmp:
        b = _approval_bridge(tmp)
        token = _defer_with_session(b)
        assert b._work_in_flight() is False
        fresh = _approval_bridge(tmp)         # 重启：内存全没了
        fresh.approvals = b.approvals
        assert fresh._awaiting == {}
        got, restore = _capture_continue(fresh)
        try:
            resp = _click(fresh, token)
        finally:
            restore()
        assert got, f"重启后点旧卡没续跑：{_toast(resp)}"
        assert fresh.sessions.get(SCOPE).claude_session_id == "sess-from-hook"


def test_running_turn_still_blocks_update():
    """阳性对照：会话锁被持着 ⇒ 有活。"""
    import asyncio

    with tempfile.TemporaryDirectory() as tmp:
        b = _approval_bridge(tmp)
        from test_interference import SCOPE

        asyncio.run(b.sessions.get(SCOPE).lock.acquire())
        assert b._work_in_flight() is True


# ---------- 6. 启动 60 秒先查 ----------


def test_first_check_60s_after_start_then_every_10_min(monkeypatch):
    import bridge.app as app_mod
    from bridge.app import Bridge

    sleeps = []

    class Stop(Exception):
        pass

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > 3:
            raise Stop

    ticks = []
    monkeypatch.setattr(app_mod.time, "sleep", fake_sleep)
    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(app_id=APP_ID)
    b.updater = types.SimpleNamespace(app_id=APP_ID, tick=lambda: ticks.append(1))
    with pytest.raises(Stop):
        b._selfupdate_loop()
    assert sleeps[0] == 60
    assert sleeps[1:] == [selfupdate.CHECK_EVERY_SEC] * 3
    assert len(ticks) == 3
