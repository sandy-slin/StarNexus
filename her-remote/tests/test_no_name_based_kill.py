"""没有任何地方按**名字**杀进程。

## 为什么这条要写成测试

规则本来只写在规格文档里（「开发期间也不许 pkill / killall 按名字杀进程」）。
2026-11-04 一个晚上，**两个** agent 在赶时间时反射性地踩了它：
一个 `pkill -f "mcp serve"`，一个 `pkill -f "pytest tests/test_fleet_codex.py"`。
两次都没造成损失，但那是运气，不是模式设计对了。

🔴 **为什么 `pkill -f <很具体的字符串>` 也不安全**：命令行里出现这个词的**任何**进程都会中。
主会话实测：一条只是 `grep "pytest tests/test_fleet_codex.py"` 的审计命令，
自己就在 `ps` 里匹配到 2 个 zsh 包装进程 —— 因为这个环境把每条命令都包在
`/bin/zsh -c …` 里，命令原文进了它的 args。也就是说：同事的会话里只要有人提到
同一个路径，就有资格被这条 pkill 杀掉。

本仓库唯一允许的杀法：`FleetJobs.signal()` —— 它只杀**自己 Popen 出来**的 pid，
而且核对 lstart 防 pid 复用（见 `bridge/fleet/model.py`）。`relay.py` 是正确样板：
信使 pid 登记进 FleetJobs，只杀那一个。

规则写进代码，才不会在下一次赶时间时被忘掉。
"""

from __future__ import annotations

import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_REPO = os.path.dirname(os.path.dirname(_ROOT))

# 按名字/模式杀进程的各种写法
_NAME_KILL = re.compile(
    r"\b(pkill|killall)\b"
    r"|\bkill\s+\$\(\s*pgrep"  # kill $(pgrep …)
    r"|\bpgrep[^\n|]*\|[^\n]*\bkill\b"  # pgrep … | xargs kill
    r"|\bkillall5\b",
)
# 注释和文档里提到它是**允许的**（本文件、以及代码里写「绝不 pkill」那种红字）
# 注释行，或者**同一行里正在禁止它**（比如给用户的提示写「别用 pkill 按名字杀」）
_MENTION_OK = re.compile(
    r"^\s*(#|\*|-|\"\"\"|'''|\|)"
    r"|绝不|不许|不要|别用|禁止|违反|forbid|never"
)

_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}


def _scan(root: str, exts: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(exts) or name == os.path.basename(__file__):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    lines = handle.readlines()
            except OSError:
                continue
            for number, line in enumerate(lines, 1):
                if not _NAME_KILL.search(line):
                    continue
                if _MENTION_OK.search(line):
                    continue  # 注释里提到（多半是在禁止它）
                hits.append(f"{os.path.relpath(path, _REPO)}:{number}: {line.strip()[:120]}")
    return hits


def test_bridge_code_has_no_name_based_kill():
    """桥的代码里一处都不许有。唯一的杀法是 FleetJobs.signal（只杀自己起的 pid）。"""
    hits = _scan(os.path.join(_ROOT, "bridge"), (".py",))
    assert not hits, "按名字杀进程（会误伤别人的会话）：\n" + "\n".join(hits)


def test_tests_and_scripts_have_no_name_based_kill():
    """测试和脚本里也不许 —— 它们在用户的真机上跑。"""
    hits = _scan(os.path.join(_ROOT, "tests"), (".py",))
    hits += _scan(os.path.join(_ROOT, "scripts"), (".py", ".sh"))
    assert not hits, "按名字杀进程：\n" + "\n".join(hits)


def test_installers_have_no_name_based_kill():
    """一键安装 / 卸载脚本同理：卸载要停服务，必须按我们自己记下的 pid 或 launchctl 标签。"""
    oneclick = os.path.join(_REPO, "feishu-agent-oneclick")
    if not os.path.isdir(oneclick):
        return
    hits = _scan(os.path.join(oneclick, "src"), (".sh", ".ps1"))
    assert not hits, "安装器里按名字杀进程：\n" + "\n".join(hits)


def test_fleet_signal_path_checks_ownership():
    """阳性对照：允许的那条路**必须**查「是不是我们起的」。

    不然这条测试只是把问题从 pkill 挪到了 os.kill。
    """
    from bridge.fleet.model import FleetJobs

    killed: list = []
    jobs = FleetJobs(lstart_of=lambda pid: "Tue Nov  4 10:00:00 2026", kill=lambda pid, sig: killed.append(pid))

    # 没登记过的 pid：拒绝，且根本不调 kill
    res = jobs.signal(99999)
    assert not res.ok and killed == []

    # 登记过的：放行
    job = jobs.register_spawned(4242, "claude", "sid-1")
    assert job is not None
    assert jobs.signal(4242).ok and killed == [4242]


def test_relay_courier_is_killed_by_pid_not_name():
    """relay 的信使是常驻进程，收它必须按 pid（它的命令行里有用户的话，按名字杀最危险）。"""
    path = os.path.join(_ROOT, "bridge", "fleet", "relay.py")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        src = handle.read()
    assert "jobs.forget" in src or "FleetJobs" in src, "信使 pid 没有登记进 FleetJobs"
    # 按行扫 + 同一套「注释里提到不算」的过滤（docstring 里正写着「绝不按名字 pkill」）
    hits = [h for h in _scan(os.path.dirname(path), (".py",)) if "relay.py" in h]
    assert not hits, "relay.py 里有按名字杀进程：\n" + "\n".join(hits)
