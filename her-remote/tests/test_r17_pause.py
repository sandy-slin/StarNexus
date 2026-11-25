"""第 17 轮：掐停判据从「被拒了几次」改成「撞了几次真红线」。

## 现场形状

一轮里被拒 3 次就把会话掐停，而「看 3 个工作目录外的文件」正好凑满 3 次 ⇒
领导远程看电脑会被莫名锁住，还要他自己点「继续」才能接着问。

**这不是推演，是实测的。** 2026-11-07 用真 claude（2.1.319、haiku、
`--permission-mode manual`、`--setting-sources ""`、`--strict-mcp-config`，
在 /tmp 下的一次性目录里跑的，已清理）发一句

    用 Read 工具读 /tmp/…/outside.txt，然后用 Bash 跑 dig +short example.com，再跑 pbpaste

拿回来的 `permission_denials` 就是 3 条，原样抄在下面的 `MEASURED_NORMAL_LOOKING`
里 ⇒ **老判据在这一轮就会掐停**。而那 3 条里一条是工作目录外的读、两条是白名单里
没有的命令，**一条红线都没撞**。

## 判据

`permission_denials` 这一个列表里混着三种完全不同的事：

| 这条拒是什么 | 例子 | 说明了什么 |
|---|---|---|
| 撞红线 | `git push`、`sudo`、`rm -rf ~`、读凭据 | 连点同意都不许的事，它还在试 |
| 边界 | `dig` / `osascript`（白名单里没有）、读工作目录外的文件 | 远程看电脑的正常样子 |
| 读不出来 | 记录里没有能判的字段 | 不知道是哪一类 |

只有第 1 类是「系统性试探」，掐停只认它，阈值收到 2 次。
第 2 类**一轮几次都不掐停**。第 3 类保持老阈值 3（见 review 里的红字）。
"""

from __future__ import annotations

import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest  # noqa: E402

from bridge.review import ReviewContext, Reviewer, Verdict  # noqa: E402

HOME = os.path.expanduser("~")


def human(text: str = "看看我的电脑", mode: str = "write") -> ReviewContext:
    """🔴 11-21：默认档从 `trusted` 改成 `write`。

    这套测试钉的是「一轮里连撞两次真红线 ⇒ 掐停让人看一眼」。
    而 11-21 用户拍板**全授权档一律不再掐停**（他原话：「经常由于权限问题停下来，
    然后要点继续，很麻烦」）—— 那一档现在只在末尾说一行，由
    `tests/test_r39_trusted_no_pause.py` 单独钉。
    这里改成 `write`（可改档）：掐停那套判据在只读 / 可改两档照旧生效，
    这套测试原本的用意（第 17 轮：别把正常用途锁住，但真撞红线要有人看）继续守住。
    """
    return ReviewContext(text=text, source="human", mode=mode)


def bash(command: str) -> dict:
    """照真 claude 返回的形状造一条 Bash 拒绝记录。

    实测形状（上面那次 2026-11-07 的一次性目录跑的）：
        {"tool_name": "Bash", "tool_use_id": "toolu_…",
         "tool_input": {"command": "dig +short example.com",
                        "description": "Resolve example.com DNS records"}}
    """
    return {"tool_name": "Bash", "tool_use_id": "toolu_x",
            "tool_input": {"command": command, "description": "d"}}


def read(path: str) -> dict:
    return {"tool_name": "Read", "tool_use_id": "toolu_y",
            "tool_input": {"file_path": path}}


def outcome(denials: list[dict], rules: tuple[str, ...] = ()):
    return Reviewer(custom_rules=rules).review_outcome(denials, human())


# 原样抄实测那一轮的三条（只把一次性目录的路径留着 —— 它就在工作目录外）
MEASURED_NORMAL_LOOKING = [
    {"tool_name": "Read", "tool_use_id": "toolu_bdrk_01N2i8q4QooAmw8QNCmsmdcq",
     "tool_input": {"file_path": "/tmp/r17probe/outside.txt"}},
    {"tool_name": "Bash", "tool_use_id": "toolu_bdrk_01W5Xp5vKKXmqUacHYgT1Ac8",
     "tool_input": {"command": "dig +short example.com",
                    "description": "Resolve example.com DNS records"}},
    {"tool_name": "Bash", "tool_use_id": "toolu_bdrk_01Yb3mYEnJH9HLEvpocMngyF",
     "tool_input": {"command": "pbpaste", "description": "Print clipboard contents"}},
]


# ======================================================================
# 一、不许掐停：远程看电脑的正常样子
# ======================================================================


def test_the_measured_turn_does_not_lock_the_session():
    """🔴 这一条就是领导撞上的那一轮，原样抄的实测记录。"""
    decision = outcome(MEASURED_NORMAL_LOOKING)
    assert decision.verdict is not Verdict.PAUSE, (
        "一次「读个目录外的文件 + 两条白名单外的命令」就把会话锁了 —— "
        f"这正是远程看电脑的常态：{decision.reason}"
    )
    assert not decision.blocked


