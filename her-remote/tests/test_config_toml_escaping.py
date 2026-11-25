"""装机写出来的 config.toml 必须能被 tomllib 解析 —— 包括目录名里有 `\\` 或 `"`。

🔴 2026-11-05 实测的「绿色的失败」：macOS 的目录名允许反斜杠和双引号，而 TOML 的
基本字符串里这两个都是特殊字符。原来 `install-mac.sh` 把路径原样拼进
`workspaces = ["$WORKSPACE"]`：

    /tmp/back\\slash  ⇒ TOMLDecodeError Unescaped '\\' in a string
    /tmp/we"ird       ⇒ TOMLDecodeError Unclosed array

坏在**没有任何一层会说出真相**：装机第 6 步只看 `launchctl print` 成功（那只证明 job
注册了，不证明进程活着），启动期崩溃在日志里既不出现 `connected` 也不出现
`connect failed`，于是安装器落到「它还在重试连接，等两分钟」，doctor 第 8 项也报
「没有 ERROR」。用户拿到一个全程打绿、却永远不回消息的机器人。

Windows 侧早就转了（`install-windows.ps1` 的 `$wsToml`），Mac 侧漏了这一道。

这条测试查**行为**不查文案：拿安装器里那一行真正的转义代码跑 shell，再用真 tomllib
解析，断言解出来的路径和原路径逐字相等。
"""

from __future__ import annotations

import os
import subprocess
import tomllib

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick")
)
MAC_SRC = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN_SRC = os.path.join(_ONECLICK, "src", "install-windows.ps1")

# 第 3、4 项是阳性对照的反面：空格和中文在 TOML 里**不特殊**，转义不该动它们。
# 报它们等于把能正常用的人挡在门外。
PATHS = (
    r"/tmp/back\slash",
    '/tmp/we"ird',
    "/Users/x/我的 项目",
    r'/tmp/both\and"q',
    "/Users/liu/codes/carher-admin",
)


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _escape_like_installer(raw: str) -> str:
    """跑安装器里那一行真正的 sed，不是在测试里重写一遍它。"""
    script = (
        'WORKSPACE="$1"\n'
        "WORKSPACE_TOML=\"$(printf '%s' \"$WORKSPACE\" "
        "| sed -e 's/\\\\/\\\\\\\\/g' -e 's/\"/\\\\\"/g')\"\n"
        'printf %s "$WORKSPACE_TOML"\n'
    )
    done = subprocess.run(
        ["bash", "-c", script, "bash", raw],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("raw", PATHS)
def test_escaped_workspace_round_trips_through_tomllib(raw):
    """转义后写进 config.toml 的形状，解析回来必须**逐字**还是那个路径。"""
    escaped = _escape_like_installer(raw)
    text = f'[agent]\nworkspaces = ["{escaped}"]\ndefault_workspace = "{escaped}"\n'
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - 失败时要看见原文
        pytest.fail(f"{raw!r} 转义成 {escaped!r} 后仍解析不了：{exc}")
    assert parsed["agent"]["workspaces"] == [raw], (
        f"解出来的路径和原路径不一样：{parsed['agent']['workspaces']!r} != [{raw!r}]"
    )
    assert parsed["agent"]["default_workspace"] == raw


@pytest.mark.parametrize("raw", (r"/tmp/back\slash", '/tmp/we"ird'))
def test_unescaped_path_really_breaks_tomllib(raw):
    """阳性对照：不转义的话 tomllib 真的炸 —— 证明上面那条不是洁癖。"""
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads(f'workspaces = ["{raw}"]')


def test_mac_installer_writes_the_escaped_variable_not_the_raw_one():
    """`config.toml` 的那两行必须用转义后的变量 —— 改回原变量这条就红。"""
    if not os.path.exists(MAC_SRC):
        pytest.skip("模板不在")
    mac = _read(MAC_SRC)
    assert "WORKSPACE_TOML=" in mac, "Mac 侧没有做 TOML 转义这一步"
    for line in ('workspaces = ["$WORKSPACE_TOML"]',
                 'default_workspace = "$WORKSPACE_TOML"'):
        assert line in mac, f"config.toml 里缺这一行：{line}"
    # 只看**真代码行**：注释里为了讲清坑在哪，原样引用了那条坏写法。
    offenders = [
        f"{lineno}: {line.strip()}"
        for lineno, line in enumerate(mac.splitlines(), 1)
        if not line.lstrip().startswith("#")
        and 'workspaces = ["$WORKSPACE"]' in line
    ]
    assert not offenders, "还在往 config.toml 里拼未转义的路径：\n" + "\n".join(offenders)


def test_windows_installer_still_escapes_backslashes():
    """Windows 侧本来是对的，别在后续改动里丢掉（路径里必然有反斜杠）。"""
    if not os.path.exists(WIN_SRC):
        pytest.skip("模板不在")
    win = _read(WIN_SRC)
    assert "$wsToml" in win and "-replace" in win, "Windows 侧的反斜杠转义不见了"
    assert 'workspaces = ["$workspace"]' not in win
