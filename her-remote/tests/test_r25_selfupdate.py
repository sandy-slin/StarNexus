"""第 25 轮 P-sre：自动更新几条用户会撞上的。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from test_r14_selfupdate import BASE, FILES, PKG, _done, _latest, _updater  # noqa: E402


def test_unreachable_source_is_not_called_up_to_date(tmp_path):
    """🔴 更新源连不上 ⇒ 原来 last_error 空 ⇒「已经是最新的了」。"""
    up = _updater(tmp_path, {})
    assert up.check() is None
    assert up.last_error and "查不到" in up.last_error


def test_recent_failure_says_so_and_manual_bypasses_cooldown(tmp_path):
    """🔴 下载断了记成失败 ⇒ 冷却期内原来也说「已是最新」；他亲手发「更新」应该能马上再试。"""
    files = dict(FILES)
    files[f"{BASE}/feishu-agent-mac.command"] = PKG[:5]           # 下载一半
    up = _updater(tmp_path, files)
    ok, _ = up.update_now(up.check())
    assert not ok
    files[f"{BASE}/feishu-agent-mac.command"] = PKG               # 网恢复了
    assert up.check() is None and "装失败" in up.last_error
    assert up.check(manual=True) is not None, "手动「更新」还被冷却挡着"


def test_half_installed_build_is_retried(tmp_path):
    """安装器写完版本号后失败 ⇒ 版本号已是新的，原来永远不再试。"""
    def half(path):
        (tmp_path / "bridge" / "buildinfo.py").write_text('BUILD_ID = "20261106-newbuild"\n')
        return _done(1)
    up = _updater(tmp_path, FILES, runner=half)
    ok, _ = up.update_now(up.check())
    assert not ok and up.current_build() == "20261106-newbuild"
    assert up.check(manual=True) is not None, "装到一半的版本再也不会被重装"


def test_installing_flag_is_up_while_installer_runs_and_down_after(tmp_path):
    seen = []
    up = None

    def runner(path):
        seen.append(up.installing)
        return _done(1)
    up = _updater(tmp_path, FILES, runner=runner)
    up.update_now(up.check())
    assert seen == [True] and up.installing is False


def test_not_idle_after_download_does_not_install(tmp_path):
    calls = []
    state = {"idle": True}
    up = _updater(tmp_path, FILES, runner=lambda p: calls.append(p) or _done(0))
    info = up.check()
    up.is_idle = lambda: False                    # 下载那几秒里来了活
    ok, _ = up.update_now(info)
    assert not ok and calls == []


def test_successful_install_whose_process_got_replaced_is_recorded_ok(tmp_path):
    """安装器换掉进程 ⇒ 成功那条路的收尾跑不到；新进程起来要认出来「装成功了」，不再重装。"""
    def replaced(path):
        (tmp_path / "bridge" / "buildinfo.py").write_text('BUILD_ID = "20261106-newbuild"\n')
        raise SystemExit(0)                       # 模拟进程被换掉：后面一行都不跑
    up = _updater(tmp_path, FILES, runner=replaced)
    try:
        up.update_now(up.check())
    except SystemExit:
        pass
    fresh = _updater(tmp_path, FILES, current="20261106-newbuild")
    assert fresh.check() is None and fresh.last_error == ""
    assert fresh._state()["20261106-newbuild"]["outcome"] == "ok"


def test_bridge_refuses_new_turn_while_installing(tmp_path):
    import asyncio
    import types

    from test_interference import SCOPE, _fake_bridge

    b = _fake_bridge(str(tmp_path))
    said = []
    b._say = lambda c, t: said.append(t)
    b.workspaces = types.SimpleNamespace(get=lambda s: "/tmp")
    b.updater = types.SimpleNamespace(installing=True)
    ran = []

    async def run_one(*a):
        ran.append(1)
    b._run_one = run_one
    asyncio.run(b._run_locally(SCOPE, "chat1", "看看桌面"))
    assert ran == [] and "装新版" in said[-1]
