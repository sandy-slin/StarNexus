"""第 38 轮 · 11-17 同事实机（Win11 10.0.26300）「每次开机弹一个空的 PowerShell 窗口」。

修法（install-windows.ps1 的 Get-CarherLaunch）：计划任务 action / Run 键 / 当场拉起（Start-Process，两处）都改成
`%WINDIR%\\System32\\conhost.exe --headless "<powershell.exe>" <原参数>`；conhost.exe 不在、或系统早于 Win10 21H2（19044）
⇒ 退回原来的写法。已装好的机器：自动更新时 Update-CarherLaunchCommand 把计划任务 / Run 键换成新命令（失败全吞）。
doctor [2] 多一行：启动方式包没包 conhost --headless + 默认终端（HKCU\\Console\\%%Startup DelegationConsole）。

和 test_herwin_autostart_1116.py 同一种测法：pwsh 里用同名函数盖掉 ScheduledTasks / 注册表 cmdlet，跑的是从脚本里原样抠出来的代码。
⚠️ 「Windows Terminal 里 -WindowStyle Hidden 不生效」「conhost --headless 不弹窗」都没在真 Windows 上看过；绿只说明命令按设计拼。
变异：R38W_SRC_DIR=<改过的模板目录> 让整组去跑那份。
"""
import os
from pathlib import Path

import pytest

from tests.test_herwin_autostart_1116 import _FAKE_WORLD, _run_ps, need_pwsh

_REPO = Path(__file__).resolve().parents[3]
_SRC_DIR = Path(os.environ.get("R38W_SRC_DIR") or (_REPO / "feishu-agent-oneclick" / "src"))
INSTALL = _SRC_DIR / "install-windows.ps1"
DOCTOR = _SRC_DIR / "doctor-windows.ps1"

_HARNESS = r'''
$ErrorActionPreference = 'Stop'
$tok = $null; $perr = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:SRC, [ref]$tok, [ref]$perr)
if ($perr.Count) { Write-Output "PARSE_ERROR=$($perr[0].Message)"; exit 3 }
$Fn = [System.Management.Automation.Language.FunctionDefinitionAst]
foreach ($f in $ast.FindAll({ param($n) $n -is $Fn }, $false)) { . ([scriptblock]::Create($f.Extent.Text)) }
$env:LOCALAPPDATA = $env:FAKE_LAD
$assign = @{}
foreach ($st in $ast.EndBlock.Statements) {
  if ($st -is [System.Management.Automation.Language.AssignmentStatementAst] -and $st.Left.VariablePath -and
      -not $assign.ContainsKey($st.Left.VariablePath.UserPath)) { $assign[$st.Left.VariablePath.UserPath] = $st.Extent.Text }
}
foreach ($n in @('appHome', 'runner', 'taskName', 'runKeyPath', 'runnerArgs', 'psExe')) { . ([scriptblock]::Create($assign[$n])) }
''' + _FAKE_WORLD + r'''
if ($env:BUILD) { $global:REG['CurrentBuildNumber'] = $env:BUILD }
# 计划任务带上 Execute（_FAKE_WORLD 里写死成 powershell.exe），好测「已经换过就不再换」
function Get-ScheduledTask { [CmdletBinding()] param($TaskName)
  $t = $global:T[$TaskName]
  if (-not $t) { Write-Error "No MSFT_ScheduledTask objects found with property 'TaskName' equal to '$TaskName'."; return }
  $exe = if ($t.Exe) { $t.Exe } else { 'powershell.exe' }
  [pscustomobject]@{ TaskName = $TaskName; State = $t.State; Actions = @([pscustomobject]@{ Execute = $exe; Arguments = $t.Args }) } }
function Set-ScheduledTask { [CmdletBinding()] param($TaskName, $Action)
  Log "SetTask|$($Action.Execute)|$($Action.Arguments)"
  if ($env:SETTASK_DENY -eq '1') { Write-Error 'Access is denied.'; return }
  if ($env:SETTASK_NOOP -ne '1') { $global:T[$TaskName].Args = $Action.Arguments; $global:T[$TaskName].Exe = $Action.Execute }
  [pscustomobject]@{ TaskName = $TaskName } }
function Get-BridgeProcs { @() }
$logPath = Join-Path $env:FAKE_LAD 'agent.log'; $logMark = 0
. ([scriptblock]::Create($assign['runKeyCmd']))
if ($env:T_EXISTS -eq '1') {
  $conhostExe = Join-Path $env:SystemRoot 'System32\conhost.exe'
  $global:T[$taskName] = @{ Args = $(if ($env:T_ARGS) { $env:T_ARGS.Replace('<RUNNER>', $runner).Replace('<PSEXE>', $psExe).Replace('<CONHOST>', $conhostExe) } else { $runnerArgs }); State = 'Ready'
                           Locked = $false; StartDenied = $false
                           Exe = $(if ($env:T_EXE -eq '<CONHOST>') { $conhostExe } elseif ($env:T_EXE) { $env:T_EXE } else { $null }) }
}
if ($env:REG_PRE) { $global:REG[$taskName] = $env:REG_PRE.Replace('<RUNNER>', $runner) }
$res = ''
switch ($env:ACT) {
  'install' { $res = (Set-CarherAutoStart).Mode }
  'soft'    { $res = (Invoke-CarherSoftRestart).Mode }
  'fix'     { $res = Update-CarherLaunchCommand }
}
$t = $global:T[$taskName]
Write-Output ("RESULT=" + $res)
Write-Output ("CALLS=" + ($global:CALLS -join ';'))
Write-Output ("REG=" + $global:REG[$taskName])
Write-Output ("TASK=" + $(if ($t) { "$($t.Exe)|$($t.Args)" } else { 'none' }))
Write-Output ("RUNNER=" + $runner)
Write-Output ("RUNNERARGS=" + $runnerArgs)
Write-Output ("PSEXE=" + $psExe)
Write-Output ("RUNKEYCMD=" + $runKeyCmd)
'''


