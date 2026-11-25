"""第 30 轮：feishu-agent-oneclick/release.sh（发版一条命令）。

🔴 绝不真发布：只跑 --dry-run，或者 `RELEASE_SH_LIB=1 source` 只加载函数、把网络函数换成桩。
"""

from __future__ import annotations

import os
import subprocess

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
_SCRIPT = os.path.join(_ONECLICK, "release.sh")


def bash(script: str, cwd: str | None = None) -> subprocess.CompletedProcess:
    # PATH 里换掉 ssh/scp/curl/lark-cli：万一桩没盖住，也打不出去
    env = dict(os.environ, RELEASE_SH_LIB="1")
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env,
                          cwd=cwd, timeout=60)


def lib(body: str) -> str:
    stub_bin = (
        'STUB="$(mktemp -d)"; for c in ssh scp curl lark-cli; do '
        'printf "#!/bin/sh\\necho STUB-CALLED-$c >&2; exit 97\\n" > "$STUB/$c"; chmod +x "$STUB/$c"; done; '
        'export PATH="$STUB:$PATH"; '
    )
    return f'{stub_bin} source "{_SCRIPT}"; set +e; {body}'


def test_syntax():
    assert subprocess.run(["bash", "-n", _SCRIPT]).returncode == 0


def test_no_var_glued_to_fullwidth_char():
    """macOS bash 3.2 会把紧贴的全角字符首字节吞进变量名 ⇒ set -u 下直接炸（本文件第一版就踩了）。"""
    import re
    pat = re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")
    text = open(_SCRIPT, encoding="utf-8").read()
    assert not [ln for ln in text.splitlines() if pat.search(ln)]


def test_dry_run_lists_all_seven_steps_in_order():
    env = {k: v for k, v in os.environ.items() if k != "RELEASE_SH_LIB"}
    res = subprocess.run(["bash", _SCRIPT, "--dry-run", "--allow-dirty"], capture_output=True,
                         text=True, env=env, timeout=60)
    assert res.returncode == 0, res.stderr
    out = res.stdout
    markers = ["▶ 1.", "▶ 2.", "▶ 3.", "▶ 4.", "▶ 5.", "▶ 6.", "▶ 7."]
    pos = [out.find(m) for m in markers]
    assert all(p >= 0 for p in pos), out
    assert pos == sorted(pos)
    for word in ("build.sh", "publish-update.sh", "refresh-doc-attachments.sh", "latest.json"):
        assert word in out
    assert out.count("dry-run，不执行") == 7


def test_unknown_flag_rejected():
    res = subprocess.run(["bash", _SCRIPT, "--publish-for-real-please"], capture_output=True, text=True, timeout=30)
    assert res.returncode != 0


# ── 工作区脏 ──────────────────────────────────────────────────────────
@pytest.fixture
def fake_repo(tmp_path):
    repo = tmp_path / "repo"
    for d in ("tools/feishu-agent-bridge/bridge", "feishu-agent-oneclick/src"):
        (repo / d).mkdir(parents=True)
    (repo / "tools/feishu-agent-bridge/bridge/app.py").write_text("x = 1\n")
    (repo / "feishu-agent-oneclick/src/install-mac.sh").write_text("echo hi\n")
    (repo / "other.txt").write_text("a\n")
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    g("init", "-q")
    g("-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def test_clean_repo_passes(fake_repo):
    (fake_repo / "other.txt").write_text("改了别处，不管\n")
    res = bash(lib(f'check_clean "{fake_repo}"; echo rc=$?'))
    assert "rc=0" in res.stdout, res.stdout + res.stderr


@pytest.mark.parametrize("rel", ["tools/feishu-agent-bridge/bridge/app.py",
                                 "feishu-agent-oneclick/src/new-file.sh"])
def test_dirty_repo_refused(fake_repo, rel):
    (fake_repo / rel).write_text("dirty\n")
    res = bash(lib(f'check_clean "{fake_repo}"; echo rc=$?'))
    assert "rc=1" in res.stdout and os.path.basename(rel) in res.stdout


def test_step_clean_refuses_without_allow_dirty_and_shouts_with_it(fake_repo):
    (fake_repo / "tools/feishu-agent-bridge/bridge/app.py").write_text("dirty\n")
    res = bash(lib(f'REPO="{fake_repo}"; step_clean; echo rc=$?'))
    assert "rc=1" in res.stdout and "--allow-dirty" in res.stdout
    res = bash(lib(f'REPO="{fake_repo}"; ALLOW_DIRTY=1; step_clean; echo rc=$?'))
    assert "rc=0" in res.stdout and "⚠️" in res.stdout


# ── 凭据检查 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("content", [
    'APP_ID="cli_a1b2c3d4e5f60718"\n',
    'app_secret = "AbCdEfGhIjKlMnOpQrStUvWx"\n',
    'app_secret="AbCdEfGhIjKlMnOpQrStUvWx"\n',
])
def test_cred_check_catches_fake_creds(tmp_path, content):
    f = tmp_path / "feishu-agent-mac.command"
    f.write_text("#!/bin/bash\n" + content)
    clean = tmp_path / "feishu-agent-windows.bat"
    clean.write_text("echo ok\n")
    res = bash(lib(f'check_creds "{clean}" "{f}"; echo rc=$?'))
    assert "rc=1" in res.stdout and "feishu-agent-mac.command" in res.stdout
    # 不回显凭据本身
    assert "AbCdEfGhIjKlMnOpQrStUvWx" not in res.stdout + res.stderr
    assert "cli_a1b2c3d4e5f60718" not in res.stdout + res.stderr