def test_reading_five_files_outside_the_workspace_does_not_pause():
    """看 5 个工作目录外的文件 ⇒ 一次都不许锁。

    全授权档读已经放开到整台电脑（`policy._scoped_read_tools` 的 `Read(//**)`），
    但只读 / 可改档照旧按作用域拒 ⇒ 这些 denial 在那两档里是**天天出现**的。
    """
    denials = [
        read(os.path.join(HOME, "Desktop/报价单.xlsx")),
        read(os.path.join(HOME, "Downloads/合同.pdf")),
        read(os.path.join(HOME, "Documents/周报.md")),
        read(os.path.join(HOME, "codes/别的项目/README.md")),
        read("/tmp/导出的名单.csv"),
    ]
    decision = outcome(denials)
    assert decision.verdict is Verdict.ALLOW, (
        f"看 5 个目录外的文件被锁了：{decision.reason}"
    )


def test_three_tools_outside_the_allowlist_do_not_pause():
    """3 次「这个命令不在白名单里」⇒ 不许锁。

    白名单是正面清单（`cmdclass` 认不出的一律要问），所以它天然会拒掉一大片
    无害命令。把这类算进「系统性试探」就是把白名单的宽严直接接到掐停上。
    """
    decision = outcome([bash("osascript /tmp/notify.scpt"),
                        bash("dig +short example.com"),
                        bash("networksetup -listallhardwareports")])
    assert decision.verdict is Verdict.ALLOW, (
        f"三条白名单外的命令被当成试探：{decision.reason}"
    )


def test_many_boundary_denials_still_do_not_pause():
    """8 次边界类 ⇒ 还是不锁。阈值不是「调高一点」，是**这类不计数**。"""
    denials = [bash("pbpaste"), bash("say hi"), bash("dig example.com"),
               bash("osascript -l JavaScript /tmp/a.js"),
               read("/tmp/a.txt"), read("/tmp/b.txt"),
               {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}},
               {"tool_name": "Write", "tool_input": {"file_path": "/tmp/out.txt"}}]
    assert outcome(denials).verdict is Verdict.ALLOW


# ======================================================================
# 二、必须掐停：同一轮里反复撞真红线
# ======================================================================


@pytest.mark.parametrize("pair", [
    ("git push origin main", "git push -f origin main"),
    ("sudo rm /etc/hosts", "sudo -i"),
    ("rm -rf ~", 'rm -rf "$HOME"'),
    ("cat ~/.ssh/id_rsa", "cat /Users/someone/.aws/credentials"),
    ("kubectl delete pod x", "git push origin main"),      # 跨类型也算同一类试探
    ("sudo systemctl stop nginx", "cat ~/.ssh/id_ed25519"),
])
def test_two_red_line_hits_pause(pair):
    """撞 2 次红线就掐停 —— 红线是「连点同意都不许」，试第二次就是在找路。"""
    decision = outcome([bash(pair[0]), bash(pair[1])])
    assert decision.verdict is Verdict.PAUSE, (
        f"{pair} 两次红线没掐停：{decision.reason}"
    )
    assert decision.blocked


def test_red_lines_mixed_into_boundary_noise_still_pause():
    """🔴 混在正常操作里也要数出来 —— 真要绕过的 agent 不会只发红线命令。"""
    decision = outcome([bash("ls -la"), bash("dig example.com"),
                        bash("git push origin main"),
                        read("/tmp/x.txt"), bash("pbpaste"),
                        bash("cat ~/.ssh/id_rsa")])
    assert decision.verdict is Verdict.PAUSE, decision.reason


def test_reading_credentials_by_path_counts_as_red_line():
    """凭据不只走 Bash：Read 工具点名读 `.env` / 私钥也是红线。

    `policy.ALWAYS_DENY` 里 `Read(//**/.env*)` 那七条拒出来的就是这个形状。
    """
    decision = outcome([read(os.path.join(HOME, "codes/proj/.env")),
                        read(os.path.join(HOME, ".ssh/id_rsa"))])
    assert decision.verdict is Verdict.PAUSE, decision.reason


def test_one_red_line_does_not_pause():
    """阳性对照：撞一次不锁 —— 问一句「帮我推上去」很正常，拒掉就够了。"""
    decision = outcome([bash("git push origin main")])
    assert decision.verdict is Verdict.ALLOW, decision.reason


def test_one_red_line_plus_boundary_noise_does_not_pause():
    decision = outcome([bash("git push origin main"), bash("dig example.com"),
                        read("/tmp/a.txt"), bash("pbpaste")])
    assert decision.verdict is Verdict.ALLOW, decision.reason


def test_custom_rules_count_as_red_lines():
    """用户自己加的禁止项**只能更严** ⇒ 撞它两次也要掐停（抄 dots 那条）。"""
    decision = outcome([bash("deploy-to-prod.sh"), bash("bash deploy-to-prod.sh --now")],
                       rules=(r"deploy-to-prod",))
    assert decision.verdict is Verdict.PAUSE, decision.reason


def test_broken_custom_rule_does_not_crash_the_turn():
    """用户写错正则不该让一轮崩掉（同 `review_request` 的做法）。"""
    assert outcome([bash("ls")], rules=(r"[unclosed",)).verdict is Verdict.ALLOW


