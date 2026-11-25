"""11-16 her remote Windows「X 没能把它设成开机自启：Access is denied.」（a-user02，PS 5.1.19041，重装）。

修法（install-windows.ps1 的 Set-CarherAutoStart）：计划任务建不了 / 改不了 ⇒ 退到当前用户登录自启（HKCU Run 键，不要管理员），
当场拉起一次；删不掉的同名旧任务跑的就是这个 run-agent.ps1 ⇒ 直接 Start-ScheduledTask 用它；两套自启不同时生效
（删不掉也停不了的旧任务由 run-agent.ps1 开头的会话锁兜住）。doctor 认得 Run 键；uninstall 删 Run 键；
提示不再教「以管理员身份运行」；专用 Python 下载失败先说「重新双击会再下一次」。

怎么测（Mac 上没有 ScheduledTasks 模块、没有注册表）：在 pwsh 里用**同名函数**盖掉 Register-ScheduledTask / Get-ItemProperty /
Start-Process 等（PowerShell 命令解析 function 先于 cmdlet），跑的是**从脚本里原样抠出来的代码**，测试里不另写一份：
  - 安装器：所有顶层函数 + 固定配置那几行 + 「Get-NewLog 定义结束 → Start-Sleep -Seconds 4」那一段（新旧两版都有这两个锚点）。
  - run-agent.ps1：抠出 $runnerText 生成真文件，真起两个 pwsh 跑它（第二个必须马上退、不起 python）。
  - doctor：[2] 那一节；uninstall：整份脚本。
红绿：HERWIN_1116_SRC_DIR=<放旧版三个 .ps1 的目录> 让整组用例去跑旧版（应当红），默认跑仓库里的新版（应当绿）。
⚠️ 全部没在真 Windows / PS5.1 上跑过：计划任务的 ACL、Run 键在登录时真被执行、Local\\ 互斥量在「任务 / Explorer」两种父进程间共享
—— 都是按文档推的，绿只说明「逻辑和文案按设计走」。pwsh 找法同 scripts/ai-portal/installer/test_installer.py，没有就 skip。
"""
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SRC_DIR = Path(os.environ.get("HERWIN_1116_SRC_DIR") or (_REPO / "feishu-agent-oneclick" / "src"))
INSTALL = _SRC_DIR / "install-windows.ps1"
DOCTOR = _SRC_DIR / "doctor-windows.ps1"
UNINSTALL = _SRC_DIR / "uninstall-windows.ps1"


def _pwsh():
    for c in (os.environ.get("CARHER_PWSH"), shutil.which("pwsh"),
              str(_REPO / ".work" / "ai-portal" / "installer" / "pwsh" / "pwsh")):
        if c and Path(c).is_file() and os.access(c, os.X_OK):
            return c
    return None


PWSH = _pwsh()
need_pwsh = pytest.mark.skipif(not PWSH, reason="本机没有 pwsh")


def _run_ps(tmp_path, script, env, timeout=120):
    harness = tmp_path / "harness.ps1"
    harness.write_text(script, encoding="utf-8")
    base = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path) + "/"}
    base.update({k: v for k, v in env.items() if v is not None})
    r = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(harness)],
                       env=base, capture_output=True, text=True, timeout=timeout)
    kv = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^([A-Z_]+)=(.*)$", line)
        if m and m.group(1) not in kv:
            kv[m.group(1)] = m.group(2)
    return r, kv


# ─────────────── 安装器：开机自启那一段（原样抠出来跑） ───────────────

