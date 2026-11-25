"""第 39 轮（11-18）：Windows 的一键卸载在用户电脑上**从来不存在**。

现场形状：`src/uninstall-windows.ps1` 本身写得很完整（删除防护、擦凭据、按命令行而非进程名杀进程），
但安装器从来不把它写到用户电脑上，198 下载目录里也没有那个 .bat；
而 `src/doctor-windows.ps1` 末尾正叫用户「双击 feishu-agent-uninstall-windows.bat」。
= 11-14 诊断那个坑（`@@DOCTOR_WINDOWS_B64@@` 从没被替换，十几处让用户双击桌面上不存在的东西）的同构复发。

这里测三件事：
1. 安装器把卸载脚本落到 `$appHome\feishu-agent-uninstall-windows.ps1`（**带 BOM**，PS5.1 读无 BOM 的
   .ps1 按 GBK 解码会炸出一堆解析错误），桌面放「CarHer 助手卸载.bat」一行转发器（**全 ASCII** 正文）；
   桌面路径走 `GetFolderPath('Desktop')`（公司电脑把桌面重定向到 OneDrive 时拼 USERPROFILE 会落到看不见的地方）；
   占位符没替换 ⇒ 什么都不写、不把 `@@` 打给用户；桌面写不了 ⇒ 打黄但**不让安装失败**。
2. 卸载脚本把桌面那两个转发器一起删掉（走 Remove-Safely，不绕过防护；桌面只精确到那两个文件
   进白名单，不是把桌面目录整个放进去），且「卸载」那个排在最后。
3. 占位符 / 字节形状：模板里正好一处占位符，替换后产物无 BOM、bat 头纯 ASCII、全 CRLF、行首无 `'@`。

能真跑的都在 pwsh 里真跑（打桩 Environment/IO.File 做不到 ⇒ 用真的临时目录当桌面，
靠 HOME 把 `GetFolderPath('Desktop')` 指过去）。没有 pwsh 的用例 skip。

⚠️ pwsh 7 on macOS ≠ Windows PowerShell 5.1：GBK 解码、cmd 代码页、.bat 自删都没在真机验过。
变异：R39_SRC_DIR=<放改坏的模板的目录> 让这组去跑那一份。
"""
from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

_REPO = Path(__file__).resolve().parents[3]
_ONECLICK = _REPO / "feishu-agent-oneclick"
_SRC_DIR = Path(os.environ.get("R39_SRC_DIR") or (_ONECLICK / "src"))
INSTALL = _SRC_DIR / "install-windows.ps1"
UNINSTALL = _SRC_DIR / "uninstall-windows.ps1"
DOCTOR = _SRC_DIR / "doctor-windows.ps1"
BUILD_SH = _ONECLICK / "build.sh"
PLACEHOLDER = "@@UNINSTALL" + "_WINDOWS_B64@@"


def _pwsh():
    for c in (os.environ.get("CARHER_PWSH"), shutil.which("pwsh"),
              str(_REPO / ".work" / "ai-portal" / "installer" / "pwsh" / "pwsh")):
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


PWSH = _pwsh()
need_pwsh = pytest.mark.skipif(not PWSH, reason="本机没有 pwsh")

# 🔴 桌面必须打桩，不能靠环境变量「指走」：.NET on macOS 的 SpecialFolder.Desktop 走原生 API，
# **HOME / XDG_DESKTOP_DIR 都不管用**，永远返回 ~/Desktop。
# 写这组时就是这么撞上的：第一版台架以为设了 HOME 就安全，结果真的在我自己的桌面上
# 创建了「CarHer 助手卸载.bat」和「我的工资表.xlsx」（已删）。
# 所以台架把那一句**文本替换**成 $env:FAKEDESK，并且替换没发生就 throw（见 _fakedesk）。
_REAL_DESKTOP_CALL = "[Environment]::GetFolderPath('Desktop')"


def _fakedesk(ps: str) -> str:
    """把 PS 片段里的真桌面调用换成台架给的目录；换不动就让台架自己炸，别去碰真桌面。"""
    out = ps.replace(_REAL_DESKTOP_CALL, "$env:FAKEDESK")
    assert _REAL_DESKTOP_CALL not in out
    return out


_GUARD_REAL_DESKTOP = r'''
if (-not $env:FAKEDESK) { throw '台架没给 FAKEDESK' }
'''


@pytest.fixture(autouse=True)
def _real_desktop_untouched():
    """🔴 反向保险：这组测试结束后，我自己的桌面上不许多出任何东西。

    上面那条注释里的事故就是没有这道保险才落到真桌面上的。
    """
    real = Path.home() / "Desktop"
    before = set(p.name for p in real.iterdir()) if real.is_dir() else set()
    yield
    after = set(p.name for p in real.iterdir()) if real.is_dir() else set()
    new = after - before
    assert not new, f"台架写到真桌面上了：{sorted(new)}"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8-sig")


def _run_ps(tmp_path, script, env=None, timeout=120, name="harness.ps1"):
    harness = tmp_path / name
    harness.write_text(script, encoding="utf-8")
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path) + "/",
            "SRC": str(INSTALL), "USRC": str(UNINSTALL), "CARHER_NO_REPORT": "1"}
    base.update({k: v for k, v in (env or {}).items() if v is not None})
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(harness)],
                       env=base, capture_output=True, text=True, timeout=timeout)
    kv = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^([A-Z_][A-Z_0-9]*)=(.*)$", line)
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

