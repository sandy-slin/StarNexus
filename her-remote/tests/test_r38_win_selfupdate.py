"""第 38 轮：Windows 和 Mac 一样自动更新（主人原话「windows 你要搞定，让他们能够自动更新」）。

盯的几条（每条对应一个会在 6 台 Windows 上变成事故的形状）：
1. sha256 不对 ⇒ 绝不跑安装器；有活在跑 ⇒ 不装；撞上另一个安装器（退出码 3）⇒ 不记 fail、恢复原记录。
2. 起安装器：powershell 5.1 + `-ExecutionPolicy Bypass`（只对本进程）+ `-NonInteractive` + 不弹窗
   （CREATE_NO_WINDOW，**不用** DETACHED_PROCESS）+ 输出进文件不进管道 + 带 CARHER_AUTO_UPDATE=1 / CARHER_SELF；
   计划任务的作业不许脱离时退一步照样装。
3. 安装器（install-windows.ps1，从模板里按标记原样抠出来在 pwsh 里真跑）：
   - INSTALL-LOCK：另一份占着锁 ⇒ 退出码 3 + 中文；上一份被强杀 ⇒ 这份接上；Restore-ConsoleEncoding 放锁。
   - FILE-RETRY：文件一直被占 ⇒ 5 次后中文 Die；中途放开 ⇒ 写进去。
   - UPDATE-ROLLBACK/VERDICT：连上 ⇒ 删 bridge.prev；自动更新没连上 / 中途 Die ⇒ 换回旧版 + 记 rolled_back
     ⇒ 桥启动后 `u=not_applied`；手动装不换回。
   - run-agent.ps1：连续 5 次活不过 60 秒 ⇒ 起一次 errreport + 报一次 u=crashloop（只 5 个键）+ 退避。
4. 上报词两边一致（selfupdate._OUTCOME_WORDS ↔ fleet-report.py FIELD_RE["u"]）。
5. 旧版 updater（线上 20261116-f06ffd64）在代码里写死 manual_only ⇒ 只能手动重装一次（写进报告）。
6. 产物字节级：在 /tmp 里照 build.sh 生成一份 .bat，BOM/ASCII 头/CRLF/here-string + PS 正文能解析。

⚠️ pwsh 7 on macOS ≠ Windows PowerShell 5.1：作业对象、CREATE_NO_WINDOW、计划任务、杀毒软件都没在真机跑过
（docs/r38/W-win-selfupdate.md「没验证」那节）。没有 pwsh 的用例 skip。
变异：R38_SRC_DIR=<放改坏的 install-windows.ps1 的目录> 让 ps1 那组去跑那一份。
"""
from __future__ import annotations

import http.server
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from bridge import selfupdate  # noqa: E402
from test_r14_selfupdate import BASE, PKG, _done, _latest, _opener  # noqa: E402

_BRIDGE_ROOT = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[3]
_SRC_DIR = Path(os.environ.get("R38_SRC_DIR") or (_REPO / "feishu-agent-oneclick" / "src"))
INSTALL = _SRC_DIR / "install-windows.ps1"
FLEET = _REPO / "feishu-agent-oneclick" / "fleet-report.py"
BUILD_SH = _REPO / "feishu-agent-oneclick" / "build.sh"
APP_ID = "cli_a1b2c3d4e5f60708"


def _pwsh():
    for c in (os.environ.get("CARHER_PWSH"), shutil.which("pwsh"),
              str(_REPO / ".work" / "ai-portal" / "installer" / "pwsh" / "pwsh")):
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


PWSH = _pwsh()
need_pwsh = pytest.mark.skipif(not PWSH, reason="本机没有 pwsh")


@pytest.fixture(autouse=True)
def _no_report(monkeypatch):
    monkeypatch.setenv("CARHER_NO_REPORT", "1")


def _windows(monkeypatch):
    monkeypatch.setattr(selfupdate.platform, "system", lambda: "Windows")


WIN_FILES = {f"{BASE}/latest.json": _latest(key="windows"),
             f"{BASE}/feishu-agent-mac.command": PKG}


def _up(tmp_path, files=None, *, runner=None, idle=True, current="20261106-oldbuild"):
    pkg = tmp_path / "bridge"
    pkg.mkdir(exist_ok=True)
    (pkg / "buildinfo.py").write_text(f'BUILD_ID = "{current}"\n', encoding="utf-8")
    return selfupdate.SelfUpdater(
        str(pkg), str(tmp_path / "update_state.json"),
        is_idle=lambda: idle, opener=_opener(files if files is not None else WIN_FILES),
        runner=runner, now=lambda: 1000.0, app_id=APP_ID)


