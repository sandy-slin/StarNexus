"""IT 预烤凭据（环境变量 APP_ID + APP_SECRET）两个平台都要生效。

推荐的推广方式是「IT 批量建应用、只把凭据烤进安装包，同事双击就完事」
（`docs/productize-plan.md`）。2026-11-06 写推广文档时发现两处不通：

- **Windows**：凭据那一步开头写死 `$appId = ''`，根本不读环境变量 ⇒ 整条路不通
- **Mac**：全新机器能用，但**重装**时那段 `read -r APP_ID` 会把预烤的值直接覆盖掉

Mac 那段真跑（抠出凭据段落，喂假环境）；Windows 没有 pwsh ⇒ 只能静态核对。
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick", "src"))
MAC = os.path.join(_SRC, "install-mac.sh")
WIN = os.path.join(_SRC, "install-windows.ps1")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _mac_cred_block() -> str:
    """凭据段落：从 PREBAKED= 那一行到「先要有一个属于你自己的飞书机器人」之前。"""
    text = _read(MAC)
    start = text.index('PREBAKED=""')
    end = text.index('if [ -z "${APP_ID:-}" ]; then', start)
    return text[start:end]


@pytest.fixture
def here_dir():
    """夹具建在测试目录旁边，不放 tmp_path（macOS 上是 pathguard 豁免前缀）。"""
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".prebake-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _run_mac(here_dir, *, env: dict, existing_config: bool, typed: str = "") -> dict:
    """真跑 Mac 的凭据段落 → 跑完之后 APP_ID / APP_SECRET / PREBAKED 的值。"""
    config = here_dir / "config.toml"
    if existing_config:
        config.write_text('app_id = "cli_old_one"\napp_secret = "old_secret"\n', encoding="utf-8")
    tty = here_dir / "tty"
    tty.write_text(typed + "\n", encoding="utf-8")
    script = (
        "set -uo pipefail\n"
        "ok(){ :; }; warn(){ :; }; say(){ :; }\n"
        # 提示语 `printf '  直接按回车…: '` 不带换行，会粘到后面那行 `APP_ID=…` 前面
        # 把解析搅乱（第一版就这么假红过）⇒ 提示语扔进 stderr
        "exec 3>&1 1>&2\n"
        f'CONFIG="{config}"\n'
        + _mac_cred_block().replace("</dev/tty", f"<{tty}")
        + '\nprintf "APP_ID=%s\\nAPP_SECRET=%s\\nPREBAKED=%s\\n" "${APP_ID:-}" "${APP_SECRET:-}" "${PREBAKED:-}" >&3\n'
    )
    clean = {k: v for k, v in os.environ.items() if k not in ("APP_ID", "APP_SECRET")}
    clean.update(env)
    out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=clean)
    return dict(line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line)


def test_mac_prebaked_on_a_fresh_machine(here_dir):
    got = _run_mac(here_dir, env={"APP_ID": "cli_it_made", "APP_SECRET": "s3"}, existing_config=False)
    assert got["APP_ID"] == "cli_it_made" and got["APP_SECRET"] == "s3" and got["PREBAKED"] == "1"


def test_mac_prebaked_survives_a_reinstall(here_dir):
    """🔴 原来的 bug：重装时问「回车继续升级」那一下，`read -r APP_ID` 把预烤的值冲掉了。"""
    got = _run_mac(here_dir, env={"APP_ID": "cli_it_made", "APP_SECRET": "s3"},
                   existing_config=True, typed="")
    assert got["APP_ID"] == "cli_it_made", f"重装把预烤的凭据冲掉了：{got}"
    assert got["APP_SECRET"] == "s3"


def test_mac_half_prebaked_is_ignored(here_dir):
    """只给了 APP_ID 没给 SECRET ⇒ 当没给，别拿半套凭据往下走。"""
    got = _run_mac(here_dir, env={"APP_ID": "cli_it_made"}, existing_config=False)
    assert got["APP_ID"] == "" and got["PREBAKED"] == ""


def test_mac_without_prebake_reinstall_still_reuses_the_old_bot(here_dir):
    """阳性对照：没预烤的老用户重装，回车照旧沿用原来的机器人。"""
    got = _run_mac(here_dir, env={}, existing_config=True, typed="")
    assert got["APP_ID"] == "cli_old_one" and got["APP_SECRET"] == "old_secret"


def test_windows_reads_the_prebaked_env_vars():
    """Windows 没有 pwsh 跑不了 ⇒ 静态核对：凭据段落开头要读 $env:APP_ID / $env:APP_SECRET。

    🔴 只能抓「又被改回写死空串」，**不是**「Windows 验过了」的依据。
    """
    text = _read(WIN)
    block = text[text.index("[4/6]"):text.index("先要有一个属于你自己的飞书机器人")]
    assert "$env:APP_ID" in block and "$env:APP_SECRET" in block, "Windows 又不认预烤凭据了"
    assert not re.search(r"^\$appId = ''\s*$", block, re.M), "凭据段落开头又写死成空串了"
    # 有预烤值时要跳过「以前装过」那段（否则 Read-Host 会把它冲掉，同 Mac 那个 bug）
    assert re.search(r"if \(-not \$appId -and \(Test-Path \$config\)\)", block), \
        "重装那段没被预烤挡住 ⇒ Read-Host 会把预烤的值冲掉"
