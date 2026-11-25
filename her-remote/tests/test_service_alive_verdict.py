"""第 6 步的绿灯必须以**进程真的活着**为判据。

## 为什么存在

原来那一步只看 `launchctl print` 返回 0。2026-11-05 实测（注册一个秒退的 job，
跑完立刻卸掉、不碰真实例）：

| | 真的活着 | 崩溃循环中 |
|---|---|---|
| `launchctl print` 退出码 | 0 | **0** |
| `state` | `running` | `spawn scheduled` |
| `pid` 这一行 | `pid = 7082` | **整份输出里没有这一行** |
| `runs` | 不动 | 12 秒内 4 → 5 |
| `last exit code` | 没有 | `1` |

也就是说**退出码完全区分不开这两种**，而「有没有 pid 这一行」+「runs 涨不涨」
能一刀切开。这是「装完打绿而它已经崩了」四条路里的第三条（另三条见
`scripts/preflight.py` 开头和下面那条 LOG_MARK）。

## 两个实现细节

1. **按字段取，不写死缩进。** 用 `awk '$1=="pid"'` 而不是把制表符写进正则 ——
   一旦 launchctl 换了排版，写死缩进的正则会**静默取不到值** ⇒ 判据恒「没有 pid」
   ⇒ 好机器被报成起不来。
   （我一度以为理由是「BSD sed 不认 `\\t`」，**实测这是错的**：本机
   `sed -n 's/^\\tpid = …'` 能正常匹配。换 awk 是为了不依赖排版。）
2. **日志只能看本轮新写的那一段。** 实测 `agent.log` 跨重启累积（本机 664 行、
   跨 3 天、58 次「桥已启动」、162 条 `connected to`）⇒ 整文件 grep 在**重装**时
   第一轮循环就命中三天前那一行，判据和本次启动彻底脱钩。

## 怎么测

把安装器里那段判据原样抽出来，装进一个 harness 跑：伪造一个 `launchctl`
按需返回上表里的各种形状，再喂一份假日志。不碰真服务、不碰 `~/.carher-feishu-agent`。
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import uuid

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
MAC_SRC = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN_SRC = os.path.join(_ONECLICK, "src", "install-windows.ps1")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def here_dir():
    """夹具建在测试目录旁边，**不用 tmp_path**。

    macOS 的 tmp_path 落在 `/private/var/folders/`，那是 `pathguard` 的系统豁免前缀 ——
    这组测试本身不判路径，但统一不用它，免得以后往这里加路径类用例时假绿
    （和 test_groupsetup.py 的 here_dir 同一条纪律）。
    """
    base = pathlib.Path(_HERE).parent / f".svcverdict-tmp-{uuid.uuid4().hex[:8]}"
    (base / "logs").mkdir(parents=True)
    (base / "bin").mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _verdict_snippet() -> str:
    """把 install-mac.sh 里那段绿灯判据原样抽出来。

    🔴 判据是「跑**安装器里的那段代码**」，不是「抽出逻辑重写一遍」——
    重写的话，删掉安装器里的调用点测试照样绿（这个项目栽过这一次）。
    """
    text = _read(MAC_SRC)
    start = text.index("# 🔴 绿灯判据")
    end = text.index("# 自检：长连接有没有真的连上")
    return text[start:end]


# launchctl 的三种形状。缩进必须是**真的制表符**，否则就测不到 BSD sed 那个坑。
_FAKE_LAUNCHCTL = """#!/bin/bash
case "$FAKE_STATE" in
  healthy)   printf '\\tstate = running\\n\\truns = 48\\n\\tpid = 7082\\n' ;;
  # 崩溃循环里 pid 可能恰好非 0（刚被 KeepAlive 拉起来的那个）——
  # 这正是 pid 那一条拦不住、必须靠 runs 的形状
  climbing)  printf '\\tstate = running\\n\\truns = %s\\n\\tpid = 9001\\n' "$(cat "$FAKE_RUNS")" ;;
  nopid)     printf '\\tstate = spawn scheduled\\n\\truns = 3\\n\\tlast exit code = 1\\n' ;;
