"""第 33 轮 ONBOARD：机器人名带电脑名 + 一行命令安装 + 装完自动打开聊天。

🔴 不连 198、不发飞书请求：install.sh 对着本机假 http server 跑；publish/release 只跑 --dry-run，
   PATH 里的 ssh/scp/curl/lark-cli 换成一调就失败的桩。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
MAC = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN = os.path.join(_ONECLICK, "src", "install-windows.ps1")
ONELINER = os.path.join(_ONECLICK, "src", "install.sh")
PUBLISH = os.path.join(_ONECLICK, "publish-update.sh")
RELEASE = os.path.join(_ONECLICK, "release.sh")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _between(text: str, start: str, end: str) -> str:
    i = text.index(start)
    return text[i:text.index(end, i)]


def _stub_bin(tmp_path, names=("ssh", "scp", "curl", "lark-cli")) -> str:
    stub = tmp_path / "stub-bin"
    stub.mkdir(exist_ok=True)
    for name in names:
        p = stub / name
        p.write_text(f"#!/bin/sh\necho STUB-CALLED-{name} >&2\nexit 97\n")
        p.chmod(0o755)
    return str(stub)


# ── 1. 机器人名 ──────────────────────────────────────────────────────
def _bot_name(computer_name: str | None) -> str:
    seg = _between(_read(MAC), "# BOT-NAME-BEGIN", "# BOT-NAME-END")
    args = "" if computer_name is None else '"$CN"'
    script = f'set -euo pipefail\n{seg}\nbot_preset_name "{sys.executable}" {args}\n'
    env = dict(os.environ, CN=computer_name or "")
    res = subprocess.run(["/bin/bash", "-c", script], capture_output=True, env=env, timeout=30)
    assert res.returncode == 0, res.stderr.decode()
    return res.stdout.decode("utf-8").rstrip("\n")


@pytest.mark.parametrize("raw,want", [
    ("某同事的MacBook Pro", "Her remote · 某同事的MacBook Pro"),
    ("Guoxian’s MacBook Air", "Her remote · Guoxian’s MacBook Air"),   # 人名照原样保留
    ("  多  空格\t制表 ", "Her remote · 多 空格 制表"),
])
def test_bot_name_has_computer_name(raw, want):
    assert _bot_name(raw) == want


@pytest.mark.parametrize("raw", [None, "", "   ", "🍎🚀", "\x07\x1b"])
def test_bot_name_without_usable_computer_name_is_plain(raw):
    assert _bot_name(raw) == "Her remote"


def test_bot_name_strips_emoji_and_control_chars():
    # 带变体选择符的 ❤️、肤色 👍🏽、ZWJ 拼的 👨‍💻、国旗、换行
    got = _bot_name("小王❤️的👍🏽Mac👨‍💻🇨🇳\n二号")
    assert got == "Her remote · 小王的Mac 二号"
    assert not re.search(r"[\U0001F000-\U0001FAFF☀-➿️‍\x00-\x1f]", got)


def test_bot_name_long_computer_name_is_cut():
    got = _bot_name("一个特别特别特别特别特别特别特别特别长的电脑名字的MacBook Pro 16 英寸")
    tail = got[len("Her remote · "):]
    assert got.startswith("Her remote · ") and len(tail) == 24 and tail.endswith("…")
    assert len(got) <= 37


def test_bot_name_python_failure_falls_back(tmp_path):
    seg = _between(_read(MAC), "# BOT-NAME-BEGIN", "# BOT-NAME-END")
    res = subprocess.run(["/bin/bash", "-c", f'set -euo pipefail\n{seg}\nbot_preset_name /nonexistent/python "X"'],
                         capture_output=True, text=True, timeout=30)
    assert res.returncode == 0 and res.stdout.strip() == "Her remote"


def test_mac_register_gets_computer_name_not_fixed_name():
    text = _read(MAC)
    assert 'register.py" "Her remote"' not in text
    assert 'register.py" "$BOT_PRESET_NAME"' in text
    assert 'bot_preset_name "$VENV/bin/python" "$(scutil --get ComputerName' in text
    # 名字要在扫码前算好（确认页上就是这个名字）
    assert text.index("BOT_PRESET_NAME=\"$(bot_preset_name") < text.index('register.py" "$BOT_PRESET_NAME"')


def test_final_screen_prefers_real_bot_name():
    text = _read(MAC)
    assert "${BOT_NAME:-Her remote}" not in text
    assert text.count("${BOT_NAME:-$BOT_PRESET_NAME}") == 3
    # BOT_NAME（飞书回读的真名）就在 bootstrap 那里取，BOT_PRESET_NAME 只是兜底
    seg = 'BOT_NAME="foo · bar"; BOT_PRESET_NAME="Her remote · X"; echo "搜「${BOT_NAME:-$BOT_PRESET_NAME}」"; '
    seg += 'BOT_NAME=""; echo "搜「${BOT_NAME:-$BOT_PRESET_NAME}」"'
    out = subprocess.run(["/bin/bash", "-c", seg], capture_output=True, text=True).stdout
    assert out.splitlines() == ["搜「foo · bar」", "搜「Her remote · X」"]


def test_windows_register_gets_computer_name():
    text = _read(WIN)
    assert "'register.py') 'Her remote'" not in text
    assert "'register.py') $botPresetName" in text
    assert "Get-BotPresetName $env:COMPUTERNAME" in text
    assert "$botShown = if ($botName) { $botName } else { $botPresetName }" in text


# ── 2. 装完自动打开聊天 ──────────────────────────────────────────────
def _run_open(tmp_path, *, scan="1", connected="true", app_id="cli_a1b2c3d4e5f60718", open_rc=0):
    seg = _between(_read(MAC), "# BOT-OPEN-BEGIN", "# BOT-OPEN-END")
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    log = tmp_path / "open.log"
    (stub / "open").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexit {open_rc}\n')
    (stub / "open").chmod(0o755)
    script = (f'set -euo pipefail\nSCAN_CREATED="{scan}"\nCONNECTED={connected}\nAPP_ID=\'{app_id}\'\n'
              f'{seg}\necho "BOT_OPENED=$BOT_OPENED"\n')
    env = dict(os.environ, PATH=f"{stub}:/usr/bin:/bin")
    res = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
    assert res.returncode == 0, res.stderr
    opened = log.read_text().splitlines() if log.exists() else []
    return opened, res.stdout.strip().rsplit("=", 1)[-1]


def test_opens_bot_chat_after_scan_and_connect(tmp_path):
    opened, flag = _run_open(tmp_path)
    assert opened == ["https://applink.feishu.cn/client/bot/open?appId=cli_a1b2c3d4e5f60718"]
    assert flag == "1"


@pytest.mark.parametrize("kw", [
    {"connected": "false"},          # 没连上：发了也没人回
    {"scan": ""},                    # 重装 / 预烤 / 手工粘：不是刚建的，不弹
    {"app_id": "cli_x&rm -rf ~"},    # 形状不对不拼进链接
    {"app_id": ""},
])
def test_does_not_open_when_not_fresh_or_not_connected(tmp_path, kw):
    opened, flag = _run_open(tmp_path, **kw)
    assert opened == [] and flag == ""


def test_open_failure_is_silent(tmp_path):
    opened, flag = _run_open(tmp_path, open_rc=1)
    assert len(opened) == 1 and flag == ""


def test_open_block_sits_after_connect_verdict_and_before_final_screen():
    text = _read(MAC)
    i = text.index("# BOT-OPEN-BEGIN")
    assert text.index("# UPDATE-VERDICT-END") < i < text.index('ok "   装好了')
    assert text.index('SCAN_CREATED=1') < i
    # 最后一屏打开了才提一句，搜索那句照旧在
    assert text.count('[ -z "$BOT_OPENED" ] || echo') == 2


def test_windows_opens_bot_chat_only_when_fresh_and_connected():
    text = _read(WIN)
    assert "Start-Process \"https://applink.feishu.cn/client/bot/open?appId=$appId\"" in text
    assert "if ($scanCreated -and $connected -and ($appId -match '^cli_[A-Za-z0-9_]+$'))" in text
    assert text.index("$scanCreated = $true") < text.index("Start-Process \"https://applink")


# ── 3. 安装器的 read 都从 /dev/tty 读（curl | bash 时 stdin 是脚本本身）────────
def test_every_interactive_read_in_mac_installer_uses_dev_tty():
    bad = []
    for n, line in enumerate(_read(MAC).splitlines(), 1):
        code = line.split("#", 1)[0] if line.lstrip().startswith("#") else line
        if not re.search(r"(^|[;&|(\s])read\s", code):
            continue
        if re.match(r"\s*while IFS= read -r line; do$", code):   # 读 here-doc，不是读人
            continue
        if "</dev/tty" not in code:
            bad.append(f"{n}: {line.strip()}")
    assert not bad, "这些 read 在 curl | bash 下会读到脚本本身：\n" + "\n".join(bad)


# ── 4. install.sh：sha 对不上就停，对上才交给 bash ──────────────────────────
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):  # noqa: D401
        pass


@pytest.fixture
def dl_server(tmp_path):
    root = tmp_path / "www"
    root.mkdir()
    handler = lambda *a, **k: _Quiet(*a, directory=str(root), **k)  # noqa: E731
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _publish_fake(root, *, tamper=False, file="feishu-agent-mac.command", marker="INSTALLER-RAN"):
    body = f'#!/usr/bin/env bash\nBUILD_ID = "x"\necho {marker} "$@" > "$HOME/ran.txt"\n'
    body += 'if read -r line; then echo "STDIN=$line" >> "$HOME/ran.txt"; fi\n'
    (root / "feishu-agent-mac.command").write_text(body)
    sha = hashlib.sha256(body.encode()).hexdigest()
    if tamper:
        (root / "feishu-agent-mac.command").write_text(body + "# 被改过\n")
    # 形状照 publish-update.sh 写的那份（多行、windows 也有 file/sha256）
    (root / "latest.json").write_text(json.dumps({
        "build_id": "20261109-abcdef12",
        "published_at": "2026-11-09T00:00:00Z",
        "mac": {"file": file, "sha256": sha, "size": len(body)},
        "windows": {"file": "feishu-agent-windows.bat", "sha256": "0" * 64, "size": 1},
    }, indent=2))


def _run_oneliner(tmp_path, base, *, piped=True):
    home = tmp_path / "home"
    (home / "Downloads").mkdir(parents=True, exist_ok=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
           "TMPDIR": str(tmp_path), "CARHER_FEISHU_AGENT_DL_BASE": base}
    if piped:   # 真实用法：curl … | bash —— 脚本从 stdin 进来
        res = subprocess.run(["/bin/bash"], input=_read(ONELINER), capture_output=True, text=True,
                             env=env, timeout=60, start_new_session=True)
    else:
        res = subprocess.run(["/bin/bash", ONELINER], capture_output=True, text=True, env=env,
                             timeout=60, start_new_session=True, stdin=subprocess.DEVNULL)
    return res, home


def test_oneliner_syntax():
    assert subprocess.run(["bash", "-n", ONELINER]).returncode == 0


def test_oneliner_runs_installer_when_sha_matches(tmp_path, dl_server):
    root, base = dl_server
    _publish_fake(root)
    res, home = _run_oneliner(tmp_path, base)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "校验通过" in res.stdout
    ran = (home / "ran.txt").read_text()
    assert ran.startswith("INSTALLER-RAN")
    # 安装器没有偷到 install.sh 自己剩下的文本当输入
    assert "STDIN=" not in ran
    # 留了一份在「下载」里，出错提示说的「重新双击」能找到
    kept = home / "Downloads" / "feishu-agent-mac.command"
    assert kept.exists() and os.access(kept, os.X_OK)
    # 临时目录清掉了
    assert not [p for p in os.listdir(tmp_path) if p.startswith("feishu-agent-install.")]


def test_oneliner_refuses_when_sha_mismatch(tmp_path, dl_server):
    root, base = dl_server
    _publish_fake(root, tamper=True)
    res, home = _run_oneliner(tmp_path, base)
    assert res.returncode != 0
    assert not (home / "ran.txt").exists(), "校验没过却跑了安装器"
    assert "校验码对不上" in res.stderr and "什么都没装" in res.stderr
    assert not (home / "Downloads" / "feishu-agent-mac.command").exists()


@pytest.mark.parametrize("file", ["../evil.sh", "a/b.command", ".hidden", ""])
def test_oneliner_refuses_weird_file_name(tmp_path, dl_server, file):
    root, base = dl_server
    _publish_fake(root, file=file)
    res, home = _run_oneliner(tmp_path, base)
    assert res.returncode != 0 and not (home / "ran.txt").exists()
    assert "读不懂" in res.stderr


def test_oneliner_unreachable_server_says_so(tmp_path):
    res, home = _run_oneliner(tmp_path, "http://127.0.0.1:9")
    assert res.returncode != 0 and "连不上下载服务器" in res.stderr
    assert not (home / "ran.txt").exists()


def test_oneliner_missing_package_stops(tmp_path, dl_server):
    root, base = dl_server
    _publish_fake(root)
    (root / "feishu-agent-mac.command").unlink()
    res, home = _run_oneliner(tmp_path, base, piped=False)
    assert res.returncode != 0 and "没下下来" in res.stderr
    assert not (home / "ran.txt").exists()


def test_oneliner_truncated_script_does_nothing(tmp_path, dl_server):
    """curl | bash 边下边跑：网断在半截，半个脚本不许执行任何东西。"""
    root, base = dl_server
    _publish_fake(root)
    text = _read(ONELINER)
    half = text[: text.index("main \"$@\"")]
    for cut in (len(half) // 3, len(half) // 2, len(half)):
        home = tmp_path / f"home{cut}"
        home.mkdir()
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "CARHER_FEISHU_AGENT_DL_BASE": base}
        subprocess.run(["/bin/bash"], input=text[:cut], capture_output=True, text=True, env=env, timeout=30)
        assert not (home / "ran.txt").exists()


@pytest.mark.parametrize("path", [ONELINER, PUBLISH, RELEASE])
def test_no_var_glued_to_fullwidth_char(path):
    """bash 3.2 把紧贴的全角字符首字节吞进变量名 ⇒ set -u 下直接炸（本轮 publish-update.sh 第一版就踩了）。"""
    pat = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")
    bad = [ln for ln in _read(path).splitlines() if not ln.lstrip().startswith("#") and pat.search(ln)]
    assert not bad, bad


def test_oneliner_uses_only_builtin_tools():
    code = "\n".join(ln for ln in _read(ONELINER).splitlines() if not ln.lstrip().startswith("#"))
    assert "python" not in code and "jq" not in code
    assert "https://chat.auto-link.com.cn/ccdl/feishu-agent" in code


# ── 5. publish / release 的 dry-run 里有 install.sh ─────────────────────────
def test_publish_dry_run_lists_install_sh(tmp_path):
    work = tmp_path / "oneclick"
    (work / "src").mkdir(parents=True)
    shutil.copy(PUBLISH, work / "publish-update.sh")
    shutil.copy(ONELINER, work / "src" / "install.sh")
    # publish-update.sh 11-13 起也发 Windows 一行命令 src/install.ps1（拿它算 sha）
    shutil.copy(os.path.join(os.path.dirname(ONELINER), "install.ps1"), work / "src" / "install.ps1")
    (work / "feishu-agent-mac.command").write_text('#!/bin/bash\nBUILD_ID = "20261109-abcdef12"\n')
    (work / "feishu-agent-windows.bat").write_text("echo win\r\n")
    # 第 39 轮起也发两个卸载包（安装器 4 处叫用户双击它，而 198 上一直没有）
    (work / "feishu-agent-uninstall-mac.command").write_text("echo uninstall mac\n")
    (work / "feishu-agent-uninstall-windows.bat").write_text("echo uninstall win\r\n")
    # 11-21 起也发两个自检（桥装不上的机器拿不到新 doctor ⇒ 鸡生蛋，
    # 见 tests/test_r39_doctor_published.py 的文件头）
    (work / "feishu-agent-doctor-mac.command").write_text("echo doctor mac\n")
    (work / "feishu-agent-doctor-windows.bat").write_text("echo doctor win\r\n")
    env = dict(os.environ, PATH=f"{_stub_bin(tmp_path)}:/usr/bin:/bin")
    res = subprocess.run(["bash", str(work / "publish-update.sh"), "--dry-run"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "STUB-CALLED" not in res.stderr
    listed = res.stdout.split("dry-run", 1)[1]
    for name in ("feishu-agent-mac.command", "feishu-agent-windows.bat",
                 "feishu-agent-uninstall-mac.command", "feishu-agent-uninstall-windows.bat",
                 "install.sh", "latest.json"):
        assert name in listed


def test_publish_uploads_install_sh_via_part_and_verifies_it():
    text = _read(PUBLISH)
    assert '"$ONELINER_SRC" "10.0.0.2:$REMOTE_DIR/$ONELINER.part"' in text
    assert "mv $REMOTE_DIR/$ONELINER.part $REMOTE_DIR/$ONELINER" in text
    assert '"$ONELINER:$ONELINER_SHA"' in text           # 公网回读核 sha
    # latest.json 仍然最后写
    assert text.index("$ONELINER.part") < text.index("latest.json.part")


def test_release_dry_run_mentions_install_sh(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "RELEASE_SH_LIB"}
    res = subprocess.run(["bash", RELEASE, "--dry-run", "--allow-dirty"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    step6 = [ln for ln in res.stdout.splitlines() if ln.startswith("▶ 6.")]
    assert step6 and "install.sh" in step6[0]


def _release_lib(tmp_path, body: str):
    env = dict(os.environ, RELEASE_SH_LIB="1", PATH=f"{_stub_bin(tmp_path)}:{os.environ['PATH']}")
    return subprocess.run(["bash", "-c", f'source "{RELEASE}"; set +e; {body}'], capture_output=True,
                          text=True, env=env, timeout=60)


def test_release_verify_oneliner_match_and_mismatch(tmp_path):
    local = tmp_path / "install.sh"
    local.write_text("echo hi\n")
    good = hashlib.sha256(b"echo hi\n").hexdigest()
    res = _release_lib(tmp_path, f'fetch_oneliner_sha() {{ echo {good}; }}; verify_oneliner "{local}"; echo rc=$?')
    assert "rc=0" in res.stdout and "✓ 公网 install.sh" in res.stdout
    res = _release_lib(tmp_path, f'fetch_oneliner_sha() {{ echo {"0" * 64}; }}; verify_oneliner "{local}"; echo rc=$?')
    assert "rc=1" in res.stdout and "不一致" in res.stdout
    res = _release_lib(tmp_path, f'fetch_oneliner_sha() {{ :; }}; verify_oneliner "{local}"; echo rc=$?')
    assert "rc=1" in res.stdout and "下不下来" in res.stdout
    assert "STUB-CALLED" not in res.stderr


def test_release_step_verify_fails_when_only_oneliner_is_wrong(tmp_path):
    body = ('verify_three() { echo THREE-OK; return 0; }; verify_oneliner() { echo ONE-BAD; return 1; }; '
            'local_build_id() { echo 20261109-abcdef12; }; step_verify; echo rc=$?')
    res = _release_lib(tmp_path, body)
    assert "THREE-OK" in res.stdout and "ONE-BAD" in res.stdout and "rc=1" in res.stdout