def _windir(tmp_path, conhost=True):
    w = tmp_path / "Windows"
    (w / "System32").mkdir(parents=True, exist_ok=True)
    if conhost:
        (w / "System32" / "conhost.exe").write_bytes(b"MZ")
    return w


def _go(tmp_path, act, conhost=True, build="26300", **env):
    lad = tmp_path / "Local AppData 用户"   # 空格 + 中文：路径要带引号
    lad.mkdir(exist_ok=True)
    w = _windir(tmp_path, conhost)
    e = {"SRC": str(INSTALL), "FAKE_LAD": str(lad), "SystemRoot": str(w), "WINDIR": str(w), "ACT": act, "BUILD": build}
    e.update(env)
    r, kv = _run_ps(tmp_path, _HARNESS, e)
    assert "PARSE_ERROR" not in kv and "CALLS" in kv, f"harness 没跑完：\n{r.stdout}\n{r.stderr}"
    kv["CONHOST"] = str(w / "System32" / "conhost.exe")
    return kv


def _headless_args(kv):
    return f'--headless "{kv["PSEXE"]}" {kv["RUNNERARGS"]}'


def _old_args(kv):
    return f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{kv["RUNNER"]}"'


@need_pwsh
def test_task_action_uses_conhost_headless(tmp_path):
    kv = _go(tmp_path, "install")
    assert kv["RESULT"] == "task", kv
    assert f"Action|{kv['CONHOST']}|{_headless_args(kv)}" in kv["CALLS"], kv
    # 计划任务认「是不是我们的」靠参数里有 run-agent.ps1 的路径：包一层之后也还在
    assert kv["RUNNER"] in kv["TASK"], kv


@need_pwsh
def test_run_key_and_spawn_use_conhost_headless(tmp_path):
    kv = _go(tmp_path, "install", POLICY_DENY="1")
    assert kv["RESULT"] == "runkey", kv
    want = f'"{kv["CONHOST"]}" {_headless_args(kv)}'
    assert kv["REG"] == want == kv["RUNKEYCMD"], kv
    assert f"Spawn|{kv['CONHOST']}|{_headless_args(kv)}|Hidden" in kv["CALLS"], kv


