"""第 12 轮 M：常驻菜单自愈 —— 装机那次没写成，桥跑起来之后自己补上；成功一次就永远不再写。

## 为什么有这组测试

同事按文档装完、也发了版本，飞书单聊里就是没有输入框上方那排按钮。菜单原来只在装机那一刻写一次，
那一刻 `application:application:patch` 还在等审批 ⇒ 写不成，之后再也没人写。

🔴 所有飞书接口都走注入的假 http；真 `urllib.request.urlopen` 和 `botmenu._http` 换成一碰就炸的桩：
**一个真请求都不发**（本机那个飞书应用的写接口更不能碰）。时间全用假时钟，不真等。
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.request

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bridge import botmenu, menu_heal  # noqa: E402

APP = "cli_test_only_000"
OTHER_APP = "cli_test_only_111"
DENIED_PATCH = {
    "code": 99991672,
    "msg": "Access denied",
    "error": {"permission_violations": [{"subject": "application:application:patch"}]},
}


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """真网络一碰就炸：漏注入 http 的地方会当场红，而不是悄悄打到飞书。"""
    def boom(*_a, **_k):
        raise AssertionError("测试里发了真网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(botmenu, "_http", boom)


@pytest.fixture
def here_dir():
    """落盘夹具放在测试目录旁边，**不放 tmp_path**（macOS 的 tmp 是 pathguard 豁免前缀，见共用纪律）。"""
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".menuheal-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class FakeHttp:
    """按 (method, path 后缀) 回包，值可以是一串（依次用，用完停在最后一个）。记下每一次调用。"""

    def __init__(self, routes: dict | None = None):
        self.routes = {k: (list(v) if isinstance(v, list) else [v]) for k, v in (routes or {}).items()}
        self.calls: list[tuple] = []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, token, body))
        if path.startswith("/auth/"):
            key = "token"
        elif path.endswith("/ability"):
            key = "menu"
        elif path.endswith("/config"):
            key = "subscribe"
        elif path.endswith("/publish"):
            key = "publish"
        else:
            key = f"{method} {path}"
        queue = self.routes.get(key)
        if queue:
            out = queue.pop(0) if len(queue) > 1 else queue[0]
            return out
        if key == "token":
            return {"code": 0, "tenant_access_token": "t-fake"}
        if key == "publish":
            return {"code": 0, "data": {"version": "1.0.1", "version_id": "v1"}}
        return {"code": 0, "data": {}}

    def steps(self) -> list[str]:
        out = []
        for method, path, _t, _b in self.calls:
            if path.startswith("/auth/"):
                out.append("token")
            elif path.endswith("/ability"):
                out.append("menu")
            elif path.endswith("/config"):
                out.append("subscribe")
            elif path.endswith("/publish"):
                out.append("publish")
            else:
                out.append(f"{method} {path}")
        return out


class FakeClock:
    """假时钟：`sleep` 只把时间往前拨，并记下每次等了多久。"""

    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _heal(here_dir, http, clock, app=APP):
    return menu_heal.MenuHeal(app, "secret-not-real", str(here_dir / menu_heal.STATE_FILENAME),
                              http=http, clock=clock)


class _StopLoop(BaseException):
    """BaseException：run_forever 兜的是 Exception，这两个不能被它吞掉。"""


class _Hammering(BaseException):
    pass


def _run_until(heal, clock, max_sleeps: int):
    """跑 run_forever，最多让它睡 max_sleeps 次、试 max_sleeps+1 次（防死循环）。返回是否自己退出。

    🔴 两样都要封顶：只数 sleep 的话，「失败后不等就重试」那种坏实现一次 sleep 都不调 ⇒ 测试卡死而不是红
    （变异验证时真卡住过）。
    """
    real_attempt = heal.attempt
    tries = {"n": 0}

    def sleep(seconds):
        if len(clock.slept) >= max_sleeps:
            raise _StopLoop
        clock.sleep(seconds)

    def attempt():
        tries["n"] += 1
        if tries["n"] > max_sleeps + 1:
            raise _Hammering
        return real_attempt()

    heal.attempt = attempt
    try:
        heal.run_forever(sleep=sleep)
    except _StopLoop:
        return False
    except _Hammering:
        pytest.fail(f"试了 {tries['n']} 次却只等了 {len(clock.slept)} 次 ⇒ 在狂打飞书")
    finally:
        heal.attempt = real_attempt
    return True


# ---------- 节奏：1 分钟后第一次 / 失败 30 分钟后再试 / 成功后不再跑 ----------


def test_first_attempt_one_minute_after_start(here_dir):
    clock = FakeClock()
    http = FakeHttp()
    heal = _heal(here_dir, http, clock)
    assert heal.next_delay() == menu_heal.FIRST_DELAY_SEC == 60
    assert http.calls == [], "构造时就打了飞书"
    assert _run_until(heal, clock, max_sleeps=5) is True
    assert clock.slept == [60]
    assert http.steps() == ["token", "menu", "subscribe", "publish"]


def test_failure_waits_thirty_minutes_then_success_stops(here_dir):
    clock = FakeClock()
    http = FakeHttp({"menu": [DENIED_PATCH, DENIED_PATCH, {"code": 0}]})
    heal = _heal(here_dir, http, clock)
    assert _run_until(heal, clock, max_sleeps=10) is True, "成功后线程没退出"
    assert clock.slept == [60, 1800, 1800]
    assert http.steps().count("menu") == 3
    assert http.steps().count("publish") == 1, "失败的那两次不该发版"
    assert heal.done


def test_missing_permission_never_hammers_feishu(here_dir):
    """一直缺权限：每 30 分钟打一次，绝不更密（原需求：别每 10 秒打飞书）。"""
    clock = FakeClock()
    http = FakeHttp({"menu": DENIED_PATCH})
    heal = _heal(here_dir, http, clock)
    assert _run_until(heal, clock, max_sleeps=6) is False
    assert clock.slept[0] == 60 and all(s == 1800 for s in clock.slept[1:])
    assert http.steps().count("menu") == 6
    assert "publish" not in http.steps()


def test_after_success_a_restart_sends_nothing(here_dir):
    """🔴 成功一次就永远不再写：看门狗重启后新进程一个请求都不发、不发版。"""
    clock = FakeClock()
    assert _heal(here_dir, FakeHttp(), clock).attempt() is True

    http = FakeHttp()
    again = _heal(here_dir, http, clock)
    assert again.done and again.next_delay() is None
    assert _run_until(again, clock, max_sleeps=3) is True
    assert http.calls == [] and clock.slept == []
    assert again.attempt() is True and http.calls == []


def test_restart_after_failure_keeps_the_thirty_minute_cadence(here_dir):
    """看门狗反复重启不能把重试节奏打快：上次失败 5 分钟后重启 ⇒ 还要等 25 分钟。"""
    clock = FakeClock()
    first = _heal(here_dir, FakeHttp({"menu": DENIED_PATCH}), clock)
    assert first.attempt() is False

    clock.now += 5 * 60
    second = _heal(here_dir, FakeHttp(), clock)
    assert second.next_delay() == pytest.approx(25 * 60)
    assert "缺权限" in second.health_text(), "重启后诊断卡忘了上次为什么失败"


def test_restart_long_after_failure_waits_only_the_first_minute(here_dir):
    clock = FakeClock()
    assert _heal(here_dir, FakeHttp({"menu": DENIED_PATCH}), clock).attempt() is False
    clock.now += 3 * 3600
    assert _heal(here_dir, FakeHttp(), clock).next_delay() == 60


def test_clock_jumped_backwards_waits_at_most_one_interval(here_dir):
    clock = FakeClock()
    assert _heal(here_dir, FakeHttp({"menu": DENIED_PATCH}), clock).attempt() is False
    clock.now -= 10 * 86400
    assert _heal(here_dir, FakeHttp(), clock).next_delay() <= menu_heal.RETRY_EVERY_SEC


# ---------- 失败不发版 ----------


def test_menu_write_denied_does_not_publish(here_dir):
    http = FakeHttp({"menu": DENIED_PATCH})
    heal = _heal(here_dir, http, FakeClock())
    assert heal.attempt() is False
    assert http.steps() == ["token", "menu"]
    assert not menu_heal.is_done(heal.state_path, APP)


def test_subscribe_failure_does_not_publish(here_dir):
    """菜单写进去了但点击没订阅上 ⇒ 菜单出来了点了没反应；不发版，下次连菜单一起重来。"""
    http = FakeHttp({"subscribe": {"code": 210040, "msg": "reviewing"}})
    heal = _heal(here_dir, http, FakeClock())
    assert heal.attempt() is False
    assert "publish" not in http.steps()
    assert "订阅" in heal.health_text()


def test_publish_failure_is_retried_not_marked(here_dir):
    clock = FakeClock()
    http = FakeHttp({"publish": [{"code": 210302, "msg": "pending"}, {"code": 0, "data": {"version": "1.2.3"}}]})
    heal = _heal(here_dir, http, clock)
    assert heal.attempt() is False and not heal.done
    assert not menu_heal.is_done(heal.state_path, APP)
    assert heal.next_delay() == 1800
    assert heal.attempt() is True
    assert menu_heal._record(heal.state_path, APP)["version"] == "1.2.3"


def test_token_failure_is_a_quiet_retry(here_dir):
    http = FakeHttp({"token": {"code": 10014, "msg": "app secret invalid"}})
    heal = _heal(here_dir, http, FakeClock())
    assert heal.attempt() is False
    assert http.steps() == ["token"]
    assert "token" in heal.health_text()


def test_an_exception_does_not_kill_the_thread(here_dir):
    clock = FakeClock()
    calls = {"n": 0}

    def flaky(method, path, token, body=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return FakeHttp()(method, path, token, body)

    heal = _heal(here_dir, flaky, clock)
    assert _run_until(heal, clock, max_sleeps=5) is True
    assert clock.slept == [60, 1800]
    assert heal.done


# ---------- 标记：按 app_id、和安装器共用 ----------


def test_marker_is_per_app_id(here_dir):
    """换了机器人（新 app_id）⇒ 新机器人要补一次；旧的标记不挡它。"""
    clock = FakeClock()
    assert _heal(here_dir, FakeHttp(), clock).attempt() is True
    http = FakeHttp()
    other = _heal(here_dir, http, clock, app=OTHER_APP)
    assert not other.done
    assert other.attempt() is True and "publish" in http.steps()
    assert menu_heal.is_done(other.state_path, APP) and menu_heal.is_done(other.state_path, OTHER_APP)


def test_installer_marker_means_bridge_never_writes(here_dir):
    """安装器那次成功了（`botmenu.py` 落了标记）⇒ 桥启动后一个请求都不发。"""
    path = str(here_dir / menu_heal.STATE_FILENAME)
    menu_heal.mark_done(path, APP, "1.20261106.000000")
    http = FakeHttp()
    heal = menu_heal.MenuHeal(APP, "x", path, http=http, clock=FakeClock())
    assert heal.next_delay() is None and http.calls == []


def test_installer_marks_while_heal_is_waiting(here_dir):
    """桥等着重试时同事重装成功了 ⇒ 下一拍回读到标记，不再写、不再发第二个版本。"""
    clock = FakeClock()
    http = FakeHttp({"menu": DENIED_PATCH})
    heal = _heal(here_dir, http, clock)
    assert heal.attempt() is False
    menu_heal.mark_done(heal.state_path, APP, "1.0.9")
    before = len(http.calls)
    assert heal.attempt() is True and len(http.calls) == before


def test_corrupt_marker_file_means_try_again(here_dir):
    path = here_dir / menu_heal.STATE_FILENAME
    path.write_text("{not json", encoding="utf-8")
    heal = _heal(here_dir, FakeHttp(), FakeClock())
    assert not heal.done
    assert heal.attempt() is True and menu_heal.is_done(str(path), APP)


def test_state_path_next_to_config(here_dir):
    cfg = str(here_dir / "config.toml")
    assert menu_heal.state_path_for(cfg) == str(here_dir / "menu_heal.json")


@pytest.mark.parametrize("lines, landed", [
    (["OK=x", "OK=常驻菜单已写入", "OK=收消息和按钮点击已订阅", "OK=已提交版本 1", "PUBLISH=review"], True),
    (["OK=常驻菜单已写入", "OK=收消息和按钮点击已订阅", "PUBLISH=online"], True),
    (["OK=常驻菜单已写入", "OK=收消息和按钮点击已订阅", "PUBLISH=submitted"], True),
    (["OK=常驻菜单已写入", "OK=收消息和按钮点击已订阅", "PUBLISH=failed"], False),
    (["WARN=常驻菜单没配上：缺权限 application:application:patch", "OK=收消息和按钮点击已订阅",
      "PUBLISH=review"], False),
    (["OK=常驻菜单已写入", "WARN=按钮点击订阅失败", "PUBLISH=online"], False),
    (["WARN=常驻菜单和权限都没配上：拿不到 token", "PUBLISH=failed"], False),
])
def test_menu_landed_reads_setup_verdict(lines, landed):
    assert botmenu.menu_landed(lines) is landed


def test_menu_landed_on_real_setup_output():
    """拿 `setup()` 真跑一遍（假 http）的输出喂进去，防止两边的措辞漂了。"""
    ok = botmenu.setup(APP, "s", "1.0.0", http=FakeHttp())
    assert botmenu.menu_landed(ok) is True
    denied = botmenu.setup(APP, "s", "1.0.0", http=FakeHttp({"menu": DENIED_PATCH}))
    assert botmenu.menu_landed(denied) is False


def test_mark_installed_writes_the_shared_marker(here_dir):
    cfg = str(here_dir / "config.toml")
    botmenu._mark_installed(cfg, APP, "1.0.0")
    assert menu_heal.is_done(menu_heal.state_path_for(cfg), APP)


def test_mark_installed_works_in_script_mode(here_dir):
    """安装器用 `python bridge/botmenu.py` 脚本方式跑：没有父包，`import menu_heal` 也得能走通。"""
    import subprocess

    cfg = str(here_dir / "config.toml")
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import botmenu; "
            "botmenu._mark_installed(sys.argv[2], 'cli_x', '1.0.0')")
    out = subprocess.run([sys.executable, "-c", code, os.path.join(_ROOT, "bridge"), cfg],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and "NOTE=" not in out.stdout, out.stdout + out.stderr
    assert menu_heal.is_done(menu_heal.state_path_for(cfg), "cli_x")


# ---------- 失败要说出来：日志一行 + 诊断卡一行 ----------


def test_missing_permission_logs_one_line_with_the_scope(here_dir, caplog):
    heal = _heal(here_dir, FakeHttp({"menu": DENIED_PATCH}), FakeClock())
    with caplog.at_level(logging.WARNING, logger="feishu-bridge"):
        heal.attempt()
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1
    text = warns[0].getMessage()
    assert "application:application:patch" in text and "30 分钟" in text and "没发版本" in text
    assert f"https://open.feishu.cn/app/{APP}/auth?q=application:application:patch" in text, "日志里没给手动开权限的链接"


def test_health_text_states(here_dir):
    clock = FakeClock()
    heal = _heal(here_dir, FakeHttp({"menu": [DENIED_PATCH, {"code": 0}]}), clock)
    assert heal.health_text().startswith("⏳")
    heal.attempt()
    assert heal.health_text() == "❌ 缺权限 application:application:patch · 每 30 分钟自动再试"
    heal.attempt()
    assert heal.health_text() == "✅ 已配好"


def test_health_text_never_carries_the_secret(here_dir):
    heal = menu_heal.MenuHeal(APP, "SECRET-should-not-leak", str(here_dir / "m.json"),
                              http=FakeHttp({"menu": DENIED_PATCH}), clock=FakeClock())
    heal.attempt()
    assert "SECRET" not in heal.health_text()
    assert "SECRET" not in (here_dir / "m.json").read_text(encoding="utf-8")


# ---------- 调用点：真的接上了（查调用形状，不查名字）----------


def test_call_site_attempt_reuses_botmenu_writers():
    """写菜单 / 订阅 / 发版复用 botmenu 那一份，不另写；发版排在写菜单和订阅之后。"""
    src = _read(os.path.join(_ROOT, "bridge", "menu_heal.py"))
    body = src[src.index("    def attempt("):src.index("    def _failed_with(")]
    for shape in ("bm.write_menu(self.app_id, token, http=call)",
                  "bm.subscribe(self.app_id, token, http=call)",
                  "bm.publish(self.app_id, token, version,",
                  "mark_done(self.state_path, self.app_id,"):
        assert shape in body, f"attempt() 里没调 {shape}"
    assert body.index("bm.write_menu(") < body.index("bm.subscribe(") < body.index("bm.publish(")
    assert "/ability" not in src, "menu_heal 里自己拼了写菜单的接口 = 另写了一份"


def test_call_site_run_forever_calls_attempt():
    src = _read(os.path.join(_ROOT, "bridge", "menu_heal.py"))
    body = src[src.index("    def run_forever("):src.index("    def attempt(")]
    assert "self.attempt()" in body and "sleep(delay)" in body


def test_call_site_installer_entry_marks_on_success():
    src = _read(os.path.join(_ROOT, "bridge", "botmenu.py"))
    main = src[src.index('if __name__ == "__main__":'):]
    assert "if menu_landed(_lines):" in main
    assert '_mark_installed(sys.argv[1], feishu.get("app_id", ""), _version)' in main
    assert "_lines.append(line)" in main


# 2026-11-06 主会话落了 app.py / status.py 接线，xfail 去掉
def test_call_site_app_starts_heal_thread():
    src = _read(os.path.join(_ROOT, "bridge", "app.py"))
    run = src[src.index("    def run(self) -> None:"):src.index("    def _notify_waiting_loop(")]
    assert "target=self._menu_heal_loop" in run
    assert "menu_heal.MenuHeal(" in src
    assert "self.menu_heal.run_forever()" in src


# 2026-11-06 主会话落了 app.py / status.py 接线，xfail 去掉
def test_call_site_diagnostics_shows_menu_line():
    from bridge import status

    assert "menu" in dict(status.HEALTH_FIELDS)
    src = _read(os.path.join(_ROOT, "bridge", "app.py"))
    body = src[src.index("    def _collect_health("):src.index("    def _scan_log_health(")]
    assert 'data["menu"] = heal.health_text()' in body
