"""安装包带版本号 ⇒ 消掉「换包和换清单之间」那个窗口。

198 实测：`feishu-agent-windows.bat` mtime `12:42:34.638`、`latest.json` `12:42:39.723`
⇒ 这 **5.1 秒**里来查更新的客户端拿到「旧清单的 sha + 已经换好的新包」，校验必错。
现场：`…<机器>` 11-20 23:20 上报 `ValueError: 下载回来的安装包校验不过`（指纹 `856d29f0669c`），
它 23:20:07 正落在那次发版的窗口里。

调顺序消不掉（先换包 → 窗口在「清单还旧」；先换清单 → 窗口在「包还旧」，两头各一个）。
根治：包名带 build_id、**新旧并存**，`latest.json` 指哪个版本就下哪个文件 ⇒ 没有错位可言。

固定名照旧更新（一行命令安装的提示、飞书文档附件、「重新双击 feishu-agent-*」那些文案在用它）。
`download()` 那次重试（test_r39_download_retry）仍留着兜老客户端 —— 它们手里的旧桥还在读固定名。
"""

from __future__ import annotations

import os
import pathlib
import re
import textwrap

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))


@pytest.fixture(scope="module")
def publish() -> str:
    return pathlib.Path(os.path.join(_ONECLICK, "publish-update.sh")).read_text(encoding="utf-8")


def test_versioned_copies_are_made(publish):
    """两个安装包都要另存一份带 build_id 的副本。"""
    assert 'MAC_V="${MAC%.command}-${BUILD_ID}.command"' in publish
    assert 'WIN_V="${WIN%.bat}-${BUILD_ID}.bat"' in publish


def test_manifest_points_at_the_versioned_copy(publish):
    """🔴 关键一条：清单里的 `file` 必须是带版本号那份，不是固定名。"""
    i = publish.index('"build_id": "$BUILD_ID"')
    seg = publish[i:i + 700]
    assert '"mac": {"file": "$MAC_V"' in seg, "mac 还指着固定名 ⇒ 窗口还在"
    assert '"windows": {"file": "$WIN_V"' in seg, "windows 还指着固定名 ⇒ 窗口还在"


def test_fixed_names_still_published(publish):
    """固定名不许停更 —— 一行命令的提示和飞书文档附件都在用它。"""
    i = publish.index("▶ 传到 198…")
    seg = publish[i:i + 1200]
    assert 'for f in "$MAC" "$WIN"' in seg


def test_versioned_copy_is_atomic(publish):
    """copy 也要先 .part 再改名 —— 否则同事正好这一刻下载会拿到半个文件。"""
    i = publish.index("MAC_V=")
    seg = publish[i:i + 900]
    assert ".part && mv" in seg, "带版本号那份不是原子改名"


def test_readback_covers_both_names(publish):
    """回读要核固定名**和**带版本号那份：清单指的是后者，它下不下来才要紧。"""
    line = [l for l in publish.splitlines() if l.startswith("for pair in") and "MAC_SHA" in l][0]
    nxt = publish.splitlines()[publish.splitlines().index(line) + 1]
    both = line + nxt
    for v in ("$MAC:", "$WIN:", "$MAC_V:", "$WIN_V:"):
        assert v in both, f"回读漏了 {v}"


def test_dry_run_lists_the_versioned_names(publish):
    """dry-run 在 MAC_V 定义之前就 exit ⇒ 它得自己拼一次（否则列不出来）。"""
    i = publish.index("▶ dry-run")
    seg = publish[i:i + 700]
    assert "${MAC%.command}-${BUILD_ID}.command" in seg
    assert "${WIN%.bat}-${BUILD_ID}.bat" in seg


def test_download_reads_file_from_manifest():
    """桥按清单的 `file` 下载（不是写死名字）—— 带版本号这招才成立。"""
    import inspect

    from bridge.selfupdate import SelfUpdater
    src = inspect.getsource(SelfUpdater.download)
    # 源码里是 f"{BASE_URL}/{part['file']}"，按 `part` + `file` 两个片段断言，
    # 别去拼引号（第一版就是引号转义写错才假红的）
    assert "part" in src and "'file'" in src, src[:200]
    # 🔴 只看**真代码**：用 AST 剥掉 docstring 和注释。
    # 前两版都栽在这：先是引号转义写错，再是「排掉含三引号的行」排不掉 docstring 中间那几行
    # —— 而那里正引用了 `feishu-agent-windows.bat`（我自己写的说明）。
    import ast
    tree = ast.parse(textwrap.dedent(src))
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)):
        fn.body = fn.body[1:]          # 去掉 docstring
    code = ast.unparse(fn)             # unparse 不带注释
    assert "feishu-agent-windows.bat" not in code, "下载路径写死了文件名"


def test_retry_kept_for_old_clients():
    """老客户端手里的旧桥还在读固定名 ⇒ 那次重试不能撤。"""
    import inspect

    from bridge.selfupdate import SelfUpdater
    src = inspect.getsource(SelfUpdater.download)
    assert "for attempt in (1, 2):" in src
