"""体检必须**真的被安装器调用**，而且不许成为新的失败点。

`tests/test_preflight.py` 盯的是判定逻辑本身（纯函数，38 条）。
这组盯的是**接线** —— 两件事分开，因为它们各自的假绿形状完全不同：

- 逻辑对了但没人调用 ⇒ 38 条全绿，而用户那边一点体检都没有。
  这正是这个项目栽过的那次「删掉调用点测试还是绿」。
- 调用上了但体检自己炸了会挡住安装 ⇒ 我们亲手造出一个新的「装不上」，
  而且是用户完全无法自救的形状（看不懂 Python 报错，也不知道可以跳过）。

所以这里有两条主线：**接上了没**，和**它炸了会不会连累安装**。
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import sys
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE_ROOT = os.path.dirname(_HERE)
_ONECLICK = os.path.normpath(os.path.join(_BRIDGE_ROOT, "..", "..", "feishu-agent-oneclick"))
MAC_SRC = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN_SRC = os.path.join(_ONECLICK, "src", "install-windows.ps1")
PREFLIGHT = os.path.join(_BRIDGE_ROOT, "scripts", "preflight.py")

sys.path.insert(0, os.path.join(_BRIDGE_ROOT, "scripts"))

import preflight  # noqa: E402


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def here_dir():
    """夹具建在测试目录旁边，不用 tmp_path（那是 pathguard 的系统豁免前缀）。"""
    base = pathlib.Path(_HERE).parent / f".preflightwire-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------- 1. 真的接上了 ----------


def test_both_installers_embed_the_preflight_script():
    """两侧都要嵌 —— 只接一侧的话 Windows 用户少一道闸门而没人知道。"""
    for path, name in ((MAC_SRC, "install-mac.sh"), (WIN_SRC, "install-windows.ps1")):
        assert "@@EMBED scripts/preflight.py@@" in _read(path), f"{name} 没有嵌入体检脚本"


def test_both_installers_actually_run_it():
    """🔴 嵌进去 ≠ 跑过它。必须有真实的调用点。

    这条是为「删掉调用点而测试还是绿」那个形状写的。
    """
    mac = _read(MAC_SRC)
    assert re.search(r'"\$PREFLIGHT_PY_BIN" "\$PREFLIGHT_PY"', mac), (
        "install-mac.sh 嵌了体检脚本但没跑它"
    )
    win = _read(WIN_SRC)
    assert re.search(r"&\s+\$preflightPy\b.*\$preflightFile", win), (
        "install-windows.ps1 嵌了体检脚本但没跑它"
    )


def test_preflight_runs_before_anything_is_installed():
    """🔴 体检的全部价值在「拦得够早」。

    判据：调用点必须出现在**下载依赖**和**写配置**之前。
    排在后面就等于让用户先等那一分钟，再告诉他第一步就该找管理员。
    """
    mac = _read(MAC_SRC)
    call_at = mac.index('"$PREFLIGHT_PY_BIN" "$PREFLIGHT_PY"')
    assert call_at < mac.index('"$VENV/bin/pip" install --no-index --find-links "$PIP_WHEELS" "lark-oapi'), "mac 侧体检排在下载依赖之后"
    assert call_at < mac.index('cat > "$CONFIG"'), "mac 侧体检排在写配置之后"
    assert call_at < mac.index("$PY\" -m venv"), "mac 侧体检排在建虚拟环境之后"

    win = _read(WIN_SRC)
    win_call = re.search(r"&\s+\$preflightPy\b.*\$preflightFile", win).start()
    assert win_call < win.index("Invoke-WithDots $venvPy"), "Windows 侧体检排在下载依赖之后"
    # 第 38 轮：写 config.toml 改走 Write-CarherFileRetry（被占时重试 5 次）
    assert win_call < win.index("Write-CarherFileRetry $config"), (
        "Windows 侧体检排在写配置之后"
    )


def test_blocked_preflight_stops_the_install():
    """挡路项必须真的停住安装 —— 打一屏红字然后照常装下去等于没有闸门。"""
    mac = _read(MAC_SRC)
    block = mac[mac.index("PREFLIGHT_RC") : mac.index("# 体检没跑成")]
    assert 'if [ "$PREFLIGHT_RC" = 2 ]' in block, "mac 侧没有按退出码 2 停住"
    assert "die " in block, "mac 侧体检红了却不停手"

    win = _read(WIN_SRC)
    assert "$preflightRc -eq 2" in win, "Windows 侧没有按退出码 2 停住"


def test_installers_and_script_agree_on_the_blocked_exit_code():
    """退出码就是契约。两边各写一个常量 ⇒ 哪天改了一侧，闸门静默失效。"""
    assert preflight._EXIT_BLOCKED == 2
    assert 'if [ "$PREFLIGHT_RC" = 2 ]' in _read(MAC_SRC)
    assert "$preflightRc -eq 2" in _read(WIN_SRC)


def test_warnings_do_not_stop_the_install():
    """只有提醒时必须放行 —— 挡了就把能正常用的人拦在门外。"""
    report = preflight.Report()
    report.add(preflight.check_feishu_client("7.18"))  # WARN
    assert report.can_install() is True
    screen = preflight.render(report)
    assert "可以装" in screen


# ---------- 2. 体检自己炸了不许挡住安装 ----------


def test_preflight_crash_exits_zero(here_dir):
    """🔴 体检是附加闸门，不是安装的前提 —— 它崩了必须放行。

    真的把一个会抛的东西塞进去跑（不是读源码找 try），确认退出码是 0。
    """
    broken = here_dir / "broken_preflight.py"
    # 原样抄一份，然后把采集那步改成「一定抛」
    body = _read(PREFLIGHT).replace(
        "    report = Report()\n\n    claude_path",
        "    raise RuntimeError('体检自己炸了 /Users/RealName/secret')\n    report = Report()\n\n    claude_path",
        1,
    )
    assert "体检自己炸了" in body, "替换没生效，这条测试没测到东西"
    broken.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(broken), "--claude-launch", "ok"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"体检崩了却返回 {proc.returncode} ⇒ 安装器会停住，而用户无法自救\n{proc.stdout}\n{proc.stderr}"
    )
    assert "跳过它继续装" in proc.stdout, f"没告诉用户「跳过体检继续装」\n{proc.stdout}"


def test_preflight_crash_prints_only_the_error_class(here_dir):
    """崩的时候只给错误类名 —— 异常消息里可能带 `/Users/<真名>/…`。"""
    broken = here_dir / "broken2.py"
    body = _read(PREFLIGHT).replace(
        "    report = Report()\n\n    claude_path",
        "    raise RuntimeError('/Users/RealName/.carher-feishu-agent/secret-token-abc')\n"
        "    report = Report()\n\n    claude_path",
        1,
    )
    broken.write_text(body, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(broken), "--claude-launch", "ok"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "RuntimeError" in proc.stdout, f"没给出错误类名\n{proc.stdout}"
    for leak in ("RealName", "secret-token-abc"):
        assert leak not in proc.stdout, f"异常消息漏到屏幕上了：{leak}\n{proc.stdout}"


def test_unknown_argument_is_not_read_as_blocked():
    """🔴 不认识的参数绝不许返回 2。

    2 是「有挡路项」。argparse 遇到未知参数正好 `sys.exit(2)` ⇒ 将来谁给安装器
    加一个参数、忘了同步这边，安装器会把「参数拼错」读成「体检不让装」，
    然后对着一屏空白的体检结果停住。所以这里是手写解析。
    """
    proc = subprocess.run(
        [sys.executable, PREFLIGHT, "--some-future-flag", "x", "--claude-launch", "ok"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"未知参数被当成「有挡路项」了（rc={proc.returncode}）\n{proc.stdout}"
    )


def test_preflight_never_writes_anything(here_dir):
    """只读。把 HOME 指到空目录真跑一遍，跑完那个目录还得是空的。"""
    env = {**os.environ, "HOME": str(here_dir)}
    proc = subprocess.run(
        [sys.executable, PREFLIGHT, "--claude-launch", "ok"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode in (0, 2)
    assert list(here_dir.iterdir()) == [], f"体检写了东西：{list(here_dir.iterdir())}"


def test_installers_clean_up_the_temp_script():
    """体检脚本是临时落盘的 ⇒ 必须删掉，否则卸载脚本也得认识它。"""
    mac = _read(MAC_SRC)
    assert 'rm -f "$PREFLIGHT_PY"' in mac, "mac 侧没删临时体检脚本"
    assert "PREFLIGHT_PY" in mac[mac.index("cleanup()") : mac.index("trap cleanup EXIT")], (
        "mac 侧没把体检脚本挂进 trap ⇒ 中途 die 时文件留在盘上"
    )
    win = _read(WIN_SRC)
    # 11-18:TEMP 可能是 8.3 短路径,Remove-Item 在那下面抛终止错误 ⇒ 改成 Remove-CarherTemp(.NET 删、异常全吞)
    assert "Remove-CarherTemp $preflightFile" in win, (
        "Windows 侧没删临时体检脚本"
    )


# ---------- 3. claude 探活 ----------


def test_claude_launch_probe_runs_under_the_service_path():
    """🔴 判据必须是「在服务那份 PATH 下真跑一次」，不是比对字符串。

    实测：plist 把 `dirname $CLAUDE_BIN` 拼进 PATH ⇒ claude 自己永远找得到，
    那条字符串比较在现版安装器下**永远不会红**。真正会崩的是它的解释器
    （npm 装的 claude 是 `#!/usr/bin/env node`，nvm 的 node 不在那四个固定目录里），
    喂一份只有 shim 目录的 PATH 实测 rc=127、`env: node: No such file or directory`。
    """
    mac = _read(MAC_SRC)
    assert 'env -i HOME="$HOME" PATH="$SERVICE_PATH"' in mac, (
        "mac 侧没有在服务那份 PATH 下真跑 claude"
    )
    assert "127) CLAUDE_LAUNCH=\"no-interpreter\"" in mac, (
        "mac 侧没认 rc=127（解释器找不到）这个形状"
    )


def test_missing_interpreter_is_a_blocker_with_consequences_spelled_out():
    f = preflight.check_claude_launch(preflight.CLAUDE_RUN_NO_INTERPRETER)
    assert f.severity == preflight.BLOCK
    # 必须说出后果，否则用户不知道这条为什么要管
    assert "干活" in f.detail
    assert f.fix.strip(), "挡路项没给修法"


def test_claude_probe_timeout_does_not_report_red():
    """探活自己超时 ⇒ 不吭声。凭猜测报红会把能装的人拦在门外。"""
    assert preflight.check_claude_launch(preflight.CLAUDE_RUN_SKIPPED).severity == preflight.OK
    assert preflight.check_claude_launch(preflight.CLAUDE_RUN_OK).severity == preflight.OK
    mac = _read(MAC_SRC)
    assert '"")  CLAUDE_LAUNCH="skipped"' in mac, "mac 侧探活超时后没退回 skipped"


def test_claude_probe_is_time_bounded_and_kills_by_pid():
    """探活挡在所有交互前面 ⇒ 必须限时；收尾必须按 pid，不许按进程名。"""
    mac = _read(MAC_SRC)
    probe = mac[mac.index('CLAUDE_LAUNCH="skipped"') : mac.index("# 体检脚本临时落盘")]
    assert 'kill "$PROBE_PID"' in probe, "探活没有超时收尾 ⇒ 可能永远卡住"
    for forbidden in ("pkill -f", "killall", "pgrep"):
        assert forbidden not in probe, (
            f"探活用了 {forbidden} —— 会连「命令行里提到这个词的无关进程」一起杀"
        )


def test_this_machine_passes_the_real_probe():
    """阳性对照：在这台机器上真跑一遍探活那条命令，确认它返回 0。

    不验这个的话，上面几条可能只是在测一个永远走不通的分支。

    🔴 11-18：判据要和安装器一致 —— 安装器**不是**照 `which claude` 写开机自启的。
    `which claude` 可能是转发脚本（本机 cmux 的 cmux-cli-shims/<id>/claude：它把自己
    所在目录从 PATH 剔掉再 exec claude ⇒ 在服务那份 PATH 下必然 127），所以安装器会
    先真跑一次、跑不通就在固定位置里挑一个能跑通的（install-mac.sh 的
    `_carher_claude_runs`，见 tests/test_r38_claude_wrapper.py）。
    这条要验的是「装机那一刻挑出来的那个 claude 在服务那份 PATH 下能跑」，
    所以这里也要走同一套挑选，否则它测的是一台装了版本管理器的机器上根本不会被用的路径。
    """
    def runs(exe: str) -> bool:
        if not exe or not os.access(exe, os.X_OK):
            return False
        service_path = os.pathsep.join(
            [os.path.dirname(exe), "/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin"]
        )
        try:
            proc = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=30,
                env={"HOME": os.path.expanduser("~"), "PATH": service_path},
            )
        except OSError:
            return False
        return proc.returncode == 0

    claude = shutil.which("claude")
    if not claude:
        pytest.skip("这台机器上没有 claude")
    if runs(claude):
        return
    # 和 install-mac.sh 里同一份候选清单、同一个顺序
    home = os.path.expanduser("~")
    for cand in (f"{home}/.carher-npm-global/bin/claude", f"{home}/.local/bin/claude",
                 "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        if cand != claude and runs(cand):
            return
    pytest.fail(
        f"本机挑不出一个在服务那份 PATH 下能跑的 claude（PATH 里那个是 {claude}）—— "
        "装机也会挑不出来，桥会起不动它"
    )


def test_step_headers_still_count_to_six():
    """接体检时**不许**把步骤数改掉。

    `test_install_ux.test_every_step_header_carries_an_expectation` 只认 `[N/6]`；
    改成 7 步的话那条文案闸门会一条都匹配不到 ⇒ 恒绿。
    """
    for path, pattern in ((MAC_SRC, r'say "\[(\d)/6\]'), (WIN_SRC, r'Say "\[(\d)/6\]')):
        headers = re.findall(pattern, _read(path))
        assert len(headers) == 6, f"{os.path.basename(path)} 的步骤标题不是 6 条：{headers}"
