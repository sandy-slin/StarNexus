"""回复里的「选一个」⇒ 选项按钮（FC-07，§5.3）。

2026-11-17 实测（claude 2.1.325）：后台续跑走 `claude -p`，这个模式**没有 AskUserQuestion 工具**
（显式 `--allowed-tools AskUserQuestion` 也一样），Claude 会改成在回复里写「问题 + 1. 2. 编号选项」。
所以手机上回答问题的入口放在回复上：认出结尾那组编号选项，挂成按钮，点一下 = 把那一项发回会话。
"""

from __future__ import annotations

import re

MAX_OPTIONS = 6
LABEL_CHARS = 40
TAIL_LINES = 14

_OPTION = re.compile(r"^\s*(?:\(?(\d{1,2})[.、)）]|([A-Fa-f])[.、)）])\s+(.+?)\s*$")
_ASKING = re.compile(r"[?？]|请选|选哪|选一|选择|哪个|哪一|which|choose|pick|prefer|reply with", re.I)


def _clean(label: str) -> str:
    text = re.sub(r"[*_`]+", "", label).strip()
    text = re.split(r"\s+[—–-]\s+|[:：]\s*", text, maxsplit=1)[0].strip() or text
    return text[:LABEL_CHARS]


def extract_choices(text: str) -> list:
    """回复结尾附近一组连续编号选项（2~6 个），且前后有在问的意思 ⇒ 选项文字；否则 []。"""
    lines = str(text or "").rstrip().splitlines()[-TAIL_LINES:]
    best: list = []
    current: list = []
    start = 0
    for index, line in enumerate(lines):
        match = _OPTION.match(line)
        if match:
            if not current:
                start = index
            current.append(_clean(match.group(3)))
            continue
        if current:
            best, best_start, current = current, start, []
            best_end = index
    if current:
        best, best_start, best_end = current, start, len(lines)
    if not (2 <= len(best) <= MAX_OPTIONS) or not all(best):
        return []
    context = "\n".join(lines[max(0, best_start - 3):best_start] + lines[best_end:best_end + 3])
    if not _ASKING.search(context):
        return []
    return best


ACT = "ch"


def choice_value(nonce: str, index: int) -> dict:
    return {"act": ACT, "n": nonce, "i": int(index)}


def resolve_choice_click(value) -> tuple:
    """回调 value → (序号, 第几项)；形状不对 ⇒ ("", -1)。选项文字在服务端，卡上伪造不出任意话。"""
    if not isinstance(value, dict) or value.get("act") != ACT or set(value) != {"act", "n", "i"}:
        return "", -1
    nonce, index = value.get("n"), value.get("i")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 32:
        return "", -1
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < MAX_OPTIONS:
        return "", -1
    return nonce, index
