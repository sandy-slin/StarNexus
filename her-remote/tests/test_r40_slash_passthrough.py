"""斜杠命令透传（第 40 轮，2026-11-22）。

用户原话：「我输入的时候，能不能每一个字符都直接转换到那边的输入？
比如说 /goal（slash goal），它能不能直接输入？」

# 实测拿到的三件事（都是真跑 claude 得到的，不是读码推的）

**① 桥自己那条路（`-p --resume`）：斜杠命令真能跑。**
`/probe-goal`（自定义）回 `GOALCMDRAN`；`/context` 回完整 token 表；
`/goal` 真设了目标、之后每轮都带着。另一条独立证据：`claude --help` 里有
`--disable-slash-commands`「Disable all skills and commands」——这个 flag 存在
就说明 `-p` 下默认是**开**的。

**② 对话模式（会话间消息）：送不进去，是上游关的。**
入队时写死 `skipSlashCommands:!0`。成对实测同一条 `/probe-goal`：
直接进目标 stdin ⇒ 回 `GOALCMDRAN`（真跑）；经会话间消息 ⇒ 目标回
「这条命令是从别的会话来的、不是你打的，我没跑」。

**③ 上游 `-p` 下实测 51 条命令，真正有害的只有两条。**
`/exit` `/logout` `/permissions` `/export` `/memory` `/bug` `/install-github-app`
一律回「isn't available in this environment」、0 turn、0 花费 —— 上游自己关的。
`/compact` 安全（sid 不变、上下文保留）。
只有 `/clear`（换 sid ⇒ 上下文断、而手机上只看到「这一轮没出结果」）
和 `/init`（真写 CLAUDE.md，3 个 turn）要拦。
"""

from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.menu import (  # noqa: E402
    DESTRUCTIVE,
    _closest_command,
    destructive_command,
    known_commands,
    unknown_command,
    unknown_command_reply,
)


# ---------- 黑名单只许有这两条 ----------

def test_blocklist_has_only_two():
    """🔴 防的是以后有人往里塞名字，把它变成第二份会漂的上游清单。

    加名字的唯一理由是「后果用户在手机上看不见、而且事后自己发现不了」。
    `/exit` `/logout` 这些上游自己在 `-p` 下就关了（实测 0 turn、0 花费），
    写进来 = 维护一份会漂的清单，还会在上游放开时静默挡住一条本该能用的命令。
    """
    assert set(DESTRUCTIVE) == {"/clear", "/init"}, (
        f"黑名单变成 {set(DESTRUCTIVE)} —— 加名字之前先读 menu.DESTRUCTIVE 那段红字")


def test_destructive_matches_whole_command_only():
    """只认整条就是这个命令（可带参数）；`/clearcache` 这种不是它。"""
    assert destructive_command("/clear") == "/clear"
    assert destructive_command("/init") == "/init"
    assert destructive_command("/init 一下") == "/init"
    assert destructive_command("  /CLEAR  ") == "/clear"      # 大小写、空白都归一
    assert destructive_command("/clearcache") == ""
    assert destructive_command("/initial") == ""
    # 阴性对照：安全的命令和普通话一个都不许进这个桶
    for safe in ("/compact", "/context", "/goal 把测试跑绿", "/usage", "普通一句话", ""):
        assert destructive_command(safe) == "", safe


# ---------- 判据：从「名字在不在表里」换成「猜得中」 ----------

def test_upstream_commands_are_no_longer_blocked():
    """🔴 这是本轮的核心修复。

    改之前：`/goal` `/context` `/compact` `/usage` 全被当成「打错的命令」拦掉，
    回一句「没有这个用法」—— **而它们实测真能跑**。
    原因：判据是「不在 `known_commands()` 里」，而上游 `-p` 下有 51 条命令，
    我们的表只有桥自己那二十来条。追着往表里加是打地鼠。
    """
    for cmd in ("/goal 把测试跑绿", "/context", "/compact", "/usage",
                "/recap", "/effort", "/security-review", "/import"):
        assert unknown_command(cmd) == "", f"{cmd} 又被拦了 —— 它在上游真能跑"
        assert unknown_command_reply(cmd) is None, cmd


def test_typos_are_still_caught():
    """打错字照旧当场拦，一分钱不花（11-05 那个场景原样保住）。

    阳性对照：这几个都要猜得中，而且猜的是对的那条。
    """
    for typo, want in (("/sessoins", "/sessions"), ("/stauts", "/status"),
                       ("/moedl", "/model")):
        assert unknown_command(typo) == typo, f"{typo} 漏过去了 —— 打错字会白跑一轮"
        assert _closest_command(typo) == want, (typo, _closest_command(typo))
        reply = unknown_command_reply(typo)
        assert reply is not None and want in reply[0], (typo, reply)


def test_known_bridge_commands_still_pass_through():
    """阴性对照：桥自己的命令一个都不许进「打错的」那个桶（遍历全量，防漂）。"""
    for cmd in known_commands():
        assert unknown_command(cmd) == "", f"{cmd} 是桥自己的命令，不该被当成打错的"


def test_absolute_paths_are_not_commands():
    """`/Users/me/codes 里有什么` 是个正常问题，不是打错的命令（原有行为，别退回去）。"""
    assert unknown_command("/Users/me/codes 里有什么") == ""
    assert unknown_command("/tmp/x/y 这个目录") == ""


# ---------- 接线：三道闸门按源码盯住 ----------

def _app_src() -> str:
    import bridge.app as app
    return open(app.__file__, encoding="utf-8").read()


