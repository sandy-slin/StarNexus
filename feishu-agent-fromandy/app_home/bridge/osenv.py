"""跨平台的三件事：单实例锁、批准目录、二维码。

🔴 **这个文件原来叫 `platform.py`，撞了标准库。** 2026-11-03 用户装机时炸了：
安装脚本用 `python bridge/register.py` 这种**脚本方式**跑，Python 会把脚本所在目录
（`bridge/`）放进 `sys.path` 最前面 ⇒ `import platform` 拿到的是这个文件 ⇒
pycryptodome 调 `platform.architecture()` 时 `AttributeError`，整个 lark_oapi 都 import 不进来。

包内 `from .platform import` 是相对导入、不受影响，所以 105 条测试全绿 ——
**测试绿不代表装机能跑**，因为测试走的是包导入，安装器走的是脚本导入。
`tests/test_no_stdlib_shadowing.py` 现在盯着这条，新增模块别再撞标准库名。

分出来是因为 Windows 上这三样都没有 Unix 对应物：

1. **单实例锁**：Windows 没有 `fcntl.flock`。这不是可选项 —— 飞书长连接是集群模式、
   随机投递，同一个应用跑两个进程会抢消息，表现是"机器人时好时坏"，极难排查。
   Windows 用 `msvcrt.locking`；两边都用"锁文件 + 进程还活着"双判，因为崩溃残留的
   锁文件不该永久挡住下次启动。

2. **常驻**：launchd 换成计划任务（Task Scheduler）。关键差别是 launchd 的
   `KeepAlive` 自带"退出就拉起"，而计划任务没有等价物 ⇒ Windows 侧必须自己包一层
   重启循环，否则看门狗那个 `os._exit(75)` 之后就再也起不来了。

3. **二维码**：Windows 终端默认字体对半角方块渲染很糟，常见症状是码糊成一片扫不出来。
   所以 Windows 上优先给链接、二维码降级为可选。
"""

from __future__ import annotations

import os
import sys


IS_WINDOWS = sys.platform.startswith("win")


class SingleInstanceError(RuntimeError):
    """已经有一个实例在跑。"""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我们的
    return True


def acquire_single_instance(path: str | None = None):
    """拿到单实例锁，拿不到抛 SingleInstanceError。

    返回的句柄必须一直持有 —— 被 GC 掉锁就释放了。
    """
    path = path or os.path.expanduser("~/.feishu-agent-bridge.lock")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    # 先看锁文件里记的进程还在不在。在 ⇒ 直接拒绝并报出 PID，方便用户去停掉；
    # 不在 ⇒ 是崩溃残留，继续往下抢锁（不能让残留文件永久挡住启动）。
    existing = ""
    try:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read().strip()
    except OSError:
        pass

    handle = open(path, "r+" if os.path.exists(path) else "w+", encoding="utf-8")
    try:
        if IS_WINDOWS:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        hint = ""
        if existing.isdigit() and _pid_alive(int(existing)):
            hint = f"（进程 {existing} 还在跑）"
        raise SingleInstanceError(
            f"已经有一个桥在跑了{hint}。\n"
            "飞书长连接是随机投递，两个进程同时在线消息会随机漂走，所以这里直接拒绝启动。"
        ) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def default_app_home() -> str:
    """程序数据目录。Windows 放 LOCALAPPDATA，别污染用户主目录。"""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "CarHerFeishuAgent")
    return os.path.expanduser("~/.carher-feishu-agent")


def hook_command(hook_path: str) -> str:
    """钩子的调用命令。

    Windows 上没有 `/usr/bin/env`，而且路径里几乎一定有空格
    （`C:\\Users\\张三\\AppData\\Local\\...`）⇒ 必须加引号。
    """
    if IS_WINDOWS:
        return f'python "{hook_path}"'
    return f"/usr/bin/env python3 {hook_path}"


def print_qr(url: str) -> bool:
    """终端里画二维码。返回是否真画出来了。

    Windows 终端默认字体渲染半角方块很糟，容易糊成一片扫不出来 ⇒
    那边直接返回 False，让调用方只给链接。
    """
    if IS_WINDOWS:
        return False
    try:
        import qrcode  # type: ignore

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
        return True
    except Exception:  # noqa: BLE001
        return False
