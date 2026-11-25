"""第 15 轮：重装到第 6 步，安装器一个字不说就退出，服务没了（主人本机回归现场）。

2026-11-07 11:08 在主人本机跑发布出去的那个安装包：输出停在「[6/6] 让它开机自动在后台跑…」，
退出码 113，没有任何报错；`launchctl print` 找不到服务 ⇒ 机器人不回话。

## 复现（一次性 job，收到 SIGTERM 要 4 秒才退 —— 真服务的形状）

    bootout rc=0 耗时 0s；紧接 bootstrap rc=5 Bootstrap failed: 5: Input/output error
       兜底 launchctl load rc=0 Load failed: 5: Input/output error     ← 失败了但返回 0
    （3 次里 2 次）

然后旧进程退完、服务没了，`svc_field` 里的 `launchctl print` 返回 113 ⇒
`set -euo pipefail` 下 `X="$(svc_field runs)"` 让整个安装器**静默退出 113**。

🔴 自动更新跑的就是这个安装器 ⇒ 不修的话，自动更新三次里两次把服务弄没。

这个文件**真的去跑 launchd**（一次性 label，跑完 bootout + 删 plist），只在 macOS 上跑。
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import tempfile
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "..",
                                    "feishu-agent-oneclick", "src", "install-mac.sh"))

mac_only = pytest.mark.skipif(platform.system() != "Darwin", reason="要真 launchd")


def _block() -> str:
    """把安装器里「停旧的、起新的」那一段原样抠出来（不抄一份 —— 抄了会和安装器飘开）。"""
    text = open(SRC, encoding="utf-8").read()
    i = text.index("svc_exists() {")
    j = text.index('svc_exists || die "', i)
    return text[i:j] + 'svc_exists || { echo "DIE"; exit 1; }\necho "BOOT_DONE"\n'


def _svc_field() -> str:
    text = open(SRC, encoding="utf-8").read()
    m = re.search(r"^svc_field\(\) \{.*?^\}", text, re.S | re.M)
    assert m, "安装器里找不到 svc_field"
    return m.group(0)


def test_svc_field_does_not_kill_the_installer_when_service_is_missing():
    """🔴 现场那个 113：服务不在时读状态，不许让 `set -euo pipefail` 的安装器静默退出。"""
    script = ("set -euo pipefail\nLABEL=com.carher.r15-never-exists\n" + _svc_field() +
              '\nX="$(svc_field runs)"\necho "STILL_ALIVE:[$X]"\n')
    proc = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30)
    assert "STILL_ALIVE:[]" in proc.stdout, f"安装器被读状态那一行杀掉了 rc={proc.returncode}"


@mac_only
def test_restart_survives_a_slow_exiting_old_service():
    """真跑：旧服务收到 SIGTERM 要 4 秒才退 ⇒ 新的必须照样起来（原来 3 次里 2 次起不来）。"""
    label = f"com.carher.r15race-{os.getpid()}"
    uid = os.getuid()
    with tempfile.TemporaryDirectory() as tmp:
        sh = os.path.join(tmp, "slow.sh")
        with open(sh, "w") as h:
            h.write('#!/bin/bash\ntrap "sleep 4; exit 0" TERM\nwhile :; do sleep 1; done\n')
        os.chmod(sh, 0o755)
        plist = os.path.join(tmp, f"{label}.plist")
        with open(plist, "w") as h:
            h.write(f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>{label}</string>
<key>ProgramArguments</key><array><string>{sh}</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/></dict></plist>''')
        try:
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist], check=True, timeout=30)
            time.sleep(2)
            script = (f"set -euo pipefail\nLABEL={label}\nPLIST={plist}\n" + _block())
            results = []
            for _ in range(3):
                proc = subprocess.run(["/bin/bash", "-c", script],
                                      capture_output=True, text=True, timeout=90)
                # 🔴 要等**旧进程退完**再看（它收到 SIGTERM 要 4 秒）。
                # 变异验证当场抓到：第一版在安装器那段跑完立刻看，旧服务还在退出 ⇒
                # 「在」⇒ 把旧的一次性 bootstrap 写法也判成了成功。真故障是几秒后才露出来的。
                time.sleep(6)
                alive = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                                       capture_output=True, text=True)
                has_pid = bool(re.search(r"^\s*pid = \d+", alive.stdout, re.M))
                results.append((alive.returncode == 0 and has_pid, proc.returncode))
            assert all(ok for ok, _ in results), f"旧的退完之后新的没起来：{results}"
        finally:
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                           capture_output=True, timeout=30)
            for _ in range(15):
                if subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                                  capture_output=True).returncode != 0:
                    break
                time.sleep(1)
