"""安装器必须把 bridge/ 装齐。

## 这组测试为什么存在

2026-11-04 实测：两个安装器都只嵌了 11 个模块，而 `bridge/` 有 19 个。
漏掉的 8 个里包含：

- `redact.py` —— 出站凭据脱敏（唯一出口）
- `cmdclass.py` —— 判一条命令要不要弹确认卡片
- `pathguard.py` —— Bash 的工作目录边界（工具白名单管不到 Bash）
- `workspace.py` / `status.py` / `menu.py` / `prefs.py` / `botmenu.py`

本机一直能跑，是因为上一轮我手动把文件拷到了 `~/.carher-feishu-agent/bridge/`。
**一台新机器一键装完会在 import 阶段直接崩** —— 而且崩在"装完之后第一次启动"，
用户看到的只是"装完了但起不来"。

原来的 build 断言只查"模板里的 `@@EMBED` 都替换掉了"（有没有拼错占位符），
查不出"源码里新增了一个模块，模板里根本没写它"。方向反了。

🔴 **判据是"产物 import 得进来"，不是"产物里有这个文件名"。**
grep 文件名只能证明那行字在；真正要证明的是装出来的那个包能起来。
所以 `test_mac_product_yields_importable_package` 把产物里的源码抽出来落盘、
真的 import 一遍 —— 这正是 py_compile 查不出 NameError（漏 import）那条教训。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE_ROOT = os.path.dirname(_HERE)
_REPO = os.path.dirname(os.path.dirname(_BRIDGE_ROOT))
_ONECLICK = os.path.join(_REPO, "feishu-agent-oneclick")

MAC_PRODUCT = os.path.join(_ONECLICK, "feishu-agent-mac.command")
WIN_PRODUCT = os.path.join(_ONECLICK, "feishu-agent-windows.bat")
MAC_TEMPLATE = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN_TEMPLATE = os.path.join(_ONECLICK, "src", "install-windows.ps1")


def _bridge_modules() -> set[str]:
    """bridge/*.py（顶层 __init__ 是空的、安装器直接写空文件）+ 会话总控子包 bridge/fleet/*.py。

    🔴 原来只扫顶层，盯不到 `bridge/fleet/` —— 子包整个漏装也全绿（fleet-build-specs 末尾那条）。
    子包的 __init__.py 有内容（导出 model 的公开 API），要算进来。
    """
    folder = os.path.join(_BRIDGE_ROOT, "bridge")
    top = {
        name
        for name in os.listdir(folder)
        if name.endswith(".py") and name != "__init__.py"
    }
    sub_dir = os.path.join(folder, "fleet")
    sub = {f"fleet/{name}" for name in os.listdir(sub_dir) if name.endswith(".py")}
    return top | sub


_EMBED_RE = r"@@EMBED bridge/([a-z_/]+\.py)@@"


def _read(path: str) -> str:
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


# ---------- 模板 ----------


@pytest.mark.parametrize(
    "template", [MAC_TEMPLATE, WIN_TEMPLATE], ids=["mac", "windows"]
)
def test_every_bridge_module_is_embedded(template):
    """新建 bridge/*.py 必须同时往两个安装器里加一段写文件。"""
    if not os.path.exists(template):
        pytest.skip("模板不在")
    embedded = set(re.findall(_EMBED_RE, _read(template)))
    missing = _bridge_modules() - embedded
    assert not missing, f"{os.path.basename(template)} 漏装：{sorted(missing)}"


@pytest.mark.parametrize(
    "template", [MAC_TEMPLATE, WIN_TEMPLATE], ids=["mac", "windows"]
)
def test_embedded_modules_actually_exist(template):
    """反向：模板里写的模块名要真的存在（改名后忘了改模板会在这里炸）。"""
    if not os.path.exists(template):
        pytest.skip("模板不在")
    embedded = set(re.findall(_EMBED_RE, _read(template)))
    ghosts = embedded - _bridge_modules()
    assert not ghosts, f"{os.path.basename(template)} 引用了不存在的模块：{sorted(ghosts)}"


def test_both_installers_ship_the_same_set():
    """两个平台装的东西必须一样，否则 Windows 用户少一层闸门而没人知道。"""
    if not (os.path.exists(MAC_TEMPLATE) and os.path.exists(WIN_TEMPLATE)):
        pytest.skip("模板不在")
    mac = set(re.findall(_EMBED_RE, _read(MAC_TEMPLATE)))
    win = set(re.findall(_EMBED_RE, _read(WIN_TEMPLATE)))
    assert mac == win, f"两边不一致：mac 独有 {sorted(mac-win)}，win 独有 {sorted(win-mac)}"


def test_embedded_sources_never_contain_the_placeholder_literal():
    """🔴 被嵌进安装器的源码里，**不许**出现嵌入占位符的字面量（哪怕在注释里）。

    2026-11-05 实测：`bridge/policy.py` 的一行注释里写了它 ⇒ 文件原样嵌进安装器后，
    build.sh 的「还有没替换的占位符」那道闸把整次 build 拦下了（产物没出、旧的没被覆盖，
    闸门是对的）。但这个只能在**跑 build 的时候**才发现，而 build 要先跑一分多钟测试 ——
    提前在单测里抓，改完就知道。
    """
    marker = "@@" + "EMBED"  # 拆开写：这个测试文件自己不被嵌入，但别给人照抄的样板
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 🔴 `_EMBED_RE` 的捕获组**去掉了** `bridge/` 前缀（拿到的是 `app.py`、`fleet/x.py`）。
    # 第一版这里直接 join(root, rel) ⇒ 每个路径都不存在 ⇒ `exists()` 全假 ⇒ 一个文件都没查，
    # 测试永远绿。变异验证（往 policy.py 写回字面量）当场抓出来的。所以下面要补回前缀，
    # 并且**断言真的查到了文件**，不然同样的事会再发生一次。
    checked, offenders = 0, []
    for rel in sorted(set(re.findall(_EMBED_RE, _read(MAC_TEMPLATE)))):
        path = os.path.join(root, "bridge", rel)
        if not os.path.exists(path):
            continue
        checked += 1
        if marker in _read(path):
            offenders.append(f"bridge/{rel}")
    assert checked >= 10, f"只查到 {checked} 个文件 ⇒ 路径拼错了，这条测试什么都没查"
    assert not offenders, f"这些嵌入的源码里写了占位符字面量，build 会失败：{offenders}"


# ---------- 安全模块不许漏 ----------

# 这几个漏了不会立刻报错，而是**静默少一层闸门**，比 import 崩更危险。
SECURITY_CRITICAL = ("redact.py", "cmdclass.py", "pathguard.py", "policy.py", "approval.py")


@pytest.mark.parametrize("module", SECURITY_CRITICAL)
@pytest.mark.parametrize("product", [MAC_PRODUCT, WIN_PRODUCT], ids=["mac", "windows"])
def test_security_modules_are_in_the_shipped_product(product, module):
    """查**产物**而不是模板 —— 模板对了但没重新 build 一样是没上线。"""
    if not os.path.exists(product):
        pytest.skip("产物没生成，先跑 build.sh")
    blob = _read(product)
    stem = module[:-3]
    assert f"bridge/{module}" in blob or f"'{module}'" in blob or f'"{module}"' in blob, (
        f"产物里找不到 {module}"
    )
    # 文件名在 ≠ 内容在：再确认该模块的标志性内容也进来了
    markers = {
        "redact.py": "redact_with_notice",
        "cmdclass.py": "never_approvable",
        "pathguard.py": "outside_workspace",
        "policy.py": "ALWAYS_DENY",
        "approval.py": "HOOK_SOURCE",
    }
    assert markers[module] in blob, f"{module} 只有文件名、正文没嵌进去（{stem}）"


# ---------- 产物真的能 import ----------


def test_mac_product_yields_importable_package(tmp_path):
    """🔴 把产物里的源码抽出来落盘，**真 import 一遍**。

    `py_compile` 查不出漏 import 造成的 NameError，更查不出"少了一个被
    `from .x import y` 引用的模块" —— 那要到真正 import 时才炸。
    这条测试走的就是用户装完第一次启动那条路。
    """
    if not os.path.exists(MAC_PRODUCT):
        pytest.skip("产物没生成，先跑 build.sh")
    blob = _read(MAC_PRODUCT)

    # 产物里每个模块是 `write_file "$APP_HOME/bridge/<名>" <<'CARHER_PY_EOF' … CARHER_PY_EOF`
    pattern = re.compile(
        r'write_file "\$APP_HOME/bridge/([a-z_/]+\.py)" <<\'CARHER_PY_EOF\'\n'
        r"(.*?)\nCARHER_PY_EOF",
        re.S,
    )
    found = dict(pattern.findall(blob))
    assert found, "产物里一个模块都抽不出来 —— heredoc 形状变了？"

    missing = _bridge_modules() - set(found)
    assert not missing, f"产物里缺模块：{sorted(missing)}"

    pkg = tmp_path / "bridge"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    for name, body in found.items():
        (pkg / name).parent.mkdir(parents=True, exist_ok=True)
        (pkg / name).write_text(body + "\n", encoding="utf-8")

    # 用桥接自己的 venv（有 lark_oapi）。没有就退化成只 import 不依赖 SDK 的那些。
    venv_py = os.path.join(_BRIDGE_ROOT, ".venv", "bin", "python")
    python = venv_py if os.path.exists(venv_py) else sys.executable
    names = sorted(n[:-3].replace("/", ".").removesuffix(".__init__") for n in found)
    script = (
        "import importlib, sys\n"
        f"sys.path.insert(0, {str(tmp_path)!r})\n"
        f"for m in {names!r}:\n"
        "    try:\n"
        "        importlib.import_module('bridge.' + m)\n"
        "    except ModuleNotFoundError as exc:\n"
        # 第三方依赖缺失不算安装器的问题；缺 bridge.* 才算
        "        if (exc.name or '').startswith('bridge'):\n"
        "            raise\n"
        "        print('SKIP', m, exc.name)\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [python, "-c", script], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"产物装出来的包 import 失败：\n{proc.stderr[-2000:]}"
    assert "OK" in proc.stdout


def test_windows_product_has_every_module_write(tmp_path):
    """Windows 那边形状不同（here-string + Write-PyFile），单独查一遍。

    ⚠️ 只做静态核对 —— PowerShell 的真实行为必须在一台 Windows 上双击跑过。
    这条测试**不是**"Windows 验过了"的依据。
    """
    if not os.path.exists(WIN_PRODUCT):
        pytest.skip("产物没生成，先跑 build.sh")
    blob = _read(WIN_PRODUCT)
    written = {
        name.replace("\\", "/")
        for name in re.findall(r"Write-PyFile '([a-z_\\]+\.py)'", blob)
    }
    missing = _bridge_modules() - written
    assert not missing, f"Windows 产物缺 Write-PyFile：{sorted(missing)}"
