"""出口脱敏：凭据不管怎么被读出来，都不许发到飞书。

## 为什么要有这一层

2026-11-03 端到端回归实测（3/3 复现）：`rg --hidden <词> .` 能把 `.env`、
`*.pem`、`.ssh/id_rsa` 的**整行原文**带回回复正文 —— 不弹卡片、没有 denial。
原因是 `Read(**/.env*)` 这类路径 deny 只在**命令里写出那个路径**时才触发，
而 `rg` 是在白名单里的只读命令、它自己去遍历目录。

我自己复测时模型两次都拒绝贴出凭据（它判断那是敏感文件）—— 但那是**模型的
判断力，不是闸门**。换个说法、换个模型、换个版本就可能贴出来。
产品的安全边界不能建立在"模型这次懂事"上面。

所以在**唯一出口**上拦：所有回传飞书的文本都过这一层。飞书是外部系统，
消息进了它的服务器就等于发布了（会被缓存、被索引，删了也可能留着）。

## 判据

按"形状"认，不按路径认 —— 因为我们看到的只是一段文本，不知道它从哪来：

1. **已知形状的密钥**：`sk-`/`ghp_`/`AKIA`/JWT/私钥块 —— 这些一眼能认
2. **赋值形式**：`KEY=value` / `"key": "value"`，键名含 secret/token/password/key 等
3. **私钥正文**：`-----BEGIN ... PRIVATE KEY-----` 到 `END` 之间整段

刻意**不**做的：不按熵值猜（误伤 commit hash、base64 的正常数据）、
不碰短值（`DEBUG=1` 这种没意义）。宁可漏掉一点也别把正常输出打成马赛克 ——
那样用户会关掉这一层，就全没了。
"""

from __future__ import annotations

import re

MASK = "[已脱敏]"

# 一眼能认的密钥形状。顺序无关，逐条替换。
_KNOWN_SHAPES = (
    # OpenAI / Anthropic 风格
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}"),
    # GitHub
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    # AWS
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # Slack / Google / 飞书
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\b(?:cli|cig)_[a-f0-9]{16}\b"),
    # JWT（三段 base64url）
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)

# URL 里的 `:密码@`。只抹密码那一段，保留协议/用户名/主机 —— 用户还要看懂这是哪个库。
#
# 🔴 2026-11-04 安全审查实测：`postgres://admin:S3cr3tPass@db.prod.internal:5432/main`
# 原来一条规则都不匹配（键名不像凭据、值不是已知形状）⇒ 原文整条进飞书。
# 这条路上**没有别的闸门**：进会话聊天用的是目标会话在电脑上那套权限，
# pathguard 和工作目录白名单都不在这条路上，脱敏是唯一的出口。
# `[^\s/:@]*` 而不是 `+`：`redis://:hunter2@host` 这种空用户名的形式也要盖到（实测漏过）
_URL_CREDENTIAL = re.compile(r"\b([a-z][a-z0-9+.\-]*://[^\s/:@]*:)([^\s/@]{3,})(@)")

# 私钥块：整段抹掉，不留行数（行数本身也是信息）
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
# 只有开头、没有结尾的私钥块（文本在中间被截断过）：从 BEGIN 一直抹到末尾。
# 兜底用 —— 正确做法是先脱敏再截断，但任何一条路漏了这个顺序，不该就把密钥发出去。
_PRIVATE_KEY_OPEN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*\Z", re.DOTALL)

