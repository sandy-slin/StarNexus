"""第 32 轮 UPDATESAFE：新版装坏了自动退回 + 没装上要说出来。

照 T3 Code 两处思路（不抄码）：
- `updateMachine.ts:220-226`：`fromVersion === appVersion && targetVersion !== appVersion` ⇒ 没装上。
- `serviceLauncher.ts`：试跑新版，没报好就回滚。

盯的几条：
1. 开装前记 from；重启后 from == 当前 且 目标 != 当前 ⇒ not_applied。from != 当前 ⇒ 不判。
2. not_applied 的那一版：自动不重装，手动「更新」能装；主人收到一句话。
3. 上报 `u` 新词 not_applied，取最近一次尝试的结果。
4. fleet-report 认得新词、单独列「没装上」。
5. 安装器：写新码前 bridge/ → bridge.prev/；自动更新没连上 ⇒ 换回 + 记 rolled_back；连上 ⇒ 删 prev；
   手动装不退回。（**跑安装器里那几段原文**，不重写逻辑。）
6. plist 有 ExitTimeOut / ProcessType Interactive。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import types
import urllib.parse
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

from bridge import selfupdate  # noqa: E402
from test_r14_selfupdate import BASE, FILES, _done, _latest, _opener  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
MAC_SRC = os.path.join(_ONECLICK, "src", "install-mac.sh")
FLEET_SRC = os.path.join(_ONECLICK, "fleet-report.py")

OLD = "20261106-oldbuild"
NEW = "20261106-newbuild"


def _up(tmp_path, *, current=OLD, files=None, seen=None, runner=None, clock=None, idle=True):
    pkg = tmp_path / "bridge"
    pkg.mkdir(exist_ok=True)
    (pkg / "buildinfo.py").write_text(f'BUILD_ID = "{current}"\n', encoding="utf-8")
    clock = clock if clock is not None else {"t": 1000.0}
    return selfupdate.SelfUpdater(
        str(pkg), str(tmp_path / "update_state.json"),
        is_idle=lambda: idle, opener=_opener(files if files is not None else FILES, seen),
        runner=runner or (lambda p: _done(0)), now=lambda: clock["t"],
        app_id="cli_a1b2c3d4e5f60708",
    )


def _u(seen) -> list:
    url = [u for u in seen if u.split("?", 1)[0].endswith("/latest.json")][-1]
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)["u"]


def _install_died_mid_way(tmp_path):
    """安装器跑起来、把桥换掉之前就死了（或新版起不来被 launchd 拉回旧码）⇒ 盘上还是旧版。"""
    def died(path):
        raise SystemExit(0)            # 进程被换掉：update_now 后面一行都不跑
    up = _up(tmp_path, runner=died)
    with pytest.raises(SystemExit):
        up.update_now(up.check())
    return up


# ---------- 1. not_applied 判定 ----------


def test_installing_record_carries_from(tmp_path):
    up = _install_died_mid_way(tmp_path)
    rec = up._state()[NEW]
    assert rec["outcome"] == "installing" and rec["from"] == OLD


def test_restart_still_on_old_build_is_not_applied(tmp_path):
    _install_died_mid_way(tmp_path)
    fresh = _up(tmp_path, current=OLD)          # 重启：还是旧版
    assert fresh.check() is None
    assert fresh._state()[NEW]["outcome"] == "not_applied"
    assert "没装上" in fresh.last_error and OLD in fresh.last_error


def test_from_not_current_is_not_judged(tmp_path):
    """from 记的是别的版本（这台后来换过版）⇒ 判不了，不许硬判 not_applied。"""
    up = _up(tmp_path, current=OLD)
    up._remember(NEW, "installing", **{"from": "20261101-somethingelse"})
    up._reconcile(OLD)
    assert up._state()[NEW]["outcome"] == "installing"


def test_legacy_installing_without_from_is_left_alone(tmp_path):
    """第 32 轮之前写的记录没有 from ⇒ 不猜。"""
    up = _up(tmp_path, current=OLD)
    up._remember(NEW, "installing")
    up._reconcile(OLD)
    assert up._state()[NEW]["outcome"] == "installing"


def test_installed_build_with_from_still_counts_ok(tmp_path):
    """阳性对照：真装上了（现在就是目标版）⇒ ok，不是 not_applied。"""
    _install_died_mid_way(tmp_path)
    fresh = _up(tmp_path, current=NEW)
    assert fresh.check() is None and fresh.last_error == ""
    assert fresh._state()[NEW]["outcome"] == "ok"


def test_installer_rolled_back_record_becomes_not_applied(tmp_path):
    up = _up(tmp_path, current=OLD)
    up._remember(NEW, "rolled_back", "60 秒内没连上飞书", **{"from": OLD})
    assert up.check() is None
    assert up._state()[NEW]["outcome"] == "not_applied"


def test_reconcile_skipped_while_this_process_is_installing(tmp_path):
    """手动「更新」装着的时候自动那一拍来查 ⇒ 那条 installing 是活的，别判成没装上。"""
    up = _up(tmp_path, current=OLD)
    up._remember(NEW, "installing", **{"from": OLD})
    up.installing = True
    up._reconcile(OLD)
    assert up._state()[NEW]["outcome"] == "installing"


# ---------- 2. 自动不重装、手动能装、告诉主人 ----------


def test_auto_does_not_reinstall_not_applied_even_after_cooldown(tmp_path):
    clock = {"t": 1000.0}
    ran = []
    up = _up(tmp_path, clock=clock, runner=lambda p: ran.append(p) or _done(0))
    up._remember(NEW, "installing", **{"from": OLD})
    clock["t"] += 7 * 3600                         # 原来 6 小时冷却一过就重装
    assert up.tick() is None and ran == []
    assert up.check() is None


def test_manual_update_installs_not_applied(tmp_path):
    ran = []
    up = _up(tmp_path, runner=lambda p: ran.append(p) or _done(0))
    up._remember(NEW, "not_applied", **{"from": OLD})
    info = up.check(manual=True)
    assert info is not None and info["build_id"] == NEW
    ok, _ = up.update_now(info)
    assert ok and len(ran) == 1


def test_newer_build_after_not_applied_is_still_auto_installed(tmp_path):
    """只挡没装上的**那一版**；服务端发了更新的一版 ⇒ 照常自动装。"""
    files = dict(FILES)
    files[f"{BASE}/latest.json"] = _latest(build="20261107-newest")
    ran = []
    up = _up(tmp_path, files=files, runner=lambda p: ran.append(p) or _done(0))
    up._remember(NEW, "not_applied", **{"from": OLD})
    assert up.tick()[0] is True and len(ran) == 1


def test_notice_text_and_told_once(tmp_path):
    _install_died_mid_way(tmp_path)
    fresh = _up(tmp_path, current=OLD)
    fresh.check()
    build, text = fresh.take_notice()
    assert build == NEW
    assert text == f"新版 {NEW} 没装上，还在用 {OLD}。发「更新」再试一次。"
    fresh.mark_told(build)
    assert fresh.take_notice() is None


def test_installer_rc_nonzero_after_rollback_keeps_not_applied(tmp_path):
    """安装器自己退回后退出码非 0 ⇒ 不许被 fail 盖掉（fail 6 小时后会自动重装）。"""
    up = None

    def rolled(path):
        state = json.loads((tmp_path / "update_state.json").read_text())
        state[NEW] = {"outcome": "rolled_back", "at": 1000.0, "from": OLD}
        (tmp_path / "update_state.json").write_text(json.dumps(state))
        return _done(1)
    up = _up(tmp_path, runner=rolled)
    ok, text = up.update_now(up.check())
    assert not ok and "退回" in text
    assert up._state()[NEW]["outcome"] == "not_applied"
    assert up.last_error == "rolled_back"


def _bridge_with(updater, owners=("ou_owner1",), say_ok=True):
    from bridge.app import Bridge

    b = Bridge.__new__(Bridge)
    b.settings = types.SimpleNamespace(app_id="cli_a1b2c3d4e5f60708", owner_open_ids=frozenset(owners))
    b.updater = updater
    said = []
    b._say = lambda chat, text: said.append((chat, text)) or say_ok
    return b, said


def test_bridge_tells_owner_once(tmp_path):
    _install_died_mid_way(tmp_path)
    up = _up(tmp_path, current=OLD)
    up.check()
    b, said = _bridge_with(up)
    b._tell_update_not_applied()
    b._tell_update_not_applied()
    assert said == [("ou_owner1", f"新版 {NEW} 没装上，还在用 {OLD}。发「更新」再试一次。")]


def test_bridge_retries_notice_when_send_fails(tmp_path):
    up = _up(tmp_path, current=OLD)
    up._remember(NEW, "not_applied", **{"from": OLD})
    up._state()  # noqa
    state = up._state()
    state[NEW]["told"] = False
    up._write_state(state)
    b, said = _bridge_with(up, say_ok=False)
    b._tell_update_not_applied()
    assert up.take_notice() is not None, "没发出去就记成「说过」了"


def test_selfupdate_loop_calls_notice(monkeypatch):
    import bridge.app as app_mod

    class Stop(Exception):
        pass
    sleeps = []

    def fake_sleep(sec):
        sleeps.append(sec)
        if len(sleeps) > 1:
            raise Stop
    monkeypatch.setattr(app_mod.time, "sleep", fake_sleep)
    told = []
    up = types.SimpleNamespace(app_id="x", tick=lambda: None,
                               take_notice=lambda: ("B", "新版 B 没装上"), mark_told=told.append)
    b, said = _bridge_with(up)
    with pytest.raises(Stop):
        b._selfupdate_loop()
    assert said and told == ["B"]


def test_on_demand_rollback_reply_counts_as_told(tmp_path):
    def rolled(path):
        state = json.loads((tmp_path / "update_state.json").read_text())
        state[NEW] = {"outcome": "rolled_back", "at": 1000.0, "from": OLD}
        (tmp_path / "update_state.json").write_text(json.dumps(state))
        return _done(1)
    up = _up(tmp_path, runner=rolled)
    b, said = _bridge_with(up)
    b._update_on_demand("oc_chat")
    assert "退回" in said[-1][1]
    assert up.take_notice() is None, "手动那句已经说了，循环会再说一遍"


# ---------- 3. 上报 u ----------


def test_u_reports_not_applied(tmp_path):
    _install_died_mid_way(tmp_path)
    seen = []
    fresh = _up(tmp_path, current=OLD, seen=seen)
    fresh._remember(OLD, "ok")          # 旧版自己以前装成功过（原来 u 取这条 ⇒ 「ok」）
    state = fresh._state()
    state[OLD]["at"] = 1.0              # 比这次尝试早
    fresh._write_state(state)
    fresh.check()
    assert _u(seen) == ["not_applied"]


def test_u_only_allows_known_words(tmp_path):
    seen = []
    up = _up(tmp_path, seen=seen)
    up._remember(NEW, "rolled_back")    # 本地中间态：落定前不许原样上报
    up._remember(NEW, "something-weird")
    up.check()
    assert _u(seen) == [""]


def test_report_keys_unchanged():
    assert selfupdate.REPORT_KEYS == ("v", "id", "os", "u", "app")


# ---------- 4. fleet-report ----------


def _fleet():
    spec = importlib.util.spec_from_file_location("fleet_report_r32", FLEET_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fleet_report_accepts_not_applied_and_lists_it():
    from datetime import datetime, timedelta, timezone

    fr = _fleet()
    assert fr.FIELD_RE["u"].match("not_applied")
    assert not fr.FIELD_RE["u"].match("rolled_back")
    line = ('1.2.3.4 - - [09/Nov/2026:00:13:51 +0800] "GET /ccdl/feishu-agent/latest.json?'
            'v=20261106-abc1234&id=0123456789abcdef&os=mac&u=not_applied&app=cli_abc HTTP/1.1" 200 368 "-" "UA"')
    rec = fr.parse_line(line)
    assert rec and rec["kind"] == "report" and rec["u"] == "not_applied"
    now = datetime(2026, 11, 9, 1, 0, tzinfo=timezone(timedelta(hours=8)))
    rep = fr.build_report([rec], "20261108-def5678", {}, now)
    assert rep["not_applied"] == ["0123456789abcdef"]
    assert rep["failed_mac"] == []
    text = fr.render_text(rep, {})
    assert "没装上（还在用旧版" in text and "…abcdef" in text.split("没装上（")[1]
    assert fr.bitable_row(rep["machines"][0])["上次更新"] == "没装上"


# ---------- 5. 安装器回滚段（跑安装器原文） ----------


def _mac() -> str:
    return pathlib.Path(MAC_SRC).read_text(encoding="utf-8")


def _between(text: str, a: str, b: str) -> str:
    i = text.index(a)
    return text[i:text.index(b, i)]


def _svc_helpers(text: str) -> str:
    out = []
    for name in ("_svc_stop_for_rollback", "_svc_start_for_rollback"):
        m = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.S | re.M)
        assert m, f"安装器里找不到 {name}"
        out.append(m.group(0))
    return "\n".join(out)


_FAKE_LAUNCHCTL = """#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case "$1" in
  print) exit 113 ;;      # 服务不在（bootout 后立刻就没了）
  *) exit 0 ;;
