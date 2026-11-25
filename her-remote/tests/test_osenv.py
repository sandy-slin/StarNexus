"""跨平台层测试。

单实例锁是核心：飞书长连接随机投递，两个进程同时在线 ⇒ 消息随机漂走、
机器人"时好时坏"。这条坏了很难从现象反推，所以必须测死。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import pytest  # noqa: E402

from bridge.osenv import (  # noqa: E402
    IS_WINDOWS,
    SingleInstanceError,
    _pid_alive,
    acquire_single_instance,
    default_app_home,
    hook_command,
)


# ---------- 单实例锁 ----------


def test_first_acquire_succeeds():
    """阳性对照放最前：正常情况必须拿得到锁。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.lock")
        handle = acquire_single_instance(path)
        assert handle is not None
        handle.close()


def test_lock_file_records_pid():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.lock")
        handle = acquire_single_instance(path)
        try:
            with open(path, encoding="utf-8") as f:
                assert f.read().strip() == str(os.getpid())
        finally:
            handle.close()


def test_second_acquire_in_other_process_is_rejected():
    """真起第二个进程抢同一把锁 —— 同进程内再抢在某些平台上会成功，
    测不出真实场景，所以必须跨进程测。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.lock")
        handle = acquire_single_instance(path)
        try:
            code = (
                "import sys; sys.path.insert(0, %r)\n"
                "from bridge.osenv import acquire_single_instance, SingleInstanceError\n"
                "try:\n"
                "    acquire_single_instance(%r)\n"
                "    print('GOT')\n"
                "except SingleInstanceError:\n"
                "    print('REJECTED')\n"
            ) % (os.path.dirname(_HERE), path)
            proc = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True
            )
            assert "REJECTED" in proc.stdout, f"第二个进程竟然拿到了锁: {proc.stdout!r} {proc.stderr!r}"
        finally:
            handle.close()


def test_lock_is_reusable_after_release():
    """阳性对照：上一条的 REJECTED 必须来自锁真的被占，而不是永远失败。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.lock")
        first = acquire_single_instance(path)
        first.close()
        second = acquire_single_instance(path)
        assert second is not None
        second.close()


def test_stale_lock_from_dead_pid_does_not_block():
    """崩溃残留的锁文件不能永久挡住下次启动。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.lock")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("999999")  # 几乎不可能存在的 PID
        got = acquire_single_instance(path)
        assert got is not None
        got.close()


def test_error_message_explains_why():
    """报错要说清为什么不许跑两个 —— 否则用户只会以为是 bug。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "x.lock")
        handle = acquire_single_instance(path)
        try:
            code = (
                "import sys; sys.path.insert(0, %r)\n"
                "from bridge.osenv import acquire_single_instance, SingleInstanceError\n"
                "try:\n"
                "    acquire_single_instance(%r)\n"
                "except SingleInstanceError as e:\n"
                "    print(str(e))\n"
            ) % (os.path.dirname(_HERE), path)
            proc = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, text=True
            )
            assert "随机" in proc.stdout or "两个" in proc.stdout
        finally:
            handle.close()


def test_creates_parent_directory():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "deep", "nested", "x.lock")
        handle = acquire_single_instance(path)
        assert os.path.exists(path)
        handle.close()


# ---------- pid 存活判断 ----------


def test_self_pid_is_alive():
    assert _pid_alive(os.getpid()) is True


def test_absurd_pid_is_not_alive():
    assert _pid_alive(999999) is False


def test_nonpositive_pid_is_not_alive():
    for bad in (0, -1):
        assert _pid_alive(bad) is False


# ---------- 平台差异 ----------


def test_app_home_is_absolute():
    assert os.path.isabs(default_app_home())


def test_hook_command_quotes_paths_with_spaces():
    """Windows 路径几乎一定带空格（C:\\Users\\张三\\AppData\\...）。"""
    command = hook_command(r"C:\Users\Zhang San\AppData\hook.py")
    if IS_WINDOWS:
        assert command.count('"') == 2, "带空格的路径必须加引号"
    else:
        assert "hook.py" in command


def test_hook_command_mentions_python():
    assert "python" in hook_command("/tmp/hook.py").lower()


@pytest.mark.skipif(IS_WINDOWS, reason="Unix 专属分支")
def test_unix_hook_uses_env():
    assert hook_command("/tmp/h.py").startswith("/usr/bin/env python3")
