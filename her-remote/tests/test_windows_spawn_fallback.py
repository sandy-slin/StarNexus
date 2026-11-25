"""公司电脑不许 PowerShell 起 powershell.exe（11-13 同事实机「拒绝访问」）时，飞书助手 Windows 安装器的退路。

Mac 上跑不了真 Windows，这里守的是「退路的代码还在、提示是对的」：
  - bat 头：powershell.exe 根本没起来 ⇒ cmd 看不到标记文件 ⇒ 打出一行能在 PowerShell 窗口里粘贴的命令
  - Invoke-WithDots：Start-Process 被拦 ⇒ 改成 & 直接跑；还被拦 ⇒ 中文 Die 说清是哪个程序
  - 计划任务：注册完实测有没有 runner 进程 / 新日志，没有 ⇒ 说「被拦住了」，不落到「起来又退出了」
  - 桌面诊断：探到被拦时给一行在本窗口里跑的命令（包 New-Module，exit 不关他的窗口）
"""

import os
import re

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_ONECLICK = os.path.join(_REPO, "feishu-agent-oneclick")
SRC = os.path.join(_ONECLICK, "src", "install-windows.ps1")
BUILD = os.path.join(_ONECLICK, "build.sh")
PRODUCT = os.path.join(_ONECLICK, "feishu-agent-windows.bat")


def _src():
    with open(SRC, encoding="utf-8-sig") as handle:
        return handle.read()


def _func(text, name):
    start = text.index(f"function {name}")
    nxt = re.search(r"^function ", text[start + 1:], re.M)
    return text[start: start + 1 + nxt.start()] if nxt else text[start:]


def test_invoke_with_dots_falls_back_when_spawn_denied():
    body = _func(_src(), "Invoke-WithDots")
    assert re.search(r"try\s*\{\s*\$proc = Start-Process", body), "Start-Process 外面没包 try"
    assert "& $Exe @Arguments" in body, "被拦后没有改成 & 直接跑"
    # PS5.1：EAP=Stop 下 pip 往 stderr 写 WARNING 就变终止错误 ⇒ 退路里必须先切 Continue
    fallback = body[body.index("catch"):]
    assert fallback.index("$ErrorActionPreference = 'Continue'") < fallback.index("& $Exe @Arguments")
    assert "发给 IT" in fallback, "退路也被拦时没告诉他找 IT"


def test_task_launch_verdict_only_blocks_when_nothing_ran():
    body = _func(_src(), "Get-TaskLaunchVerdict")
    assert "$RunnerCount -gt 0" in body and "$NewLog" in body
    assert "'blocked'" in body


def test_task_blocked_check_runs_before_crash_verdict():
    text = _src()
    blocked = text.index("(Get-TaskLaunchVerdict $runnerProcs.Count (Get-NewLog)) -eq 'blocked'")
    assert blocked < text.index("$restarted = ("), "被拦的判据要在「起来又退出了」之前，不然人会去查 Python"
    die = text[blocked: blocked + 1500]
    assert "被拦住了" in die and "powershell.exe -File $runner" in die
    # 任务留着不删：IT 放行后下次登录它自己起来
    assert "Unregister-ScheduledTask" not in die


def test_doctor_hint_wraps_in_new_module():
    text = _src()
    assert "$psChildAllowed = Test-PsChildAllowed" in text
    hint = text[text.index("if (-not $psChildAllowed) {\n        #"):]
    hint = hint[: hint.index("\n      }")]
    assert "New-Module ([scriptblock]::Create(" in hint, "诊断里有 exit 1，直接 & 会关掉他的窗口"
    assert "$doctorQ" in hint, "路径里的单引号没转义"


def test_bat_head_detects_powershell_never_started():
    with open(BUILD, encoding="utf-8") as handle:
        build = handle.read()
    head = build[build.index("HEAD = ("): build.index("##CARHER-PS1\\r\\n")]
    assert "CARHER_PSMARK" in head
    assert head.index("WriteAllText($env:CARHER_PSMARK") < head.index("$c=[IO.File]::ReadAllText(")
    assert 'if exist "%CARHER_PSMARK%" goto carher_ran' in head
    assert ":carher_ran" in head and "New-Module" in head
    assert all(ord(c) < 128 for c in head), "bat 头提示必须纯 ASCII"


def test_product_carries_new_bat_head():
    if not os.path.exists(PRODUCT):
        pytest.skip("产物还没生成（跑 build.sh）")
    with open(PRODUCT, "rb") as handle:
        blob = handle.read()
    head = blob[: blob.index(b"##CARHER-PS1")]
    assert b"CARHER_PSMARK" in head and b":carher_ran" in head, "产物还是旧 bat 头 ⇒ 重跑 build.sh"
