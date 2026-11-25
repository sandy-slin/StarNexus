"""回归：`bridge/` 下的模块名不能撞标准库。

2026-11-03 真实故障：有个模块叫 `platform.py`。安装脚本用
`python bridge/register.py` 这种**脚本方式**跑，Python 把脚本所在目录放进
`sys.path[0]` ⇒ `import platform` 拿到我们的文件 ⇒ pycryptodome 调
`platform.architecture()` 时 AttributeError ⇒ 整个 lark_oapi 都 import 不进来，
装机直接崩在"扫码建应用"那步。

当时 105 条测试全是绿的 —— 因为包内用的是相对导入（`from .platform import`），
**测试走包导入，安装器走脚本导入**，两条路不一样。所以这里必须用脚本方式验。
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig

BRIDGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge")


def _stdlib_top_level_names() -> set[str]:
    names = set(sys.stdlib_module_names)
    # 再补一层实际目录扫描，防 sys.stdlib_module_names 漏掉什么
    stdlib_path = sysconfig.get_paths().get("stdlib")
    if stdlib_path and os.path.isdir(stdlib_path):
        for entry in os.listdir(stdlib_path):
            if entry.endswith(".py"):
                names.add(entry[:-3])
            elif os.path.isdir(os.path.join(stdlib_path, entry)) and not entry.startswith("_"):
                names.add(entry)
    return names


def test_no_module_shadows_stdlib():
    """硬断言：模块名不能和任何标准库顶层模块同名。"""
    stdlib = _stdlib_top_level_names()
    offenders = []
    for name in sorted(os.listdir(BRIDGE_DIR)):
        if not name.endswith(".py") or name == "__init__.py":
            continue
        stem = name[:-3]
        if stem in stdlib:
            offenders.append(stem)
    assert not offenders, (
        f"这些模块名撞了标准库：{offenders}。"
        "安装器用脚本方式跑时 bridge/ 会进 sys.path[0]，会把标准库顶掉。"
    )


def test_stdlib_survives_bridge_on_syspath():
    """bridge/ 在 sys.path[0] 时，每个同名风险模块都不能顶掉标准库。

    只验**标准库仍然完好**，不验模块本身能当顶层模块导入 —— `app`/`runner`/
    `approval` 用相对导入，只通过 `python -m bridge.app` 跑，按顶层导入报
    "relative import with no known parent package" 是正确行为，不是 bug。
    """
    # 这些是最容易被顶掉、且被第三方库依赖的标准库模块
    risky = ["platform", "types", "typing", "json", "os", "sys", "time", "socket", "ssl"]
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import importlib\n"
        "bad = []\n"
        "for name in %r:\n"
        "    m = importlib.import_module(name)\n"
        "    f = getattr(m, '__file__', '') or ''\n"
        "    if f.startswith(%r):\n"
        "        bad.append((name, f))\n"
        "print('BAD=' + repr(bad))\n"
    ) % (BRIDGE_DIR, risky, BRIDGE_DIR)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "BAD=[]" in proc.stdout, (
        f"有标准库被 bridge/ 顶掉了：{proc.stdout.strip()} {proc.stderr[-300:]}"
    )


def test_scripts_in_bridge_are_importable_standalone():
    """安装器会直接 `python bridge/<x>.py` 跑的那几个，必须能独立起来。

    其余模块只通过包导入（`python -m bridge.app`），不在此列。
    """
    failures = []
    for stem in ("register", "bootstrap", "osenv", "policy", "review", "sessions", "reply"):
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import platform\n"
            "assert hasattr(platform, 'architecture'), 'stdlib platform 被顶掉了'\n"
            "import importlib; importlib.import_module(%r)\n"
            "print('OK')\n"
        ) % (BRIDGE_DIR, stem)
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True
        )
        if "OK" not in proc.stdout:
            failures.append((stem, (proc.stderr or "").strip().splitlines()[-1:]))
    assert not failures, f"脚本方式导入失败：{failures}"


def test_lark_oapi_imports_with_bridge_on_syspath():
    """真实现场复现：bridge/ 在 sys.path 最前面时，lark_oapi 必须还能 import。

    这是当时炸掉的那一行 —— 它会连带 import pycryptodome，而后者要
    `platform.architecture()`。
    """
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import lark_oapi\n"
        "print('OK')\n"
    ) % BRIDGE_DIR
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert "OK" in proc.stdout, (
        "bridge/ 在 sys.path 最前面时 lark_oapi import 失败：\n"
        + (proc.stderr or "")[-600:]
    )


def test_register_script_runs_as_script():
    """`register.py` 必须能按脚本方式起来 —— 安装器就是这么调它的。

    用 `--check`：不带参数会真发起扫码并轮询最多一小时，测试会挂住
    （2026-11-03 踩过，就是这条暴露的）。
    """
    script = os.path.join(BRIDGE_DIR, "register.py")
    proc = subprocess.run(
        [sys.executable, script, "--check"], capture_output=True, text=True, timeout=60
    )
    combined = proc.stdout + proc.stderr
    assert "AttributeError" not in combined, f"脚本方式起不来：{combined[-500:]}"
    assert "Traceback" not in combined, f"脚本方式报错：{combined[-500:]}"
    assert "ImportError" not in combined, f"相对导入在脚本方式下炸了：{combined[-500:]}"
    assert "CHECK_OK=" in proc.stdout, f"自检没跑通：{combined[-300:]}"


def test_bootstrap_script_runs_as_script():
    script = os.path.join(BRIDGE_DIR, "bootstrap.py")
    proc = subprocess.run(
        [sys.executable, script], capture_output=True, text=True, timeout=60
    )
    combined = proc.stdout + proc.stderr
    assert "Traceback" not in combined, f"脚本方式报错：{combined[-500:]}"
    # 没给参数时应该打印用法
    assert "FATAL=" in combined