def _state(tmp_path):
    p = tmp_path / "update_state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ═══════════════ 1. Python 侧：装不装 ═══════════════

def test_positive_control_windows_downloads_and_installs(tmp_path, monkeypatch):
    """阳性对照：sha 对、空闲 ⇒ 真跑安装器（下面「不装」的用例都以它为基线）。"""
    _windows(monkeypatch)
    ran = []
    up = _up(tmp_path, runner=lambda p: ran.append(Path(p).read_bytes()) or _done(0))
    assert up.tick()[0] is True
    assert ran == [PKG]
    assert _state(tmp_path)["20261106-newbuild"]["outcome"] == "ok"


def test_bad_sha_never_runs_installer(tmp_path, monkeypatch):
    _windows(monkeypatch)
    ran = []
    files = dict(WIN_FILES)
    files[f"{BASE}/latest.json"] = _latest(key="windows", sha="a" * 64)
    up = _up(tmp_path, files, runner=lambda p: ran.append(p) or _done(0))
    ok, _msg = up.tick()
    assert ok is False and ran == []
    assert _state(tmp_path)["20261106-newbuild"]["outcome"] == "fail"


def test_busy_never_runs_installer(tmp_path, monkeypatch):
    _windows(monkeypatch)
    ran = []
    up = _up(tmp_path, runner=lambda p: ran.append(p) or _done(0), idle=False)
    assert up.tick() is None and ran == []
    assert _state(tmp_path) == {}


def test_installer_lock_busy_rc3_is_not_a_failure(tmp_path, monkeypatch):
    """撞上另一个安装器（INSTALL-LOCK 退出码 3）⇒ 这次没动 ⇒ 不记 fail、「在装」撤掉，下一拍再试。"""
    _windows(monkeypatch)
    ran = []
    up = _up(tmp_path, runner=lambda p: ran.append(p) or _done(selfupdate.WIN_INSTALLER_BUSY_RC))
    ok, msg = up.tick()
    assert ok is False and "另一个安装窗口还在跑" in msg
    assert "20261106-newbuild" not in _state(tmp_path)
    assert up.report_params(up.current_build())["u"] == ""
    assert up.check() is not None, "撞锁之后下一拍应当还会再试"
    assert len(ran) == 1


def test_rolled_back_by_installer_reports_not_applied(tmp_path, monkeypatch):
    """安装器自己换回旧版（记 rolled_back、退出码 1）⇒ 不被 fail 盖掉 ⇒ 上报 u=not_applied、自动不再装这一版。"""
    _windows(monkeypatch)
    up = _up(tmp_path)

    def runner(_p):
        st = _state(tmp_path)
        st["20261106-newbuild"] = {"outcome": "rolled_back", "at": 1000.0, "detail": "60 秒内没连上飞书",
                                   "from": "20261106-oldbuild"}
        (tmp_path / "update_state.json").write_text(json.dumps(st), encoding="utf-8")
        return _done(1)

    up._runner = runner
    ok, msg = up.tick()
    assert ok is False and "退回原来那版" in msg
    assert _state(tmp_path)["20261106-newbuild"]["outcome"] == "not_applied"
    assert up.report_params(up.current_build())["u"] == "not_applied"
    assert up.check() is None, "没装上的那一版自动那一拍不该再装"


# ═══════════════ 2. Python 侧：怎么起安装器 ═══════════════

class _Rec:
    def __init__(self, fail_breakaway=False, rc=0):
        self.calls = []
        self.fail_breakaway = fail_breakaway
        self.rc = rc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.fail_breakaway and kw.get("creationflags", 0) & selfupdate._CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError(5, "Access is denied")
        kw["stdout"].write("安装器输出\n")
        return subprocess.CompletedProcess(argv, self.rc)


def _run_win(tmp_path, monkeypatch, rec):
    _windows(monkeypatch)
    monkeypatch.setattr(selfupdate.subprocess, "run", rec)
    up = selfupdate.SelfUpdater(str(tmp_path / "bridge"), str(tmp_path / "update_state.json"),
                                is_idle=lambda: True)
    pkg = tmp_path / "pkg.bat"
    pkg.write_bytes(b"x")
    return up.run_installer(str(pkg)), pkg