@need_pwsh
def test_soft_restart_without_loop_spawns_via_conhost(tmp_path):
    """自动更新时没有循环在跑 ⇒ 自己拉一份（原 802 行那处 Start-Process）。"""
    kv = _go(tmp_path, "soft")
    assert f"Spawn|{kv['CONHOST']}|{_headless_args(kv)}|Hidden" in kv["CALLS"], kv


@need_pwsh
@pytest.mark.parametrize("conhost,build", [(False, "26300"), (True, "17763"), (True, "")])
def test_falls_back_to_old_command(tmp_path, conhost, build):
    """阳性对照：没有 conhost.exe / 系统早于 21H2 / 版本读不到 ⇒ 原来那串一个字不差（老版本安装器注册的就是它）。"""
    kv = _go(tmp_path, "install", conhost=conhost, build=build)
    assert f"Action|powershell.exe|{_old_args(kv)}" in kv["CALLS"], kv
    assert "conhost" not in kv["CALLS"], kv
    (tmp_path / "rk").mkdir()
    kv = _go(tmp_path / "rk", "install", conhost=conhost, build=build, POLICY_DENY="1")
    assert kv["REG"] == f'"{kv["PSEXE"]}" {_old_args(kv)}', kv
    assert f"Spawn|{kv['PSEXE']}|{_old_args(kv)}|Hidden" in kv["CALLS"], kv


@need_pwsh
def test_auto_update_rewrites_old_task_and_run_key(tmp_path):
    """已经装好的机器：计划任务还是老命令 ⇒ 换成 conhost 那条；Run 键同理。"""
    kv = _go(tmp_path, "fix", T_EXISTS="1")
    assert kv["RESULT"] == "task", kv
    assert kv["TASK"] == f"{kv['CONHOST']}|{_headless_args(kv)}", kv
    (tmp_path / "b").mkdir()
    kv = _go(tmp_path / "b", "fix",
             REG_PRE='"C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe" -NoProfile -WindowStyle Hidden -File "<RUNNER>"')
    assert kv["RESULT"] == "runkey", kv
    assert kv["REG"] == f'"{kv["CONHOST"]}" {_headless_args(kv)}', kv


@need_pwsh
def test_auto_update_fix_is_idempotent_and_never_throws(tmp_path):
    # 计划任务 Execute + Args 都已经是 headless 那条 ⇒ 不再写（真正的幂等）
    (tmp_path / "a").mkdir()
    kv = _go(tmp_path / "a", "fix", T_EXISTS="1",
             T_EXE="<CONHOST>",
             T_ARGS='--headless "<PSEXE>" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "<RUNNER>"')
    assert kv["RESULT"] == "" and "SetTask|" not in kv["CALLS"], kv
    # 改任务被拒 ⇒ 不抛、不算改了；新代码里 $taskOk 留 false ⇒ 走 Run 键但无 REG_PRE ⇒ 空
    (tmp_path / "b").mkdir()
    kv = _go(tmp_path / "b", "fix", T_EXISTS="1", SETTASK_DENY="1")
    assert kv["RESULT"] == "" and "SetTask|" in kv["CALLS"], kv
    # 任务跑的是别处的 run-agent.ps1 ⇒ 不碰；Run 键指向别处的 run-agent.ps1（别的账号装的）⇒ 不碰
    (tmp_path / "c").mkdir()
    kv = _go(tmp_path / "c", "fix", T_EXISTS="1", T_ARGS='-File "C:\\Users\\itadmin\\x\\run-agent.ps1"',
             REG_PRE='"powershell.exe" -File "C:\\Users\\itadmin\\x\\run-agent.ps1"')
    assert kv["RESULT"] == "" and "SetReg" not in kv["CALLS"] and "SetTask|" not in kv["CALLS"], kv
    # 这台用不了 conhost ⇒ 什么都不动
    (tmp_path / "d").mkdir()
    kv = _go(tmp_path / "d", "fix", conhost=False, T_EXISTS="1")
    assert kv["RESULT"] == "" and "SetTask|" not in kv["CALLS"], kv


