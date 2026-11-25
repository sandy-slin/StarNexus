"""doctor 第 [6] 项按**配对码**判（2026-11-06 第 4 轮，新同事侦察 Top4）。

原来是「首条消息自动绑定」时代的判据（autobind 早删了），没绑定时一律报红、叫人「翻日志抄
open_id 填进 config.toml」。而安装器自己就叫新同事「等两分钟双击 doctor」⇒ 每个新同事都会撞上。

Mac 段真跑（抠出 [6] 那段，喂假配置）；Windows 没有 pwsh ⇒ 静态核对。
"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick", "src"))
MAC = os.path.join(_SRC, "doctor-mac.sh")
WIN = os.path.join(_SRC, "doctor-windows.ps1")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def here_dir():
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".doctor-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _run_owner_check(here_dir, config_body: str) -> str:
    text = _read(MAC)
    start = text.index('say "[6] 授权用户"')
    end = text.index("# ── 7.", start)
    config = here_dir / "config.toml"
    config.write_text(config_body, encoding="utf-8")
    script = (
        "set -uo pipefail\n"
        "say(){ :; }\n"
        'ok(){ printf "OK %s\\n" "$1"; }\n'
        'warn(){ printf "WARN %s\\n" "$1"; }\n'
        'bad(){ printf "BAD %s\\n" "$1"; }\n'
        'hint(){ printf "HINT %s\\n" "$1"; }\n'
        f'CONFIG="{config}"\n' + text[start:end]
    )
    out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


def test_bound_owner_is_green(here_dir):
    out = _run_owner_check(here_dir, 'owner_open_ids = ["ou_abc"]\n')
    assert out.startswith("OK"), out


def test_fresh_install_with_live_code_is_yellow_and_says_send_the_code(here_dir):
    """新同事装完来跑 doctor 的典型状态：码刚发、还没在飞书里发。⇒ 黄，不是红。"""
    out = _run_owner_check(
        here_dir,
        f'owner_open_ids = []\npairing_code = "ABC123"\npairing_issued_at = {int(time.time())}\npairing_ttl_min = 30\n',
    )
    assert out.startswith("WARN"), out
    assert "6 位码" in out and "分钟有效" in out
    assert "open_id" not in out and "config.toml" not in out, "又叫新同事去手改配置了"


def test_expired_code_says_reinstall_for_a_new_one(here_dir):
    out = _run_owner_check(
        here_dir,
        'owner_open_ids = []\npairing_code = "ABC123"\npairing_issued_at = 1000\npairing_ttl_min = 30\n',
    )
    assert out.startswith("BAD") and "过期" in out and "重新双击" in out, out


def test_no_owner_no_code_points_to_reinstall_not_config_editing(here_dir):
    out = _run_owner_check(here_dir, "owner_open_ids = []\n")
    assert out.startswith("BAD")
    assert "open_id" not in out and "config.toml" not in out


def test_autobind_is_no_longer_consulted():
    """autobind 那条路删了（谁抢先发谁当主人）⇒ doctor 不许再提它，不然会教人去开它。"""
    for path in (MAC, WIN):
        block = _read(path)
        block = block[block.index("[6]"):block.index("[7]")]
        assert "autobind" not in block, f"{os.path.basename(path)} 还在看 autobind"


def test_windows_reads_the_pairing_code():
    """静态核对（没有 pwsh）：Windows 那段要读 pairing_code / pairing_issued_at。"""
    block = _read(WIN)
    block = block[block.index("[6]"):block.index("[7]")]
    assert "pairing_code" in block and "pairing_issued_at" in block
