"""全授权档撞红线不再掐停 —— 用户：「经常由于权限问题停下来，然后要点继续，很麻烦」。

现场（某同事 11-21 截图）：一轮长活里 3 次撞红线（读密钥/凭据），整轮停住等人点「继续」。

为什么敢撤：`review.py` 自己的文档末尾写着
「**真正的防线是那次 deny 本身，掐停只是『叫人来看』**」——
操作早就被挡住了，agent 该做的是跳过继续干。
而全授权档的语义就是「什么都不问」（11-19 用户拍板），这道闸原来一个字都没看
`ctx.mode` ⇒ 全授权档照样停，那是漏洞不是设计。

只读 / 可改档**照旧停**（11-21 用户确认不放宽）。
"""

from __future__ import annotations

import os
import pathlib

import pytest

from bridge.review import Decision, ReviewContext, Reviewer, Verdict

# 他的现场：一轮里 3 次读凭据被挡
_CRED_DENIALS = [
    {"tool_name": "Read", "tool_input": {"file_path": "/Users/x/.ssh/id_rsa"}},
    {"tool_name": "Read", "tool_input": {"file_path": "/Users/x/.aws/credentials"}},
    {"tool_name": "Bash", "tool_input": {"command": "cat ~/.carher-feishu-agent/config.toml"}},
]
# 「判不出来」那条路（拿不到 tool_input）
_BLIND_DENIALS = [{"tool_name": "Bash"}, {"tool_name": "Read"}, {"tool_name": "Bash"}]


def _app_src() -> str:
    """🔴 用绝对路径读 —— `build.sh` 跑测试时 cwd 是 `feishu-agent-oneclick/`，
    相对路径 `bridge/app.py` 读不到（本轮发版第 2 步就这么红的）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return pathlib.Path(os.path.join(here, "..", "bridge", "app.py")).read_text(encoding="utf-8")


def _decide(denials, mode: str) -> Decision:
    return Reviewer().review_outcome(denials, ReviewContext(text="跑一轮长活", mode=mode))


def test_trusted_does_not_pause_on_red_lines():
    """🔴 他抱怨的那一条。"""
    d = _decide(_CRED_DENIALS, "trusted")
    assert d.verdict is Verdict.ALLOW, d
    assert d.rule == "red_line_probing_noted"


@pytest.mark.parametrize("mode", ["readonly", "write"])
def test_other_modes_still_pause(mode):
    """阴性对照：那两档用户主动选了「动手前问我」，不放宽（11-21 确认）。"""
    d = _decide(_CRED_DENIALS, mode)
    assert d.verdict is Verdict.PAUSE, d
    assert "点「继续」" in d.reason


def test_trusted_still_says_what_happened():
    """不停 ≠ 不说。用户要知道撞了红线，否则只看到「⛔ 这几步被挡下了」不知道为什么。"""
    d = _decide(_CRED_DENIALS, "trusted")
    assert "撞到了红线" in d.reason
    assert "3 次" in d.reason
    assert "照常往下做" in d.reason
    assert "点「继续」" not in d.reason


def test_trusted_does_not_pause_on_blind_denials():
    """「判不出来」那条路同样处理（原来也会停）。"""
    d = _decide(_BLIND_DENIALS, "trusted")
    assert d.verdict is Verdict.ALLOW, d
    assert d.rule == "repeated_denials_noted"


@pytest.mark.parametrize("mode", ["readonly", "write"])
def test_blind_denials_still_pause(mode):
    d = _decide(_BLIND_DENIALS, mode)
    assert d.verdict is Verdict.PAUSE, d


def test_one_red_line_never_paused_anyway():
    """阈值是 2 次 —— 撞一次本来就不停，别把这条也改了。"""
    for mode in ("trusted", "readonly", "write"):
        d = _decide(_CRED_DENIALS[:1], mode)
        assert d.verdict is Verdict.ALLOW, (mode, d)


def test_the_note_reaches_the_user():
    """🔴 `ALLOW` 带的那句话原来会被丢掉 —— app.py 只处理 PAUSE。

    不补这个 elif，用户就只看到「⛔ 这几步被挡下了」、不知道撞的是红线。
    """
    src = _app_src()
    assert "elif outcome.reason:" in src, "ALLOW 带的说明没拼进正文"
    i = src.index("elif outcome.reason:")
    assert "body +=" in src[i:i + 400]


def test_no_pause_means_no_resume_button():
    """不停就不该往 `_paused` 里记（否则下一句会被当成「在等继续」）。"""
    src = _app_src()
    i = src.index("elif outcome.reason:")
    seg = src[i:i + 400]
    assert "_paused" not in seg, "ALLOW 那支不该写 _paused"