@need_pwsh
def test_auto_update_catches_stale_args_despite_correct_exe(tmp_path):
    """dd 发现的缺陷：Execute 已经是 conhost，但 Args 是旧格式（没有 --headless，
    直接跟的 powershell 参数）⇒ 旧代码只比 Execute 会放过（conhost==conhost），
    新代码比完整命令 $taskCmd != $want ⇒ 触发更新。
    注：如果 $runner 路径完全变了（重命名 Windows 账号），Test-CarherTaskIsOurs
    按设计不认那条任务（安全措施），Update 不会碰它——那种场景只能重装。"""
    (tmp_path / "a").mkdir()
    kv = _go(tmp_path / "a", "fix", T_EXISTS="1",
             T_EXE="<CONHOST>",
             T_ARGS='-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "<RUNNER>"')
    # Execute=conhost 但 Args 是旧格式 ⇒ 完整命令不同 ⇒ 应该更新
    assert "task" in kv["RESULT"], kv
    assert "SetTask|" in kv["CALLS"], kv


@need_pwsh
def test_set_task_silent_noop_falls_back_to_run_key(tmp_path):
    """Set-ScheduledTask 不抛异常但内容没写进去（安全软件还原 / 组策略锁了任务库）
    ⇒ 回读校验发现不对 ⇒ $taskOk 留 false ⇒ 不清 Run 键 ⇒ 退到 Run 键那条腿。
    这是守「兜底被误删」的：修之前 $taskOk 无条件 true 会清掉唯一能用的 Run 键。"""
    (tmp_path / "a").mkdir()
    kv = _go(tmp_path / "a", "fix", T_EXISTS="1", SETTASK_NOOP="1",
             REG_PRE='"<PSEXE>" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "<RUNNER>"')
    # Set-ScheduledTask 被调了（不抛），但回读发现没变 ⇒ $taskOk = false
    assert "SetTask|" in kv["CALLS"], kv
    # 不清 Run 键（RemoveReg 不出现）
    assert "RemoveReg" not in kv["CALLS"], kv
    # 退到 Run 键腿，Run 键被更新成新命令
    assert "runkey" in kv["RESULT"], kv


def test_every_launch_site_goes_through_get_carher_launch():
    """静态：老写法一处都不许剩（原 272 / 313 / 329 / 802 四处）。"""
    text = INSTALL.read_text(encoding="utf-8-sig")
    assert "-Execute 'powershell.exe' -Argument $runnerArgs" not in text
    assert "Start-Process -FilePath $psExe -ArgumentList $runnerArgs" not in text
    assert '$runKeyCmd = "`"$psExe`" $runnerArgs"' not in text
    assert text.count("Start-Process -FilePath $launch.Exe -ArgumentList $launch.Args -WindowStyle Hidden") == 2
    assert "New-ScheduledTaskAction -Execute $launch.TaskExe -Argument $launch.Args" in text
    # 自动更新那条路上调了修补，并且包在 try 里（失败不影响更新）
    i = text.index("$autoStart = Invoke-CarherSoftRestart")
    assert "try { $launchFixed = Update-CarherLaunchCommand" in text[i:i + 600]


# ─────────────── doctor [2]：启动方式 + 默认终端 ───────────────

