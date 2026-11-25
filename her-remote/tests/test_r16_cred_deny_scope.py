"""第 16 轮：凭据 deny 只管工作目录下面 —— 放开读范围之前必须先补这个口子。

## 实测（真起 claude 2.1.319，2026-11-07，主会话）

凭据放在工作目录**外**：

- 拿 `bypassPermissions` 跑（**不是**桥的真实参数）：
  `Read(**/.env*)` → 读到了；`Read(//**/.env*)` → DENIED
  ⇒ `**/` 是相对工作目录的 glob，要盖住整台机器得写 `//**/`（P2 审计也实测到同一点）。
- 拿**桥全授权档的真实参数**跑（`--permission-mode manual` + 读工具限定在工作目录）：
  修复前、修复后工具层都拒了 1 次，凭据内容都没进模型上下文。

🔴 所以**现在线上没有漏**：读工具本来就只允许工作目录（`Read(<ws>/**)`），目录外的被拒是
「不在允许清单里」拒的，不是凭据 deny 拒的。我第一版把这个文件写成「现在就漏」——
那是拿 bypass 参数测出来的结论，套到桥身上是错的（同一天第三次差点把实验条件当现场）。

这条要紧在**下一步**：领导要远程看电脑，读范围要放到工作目录外（P2 #1）。一旦放开，
凭据就只剩这几条 deny 挡着 ⇒ 它们必须先改成 `//**/`。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.policy import ALWAYS_DENY

CRED_SHAPES = (".env", ".pem", ".key", "id_rsa", ".aws", ".ssh", "credentials")


def _read_rules():
    return [r for r in ALWAYS_DENY if r.startswith("Read(")]


def test_every_credential_read_rule_covers_the_whole_machine():
    """每一类凭据都要有一条 `//**/` 写法（盖整台机器）。只有 `**/` 那份 = 只盖工作目录下面。"""
    rules = _read_rules()
    bad = [s for s in CRED_SHAPES if not any(r.startswith("Read(//") and s in r for r in rules)]
    assert not bad, f"这几类只管工作目录下面，目录外的照样能读：{bad}"


def test_all_credential_shapes_are_still_denied():
    """阳性对照：改写法不许把哪一类漏掉。"""
    blob = " ".join(_read_rules())
    missing = [s for s in CRED_SHAPES if s not in blob]
    assert not missing, f"凭据 deny 少了：{missing}"


def test_both_spellings_kept_for_older_cli():
    """保留 `**/` 那份：老版本 CLI 认不认 `//` 没测过，两份都写不会更松。"""
    rules = _read_rules()
    for shape in CRED_SHAPES:
        assert any(r.startswith("Read(//") and shape in r for r in rules), shape
