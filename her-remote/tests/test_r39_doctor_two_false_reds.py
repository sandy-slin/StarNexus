"""doctor 两个误报：「2 个进程在跑」和「没找到 claude 命令」。

现场 a-user …<机器>，11-20 15:54/15:55 两次体检，3 项红，其中 2 项是量具自己的毛病：

**① `X 有 2 个进程在跑` —— 误报。** 他自己查出来并告诉用户：
   「现在其实只有一个实例在跑，体检报的 2 个进程是误报。只有一条启动链：
     计划任务 → conhost → powershell run-agent.ps1」。他是对的。
   证据就在同一屏上：各自跑了 10125 秒、10125 秒（一模一样）+「其中一个是另一个的子进程：是」。
   = Windows 上 venv 的 python.exe 是转发壳，会再起一层真解释器，两个的命令行都是
   `-m bridge.app` ⇒ 被数两遍。代码注释里早写了这是「形状 ②」，但判决一直没用上它。
   🔴 后果不只是难看：它让人去「停掉多余的」—— 差点让他把唯一那个桥停了。

**② `X 没找到 claude 命令` —— 问错了对象。**
   `Get-Command claude` 查的是 doctor 这个窗口的 PATH，而桥是后台计划任务跑的，
   用 run-agent.ps1 里写死的那份（装机那一刻的 $claudeDir）。两份不是一回事。
   他 11-18 装 Claude Code 明明成功（上报 `√ 汇总:全部装好 (Claude Code 2.1.326)`），
   doctor 却说「没找到，装一下」⇒ 把人指到错路上。
   而他 11:03 那条 relay.py FileNotFoundError 的真病根恰恰是**桥那份 PATH 里没有**。
   装机侧早有正确做法（install-windows.ps1 的 Test-CarherClaudeRuns），doctor 侧一直没有。
"""

from __future__ import annotations

import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
_DOCTOR_WIN = os.path.join(_ONECLICK, "src", "doctor-windows.ps1")
_DOCTOR_MAC = os.path.join(_ONECLICK, "src", "doctor-mac.sh")
_INSTALL_WIN = os.path.join(_ONECLICK, "src", "install-windows.ps1")


@pytest.fixture(scope="module")
def doctor() -> str:
    return open(_DOCTOR_WIN, encoding="utf-8").read()


# ---------- ① 进程数：父子关系不算两份 ----------

def test_instance_count_excludes_child_processes(doctor):
    """只数「父进程不在这批里」的那些 —— venv 壳套真解释器是父子，算一份。"""
    idx = doctor.find("[3] 实例唯一性")
    assert idx > 0
    seg = doctor[idx: idx + 2600]
    assert "$rootPids" in seg and "$roots" in seg, "没有排掉子进程的逻辑"
    # 判据必须是「父进程不在这批 pid 里」
    assert "$rootPids -notcontains $_.ParentProcessId" in seg, seg[:500]


def test_shell_wrapper_shape_reads_green(doctor):
    """形状 ②（父子 + 只有一条 run-agent.ps1）必须判绿，并说清为什么看到 2 个。"""
    idx = doctor.find("[3] 实例唯一性")
    seg = doctor[idx: idx + 2600]
    assert "$roots.Count -le 1" in seg and "$loopCount -le 1" in seg, "绿的条件不完整"
    assert "转发壳" in seg, "没告诉用户为什么会看到 2 个 python"


def test_real_double_bridge_still_red(doctor):
    """阴性对照：真有两份（两层 run-agent.ps1）照样要红，别把闸门关死了。"""
    idx = doctor.find("[3] 实例唯一性")
    seg = doctor[idx: idx + 2600]
    assert 'Bad "有 $($roots.Count) 个进程在跑"' in seg, "真两份的那支没了"
    assert "停掉多余的" in seg


def test_count_reported_is_roots_not_raw(doctor):
    """报出来的数字要是去重后的，不能还是原始的 $running.Count。"""
    idx = doctor.find("[3] 实例唯一性")
    seg = doctor[idx: idx + 2600]
    assert 'Bad "有 $($running.Count) 个进程在跑"' not in seg, "还在报原始计数"


def test_mac_doctor_untouched():
    """Mac 上 launchd 直起、没有转发壳（本机实测 1 个、ppid=1）⇒ 那份不动。"""
    mac = open(_DOCTOR_MAC, encoding="utf-8").read()
    assert 'bad "有 ${RUNNING} 个进程在跑"' in mac, "Mac 那份被误改了"


# ---------- ② claude：要问后台那份 PATH ----------

def test_checks_the_background_path(doctor):
    """必须从 run-agent.ps1 里读出后台那份 PATH 再判。"""
    idx = doctor.find("[7] Claude Code")
    seg = doctor[idx: idx + 2400]
    assert "$bgClaudeDir" in seg
    assert "run-agent" in seg, "没去 runner 里读那份 PATH"
    assert "env:PATH" in seg, "没有抽 PATH 那一行的正则"


def test_background_missing_is_the_red_that_matters(doctor):
    """后台那份没有 ⇒ 判红，并说清这正是飞书里「找不到要用的文件或程序」的原因。"""
    idx = doctor.find("[7] Claude Code")
    seg = doctor[idx: idx + 2400]
    assert "后台服务那份 PATH 里没有 claude" in seg
    assert "找不到要用的文件或程序" in seg, "没和用户看到的那句话对上"
    # 出路是重跑第 2 步命令，不是「去装一下」
    assert "chat.auto-link.com.cn/ai/" in seg


def test_window_only_miss_is_not_a_red(doctor):
    """后台那份有、当前窗口没有 ⇒ 桥是好的，只许黄字，不许说「没装」。"""
    idx = doctor.find("[7] Claude Code")
    seg = doctor[idx: idx + 3000]
    assert "这个窗口的 PATH 里没有 claude" in seg
    assert "不影响飞书里用" in seg
    # 这一支必须在「没找到 claude 命令」那支之前（elseif 在 else 前）
    assert seg.index("这个窗口的 PATH 里没有") < seg.index('Bad "没找到 claude 命令"')


def test_install_side_already_had_the_right_idea():
    """装机侧的 Test-CarherClaudeRuns 是这条修法的来源，别让它消失。"""
    ins = open(_INSTALL_WIN, encoding="utf-8").read()
    assert "function Test-CarherClaudeRuns" in ins
    assert "后台服务" in ins or "服务那份 PATH" in ins


def test_promised_followup_sets_need_report(doctor):
    """说了「这是程序自身的问题」就要置 needReport（同 test_r38_no_screenshot 的闸门）。"""
    for no, line in enumerate(doctor.splitlines(), 1):
        if "这是程序自身的问题" in line and "Hint" in line:
            assert "$script:needReport = $true" in line, f"doctor-windows.ps1:{no} 说了跟进却不报"
