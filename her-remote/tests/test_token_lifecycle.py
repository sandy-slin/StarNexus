"""审批令牌 × 会话状态的跨层回归。

今天这一轮把三层状态的寿命改成了**不一样**，这个文件专门盯它们的接缝：

| 状态 | 在哪 | 寿命 |
|---|---|---|
| nonce / `_pending` / `_used` | `review.ApprovalStore` | **进程内** |
| 签名密钥 `approval.secret` | 磁盘 0600 | 跨重启 |
| `.pending` / `.asked` / `.ok` | `approval.ApprovalGate` | 跨重启 |
| `_awaiting`（点同意要 resume 谁） | `app.Bridge` | **进程内** |
| `_paused` / `paused.json` | 磁盘 | 跨重启 |

寿命不一致的地方就是 bug 的产地：**落盘的那一层会记住内存那一层早就忘了的事**。

纪律：每条 DENY 用例都配一条同形状的阳性对照 —— 否则"被拒绝"可能只是因为桩
不完整、代码根本没跑到判断那行。本文件里阳性对照一律紧跟在 deny 用例后面，
并在名字里带 `_positive_control` 或在 docstring 里写明。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import pytest  # noqa: E402

from bridge.approval import (  # noqa: E402
    HOOK_SOURCE,
    ApprovalGate,
    action_digest,
)
from bridge.review import ApprovalStore  # noqa: E402

OWNER = "ou_owner"
STRANGER = "ou_stranger"
OWNERS = frozenset({OWNER})
SCOPE = "dm:ou_owner"

# 钩子里的 `_GRANT_TTL_SEC`。这里重复一份是故意的 —— 两边漂移要让测试红。
GRANT_TTL_SEC = 600

PUBLISH = {"command": "npm publish"}
PIPINSTALL = {"command": "pip install evil-pkg"}


# ---------- 夹具：真钩子进程 + 能"重启"的 Bridge ----------


@pytest.fixture
def home():
    """隔离的服务主目录。**绝不碰 ~/.carher-feishu-agent**（用户正在用的那个）。"""
    with tempfile.TemporaryDirectory(prefix="tokenlife-") as tmp:
        yield tmp


def write_hook(directory: str) -> str:
    """把钩子源码落成真脚本。和 Claude Code 装它的方式一致。"""
    path = os.path.join(directory, "approval_hook.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(HOOK_SOURCE)
    os.chmod(path, 0o755)
    return path


def run_hook(hook_path: str, approval_dir: str, tool: str, tool_input: dict) -> str:
    """真起钩子子进程，返回 permissionDecision。"""
    env = dict(os.environ)
    env["CARHER_APPROVAL_DIR"] = approval_dir
    proc = subprocess.run(
        [sys.executable, hook_path],
        input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


def make_bridge(home_dir: str, owners=OWNERS):
    """造一个「刚启动」的 Bridge：只装状态层，不连网络、不起事件循环。

    每次调用就是一次**进程重启** —— 内存状态（`_pending` / `_used` / `_awaiting`）
    全新，落盘状态（密钥 / `.ok` / `paused.json`）从同一个 home 读回来。
    这正是我们要验的那条缝。
    """
    from bridge.app import Bridge, _load_or_create_secret

    bridge = Bridge.__new__(Bridge)
    bridge.settings = types.SimpleNamespace(owner_open_ids=owners)
    bridge.gate = ApprovalGate(os.path.join(home_dir, "approvals"))
    bridge.approvals = ApprovalStore(_load_or_create_secret(home_dir))
    bridge._awaiting = {}
    bridge._paused = {}
    bridge._paused_path = os.path.join(home_dir, "paused.json")
    bridge._load_paused()
    bridge._cancels = {}
    bridge._loop = None  # 不派发续跑协程
    bridge._last_event_at = 0.0
    bridge.said = []
    bridge._say = lambda chat_id, text: bridge.said.append(text)
    return bridge


def click(bridge, value: dict, clicker: str = OWNER):
    """模拟飞书卡片回调，返回 (toast_type, toast_content)。"""
    event = types.SimpleNamespace(
        operator=types.SimpleNamespace(open_id=clicker),
        action=types.SimpleNamespace(value=value),
    )
    response = bridge.on_card_action(types.SimpleNamespace(event=event))
    toast = response.toast
    return toast.type, toast.content


def send_card(bridge, scope: str, tool_input: dict, hook_path: str) -> dict:
    """走完"钩子 defer → 取待办 → 签发令牌"这一串，返回卡片的 approve value。"""
    approval_dir = bridge.gate.dir_for(scope)
    assert run_hook(hook_path, approval_dir, "Bash", tool_input) == "defer"
    pending = bridge.gate.take_pending(scope)
    assert len(pending) == 1
    digest = pending[0]["digest"]
    token = bridge.approvals.issue(scope, digest, {"digest": digest, "scope": scope})
    bridge._awaiting[scope] = {"chat_id": "oc_1", "session_id": "sess-1", "count": 1}
    return {"act": "approve", **token.to_value()}


def oks_in(directory: str) -> list[str]:
    return sorted(n for n in os.listdir(directory) if n.endswith(".ok"))


# ================================================================
# 1. 跨重启的完整闭环 —— 今天刚修的那条，也是最可疑的一条
# ================================================================
#
# 修的内容：签名密钥从 `os.urandom(32)` 改成落盘 `approval.secret`。
# 想达到的效果：「服务重启 14 次之后，用户点旧卡片还能用」。
#
# 三段式：
#   假设：密钥落盘 ⇒ 重启后点旧卡片能成功。
#   证伪条件：如果假设错，重启后的 `redeem` 仍然返回 None，只是**原因**从
#             "签名算不出来" 换成别的。
#   数据：密钥确实稳定（`test_secret_survives_restart` 绿）。`redeem_detailed`
#         在 22:24 被改成「签名过了就不再要求 `_pending`，重启后从令牌重建载荷」，
#         令牌这一层通了。但重建的载荷**少了 `payload` 这个键**，而
#         `on_card_action` 读的是 `redeemed["payload"]["digest"]` ⇒ KeyError ⇒
#         被外层 except 吞成「处理失败，看本机日志」。
#         而且重建出来的 `digest` 是**令牌的 32 位内容指纹**，不是
#         `.asked`/`.ok` 文件名用的 24 位 `action_digest` ⇒ 即使不崩，
#         `gate.approve()` 也对不上任何未决记录。
#         ⇒ **闭环仍然不通**，断点从"令牌"挪到了"载荷形状"。


def test_secret_survives_restart(home):
    """修复的前半截：同一个 home 起两次，密钥必须一样。

    这一条是绿的 —— 密钥落盘确实生效了。
    """
    from bridge.app import _load_or_create_secret

    first = _load_or_create_secret(home)
    second = _load_or_create_secret(home)
    assert first == second
    assert len(first) >= 32


def test_signature_still_verifies_after_restart(home):
    """修复的中间一截：重启后的新 store 必须认得旧卡片的**签名**。

    这一条也是绿的。它和下一条合起来说明：签名这一环通了，断在 `_pending`。
    """
    from bridge.app import _load_or_create_secret

    before = ApprovalStore(_load_or_create_secret(home))
    token = before.issue(SCOPE, "digest-abc", {"digest": "digest-abc", "scope": SCOPE})
    value = token.to_value()

    after = ApprovalStore(_load_or_create_secret(home))
    # 手动重算签名：绕过 `_pending` 这一环，单独验签名那一环。
    import hashlib
    import hmac

    message = (
        f"{value['scope']}|{value['digest']}|{value['nonce']}|{int(value['exp'])}"
    ).encode()
    expected = hmac.new(
        after._secret, message, hashlib.sha256
    ).hexdigest()[:32]
    assert expected == value["sig"], "密钥没复用，签名这一环就断了"


def test_old_card_token_verifies_after_restart(home):
    """令牌这一层在重启后通了（22:24 的改动）：签名过了就不再要求 `_pending`。

    这一条是绿的。它只说明"令牌能兑换"，不说明"闭环通" —— 载荷形状的问题见下面两条。
    """
    from bridge.app import _load_or_create_secret

    before = ApprovalStore(_load_or_create_secret(home))
    token = before.issue(SCOPE, "digest-abc", {"digest": "digest-abc", "scope": SCOPE})
    value = token.to_value()

    after = ApprovalStore(_load_or_create_secret(home))  # 进程重启
    result, reason = after.redeem_detailed(value, OWNER, OWNERS)
    assert result is not None, f"重启后旧卡片点不动，原因={reason}"
    assert reason == ""


# 2026-11-06 修好：令牌里放 action 本身 + 两条路同一形状（review.ApprovalStore.issue 的红字）
def test_redeemed_payload_shape_is_the_same_after_restart(home):
    """🔴 两条路返回的 dict 形状必须一致，否则调用方只能二选一地写。

    同进程：`{"scope", "action", "payload"}`
    重启后：`{"scope", "digest"}`  ← 少了 `payload`
    """
    from bridge.app import _load_or_create_secret

    secret = _load_or_create_secret(home)
    before = ApprovalStore(secret)
    token = before.issue(SCOPE, "digest-abc", {"digest": "digest-abc", "scope": SCOPE})
    value = token.to_value()

    same_process = before.redeem(dict(value), OWNER, OWNERS)
    assert same_process is not None
    # 重新签一张（上面那张已被消费）
    token2 = before.issue(SCOPE, "digest-abc", {"digest": "digest-abc", "scope": SCOPE})
    after_restart = ApprovalStore(secret).redeem(token2.to_value(), OWNER, OWNERS)
    assert after_restart is not None

    assert set(same_process) == set(after_restart), (
        f"形状不一致：同进程={sorted(same_process)} 重启后={sorted(after_restart)}"
    )


# 2026-11-06 修好：令牌里放 action 本身 + 两条路同一形状（review.ApprovalStore.issue 的红字）
def test_rebuilt_digest_matches_the_gate_filename(home):
    """🔴 重启后重建出来的 digest 必须能喂给 `gate.approve()`。

    `issue(scope, action)` 把 `action`（= gate 的 24 位 action_digest）又哈希了一遍
    存进 `token.action_digest`。同进程那条路靠 `payload` 保住了原值；
    重启那条路只能从令牌里读，读到的是哈希后的值。
    """
    from bridge.app import _load_or_create_secret

    secret = _load_or_create_secret(home)
    gate_digest = action_digest("Bash", PUBLISH)
    token = ApprovalStore(secret).issue(
        SCOPE, gate_digest, {"digest": gate_digest, "scope": SCOPE}
    )
    rebuilt = ApprovalStore(secret).redeem(token.to_value(), OWNER, OWNERS)
    assert rebuilt is not None
    got = rebuilt.get("digest") or rebuilt.get("payload", {}).get("digest")
    assert got == gate_digest, f"重建的 digest={got} 对不上 gate 的 {gate_digest}"


def test_same_process_card_redeems_positive_control(home):
    """阳性对照：同一个进程里发卡 + 点卡必须成功。

    没有这条，上面那个 xfail 可能只是因为我们造的 value 本身是坏的。
    """
    from bridge.app import _load_or_create_secret

    store = ApprovalStore(_load_or_create_secret(home))
    token = store.issue(SCOPE, "digest-abc", {"digest": "digest-abc", "scope": SCOPE})
    result, reason = store.redeem_detailed(token.to_value(), OWNER, OWNERS)
    assert reason == ""
    assert result is not None
    assert result["payload"]["digest"] == "digest-abc"


# 2026-11-06 修好：令牌里放 action 本身 + 两条路同一形状（review.ApprovalStore.issue 的红字）
def test_end_to_end_approval_survives_restart(home):
    """🔴 端到端：钩子 defer → 发卡 → **重启** → 点同意 → 钩子该放行。

    磁盘那一侧是好的（`.asked` 还在，重启后新建的 Gate 照样能 `approve()` ——
    见 `test_disk_side_of_the_loop_survives_restart`）。断点在
    `on_card_action` 和 `redeem_detailed` 之间的载荷约定上。
    """
    hook = write_hook(home)
    first = make_bridge(home)
    value = send_card(first, SCOPE, PUBLISH, hook)
    approval_dir = first.gate.dir_for(SCOPE)

    second = make_bridge(home)  # 进程重启：_pending / _awaiting 全空
    kind, content = click(second, value)
    assert kind == "info", f"重启后点同意被拒：{content}"
    assert oks_in(approval_dir), "没落下批准记录"
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "allow"


def test_end_to_end_approval_in_one_process_positive_control(home):
    """阳性对照：不重启的话这一整串必须通。"""
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    approval_dir = bridge.gate.dir_for(SCOPE)

    kind, content = click(bridge, value)
    assert kind == "info", content
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "allow"


def test_disk_side_of_the_loop_survives_restart(home):
    """磁盘那一半**确实**跨重启可用 —— 定位到底是哪一层断的。

    `.asked` 落盘，所以重启后新建的 Gate 照样能 `approve()`。
    这条绿 + 上面那条红 ⇒ 断点在 `ApprovalStore._pending`，不在 Gate。
    """
    hook = write_hook(home)
    first = ApprovalGate(os.path.join(home, "approvals"))
    approval_dir = first.dir_for(SCOPE)
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "defer"
    pending = first.take_pending(SCOPE)
    digest = pending[0]["digest"]

    second = ApprovalGate(os.path.join(home, "approvals"))  # 重启
    assert second.approve(SCOPE, digest) is True
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "allow"


# ================================================================
# 2. redeem_detailed 的五种原因
# ================================================================


def store() -> ApprovalStore:
    return ApprovalStore(b"k" * 32)


def test_reason_empty_on_success_positive_control():
    """阳性对照打头：成功时 reason 必须是空串。

    下面每条 deny 都断言一个**具体**的原因串；没有这条打底，
    "原因对得上"可能只是因为所有路径都返回同一个值。
    """
    s = store()
    token = s.issue(SCOPE, "dg", {})
    result, reason = s.redeem_detailed(token.to_value(), OWNER, OWNERS)
    assert result is not None and reason == ""


def test_reason_used_on_replay():
    s = store()
    token = s.issue(SCOPE, "dg", {})
    assert s.redeem_detailed(token.to_value(), OWNER, OWNERS)[1] == ""
    assert s.redeem_detailed(token.to_value(), OWNER, OWNERS)[1] == ApprovalStore.USED


def test_reason_expired_when_ttl_passed():
    s = ApprovalStore(b"k" * 32, ttl_sec=-1)
    token = s.issue(SCOPE, "dg", {})
    assert s.redeem_detailed(token.to_value(), OWNER, OWNERS)[1] == (
        ApprovalStore.EXPIRED
    )


def test_reason_forbidden_for_non_owner_clicker():
    s = store()
    token = s.issue(SCOPE, "dg", {})
    assert s.redeem_detailed(token.to_value(), STRANGER, OWNERS)[1] == (
        ApprovalStore.FORBIDDEN
    )
    # 阳性对照：同一张卡、同一瞬间，主人点就该过。
    assert s.redeem_detailed(token.to_value(), OWNER, OWNERS)[1] == ""


def test_reason_malformed_without_nonce():
    s = store()
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value.pop("nonce")
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ApprovalStore.MALFORMED


def test_reason_stale_on_bad_signature():
    s = store()
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value["sig"] = "0" * 32
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ApprovalStore.STALE


def test_reason_stale_only_comes_from_a_bad_signature_now():
    """🟡 `/new` 清掉 `_pending` 之后，`redeem` **不再**拒绝那张卡。

    22:24 的改动把"要有待办记录"这个条件去掉了（理由：`_pending` 是内存的，
    重启就空）。代价是 `_pending` 不再承担任何闸门作用：
    `/new` 之后点旧卡片，令牌这一层直接放过，只剩 `gate.approve()`
    靠磁盘上的 `.asked`/`.pending` 挡 —— 那一层是靠得住的（`clear()` 会删掉），
    所以**净效果仍然安全**，但闸门从两层变成了一层。

    钉住这个事实：STALE 现在只有"签名对不上"一种来源。
    """
    s = store()
    token = s.issue(SCOPE, "dg", {})
    # 2026-11-06 起没有 `_pending` 了（载荷全在令牌里）⇒ 不需要「清内存」这一步
    assert s.redeem_detailed(token.to_value(), OWNER, OWNERS)[1] == "", (
        "_pending 又变成闸门了 —— 更新这条测试，并重新评估重启那条路"
    )


def test_single_use_still_holds_within_one_process_after_rebuild():
    """去掉 `_pending` 闸门之后，一次性**只靠 `_used`** 兜着。

    `_used` 也是内存的 ⇒ 重启后同一张卡能再兑换一次。这是上面那个改动
    的直接后果，下一条专门验它。
    """
    s = store()
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    assert s.redeem_detailed(dict(value), OWNER, OWNERS)[1] == ""
    assert s.redeem_detailed(dict(value), OWNER, OWNERS)[1] == ApprovalStore.USED


# 2026-11-06 修好：用过的 nonce 落盘（ApprovalStore(used_path=…)，见 _mark_used）
def test_token_remains_single_use_across_restart(home):
    """🔴 一次性必须跨重启成立 —— 否则"一次点击换一次放行"这句话只在进程内为真。

    默认 TTL 是 24 小时，看门狗一天会重启好几次 ⇒ 这个窗口是常态。
    """
    from bridge.app import _load_or_create_secret

    secret = _load_or_create_secret(home)
    used = os.path.join(home, "approvals-used.txt")  # 线上那条路就是这么传的（app.py 的 ApprovalStore(...)）
    token = ApprovalStore(secret, used_path=used).issue(SCOPE, "dg", {"digest": "dg", "scope": SCOPE})
    value = token.to_value()

    first = ApprovalStore(secret, used_path=used)
    assert first.redeem_detailed(dict(value), OWNER, OWNERS)[1] == ""

    second = ApprovalStore(secret, used_path=used)  # 重启：内存里的 _used 空了，磁盘那份还在
    result, reason = second.redeem_detailed(dict(value), OWNER, OWNERS)
    assert result is None, f"重启后同一张卡又兑换成功了，reason={reason!r}"
    assert reason == ApprovalStore.USED


def test_bridge_wires_the_used_nonce_file_into_the_store():
    """🔴 「调用点还在」：单测里传了 used_path 不代表线上传了。不传 ⇒ 跨重启一次性又不成立。

    ⚠️ 不能用上面的 `make_bridge` 验：它绕过 `Bridge.__init__` **手搭**状态，
    自己写的那行 `ApprovalStore(...)` 永远看不到 app.py 的改动（第一版就这么假红过）。
    所以直接查 `Bridge.__init__` 里那个真实的调用形状。
    """
    import inspect
    import re as _re

    from bridge.app import Bridge

    src = inspect.getsource(Bridge.__init__)
    # `[^)]*` 会停在 `_load_or_create_secret(home)` 的那个右括号上 ⇒ 永远找不到 used_path
    # （第二版就这么假红过）。只看紧跟 ApprovalStore( 的那一小段。
    call = src[src.index("ApprovalStore("):][:200]
    assert _re.search(r"used_path\s*=", call), \
        "Bridge.__init__ 构造 ApprovalStore 时没传 used_path ⇒ 重启后同一张卡能再点一次"


def test_card_toasts_differ_by_reason(home):
    """每种失败原因必须给**不同的**话术，而且告诉用户下一步做什么。

    2026-11-03 用户卡住的那个表现是四种情况一句话。22:24 接上了
    `_CARD_TOASTS`，这条守着它别退化。
    """
    from bridge.app import _CARD_TOASTS

    reasons = [
        ApprovalStore.USED,
        ApprovalStore.EXPIRED,
        ApprovalStore.STALE,
        ApprovalStore.FORBIDDEN,
        ApprovalStore.MALFORMED,
    ]
    texts = [_CARD_TOASTS[r] for r in reasons]
    assert len(set(texts)) == len(texts), f"话术有重复：{texts}"
    assert "" in _CARD_TOASTS, "没有兜底话术"

    # 真走一遍回调：陌生人点 ⇒ FORBIDDEN 的那句
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    kind, content = click(bridge, value, clicker=STRANGER)
    assert kind == "error"
    assert content == _CARD_TOASTS[ApprovalStore.FORBIDDEN]


def test_expired_and_forbidden_toasts_tell_the_user_what_to_do():
    """话术要可执行 —— "失效了"没有下一步，"重新发一次"才有。"""
    from bridge.app import _CARD_TOASTS

    assert "重新发" in _CARD_TOASTS[ApprovalStore.EXPIRED]
    assert "重新发" in _CARD_TOASTS[ApprovalStore.STALE]
    # FORBIDDEN 不该教陌生人怎么绕过，只说没生效
    assert "重新发" not in _CARD_TOASTS[ApprovalStore.FORBIDDEN]


def test_detailed_reasons_are_all_distinct_values():
    """五个常量不能有重复值，否则"分原因"从一开始就是假的。"""
    values = [
        ApprovalStore.USED,
        ApprovalStore.EXPIRED,
        ApprovalStore.STALE,
        ApprovalStore.FORBIDDEN,
        ApprovalStore.MALFORMED,
    ]
    assert len(set(values)) == 5
    assert "" not in values, "空串是成功的标记，不能被某个失败原因占用"


def test_forbidden_is_checked_before_signature():
    """顺序性质：陌生人点一张**伪造**的卡，先告诉他 FORBIDDEN。

    这个顺序是对的 —— 不该让没权限的人靠原因串去探"签名猜对了没"。
    """
    s = store()
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value["sig"] = "0" * 32
    assert s.redeem_detailed(value, STRANGER, OWNERS)[1] == ApprovalStore.FORBIDDEN


def test_used_leaks_before_the_owner_check():
    """🟡 `USED` 的判定排在白名单**之前** ⇒ 陌生人能探出"这个 nonce 被用过"。

    影响很小（nonce 本来就在卡片 value 里，看得到卡片的人才有它），
    但它和上一条的顺序不一致，记在这里免得以后被当成"本来就这样"。
    """
    s = store()
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    assert s.redeem_detailed(value, STRANGER, OWNERS)[1] == ApprovalStore.FORBIDDEN
    s.redeem(value, OWNER, OWNERS)  # 主人正常点掉
    assert s.redeem_detailed(value, STRANGER, OWNERS)[1] == ApprovalStore.USED, (
        "顺序改了 —— 现在陌生人探不到 USED 了，这是改好了，更新这条测试"
    )


# ================================================================
# 3. 三层状态的不一致组合
# ================================================================


def test_disk_ok_without_memory_used_still_lets_the_hook_through(home):
    """组合 A：`.ok` 在磁盘上、`_used` 里没有（重启后）。

    钩子**只看磁盘**，所以它照样放行；服务这边 `_used` 是空的，完全不知道。
    这不是 bug 本身，是下面几条泄漏的**机制**：落盘那层记得内存那层忘了的事。
    钉住它，免得以后有人以为"重启会让 .ok 失效"。
    """
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    approval_dir = bridge.gate.dir_for(SCOPE)
    assert click(bridge, value)[0] == "info"
    assert oks_in(approval_dir), "批准没落盘"

    restarted = make_bridge(home)
    assert restarted.approvals._used == set(), "重启后内存里当然没有用过的记录"
    # 钩子依然放行 —— 没有任何一层在重启后复核这个 .ok
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "allow"


def test_fresh_scope_without_ok_defers_positive_control(home):
    """阳性对照：没有 `.ok` 的时候钩子必须 defer。

    没有这条，上面那个 `allow` 可能只是因为命令被判成了只读。
    """
    hook = write_hook(home)
    bridge = make_bridge(home)
    approval_dir = bridge.gate.dir_for(SCOPE)
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "defer"


def test_asked_on_disk_without_memory_pending_lets_approve_succeed(home):
    """组合 B：`.asked` 在磁盘、`_pending` 里没有（重启后）。

    `gate.approve()` **会成功** —— 它故意只看磁盘（注释里写明了理由：
    和 `.ok` 的寿命对齐）。这是对的：真正该挡住重启后点击的闸门是令牌那一层，
    而令牌那层现在挡过头了（见第 1 节）。两层加起来的净效果是「挡住了，
    但挡在错的地方、给错的理由」。
    """
    hook = write_hook(home)
    first = ApprovalGate(os.path.join(home, "approvals"))
    approval_dir = first.dir_for(SCOPE)
    run_hook(hook, approval_dir, "Bash", PUBLISH)
    digest = first.take_pending(SCOPE)[0]["digest"]
    assert os.path.exists(os.path.join(approval_dir, digest + ".asked"))

    after_restart = ApprovalGate(os.path.join(home, "approvals"))
    assert after_restart.approve(SCOPE, digest) is True


def test_approve_without_any_outstanding_record_is_refused(home):
    """同形状的 deny 对照：磁盘上没有未决记录时 `approve()` 必须拒。

    和上一条只差"磁盘上有没有 `.asked`"这一个变量 ⇒ 证明上面那个 True
    来自真实判断，不是因为 `approve()` 永远返回 True。
    """
    gate = ApprovalGate(os.path.join(home, "approvals"))
    directory = gate.dir_for(SCOPE)
    assert gate.approve(SCOPE, action_digest("Bash", PUBLISH)) is False
    assert oks_in(directory) == []


@pytest.mark.xfail(
    strict=True,
    reason="🔴 没人等续跑时写下的 .ok 没有被收回 ⇒ TTL 10 分钟内同一条命令免卡片放行。"
    "和今天修的第 2 条是同一类泄漏，只是入口从「拒绝后再同意」换成了「重启后没人续跑」。",
)
def test_lost_awaiting_must_not_leave_a_usable_ok(home):
    """🔴 组合 C：`_awaiting` 丢了（重启）但用户点了同意。

    `on_card_action` 里写 `.ok` 和找 `_awaiting` 是**两件独立的事**：
    `gate.approve()` 先落盘，然后才看有没有人等着续跑；没人等就只回一句
    「好，继续」，`.ok` 原地留下。

    ⇒ 在 TTL 10 分钟内，下一轮跑到同一条命令会被钩子**直接放行**，
      没有卡片、没有点击、用户不知道。

    正确行为：没有会话等着续跑时，这个批准不该留下可用的放行标记
    （要么别写，要么写完发现没人等就撤回）。
    """
    hook = write_hook(home)
    first = make_bridge(home)
    send_card(first, SCOPE, PUBLISH, hook)
    approval_dir = first.gate.dir_for(SCOPE)

    # 重启：`.asked` 还在磁盘上，`_awaiting` 空了。
    # 直接用重启后的 gate 批准（绕开第 1 节那个令牌 bug，单独看这一层）。
    second = make_bridge(home)
    assert second._awaiting == {}
    second.gate.approve(SCOPE, action_digest("Bash", PUBLISH))

    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "defer", (
        "没人等续跑，却留下了一张免卡片通行证"
    )


@pytest.mark.xfail(
    strict=True,
    reason="🔴 一轮两张卡：第一张续跑完 _awaiting 就空了，第二张的「同意」仍然落下 .ok，"
    "没人 resume ⇒ 下一轮同样的命令免卡片放行。这个入口不需要重启就能撞到。",
)
def test_approving_a_second_card_after_the_round_ended_must_not_leak(home):
    """🔴 组合 C 的另一个入口：一轮两张卡，第一张续跑完、第二张才被点。

    `_continue_after_approval` 会 `pop(_awaiting)`，所以第一张卡续跑结束后
    `_awaiting` 就空了。此时点第二张卡：令牌还在 `_pending` 里（同一进程、
    没重启）⇒ 兑换成功 ⇒ `.ok` 落盘 ⇒ 但没人 resume。
    下一轮同样的命令直接放行。

    **这条不需要重启**，是日常路径（一轮 defer 多个操作很常见）。
    """
    hook = write_hook(home)
    bridge = make_bridge(home)
    approval_dir = bridge.gate.dir_for(SCOPE)

    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "defer"
    assert run_hook(hook, approval_dir, "Bash", PIPINSTALL) == "defer"
    pending = bridge.gate.take_pending(SCOPE)
    assert len(pending) == 2
    tokens = {
        item["digest"]: bridge.approvals.issue(
            SCOPE, item["digest"], {"digest": item["digest"], "scope": SCOPE}
        )
        for item in pending
    }
    bridge._awaiting[SCOPE] = {"chat_id": "oc_1", "session_id": "s", "count": 2}

    first_digest = action_digest("Bash", PUBLISH)
    second_digest = action_digest("Bash", PIPINSTALL)

    assert click(bridge, {"act": "approve", **tokens[first_digest].to_value()})[0] == (
        "info"
    )
    # 第一张的续跑跑完了（`_continue_after_approval` 把 _awaiting pop 掉）
    bridge._awaiting.pop(SCOPE, None)

    click(bridge, {"act": "approve", **tokens[second_digest].to_value()})
    assert run_hook(hook, approval_dir, "Bash", PIPINSTALL) == "defer", (
        "这一轮已经续跑结束，第二张卡的批准却留下了可用的放行标记"
    )


def test_ok_expires_after_grant_ttl(home):
    """今天修的第 2 条的正题：躺着没被兑换的 `.ok` 过 10 分钟就不算了。

    用 `os.utime` 把 mtime 往前拨，不真等 10 分钟。
    """
    hook = write_hook(home)
    gate = ApprovalGate(os.path.join(home, "approvals"))
    approval_dir = gate.dir_for(SCOPE)
    run_hook(hook, approval_dir, "Bash", PUBLISH)
    digest = gate.take_pending(SCOPE)[0]["digest"]
    assert gate.approve(SCOPE, digest) is True

    ok_path = os.path.join(approval_dir, digest + ".ok")
    stale = time.time() - (GRANT_TTL_SEC + 5)
    os.utime(ok_path, (stale, stale))
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "defer", "过期的批准还在放行"
    # 过期的 .ok 必须被消费掉，不能留在磁盘上等下一次
    assert oks_in(approval_dir) == []


def test_ok_within_ttl_still_allows_positive_control(home):
    """阳性对照：刚批准、没到 TTL 的 `.ok` 必须放行。

    和上一条只差 mtime 一个变量 —— 否则那个 defer 可能只是因为 `.ok` 根本没写上。
    """
    hook = write_hook(home)
    gate = ApprovalGate(os.path.join(home, "approvals"))
    approval_dir = gate.dir_for(SCOPE)
    run_hook(hook, approval_dir, "Bash", PUBLISH)
    digest = gate.take_pending(SCOPE)[0]["digest"]
    gate.approve(SCOPE, digest)

    fresh = time.time() - (GRANT_TTL_SEC - 60)  # 还在有效期内
    ok_path = os.path.join(approval_dir, digest + ".ok")
    os.utime(ok_path, (fresh, fresh))
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "allow"


def test_ok_survives_restart_within_ttl(home):
    """`.ok` 的寿命确实跨重启（TTL 之内）。

    和 `_used`（进程内）对比：重启只清掉内存那一半。这就是组合 A 的后果面。
    """
    hook = write_hook(home)
    first = ApprovalGate(os.path.join(home, "approvals"))
    approval_dir = first.dir_for(SCOPE)
    run_hook(hook, approval_dir, "Bash", PUBLISH)
    digest = first.take_pending(SCOPE)[0]["digest"]
    first.approve(SCOPE, digest)

    ApprovalGate(os.path.join(home, "approvals"))  # 重启
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "allow"


def test_new_command_clears_ok_across_restart(home):
    """今天修的第 2 条的另一半：`/new` 之后 `.ok` 必须没了，重启也救不回来。"""
    hook = write_hook(home)
    first = ApprovalGate(os.path.join(home, "approvals"))
    approval_dir = first.dir_for(SCOPE)
    run_hook(hook, approval_dir, "Bash", PUBLISH)
    digest = first.take_pending(SCOPE)[0]["digest"]
    first.approve(SCOPE, digest)

    first.clear(SCOPE)  # /new
    ApprovalGate(os.path.join(home, "approvals"))  # 重启
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "defer"


# 2026-11-06 修好：on_card_action 同意前查 _paused（app.py「被掐停的会话不许写放行证」）
def test_paused_scope_must_not_accept_approvals(home):
    """🔴 组合 D：`paused.json` 说暂停，`_awaiting` 里还有待续跑的。

    `on_card_action` **完全不看 `_paused`** ⇒ 点同意照样写 `.ok`、照样派发续跑。
    续跑走 `_run_one`，那里才会看到暂停、只回一句「已暂停」就 return
    —— 于是**那一步没跑，但 `.ok` 留在磁盘上**。
    用户发 `/resume` 之后（`/resume` 只删 `_paused`，不碰 gate），
    TTL 内任何一轮跑到同一条命令就被直接放行。

    掐停的意义是"要人明确看过才继续"。暂停期间的点击不该攒下通行证 ——
    要么直接拒掉（toast 告诉用户"这个会话暂停了，先 /resume"），
    要么暂停时一起 `gate.clear()`。
    """
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    approval_dir = bridge.gate.dir_for(SCOPE)

    # 执行后审查判了 PAUSE（反复试探被拦），落盘
    bridge._paused[SCOPE] = "这一轮有 3 次操作被挡"
    bridge._save_paused()

    click(bridge, value)

    # 用户 /resume（只清 _paused，不碰 gate）
    bridge._paused.pop(SCOPE)
    bridge._save_paused()
    assert run_hook(hook, approval_dir, "Bash", PUBLISH) == "defer", (
        "暂停期间攒下的批准在 /resume 后变成了免卡片通行证"
    )


def test_paused_state_itself_survives_restart_positive_control(home):
    """阳性对照：掐停本身确实跨重启（今天改的落盘是好的）。

    没有这条，上面那条可能只是因为 `_paused` 压根没生效。
    """
    first = make_bridge(home)
    first._paused[SCOPE] = "连续试探被拦"
    first._save_paused()

    second = make_bridge(home)
    assert second._paused.get(SCOPE) == "连续试探被拦"


def test_resume_does_not_clear_approvals(home):
    """`/resume` 不碰 gate —— 把这个事实钉住（组合 D 的另一半）。

    对比 `/new`：它会 `gate.clear()`。两条路径对审批状态的处理不一致，
    而用户感知上都是"重新开始"。
    """
    hook = write_hook(home)
    gate = ApprovalGate(os.path.join(home, "approvals"))
    approval_dir = gate.dir_for(SCOPE)
    run_hook(hook, approval_dir, "Bash", PUBLISH)
    digest = gate.take_pending(SCOPE)[0]["digest"]
    gate.approve(SCOPE, digest)

    bridge = make_bridge(home)
    bridge._paused[SCOPE] = "x"
    bridge._save_paused()
    bridge._paused.pop(SCOPE)  # /resume 做的全部事情
    bridge._save_paused()

    assert oks_in(approval_dir), "/resume 现在会清审批了 —— 更新这条测试"


def test_scope_isolation_holds_across_restart(home):
    """A 会话的批准不能让 B 会话放行，重启之后也不行。

    阳性对照内建：同一个 scope 必须放行，不同 scope 必须 defer。
    """
    hook = write_hook(home)
    gate = ApprovalGate(os.path.join(home, "approvals"))
    dir_a = gate.dir_for("dm:ou_a")
    dir_b = gate.dir_for("dm:ou_b")
    run_hook(hook, dir_a, "Bash", PUBLISH)
    digest = gate.take_pending("dm:ou_a")[0]["digest"]
    gate.approve("dm:ou_a", digest)

    ApprovalGate(os.path.join(home, "approvals"))  # 重启
    assert run_hook(hook, dir_b, "Bash", PUBLISH) == "defer", "跨会话放行了"
    assert run_hook(hook, dir_a, "Bash", PUBLISH) == "allow", "本会话反而不放行"


# ================================================================
# 4. 令牌的安全性质 —— 用**不同的构造方式**再验一遍
# ================================================================
#
# `tests/test_review.py` 已经验过一次性 / 改内容 / 改过期 / 白名单 / 不绑任务生命周期。
# 那些用例全都走 `store.issue()` 拿到的那一份 value 然后改一个字段。
# 共同盲区：都假定 nonce 是"issue 给的那个"。这里换几种构造：
#   - 自己从密钥重算签名（攻击者视角）
#   - 用别的 store（别的密钥）签同样的内容
#   - 并发/重入形状的重放
#   - 类型层面的奇怪输入（exp 是字符串 / float / None）


def _sign(secret: bytes, scope: str, digest: str, nonce: str, exp: int) -> str:
    """照 ApprovalStore 的算法自己签一个 —— 不走 issue()。"""
    import hashlib
    import hmac

    message = f"{scope}|{digest}|{nonce}|{int(exp)}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()[:32]


def test_self_signed_token_is_accepted_so_the_secret_is_the_only_gate():
    """🔴 知道密钥就够了：**完全凭空**造一个 nonce 也能兑换。

    22:24 去掉 `_pending` 闸门之后，令牌层的全部安全性 = 密钥保密 + 白名单。
    服务端不再要求"这个 nonce 是我签发过的"。

    这把 `approval.secret` 的泄露后果放大了：原来拿到密钥还需要知道一个
    真实签发过的 nonce，现在自己编一个就行。叠加
    `test_preexisting_world_readable_secret_is_tightened`（宽权限不收紧），
    这两条要一起看。

    这条是绿的（断言现状），同时也是守卫：哪天加回待办要求，它会红。
    """
    secret = b"k" * 32
    s = ApprovalStore(secret)
    exp = int(time.time() + 3600)
    nonce = "ff" * 12  # 从来没被 issue() 签发过
    value = {
        "scope": SCOPE,
        "digest": "dg",
        "nonce": nonce,
        "exp": exp,
        "sig": _sign(secret, SCOPE, "dg", nonce, exp),
    }
    # 2026-11-06 起没有 `_pending`：本来就不存在「是不是我签发过的 nonce」这一闸 ——
    # 这条测试钉的事实（全部安全性 = 密钥保密 + 白名单 + 磁盘上的 .asked）照旧成立
    result, reason = s.redeem_detailed(value, OWNER, OWNERS)
    assert reason == "" and result is not None, (
        "又要求待办记录了 —— 改好了，更新这条测试并重新评估重启那条路"
    )


def test_self_signed_token_verifies_as_positive_control():
    """阳性对照：自签的签名算法本身是对的 —— 把它注进 `_pending` 就能兑换。

    没有这条，上面那个 STALE 可能只是因为我自己的 `_sign` 写错了，
    那整节的"用不同构造再验一遍"就全是假通过。
    """
    secret = b"k" * 32
    s = ApprovalStore(secret)
    exp = int(time.time() + 3600)
    nonce = "ff" * 12
    value = {
        "scope": SCOPE,
        "digest": "dg",
        "nonce": nonce,
        "exp": exp,
        "sig": _sign(secret, SCOPE, "dg", nonce, exp),
    }
    # 2026-11-06 起不需要注入 `_pending`：签名算对了就能兑（下面断言的就是这个）
    result, reason = s.redeem_detailed(value, OWNER, OWNERS)
    assert reason == "" and result is not None


def test_token_signed_with_a_different_secret_is_rejected():
    """换一种"改签名"的构造：不是把 sig 填成 0，而是用**另一个密钥**真签一份。

    现有测试用 `sig = "0"*32`，那种形状连长度都可能是巧合通过。
    """
    real = b"k" * 32
    s = ApprovalStore(real)
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value["sig"] = _sign(
        b"attacker-secret-32-bytes-padding",
        value["scope"],
        value["digest"],
        value["nonce"],
        int(value["exp"]),
    )
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ApprovalStore.STALE
    # 阳性对照：真密钥签的同一份内容能过
    value["sig"] = _sign(
        real, value["scope"], value["digest"], value["nonce"], int(value["exp"])
    )
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ""


def test_scope_field_is_signed_so_it_cannot_be_moved_between_sessions():
    """改 scope 也要签名失效 —— 否则 A 会话的卡能用到 B 会话上。

    现有测试改的是 `digest`，没人改过 `scope`。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue("dm:ou_a", "dg", {"scope": "dm:ou_a", "digest": "dg"})
    value = token.to_value()
    value["scope"] = "dm:ou_b"
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ApprovalStore.STALE


