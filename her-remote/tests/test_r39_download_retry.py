"""发版那几秒下的包 sha 必然对不上 ⇒ 重读一次清单再试（不放松校验）。

发版不是原子的：`publish-update.sh` 先换包、最后写 `latest.json`
（那个顺序防的是「json 新、包旧」）。但**反方向有个真窗口**——
198 实测 `feishu-agent-windows.bat` mtime `12:42:34.638`、`latest.json` `12:42:39.723`
⇒ 这 **5.1 秒**里来查更新的客户端拿到「旧 json 的 sha + 已经换好的新包」，必然校验失败。

现场：`…<机器>` 11-20 23:20 上报 `ValueError: 下载回来的安装包校验不过`
（指纹 `856d29f0669c` @ `selfupdate.py:617`），而它 23:20:07 正落在那次发版的窗口里。
原来 `download` 只下一次、对不上就抛 ⇒ 这一拍记成 fail，白等一轮。

🔴 重试**不等于放松校验**：重新取一份清单、拿新的 sha 再核，还不对照旧抛。
🔴 调顺序消不掉这个窗口（两头各一个），根治要给包名带 build_id、新旧并存 —— 那是另一件事。
"""

from __future__ import annotations

import hashlib
import tempfile

import pytest

import bridge.selfupdate as su

_NEW = b"new package bytes"
_NEW_SHA = hashlib.sha256(_NEW).hexdigest()
_OLD_SHA = hashlib.sha256(b"old package bytes").hexdigest()


@pytest.fixture()
def updater(monkeypatch):
    u = su.SelfUpdater.__new__(su.SelfUpdater)
    u._opener = None
    monkeypatch.setattr(su, "_fetch", lambda url, opener=None: _NEW)
    monkeypatch.setattr(su, "installer_key", lambda: "windows")
    return u


def _info(build: str, sha: str) -> dict:
    return {"build_id": build, "windows": {"file": "w.bat", "sha256": sha}}


def test_stale_manifest_is_retried(updater, monkeypatch):
    """🔴 他的现场：旧 json 的 sha + 新包 ⇒ 重读清单后装上，不记 fail。"""
    calls = []
    monkeypatch.setattr(su, "latest",
                        lambda opener=None, params=None: calls.append(1) or _info("NEW", _NEW_SHA))
    path = updater.download(_info("OLD", _OLD_SHA), tempfile.mkdtemp())
    assert path.endswith("w.bat")
    assert len(calls) == 1, "该重读一次清单"


def test_really_corrupt_package_still_raises(updater, monkeypatch):
    """🔴 阴性对照：包真的坏了（清单没变）⇒ 必须抛，校验不许被放松。"""
    stale = _info("OLD", _OLD_SHA)
    monkeypatch.setattr(su, "latest", lambda opener=None, params=None: stale)
    with pytest.raises(ValueError, match="校验不过"):
        updater.download(stale, tempfile.mkdtemp())


def test_manifest_unreachable_raises(updater, monkeypatch):
    """清单取不回来 ⇒ 也要抛，不许当成通过。"""
    monkeypatch.setattr(su, "latest", lambda opener=None, params=None: None)
    with pytest.raises(ValueError, match="校验不过"):
        updater.download(_info("OLD", _OLD_SHA), tempfile.mkdtemp())


def test_manifest_without_our_platform_raises(updater, monkeypatch):
    """重读到的清单里没有本平台那段 ⇒ 抛（别 KeyError 崩在半路）。"""
    monkeypatch.setattr(su, "latest", lambda opener=None, params=None: {"build_id": "NEW"})
    with pytest.raises(ValueError, match="校验不过"):
        updater.download(_info("OLD", _OLD_SHA), tempfile.mkdtemp())


def test_happy_path_does_not_refetch(updater, monkeypatch):
    """一次就对 ⇒ 不该多读清单（别给每次更新加一次请求）。"""
    calls = []
    monkeypatch.setattr(su, "latest",
                        lambda opener=None, params=None: calls.append(1) or _info("NEW", _NEW_SHA))
    updater.download(_info("NEW", _NEW_SHA), tempfile.mkdtemp())
    assert calls == [], "白读了一次清单"


def test_retry_uses_latest_not_check():
    """🔴 重试走 `latest()`，不走 `check()` —— 后者判冷却、还 `_reconcile`，都是副作用。"""
    import inspect
    src = inspect.getsource(su.SelfUpdater.download)
    assert "latest(opener=self._opener)" in src
    assert "self.check(" not in src


def test_only_two_attempts():
    """只试两次 —— 真坏的包不该被重试拖成循环。"""
    import inspect
    src = inspect.getsource(su.SelfUpdater.download)
    assert "for attempt in (1, 2):" in src
