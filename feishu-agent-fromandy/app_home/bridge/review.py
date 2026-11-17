"""审查层：把"这台机器上允许发生什么"从工具白名单里独立出来。

设计来源是 2026-11-03 拆开的两个同类产品（Anthropic Duet / OpenAI dots），它们做的
正是"手机上的 agent 在远端机器干活"，两家的安全模型是两套哲学：

- **Duet**：审批**加密绑定**到具体事件 —— 每张审批卡带
  `sign{session_id, event_uuid, event_type, tool_name, input}` + `device_attestation`。
  也就是说「批准」不是 UI 上点一下，而是绑死某一次具体的 tool-use 和这台设备，
  重放和挪用都不成立。
- **dots**：一个**独立的 auto-review 层**对照用户的 Custom Rules 过每个动作，判定
  「能做 / 要批 / 你自己来」，发现问题**直接把 agent 暂停**
  （"Auto-review did not approve some recent actions, so your dot was paused"）。
  而且 Custom Rules **关不掉** auto-review。

我们这版原来只有工具白名单（`--allowed-tools`），相当于只有 Duet 的闸门那一半，
缺了 dots 那种"事后看、能掐停"的一层。差别在于：白名单只能表达"这个工具能不能用"，
表达不了"这一轮里发生的事整体上对不对"。

两家还有一条**共同**的设计，我们原来也没有：
**主动/后台任务的工具面被强制收窄成只读**（dots 的 proactive research 不能发消息、
不能改内容、不能开浏览器）。我们的定时任务、看门狗触发之类将来一定会有，
所以这里把"来源"作为降权依据，而不是全局一个 mode。

这一层**只做判定，不执行** —— 执行仍由 Claude Code 自己的权限层兜底。两层叠加是故意的：
审查层可能漏判，白名单是硬底线。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass, field
from enum import Enum


class Verdict(str, Enum):
    ALLOW = "allow"  # 直接做
    ASK = "ask"  # 要人点头
    DENY = "deny"  # 不做，并说明原因
    PAUSE = "pause"  # 掐停这个会话，等人来看（抄 dots）


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    reason: str
    rule: str = ""

    @property
    def blocked(self) -> bool:
        return self.verdict in {Verdict.DENY, Verdict.PAUSE}


# 不管谁、什么模式、什么来源，一律不做。
# 和 policy.py 的 ALWAYS_DENY 是两层：那层交给 Claude Code 执行，这层在我们自己手里，
# 能看到整句话而不只是工具名。
HARD_DENY = (
    (r"\brm\s+-rf?\s+[/~]", "删根目录或家目录"),
    (r"\bgit\s+push\b", "推代码到远端"),
    (r"\bgit\s+reset\s+--hard\b", "丢弃本地改动"),
    (r"\bkubectl\s+delete\b", "删 K8s 资源"),
    (r"\bshutdown\b|\breboot\b", "关机重启"),
    (r"\bdd\s+if=.*of=/dev/", "裸写磁盘"),
    (r":\(\)\s*\{.*\}\s*;?\s*:", "fork 炸弹"),
    (r"\bcurl\b[^|]*\|\s*(ba)?sh", "下载直接执行"),
    # 🔴 这里的模式匹配的是 **lower() 之后**的文本（见 review_request），
    # 所以字面量一律写小写 —— 原来写 `-R` 导致这一条永远匹配不到，
    # `chmod -R 777 /` 完全不被拦。tests/test_review.py 有条测试盯着这个。
    (r"\bchmod\s+-[a-z]*r[a-z]*\s+777\b", "递归改成全开权限"),
)

# 碰到这些要人点头（将来接审批卡；现在先一律拒并说明，不静默放过）
NEEDS_APPROVAL = (
    (r"\bgit\s+commit\b", "提交代码"),
    (r"\bnpm\s+(publish|install\s+-g)\b", "发包或全局装包"),
    (r"\bpip\s+install\b", "装 Python 包"),
    (r"\bbrew\s+(install|uninstall)\b", "装卸软件"),
    (r"\blaunchctl\b", "改开机启动项"),
    (r"\bdefaults\s+write\b", "改系统设置"),
)

# 凭据与隐私面。读到就会被回传进飞书 —— 飞书是外部系统，发出去等于发布了。
SECRET_PATHS = (
    r"\.env\b",
    r"\.pem\b",
    r"\.key\b",
    r"id_rsa",
    r"\.ssh/",
    r"\.aws/",
    r"credentials",
    r"\.netrc",
    r"Keychain",
    r"\.kube/config",
)


@dataclass
class ReviewContext:
    """一次请求的来龙去脉。"""

    text: str
    source: str = "human"  # human | proactive | scheduled
    mode: str = "readonly"


class Reviewer:
    """独立审查层。判定在 agent 跑之前做一遍（看请求），跑完再做一遍（看发生了什么）。"""

    def __init__(self, custom_rules: tuple[str, ...] = ()) -> None:
        # 用户自己加的禁止项。**只能更严不能更松** —— 抄 dots 那条
        # 「Custom Rules 关不掉 auto-review」。
        self.custom_rules = custom_rules

    def review_request(self, ctx: ReviewContext) -> Decision:
        lowered = ctx.text.lower()

        for pattern, why in HARD_DENY:
            if re.search(pattern, lowered):
                return Decision(Verdict.DENY, f"不做这件事：{why}", rule=pattern)

        for pattern in SECRET_PATHS:
            if re.search(pattern, ctx.text, re.I):
                return Decision(
                    Verdict.DENY,
                    "涉及凭据文件。读出来会被发到飞书，而飞书是外部系统。",
                    rule=pattern,
                )

        for rule in self.custom_rules:
            try:
                if re.search(rule, ctx.text, re.I):
                    return Decision(Verdict.DENY, f"你自己的规则挡住了：{rule}", rule=rule)
            except re.error:
                continue  # 用户写错正则不该让整个服务挂掉

        # 非人触发的一律降到只读 —— 两家产品共同的设计：
        # 主动/后台任务不能改东西。
        if ctx.source != "human":
            for pattern, why in NEEDS_APPROVAL:
                if re.search(pattern, lowered):
                    return Decision(
                        Verdict.DENY,
                        f"后台任务不做需要确认的事：{why}",
                        rule=pattern,
                    )

        for pattern, why in NEEDS_APPROVAL:
            if re.search(pattern, lowered):
                return Decision(Verdict.ASK, f"这步要你点头：{why}", rule=pattern)

        return Decision(Verdict.ALLOW, "")

    def review_outcome(self, denials: list[dict], ctx: ReviewContext) -> Decision:
        """跑完之后看发生了什么 —— 这是 dots 那条"能掐停"的落点。

        判据不是"有没有被拒"（偶尔撞到白名单边界是正常的），而是**反复试探**：
        同一轮里被拒多次，说明 agent 在系统性地找绕过的路，这种要掐停让人来看。
        """
        if len(denials) >= 3:
            names = sorted({d.get("tool_name", "?") for d in denials})
            return Decision(
                Verdict.PAUSE,
                f"这一轮有 {len(denials)} 次操作被挡（{', '.join(names)}）。"
                "连续试探被拦的工具不正常，已暂停这个会话等你确认。",
                rule="repeated_denials",
            )
        return Decision(Verdict.ALLOW, "")


# ---------- 审批令牌 ----------
#
# 抄 Duet 的"绑定到具体事件"，并避开调研里一个真实故障：
# 某开源项目把按钮回调令牌绑在"运行中的那次任务"上，而 agent 发完卡片这一轮就结束了
# ⇒ **每一张 agent 发出的审批卡，点下去必定失败**，而且没有任何用户可见的反馈。
# 唯一能用的是 Stop 按钮，因为它只在跑的过程中点。
#
# 所以令牌只绑**内容**（scope + 动作 + 一次性 nonce + 过期时间），不绑任务生命周期。


@dataclass
class ApprovalToken:
    scope: str
    action_digest: str
    nonce: str
    expires_at: float
    signature: str

    def to_value(self) -> dict:
        # 飞书卡片回调的 value 只支持 object，不支持字符串。
        return {
            "scope": self.scope,
            "digest": self.action_digest,
            "nonce": self.nonce,
            "exp": int(self.expires_at),
            "sig": self.signature,
        }


class ApprovalStore:
    def __init__(self, secret: bytes, ttl_sec: int = 86400, used_path: str = "") -> None:
        self._secret = secret
        self._ttl = ttl_sec
        self._used: set[str] = set()
        # 用过的 nonce 落盘的地方（空 = 只在内存，老调用方 / 测试用）。见 `_mark_used`。
        self.used_path = used_path
        self._used_loaded = False

    @staticmethod
    def _digest(scope: str, action: str) -> str:
        return hashlib.sha256(f"{scope}\x00{action}".encode()).hexdigest()[:32]

    def issue(self, scope: str, action: str, payload: dict) -> ApprovalToken:
        """签一张审批令牌。🔴 令牌里放的就是 `action` **本身**（gate 的 24 位 action_digest），不再哈希一遍。

        2026-11-06 实测的断点（`tests/test_token_lifecycle.py` 三条 xfail 钉着）：
        原来令牌里存的是 `sha256(scope + action)[:32]` —— 同进程那条路靠内存 `_pending` 里的
        `payload` 拿回原值，**一重启 `_pending` 就空了**，只能从令牌读，读到的是哈希后的值 ⇒
        `gate.approve()` 对不上任何 `.asked` 文件；而且重建的 dict 少了 `payload` 键 ⇒
        `on_card_action` 读 `redeemed["payload"]` 直接 KeyError ⇒ 批准存不下来，agent 一直停在那。
        这个桥重启是常态（看门狗 + 每次上线），所以这不是边角情况，是每天都会撞上的。

        为什么不哈希也安全：`action` 本来就是**内容的指纹**（不是原文），HMAC 签名已经证明
        「是我签发的、没被改过」；未决与否由磁盘上的 `.asked` 决定（`ApprovalGate.approve` 自己核）。
        签进令牌的字段一样多，只是不再丢掉还原所需的那一份信息。
        """
        import secrets

        del payload  # 载荷完全由令牌字段决定（scope + digest）—— 不再存一份只在内存里的
        nonce = secrets.token_hex(12)
        digest = str(action or "")
        expires = time.time() + self._ttl
        message = f"{scope}|{digest}|{nonce}|{int(expires)}".encode()
        signature = hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]
        return ApprovalToken(scope, digest, nonce, expires, signature)

    # 兑换失败的原因。**不同原因要给用户不同的话** ——
    # 原来四种情况都回同一句「这个按钮已失效或你无权操作」，用户完全不知道
    # 该重发消息、该等一会儿、还是自己没权限（2026-11-03 用户真的卡在这）。
    USED = "used"          # 这张卡已经点过了（一次性）
    EXPIRED = "expired"    # 超过有效期
    STALE = "stale"        # 签名对不上：通常是服务重启过、或卡片是上个版本发的
    FORBIDDEN = "forbidden"  # 点的人不在白名单
    MALFORMED = "malformed"  # 回传数据不对劲

    def redeem(
        self, value: dict, clicker_open_id: str, owners: frozenset[str]
    ) -> dict | None:
        """核验并消费。任一条不过就返回 None（兼容老调用方）。

        注意**点击时重新查白名单** —— 这比"签发时查过"更严：卡片可能在群里被
        别人看到，而白名单在这期间可能已经变了。
        """
        result, _ = self.redeem_detailed(value, clicker_open_id, owners)
        return result

    def redeem_detailed(
        self, value: dict, clicker_open_id: str, owners: frozenset[str]
    ) -> tuple[dict | None, str]:
        """同 redeem，但失败时一并返回原因（上面那几个常量之一）。"""
        nonce = str(value.get("nonce") or "")
        if not nonce:
            return None, self.MALFORMED
        self._load_used()
        if nonce in self._used:
            return None, self.USED  # 一次性（跨重启：见 _mark_used）
        if clicker_open_id not in owners:
            return None, self.FORBIDDEN  # 点击时再查一遍
        try:
            expires = float(value.get("exp") or 0)
        except (TypeError, ValueError):
            return None, self.MALFORMED
        if time.time() > expires:
            return None, self.EXPIRED
        scope = str(value.get("scope") or "")
        digest = str(value.get("digest") or "")
        message = f"{scope}|{digest}|{nonce}|{int(expires)}".encode()
        expected = hmac.new(self._secret, message, hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expected, str(value.get("sig") or "")):
            # 签名对不上。最常见的原因不是伪造，而是**服务重启换了密钥**
            # 或者卡片是上个版本发出来的。
            return None, self.STALE
        # 签名已经证明"这个令牌是我签发的、内容没被改过"；
        # 「这一步还在不在等」由**落盘的 `.asked` 文件**说话（`ApprovalGate.approve` 自己核）。
        # 🔴 同进程和重启后**返回同一个形状**（原来一个带 `payload`、一个不带 ⇒ 重启后 KeyError）。
        self._mark_used(nonce)
        return {"scope": scope, "digest": digest, "payload": {"scope": scope, "digest": digest}}, ""

    # ---------- 一次性要跨重启成立 ----------
    #
    # 🔴 原来只靠内存里的 `_used` ⇒ 重启后同一张卡能**再兑换一次**（xfail 钉着）。
    # 磁盘上的 `.asked` 兜住了真正放行那一步（不是直接提权），但「一次点击换一次放行」
    # 这句话只在进程内为真。默认 TTL 24 小时、看门狗一天重启好几次 ⇒ 窗口是常态。

    def _used_path(self) -> str:
        return getattr(self, "used_path", "") or ""

    def _load_used(self) -> None:
        path = self._used_path()
        if not path or getattr(self, "_used_loaded", False):
            return
        self._used_loaded = True
        now = time.time()
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    nonce, _, exp = line.strip().partition(" ")
                    try:
                        if nonce and float(exp) > now:  # 过期的令牌反正兑不了，不用记
                            self._used.add(nonce)
                    except ValueError:
                        continue
        except OSError:
            pass

    def _mark_used(self, nonce: str) -> None:
        self._used.add(nonce)
        path = self._used_path()
        if not path:
            return
        try:
            # 追加写：一行一个 nonce + 它能活到什么时候。追加是原子的（单行远小于 PIPE_BUF）
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(f"{nonce} {time.time() + self._ttl:.0f}\n")
        except OSError:
            # 写不下去：内存里那份照样挡同进程重放，只是跨重启的保护没了 —— 不能因此让点击失败
            pass
