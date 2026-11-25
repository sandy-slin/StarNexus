"""出口脱敏：凭据不管怎么被读出来，都不许发到飞书。

2026-11-03 端到端回归实测（3/3 复现）：`rg --hidden <词> .` 能把 `.env`、
`*.pem`、`.ssh/id_rsa` 的整行原文带回回复正文 —— 不弹卡片、没有 denial。
路径 deny（`Read(**/.env*)`）只在命令里写出那个路径时才触发，而 `rg`
是白名单里的只读命令、自己去遍历目录。

我复测时模型两次拒绝贴出凭据 —— 那是**模型的判断力，不是闸门**。
产品安全不能建立在"模型这次懂事"上。所以在唯一出口上拦。

两组测试同等重要：
- 挡得住（安全）
- **不误伤**（可用性）—— 误伤会让用户关掉这层，那就全没了
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.redact import MASK, redact, redact_with_notice  # noqa: E402


MUST_REDACT = [
    # 赋值形式（实测现场就是这个形状）
    "SECRET=canary-rg-7f3a-long-enough",
    "api_key: abcdef1234567890abcdef",
    'export GITHUB_TOKEN="ghp_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"',
    "DB_PASSWORD=sup3rs3cr3tvalue",
    "AUTH_TOKEN=Bearer abcdefghijklmnop",
    # JSON 字段
    '{"access_token": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij"}',
    '{"client_secret": "0123456789abcdef0123"}',
    # 已知形状
    "sk-ant-api03-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "AKIAIOSFODNN7EXAMPLE",
    "xoxb-" + "123456789012-abcdefghijklmnop",
    "AIzaSyA1234567890abcdefghijklmnopqrstuv",
    "cli_a1b2c3d4e5f60708",
    # 私钥整段
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK\n-----END RSA PRIVATE KEY-----",
]

MUST_NOT_TOUCH = [
    "分支 feat/sec-redact，879 个文件改了没提交",
    "commit a3f91c2e8b4d5f6071829304a5b6c7d8e9f0a1b2",
    "DEBUG=1",
    "PORT=8080",
    'model = "claude-sonnet-5-5"',
    "测试 565 passed, 13 xfailed",
    "https://chat.auto-link.com.cn/model",
    "backend/feishu_bridge.py:143 里有一个 send_text 函数",
    "总共 2450 行，最大的是 app.py（680 行）",
    "import hashlib\nimport json\nimport os",
    "这个仓库用 pytest，跑 `python -m pytest backend/tests/ -v`",
]


@pytest.mark.parametrize("text", MUST_REDACT)
def test_credentials_are_masked(text):
    result, hits = redact(text)
    assert hits >= 1, f"没挡住：{text[:50]}"
    assert MASK in result
    # 原始秘密值不能残留。只检查"看起来是值的那部分"——
    # 键名本身留着是对的（用户要知道是哪个变量被挡了）。
    for chunk in text.replace("=", " ").replace(":", " ").split():
        cleaned = chunk.strip('",{}')
        if len(cleaned) < 16 or cleaned == MASK:
            continue
        # 键名含凭据词 ⇒ 那是键不是值，跳过
        if any(w in cleaned.lower() for w in ("secret", "token", "key", "password", "auth")):
            continue
        assert cleaned not in result, f"秘密值残留：{cleaned[:30]}"


@pytest.mark.parametrize("text", MUST_NOT_TOUCH)
def test_normal_output_is_untouched(text):
    """误伤会让用户关掉这一层 —— 那就等于没有这层。"""
    result, hits = redact(text)
    assert hits == 0, f"误伤了：{text[:50]} -> {result[:60]}"
    assert result == text


def test_private_key_block_leaves_no_line_count():
    """私钥整段抹掉 —— 行数本身也是信息。"""
    text = "-----BEGIN PRIVATE KEY-----\n" + "A" * 64 + "\n" + "B" * 64 + "\n-----END PRIVATE KEY-----"
    result, hits = redact(text)
    assert hits == 1
    assert "A" * 64 not in result
    assert result.count("\n") == 0


def test_notice_tells_the_user_something_was_held_back():
    """必须告诉用户 —— 不说他会以为没读到，然后换问法反复重试。"""
    text = "SECRET=verylongsecretvalue123"
    with_notice = redact_with_notice(text)
    assert MASK in with_notice
    assert "没有发出来" in with_notice


def test_no_notice_when_nothing_matched():
    clean = "分支 feat/x，879 个文件"
    assert redact_with_notice(clean) == clean


def test_multiple_hits_counted():
    text = "A_TOKEN=aaaaaaaaaaaaaaaa\nB_SECRET=bbbbbbbbbbbbbbbb"
    _, hits = redact(text)
    assert hits == 2


def test_empty_and_none_safe():
    assert redact("") == ("", 0)
    assert redact(None) == (None, 0)


def test_reply_send_applies_redaction():
    """回传路径真的用上了这一层 —— 这是唯一出口。"""
    import json as _json

    from bridge.reply import Replier

    class FakeResponse:
        code = 0
        msg = "ok"

        def success(self):
            return True

    class FakeClient:
        def __init__(self):
            self.sent = []
            self.im = self

        @property
        def v1(self):
            return self

        @property
        def message(self):
            return self

        def create(self, request):
            content = _json.loads(request.request_body.content)
            # 回复走的是 `post` 富文本（markdown 能渲染），不是纯 text ——
            # 桩要认这个形状，否则改了消息类型这条测试就变成死票。
            if "text" in content:
                self.sent.append(content["text"])
            elif "zh_cn" in content:
                self.sent.append(
                    "".join(
                        seg.get("text", "")
                        for line in (content["zh_cn"].get("content") or [])
                        for seg in line
                    )
                )
            else:
                self.sent.append(request.request_body.content)
            return FakeResponse()

    client = FakeClient()
    Replier(client, 20000).send("oc_1", "SECRET=thisisaverylongsecret123")
    assert "thisisaverylongsecret123" not in client.sent[0]
    assert MASK in client.sent[0]


# ---------- 代码片段不该被打成马赛克 ----------
#
# agent 解释代码时经常贴 `api_key = os.environ["..."]` 这种。
# 实测误伤过 —— 把代码打成马赛克会让回复没法读，用户会要求关掉脱敏，
# 那就全没了。所以赋值右边是**代码表达式**时放过。
#
# ⚠️ 已知密钥形状**优先于**代码豁免：`sk-proj-xxx` 里的连字符让它像标识符、
# `ghp_xxx` 像函数名，不特判就会被豁免规则放过。
#
# 🔴 测试数据必须**运行时拼接**。直接在源码里写 `sk-proj-abc...` 会被
# 环境的凭据保护替换成占位符，测试就在验一个假输入（踩过：显示"漏了"
# 其实是输入已经被换掉了）。


def _fake_key(prefix: str, body_len: int, char: str = "x") -> str:
    """运行时拼出密钥形状，避免源码里的字面量被替换。

    `char` 要符合真实格式：AWS 的 access key 是 `[0-9A-Z]{16}`，
    用小写 `x` 拼出来的串不匹配它的正则（踩过，以为是代码漏判）。
    """
    return prefix + char * body_len


CODE_SNIPPETS_MUST_PASS = [
    'api_key = os.environ["ANTHROPIC_API_KEY"]',
    "token = self._token",
    'api_key = getenv("KEY")',
    "secret = config.secret",
    "TOKEN=$MY_TOKEN",
    "password = your_password_here",
    "api_key = fake-key-placeholder",
    'headers = {"Authorization": f"Bearer {token}"}',
    "if not secret: raise ValueError('missing')",
    "# TODO: 把 api_key 移到环境变量",
    'grep -rn "api_key" .',
    "self._secret = secret",
]


@pytest.mark.parametrize("snippet", CODE_SNIPPETS_MUST_PASS)
def test_code_snippets_are_not_masked(snippet):
    result, hits = redact(snippet)
    assert hits == 0, f"误伤了代码：{snippet} -> {result}"
    assert result == snippet


def test_known_key_shapes_beat_the_code_exemption():
    """真密钥不管长得像不像代码都要挡。"""
    for prefix, length, char in (
        ("sk" + "-proj-", 24, "x"),
        ("ghp" + "_", 36, "x"),
        ("AKIA", 16, "Q"),  # AWS 是全大写数字，小写拼的串不符合真实格式
    ):
        key = _fake_key(prefix, length, char)
        for template in ("api_key: {}", 'export TOKEN="{}"', "AUTH={}", "用这个：{}"):
            text = template.format(key)
            result, hits = redact(text)
            assert key not in result, f"漏了：{text[:50]}"
            assert hits >= 1


def test_jwt_in_assignment_is_masked():
    jwt = "eyJ" + "a" * 12 + "." + "b" * 12 + "." + "c" * 12
    result, hits = redact(f"SESSION_TOKEN={jwt}")
    assert jwt not in result
    assert hits >= 1


def test_random_looking_value_is_still_masked():
    """没有已知前缀但明显是随机串的赋值，仍要挡。"""
    result, hits = redact("SECRET=aB3xK9mPqR7wZ2nL")
    assert "aB3xK9mPqR7wZ2nL" not in result
    assert hits == 1


# ---------- 2026-11-04 会话总控安全审查 ----------


def _pem_body():
    # 运行时拼，免得测试文件本身被凭据保护替换成占位符
    return "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + ("b3BlbnNzaC1rZXktdjEAAAAA" * 6) + "\n"


def test_truncated_private_key_block_is_still_masked():
    """先截断后脱敏时 END 行没了 —— 原来整块匹配不上，密钥正文原样外发。"""
    from bridge.redact import redact

    cut = "帮我把这个 key 配上：\n" + _pem_body()  # 没有 END 行
    out, n = redact(cut)
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in out
    assert n >= 1


def test_blob_encryption_key_is_masked():
    """Cursor store.db 的 meta 里有 blobEncryptionKey，原来一个关键词都对不上。"""
    from bridge.redact import redact

    value = "Q" * 43 + "="
    for text in (
        f'"blobEncryptionKey": "{value}"',
        f"blobEncryptionKey={value}",
        f"encryption_key: {value}",
    ):
        out, _ = redact(text)
        assert value not in out, text


def test_detail_card_redacts_before_truncating():
    """卡片截断前先脱敏：私钥块整段被抹掉，正文不会被截成一半漏出去。"""
    import types

    from bridge.app import _redact_text
    from bridge.fleet.cards import build_detail_card
    from bridge.fleet.model import Message, Session

    sess = Session(tool="claude", surface="cli", id="c1", title="配 key", cwd="/x",
                   status="idle", owner="none", updated_at=0, can_send=False)
    # 私钥正文做得比卡片的截断长度（300 字）长 ⇒ 先截断的话 END 行一定被切掉
    body = "-----BEGIN " + "OPENSSH PRIVATE KEY-----\n" + ("b3BlbnNzaC1rZXktdjEAAAAA" * 40) + "\n"
    text = "帮我把这个 key 配上：\n" + body + "-----END " + "OPENSSH PRIVATE KEY-----\n"
    card = build_detail_card(sess, [Message(role="user", text=text, at=0)], redact=_redact_text)
    assert "b3BlbnNzaC1rZXktdjEAAAAA" not in card


# ---------- URL 里的密码（2026-11-04 进会话聊天安全审查） ----------


@pytest.mark.parametrize("text,secret", [
    ("DATABASE_URL=postgres://admin:S3cr3tPass@db.prod.internal:5432/main", "S3cr3tPass"),
    ("postgres://admin:S3cr3tP%40ss@db:5432/main", "S3cr3tP%40ss"),
    ("redis://:hunter2x@cache:6379/0", "hunter2x"),
    ("mongodb+srv://u:pw12345@c0.mongodb.net/db", "pw12345"),
    ("连上 amqp://guest:guestpw@rabbit:5672 看看", "guestpw"),
])
def test_url_password_is_masked(text, secret):
    """🔴 进会话聊天这条路上脱敏是**唯一**的出口闸门（目标用的是它在电脑上那套权限，
    pathguard 和工作目录白名单都不在路上）。原来这些一条规则都不匹配，原文进飞书。"""
    from bridge.redact import redact

    out, n = redact(text)
    assert secret not in out and n >= 1
    assert "db" in out or "cache" in out or "rabbit" in out or "mongodb" in out  # 主机名要留着看得懂


@pytest.mark.parametrize("text", [
    "https://github.com/anthropics/claude-code",
    "see http://localhost:8900/api",
    "wss://msg-frontier.feishu.cn/ws/v2?fpid=493",
    "https://open.feishu.cn/document/client-docs/bot-v3/bot-customized-menu",
])
def test_normal_urls_are_untouched(text):
    """阳性对照：没有 `:密码@` 的正常链接不许动 —— 否则回复里满屏脱敏标记。"""
    from bridge.redact import redact

    out, n = redact(text)
    assert out == text and n == 0
