"""卡片 / 回复排版（polish-1105 第二节）：代码成块、截断不留半个代码块、先脱敏再排版。

飞书实际支持什么是 2026-11-05 查的官方文档（见 bridge/reply.py 末尾「排版」一节），不是凭印象：
- 卡片 markdown：```lang 代码块必须在行首；转义用 HTML 实体
- post 的 md：CommonMark + GFM，**不支持缩进代码块**；富文本和卡片一样 **30KB**
"""

from __future__ import annotations

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge import reply as mdfmt  # noqa: E402
from bridge.reply import (  # noqa: E402
    FENCE,
    action_card_md,
    card_snippet,
    md_chunk as chunk,
    escape_card,
    fence,
    normalize,
    normalize_lang,
    split_blocks,
    truncate,
)


def _fences_balanced(md: str) -> bool:
    """行首的 ``` 个数是偶数 = 没有没关上的代码块。"""
    return sum(1 for line in md.split("\n") if line.startswith(FENCE)) % 2 == 0


def _code(n: int, lang: str = "python") -> str:
    return f"```{lang}\n" + "\n".join(f"value_{i} = {i}  # 第 {i} 行" for i in range(n)) + "\n```"


# ---------- 语言名 ----------


@pytest.mark.parametrize("info,want", [
    ("python", "python"), ("py", "python"), ("PYTHON", "python"), ("sh", "bash"), ("ts", "typescript"),
    ("yml", "yaml"), ("Dockerfile", "docker_file"), ("python title=\"x.py\"", "python"),
    ("", ""), ("text", ""), ("plain_text", ""), ("brainfuck", ""), ("{.python}", "python"),
])
def test_lang_maps_to_feishu_names(info, want):
    """飞书不认的语言名一律当纯文本 —— 写一个它不认的名字，渲染效果没保证。"""
    assert normalize_lang(info) == want


# ---------- 拆块 ----------


def test_split_blocks_basic():
    blocks = split_blocks("前\n```py\na = 1\n```\n后")
    assert [b.kind for b in blocks] == ["text", "code", "text"]
    assert blocks[1].lang == "python" and blocks[1].body == "a = 1"


def test_tilde_fence_and_longer_fence_are_normalized():
    out = normalize("~~~js\nx()\n~~~\n````\ny\n````")
    assert "```javascript\nx()\n```" in out
    assert "```\ny\n```" in out
    assert "~~~" not in out and "````" not in out


def test_unclosed_fence_is_closed():
    out = normalize("看：\n```bash\nls -la")
    assert _fences_balanced(out) and out.endswith("ls -la\n```")


def test_backticks_inside_code_cannot_close_the_block():
    """代码里本来就有 ```（比如 markdown 文件的内容）⇒ 打断，不能把块提前关掉。"""
    out = fence("echo '```'\nrm -rf x", "bash")
    inner = out.split("\n")[1:-1]
    assert not any(line.startswith(FENCE) for line in inner)
    assert _fences_balanced(out)


def test_inline_triple_backtick_is_not_a_fence():
    blocks = split_blocks("用 ```x``` 包起来")
    assert [b.kind for b in blocks] == ["text"]


# ---------- 没包围栏的成段代码 ----------


def test_bare_diff_gets_fenced():
    text = "改了这些：\ndiff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n\n就这样"
    out = normalize(text)
    assert "```diff\ndiff --git a/x.py b/x.py" in out
    assert out.endswith("就这样") and _fences_balanced(out)


def test_markdown_list_is_not_mistaken_for_diff():
    """阴性对照：「- 第一条」是列表，不能被包成 diff。"""
    text = "要点：\n- 第一条\n- 第二条\n+ 第三条"
    assert normalize(text) == text


def test_log_lines_get_fenced():
    text = "日志：\n2026-11-05 11:37:20,001 INFO a\n2026-11-05 11:37:21,002 ERROR b\n完"
    out = normalize(text)
    assert "```\n2026-11-05 11:37:20,001 INFO a\n2026-11-05 11:37:21,002 ERROR b\n```" in out