def test_cred_check_passes_placeholders(tmp_path):
    f = tmp_path / "pkg"
    f.write_text('APP_ID="cli_xxx"\napp_secret = ""\nBUILD_ID = "20261108-c8260b5e"\n')
    res = bash(lib(f'check_creds "{f}"; echo rc=$?'))
    assert "rc=0" in res.stdout


def test_cred_rule_matches_refresh_doc_attachments():
    """规则抄 refresh-doc-attachments.sh —— 两处不许漂移。"""
    ref = open(os.path.join(_ONECLICK, "refresh-doc-attachments.sh"), encoding="utf-8").read()
    res = bash(lib('printf "%s" "$CRED_RE"'))
    assert f"grep -qE '{res.stdout}'" in ref


# ── 三处回读 ──────────────────────────────────────────────────────────
WANT = "20261108-c8260b5e"


def verify(latest=WANT, mac=WANT, mac_doc=WANT, win_doc=WANT, sizes="100 200", mac_oneliner=True):
    body = f'''
fetch_latest_id()  {{ echo "{latest}"; }}
fetch_mac_pkg_id() {{ echo "{mac}"; }}
fetch_doc_id() {{ case "$2" in *mac*) echo "{mac_doc}";; *) echo "{win_doc}";; esac; }}
mac_doc_has_oneliner() {{ {"true" if mac_oneliner else "false"}; }}
verify_three "{WANT}" {sizes}; echo rc=$?
'''
    return bash(lib(body))


def test_verify_all_match():
    res = verify()
    assert "rc=0" in res.stdout and res.stdout.count("✓") == 4, res.stdout
    assert "STUB-CALLED" not in res.stderr


@pytest.mark.parametrize("kw,where", [
    ({"latest": "20261107-877f027c"}, "latest.json"),
    ({"latest": ""}, "latest.json"),
    ({"mac": "20261107-877f027c"}, "mac 包"),
    # 11-09 起《Mac 安装》不挂附件、改成一行命令 ⇒ 核的是正文里那行命令还在
    ({"mac_oneliner": False}, "Mac 安装"),
    ({"win_doc": ""}, "feishu-agent-windows.bat"),
    ({"win_doc": "size:999"}, "feishu-agent-windows.bat"),
])
def test_verify_mismatch_fails(kw, where):
    res = verify(**kw)
    assert "rc=1" in res.stdout
    bad = [ln for ln in res.stdout.splitlines() if "✗" in ln]
    assert len(bad) == 1 and where in bad[0], res.stdout


def test_verify_falls_back_to_size_when_doc_download_unavailable():
    res = verify(win_doc="size:200")
    assert "rc=0" in res.stdout and "退回比字节数" in res.stdout


def test_step_failure_stops_and_names_the_step():
    body = '''
step_clean() { return 0; }
step_build() { echo BUILD-RAN; return 0; }
step_creds() { return 1; }
step_publish() { echo PUBLISH-RAN; }
step_docs() { echo DOCS-RAN; }
step_verify() { echo VERIFY-RAN; }
step_summary() { echo SUMMARY-RAN; }
set -e; main
'''
    res = bash(lib(body))
    assert res.returncode != 0
    assert "BUILD-RAN" in res.stdout and "PUBLISH-RAN" not in res.stdout
    assert "第 3 步失败" in res.stderr
