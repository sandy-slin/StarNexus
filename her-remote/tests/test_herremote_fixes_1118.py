"""巡检 11-18 18:41 · her remote 安装器 3 处(红绿对照见各节)。

① 180209-herremote-7413e2:$env:TEMP 是 8.3 短路径 C:\\Users\\C-HENG~1.ZHO\\…,PS5.1 的 Remove-Item 抛终止错误
   (-ErrorAction SilentlyContinue 压不住)⇒ 顶部 trap 停掉整次安装。修:临时路径 [IO.Path]::GetTempPath()
   + Remove-CarherTemp(.NET 删,异常全吞),照抄 claude-oneclick 17:50 已上线那版。⚠️ PS5.1 崩溃本身 PS5.1 未实测。
② 175432-herremote-de6727:自动更新多等了「还在起」之后,日志第一次有字(先写「桥已启动」)的下一秒就判没连上,
   几秒后 connected to 才写进来 ⇒ 把一个马上连上的新版换回去。修:日志首次有字起再宽限 $connectGrace 秒。
③ 183843-herremote-29a3f2:证书错(公司安全软件做 HTTPS 检查)被冠上「飞书不认这对凭据」。修:网络类换标题,
   bootstrap.py 证书错单独一句人话。

红对照:HR1118_WIN_PS1 / HR1118_DOCTOR_PS1 / HR1118_MAC_SH / HR1118_BOOTSTRAP 指向本目录的 .pre 即可
  (.work/watch/20261118-1841/herremote-win-temp-shortpath/*.pre)。
"""
import importlib.machinery
import importlib.util
import io
import os
import re
import ssl
import subprocess
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from tests.test_r38_win_connect import _LARK_FAIL, _LARK_OK, _OLD_ROUND, _WAIT_HARNESS, _write
from tests.test_r38_win_selfupdate import INSTALL, PWSH, _run_ps, need_pwsh

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "feishu-agent-oneclick" / "src"
WIN = Path(os.environ.get("HR1118_WIN_PS1") or INSTALL)
DOCTOR = Path(os.environ.get("HR1118_DOCTOR_PS1") or _SRC / "doctor-windows.ps1")
MAC = Path(os.environ.get("HR1118_MAC_SH") or _SRC / "install-mac.sh")
BOOT = Path(os.environ.get("HR1118_BOOTSTRAP")
            or Path(__file__).resolve().parents[1] / "bridge" / "bootstrap.py")


