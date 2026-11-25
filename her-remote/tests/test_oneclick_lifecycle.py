"""运维与生命周期脚本的回归测试：卸载防护、睡眠自检、静默失联自检。

这些测试盯的是**三类会在推广到上百人时变成支持负担的问题**，每一类都配了
「文本特征」+ 尽量带一条「真跑一遍」的阳性对照 —— 只查文本的判据太容易变成
洁癖检查，查不出判据本身是不是写错了。

1. **卸载脚本的空变量防护。** `rm -rf "$APP_HOME"` 在 APP_HOME 为空时是
   `rm -rf ""`，而拼过路径的写法（`rm -rf "$APP_HOME/"*`）会变成 `rm -rf /*`。
   卸载脚本是**唯一一个会删东西**的产物，这条防护失效的后果不可逆，所以这里
   既查「所有 rm 都走 safe_rm」，也真的把 safe_rm 抽出来喂空值跑一遍。

2. **「电脑睡着了就收不到，而且不会补发」。** 飞书长连接只投递给在线客户端，
   没有任何补发机制 ⇒ 合盖期间用户发的消息一个字都不会回，而这和「它正在思考」
   在用户眼里完全一样。doctor 必须查睡眠设置并说清后果，且**只读不代改**。

3. **静默失联。** lark-oapi issue #169：重连路径有个没设超时的请求，卡住时
   进程还活着、不报错、日志直接断掉（有人实测静默失联 3 天 13 小时）⇒
   「进程在跑」是绿的也不代表还能收消息，必须看日志最后一行的时间。
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick")
)

UNINSTALL_MAC = os.path.join(_ONECLICK, "src", "uninstall-mac.sh")
UNINSTALL_WIN = os.path.join(_ONECLICK, "src", "uninstall-windows.ps1")
DOCTOR_MAC = os.path.join(_ONECLICK, "src", "doctor-mac.sh")
DOCTOR_WIN = os.path.join(_ONECLICK, "src", "doctor-windows.ps1")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _code_lines(path: str):
    """去掉注释和空行 —— 判据只能看真正会执行的代码。

    不这么做的话，注释里写一句 `rm -rf "$APP_HOME"` 当反面教材就会把测试搞红。
    """
    for lineno, line in enumerate(_read(path).splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield lineno, line


# ─────────────────────── 1. 卸载：空变量防护 ───────────────────────


def test_uninstall_scripts_exist():
    """没有卸载路径 = 离职/换机时 launchd job 还在、配置里还有 app_secret。"""
    for path in (UNINSTALL_MAC, UNINSTALL_WIN):
        assert os.path.exists(path), f"缺卸载脚本：{path}"


def test_mac_uninstall_has_no_bare_rm_rf():
    """所有删除都必须走 safe_rm —— 裸 `rm -rf "$VAR"` 在 VAR 为空时后果不可逆。

    唯一允许出现 `rm -rf` 的地方是 safe_rm 函数体自己（它已经校验过路径了）。
    """
    offenders = []
    for lineno, line in _code_lines(UNINSTALL_MAC):
        if not re.search(r"\brm\s+-[a-zA-Z]*r", line):
            continue
        # safe_rm 内部那一行用 `rm -rf -- "$target"`，前面五道校验都过了才到这
        if 'rm -rf -- "$target"' in line:
            continue
        offenders.append(f"{lineno}: {line.strip()}")
    assert not offenders, (
        "卸载脚本里有没走 safe_rm 的递归删除（变量为空时会删错地方）：\n"
        + "\n".join(offenders)
    )


def test_mac_uninstall_guard_rejects_dangerous_paths():
    """阳性对照：把 safe_rm 抽出来真跑一遍，证明防护不是注释里的承诺。

    只查「代码里有 safe_rm」是不够的 —— 得证明它真的拦得住空值。
    """
    source = _read(UNINSTALL_MAC)
    guard = re.search(r"^safe_rm\(\) \{.*?^\}", source, re.S | re.M)
    assert guard, "没找到 safe_rm 函数体，判据要重新想"
    colors = re.search(r"^BLUE=.*?^warn\(\).*?$", source, re.S | re.M)
    assert colors, "没找到颜色/输出函数，harness 跑不起来"

    # 每个用例：(路径表达式, 期望)。allow 的那几个证明防护没有把正常路径也拦掉。
    cases = [
        ('""', "deny"),          # 空变量 —— 本防护的首要目标
        ('"   "', "deny"),       # 只有空白
        ('"/"', "deny"),         # 根目录
        ('"$HOME"', "deny"),     # 家目录本身
        ('"$HOME/"', "deny"),    # 家目录带斜杠
        ('"$HOME/Documents"', "deny"),      # 用户自己的东西
        ('".carher-feishu-agent"', "deny"),  # 相对路径
        # 字面量星号（引号内，不让 shell 先展开）—— 展开后范围会超出预期
        ('"$APP_HOME/*"', "deny"),
        ('"$APP_HOME/../Documents"', "deny"),  # ..
        ('"/usr/local"', "deny"),            # 不在白名单
        ('"$APP_HOME"', "allow"),
        ('"$APP_HOME/logs/agent.log"', "allow"),
    ]

    checks = "\n".join(
        f'if safe_rm {expr} >/dev/null 2>&1; then echo "{i} allow"; '
        f'else echo "{i} deny"; fi'
        for i, (expr, _) in enumerate(cases)
    )
    script = f"""
