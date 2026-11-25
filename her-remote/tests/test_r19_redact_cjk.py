"""第 19 轮 W3：密钥**紧贴中文字**写时原来一条都不脱敏。

`\\b` 在 Python 里把中文当 `\\w` ⇒ 「是」和 `sk-` 之间没有词边界 ⇒ 不匹配 ⇒ 原样出网
（回复、状态卡、审批卡、流式卡全走 redact）。真人最常见的写法恰恰是「密钥是sk-…」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bridge.redact import MASK, redact  # noqa: E402

A36 = "a" * 36
A16 = "A" * 16


@pytest.mark.parametrize("prefix", ["密钥是", "token为", "用这个：", "（", "值", "\n"])
@pytest.mark.parametrize("secret", [
    "sk-proj-" + A36, "sk-ant-api03-" + A36, "ghp_" + A36, "github_pat_" + A36,
    "AKIA" + A16, "xoxb-" + "1" * 12, "AIza" + A36, "cli_" + "a" * 16,
    "eyJ" + A36 + "." + A36 + "." + A36,
])
def test_secret_glued_to_cjk_is_masked(prefix, secret):
    out, n = redact(prefix + secret + "，记得别外传")
    assert n >= 1 and secret not in out and MASK in out, out


@pytest.mark.parametrize("text", [
    "mask-" + A36,               # 普通英文词里带 sk- ⇒ 不能误伤
    "task-" + A36,
    "x" + "AKIA" + A16,
    "AKIA" + A16 + "B",          # 长度不对
    "cli_" + "a" * 16 + "f",
])
def test_lookalikes_inside_ascii_words_untouched(text):
    assert redact(text) == (text, 0)
