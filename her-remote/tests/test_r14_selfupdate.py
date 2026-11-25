"""第 14 轮：自动更新（用户原话「我发布了新版本 它那边能立即更新」）。

盯的是三类会在上百人身上变成事故的问题：
1. **下载地址被换掉** = 在同事机器上以他的身份跑任意代码 ⇒ 域名写死、只认 HTTPS。
2. **半个包 / 被改过的包** ⇒ sha256 对不上就绝不执行。
3. **装在他正干活的时候** ⇒ 安装器会停掉服务，腰斩那一轮 ⇒ 必须空闲才装。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge import selfupdate


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


def _opener(files: dict, seen: list | None = None):
    def open_fn(url, timeout=None):
        if seen is not None:
            seen.append(url)
        # 第 30 轮起 latest.json 带上报查询串（`?v=…&id=…`）；静态文件服务器不看查询串，这里也不看
        path = url.split("?", 1)[0]
        if path not in files:
            raise OSError(f"404 {url}")
        return _Resp(files[path])
    return open_fn


PKG = b"#!/bin/bash\necho installed\n"
SHA = hashlib.sha256(PKG).hexdigest()
BASE = selfupdate.BASE_URL


def _latest(build="20261106-newbuild", sha=SHA, key=None):
    key = key or selfupdate.installer_key()
    return json.dumps({
        "build_id": build,
        key: {"file": "feishu-agent-mac.command", "sha256": sha, "size": len(PKG)},
    }).encode()


def _updater(tmp_path, files, *, idle=True, runner=None, current="20261106-oldbuild", now=None):
    pkg = tmp_path / "bridge"
    pkg.mkdir(exist_ok=True)
    if current:
        (pkg / "buildinfo.py").write_text(f'BUILD_ID = "{current}"\n', encoding="utf-8")
    return selfupdate.SelfUpdater(
        str(pkg), str(tmp_path / "update_state.json"),
        is_idle=lambda: idle, opener=_opener(files),
        runner=runner or (lambda path: _done(0)),
        now=now or (lambda: 1000.0),
    )


class _Done:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = ""


def _done(rc, out=""):
    return _Done(rc, out)


FILES = {f"{BASE}/latest.json": _latest(),
         f"{BASE}/feishu-agent-mac.command": PKG}


# ---------- 1. 下载地址 ----------


@pytest.mark.parametrize("bad", [
    "http://chat.auto-link.com.cn/ccdl/feishu-agent/latest.json",   # 明文
    "https://evil.example.com/ccdl/feishu-agent/latest.json",       # 别的域名
    "https://chat.auto-link.com.cn.evil.com/x",                     # 前缀像我们的域名
    "file:///etc/passwd",
])
def test_only_https_on_our_host(bad):
    with pytest.raises(ValueError):
        selfupdate._fetch(bad, opener=_opener({bad: b"x"}))


def test_base_url_is_hardcoded_https():
    assert selfupdate.BASE_URL.startswith("https://chat.auto-link.com.cn/")
    src = open(os.path.join(os.path.dirname(selfupdate.__file__), "selfupdate.py"),
               encoding="utf-8").read()
    # 地址不许从配置 / 环境变量来（见文件头红线）
    assert "os.environ.get(\"CARHER_UPDATE" not in src
    assert "_ALLOWED_HOST = \"chat.auto-link.com.cn\"" in src


# ---------- 2. 校验值 ----------


def test_wrong_sha_is_refused_and_installer_never_runs(tmp_path):
    files = dict(FILES)
    files[f"{BASE}/latest.json"] = _latest(sha="0" * 64)
    ran = []
    up = _updater(tmp_path, files, runner=lambda p: ran.append(p) or _done(0))
    ok, text = up.update_now(up.check())
    assert not ok and ran == []
    # 给人看的那句不提校验值（文案闸门），原因在 last_error / 日志里
    assert "没装上" in text, text
    assert "校验" in up.last_error, up.last_error


def test_truncated_download_is_refused(tmp_path):
    files = dict(FILES)
    files[f"{BASE}/feishu-agent-mac.command"] = PKG[:10]   # 传输中断
    ran = []
    up = _updater(tmp_path, files, runner=lambda p: ran.append(p) or _done(0))
    ok, _ = up.update_now(up.check())
    assert not ok and ran == []


def test_good_package_is_installed(tmp_path):
    ran = []
    up = _updater(tmp_path, FILES, runner=lambda p: ran.append(p) or _done(0))
    ok, text = up.update_now(up.check())
    assert ok and len(ran) == 1
    assert "20261106-newbuild" in text


# ---------- 3. 时机 ----------


def test_not_installed_while_busy(tmp_path):
    ran = []
    up = _updater(tmp_path, FILES, idle=False, runner=lambda p: ran.append(p) or _done(0))
    assert up.tick() is None
    assert ran == []


def test_installed_when_idle(tmp_path):
    ran = []
    up = _updater(tmp_path, FILES, runner=lambda p: ran.append(p) or _done(0))
    assert up.tick()[0] is True
    assert len(ran) == 1


# ---------- 4. 不该更新的情形 ----------


def test_same_build_is_not_reinstalled(tmp_path):
    up = _updater(tmp_path, FILES, current="20261106-newbuild")
    assert up.check() is None


def test_dev_machine_without_buildinfo_is_never_touched(tmp_path):
    ran = []
    up = _updater(tmp_path, FILES, current="", runner=lambda p: ran.append(p) or _done(0))
    assert up.check() is None
    assert up.tick() is None and ran == []


def test_success_is_remembered_so_it_does_not_loop(tmp_path):
    ran = []
    up = _updater(tmp_path, FILES, runner=lambda p: ran.append(p) or _done(0))
    assert up.tick()[0] is True
    assert up.check() is None, "装过一次还想再装"
    assert len(ran) == 1


def test_failure_backs_off_then_retries(tmp_path):
    ran = []
    clock = {"t": 1000.0}
    up = _updater(tmp_path, FILES, runner=lambda p: ran.append(p) or _done(1, "boom"),
                  now=lambda: clock["t"])
    assert up.tick()[0] is False
    assert up.check() is None, "刚失败就马上重装"
    clock["t"] += selfupdate._RETRY_AFTER_SEC + 1
    assert up.check() is not None, "过了退避窗口还不重试"
    assert len(ran) == 1


def test_oversized_download_is_refused():
    big = b"x" * (selfupdate._MAX_BYTES + 10)
    url = f"{BASE}/feishu-agent-mac.command"
    with pytest.raises(ValueError):
        selfupdate._fetch(url, opener=_opener({url: big}))


def test_unreachable_server_is_not_an_error(tmp_path):
    up = _updater(tmp_path, {})
    assert selfupdate.latest(opener=_opener({})) is None
    assert up.check() is None


def test_malformed_latest_json_is_ignored(tmp_path):
    for body in (b"not json", b"[]", b'{"build_id": 7}'):
        assert selfupdate.latest(opener=_opener({f"{BASE}/latest.json": body})) is None


def test_latest_without_this_platform_is_skipped(tmp_path):
    files = dict(FILES)
    files[f"{BASE}/latest.json"] = _latest(key="plan9")
    up = _updater(tmp_path, files)
    assert up.check() is None
    assert "校验值" in up.last_error or "文件名" in up.last_error


# ---------- 5. 接线（调用点还在） ----------


def test_bridge_wires_updater_with_idle_gate_and_loop():
    import inspect

    from bridge import app as app_mod

    cls = next(v for v in vars(app_mod).values()
               if inspect.isclass(v) and hasattr(v, "_selfupdate_loop"))
    init = inspect.getsource(cls.__init__)
    assert "selfupdate.SelfUpdater(" in init
    assert "_work_in_flight()" in init, "没把「有活在跑就别装」接上"
    run = inspect.getsource(cls.run)
    assert "_selfupdate_loop" in run, "循环没启动"
    cmd = inspect.getsource(cls._handle_command)
    assert "UPDATE_WORDS" in cmd, "飞书里发「更新」没接上"


def test_update_words_cover_plain_chinese():
    from bridge.app import UPDATE_WORDS

    assert "更新" in UPDATE_WORDS and "升级" in UPDATE_WORDS