def test_timeline_prose_is_not_a_log():
    """阴性对照：不带秒的时间线是正文。"""
    text = "2026-11-05 11:37 同意了\n2026-11-05 11:38 又同意了"
    assert normalize(text) == text


def test_traceback_gets_fenced():
    text = ('出错了\nTraceback (most recent call last):\n  File "x.py", line 1, in <module>\n'
            "    foo()\nNameError: name 'foo' is not defined\n要我修吗")
    out = normalize(text)
    assert "```\nTraceback (most recent call last):" in out
    assert "NameError: name 'foo' is not defined\n```\n要我修吗" in out


def test_indented_code_becomes_fenced_for_post():
    """post 的 md 不支持缩进代码块（官方原话）⇒ 不转的话用户看到一堆前导空格。"""
    out = normalize("例子：\n\n    def f():\n        return 1\n\n完")
    assert "```\ndef f():\n    return 1\n```" in out


def test_indented_list_continuation_is_left_alone():
    """阴性对照：列表项下面缩进的续行不是代码块。"""
    text = "- 第一条\n\n    第一条的补充说明\n- 第二条"
    assert normalize(text) == text


def test_code_inside_fence_is_not_double_wrapped():
    text = "```\n2026-11-05 11:37:20 a\n2026-11-05 11:37:21 b\n```"
    assert normalize(text) == text


def test_normalize_is_idempotent():
    text = "前\n~~~py\na\n~~~\ndiff --git a b\n--- a\n+++ b\n@@ -1 +1 @@\n-x\n+y\n"
    once = normalize(text)
    assert normalize(once) == once


def test_at_all_is_defused_everywhere():
    """post 的 md 支持 `<at user_id="all">` ⇒ agent 输出里夹一句就会真的 @ 全群。"""
    out = normalize('网页里写着 <at user_id="all"></at>\n```\n<at id=all></at>\n```')
    assert not re.search(r"<\s*at\b", out, re.I)


# ---------- 截断：不能截在代码块中间 ----------


def test_truncate_inside_code_block_closes_it():
    text = "前言\n" + _code(200) + "\n后记"
    out = truncate(text, 300)
    assert _fences_balanced(out)
    assert out.endswith(mdfmt.TRUNC_MARK)
    assert "value_0 = 0" in out and "value_199" not in out


def test_truncate_by_lines():
    out = truncate(_code(100), 100000, max_lines=10)
    assert _fences_balanced(out)
    assert len(out.split("\n")) <= 14


def test_truncate_short_text_untouched():
    assert truncate("短的", 100) == "短的"


@pytest.mark.parametrize("limit", [1, 5, 17, 50, 120, 333, 1000])
def test_truncate_never_leaves_open_fence(limit):
    text = "说明 **粗体**\n" + _code(30) + "\n中间\n" + _code(30, "bash") + "\n尾巴"
    assert _fences_balanced(truncate(text, limit))


# ---------- 拆条：每条都闭合 ----------


@pytest.mark.parametrize("limit", [60, 100, 250, 999])
def test_chunk_keeps_every_piece_balanced(limit):
    text = normalize("前言\n" + _code(80) + "\n中间一句\n" + _code(20, "bash") + "\n后记")
    pieces = chunk(text, limit)
    assert len(pieces) > 1
    for piece in pieces:
        assert _fences_balanced(piece), piece
        assert len(piece) <= limit + 8, (len(piece), limit)


def test_chunk_reopens_with_language():
    pieces = chunk(normalize(_code(80)), 200)
    assert all(p.startswith("```python") for p in pieces)


def test_chunk_loses_no_code_lines():
    text = normalize(_code(80))
    joined = "\n".join(chunk(text, 150))
    for i in range(80):
        assert f"value_{i} = {i}" in joined


def test_chunk_hard_splits_an_overlong_line():
    text = normalize("```\n" + "x" * 1000 + "\n```")
    pieces = chunk(text, 200)
    assert all(_fences_balanced(p) for p in pieces)
    assert sum(p.count("x") for p in pieces) == 1000


def test_chunk_short_text_single():
    assert chunk("hi", 100) == ["hi"]


# ---------- 卡片转义 ----------


