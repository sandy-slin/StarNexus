"""第 15 轮：装完那一屏要说清「你装的是哪个机器人、哪一版」。

## 为什么（今晚这桩事的根）

2026-11-06 21:34/21:35，有人跟主人这台的机器人说话，被静默丢掉，他那边毫无反应。
查到最后卡在一个谁都答不上的问题：**他和主人是不是在用同一个机器人？**
R5 核过：`install-mac.sh` 的结论屏里 `App ID` / `APP_ID` **零命中** ——
走扫码那条路的人从头到尾没见过自己的机器人编号，根本没有「我装的是哪个」这个概念。
版本号同理：`BUILD_ID` 在安装器里只出现在「写 buildinfo.py」那两处，结论屏一个字没提。
于是一天换三次附件之后，没人能回答「我手上是哪一版」。

## 这一轮

结论屏加两行：机器人编号前 12 位（和 doctor 的 `[0]` 一字不差，方便两人对）+ 版本号。
🔴 新加的行**不许带「装好了」三个字** —— `test_install_verdict.py` 拿那三个字当假绿判据。
🔴 只打前 12 位，`app_secret` 一个字符都不许出现。
"""
from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "..",
                                     "feishu-agent-oneclick", "src"))
MAC = os.path.join(_SRC, "install-mac.sh")
WIN = os.path.join(_SRC, "install-windows.ps1")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _final_screen(text: str) -> str:
    """结论屏那一段：从「装好了」那行往后到脚本末尾。"""
    i = text.index("装好了，它现在已经在听你的飞书消息")
    return text[i:]


def test_mac_final_screen_shows_the_bot_id():
    screen = _final_screen(_read(MAC))
    assert "你的机器人" in screen, "结论屏没说装的是哪个机器人"
    assert "${APP_ID:0:12}" in screen, "没打机器人编号的前 12 位"


def test_mac_final_screen_shows_the_version():
    """版本号要**显示出来**。断言「这一版：」这句人话，而不是某个内部变量名 ——
    第一版我断言 `BUILD_ID` 字面量，而实现用的是 `$INSTALLED_BUILD`，测试为了错误的理由红。
    """
    screen = _final_screen(_read(MAC))
    assert "这一版：" in screen, "结论屏没说这是哪一版"
    assert "INSTALLED_BUILD" in screen, "版本号不是从装上的那份读回来的"


def test_mac_version_comes_from_the_file_on_disk():
    """🔴 要的是「我机器上装的是哪一版」，不是「这个安装包自称哪一版」。"""
    text = _read(MAC)
    assert 'buildinfo.py' in text.split("INSTALLED_BUILD=")[1][:200]
    # 占位符没被替换时不许打给用户
    assert "@@*" in text


def test_windows_final_screen_shows_both():
    text = _read(WIN)
    i = text.index("装好了，它现在已经在听你的飞书消息")
    screen = text[i:]
    assert "你的机器人" in screen
    assert "这一版：" in screen and "installedBuild" in screen


def test_new_lines_do_not_contain_the_false_green_words():
    """🔴 新加的行不许带「装好了」—— 那三个字是假绿判据。"""
    for path in (MAC, WIN):
        text = _read(path)
        for line in text.splitlines():
            if "你的机器人" in line:
                assert "装好了" not in line, f"{os.path.basename(path)}: {line}"


def test_secret_never_printed_on_the_final_screen():
    for path in (MAC, WIN):
        screen = _read(path)
        i = screen.index("装好了")
        tail = screen[i:]
        # 允许提到「凭据」这类词，但绝不许把 secret 变量打出来
        assert not re.search(r'echo.*\$\{?APP_SECRET', tail), os.path.basename(path)
        assert not re.search(r'Write-Host.*\$appSecret', tail), os.path.basename(path)