# 假世界：一个计划任务表 + 一个 Run 键。stub 全是 advanced function（才吃得下 -ErrorAction），
# 出错用 Write-Error ⇒ 和真 CDXML cmdlet 一样受 -ErrorAction / $ErrorActionPreference 控制。
_FAKE_WORLD = r'''
$global:T = @{}
$global:REG = @{}
$global:CALLS = New-Object System.Collections.ArrayList
function global:Log($s) { [void]$global:CALLS.Add($s) }
function New-ScheduledTaskAction { [CmdletBinding()] param($Execute, $Argument)
  Log "Action|$Execute|$Argument"
  if ($env:NO_MODULE -eq '1') { throw "The term 'New-ScheduledTaskAction' is not recognized as a name of a cmdlet" }
  [pscustomobject]@{ Execute = $Execute; Arguments = $Argument } }
function New-ScheduledTaskTrigger { [CmdletBinding()] param([switch]$AtLogOn) 'trigger' }
function New-ScheduledTaskSettingsSet { [CmdletBinding()] param([switch]$AllowStartIfOnBatteries,
  [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit, $RestartCount, $RestartInterval) 'settings' }
function Get-ScheduledTask { [CmdletBinding()] param($TaskName)
  $t = $global:T[$TaskName]
  if (-not $t) { Write-Error "No MSFT_ScheduledTask objects found with property 'TaskName' equal to '$TaskName'."; return }
  [pscustomobject]@{ TaskName = $TaskName; State = $t.State; Actions = @([pscustomobject]@{ Execute = 'powershell.exe'; Arguments = $t.Args }) } }
function Unregister-ScheduledTask { [CmdletBinding()] param($TaskName, [switch]$Confirm)
  Log 'Unregister'
  $t = $global:T[$TaskName]
  if (-not $t) { Write-Error "No MSFT_ScheduledTask objects found"; return }
  if ($t.Locked) { Write-Error 'Access is denied.'; return }
  $global:T.Remove($TaskName) }
function Register-ScheduledTask { [CmdletBinding()] param($TaskName, $Action, $Trigger, $Settings, $Description)
  Log 'Register'
  if ($env:POLICY_DENY -eq '1') { Write-Error 'Access is denied.'; return }
  $t = $global:T[$TaskName]
  if ($t) { if ($t.Locked) { Write-Error 'Access is denied.' } else { Write-Error 'Cannot create a file when that file already exists.' }; return }
  $global:T[$TaskName] = @{ Args = $Action.Arguments; State = 'Ready'; Locked = $false; StartDenied = ($env:NEW_STARTDENY -eq '1') }
  [pscustomobject]@{ TaskName = $TaskName } }
function Start-ScheduledTask { [CmdletBinding()] param($TaskName)
  Log 'StartTask'
  $t = $global:T[$TaskName]
  if (-not $t) { Write-Error "No MSFT_ScheduledTask objects found"; return }
  if ($t.StartDenied) { Write-Error 'Access is denied.'; return }
  if ($t.State -eq 'Disabled') { Write-Error 'The task is disabled.'; return }
  Log 'TASK-STARTED' }
function Disable-ScheduledTask { [CmdletBinding()] param($TaskName)
  Log 'Disable'
  $t = $global:T[$TaskName]
  if (-not $t) { Write-Error "No MSFT_ScheduledTask objects found"; return }
  if ($t.Locked) { Write-Error 'Access is denied.'; return }
  $t.State = 'Disabled'; [pscustomobject]@{ TaskName = $TaskName; State = 'Disabled' } }
function Get-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name)
  if (-not $global:REG.ContainsKey($Name)) { Write-Error "Property $Name does not exist at path $LiteralPath."; return }
  $o = [pscustomobject]@{}; $o | Add-Member -NotePropertyName $Name -NotePropertyValue $global:REG[$Name]; $o }
function Set-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name, $Value)
  Log "SetReg|$LiteralPath"
  if ($env:REG_DENY -eq '1') { Write-Error 'Requested registry access is not allowed.'; return }
  if ($env:REG_REVERT -ne '1') { $global:REG[$Name] = $Value } }
function Remove-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name) Log 'RemoveReg'; [void]$global:REG.Remove($Name) }
function New-Item { [CmdletBinding()] param($Path, $ItemType, [switch]$Force)
  Log "NewItem|$Path"; if ($env:REG_DENY -eq '1') { Write-Error 'Requested registry access is not allowed.' } }
function Start-Process { [CmdletBinding()] param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru, [switch]$NoNewWindow)
  Log "Spawn|$FilePath|$ArgumentList|$WindowStyle"
  if ($env:SPAWN_DENY -eq '1') { Write-Error 'This command cannot be run due to the error: Access is denied.' } }
'''