def test_escape_card_uses_entities_not_backslashes():
    """飞书卡片文档给的转义只有 HTML 实体，没说支持反斜杠。"""
    out = escape_card("a_b *c* [x](y) <b> #h `z` ~s")
    assert "\\" not in out
    shown = re.sub(r"&(#\d+|sim);", "", out)  # 去掉实体后剩下的字符里不能再有标记
    for raw in ("_", "*", "[", "]", "<", ">", "#", "`", "~"):
        assert raw not in shown, raw


def test_escape_card_keep_format_keeps_bold_and_inline_code():
    out = escape_card("**重点** 看 `a_b<c>.py`，别点 [x](http://evil)", keep_format=True)
    assert "**重点**" in out
    assert "`a_b<c>.py`" in out, "行内代码里面原样，不转"
    assert "[x]" not in out, "链接要被打断"


def test_escape_card_keeps_existing_entities_literal():
    """原文里的 `&#60;` 要原样显示，不能被渲染成 <。"""
    import html

    for raw in ("写 &#60; 就是小于号", "a &amp; b", "<x> & [y]_z"):
        assert html.unescape(escape_card(raw)) == raw, "按实体渲染后必须等于原文"


def test_card_snippet_at_all_cannot_reach_card():
    """没开「@所有人」权限的群里，卡片带 <at id=all> 会整张发不出去（卡片文档原话）。"""
    for text in ("<at id=all></at>", "`<at id=all></at>`", "```\n<at id=all></at>\n```"):
        out = card_snippet(text, 500)
        assert not re.search(r"<\s*at\b", out, re.I), out


# ---------- 审批卡 ----------


def test_bash_action_is_a_code_block_and_byte_exact():
    """命令进 ```bash 代码块，而且一个字不改（批准的必须就是会跑的那条）。"""
    command = 'node ~/.claude/carher/carher_image.js "画个 *农家* 田园 [图]" ~/codes/farm_countryside.png'
    out = action_card_md(f"跑这条命令：\n{command}")
    assert f"```bash\n{command}\n```" in out


def test_multiline_bash_action_keeps_lines():
    command = "cd /tmp && \\\n  ls -la"
    assert f"```bash\n{command}\n```" in action_card_md(f"跑这条命令：\n{command}")


def test_long_bash_action_is_truncated_balanced():
    out = action_card_md("跑这条命令：\n" + "echo " + "x" * 5000, limit=1500)
    assert _fences_balanced(out) and mdfmt.TRUNC_MARK in out
    assert len(out) < 1700


def test_bash_action_with_fence_in_command_stays_balanced():
    out = action_card_md("跑这条命令：\nprintf '```\\nhi\\n```' > a.md")
    assert _fences_balanced(out)
    assert out.split("\n")[-1] == FENCE


def test_file_action_path_is_inline_code():
    assert action_card_md("修改文件：/tmp/my_app/a_b.py") == "修改文件：`/tmp/my_app/a_b.py`"


def test_other_tool_json_is_a_code_block():
    out = action_card_md('WebFetch：{"url": "https://x.y/a_b"}')
    assert out.startswith("WebFetch：\n```json\n")
    assert _fences_balanced(out)


def test_unknown_action_shape_is_escaped():
    out = action_card_md("随便一段 <at id=all></at> [x](y)")
    assert "[x]" not in out and not re.search(r"<\s*at\b", out)


# ---------- 真实 describe_action 输出 ----------


def test_describe_action_outputs_render_as_code():
    from bridge.approval import describe_action

    out = action_card_md(describe_action("Bash", {"command": "git status && ls ~/x_y", "timeout": 1}))
    assert "```bash\ngit status && ls ~/x_y\n```" in out
    out = action_card_md(describe_action("Edit", {"file_path": "/a/b_c.py", "old_string": "x"}))
    assert "`/a/b_c.py`" in out
    out = action_card_md(describe_action("WebSearch", {"query": "a_b"}))
    assert "```json" in out and _fences_balanced(out)


def test_card_markdown_is_valid_json_payload():
    """排版结果塞进卡片 JSON 后能被解析（零宽空格、实体都不影响 JSON）。"""
    md = card_snippet("```\n```x\n```\n<at id=all>", 500)
    assert json.loads(json.dumps({"content": md}, ensure_ascii=False))["content"] == md


