"""第 15 轮：自动更新会把自己杀掉 ⇒ 装到一半、服务回不来。

## 实测（R1 的同形状 A/B 探针，`KeepAlive` plist + 独立 job）

安装器第 6 步做的是 `launchctl bootout` 旧服务 → 写 plist → `launchctl bootstrap` 起新的。
而自动更新是**桥自己的子进程**在跑这个安装器 ⇒ `bootout` 把桥连着它的子进程一起干掉：

    A 组（现在的写法）：日志停在「要 bootout 了」，`launchctl print` 显示 job 已不在，
                       **`bootstrap` 那一行永远没跑到** ⇒ 服务没了，自己也没了
    B 组（加 start_new_session=True）：bootout 之后子进程还活着 → bootstrap 成功 → 服务回来

`start_new_session=True` 让安装器进到**自己的进程组 / 会话**，不随桥一起被收走。
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge import selfupdate


def test_installer_runs_in_its_own_session(monkeypatch, tmp_path):
    """🔴 没有这个参数，`launchctl bootout` 会把安装器和桥一起收走，装到一半就断。

    🔴 **真的去看那次调用的参数**，不查源码文本：第一版写成
    `assert "start_new_session=True" in inspect.getsource(...)`，而我的注释里也有这个词
    ⇒ 把参数整行删掉测试照样全绿（变异验证当场抓到）。这就是「测试为了错误的理由通过」。
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen.update(kwargs)
        return _Done(0)

    monkeypatch.setattr(selfupdate.subprocess, "run", fake_run)
    monkeypatch.setattr(selfupdate.platform, "system", lambda: "Darwin")
    pkg = tmp_path / "bridge"
    pkg.mkdir()
    up = selfupdate.SelfUpdater(str(pkg), str(tmp_path / "s.json"), is_idle=lambda: True)
    up.run_installer(str(tmp_path / "x.command"))
    assert seen.get("start_new_session") is True, \
        f"安装器没有脱离桥的进程组 —— bootout 会连它一起杀；实际参数：{sorted(seen)}"
    assert seen["argv"][0] == "/bin/bash"


def test_runner_override_still_used_by_tests(tmp_path):
    """阳性对照：注入的 runner 照旧优先（否则这个模块没法测）。"""
    called = []
    pkg = tmp_path / "bridge"
    pkg.mkdir()
    up = selfupdate.SelfUpdater(str(pkg), str(tmp_path / "s.json"), is_idle=lambda: True,
                                runner=lambda path: called.append(path) or _Done(0))
    up.run_installer("/tmp/whatever")
    assert called == ["/tmp/whatever"]


class _Done:
    def __init__(self, rc):
        self.returncode = rc
        self.stdout = ""
        self.stderr = ""