esac
exit 0
"""


def _run_verdict(here, log_body: str, state: str, runs_before=3, runs_after=3) -> str:
    """喂一种机器状态 + 一份日志，返回那段判据打到屏幕上的东西。"""
    launchctl = here / "bin" / "launchctl"
    launchctl.write_text(_FAKE_LAUNCHCTL, encoding="utf-8")
    launchctl.chmod(0o755)
    (here / "logs" / "agent.log").write_text(log_body, encoding="utf-8")
    (here / "runs").write_text(f"{runs_before}\n", encoding="utf-8")

    preamble = (
        "set -uo pipefail\n"
        f'PATH="{here}/bin:$PATH"\n'
        f'export FAKE_STATE="{state}" FAKE_RUNS="{here}/runs"\n'
        f'LOG_DIR="{here}/logs"\n'
        'LABEL="fake.label"\n'
        # 安装器在放桌面诊断那一步定义它(判据段的 die 文案里要用);这里没有就会被 set -u 当场打断
        'DOCTOR_HINT="双击桌面上的「CarHer 助手诊断」"\n'
        # 同上：第 39 轮起判据段的 die 文案也用 UNINSTALL_HINT（放桌面卸载那一步定义它）
        'UNINSTALL_HINT="双击桌面上的「CarHer 助手卸载」"\n'
        "ok(){ printf 'VERDICT=GREEN %s\\n' \"$1\"; }\n"
        "die(){ printf 'VERDICT=RED %s\\n' \"$1\"; exit 1; }\n"
        "LOG_MARK=1\n"
        'new_log(){ tail -c "+$LOG_MARK" "$LOG_DIR/agent.log" 2>/dev/null || true; }\n'
        # 判据第二次读 runs 时才变 —— 模拟「5 秒内又被拉起了一次」
        f'( sleep 2; echo "{runs_after}" > "{here}/runs" ) &\n'
    )
    proc = subprocess.run(
        ["/bin/bash", "-c", preamble + _verdict_snippet()],
        capture_output=True,
        text=True,
        timeout=60,
    )
    # 🔴 判据段引用了段外定义的变量（DOCTOR_HINT / UNINSTALL_HINT / LABEL…）时，
    # `set -u` 让整段**静默**死掉、stdout 为空，下游断言就报成「没告诉用户它崩了」——
    # 看起来像产品回归，实际是台架少喂了一个变量（11-19 第 39 轮加 UNINSTALL_HINT 时撞到，
    # 前人为 DOCTOR_HINT 撞过同一下）。量具坏了要自己说出来，不许伪装成产品的问题。
    if not proc.stdout.strip():
        raise AssertionError(
            "判据段一个字都没输出 ⇒ 多半是台架少喂了段外变量，看 stderr：\n"
            f"{proc.stderr.strip() or '(stderr 也是空的)'}"
        )
    return proc.stdout


HEALTHY_LOG = (
    "2026-11-05 16:48:13,513 INFO feishu-bridge 桥已启动。模式=readonly\n"
    "[Lark] [2026-11-05 16:48:14,714] [INFO] connected to wss://open.feishu.cn/x\n"
)
# 真实形状：目录名带一个反斜杠 ⇒ tomllib 在启动时抛，而日志里**既没有** connected
# **也没有** connect failed ⇒ 原来的判据会落到「它还在重试连接，等两分钟」。
CRASH_LOG = (
    "Traceback (most recent call last):\n"
    '  File "/Users/RealName/.carher-feishu-agent/bridge/app.py", line 246, in load\n'
    "    raw = tomllib.load(handle)\n"
    "tomllib.TOMLDecodeError: Unescaped '\\' in a string (at line 7, column 27)\n"
)


def test_healthy_service_is_green(here_dir):
    """阳性对照：真活着必须打绿，否则下面几条可能只是「永远报红」。"""
    out = _run_verdict(here_dir, HEALTHY_LOG, "healthy", 48, 48)
    assert "VERDICT=GREEN" in out, f"真活着却报红\n{out}"


def test_crash_on_startup_is_not_green(here_dir):
    """🔴 本轮最关键的一条：起来就崩不许打绿，且要说「它起来就崩了」。"""
    out = _run_verdict(here_dir, CRASH_LOG, "nopid")
    assert "VERDICT=GREEN" not in out, f"假绿：它起来就崩了却报成功\n{out}"
    assert "崩" in out, f"没告诉用户「它起来就崩了」\n{out}"


def test_crash_verdict_names_the_error_class(here_dir):
    """要把最后一类错告诉用户 —— 否则他拿不到任何可以转给 IT 的线索。"""
    out = _run_verdict(here_dir, CRASH_LOG, "nopid")
    assert "tomllib.TOMLDecodeError" in out, f"没给出错误类名\n{out}"


def test_crash_verdict_never_prints_the_log_body(here_dir):
    """🔴 只给类名，**不许**打日志原文。

    本机 494 行真日志里有 189 个敏感 token（ws ticket、open_id、`/Users/<真名>/…`），
    而这一屏是用户最可能截图发给 IT 的一屏。
    """
    noisy = CRASH_LOG.replace(
        "    raw = tomllib.load(handle)\n",
        "    ticket=AbC123SecretTicketValue open_id=ou_deadbeefcafe\n",
    )
    out = _run_verdict(here_dir, noisy, "nopid")
    for leak in (
        "RealName",
        "AbC123SecretTicketValue",
        "ou_deadbeefcafe",
        "line 246",
        "Traceback",
    ):
        assert leak not in out, f"日志原文漏到屏幕上了：{leak}\n{out}"


def test_missing_pid_is_not_green(here_dir):
    """`launchctl print` rc=0 但没有 pid 那一行 = 秒退。实测形状。"""
    out = _run_verdict(here_dir, "桥已启动\n", "nopid")
    assert "VERDICT=GREEN" not in out, f"假绿：没有 pid 却报在跑\n{out}"


def test_climbing_restart_count_is_not_green(here_dir):
    """pid 非 0 但 runs 在涨 = 崩溃循环。

    这条单独存在的理由：崩溃循环里 pid 可能恰好非 0（刚被 KeepAlive 拉起来的那个），
    所以 pid 那一条**拦不住**这种形状，必须数 runs。
    """
    out = _run_verdict(here_dir, "桥已启动\n", "climbing", runs_before=4, runs_after=5)
    assert "VERDICT=GREEN" not in out, f"假绿：服务在反复重启却报在跑\n{out}"
    assert "重启" in out, f"没说清是「在反复重启」\n{out}"


def test_steady_restart_count_is_green(here_dir):
    """阴性对照：runs 不动时必须放过 —— 否则上一条只是「永远红」。"""
    out = _run_verdict(here_dir, HEALTHY_LOG, "climbing", runs_before=7, runs_after=7)
    assert "VERDICT=GREEN" in out, f"runs 没涨却报反复重启\n{out}"


# ---------- 判据本身的写法纪律 ----------


def test_verdict_does_not_rely_on_launchctl_exit_code(here_dir):
    """🔴 不许退回「`launchctl print` 成功就打绿」。

    实测崩溃循环下它也返回 0 —— 这正是原来那套假绿的根。
    """
    snippet = _verdict_snippet()
    assert not re.search(r"if\s+launchctl print[^\n]*>/dev/null 2>&1; then", snippet), (
        "又用 launchctl print 的退出码当判据了 —— 崩溃循环下它也是 0"
    )


def test_fields_are_read_by_field_not_by_hardcoded_indent():
    """取 pid/runs 不许依赖缩进字符 —— 排版一变就静默取不到值、好机器被判起不来。"""
    text = _read(MAC_SRC)
    assert "awk -v want=" in text, "没用 awk 按字段取 launchctl 的输出"


def test_field_extraction_survives_both_indent_styles():
    """阳性对照 + 它要防的那件事：制表符和空格缩进都得取得到。

    同时钉住「写死缩进的那种写法会静默失败」—— 用空格缩进的输入喂进去，
    按制表符写死的正则取不到任何东西（而 awk 取得到）。
    """
    awk = 'awk \'$1=="pid" && $2=="=" {print $3; exit}\''
    tab_sed = r"sed -n 's/^\tpid = \([0-9]*\)$/\1/p'"
    for line, label in (("\tpid = 7082\n", "制表符缩进"), ("    pid = 7082\n", "空格缩进")):
        got = subprocess.run(
            ["/bin/sh", "-c", awk], input=line, capture_output=True, text=True
        ).stdout.strip()
        assert got == "7082", f"awk 在{label}下取不到字段：{got!r}"
    # 写死制表符的那种写法：空格缩进时静默返回空 —— 这就是不用它的理由
    spaced = subprocess.run(
        ["/bin/sh", "-c", tab_sed], input="    pid = 7082\n", capture_output=True, text=True
    ).stdout.strip()
    assert spaced == "", (
        f"按制表符写死的正则居然匹配了空格缩进（得到 {spaced!r}）—— 这条判据要重新想"
    )


# ---------- 日志只看本轮 ----------


# ---------- Windows 侧（只能静态查） ----------
#
# ⚠️ 这一节**全部是静态核对**。手上没有 Windows，这几条判据也从没在真机跑过 ——
# 它们能抓的只有「有人把判据删了」，抓不了「判据在真 Windows 上到底成不成立」。
# 不许把这节的绿当成「Windows 验过了」。
# 2026-11-05 变异验证时实测：把 Windows 侧的进程判据改成恒假，30 条测试全绿 ——
# 这一节就是为补那个洞加的。


def test_windows_verdict_checks_the_process_not_the_task_state():
    """🔴 任务的 `State` 只证明调度器认它，不证明进程活着。

    和 launchd 的 `launchctl print` rc=0 是同一类错觉，而 doctor-windows.ps1
    自己早就写明「任务状态是 Ready 也可能进程已经没了」。
    """
    win = _read(WIN_SRC)
    verdict = win[win.index("# 🔴 绿灯判据") :]
    assert "$svcProcs.Count -lt 1" in verdict, (
        "Windows 侧不再判「进程在不在」—— 注册成功不等于进程活着"
    )
    assert "Get-CimInstance Win32_Process" in verdict, "Windows 侧没有按进程判活"
    assert re.search(r"-m\\s\+bridge\\\.app", verdict) or "-m\\s+bridge\\.app" in verdict, (
        "Windows 侧的进程匹配不够精确 —— 宽松匹配会把 doctor 自己算进来（实测误报过）"
    )
    # 不许退回「任务状态是 Ready 就打绿」
    assert "$task.State -eq 'Ready'" not in verdict, (
        "Windows 侧用任务状态当绿灯判据了 —— State=Ready 时进程可能已经没了"
    )


def test_windows_verdict_catches_crash_and_restart_loop():
    """三条判据都要在：Traceback、反复重启、进程不在。"""
    win = _read(WIN_SRC)
    verdict = win[win.index("# 🔴 绿灯判据") :]
    assert "(?m)^Traceback" in verdict, "Windows 侧不查 Traceback"
    assert "[runner] 进程退出" in verdict, (
        "Windows 侧没有「反复重启」的判据 —— 计划任务没有 launchd 的 runs 计数，"
        "我们自己那层 while 循环每次重启写的那行才是等价物"
    )
    assert "$crashClass" in verdict and "$restarted" in verdict


def test_windows_crash_verdict_only_names_the_error_class():
    """只给类名，不许把日志原文打出来（本机 494 行日志里有 189 个敏感 token）。"""
    win = _read(WIN_SRC)
    verdict = win[win.index("# 🔴 绿灯判据") :]
    assert "Error|Exception|Interrupt" in verdict, "Windows 侧没有按类名抽取"
    # 典型的泄露写法：把日志尾巴直接塞进给用户的那句话
    for leak in ("-Tail 30", "$newLog\"", "$newLog`n"):
        assert leak not in verdict.replace("Get-Content", ""), (
            f"Windows 侧可能把日志原文打给用户了：{leak}"
        )


def test_both_installers_only_read_this_rounds_log():
    """🔴 `agent.log` 跨重启累积 ⇒ 整文件 grep 在重装时会命中上周那一行。

    实测本机这份日志：664 行、11-03 到 11-05、58 次「桥已启动」、162 条 `connected to`。
    那意味着重装时第一轮循环就 CONNECTED=true，判据和本次启动彻底脱钩。
    """
    mac = _read(MAC_SRC)
    assert "LOG_MARK" in mac and "new_log()" in mac, "mac 侧没有「只看本轮日志」的切片"
    assert not re.search(r'grep -q "connected to" "\$LOG_DIR/agent\.log"', mac), (
        "mac 侧还在整文件 grep connected to ⇒ 重装时会命中上一次的那一行"
    )

    win = _read(WIN_SRC)
    assert "logMark" in win and "Get-NewLog" in win, "Windows 侧没有「只看本轮日志」的切片"
    assert "Select-String -Path \"$logDir\\agent.log\" -Pattern 'connected to'" not in win, (
        "Windows 侧还在整文件搜 connected to"
    )
    # 🔴 名字在 ≠ 真的从那个位置读。2026-11-05 变异验证实测：把
    # `$stream.Position = $logMark` 改成 `= 0`（等于整文件），只查名字的判据全绿。
    assert "$stream.Position = $logMark" in win, (
        "Windows 侧的切片没有真的从本轮起点读 ⇒ 等于整文件，重装时会假绿"
    )
    assert "$stream.Length -le $logMark" in win, (
        "Windows 侧没有「本轮还没有新内容」那条短路 ⇒ 会读到越界或旧内容"
    )
    assert "logMark = (Get-Item $logPath).Length" in win, (
        "Windows 侧没有在启动服务前记下日志位置"
    )


def _real_new_log_definition() -> str:
    """把安装器里**那一行** `new_log()` 的定义原样取出来。

    🔴 不许在测试里自己写一份同样的函数。2026-11-05 变异验证时实测：
    测试里内联一份 `new_log`，然后把安装器里的实现换成 `cat 整个文件`，
    12 条测试**全绿** —— 测试测的是它自己那份副本，和安装器已经脱钩。
    这正是「只验写完是绿的不算」那条纪律指的形状。
    """
    text = _read(MAC_SRC)
    match = re.search(r"(?m)^new_log\(\) \{.*\}$", text)
    assert match, "install-mac.sh 里找不到 new_log() 的定义 —— 判据要跟着改"
    return match.group(0)


def test_log_slice_ignores_everything_before_this_round(here_dir):
    """用安装器里**真正那一行**切片实现跑：老日志读不到，本轮新写的读得到。"""
    log = here_dir / "logs" / "agent.log"
    log.write_text("[Lark] connected to wss://old-from-three-days-ago\n", encoding="utf-8")
    mark = log.stat().st_size + 1
    script = (
        f'LOG_DIR="{here_dir}/logs"\n'
        f"LOG_MARK={mark}\n"
        f"{_real_new_log_definition()}\n"
        'if new_log | grep -q "connected to"; then echo HIT; else echo MISS; fi\n'
    )
    before = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True
    ).stdout
    assert "MISS" in before, (
        "切片读到了本轮之前的日志 ⇒ 重装时第一轮就命中上周那行 connected，判据和本次启动脱钩"
    )

    with open(log, "a", encoding="utf-8") as handle:
        handle.write("[Lark] connected to wss://this-round\n")
    after = subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True
    ).stdout
    assert "HIT" in after, "本轮真写进去的 connected 却读不到 ⇒ 判据恒失败"


def test_log_mark_is_taken_before_the_service_starts(here_dir):
    """🔴 记位置必须在**启动服务之前**。

    记晚了（比如 bootstrap 之后）会把本轮自己刚写的 connected 一起切掉 ⇒ 判据恒失败。
    """
    text = _read(MAC_SRC)
    mark_at = text.index("LOG_MARK=1")
    boot_at = text.index("launchctl bootstrap")
    assert mark_at < boot_at, "LOG_MARK 记在了启动服务之后 ⇒ 本轮的日志会被切掉"


def test_harness_stubs_every_outside_variable_the_segment_uses():
    """🔴 判据段新引用一个段外变量 ⇒ 立刻点名，而不是等到 stdout 空、下游报「判词不见了」。

    这条是量具的自检。11-19 第 39 轮：判据段里的 die 文案改用 `${UNINSTALL_HINT}`
    （它在安装器放桌面卸载那一步定义，在段外），台架没跟上 ⇒ `set -u` 让整段静默死掉，
    三条测试一起红在「没告诉用户它崩了」上，看起来像产品回归。前人为 `DOCTOR_HINT`
    撞过同一下（preamble 里那行注释就是那次留下的）。
    """
    seg = _verdict_snippet()
    used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)[:\}]", seg))
    used |= set(re.findall(r"\$([A-Z_][A-Z0-9_]*)", seg))
    defined = set(re.findall(r"^\s*([A-Z_][A-Z0-9_]*)=", seg, re.M))
    # 环境自带的不用喂
    builtin = {"HOME", "PATH", "IFS", "PWD", "USER", "SHELL", "TMPDIR", "LANG"}
    outside = used - defined - builtin
    harness = _read(__file__) if False else open(__file__, encoding="utf-8").read()
    missing = sorted(v for v in outside if f"{v}=" not in harness)
    assert not missing, (
        f"判据段用了这些段外变量但台架没喂：{missing} —— "
        "不补的话 set -u 会让整段静默死掉，报出来的会是「判词不见了」"
    )
