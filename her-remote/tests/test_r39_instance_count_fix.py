"""体检别再报「有 1 个进程在跑」这种自相矛盾的话。

🔴 这是我 11-20 改出来的 bug，a-user 11-21 原话上报了那一屏：
```
[3] 实例唯一性
  X   有 1 个进程在跑
      -> 停掉多余的：Stop-ScheduledTask …
    - 各自跑了：67 秒、67 秒
    - 同一个父进程：不是；其中一个是另一个的子进程：是
    - 重启循环在跑 2 层
```
判红、数字是 1、还让他去「停掉多余的」——而多余的是**自启**，不是进程。

成因：11-20 修「2 个进程」误报时，我把 `$loopCount -le 1` 塞进了判绿的条件
⇒ 他的现场（2 个 python 父子 + 2 层 run-agent.ps1）落到 else，
报出 `$roots.Count`（= 1）。复现过：decide(running=2, roots=1, loops=2) ⇒ Bad「有 1 个」。

修法：**「桥有几个」和「自启有几层」是两件事，分开判。**
- 进程数只看 `$roots`（父子算一个）
- 自启层数独立成一项（原来只写在「进程数判红」的 else 里 ⇒ 进程数判绿时这条真问题没人说）
"""

from __future__ import annotations

import os
import pathlib

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DOCTOR = os.path.normpath(os.path.join(_HERE, "..", "..", "..",
                                        "feishu-agent-oneclick", "src", "doctor-windows.ps1"))


@pytest.fixture(scope="module")
def doctor() -> str:
    return pathlib.Path(_DOCTOR).read_text(encoding="utf-8")


def _section(doctor: str, head: str, until: str) -> str:
    i = doctor.index(head)
    j = doctor.index(until, i)
    return doctor[i:j]


def test_process_verdict_ignores_loop_count(doctor):
    """🔴 判绿的条件里**不许**再有自启层数 —— 那正是这次 bug 的成因。"""
    seg = _section(doctor, "[3] 实例唯一性", "[4] 飞书连通性")
    i = seg.index("if ($roots.Count -le 1)")
    cond = seg[i:seg.index("\n", i)]
    assert "loop" not in cond.lower(), f"自启层数又混进进程数的判决了：{cond}"


def test_green_branch_keyed_on_roots(doctor):
    """父子算一个 ⇒ 判据是 `$roots`，不是 `$running`。"""
    seg = _section(doctor, "[3] 实例唯一性", "[4] 飞书连通性")
    assert "if ($roots.Count -le 1)" in seg


def test_red_branch_reports_roots_not_running(doctor):
    """真有两个桥时，报的数字要是去重后的。"""
    seg = _section(doctor, "[3] 实例唯一性", "[4] 飞书连通性")
    assert 'Bad "有 $($roots.Count) 个进程在跑"' in seg
    assert 'Bad "有 $($running.Count) 个进程在跑"' not in seg


def test_autostart_layers_is_its_own_item(doctor):
    """自启层数要独立成项 —— 不能只在「进程数判红」里才说。

    🔴 按**输出**断言，不按变量名：第一版只查 `$loopLayers` 这个名字存不存在，
    改个名就绕过去了（变异验出来的）。真正要钉的是那句话在 if/else 之外。
    """
    seg = _section(doctor, "[3] 实例唯一性", "[4] 飞书连通性")
    assert "开机自启装了" in seg, "自启层数这一项不见了"
    # 🔴 判据用**缩进**，不用数花括号：PS 的字符串插值 `$(if (…) {…})` 里也有花括号，
    # 纯文本计数算不准（第一版就这么假红的：正常代码也被判成「嵌在 1 层里」）。
    # 顶层语句缩进 2 空格（`if (…) {` 的那一层），嵌在 else 里会是 4 格以上。
    line = [l for l in seg.splitlines() if 'Warn "开机自启装了' in l][0]
    indent = len(line) - len(line.lstrip())
    assert indent <= 2, f"「开机自启装了」缩进 {indent} 格 ⇒ 还嵌在块里（该在 if/else 之外）"
    # 它读的那个变量也要在顶层算
    decl = [l for l in seg.splitlines() if l.lstrip().startswith("$loopLayers = 0")]
    assert decl, "自启层数没有独立的计数变量"
    assert len(decl[0]) - len(decl[0].lstrip()) == 0, "计数变量不在顶层"


def test_autostart_warning_says_it_still_works(doctor):
    """两层自启不影响现在用（桥自己有锁）—— 别让人以为坏了。"""
    seg = _section(doctor, "[3] 实例唯一性", "[4] 飞书连通性")
    assert "不影响现在用" in seg
    assert "桥自己有锁" in seg or "只会有一个在跑" in seg


def test_autostart_fix_is_not_hunting_for_the_installer(doctor):
    """🔴 用户是小白，别叫他找 .bat（用户原话：「他们都是小白」）。"""
    i = doctor.index("$loopLayers")
    seg = doctor[i:i + 900]
    assert "双击 feishu-agent-windows.bat" not in seg, seg[:300]
    assert "chat.auto-link.com.cn/ai/" in seg


def test_promised_followup_sets_need_report(doctor):
    """说了「这是程序自身的问题」就要置 needReport（同 test_r38_no_screenshot 的闸门）。"""
    for no, line in enumerate(doctor.splitlines(), 1):
        if "这是程序自身的问题" in line and "Hint" in line:
            assert "$script:needReport = $true" in line, f"doctor-windows.ps1:{no} 说了跟进却不报"


# ---------- 判决表（复刻那两段 PS，喂真实现场） ----------

def _decide(running: int, roots: int, loops: int) -> tuple:
    proc = ("Ok", f"只有一个在跑（{running} 个 python）") if roots <= 1 else ("Bad", f"有 {roots} 个进程在跑")
    if loops > 1:
        auto = ("Warn", f"开机自启装了 {loops} 层")
    elif loops == 1:
        auto = ("Ok", "开机自启只有一条")
    else:
        auto = ("", "")
    return proc, auto


@pytest.mark.parametrize(("name", "running", "roots", "loops", "want_proc", "want_auto"), [
    ("a-user 的现场", 2, 1, 2, "Ok", "Warn"),
    ("父子 + 单层（11-20 验过的）", 2, 1, 1, "Ok", "Ok"),
    ("最常见：1 个 + 单层", 1, 1, 1, "Ok", "Ok"),
    ("真两个桥 + 两层", 2, 2, 2, "Bad", "Warn"),
])
def test_verdict_table(name, running, roots, loops, want_proc, want_auto):
    (pv, pm), (av, _am) = _decide(running, roots, loops)
    assert pv == want_proc, f"{name}: 进程数判成 {pv}«{pm}»"
    assert av == want_auto, f"{name}: 自启判成 {av}"


def test_never_says_one_process_as_a_problem():
    """🔴 他看到的那句话：判红而数字是 1。任何输入都不许再出现。"""
    for running in range(0, 5):
        for roots in range(0, running + 1):
            for loops in range(0, 4):
                (pv, pm), _ = _decide(running, roots, loops)
                if pv == "Bad":
                    assert "有 1 个进程在跑" not in pm, (running, roots, loops, pm)
                    assert "有 0 个进程在跑" not in pm, (running, roots, loops, pm)
