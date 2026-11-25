"""产物必须带版本号。

## 为什么存在

2026-11-05 实测：`build.sh` 里版本/时间/commit **零命中** —— 这个产品没有版本号。
而 `bridge/status.py` 的 `HEALTH_FIELDS` 第一项就是 `("version", "🏷 版本 {}")`，
现在填的是**代码指纹**（`bridge/**.py` 内容的 sha256 前 8 位）。

指纹能答的问题只有一个：「两台机器是不是同一份码」。
它答不出「新还是旧」—— 而远程支持第一句要问的恰恰是这个。
同事发来一张诊断卡，你看不出他手里是哪一版，就可能去复查一个上周已经修好的 bug。

## 形状

`<commit 日期>-<短 commit>`，例 `20261105-ca34375d`；工作区脏时加 `-dirty`。

两个决定都有理由，不是随手定的：

- 日期取 **commit 的**日期，不是构建日期。否则同一份代码今天构建和明天构建会得出
  两个号，而它们装出来的东西逐字节相同 ⇒ 又变成一个答不出「新还是旧」的号。
- `-dirty` 必须标。带未提交改动出的产物不对应任何一个 commit，
  不标出来它就会伪装成某个干净版本 —— 那是远程支持里最坏的一种谎。

🔴 这组测试**不跑 `build.sh`**（出产物要用户定时间）。查的是：模板里有占位符、
build.sh 会注入、以及注入那段逻辑本身喂进各种 git 状态会得出什么。
"""

from __future__ import annotations

import os
import re
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
_BRIDGE_ROOT = os.path.dirname(_HERE)
_ONECLICK = os.path.normpath(os.path.join(_BRIDGE_ROOT, "..", "..", "feishu-agent-oneclick"))
BUILD = os.path.join(_ONECLICK, "build.sh")
MAC_SRC = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN_SRC = os.path.join(_ONECLICK, "src", "install-windows.ps1")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------- 1. 模板留了占位符 ----------


def test_both_installers_carry_the_placeholder():
    """两侧都要有 —— 只给一侧的话 Windows 用户的诊断卡上没有版本号。

    🔴 判据是**那行赋值**，不是「文件里出现过这个串」。2026-11-05 变异验证实测：
    把赋值改成 `BUILD_ID = ""`，而解释它的那句注释里还留着 `@@BUILD_ID@@` ⇒
    松判据（`"@@BUILD_ID@@" in text`）15 条全绿，而产物已经没有版本号了。
    """
    for path, name in ((MAC_SRC, "install-mac.sh"), (WIN_SRC, "install-windows.ps1")):
        code = "\n".join(
            line
            for line in _read(path).splitlines()
            if not line.lstrip().startswith("#")
        )
        assert 'BUILD_ID = "@@BUILD_ID@@"' in code, (
            f"{name} 里没有 BUILD_ID 的占位符赋值（注释里写了不算）"
        )


def test_installers_write_buildinfo_onto_the_users_machine():
    """占位符得真的落成用户机器上的一个文件，否则桥读不到它。"""
    mac = _read(MAC_SRC)
    assert 'write_file "$APP_HOME/bridge/buildinfo.py"' in mac, "mac 侧没写 buildinfo.py"
    win = _read(WIN_SRC)
    assert "Write-PyFile 'buildinfo.py'" in win, "Windows 侧没写 buildinfo.py"


def test_buildinfo_is_not_a_source_file():
    """🔴 `buildinfo.py` **不许**存在于仓库的 bridge/ 里。

    build.sh 的 `check_complete` 按目录 glob 盯着 `bridge/*.py`：真往那里放一个文件，
    两个安装器都得同时加一段 write_file，而它是**构建产物**不是源码 ——
    放进去等于让每次构建都去嵌一份上次构建的号。
    """
    path = os.path.join(_BRIDGE_ROOT, "bridge", "buildinfo.py")
    assert not os.path.exists(path), (
        "bridge/buildinfo.py 进了源码目录 —— 它是构建产物，应该只由安装器写出来"
    )


def test_embed_placeholder_is_not_confused_with_module_embeds():
    """`@@BUILD_ID@@` 不能长成 `@@EMBED …@@` 的样子，否则会被当成一个要嵌的模块。"""
    for path in (MAC_SRC, WIN_SRC):
        text = _read(path)
        embeds = re.findall(r"^@@EMBED (\S+)@@$", text, re.M)
        assert "BUILD_ID" not in " ".join(embeds), "BUILD_ID 被写成了 @@EMBED 形式"


# ---------- 2. build.sh 会注入 ----------


def test_build_computes_a_build_id():
    build = _read(BUILD)
    assert "BUILD_ID=" in build, "build.sh 不算 BUILD_ID"
    assert "rev-parse --short HEAD" in build, "BUILD_ID 里没有 commit"
    assert "--date=format:'%Y%m%d'" in build, "BUILD_ID 里没有日期"