INSTALL_HARNESS = r'''
$ErrorActionPreference = 'Stop'
$tok = $null; $perr = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SRC, [ref]$tok, [ref]$perr)
if ($perr.Count) { Write-Output "PARSE_ERROR=$($perr[0].Message)"; exit 3 }
$text = [IO.File]::ReadAllText($env:SRC)
$Fn = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($f in $ast.FindAll({ param($n) $n -is $Fn }, $false)) { . ([scriptblock]::Create($f.Extent.Text)) }
$env:LOCALAPPDATA = $env:FAKE_LAD
foreach ($st in $ast.EndBlock.Statements) {
  if ($st -is [System.Management.Automation.Language.AssignmentStatementAst] -and
      @('appHome','runner','taskName','runKeyPath','runnerArgs','psExe','runKeyCmd') -contains $st.Left.VariablePath.UserPath) {
    . ([scriptblock]::Create($st.Extent.Text))
  }
}
''' + _FAKE_WORLD + r'''
# 「上一次装下的那个任务」：默认就是老版本安装器注册的 action（跑同一个 run-agent.ps1）
if ($env:T_EXISTS -eq '1') {
  $oldArgs = if ($env:T_ARGS) { $env:T_ARGS } else { "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`"" }
  $global:T[$taskName] = @{ Args = $oldArgs; State = 'Ready'; Locked = ($env:T_LOCKED -eq '1'); StartDenied = ($env:T_STARTDENY -eq '1') }
}
if ($env:REG_PRE) { $global:REG[$taskName] = $env:REG_PRE }
$global:died = ''
function Die($m) { $global:died = "$m"; throw 'CARHER-TEST-DIE' }
$psChildAllowed = $true
$gn = $ast.FindAll({ param($n) $n -is $Fn -and $n.Name -eq 'Get-NewLog' }, $false) | Select-Object -First 1
$from = $gn.Extent.EndOffset
$to = $text.IndexOf('Start-Sleep -Seconds 4', $from, [StringComparison]::Ordinal)
$uncaught = ''
try { . ([scriptblock]::Create($text.Substring($from, $to - $from))) } catch { if ("$_" -ne 'CARHER-TEST-DIE') { $uncaught = "$_" } }
$t = $global:T[$taskName]
Write-Output ("DIED=" + [bool]$global:died)
Write-Output ("DIEMSG=" + ($global:died -replace "`r?`n", ' / '))
Write-Output ("UNCAUGHT=" + $uncaught)
Write-Output ("MODE=" + $autoStartMode)
Write-Output ("REUSED=" + $autoStart.Reused)
Write-Output ("LEFT=" + $autoStart.Left)
Write-Output ("REG=" + $global:REG[$taskName])
Write-Output ("TASK=" + $(if ($t) { "$($t.State)|$($t.Args)" } else { 'none' }))
Write-Output ("CALLS=" + ($global:CALLS -join ';'))
Write-Output ("RUNNER=" + $runner)
Write-Output ("RUNNERARGS=" + $runnerArgs)
Write-Output ("RUNKEYCMD=" + $runKeyCmd)
'''


def _install(tmp_path, **env):
    lad = tmp_path / "LocalAppData"
    lad.mkdir()
    # Mac 的 pwsh 里 Join-Path 'C:\Windows' 会报「找不到驱动器 C」⇒ SystemRoot 指一个真目录
    r, kv = _run_ps(tmp_path, INSTALL_HARNESS,
                    {"SRC": str(INSTALL), "FAKE_LAD": str(lad), "SystemRoot": str(tmp_path / "Windows"), **env})
    assert "PARSE_ERROR" not in kv, kv
    assert "DIED" in kv, f"harness 没跑完：\n{r.stdout}\n{r.stderr}"
    return r, kv


def _no_double_autostart(kv):
    """两套自启不能同时生效：Run 键在 ⇒ 不许还有一个「Ready」的计划任务（删不掉也停不了的 stuck 由 runner 会话锁兜住，单独断言）。"""
    task_ready = kv["TASK"].startswith("Ready|")
    assert not (kv["REG"] and task_ready and kv["LEFT"] != "stuck"), kv


def _ran_runner_now(kv):
    return [c for c in kv["CALLS"].split(";") if c.startswith("Spawn|")]


@need_pwsh
def test_fresh_install_still_uses_scheduled_task(tmp_path):
    """阳性对照：普通电脑照旧注册计划任务，不碰 Run 键、不自己起进程。"""
    _, kv = _install(tmp_path)
    assert kv["DIED"] == "False" and not kv["UNCAUGHT"], kv
    assert kv["TASK"].startswith("Ready|") and "TASK-STARTED" in kv["CALLS"], kv
    assert kv["REG"] == "" and not _ran_runner_now(kv), kv
    # 计划任务的 action 参数就是 $runnerArgs（Run 键用的也是这一串 ⇒ 两边等价）
    if kv.get("RUNNERARGS"):
        assert f"Action|powershell.exe|{kv['RUNNERARGS']}" in kv["CALLS"], kv