def test_destructive_gate_is_wired_before_the_typo_gate():
    """🔴 顺序：写死拦下的两条要在「打错的命令」兜底**之前**。

    反了的话 `/clear` 先被兜底判一次 —— 而它现在猜不中任何已知命令 ⇒ 兜底放行
    ⇒ 直接透传给 claude ⇒ 上下文被悄悄清掉，正是这一轮要修的事。
    """
    src = _app_src()
    i_destructive = src.index("blocked = destructive_command(text)")
    i_typo = src.index("guessed = unknown_command_reply(text)")
    assert i_destructive < i_typo, "写死拦下的那两条排在兜底后面了 ⇒ /clear 会漏过去"


def test_command_dispatch_runs_before_talk_mode():
    """🔴 `/cancel` `/stop` 这些**桥自己的**命令必须先被分派掉。

    否则在对话模式下它们会撞上「斜杠命令发不进去」那一拦 —— 用户想退出对话
    却被告知「到电脑上自己打」，等于退不出来。
    """
    src = _app_src()
    i_cmd = src.index("if await self._handle_command(scope, chat_id, text):")
    i_fleet = src.index("if await self._maybe_send_to_fleet(scope, chat_id, text):")
    assert i_cmd < i_fleet, "命令分派排到对话模式后面了 ⇒ /cancel 退不出对话模式"


def test_slash_gate_is_in_relay_not_in_app():
    """🔴 拦截必须在 `relay.send` 里，**不能**在 `app._maybe_send_to_fleet`。

    2026-11-22 当天就撞了：第一版拦在 app.py，主人在群里打 `/goal 看看天气`，
    回的是「斜杠命令发不进 Claude「198 k3s 节点硬盘」」，他问「是我没更新吗？」
    —— **那条会话本来能跑**。

    原因：对话模式有**两条**路，`control.send()` 在动手那一刻按
    「终端有没有人占着」现判：
      没人占着 ⇒ `claude -p --resume` ⇒ 斜杠命令**真执行**
      终端占着 ⇒ relay ⇒ 上游写死 `skipSlashCommands` ⇒ 真跑不了
    拦在 app.py 等于把能跑的那条也拦了，而现场多数会话是前一种。
    **判据必须和选路在同一个地方。**
    """
    import bridge.fleet.relay as relay_mod

    app_src = _app_src()
    assert "_COMMAND_SHAPE.match(_first)" not in app_src, \
        "斜杠拦截又跑回 app.py 了 ⇒ 会把 `-p --resume` 那条能跑的路一起拦掉"

    relay_src = open(relay_mod.__file__, encoding="utf-8").read()
    assert "_looks_like_slash_command(text)" in relay_src, "relay 这条路没拦"
    # 必须在拿锁 / 起信使**之前**（否则白起一个信使进程）
    i_gate = relay_src.index("_looks_like_slash_command(text)")
    i_lock = relay_src.index("self._lock.acquire(blocking=False)")
    assert i_gate < i_lock, "拦截排在拿锁之后了"


def test_resume_lane_never_blocks_slash_commands():
    """🔴 `-p --resume` 那条路**一个字都不许拦** —— 斜杠命令在那儿实测真能跑。"""
    import bridge.fleet.claude as claude_mod

    src = open(claude_mod.__file__, encoding="utf-8").read()
    start = src.index("def _send_locked")
    body = src[start:src.index("\n    def ", start + 10)]
    assert "_looks_like_slash_command" not in body, \
        "resume 那条路加了斜杠拦截 ⇒ /goal 又发不出去了"


def test_relay_slash_copy_says_why_and_how():
    """那句话要说清**为什么是这个会话**，别让用户以为功能整个没了。

    11-22 现场的文案只说「发不进」，主人的反应是「是我没更新吗？」——
    因为他看不出这是「这个会话正开着」的个别情况。
    """
    from bridge.fleet.relay import WHY_SLASH_IN_RELAY

    assert "正开在" in WHY_SLASH_IN_RELAY, "没说清是因为那个会话正开着"
    assert "关掉" in WHY_SLASH_IN_RELAY, "没给出路（关掉窗口就能从这儿发）"


def test_relay_slash_shape_matches_menu():
    """relay 的判据和 `menu._COMMAND_SHAPE` 必须同一个口径（两处一起改）。"""
    from bridge.fleet.relay import _SLASH_SHAPE, _looks_like_slash_command
    from bridge.menu import _COMMAND_SHAPE

    assert _SLASH_SHAPE.pattern == _COMMAND_SHAPE.pattern, "两处形状漂了"
    # 阳性：真命令认得出
    for cmd in ("/goal 看看天气", "/context", "/clear", "/权限"):
        assert _looks_like_slash_command(cmd), cmd
    # 阴性：路径和普通话不许被当成命令
    for plain in ("/Users/me/codes 里有什么", "/tmp/x", "看看 /goal 是什么",
                  "普通一句话", "", "   "):
        assert not _looks_like_slash_command(plain), plain


def test_no_clear_bang_escape_hatch():
    """🔴 用户 11-22 拍的：**不要** `/clear!` 这种逃生写法。

    要清上下文就点「🆕 新话题」—— 效果等价，而且那条路桥知道自己清了、能把话说对。
    这条盯着别有人后来又加回来。

    🔴 判据必须**跳过注释**：源码里有一行注释写着「不提供 `/clear!` 这种逃生写法」，
    直接 `in src` 会抓到那行注释自己 ⇒ 第一版就这么假红的。按 AST 取字面量。
    """
    import ast

    tree = ast.parse(_app_src())
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for bang in ("/clear!", "/init!"):
        assert not any(bang in s for s in literals), \
            f"又加回 {bang} 了 —— 用户明确说不要这个例外"
    # 阳性对照：同形的既有写法确实存在 ⇒ 证明这把尺子真能看见字面量，不是恒绿
    assert any("/mode!" in s for s in literals), "尺子失效：连既有的 /mode! 都看不见"