def test_build_id_uses_the_commit_date_not_the_build_date():
    """🔴 必须用 commit 日期。

    用 `date +%Y%m%d`（构建当天）的话，同一份代码隔天重新构建会得出两个号，
    而两个产物逐字节相同 ⇒ 这个号又答不出「新还是旧」了。
    """
    build = _read(BUILD)
    assert "git log -1 --format=%cd" in build, "没有用 commit 的日期"
    assert not re.search(r"BUILD_DATE=\"\$\(date ", build), (
        "BUILD_DATE 用了构建当天的日期 —— 同一份代码会得出多个版本号"
    )


def test_build_marks_dirty_trees():
    """带未提交改动出的产物不对应任何 commit ⇒ 必须标出来。"""
    build = _read(BUILD)
    assert "-dirty" in build, "build.sh 不标 dirty ⇒ 脏产物会伪装成某个干净版本"
    assert "git status --porcelain" in build


def test_build_injects_into_both_installers():
    build = _read(BUILD)
    assert "def inject_build_id" in build, "build.sh 没有注入函数"
    # 两个安装器各要调一次
    assert build.count("inject_build_id(raw, template_name)") == 2, (
        "BUILD_ID 没有注入到两个安装器（mac + windows 各一次）"
    )


def test_injection_fails_loudly_when_the_placeholder_is_gone():
    """🔴 占位符被删掉时必须**炸**，不许静默退回「没有版本号」。

    只做 replace 的话，谁把模板里那行删了，产物会静默没有版本号 ——
    而那正是这个机制要消灭的状态，且没有任何地方会报错。
    """
    build = _read(BUILD)
    body = build[build.index("def inject_build_id") :]
    body = body[: body.index("\n\n\n")] if "\n\n\n" in body else body
    assert "assert" in body and "@@BUILD_ID@@" in body, (
        "inject_build_id 没有断言占位符存在 ⇒ 删掉它会静默出一个没版本号的产物"
    )


def test_build_id_is_required_not_optional():
    """build.sh 的 Python 段必须**要求** BUILD_ID 存在。

    `os.environ.get("BUILD_ID", "")` 那种写法会在变量没传时静默出一个空号。
    """
    build = _read(BUILD)
    assert 'os.environ["BUILD_ID"]' in build, (
        "BUILD_ID 用了带默认值的读法 ⇒ 没传时会静默出一个没版本号的产物"
    )


def test_build_id_does_not_ride_on_the_template_banner():
    """🔴 BUILD_ID 不许塞进 `strip_template_banner`。

    `test_oneclick_lifecycle.test_uninstall_products_match_templates` 断言
    「产物 = 模板，只换了那条横幅」——  在横幅里多一行，卸载/自检产物就和模板对不上，
    而那条测试盯的是另一件事（忘了跑 build.sh）。
    """
    build = _read(BUILD)
    banner = build[build.index("def strip_template_banner") :]
    banner = banner[: banner.index("return out")]
    # 只看会执行的行 —— 判据不该被注释里的说明文字喂饱
    # （和 test_install_ux.code_lines 同一条纪律：这里的注释正好解释了为什么不这么写）
    code = "\n".join(
        line for line in banner.splitlines() if not line.lstrip().startswith("#")
    )
    assert "BUILD_ID" not in code, (
        "BUILD_ID 挤进了模板横幅 ⇒ 会把卸载/自检产物的一致性测试搞红"
    )


# ---------- 3. 算号那段逻辑真跑一遍 ----------