_DOCTOR = r'''
$ErrorActionPreference = 'Continue'
$text = [IO.File]::ReadAllText($env:SRC)
function Cut([string]$from, [string]$to) {
  $a = $text.IndexOf($from, [StringComparison]::Ordinal); if ($a -lt 0) { throw "缺锚点 $from" }
  $b = $text.IndexOf($to, $a, [StringComparison]::Ordinal); if ($b -lt 0) { throw "缺锚点 $to" }
  return $text.Substring($a, $b - $a)
}
. ([scriptblock]::Create((Cut '$script:failed = 0' '$appHome  = ')))
function Get-ScheduledTask { [CmdletBinding()] param($TaskName)
  if ($env:T_EXE) { $args_ = if ($env:T_TASK_ARGS) { $env:T_TASK_ARGS } else { 'x' }
    [pscustomobject]@{ TaskName = $TaskName; State = 'Ready'; Actions = @([pscustomobject]@{ Execute = $env:T_EXE; Arguments = $args_ }) } }
  else { Write-Error "No MSFT_ScheduledTask objects found." } }
function Get-ItemProperty { [CmdletBinding()] param($LiteralPath, $Name)
  $v = if ($Name -eq 'DelegationConsole') { $env:DELEG } else { $env:REG_VAL }
  if ($v) { $o = [pscustomobject]@{}; $o | Add-Member -NotePropertyName $Name -NotePropertyValue $v; $o }
  else { Write-Error "Property $Name does not exist at path $LiteralPath." } }
function Get-CimInstance { [CmdletBinding()] param($ClassName, $Filter)
  [pscustomobject]@{ ProcessId = 4242; CommandLine = '"C:\x\venv\Scripts\python.exe" -m bridge.app' } }
function Test-Path { [CmdletBinding()] param($LiteralPath)
  if ($env:TESTPATH_FALSE -eq '1') { return $false }; return $true }
. ([scriptblock]::Create((Cut '$taskName = ' '$buildInfo = ')))
. ([scriptblock]::Create((Cut '# ── 2. 后台任务' '# ── 3. 实例唯一性')))
'''

_WT = "{2EACA947-7F5F-4CFA-BA87-8F7FBEEFBE69}"
_CONHOST_CLSID = "{B23D10C0-E52E-411E-9D5B-C09FDF709C7D}"


def _doctor(tmp_path, **env):
    r, _ = _run_ps(tmp_path, _DOCTOR, {"SRC": str(DOCTOR), "LOCALAPPDATA": str(tmp_path), **env})
    return r.stdout + r.stderr


@need_pwsh
def test_doctor_old_task_under_windows_terminal_warns(tmp_path):
    out = _doctor(tmp_path, T_EXE="powershell.exe", DELEG=_WT)
    assert "启动方式是老的那种；默认终端：Windows Terminal" in out, out
    assert "别关它" in out


@need_pwsh
def test_doctor_conhost_task_is_ok(tmp_path):
    out = _doctor(tmp_path, T_EXE="C:\\Windows\\System32\\conhost.exe", DELEG=_CONHOST_CLSID)
    assert "启动方式不弹窗（conhost --headless）；默认终端：旧控制台（conhost）" in out, out


@need_pwsh
def test_doctor_run_key_and_default_terminal(tmp_path):
    # Run 键是新命令；DelegationConsole 不在（= 让 Windows 决定）
    out = _doctor(tmp_path, REG_VAL='"C:\\Windows\\System32\\conhost.exe" --headless "C:\\p\\powershell.exe" -File "C:\\x\\run-agent.ps1"')
    assert "启动方式不弹窗" in out and "让 Windows 决定" in out, out
    # 阳性对照：Run 键是老命令 ⇒ 黄
    out = _doctor(tmp_path, REG_VAL='"C:\\p\\powershell.exe" -NoProfile -File "C:\\x\\run-agent.ps1"', DELEG=_WT)
    assert "启动方式是老的那种" in out, out
    # 只说形状：命令原文（含用户名路径）不上屏
    assert "run-agent.ps1" not in out, out


@need_pwsh
def test_doctor_stale_runner_path_reports_red(tmp_path):
    """任务指向的 run-agent.ps1 不存在（账号名改过 / 目录搬了）⇒ doctor 报红，不报假绿。"""
    # 计划任务：Execute=conhost，Args 里有一个不存在的 run-agent.ps1 路径
    out = _doctor(tmp_path,
                  T_EXE="C:\\Windows\\System32\\conhost.exe",
                  T_TASK_ARGS='--headless "C:\\ps\\powershell.exe" -File "C:\\Users\\old-name\\agent\\run-agent.ps1"',
                  TESTPATH_FALSE="1")
    assert "任务指向的脚本不存在" in out, out
    assert "重新双击" in out, out
    # Run 键同理
    (tmp_path / "rk").mkdir()
    out = _doctor(tmp_path / "rk",
                  REG_VAL='"C:\\conhost.exe" --headless "C:\\ps\\powershell.exe" -File "C:\\Users\\old-name\\agent\\run-agent.ps1"',
                  TESTPATH_FALSE="1")
    assert "Run 键指向的脚本不存在" in out, out