def test_windows_installer_argv_flags_env_and_no_pipe(tmp_path, monkeypatch):
    rec = _Rec()
    proc, pkg = _run_win(tmp_path, monkeypatch, rec)
    assert proc.returncode == 0 and "安装器输出" in proc.stdout
    assert len(rec.calls) == 1
    argv, kw = rec.calls[0]
    assert argv[0].lower().endswith("powershell.exe")
    joined = " ".join(argv[:-1])
    assert "-ExecutionPolicy Bypass" in joined and "-NonInteractive" in joined and "-NoProfile" in joined
    assert "-WindowStyle Hidden" in joined
    assert not any("Set-ExecutionPolicy" in a or "RunAs" in a for a in argv), "不许改执行策略 / 提权"
    flags = kw["creationflags"]
    assert flags & selfupdate._CREATE_NO_WINDOW
    assert not flags & selfupdate._DETACHED_PROCESS, "DETACHED 会让安装器起的每个子进程各弹一个黑窗"
    assert flags & selfupdate._CREATE_NEW_PROCESS_GROUP
    assert kw["stdout"] is not subprocess.PIPE and hasattr(kw["stdout"], "write"), "输出必须进文件（桥被换掉时管道会断）"
    assert kw.get("capture_output") is not True
    assert kw["stderr"] is subprocess.STDOUT
    assert kw["env"]["CARHER_AUTO_UPDATE"] == "1"
    assert kw["env"]["CARHER_SELF"] == os.path.abspath(str(pkg))
    assert (tmp_path / "logs" / "selfupdate-install.log").read_text(encoding="utf-8").strip() == "安装器输出"


def test_windows_breakaway_denied_still_installs(tmp_path, monkeypatch):
    """计划任务的作业不许脱离 ⇒ CreateProcess 拒绝 ⇒ 退一步不脱离，照样装（不是装失败）。"""
    rec = _Rec(fail_breakaway=True)
    proc, _ = _run_win(tmp_path, monkeypatch, rec)
    assert proc.returncode == 0
    assert len(rec.calls) == 2
    assert rec.calls[0][1]["creationflags"] & selfupdate._CREATE_BREAKAWAY_FROM_JOB
    assert not rec.calls[1][1]["creationflags"] & selfupdate._CREATE_BREAKAWAY_FROM_JOB
    assert rec.calls[1][1]["creationflags"] & selfupdate._CREATE_NO_WINDOW


def test_ps_boot_only_runs_the_ps_body():
    """_PS_BOOT 只读 CARHER_SELF 里 ##CARHER-PS1 之后那段跑；不碰桥 / 循环（那是安装器第 6 步的事）。"""
    b = selfupdate._PS_BOOT
    assert "$env:CARHER_SELF" in b and "'#'+'#CARHER-PS1'" in b
    assert "bridge.app" not in b and "run-agent" not in b and "ExecutionPolicy" not in b


@need_pwsh
def test_ps_boot_exit_codes_in_pwsh(tmp_path):
    """真跑 _PS_BOOT：正文正常 ⇒ 0；exit 3（撞锁）⇒ 3；throw / Die ⇒ 非 0；没标记 ⇒ 9。"""
    bat = tmp_path / "t.bat"
    bat.write_bytes(b"@echo off\r\nexit /b\r\n##CARHER-PS1\r\nif ($env:X -eq '3') { exit 3 }\r\n"
                    b"if ($env:X -eq 't') { throw 'boom' }\r\nif ($env:X -eq 'd') { exit 1 }\r\n")
    nomark = tmp_path / "n.bat"
    nomark.write_bytes(b"@echo off\r\n")
    got = {}
    for x, path in (("0", bat), ("3", bat), ("t", bat), ("d", bat), ("n", nomark)):
        r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", selfupdate._PS_BOOT],
                           env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "X": x, "CARHER_SELF": str(path)},
                           capture_output=True, text=True, timeout=60)
        got[x] = r.returncode
    assert got == {"0": 0, "3": 3, "t": 1, "d": 1, "n": 9}, got


# ═══════════════ 3. 上报词 / 旧版 ═══════════════

def test_u_words_match_fleet_report():
    text = FLEET.read_text(encoding="utf-8")
    m = re.search(r'"u": re\.compile\(r"\^\(([^)]*)\)\$"\)', text)
    assert m, "fleet-report.py 里找不到 FIELD_RE['u']"
    fleet_words = {w for w in m.group(1).split("|") if w}
    assert fleet_words == set(selfupdate._OUTCOME_WORDS)
    assert selfupdate.ROLLED_BACK not in fleet_words, "rolled_back 不上报（启动后转成 not_applied）"


