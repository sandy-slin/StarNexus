"""第 19 轮热修：流式收尾把答案结尾吞掉；带符号的密码半截上屏。

值守 20:09 报、主会话复现（两项新权限 20:0x 刚批下来，所以问题 1 **当时就在线上每一轮发生**）。

## 1. 终态卡吞掉结尾

`finish()` 也走 `_safe_body`，而 `_safe_body` 里有 `hold_tail` ⇒ 终态全文也被扣尾：
    'All done. See README.md'          → 'All done. See '
    'Done.'                            → ''
    '测试全过了，改动在 bridge/app.py'   → '测试全过了，改动在 '
而 `app.py` 拿到 finish 返回 True 就不再发消息 ⇒ 用户看到的就是截断版，没有任何兜底。
扣尾只对**中间帧**有意义（下一帧会补上）；终态没有下一帧了。

## 2. 带符号的密码半截上屏

扣尾靠「凭据字符集」判断末尾要不要扣，而字符集不含 `# $ ! % & *` ⇒
`密码 password=Xy9#kL2$mN8!qR` 有一帧推上去的是 `密码 password=Xy9#`（泄了前 3 位）。
🔴 不再枚举字符集（枚举永远漏），改成**从最后一个空白之后全扣**：
不管是什么字符，没写完的那个词一律等下一帧。
代价：中文没有空格 ⇒ 中文那一段会被整段扣住直到出现空白/标点换行 —— 所以中文按
「最后一个中文标点或空白」切（中文里凭据不会紧贴着中文字写）。
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import pytest

from bridge.redact import redact
from bridge.stream import StreamReply, _safe_body, _safe_final, hold_tail


def _shown(text):
    got = redact(text)
    return got[0] if isinstance(got, (tuple, list)) else got


# ---------- 1. 终态不许扣尾 ----------

@pytest.mark.parametrize("text", [
    "All done. See README.md",
    "Done.",
    "测试全过了，改动在 bridge/app.py",
    "文档在 https://open.feishu.cn/document/abc",
    "跑完了：3628 passed",
])
def test_final_text_keeps_its_ending(text):
    assert _safe_final(text) == text, f"终态把结尾吞了：{text!r} → {_safe_final(text)!r}"


def test_final_text_is_still_redacted():
    """终态不扣尾 ≠ 终态不脱敏。"""
    out = _safe_final("密钥是 " + "sk-" + "proj-" + "A" * 40)
    from bridge.redact import MASK

    assert MASK in out and "AAAAAAAA" not in out


def test_finish_writes_the_full_ending():
    """真跑 finish：终态卡上的就是全文结尾。"""
    from test_r18_stream import _FakeClient

    written = []
    client = _FakeClient()
    s = StreamReply(client, "oc_1", now=lambda: 100.0)
    real_put = s._put_content
    s._put_content = lambda cid, content, seq: written.append(content) or real_put(cid, content, seq)
    asyncio.run(s.start())
    asyncio.run(s.finish("All done. See README.md"))
    assert written and written[-1].startswith("All done. See README.md"), written


# ---------- 2. 中间帧：任何字符组成的半截凭据都不许上屏 ----------

SYMBOL_SECRETS = [
    "密码 password=" + "Xy9#kL2$mN8!qR" + " 完",
    "token: " + "Ab3%Cd5&Ef7*Gh9^" + " 结束",
    "DB_PASSWORD=" + "p@ss!w0rd#2026" + "\n",
]


@pytest.mark.parametrize("full", SYMBOL_SECRETS)
def test_no_frame_leaks_a_symbol_password_prefix(full):
    secret = full.split("=")[-1].split(": ")[-1].split()[0]
    head = secret[:3]
    bad = sorted({hold_tail(full[:i]) for i in range(1, len(full) + 1)
                  if head in _shown(hold_tail(full[:i]))
                  and secret not in _shown(hold_tail(full[:i]))})
    assert not bad, f"半截密码上屏了：{bad}"


def test_original_samples_still_zero_leak():
    """上一轮那批（sk-/AKIA/ghp/连接串）不许因为改判据而回退。"""
    import test_r17_stream_redact as old

    for name, full in old.SAMPLES.items():
        secret = full.split()[-1]
        for i in range(1, len(full) + 1):
            frame = hold_tail(full[:i])
            assert not (secret[:6] in frame and _shown(frame) == frame), f"{name} 第 {i} 帧漏了"


def test_chinese_prose_is_not_held_back_forever():
    """🔴 中文没有空格：不许整段扣到最后才出 —— 那流式就白做了。"""
    text = "我看了一下这个目录，里面有三个项目，测试都是通过的。下面说说每个项目是做什么的"
    held = max(len(text[:i]) - len(hold_tail(text[:i])) for i in range(1, len(text) + 1))
    assert held <= 20, f"中文被扣住 {held} 字，流式等于没开"