# ======================================================================
# 三、判不出来的照老判据走
# ======================================================================


def test_unreadable_denials_keep_the_old_threshold():
    """没有能判的字段 ⇒ 不知道是哪一类，保守按老判据（3 次）掐停。

    「判不出来」不等于「无害」—— 这里宁可锁住让人来看。
    """
    blind = [{"tool_name": "Bash"}, {"tool_name": "Bash"}, {"tool_name": "Bash"}]
    assert outcome(blind).verdict is Verdict.PAUSE
    assert outcome(blind[:2]).verdict is Verdict.ALLOW


def test_no_denials_is_clean():
    assert outcome([]).verdict is Verdict.ALLOW


# ======================================================================
# 四、文案：掐停那句话是给不写代码的同事看的
# ======================================================================


def test_pause_reason_is_plain_language():
    """不许出现内部名词，也不许把命令原文（可能带凭据）抄进去。"""
    reason = outcome([bash("cat ~/.ssh/id_rsa"), bash("sudo -i")]).reason
    assert reason
    for bad in ("tool_name", "permission_denials", "never_approvable", "denial",
                "id_rsa", "sudo", "red_line", "scope", "digest", "退出码"):
        assert bad not in reason, f"掐停那句话里有「{bad}」：{reason}"
    assert "继续" in reason or "暂停" in reason, reason


# ======================================================================
# 五、阳性对照：掐停之后「继续」照旧能解
# ======================================================================
#
# 🔴 改判据最容易顺手改坏的就是出口。掐停能进不能出的话，这一轮等于把
# 「莫名锁住」换成了「锁住还解不开」—— 比原来更糟。
# 走的是真实的命令分派（`app._handle_command`），不是抄一份分派表。


def _paused_bridge(tmp: str):
    from test_interference import _fake_bridge

    b = _fake_bridge(tmp)
    b.said = []
    b._say = lambda chat_id, text: b.said.append(text) or True
    b._say_with_buttons = lambda chat_id, text, buttons: b.said.append(text) or True
    b.workspaces = None          # 换目录分支用 getattr 兜底
    b.prefs = None
    return b


def test_resume_still_clears_a_pause_and_persists():
    """点「继续」⇒ 内存和磁盘上的暂停都解掉，下一轮能跑。"""
    import asyncio

    from test_interference import SCOPE

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _paused_bridge(tmp)
        # 按新判据真掐一次停，用它自己的话写进 _paused（和 app.py 一样）
        decision = outcome([bash("git push origin main"), bash("sudo -i")])
        assert decision.verdict is Verdict.PAUSE, "这轮的前提没成立"
        b._paused[SCOPE] = decision.reason
        b._save_paused()

        handled = asyncio.run(b._handle_command(SCOPE, "chat1", "/resume"))
        assert handled, "/resume 没被命令分派接住"
        assert SCOPE not in b._paused, "点了继续还锁着"

        fresh = _paused_bridge(tmp)
        fresh._paused_path = b._paused_path
        fresh._load_paused()
        assert SCOPE not in fresh._paused, "解锁没落盘 ⇒ 重启后又被锁住"


def test_a_paused_session_refuses_to_run_until_resumed():
    """阴性对照：没解锁之前这一轮不许跑 —— 不然掐停就是摆设。"""
    import asyncio

    from test_interference import SCOPE

    with tempfile.TemporaryDirectory(dir="/tmp") as tmp:
        b = _paused_bridge(tmp)
        b._paused[SCOPE] = "撞了两次红线"
        session = type("S", (), {"scope": SCOPE})()
        ran: list = []
        b.runner = type("R", (), {"run_turn": lambda *a, **k: ran.append(1)})()

        asyncio.run(b._run_one(session, "chat1", "接着改"))
        assert ran == [], "被掐停的会话还是跑了"
        assert b.said and "暂停" in b.said[-1]


def test_trusted_mode_never_pauses_even_for_heavy_red_lines():
    """🔴 11-21 用户拍板：全授权档**一律**不掐停，连 rm -rf ~ / sudo / git push 也不停。

    理由不是「这些不危险」，而是：**那次 deny 本身才是防线**（本文件开头的判据），
    操作早就被挡住了；掐停只是「叫人来看」，而它的代价是一轮长活被打断
    （他原话：「经常由于权限问题停下来，然后要点继续，很麻烦」）。
    说明仍然会给（见 test_r39_trusted_no_pause.py），只是不要求点击。
    """
    heavy = [
        ("git push origin main", "git push -f origin main"),
        ("sudo rm /etc/hosts", "sudo -i"),
        ("rm -rf ~", 'rm -rf "$HOME"'),
        ("kubectl delete pod a", "kubectl delete ns b"),
    ]
    for first, second in heavy:
        decision = Reviewer().review_outcome(
            [bash(first), bash(second)], human(mode="trusted"))
        assert decision.verdict is Verdict.ALLOW, (first, second, decision)
        assert decision.reason, "不停也要说一句"
        assert "点「继续」" not in decision.reason
