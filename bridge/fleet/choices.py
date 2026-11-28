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

# 一行选项的形状。认这几种编号头（都实测过 Claude / Codex 真会这么写）：
#   数字：`1.` `1、` `1)` `1）` `(1)` `1 、`
#   字母：`A.` `a)` `A、`（大小写）
#   圈号：`①`…`⑩`（Codex / 纪要类回复常用）
#   列表符 + 必带编号/强调：`- 1. x` `* **1.** x`（纯 `- x` 不收，见下）
# 🔴 允许编号前有 `-`/`*`/`•` 列表符和 `**` 强调，但**编号本身必须在**：纯 `- 苹果` 这种无编号
#   列表**不收**，否则任何普通 markdown 列表都会被当成选项（误报）。
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
_OPTION = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:\*\*)?\s*"
    r"(?:\(?(?P<num>\d{1,2})\s*[.、)）]|(?P<alpha>[A-Za-z])[.、)）]|(?P<circ>[" + _CIRCLED + r"]))"
    r"(?:\*\*)?\s+(?P<label>.+?)\s*$"
)
_ASKING = re.compile(
    r"[?？]|请选|选哪|选一|选择|选个|挑一|哪个|哪一|可多选|多选|都要|几个|方案|选项|"
    r"which|choose|pick|prefer|select|option|reply with|回复|回车",
    re.I,
)
# 多选信号：命中 ⇒ 允许选多项（UI 侧据此渲染成可多选 + 一个「就这些」提交）。
_MULTI = re.compile(r"可多选|多选|可以?选多|选几个|都要|都想|选出所有|all that apply|multiple|select all", re.I)


def _clean(label: str) -> str:
    text = re.sub(r"[*_`]+", "", label).strip()
    text = re.split(r"\s+[—–-]\s+|[:：]\s*", text, maxsplit=1)[0].strip() or text
    return text[:LABEL_CHARS]


def extract_choices(text: str) -> list:
    """回复结尾附近一组连续编号选项（2~6 个），且前后有在问的意思 ⇒ 选项文字；否则 []。"""
    lines = str(text or "").rstrip().splitlines()[-TAIL_LINES:]
    best: list = []
    best_start = best_end = 0
    current: list = []
    start = 0
    for index, line in enumerate(lines):
        match = _OPTION.match(line)
        if match:
            if not current:
                start = index
            current.append(_clean(match.group("label")))
            continue
        if current:
            best, best_start, best_end, current = current, start, index, []
    if current:
        best, best_start, best_end = current, start, len(lines)
    if not (2 <= len(best) <= MAX_OPTIONS) or not all(best):
        return []
    context = "\n".join(lines[max(0, best_start - 3):best_start] + lines[best_end:best_end + 3])
    if not _ASKING.search(context):
        return []
    return best


def wants_multi(text: str) -> bool:
    """这组选项是不是「可多选」⇒ True（UI 据此渲染成可多选 + 一个提交）。

    只在**真有选项**（`extract_choices` 非空）时才有意义；判据是整段里出现多选信号词。
    保守：认不出就当单选（单选永远安全，多选多一次点击确认）。
    """
    if not extract_choices(text):
        return False
    return bool(_MULTI.search(str(text or "")))


ACT = "ch"
# 多选里的两种按钮：`t` = 勾/取消某一项（toggle），`g` = 「就这些」提交。
OP_PICK = "p"     # 单选：点一下直接发（老行为，默认）
OP_TOGGLE = "t"   # 多选：勾/取消一项
OP_SUBMIT = "g"   # 多选：提交已勾的


def choice_value(nonce: str, index: int) -> dict:
    """单选按钮的回调值（老形状，保持兼容：点一下直接把那一项发回会话）。"""
    return {"act": ACT, "n": nonce, "i": int(index)}


def multi_value(nonce: str, index: int) -> dict:
    """多选里某一项的「勾/取消」回调值。"""
    return {"act": ACT, "op": OP_TOGGLE, "n": nonce, "i": int(index)}


def submit_value(nonce: str) -> dict:
    """多选的「就这些」提交回调值。"""
    return {"act": ACT, "op": OP_SUBMIT, "n": nonce}


def _valid_nonce(nonce) -> bool:
    return isinstance(nonce, str) and bool(nonce) and len(nonce) <= 32


def _valid_index(index) -> bool:
    return isinstance(index, int) and not isinstance(index, bool) and 0 <= index < MAX_OPTIONS


def resolve_choice_click(value) -> tuple:
    """单选回调 value → (序号, 第几项)；形状不对 ⇒ ("", -1)。

    兼容两种形状：老的 `{act,n,i}`（无 op）和新的单选 `{act,op=p,n,i}`。
    多选的 toggle / submit 走 `resolve_multi_click`，这里一律当不合格。
    选项文字只在服务端，卡上伪造不出任意话。
    """
    if not isinstance(value, dict) or value.get("act") != ACT:
        return "", -1
    keys = set(value)
    if keys == {"act", "n", "i"}:
        pass  # 老形状
    elif keys == {"act", "op", "n", "i"} and value.get("op") == OP_PICK:
        pass  # 新单选形状
    else:
        return "", -1
    nonce, index = value.get("n"), value.get("i")
    if not _valid_nonce(nonce) or not _valid_index(index):
        return "", -1
    return nonce, index


def resolve_multi_click(value) -> tuple:
    """多选回调 value → (op, 序号, 第几项)；不合格 ⇒ ("", "", -1)。

    - toggle：`(OP_TOGGLE, nonce, index)` —— index 合法
    - submit：`(OP_SUBMIT, nonce, -1)` —— 没有 index
    """
    if not isinstance(value, dict) or value.get("act") != ACT:
        return "", "", -1
    op, keys = value.get("op"), set(value)
    if op == OP_TOGGLE and keys == {"act", "op", "n", "i"}:
        nonce, index = value.get("n"), value.get("i")
        if _valid_nonce(nonce) and _valid_index(index):
            return OP_TOGGLE, nonce, index
        return "", "", -1
    if op == OP_SUBMIT and keys == {"act", "op", "n"}:
        nonce = value.get("n")
        if _valid_nonce(nonce):
            return OP_SUBMIT, nonce, -1
        return "", "", -1
    return "", "", -1