set -uo pipefail
export HOME="$TESTHOME"
{colors.group(0)}
APP_HOME="$HOME/.carher-feishu-agent"
BRIDGE_CC="$HOME/.claude-feishu-bridge"
LOCK="$HOME/.feishu-agent-bridge.lock"
PLIST="$HOME/Library/LaunchAgents/com.carher.feishu-agent.plist"
{guard.group(0)}
mkdir -p "$APP_HOME/logs" "$HOME/Documents"
echo keep > "$HOME/Documents/keep.txt"
echo x > "$APP_HOME/logs/agent.log"
{checks}
echo "HOME_ALIVE=$([ -d "$HOME" ] && echo 1 || echo 0)"
echo "USERFILE_ALIVE=$([ -f "$HOME/Documents/keep.txt" ] && echo 1 || echo 0)"
"""
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        testhome = os.path.join(tmp, "home")
        os.makedirs(testhome)
        proc = subprocess.run(
            ["/bin/bash", "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "TESTHOME": testhome},
        )
        out = proc.stdout
        for i, (expr, expected) in enumerate(cases):
            assert f"{i} {expected}" in out, (
                f"safe_rm {expr} 期望 {expected}，实际输出：\n{out}\n{proc.stderr}"
            )
        # 防护跑完之后，家目录和用户文件必须一个没少
        assert "HOME_ALIVE=1" in out, f"测试把家目录删了：\n{out}"
        assert "USERFILE_ALIVE=1" in out, f"测试把用户文件删了：\n{out}"


def test_windows_uninstall_refuses_empty_localappdata():
    """Windows 侧同一条：LOCALAPPDATA 取不到时必须退出，不能退化成相对路径。"""
    text = _read(UNINSTALL_WIN)
    assert "IsNullOrWhiteSpace($localAppData)" in text, (
        "没有校验 LOCALAPPDATA 为空 —— 那会让删除目标变成相对路径"
    )
    assert "IsNullOrWhiteSpace($userProfile)" in text
    assert "Remove-Safely" in text, "Windows 侧也要有统一的删除防护函数"


def test_windows_uninstall_has_no_bare_remove_item():
    """所有 Remove-Item -Recurse 都必须走 Remove-Safely。"""
    offenders = []
    for lineno, line in _code_lines(UNINSTALL_WIN):
        if "Remove-Item" not in line:
            continue
        if "-Recurse" not in line:
            continue
        if "$target" in line:   # Remove-Safely 内部那一行
            continue
        offenders.append(f"{lineno}: {line.strip()}")
    assert not offenders, (
        "Windows 卸载脚本里有没走 Remove-Safely 的递归删除：\n" + "\n".join(offenders)
    )


def test_windows_uninstall_kills_by_commandline_not_process_name():
    """按进程名杀会打到用户别的 python/powershell —— 必须按命令行匹配。"""
    text = _read(UNINSTALL_WIN)
    assert "CommandLine -like '*bridge.app*'" in text, (
        "残留进程必须按命令行里的 bridge.app 精确匹配"
    )
    assert not re.search(r"Stop-Process\s+-Name", text), (
        "不许按进程名杀：用户机器上还有别的 python.exe / powershell.exe"
    )


# ─────────────────────── 2. 卸载：凭据擦除 + 确认 + 云端 ───────────────────────


def test_uninstall_shreds_credentials_before_delete():
    """config.toml 里有 app_secret，只 unlink 盘上字节还在 ⇒ 必须先覆写。"""
    mac = _read(UNINSTALL_MAC)
    assert "shred_file" in mac, "Mac 卸载没有覆写函数"
    assert "/dev/urandom" in mac, "覆写要写随机字节"
    assert "conv=notrunc" in mac, (
        "必须原地覆写（conv=notrunc），换文件的话旧内容还留在盘上"
    )
    win = _read(UNINSTALL_WIN)
    assert "Clear-FileBytes" in win, "Windows 卸载没有覆写函数"
    assert "RandomNumberGenerator" in win


def test_mac_shred_actually_removes_the_secret(tmp_path):
    """阳性对照：真写一个带 secret 的文件，跑 shred_file，确认字节不在了。"""
    source = _read(UNINSTALL_MAC)
    shred = re.search(r"^shred_file\(\) \{.*?^\}", source, re.S | re.M)
    assert shred, "没找到 shred_file 函数体"

    target = tmp_path / "config.toml"
    secret = "SUPERSECRET_abcdefghijklmnop123456"
    target.write_text(f'app_secret = "{secret}"\n', encoding="utf-8")

    script = f"""