def test_old_updater_is_hardcoded_manual_only_needs_one_reinstall():
    """线上 Windows 跑的旧版：selfupdate.check 在下载前就返回 manual_only（代码写死，latest.json 改不了它）
    ⇒ 6 台只能各手动重装一次。这条盯「报告里的结论」有据：旧码里确实是这样，新码里确实没了。"""
    try:
        old = subprocess.run(["git", "-C", str(_REPO), "show", "f06ffd64:tools/feishu-agent-bridge/bridge/selfupdate.py"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("没有 git")
    if old.returncode != 0:
        pytest.skip("仓库里没有 f06ffd64（浅克隆？）")
    assert 'if installer_key() == "windows":' in old.stdout and "return dict(info, manual_only=True)" in old.stdout
    new = (_BRIDGE_ROOT / "bridge" / "selfupdate.py").read_text(encoding="utf-8")
    assert "return dict(info, manual_only=True)" not in new


# ═══════════════ 4. 安装器：按标记抠出来在 pwsh 里真跑 ═══════════════

def _run_ps(tmp_path, script, env, timeout=120, name="harness.ps1"):
    harness = tmp_path / name
    harness.write_text(script, encoding="utf-8")
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path) + "/",
            "SRC": str(INSTALL), "CARHER_NO_REPORT": "1"}
    base.update({k: v for k, v in env.items() if v is not None})
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(harness)],
                       env=base, capture_output=True, text=True, timeout=timeout)
    kv = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m:
            kv[m.group(1)] = m.group(2)
    return r, kv


# Cut：从模板里按锚点原样抠一段（测试里不另写一份被测代码）
_CUT = r'''
$text = [IO.File]::ReadAllText($env:SRC)
function Cut([string]$from, [string]$to) {
  $a = $text.IndexOf($from, [StringComparison]::Ordinal); if ($a -lt 0) { throw "缺锚点 $from" }
  $b = $text.IndexOf($to, $a, [StringComparison]::Ordinal); if ($b -lt 0) { throw "缺锚点 $to" }
  return $text.Substring($a, $b - $a)
}
'''

# ---------- INSTALL-LOCK ----------

_LOCK_HARNESS = _CUT + r'''
$script:encSaved = @{ Out = $null; In = $null; PyIo = $null; PyUtf8 = $null }
. ([scriptblock]::Create((Cut 'function Restore-ConsoleEncoding {' 'function Say(')))
. ([scriptblock]::Create((Cut '# INSTALL-LOCK-BEGIN' '# INSTALL-LOCK-END')))
Write-Output 'GOT=1'
if ($env:RELEASE -eq '1') { Restore-ConsoleEncoding; Write-Output 'RELEASED=1' }
if ($env:HOLD) { [IO.File]::WriteAllText($env:HOLD, 'x'); Start-Sleep -Seconds 40 }
'''


def _wait(pred, sec):
    end = time.time() + sec
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def _holder(tmp_path, release=False):
    flag = tmp_path / ("held-r" if release else "held")
    harness = tmp_path / ("hold-r.ps1" if release else "hold.ps1")
    harness.write_text(_LOCK_HARNESS, encoding="utf-8")
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path) + "/", "SRC": str(INSTALL),
           "HOLD": str(flag), "RELEASE": "1" if release else ""}
    p = subprocess.Popen([PWSH, "-NoProfile", "-NonInteractive", "-File", str(harness)], env=env,
                         process_group=0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert _wait(flag.exists, 40), "第一份没拿到锁"
    return p


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


@need_pwsh
def test_install_lock_second_installer_exits_3_with_chinese(tmp_path):
    first = _holder(tmp_path)
    try:
        r, kv = _run_ps(tmp_path, _LOCK_HARNESS, {})
        assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)
        assert "GOT" not in kv
        assert "另一个安装窗口还在跑" in r.stdout and "下一步" in r.stdout
        assert "Exception" not in r.stdout + r.stderr
        _kill(first)   # 强杀 = 锁被遗弃 ⇒ 下一份要能接上
        r2, kv2 = _run_ps(tmp_path, _LOCK_HARNESS, {})
        assert r2.returncode == 0 and kv2.get("GOT") == "1", (r2.stdout, r2.stderr)
    finally:
        _kill(first)


@need_pwsh
def test_install_lock_released_by_restore_console_encoding(tmp_path):
    """一行命令在他自己的窗口里跑完、窗口不关 ⇒ Restore-ConsoleEncoding 必须放锁，不然自动更新永远撞锁。"""
    first = _holder(tmp_path, release=True)
    try:
        r, kv = _run_ps(tmp_path, _LOCK_HARNESS, {})
        assert r.returncode == 0 and kv.get("GOT") == "1", (r.returncode, r.stdout, r.stderr)
    finally:
        _kill(first)


# ---------- FILE-RETRY ----------

