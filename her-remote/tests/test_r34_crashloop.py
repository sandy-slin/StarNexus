"""第 34 轮 CRASHLOOP：桥一启动就崩时，别让 launchd 无限狂转，并让外面知道。

盯的几条：
1. 启动守卫（scripts/launch_guard.sh）真跑：假 python 立刻 exit 1，连续启动 ⇒ 连击计数涨；
   到第 5 次 ⇒ 先睡再 exec（退避 60 起翻倍、封顶 1800）；活够 60 秒 ⇒ 清零。
2. 上报 URL：只有 v/id/os/u/app 5 个键，u=crashloop，域名写死；每段降级只报一次；
   值形状不对（id、app_id 被换成 secret）就留空，不外发。
3. 状态文件坏了不崩；sleep 时收到 TERM 立刻退出（停服务不被拖 30 分钟）。
4. 安装器：plist 先起守卫再起 python；守卫自检不过 ⇒ 退回直接起 python；渲染出的 plist 合法。
5. 词表同步：selfupdate._OUTCOME_WORDS / fleet-report FIELD_RE["u"] / _U_LABEL 都认 crashloop。

上报全部打到 PATH 里的假 curl（只记参数），不碰真 198。
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from bridge import selfupdate  # noqa: E402

_HERE = pathlib.Path(__file__).resolve().parent
GUARD_SRC = _HERE.parent / "scripts" / "launch_guard.sh"
_ONECLICK = (_HERE / ".." / ".." / ".." / "feishu-agent-oneclick").resolve()
MAC_SRC = _ONECLICK / "src" / "install-mac.sh"
FLEET_SRC = _ONECLICK / "fleet-report.py"

BUILD = "20261110-abc1234"
INSTALL_ID = "0123456789abcdef"
APP = "cli_a1b2c3d4e5f60708"

_FAKE_CURL = """#!/bin/bash
for a in "$@"; do printf '%s\\n' "$a"; done >> "$CURL_LOG"
echo "--END--" >> "$CURL_LOG"
exit "${CURL_RC:-0}"
"""
# 假 sleep：不真睡，只记秒数（退避值是这一层要证明的东西）
_FAKE_SLEEP = """#!/bin/bash
echo "$1" >> "$SLEEP_LOG"
"""
_FAKE_PY = """#!/bin/bash
echo "ran $*" >> "$PY_LOG"
exit 1
"""


@pytest.fixture
def home():
    """夹具建在测试目录旁边（同 test_r32_update_safety.py 的纪律）。"""
    base = _HERE.parent / f".r34guard-tmp-{uuid.uuid4().hex[:8]}"
    app = base / "app"
    (app / "bridge").mkdir(parents=True)
    (base / "bin").mkdir()
    (app / "bridge" / "buildinfo.py").write_text(f'BUILD_ID = "{BUILD}"\n', encoding="utf-8")
    (app / "install_id").write_text(INSTALL_ID, encoding="utf-8")
    (app / "config.toml").write_text(f'app_id = "{APP}"\napp_secret = "s3cr3t-do-not-send"\n', encoding="utf-8")
    shutil.copy(GUARD_SRC, app / "launch-guard.sh")
    for name, body in (("curl", _FAKE_CURL), ("sleep", _FAKE_SLEEP), ("fakepy", _FAKE_PY)):
        p = base / "bin" / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _env(home, **extra):
    env = {
        "PATH": f"{home}/bin:/usr/bin:/bin",
        "HOME": str(home),
        "CURL_LOG": str(home / "curl.log"),
        "SLEEP_LOG": str(home / "sleep.log"),
        "PY_LOG": str(home / "py.log"),
    }
    env.update(extra)
    return env


def _start(home, **extra) -> subprocess.CompletedProcess:
    """模拟 launchd 拉起一次：/bin/bash launch-guard.sh <python> -m bridge.app"""
    return subprocess.run(
        ["/bin/bash", str(home / "app" / "launch-guard.sh"), str(home / "bin" / "fakepy"), "-m", "bridge.app"],
        env=_env(home, **extra), capture_output=True, text=True, timeout=30,
    )


def _state(home) -> dict:
    out = {}
    for line in (home / "app" / "crashloop.state").read_text().splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def _lines(path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def _curl_calls(home) -> list[list[str]]:
    calls, cur = [], []
    for line in _lines(home / "curl.log"):
        if line == "--END--":
            calls.append(cur)
            cur = []
        else:
            cur.append(line)
    return calls


def _url_of(call: list[str]) -> str:
    urls = [a for a in call if a.startswith("http")]
    assert len(urls) == 1, call
    return urls[0]


def _age_last_exec(home, seconds: int) -> None:
    """把「上次 exec python」往前拨，模拟那一次活了这么久。"""
    st = _state(home)
    st["last_exec"] = str(int(st["last_exec"]) - seconds)
    (home / "app" / "crashloop.state").write_text("".join(f"{k}={v}\n" for k, v in st.items()))


# ---------- 1. 计数 + 退避 ----------


def test_first_start_execs_python_without_waiting(home):
    proc = _start(home)
    assert proc.returncode == 1                         # 退出码就是假 python 的（exec 了）
    assert _lines(home / "py.log") == ["ran -m bridge.app"]
    assert _state(home)["streak"] == "1"
    assert not (home / "sleep.log").exists() and not (home / "curl.log").exists()


def test_quick_exits_count_up_and_degrade_at_fifth(home):
    for _ in range(4):
        _start(home)
    assert _state(home)["streak"] == "4"
    assert not (home / "sleep.log").exists(), "前 4 次不许睡（偶发一次崩不该拖慢恢复）"
    proc = _start(home)
    assert _state(home)["streak"] == "5"
    assert _lines(home / "sleep.log") == ["60"]
    assert len(_lines(home / "py.log")) == 5, "睡完还要再试一次 python（原因消失了就自己好）"
    assert "waiting 60s" in proc.stderr


def test_backoff_doubles_and_caps(home):
    for _ in range(12):
        _start(home)
    assert _lines(home / "sleep.log") == ["60", "120", "240", "480", "960", "1800", "1800", "1800"]


def test_long_enough_run_resets_streak_and_report(home):
    for _ in range(5):
        _start(home)
    assert _state(home)["reported"] == "1"
    _age_last_exec(home, 61)                           # 上一次活过了 60 秒
    _start(home)
    st = _state(home)
    assert st["streak"] == "1" and st["reported"] == "0"
    assert _lines(home / "sleep.log") == ["60"], "活够了就不该再睡"


def test_watchdog_style_exit_after_15min_is_not_counted(home):
    """看门狗是静默 15 分钟才 exit 75 ⇒ 间隔远大于 60 秒，不许被当成崩溃循环。"""
    for _ in range(10):
        _start(home)
        _age_last_exec(home, 15 * 60)
    assert _state(home)["streak"] == "1"
    assert not (home / "sleep.log").exists() and not (home / "curl.log").exists()


def test_corrupt_state_is_treated_as_fresh(home):
    (home / "app" / "crashloop.state").write_text("streak=abc\nlast_exec=;rm -rf /\n\x00garbage\n")
    proc = _start(home)
    assert proc.returncode == 1 and _lines(home / "py.log") == ["ran -m bridge.app"]
    assert _state(home)["streak"] == "1"


def test_clock_going_backwards_resets(home):
    _start(home)
    st = _state(home)
    st["last_exec"] = str(int(time.time()) + 3600)     # 上次时间在未来（改过系统时间）
    (home / "app" / "crashloop.state").write_text("".join(f"{k}={v}\n" for k, v in st.items()))
    _start(home)
    assert _state(home)["streak"] == "1"


# ---------- 2. 上报 ----------


def test_report_url_shape_is_exactly_five_keys(home):
    for _ in range(5):
        _start(home)
    calls = _curl_calls(home)
    assert len(calls) == 1
    url = _url_of(calls[0])
    parts = urllib.parse.urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "chat.auto-link.com.cn",
                                                        "/ccdl/feishu-agent/latest.json")
    q = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    assert set(q) == set(selfupdate.REPORT_KEYS)
    assert q == {"v": [BUILD], "id": [INSTALL_ID], "os": ["mac"], "u": ["crashloop"], "app": [APP]}
    assert "s3cr3t" not in url
    assert "--proto" in calls[0] and "=https" in calls[0]


def test_report_once_per_episode(home):
    for _ in range(9):
        _start(home)
    assert len(_curl_calls(home)) == 1


def test_failed_report_is_retried_next_round(home):
    for _ in range(5):
        _start(home, CURL_RC="7")
    assert _state(home)["reported"] == "0"
    _start(home)
    assert _state(home)["reported"] == "1" and len(_curl_calls(home)) == 2


def test_report_is_parsed_by_fleet_report(home):
    """守卫打出去的 URL 原样塞进 nginx 日志行 ⇒ fleet-report 认成合规上报、u=crashloop。"""
    for _ in range(5):
        _start(home)
    url = urllib.parse.urlsplit(_url_of(_curl_calls(home)[0]))
    line = (f'1.2.3.4 - - [10/Nov/2026:10:00:00 +0800] "GET {url.path}?{url.query} HTTP/1.1" '
            '200 368 "-" "curl/8.7.1"')
    rec = _fleet().parse_line(line)
    assert rec and rec["kind"] == "report" and rec["u"] == "crashloop" and rec["id"] == INSTALL_ID


@pytest.mark.parametrize("bad_id", ["", "XYZ", "0123456789abcdef0", "../../etc/passwd", "0123456789ABCDEF"])
def test_bad_install_id_is_dropped(home, bad_id):
    (home / "app" / "install_id").write_text(bad_id)
    for _ in range(5):
        _start(home)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(_url_of(_curl_calls(home)[0])).query, keep_blank_values=True)
    assert "id" not in q and q["u"] == ["crashloop"]


@pytest.mark.parametrize("bad_app", ["s3cr3t-do-not-send", "cli_", "cli_abc&u=ok", "cli_a b", "cli_" + "a" * 41])
def test_bad_app_id_is_blanked(home, bad_app):
    (home / "app" / "config.toml").write_text(f'app_id = "{bad_app}"\n')
    for _ in range(5):
        _start(home)
    url = _url_of(_curl_calls(home)[0])
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)
    assert q["app"] == [""] and set(q) <= set(selfupdate.REPORT_KEYS)
    assert bad_app not in url or bad_app == "cli_"


def test_weird_build_id_is_sanitised(home):
    (home / "app" / "bridge" / "buildinfo.py").write_text('BUILD_ID = "2026&u=ok x/../y"\n')
    for _ in range(5):
        _start(home)
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(_url_of(_curl_calls(home)[0])).query, keep_blank_values=True)
    assert q["v"] == ["2026uokx..y"] and q["u"] == ["crashloop"]


def test_report_host_cannot_be_overridden_by_env(home):
    for _ in range(5):
        _start(home, REPORT_URL="https://evil.example/x", CARHER_REPORT_URL="https://evil.example/x")
    assert _url_of(_curl_calls(home)[0]).startswith("https://chat.auto-link.com.cn/")


# ---------- 3. 停服务 ----------


def test_term_during_backoff_exits_promptly(home):
    """bootout 发 TERM：睡着的守卫要立刻退，不许 exec python、不许拖到 ExitTimeOut 被 SIGKILL。"""
    for _ in range(4):
        _start(home)
    (home / "bin" / "sleep").unlink()                  # 这次用真 sleep（会睡 60 秒）
    proc = subprocess.Popen(
        ["/bin/bash", str(home / "app" / "launch-guard.sh"), str(home / "bin" / "fakepy"), "-m", "bridge.app"],
        env=_env(home), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    time.sleep(1.5)
    assert proc.poll() is None, "应该正在退避里睡着"
    t0 = time.time()
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    assert time.time() - t0 < 5
    assert len(_lines(home / "py.log")) == 4, "收到 TERM 后不许再起 python"


# ---------- 4. 安装器 ----------


def _mac() -> str:
    return MAC_SRC.read_text(encoding="utf-8")


def _between(text: str, a: str, b: str) -> str:
    i = text.index(a)
    return text[i:text.index(b, i)]


def _render_guard_block(text: str) -> str:
    """按 build.sh finish_sh 的规则把守卫段里的 @@EMBED 换成脚本原文。"""
    block = _between(text, "# LAUNCH-GUARD-BEGIN", "# LAUNCH-GUARD-END")
    assert block.count("@@EMBED scripts/launch_guard.sh@@") == 1
    body = GUARD_SRC.read_text(encoding="utf-8").rstrip("\n")
    assert not re.search(r"^CARHER_PY_EOF$", body, re.M)
    return block.replace("@@EMBED scripts/launch_guard.sh@@", body)


def _plist_block(text: str) -> str:
    return _between(text, 'cat > "$PLIST" <<PLISTEOF', "\nPLISTEOF") + "\nPLISTEOF\n"


def _install_and_render(tmp_path, *, break_guard: bool = False) -> tuple[dict, subprocess.CompletedProcess, pathlib.Path]:
    text = _mac()
    app = tmp_path / "app"
    app.mkdir()
    (app / "crashloop.state").write_text("streak=9\nlast_exec=1\nreported=1\n")
    block = _render_guard_block(text)
    if break_guard:
        block = block.replace("exec \"$@\"", "exec \"$@\"\nif then fi", 1)
    out = tmp_path / "x.plist"
    script = (
        "set -euo pipefail\n"
        f'PLIST="{out}"; LABEL=l; APP_HOME="{app}"; VENV="{app}/venv"; CLAUDE_BIN=/c/claude\n'
        f'CONFIG="{app}/config.toml"; LOG_DIR="{app}/logs"\n'
        "warn(){ printf '%s\\n' \"WARN $1\"; }\n"
        'write_file(){ mkdir -p "$(dirname "$1")"; cat > "$1"; }\n'
        + block + "\n" + _plist_block(text)
    )
    proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    conv = subprocess.run(["plutil", "-convert", "json", "-o", "-", str(out)], capture_output=True, text=True)
    assert conv.returncode == 0, conv.stderr
    return json.loads(conv.stdout), proc, app


@pytest.mark.skipif(shutil.which("plutil") is None, reason="要 macOS 的 plutil")
def test_plist_launches_guard_then_python(tmp_path):
    data, proc, app = _install_and_render(tmp_path)
    assert data["ProgramArguments"] == ["/bin/bash", f"{app}/launch-guard.sh",
                                        f"{app}/venv/bin/python", "-m", "bridge.app"]
    assert (app / "launch-guard.sh").read_text() == GUARD_SRC.read_text().rstrip("\n") + "\n"
    assert os.access(app / "launch-guard.sh", os.X_OK)
    assert not (app / "crashloop.state").exists(), "重装要清掉上一版的连击"
    assert data["KeepAlive"] is True and data["ExitTimeOut"] == 30


@pytest.mark.skipif(shutil.which("plutil") is None, reason="要 macOS 的 plutil")
def test_broken_guard_falls_back_to_direct_python(tmp_path):
    data, proc, app = _install_and_render(tmp_path, break_guard=True)
    assert data["ProgramArguments"] == [f"{app}/venv/bin/python", "-m", "bridge.app"]
    assert "启动守卫没通过自检" in proc.stdout


def test_rollback_clears_crash_streak():
    """自动更新新版崩 → 守卫进退避 → 安装器换回旧版：旧版不能继承新版的连击、一起来先睡几分钟。"""
    text = _mac()
    m = re.search(r"^rollback_to_prev\(\) \{.*?^\}", text, re.S | re.M)
    assert m
    body = m.group(0)
    assert 'rm -f -- "${APP_HOME:?}/crashloop.state"' in body
    assert body.index("crashloop.state") < body.index("_svc_start_for_rollback")


def test_guard_lives_outside_bridge_dir():
    """自动更新回滚只换 bridge/ ⇒ 守卫必须在 bridge/ 外面，否则旧版换回来守卫就没了。"""
    block = _between(_mac(), "# LAUNCH-GUARD-BEGIN", "# LAUNCH-GUARD-END")
    assert 'GUARD="$APP_HOME/launch-guard.sh"' in block


def test_guard_block_sits_right_before_plist():
    text = _mac()
    assert text.index("# LAUNCH-GUARD-END") < text.index('cat > "$PLIST" <<PLISTEOF')
    assert text.index("# LAUNCH-GUARD-BEGIN") > text.index("# UPDATE-ROLLBACK-END")


def test_guard_script_is_bash32_syntax_clean():
    proc = subprocess.run(["/bin/bash", "-n", str(GUARD_SRC)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    src = GUARD_SRC.read_text()
    # bash 3.2（macOS 自带）没有这些
    for bad in ("declare -A", "mapfile", "readarray", "${var,,}", "&>>"):
        assert bad not in src
    assert "python" not in re.sub(r"#.*", "", src).replace('"$@"', ""), "守卫不许依赖 Python"


# ---------- 5. 词表同步 ----------


def _fleet():
    spec = importlib.util.spec_from_file_location("fleet_report_r34", FLEET_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_outcome_words_in_sync_with_fleet_report():
    fr = _fleet()
    for word in selfupdate._OUTCOME_WORDS:
        assert fr.FIELD_RE["u"].match(word), word
        assert word in fr._U_LABEL, word
    assert "crashloop" in selfupdate._OUTCOME_WORDS
    assert fr._U_LABEL["crashloop"] == "反复崩溃"
    assert not fr.FIELD_RE["u"].match("crashloop:ImportError")


def test_bridge_reports_crashloop_word_if_ever_recorded(tmp_path):
    """桥自己不写 crashloop；万一记录里有，它在词表内 ⇒ 原样上报，不会被吞成空。"""
    pkg = tmp_path / "bridge"
    pkg.mkdir()
    (pkg / "buildinfo.py").write_text(f'BUILD_ID = "{BUILD}"\n')
    up = selfupdate.SelfUpdater(str(pkg), str(tmp_path / "update_state.json"), is_idle=lambda: True)
    up._remember(BUILD, "crashloop")
    assert up.report_params(BUILD)["u"] == "crashloop"
