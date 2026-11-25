"""11-16：发版闸——安装包里嵌的 bridge 文件必须 == 仓库源码（拆包镜像发版把「新会话」冲掉过）。"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHECK = os.path.join(HERE, "..", "scripts", "check_package_matches_source.py")


def _run(tmp_path, pkg_body):
    src = tmp_path / "src"
    (src / "bridge").mkdir(parents=True)
    (src / "bridge" / "a.py").write_text("print('new')\n")
    tpl = tmp_path / "install-mac.sh"
    tpl.write_text("cat <<'EOF'\n@@EMBED bridge/a.py@@\nEOF\n")
    pkg = tmp_path / "feishu-agent-mac.command"
    pkg.write_text(pkg_body)
    return subprocess.run([sys.executable, CHECK, str(pkg), str(tpl), str(src)], capture_output=True, text=True)


def test_package_built_from_current_source_passes(tmp_path):
    assert _run(tmp_path, "cat <<'EOF'\nprint('new')\nEOF\n").returncode == 0      # 阳性对照


def test_package_with_stale_bridge_file_is_refused(tmp_path):
    res = _run(tmp_path, "cat <<'EOF'\nprint('old')\nEOF\n")
    assert res.returncode == 1 and "bridge/a.py" in res.stderr


def test_publish_script_runs_the_gate():
    pub = open(os.path.join(HERE, "..", "..", "..", "feishu-agent-oneclick", "publish-update.sh"), encoding="utf-8").read()
    assert "check_package_matches_source.py" in pub