def _code_lines(path):
    src = path.read_text(encoding="utf-8-sig")
    return [re.split(r"\s+#\s", l, maxsplit=1)[0] for l in src.splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


# ═══════════════ ① 8.3 短路径 TEMP ═══════════════
# 修前这些变量都指 TEMP 下的文件/目录,用裸 Remove-Item 删
_WIN_TEMP_VARS = ("probeOut", "preflightFile", "tmp", "markPy")


@pytest.mark.parametrize("path", [WIN, DOCTOR], ids=["install", "doctor"])
def test_no_env_temp_paths_1118(path):
    bad = [l.strip() for l in _code_lines(path) if "Join-Path $env:TEMP" in l]
    assert not bad, f"临时路径还在用 $env:TEMP(可能是 8.3 短路径):{bad}"


def test_temp_vars_not_removed_by_bare_remove_item_1118():
    bad = []
    for path, names in ((WIN, _WIN_TEMP_VARS), (DOCTOR, ("menuPyFile",))):
        for l in _code_lines(path):
            for v in names:
                if re.search(r"Remove-Item\b[^|;]*\$" + v + r"\b", l):
                    bad.append(f"{path.name}: {l.strip()}")
    assert not bad, f"TEMP 下的临时文件还在用 Remove-Item 删(短路径下抛终止错误):{bad}"


def test_helper_defined_before_first_use_1118():
    src = WIN.read_text(encoding="utf-8-sig")
    assert "function Remove-CarherTemp" in src, "修前没有 Remove-CarherTemp"
    d = src.index("function Remove-CarherTemp")
    uses = [m.start() for m in re.finditer(r"Remove-CarherTemp \$", src)]
    assert uses and min(uses) > d, "Remove-CarherTemp 必须在第一次用之前定义"
    # 必须在顶部 trap 之前就有(trap 一触发整次安装就停了)
    assert d < src.index("\ntrap {"), "Remove-CarherTemp 要定义在 trap 之前"


def _helper():
    return next(l for l in WIN.read_text(encoding="utf-8-sig").splitlines()
                if l.startswith("function Remove-CarherTemp"))


@need_pwsh
def test_helper_never_throws_under_shortpath_temp_1118(tmp_path):
    """pwsh 7.4 + EAP=Stop + 带 `~1` 的 TEMP:文件/目录/不存在/空值都不抛,而且真删掉了。"""
    assert "function Remove-CarherTemp" in WIN.read_text(encoding="utf-8-sig"), "修前没有 Remove-CarherTemp"
    t = tmp_path / "C-HENG~1.ZHO" / "AppData" / "Local" / "Temp"
    (t / "carher-py-x" / "python").mkdir(parents=True)
    (t / "carher-py-x" / "python" / "a.txt").write_text("x")
    (t / "carher-preflight-x.py").write_text("x")
    script = ("$ErrorActionPreference = 'Stop'\n" + _helper() + "\n"
              "$f = Join-Path $env:TEMP 'carher-preflight-x.py'; $d = Join-Path $env:TEMP 'carher-py-x'\n"
              "Remove-CarherTemp $f, \"$f.err\"\nRemove-CarherTemp $d, (Join-Path $env:TEMP 'nope.py'), $null, ''\n"
              "Remove-CarherTemp $f\n'DONE ' + [IO.File]::Exists($f) + ' ' + [IO.Directory]::Exists($d)\n")
    env = dict(os.environ, TEMP=str(t), TMPDIR=str(t) + "/")
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode == 0 and "DONE False False" in r.stdout, r.stdout + r.stderr


@need_pwsh
@pytest.mark.parametrize("path", [WIN, DOCTOR], ids=["install", "doctor"])
def test_ps1_parses_1118(path):
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command",
                        "$e=$null; [void][Management.Automation.Language.Parser]::ParseFile("
                        f"'{path}',[ref]$null,[ref]$e); 'ERR=' + $e.Count"],
                       capture_output=True, text=True, timeout=60)
    assert "ERR=0" in r.stdout, r.stdout + r.stderr


# ═══════════════ ② 日志刚有字就判没连上 ═══════════════
# 在 test_r38_win_connect 的台架上把 Start-Sleep 换成「两段追加」:第 A 秒写「桥已启动」,第 B 秒写 connected to
_STARTED = "2026-11-18 17:50:00 INFO feishu-bridge 桥已启动\n"
_SLEEP2 = r'''
$append2At = if ($env:APPEND2_AT) { [int]$env:APPEND2_AT } else { 0 }
$append2Text = "$env:APPEND2_TEXT"
function Start-Sleep { param([int]$Seconds)
  $script:tick++
  if ($appendAt -gt 0 -and $script:tick -eq $appendAt -and $appendText) {
    [IO.File]::AppendAllText($logPath, $appendText, (New-Object Text.UTF8Encoding $false))
  }
  if ($append2At -gt 0 -and $script:tick -eq $append2At -and $append2Text) {
    [IO.File]::AppendAllText($logPath, $append2Text, (New-Object Text.UTF8Encoding $false))
  }
}
'''
_ANCHOR = "$sw = [Diagnostics.Stopwatch]::StartNew()"
assert _ANCHOR in _WAIT_HARNESS
_HARNESS2 = _WAIT_HARNESS.replace(_ANCHOR, _SLEEP2 + _ANCHOR)