# ---------- reply.py：post 富文本 ----------


class _PostClient:
    """记下每条发出去的 (msg_type, 正文)。`fail_post_over` 字节以上的 post 返回 230025。"""

    def __init__(self, fail_post_over: int = 30 * 1024, fail_all_post: bool = False):
        import types

        self.sent: list[tuple[str, str, int]] = []
        outer = self

        class _Msg:
            def create(self, request):
                body = request.request_body
                size = len(body.content.encode("utf-8"))
                content = json.loads(body.content)
                if body.msg_type == "post":
                    text = content["zh_cn"]["content"][0][0]["text"]
                    ok = not fail_all_post and size <= fail_post_over
                elif body.msg_type == "text":
                    text, ok = content["text"], True
                else:
                    text, ok = body.content, True
                if ok:
                    outer.sent.append((body.msg_type, text, size))
                return types.SimpleNamespace(success=lambda: ok, code=0 if ok else 230025, msg="")

        self.im = types.SimpleNamespace(v1=types.SimpleNamespace(message=_Msg()))


def test_reply_fences_bare_diff():
    from bridge.reply import Replier

    client = _PostClient()
    Replier(client, 20000).send("oc_x", "改了：\ndiff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b")
    kind, text, _ = client.sent[-1]
    assert kind == "post" and "```diff\n" in text and _fences_balanced(text)


def test_reply_redacts_before_formatting():
    """先脱敏再排版：key 在代码块里也一样被抹掉。"""
    from bridge.reply import Replier

    key = "sk" + "-proj-" + "B" * 32
    client = _PostClient()
    Replier(client, 20000).send("oc_x", f"```bash\nexport OPENAI_API_KEY={key}\n```")
    assert all(key not in text for _, text, _ in client.sent)


def test_long_cjk_reply_is_split_to_fit_post_30kb():
    """🔴 富文本和卡片一样只有 30KB（官方）。默认 20000 字一段，中文 ≈ 60KB ⇒ 原来必回 230025、
    整段静默退成纯文本，代码块全变成反引号。现在先切到预算内，每条都是闭合的代码块。"""
    from bridge.reply import RICH_MAX_BYTES, Replier

    text = "说明\n```python\n" + "\n".join(f"# 中文注释第{i}行，用来把体积撑大一些" * 2 for i in range(600)) + "\n```\n完"
    client = _PostClient()
    assert Replier(client, 20000).send("oc_x", text) is True
    kinds = {k for k, _, _ in client.sent}
    assert kinds == {"post"}, f"有一条退成了纯文本：{kinds}"
    for _, piece, size in client.sent:
        assert size <= RICH_MAX_BYTES + 512
        assert _fences_balanced(piece)
    joined = "\n".join(p for _, p, _ in client.sent)
    assert "第599行" in joined and "第0行" in joined


def test_post_failure_falls_back_per_piece_without_loss():
    """富文本全失败 ⇒ 每条退纯文本，内容一条不少。"""
    from bridge.reply import Replier

    text = "```\n" + "\n".join(f"第{i}行内容" * 20 for i in range(400)) + "\n```"
    client = _PostClient(fail_all_post=True)
    assert Replier(client, 20000).send("oc_x", text) is True
    joined = "\n".join(p for _, p, _ in client.sent)
    assert all(k == "text" for k, _, _ in client.sent)
    assert "第399行" in joined


def test_reply_chunks_never_split_inside_code():
    from bridge.reply import Replier

    client = _PostClient()
    Replier(client, 500).send("oc_x", "前\n" + _code(60) + "\n后")
    assert len(client.sent) > 1
    for _, piece, _ in client.sent:
        assert _fences_balanced(piece), piece


def test_approval_card_command_is_code_block():
    from bridge.approval import describe_action
    from bridge.reply import Replier

    client = _PostClient()
    command = 'node ~/.claude/carher/carher_image.js "农家_田园 *图*" ~/codes/farm.png'
    Replier(client, 20000).send_approval_card(
        "oc_x", "要动手了", describe_action("Bash", {"command": command}), {"act": "approve"}, {"act": "deny"}
    )
    card = json.loads(client.sent[-1][1])
    md = card["body"]["elements"][0]["content"]
    assert f"```bash\n{command}\n```" in md