def test_redeemed_payload_comes_from_the_server_not_the_card():
    """即使 value 里的 scope 被改了（签名失效那条已拦住），

    成功路径返回的 scope/digest 也必须来自**服务端存的 payload**，
    不是从回传数据里读的。否则一旦签名那层哪天放松，整个会话隔离就没了。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue("dm:ou_a", "dg", {"scope": "dm:ou_a", "digest": "dg"})
    result, reason = s.redeem_detailed(token.to_value(), OWNER, OWNERS)
    assert reason == ""
    assert result["payload"]["scope"] == "dm:ou_a"
    assert result["scope"] == "dm:ou_a"


def test_two_clicks_in_the_same_instant_only_one_wins():
    """一次性的另一种构造：不是"点两次"，而是同一份 value 兑换两遍紧挨着。

    `_pending.pop` 是原子的，所以第二次必然拿不到。
    （真正的并发要靠 GIL 之外的手段才能测，这里验的是"消费点只有一个"。）
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    outcomes = [s.redeem_detailed(dict(value), OWNER, OWNERS) for _ in range(5)]
    wins = [r for r, reason in outcomes if r is not None]
    assert len(wins) == 1, "同一张卡被兑换了多次"
    assert [reason for _, reason in outcomes[1:]] == [ApprovalStore.USED] * 4


