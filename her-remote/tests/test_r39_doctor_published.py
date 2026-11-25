"""自检（doctor）必须发到公网 —— 这是「桥装不上」时唯一还能用的路。

现场（11-21）：3 台 Windows（…<机器> / …<机器> / …<机器>）自动更新连着几天失败，
而**失败原因就写在他们本机**：`logs/selfupdate-install.log` 和
`update_state.json` 的 `detail`（`_remember` 截的输出尾巴）。
`b261f807` 让 doctor 会读这两样并判红 —— 但 doctor 只随安装包嵌在机器上
⇒ 他们手上那份是旧的 ⇒ **鸡生蛋**：要用上新 doctor 得先装成功，
而「装不上」正是要查的事。发新版的桥对他们没用（那句日志是他们机器上那版打的）。

⇒ doctor 传到公网之后这条路不再依赖桥：一条命令就能跑最新自检。

同形第二次：卸载包当初也是「安装器叫用户双击、而 publish 从来没传」
（`publish-update.sh` 里那段红字），所以这条测试一起钉住四个文件。
"""

from __future__ import annotations

import os
import pathlib
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))

# build.sh 产出、用户要用得上的四个「救命文件」（桥起不来时还能跑）
_RESCUE = [
    "feishu-agent-uninstall-mac.command",
    "feishu-agent-uninstall-windows.bat",
    "feishu-agent-doctor-mac.command",
    "feishu-agent-doctor-windows.bat",
]


@pytest.fixture(scope="module")
def publish() -> str:
    return pathlib.Path(os.path.join(_ONECLICK, "publish-update.sh")).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def build() -> str:
    return pathlib.Path(os.path.join(_ONECLICK, "build.sh")).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", _RESCUE)
def test_build_produces_it(name, build):
    assert name in build, f"build.sh 不再产出 {name}？"


@pytest.mark.parametrize("name", _RESCUE)
def test_publish_declares_it(name, publish):
    """文件名要出现在 publish-update.sh 里（否则 build 出来也只躺在本地）。"""
    assert name in publish, f"{name} 没进 publish-update.sh ⇒ 公网 404"


def _vars_in(line: str) -> set:
    return set(re.findall(r'"\$([A-Z_]+)"', line))


def test_upload_loop_covers_the_rescue_files(publish):
    """🔴 光声明变量不算 —— 要真进上传那个循环。"""
    loops = [l for l in publish.splitlines()
             if l.strip().startswith("for f in") and "$MAC" in l]
    assert loops, "找不到上传循环"
    for line in loops:
        got = _vars_in(line)
        for v in ("UN_MAC", "UN_WIN", "DOC_MAC", "DOC_WIN"):
            assert v in got, f"上传循环漏了 {v}：{line.strip()}"


def test_dry_run_lists_them(publish):
    """--dry-run 要把它们列出来，不然「发了没发」看不见。"""
    line = [l for l in publish.splitlines() if "latest.json; do echo" in l]
    assert line, "找不到 dry-run 的清单行"
    got = _vars_in(line[0])
    for v in ("DOC_MAC", "DOC_WIN"):
        assert v in got, f"dry-run 清单漏了 {v}"


def test_doctor_reads_the_local_failure_record():
    """doctor 真的会去读那两样本机数据 —— 不读的话发到公网也没用。"""
    doc = pathlib.Path(os.path.join(_ONECLICK, "src", "doctor-windows.ps1")).read_text(encoding="utf-8")
    assert "update_state.json" in doc
    assert "Get-CarherUpdateFailure" in doc
