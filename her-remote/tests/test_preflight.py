"""装机前体检的回归测试。

这组测试盯的是**「装完了但起不来，而安装器报绿」**那一类 —— 它比「装不上」难查得多，
因为用户手上没有任何红字，只有一个在飞书里不说话的机器人。

每条测试对应一个 2026-11-05 实测出来的形状，实测命令和真实输出写在 findings 里：

1. **launchd 注册成功 ≠ 进程活着。** 造一个秒退的 job，`launchctl print` 照样返回 0
   ⇒ 安装器打「✓ 已经在后台跑起来了」。45 秒内 runs 从 1 涨到 5，last exit code 恒 1。
2. **配置写坏会在启动时才炸，且日志里两个判据都不出现。** 目录名带一个反斜杠，
   tomllib 抛 `Unescaped '\\'`；日志里既没有 `connected to` 也没有 `connect failed`
   ⇒ 安装器落到「它还在重试连接，等两分钟」。所以必须在**写配置之前**拦。
3. **服务那份 PATH 和装机 shell 的 PATH 会分叉。** 分叉时 `create_subprocess_exec`
   抛 `FileNotFoundError: 'claude'`，而装机全程是绿的。

测试用**注入事实**的方式跑纯函数，不摸真机器 —— 这样 Windows、公司代理、
OneDrive 重定向过的家目录这些**本机造不出来的形状**也能被覆盖。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
)

import preflight  # noqa: E402
from preflight import (  # noqa: E402
    BLOCK,
    OK,
    WARN,
    Finding,
    Report,
    check_claude,
    check_credentials,
    check_disk,
    check_feishu_client,
    check_network,
    check_python,
    check_quarantine,
    check_workspace_path,
    render,
)


# ─────────────────── 1. 报告本身的纪律 ───────────────────


def test_blocking_finding_must_carry_a_fix():
    """🔴 「错了」不算结论 —— 每个挡路项必须给「那我该干什么」。

    和 test_install_ux 的「每个 die 都要给下一步」同一条纪律，只是这里能在
    构造那一刻就拦住，不用等到有人读文案。
    """
    with pytest.raises(ValueError, match="必须给修法"):
        Finding(key="x", severity=BLOCK, title="装不上")
    with pytest.raises(ValueError, match="必须给修法"):
        Finding(key="x", severity=WARN, title="有点问题")
    # 阳性对照：OK 不需要修法，不能把它也拦掉
    Finding(key="x", severity=OK, title="一切正常")


def test_duplicate_checks_are_rejected():
    """同一个问题在一屏里说两遍，用户会以为是两个问题。"""
    report = Report()
    report.add(Finding(key="python", severity=OK, title="a"))
    report.add(Finding(key="python", severity=OK, title="b"))
    with pytest.raises(ValueError, match="重复的检查项"):
        report.invariants()


def test_blockers_sort_before_warnings_and_ok():
    """挡路的必须排最前 —— 用户不该自己在一屏里找红字。"""
    report = Report()
    report.add(Finding(key="a", severity=OK, title="好的"))
    report.add(Finding(key="b", severity=WARN, title="提醒", fix="看一下"))
    report.add(Finding(key="c", severity=BLOCK, title="挡路", fix="先修这个"))
    assert [f.key for f in report.sorted_findings()] == ["c", "b", "a"]


def test_same_severity_keeps_check_order():
    """同档位内保持加入顺序 —— 检查顺序是按「同事最可能卡在哪」排的，不能被打乱。"""
    report = Report()
    for key in ("claude", "python", "disk"):
        report.add(Finding(key=key, severity=BLOCK, title=key, fix="修"))
    assert [f.key for f in report.sorted_findings()] == ["claude", "python", "disk"]


def test_can_install_is_false_only_for_blockers():
    """WARN 不该挡住安装 —— 挡了就会把能正常用的人拦在门外。"""
    report = Report()
    report.add(Finding(key="a", severity=WARN, title="旧客户端", fix="升一下"))
    assert report.can_install() is True
    report.add(Finding(key="b", severity=BLOCK, title="没 python", fix="装一个"))
    assert report.can_install() is False


# ─────────────────── 2. claude：服务 PATH 分叉 ───────────────────


def test_missing_claude_blocks_and_says_reopen_the_window():
    f = check_claude(None)
    assert f.severity == BLOCK
    assert "关掉这个窗口" in f.fix, "没提「要重开窗口」—— 这是最常见的「装过了却还报没装」"


def test_claude_outside_the_service_path_is_a_blocker():
    """🔴 本轮最关键的一条。

    实测：mac 侧 plist 把 PATH 写死成 `dirname $CLAUDE_BIN` 加四个固定目录。
    claude 装在 ~/.carher-npm-global/bin（claude-oneclick 的默认位置）而服务那份
    PATH 没有它时，装机全程绿、飞书里能聊，一让它干活就抛
    `FileNotFoundError: [Errno 2] No such file or directory: 'claude'`
    —— 用户看到的是「我这边出错了，没能完成」，完全不知道为什么。
    """
    f = check_claude(
        "/Users/x/.carher-npm-global/bin/claude",
        ["/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"],
    )
    assert f.severity == BLOCK
    assert "后台服务" in f.title
    # 必须说出后果，否则用户不知道这条为什么要管
    assert "干活" in f.detail or "答不出" in f.detail


def test_claude_inside_the_service_path_passes():
    """阴性对照：PATH 对得上时不能报警，否则这一项就是噪音。"""
    f = check_claude("/opt/homebrew/bin/claude", ["/opt/homebrew/bin", "/usr/bin"])
    assert f.severity == OK


def test_claude_check_skips_path_comparison_when_not_given():
    """没传服务 PATH 时只判「在不在」—— 不许凭空假设一个 PATH 然后报红。"""
    assert check_claude("/opt/homebrew/bin/claude").severity == OK


# ─────────────────── 3. Python / 磁盘 ───────────────────


@pytest.mark.parametrize(
    "version,expected",
    [
        (None, BLOCK),
        ((3, 9), BLOCK),  # macOS 自带的那个，实测 3.9.6
        ((3, 10), BLOCK),  # tomllib 是 3.11 才有的
        ((3, 11), OK),
        ((3, 14), OK),  # 本机 brew 的版本
    ],
)
def test_python_version_gate(version, expected):
    assert check_python(version).severity == expected


def test_python_without_venv_module_blocks():
    """3.11+ 但缺 venv 是真实形状，而它要到「正在准备独立的 Python 环境」才炸。"""
    f = check_python((3, 12), has_venv=False)
    assert f.severity == BLOCK
    assert "venv" in f.title


def test_disk_threshold_matches_measured_footprint():
    """阈值不是猜的：实测 ~/.carher-feishu-agent 占 146M、全新 venv 142M。

    这条盯的是「阈值被人随手改小到装不下」。
    """
    assert check_disk(50 * 1024 * 1024).severity == BLOCK
    assert check_disk(10 * 1024 * 1024 * 1024).severity == OK
    # 读不到空间时不许拦 —— 宁可放过，不要凭猜测挡住安装
    assert check_disk(None).severity == OK


# ─────────────────── 4. 网络：两个出口分开判 ───────────────────


def test_feishu_unreachable_blocks_but_slow_pip_only_warns():
    """实测本机：pypi.org:443 不通、清华源通、飞书接口 200。

    下载源坏了只是慢（pip 会重试，实测官方源那条路总共 5 分 29 秒才装完）；
    飞书连不上则是**根本收不到消息**，那才是挡路的。两者不能同档。
    """
    assert check_network(pip_index_ok=True, feishu_api_ok=False).severity == BLOCK
    assert check_network(pip_index_ok=False, feishu_api_ok=True).severity == WARN
    assert check_network(pip_index_ok=True, feishu_api_ok=True).severity == OK


def test_feishu_blocker_mentions_the_long_connection():
    """修法要说清「长连接必须直连」，否则用户会挂着代理反复重装。"""
    f = check_network(pip_index_ok=True, feishu_api_ok=False)
    assert "代理" in f.fix or "代理" in f.detail


# ─────────────────── 5. 凭据：错误码翻成人话 ───────────────────


@pytest.mark.parametrize("code", [10003, 10013, 10014])
def test_bad_credential_codes_say_dont_swap_the_two_strings(code):
    """这三个码的真实原因几乎总是「两串贴反了」—— 不说出来用户会反复重粘。"""
    f = check_credentials(code)
    assert f.severity == BLOCK
    assert "弄反" in f.fix or "反" in f.fix


def test_unpublished_app_says_it_is_not_your_fault():
    """10012 = 要管理员审批。不说「这不是你装错了」，用户会一直怀疑自己。"""
    f = check_credentials(10012)
    assert f.severity == BLOCK
    assert "管理员" in f.fix
    assert "不是你装错" in f.detail


def test_credentials_absent_or_valid_do_not_block():
    assert check_credentials(None).severity == OK  # 第一次装
    assert check_credentials(0).severity == OK


# ─────────────────── 6. 工作目录：写配置之前拦 ───────────────────


@pytest.mark.parametrize("bad", ["/Users/x/back\\slash", '/Users/x/we"ird'])
def test_workspace_with_toml_breaking_chars_blocks(bad):
    """🔴 实测：这两种字符会让生成的 config.toml 读不出来，而服务是**启动时**才崩。

    那一刻安装器已经打完绿了（launchd 的 job 注册成功），用户只会看到
    「它还在重试连接，等两分钟」。所以这一项的价值全在「拦得够早」。
    """
    f = check_workspace_path(bad)
    assert f.severity == BLOCK
    assert "起来就崩" in f.detail or "读不出来" in f.detail


@pytest.mark.parametrize(
    "good", ["/Users/x/codes", "/Users/x/my code", "/Users/x/我的 项目"]
)
def test_workspace_with_spaces_and_cjk_is_fine(good):
    """阴性对照：空格和中文实测能被 tomllib 正常解析，报警会让人不敢用中文目录。"""
    assert check_workspace_path(good).severity == OK


def test_toml_breakage_is_real_not_a_style_rule(tmp_path):
    """阳性对照：证明上面那条判据不是洁癖 —— 真写一份配置，真让 tomllib 读。

    不验这个的话，check_workspace_path 可能只是在拦一个其实无害的字符。
    """
    import tomllib

    for bad_path, expect_fail in (
        ("/tmp/back\\slash", True),
        ('/tmp/we"ird', True),
        ("/tmp/my code", False),
        ("/tmp/我的 项目", False),
    ):
        config = tmp_path / "c.toml"
        # 和两个安装器生成配置时完全一样的写法：直接把路径塞进双引号
        config.write_text(f'[agent]\nworkspaces = ["{bad_path}"]\n', encoding="utf-8")
        try:
            with open(config, "rb") as handle:
                tomllib.load(handle)
            failed = False
        except tomllib.TOMLDecodeError:
            failed = True
        assert failed == expect_fail, (
            f"{bad_path!r} 期望 tomllib {'失败' if expect_fail else '成功'}，实际相反"
        )
        # 判据和真实行为必须对得上
        assert (check_workspace_path(bad_path).severity == BLOCK) == expect_fail


# ─────────────────── 7. 飞书客户端 / 隔离标记：只提醒不挡路 ───────────────────


def test_old_feishu_client_only_warns():
    """旧客户端只是看不到常驻菜单，聊天照样能用 ⇒ 报 BLOCK 会把能用的人挡在门外。"""
    f = check_feishu_client("7.18")
    assert f.severity == WARN
    assert "一样有按钮" in f.fix, "没给「不升也能用」的出口"
    assert check_feishu_client("7.22").severity == OK
    assert check_feishu_client("8.1").severity == OK
    assert check_feishu_client(None).severity == OK  # 读不到就别制造焦虑


def test_quarantine_warning_gives_the_terminal_fallback():
    """实测：产物没签名，spctl 判 rejected；但 bash <文件> 一路畅通。

    所以修法必须是那条终端命令 —— 叫用户「右键打开」在新版 macOS 上不一定有用。
    """
    f = check_quarantine(quarantined=True, platform="darwin")
    assert f.severity == WARN
    assert "bash" in f.fix
    assert check_quarantine(quarantined=False).severity == OK
    # Windows 没有这个机制，不许在那边也报
    assert check_quarantine(quarantined=True, platform="win32").severity == OK


# ─────────────────── 8. 渲染：一屏说清 ───────────────────


def test_render_lists_every_blocker_with_its_fix():
    """一屏列全，而不是报第一个就停 —— 逐个报的话用户要跑好几轮。"""
    report = Report()
    report.add(check_claude(None))
    report.add(check_python((3, 9)))
    report.add(check_credentials(10012))
    screen = render(report)
    assert "有 3 项必须先解决" in screen
    for finding in report.blockers:
        assert finding.title in screen
        assert finding.fix.splitlines()[0] in screen, f"{finding.key} 的修法没进屏幕"


def test_render_says_go_ahead_when_clean():
    report = Report()
    report.add(check_python((3, 12)))
    report.add(check_disk(10 * 1024 * 1024 * 1024))
    assert "都没问题" in render(report)


def test_render_distinguishes_warn_only_from_blocked():
    """只有提醒时必须明说「可以装」，否则用户看到黄字也会停手。"""
    report = Report()
    report.add(check_feishu_client("7.18"))
    screen = render(report)
    assert "可以装" in screen
    assert "必须先解决" not in screen


def test_render_has_no_technical_noise():
    """🔴 和 test_install_ux 第 3 条同一纪律：命令噪音会把真正要做的事挤出视线。"""
    report = Report()
    report.add(check_claude(None))
    report.add(check_credentials(10012))
    screen = render(report)
    for noise in ("launchctl", "Register-ScheduledTask", "Stop-ScheduledTask", "plist"):
        assert noise not in screen, f"体检屏幕里混进了技术命令：{noise}"


# ─────────────────── 9. 采集：只读 ───────────────────


def test_gather_local_is_read_only_and_runs_here():
    """在这台机器上真跑一遍：不许报错，也不许碰 ~/.carher-feishu-agent。"""
    before = os.path.exists(os.path.expanduser("~/.carher-feishu-agent/config.toml"))
    report = preflight.gather_local()
    assert report.findings, "一项都没采到"
    report.invariants()
    render(report)  # 渲染不能抛
    after = os.path.exists(os.path.expanduser("~/.carher-feishu-agent/config.toml"))
    assert before == after, "体检动了用户的配置 —— 它必须只读"


def test_gather_local_detects_this_machines_python():
    """阳性对照：确认采集真的读到了东西，而不是返回一堆空壳。"""
    report = preflight.gather_local()
    python = [f for f in report.findings if f.key == "python"][0]
    assert python.severity == OK, "本机 Python 够新却被判不合格"
    assert f"{sys.version_info[0]}.{sys.version_info[1]}" in python.title


def test_gather_local_never_writes_config_files(tmp_path, monkeypatch):
    """阴性对照：把 HOME 指到空目录，跑完之后那个目录必须还是空的。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    preflight.gather_local()
    assert list(tmp_path.iterdir()) == [], f"体检在家目录里写了东西：{list(tmp_path.iterdir())}"