set -uo pipefail
{shred.group(0)}
shred_file "$1"
"""
    proc = subprocess.run(
        ["/bin/bash", "-c", script, "bash", str(target)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert secret.encode() not in target.read_bytes(), (
        "覆写之后 secret 字节还在文件里"
    )
    # 空参数不该把什么东西删掉，也不该成功
    proc = subprocess.run(
        ["/bin/bash", "-c", script, "bash", ""], capture_output=True, text=True
    )
    assert proc.returncode != 0, "shred_file 对空路径应该返回失败"


def test_uninstall_confirms_before_deleting():
    """默认不是无条件删：要先列清单再等用户确认；--yes 才跳过。"""
    mac = _read(UNINSTALL_MAC)
    assert "ASSUME_YES" in mac and "--yes" in mac, "缺 --yes（IT 批量要用）"
    assert "已取消" in mac, "取消分支必须明确告诉用户什么都没动"
    assert "将要删除这些东西" in mac, "必须先把要删的东西列出来"

    win = _read(UNINSTALL_WIN)
    assert "assumeYes" in win and "--yes" in win
    assert "已取消" in win
    # 双击 .bat 时 $args 到不了 PS（bat 头没有 %*）⇒ 必须有环境变量这条路
    assert "CARHER_UNINSTALL_YES" in win, (
        "双击 .bat 时参数到不了 PowerShell，IT 批量必须能靠环境变量跳过确认"
    )


def test_uninstall_says_feishu_app_must_be_handled_by_user():
    """脚本删不掉云端的东西，必须明说，不能让用户以为卸载完就都没了。

    自建应用和它的 app_secret 在飞书那边仍然有效 —— 谁拿到凭据都能在别的机器上
    装一次。不说这句话就是在制造一个「以为清干净了」的安全假象。
    """
    for path in (UNINSTALL_MAC, UNINSTALL_WIN):
        text = _read(path)
        assert "open.feishu.cn" in text, f"{path} 没告诉用户去哪处理自建应用"
        assert "停用" in text, f"{path} 没说要停用/删除自建应用"


def test_uninstall_stops_service_before_deleting_files():
    """顺序：先注销服务，再删文件。

    反过来的话 launchd 的 KeepAlive / Windows 那层重启循环会在删除中途把进程
    拉起来，它又重新写日志和锁文件 —— 删了也会回来。
    """
    mac = _read(UNINSTALL_MAC)
    stop_at = mac.index("launchctl bootout")
    # 第一个 remove 调用（删文件阶段）必须在 bootout 之后
    delete_at = mac.index('remove "$PLIST"')
    assert stop_at < delete_at, "必须先 bootout 再删文件，否则 KeepAlive 会把它拉回来"

    win = _read(UNINSTALL_WIN)
    assert win.index("Unregister-ScheduledTask") < win.index("Remove-Target"), (
        "必须先注销计划任务再删文件，否则重启循环会把进程拉回来"
    )


def test_uninstall_whitelist_is_exactly_this_set():
    """🔴 删除范围必须**逐项钉死**：加一条路径就要来改这个测试 = 显式 review。

    原来这条只断言「该有的几个在不在」，根本拦不住往白名单里再塞路径
    （docstring 写着「不能悄悄扩大删除范围」，代码却没检查 —— 空承诺）。
    第 39 轮加桌面快捷方式时发现的：加完照样绿。
    """
    mac = _read(UNINSTALL_MAC)
    loop = re.search(r"^\s*for item in (.+?); do$", mac, re.M)
    assert loop, "没找到 safe_rm 的白名单循环，判据要重新想"
    got = set(re.findall(r"\$\{?([A-Z_]+)", loop.group(1)))
    assert got == {"APP_HOME", "BRIDGE_CC", "LOCK", "PLIST",
                   "DESKTOP_DOCTOR", "DESKTOP_UNINSTALL"}, (
        f"Mac 卸载的白名单变了：{sorted(got)} —— 扩大删除范围要显式 review"
    )
    win = _read(UNINSTALL_WIN)
    roots = re.search(r"^\$allowedRoots = @\((.+?)\)$", win, re.M)
    assert roots, "没找到 Windows 的 $allowedRoots，判据要重新想"
    wgot = set(re.findall(r"\$([A-Za-z]\w*)", roots.group(1)))
    assert wgot == {"appHome", "bridgeCC", "lock", "deskDoctor", "deskUninstall"}, (
        f"Windows 卸载的白名单变了：{sorted(wgot)} —— 扩大删除范围要显式 review"
    )


def test_uninstall_targets_only_the_four_known_paths():
    """白名单里该有的那几个在不在（范围有没有被扩大看上面那条）。"""
    mac = _read(UNINSTALL_MAC)
    for expected in (
        ".carher-feishu-agent",
        ".claude-feishu-bridge",
        ".feishu-agent-bridge.lock",
        "com.carher.feishu-agent.plist",
    ):
        assert expected in mac, f"Mac 卸载漏了 {expected}"
    win = _read(UNINSTALL_WIN)
    for expected in (
        "CarHerFeishuAgent",
        ".claude-feishu-bridge",
        ".feishu-agent-bridge.lock",
        "CarHerFeishuAgent",
    ):
        assert expected in win, f"Windows 卸载漏了 {expected}"


# ─────────────────────── 3. doctor：会不会睡着 ───────────────────────


def test_doctor_checks_sleep_settings():
    """飞书长连接不补发 ⇒ 睡着期间的消息全丢。doctor 必须查这个。"""
    mac = _read(DOCTOR_MAC)
    assert "pmset -g custom" in mac, "Mac doctor 没查睡眠设置"
    assert "disablesleep" in mac, "没查合盖是否睡（sleep=0 只管空闲不睡）"

    win = _read(DOCTOR_WIN)
    assert "powercfg" in win, "Windows doctor 没查睡眠设置"
    assert "STANDBYIDLE" in win, "没查「在此时间后睡眠」"
    assert "LIDACTION" in win, "没查合盖动作"


def test_doctor_explains_messages_are_lost_not_queued():
    """光说「你会睡着」没用 —— 必须说清后果是**消息丢了且不补发**。

    用户自己绝对想不到「飞书发出去的消息会凭空消失」，他只会以为机器人坏了，
    或者以为「它正在思考」。
    """
    for path in (DOCTOR_MAC, DOCTOR_WIN):
        text = _read(path)
        assert "不补发" in text, f"{path} 没说清消息不会补发"
        assert "正在思考" in text, (
            f"{path} 没点出「丢消息」和「正在思考」在用户眼里长得一样"
        )


def test_doctor_does_not_change_power_settings():
    """🔴 睡眠策略影响整机，必须用户自己决定 ⇒ doctor 只读，只给命令。

    这是 doctor 这个产物的立身之本（README 和脚本头都写了「只读」），
    一旦它开始 sudo 改系统设置，用户就再也不敢双击它了。
    """
    offenders = []
    for lineno, line in _code_lines(DOCTOR_MAC):
        # 允许出现在 hint 字符串里（那是给用户复制的命令），不允许真的执行
        if re.search(r"^\s*(sudo\s+)?pmset\s+(-[abcu]\s+)?\w+\s+\d", line):
            offenders.append(f"doctor-mac.sh:{lineno}: {line.strip()}")
    for lineno, line in _code_lines(DOCTOR_WIN):
        if re.search(r"^\s*&?\s*powercfg\s+/(change|setacvalueindex|setactive)", line):
            offenders.append(f"doctor-windows.ps1:{lineno}: {line.strip()}")
    assert not offenders, (
        "doctor 必须只读 —— 不能真去改睡眠设置，只能打印命令让用户自己决定：\n"
        + "\n".join(offenders)
    )


def test_doctor_sleep_hint_gives_a_copyable_command():
    """只说「会睡着」用户不知道怎么办 ⇒ 必须给能直接复制的命令。"""
    mac = _read(DOCTOR_MAC)
    assert "sudo pmset -c sleep 0" in mac
    win = _read(DOCTOR_WIN)
    assert "powercfg /change standby-timeout-ac 0" in win


# ─────────────────────── 4. doctor：静默失联 ───────────────────────


def test_doctor_checks_last_activity_age():
    """issue #169：进程还活着、不报错、日志直接断掉 ⇒ 必须看日志最后一行的时间。"""
    mac = _read(DOCTOR_MAC)
    assert "STALE_WARN_MIN" in mac, "Mac doctor 没有「多久算失联」的阈值"
    assert "上次有动静" in mac
    win = _read(DOCTOR_WIN)
    assert "staleWarnMin" in win, "Windows doctor 没有失联阈值"
    assert "上次有动静" in win


