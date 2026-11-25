"""信使起不来时：日志要带程序名，给用户的话要说「重新双击安装包」而不是「刷新再试」。

现场（11-20 11:03，a-user 机器 …<机器>，Windows，版本 20261119-7af89c47）：
群聊里发消息回「❌ 没发进去：没能启动 claude 当信使：电脑上找不到要用的文件或程序
（可能删掉或挪走了），刷新一下再试。」。用户已经更新到最新版，照「刷新再试」点也没用。

两个缺口，都在这里钉住：
1. **上报里看不出起的是哪个程序** —— `relay._default_spawn` 原来只走 `friendly_error`，
   日志模板是通用的「%s（原文 %s: %s）」。`SubprocessRunner` 11-17 为同一个坑建过
   `_SPAWN_FAIL_LOGS` 那张表（当时 9 次 FileNotFoundError 不知道是哪个程序），
   信使这条腿没接上 ⇒ 同一个坑在两条腿上各犯一次。
2. **给用户的那句话是死路** —— `FileNotFoundError` 的通用文案是「刷新一下再试」，
   而这个病（后台那份 PATH 里没有 claude）刷新一百次都是同一句；`runner.py` 早就
   为它写了对症的「重新双击一次安装包就能修好」，信使这条腿没有。
"""

from __future__ import annotations

import logging
import tempfile

import pytest

from bridge.fleet.model import FleetJobs
from bridge.fleet.relay import Courier, _basename_any_os


@pytest.fixture()
def caplogs(monkeypatch):
    got: list[str] = []

    class H(logging.Handler):
        def emit(self, record):  # noqa: D102
            got.append(record.getMessage())

    lg = logging.getLogger("feishu-bridge.fleet")
    h = H()
    lg.addHandler(h)
    old = lg.level
    lg.setLevel(logging.ERROR)
    yield got
    lg.removeHandler(h)
    lg.setLevel(old)


def courier(claude_bin: str, exc: BaseException) -> Courier:
    def boom(argv, cwd, env):
        raise exc

    d = tempfile.mkdtemp()
    return Courier(home=d, cwd=d, jobs=FleetJobs(), claude_bin=claude_bin, spawn=boom)


# 真机上 claude_bin 可能长成这四种。🔴 第二个是 Windows npm 装出来的常态，
# 而测试跑在 mac 上 —— `os.path.basename` 不认 `\`，查表会落到「其他程序」（本轮实测抓到）。
_BINS = [
    "claude",
    r"C:\Users\x\.carher-npm-global\claude.cmd",
    "/opt/homebrew/bin/claude",
    r"C:\Users\x\.local\bin\claude.exe",
]


@pytest.mark.parametrize("claude_bin", _BINS)
def test_log_names_the_program(caplogs, claude_bin):
    """日志必须是带程序名的**源码常量**（上报只发常量模板）。"""
    msg = courier(claude_bin, FileNotFoundError(2, "The system cannot find the file specified")).ensure()
    assert msg, "起不来就得给个失败原因"
    assert caplogs, "一条日志都没打 ⇒ 上报里什么都看不到"
    assert "程序=claude" in caplogs[-1], f"认不出程序名：{caplogs[-1]}"


@pytest.mark.parametrize("claude_bin", _BINS)
def test_not_found_tells_user_to_reinstall(claude_bin):
    """这个病「刷新一下再试」没用 —— 必须说重新双击安装包。"""
    msg = courier(claude_bin, FileNotFoundError(2, "nope")).ensure()
    assert "重新双击" in msg, msg
    assert "刷新一下再试" not in msg, f"还是那句死路文案：{msg}"


def test_other_errors_keep_their_own_wording():
    """阴性对照：权限错不该被套上「重新双击」—— 否则这条就成了万能话术。"""
    msg = courier("claude", PermissionError(13, "denied")).ensure()
    assert "重新双击" not in msg, msg
    assert "系统权限" in msg, msg


def test_user_text_never_leaks_the_path():
    """给用户的话里不许出现本机路径（带同事真名，会跟着截图转出去）。"""
    bin_path = r"C:\Users\a-user\.carher-npm-global\claude.cmd"
    msg = courier(bin_path, FileNotFoundError(2, bin_path)).ensure()
    assert "a-user" not in msg and "C:\\" not in msg, msg


@pytest.mark.parametrize(("path", "want"), [
    ("claude", "claude"),
    ("/opt/homebrew/bin/claude", "claude"),
    (r"C:\Users\x\claude.cmd", "claude.cmd"),
    (r"C:/mixed\sep/claude.exe", "claude.exe"),
    ("", ""),
])
def test_basename_any_os(path, want):
    assert _basename_any_os(path) == want
