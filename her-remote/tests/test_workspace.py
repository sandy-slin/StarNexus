"""换目录测试。

这里大部分是 **DENY 用例**（越界拒绝、软链逃逸、`..` 逃逸），而 DENY 用例最会
假通过 —— 桩写得不完整时，"被拒绝"可能只是因为根本没跑到判断那一行，或者那个
目录压根不存在。所以每条 deny 都配一条**阳性对照**：同样的形状、同样的入口，
但应该放行。两条一起绿才说明判断真的在生效。

所有目录树都造在 `/tmp` 下的独立 fixture 里。绝不碰 `~/.carher-feishu-agent`、
`~/.claude*` 和用户的真实仓库 —— 这些测试会递归扫目录、会写 json 文件。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.policy import Policy  # noqa: E402
from bridge import workspace as ws  # noqa: E402


@pytest.fixture
def tree(tmp_path):
    """造一棵假目录树。

    形状是刻意的，每一项都对应一条用例：
      root/carher-admin/.git     普通仓库
      root/carher-bot/.git       和上面同前缀 ⇒ `/cd carher` 必须要他选
      root/notes                 不是仓库（/dirs 不该列它）
      root/deep/a/b/buried/.git  第 4 层 ⇒ 超出扫描深度
      root/escape -> outside     软链逃逸
      root-evil/                 同前缀的兄弟目录 ⇒ 不能因为前缀像就放行
      outside/secret/            白名单外
    """
    root = tmp_path / "root"
    for name in ("carher-admin", "carher-bot"):
        (root / name / ".git").mkdir(parents=True)
    (root / "notes").mkdir(parents=True)
    (root / "deep" / "a" / "b" / "buried" / ".git").mkdir(parents=True)
    outside = tmp_path / "outside" / "secret"
    outside.mkdir(parents=True)
    (tmp_path / "root-evil").mkdir()
    os.symlink(str(tmp_path / "outside"), str(root / "escape"))

    policy = Policy(
        mode="readonly",
        workspaces=(str(root),),
        default_workspace=str(root),
        model="claude-haiku-5-5",
        turn_timeout_sec=60,
    )
    store = ws.WorkspaceStore(str(tmp_path / "workspaces.json"), policy)
    return SimpleTree(tmp_path, root, outside, policy, store)


class SimpleTree:
    def __init__(self, tmp_path, root, outside, policy, store):
        self.tmp_path = tmp_path
        self.root = str(root)
        self.outside = str(outside)
        self.policy = policy
        self.store = store
        # realpath：/tmp 在 macOS 上是 /private/tmp 的软链，不收敛的话
        # 每条断言都会在路径比较上假红。
        self.real_root = os.path.realpath(self.root)

    def cd(self, arg: str, scope: str = "dm:test", current: str | None = None):
        cur = current if current is not None else self.store.get(scope)
        return ws.handle(self.policy, self.store, scope, cur, f"/cd {arg}".strip())


# ---------- 越界：每条 deny 紧跟一条阳性对照 ----------


def test_absolute_path_outside_is_denied(tree):
    r = tree.cd(tree.outside)
    assert r.outcome == ws.Outcome.OUTSIDE
    assert not r.ok
    assert not r.workspace


def test_absolute_path_inside_is_allowed(tree):
    """阳性对照：同样走绝对路径这条分支，白名单内的必须通过。

    少了这条，上面那条"被拒"可能只是因为绝对路径分支整个坏了。
    """
    r = tree.cd(os.path.join(tree.root, "carher-admin"))
    assert r.outcome == ws.Outcome.OK, r.reply
    assert r.workspace == os.path.join(tree.real_root, "carher-admin")


def test_dotdot_escape_is_denied(tree):
    r = tree.cd("../outside/secret")
    assert r.outcome == ws.Outcome.OUTSIDE


def test_dotdot_inside_is_allowed(tree):
    """阳性对照：`..` 本身不是坏东西，爬回白名单内要能用。

    从 root/carher-admin 出发 `../carher-bot` 是个很自然的写法；
    要是这条也红，上面那条 deny 只是"所有 `..` 都被拒"而不是真在判边界。
    """
    start = os.path.join(tree.real_root, "carher-admin")
    r = tree.cd("../carher-bot", current=start)
    assert r.outcome == ws.Outcome.OK, r.reply
    assert r.workspace == os.path.join(tree.real_root, "carher-bot")


def test_symlink_escape_is_denied(tree):
    """`root/escape -> outside`：路径字面量在白名单里，realpath 在外面。

    这是最容易被自己写的 `startswith` 放过去的一类 —— 所以这个模块一行路径
    收敛都不自己写，全走 `Policy.resolve_workspace()`。
    """
    r = tree.cd(os.path.join(tree.root, "escape", "secret"))
    assert r.outcome == ws.Outcome.OUTSIDE


def test_symlink_inside_is_allowed(tree):
    """阳性对照：指向白名单**内**的软链必须能用，否则上面那条只是"见链就拒"。"""
    link = os.path.join(tree.root, "link-to-admin")
    os.symlink(os.path.join(tree.root, "carher-admin"), link)
    r = tree.cd(link)
    assert r.outcome == ws.Outcome.OK, r.reply
    # 落下来的是 realpath，不是那条链 —— agent 的 cwd 不该是个链。
    assert r.workspace == os.path.join(tree.real_root, "carher-admin")


def test_sibling_prefix_dir_is_denied(tree):
    """`<root>-evil` 不能因为前缀像 root 就被当成在里面。"""
    r = tree.cd(tree.root + "-evil")
    assert r.outcome == ws.Outcome.OUTSIDE


def test_root_itself_is_allowed(tree):
    """阳性对照：白名单根自己要能切回去（上面那条不是"根一律拒"）。"""
    start = os.path.join(tree.real_root, "carher-admin")
    r = tree.cd(tree.root, current=start)
    assert r.outcome == ws.Outcome.OK, r.reply
    assert r.workspace == tree.real_root


def test_outside_reply_does_not_leak_existence(tree):
    """🔴 越界的回话里不能区分"存在"和"不存在"。

    区别对待 ⇒ `/cd` 变成文件系统探测器：他能一句一句把白名单外的目录结构
    问出来，而这些话是发到飞书（外部系统）的。
    """
    gone = os.path.join(str(tree.tmp_path), "outside", "no-such-thing")
    exists = tree.cd(tree.outside)
    missing = tree.cd(gone)
    assert exists.outcome == missing.outcome == ws.Outcome.OUTSIDE
    # 回话里会回显他**自己打的那串**（他已经知道了，不算泄露）；把回显挖掉之后
    # 剩下的话必须一模一样 —— 任何差异都是我们在告诉他外面有什么。
    assert exists.reply.replace(tree.outside, "<X>") == missing.reply.replace(
        gone, "<X>"
    ), "两种情况回话不同 = 泄露了存在性"
    for leak in ("不存在", "不是目录", "权限"):
        assert leak not in exists.reply and leak not in missing.reply


def test_inside_missing_dir_says_so(tree):
    """阳性对照：白名单**内**不存在的目录要说清"不存在"，不能也笼统拒掉。

    白名单内说实话是对的 —— 他本来就有权看这里，含糊反而让他反复重打。
    """
    r = tree.cd("no-such-repo-xyz")
    assert r.outcome == ws.Outcome.MISSING
    assert "不存在" in r.reply


def test_file_is_not_a_dir(tree):
    open(os.path.join(tree.root, "README.md"), "w").close()
    r = tree.cd("./README.md")
    assert r.outcome == ws.Outcome.NOT_DIR
    assert "文件" in r.reply


def test_unreadable_dir_says_permission(tree):
    """没权限要单独说，不能混成"不存在" —— 两者的下一步动作完全不同。"""
    locked = os.path.join(tree.root, "locked")
    os.mkdir(locked)
    os.chmod(locked, 0o000)
    try:
        r = tree.cd("./locked")
        assert r.outcome == ws.Outcome.NO_ACCESS
        assert "权限" in r.reply
    finally:
        os.chmod(locked, 0o755)  # 不然 tmp_path 清不掉


def test_readable_dir_is_allowed(tree):
    """阳性对照：权限正常的目录必须通过（上面那条不是"所有新建目录都拒"）。"""
    fine = os.path.join(tree.root, "fine")
    os.mkdir(fine)
    r = tree.cd("./fine")
    assert r.outcome == ws.Outcome.OK, r.reply


# ---------- 模糊匹配 ----------


def test_bare_project_name_matches(tree):
    """用户大概率只打项目名。这是这个功能存在的主要理由。"""
    r = tree.cd("carher-admin")
    assert r.outcome == ws.Outcome.OK, r.reply
    assert r.workspace == os.path.join(tree.real_root, "carher-admin")


def test_ambiguous_prefix_does_not_auto_pick(tree):
    """🔴 命中多个绝不替他挑。

    挑错的后果是他在**另一个仓库**上干活，而回话里只有"已切到 xxx"一行 ——
    他不会立刻发现，等发现时改动已经落在错的地方了。
    """
    r = tree.cd("carher")
    assert r.outcome == ws.Outcome.AMBIGUOUS
    assert not r.ok
    assert not r.workspace, "要他选的时候不能顺手给个目录"
    assert len(r.candidates) == 2
    # 回话里要把两条都列出来，且是能直接粘回去的形式。
    assert r.reply.count("/cd ") == 2


def test_ambiguous_reply_lines_round_trip(tree):
    """回话里列的那几行必须能**原样粘回去**就切过去。

    不能粘的话"要你选"就是个死胡同：他看到两个候选，却没有一句能直接用，
    只能自己拼路径 —— 而拼错又会得到"不在范围里"。
    """
    r = tree.cd("carher")
    assert r.outcome == ws.Outcome.AMBIGUOUS
    pasted = [line.strip() for line in r.reply.splitlines() if line.strip().startswith("/cd ")]
    assert len(pasted) == 2
    for line in pasted:
        again = ws.handle(tree.policy, tree.store, "dm:test", tree.real_root, line)
        assert again.outcome == ws.Outcome.OK, f"粘回去不管用：{line} -> {again.reply}"


def test_home_shortened_reply_round_trips(tree, monkeypatch):
    """回话里把家目录缩成 `~` 之后，缩出来的那串还得能粘回去。

    生产上白名单就在家目录下（安装器默认 `~/codes`），所以列出来的几乎全是
    `~/...` 这个形式 —— 它要是解析不回去，`/dirs` 和"要你选"列的每一行都是废的。
    这里把 HOME 指到 tmp 里，不碰用户真实家目录。
    """
    monkeypatch.setenv("HOME", str(tree.tmp_path))
    r = tree.cd("carher")
    shown = [line.strip() for line in r.reply.splitlines() if line.strip().startswith("/cd ")]
    assert shown and all(line.startswith("/cd ~/") for line in shown), shown
    for line in shown:
        again = ws.handle(tree.policy, tree.store, "dm:test", tree.real_root, line)
        assert again.outcome == ws.Outcome.OK, f"缩短后粘不回去：{line}"


def test_ambiguous_does_not_persist_anything(tree):
    """要他选的那一次**不能落盘** —— 否则下一轮就在某个候选里跑了。"""
    before = tree.store.get("dm:test")
    tree.cd("carher")
    assert tree.store.get("dm:test") == before


def test_exact_name_beats_prefix_match(tree):
    """同时有 `carher` 和 `carher-admin` 时，`/cd carher` 该去 `carher`。

    精确同名必须压掉模糊命中 —— 他已经打准了，再让他选一遍是倒退。
    """
    os.makedirs(os.path.join(tree.root, "carher", ".git"))
    r = tree.cd("carher")
    assert r.outcome == ws.Outcome.OK, r.reply
    assert r.workspace == os.path.join(tree.real_root, "carher")


def test_fuzzy_match_never_leaves_allowed_roots(tree):
    """模糊匹配不能变成全盘搜索。

    白名单外有个同名目录，匹配必须**看不见**它 —— 看见了就等于 `/cd` 能用来
    探测白名单外的目录名。
    """
    os.makedirs(os.path.join(str(tree.tmp_path), "outside", "lonely-repo", ".git"))
    r = tree.cd("lonely-repo")
    assert r.outcome != ws.Outcome.OK
    assert not r.workspace


def test_fuzzy_match_respects_depth_limit(tree):
    """第 4 层的仓库不该被模糊匹配捞出来 —— 深度上限是性能闸门。

    配一条阳性对照（同一棵树第 3 层以内的目录能匹上），否则这条"没匹上"
    可能只是模糊匹配整个坏了。
    """
    assert ws._fuzzy_match(tree.policy, "buried") == []
    assert ws._fuzzy_match(tree.policy, "deep"), "阳性对照失败：浅层目录也匹不上"


def test_path_shaped_input_skips_fuzzy_match(tree):
    """写了路径就按路径算，不给他换个别的目录。

    `./carher` 不存在 ⇒ 要回"不存在"，而不是模糊匹配到 carher-admin。
    """
    r = tree.cd("./carher")
    assert r.outcome == ws.Outcome.MISSING


def test_tilde_path_is_expanded_not_taken_literally(tree):
    """`~` 必须当家目录展开，不能当成当前目录下一个叫 `~` 的子目录。

    判据造得很具体：真在白名单内建一个名字就是 `~` 的目录。没展开的话
    `~/trap` 会落到 `<root>/~/trap` 并**成功切过去** —— 他以为去了家目录，
    实际在白名单里一个陷阱目录里跑。展开了的话家目录不在白名单 ⇒ 越界。
    """
    os.makedirs(os.path.join(tree.root, "~", "trap"))
    r = tree.cd("~/trap")
    assert r.outcome == ws.Outcome.OUTSIDE, (
        f"`~` 被当字面量了，落到 {r.workspace}"
    )


def test_quoted_and_padded_input_is_tolerated(tree):
    """飞书里粘路径常带引号和空格。这些不该变成"目录不存在"。"""
    r = tree.cd('  "carher-admin"  ')
    assert r.outcome == ws.Outcome.OK, r.reply


def test_empty_cd_asks_instead_of_moving(tree):
    r = ws.handle(tree.policy, tree.store, "dm:test", tree.real_root, "/cd")
    assert r.outcome == ws.Outcome.EMPTY
    assert "/dirs" in r.reply


def test_same_dir_is_not_reported_as_a_move(tree):
    """已经在这儿了要如实说，不然他以为切过去了（其实本来就在）。"""
    r = tree.cd("carher-admin")
    again = tree.cd("carher-admin", current=r.workspace)
    assert again.outcome == ws.Outcome.SAME


# ---------- 落盘：重启之后还得在那儿 ----------


def test_survives_restart(tree):
    """🔴 看门狗在长连接静默时会自杀等重启，所以"重启"是常态不是例外。

    只记内存 ⇒ 他昨天切到某个仓库、今天第一条消息在默认目录跑了，
    而回话里一个字都不会提。这里用"新建一个 store 指向同一个文件"模拟重启。
    """
    moved = tree.cd("carher-bot")
    assert moved.outcome == ws.Outcome.OK, moved.reply

    reborn = ws.WorkspaceStore(str(tree.tmp_path / "workspaces.json"), tree.policy)
    assert reborn.get("dm:test") == os.path.join(tree.real_root, "carher-bot")


def test_default_when_nothing_remembered(tree):
    """阳性对照：没记过的 scope 回默认目录（上面那条不是"永远返回同一个值"）。"""
    fresh = ws.WorkspaceStore(str(tree.tmp_path / "workspaces.json"), tree.policy)
    assert fresh.get("dm:never-seen") == tree.real_root


def test_scopes_do_not_bleed(tree):
    """一个会话切目录不能影响另一个会话。群和单聊共用一台机器。"""
    tree.cd("carher-admin", scope="dm:a")
    assert tree.store.get("dm:b") == tree.real_root
    assert tree.store.get("dm:a") == os.path.join(tree.real_root, "carher-admin")


def test_persisted_path_outside_allowlist_is_dropped_on_load(tree):
    """🔴 落盘的路径**必须重新过白名单**，不能信磁盘。

    用户收窄了 config.toml 的 workspaces（或安装器重跑覆盖了配置），而这个
    文件还留着旧目录。直接采信 ⇒ 一次旧的 /cd 永久绕过新边界。
    """
    path = str(tree.tmp_path / "workspaces.json")
    with open(path, "w") as handle:
        json.dump({"dm:test": tree.outside}, handle)
    store = ws.WorkspaceStore(path, tree.policy)
    assert store.get("dm:test") == tree.real_root, "采信了磁盘上的越界目录"


def test_persisted_path_inside_allowlist_is_kept_on_load(tree):
    """阳性对照：白名单内的落盘值要留着，否则上面那条只是"启动时全丢"。"""
    path = str(tree.tmp_path / "workspaces.json")
    inside = os.path.join(tree.real_root, "carher-admin")
    with open(path, "w") as handle:
        json.dump({"dm:test": inside}, handle)
    store = ws.WorkspaceStore(path, tree.policy)
    assert store.get("dm:test") == inside


def test_corrupt_state_file_does_not_block_startup(tree):
    """半截 JSON（断电写坏）不能让启动失败 —— 启动不了比回默认目录严重得多。"""
    path = str(tree.tmp_path / "workspaces.json")
    with open(path, "w") as handle:
        handle.write('{"dm:test": "/tmp/roo')
    store = ws.WorkspaceStore(path, tree.policy)
    assert store.get("dm:test") == tree.real_root


def test_store_set_rejects_outside_path_directly(tree):
    """`store.set()` 自己也要判白名单 —— 不能靠"调用方记得先校验"。"""
    assert tree.store.set("dm:test", tree.outside) is False
    assert tree.store.get("dm:test") == tree.real_root


def test_store_set_accepts_inside_path_directly(tree):
    """阳性对照：同一个入口，白名单内的要写进去。"""
    inside = os.path.join(tree.real_root, "notes")
    assert tree.store.set("dm:test", inside) is True
    assert tree.store.get("dm:test") == inside


def test_save_failure_is_not_silently_a_success(tree, monkeypatch):
    """🔴 落盘失败不许静默成功。

    静默成功 ⇒ 他以为重启后还在这儿，实际回了默认目录，而他不知道为什么。
    这里把原子改名打成 OSError 来模拟盘满 / 只读。
    """
    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ws.os, "replace", boom)
    r = tree.cd("carher-admin")
    assert r.outcome == ws.Outcome.SAVE_FAILED
    # 目录确实切了（这一轮能用），但回话必须把"重启会丢"说出来。
    assert r.workspace == os.path.join(tree.real_root, "carher-admin")
    assert "重启" in r.reply


def test_save_success_says_nothing_about_restart(tree):
    """阳性对照：正常落盘时不能挂那条警告（否则警告就成了噪音）。"""
    r = tree.cd("carher-admin")
    assert r.outcome == ws.Outcome.OK
    assert "重启" not in r.reply


# ---------- /dirs ----------


def test_dirs_lists_repos_and_marks_current(tree):
    """用户根本不知道有哪些目录可选，所以这条得同时回答"能去哪"和"现在在哪"。"""
    out = ws.list_dirs(tree.policy, os.path.join(tree.real_root, "carher-admin"))
    assert "carher-admin" in out
    assert "carher-bot" in out
    assert "当前" in out
    assert "notes" not in out, "notes 没有 .git，不该列"


def test_dirs_sorted_by_mtime(tree):
    """最近动过的在前 —— 他要去的几乎总是刚改过的那个。"""
    old = os.path.join(tree.real_root, "carher-admin")
    new = os.path.join(tree.real_root, "carher-bot")
    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, None)
    out = ws.list_dirs(tree.policy, tree.real_root)
    assert out.index("carher-bot") < out.index("carher-admin")


def test_dirs_never_lists_anything_outside(tree):
    """白名单外的仓库不能出现在列表里 —— 列出来就是在报外面有什么。"""
    os.makedirs(os.path.join(str(tree.tmp_path), "outside", "leaky-repo", ".git"))
    out = ws.list_dirs(tree.policy, tree.real_root)
    assert "leaky-repo" not in out


def test_dirs_does_not_follow_symlink_out(tree):
    """`root/escape -> outside` 这条链不跟 —— 跟了就把外面的仓库列进来了。"""
    os.makedirs(os.path.join(str(tree.tmp_path), "outside", "linked-repo", ".git"))
    out = ws.list_dirs(tree.policy, tree.real_root)
    assert "linked-repo" not in out


def test_dirs_shows_roots_even_without_git(tree, tmp_path):
    """根目录自己要列出来 —— 他得看到自己的授权边界划在哪。"""
    out = ws.list_dirs(tree.policy, tree.real_root)
    assert "根目录" in out


def test_git_worktree_marker_file_counts_as_repo(tree):
    """worktree / submodule 里 `.git` 是**文件**不是目录。

    按 isdir 判会把这些目录整个漏掉，而用户照样会想切进去。
    """
    wt = os.path.join(tree.root, "wt-repo")
    os.mkdir(wt)
    with open(os.path.join(wt, ".git"), "w") as handle:
        handle.write("gitdir: /somewhere/.git/worktrees/wt\n")
    assert "wt-repo" in ws.list_dirs(tree.policy, tree.real_root)


# ---------- 多个根目录 ----------


def _two_root_policy(tmp_path):
    a = tmp_path / "codes"
    b = tmp_path / "work"
    (a / "alpha" / ".git").mkdir(parents=True)
    (b / "beta" / ".git").mkdir(parents=True)
    (b / "alpha" / ".git").mkdir(parents=True)  # 两个根下同名 ⇒ 必须要他选
    return Policy(
        mode="readonly",
        workspaces=(str(a), str(b)),
        default_workspace=str(a),
        model="claude-haiku-5-5",
        turn_timeout_sec=60,
    )


def test_second_root_is_reachable(tmp_path):
    """多个根必须都能去。`workspaces` 配了多条却只能用第一个，就是这个功能的起因。"""
    policy = _two_root_policy(tmp_path)
    store = ws.WorkspaceStore(str(tmp_path / "s.json"), policy)
    r = ws.handle(policy, store, "dm:x", store.get("dm:x"), "/cd beta")
    assert r.outcome == ws.Outcome.OK, r.reply
    assert r.workspace == os.path.realpath(str(tmp_path / "work" / "beta"))


def test_same_name_in_two_roots_asks(tmp_path):
    """两个根下同名 ⇒ 要他选。替他挑一个 = 他可能在另一个客户的仓库上干活。"""
    policy = _two_root_policy(tmp_path)
    store = ws.WorkspaceStore(str(tmp_path / "s.json"), policy)
    r = ws.handle(policy, store, "dm:x", store.get("dm:x"), "/cd alpha")
    assert r.outcome == ws.Outcome.AMBIGUOUS
    assert len(r.candidates) == 2
    # 候选给的是全路径，否则列两条一样的名字他没法选。
    assert len({c for c in r.candidates}) == 2


# ---------- 命令分派 ----------


def test_handle_ignores_other_commands(tree):
    """不是这个模块的命令要返回 None，让 app.py 接着往下判。

    返回别的东西（比如一个"未知命令"的 CdResult）会把 /status、/new 全吃掉。
    """
    for other in ("/status", "/new", "/stop", "/help", "/resume", "随便一句话"):
        assert ws.handle(tree.policy, tree.store, "dm:x", tree.real_root, other) is None


def test_handle_claims_its_own_commands(tree):
    """阳性对照：自己的命令必须认领（上面那条不是"全都返回 None"）。"""
    for mine in ("/cd carher-admin", "/dirs", "/ls", "/pwd", "/CD carher-admin"):
        assert ws.handle(tree.policy, tree.store, "dm:x", tree.real_root, mine) is not None


def test_commands_set_matches_dispatch(tree):
    """`COMMANDS` 是给 app.py 看的声明，必须和实际认领的一致。

    不一致的后果：app.py 按 COMMANDS 把命令路由过来，handle 返回 None，
    然后那条命令被当成**给 agent 的自然语言任务**执行掉。
    """
    for command in ws.COMMANDS:
        assert ws.handle(
            tree.policy, tree.store, "dm:x", tree.real_root, command
        ) is not None, f"{command} 在 COMMANDS 里但 handle 不认"


# ---------- 扫描预算：飞书那条 3 秒纪律 ----------


def test_scan_stops_at_entry_budget(tree, monkeypatch):
    """横向爆炸也要有上限 —— 深度限不住它。

    `~` 是安装器的默认回退，家目录下一层就可能有上千个目录。无预算地扫
    ⇒ 一次 /cd 几万次 stat ⇒ 撞飞书"3 秒内必须返回"。
    把预算压到 10 来验闸门真的在：不设预算的话结果会是 60 个。
    """
    for i in range(60):
        os.makedirs(os.path.join(tree.root, f"bulk-{i:03d}", ".git"))
    monkeypatch.setattr(ws, "MAX_SCAN_ENTRIES", 10)
    found = ws._scan(tree.policy, want_git_only=True)
    assert len(found) < 20, f"预算没生效，扫出了 {len(found)} 个"


def test_scan_without_budget_squeeze_finds_them_all(tree):
    """阳性对照：预算正常时这些目录都要扫得到（上面那条不是"扫不出东西"）。"""
    for i in range(60):
        os.makedirs(os.path.join(tree.root, f"bulk-{i:03d}", ".git"))
    found = ws._scan(tree.policy, want_git_only=True)
    assert len(found) >= 60


def test_noise_dirs_are_skipped_in_scan(tree):
    """node_modules 这类只会把目录数撑爆，不该进扫描。"""
    os.makedirs(os.path.join(tree.root, "node_modules", "pkg", ".git"))
    assert ws._fuzzy_match(tree.policy, "pkg") == []


def test_skipped_dir_is_still_reachable_explicitly(tree):
    """但他显式打出路径时必须能进去 —— 跳过只是**扫描**优化，不是边界。

    把它当边界会让人困惑：明明在白名单里、明明存在，却说"不在范围里"。
    """
    target = os.path.join(tree.root, "node_modules", "pkg")
    os.makedirs(target, exist_ok=True)
    r = tree.cd("./node_modules/pkg")
    assert r.outcome == ws.Outcome.OK, r.reply
