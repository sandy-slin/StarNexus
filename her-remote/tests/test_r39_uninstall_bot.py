"""第 39 轮：飞书里发「卸载」要有回应。

起因：有人误装了，在手机上问「怎么卸载」—— **一点回应都没有**。
这两个字不在任何词表里 ⇒ 不进命令分派 ⇒ 当成普通问题丢给桥自己的 claude 跑一整轮，
而它既不知道卸载脚本叫什么、也不知道「云端那一步」。他要么放弃，要么来问人。

这组测试盯四件事：

1. **认得出**（`UNINSTALL_WORDS` + `_is_fleet_word` + 真走一次 `_handle_command`）；
2. **不误伤**：「删掉这个文件」「不想用了」「帮我卸载 node_modules」必须照常当普通话处理 ——
   收宽词的代价是用户在对话里提一句"删掉"就收到一张卸载指引；
3. **只回指引、不代执行**（用户拍板）：卸载不可逆，让桥删自己的程序目录、注销自己的常驻任务
   是自毁，死在半路没人收尾。这里扫分支源码，出现起线程 / 起子进程 / rm 就红；
4. **那段话本身是对的**：按平台只给一条路、桌面图标名和安装器放的那个一致、
   下载地址走 `selfupdate.BASE_URL`（写死的白名单域名）、云端那段措辞和
   `feishu-agent-oneclick/src/uninstall-mac.sh` 末尾对得上 —— 他在电脑上跑完脚本会再看到
   同一段话，两处对不上他会以为自己少做了一步。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import pathlib
import re
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = pathlib.Path(os.path.dirname(_HERE))
sys.path.insert(0, str(_ROOT))

# 系统 python3 上没有 lark_oapi（它只在 .venv 里）。这个文件一行飞书接口都不打 ——
# 要的只是 `bridge.app` 能 import 进来，所以在**收集阶段**（fixture 之前）放一个空壳。
# 真有 lark_oapi 的环境（.venv）走真的那份，不覆盖。
if "lark_oapi" not in sys.modules:
    try:
        import lark_oapi  # noqa: F401
    except ModuleNotFoundError:
        _stub = types.ModuleType("lark_oapi")
        _stub.__path__ = []  # 让 `import lark_oapi.xxx` 这种写法也不炸
        sys.modules["lark_oapi"] = _stub

import pytest  # noqa: E402

from bridge import app as app_mod  # noqa: E402
from bridge.app import Bridge  # noqa: E402

UNINSTALL_MAC_SH = _ROOT.parent.parent / "feishu-agent-oneclick" / "src" / "uninstall-mac.sh"


def _ask(text: str) -> tuple[bool, list[str]]:
    """真走一遍命令分派，返回（吃掉了吗，说了哪几句）。

    `Bridge.__new__` + 只挂 `_say`：这条分支不该碰 sessions / workspaces / fleet 里的任何东西，
    少挂一个属性就炸 = 它去碰了不该碰的，也是一条判据。
    """
    bridge = Bridge.__new__(Bridge)
    said: list[str] = []
    bridge._say = lambda chat, msg: said.append(msg)
    handled = asyncio.run(Bridge._handle_command(bridge, "dm:owner", "oc_1", text))
    return handled, said


# ---------- 1. 认得出 ----------


@pytest.mark.parametrize("word", ["卸载", "怎么卸载", "如何卸载", "卸载助手", "我要卸载", "卸载掉"])
def test_plain_chinese_uninstall_is_answered(word):
    """手机上打的就是这几个字，不带斜杠。"""
    assert word in app_mod.UNINSTALL_WORDS
    # `_is_fleet_word` 是 `_handle` 里"这句话要不要进命令分派"的闸门。不在里面 ⇒
    # 分派根本不会被调用，词表写得再全也没用（app._handle 那个 if）。
    assert app_mod._is_fleet_word(word), "没进命令分派的闸门"
    handled, said = _ask(word)
    assert handled is True, "分派没吃掉这句话 ⇒ 会被当成普通问题跑一整轮"
    assert said, "吃掉了却一句话都没回 = 用户看到的还是「没反应」"


@pytest.mark.parametrize("command", ["/uninstall", "/卸载"])
def test_slash_uninstall_is_answered_too(command):
    handled, said = _ask(command)
    assert handled is True and said


def test_slash_uninstall_is_registered_in_known_commands():
    """没登记 ⇒ 打错命令的兜底会回「没有这个用法」，而他打的是对的（menu.py 文件头那条红线）。"""
    from bridge.menu import known_commands, unknown_command

    known = {c.lower() for c in known_commands()}
    for command in app_mod.UNINSTALL_COMMANDS:
        assert command.lower() in known, f"{command} 没进 menu.known_commands()"
        assert not unknown_command(command), f"{command} 被当成打错的命令"


# ---------- 2. 不误伤 ----------


@pytest.mark.parametrize("text", [
    "删掉这个文件",
    "不想用了",
    "把 node_modules 删掉",
    "帮我卸载 node_modules",
    "卸载一下这个包",
    "怎么卸载 homebrew",
    "这个依赖卸载不干净，报错了",
])
def test_wide_phrasings_are_not_hijacked(text):
    """这些是**正常的一句话**，要照常发给会话 —— 不能被卸载指引吃掉。

    反过来（收 "删掉"、"不想用了" 这类宽词）的代价：用户让它删个文件，
    收到的是一张教他怎么把助手卸掉的指引。
    """
    assert not app_mod._is_fleet_word(text)
    handled, said = _ask(text)
    assert handled is False and not said, f"「{text}」被吃掉了"


# ---------- 3. 只回指引，不代执行 ----------


def _uninstall_branch_source() -> str:
    """`_handle_command` 里卸载那一支的源码。"""
    source = inspect.getsource(Bridge._handle_command)
    assert "UNINSTALL_WORDS" in source, "飞书里发「卸载」没接上命令分派"
    start = source.index("UNINSTALL_WORDS")
    rest = source[start:]
    # 到下一个 `if command` 分支为止
    match = re.search(r"\n        if (command|is_menu)", rest)
    return rest[:match.start()] if match else rest


def test_the_branch_only_talks_never_executes():
    """🔴 桥不许自己跑卸载：不可逆、而且是自毁（删自己的目录、注销自己的常驻）。

    这条扫分支源码。改成代执行（起线程跑脚本、subprocess、rm、launchctl/schtasks）就红。
    """
    branch = _uninstall_branch_source()
    for forbidden in ("Thread", "subprocess", "Popen", "os.remove", "shutil.rmtree",
                      "rm -rf", "launchctl", "schtasks", "os.system"):
        assert forbidden not in branch, f"卸载分支里出现了 {forbidden} —— 这一支只许回话"
    assert "self._say(chat_id, uninstall_help())" in branch, "回的不是那段指引"


def test_asking_to_uninstall_touches_nothing_on_disk(tmp_path, monkeypatch):
    """阳性对照不靠扫源码：真发一次「卸载」，看有没有任何东西被起起来 / 被删掉。"""
    import subprocess
    import threading

    monkeypatch.chdir(tmp_path)
    victim = tmp_path / "程序目录"
    victim.mkdir()
    (victim / "config.toml").write_text("x = 1\n", encoding="utf-8")

    def no_popen(*a, **kw):  # noqa: ANN002, ANN003
        raise AssertionError(f"卸载分支起了子进程：{a!r}")

    def no_thread(*a, **kw):  # noqa: ANN002, ANN003
        raise AssertionError(f"卸载分支起了线程：{kw.get('name') or a!r}")

    monkeypatch.setattr(subprocess, "Popen", no_popen)
    monkeypatch.setattr(subprocess, "run", no_popen)
    monkeypatch.setattr(threading, "Thread", no_thread)

    handled, said = _ask("卸载")
    assert handled and said
    assert (victim / "config.toml").exists(), "它真去删东西了"


# ---------- 4. 那段话本身 ----------


def test_reply_gives_only_this_platform_route():
    """按平台只给他那一条。两条都给 = 让他自己判断自己用的是什么系统，而他问的就是「怎么卸载」。"""
    mac = app_mod.uninstall_help(windows=False)
    win = app_mod.uninstall_help(windows=True)

    assert "CarHer 助手卸载.command" in mac
    assert "feishu-agent-uninstall-mac.command" in mac
    for leak in ("CarHer 助手卸载.bat", "feishu-agent-uninstall-windows.bat", "Windows"):
        assert leak not in mac, f"Mac 那段里混进了 {leak}"

    assert "CarHer 助手卸载.bat" in win
    assert "feishu-agent-uninstall-windows.bat" in win
    for leak in ("CarHer 助手卸载.command", "feishu-agent-uninstall-mac.command"):
        assert leak not in win, f"Windows 那段里混进了 {leak}"


def test_reply_follows_this_machine_by_default():
    """不带参数 ⇒ 看这台机器（桥知道自己在哪个平台，osenv.IS_WINDOWS）。"""
    from bridge import osenv

    assert app_mod.uninstall_help() == app_mod.uninstall_help(windows=osenv.IS_WINDOWS)


def test_desktop_shortcut_name_matches_what_the_installer_puts_there():
    """🔴 和第 15 轮那个坑同形：十几处叫人双击桌面诊断，而桌面上放的是另一个名字。

    判据取安装器源码，不是抄一份常量。
    """
    installers = {
        False: _ROOT.parent.parent / "feishu-agent-oneclick" / "src" / "install-mac.sh",
        True: _ROOT.parent.parent / "feishu-agent-oneclick" / "src" / "install-windows.ps1",
    }
    for windows, path in installers.items():
        if not path.exists():
            pytest.skip("本地没有安装器源码")
        assert "CarHer 助手卸载" in path.read_text(encoding="utf-8"), f"{path.name} 不放那个图标了"
        assert "「CarHer 助手卸载」" in app_mod.uninstall_help(windows=windows)


def test_download_url_comes_from_the_pinned_host():
    """地址不另写一份：`selfupdate.BASE_URL` 是写死的白名单域名（改它等于改"谁能在同事机器上跑代码"）。"""
    from bridge import selfupdate

    for windows in (False, True):
        text = app_mod.uninstall_help(windows=windows)
        assert f"{selfupdate.BASE_URL}/" in text
        assert "https://chat.auto-link.com.cn/ccdl/feishu-agent/" in text
    # 🔴 判据要的是「从 selfupdate 取」，不是「源码里出现 BASE_URL 这几个字」——
    # 后者连 `BASE_URL = "https://…"` 这种在本地抄一份的写法都放过（做过变异检查，它真的放过了）。
    source = inspect.getsource(app_mod.uninstall_help)
    assert "from .selfupdate import BASE_URL" in source, "地址是抄的字面量 ⇒ 改域名后这里把人送到 404"
    assert "chat.auto-link.com.cn" not in source, "域名在这里又抄了一份"


def test_reply_says_the_desktop_icon_may_be_missing_and_why():
    """老版本装的机器上桌面没有那个图标。不说清楚，他会以为自己装坏了。"""
    for windows in (False, True):
        text = app_mod.uninstall_help(windows=windows)
        assert "桌面上没有" in text
        assert "老版本" in text and "自动更新" in text


def test_reply_carries_the_cloud_step_in_the_installer_wording():
    """🔴 脚本删不掉云端：自建应用还在 ⇒ app_secret 仍然有效，谁拿到都能在别的机器上装一次。

    措辞照 `uninstall-mac.sh` 末尾那段（他在电脑上跑完会再看到同一段话）。
    """
    if not UNINSTALL_MAC_SH.exists():
        pytest.skip("本地没有安装器源码")
    script = UNINSTALL_MAC_SH.read_text(encoding="utf-8")
    # 这几句在卸载脚本末尾逐字存在；两边必须一致，不许各编一套
    shared = [
        "停用或删除",
        "https://open.feishu.cn/app",
        "Her remote",
        "停用应用",
        "仍然有效",
        "以你的名义收你的消息",
    ]
    for windows in (False, True):
        text = app_mod.uninstall_help(windows=windows)
        for phrase in shared:
            assert phrase in script, f"卸载脚本里已经没有「{phrase}」了，两边的措辞得一起改"
            assert phrase in text, f"回复里少了「{phrase}」"
        assert "open.feishu.cn" in text


def test_reply_keeps_the_copy_discipline():
    """用户骂过的几条：不许让他截图自证、不许说"已自动发给管理员"、程序自己的事不许推给 IT。"""
    for windows in (False, True):
        text = app_mod.uninstall_help(windows=windows)
        for banned in ("截图", "已自动发给管理员", "已经发给管理员", "自动上报给管理员"):
            assert banned not in text, f"回复里出现了「{banned}」"
        assert not re.search(r"(找|联系|问)\s*(IT|管理员|运维)", text), "把程序自己的事推给了 IT"
        # 产品面向不写代码的同事：不许叫他打命令、不许塞内部名词
        assert "sudo" not in text and "终端" not in text


def test_reply_passes_the_shared_copy_gate():
    """走 `test_copy_quality` 那套判据（扫 `bridge/**` 的所有给人看的话）。

    单独断一次是因为那条闸门按文件扫，这段话被改坏时它报的是"某文件某行"，
    这里报的是"卸载那段话不合规"。
    """
    sys.path.insert(0, _HERE)
    from test_copy_quality import all_hits, scan_source, unexplained

    from bridge.menu import known_commands

    commands = known_commands()
    units = scan_source(
        "def uninstall_help():\n    return %r\n" % app_mod.uninstall_help(windows=False),
        "bridge/app.py", commands,
    )
    assert units, "判据失效了：一条都没扫到"
    assert not unexplained(all_hits(units)), [h.token for h in unexplained(all_hits(units))]


def test_no_bot_menu_node_was_added():
    """常驻菜单已经 10/10 满了（botmenu.MAX_NODES），卸载是一辈子按一次的事 ⇒ 不占节点。"""
    from bridge import botmenu

    total = len(botmenu.MENU_TREE) + sum(len(children) for _, _, children in botmenu.MENU_TREE)
    assert total <= botmenu.MAX_NODES, f"菜单节点 {total} 个，超了 {botmenu.MAX_NODES}"
    tree = repr(botmenu.MENU_TREE)
    assert "卸载" not in tree, "卸载不该占菜单节点（满了，而且这是一辈子按一次的事）"