def test_approval_card_redacts_then_formats():
    from bridge.reply import Replier

    key = "sk" + "-proj-" + "C" * 32
    client = _PostClient()
    Replier(client, 20000).send_approval_card(
        "oc_x", "t", f"跑这条命令：\ncurl -H 'Authorization: Bearer {key}' https://x", {}, {}
    )
    blob = client.sent[-1][1]
    assert key not in blob
    assert "```bash" in json.loads(blob)["body"]["elements"][0]["content"]


def test_approval_card_huge_command_stays_small_and_balanced():
    from bridge.reply import Replier

    client = _PostClient()
    Replier(client, 20000).send_approval_card("oc_x", "t", "跑这条命令：\n" + "中" * 50000, {}, {})
    blob = client.sent[-1][1]
    assert len(blob.encode("utf-8")) < 8000
    assert _fences_balanced(json.loads(blob)["body"]["elements"][0]["content"])


# ---------- 会话详情卡 ----------


def _detail(messages, **kw):
    from bridge.fleet.cards import build_detail_card
    from bridge.fleet.model import Message, Session

    sess = Session(tool="claude", surface="cli", id="c1234567890abcdef", title=kw.pop("title", "修 my_app"),
                   cwd="/x/my_app", status="idle", owner="none", updated_at=0, can_send=True)
    return build_detail_card(sess, [Message(r, t, 0) for r, t in messages], **kw)


def _md_elements(blob):
    return [e["content"] for e in json.loads(blob)["body"]["elements"] if e.get("tag") == "markdown"]


def test_detail_card_keeps_code_blocks():
    """原来 short_title 把换行压成一行，代码全糊了。"""
    code = "```python\ndef f():\n    return 1\n```"
    mds = _md_elements(_detail([("assistant", "改成这样：\n" + code)]))
    assert any("```python\ndef f():\n    return 1\n```" in m for m in mds)


def test_detail_card_message_starting_with_code_puts_fence_on_its_own_line():
    """代码块的 ``` 必须在行首（卡片文档原话）。"""
    mds = _md_elements(_detail([("assistant", "```\nls\n```")]))
    msg = next(m for m in mds if "ls" in m)
    assert "\n```\nls\n```" in msg


def test_detail_card_long_code_truncated_balanced():
    from bridge.fleet.cards import MAX_CARD_BYTES

    big = "```\n" + ("中" * 200 + "\n") * 400 + "```"
    blob = _detail([("assistant", big)] * 4)
    assert len(blob.encode("utf-8")) < MAX_CARD_BYTES
    for md in _md_elements(blob):
        assert _fences_balanced(md), md


def test_detail_card_redacts_key_inside_code_block():
    from bridge.app import _redact_text

    key = "sk" + "-ant-" + "D" * 40
    blob = _detail([("assistant", f"```bash\nexport ANTHROPIC_API_KEY={key}\n```")], redact=_redact_text)
    assert key not in blob


def test_detail_card_message_text_cannot_inject_markup():
    mds = _md_elements(_detail([("user", "点 [这里](http://evil) <at id=all></at> # 标题")]))
    msg = next(m for m in mds if "evil" in m)
    assert "[这里]" not in msg and not re.search(r"<\s*at\b", msg) and "\n#" not in msg


def test_detail_card_session_id_and_project_are_inline_code():
    head = _md_elements(_detail([]))[0]
    assert "`my_app`" in head and "`c1234567`" in head


# ---------- 状态卡 / 菜单 ----------


def test_status_card_unknown_tool_is_inline_code():
    """`mcp__x__y` 裸写的话 `__` 被当成粗体吃掉。"""
    from bridge.status import StatusCard

    text = StatusCard(None, "oc")._render(5, 2, "mcp__lark__send")
    assert "`mcp__lark__send`" in text


def test_menu_card_workspace_is_inline_code():
    from bridge.menu import build_menu_card

    assert "`~/codes/my_app`" in build_menu_card("~/codes/my_app")
