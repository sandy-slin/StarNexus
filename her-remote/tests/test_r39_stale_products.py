"""`scripts/stale-products.sh`：扫全部三条线的「产物 vs 源码」。

今天同一个病犯了四次（卸载脚本没 build、反引号改 src 没重建、红线判据忘发版、
`carher_lark_setup.js` 改好而 codex/claude 四个产物全是旧的）。
每次补的检查都只管一条路 —— `unshipped.sh` 只看 her remote 的 `bridge/` 和 `src/`。

判据：**源码（rstrip 后）是不是产物的子串**。`build.sh` 把 `@@EMBED <file>@@`
原样换成那个文件的内容（三条线都做 `.rstrip("\\n")`）⇒ 那段必然整段出现在产物里。

🔴 这是第五版判据，前四版都栽在「自己造尺子」上（见脚本里那段红字）。
"""

from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, "..", "..", ".."))
_TOOL = os.path.join(_REPO, "scripts", "stale-products.sh")


def _run():
    return subprocess.run(["bash", _TOOL], capture_output=True, text=True,
                          cwd=_REPO, timeout=180)


def test_syntax():
    assert subprocess.run(["bash", "-n", _TOOL]).returncode == 0


def test_covers_all_three_lines():
    out = _run().stdout
    assert "三条线" in out
    # 核到的处数要是真数字、且够大（134 处是 11-21 的实测；少于 100 说明路径解析又瞎了）
    import re
    m = re.search(r"核了 (\d+) 处", out)
    assert m, out
    assert int(m.group(1)) >= 100, f"只核到 {m.group(1)} 处 ⇒ 路径解析有问题：\n{out}"


def test_green_on_clean_tree():
    r = _run()
    assert r.returncode == 0, f"产物过期或量具报错：\n{r.stdout}\n{r.stderr}"
    assert "产物和源码一致" in r.stdout


def test_nothing_skipped():
    """🔴「没核到」比「过期」更危险 —— 它会让人以为全绿。"""
    out = _run().stdout
    assert "这些没核到" not in out, out


def test_catches_a_stale_product(tmp_path):
    """🔴 阳性对照：改一个源码不重建，必须变红。

    改 `carher_dl_cache.sh`（codex/claude 共用那份）⇒ 两条线的产物同时报红，
    这正是今天该抓到却漏了的场景。
    """
    src = pathlib.Path(_REPO) / "codex-oneclick" / "src" / "carher_dl_cache.sh"
    backup = src.read_text(encoding="utf-8")
    try:
        src.write_text(backup + "\n# 阳性对照\n", encoding="utf-8")
        r = _run()
        assert r.returncode == 2, f"改了源码却没报红：\n{r.stdout}"
        assert "产物是旧的" in r.stdout
        assert "codex-mac.command" in r.stdout
        assert "claude-mac.command" in r.stdout, "共用源码改了，另一条线也该红"
    finally:
        src.write_text(backup, encoding="utf-8")
    assert _run().returncode == 0, "还原后没回绿"


def test_release_sh_points_at_it():
    """发版收尾要提它 —— 不然没人知道它存在（这才是漏发的真修法）。"""
    rel = pathlib.Path(_REPO, "feishu-agent-oneclick", "release.sh").read_text(encoding="utf-8")
    assert "stale-products.sh" in rel
