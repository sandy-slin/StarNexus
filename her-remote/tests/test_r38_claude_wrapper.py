"""11-18：PATH 里的 claude 可能是转发脚本（wrapper），照它写开机自启 ⇒ 后台起不动 claude。

现场：本机装了 cmux，它往 PATH 最前面放 ~/.cmuxterm/cmux-cli-shims/<id>/claude，
脚本最后把自己所在目录从 PATH 剔掉再 exec claude ⇒ 在服务那份 PATH（只有自己 + 四个固定目录）
下必然 rc=127。用户看到的是「飞书里能聊，一让它干活就答不出来」。
"""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[3] / "feishu-agent-oneclick" / "src"
MAC = SRC / "install-mac.sh"
WIN = SRC / "install-windows.ps1"


def _mac() -> str:
    return MAC.read_text(encoding="utf-8")


def test_mac_probes_candidate_under_the_service_path():
    """判据必须是「拿服务那份 PATH 真跑一次」，不是看文件在不在 / 是不是脚本。"""
    s = _mac()
    assert "_carher_claude_runs()" in s, "mac 侧没有「真跑一次」的判据函数"
    fn = s[s.index("_carher_claude_runs()") : s.index("if [ -n \"$CLAUDE_BIN\" ] && ! _carher_claude_runs")]
    assert 'env -i HOME="$HOME"' in fn, "探活没用干净环境 ⇒ 会被交互 shell 的 PATH 带偏"
    assert "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin" in fn, "探活用的不是服务那份 PATH"
    assert "--version" in fn


def test_mac_only_switches_when_the_current_one_fails():
    """跑得通就不换 —— 少改一分少错一分。"""
    s = _mac()
    assert '! _carher_claude_runs "$CLAUDE_BIN"' in s, "没有「只在跑不通时才换」这个前提"


def test_mac_switch_rewrites_path_so_plist_follows():
    """换了之后必须同时改 PATH，否则 plist 里写的还是旧目录。"""
    s = _mac()
    blk = s[s.index('if [ -n "$CLAUDE_BIN" ] && ! _carher_claude_runs') :]
    blk = blk[: blk.index("\nPY=")] if "\nPY=" in blk else blk[:2000]
    assert 'CLAUDE_BIN="$_c"' in blk
    assert 'PATH="$(dirname "$_c"):$PATH"' in blk, "换了 claude 却没改 PATH ⇒ plist 还是旧的"
    assert "转发脚本" in blk, "没告诉用户发生了什么"


def test_windows_has_the_same_shape():
    s = WIN.read_text(encoding="utf-8")
    assert "function Test-CarherClaudeRuns" in s
    assert "$claudeBin" in s
    # 探活、显示、写开机自启都要用换过之后那个
    assert "Start-Process -FilePath $claudeBin" in s, "Windows 探活还在用没换过的那个"
    assert 'Ok "OK claude 在 $claudeBin"' in s
    assert "$claudeDir = if ($claudeBin)" in s, "写开机自启的目录还是从没换过的那个算的"


@pytest.mark.skipif(shutil.which("bash") is None, reason="没有 bash")
def test_real_wrapper_gets_bypassed():
    """阳性对照 + 真跑：造一个和 cmux 同形状的转发脚本，确认这段逻辑能绕过它。"""
    s = _mac()
    start = s.index("_carher_claude_runs()")
    end = s.index("PY=\"\"", start)
    logic = s[start:end]

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        shim_dir, real_dir = td / "shims", td / "real"
        shim_dir.mkdir(), real_dir.mkdir()
        # 真的 claude
        real = real_dir / "claude"
        # 🔴 不要带括号：sh 里 `echo 9.9.9 (Claude Code)` 是语法错 ⇒ rc≠0 ⇒ 候选被判成也跑不通 ⇒ 假红
        real.write_text("#!/bin/sh\necho 9.9.9\n")
        real.chmod(0o755)
        # 转发脚本：照 cmux 的形状，把自己所在目录从 PATH 剔掉再 exec claude
        shim = shim_dir / "claude"
        shim.write_text(
            "#!/bin/sh\n"
            f'PATH="$(echo \"$PATH\" | tr : \"\\n\" | grep -v \"^{shim_dir}$\" | paste -sd: -)"\n'
            'export PATH\nexec claude "$@"\n'
        )
        shim.chmod(0o755)

        # 🔴 候选必须在跑脚本**之前**就位（第一版写在后面 ⇒ 脚本跑时目录还是空的 ⇒ 假红）
        (td / ".local" / "bin").mkdir(parents=True)
        (td / ".local" / "bin" / "claude").write_bytes(real.read_bytes())
        (td / ".local" / "bin" / "claude").chmod(0o755)

        script = f"""
set -u   # 🔴 不要 set -e：探活故意会返回非 0（127），set -e 会让脚本在那一行直接退出 ⇒ 假红
HOME={td}
PATH="{shim_dir}:/usr/bin:/bin"
CLAUDE_BIN="$(command -v claude 2>/dev/null || true)"
{logic}
echo "RESULT=$CLAUDE_BIN"
"""

        out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        line = [x for x in out.stdout.splitlines() if x.startswith("RESULT=")][-1]
        got = line.split("=", 1)[1]
        assert got != str(shim), "没绕过转发脚本 —— 开机自启还会指向它"
        assert got == str(td / ".local" / "bin" / "claude"), got
        assert "转发脚本" in out.stdout, "没把换了这件事告诉用户"
