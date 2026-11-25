"""闸门测试。

这些用例里大部分是 **DENY 用例**，而 DENY 用例最会假通过 —— 桩写得不完整时，
"被拒绝"可能只是因为根本没跑到判断那一行。所以每条 deny 都配一条**阳性对照**
（同样的形状但应该放行），确保判断真的在生效。
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.policy import Policy, PolicyError  # noqa: E402

WS = "/tmp/bridge-test-ws"


def make(mode: str = "readonly", workspaces=(WS,)) -> Policy:
    return Policy(
        mode=mode,
        workspaces=tuple(workspaces),
        default_workspace=workspaces[0],
        model="claude-haiku-5-5",
        turn_timeout_sec=60,
    )


# ---------- 配置校验 ----------


def test_rejects_unknown_mode():
    with pytest.raises(PolicyError):
        Policy(
            mode="yolo",
            workspaces=(WS,),
            default_workspace=WS,
            model="m",
            turn_timeout_sec=60,
        )


def test_rejects_relative_workspace():
    with pytest.raises(PolicyError):
        Policy(
            mode="readonly",
            workspaces=("./relative",),
            default_workspace="./relative",
            model="m",
            turn_timeout_sec=60,
        )


def test_rejects_default_outside_workspaces():
    with pytest.raises(PolicyError):
        Policy(
            mode="readonly",
            workspaces=("/tmp/a",),
            default_workspace="/tmp/b",
            model="m",
            turn_timeout_sec=60,
        )


def test_accepts_valid_config():
    """阳性对照：合法配置必须能建起来，否则上面三条 raise 可能只是构造函数本身坏了。"""
    policy = make()
    assert policy.mode == "readonly"


# ---------- 路径收敛 ----------


def test_workspace_inside_is_allowed():
    policy = make()
    os.makedirs(os.path.join(WS, "sub"), exist_ok=True)
    assert policy.resolve_workspace(os.path.join(WS, "sub")) is not None


def test_workspace_escape_via_dotdot_is_rejected():
    policy = make()
    assert policy.resolve_workspace(os.path.join(WS, "..", "..", "etc")) is None


def test_workspace_sibling_prefix_is_rejected():
    """`/tmp/bridge-test-ws-evil` 不能因为前缀像就被当成在 workspace 里。"""
    policy = make()
    assert policy.resolve_workspace(WS + "-evil") is None


def test_workspace_symlink_escape_is_rejected():
    policy = make()
    os.makedirs(WS, exist_ok=True)
    link = os.path.join(WS, "escape-link")
    if not os.path.islink(link):
        os.symlink("/etc", link)
    assert policy.resolve_workspace(link) is None


def test_empty_workspace_request_is_rejected():
    assert make().resolve_workspace("") is None


# ---------- 工具白名单 ----------


def test_readonly_has_no_write_tools():
    tools = make("readonly").allowed_tools()
    for forbidden in ("Edit", "Write", "NotebookEdit"):
        assert forbidden not in tools


def test_readonly_grants_read_tools():
    """阳性对照：只读模式必须真的给了读的能力，否则上面那条 assert 没有意义。

    注意读类工具带 **workspace 作用域**（`Read(<目录>/**)`）——
    2026-11-03 实测不加作用域时 agent 能读 workspace 之外的任意绝对路径。
    """
    tools = make("readonly").allowed_tools()
    for expected in ("Read", "Glob", "Grep"):
        matching = [t for t in tools if t.startswith(expected + "(")]
        assert matching, f"没给 {expected} 工具：{tools}"
        for entry in matching:
            assert WS in entry, f"{expected} 没限定在 workspace 内：{entry}"
            assert entry.endswith("/**)"), f"作用域形状不对：{entry}"


def test_read_tools_are_scoped_to_every_workspace():
    """多个 workspace 时每个都要有作用域条目，否则第二个目录读不了。"""
    policy = make("readonly", workspaces=(WS, "/tmp/bridge-test-ws2"))
    tools = policy.allowed_tools()
    for workspace in policy.workspaces:
        assert any(
            t.startswith("Read(") and workspace in t for t in tools
        ), f"{workspace} 没有 Read 作用域"


def test_trusted_reads_the_whole_machine_but_never_credentials():
    """🔴 第 16 轮改过意图。

    原来这条叫 `test_trusted_also_keeps_the_read_boundary`，断言全授权下 Read 仍限定在
    工作目录。用户 2026-11-07 拍板推翻：「终端历史、桌面、下载、别的项目都在外面，
    这些都是基本的查阅操作，为啥还有弹卡呢，要做放开到整台电脑」——
    远程看电脑的主要用途正是看工作目录外的东西。

    所以边界从「工作目录」挪到「凭据」：
    - 全授权 ⇒ 读整台机器（`Read(//**)`）
    - 凭据路径 ⇒ 照旧拒（`ALWAYS_DENY` 的 `Read(//**/...)` 七条，deny 优先于 allow；
      Bash 那条路由 `cmdclass._touches_credentials` 挡，点同意也不许）
    - 只读 / 可改档 ⇒ **不跟着放开**（见下一条测试）
    """
    policy = make("trusted")
    tools = policy.allowed_tools()
    assert "Read" not in tools, "不许给裸 Read（那样 deny 规则的形状也会跟着变）"
    assert "Read(//**)" in tools, "全授权下读不了工作目录外 ⇒ 远程看电脑没法用"
    denied = " ".join(policy.disallowed_tools())
    for shape in (".env", ".ssh", ".aws", "id_rsa", "credentials", ".pem", ".key"):
        assert f"Read(//**/" in denied and shape in denied, f"凭据没挡住：{shape}"


def test_readonly_and_write_keep_the_workspace_read_boundary():
    """阳性对照：另外两档的语义是「先问你」，不跟着全授权一起放开。"""
    for mode in ("readonly", "write"):
        tools = make(mode).allowed_tools()
        assert "Read(//**)" not in tools, f"{mode} 档跟着放开了"
        assert any(t.startswith("Read(") and WS in t for t in tools)


def test_write_mode_grants_edit_but_still_denies_push():
    policy = make("write")
    tools = policy.allowed_tools()
    # 写类工具带工作目录作用域（2026-11-04 review：裸 Edit/Write 能写到范围外）
    assert any(t.startswith("Edit(") for t in tools)
    assert "Edit" not in tools and "Write" not in tools, "写类工具不许裸给"
    for t in tools:
        if t.startswith(("Edit(", "Write(", "NotebookEdit(")):
            assert t.endswith("/**)") and policy.workspaces[0] in t
    # 能改文件 ≠ 能把改动发出去。
    assert "Bash(git push:*)" in policy.disallowed_tools()


def test_no_bare_git_in_allowlist():
    """裸 `git` 会放进 push/reset —— 前缀匹配的陷阱。"""
    for tool in make("write").allowed_tools():
        assert tool != "Bash(git:*)"
        assert tool != "Bash(git *)"


def test_no_interpreter_or_find_in_allowlist():
    """解释器和 find -exec 等于任意代码执行。"""
    joined = " ".join(make("write").allowed_tools())
    for dangerous in ("python", "node", "Bash(sh", "Bash(bash", "Bash(find", "Bash(xargs"):
        assert dangerous not in joined


def test_credentials_are_denied_in_every_mode():
    for mode in ("readonly", "write"):
        denied = " ".join(make(mode).disallowed_tools())
        for secret in (".env", ".pem", "id_rsa", ".aws", ".ssh"):
            assert secret in denied


def test_destructive_bash_denied_in_every_mode():
    """任何档位都硬拒的那几条。

    🔴 第 16 轮改过判据。原来含 `Bash(rm:*)` 和 `Bash(git reset:*)` —— 这两条**过宽**：
    全授权档下 `rm victim.txt`（删工作目录里自己的文件）、`git reset HEAD`（撤销 add、
    不丢改动）都被直接拒、连卡都不弹，而领导要的是「默认全控制」。
    收窄之后谁接着管，以及「撤掉 rm 的兜底之后删家目录只剩一道红线」这件事，
    见 `bridge/policy.py` 的红字和 `tests/test_r16_deny_narrow.py`。
    """
    for mode in ("readonly", "write", "trusted"):
        denied = make(mode).disallowed_tools()
        for dangerous in ("Bash(sudo:*)", "Bash(git push:*)", "Bash(kubectl delete:*)"):
            assert dangerous in denied, f"{mode} 档少了硬拒：{dangerous}"
        # 第 39 轮：`git reset --hard` / `git clean` 从硬拒撤掉 —— 日常开发命令，
        # 丢的是本机未提交改动，不是不可逆的灾难（reflog 还能找回提交）。
        for daily in ("Bash(git reset --hard:*)", "Bash(git clean:*)"):
            assert daily not in denied, f"{daily} 又被加回硬拒了 —— 它是日常命令"


def test_describe_mentions_what_is_not_allowed():
    """给用户看的描述得说清边界，不能只讲能干什么。

    🔴 2026-11-05 改过判据。原来这条要求 describe() 里出现「不能」二字，
    而只读那句写的是「不能改文件」—— **那句话是假的**：实测只读档下坚持一句会弹
    审批卡，点同意文件真的被改了。用户当天拍板按真实行为改说法，于是这条测试
    把那句谎话钉住了，得一起改。
    判据换成：只读那句必须说清「要改会先问你」这个真实边界，**并且不许承诺改不了**。
    """
    readonly = make("readonly").describe()
    assert "只看" in readonly or "默认只看" in readonly
    assert "问" in readonly, f"没说清「要改会先问你」：{readonly!r}"
    assert "不能改文件" not in readonly, f"又把「改不了」这句谎话写回来了：{readonly!r}"
    # write / trusted 两档要说清**挡什么**。
    # 🔴 第 17 轮换过判据：原来钉「不能」两个字，而新文案写的是
    # 「删家目录、推代码、读私钥这几样永远不做」—— 语义更强（「永远不做」比「不能」明确），
    # 只是没有那两个字。钉字面量的毛病今天红了 7 次，一律改成钉语义。
    for mode in ("write", "trusted"):
        text = make(mode).describe()
        assert "永远不做" in text or "不能" in text, f"{mode} 没说清挡什么：{text!r}"
        for guard in ("删家目录", "推代码", "私钥"):
            assert guard in text, f"{mode} 漏了这条红线：{guard} ← {text!r}"


# ---------- IT 策略文件（统一管控） ----------
#
# 这组盯的是 2026-11-05 实测的那个缺口：doctor 向用户和 IT 两边宣称
# 「由 IT 固定 mode=…」，而桥根本不读那个文件。
# 端到端那条（走 Settings.load）在 tests/test_oneclick_lifecycle.py。


@pytest.fixture
def policy_dir():
    """策略文件夹具放在测试目录旁边，**不放 tmp_path**。

    macOS 的 tmp_path 在 /private/var/folders/ 下，那是 pathguard 的系统豁免前缀 ⇒
    放那儿的路径类用例会假绿。这组用例要判「这个文件能不能约束住」，是路径相关的。
    """
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(os.path.dirname(os.path.abspath(__file__))) / (
        f".itpolicy-tmp-{uuid.uuid4().hex[:8]}"
    )
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _write_policy(policy_dir, text: str):
    path = policy_dir / "policy.toml"
    path.write_text(text, encoding="utf-8")
    return (str(path),)


def test_policy_file_tightens_config_mode(policy_dir):
    """IT 写 readonly、用户 config 写 trusted ⇒ 必须按 readonly 算。

    这是整件事的理由：这一条红就等于 doctor 那句「由 IT 固定」是空话。
    """
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, 'mode = "readonly"\n')
    assert effective_ceiling("trusted", paths) == "readonly"


def test_policy_file_cannot_loosen_config_mode(policy_dir):
    """反方向不成立：IT 写 trusted、用户 config 写 readonly ⇒ 还是 readonly。

    方向只有一条（见 policy.py 末节「IT 统一管控」）。IT 不能替用户把他本机的权限放开。
    """
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, 'mode = "trusted"\n')
    assert effective_ceiling("readonly", paths) == "readonly"


def test_policy_file_in_the_middle_still_tightens(policy_dir):
    """阳性对照：收紧到中间档也要生效，不是只会把一切压成 readonly。"""
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, 'mode = "write"\n')
    assert effective_ceiling("trusted", paths) == "write"
    # 比它更紧的 config 不会被策略文件抬上来
    assert effective_ceiling("readonly", paths) == "readonly"


def test_missing_policy_file_falls_back_to_config(policy_dir):
    """绝大多数机器没有策略文件 ⇒ 完全按 config 走，而且不许抛。"""
    from bridge.policy import effective_ceiling

    missing = (str(policy_dir / "nope.toml"),)
    assert effective_ceiling("trusted", missing) == "trusted"
    assert effective_ceiling("readonly", missing) == "readonly"


def test_garbage_policy_file_falls_back_to_config(policy_dir):
    """坏 TOML 当没有策略 —— 服务起不来比没锁住更糟（用户整个功能没了）。"""
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, "mode = \nthis is not toml ][\n")
    assert effective_ceiling("trusted", paths) == "trusted"


def test_policy_file_without_mode_key_falls_back_to_config(policy_dir):
    """只写了别的字段（workspaces 之类，我们刻意不支持）⇒ 当没固定 mode。

    doctor 这种情况打印的也是「有 IT 策略文件，但没固定 mode」，两边口径一致。
    """
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, 'workspaces = ["/Users/x/codes"]\n')
    assert effective_ceiling("trusted", paths) == "trusted"


def test_unreadable_policy_file_falls_back_to_config(policy_dir):
    """没权限读（IT 放成 0600 root）也不许崩。"""
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, 'mode = "readonly"\n')
    os.chmod(paths[0], 0o000)
    try:
        if os.access(paths[0], os.R_OK):
            pytest.skip("当前用户照样读得到（root?），这条判不出来")
        assert effective_ceiling("trusted", paths) == "trusted"
    finally:
        os.chmod(paths[0], 0o600)


def test_unknown_mode_in_policy_file_clamps_to_readonly(policy_dir):
    """🔴 IT 把值拼错（`read-only`）⇒ 压到 readonly，**不许**当没有策略。

    当没有策略在这里等于把一个拼错翻译成静默放开 —— 正是这个模块要消灭的那类失败。
    方向不同于「文件坏了」：那时根本没读到意图，这时意图明确（他写了 mode）。
    """
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, 'mode = "read-only"\n')
    assert effective_ceiling("trusted", paths) == "readonly"


def test_unknown_mode_in_user_config_is_not_silently_rewritten(policy_dir):
    """用户 config.toml 的 mode 拼错 ⇒ **原样传下去**让 Policy 抛错，不在这里改成 readonly。

    方向和策略文件那条刻意相反：这里悄悄改成 readonly 的话，一个拼错会表现成
    「它怎么不能改文件了」，而 config.toml 看上去毫无问题。让 PolicyError 把
    「mode 只能是 readonly / write / trusted」说出来。
    """
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, 'mode = "readonly"\n')
    assert effective_ceiling("yolo", paths) == "yolo"
    with pytest.raises(PolicyError):
        Policy(
            mode=effective_ceiling("yolo", paths),
            workspaces=(WS,),
            default_workspace=WS,
            model="claude-haiku-5-5",
            turn_timeout_sec=60,
        )


def test_nested_mode_matches_what_doctor_greps(policy_dir):
    """IT 照 config.toml 的样子写 `[agent]` + mode ⇒ 也要认。

    doctor 两边都是按行抓 mode 的，这种写法它会打印「由 IT 固定」。
    桥只认顶层的话，同一个 bug 就有了第二形态。
    """
    from bridge.policy import effective_ceiling

    paths = _write_policy(policy_dir, '[agent]\nmode = "readonly"\n')
    assert effective_ceiling("trusted", paths) == "readonly"


def test_multiple_policy_files_take_the_strictest(policy_dir):
    """多个来源取最严 —— 这是 CARHER_POLICY_FILE 不能用来提权的原因。"""
    from bridge.policy import ceiling_from_files

    (policy_dir / "a.toml").write_text('mode = "trusted"\n', encoding="utf-8")
    (policy_dir / "b.toml").write_text('mode = "readonly"\n', encoding="utf-8")
    both = (str(policy_dir / "a.toml"), str(policy_dir / "b.toml"))
    assert ceiling_from_files(both) == "readonly"
    assert ceiling_from_files(tuple(reversed(both))) == "readonly"


def test_env_override_is_additive_not_a_replacement(policy_dir, monkeypatch):
    """🔴 `CARHER_POLICY_FILE` 只是**多一个**来源，不能顶掉系统位置。

    顶掉的话用户给自己家目录里的 LaunchAgent plist 加一行环境变量就绕过了 IT ——
    改那个 plist 不需要管理员权限。
    """
    from bridge.policy import policy_files, system_policy_file

    monkeypatch.setenv("CARHER_POLICY_FILE", str(policy_dir / "policy.toml"))
    paths = policy_files()
    assert system_policy_file() in paths, f"系统位置被顶掉了：{paths}"
    assert str(policy_dir / "policy.toml") in paths


def test_no_env_override_means_only_the_system_path(monkeypatch):
    """阳性对照：不设环境变量时就只看系统那一个位置。"""
    from bridge.policy import policy_files, system_policy_file

    monkeypatch.delenv("CARHER_POLICY_FILE", raising=False)
    assert policy_files() == (system_policy_file(),)


def test_policy_path_matches_what_doctor_reads(monkeypatch):
    """桥读的路径必须和 doctor 打印的是同一个 —— 飘开就又回到「说了没做」。"""
    import bridge.policy as policy_mod

    oneclick = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..",
                     "feishu-agent-oneclick")
    )
    with open(os.path.join(oneclick, "src", "doctor-mac.sh"), encoding="utf-8") as fh:
        mac = fh.read()
    assert policy_mod.MAC_POLICY_FILE in mac

    with open(os.path.join(oneclick, "src", "doctor-windows.ps1"), encoding="utf-8") as fh:
        win = fh.read()
    # Windows 侧 doctor 用 $env:ProgramData 拼，比不了整串 ⇒ 比尾巴那一段。
    # 走 Windows 分支靠改 sys.platform（policy.py 里是现读的，没缓存成常量）。
    assert "CarHer\\policy.toml" in win
    monkeypatch.setattr(policy_mod.sys, "platform", "win32")
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    win_path = policy_mod.system_policy_file()
    assert win_path.endswith(os.path.join("CarHer", "policy.toml")), win_path
    assert "ProgramData" in win_path


def test_it_ceiling_pins_the_chat_mode_switch(policy_dir):
    """IT 收紧之后，用户在飞书里点「权限」也提不上去 —— 包括旧的落盘偏好。

    prefs 已经用 clamp_mode(requested, ceiling_mode)，但这条要钉死：
    IT 锁之前用户点过 trusted、prefs.json 里还留着，重启后不能照着那张旧票提权。
    """
    import json

    from bridge.policy import effective_ceiling
    from bridge.prefs import PrefsStore

    paths = _write_policy(policy_dir, 'mode = "readonly"\n')
    ceiling = effective_ceiling("trusted", paths)
    base = Policy(
        mode=ceiling,
        workspaces=(WS,),
        default_workspace=WS,
        model="claude-haiku-5-5",
        turn_timeout_sec=60,
        ceiling_mode=ceiling,
    )

    prefs_path = str(policy_dir / "prefs.json")
    with open(prefs_path, "w", encoding="utf-8") as handle:
        json.dump({"mode": "trusted"}, handle)

    prefs = PrefsStore(prefs_path, base)
    assert prefs.policy().mode == "readonly", "旧的落盘偏好绕过了 IT 上限"
    assert prefs.set_mode("trusted") == ("readonly", True)
    assert base.can_switch_mode() is False, "只剩一档时菜单里不该出现「权限」"
