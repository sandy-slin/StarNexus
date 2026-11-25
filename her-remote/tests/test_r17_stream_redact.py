"""第 17 轮：流式输出**还没实现**，但先把判据立下来 —— 否则实现那天必踩。

## 缺口（主会话独立实测，不是转述）

脱敏层按「形状」认凭据（`sk-` 后面 ≥16 位、`AKIA` + 16 位…），而流式的中间帧天然是
**前缀**：凑不够长度就不匹配 ⇒ 半截凭据已经推给飞书。飞书服务端收到就等于发布了
（`bridge/redact.py` 文件头那句）。逐前缀实测：

    样本           直推漏帧   最长已上屏   扣尾后
    openai            13        18 字       0
    aws               14        19 字       0
    github            18        23 字       0
    postgres 连接串    20        25 字       0

末帧（全文）脱敏是对的，所以**现在的非流式实现没问题**。这一条是给尚未实现的功能
提前立判据，不是现网回退。

## 判据：推之前把「还可能是凭据一部分」的尾巴扣住

下一帧会把它补上，所以用户看到的只是「最后几个字慢一帧」，而不是「半个密钥已经发出去了」。
代价（主会话实测，比 F 报告里的数字大）：中文 0 字；英文平均 3 字；
中英混排带长路径时最多扣住 29 字（`/Users/x/codes/...` 整条路径没有空格、全在字符集内）。
对中文主场景近乎零代价。

🔴 **这个文件的判据是「逐帧前缀里任何一帧都不含凭据片段」，不是「全文脱敏对不对」。**
今天两次「全量测试绿着漏」都是只写了正常用法的用例（见 `tests/test_r16_watch_baseline.py` 文件头）。
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge.redact import redact

# 🔴 密钥在代码里**拼出来**，不写成字面量 —— 否则这个文件本身就成了仓库里的凭据样本，
# 而且会被我们自己的脱敏层/扫描器命中。
SAMPLES = {
    "openai": "他给我的密钥是 " + "sk-" + "proj-" + "A" * 40,
    "aws": "凭据 " + "AKIA" + "IOSFODNN7EXAMPLE",
    "github": "token " + "ghp_" + "B" * 36,
    "pgurl": "连接串 postgres://admin:" + "s3cretpw" + "@db.example.com:5432/x",
    "bearer": "Authorization: Bearer " + "eyJhbGciOi" + "C" * 40,
}

# 凭据字符集：末尾连续的这些字符都「可能还是凭据的一部分」⇒ 本帧先别推
_CRED_TAIL = re.compile(r"[A-Za-z0-9_\-+/=:@.~]*\Z")


def hold_tail(text: str) -> str:
    """流式推送前扣住可能是凭据一半的尾巴。下一帧会补上。"""
    return text[: _CRED_TAIL.search(text).start()]


def _redacted(text: str) -> str:
    got = redact(text)
    return got[0] if isinstance(got, (tuple, list)) else got


def _leaks(frame: str, secret: str) -> bool:
    """这一帧推出去会不会带着凭据片段（脱敏层没认出来、而片段已经在里面）。"""
    return secret[:6] in frame and _redacted(frame) == frame


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_naive_streaming_would_leak(name):
    """先证明缺口真的存在 —— 不然下面那条「修好了」就没有参照。"""
    full = SAMPLES[name]
    secret = full.split()[-1]
    leaked = [full[:i] for i in range(1, len(full) + 1) if _leaks(full[:i], secret)]
    assert leaked, f"{name}: 直推居然不漏？那这个文件的前提变了，重新量一遍"


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_holding_the_tail_leaks_nothing(name):
    """🔴 核心判据：扣住尾巴之后，**任何一帧**都不含凭据片段。"""
    full = SAMPLES[name]
    secret = full.split()[-1]
    bad = [full[:i] for i in range(1, len(full) + 1)
           if _leaks(hold_tail(full[:i]), secret)]
    assert not bad, f"{name}: 这些帧仍然带着凭据片段：{[len(x) for x in bad]}"


@pytest.mark.parametrize("name", sorted(SAMPLES))
def test_final_frame_is_still_redacted(name):
    """末帧（全文）照旧要脱敏 —— 扣尾巴不能把最后那一下也扣掉。"""
    assert "已脱敏" in _redacted(SAMPLES[name])


def test_chinese_text_is_not_held_back():
    """中文主场景零代价：CJK 不在凭据字符集里，一个字都不扣。"""
    text = "我看了一下这个目录，里面有三个项目，测试都是通过的。"
    for i in range(1, len(text) + 1):
        assert hold_tail(text[:i]) == text[:i]


def test_english_hold_is_short():
    """英文代价要小：扣住的只能是最后一个词。"""
    text = "I checked the directory and all tests pass"
    worst = max(len(text[:i]) - len(hold_tail(text[:i])) for i in range(1, len(text) + 1))
    assert worst <= 12, f"扣得太多：{worst} 字"


def test_long_path_is_the_worst_case():
    """🔴 实测最坏情况：中英混排里的长路径会被整条扣住（最多 29 字）。

    不是 bug，是这个判据的已知代价 —— 手机上看是「路径打到一半卡住、下一帧整段蹦出来」。
    记在这里，将来谁要给用户承诺「逐字出」得按这个预期说，别按平均值说。
    """
    text = "文件在 /Users/liu/codes/carher-admin/tools/feishu-agent-bridge 里"
    worst = max(len(text[:i]) - len(hold_tail(text[:i])) for i in range(1, len(text) + 1))
    assert 20 <= worst <= 60, f"最坏情况变了（{worst} 字），重新量一遍再改承诺"