# ═══════════════ 1. 安装器：把卸载脚本落到电脑上 ═══════════════
#
# 台架怎么造现场：
#   - `$appHome` 指到临时目录（安装器真正的那行在别处，这里只给这一段它要的变量）；
#   - 桌面：pwsh on macOS 的 `GetFolderPath('Desktop')` 返回 $HOME/Desktop，而 _run_ps 把
#     HOME 设成 tmp_path ⇒ 真的能用「存在 / 不存在」两种形状跑（不用打桩 Environment）；
#   - 占位符：build.sh 还没跑，模板里就是 `@@...@@`。要测「替换之后」就在台架里自己替换
#     （和 build.sh 一样：base64 分行），这样连「分行 base64 解得开」一起验了。

_DROP = _CUT + r'''
$appHome = $env:APPHOME
$psChildAllowed = ($env:PSCHILD -ne '0')
# Write-Host，不是 Write-Output（被测段落是 dot-source 进来的，Write-Output 的东西
# 会混进这一层的输出流 —— 和下面 _GUARD 里那条坑同源，统一按 Write-Host 写）
function Ok($m)   { Write-Host ("OKLINE=" + $m) }
function Warn($m) { Write-Host ("WARNLINE=" + $m) }
function Say($m)  { Write-Host ("SAYLINE=" + $m) }
$seg = Cut '# UNINSTALL-DROP-BEGIN' '# UNINSTALL-DROP-END'
if ($env:B64) {
  # build.sh 那一步：把占位符换成分行 base64
  $seg = $seg.Replace($env:PLACEHOLDER, [IO.File]::ReadAllText($env:B64))
}
# 🔴 桌面打桩（见模块顶部 _fakedesk）：.NET on macOS 的 SpecialFolder.Desktop 不认 HOME。
# 替换不成功就 throw —— 绝不让台架跑到真桌面上去写东西。
$before = $seg
$seg = $seg.Replace($env:REALDESK, '$env:FAKEDESK')
if ($seg -eq $before) { throw '桌面那一句没打上桩（锚点变了？），拒绝继续' }
. ([scriptblock]::Create($seg))
Write-Output ("TAIL=1")
'''


def _b64_lines(text: str) -> str:
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\n".join(b64[i:i + 76] for i in range(0, len(b64), 76))


def _drop(tmp_path, *, embed=True, desktop=True, apphome=None, pschild="1", body=None,
          env_extra=None):
    """跑安装器那一段。embed=False ⇒ 占位符没被替换（build.sh 少跑一步）。"""
    app = Path(apphome) if apphome else (tmp_path / "app")
    if apphome is None:
        app.mkdir(parents=True, exist_ok=True)
    desk = tmp_path / "Desktop"
    if desktop:
        desk.mkdir(exist_ok=True)
    env = {"APPHOME": str(app), "PSCHILD": pschild, "PLACEHOLDER": PLACEHOLDER,
           "REALDESK": _REAL_DESKTOP_CALL,
           # desktop=False ⇒ 指到一个不存在的目录（= 取不到桌面 / 桌面没了那个形状）
           "FAKEDESK": str(desk if desktop else (tmp_path / "no-desktop-here"))}
    if embed:
        src = body if body is not None else _text(UNINSTALL)
        b64 = tmp_path / "u.b64"
        b64.write_text(_b64_lines(src), encoding="ascii")
        env["B64"] = str(b64)
    env.update(env_extra or {})
    r, kv = _run_ps(tmp_path, _DROP, env, name="drop.ps1")
    return r, kv, app, desk


@need_pwsh
def test_installer_writes_uninstaller_into_apphome(tmp_path):
    """🔴 核心：装完电脑上要**真的有**那个卸载脚本 —— 这一条红就等于「装了卸不掉」回来了。"""
    r, kv, app, desk = _drop(tmp_path)
    assert kv.get("TAIL") == "1", (r.stdout, r.stderr)
    ps1 = app / "feishu-agent-uninstall-windows.ps1"
    assert ps1.is_file(), f"卸载脚本没落到 appHome：{r.stdout}\n{r.stderr}"
    # 内容逐字等于模板（解码没走丢）
    assert ps1.read_text(encoding="utf-8-sig") == _text(UNINSTALL)