@need_pwsh
def test_elevated_leftover_task_same_runner_is_reused(tmp_path):
    """现场假设①：以前用管理员身份装过 ⇒ 同名任务删不掉、注册报 Access is denied；它跑的就是同一个 run-agent.ps1 ⇒ 直接拉起来用。"""
    r, kv = _install(tmp_path, T_EXISTS="1", T_LOCKED="1")
    assert kv["DIED"] == "False", f"还是停在这一步了：{kv['DIEMSG']}"
    assert kv["MODE"] == "task" and kv["REUSED"] == "True", kv
    assert kv["CALLS"].count("TASK-STARTED") == 1 and kv["REG"] == "", kv
    assert "直接用它" in r.stdout


@need_pwsh
def test_elevated_leftover_task_start_denied_falls_back_to_run_key(tmp_path):
    """旧任务删不掉、改不了、也拉不起来 ⇒ 写 Run 键 + 当场起一次；那个旧任务停不了 ⇒ 说明一句、靠 runner 会话锁兜住。"""
    r, kv = _install(tmp_path, T_EXISTS="1", T_LOCKED="1", T_STARTDENY="1")
    assert kv["DIED"] == "False", f"还是停在这一步了：{kv['DIEMSG']}"
    assert kv["MODE"] == "runkey" and kv["LEFT"] == "stuck", kv
    assert kv["REG"] == kv["RUNKEYCMD"], kv
    spawned = _ran_runner_now(kv)
    assert spawned and spawned[0].endswith(f"|{kv['RUNNERARGS']}|Hidden"), kv
    assert "登录电脑时自动启动" in r.stdout and "只会留一份在跑" in r.stdout


@need_pwsh
def test_policy_forbids_creating_tasks_falls_back_to_run_key(tmp_path):
    """现场假设②：公司管控不让普通用户建计划任务 ⇒ Run 键，零操作、不找管理员。"""
    r, kv = _install(tmp_path, POLICY_DENY="1")
    assert kv["DIED"] == "False", f"还是停在这一步了：{kv['DIEMSG']}"
    assert kv["MODE"] == "runkey" and kv["LEFT"] == "" and kv["TASK"] == "none", kv
    assert kv["REG"] == kv["RUNKEYCMD"] and _ran_runner_now(kv), kv
    assert "Access is denied." in r.stdout, "退到 Run 键时要把计划任务那步的原话带出来（截图里能对上号）"


@need_pwsh
def test_task_registered_but_start_denied_removes_own_task(tmp_path):
    """注册成功、Start 被拒 ⇒ 退 Run 键前必须把自己刚注册的任务删掉，否则下次登录两边各起一份。"""
    _, kv = _install(tmp_path, NEW_STARTDENY="1")
    assert kv["DIED"] == "False", kv["DIEMSG"]
    assert kv["MODE"] == "runkey" and kv["TASK"] == "none" and kv["LEFT"] == "", kv
    _no_double_autostart(kv)


@need_pwsh
def test_leftover_task_running_another_runner_is_not_reused(tmp_path):
    """删不掉的旧任务跑的是别处的 run-agent.ps1（比如别的账号装的）⇒ 不拿它当自己的，走 Run 键。"""
    _, kv = _install(tmp_path, T_EXISTS="1", T_LOCKED="1",
                     T_ARGS='-NoProfile -File "C:\\Users\\itadmin\\AppData\\Local\\CarHerFeishuAgent\\run-agent.ps1"')
    assert kv["DIED"] == "False", kv["DIEMSG"]
    assert kv["MODE"] == "runkey" and "TASK-STARTED" not in kv["CALLS"], kv


@need_pwsh
def test_scheduled_tasks_module_missing_falls_back(tmp_path):
    """New-ScheduledTaskAction 都没有（模块被拿掉 / 被拦）⇒ 原来在 try 外面，直接成了「没接住的错误」。"""
    _, kv = _install(tmp_path, NO_MODULE="1")
    assert not kv["UNCAUGHT"] and kv["DIED"] == "False", kv
    assert kv["MODE"] == "runkey" and kv["REG"] == kv["RUNKEYCMD"], kv