_RETRY_HARNESS = _CUT + r'''
function Die($m) { Write-Output ("DIE=" + ($m -replace "`r?`n", ' | ')); exit 1 }
. ([scriptblock]::Create((Cut '# FILE-RETRY-BEGIN' '# FILE-RETRY-END')))
if ($env:GO) { [IO.File]::WriteAllText($env:GO, '1') }   # 告诉占锁那边：开始写了（它从这时起再占 1.2 秒）
$t0 = [DateTime]::UtcNow
Write-CarherFileRetry $env:TARGET 'hello' (New-Object Text.UTF8Encoding $false) 'config.toml'
Write-Output ("WROTE=" + [IO.File]::ReadAllText($env:TARGET))
Write-Output ("MS=" + [int]([DateTime]::UtcNow - $t0).TotalMilliseconds)
'''

_LOCKER = ("import fcntl, os, sys, time\n"
           "f = open(sys.argv[1], 'r+'); fcntl.flock(f, fcntl.LOCK_EX)\n"
           "open(sys.argv[2], 'w').close()\n"
           "if len(sys.argv) > 4:\n"
           "    end = time.time() + 60\n"
           "    while not os.path.exists(sys.argv[4]) and time.time() < end: time.sleep(0.05)\n"
           "time.sleep(float(sys.argv[3]))\n")


def _lock_file(tmp_path, target, sec, go=None):
    """.NET 在 Unix 上用 flock 实现 FileShare.None ⇒ 别的进程 flock 住 = Windows 上「文件正由另一进程使用」（11-17 实测 IOException）。"""
    flag = tmp_path / "locked"
    flag.unlink(missing_ok=True)
    p = subprocess.Popen([sys.executable, "-c", _LOCKER, str(target), str(flag), str(sec)] + ([str(go)] if go else []))
    assert _wait(flag.exists, 10)
    return p


