"""11-16 巡检:Mac 装依赖那步 pip 零输出几秒就失败,却叫人查网络 / 找 IT。

现场(上报 165555/165623/170455,同一台 macOS 27 arm64):install-pip.log 是空的(上报里没有它),
198 nginx 同窗 /ccdl/feishu-agent/wheels/ 零请求 ⇒ pip 没连到服务器就失败了,「查网络」方向错。

这里**抽安装器原文那一段真跑**(从「# 不碰系统 Python」到「ok "✓ 运行环境就绪"」),桩掉 pip / python:
- 桩 A:pip 被 SIGKILL(零输出,退出码 137)⇒ 文案说「被系统结束 / 不是网络问题」,日志有退出码和 python -V;
- 桩 B:pip 打一行网络错误 exit 1 ⇒ 文案保持「两个源都没成功」+ 找 IT 放行。
`CARHER_MAC_SRC_1116=<路径>` 可指向修前那份,看它红。
"""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
MAC_SRC = os.environ.get("CARHER_MAC_SRC_1116") or os.path.join(_ONECLICK, "src", "install-mac.sh")

START = "# 不碰系统 Python，全在 ~/.carher-feishu-agent 里"
END = 'ok "✓ 运行环境就绪"'

PIP_STUB = """#!/bin/bash
case "$FAKE_PIP_MODE" in
  killed) kill -9 $$ ;;
  neterr) echo "ERROR: Could not fetch URL https://chat.auto-link.com.cn/ccdl/feishu-agent/wheels/index.html: connection error"; exit 1 ;;
  ok)     echo "Successfully installed lark-oapi-1.7.5"; exit 0 ;;
esac
"""
PY_STUB = """#!/bin/bash
if [ "$1" = "-V" ]; then echo "Python 3.12.16"; exit 0; fi
if [ "$1" = "-m" ] && [ "$2" = "pip" ]; then
  [ "$FAKE_PIP_MODE" = killed ] && kill -9 $$
  echo "pip 26.3 from /fake (python 3.12)"; exit 0
fi
exit 0
"""


def _block() -> str:
    text = pathlib.Path(MAC_SRC).read_text(encoding="utf-8")
    a = text.index(START)
    b = text.index(END, a) + len(END)
    return text[a:b]


def _exe(path: pathlib.Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _run(tmp_path: pathlib.Path, mode: str, *, fresh: int = 1, plist: bool = False):
    app = tmp_path / "app"
    venv = app / "venv"
    (venv / "bin").mkdir(parents=True)
    (app / "logs").mkdir()
    _exe(venv / "bin" / "pip", PIP_STUB)
    _exe(venv / "bin" / "python", PY_STUB)
    plist_path = tmp_path / "fake.plist"
    if plist:
        plist_path.write_text("<plist/>")
    pre = (
        "set -euo pipefail\n"
        f'APP_HOME="{app}"\nVENV="$APP_HOME/venv"\nLOG_DIR="$APP_HOME/logs"\n'
        f'PLIST="{plist_path}"\n'
        'PIP_WHEELS="https://example.invalid/wheels/index.html"\nPIP_INDEX="https://example.invalid/simple"\n'
        f"_venv_fresh={fresh}\n"
        "heartbeat_start(){ :; }\nheartbeat_stop(){ :; }\n"
        "warn(){ printf '%s\\n' \"$1\"; }\nok(){ printf '%s\\n' \"$1\"; }\n"
        "die(){ printf '%s\\n' \"DIE $1\" >&2; exit 1; }\n"
    )
    env = {**os.environ, "FAKE_PIP_MODE": mode}
    proc = subprocess.run(["/bin/bash", "-c", pre + _block()], capture_output=True, text=True,
                          timeout=90, env=env)
    log = app / "logs" / "install-pip.log"
    return proc, (log.read_text(encoding="utf-8") if log.exists() else ""), venv


def test_killed_pip_is_not_blamed_on_network(tmp_path):
    proc, log, venv = _run(tmp_path, "killed")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    err = proc.stderr
    assert "被系统结束" in err and "不是网络问题" in err, err
    assert "两个源都没成功" not in err and "放行" not in err, err
    assert "提交反馈" in err
    # 日志不再是空的 ⇒ 上报(跳过空文件)会带上它
    assert log.strip(), "install-pip.log 还是空的,上报带不上"
    assert "退出码 137" in log, log
    assert "Python 3.12.16" in log and "-V" in log, log
    assert "-m pip --version" in log, log
    assert "uname -a" in log and "sw_vers" in log, log


def test_killed_pip_fresh_venv_is_removed_so_redouble_click_rebuilds(tmp_path):
    proc, _log, venv = _run(tmp_path, "killed", fresh=1)
    assert "从头重建" in proc.stderr, proc.stderr
    assert not venv.exists(), "文案说重新双击会重建,venv 却还在"
    # 删掉之后,上面那段 `[ ! -x "$VENV/bin/python" ]` 才会走建 venv
    text = pathlib.Path(MAC_SRC).read_text(encoding="utf-8")
    assert text.index('if [ ! -x "$VENV/bin/python" ]; then') < text.index(START)


def test_killed_pip_keeps_venv_of_an_installed_service(tmp_path):
    """重装时复用旧 venv、服务已装(plist 在)⇒ 不删正在用的 venv,也不承诺重建。"""
    proc, _log, venv = _run(tmp_path, "killed", fresh=0, plist=True)
    assert "被系统结束" in proc.stderr, proc.stderr
    assert "从头重建" not in proc.stderr, proc.stderr
    assert (venv / "bin" / "python").exists()


def test_reused_venv_without_service_is_removed(tmp_path):
    proc, _log, venv = _run(tmp_path, "killed", fresh=0, plist=False)
    assert "从头重建" in proc.stderr and not venv.exists(), proc.stderr


def test_network_error_keeps_the_network_copy(tmp_path):
    proc, log, venv = _run(tmp_path, "neterr")
    assert proc.returncode == 1
    err = proc.stderr
    assert "两个源都没成功" in err and "放行" in err, err
    assert "被系统结束" not in err, err
    assert "connection error" in log
    assert (venv / "bin" / "python").exists(), "网络问题不该删 venv"


def test_success_path_untouched(tmp_path):
    proc, log, _venv = _run(tmp_path, "ok")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "✓ 运行环境就绪" in proc.stdout
    assert "安装器诊断" not in log
