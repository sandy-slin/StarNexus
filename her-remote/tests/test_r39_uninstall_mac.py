"""第 39 轮（macOS 侧）：卸载脚本从来没被放到用户电脑上。

和第 15 轮自检那个坑同构（用户原话：「你让他在飞书里发诊断，他飞书链接上，怎么诊断」）：
- `src/install-mac.sh` 4 处 + `src/doctor-mac.sh` 1 处叫人「双击 feishu-agent-uninstall-mac.command」
- 而这个文件**既不在他电脑上，也不在 198 下载目录里** ⇒ 误装了想卸掉的人没有任何入口。

这一轮：安装器把卸载脚本一起装到 `$APP_HOME`，桌面放一个一行转发器
「CarHer 助手卸载.command」，4 处文案改用 `UNINSTALL_HINT`（桌面真放上了才叫他去桌面找）。
卸载时把桌面那两个快捷方式一起删掉（走 safe_rm 白名单，不绕过防护）。

量法：**安装器那一段和卸载脚本都抽出来在沙箱里真跑**（HOME 指向 tmp，launchctl 打桩）。
🔴 真跑前后对真家目录里几个文件的 sha（11-17 有过只隔离 CLAUDE_CONFIG_DIR 结果写进真 HOME 的事故）。
红对照：`CARHER_R39_INSTALL_MAC` / `CARHER_R39_UNINSTALL_MAC` / `CARHER_R39_DOCTOR_MAC` 指向修前那份。
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SRC = _REPO / "feishu-agent-oneclick" / "src"
INSTALL = Path(os.environ.get("CARHER_R39_INSTALL_MAC") or _SRC / "install-mac.sh")
UNINSTALL = Path(os.environ.get("CARHER_R39_UNINSTALL_MAC") or _SRC / "uninstall-mac.sh")
DOCTOR = Path(os.environ.get("CARHER_R39_DOCTOR_MAC") or _SRC / "doctor-mac.sh")

PLACEHOLDER = "@@EMBED_UNINSTALL_MAC@@"
EOF_MARK = "CARHER_UNINSTALL_EOF"
DESK_NAME = "CarHer 助手卸载.command"
# 安装器里新加那一段的起止（END 是下一节的标题，不含进去）
BLOCK_START = "# ── 卸载脚本一起装上"
BLOCK_END = "# ── 4. 凭据"
# 🔴 lint-bash-cjk-vars.py 的判据：`$VAR` 紧贴全角字符，bash 3.2 会把首字节吞进变量名
CJK_VAR = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ═══════════════ 真 HOME 守卫 ═══════════════
# 11-17 事故：测试只隔离了 CLAUDE_CONFIG_DIR，App 配置那条腿照样写进真家目录。
# 这里凡是真跑 bash 的用例都挂 _real_home_untouched：跑前跑后对这几个文件的 sha。
_WATCHED = (
    "Desktop/CarHer 助手卸载.command",
    "Desktop/CarHer 助手诊断.command",
    ".carher-feishu-agent/config.toml",
    ".carher-feishu-agent/feishu-agent-uninstall-mac.command",
    "Library/LaunchAgents/com.carher.feishu-agent.plist",
    ".feishu-agent-bridge.lock",
)


def _snapshot_real_home() -> dict:
    real = Path(os.path.expanduser("~"))
    out = {}
    for rel in _WATCHED:
        p = real / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            out[rel] = "ABSENT" if not p.exists() else "NONFILE"
    return out


@pytest.fixture()
def real_home_untouched():
    before = _snapshot_real_home()
    yield
    after = _snapshot_real_home()
    diff = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert not diff, f"🔴 测试写进了真家目录：{diff}"


def _sandbox(tmp_path: Path, *, desktop: bool = True) -> dict:
    """一个假 HOME。desktop=False 模拟「没有 Desktop 目录」。"""
    home = tmp_path / "home"
    home.mkdir()
    if desktop:
        (home / "Desktop").mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "TMPDIR")}
    env["HOME"] = str(home)
    return env


def _bin_stubs(tmp_path: Path) -> Path:
    """launchctl / ps 打桩：卸载脚本会调它们，沙箱里不能碰真 launchd。"""
    bin_dir = tmp_path / "stubbin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "launchctl").write_text("#!/bin/bash\nexit 1\n")  # 「服务没注册」
    (bin_dir / "ps").write_text("#!/bin/bash\nexit 0\n")  # 没有残留进程
    for name in ("launchctl", "ps"):
        p = bin_dir / name
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return bin_dir


# ═══════════════ ① 安装器把卸载脚本带上（源码断言）═══════════════


def test_installer_embeds_the_uninstaller_exactly_once():
    text = _read(INSTALL)
    n = text.count(PLACEHOLDER)
    assert n == 1, f"安装器里卸载占位符出现 {n} 次（要正好 1 次）—— 他手上还是没有这个文件"
    assert f"<<'{EOF_MARK}'" in text, "heredoc 要用独立结束符，不能和 CARHER_PY_EOF 撞"
    assert re.search(rf"^{EOF_MARK}$", text, re.M), "heredoc 没有闭合"


def test_uninstaller_body_has_no_line_equal_to_the_heredoc_terminator():
    """🔴 卸载脚本正文里只要有一行正好等于结束符，嵌进去就会把安装器截断。"""
    assert not re.search(rf"^{EOF_MARK}$", _read(UNINSTALL), re.M)
    # 它内部那个 heredoc 用的是 EOF，不冲突
    assert "done <<EOF" in _read(UNINSTALL)


def test_installer_lands_the_uninstaller_in_the_app_home():
    text = _read(INSTALL)
    assert 'UNINSTALL="$APP_HOME/feishu-agent-uninstall-mac.command"' in text
    assert 'chmod 755 "$UNINSTALL"' in text, "不给执行位的话双击跑不起来"


def test_desktop_entry_is_a_forwarder_not_a_copy():
    """桌面那个必须是一行 exec 转发：拷贝的话升级后桌面那份就是旧的。"""
    text = _read(INSTALL)
    assert re.search(r'DESKTOP_UNINSTALL="\$HOME/Desktop/[^"]*卸载[^"]*\.command"', text), \
        "桌面上没有能双击的卸载"
    block = text[text.index(BLOCK_START):text.index(BLOCK_END)]
    assert 'exec /bin/bash' in block and '"$UNINSTALL"' in block, "桌面那个不是转发器"
    assert 'cp ' not in block, "桌面那个不能是拷贝（升级跟不上）"


def test_redirect_silencer_comes_before_the_redirect():
    """🔴 `2>/dev/null` 必须在 `>` **前面**：桌面不让写时报错的是 `>` 这一步。"""
    block = _read(INSTALL)[_read(INSTALL).index(BLOCK_START):_read(INSTALL).index(BLOCK_END)]
    line = next(l for l in block.splitlines() if '> "$DESKTOP_UNINSTALL"' in l)
    assert line.index("2>/dev/null") < line.index('> "$DESKTOP_UNINSTALL"'), \
        f"写后面挡不住，屏幕上会多一行英文 Permission denied：{line.strip()}"


def test_no_bare_dollar_var_next_to_cjk_in_the_new_block():
    """🔴 bash 3.2：`$VAR` 紧跟全角字符会把首字节吞进变量名，set -u 下直接退出。"""
    text = _read(INSTALL)
    offenders = []
    for i, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in CJK_VAR.finditer(line):
            offenders.append(f"install-mac.sh:{i}  ${m.group(1)}  {line.strip()[:90]}")
    assert not offenders, "要写 ${VAR}：\n" + "\n".join(offenders)


# ═══════════════ ② 4 处文案改用 UNINSTALL_HINT ═══════════════


def test_no_copy_still_tells_him_to_double_click_a_file_he_does_not_have():
    """修前 4 处写死「双击 feishu-agent-uninstall-mac.command」。"""
    text = _read(INSTALL)
    bad = [f"install-mac.sh:{i}  {l.strip()[:100]}"
           for i, l in enumerate(text.splitlines(), 1)
           if not l.lstrip().startswith("#") and "双击 feishu-agent-uninstall-mac.command" in l]
    assert not bad, "还在叫他双击一个可能不在他手上的文件名：\n" + "\n".join(bad)


def test_all_four_uninstall_mentions_go_through_the_hint():
    """4 处出口都要用 ${UNINSTALL_HINT}（含 NOTESEOF 那个 cat 块里的普通字符串那条）。"""
    text = _read(INSTALL)
    hits = [i for i, l in enumerate(text.splitlines(), 1)
            if "${UNINSTALL_HINT}" in l and not l.lstrip().startswith("#")]
    # 1 处是定义/赋值 + 1 处是桌面没放上的 warn，其余是出口文案
    assert len(hits) >= 5, f"${{UNINSTALL_HINT}} 只用了 {len(hits)} 处，4 个出口没改全：{hits}"
    assert "想重来一次：先卸掉再双击安装器 —— ${UNINSTALL_HINT}" in text
    assert "要彻底卸掉：${UNINSTALL_HINT}" in text, "NOTESEOF 那块（普通字符串）没改"


def test_hint_default_is_a_pasteable_terminal_command():
    """桌面没放上时给的必须是能在终端里直接粘的一行，且命令后跟空格。"""
    text = _read(INSTALL)
    line = next(l for l in text.splitlines() if l.startswith("UNINSTALL_HINT="))
    assert "bash ~/.carher-feishu-agent/feishu-agent-uninstall-mac.command " in line, \
        f"默认兜底不是可粘的命令（或命令后没跟空格）：{line}"
    assert "双击桌面" not in line, "默认值不能先说桌面 —— 那时还不知道放上没"


def test_hint_switches_to_desktop_only_after_a_successful_write():
    """🔴 改成「双击桌面上的」只能发生在 if 的成功分支里。"""
    text = _read(INSTALL)
    block = text[text.index(BLOCK_START):text.index(BLOCK_END)]
    lines = block.splitlines()
    i_if = next(i for i, l in enumerate(lines) if '> "$DESKTOP_UNINSTALL"' in l)
    i_set = next(i for i, l in enumerate(lines) if 'UNINSTALL_HINT="双击桌面' in l)
    i_else = next(i for i, l in enumerate(lines) if l.strip() == "else")
    assert i_if < i_set < i_else, "「双击桌面上的」不在 if 成功分支里"


# ═══════════════ ③ 真跑：安装器那一段（沙箱 HOME）═══════════════


def _install_block() -> str:
    text = _read(INSTALL)
    return text[text.index(BLOCK_START):text.index(BLOCK_END)]


def _run_install_block(tmp_path: Path, *, desktop: bool, desktop_writable: bool = True):
    env = _sandbox(tmp_path, desktop=desktop)
    home = Path(env["HOME"])
    app = home / ".carher-feishu-agent"
    app.mkdir(parents=True)
    if desktop and not desktop_writable:
        (home / "Desktop").chmod(0o500)  # 只读 ⇒ `>` 这一步报错
    # 占位符在真安装器里由 build.sh 替换；这里换成一段可运行的桩正文
    body = _install_block().replace(PLACEHOLDER, '#!/usr/bin/env bash\necho FAKE_UNINSTALL_BODY')
    pre = (
        "set -euo pipefail\n"
        f'APP_HOME="{app}"\n'
        "write_file() { mkdir -p \"$(dirname \"$1\")\"; cat > \"$1\"; }\n"
        "ok(){ printf 'OK %s\\n' \"$1\"; }\nwarn(){ printf 'WARN %s\\n' \"$1\"; }\n"
        "say(){ printf '%s\\n' \"$1\"; }\n"
    )
    post = '\nprintf "HINT=%s\\n" "${UNINSTALL_HINT}"\n'
    proc = subprocess.run(["/bin/bash", "-c", pre + body + post],
                          capture_output=True, text=True, timeout=60, env=env)
    if desktop and not desktop_writable:
        (home / "Desktop").chmod(0o700)  # 还原，否则 tmp 清不掉
    return proc, home, app


def test_real_run_lands_both_copies_and_points_at_the_desktop(tmp_path, real_home_untouched):
    proc, home, app = _run_install_block(tmp_path, desktop=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    prog = app / "feishu-agent-uninstall-mac.command"
    assert prog.is_file(), f"程序目录里没有卸载脚本：{proc.stdout}{proc.stderr}"
    assert os.access(prog, os.X_OK), "程序目录那份没有执行位"
    desk = home / "Desktop" / DESK_NAME
    assert desk.is_file(), f"桌面上没放：{proc.stdout}{proc.stderr}"
    assert os.access(desk, os.X_OK), "桌面那份没有执行位，双击跑不起来"
    # 转发器只有一行 exec，指向程序目录那份
    assert f'exec /bin/bash "{prog}"' in desk.read_text(encoding="utf-8")
    assert "FAKE_UNINSTALL_BODY" not in desk.read_text(encoding="utf-8"), "桌面那份是拷贝不是转发"
    assert "HINT=双击桌面上的「CarHer 助手卸载」" in proc.stdout, proc.stdout


def test_real_run_forwarder_actually_executes_the_program_copy(tmp_path, real_home_untouched):
    """桌面那一行真跑得起来，跑到的是程序目录那份。"""
    proc, home, app = _run_install_block(tmp_path, desktop=True)
    assert proc.returncode == 0
    desk = home / "Desktop" / DESK_NAME
    r = subprocess.run(["/bin/bash", str(desk)], capture_output=True, text=True, timeout=60,
                       env=_sandbox_reuse(home))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAKE_UNINSTALL_BODY" in r.stdout, r.stdout + r.stderr


def _sandbox_reuse(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "HOME"}
    env["HOME"] = str(home)
    return env


def test_real_run_without_a_desktop_dir_still_succeeds(tmp_path, real_home_untouched):
    """🔴 没有 Desktop 目录时安装绝不能失败，且提示要给终端命令。"""
    proc, home, app = _run_install_block(tmp_path, desktop=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (app / "feishu-agent-uninstall-mac.command").is_file()
    assert "HINT=在「终端」里粘 bash ~/.carher-feishu-agent/feishu-agent-uninstall-mac.command 回车" \
        in proc.stdout, proc.stdout


def test_real_run_with_unwritable_desktop_warns_and_prints_no_english_error(
        tmp_path, real_home_untouched):
    """🔴 桌面只读：安装仍成功、打黄、给终端命令，且屏幕上不许出现英文 Permission denied。"""
    if os.geteuid() == 0:
        pytest.skip("root 下 0o500 的目录照样能写")
    proc, home, app = _run_install_block(tmp_path, desktop=True, desktop_writable=False)
    assert proc.returncode == 0, "桌面写不了把安装弄失败了：" + proc.stdout + proc.stderr
    assert (app / "feishu-agent-uninstall-mac.command").is_file()
    assert "WARN" in proc.stdout and "桌面上的卸载没放上" in proc.stdout, proc.stdout
    both = proc.stdout + proc.stderr
    assert "Permission denied" not in both, f"屏幕上多了一行英文：{both}"
    assert "HINT=在「终端」里粘 bash" in proc.stdout, proc.stdout


# ═══════════════ ④ 卸载把桌面那两个一起删掉（走 safe_rm）═══════════════


def test_shortcuts_are_in_the_safe_rm_whitelist_not_bypassing_it():
    text = _read(UNINSTALL)
    wl = next(l for l in text.splitlines() if l.strip().startswith("for item in \"$APP_HOME\""))
    assert "DESKTOP_DOCTOR" in wl and "DESKTOP_UNINSTALL" in wl, \
        f"新路径没进 safe_rm 白名单：{wl}"
    # 🔴 这两个写 `${VAR:-}`：safe_rm 被单独抽出来跑时（lifecycle 那个阳性对照就是这么干的）
    # 它们没定义，裸 $VAR 会让整函数在 set -u 下炸掉 —— 从「拒绝删」变成「卸载中断」。
    assert "${DESKTOP_DOCTOR:-}" in wl and "${DESKTOP_UNINSTALL:-}" in wl, \
        f"白名单变量没写成 ${{VAR:-}}，safe_rm 单独跑时会 unbound variable：{wl}"
    # 删除动作只许走 remove/safe_rm，不许自己 rm
    bad = [f"{i}: {l.strip()}" for i, l in enumerate(text.splitlines(), 1)
           if not l.lstrip().startswith("#") and re.search(r"\brm -rf?\b", l)
           and "safe_rm" not in l and 'rm -rf -- "$target"' not in l]
    assert not bad, f"绕过了 safe_rm 自己 rm：{bad}"
    for var in ("DESKTOP_DOCTOR", "DESKTOP_UNINSTALL"):
        assert f'remove "${var}"' in text, f"{var} 没被删"


def test_desktop_uninstall_is_removed_last_and_says_why_that_is_safe():
    """🔴 桌面那个卸载就是当前这条路的入口 ⇒ 必须最后删，而且注释要写明为什么安全。"""
    text = _read(UNINSTALL)
    lines = text.splitlines()
    i_self = next(i for i, l in enumerate(lines) if 'remove "$DESKTOP_UNINSTALL"' in l)
    others = [i for i, l in enumerate(lines)
              if re.match(r'^\s*(\[ -n [^]]*\] && )?remove "\$', l) and i != i_self]
    assert others and max(others) < i_self, "桌面那个卸载不是最后删的"
    note = "\n".join(lines[max(0, i_self - 6):i_self])
    assert "exec" in note, f"没写清为什么现在删它是安全的：{note}"


def test_shortcuts_count_as_installed_so_orphans_get_cleaned():
    """程序目录被手删过、只剩桌面两个时，不能直接说「本机没装」就退。"""
    text = _read(UNINSTALL)
    i = text.index('if [ ! -d "$APP_HOME" ]')
    guard = text[i:text.index("本机没装", i)]
    assert "DESKTOP_DOCTOR" in guard and "DESKTOP_UNINSTALL" in guard, \
        "早退判据没算上桌面那两个 ⇒ 孤儿快捷方式会一直留在他桌面"


def test_empty_home_cannot_make_the_shortcut_paths_point_elsewhere():
    """🔴 HOME 为空时 $HOME/Desktop/... 会变成 /Desktop/...（别人的路径）⇒ 置空让白名单跳过。"""
    text = _read(UNINSTALL)
    assert 'if [ -n "${HOME:-}" ]; then' in text
    assert re.search(r'^DESKTOP_DOCTOR=""$', text, re.M)
    assert re.search(r'^DESKTOP_UNINSTALL=""$', text, re.M)


def _run_uninstall(tmp_path: Path, *, make_app: bool = True, shortcuts: bool = True):
    env = _sandbox(tmp_path)
    home = Path(env["HOME"])
    app = home / ".carher-feishu-agent"
    if make_app:
        app.mkdir(parents=True)
        (app / "config.toml").write_text('app_id = "cli_x"\napp_secret = "SECRET-NEEDLE"\n')
        (home / ".claude-feishu-bridge").mkdir()
        (home / ".feishu-agent-bridge.lock").write_text("")
        (home / "Library" / "LaunchAgents").mkdir(parents=True)
        (home / "Library" / "LaunchAgents" / "com.carher.feishu-agent.plist").write_text("<plist/>")
    desk = home / "Desktop"
    if shortcuts:
        for name in ("CarHer 助手诊断.command", DESK_NAME):
            p = desk / name
            p.write_text(f'#!/bin/bash\nexec /bin/bash "{app}/x.command"\n')
            p.chmod(0o755)
    env["PATH"] = f"{_bin_stubs(tmp_path)}:{env.get('PATH', '')}"
    proc = subprocess.run(["/bin/bash", str(UNINSTALL), "--yes"],
                          capture_output=True, text=True, timeout=120, env=env)
    return proc, home, app, desk


def test_real_uninstall_removes_both_desktop_shortcuts(tmp_path, real_home_untouched):
    proc, home, app, desk = _run_uninstall(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not app.exists(), proc.stdout
    left = [p.name for p in desk.iterdir()]
    assert not left, f"卸完桌面还留着双击就报错的东西：{left}\n{proc.stdout}"
    assert "不在卸载白名单里" not in proc.stdout, "safe_rm 把新路径挡了：" + proc.stdout
    assert "内部错误" not in proc.stdout, proc.stdout
    assert "本机已清干净" in proc.stdout, proc.stdout


def test_real_uninstall_lists_the_shortcuts_before_deleting(tmp_path, real_home_untouched):
    proc, home, app, desk = _run_uninstall(tmp_path)
    assert "CarHer 助手诊断.command" in proc.stdout and DESK_NAME in proc.stdout, proc.stdout


def test_real_uninstall_cleans_orphan_shortcuts_with_no_app_home(tmp_path, real_home_untouched):
    """只剩桌面两个（程序目录被手删过）时也要清掉，不能报「本机没装」就退。"""
    proc, home, app, desk = _run_uninstall(tmp_path, make_app=False, shortcuts=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "本机没装" not in proc.stdout, proc.stdout
    left = [p.name for p in desk.iterdir()]
    assert not left, f"孤儿快捷方式没清掉：{left}\n{proc.stdout}"


def test_real_uninstall_still_says_nothing_installed_when_truly_clean(tmp_path,
                                                                     real_home_untouched):
    proc, home, app, desk = _run_uninstall(tmp_path, make_app=False, shortcuts=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "本机没装" in proc.stdout, proc.stdout


def test_real_uninstall_still_shreds_the_secret(tmp_path, real_home_untouched):
    """回归：加了桌面那两条不能把原来的凭据擦除顺序弄坏。"""
    proc, home, app, desk = _run_uninstall(tmp_path)
    assert "config.toml 已覆写" in proc.stdout, proc.stdout
    assert "SECRET-NEEDLE" not in proc.stdout


def test_safe_rm_still_refuses_paths_outside_the_whitelist(tmp_path, real_home_untouched):
    """阳性对照：白名单放宽了，但别的路径照样拒。"""
    env = _sandbox(tmp_path)
    home = Path(env["HOME"])
    victim = home / "Documents" / "important.txt"
    victim.parent.mkdir(parents=True)
    victim.write_text("keep me")
    body = _read(UNINSTALL)
    head = body[:body.index("# 凭据安全擦除")]
    script = head + (
        f'\nsafe_rm "{victim}" && echo DELETED || echo REFUSED\n'
        'safe_rm "" && echo DELETED || echo REFUSED\n'
        f'safe_rm "{home}" && echo DELETED || echo REFUSED\n'
        f'safe_rm "{home}/Desktop" && echo DELETED || echo REFUSED\n'
    )
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                       timeout=60, env=env)
    assert r.stdout.count("REFUSED") == 4, r.stdout + r.stderr
    assert victim.exists() and victim.read_text() == "keep me"


def test_safe_rm_accepts_exactly_the_two_shortcut_paths(tmp_path, real_home_untouched):
    env = _sandbox(tmp_path)
    home = Path(env["HOME"])
    for name in ("CarHer 助手诊断.command", DESK_NAME):
        (home / "Desktop" / name).write_text("x")
    body = _read(UNINSTALL)
    head = body[:body.index("# 凭据安全擦除")]
    script = head + (
        '\nsafe_rm "$DESKTOP_DOCTOR" && echo OK1 || echo NO1\n'
        'safe_rm "$DESKTOP_UNINSTALL" && echo OK2 || echo NO2\n'
    )
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                       timeout=60, env=env)
    assert "OK1" in r.stdout and "OK2" in r.stdout, r.stdout + r.stderr
    assert not list((home / "Desktop").iterdir())


def test_safe_rm_survives_undefined_whitelist_vars_under_set_u(tmp_path, real_home_untouched):
    """🔴 safe_rm 只抽函数体出来跑（白名单变量没定义）时不能炸 —— 否则防护从「拒绝删」
    变成「整个卸载中断」。lifecycle 里那个阳性对照正是这么抽的。"""
    env = _sandbox(tmp_path)
    home = Path(env["HOME"])
    (home / ".carher-feishu-agent" / "logs").mkdir(parents=True)
    (home / ".carher-feishu-agent" / "logs" / "a.log").write_text("x")
    src = _read(UNINSTALL)
    guard = re.search(r"^safe_rm\(\) \{.*?^\}", src, re.S | re.M)
    assert guard, "没找到 safe_rm 函数体"
    colors = re.search(r"^BLUE=.*?^warn\(\).*?$", src, re.S | re.M)
    script = (
        "set -uo pipefail\n" + colors.group(0) + "\n"
        'APP_HOME="$HOME/.carher-feishu-agent"\nBRIDGE_CC="$HOME/.claude-feishu-bridge"\n'
        'LOCK="$HOME/.feishu-agent-bridge.lock"\n'
        'PLIST="$HOME/Library/LaunchAgents/com.carher.feishu-agent.plist"\n'
        + guard.group(0) + "\n"
        'safe_rm "$APP_HOME/logs/a.log" && echo ALLOW || echo DENY\n'
        'safe_rm "$HOME/Documents" && echo ALLOW || echo DENY\n'
    )
    r = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                       timeout=60, env=env)
    assert "unbound variable" not in r.stderr, r.stderr
    # 两个判决各出现一次，顺序是先 ALLOW 再 DENY（中间夹着 safe_rm 自己打的那行红字）
    verdicts = [w for w in r.stdout.split() if w in ("ALLOW", "DENY")]
    assert verdicts == ["ALLOW", "DENY"], r.stdout + r.stderr


def test_uninstaller_has_no_bare_dollar_var_next_to_cjk():
    offenders = []
    for i, line in enumerate(_read(UNINSTALL).splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for m in CJK_VAR.finditer(line):
            offenders.append(f"uninstall-mac.sh:{i}  ${m.group(1)}")
    assert not offenders, "要写 ${VAR}：" + str(offenders)


# ═══════════════ ⑤ doctor 指桌面，但留着兜底文件名 ═══════════════


def test_doctor_points_at_the_desktop_shortcut():
    text = _read(DOCTOR)
    assert "双击桌面上的「CarHer 助手卸载」" in text, "doctor 还在指一个他可能没有的文件名"


def test_doctor_keeps_the_filename_as_a_fallback():
    """🔴 桌面那个没放上时要有兜底；test_oneclick_lifecycle 也断言这个字符串在 doctor 里。"""
    assert "feishu-agent-uninstall-mac.command" in _read(DOCTOR)


# ═══════════════ ⑥ 语法 ═══════════════


@pytest.mark.parametrize("path", [INSTALL, UNINSTALL, DOCTOR],
                         ids=["install", "uninstall", "doctor"])
def test_bash_syntax_ok(path):
    r = subprocess.run(["/bin/bash", "-n", str(path)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
