"""需要人介入的群推送：@ bot 的主人（产品定义 §5.8，验收 FC-08）。

推「在等你 / 出错」时消息里 @ 主人（`at_owner_line`）。2026-11-17 起不再加急：
林森要求只 @ 主人，不发应用内加急。
"""

from __future__ import annotations

from typing import Iterable


def at_owner_line(owner_ids: Iterable[str]) -> str:
    """post 富文本 md 里的 @：`<at user_id="ou_xxx"></at>`。只认 ou_ 开头的 id。"""
    ids = sorted({str(i) for i in owner_ids or () if str(i).startswith("ou_")})
    return " ".join(f'<at user_id="{i}"></at>' for i in ids)