@need_pwsh
def test_reinstall_back_to_task_removes_run_key(tmp_path):
    """以前退到过 Run 键、这次计划任务能建了 ⇒ Run 键要删掉（两套自启不能同时生效）。"""
    _, kv = _install(tmp_path, REG_PRE='"C:\\x\\powershell.exe" -File "C:\\x\\run-agent.ps1"')
    assert kv["DIED"] == "False" and kv["TASK"].startswith("Ready|"), kv
    assert kv["REG"] == "", f"计划任务和 Run 键同时在：{kv}"
    assert not _ran_runner_now(kv), kv


@need_pwsh
def test_both_paths_blocked_dies_without_admin_advice(tmp_path):
    """两条路都不行才停，提示里不许再有「以管理员身份运行」（那正是这个问题的来源之一）。"""
    _, kv = _install(tmp_path, POLICY_DENY="1", REG_DENY="1")
    assert kv["DIED"] == "True", kv
    msg = kv["DIEMSG"]
    assert "以管理员身份运行" not in msg and "管理员身份" not in msg, msg
    assert "下一步" in msg and "发给 IT" in msg and "Run" in msg, msg
    assert not _ran_runner_now(kv), kv


@need_pwsh
def test_run_key_reverted_by_security_software_is_not_trusted(tmp_path):
    """写了不等于写进去了：读回来不对 ⇒ 当没写成（不能报「以后登录会自动启动」）。"""
    _, kv = _install(tmp_path, POLICY_DENY="1", REG_REVERT="1")
    assert kv["DIED"] == "True" and kv["REG"] == "", kv


@need_pwsh
def test_run_key_command_matches_task_action(tmp_path):
    """Run 键的命令 = 计划任务 action：同一个 powershell.exe 参数串、路径带引号（用户名里有空格 / 中文也行）。"""
    lad_name = "LocalAppData"
    _, kv = _install(tmp_path, POLICY_DENY="1")
    cmd, args, runner = kv["RUNKEYCMD"], kv["RUNNERARGS"], kv["RUNNER"]
    assert lad_name in runner and runner.endswith("run-agent.ps1"), kv
    assert args == f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{runner}"', args
    assert re.match(r'^"[^"]*powershell\.exe" ', cmd) and cmd.endswith(" " + args), cmd


def test_installer_never_tells_people_to_run_as_admin():
    """静态：安装器里任何会打到屏幕上的行都不许教「以管理员身份运行」（注释里讲来龙去脉可以）。"""
    offenders = [f"{i}: {line.strip()}" for i, line in enumerate(INSTALL.read_text(encoding="utf-8-sig").splitlines(), 1)
                 if "以管理员身份运行" in line and not line.lstrip().startswith("#")]
    assert not offenders, "\n".join(offenders)


def test_blocked_verdict_has_run_key_branch():
    """静态：登录自启模式下「当场没拉起来」要按 Run 键说（注销再登录），不能落到「计划任务被拦」那句。"""
    text = INSTALL.read_text(encoding="utf-8-sig")
    anchor = text.index("(Get-TaskLaunchVerdict $runnerProcs.Count (Get-NewLog)) -eq 'blocked'")
    block = text[anchor: anchor + 2500]
    assert "if ($autoStartMode -eq 'runkey')" in block, "blocked 判据没分 Run 键模式"
    rk = block[block.index("if ($autoStartMode -eq 'runkey')"): block.index("$taskResult = ''")]
    assert "注销再登录" in rk and "以管理员身份运行" not in rk


# ─────────────── run-agent.ps1：同一会话只留一层循环 ───────────────

RUNNER_HARNESS = r'''
$ErrorActionPreference = 'Stop'
$tok = $null; $perr = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SRC, [ref]$tok, [ref]$perr)
$config = $env:R_CONFIG; $claudeDir = $env:R_CLAUDEDIR; $appHome = $env:R_APPHOME; $venvPy = $env:R_VENVPY; $logDir = $env:R_LOGDIR
$st = $ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $_.Left.VariablePath.UserPath -eq 'runnerText' } | Select-Object -First 1
. ([scriptblock]::Create($st.Extent.Text))
[IO.File]::WriteAllText($env:R_OUT, $runnerText, (New-Object Text.UTF8Encoding $true))
Write-Output 'WROTE=1'
'''


def _calls(path):
    return path.read_text().splitlines() if path.exists() else []


def _wait(pred, sec):
    end = time.time() + sec
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.3)
    return pred()


def _kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