def _wait2(tmp_path, **env):
    p, mark = _write(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")])
    e = {"LOGPATH": str(p), "LOGMARK": str(mark), "PYALIVE": "1", "PYAGE": "300", "SRC": str(WIN),
         "CARHER_AUTO_UPDATE": "1", "CONNECT_WAIT_SEC": "3", "CONNECT_WAIT_MAX_SEC": "30"}
    e.update(env)
    r, kv = _run_ps(tmp_path, _HARNESS2, e, name="wait2.ps1")
    assert "CONNECTED" in kv, r.stdout + r.stderr
    return kv


@need_pwsh
def test_connected_a_few_seconds_after_log_starts_1118(tmp_path):
    """🔴 现场形状:到点时 0 字节 + python 活着 ⇒ 多等;第 6 秒先写「桥已启动」,第 8 秒才写 connected to ⇒ 要判 CONNECTED。"""
    kv = _wait2(tmp_path, APPEND_AT="6", APPEND_TEXT=_STARTED, APPEND2_AT="8", APPEND2_TEXT=_LARK_OK)
    assert kv["CONNECTED"] == "True", f"日志刚有字就判死 ⇒ 马上连上的新版被换回:{kv}"


@need_pwsh
def test_grace_is_bounded_when_never_connected_1118(tmp_path):
    """反向对照:日志开始后一直没有 connected to ⇒ 宽限到了照旧判没连上(不许变成永远等)。"""
    kv = _wait2(tmp_path, APPEND_AT="6", APPEND_TEXT=_STARTED, CONNECT_GRACE_SEC="5")
    assert kv["CONNECTED"] == "False", kv
    assert 6 <= int(kv["WAITED"]) <= 12, f"宽限没按 5 秒收住:{kv}"


@need_pwsh
def test_grace_never_exceeds_wait_max_1118(tmp_path):
    """默认宽限 60 秒,但总时长不超过 $connectWaitMax(这里 15)。"""
    kv = _wait2(tmp_path, APPEND_AT="6", APPEND_TEXT=_STARTED, CONNECT_WAIT_MAX_SEC="15")
    assert kv["CONNECTED"] == "False" and int(kv["WAITED"]) <= 15, kv


@need_pwsh
def test_grace_stops_on_runner_exit_1118(tmp_path):
    """宽限中崩了([runner] 进程退出)⇒ 立刻停,不等满宽限。"""
    kv = _wait2(tmp_path, APPEND_AT="6", APPEND_TEXT=_STARTED,
                APPEND2_AT="7", APPEND2_TEXT="[runner] 进程退出，code=1，5 秒后重启 2026-11-18T17:50:07\n")
    assert kv["CONNECTED"] == "False" and int(kv["WAITED"]) <= 9, kv


@need_pwsh
def test_grace_stops_on_numeric_connect_failed_1118(tmp_path):
    """宽限中飞书按凭据拒绝(err: <数字码>)⇒ 立刻停。"""
    kv = _wait2(tmp_path, APPEND_AT="6", APPEND_TEXT=_STARTED, APPEND2_AT="7", APPEND2_TEXT=_LARK_FAIL)
    assert kv["CONNECTED"] == "False" and int(kv["WAITED"]) <= 8, kv


@need_pwsh
def test_manual_install_unchanged_1118(tmp_path):
    """手动装(非自动更新):上限就是 $connectWait,行为不变。"""
    kv = _wait2(tmp_path, CARHER_AUTO_UPDATE=None, CONNECT_WAIT_MAX_SEC=None,
                APPEND_AT="2", APPEND_TEXT=_STARTED, APPEND2_AT="5", APPEND2_TEXT=_LARK_OK)
    assert kv["CONNECTED"] == "False" and int(kv["WAITED"]) == 3 and int(kv["MAX"]) == 3, kv


# ═══════════════ ③ 证书/网络错不再说「飞书不认这对凭据」 ═══════════════
_CERT = ssl.SSLCertVerificationError(
    1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
       "self-signed certificate in certificate chain (_ssl.c:1144)")


def _boot(monkeypatch, reason):
    # 显式 SourceFileLoader:红对照指向 bootstrap.py.pre 时按扩展名找不到 loader
    spec = importlib.util.spec_from_file_location(
        "bootstrap_1118", BOOT, loader=importlib.machinery.SourceFileLoader("bootstrap_1118", str(BOOT)))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def boom(*a, **kw):
        raise urllib.error.URLError(reason)

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(["bootstrap.py", "cli_a1b2c3d4e5f60708", "s" * 32])
    return rc, buf.getvalue()


def test_bootstrap_cert_error_is_human_1118(monkeypatch):
    rc, out = _boot(monkeypatch, _CERT)
    assert rc == 1 and out.startswith("FATAL=连不上飞书"), out
    assert "证书对不上" in out and "代理" in out and "抓包" in out, out
    assert "CERTIFICATE_VERIFY_FAILED" not in out and "_ssl.c" not in out, f"还在甩英文原话:{out}"


def test_bootstrap_plain_network_error_unchanged_1118(monkeypatch):
    rc, out = _boot(monkeypatch, OSError(61, "Connection refused"))
    assert rc == 1 and out.startswith("FATAL=连不上飞书（") and "检查网络后重试" in out, out
    assert "证书" not in out, out


def _cut(text, a, b):
    i = text.index(a)
    return text[i:text.index(b, i)]


_WHYS = {
    "cert": "连不上飞书：证书对不上。这台电脑的网络在中间换了证书",
    "net": "连不上飞书（[Errno 61] Connection refused）。检查网络后重试。",
    "cred": "App ID 或 App Secret 填错了（注意别把两个贴反）",
}


def _mac_title(why):
    block = _cut(MAC.read_text(encoding="utf-8"),
                 "if printf '%s' \"$BOOTSTRAP_OUT\" | grep -q '^FATAL='; then",
                 'if [ -n "$BOT_NAME" ]; then')
    script = "die() { printf '%s\\n' \"$1\"; exit 1; }\nDOCTOR_HINT=x\n" + block
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                       env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8", "BOOTSTRAP_OUT": "FATAL=" + why})
    return r.stdout.splitlines()[1]   # 第 1 行是 echo 的空行


def _win_title(tmp_path, why):
    block = _cut(WIN.read_text(encoding="utf-8-sig"), "if ($bootOut -match '(?m)^FATAL=(.+)$') {", "$botName = ''")
    script = ("function Die($m) { Write-Output ('TITLE=' + ($m -split \"`n\")[0]); exit 1 }\n"
              "$bootOut = $env:BOOTOUT\n" + block)
    _, kv = _run_ps(tmp_path, script, {"BOOTOUT": "FATAL=" + why}, name="title.ps1")
    return kv["TITLE"]


@pytest.mark.parametrize("kind", ["cert", "net", "cred"])
def test_mac_fatal_title_1118(kind):
    t = _mac_title(_WHYS[kind])
    if kind == "cred":
        assert t.startswith("✗ 飞书不认这对凭据：App ID"), t
    else:
        assert "飞书不认这对凭据" not in t and t.startswith("✗ 连不上飞书"), t
        assert "连不上飞书：连不上飞书" not in t, t


@need_pwsh
@pytest.mark.parametrize("kind", ["cert", "net", "cred"])
def test_win_fatal_title_1118(tmp_path, kind):
    t = _win_title(tmp_path, _WHYS[kind])
    if kind == "cred":
        assert t.startswith("X 飞书不认这对凭据：App ID"), t
    else:
        assert "飞书不认这对凭据" not in t and t.startswith("X 连不上飞书"), t
        assert "连不上飞书：连不上飞书" not in t, t
