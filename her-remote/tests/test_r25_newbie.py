"""第 25 轮 P-newbie：新同事第一次装 / 第一次用会撞上的几条。"""
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick", "src"))


def _read(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as fh:
        return fh.read()


def _doctor_first_screen(log_text: str) -> str:
    """抽 doctor-mac.sh 判连接的那一段，喂一份日志真跑。"""
    text = _read("doctor-mac.sh")
    start = text.index('if [ -f "$LOG" ]; then\n  ROUND_FROM=')
    end = text.index("\nfi\n", text.index("case \"$CONN_LINE\" in", start)) + 4
    with tempfile.TemporaryDirectory() as tmp:
        log = os.path.join(tmp, "agent.log")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(log_text)
        script = ('ok(){ echo "OK $*"; }; bad(){ echo "BAD $*"; }; warn(){ echo "WARN $*"; }; '
                  'hint(){ :; }\nLOG="%s"\n' % log) + text[start:end]
        return subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True).stdout


def test_doctor_does_not_call_a_disconnect_connected():
    """🔴 SDK 断线那行 `disconnected to` 里含 `connected to` ⇒ 原来断着也说「✓ 连着飞书」。"""
    log = ("2026-11-06 20:00:00,000 INFO feishu-bridge 桥已启动。\n"
           "[Lark] [2026-11-06 20:00:01,000] [INFO] connected to wss://x\n"
           "[Lark] [2026-11-06 20:35:40,870] [INFO] disconnected to wss://x\n")
    out = _doctor_first_screen(log)
    assert "BAD" in out and "OK 它现在连着" not in out, out


def test_doctor_still_says_connected_when_connected():
    log = ("2026-11-06 20:00:00,000 INFO feishu-bridge 桥已启动。\n"
           "[Lark] [2026-11-06 20:00:01,000] [INFO] connected to wss://x\n")
    assert "OK 它现在连着" in _doctor_first_screen(log)


def test_scan_path_does_not_echo_app_secret_to_screen():
    """🔴 扫码那条路原来整段 tee 到屏幕 ⇒ APP_SECRET 明文出现在安装窗口上。"""
    text = _read("install-mac.sh")
    line = next(ln for ln in text.splitlines() if "register.py" in ln and "REG_OUT=" in ln)
    nxt = text.splitlines()[text.splitlines().index(line) + 1]
    full = line + "\n" + nxt
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "register.py")
        with open(fake, "w") as fh:
            fh.write("print('APP_ID=cli_x'); print('APP_SECRET=SUPERSECRET123'); print('OWNER_OPEN_ID=ou_x')\n")
        script = f'VENV="{os.path.dirname(os.path.dirname(sys.executable))}"\nAPP_HOME="{tmp}"\n' \
                 + full.replace('"$APP_HOME/bridge/register.py"', f'"{fake}"').replace("/dev/tty", "/dev/stderr") \
                 + '\necho "GOT=$REG_OUT"\n'
        proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    assert "SUPERSECRET123" not in proc.stderr, f"屏幕上出现了 secret：{proc.stderr!r}"
    assert "APP_ID=cli_x" in proc.stderr, "非凭据的行也没显示了"
    assert "APP_SECRET=SUPERSECRET123" in proc.stdout, "安装器自己拿不到 secret 了"


def test_new_pairing_code_resets_failure_count():
    """🔴 旧码试错 5 次后重装拿新码 ⇒ 失败计数要清零，否则新码一发就作废。"""
    text = _read("install-mac.sh")
    i = text.index('PAIRING_ISSUED_AT="$(date +%s)"')
    seg = text[i:text.index('PAIRING_LINES=', i)]
    assert 'rm -f "$APP_HOME/pairing.state"' in seg
    with tempfile.TemporaryDirectory() as tmp:
        state = os.path.join(tmp, "pairing.state")
        with open(state, "w") as fh:
            fh.write('{"failed": 5}')
        subprocess.run(["/bin/bash", "-c", f'APP_HOME="{tmp}"\n' + seg], check=True)
        assert not os.path.exists(state)


def test_missing_claude_says_what_to_do(monkeypatch, tmp_path):
    from bridge.policy import Policy
    from bridge.runner import ClaudeRunner

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    policy = Policy(mode="trusted", workspaces=(str(tmp_path),), default_workspace=str(tmp_path),
                    model="claude-sonnet-5-5", turn_timeout_sec=30)
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))   # 后台 PATH 里没有 claude
    # 🔴 11-20：光清 PATH 已经造不出来了 —— `_claude_bin()` 现在会在 which 落空后
    # 去默认安装位置找（这正是修 a-user「后台 PATH 失效」那个病的办法）。
    # 本机真装着 claude，于是这条测试变成「真跑了一次 claude」（还花了钱）。
    # 要造的是「哪都没有」⇒ 把兜底也堵掉，保持这条测试原本的用意。
    import bridge.fleet.newsession as _ns
    monkeypatch.setattr(_ns, "_FALLBACKS", {k: () for k in _ns._FALLBACKS})
    monkeypatch.setattr(_ns, "_FALLBACKS_WINDOWS", {k: () for k in _ns._FALLBACKS_WINDOWS})
    runner = ClaudeRunner(policy, config_dir=str(tmp_path))
    result = asyncio.run(runner.run_turn("x", str(tmp_path)))
    assert not result.ok and "安装包" in (result.error or ""), result


@pytest.mark.parametrize("content,want", [
    ({"title": "", "content": [[{"tag": "img", "image_key": "k"}],
                               [{"tag": "text", "text": "这个报错是什么意思"}]]}, "这个报错是什么意思"),
    ({"zh_cn": {"title": "求助", "content": [[{"tag": "text", "text": "帮我看看"}]]}}, "求助 帮我看看"),   # 换行被后面的「去 @ 占位」拼成空格
    ({"text": "普通文字"}, "普通文字"),
])
def test_rich_text_message_text_is_read(content, want):
    from bridge.app import Bridge

    msg = types.SimpleNamespace(content=json.dumps(content, ensure_ascii=False))
    assert Bridge._extract_text(msg) == want


def test_installer_screen_does_not_claim_it_can_only_see_one_folder():
    for name in ("install-mac.sh", "install-windows.ps1"):
        text = _read(name)
        assert "它只能看" not in text, name
        assert "起步是「只读」" not in text, name
    assert "选一个目录" not in _read("install-mac.sh")