esac
"""


@pytest.fixture
def home():
    """夹具建在测试目录旁边，不用 tmp_path（同 test_service_alive_verdict.py 的纪律）。"""
    base = pathlib.Path(_HERE).parent / f".r32upd-tmp-{uuid.uuid4().hex[:8]}"
    (base / "app" / "venv" / "bin").mkdir(parents=True)
    (base / "bin").mkdir()
    os.symlink(sys.executable, base / "app" / "venv" / "bin" / "python")
    lc = base / "bin" / "launchctl"
    lc.write_text(_FAKE_LAUNCHCTL, encoding="utf-8")
    lc.chmod(0o755)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _write_bridge(d: pathlib.Path, build: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "buildinfo.py").write_text(f'BUILD_ID = "{build}"\n', encoding="utf-8")


def _run(home, body: str, *, auto: bool) -> subprocess.CompletedProcess:
    pre = (
        "set -euo pipefail\n"
        f'PATH="{home}/bin:$PATH"\n'
        f'export FAKE_LOG="{home}/launchctl.log"\n'
        f'APP_HOME="{home}/app"\nVENV="$APP_HOME/venv"\n'
        f'PLIST="{home}/fake.plist"\nLABEL="fake.label"\n'
        "RED=''; NC=''\n"
        "die(){ printf '%s\\n' \"DIE $1\" >&2; exit 1; }\n"
        + ("export CARHER_AUTO_UPDATE=1\n" if auto else "unset CARHER_AUTO_UPDATE\n")
    )
    return subprocess.run(["/bin/bash", "-c", pre + body], capture_output=True, text=True, timeout=60)


def _step3_then_step6(connected: bool) -> str:
    text = _mac()
    step3 = _between(text, "# UPDATE-ROLLBACK-BEGIN", "# UPDATE-ROLLBACK-END")
    verdict = _between(text, "# UPDATE-VERDICT-BEGIN", "# UPDATE-VERDICT-END")
    return (
        step3
        # 第 3 步写新码（模拟）
        + f'\nmkdir -p "$APP_HOME/bridge"\nprintf \'BUILD_ID = "{NEW}"\\n\' > "$APP_HOME/bridge/buildinfo.py"\n'
        + _svc_helpers(text) + "\n"
        + "SVC_RESTARTED=1\n"
        + f"CONNECTED={'true' if connected else 'false'}\nCONNECT_WAIT_SEC=60\n"
        + verdict
        + '\necho "SCREEN_REACHED"\n'
    )


def _build(home, name="bridge") -> str:
    p = home / "app" / name / "buildinfo.py"
    return p.read_text().split('"')[1] if p.exists() else ""


def test_auto_update_not_connected_rolls_back(home):
    _write_bridge(home / "app" / "bridge", OLD)
    proc = _run(home, _step3_then_step6(connected=False), auto=True)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "SCREEN_REACHED" not in proc.stdout
    assert _build(home) == OLD, "目录没换回来"
    assert not (home / "app" / "bridge.prev").exists()
    assert not (home / "app" / "bridge.bad").exists()
    state = json.loads((home / "app" / "update_state.json").read_text())
    assert state[NEW]["outcome"] == "rolled_back" and state[NEW]["from"] == OLD
    calls = (home / "launchctl.log").read_text().splitlines()
    assert any(c.startswith("bootout") for c in calls) and any(c.startswith("bootstrap") for c in calls)
    assert [c.split()[0] for c in calls].index("bootout") < [c.split()[0] for c in calls].index("bootstrap")
    assert "换回原来那版" in proc.stderr


def test_rolled_back_state_is_read_by_selfupdate(home):
    """安装器写的那条 rolled_back ⇒ 桥起来后 SelfUpdater 认出是 not_applied、自动不再装。"""
    _write_bridge(home / "app" / "bridge", OLD)
    _run(home, _step3_then_step6(connected=False), auto=True)
    ran = []
    up = selfupdate.SelfUpdater(
        str(home / "app" / "bridge"), str(home / "app" / "update_state.json"),
        is_idle=lambda: True, opener=_opener(FILES), runner=lambda p: ran.append(p) or _done(0),
    )
    assert up.tick() is None and ran == []
    assert up._state()[NEW]["outcome"] == "not_applied"
    assert up.take_notice()[0] == NEW


def test_auto_update_connected_deletes_prev(home):
    _write_bridge(home / "app" / "bridge", OLD)
    proc = _run(home, _step3_then_step6(connected=True), auto=True)
    assert proc.returncode == 0, proc.stderr
    assert "SCREEN_REACHED" in proc.stdout
    assert _build(home) == NEW
    assert not (home / "app" / "bridge.prev").exists()
    assert not (home / "app" / "update_state.json").exists()


def test_manual_install_not_connected_does_not_roll_back(home):
    _write_bridge(home / "app" / "bridge", OLD)
    proc = _run(home, _step3_then_step6(connected=False), auto=False)
    assert proc.returncode == 0, proc.stderr
    assert "SCREEN_REACHED" in proc.stdout, "手动装没连上应该照旧走到最后一屏把原因摆出来"
    assert _build(home) == NEW
    assert not (home / "app" / "update_state.json").exists()


def test_first_install_has_nothing_to_roll_back(home):
    proc = _run(home, _step3_then_step6(connected=False), auto=True)
    assert proc.returncode == 0 and "SCREEN_REACHED" in proc.stdout, proc.stderr
    assert _build(home) == NEW


def test_auto_update_die_before_restart_restores_dir_without_touching_service(home):
    """第 3~5 步 die（服务还跑着旧码）⇒ 换回目录，不 bootout/bootstrap。"""
    _write_bridge(home / "app" / "bridge", OLD)
    text = _mac()
    body = (_between(text, "# UPDATE-ROLLBACK-BEGIN", "# UPDATE-ROLLBACK-END")
            + f'\nmkdir -p "$APP_HOME/bridge"\nprintf \'BUILD_ID = "{NEW}"\\n\' > "$APP_HOME/bridge/buildinfo.py"\n'
            + 'die "✗ 第 5 步失败"\n')
    proc = _run(home, body, auto=True)
    assert proc.returncode == 1 and "第 5 步失败" in proc.stderr
    assert _build(home) == OLD
    assert not (home / "launchctl.log").exists(), "服务还跑着旧码，不该去停 / 起"
    state = json.loads((home / "app" / "update_state.json").read_text())
    assert state[NEW]["outcome"] == "rolled_back"


def test_rename_happens_before_any_new_code_is_written():
    text = _mac()
    mv_at = text.index('mv -- "$APP_HOME/bridge" "$PREV_BRIDGE"')
    first_write = text.index('write_file "$APP_HOME/bridge/')
    assert mv_at < first_write
    assert text.index("# UPDATE-VERDICT-BEGIN") > text.index('CONNECTED=false')
    assert text.index("# UPDATE-VERDICT-BEGIN") < text.index('if [ "$CONNECTED" = true ]; then\n  ok "   装好了')


# ---------- 6. plist 新键 ----------


def test_plist_has_exit_timeout_and_interactive():
    text = _mac()
    plist = _between(text, 'cat > "$PLIST" <<PLISTEOF', "\nPLISTEOF")
    assert "<key>ExitTimeOut</key><integer>30</integer>" in plist
    assert "<key>ProcessType</key><string>Interactive</string>" in plist


@pytest.mark.skipif(shutil.which("plutil") is None, reason="要 macOS 的 plutil")
def test_rendered_plist_is_valid(tmp_path):
    text = _mac()
    block = _between(text, 'cat > "$PLIST" <<PLISTEOF', "\nPLISTEOF") + "\nPLISTEOF\n"
    out = tmp_path / "x.plist"
    script = (f'PLIST="{out}"; LABEL=l; VENV=/v; APP_HOME=/a; CLAUDE_BIN=/c/claude; CONFIG=/a/c; LOG_DIR=/a/l\n'
              + block)
    subprocess.run(["/bin/bash", "-c", script], check=True, timeout=30)
    lint = subprocess.run(["plutil", "-lint", str(out)], capture_output=True, text=True)
    assert lint.returncode == 0, lint.stdout + lint.stderr
    conv = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(out)], capture_output=True, text=True)
    data = json.loads(conv.stdout)
    assert data["ExitTimeOut"] == 30 and data["ProcessType"] == "Interactive"


def test_installer_output_goes_to_a_file_not_a_pipe(tmp_path, monkeypatch):
    """🔴 第 33 轮：安装器会把父进程（旧桥）换掉；输出接管道 ⇒ 读端没了 ⇒ 安装器下一次 echo 吃 SIGPIPE 死掉，
    「连上删 bridge.prev / 没连上换回旧版」那段永远跑不到（本机 11-09 21:57 现场 bridge.prev 一直留着）。"""
    import subprocess as sp

    from bridge import selfupdate

    seen = {}

    def fake_run(*a, **k):
        seen.update(k)
        k["stdout"].write("installer said hi\n")
        return sp.CompletedProcess(a[0], 0)

    monkeypatch.setattr(selfupdate.subprocess, "run", fake_run)
    monkeypatch.setattr(selfupdate.platform, "system", lambda: "Darwin")
    up = selfupdate.SelfUpdater(str(tmp_path / "bridge"), str(tmp_path / "update_state.json"), is_idle=lambda: True)
    proc = up.run_installer(str(tmp_path / "x.command"))
    assert "capture_output" not in seen and seen.get("stdout") is not sp.PIPE
    assert hasattr(seen["stdout"], "write"), "安装器输出要进文件"
    assert "installer said hi" in proc.stdout, "失败时还要能拿到最后几行给日志"
    assert (tmp_path / "logs" / "selfupdate-install.log").exists()