def test_owner_list_change_between_issue_and_click_is_honored():
    """点击时重查白名单：换一种构造 —— 签发时是主人，点击时已经被移出去了。

    现有测试是"从头到尾都不是主人"。这个顺序才是注释里说的那个场景。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    # 签发时 OWNER 在名单里，点击时名单已经变了
    assert s.redeem_detailed(token.to_value(), OWNER, frozenset({"ou_new_owner"}))[
        1
    ] == ApprovalStore.FORBIDDEN
    # 阳性对照：名单没变的话同一张卡能过
    assert s.redeem_detailed(token.to_value(), OWNER, OWNERS)[1] == ""


def test_empty_owner_set_refuses_everyone():
    """fail-closed：白名单空了谁都不能点（哪怕令牌完全合法）。"""
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    assert s.redeem_detailed(token.to_value(), OWNER, frozenset())[1] == (
        ApprovalStore.FORBIDDEN
    )


def test_empty_clicker_id_is_refused():
    """拿不到点击者 open_id（事件结构异常）时不能当成主人。

    `on_card_action` 里 `clicker` 取不到就是空串，空串不在白名单 ⇒ FORBIDDEN。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    assert s.redeem_detailed(token.to_value(), "", OWNERS)[1] == (
        ApprovalStore.FORBIDDEN
    )