def _compute_build_id(repo: str) -> str:
    """把 build.sh 里算 BUILD_ID 那几行抽出来，在给定的仓库上真跑一遍。"""
    build = _read(BUILD)
    start = build.index('BUILD_DATE="$(git log')
    end = build.index("export BUILD_ID")
    snippet = build[start:end]
    proc = subprocess.run(
        ["/bin/bash", "-c", f'BRIDGE_SRC="."\n{snippet}\nprintf "%s" "$BUILD_ID"'],
        capture_output=True,
        text=True,
        cwd=repo,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def test_build_id_shape_on_a_clean_repo(tmp_path):
    """真建一个干净仓库算一次号，确认形状是 `<8位日期>-<短commit>`。

    这里用 tmp_path 是安全的：不涉及任何路径判定，只是要一个一次性 git 仓库
    （夹具纪律那条针对的是 pathguard 的系统豁免前缀会让**路径类**用例假绿）。
    """
    repo = tmp_path / "clean"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(str(repo), "add", "a.txt")
    _git(str(repo), "commit", "-q", "-m", "one")

    build_id = _compute_build_id(str(repo))
    assert re.fullmatch(r"\d{8}-[0-9a-f]{7,}", build_id), f"形状不对：{build_id!r}"
    assert not build_id.endswith("-dirty"), f"干净仓库被标成 dirty：{build_id}"


def test_dirty_tree_is_marked(tmp_path):
    """阳性对照：有未提交改动时必须带 `-dirty`。

    没有这条的话，上面那条「不该是 dirty」可能只是因为 dirty 根本没实现。
    """
    repo = tmp_path / "dirty"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(str(repo), "add", "a.txt")
    _git(str(repo), "commit", "-q", "-m", "one")
    (repo / "a.txt").write_text("changed", encoding="utf-8")

    build_id = _compute_build_id(str(repo))
    # 第 15 轮起 dirty 后面带内容指纹（`-dirty-<8位>`），见 build.sh 的红字
    assert re.search(r"-dirty-[0-9a-f]{8}$", build_id), f"脏仓库没被标出来：{build_id}"


def test_dirty_build_id_changes_when_content_changes(tmp_path):
    """🔴 2026-11-07 现场：同一天两次发版版本号一字不差（都是 `…-dirty`）
    ⇒ 自动更新按「版本号变没变」决定装不装 ⇒ 装了上一版的人**永远收不到**这一版，
    而发布脚本照样打绿。内容变了号必须跟着变。
    """
    repo = tmp_path / "dirty2"
    (repo / "src").mkdir(parents=True)
    (repo / "bridge").mkdir()
    _git(str(repo), "init", "-q")
    (repo / "src" / "a.sh").write_text("echo 1\n", encoding="utf-8")
    _git(str(repo), "add", ".")
    _git(str(repo), "commit", "-q", "-m", "one")
    (repo / "src" / "a.sh").write_text("echo 2\n", encoding="utf-8")
    first = _compute_build_id(str(repo))
    (repo / "src" / "a.sh").write_text("echo 3\n", encoding="utf-8")
    second = _compute_build_id(str(repo))
    assert first != second, f"内容变了版本号没变：{first}"
    # 内容不变 ⇒ 号也不变（不然每次 build 都像「有新版」，自动更新会白装）
    assert _compute_build_id(str(repo)) == second


def test_same_commit_gives_the_same_build_id_twice(tmp_path):
    """同一个 commit 算两次必须得出同一个号 —— 否则它答不出「新还是旧」。"""
    repo = tmp_path / "stable"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(str(repo), "add", "a.txt")
    _git(str(repo), "commit", "-q", "-m", "one")
    assert _compute_build_id(str(repo)) == _compute_build_id(str(repo))


def test_different_commits_give_different_build_ids(tmp_path):
    """阴性对照：换了 commit 必须换号，否则这个号区分不出版本。"""
    repo = tmp_path / "moving"
    repo.mkdir()
    _git(str(repo), "init", "-q")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(str(repo), "add", "a.txt")
    _git(str(repo), "commit", "-q", "-m", "one")
    first = _compute_build_id(str(repo))

    (repo / "b.txt").write_text("y", encoding="utf-8")
    _git(str(repo), "add", "b.txt")
    _git(str(repo), "commit", "-q", "-m", "two")
    second = _compute_build_id(str(repo))

    assert first != second, f"两个不同 commit 得出同一个号：{first}"


# ── 11-14 复审实测:内容指纹漏算 scripts/(安装器嵌了 scripts/preflight.py、scripts/launch_guard.sh)──
# 只改这两个时 -dirty-<指纹> 不变 ⇒ 自动更新看号没变 ⇒ 已装的 Mac 永远收不到(11-07 同一个形状)。
def _fingerprint(oneclick) -> str:
    import subprocess as sp
    build = (oneclick / "build.sh").read_text(encoding="utf-8")
    m = re.search(r'CONTENT_HASH="\$\((.*?)\)"\n', build, re.S)
    assert m, "build.sh 里找不到 CONTENT_HASH 的算式"
    script = 'BRIDGE_SRC="../tools/feishu-agent-bridge"\n' + m.group(1)
    return sp.run(["bash", "-c", script], cwd=oneclick, capture_output=True, text=True, check=True).stdout.strip()


def test_fingerprint_covers_every_embedded_file(tmp_path):
    import shutil
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3]
    shutil.copytree(repo / "feishu-agent-oneclick" / "src", tmp_path / "feishu-agent-oneclick" / "src")
    shutil.copy(repo / "feishu-agent-oneclick" / "build.sh", tmp_path / "feishu-agent-oneclick" / "build.sh")
    shutil.copytree(repo / "tools" / "feishu-agent-bridge", tmp_path / "tools" / "feishu-agent-bridge",
                    ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "tests"))
    oc = tmp_path / "feishu-agent-oneclick"
    embeds = sorted({m.group(1) for f in (oc / "src").iterdir() if f.is_file()
                     for m in re.finditer(r"^@@EMBED (\S+)@@$", f.read_text(encoding="utf-8", errors="replace"), re.M)})
    assert any(e.startswith("scripts/") for e in embeds), "安装器不再嵌 scripts/ 了?那这条测试要跟着改"
    base = _fingerprint(oc)
    assert re.fullmatch(r"[0-9a-f]{8}", base), base
    for rel in embeds:   # 每一个被嵌进安装器的文件,改一个字节都必须换号
        p = tmp_path / "tools" / "feishu-agent-bridge" / rel
        orig = p.read_bytes()
        p.write_bytes(orig + b"\n# x\n")
        try:
            assert _fingerprint(oc) != base, f"只改 {rel} 时版本号不变 ⇒ 已装的机器收不到这次更新"
        finally:
            p.write_bytes(orig)
    assert _fingerprint(oc) == base, "还原后指纹应回到原值(算式要稳定)"
