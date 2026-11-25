"""doctor 必须报出「自动更新在反复失败」，别再说「它自己会换成新版」。

现场（11-20）：Windows 4 台（…<机器> / …<机器> / …<机器> / …<机器>）连着几天
自动更新装不上，而**三条路都看不到原因**：
  ① errreport 只发源码常量模板 ⇒「安装包退出码 %s」的实参传不回来
  ② 门户 install-reports 收不到 her remote 的自动更新失败（实测这 4 台今天一条都没有）
  ③ doctor 根本不看这一项，还在跟用户说「它自己会在十几分钟内换成新版」——
     对这几台是假的，每 10 分钟失败一轮，永远换不过去。

这台开发机没装 pwsh（PowerShell 测试会 skip），所以这里测两样：
  - 文本契约：doctor 脚本里该有的判断和文案在不在
  - 判据一致性：PS 认的 outcome 词表 == bridge/selfupdate.py 的 `_OUTCOME_WORDS` 子集
Python 复刻那段判据的实测（含真名抹除）见 commit message。
"""

from __future__ import annotations

import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
_DOCTOR_WIN = os.path.join(_ONECLICK, "src", "doctor-windows.ps1")


@pytest.fixture(scope="module")
def doctor() -> str:
    return open(_DOCTOR_WIN, encoding="utf-8").read()


def test_reads_the_update_state_file(doctor):
    """原因只留在 update_state.json 里 —— doctor 必须去读它。"""
    assert "update_state.json" in doctor
    assert "Get-CarherUpdateFailure" in doctor


def test_stale_version_branch_checks_for_failure_first(doctor):
    """「你这版旧了」那支必须先问「是不是一直在装失败」，再决定说什么。

    🔴 这条盯的是 11-20 那个具体错误：无条件安慰「它自己会换成新版」。
    """
    # 取「你这版旧了」之后那一段
    idx = doctor.find("你这版旧了")
    assert idx > 0, "文案改了？这条测试要跟着改"
    tail = doctor[idx: idx + 1200]
    assert "Get-CarherUpdateFailure" in tail, "旧版分支里没查失败"
    reassure = "它自己会在十几分钟内换成新版"
    assert reassure in tail, "安慰那句不见了（它在「没失败」时仍然是对的）"
    # 安慰必须在 else 支里，不能无条件
    assert tail.index("Get-CarherUpdateFailure") < tail.index(reassure), \
        "先说安慰再查失败 = 没查"


def _stale_branch(doctor: str) -> str:
    """「你这版旧了」那一段。

    🔴 不能用 `doctor.find("Get-CarherUpdateFailure")` —— 第一处命中是诊断包那里，
    不是失败分支（本轮第一版就这么假红的）。
    """
    idx = doctor.find("你这版旧了")
    assert idx > 0, "文案改了？这些测试要跟着改"
    # 🔴 到下一节（`# (b) 我在和哪个机器人说话。`）为止 —— 固定取 N 个字会扫进别节，
    # 把别节里正当的「重新双击 …bat」当成本节的问题（本轮第一版就这么假红的）。
    end = doctor.find("# (b) ", idx)
    assert end > idx, "找不到下一节的边界"
    return doctor[idx:end]


def test_failure_branch_tells_the_truth_and_gives_a_way_out(doctor):
    """失败时要判红、要给出路，而且出路不是「去找安装包」。"""
    seg = _stale_branch(doctor)
    assert "没装上" in seg
    # 用户入口是 /ai/ 页面第 2 步那条命令（/oneclick 已 302 过去）
    assert "chat.auto-link.com.cn/ai/" in seg, "没给 /ai/ 页面那条路"
    assert "发「更新」" in seg, "没给「在飞书里发更新」这条最省事的路"


def test_never_tells_the_user_to_hunt_for_a_file(doctor):
    """🔴 用户是不写代码的同事，不知道安装包在哪。

    11-20 用户原话：「用户不知道安装包在哪儿，他们都是小白」。
    失败那支里不许出现「双击 …bat」这种要他自己找文件的话。
    """
    seg = _stale_branch(doctor)
    # 允许别处（比如「没装过」）说双击，但更新失败这支不许
    assert "双击 feishu-agent-windows.bat" not in seg, seg[:400]


def test_detail_is_scrubbed_of_local_paths(doctor):
    """detail 里是安装器输出，可能带 `C:\\Users\\<真名>\\…` —— 必须抹掉再上屏。

    同事会把自检截图转出去，本机路径里有他的真名。
    """
    assert r"[A-Za-z]:\\[^\s" in doctor, "没有抹盘符路径的正则"
    assert "'<路径>'" in doctor


def test_outcome_words_match_selfupdate(doctor):
    """PS 里认的 outcome 必须是 bridge/selfupdate.py `_OUTCOME_WORDS` 的子集。

    两边各写一份词表 ⇒ 加一个新 outcome 时 doctor 会漏判（静默）。
    """
    from bridge.selfupdate import NOT_APPLIED, ROLLED_BACK, _OUTCOME_WORDS
    m = re.search(r"\$rec\.outcome -in @\(([^)]*)\)", doctor)
    assert m, "找不到 PS 里那张词表"
    ps_words = set(re.findall(r"'([a-z_]+)'", m.group(1)))
    assert ps_words, m.group(1)
    # 这三个是「没装上」的三种形状，一个都不能少
    assert {"fail", NOT_APPLIED, ROLLED_BACK} <= ps_words, ps_words
    # 🔴 doctor 读的是**磁盘上的 state**，所以它必须认 rolled_back —— 而 `_OUTCOME_WORDS`
    # （上报用的词表）**故意没有** rolled_back：那是中间状态，`_reconcile()` 下次启动会把它
    # 转成 not_applied 再上报。两张表口径不同是对的，别拿一张去套另一张
    # （11-20 我就是这么误判成「漏了同步」、擅自往 _OUTCOME_WORDS 里加，被
    #  test_r38_win_selfupdate.py::test_u_words_match_fleet_report 拦住的）。
    assert ROLLED_BACK not in _OUTCOME_WORDS, "rolled_back 不该进上报词表（见 _reconcile）"
    assert ps_words - {ROLLED_BACK} <= set(_OUTCOME_WORDS), (
        f"PS 认了既不在上报词表、也不是 rolled_back 的词：{ps_words - {ROLLED_BACK} - set(_OUTCOME_WORDS)}"
    )


def test_diag_carries_the_update_failure(doctor):
    """诊断包要带上，不然每次都得去问同事。"""
    idx = doctor.find("$diag = ")
    seg = doctor[max(0, idx - 600): idx + 800]
    assert "update_fail=" in seg, "诊断包里没有这一项"
    assert "Get-CarherUpdateFailure" in seg


def test_unreadable_state_is_not_a_red(doctor):
    """读不到 / 文件坏了 ⇒ 当「没失败」，不造假红。

    doctor 一红同事就慌，而「state 文件读不到」本身不说明更新有问题。
    """
    idx = doctor.find("function Get-CarherUpdateFailure")
    body = doctor[idx: idx + 2000]
    assert "catch { return $out }" in body, "读失败没有兜底返回"
    assert body.count("catch") >= 3, "至少三处 catch（读文件 / 时间 / detail）"