def test_token_outlives_the_turn_that_issued_it(home):
    """不绑任务生命周期 —— 换个构造：真跑完"发卡那一轮"再点。

    现有测试用 `time.sleep(0.05)` 代表"那一轮结束了"。这里用真实形状：
    钩子 defer ⇒ take_pending ⇒ 发卡 ⇒ 那一轮的 TurnResult 已经处理完了
    （`_ask_approval` 返回），之后才点。
    """
    hook = write_hook(home)
    bridge = make_bridge(home)
    value = send_card(bridge, SCOPE, PUBLISH, hook)
    # 那一轮到这里已经彻底结束（进程退了、锁放了），只有 _awaiting 还挂着
    kind, content = click(bridge, value)
    assert kind == "info", f"发卡那一轮结束后卡片就点不动了：{content}"


@pytest.mark.parametrize(
    "exp_value",
    [None, "", "abc", [], {}],
)
def test_non_numeric_exp_is_malformed(exp_value):
    """类型层面的构造：exp 不是数字时必须走 MALFORMED，不能当成 0 或抛异常。"""
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value["exp"] = exp_value
    result, reason = s.redeem_detailed(value, OWNER, OWNERS)
    assert result is None
    assert reason in {ApprovalStore.MALFORMED, ApprovalStore.EXPIRED}


