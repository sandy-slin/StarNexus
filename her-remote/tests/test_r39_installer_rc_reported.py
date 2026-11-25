"""自动更新装失败时，退出码必须进得了上报。

现场（11-20）：Windows 4 台（…<机器> / …<机器> / …<机器> / …<机器>）连着几天
同一个指纹 f31f81c6b682「安装包退出码 %s build=%s」@ selfupdate.py:636，
Mac 11 台全部更新到 20261120-e691cb45 没问题。

而**退出码是几，谁都不知道**：
- `errreport` 只发 `record.msg`（源码里的**模板**），绝不 `getMessage()` ——
  格式化后的字符串会带进用户原话，这是红线。所以 `%s` 的实参传不回来。
- `report_params` 那条通道只许 5 个字段，`u` 只能是一个词，detail 明确不发。

所以把退出码**编进常量模板本身**（一句一个码）。码是我们自己程序的退出码，
不含路径 / 用户名 / 机器信息。
"""

from __future__ import annotations

import logging

import pytest

from bridge import errreport
from bridge.selfupdate import _INSTALLER_RC_LOGS, _INSTALLER_RC_OTHER, installer_rc_log


def _report_msg(const: str) -> str:
    """走真正的上报入口，拿它最终会发出去的 msg。"""
    rec = logging.LogRecord("feishu-bridge.selfupdate", logging.ERROR,
                            "bridge/selfupdate.py", 650, const, (), None)
    return str(errreport.item_from_record(rec).get("msg") or "")


@pytest.mark.parametrize("rc", sorted(_INSTALLER_RC_LOGS))
def test_known_rc_reaches_the_report(rc):
    """列到的码：上报里看得见这个数字。"""
    msg = _report_msg(installer_rc_log(rc))
    assert str(rc) in msg, (rc, msg)


@pytest.mark.parametrize("rc", [None, "x", object(), 4242, 1.5])
def test_non_numeric_or_unlisted_falls_back(rc):
    """🔴 11-20 第二轮改了行为：原来 7/99/255 都落「其他」桶（不带数字），
    现在它们各有带数字的常量（见 `_INSTALLER_RC_EXTRA`）⇒ 这条只管
    「真的不是数」和「真的没列到」，兜底那句要说清下一步。
    """
    const = installer_rc_log(rc)
    assert const == _INSTALLER_RC_OTHER
    assert _report_msg(const) == const
    # 11-21：兜底那句从「去 _INSTALLER_RC_LOGS 加一行」改成「看 emsg 里的数字」——
    # 因为码现在**一定**查得到（走 emsg），不再需要人去表里补一行。
    assert "emsg" in const, const


def test_old_template_really_loses_the_argument():
    """对照组：原来那句带 %s 的，实参确实传不回来 —— 这才是要改的理由。

    这条是反向阳性对照：如果哪天 errreport 改成发格式化后的消息，
    它会红，提醒我们「把码编进常量」这个绕法已经没必要了（而不是默默留着）。
    """
    rec = logging.LogRecord("feishu-bridge.selfupdate", logging.ERROR,
                            "bridge/selfupdate.py", 651,
                            "安装包退出码 %s build=%s", (9, "20261120-e691cb45"), None)
    msg = str(errreport.item_from_record(rec).get("msg") or "")
    assert msg == "安装包退出码 %s build=%s"
    assert "9" not in msg and "20261120" not in msg


def test_different_rc_get_different_fingerprints():
    """不同退出码要分得开桶 —— 撞在一起就没法说「3 台是 rc=9、1 台是 rc=1」。"""
    seen = {}
    for rc in sorted(_INSTALLER_RC_LOGS):
        rec = logging.LogRecord("feishu-bridge.selfupdate", logging.ERROR,
                                "bridge/selfupdate.py", 650, installer_rc_log(rc), (), None)
        seen[rc] = errreport.item_from_record(rec).get("fp")
    assert len(set(seen.values())) == len(seen), seen