# 赋值形式。键名要"看着像凭据"，值要够长才算（短值没意义且容易误伤）。
_SECRET_KEY_WORDS = (
    r"secret|token|password|passwd|pwd|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|auth|credential|bearer|session[_-]?id|cookie|"
    # 🔴 Cursor store.db 的 blobEncryptionKey（44 位 base64）原来一个词都对不上 ⇒ 原样外发。
    # 本机 3 份 Claude 记录里出现过它，其中 2 份带着真值（2026-11-04 安全审查）。
    r"encryption[_-]?key|blob[_-]?key|signing[_-]?key|master[_-]?key"
)
# 值可能带 `Bearer ` 前缀（`AUTH_TOKEN=Bearer abc…`）—— 实测漏过，
# 因为原来的字符类在空格处就停了。这里显式允许一个方案前缀。
_ASSIGNMENT = re.compile(
    rf"((?:{_SECRET_KEY_WORDS})[A-Za-z0-9_-]*\s*[=:]\s*)"
    rf"(\"?)((?:Bearer\s+|Basic\s+|token\s+)?[^\s\"',;}}]{{8,}})(\"?)",
    re.IGNORECASE,
)
# JSON 里 `"key": "value"` 的形式（键名在引号里）
_JSON_FIELD = re.compile(
    rf"(\"[A-Za-z0-9_-]*(?:{_SECRET_KEY_WORDS})[A-Za-z0-9_-]*\"\s*:\s*\")"
    rf"([^\"]{{8,}})(\")",
    re.IGNORECASE,
)


def redact(text: str) -> tuple[str, int]:
    """脱敏，返回 (处理后的文本, 命中次数)。

    命中次数给调用方做提示用 —— 用户应该知道"有东西被挡掉了"，
    否则他会以为 agent 没读到，然后换个问法再试一次。
    """
    if not text:
        return text, 0

    hits = 0

    def bump(_match: re.Match) -> str:
        nonlocal hits
        hits += 1
        return MASK

    result = _URL_CREDENTIAL.sub(lambda m: m.group(1) + bump(m) + m.group(3), text)
    result = _PRIVATE_KEY_BLOCK.sub(bump, result)
    result = _PRIVATE_KEY_OPEN.sub(bump, result)

    for pattern in _KNOWN_SHAPES:
        result = pattern.sub(bump, result)

    # 赋值右边是**代码表达式**时放过 —— 那不是硬编码的密钥。
    # agent 解释代码时经常贴这种（实测误伤过 `api_key = os.environ["..."]`），
    # 把代码打成马赛克会让回复没法读。
    _CODE_EXPR = re.compile(
        r"^(?:os\.environ|os\.getenv|getenv|environ|process\.env|"
        r"self\.|cls\.|config\.|settings\.|args\.|opts\.|"
        r"[A-Za-z_][A-Za-z0-9_]*\s*\(|"      # 函数调用
        r"[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_]|"  # 属性访问
        r"\$\{|\$[A-Za-z_]|"                  # shell 变量
        r"[\[\{<]|"                           # 容器/占位
        r"(?:xxx+|yyy+|placeholder|your[_-]|my[_-]|fake|dummy|example|test|changeme|TODO)"
        r")",
        re.IGNORECASE,
    )

    def mask_assignment(match: re.Match) -> str:
        nonlocal hits
        value = match.group(3).strip()
        # 🔴 已知密钥形状**优先于**代码豁免。
        # 放宽前实测漏过 `api_key: sk-proj-...` 和 `TOKEN="ghp_..."` ——
        # 因为 `sk-proj-xxx` 里的连字符让它像个标识符、`ghp_` 像函数名。
        # 真密钥不管长得像什么都要挡。
        if not any(shape.search(value) for shape in _KNOWN_SHAPES):
            if _CODE_EXPR.match(value):
                return match.group(0)  # 代码表达式，原样留下
        hits += 1
        return f"{match.group(1)}{match.group(2)}{MASK}{match.group(4)}"

    result = _ASSIGNMENT.sub(mask_assignment, result)

    def mask_json(match: re.Match) -> str:
        nonlocal hits
        hits += 1
        return f"{match.group(1)}{MASK}{match.group(3)}"

    result = _JSON_FIELD.sub(mask_json, result)

    return result, hits


def redact_with_notice(text: str) -> str:
    """脱敏并在末尾说明挡了几处。

    必须告诉用户 —— 不说的话他会以为 agent 读不到，然后反复换问法重试
    （那正是最该避免的：一次次把凭据往外送）。
    """
    result, hits = redact(text)
    if hits:
        result += f"\n\n🔒 有 {hits} 处看着像凭据的内容没有发出来（飞书是外部系统）"
    return result
