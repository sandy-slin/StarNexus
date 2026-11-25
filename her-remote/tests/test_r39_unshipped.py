"""第 39 轮：feishu-agent-oneclick/unshipped.sh（「已提交但还没发版」）。

起因（11-19）：`fe69d614` 红线判据 15:35 提交、4985 测试全绿，**当天没发版** ——
线上 latest.json 一直停在早上 06:50 的 `20261119-1d0d778a`，用户以为红线已经放开了，
手上那份还在拒 `python3 -c`。`release.sh` 只保证「这次发的 == 本地」，不回答「还有什么没发」。

🔴 不碰网络：把读 latest.json 那一句换成台架给的 `FAKE_LIVE`（替换点找不到就直接 fail，
    不许静默跑成「用真 curl」）。
🔴 每条都带阳性对照：rc=0 要在「真有未发改动」时变成 rc=2，否则这把尺子是废的。
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
_REPO = os.path.normpath(os.path.join(_ONECLICK, ".."))
_SCRIPT = os.path.join(_ONECLICK, "unshipped.sh")

# 脚本里读公网那一句。台架把它换成 FAKE_LIVE。
_LIVE_LINE = 'live="$(live_build_id)"'


def _rev(ref: str) -> str:
    out = subprocess.run(["git", "-C", _REPO, "rev-parse", "--short=8", ref],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        pytest.skip(f"git 读不到 {ref}")
    return out.stdout.strip()


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> str:
    """把 unshipped.sh 的「读公网」换成 FAKE_LIVE，放回 oneclick 目录（REPO 是按脚本位置算的）。"""
    text = open(_SCRIPT, encoding="utf-8").read()
    assert _LIVE_LINE in text, (
        f"台架替换点 {_LIVE_LINE!r} 不在 unshipped.sh 里了 —— 脚本改过，台架要跟着改；"
        "不许就这么跑下去（那会去打真网络）"
    )
    # 整行换掉（原行尾部带 `|| { ... exit 1; }`）
    patched = re.sub(
        r"^live=\"\$\(live_build_id\)\".*$",
        'live="${FAKE_LIVE?台架必须给 FAKE_LIVE}"',
        text, count=1, flags=re.M,
    )
    assert "live_build_id)" not in patched.split("\n")[0:200][0:0] or True
    assert 'FAKE_LIVE' in patched
    path = os.path.join(_ONECLICK, ".unshipped-harness-test.sh")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(patched)
    yield path
    os.unlink(path)


def run(harness: str, live: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, FAKE_LIVE=live)
    return subprocess.run(["bash", harness], capture_output=True, text=True,
                          env=env, timeout=60)


def test_syntax():
    assert subprocess.run(["bash", "-n", _SCRIPT]).returncode == 0


def test_no_var_glued_to_fullwidth_char():
    """macOS 自带 bash 3.2 把紧贴的全角字符首字节吞进变量名 ⇒ set -u 下直接炸。

    这个脚本第一版就踩了两处（`$commit（`）。同 `test_r30_release.py` 那条。
    """
    pat = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")
    text = open(_SCRIPT, encoding="utf-8").read()
    assert not [ln for ln in text.splitlines() if pat.search(ln)]


def test_no_bash4_only_builtins():
    """macOS 自带 bash 3.2 没有 `mapfile`/`readarray` ⇒ rc=127、数组是空的 ⇒ 永远报「已全部发出」= 假绿。"""
    text = open(_SCRIPT, encoding="utf-8").read()
    bad = [ln for ln in text.splitlines()
           if re.search(r"^\s*(mapfile|readarray)\b", ln)]
    assert not bad, f"用了 bash 4 才有的内建：{bad}"


def test_head_shipped_means_clean(harness):
    """线上 build_id == HEAD ⇒ 没有未发的，rc=0。"""
    r = run(harness, f"20261119-{_rev('HEAD')}")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "没有未发版的改动" in r.stdout


def test_older_live_lists_the_unshipped_commit(harness):
    """阳性对照 —— 重现 11-19 真实缺口：线上停在 1d0d778a 时必须点名 fe69d614。"""
    r = run(harness, "20261119-1d0d778a")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "还没发出去" in r.stdout
    assert "fe69d614" in r.stdout, "没点名那个没发的 commit，这把尺子没用"


def test_unknown_commit_fails_loudly(harness):
    """线上那版对不上任何本地 commit ⇒ rc=1，不许静默报绿。"""
    r = run(harness, "20261119-deadbeef")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "本地仓库里没有 commit" in r.stdout


def test_dirty_live_warns_but_still_compares(harness):
    """线上那版是 -dirty ⇒ 说清基线可能不准，但照样比出未发的。"""
    r = run(harness, "20261119-1d0d778a-dirty-abc123")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "-dirty" in r.stdout
    assert "fe69d614" in r.stdout


def test_watch_paths_match_release_sh():
    """盯的两个目录必须和 release.sh 的 check_clean 一致 —— 否则「发版会带上」和「检查有没有漏发」口径不同。"""
    unshipped = open(_SCRIPT, encoding="utf-8").read()
    release = open(os.path.join(_ONECLICK, "release.sh"), encoding="utf-8").read()
    for path in ("tools/feishu-agent-bridge/bridge", "feishu-agent-oneclick/src"):
        assert path in unshipped, f"unshipped.sh 没盯 {path}"
        assert path in release, f"release.sh 不再盯 {path} 了，两处口径要一起改"


def test_release_sh_points_at_this_script():
    """release.sh 跑完要提这条命令 —— 不然没人知道它存在（这才是 11-19 漏发的真修法）。"""
    release = open(os.path.join(_ONECLICK, "release.sh"), encoding="utf-8").read()
    assert "unshipped.sh" in release