def test_stale_threshold_exceeds_watchdog_window():
    """阈值必须大于程序内置看门狗的 15 分钟。

    不然「看门狗正常工作、刚重启完」会被报成「失联」—— 自愈成功却报红，
    用户下次就不信这一项了。
    """
    mac = _read(DOCTOR_MAC)
    match = re.search(r"^STALE_WARN_MIN=(\d+)", mac, re.M)
    assert match, "没找到 STALE_WARN_MIN"
    assert int(match.group(1)) > 15, (
        f"阈值 {match.group(1)} 分钟 <= 看门狗的 15 分钟 ⇒ 自愈成功也会被报成失联"
    )
    win = _read(DOCTOR_WIN)
    match = re.search(r"^\$staleWarnMin\s*=\s*(\d+)", win, re.M)
    assert match, "没找到 $staleWarnMin"
    assert int(match.group(1)) > 15


def test_doctor_distinguishes_quiet_from_disconnected():
    """「没人发消息」和「失联」在日志上长得一样 ⇒ 必须给用户一个能分清的判据。

    不给判据的话这一项就是制造焦虑：用户看到红字但不知道该不该管。
    """
    for path in (DOCTOR_MAC, DOCTOR_WIN):
        text = _read(path)
        assert "判据" in text, f"{path} 没给「怎么分清是哪一种」的判据"
        assert "发一句话" in text, f"{path} 没给可操作的区分动作"