@need_pwsh
def test_second_runner_in_same_session_exits_without_starting_python(tmp_path):
    """计划任务删不掉又写了 Run 键 ⇒ 登录时两边都可能拉一份 run-agent.ps1。第二份必须马上退、不起 python
    （不然它的 python 抢不到单实例锁，每 5 秒重启一次刷日志）。上一份被强杀后，新的一份要能接上。"""
    app = tmp_path / "app"
    logs = app / "logs"
    logs.mkdir(parents=True)
    calls = tmp_path / "calls.txt"
    fakepy = tmp_path / "fakepy"
    fakepy.write_text('#!/bin/sh\necho "$$ $*" >> "$CALLS"\nexec sleep 40\n')
    fakepy.chmod(0o755)
    runner = tmp_path / "run-agent.ps1"
    _, kv = _run_ps(tmp_path, RUNNER_HARNESS, {
        "SRC": str(INSTALL), "R_CONFIG": str(app / "config.toml"), "R_CLAUDEDIR": str(tmp_path),
        "R_APPHOME": str(app), "R_VENVPY": str(fakepy), "R_LOGDIR": str(logs), "R_OUT": str(runner)})
    assert kv.get("WROTE") == "1", kv
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path) + "/", "CALLS": str(calls)}
    argv = [PWSH, "-NoProfile", "-NonInteractive", "-File", str(runner)]
    # 🔴 Unix 上 .NET 的 Local\ 互斥量按会话隔离 ⇒ 三份都留在本会话（只另起进程组，方便整组杀）
    first = subprocess.Popen(argv, env=env, process_group=0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    second = third = None
    try:
        assert _wait(lambda: len(_calls(calls)) >= 1, 30), "第一份 runner 没把 python 拉起来"
        second = subprocess.Popen(argv, env=env, process_group=0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            rc = second.wait(timeout=25)
        except subprocess.TimeoutExpired:
            rc = None
        assert rc == 0, "第二份 runner 没有马上退出（同一会话里起了两层循环）"
        time.sleep(1)
        assert len(_calls(calls)) == 1, f"第二份 runner 也起了 python：{_calls(calls)}"
        _kill_group(first)   # 强杀 = 锁被遗弃（Windows 上是 AbandonedMutexException）
        third = subprocess.Popen(argv, env=env, process_group=0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert _wait(lambda: len(_calls(calls)) >= 2, 30), "上一份被强杀后，新的一份没接上（锁没放出来）"
    finally:
        for p in (first, second, third):
            if p is not None:
                _kill_group(p)


# ─────────────── doctor：认得 Run 键模式 ───────────────

DOCTOR_HARNESS = r'''
$ErrorActionPreference = 'Continue'
$text = [IO.File]::ReadAllText($env:SRC)
function Cut([string]$from, [string]$to) {
  $a = $text.IndexOf($from, [StringComparison]::Ordinal); if ($a -lt 0) { throw "缺锚点 $from" }
  $b = $text.IndexOf($to, $a, [StringComparison]::Ordinal); if ($b -lt 0) { throw "缺锚点 $to" }
  return $text.Substring($a, $b - $a)
}
. ([scriptblock]::Create((Cut '$script:failed = 0' '$appHome  = ')))
function Get-ScheduledTask { [CmdletBinding()] param($TaskName)
  if ($env:T_STATE) { [pscustomobject]@{ TaskName = $TaskName; State = $env:T_STATE } }
  else { Write-Error "No MSFT_ScheduledTask objects found with property 'TaskName' equal to '$TaskName'." } }
function Get-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name)
  if ($env:REG_VAL) { $o = [pscustomobject]@{}; $o | Add-Member -NotePropertyName $Name -NotePropertyValue $env:REG_VAL; $o }
  else { Write-Error "Property $Name does not exist at path $LiteralPath." } }
function Get-CimInstance { [CmdletBinding()] param($ClassName, $Filter)
  if ($env:PROC -eq '1') { [pscustomobject]@{ ProcessId = 4242; CommandLine = '"C:\x\venv\Scripts\python.exe" -m bridge.app' } } }
function Test-Path { [CmdletBinding()] param($LiteralPath)
  if ($env:TESTPATH_FALSE -eq '1') { return $false }; return $true }
. ([scriptblock]::Create((Cut '$taskName = ' '$buildInfo = ')))
. ([scriptblock]::Create((Cut '# ── 2. 后台任务' '# ── 3. 实例唯一性')))
Write-Output "FAILED=$script:failed"
Write-Output "KICK=$kickHint"
'''

_RUNKEY = '"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -File "C:\\x\\run-agent.ps1"'


def _doctor(tmp_path, **env):
    r, kv = _run_ps(tmp_path, DOCTOR_HARNESS, {"SRC": str(DOCTOR), **env})
    assert "FAILED" in kv, f"harness 没跑完：\n{r.stdout}\n{r.stderr}"
    return r.stdout, kv


@need_pwsh
def test_doctor_run_key_mode_is_not_reported_missing(tmp_path):
    out, kv = _doctor(tmp_path, REG_VAL=_RUNKEY, PROC="1")
    assert "后台任务没注册" not in out, out
    assert "登录电脑时自动启动" in out and "进程在跑" in out, out
    assert kv["FAILED"] == "0", out


@need_pwsh
def test_doctor_run_key_mode_with_stuck_old_task(tmp_path):
    out, kv = _doctor(tmp_path, REG_VAL=_RUNKEY, T_STATE="Ready", PROC="1")
    assert "登录电脑时自动启动" in out and "删不掉，不碍事" in out, out
    assert kv["FAILED"] == "0", out
    assert "ScheduledTask" not in kv["KICK"], "Run 键模式下还叫他 Stop/Start-ScheduledTask（旧任务删不掉，只会再报错）"


@need_pwsh
def test_doctor_run_key_mode_process_down_gives_logoff_hint(tmp_path):
    out, kv = _doctor(tmp_path, REG_VAL=_RUNKEY)
    assert "后台任务没注册" not in out and "进程不在" in out, out
    assert "注销再登录" in out and "Start-ScheduledTask" not in out, out


@need_pwsh
def test_doctor_task_mode_unchanged(tmp_path):
    """阳性对照：计划任务那种照旧（状态 + 进程），拉一把的命令还是 Stop/Start-ScheduledTask。"""
    out, kv = _doctor(tmp_path, T_STATE="Ready", PROC="1")
    assert "任务已注册（状态：Ready）" in out and "进程在跑" in out and kv["FAILED"] == "0", out
    out2, _ = _doctor(tmp_path, T_STATE="Ready")
    assert "Start-ScheduledTask -TaskName CarHerFeishuAgent" in out2, out2
    out3, kv3 = _doctor(tmp_path)
    assert "后台任务没注册" in out3 and kv3["FAILED"] == "1", out3


# ─────────────── uninstall：Run 键也删 ───────────────

UNINSTALL_HARNESS = r'''
$global:REG = @{}
if ($env:REG_VAL) { $global:REG['CarHerFeishuAgent'] = $env:REG_VAL }
$global:T = $null
if ($env:T_STATE) { $global:T = @{ State = $env:T_STATE; Locked = ($env:T_LOCKED -eq '1') } }
function Get-ScheduledTask { [CmdletBinding()] param($TaskName)
  if ($global:T) { [pscustomobject]@{ TaskName = $TaskName; State = $global:T.State } } else { Write-Error 'No MSFT_ScheduledTask objects found' } }
function Stop-ScheduledTask { [CmdletBinding()] param($TaskName) }
function Unregister-ScheduledTask { [CmdletBinding()] param($TaskName, [switch]$Confirm)
  if (-not $global:T) { Write-Error 'not found'; return }
  if ($global:T.Locked) { Write-Error 'Access is denied.'; return }
  $global:T = $null }
function Disable-ScheduledTask { [CmdletBinding()] param($TaskName)
  if ($global:T.Locked) { Write-Error 'Access is denied.'; return }; $global:T.State = 'Disabled' }
function Get-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name)
  if ($global:REG.ContainsKey($Name)) { $o = [pscustomobject]@{}; $o | Add-Member -NotePropertyName $Name -NotePropertyValue $global:REG[$Name]; $o }
  else { Write-Error "Property $Name does not exist at path $LiteralPath." } }
function Remove-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name) [void]$global:REG.Remove($Name) }
function Get-CimInstance { [CmdletBinding()] param($ClassName, $Filter) }
& $env:SRC
Write-Output "REG_AFTER=$($global:REG['CarHerFeishuAgent'])"
Write-Output "TASK_AFTER=$(if ($global:T) { $global:T.State } else { 'none' })"
'''


def _uninstall(tmp_path, **env):
    lad = tmp_path / "LocalAppData"
    prof = tmp_path / "Profile"
    (lad / "CarHerFeishuAgent").mkdir(parents=True)
    (lad / "CarHerFeishuAgent" / "run-agent.ps1").write_text("x")
    prof.mkdir()
    r, kv = _run_ps(tmp_path, UNINSTALL_HARNESS, {
        "SRC": str(UNINSTALL), "LOCALAPPDATA": str(lad), "USERPROFILE": str(prof), "CARHER_UNINSTALL_YES": "1", **env})
    assert "REG_AFTER" in kv, f"harness 没跑完：\n{r.stdout}\n{r.stderr}"
    assert not (lad / "CarHerFeishuAgent").exists(), r.stdout
    return r.stdout, kv


@need_pwsh
def test_uninstall_removes_run_key(tmp_path):
    out, kv = _uninstall(tmp_path, REG_VAL=_RUNKEY)
    assert kv["REG_AFTER"] == "", f"卸载后登录自启还在 ⇒ 下次登录还会去拉 run-agent.ps1：\n{out}"
    assert "登录自启已删掉" in out and "本机已清干净" in out, out


@need_pwsh
def test_uninstall_undeletable_task_is_explained_not_failed(tmp_path):
    """以前用管理员身份装的任务卸载时同样删不掉：不再教「手动跑 Unregister-ScheduledTask」（同样会失败），说清它已无害。"""
    out, kv = _uninstall(tmp_path, REG_VAL=_RUNKEY, T_STATE="Ready", T_LOCKED="1")
    assert kv["REG_AFTER"] == "", out
    assert "手动跑：Unregister-ScheduledTask" not in out, out
    assert "删不掉" in out and "不碍事" in out and "本机已清干净" in out, out


# ─────────────── 专用 Python 下载失败：先说「重新双击会再下一次」 ───────────────

PY_HARNESS = r'''
$ErrorActionPreference = 'Stop'
$tok = $null; $perr = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SRC, [ref]$tok, [ref]$perr)
$Fn = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($f in $ast.FindAll({ param($n) $n -is $Fn }, $false)) { . ([scriptblock]::Create($f.Extent.Text)) }
foreach ($st in $ast.EndBlock.Statements) {
  if ($st -is [System.Management.Automation.Language.AssignmentStatementAst] -and
      @('appHome','bundledPy','pyRetryHint','pyMinMinor','script:pyDlFailed') -contains $st.Left.VariablePath.UserPath) {
    . ([scriptblock]::Create($st.Extent.Text))
  }
}
function Invoke-WebRequest { [CmdletBinding()] param($Uri, $OutFile, [switch]$UseBasicParsing, $TimeoutSec)
  throw 'Unable to connect to the remote server' }
$ok = Get-CarherBundledPy
Write-Output "RESULT=$ok"
Write-Output "FLAG=$script:pyDlFailed"
'''


@need_pwsh
def test_bundled_python_download_failure_says_double_click_again(tmp_path):
    sysroot = tmp_path / "Windows"
    (sysroot / "System32").mkdir(parents=True)
    (sysroot / "System32" / "tar.exe").write_text("x")   # Mac 上 pwsh 把 'System32\tar.exe' 里的 \ 当成目录分隔符
    r, kv = _run_ps(tmp_path, PY_HARNESS, {
        "SRC": str(INSTALL), "LOCALAPPDATA": str(tmp_path / "lad"), "SystemRoot": str(sysroot),
        "PROCESSOR_ARCHITECTURE": "AMD64"})
    assert kv.get("RESULT") == "False", r.stdout + r.stderr
    out = r.stdout
    # 11-17 起 198 下不动先退官方兜底,两边都没下完 ⇒ 报「下载不完整」(半截留着,重跑接着下);第一句建议仍是重新双击
    assert ("专用 Python 没下载成功" in out or "专用 Python 下载不完整" in out) and "重新双击一次" in out, out
    assert out.index("重新双击一次") < (out.index("python.org") if "python.org" in out else len(out)), out
    assert kv.get("FLAG") == "True", "下载失败没记下来 ⇒ 后面的提示还会先叫人去 python.org"


def test_no_python_die_puts_python_org_last():
    """静态：下载失败那条 Die 第一步是「重新双击」，python.org 在最后。"""
    text = INSTALL.read_text(encoding="utf-8-sig")
    start = text.index("if (-not $py -and $script:pyDlFailed) {")
    body = text[start: text.index('"@', start)]
    assert body.index("重新双击") < body.index("python.org"), body
