"""体检在 GBK 控制台下不许因为编码崩（2026-11-13 22:03 同事 Windows 实机）。

现场：门户一键安装第 4/4 个（her remote），stdout 被重定向 ⇒ Python 用本机 ANSI 代码页 GBK，
`print(render(report))` 遇到 ✓ 抛 UnicodeEncodeError；兜底 except 里那句带 ⚠，又崩一次，
用户看到两段 Python 堆栈。

这里用 `PYTHONIOENCODING=gbk` 的子进程**真跑**体检脚本（不是读源码找 reconfigure），
并且带一条阳性对照：把修复拿掉，同样条件必须复现崩溃 —— 否则这组测试可能根本没打到那条路。
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
PREFLIGHT = os.path.join(_BRIDGE_ROOT, "scripts", "preflight.py")
WIN_SRC = os.path.normpath(
    os.path.join(_BRIDGE_ROOT, "..", "..", "feishu-agent-oneclick", "src", "install-windows.ps1")
)

# 会让体检报出一条挡路项（✗）和几条 ✓ 的参数：服务 PATH 里没有 claude
_ARGS = ["--service-path", os.devnull, "--claude-launch", "ok"]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture
def here_dir():
    """夹具建在测试目录旁边，不用 tmp_path（那是 pathguard 的系统豁免前缀）。"""
    base = pathlib.Path(_HERE).parent / f".preflightgbk-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _run(script: str, encoding: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONIOENCODING": encoding}
    env.pop("PYTHONUTF8", None)
    # 🔴 不能加 -I / -E：那会连 PYTHONIOENCODING 一起忽略，测的就不是 GBK 了
    return subprocess.run(
        [sys.executable, script, *_ARGS],
        capture_output=True,
        env=env,
        timeout=60,
    )


def _broken_copy(here_dir: pathlib.Path, name: str, body: str) -> str:
    path = here_dir / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_gbk_console_renders_without_crashing():
    proc = _run(PREFLIGHT, "gbk")
    err = proc.stderr.decode("gbk", errors="replace")
    assert "Traceback" not in err and "UnicodeEncodeError" not in err, err
    assert proc.returncode in (0, 2), f"rc={proc.returncode}\n{err}"
    out = proc.stdout.decode("gbk")  # 必须是合法 GBK，不是半截字节
    assert "装机前体检" in out, out
    # 标记不能全变成 `?`：挡路的和通过的要分得出来
    assert re.search(r"^\s+×", out, re.M) and re.search(r"^\s+√", out, re.M), out
    assert "?" not in out.replace("？", ""), f"有字符被替换成了 ?：\n{out}"


def test_gbk_console_crash_fallback_does_not_crash_again(here_dir):
    """兜底那句（带 ⚠）在 GBK 下也要打得出来，并且照旧放行（rc=0）。"""
    body = _read(PREFLIGHT).replace(
        "    report = Report()\n\n    claude_path",
        "    raise RuntimeError('体检自己炸了')\n    report = Report()\n\n    claude_path",
        1,
    )
    assert "体检自己炸了" in body, "替换没生效，这条测试没测到东西"
    proc = _run(_broken_copy(here_dir, "broken_gbk.py", body), "gbk")
    err = proc.stderr.decode("gbk", errors="replace")
    assert proc.returncode == 0, f"rc={proc.returncode}\n{err}"
    assert "Traceback" not in err, err
    assert "跳过它继续装" in proc.stdout.decode("gbk"), proc.stdout


def test_ascii_console_also_survives():
    """更窄的编码（ascii）也只许降级，不许崩。"""
    proc = _run(PREFLIGHT, "ascii")
    err = proc.stderr.decode("ascii", errors="replace")
    assert proc.returncode in (0, 2), f"rc={proc.returncode}\n{err}"
    assert "Traceback" not in err, err


def test_positive_control_without_the_fix_it_crashes(here_dir):
    """阳性对照：拿掉修复，同样的 GBK 条件必须复现现场那个 UnicodeEncodeError。"""
    body = _read(PREFLIGHT)
    assert body.count("    _harden_stdio()\n") == 1
    assert body.count("print(_fit_console(render(report)))") == 1
    assert body.count("_say_safely(f\"\\n  ⚠ 装机前体检没跑完") == 1
    body = (
        body.replace("    _harden_stdio()\n", "", 1)
        .replace("print(_fit_console(render(report)))", "print(render(report))", 1)
        .replace("_say_safely(f\"\\n  ⚠ 装机前体检没跑完", "print(f\"\\n  ⚠ 装机前体检没跑完", 1)
    )
    proc = _run(_broken_copy(here_dir, "unfixed.py", body), "gbk")
    err = proc.stderr.decode("gbk", errors="replace")
    assert "UnicodeEncodeError" in err, f"没修的版本在 GBK 下居然没崩 ⇒ 这组测试没打到现场那条路\n{err}"


def test_windows_installer_sets_utf8_for_every_python_it_starts():
    """安装器那一层：起任何 python 之前就设好 UTF-8，后台服务的重启循环也要设。"""
    win = _read(WIN_SRC)
    first_python = re.search(r"&\s+\$exe\.Source\b", win).start()  # 挑 Python 的探针是第一个起 python 的地方
    for needle in ("$env:PYTHONIOENCODING = 'utf-8'", "$env:PYTHONUTF8 = '1'", "[Console]::OutputEncoding = "):
        at = win.find(needle)
        assert 0 <= at < first_python, f"{needle} 没有设在第一个 python 之前"
    runner = win[win.index("$runnerText = @\"") : win.index("$runnerText, ")]
    assert "`$env:PYTHONIOENCODING = 'utf-8'" in runner, "run-agent.ps1 没设 PYTHONIOENCODING ⇒ 服务往日志 print emoji 会崩"
    assert runner.index("PYTHONIOENCODING") < runner.index("-m bridge.app"), "要在起 python 之前设"
    # Invoke-WithDots 把 pip 的输出读回日志：PS5 默认按 ANSI 读，要指明 UTF-8
    assert "Get-Content -Raw -Encoding UTF8" in win