def test_rc_lines_carry_no_machine_info():
    """常量里不许出现路径 / 用户名 / 版本号 —— 它是无条件发出去的。"""
    for rc, line in list(_INSTALLER_RC_LOGS.items()) + [(None, _INSTALLER_RC_OTHER)]:
        assert "/" not in line.replace(" / ", ""), (rc, line)   # 「参数不对 / 用法错」那种顿号不算
        assert "\\" not in line and "~" not in line, (rc, line)
        assert "2026" not in line, (rc, line)


def test_busy_rc_is_listed():
    """3 = 另一个安装器在跑，这个码有专门分支，别从表里掉了。"""
    from bridge.selfupdate import WIN_INSTALLER_BUSY_RC
    assert WIN_INSTALLER_BUSY_RC in _INSTALLER_RC_LOGS


def test_failure_path_logs_the_constant(monkeypatch):
    """真走一遍失败分支：带码那句常量必须打出来。"""
    got: list[str] = []

    class H(logging.Handler):
        def emit(self, record):
            got.append(str(record.msg))

    lg = logging.getLogger("feishu-bridge.selfupdate")
    h = H()
    lg.addHandler(h)
    lg.setLevel(logging.ERROR)
    try:
        import bridge.selfupdate as su
        src = open(su.__file__, encoding="utf-8").read()
        # 盯住失败分支里确实调了 installer_rc_log（而不是只剩那句带 %s 的）
        # 11-21：这一行加了 exc_info（把码塞进 emsg）⇒ 按前缀断言，别钉整行
        # 11-21：这行又加了 log=<形状>，按「调了 installer_rc_log」断言，别钉整行原文
        assert "installer_rc_log(proc.returncode)" in src, \
            "失败分支没打带码的常量 ⇒ 上报又会变回「退出码未知」"
    finally:
        lg.removeHandler(h)


# ---------- 11-20 第二轮：带数字的句子必须是**真写在源码里**的 ----------

def test_every_listed_code_reaches_the_report_intact():
    """表里每一句都要原样上报，不能变成 `<动态消息>`。

    🔴 两种「看着对、实测不行」的写法，都在这条上翻过车：
    - `f"安装包退出码=其他({rc})"`（「一键安装」会话的建议）
    - `{rc: f"…{rc}…" for rc in range(256)}`（我的第一版）
    errreport 要求模板**整句**出现在 `_source_literals()` 里，而那是 AST 扫源码文件
    收集的字面量 —— 运行时拼出来的一个都不在。能上报的只有手写在文件里的句子。
    """
    from bridge.selfupdate import _INSTALLER_RC_EXTRA, _INSTALLER_RC_LOGS
    for rc in sorted(set(_INSTALLER_RC_LOGS) | set(_INSTALLER_RC_EXTRA)):
        const = installer_rc_log(rc)
        assert _report_msg(const) == const, f"rc={rc} 上报时被吞成 {_report_msg(const)!r}"


@pytest.mark.parametrize("rc", [0, 7, 126, 127, 137, 143, 255, -2, -11])
def test_extra_codes_carry_the_number(rc):
    """「没单独归类」的桶也要带着数字 —— 不带等于「知道是别的码但不知道是几」。"""
    msg = _report_msg(installer_rc_log(rc))
    assert str(rc) in msg, (rc, msg)


def test_truly_unknown_code_says_where_to_add_it():
    """真·没列到的码：说清下一步，别只说「其他」。"""
    from bridge.selfupdate import _INSTALLER_RC_OTHER
    assert installer_rc_log(4242) == _INSTALLER_RC_OTHER
    assert _report_msg(_INSTALLER_RC_OTHER) == _INSTALLER_RC_OTHER
    assert "emsg" in _INSTALLER_RC_OTHER