@need_pwsh
def test_file_retry_gives_chinese_die_after_5_tries(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("old", encoding="utf-8")
    locker = _lock_file(tmp_path, target, 30)
    try:
        r, kv = _run_ps(tmp_path, _RETRY_HARNESS, {"TARGET": str(target)})
    finally:
        locker.kill()
        locker.wait()
    assert r.returncode == 1 and "DIE" in kv, (r.stdout, r.stderr)
    die = kv["DIE"]
    assert "写不进 config.toml" in die and "试了 5 次" in die and "下一步" in die
    assert "IOException" in die, die              # 只带异常类型名
    assert str(tmp_path) not in die, "Die 里不能带路径（会进失败上报，路径里有用户名）"
    assert "WROTE" not in kv
    assert "at System." not in r.stdout + r.stderr, "不许甩英文堆栈"


@need_pwsh
def test_file_retry_succeeds_when_lock_goes_away(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text("old", encoding="utf-8")
    go = tmp_path / "go"
    locker = _lock_file(tmp_path, target, 1.2, go=go)   # pwsh 起得慢也不怕：从它开写起才计 1.2 秒
    try:
        r, kv = _run_ps(tmp_path, _RETRY_HARNESS, {"TARGET": str(target), "GO": str(go)})
    finally:
        locker.wait()
    assert r.returncode == 0 and kv.get("WROTE") == "hello", (r.stdout, r.stderr)
    assert "DIE" not in kv
    assert int(kv["MS"]) >= 900, "应当是重试了几次才写进去（阳性：确实撞上了锁）"


# ---------- UPDATE-ROLLBACK + UPDATE-VERDICT ----------

_UPD_HARNESS = _CUT + r'''
$appHome = $env:APP; $venvPy = $env:VENVPY
function Get-BridgeProcs { @() }
function Send-CarherFailReport { param($Tool, $Msg, $LogFiles) Write-Output ("REPORT=" + $Msg + " LOGS=" + @($LogFiles).Count) }
function Restore-ConsoleEncoding { }
function Get-CarherReportLogs { @() }
. ([scriptblock]::Create((Cut '$script:carherRollbackArmed = $false' '# 没被 catch 住')))
$bridgeDir = Join-Path $appHome 'bridge'
. ([scriptblock]::Create((Cut '# UPDATE-ROLLBACK-BEGIN' '# UPDATE-ROLLBACK-END')))
Write-Output ("ARMED=" + $script:carherRollbackArmed)
# 第 3 步：写新码
New-Item -ItemType Directory -Force -Path $bridgeDir | Out-Null
[IO.File]::WriteAllText((Join-Path $bridgeDir 'buildinfo.py'), "BUILD_ID = `"$env:NEW`"`n")
if ($env:MODE -eq 'die') { Die 'X 第 4 步出错（台架）' }
$connected = ($env:CONNECTED -eq '1'); $connectWait = 60; $connectErr = "$env:CONNECT_ERR"
if ($env:SVC -eq '1') { $script:carherSvcRestarted = $true }
# 定位信息：真的 Get-NewLogBytes / Get-NewLog / Get-CarherConnectDiag（原样抠），日志和 python 进程由台架给
$logPath = if ($env:LOGPATH) { $env:LOGPATH } else { Join-Path $appHome 'logs/agent.log' }
$logMark = [long]("0$env:LOGMARK")
function Get-CimInstance { [CmdletBinding()] param($ClassName, $Filter)
  if ($env:PYALIVE -eq '1') { [pscustomobject]@{ ProcessId = 4242; CommandLine = '"C:\x\python.exe" -m bridge.app' } } }
. ([scriptblock]::Create((Cut '# 🔴 第 38 轮（11-17 同事实机 Win11 PS5.1 自动更新连回滚 3 次' '# 计划任务 → 建不了/改不了就退到登录自启')))
. ([scriptblock]::Create((Cut '# UPDATE-VERDICT-BEGIN' '# UPDATE-VERDICT-END')))
Write-Output 'PASSED=1'
'''

OLD, NEW = "20261116-f06ffd64", "20261117-newbuild"


def _app(tmp_path, with_old=True):
    app = tmp_path / "用户 a" / "app"     # 空格 + 中文：PS5.1 传参/路径的老坑
    (app / "logs").mkdir(parents=True)
    if with_old:
        (app / "bridge").mkdir()
        (app / "bridge" / "buildinfo.py").write_text(f'BUILD_ID = "{OLD}"\n', encoding="utf-8")
        (app / "bridge" / "app.py").write_text("# old\n", encoding="utf-8")
    return app


def _build_of(d: Path):
    p = d / "buildinfo.py"
    return re.search(r'"(.*)"', p.read_text(encoding="utf-8")).group(1) if p.exists() else None


def _upd(tmp_path, app, **env):
    e = {"APP": str(app), "VENVPY": sys.executable, "NEW": NEW}
    e.update(env)
    return _run_ps(tmp_path, _UPD_HARNESS, e)


@need_pwsh
def test_auto_update_connected_deletes_prev(tmp_path):
    app = _app(tmp_path)
    r, kv = _upd(tmp_path, app, CARHER_AUTO_UPDATE="1", CONNECTED="1")
    assert r.returncode == 0 and kv.get("PASSED") == "1", (r.stdout, r.stderr)
    assert kv["ARMED"] == "True"
    assert _build_of(app / "bridge") == NEW
    assert not (app / "bridge.prev").exists() and not (app / "bridge.bad").exists()
    assert "REPORT" not in kv


@need_pwsh
def test_auto_update_not_connected_rolls_back_and_reports_not_applied(tmp_path):
    app = _app(tmp_path)
    r, kv = _upd(tmp_path, app, CARHER_AUTO_UPDATE="1", CONNECTED="0", SVC="1")
    assert r.returncode == 1 and "PASSED" not in kv, (r.stdout, r.stderr)
    assert _build_of(app / "bridge") == OLD and (app / "bridge" / "app.py").read_text() == "# old\n"
    assert not (app / "bridge.prev").exists() and not (app / "bridge.bad").exists()
    assert "已经换回原来那版" in kv.get("REPORT", "") and kv["REPORT"].endswith("LOGS=0"), kv
    st = json.loads((app / "update_state.json").read_text(encoding="utf-8"))
    assert st[NEW]["outcome"] == "rolled_back" and st[NEW]["from"] == OLD
    assert "没连上飞书" in st[NEW]["detail"]
    # 桥用换回来的旧码重启 ⇒ 落定成 not_applied ⇒ 上报 u=not_applied、自动不再装这一版
    up = selfupdate.SelfUpdater(str(app / "bridge"), str(app / "update_state.json"), is_idle=lambda: True)
    up._reconcile(up.current_build())
    assert up.report_params(up.current_build())["u"] == "not_applied"
    assert up.not_applied(NEW)
    assert not list(tmp_path.glob("carher-rollback-*.py")), "临时脚本要删掉"


@need_pwsh
def test_auto_update_die_midway_rolls_back(tmp_path):
    app = _app(tmp_path)
    r, kv = _upd(tmp_path, app, CARHER_AUTO_UPDATE="1", MODE="die")
    assert r.returncode == 1, (r.stdout, r.stderr)
    assert _build_of(app / "bridge") == OLD
    st = json.loads((app / "update_state.json").read_text(encoding="utf-8"))
    assert st[NEW]["outcome"] == "rolled_back" and st[NEW]["detail"] == "安装中途失败"


@need_pwsh
def test_manual_install_not_connected_does_not_roll_back(tmp_path):
    """手动重装没连上 ⇒ 不换回（原因摆给人看），bridge.prev 留着。"""
    app = _app(tmp_path)
    r, kv = _upd(tmp_path, app, CONNECTED="0")
    assert r.returncode == 0 and kv.get("PASSED") == "1", (r.stdout, r.stderr)
    assert kv["ARMED"] == "False"
    assert _build_of(app / "bridge") == NEW and _build_of(app / "bridge.prev") == OLD
    assert not (app / "update_state.json").exists()


@need_pwsh
def test_fresh_install_has_nothing_to_roll_back(tmp_path):
    app = _app(tmp_path, with_old=False)
    r, kv = _upd(tmp_path, app, CARHER_AUTO_UPDATE="1", CONNECTED="0")
    assert r.returncode == 0 and kv.get("PASSED") == "1", (r.stdout, r.stderr)
    assert kv["ARMED"] == "False" and _build_of(app / "bridge") == NEW


# ---------- run-agent.ps1：Windows 的启动守卫（接 E 的 errreport） ----------

_RUNNER_GEN = r'''
$ErrorActionPreference = 'Stop'
$tok = $null; $perr = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SRC, [ref]$tok, [ref]$perr)
$config = $env:R_CONFIG; $claudeDir = $env:R_CLAUDEDIR; $appHome = $env:R_APPHOME; $venvPy = $env:R_VENVPY; $logDir = $env:R_LOGDIR
$st = $ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $_.Left.VariablePath.UserPath -eq 'runnerText' } | Select-Object -First 1
. ([scriptblock]::Create($st.Extent.Text))
[IO.File]::WriteAllText($env:R_OUT, $runnerText, (New-Object Text.UTF8Encoding $true))
$t2 = $null; $e2 = $null
[void][System.Management.Automation.Language.Parser]::ParseFile($env:R_OUT, [ref]$t2, [ref]$e2)
Write-Output ("PARSE_ERR=" + @($e2).Count)
'''

# 同名函数盖掉 cmdlet（函数先于 cmdlet 解析）：不真起进程、不真打网络、不真等
_RUNNER_STUBS = r'''
function Start-Process { param($FilePath, $WindowStyle, $WorkingDirectory, $ArgumentList)
  Add-Content -LiteralPath $env:CALLS ("SP|" + $FilePath + "|" + $WindowStyle + "|" + $ArgumentList) }
function Invoke-WebRequest { param($Uri, [switch]$UseBasicParsing, $TimeoutSec)
  Add-Content -LiteralPath $env:CALLS ("IWR|" + $Uri) }
function Start-Sleep { param($Seconds)
  Add-Content -LiteralPath $env:CALLS ("SLEEP|" + $Seconds)
  if ($Seconds -ge 240) { exit 0 } }
. $env:RUNNER
'''


@need_pwsh
@pytest.mark.parametrize("no_report", ["", "1"])
def test_runner_crashloop_reports_once_then_backs_off(tmp_path, no_report):
    app = tmp_path / "用户 a" / "app"
    logs = app / "logs"
    (app / "bridge").mkdir(parents=True)
    logs.mkdir()
    (app / "bridge" / "buildinfo.py").write_text(f'BUILD_ID = "{NEW}"\n', encoding="utf-8")
    (app / "bridge" / "errreport.py").write_text("# stub\n", encoding="utf-8")
    (app / "install_id").write_text("0123456789abcdef\n", encoding="utf-8")
    config = app / "config.toml"
    config.write_text(f'app_id = "{APP_ID}"\napp_secret = "SuperSecretValue0123"\n', encoding="utf-8")
    fakepy = tmp_path / "fakepy"
    fakepy.write_text('#!/bin/sh\necho "Traceback: boom" \nexit 1\n')
    fakepy.chmod(0o755)
    runner = tmp_path / "run-agent.ps1"
    r, kv = _run_ps(tmp_path, _RUNNER_GEN, {
        "R_CONFIG": str(config), "R_CLAUDEDIR": str(tmp_path), "R_APPHOME": str(app),
        "R_VENVPY": str(fakepy), "R_LOGDIR": str(logs), "R_OUT": str(runner)})
    assert kv.get("PARSE_ERR") == "0", (r.stdout, r.stderr)
    calls = tmp_path / "calls.txt"
    r, _ = _run_ps(tmp_path, _RUNNER_STUBS, {"RUNNER": str(runner), "CALLS": str(calls),
                                             "CARHER_NO_REPORT": no_report}, timeout=90, name="stubs.ps1")
    got = calls.read_text(encoding="utf-8").splitlines()
    sleeps = [int(c.split("|")[1]) for c in got if c.startswith("SLEEP|")]
    assert sleeps == [5, 5, 5, 5, 60, 120, 240], got     # 前 4 次照旧 5 秒；第 5 次起退避翻倍
    sp = [c for c in got if c.startswith("SP|")]
    iwr = [c for c in got if c.startswith("IWR|")]
    if no_report == "1":
        assert sp == [] and iwr == [], got
        return
    assert len(sp) == 1 and len(iwr) == 1, got           # 一段崩溃只报一次
    _sp, exe, style, args = sp[0].split("|", 3)
    assert exe == str(fakepy) and style == "Hidden"
    assert args == f'-m bridge.errreport --home "{app}" --crash-log "{logs}\\agent.log"', args
    assert got.index(sp[0]) == got.index("SLEEP|60") - 2   # 第 5 次退出之后、退避之前
    url = urllib.parse.urlsplit(iwr[0].split("|", 1)[1])
    assert url.scheme == "https" and url.netloc == "chat.auto-link.com.cn"
    q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
    assert q == {"v": [NEW], "id": ["0123456789abcdef"], "os": ["windows"], "u": ["crashloop"], "app": [APP_ID]}
    assert "Secret" not in iwr[0]
    log = (logs / "agent.log").read_text(encoding="utf-8", errors="replace")
    assert "连续 5 次很快就退出，等 60 秒再试" in log


@need_pwsh
def test_runner_long_run_resets_streak():
    """活过 60 秒 = 正常退出（看门狗自杀是 15 分钟静默后）⇒ 计数清零，不算崩溃循环。按源码判：计数清零和 reported 一起复位。"""
    text = INSTALL.read_text(encoding="utf-8")
    assert "TotalSeconds -lt 60) { `$carherStreak++ } else { `$carherStreak = 0; `$carherReported = `$false }" in text


# ═══════════════ 5. 产物字节级（在 /tmp 照 build.sh 生成，不碰共享产物） ═══════════════

def _build_bat(tmp_path) -> bytes:
    """照 build.sh 的 python 段生成 feishu-agent-windows.bat，输出放 tmp_path（不跑 pytest、不覆盖仓库里的产物）。"""
    sh = BUILD_SH.read_text(encoding="utf-8")
    m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", sh, re.S)
    assert m, "build.sh 里找不到 python heredoc"
    code = m.group(1)
    oneclick = BUILD_SH.parent
    code = code.replace('Path("src")', f'Path({str(oneclick / "src")!r})')
    code = code.replace('Path("../tools/feishu-agent-bridge")', f'Path({str(_BRIDGE_ROOT)!r})')
    script = tmp_path / "build_products.py"
    script.write_text(code, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    r = subprocess.run([sys.executable, str(script)], cwd=out, env=dict(os.environ, BUILD_ID="20261117-r38test"),
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    return (out / "feishu-agent-windows.bat").read_bytes()


def test_product_bat_bytes(tmp_path):
    blob = _build_bat(tmp_path)
    cut = blob.index(b"##CARHER-PS1")
    assert not blob.startswith(b"\xef\xbb\xbf")
    assert all(c < 128 for c in blob[:cut])
    assert blob.count(b"\n") == blob.count(b"\r\n")
    body = blob[cut:].decode("utf-8")
    assert "@@EMBED" not in body and "@@BUILD_ID@@" not in body
    for marker in ("# INSTALL-LOCK-BEGIN", "# UPDATE-ROLLBACK-BEGIN", "# FILE-RETRY-BEGIN", "# UPDATE-VERDICT-BEGIN",
                   "Local\\CarHerFeishuAgent.install", "bridge.errreport"):
        assert marker in body, marker
    # 自动更新那支（_PS_BOOT）就是靠这个标记找正文的
    assert body.startswith("##CARHER-PS1")
    (tmp_path / "body.ps1").write_bytes(blob[cut:])


@need_pwsh
def test_product_bat_body_parses(tmp_path):
    blob = _build_bat(tmp_path)
    body = tmp_path / "body.ps1"
    body.write_bytes(blob[blob.index(b"##CARHER-PS1"):])
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command",
                        '$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:B,[ref]$t,[ref]$e);'
                        '"ERR=" + @($e).Count; $e | Select-Object -First 3 | ForEach-Object { $_.Message }'],
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "B": str(body)},
                       capture_output=True, text=True, timeout=120)
    assert "ERR=0" in r.stdout, r.stdout + r.stderr


def test_template_never_changes_execution_policy_or_elevates():
    text = INSTALL.read_text(encoding="utf-8")
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "Set-ExecutionPolicy" not in code
    assert "-Verb RunAs" not in code and "-Verb runas" not in code.lower()