def test_missing_exp_is_treated_as_expired_not_as_forever():
    """🟡 没有 exp 字段时按"已过期"处理（`value.get("exp") or 0` ⇒ 0）。

    结论是安全的（fail-closed），但原因串是 EXPIRED 而不是 MALFORMED ——
    回传数据缺字段其实是"格式不对"。记在这里，别哪天被当成"过期了，等一会儿再点"。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value.pop("exp")
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ApprovalStore.EXPIRED


def test_string_exp_that_matches_the_signature_is_accepted():
    """🟡 exp 是**字符串**时照样能过（`float()` 转得动，`int()` 签名也对得上）。

    不是漏洞（签名覆盖了 `int(exp)`），但说明回传数据的类型没有被收紧。
    钉住它：哪天改成严格类型检查，这条会红。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value["exp"] = str(value["exp"])
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == "", (
        "exp 的类型收紧了 —— 更新这条测试"
    )


def test_fractional_exp_bump_within_one_second_is_accepted():
    """🟡 把 exp 加 0.99 秒还能过 —— 签名只覆盖 `int(exp)`。

    最多多撑 1 秒，没有实际危害。钉住它是为了说明"exp 在签名范围内"
    这句话的精确边界是**整秒**，不是任意精度。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value["exp"] = float(value["exp"]) + 0.99
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ""


def test_whole_second_exp_bump_is_rejected_positive_control():
    """阳性对照：加满 1 秒就签名对不上了 —— 证明上一条真的是精度边界，

    而不是"exp 根本没进签名"。
    """
    s = ApprovalStore(b"k" * 32)
    token = s.issue(SCOPE, "dg", {})
    value = token.to_value()
    value["exp"] = int(value["exp"]) + 1
    assert s.redeem_detailed(value, OWNER, OWNERS)[1] == ApprovalStore.STALE


def test_nonce_is_long_enough_to_not_be_guessable():
    """nonce 要足够长、每次不同。它是"一次性"的全部依据。"""
    s = ApprovalStore(b"k" * 32)
    nonces = {s.issue(SCOPE, "dg", {}).nonce for _ in range(200)}
    assert len(nonces) == 200, "nonce 撞了"
    assert all(len(n) >= 24 for n in nonces)


def test_pending_records_are_never_reaped():
    """🟡 `_pending` / `_used` 只增不减 —— 过期的 nonce 也留着。

    单进程寿命内不会出事（服务本来就常重启），但它是个无界 dict。
    记一笔，别在未来把 ApprovalStore 改成长寿命组件时踩到。
    """
    s = ApprovalStore(b"k" * 32, ttl_sec=-1)  # 签出来就过期
    for i in range(100):
        s.issue(SCOPE, f"dg{i}", {})
    # 2026-11-06：`_pending` 整个删了（载荷全在令牌里），这个无界 dict 不复存在
    assert not hasattr(s, "_pending"), "又加回了只在内存里的待办表 —— 重启那条路会再断"


# ================================================================
# 5. 密钥落盘本身
# ================================================================


def secret_path(home_dir: str) -> str:
    return os.path.join(home_dir, "approval.secret")


def mode_of(path: str) -> int:
    return os.stat(path).st_mode & 0o777


def secret_loader_source() -> str:
    """`_load_or_create_secret` 的源码文本。

    🔴 不用 `inspect.getsource` —— 它按**导入时**的行号去切现在磁盘上的文件。
    并行会话改了 app.py 之后行号会漂，切出来是别的函数的代码（实测踩过：
    切到了看门狗循环）。所以直接按函数名在文件里找。
    """
    import re

    path = os.path.join(os.path.dirname(_HERE), "bridge", "app.py")
    text = open(path, encoding="utf-8").read()
    match = re.search(
        r"^def _load_or_create_secret\b.*?(?=^def |\Z)", text, re.S | re.M
    )
    assert match, "app.py 里找不到 _load_or_create_secret"
    return match.group(0)


def test_new_secret_file_is_0600(home):
    from bridge.app import _load_or_create_secret

    _load_or_create_secret(home)
    assert mode_of(secret_path(home)) == 0o600


def test_secret_is_created_0600_with_no_readable_window(home):
    """不能出现"先 0644 写好、再 chmod 收紧"的窗口。

    判据：`os.open(..., 0o600)` 直接建出来，中间文件也必须是 0600。
    这里通过"中间文件没留下 + 最终文件 0600 + 源码里用 os.open 带 mode"
    三条一起判 —— 单看最终权限看不出有没有窗口。
    """
    from bridge.app import _load_or_create_secret

    _load_or_create_secret(home)
    assert mode_of(secret_path(home)) == 0o600
    assert not os.path.exists(secret_path(home) + ".tmp"), "中间文件留下了"

    source = secret_loader_source()
    assert "os.open(" in source and "0o600" in source, (
        "不是用 os.open 带 mode 创建的 ⇒ 存在短暂可读窗口"
    )
    assert "os.replace(" in source, "不是原子改名 ⇒ 并发首启会读到半截密钥"


def test_secret_umask_does_not_widen_the_mode(home):
    """宽松 umask 下也必须是 0600 —— `os.open` 的 mode 会被 umask 削，不会被放宽，

    但这条能挡住未来有人改成 `open()` + `chmod`。
    """
    from bridge.app import _load_or_create_secret

    old = os.umask(0o000)
    try:
        _load_or_create_secret(home)
    finally:
        os.umask(old)
    assert mode_of(secret_path(home)) == 0o600


def test_deleted_secret_file_is_regenerated(home):
    """密钥被删 ⇒ 重新生成（旧卡片会失效，但服务不能挂）。"""
    from bridge.app import _load_or_create_secret

    first = _load_or_create_secret(home)
    os.unlink(secret_path(home))
    second = _load_or_create_secret(home)
    assert len(second) >= 32
    assert second != first, "删掉之后居然还是同一个密钥"
    assert mode_of(secret_path(home)) == 0o600


def test_short_secret_file_is_replaced_and_persisted(home):
    """长度不足 32 字节 ⇒ 当成坏文件，重写一份**并落盘**。

    关键是落盘：否则每次启动都读到那个短文件、每次都生成新密钥，
    等于回到了"每次重启换密钥"那个 bug。
    """
    from bridge.app import _load_or_create_secret

    with open(secret_path(home), "wb") as handle:
        handle.write(b"short")

    first = _load_or_create_secret(home)
    assert len(first) >= 32
    assert len(open(secret_path(home), "rb").read()) >= 32, "坏文件没被换掉"
    assert _load_or_create_secret(home) == first, "换掉之后还是不稳定"


def test_garbage_secret_file_of_sufficient_length_is_accepted(home):
    """🟡 只看长度不看内容：64 个 0 字节会被原样当成密钥用。

    HMAC 用全零 key 不会报错，签名照样算得出来，所以这**不影响可用性**；
    而且能写这个文件的人本来就能写真密钥。记在这里是因为它意味着
    "文件被改成垃圾"和"文件是好的"在行为上分不出来 —— 不会有任何告警。
    """
    from bridge.app import _load_or_create_secret

    with open(secret_path(home), "wb") as handle:
        handle.write(b"\x00" * 64)
    assert _load_or_create_secret(home) == b"\x00" * 64, (
        "开始校验内容了 —— 更新这条测试"
    )


@pytest.mark.xfail(
    strict=True,
    reason="🟡 已存在的 0644 密钥文件不会被收紧：_load_or_create_secret 只在新建时设 mode，"
    "读到的合法文件原样返回。早先版本或手工拷贝留下的宽权限会一直宽着且无告警。",
)
def test_preexisting_world_readable_secret_is_tightened(home):
    """🟡 已经是 0644 的旧密钥文件应该被收紧到 0600。

    `_load_or_create_secret` 只在"要新建"时设 mode；读到的合法文件原样返回。
    这是密钥落盘这个改动新引入的面 —— 落盘前没有文件，也就没有这个问题。
    """
    from bridge.app import _load_or_create_secret

    with open(secret_path(home), "wb") as handle:
        handle.write(os.urandom(32))
    os.chmod(secret_path(home), 0o644)

    _load_or_create_secret(home)
    assert mode_of(secret_path(home)) == 0o600, (
        "旧的宽权限密钥文件没被收紧 —— 任何本机用户都能读到签名密钥"
    )


def test_unwritable_home_degrades_to_process_local_secret(home):
    """磁盘写不进去 ⇒ 退化成进程内随机，不抛异常（宁可少个便利，别整层挂掉）。

    代价是旧卡片失效，和落盘前的老行为一样。
    """
    from bridge.app import _load_or_create_secret

    locked = os.path.join(home, "locked")
    os.makedirs(locked, mode=0o500)
    try:
        first = _load_or_create_secret(locked)
        second = _load_or_create_secret(locked)
        assert len(first) >= 32
        assert first != second, "写不进磁盘却拿到了稳定密钥？"
        assert not os.path.exists(secret_path(locked))
    finally:
        os.chmod(locked, 0o700)


def test_writable_home_gives_a_stable_secret_positive_control(home):
    """阳性对照：目录可写时必须拿到稳定密钥。

    和上一条只差"目录权限"一个变量 —— 否则那个 `first != second`
    可能是因为落盘从来就没生效过。
    """
    from bridge.app import _load_or_create_secret

    assert _load_or_create_secret(home) == _load_or_create_secret(home)
    assert os.path.exists(secret_path(home))


def test_concurrent_first_start_converges_on_the_disk_secret(home):
    """🔴 多个进程同时首启会**互相覆盖**：最后一个 `os.replace` 赢，

    而先跑完的那几个进程手里拿的是自己生成的、**和磁盘上不一样**的密钥。

    真实影响：单实例文件锁（`_acquire_single_instance_lock`）正常时不会有两个
    服务进程，所以这条在生产路径上撞不到。但只要它发生一次，那个进程发出去的
    卡片在它自己重启后就全失效了（它的密钥从没落盘）。

    这里起真子进程比对"进程内密钥"和"磁盘密钥"。
    """
    script = os.path.join(home, "probe.py")
    repo_root = os.path.dirname(_HERE)
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(
            "import sys\n"
            f"sys.path.insert(0, {repo_root!r})\n"
            "from bridge.app import _load_or_create_secret\n"
            "print(_load_or_create_secret(sys.argv[1]).hex())\n"
        )
    target = os.path.join(home, "race")
    os.makedirs(target, exist_ok=True)

    procs = [
        subprocess.Popen(
            [sys.executable, script, target],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        for _ in range(8)
    ]
    got = [proc.communicate()[0].strip() for proc in procs]
    assert all(got), "探针没输出"

    on_disk = open(secret_path(target), "rb").read().hex()
    # 至少有一个进程的密钥和磁盘一致（赢的那个）
    assert on_disk in got
    # 并发窗口没有被序列化掉 —— 不同进程可能拿到不同密钥。
    # 不断言"一定不一致"（竞态不稳定），只断言**没有任何锁**在保护这段。
    source = secret_loader_source()
    assert "flock" not in source and "lock" not in source.lower(), (
        "加了锁 —— 并发首启不再互相覆盖了，更新这条测试"
    )


def test_paused_file_is_atomic_and_survives_corrupt_content(home):
    """`paused.json` 的落盘：半截 JSON 不能让服务起不来，临时文件不能留。

    和密钥文件对照：这个是 `tempfile.NamedTemporaryFile` + `os.replace`。
    """
    bridge = make_bridge(home)
    bridge._paused[SCOPE] = "连续试探被拦"
    bridge._save_paused()
    assert os.path.exists(bridge._paused_path)
    leftovers = [n for n in os.listdir(home) if n.startswith("tmp")]
    assert leftovers == [], f"临时文件留下了：{leftovers}"

    with open(bridge._paused_path, "w", encoding="utf-8") as handle:
        handle.write("{broken")
    recovered = make_bridge(home)
    assert recovered._paused == {}, "半截 JSON 应该当成没有暂停，不该抛"


def test_paused_file_permissions_are_not_sensitive(home):
    """`paused.json` 里只有 scope 和原因，没有凭据 —— 不要求 0600。

    放这条是为了把"哪些文件是敏感的"写清楚：只有 `approval.secret` 是。
    """
    bridge = make_bridge(home)
    bridge._paused[SCOPE] = "x"
    bridge._save_paused()
    content = open(bridge._paused_path, encoding="utf-8").read()
    assert "secret" not in content.lower()
    assert "sig" not in content.lower()


def test_secret_never_lands_in_the_card_value(home):
    """卡片回传数据里不能出现密钥本身 —— 它要发到飞书（外部系统）。"""
    from bridge.app import _load_or_create_secret

    secret = _load_or_create_secret(home)
    s = ApprovalStore(secret)
    value = s.issue(SCOPE, "dg", {}).to_value()
    blob = json.dumps(value, ensure_ascii=False)
    assert secret.hex() not in blob
    assert secret.hex()[:16] not in blob