@pytest.mark.parametrize(
    "age_hours,expect_warn", [(0, False), (4, True)]
)
def test_mac_staleness_branch_runs(tmp_path, age_hours, expect_warn):
    """阳性+阴性对照：造一份指定年龄的日志，确认 doctor 真的分别走两条分支。

    只查文本里有没有 STALE_WARN_MIN 不够 —— 时间算错（比如时区、date 参数）
    的话判据照样是绿的，但实际永远不告警。
    """
    log = tmp_path / "agent.log"
    stamp = subprocess.run(
        ["date", "-v", f"-{age_hours}H", "+%Y-%m-%d %H:%M:%S"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    log.write_text(f"{stamp},100 INFO feishu-bridge 桥已启动。\n", encoding="utf-8")

    source = _read(DOCTOR_MAC)
    # 按脚本里的显式标记抽，不按散文措辞抽 —— 改一句提示词不该让测试红
    section = re.search(
        r"^# STALENESS-CHECK-BEGIN.*?^# STALENESS-CHECK-END", source, re.S | re.M
    )
    assert section, "没找到 STALENESS-CHECK 标记对，判据要重新想"
    colors = re.search(r"^BLUE=.*?^hint\(\).*?$", source, re.S | re.M)
    assert colors, "没找到颜色/输出函数，harness 跑不起来"
    stale = re.search(r"^STALE_WARN_MIN=(\d+)", source, re.M).group(1)

    script = f"""
set -uo pipefail
{colors.group(0)}
FAILED=0
LOG="$1"
LABEL="com.carher.feishu-agent"
STALE_WARN_MIN={stale}
{section.group(0)}
"""
    proc = subprocess.run(
        ["/bin/bash", "-c", script, "bash", str(log)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    if expect_warn:
        assert "没有任何新日志" in out, f"4 小时没动静却没告警：\n{out}"
    else:
        assert "分钟前还有动静" in out, f"刚刚才有动静却报了失联：\n{out}"
        assert "没有任何新日志" not in out


# ─────────────────────── 5. doctor：IT 策略可见性 + 卸载出口 ───────────────────────


def test_doctor_shows_it_policy_when_present():
    """IT 放了策略文件就要显示，否则用户改 config.toml 不生效却不知道为什么。"""
    mac = _read(DOCTOR_MAC)
    assert "POLICY_FILE" in mac
    assert "/Library/Application Support/CarHer/policy.toml" in mac
    assert "由 IT 固定" in mac
    win = _read(DOCTOR_WIN)
    assert "policyFile" in win
    assert "CarHer\\policy.toml" in win
    assert "由 IT 固定" in win


@pytest.fixture
def policy_home():
    """夹具建在测试目录旁边，**不用 tmp_path**。

    macOS 的 tmp_path 落在 `/private/var/folders/`，那是 pathguard 的系统豁免前缀 ⇒
    放那儿的路径类用例会假绿。这条用例既放策略文件又当 workspace，是路径相关的。
    """
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".itpolicy-e2e-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_it_policy_file_actually_constrains_mode(policy_home, monkeypatch):
    """🔴 doctor 说「由 IT 固定」，就必须真的固定得住。

    2026-11-05 之前是**说了没做**（这条当时是 `xfail(strict=True)`，
    修好之后去掉了装饰器）：`doctor-mac.sh` 读
    `/Library/Application Support/CarHer/policy.toml` 并打印
    「这台机器的权限由管理员统一管，改 config.toml 的 mode 不生效」，
    而 `Settings.load()` 只打开了 config.toml 一个文件（2026-11-05 用
    `builtins.open` 探针实测）。所以 IT 以为自己把一批机器锁在 readonly、
    doctor 也向用户确认了这一点，实际用户把 config.toml 的 mode 改成
    trusted 就全程无审批 —— 这比「没有这个功能」更糟，因为两边都收到了
    「已经锁住」的信号。

    这条测试不查文案，查**行为**：策略文件写 readonly，config 写 trusted，
    加载出来的 mode 必须是 readonly。真实位置是绝对的 `/Library/...`
    （测试不碰系统目录），`CARHER_POLICY_FILE` 是额外追加的一个来源 ——
    它只能把机器调得更紧，拿它提权不行（见 `policy.policy_files`）。
    """
    policy_dir = policy_home / "Library" / "Application Support" / "CarHer"
    policy_dir.mkdir(parents=True)
    (policy_dir / "policy.toml").write_text('mode = "readonly"\n', encoding="utf-8")
    monkeypatch.setenv("CARHER_POLICY_FILE", str(policy_dir / "policy.toml"))

    workspace = policy_home / "codes"
    workspace.mkdir()
    config = policy_home / "config.toml"
    config.write_text(
        "[feishu]\n"
        'app_id = "cli_test"\n'
        'app_secret = "x"\n'
        'owner_open_ids = ["ou_test"]\n'
        "\n"
        "[agent]\n"
        f'workspaces = ["{workspace}"]\n'
        f'default_workspace = "{workspace}"\n'
        'mode = "trusted"\n',
        encoding="utf-8",
    )

    from bridge.app import Settings

    settings = Settings.load(str(config))
    assert settings.policy.mode == "readonly", (
        "IT 策略文件写了 readonly，用户 config.toml 写 trusted ⇒ "
        f"加载出来却是 {settings.policy.mode}：doctor 那句「由 IT 固定」是假的"
    )
    # 上限也要跟着锁住，否则用户在飞书里点一下「权限」就提回 trusted 了
    assert settings.policy.ceiling_mode == "readonly"
    assert settings.policy.can_switch_mode() is False


def test_without_it_policy_the_config_mode_still_wins(policy_home, monkeypatch):
    """阳性对照：没有策略文件时 config 写 trusted 就是 trusted。

    少了这条，上面那条可能只是因为「加载出来永远是 readonly」而假绿。
    """
    monkeypatch.setenv("CARHER_POLICY_FILE", str(policy_home / "absent.toml"))

    workspace = policy_home / "codes"
    workspace.mkdir()
    config = policy_home / "config.toml"
    config.write_text(
        "[feishu]\n"
        'app_id = "cli_test"\n'
        'app_secret = "x"\n'
        'owner_open_ids = ["ou_test"]\n'
        "\n"
        "[agent]\n"
        f'workspaces = ["{workspace}"]\n'
        f'default_workspace = "{workspace}"\n'
        'mode = "trusted"\n',
        encoding="utf-8",
    )

    from bridge.app import Settings

    settings = Settings.load(str(config))
    assert settings.policy.mode == "trusted"
    assert settings.policy.ceiling_mode == "trusted"


def test_doctor_points_at_the_uninstaller():
    """用户找不到卸载入口就会来问 IT ⇒ doctor 末尾必须给出口。"""
    assert "feishu-agent-uninstall-mac.command" in _read(DOCTOR_MAC)
    assert "feishu-agent-uninstall-windows.bat" in _read(DOCTOR_WIN)


# ─────────────────────── 6. 产物一致性 ───────────────────────


def test_build_emits_uninstall_products():
    """build.sh 不带上新产物的话，改了 src/ 也到不了用户手里。"""
    build = _read(os.path.join(_ONECLICK, "build.sh"))
    assert "uninstall-mac.sh" in build
    assert "feishu-agent-uninstall-mac.command" in build
    assert "uninstall-windows.ps1" in build
    assert "feishu-agent-uninstall-windows.bat" in build
    assert "bash -n src/uninstall-mac.sh" in build, "新脚本也要进语法检查"


def test_uninstall_products_match_templates():
    """产物必须是当前 src/ 生成的 —— 忘了跑 build.sh 是最常见的「改了没上线」。"""
    pairs = [
        ("src/uninstall-mac.sh", "feishu-agent-uninstall-mac.command"),
        ("src/doctor-mac.sh", "feishu-agent-doctor-mac.command"),
    ]
    for template, product in pairs:
        tpath = os.path.join(_ONECLICK, template)
        ppath = os.path.join(_ONECLICK, product)
        if not os.path.exists(ppath):
            pytest.skip(f"{product} 还没生成（跑 build.sh）")
        # 产物只替换了模板横幅，其余正文应当逐字相同
        body = _read(tpath)
        banner = re.search(r"^# ⚠️ 这是模板：.*\n(#    .*\n)*", body, re.M)
        assert banner, f"{template} 少了模板横幅"
        expected = body.replace(banner.group(0), "")
        got = _read(ppath)
        marker = "# 由 build.sh 生成，别直接改；源在 feishu-agent-oneclick/src/\n"
        assert marker in got, f"{product} 没有「由 build.sh 生成」横幅"
        assert got.replace(marker, "") == expected, (
            f"{product} 和 {template} 不一致 —— 改完要跑 bash build.sh"
        )


def test_mac_uninstall_product_is_executable():
    """双击要能跑 ⇒ 必须有执行位。"""
    path = os.path.join(_ONECLICK, "feishu-agent-uninstall-mac.command")
    if not os.path.exists(path):
        pytest.skip("产物还没生成（跑 build.sh）")
    assert os.access(path, os.X_OK), "卸载产物没有执行位，双击跑不起来"


def test_windows_uninstall_product_byte_shape():
    """Windows 侧没法真跑 ⇒ 至少守住 build.sh 那套字节级防线。"""
    path = os.path.join(_ONECLICK, "feishu-agent-uninstall-windows.bat")
    if not os.path.exists(path):
        pytest.skip("产物还没生成（跑 build.sh）")
    blob = open(path, "rb").read()
    assert not blob.startswith(b"\xef\xbb\xbf"), "不能有 BOM"
    cut = blob.index(b"##CARHER-PS1")
    assert all(c < 128 for c in blob[:cut]), "bat 头必须纯 ASCII"
    assert b"chcp" not in blob.lower(), "bat 头不能有 chcp"
    assert blob.count(b"\n") == blob.count(b"\r\n"), "必须全是 CRLF"
    # here-string 截断：行首 '@ 或 "@ 会把 PS 正文切断，而 PS 语法检查不一定报错
    text = blob.decode("utf-8")
    assert not re.search(r"^'@", text, re.M), "行首 '@ 会截断 here-string"


def test_no_bare_var_before_fullwidth_in_lifecycle_scripts():
    """bash 3.2 会把紧跟的全角字符首字节吞进变量名 ⇒ set -u 下直接退出。

    和 scripts/lint-bash-cjk-vars.py 同一条判据。卸载脚本尤其要守：
    它报错的那句话本身把脚本炸掉的话，用户会停在一个删了一半的状态。
    """
    pattern = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")
    targets = [
        UNINSTALL_MAC,
        DOCTOR_MAC,
        os.path.join(_ONECLICK, "feishu-agent-uninstall-mac.command"),
    ]
    offenders = []
    for path in targets:
        if not os.path.exists(path):
            continue
        for lineno, line in _code_lines(path):
            for name in pattern.findall(line):
                offenders.append(f"{os.path.basename(path)}:{lineno} ${name}")
    assert not offenders, "$VAR 紧贴全角字符（bash 3.2 会炸）：\n" + "\n".join(offenders)


# ─────────────── 7. 第 39 轮：卸载入口真到用户手里 ───────────────
#
# 🔴 为什么要这一组：卸载脚本本身一直是好的（防护、擦凭据、停服务都对），
# 但它**从来没被放到用户电脑上，也没发到公网** —— 而安装器 4 处 + 自检 1 处
# 正叫用户「双击 feishu-agent-uninstall-*」。有人误装了想卸掉，手上一个入口都没有。
# 和第 15 轮自检那个坑同形（十几处让他双击桌面诊断，而桌面上什么都没有）。
# 这一组盯的就是「拿得到 + 找得到」这两环，不是脚本正文。

INSTALL_MAC = os.path.join(_ONECLICK, "src", "install-mac.sh")
INSTALL_WIN = os.path.join(_ONECLICK, "src", "install-windows.ps1")
DESKTOP_UNINSTALL_MAC = "CarHer 助手卸载.command"
DESKTOP_UNINSTALL_WIN = "CarHer 助手卸载.bat"


def test_build_embeds_the_uninstaller_into_both_installers():
    """安装器里没有卸载正文 ⇒ 用户电脑上永远不会有这个文件。"""
    build = _read(os.path.join(_ONECLICK, "build.sh"))
    for fn in ("def embed_uninstall_mac", "def embed_uninstall_windows"):
        assert fn in build, f"build.sh 少了 {fn}"
    assert "raw = embed_uninstall_mac(raw)" in build, "embed_uninstall_mac 定义了但没调用"
    assert "ps1 = embed_uninstall_windows(ps1)" in build, "embed_uninstall_windows 定义了但没调用"


def test_installers_have_exactly_one_uninstall_placeholder():
    """占位符必须正好一处：零处 ⇒ 没嵌进去；多处 ⇒ build.sh 的断言会炸。"""
    assert _read(INSTALL_MAC).count("@@EMBED_UNINSTALL_MAC@@") == 1
    assert _read(INSTALL_WIN).count("@@UNINSTALL_WINDOWS_B64@@") == 1


def test_installers_put_an_uninstall_shortcut_on_the_desktop():
    """桌面那个图标是「一键卸载」的本体 —— 误装的人不会去翻程序目录。"""
    assert DESKTOP_UNINSTALL_MAC in _read(INSTALL_MAC)
    assert DESKTOP_UNINSTALL_WIN in _read(INSTALL_WIN)


def test_windows_desktop_uninstaller_uses_the_real_desktop_folder():
    """🔴 公司电脑常把桌面重定向到 OneDrive：拼 USERPROFILE\\Desktop 的话文件会落在
    用户永远看不见的地方，而安装器还打绿说放好了。"""
    text = _read(INSTALL_WIN)
    idx = text.index(DESKTOP_UNINSTALL_WIN)
    around = text[max(0, idx - 2000):idx]
    assert "GetFolderPath('Desktop')" in around, "要用 GetFolderPath('Desktop')"


def test_uninstall_scripts_clean_up_the_desktop_shortcuts():
    """卸载完桌面还留着两个双击就报错的图标 = 没卸干净。"""
    mac = _read(UNINSTALL_MAC)
    assert DESKTOP_UNINSTALL_MAC in mac and "CarHer 助手诊断.command" in mac
    win = _read(UNINSTALL_WIN)
    assert DESKTOP_UNINSTALL_WIN in win and "CarHer 助手诊断.bat" in win


def test_publish_sends_the_uninstall_packages_to_198():
    """桥已经起不来、等不到自动更新的机器，只剩公网这一条路。"""
    pub = _read(os.path.join(_ONECLICK, "publish-update.sh"))
    assert 'UN_MAC="feishu-agent-uninstall-mac.command"' in pub
    assert 'UN_WIN="feishu-agent-uninstall-windows.bat"' in pub
    # 🔴 不能只断言「文件里有 $UN_MAC」：存在性检查和 dry-run 打印里也有同样的字符串，
    # 把真正上传的那个循环改回去照样绿（本轮实测过）。只盯 scp 那一段。
    upload = pub[pub.index("▶ 传到 198"):]
    upload = upload[:upload.index("\ndone")]
    assert "scp" in upload and "$UN_MAC" in upload and "$UN_WIN" in upload, \
        "卸载包没进真正上传的那个 scp 循环"
    assert '"$UN_MAC:$UN_MAC_SHA"' in pub, "回读核对要带上卸载包：传上去了 ≠ 下得下来"
    assert '"$UN_WIN:$UN_WIN_SHA"' in pub


def test_doctor_points_at_the_desktop_uninstaller():
    """自检是用户出问题时唯一会看的东西 ⇒ 卸载出口要指桌面那个（文件名留着兜底）。"""
    mac = _read(DOCTOR_MAC)
    assert DESKTOP_UNINSTALL_MAC in mac or "助手卸载" in mac
    win = _read(DOCTOR_WIN)
    assert DESKTOP_UNINSTALL_WIN in win or "助手卸载" in win