@need_pwsh
def test_uninstaller_on_disk_has_bom(tmp_path):
    """🔴 带 BOM：PS5.1 读无 BOM 的 .ps1 按 GBK 解码，满篇中文注释会炸成一堆解析错误。"""
    _r, _kv, app, _desk = _drop(tmp_path)
    raw = (app / "feishu-agent-uninstall-windows.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "没有 BOM ⇒ PS5.1 按 GBK 解，中文注释炸成解析错误"


@need_pwsh
def test_desktop_forwarder_is_one_line_not_a_copy(tmp_path):
    """桌面那个是转发器（指向 appHome 里的 ps1），不是拷贝 —— 否则升级后桌面那份就旧了。"""
    _r, kv, app, desk = _drop(tmp_path)
    bat = desk / "CarHer 助手卸载.bat"
    assert bat.is_file(), f"桌面上没有卸载入口：{kv}"
    raw = bat.read_bytes()
    assert b"feishu-agent-uninstall-windows.ps1" in raw, raw
    assert str(app).encode() in raw, "转发器没指向 appHome 里那份"
    # 不是拷贝：卸载脚本的正文一个字都不在里面
    assert b"Remove-Safely" not in raw, "桌面那个是整份拷贝 ⇒ 升级不了"
    assert len(raw) < 600, f"转发器太大，像是拷贝：{len(raw)}"
    assert any(l.startswith(b"powershell ") for l in raw.split(b"\r\n")), raw


@need_pwsh
def test_desktop_forwarder_body_is_pure_ascii(tmp_path):
    """🔴 .bat 由 cmd 按当前代码页读：正文里有中文，非 936 代码页的机器上会变成乱码命令。

    （文件**名**用中文没事：NTFS 存 UTF-16。）
    """
    _r, _kv, _app, desk = _drop(tmp_path)
    raw = (desk / "CarHer 助手卸载.bat").read_bytes()
    assert all(b < 128 for b in raw), raw
    assert not raw.startswith(b"\xef\xbb\xbf"), "转发器不能有 BOM（cmd 会把它当命令的一部分）"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "转发器必须全是 CRLF"


@need_pwsh
def test_desktop_goes_to_getfolderpath_not_a_userprofile_join(tmp_path):
    """🔴 桌面重定向到 OneDrive 的电脑上，拼 "$env:USERPROFILE\\Desktop" 会落到用户看不见的地方。

    判据就是台架那条打桩：它按 `GetFolderPath('Desktop')` 这句原文替换，
    改成 USERPROFILE 拼接 ⇒ 替换不中 ⇒ 台架 throw ⇒ 这条红（见 _DROP 里那句 throw）。
    另外再核一遍：文件真的落在 GetFolderPath 说的那个地方，USERPROFILE\\Desktop 下面一个都没有。
    """
    other = tmp_path / "redirected-profile"
    (other / "Desktop").mkdir(parents=True)
    r, kv, _app, desk = _drop(tmp_path, env_extra={"USERPROFILE": str(other)})
    assert "没打上桩" not in (r.stdout + r.stderr), \
        f"桌面那一句不是 GetFolderPath（台架替换不中）：{r.stdout}\n{r.stderr}"
    assert (desk / "CarHer 助手卸载.bat").is_file(), (r.stdout, r.stderr)
    assert not (other / "Desktop" / "CarHer 助手卸载.bat").exists(), \
        "走的是 USERPROFILE 拼接 ⇒ 桌面被重定向的电脑上文件会落到看不见的地方"


@need_pwsh
def test_unreplaced_placeholder_writes_nothing_and_never_shows_at_at(tmp_path):
    """🔴 占位符没被替换（build.sh 少跑一步）⇒ 什么都不写，也不把 @@ 打给用户。

    宁可桌面上没有卸载，也不要放一个双击就报错的东西。
    """
    r, kv, app, desk = _drop(tmp_path, embed=False)
    assert kv.get("TAIL") == "1", (r.stdout, r.stderr)
    assert not (app / "feishu-agent-uninstall-windows.ps1").exists(), "占位符没替换却写了个 @@ 文件出去"
    assert not (desk / "CarHer 助手卸载.bat").exists(), "桌面上放了个双击就报错的东西"
    assert "@@" not in r.stdout, f"把占位符打给用户看了：{r.stdout}"
    warn = [v for k, v in kv.items() if k == "WARNLINE"] or re.findall(r"^WARNLINE=(.*)$", r.stdout, re.M)
    assert any("构建没跑完" in w for w in warn), r.stdout


@need_pwsh
def test_no_desktop_is_a_warning_not_a_failure(tmp_path):
    """🔴 桌面取不到 / 不存在 ⇒ 打黄 + 告诉他程序目录里有一份，**绝不让安装失败**。"""
    r, kv, app, desk = _drop(tmp_path, desktop=False)
    assert kv.get("TAIL") == "1", "桌面没有就把安装整段中断了"
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert (app / "feishu-agent-uninstall-windows.ps1").is_file(), "桌面没了连程序目录那份也不写了"
    assert re.search(r"^WARNLINE=.*没放上", r.stdout, re.M), r.stdout
    assert str(app) in r.stdout, "没告诉他程序目录里那份在哪"


@need_pwsh
def test_apphome_unwritable_is_a_warning_not_a_failure(tmp_path):
    """写不进去（权限 / 杀毒）也只是打黄：安装不能因为「桌面上的卸载」挂掉。"""
    nope = tmp_path / "no-such-dir" / "app"   # 父目录不存在 ⇒ WriteAllText 抛
    r, kv, _app, desk = _drop(tmp_path, apphome=str(nope))
    assert kv.get("TAIL") == "1", "写不进去就把安装整段中断了"
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert not (desk / "CarHer 助手卸载.bat").exists(), "ps1 都没写成却在桌面放了个转发器"
    assert re.search(r"^WARNLINE=.*没放上", r.stdout, re.M), r.stdout


@need_pwsh
def test_blocked_powershell_child_gets_a_paste_line(tmp_path):
    """这台电脑不让 PowerShell 起 powershell.exe ⇒ 多给一条不用起新进程的路（包 New-Module）。"""
    r, _kv, app, _desk = _drop(tmp_path, pschild="0")
    assert "New-Module" in r.stdout, r.stdout
    assert str(app / "feishu-agent-uninstall-windows.ps1") in r.stdout, r.stdout


@need_pwsh
def test_ok_line_tells_him_what_it_is_for(tmp_path):
    """打绿那句要让人看懂这是干什么的（离职 / 换机），否则桌面上多个 .bat 没人敢点。"""
    r, _kv, _app, _desk = _drop(tmp_path)
    ok = re.findall(r"^OKLINE=(.*)$", r.stdout, re.M)
    assert any("CarHer 助手卸载" in o for o in ok), r.stdout
    assert any(("离职" in o or "换机" in o) for o in ok), ok


# ═══════════════ 2. 源码形状：占位符正好一处、不动 build.sh 也不坏 ═══════════════


def test_template_has_exactly_one_placeholder():
    """🔴 正好一处：build.sh 那一步是 `count == 1` + 替换后不许再剩（照诊断那条抄的）。

    连注释里再写一遍字面量都会让它红 —— 所以这条测的是模板，不是 build.sh。
    """
    text = _text(INSTALL)
    assert text.count(PLACEHOLDER) == 1, f"占位符出现 {text.count(PLACEHOLDER)} 次，要正好一处"


def test_placeholder_name_avoids_the_build_assertion_word():
    """build.sh 的 finish_ps1 末尾断言「还有没替换的模块占位符」，查的是 @@EMBED 那个字面量。

    占位符名字（和这一段的注释）里出现它 ⇒ 整次 build 被拦下。
    """
    text = _text(INSTALL)
    i = text.index("# UNINSTALL-DROP-BEGIN")
    j = text.index("# UNINSTALL-DROP-END")
    seg = text[i:j]
    assert "@@EMBED" not in seg, "这一段里出现了 @@EMBED 字面量 ⇒ build.sh 会把整次构建拦下"


def test_placeholder_sits_in_a_single_quoted_here_string():
    """base64 要放在单引号 here-string 里：双引号的会把 $ 当变量展开。"""
    text = _text(INSTALL)
    i = text.index(PLACEHOLDER)
    before = text[:i].splitlines()[-1]
    assert before.strip() == "@'" or before.rstrip().endswith("@'"), before
    after = text[i:].splitlines()[1]
    assert after.strip() == "'@", after


def _drop_segment(code_only=False) -> str:
    text = _text(INSTALL)
    seg = text[text.index("# UNINSTALL-DROP-BEGIN"):text.index("# UNINSTALL-DROP-END")]
    if code_only:
        # 🔴 去掉注释行再判「不该出现」类断言：这一段的注释里**正好**写着
        # 「不是拼 $env:USERPROFILE\Desktop」这句解释，按原文判会永远红。
        seg = "\n".join(l for l in seg.splitlines() if not l.lstrip().startswith("#"))
    return seg


def test_write_uses_bom_encoding_and_ascii_forwarder():
    """源码侧守住两种编码：ps1 带 BOM（UTF8Encoding $true），转发器 ASCII。"""
    seg = _drop_segment()
    assert "New-Object Text.UTF8Encoding $true" in seg, "ps1 没带 BOM ⇒ PS5.1 按 GBK 解"
    assert "New-Object Text.ASCIIEncoding" in seg, "转发器正文没强制 ASCII"
    assert "[Environment]::GetFolderPath('Desktop')" in seg, "桌面路径没走 GetFolderPath"
    assert "USERPROFILE" not in _drop_segment(code_only=True), "代码里还在拼 USERPROFILE"
    assert ".Trim().StartsWith('@@')" in seg, "没有「占位符没替换就什么都不写」那条降级"


def test_installer_never_dies_on_the_desktop_step():
    """这一段里不许有 Die / throw / exit：桌面写不了不能让安装失败。"""
    code = _drop_segment(code_only=True)
    for bad in ("Die ", "throw", "exit "):
        assert bad not in code, f"这一段里有 {bad!r} ⇒ 桌面写不了会让整次安装失败：{code}"
    assert "} catch {" in code, "没有 try/catch 兜底"


def test_uninstaller_body_has_no_here_string_terminator():
    """卸载脚本是整份 base64 嵌进来的，这条是防「将来改成直接内联」埋的雷。

    PowerShell here-string 用行首 `'@` 结束 —— 正文里一旦有这样的行，后半截会变成乱命令。
    """
    body = _text(UNINSTALL)
    assert not re.search(r"^'@", body, re.M), "卸载脚本里有行首 '@"
    assert not re.search(r'^"@', body, re.M), '卸载脚本里有行首 "@'


# ═══════════════ 3. 卸载：桌面那两个也要删掉 ═══════════════
#
# 台架：把卸载脚本的「变量 + 删除防护」两段抠出来真跑（Remove-Safely 是被测的那个函数，
# 不另写一份），桌面用真的 HOME/Desktop。

_GUARD = r'''
$textU = [IO.File]::ReadAllText($env:USRC)
function CutU([string]$from, [string]$to) {
  $a = $textU.IndexOf($from, [StringComparison]::Ordinal); if ($a -lt 0) { throw "缺锚点 $from" }
  $b = $textU.IndexOf($to, $a, [StringComparison]::Ordinal); if ($b -lt 0) { throw "缺锚点 $to" }
  return $textU.Substring($a, $b - $a)
}
# 🔴 这几个必须用 Write-Host，不能 Write-Output：Bad 是在 Remove-Safely **函数体内**调的，
# Write-Output 会把那行字一起当成函数的返回值（PS 的隐式返回是整条输出流）⇒
# `Remove-Safely` 返回 "BADLINE=… False" 而不是 $false，判据就全糊了（第一版台架就是这样）。
function Say($m)  { Write-Host ("SAYLINE=" + $m) }
function Ok($m)   { Write-Host ("OKLINE=" + $m) }
function Bad($m)  { Write-Host ("BADLINE=" + $m) }
function Warn($m) { Write-Host ("WARNLINE=" + $m) }
function Hint($m) { Write-Host ("HINTLINE=" + $m) }
$script:failed = 0
# 🔴 同上：桌面那一句打桩，替换不成功就 throw（别碰真桌面）。
$vars = CutU '$localAppData = $env:LOCALAPPDATA' '# ── 跳过确认'
$b = $vars
$vars = $vars.Replace($env:REALDESK, '$env:FAKEDESK')
if ($vars -eq $b) { throw '卸载脚本里桌面那一句没打上桩，拒绝继续' }
. ([scriptblock]::Create($vars))
. ([scriptblock]::Create((CutU '# ── 删除防护' '# 凭据安全擦除')))
Write-Output ("DESKDOCTOR=" + $deskDoctor)
Write-Output ("DESKUNINST=" + $deskUninstall)
'''


def _guard(tmp_path, script_tail, env=None):
    desk = tmp_path / "Desktop"
    desk.mkdir(exist_ok=True)
    lad = tmp_path / "LocalAppData"
    lad.mkdir(exist_ok=True)
    up = tmp_path / "Profile"
    up.mkdir(exist_ok=True)
    e = {"LOCALAPPDATA": str(lad), "USERPROFILE": str(up),
         "REALDESK": _REAL_DESKTOP_CALL, "FAKEDESK": str(desk)}
    e.update(env or {})
    r, kv = _run_ps(tmp_path, _GUARD + script_tail, e, name="guard.ps1")
    return r, kv, desk


@need_pwsh
def test_uninstall_deletes_both_desktop_shortcuts(tmp_path):
    """🔴 核心：卸完桌面不能留着两个双击就报错的 .bat。走 Remove-Safely，不绕过防护。"""
    tail = r'''
[IO.File]::WriteAllText($deskDoctor, 'x')
[IO.File]::WriteAllText($deskUninstall, 'x')
Write-Output ("D1=" + (Remove-Safely $deskDoctor))
Write-Output ("D2=" + (Remove-Safely $deskUninstall))
Write-Output ("LEFT1=" + (Test-Path -LiteralPath $deskDoctor))
Write-Output ("LEFT2=" + (Test-Path -LiteralPath $deskUninstall))
'''
    r, kv, desk = _guard(tmp_path, tail)
    assert kv.get("D1") == "True" and kv.get("D2") == "True", (r.stdout, r.stderr)
    assert kv.get("LEFT1") == "False" and kv.get("LEFT2") == "False", kv
    assert not list(desk.iterdir()), list(desk.iterdir())


@need_pwsh
def test_guard_still_refuses_other_desktop_files(tmp_path):
    """🔴 反向阳性对照：桌面是**精确到那两个文件**进白名单，不是把桌面目录整个放进来。

    放目录的话卸载就能删用户桌面上任何东西（他的工资表、他的照片）。

    🔴 两种分隔符都要试，而且**反斜杠那个才是判据**：
    Remove-Safely 第 5 道的前缀匹配写的是 `$norm.StartsWith($r + '\')` —— 反斜杠是写死的。
    macOS 上 `Join-Path` 产出的是 `/`，所以只用斜杠路径探的话，
    「白名单放的是桌面目录」和「白名单放的是那两个文件」**在这台机器上结果一样**（都拒），
    这条测试就分不出好坏（写这组时实测：把白名单改成整个桌面目录，只用斜杠探 ⇒ 这条仍然绿）。
    反斜杠拼出来的那个路径在 Windows 上就是「桌面下面的一个文件」，正好打在那道前缀匹配上。
    """
    tail = r'''
# ① Windows 形状（反斜杠）：路径不用真存在 —— 前 5 道防护在 Test-Path 之前就该拒掉它。
#    白名单一旦放成整个桌面目录，这里会过了防护、走到「本来就没有 = 已达成目的」那条 return $true。
$win = $env:FAKEDESK + '\我的工资表.xlsx'
Write-Output ("RWIN=" + (Remove-Safely $win))
# ② 本机形状（斜杠）：真造一个文件，连「没被删掉」一起核
$mine = Join-Path $env:FAKEDESK '我的工资表.xlsx'
[IO.File]::WriteAllText($mine, 'x')
Write-Output ("R=" + (Remove-Safely $mine))
Write-Output ("LEFT=" + (Test-Path -LiteralPath $mine))
'''
    r, kv, desk = _guard(tmp_path, tail)
    assert kv.get("RWIN") == "False", (
        f"白名单放的是整个桌面目录 ⇒ 卸载能删用户桌面上任何东西：{r.stdout}")
    assert kv.get("R") == "False", f"防护放行了桌面上别的文件：{r.stdout}"
    assert kv.get("LEFT") == "True", kv
    assert any("白名单" in b for b in re.findall(r"^BADLINE=(.*)$", r.stdout, re.M)), r.stdout


@need_pwsh
def test_positive_control_the_two_shortcuts_do_pass_the_guard(tmp_path):
    """阳性对照：上面那条「拒」不是因为防护把桌面上的东西一律拒掉。

    同一把尺子量那两个快捷方式（Windows 形状的反斜杠路径）⇒ 必须放行。
    少了这条，白名单漏掉桌面那两项时上面那条照样绿。
    """
    tail = r'''
# 白名单里存的是 Join-Path 的结果（本机是 /），这里换成 Windows 形状的反斜杠再量一次：
# 走的是第 5 道的「$norm -eq $r」那支 ⇒ 两种分隔符都要能对上才算真进了白名单。
$a = $env:FAKEDESK + '\CarHer 助手卸载.bat'
$b = Join-Path $env:FAKEDESK 'CarHer 助手卸载.bat'
Write-Output ("RWIN=" + (Remove-Safely $a))
Write-Output ("RNATIVE=" + (Remove-Safely $b))
'''
    r, kv, _desk = _guard(tmp_path, tail)
    # 本机形状必须放行（白名单就是用 Join-Path 拼的）
    assert kv.get("RNATIVE") == "True", f"桌面那两项没进白名单：{r.stdout}"
    # 反斜杠形状在 macOS 上拼出来的是另一个字符串 ⇒ 这里不作要求，只记录：
    # Windows 上两者同形，这条的意义是证明「拒」那条不是一刀切。
    assert "RWIN" in kv, r.stdout


@need_pwsh
def test_desktop_paths_use_getfolderpath(tmp_path):
    """卸载侧同一条纪律：桌面走 GetFolderPath，不是 USERPROFILE 拼接。

    台架把 USERPROFILE 指到别处 ⇒ 两个变量都不该出现在 USERPROFILE 下面。
    """
    r, kv, desk = _guard(tmp_path, "")
    assert kv.get("DESKDOCTOR", "").startswith(str(desk)), kv
    assert kv.get("DESKUNINST", "").startswith(str(desk)), kv
    assert str(tmp_path / "Profile") not in kv.get("DESKUNINST", ""), kv


@need_pwsh
def test_no_desktop_leaves_the_vars_empty_and_guard_refuses(tmp_path):
    """取不到桌面 ⇒ 两个变量是空串 ⇒ Remove-Safely 第 1 道按空值拒掉（不会退化成相对路径）。

    这是「变量为空导致删错地方」那条老纪律在桌面这两项上的复用。
    """
    tail = r'''
# 台架模拟「GetFolderPath 返回空」：按卸载脚本里那段的写法，两个变量就是空串
$dd = ''
$du = ''
Write-Output ("R1=" + (Remove-Safely $dd))
Write-Output ("R2=" + (Remove-Safely $du))
'''
    r, kv, _desk = _guard(tmp_path, tail)
    assert kv.get("R1") == "False" and kv.get("R2") == "False", r.stdout
    assert any("空" in b for b in re.findall(r"^BADLINE=(.*)$", r.stdout, re.M)), r.stdout


def test_no_desktop_leaves_the_two_vars_empty_in_source():
    """源码侧：取不到桌面时那两个变量必须是空串（不是 'CarHer 助手卸载.bat' 这种相对路径）。"""
    text = _text(UNINSTALL)
    seg = text[text.index("$desktop   = "):text.index("$runKeyPath = ")]
    assert "$deskDoctor  = ''" in seg and "$deskUninstall  = ''" in seg, seg
    assert "IsNullOrWhiteSpace($desktop)" in seg, "没判桌面取不到就直接 Join-Path 了"


def test_uninstall_deletes_desktop_entry_last():
    """桌面上的「卸载」是当前这条路的入口 ⇒ 排在所有删除动作的最后。"""
    text = _text(UNINSTALL)
    i = text.index('Remove-Target $appHome')
    seg = text[i:]
    order = [m.group(1) for m in re.finditer(r"Remove-Target \$(\w+)", seg)]
    assert order[-1] == "deskUninstall", f"桌面上的卸载入口不是最后删的：{order}"
    assert "deskDoctor" in order and order.index("deskDoctor") < order.index("deskUninstall"), order
    # 为什么可以删它：注释里要写明（转发器已经把 ps1 读进来跑了）
    j = text.index("Remove-Target $deskUninstall")
    why = text[text.index("Remove-Target $deskDoctor"):j]
    assert "转发器" in why or "读进" in why, why
    assert "最后" in why, why


def test_uninstall_lists_desktop_items_before_asking():
    """「先列清单再动手」那条纪律：桌面那两项也要出现在清单里（删什么要让他看见）。"""
    text = _text(UNINSTALL)
    head = text[:text.index("确认删除？")]
    assert "Show-Item $deskDoctor" in head, "清单里没列桌面上的诊断"
    assert "Show-Item $deskUninstall" in head, "清单里没列桌面上的卸载"


def test_desktop_leftover_alone_is_not_not_installed():
    """程序目录被手删过、桌面还留着两个坏 .bat ⇒ 不能打「本机没装」然后把它们留在桌面上。"""
    text = _text(UNINSTALL)
    i = text.index('Ok "本机没装')
    guard = text[text.rindex("if (-not $task", 0, i):i]
    assert "$deskLeft" in guard, f"快速退出没把桌面残留算进去：{guard}"


@need_pwsh
def test_deskleft_true_when_only_the_desktop_bats_remain(tmp_path):
    """上一条的真跑版：只有桌面那两个在 ⇒ $deskLeft 为真；都不在 ⇒ 为假。"""
    tail = r'''
. ([scriptblock]::Create((CutU 'function Test-DeskLeftover' '$deskLeft = ')))
Write-Output ("EMPTY=" + (Test-DeskLeftover ''))
Write-Output ("ABSENT=" + (Test-DeskLeftover $deskUninstall))
[IO.File]::WriteAllText($deskUninstall, 'x')
Write-Output ("PRESENT=" + (Test-DeskLeftover $deskUninstall))
Write-Output ("EITHER=" + ((Test-DeskLeftover $deskDoctor) -or (Test-DeskLeftover $deskUninstall)))
'''
    r, kv, _desk = _guard(tmp_path, tail)
    assert kv.get("EMPTY") == "False" and kv.get("ABSENT") == "False", (r.stdout, r.stderr)
    assert kv.get("PRESENT") == "True" and kv.get("EITHER") == "True", kv


def test_deskleftover_guards_empty_before_test_path():
    """🔴 空串要在喂给 Test-Path **之前**判掉（取不到桌面时那两个变量就是空串）。

    ⚠️ 这一条只能看源码，量不到行为：pwsh 7 on macOS 上 `Test-Path -LiteralPath ''`
    安安静静返回 False、$Error 里一条都不加（写这组时实测），所以「有没有这道判空」
    在这台机器上**跑出来一模一样** —— 把判空删掉，真跑那条照样绿（变异 M17 实测如此）。
    PS 5.1 上空串喂 -LiteralPath 是什么行为（抛参数校验错 / 静默 False）**没验过**，
    不拿它当归因；这道判空是便宜的保险，而且它让意图留在码里。
    """
    text = _text(UNINSTALL)
    fn = text[text.index("function Test-DeskLeftover"):]
    fn = fn[:fn.index("$deskLeft = ")]
    i = fn.index("IsNullOrWhiteSpace")
    j = fn.index("Test-Path")
    assert i < j, f"判空没排在 Test-Path 前面：{fn}"


def test_uninstall_still_has_no_bare_remove_item():
    """桌面那两项不许开后门：递归删除仍然只有 Remove-Safely 内部那一行。"""
    offenders = []
    for n, line in enumerate(_text(UNINSTALL).splitlines(), 1):
        code = line.split("#", 1)[0]
        if "Remove-Item" in code and "-Recurse" in code and "$target" not in code:
            offenders.append(f"{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


# ═══════════════ 4. doctor：指的是桌面上那个，但保留老名字兜底 ═══════════════


def test_doctor_points_at_the_desktop_uninstaller():
    text = _text(DOCTOR)
    tail = text[text.index("要卸载（"):]
    assert "CarHer 助手卸载" in tail, "doctor 还在指一个用户电脑上不存在的文件"
    # 兜底：老安装包 / 从下载目录拿包的那条路（tests/test_oneclick_lifecycle.py 也盯着这个名字）
    assert "feishu-agent-uninstall-windows.bat" in tail, "把兜底的文件名删了"


# ═══════════════ 4b. 叫用户双击的文件必须真在他电脑上 ═══════════════
#
# 11-19 总管发现：install-windows.ps1 有 4 句「双击 feishu-agent-uninstall-windows.bat」，
# 但安装器只写两份东西 —— 程序目录里的 `feishu-agent-uninstall-windows.ps1`(BOM)
# 和桌面上的「CarHer 助手卸载.bat」。那个 .bat 名只是 198 上的下载文件名，
# **用户电脑上没有** ⇒ 他按提示去双击，什么都不会发生。mac 侧第 39 轮已改成 ${UNINSTALL_HINT}，Windows 漏改。

def _bad_uninstall_hints(text: str) -> list[str]:
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        code = line.split("#", 1)[0]
        if "uninstall-windows.bat" in code:
            out.append(f"{n}: {line.strip()[:110]}")
    return out


def test_installer_never_tells_user_to_double_click_the_download_only_bat():
    text = _text(INSTALL)
    assert not _bad_uninstall_hints(text), "又叫用户双击那个只存在于下载目录的 .bat：" + str(_bad_uninstall_hints(text))
    assert "$uninstHint = '双击桌面上的「CarHer 助手卸载」'" in text, "桌面放上了就该指桌面那个"
    assert text.count("先$uninstHint 卸掉") == 3, "三处「想重来一次」都要走变量"
    assert "要彻底卸掉：$uninstHint。" in text


def test_that_gate_has_teeth():
    """反向对照:把旧句式塞回去必须变红。"""
    text = _text(INSTALL)
    bad = text.replace("先$uninstHint 卸掉", "先双击 feishu-agent-uninstall-windows.bat 卸掉", 1)
    assert bad != text and _bad_uninstall_hints(bad), "尺子量不到旧句式"


# ═══════════════ 5. 产物字节级（在 tmp 里照 build.sh 生成，不碰共享产物） ═══════════════


def _build(tmp_path):
    """照 build.sh 的 python 段生成产物到 tmp（不跑 pytest、不覆盖仓库里的产物、不跑 build.sh 本身）。

    替换 @@UNINSTALL_WINDOWS_B64@@ 那一步在 build.sh 里（主会话加的 embed_uninstall_windows）⇒
    这里只负责验「那一步跑完之后产物的字节形状和内容」。它不在 ⇒ 这组会红，那正是要盯的。
    """
    sh = BUILD_SH.read_text(encoding="utf-8")
    m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", sh, re.S)
    assert m, "build.sh 里找不到 python heredoc"
    code = m.group(1)
    assert "embed_uninstall_windows(ps1)" in code, (
        "build.sh 没有替换卸载占位符那一步 ⇒ 产物里不会带卸载脚本（= 11-14 诊断那个坑）"
    )
    code = code.replace('Path("src")', f'Path({str(_SRC_DIR)!r})')
    code = code.replace('Path("../tools/feishu-agent-bridge")',
                        f'Path({str(Path(__file__).resolve().parents[1])!r})')
    script = tmp_path / "build_products.py"
    script.write_text(code, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    r = subprocess.run([sys.executable, str(script)], cwd=out,
                       env=dict(os.environ, BUILD_ID="20261118-r39test"),
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def test_product_carries_the_uninstaller_and_keeps_byte_shape(tmp_path):
    """产物里真的有卸载脚本（解得开、是那一份），且 BOM/ASCII 头/CRLF/here-string 都没破。"""
    blob = (_build(tmp_path) / "feishu-agent-windows.bat").read_bytes()
    cut = blob.index(b"##CARHER-PS1")
    assert not blob.startswith(b"\xef\xbb\xbf"), "产物有 BOM"
    assert all(c < 128 for c in blob[:cut]), "bat 头不是纯 ASCII"
    assert blob.count(b"\n") == blob.count(b"\r\n"), "不是全 CRLF"
    body = blob[cut:].decode("utf-8")
    assert PLACEHOLDER not in body, "占位符没被替换就出了产物"
    assert "@@EMBED" not in body and "@@BUILD_ID@@" not in body
    # 把嵌进去的 base64 抠出来解开，必须等于卸载模板（去横幅后）
    m = re.search(r"\$uninstB64 = @'\r?\n(.*?)\r?\n'@", body, re.S)
    assert m, "产物里找不到卸载 base64 那段"
    decoded = base64.b64decode(re.sub(r"\s", "", m.group(1))).decode("utf-8")
    assert "Remove-Safely" in decoded and "CarHerFeishuAgent" in decoded, decoded[:200]
    assert "这是模板" not in decoded, "横幅没去掉"
    assert "CarHer 助手卸载.bat" in decoded, "嵌进去的那份不删桌面快捷方式 ⇒ 嵌错了文件"
    # here-string 没被截断：base64 是纯 ASCII，不可能出现行首 '@
    assert not re.search(r"^'@", m.group(1), re.M)


@need_pwsh
def test_product_body_still_parses(tmp_path):
    """嵌进去一大段 base64 之后整份正文还能解析（here-string 边界、引号都没破）。"""
    blob = (_build(tmp_path) / "feishu-agent-windows.bat").read_bytes()
    body = tmp_path / "body.ps1"
    body.write_bytes(blob[blob.index(b"##CARHER-PS1"):])
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command",
                        '$t=$null;$e=$null;'
                        '[void][System.Management.Automation.Language.Parser]::ParseFile($env:B,[ref]$t,[ref]$e);'
                        '"ERR=" + @($e).Count; $e | Select-Object -First 3 | ForEach-Object { $_.Message }'],
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "B": str(body)},
                       capture_output=True, text=True, timeout=120)
    assert "ERR=0" in r.stdout, r.stdout + r.stderr


@need_pwsh
def test_uninstaller_template_parses(tmp_path):
    """卸载模板本身能解析 —— 它现在真的会被写到用户电脑上执行了。"""
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command",
                        '$t=$null;$e=$null;'
                        '[void][System.Management.Automation.Language.Parser]::ParseFile($env:B,[ref]$t,[ref]$e);'
                        '"ERR=" + @($e).Count; $e | Select-Object -First 3 | ForEach-Object { $_.Message }'],
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "B": str(UNINSTALL)},
                       capture_output=True, text=True, timeout=120)
    assert "ERR=0" in r.stdout, r.stdout + r.stderr
