"""第 15 轮：连不上飞书的时候，「发『诊断』」这条路本身就走不通。

用户原话（2026-11-07）：「如何让领导自己诊断哪儿出了问题。你让他在飞书里发诊断，
他飞书链接上，怎么诊断」—— 他连不上，发「诊断」就没人回。

查下来（只读）：
- 安装器 6 处提到「双击 feishu-agent-doctor-mac.command」，但**从来没把这个文件放到他电脑上**
  （`grep -E "cp |write_file|curl" install-mac.sh | grep doctor` 零命中）
- 安装文档里也**没有这个附件**（文档只挂了安装包一个，`doctor` 零次提及）
⇒ 我们一直在叫他双击一个他手上没有的文件。

这一轮：安装器把自检脚本**一起装到他电脑上**，并在桌面放一个能直接双击的「诊断」。
不靠飞书、不靠网络，电脑开着就能跑。
"""
from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
SRC_MAC = os.path.join(_ONECLICK, "src", "install-mac.sh")
PRODUCT_MAC = os.path.join(_ONECLICK, "feishu-agent-mac.command")


def _read(p):
    with open(p, encoding="utf-8") as h:
        return h.read()


def test_installer_writes_the_doctor_to_disk():
    text = _read(SRC_MAC)
    assert "@@EMBED_DOCTOR_MAC@@" in text, "安装器没把自检脚本带上 —— 他手上根本没有这个文件"


def test_installer_puts_a_double_clickable_shortcut_on_the_desktop():
    text = _read(SRC_MAC)
    assert re.search(r'Desktop/[^"]*诊断[^"]*\.command', text), "桌面上没有能双击的诊断"


def test_doctor_needs_no_network_to_answer_the_basics():
    """🔴 自检第一屏必须在**断网**时也能出结果（他就是因为连不上才要诊断）。

    版本号、机器人编号、服务有没有在跑、日志里最后一次连没连上 ——
    这几项都只读本机文件，不需要网。只有「是不是最新」要查网，查不到要照实说。
    """
    doc = _read(os.path.join(_ONECLICK, "src", "doctor-mac.sh"))
    assert "BUILDINFO" in doc and "CONFIG" in doc and "LOG" in doc
    assert "查不到最新版" in doc


def test_product_contains_the_real_doctor_body():
    """产物里要真有自检的正文，不只是占位符。"""
    text = _read(PRODUCT_MAC)
    assert "@@EMBED_DOCTOR_MAC@@" not in text, "占位符没被替换"
    assert "先看这三行" in text, "产物里没有自检正文（[0] 先看这三行）"