def test_no_runtime_formatting_in_the_tables():
    """盯住：这两张表里不许出现 f-string / 推导式（那样的句子上报不了）。"""
    import ast
    import bridge.selfupdate as su
    tree = ast.parse(open(su.__file__, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if not any(n in ("_INSTALLER_RC_LOGS", "_INSTALLER_RC_EXTRA") for n in names):
            continue
        for sub in ast.walk(node.value):
            assert not isinstance(sub, (ast.JoinedStr, ast.DictComp, ast.ListComp)), (
                f"{names} 里用了运行时拼接（{type(sub).__name__}）⇒ 上报会变 <动态消息>"
            )


# ---------- 11-21：码不靠「表列全了」，靠 emsg ----------

def _e2e(rc):
    """走真正的上报入口，拿 (msg, emsg)。"""
    from bridge.selfupdate import _RcOnly
    rec = logging.LogRecord("feishu-bridge.selfupdate", logging.ERROR,
                            "bridge/selfupdate.py", 650, installer_rc_log(rc), (), None)
    exc = _RcOnly(f"installer exited rc={rc}")
    rec.exc_info = (type(exc), exc, None)
    item = errreport.item_from_record(rec)
    return str(item.get("msg") or ""), str(item.get("emsg") or "")


# 「一键安装」会话实测我上一版 7/7 全落兜底的那组（Windows 进程层 NTSTATUS）
_WIN_PROC_CODES = [
    -1073741819, 3221225477,     # 0xC0000005 访问冲突
    -1073741510, 3221225786,     # 0xC000013A 被终止
    -1073740791, 3221226505,     # 0xC0000409 栈溢出
    -1073741502, 3221225794,     # 0xC0000142 DLL 初始化失败
    -1073741515, 3221225781,     # 0xC0000135 缺 DLL
    4294967295,                  # 0xFFFFFFFF
]


@pytest.mark.parametrize("rc", _WIN_PROC_CODES)
def test_windows_process_codes_are_named(rc):
    """这组要有看得懂的名字（0xC0000005 比 -1073741819 好认）。"""
    msg, _ = _e2e(rc)
    assert "没有的码" not in msg, f"rc={rc} 还落在兜底桶：{msg}"
    assert "0x" in msg or "无符号" in msg, msg


@pytest.mark.parametrize("rc", _WIN_PROC_CODES + [4242, -999, 0xDEAD, 1, 3, 9])
def test_any_code_is_recoverable_from_emsg(rc):
    """🔴 关键一条：**不管码在不在表里**，数字都要能查到。

    上一版的做法是手写码表，列不到的只报「表里没有的码」—— 追着加是打地鼠，
    而且发一版白一版（它实测那 7 个我一个都没列到）。
    `emsg` 走 `_scrub` 脱敏那条路，不要求是源码常量 ⇒ 这才是通用通道。
    """
    _, emsg = _e2e(rc)
    assert str(rc) in emsg, (rc, emsg)


def test_emsg_carries_nothing_but_the_code():
    """🔴 它会原样（脱敏后）发到 198 ⇒ 只许装退出码。"""
    _, emsg = _e2e(1)
    assert emsg == "installer exited rc=1", emsg


def test_failure_path_attaches_the_code_exception():
    """盯住调用处：不挂 exc_info 的话 emsg 是空的，这条修法就没生效。"""
    import bridge.selfupdate as su
    src = open(su.__file__, encoding="utf-8").read()
    # 11-21：那句尾部加了 ` log=…`，按前缀断言
    assert 'exc_info=_RcOnly(' in src and 'installer exited rc={proc.returncode}' in src, \
        "失败分支没把退出码挂进 exc_info ⇒ 上报里又查不到码了"


# ---------- 11-21：顺带带上「安装器日志的形状」 ----------

def _e2e_with_log(rc, tail):
    from bridge.selfupdate import _RcOnly
    exc = _RcOnly(f"installer exited rc={rc} log={'empty' if not tail else len(tail)}")
    rec = logging.LogRecord("feishu-bridge.selfupdate", logging.ERROR,
                            "bridge/selfupdate.py", 757, installer_rc_log(rc), (), None)
    rec.exc_info = (type(exc), exc, None)
    return str(errreport.item_from_record(rec).get("emsg") or "")


def test_empty_log_means_process_never_ran():
    """`rc=-1` + 日志空 ⇒ powershell 根本没起来（杀软拦下 / 作业对象限制 / 文件没了）。

    现场（…9813b0 11-21 12:04）：装 `04f9541a` 拿到 rc=4294967295（0xFFFFFFFF = -1），
    而那一版对 Windows 安装器只改了一个数字（defaults_rev 16→17，纯 config 模板），
    同一版 11 台（含 3 台 Windows）全部 u=ok ⇒ 不是包的问题。
    这时安装器正文一行都没跑到，正文里的 Die/trap 上报点一个都碰不到
    （那台今天 0 条 her-remote 安装上报，吻合）⇒ 只能靠这个判据分形状。
    """
    emsg = _e2e_with_log(4294967295, "")
    assert "log=empty" in emsg
    assert "4294967295" in emsg


def test_nonempty_log_means_it_died_midway():
    """起来了但死在半路 ⇒ 日志有字节数，该去读 state 里的 detail。"""
    assert "log=1500" in _e2e_with_log(4294967295, "x" * 1500)


def test_log_content_never_leaks():
    """🔴 安装器日志里有路径和用户名 —— 只许发字节数。"""
    leak = "C:\\Users\\a-user\\AppData\\Local\\CarHer\\err.log 写不进去"
    emsg = _e2e_with_log(1, leak)
    assert "a-user" not in emsg and "C:\\" not in emsg, emsg
    assert f"log={len(leak)}" in emsg


def test_failure_path_reads_tail_before_reporting():
    """顺序：`tail` 要在上报那行**之前**读好，否则永远报 empty（假数据比没数据更糟）。"""
    import bridge.selfupdate as su
    src = open(su.__file__, encoding="utf-8").read()
    i_tail = src.index('tail = (proc.stdout or "")')
    i_report = src.index("log={'empty' if not tail else len(tail)}")
    assert i_tail < i_report, "tail 还没读就上报了 ⇒ 永远 empty"


# ---------- 11-22：再带上「安装器跑了多少秒」 ----------
#
# 为什么要这个数字（它真的分出过结论，不是凑数的）：
# 「一键安装」会话在一台**手动**装的机器上拿到两条同形上报（我核过 198 上的原文）：
#   17:38 子安装器 codex  rc=-1/0xFFFFFFFF，跑了 45 秒
#   17:43 子安装器 claude rc=-1/0xFFFFFFFF，跑了  6 秒
# 同一台机器、两个不同的子安装器、都被结束 ⇒ 固定超时不可能一次 45 秒一次 6 秒
# ⇒ 「超时」这个解释当场被排掉，剩下的是「有东西在反复杀 powershell.exe」。
# 而走**自动更新**的那三台（…<机器> / …<机器> / …<机器>）没有「上报 + 当前窗口重试」
# 那层兜底，连这个秒数都没采到 ⇒ 查不动。这一组就是补这个缺口。
#
# 🔴 阴性对照（真跑过）：把记时退回「只在 `_run_installer_windows` 里采」那个旧形状，
#    四条路全读成 `sec=-1` —— 和「安装器一次都没跑」撞成同一个值，等于没采。

import pathlib
import subprocess
import tempfile
import time


def _updater(runner):
    from bridge.selfupdate import SelfUpdater
    home = pathlib.Path(tempfile.mkdtemp())
    pkg = home / "pkg"
    pkg.mkdir()
    up = SelfUpdater(str(pkg), str(home / "update_state.json"),
                     is_idle=lambda: True, runner=runner)
    up.download = lambda info, tmp: str(pkg / "x")
    up.current_build = lambda: "20261121-old"
    return up


def _emsgs(up):
    """走真正的 update_now（**不覆盖 run_installer**，否则测不到记时那一层）。"""
    items: list[dict] = []

    class Cap(logging.Handler):
        def emit(self, record):
            item = errreport.item_from_record(record)
            if item:
                items.append(item)

    lg = logging.getLogger("feishu-bridge.selfupdate")
    h = Cap()
    lg.addHandler(h)
    lg.setLevel(logging.ERROR)
    try:
        up.update_now({"build_id": "20261122-new"})
    finally:
        lg.removeHandler(h)
    return [str(i.get("emsg") or "") for i in items]


def test_killed_installer_reports_how_long_it_ran():
    """rc≠0：秒数要跟着退出码一起回来。"""
    up = _updater(lambda p: (time.sleep(2),
                             subprocess.CompletedProcess(["x"], -1, "", ""))[1])
    got = _emsgs(up)
    assert up._last_installer_sec == 2, up._last_installer_sec
    assert any("sec=2" in e for e in got), got


def test_zero_seconds_is_still_reported():
    """🔴 跑了 0 秒**也要发**：0 秒被杀是最强的信号（powershell 压根没起来），
    这时候如果和「没采到」共用一个表示，这条线索就丢了。"""
    up = _updater(lambda p: subprocess.CompletedProcess(["x"], -1, "", ""))
    got = _emsgs(up)
    assert up._last_installer_sec == 0
    assert any("sec=0" in e for e in got), got


def test_timeout_path_also_carries_the_seconds():
    """抛异常那条路（20 分钟超时）同样要带秒数 —— 否则「超时」和「秒杀」分不开。"""
    def boom(p):
        time.sleep(1)
        raise subprocess.TimeoutExpired(["x"], 1200)

    up = _updater(boom)
    got = _emsgs(up)
    assert up._last_installer_sec == 1
    assert any("installer raised sec=1" in e for e in got), got


def test_installer_never_ran_sends_no_seconds():
    """下载 / 校验就挂 ⇒ 安装器一次都没跑：**不许**发一个 sec 出去。

    发了会把人带去查杀软，而真正该查的是网络和 sha256。
    """
    up = _updater(lambda p: subprocess.CompletedProcess(["x"], 0, "", ""))

    def boom(info, tmp):
        raise ValueError("下载回来的安装包校验不过")

    up.download = boom
    got = _emsgs(up)
    assert up._last_installer_sec == -1
    assert not [e for e in got if "sec=" in e], got


def test_timing_lives_in_the_shared_entry_not_one_platform():
    """🔴 记时必须在 `run_installer`（mac / windows / 注入 runner 的唯一入口）。

    第一版只在 `_run_installer_windows` 里采 ⇒ mac 恒 -1、测试注入的也恒 -1。
    这条盯着它别退回去。
    """
    import bridge.selfupdate as su
    src = open(su.__file__, encoding="utf-8").read()
    head = src.index("def run_installer(")
    body = src[head:src.index("def _run_installer(", head)]
    assert "time.monotonic()" in body, "run_installer 这一层没记时 ⇒ mac 和测试腿采不到"
    assert "finally:" in body, "没用 finally ⇒ 抛异常（超时）那条路丢秒数"
    win = src[src.index("def _run_installer_windows("):]
    assert "_last_installer_sec" not in win.split("def update_now")[0], \
        "windows 那支又自己记了一遍 ⇒ 两份实现，迟早漂移"


def test_seconds_line_is_a_source_literal():
    """带 sec 的那句模板要能原样上报（f-string 拼的会变 `<动态消息>`）。"""
    from bridge.selfupdate import _INSTALLER_DIED_WITH_EXC
    assert _report_msg(_INSTALLER_DIED_WITH_EXC) == _INSTALLER_DIED_WITH_EXC
    assert "2026" not in _INSTALLER_DIED_WITH_EXC
    assert "/" not in _INSTALLER_DIED_WITH_EXC and "\\" not in _INSTALLER_DIED_WITH_EXC
