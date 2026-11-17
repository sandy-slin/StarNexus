"""会话总控的飞书卡片 + 按钮回调白名单。

卡片约束照 `bridge/menu.py`（踩过的）：schema 2.0、**没有** action 容器、按钮回调走
`behaviors: [{"type":"callback","value":{...}}]`，整张卡远小于 30KB（这里压到 < 20KB）。

回调只认 `{"act":"fleet","op":"open|send|go|stop|refresh|settle|unsettle","tool":...,"id":...}`、
`{"act":"fleet","op":"snooze","tool":...,"id":...,"q":<16 位十六进制>}`（推送卡上「1 小时后再提醒」）、
`{"act":"fleet","op":"more","tool":...}`（总览上展开一组，不带 id）和
`{"act":"relay","op":"talk|no",...}`（进对话模式的那次确认），
并且 (tool, id) 必须在**最近一次扫描结果**里出现过 —— 伪造的 id 进不来
（`resolve_fleet_click` / `resolve_relay_click`）。
"""

from __future__ import annotations

import json
import os
import time
import unicodedata
from typing import Any, Iterable, Optional

from ..reply import FENCE, card_snippet, escape_card, inline_code
from .actions import button_rows, detail_nav_rows, do_button, jump_callback_button, newsession_value
from .model import TOOLS, Session, short_title
from .scan import TOOL_LABELS, Overview

# `go`：详情卡上「接着聊」**已经跳进群**的那一下（按钮同时带 open_url，见 `build_detail_card` 的 group_link）。
# 和 send 走同一条路，只是单聊里不再补「➡️ 进群说话」。
# `more`：总览上「还有 N 个没列出来」那个按钮（第 28 轮）⇒ 原地换卡、把这一组全列出来。只带 tool，不带 id。
# `settle` / `unsettle`：详情卡上「✓ 结束」和结束后那张卡上的「撤销」（第 33 轮，`fleet/settled.py`）。
# `snooze`：「等你」推送卡上「1 小时后再提醒」（第 33 轮）。只有它可以多带一个 `q`（它在问什么的指纹）。
OPS = ("open", "send", "go", "stop", "refresh", "more", "settle", "unsettle", "snooze")
_SIG_MAX = 16
RELAY_OPS = ("talk", "no")  # 进对话模式那张确认卡：确认 / 算了
MAX_CARD_BYTES = 20 * 1024
# 一张卡最多 200 个组件（飞书官方文档）。「全部列出来」那一组可能有几十行 ⇒ 字节之外还得数组件。
MAX_CARD_COMPONENTS = 200
# 折叠面板标题右边那个小箭头（主会话 2026-11-08 真发过、飞书接受的写法）
PANEL_ICON = {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"}
# 总览一个工具最多列几行。瘦身后一行只剩「状态点 + 名字 + 多久前」（旧的一行还带工具名和
# 项目名），同样的字节能多列几行 ⇒ 6 提到 8。再多手机上就要一直划了，而且更早的会话
# 本来也不是他要找的（底下有「还有 N 个更早的没列出来」）。
MAX_ROWS_PER_TOOL = 8
MSG_PREVIEW_CHARS = 600  # 每条消息最多显示多少字（代码块按原样保留换行，所以比原来的 300 宽一点）
MSG_PREVIEW_LINES = 16  # 再多行手机上一屏就满了
_ID_MAX = 128

STATUS_LABELS = {
    "running": "🟢 在跑",
    "waiting": "🟡 等你",
    "idle": "⚪ 闲着",
    "stopped": "⏸ 挂起",
    "unknown": "❔ 看不出",
}

# ---------- 卡片上显示的状态（第 32 轮，照 T3 手机列表）----------
#
# `Session.status` 是 adapter 读出来的「进程在干什么」；卡片上要的是「要不要我管、管什么」，
# 所以多分几种（`display_status`）。读不到的一律退回原来那几种，**不猜**：
# - 「等你」能读出它在问什么（`pending`）才拆成「等你批准 / 等你回答」，读不出就还是「🟡 等你」
# - 「出错了」只认 adapter 真读到的失败信号（`Session.last_turn == "failed"`）
# - 「跑完了你还没看」要知道上一次给这个人发总览卡是什么时候（`seen_at`）；不知道就不显示
DISPLAY_LABELS = {
    "approve": "🟡 等你批准",
    "ask": "❓ 等你回答",
    "waiting": "🟡 等你",
    "failed": "⚠️ 出错了",
    "running": "🟢 在跑",
    "unseen": "✅ 跑完了，你还没看",
    **{k: v for k, v in STATUS_LABELS.items() if k not in ("running", "waiting")},
}
# 排序：数字小的排前面。等你批准 > 等你回答 > 出错了 > 在跑 > 跑完没看 > 闲着（tasks-r32 第 4 条）。
# 读不出在问什么的「等你」跟「等你回答」并列（都是要他现在就管的）。
DISPLAY_RANK = {"approve": 0, "ask": 1, "waiting": 1, "failed": 2, "running": 3, "unseen": 4,
                "idle": 5, "stopped": 6, "unknown": 7}
# 要他管的（组要展开、标题要点数）
NEEDS_YOU = ("approve", "ask", "waiting", "failed", "running", "unseen")
# 点数时的说法（标题 / 折叠面板 / 「还有 N 个」按钮共用一份，别各写各的）
_COUNT_WORDS = {
    "approve": "个等你批准",
    "ask": "个等你回答",
    "waiting": "个在等你",
    "failed": "个出错了",
    "running": "个在跑",
    "unseen": "个跑完了你还没看",
}
# 「还有 N 个」按钮上一行只有 48 格 ⇒ 用短说法（只这一处用）
_SHORT_WORDS = dict(_COUNT_WORDS, unseen="个跑完没看")


# 「出错了」只标最近这么久之内的那一轮。本机实测 Claude 历史里有几天前测坏 key 留下的
# `authentication_failed` 收尾 —— 那种会话他早不管了，天天挂一个 ⚠️ 等于狼来了。
FAILED_FRESH_SEC = 24 * 3600


def display_status(sess: Session, seen_at: Optional[float] = None, now: Optional[float] = None) -> str:
    """一个会话在卡片上算哪种状态（`DISPLAY_LABELS` 的键）。

    `seen_at`：上一次给**这个人**发总览卡的时间（epoch 秒）；None ⇒ 不知道 ⇒ 不判「跑完没看」。
    """
    status = sess.status
    if status == "waiting":
        pending = getattr(sess, "pending", None)
        if pending is None:
            return "waiting"
        if pending.tool == "AskUserQuestion":
            return "ask"
        return "approve" if pending.tool else "waiting"
    if status != "idle":
        return status if status in DISPLAY_LABELS else "unknown"
    turn = getattr(sess, "last_turn", "")
    turn_at = getattr(sess, "last_turn_at", 0) or 0
    if turn == "failed":
        now = time.time() if now is None else now
        return "failed" if now - turn_at < FAILED_FRESH_SEC else "idle"
    if turn == "done" and seen_at and turn_at > float(seen_at):
        return "unseen"
    return "idle"


def display_label(sess: Session, seen_at: Optional[float] = None) -> str:
    return DISPLAY_LABELS[display_status(sess, seen_at)]


def sort_for_overview(sessions: list, seen_at: Optional[float] = None) -> list:
    """按 `DISPLAY_RANK` 排；同一档里保持原来的先后（扫描已经按活的在前、最近的在前排过）。"""
    return sorted(sessions or (), key=lambda s: DISPLAY_RANK.get(display_status(s, seen_at), 9))


def _count_parts(sessions: list, seen_at: Optional[float], kinds=NEEDS_YOU, bold: bool = False,
                 with_dot: bool = True, words: Optional[dict] = None) -> list:
    """「🟡 2 个等你批准」「⚠️ 1 个出错了」…… 按优先级排，没有的不写。"""
    counts: dict = {}
    for sess in sessions or ():
        kind = display_status(sess, seen_at)
        counts[kind] = counts.get(kind, 0) + 1
    out = []
    for kind in sorted(kinds, key=lambda k: DISPLAY_RANK[k]):
        n = counts.get(kind, 0)
        if not n:
            continue
        text = f"{n} {(words or _COUNT_WORDS)[kind]}"
        if bold:
            text = f"**{text}**"
        if with_dot:
            text = DISPLAY_LABELS[kind].split(" ", 1)[0] + " " + text
        out.append(text)
    return out


class OverviewSeen:
    """「上一次给这个人发总览卡是什么时候」—— 只记在内存（tasks-r32：记不到就不显示，不要猜）。

    桥重启就清空 ⇒ 重启后第一张总览不会有「跑完没看」，第二张起才有。
    `viewer` 用发卡的那个 chat_id（单聊就是这个人）。记的是**那次扫描的时间**（`Overview.at`）：
    扫描之后才结束的那一轮，那张卡上本来就没有它。

    用法（app.py 那边）：
        seen = OVERVIEW_SEEN.before(chat_id, view.at)        # 造卡前：这张卡之前的那一次
        card = build_overview_card(view, ..., seen_at=seen)
        …发出去之后… OVERVIEW_SEEN.mark(chat_id, view.at)
    「看全部」原地换卡用的是同一次扫描（`control.last`）⇒ `before(chat_id, last.at)` 拿到的还是
    造原卡时那个值，换出来的卡上「跑完没看」不会因为刚发过一次就全没了。
    """

    KEEP = 4  # 每个人留最近几次；「看全部」只会回头找上一张

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._marks: dict = {}

    def before(self, viewer: str, at: Any = None) -> Optional[float]:
        """严格早于 `at` 的最近一次发卡时间；`at` 不给 = 最近一次。一次都没记过 ⇒ None。"""
        if not viewer:
            return None
        try:
            limit = float(at) if at else float("inf")
        except (TypeError, ValueError):
            limit = float("inf")
        with self._lock:
            marks = [m for m in self._marks.get(str(viewer), ()) if m < limit]
        return max(marks) if marks else None

    def mark(self, viewer: str, at: Any) -> None:
        try:
            at = float(at or 0)
        except (TypeError, ValueError):
            return
        if not viewer or not at > 0:
            return
        with self._lock:
            marks = sorted(set(self._marks.get(str(viewer), ())) | {at})[-self.KEEP:]
            self._marks[str(viewer)] = marks


OVERVIEW_SEEN = OverviewSeen()
# 桥只给主人一个人发总览（`owner_open_ids`）⇒ 不传 `viewer` 时都记在这一个键上
OWNER_VIEWER = "owner"
_AUTO = object()
SURFACE_LABELS = {"cli": "终端", "app": "App", "bridge": "飞书"}
_HOST_APP_LABELS = {"cursor": "Cursor", "vscode": "VS Code"}


def surface_label(sess) -> str:
    """开在哪儿：「终端」/「App」/「飞书」；第 36 轮细分编辑器里的：「编辑器面板（Cursor）」/「终端（Cursor）」。

    `opened_in` / `host_app` 由 `fleet/claude.py` 写进 extra（只用于显示，判占用/发送不看它）。
    """
    extra = getattr(sess, "extra", None) or {}
    base = "编辑器面板" if extra.get("opened_in") == "ide_panel" else SURFACE_LABELS.get(sess.surface, sess.surface)
    host = _HOST_APP_LABELS.get(extra.get("host_app", ""))
    return f"{base}（{host}）" if host else base
ROLE_LABELS = {"user": "🙋 你", "assistant": "🤖 它"}


def fleet_value(op: str, tool: str = "", sid: str = "") -> dict:
    value = {"act": "fleet", "op": op}
    if tool:
        value["tool"] = tool
    if sid:
        value["id"] = sid
    return value


def resolve_fleet_click(value: Any, known: Iterable[tuple]) -> Optional[dict]:
    """回调 value → {"op","tool","id"}；不认识 / id 不在最近一次扫描里 ⇒ None。

    🔴 白名单思路同 `menu.resolve_click`：value 来自飞书，理论上能伪造。只放预设的 op、
    已知的 tool、在扫描结果里出现过的 id；多余的键一律不认（不许夹带 text 之类）。
    """
    if not isinstance(value, dict) or value.get("act") != "fleet":
        return None
    op = value.get("op")
    allowed = {"act", "op", "tool", "id"} | ({"q"} if op == "snooze" else set())
    if set(value) - allowed:
        return None
    if op not in OPS:
        return None
    sig = value.get("q", "")
    if op == "snooze" and not (isinstance(sig, str) and len(sig) <= _SIG_MAX
                               and all(c in "0123456789abcdef" for c in sig)):
        return None
    if op == "more":
        # 「展开这一组」：只认 act/op/tool 三个键、tool 必须是已知的那几家；带 id 的一律不认
        tool = value.get("tool")
        if set(value) != {"act", "op", "tool"} or not isinstance(tool, str) or tool not in TOOLS:
            return None
        return {"op": "more", "tool": tool, "id": ""}
    if op == "refresh" and "id" not in value and "tool" not in value:
        return {"op": "refresh", "tool": "", "id": ""}
    tool, sid = value.get("tool"), value.get("id")
    if not isinstance(tool, str) or tool not in TOOLS:
        return None
    if not isinstance(sid, str) or not sid or len(sid) > _ID_MAX:
        return None
    if (tool, sid) not in set(known):
        return None
    if op == "snooze":
        return {"op": op, "tool": tool, "id": sid, "q": sig}
    return {"op": op, "tool": tool, "id": sid}


def relay_value(op: str, tool: str = "", sid: str = "") -> dict:
    """进对话模式那张确认卡上的按钮。独立的 `act` ⇒ 不会和 `fleet` 的 op 混。"""
    return {"act": "relay", "op": op, "tool": tool, "id": sid}


def resolve_relay_click(value: Any, known: Iterable[tuple]) -> Optional[dict]:
    """确认卡回调 → {"op","tool","id"}；不认识 ⇒ None。

    🔴 白名单同 `resolve_fleet_click`：value 来自飞书、理论上能伪造，而这张卡点一下的
    后果是「让电脑上那个会话不问就动手」。所以 op 只认预设两个、id 必须在最近一次扫描里。
    """
    if not isinstance(value, dict) or value.get("act") != "relay":
        return None
    if set(value) - {"act", "op", "tool", "id"}:
        return None
    op = value.get("op")
    if op not in RELAY_OPS:
        return None
    tool, sid = value.get("tool"), value.get("id")
    if not isinstance(tool, str) or tool not in TOOLS:
        return None
    if not isinstance(sid, str) or not sid or len(sid) > _ID_MAX:
        return None
    if (tool, sid) not in set(known):
        return None
    return {"op": op, "tool": tool, "id": sid}


def _button(label: str, value: dict, primary: bool = False) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _tap_row(text_md: str, value: dict, urgent: bool = False) -> dict:
    """总览里左对齐的一行（第 36 轮 11-16 主人「列表都向左靠起」）：交互容器 + 一段 markdown。

    按钮上的字飞书固定居中 ⇒ 换成 `interactive_container`（字段照官方文档
    https://open.feishu.cn/document/feishu-cards/card-json-v2-components/containers/interactive-container ）。
    点它回调的 `action.value` 就是这里的 `value`（同按钮，见卡片回传交互文档），`resolve_fleet_click` 不用改。
    `text_md` 必须已经转义过（`escape_card`）—— 它是 markdown，不是按钮那种纯文字。
    `urgent`（等你批准 / 等你回答）⇒ 蓝边 + 浅蓝底，顶替原来的主按钮。
    """
    node = {
        "tag": "interactive_container",
        "width": "fill",
        "horizontal_align": "left",
        "has_border": True,
        "border_color": "blue" if urgent else "grey",
        "corner_radius": "8px",
        "padding": "6px 12px 6px 12px",
        "behaviors": [{"type": "callback", "value": value}],
        "elements": [_md(text_md)],
    }
    if urgent:
        node["background_style"] = "blue-50"
    return node


def _row(buttons: list) -> dict:
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]} for b in buttons
        ],
    }


def _md(text: str, small: bool = False) -> dict:
    node = {"tag": "markdown", "content": text}
    if small:
        node["text_size"] = "notation"
    return node


def _escape(text: str) -> str:
    """卡片 markdown 里别让会话标题 / 消息变成格式、标签或链接。

    🔴 用 HTML 实体（`&#60;`），不用反斜杠：飞书卡片文档（JSON 2.0 富文本组件，2026-11-05 查）
    给的转义办法只有实体这一种，反斜杠转义没写支持 —— 显示出来可能就是 `\\*`。
    """
    return escape_card(text)


def message_md(who: str, text: str, chars: int = MSG_PREVIEW_CHARS, lines: int = MSG_PREVIEW_LINES) -> str:
    """详情卡里的一条消息：代码块保留成代码块，其余文字转义（粗体和行内代码照常显示）。

    🔴 `text` 必须是**已经整段脱敏过**的（见 `build_detail_card` 的红字）。这里排版 → 截断 → 转义，
    截在代码块中间也会补上收尾的 ```（不然后面的消息和按钮整张卡都被染成代码）。
    原来用 `short_title` 把换行压成一行，代码全糊成一坨。
    """
    body = card_snippet(text, chars, lines) if text.strip() else "（空）"
    # 代码块的 ``` 必须在行首（卡片文档原话）⇒ 一开头就是代码块时「谁：」单独一行
    sep = "\n" if body.startswith(FENCE) else ""
    return f"{who}：{sep}{body}"


def project_of(cwd: str) -> str:
    if not cwd:
        return "不知道哪个项目"
    return os.path.basename(cwd.rstrip("/\\")) or cwd


# ---------- 一行能放多少字 ----------

# 手机竖屏一行看得见的宽度，按**半角格**算：一个汉字 2 格 ⇒ 48 格 = 24 个汉字。
# 用户原话「文字太多 手机上显示不全」—— 原来一行是「工具 · 项目 · 状态 · 多久前｜标题」，
# 实测 40 格开头、带长标题的到 60 格，后半截在手机上直接看不见。
# 🔴 这个数是**可见宽度**不是字符数：`slot-allocation-aimd` 20 个字母只占 20 格，
# 原来按字符数截（18 个）把它截成了 `slot-allocation-a…`，白白浪费半行。
OVERVIEW_ROW_CELLS = 48
# 名字最少留这么多格（5 个汉字）：项目名 + 多久前再长也不能把名字挤没了
NAME_FLOOR_CELLS = 10
# 名字**先拿够**这么多格（10 个汉字），项目名只分剩下的。
#
# 🔴 2026-11-05 体验审查：原来项目名先拿满 14 格，名字拿剩下的 ⇒ 不在主项目的那几行，
# 名字被剪到 7 个汉字（实测 `⚪ 生成一个视频：… · oneline-video · 2 分钟前`）。
# 用户是按**名字**找会话的 —— 工具名和项目已经在分组标题上说过一遍了，
# 行里的项目名只是「这行跟标题那个项目不一样」的提示，不该比名字先吃饱。
NAME_TARGET_CELLS = 20
SEP = " · "  # 行里各段之间的分隔。宽度由 `display_width` 现算，别写死数字（见下面红字）
# 文字兜底那条路（卡片发不出去时的纯文字消息）一行的预算。比卡片宽：**纯文字消息会自己折行**，
# 超出屏宽只是换一行，不像按钮那样后半截直接看不见。所以那边不必为了一屏把名字削掉。
FALLBACK_ROW_CELLS = 72
# 行里补的项目名（和分组标题不是同一个项目时）最多占几格
ROW_PROJECT_CELLS = 14
# 行里「不知道是哪个项目」的短说法。
# 🔴 整句「不知道哪个项目」占 14 格 —— 在一行只有 48 格、名字要先拿够的时候，
# 它会被剪成「不知道…」（说了等于没说，还白占 6 格）。行里的项目名只是个提示 ⇒ 用短的。
# 详情卡和文字兜底那条路不受影响，那边地方够，照旧用整句。
ROW_NO_PROJECT = "项目不明"
HEADING_PROJECT_CELLS = 24  # 分组标题里的项目名最多占几格
# 名字撞车时补的短 id 长度。uuid 前 4 位撞车的概率可以忽略，撞了两行拿到同一个尾巴，
# 也比两行一模一样强。
DUP_ID_CHARS = 4
# 每条「哪家读不到」说明最多显示多少字。🔴 `_fit` 只会少放会话行、不会剪说明 ⇒ 一条超长的
# 报错原文（比如 adapter 把整段 stderr 塞进 last_error）能让卡片永远超 20KB、只剩 1 行也下不来。
NOTE_CHARS = 160


def _cluster_width(cluster: str) -> int:
    """一个「看起来是一个字」的东西占几格。"""
    base = cluster[0]
    # emoji / 符号（🟢 ⏸ ❔ 国旗的区域指示符都在这里）：手机上都当两格算
    if unicodedata.category(base) in ("So", "Sk"):
        return 2
    return 2 if unicodedata.east_asian_width(base) in ("W", "F", "A") else 1


def _graphemes(text: str) -> list:
    """按「一个字」切开，**不切到 emoji 中间**。

    🔴 会话名是用户项目里的文字，里面可能有 emoji。`👨‍👩‍👧‍👦` 是 7 个码位拼出来的一个字，
    按码位截会留下半截 ZWJ ⇒ 手机上显示成一串碎人头，或者干脆是方块。
    这里把「组合符号 / 变体选择符 / 肤色 / ZWJ 连起来的」都粘回前一个字。
    """
    out: list = []
    join_next = False
    for ch in text:
        code = ord(ch)
        glue = join_next or (
            code == 0x200D  # ZWJ 自己也粘前面（它是 Cf 不是 Mn，漏了实测一家四口算成 5 格、还能被剪开）
            or unicodedata.category(ch) in ("Mn", "Mc", "Me")  # 组合符号、藏文元音之类
            or 0xFE00 <= code <= 0xFE0F  # 变体选择符（VS16 = 让符号按 emoji 显示）
            or 0xE0100 <= code <= 0xE01EF
            or 0xE0020 <= code <= 0xE007F  # 旗帜的 tag 序列（🏴󠁧󠁢󠁳󠁣󠁴󠁿）
            or 0x1F3FB <= code <= 0x1F3FF  # 肤色
            or code == 0x20E3  # 键帽（1️⃣）
            or (out and 0x1F1E6 <= code <= 0x1F1FF and len(out[-1]) == 1
                and 0x1F1E6 <= ord(out[-1]) <= 0x1F1FF)  # 国旗 = 两个区域指示符
        )
        join_next = ch == "‍"  # ZWJ：它自己粘前面，后面那个也粘上来
        if glue and out:
            out[-1] += ch
        else:
            out.append(ch)
    return out


def display_width(text: Any) -> int:
    """一段文字在手机上占几格（汉字 / emoji 算 2，字母数字算 1）。"""
    return sum(_cluster_width(g) for g in _graphemes(str(text or "")))


def clip_cells(text: Any, cells: int) -> str:
    """压成一行、按**可见宽度**截断、超了加「…」。`cells` 是半角格数。"""
    flat = " ".join(str(text or "").split())
    if cells <= 0:
        return ""
    if display_width(flat) <= cells:
        return flat
    budget = cells - 2  # 「…」自己占两格
    out: list = []
    used = 0
    for g in _graphemes(flat):
        wide = _cluster_width(g)
        if used + wide > budget:
            break
        out.append(g)
        used += wide
    return "".join(out).rstrip() + "…"


# ---------- 总览的行和分组标题 ----------


def status_dot(sess: Session, seen_at: Optional[float] = None) -> str:
    """状态只留那个点（「在跑 / 闲着」那两个字挪到卡片底下的图例里，一行省 6 格）。"""
    return display_label(sess, seen_at).split(" ", 1)[0]


def session_name(sess: Session, cells: int = OVERVIEW_ROW_CELLS) -> str:
    """会话名：没标题的用短 id 兜着，绝不返回空串（空按钮点不着也看不懂）。

    `sess.title` 必须是**已经整段脱敏过**的（调用方先过 `_cleaned`，见 `build_detail_card` 的红字）。
    """
    name = clip_cells(sess.title, cells)
    if name:
        return name
    sid = str(sess.id or "")
    return sid[:8] or "没名字的会话"


def _row_parts(sess: Session, shared_project: str, dup_tag: str,
               cells: int = OVERVIEW_ROW_CELLS, seen_at: Optional[float] = None) -> tuple:
    """(状态点, 名字, 后面那几段)。只给卡片那种样子用。

    格子怎么分（🔴 **名字优先**，2026-11-05 体验审查改的）：
    1. 状态点、`#短 id`、多久前 这几样先占位 —— 它们都短且固定，而且少了会分不清哪行是哪行
    2. 名字拿到 `NAME_TARGET_CELLS` 为止（够放 10 个汉字）
    3. 剩下的才归项目名；不够放 `不知道哪个项目` 那么宽也没关系，它只是个提示
    4. 项目名剪完还有富余 ⇒ 全给名字（短项目名的行名字能更长）
    """
    head = status_dot(sess, seen_at)
    project = project_of(sess.cwd)
    show_project = bool(project and project != shared_project)
    if show_project and not sess.cwd:
        project = ROW_NO_PROJECT  # 见 ROW_NO_PROJECT 的红字
    # 短且必须显示的那几段
    fixed: list = []
    if dup_tag:
        fixed.append("#" + dup_tag)
    if sess.age_text:
        fixed.append(sess.age_text)
    # 🔴 分隔符的宽度要**现算**：`·` 是 East-Asian-Ambiguous，按两格算 ⇒ ` · ` 是 4 格不是 3。
    # 写死 3 的话每段都少留一格，整行算下来没超、拼出来超了 ⇒ `clip_cells` 再剪一刀，
    # 名字尾巴被砍成 `198 的 litel…`（实测）。
    sep = display_width(SEP)
    used = display_width(head) + 1 + sum(display_width(t) + sep for t in fixed)
    room = cells - used
    tail: list = []
    if show_project:
        # 名字先拿够（但不超过它真的需要的宽度），项目名分剩下的
        want = min(NAME_TARGET_CELLS, display_width(sess.title or "") or NAME_TARGET_CELLS)
        for_project = max(0, room - max(NAME_FLOOR_CELLS, want) - sep)
        shown = clip_cells(project, min(ROW_PROJECT_CELLS, for_project))
        if shown:
            tail.append(shown)
            room -= display_width(shown) + sep
    name = session_name(sess, max(NAME_FLOOR_CELLS, room))
    return head, name, tail + fixed


def _standalone_label(sess: Session, cells: int, seen_at: Optional[float] = None) -> str:
    """一行自己说清楚：「工具 · 项目 · 状态 · 多久前｜标题」（文字兜底用，没有分组标题和图例兜着）。

    🔴 状态写成字（「闲着」）不写成点：纯文字消息底下没有图例，光一个 `⚪` 认不出来。
    """
    bits = [TOOL_LABELS.get(sess.tool, sess.tool), project_of(sess.cwd),
            display_label(sess, seen_at).split(" ", 1)[-1]]
    if sess.age_text:
        bits.append(sess.age_text)
    label = SEP.join(bits)
    room = max(NAME_FLOOR_CELLS, cells - display_width(label) - 2)  # 2 = 「｜」
    label += "｜" + session_name(sess, room)
    return clip_cells(label, cells)


def row_label(sess: Session, *, shared_project: str = "", with_tool: bool = True,
              dup_tag: str = "", cells: int = 0, seen_at: Optional[float] = None) -> str:
    """总览里的一行。

    `with_tool=False` + `shared_project`：工具名和项目已经在分组标题上了 ⇒ 行里只留
    **不一样的那几样**：状态点 + 会话名 + 多久前，例 `🟢 修登录 · 3 分钟前`。
    这个会话的项目和标题上那个不一样时，才在名字后面补一个项目名。

    不传这两个参数就是「一行自己说清楚」的样子（带工具名和项目），给卡片发不出去时的
    文字兜底用 —— 那边没有分组标题兜着，只剩一个点和一个名字就看不出是哪个工具的了。

    `dup_tag`：同一组里名字撞车时补的短 id。
    `cells`：这一行最多几格；不传按上面两种样子各自的默认（卡片 48，文字兜底 72）。
    """
    if with_tool:
        return _standalone_label(sess, cells or FALLBACK_ROW_CELLS, seen_at)
    cells = cells or OVERVIEW_ROW_CELLS
    head, name, tail = _row_parts(sess, shared_project, dup_tag, cells, seen_at)
    label = SEP.join([f"{head} {name}"] + tail)
    # 兜底再剪一刀：名字有下限 ⇒ 项目名 / 多久前特别长时整行可能超，超了宁可丢尾巴
    return clip_cells(label, cells)


def row_names(sessions: list, shared_project: str = "", seen_at: Optional[float] = None) -> list:
    """这几行**真正会显示出来**的名字（已按行宽截断）。给撞车检测用。

    🔴 必须用截断后的名字去比：`proxy-ip-binding-admin` 和 `proxy-ip-binding-dev`
    原文不一样，截到一行里都是 `proxy-ip-binding-…`，比原文就看不出撞车。
    比的是**没补短 id 时**的名字：补了 id 名字会再短一点，但每行尾巴已经不一样了。
    """
    return [_row_parts(s, shared_project, "", seen_at=seen_at)[1] for s in sessions or ()]


def dup_tags(sessions: list, names: list) -> list:
    """同一组里显示出来一样的名字 ⇒ 每个都补一截短 id。

    实测 Cursor 里有三个会话都叫 `Chick eating rice video`，三行长得一模一样；
    加上长标题被截断之后撞车的，不补 id 根本点不准要哪个。
    """
    seen: dict = {}
    for name in names:
        seen[name] = seen.get(name, 0) + 1
    return [str(s.id or "")[:DUP_ID_CHARS] if seen.get(n, 0) > 1 else ""
            for s, n in zip(sessions or (), names or ())]


def main_project(sessions: list) -> str:
    """列出来的这几行里最多的那个项目。标题和行里**必须用同一份答案** ⇒ 只有这一个实现。"""
    projects = [project_of(s.cwd) for s in sessions or ()]
    if not projects:
        return ""
    # 并列最多时取先出现的那个（`set` 的顺序不稳，不能直接拿它决定显示什么）
    return max(sorted(set(projects), key=projects.index), key=projects.count)


def group_heading(tool: str, items: list, shown: list, clean=None, brand: str = "") -> str:
    """分组标题：把一组里**一样的那几样**（工具名、项目）提到这里来。

    例 `🤖 **Claude** · carher-admin（6 个）`。一个工具跨几个项目时写成
    `carher-admin 等 3 个项目` —— 不再按项目切一层：切完「在跑的排最前」就被打散了，
    而标题行还会多出好几行。跟标题不是同一个项目的行，自己带项目名（见 `row_label`）。

    🔴 「等 N 个项目」只数**列出来的那几行**（2026-11-05 体验审查）：原来数的是 `items`
    全部，于是 8 行全在 carher-admin、更早的 6 个在别处时，标题写「等 7 个项目」而
    屏幕上一个别的项目都看不见。这句话的作用是解释「为什么有的行后面带项目名」——
    数没显示的行等于凭空制造一个找不到出处的数字。没列出来的那些有「还有 N 个更早的」兜着。
    """
    clean = clean or (lambda s: s)
    label = TOOL_LABELS.get(tool, tool)
    rows = shown or items
    main = main_project(rows)
    # 第 36 轮：有这一家的品牌图标（`brand` = img_key，画在面板标题的 icon 上，见 `_panel`）⇒ 不再写 🤖
    bits = [f"**{escape_card(label)}**" if brand else f"🤖 **{escape_card(label)}**"]
    text = clip_cells(_safe_clean(clean, main), HEADING_PROJECT_CELLS) if main else ""
    if text:  # 脱敏失败 ⇒ 空串 ⇒ 不写项目，别留一个「 · （1 个）」
        kinds = len({project_of(s.cwd) for s in rows})
        if kinds > 1:
            text += f" 等 {kinds} 个项目"
        # 先剪再转义：反过来会把 `&#60;` 这种实体剪成半截
        bits.append(escape_card(text))
    return SEP.join(bits) + f"（{len(items)} 个）"


def _live(sess: Session) -> bool:
    return bool(sess.pids) or sess.status in ("running", "waiting")


# 标题里加粗的那几种：要他**现在**就动手的
_URGENT = ("approve", "ask", "waiting")


def overview_headline(sessions: list, seen_at: Optional[float] = None) -> str:
    """总览第一句。🔴 先说「几个在等你」—— 那是主人掏出手机最想知道的。

    原来是「电脑上现在开着 **19** 个会话」：分不出几个在干活、几个卡住、几个在等他点头
    （2026-11-06 UX 侦察）。手机上几秒钟扫一眼，要的是**要不要我管**。

    第 32 轮：按 `DISPLAY_RANK` 点数（等你批准 > 等你回答 > 出错了 > 在跑 > 跑完没看），
    要他现在动手的那几种加粗。「出错了」「跑完没看」可能是没开着的历史会话，照样点数 ——
    那正是他要知道的。剩下开着但闲着的写「N 个开着闲着」；其余闲着的不进标题（噪音）。
    """
    sessions = list(sessions or ())
    kinds = [display_status(s, seen_at) for s in sessions]
    urgent = _count_parts([s for s, k in zip(sessions, kinds) if k in _URGENT], seen_at, bold=True)
    rest_parts = _count_parts([s for s, k in zip(sessions, kinds) if k not in _URGENT], seen_at)
    idle_live = sum(1 for s, k in zip(sessions, kinds) if k not in NEEDS_YOU and _live(s))
    parts = urgent + rest_parts
    if idle_live:
        parts.append(f"{idle_live} 个开着闲着")
    if not parts:
        return "电脑上现在没有开着的会话。"
    return "，".join(parts) + "。"


def overview_fallback_lines(sessions: list, clean=None, seen_at: Optional[float] = None,
                            limit: int = 12) -> list:
    """卡片发不出去时那条纯文字消息的行（给 app.py 的 `_fleet_overview` 用）。

    和卡片同一套：按 `DISPLAY_RANK` 排、状态**写成字**（`row_label` 的独立样子，底下没有图例）、
    等你的紧跟一句它在问什么（`overview_pending_line`）。标题先整段脱敏再截断（`_cleaned`）。
    """
    clean = clean or (lambda s: s)
    lines: list = []
    for sess in sort_for_overview(sessions, seen_at)[:limit]:
        cleaned = _cleaned(sess, clean)
        lines.append(row_label(cleaned, seen_at=seen_at))
        asking = overview_pending_line(cleaned, clean)
        if asking:
            lines.append("  " + asking)
    return lines


def _hidden_extra(hidden: list, seen_at: Optional[float], short: bool = False) -> list:
    """藏起来的那几个里要他管的：「2 个等你批准」「1 个出错了」…（不带点，按优先级）。"""
    return _count_parts(hidden, seen_at, with_dot=False, words=_SHORT_WORDS if short else None)


def hidden_rows_note(hidden: list, seen_at: Optional[float] = None) -> str:
    """「还有 N 个没列出来」—— 🔴 要照实说里面有什么，别一律叫「更早的」。

    原来写死「还有 11 个更早的没列出来」，实测那 11 个里有 2 个在跑、2 个在等你
    （2026-11-06 UX 侦察）。现在「等你」已经排到最前，一般不会被藏；但组里「等你」多于
    一屏能列的行数时，照样可能有藏着的 —— 那时这一句必须点名。
    """
    extra = _hidden_extra(hidden, seen_at)
    if extra:
        return f"⚠️ 还有 {len(hidden)} 个没列出来（其中 {'，'.join(extra)}），在电脑上看"
    return f"还有 {len(hidden)} 个更早的没列出来"


def status_legend(sessions: list, seen_at: Optional[float] = None) -> str:
    """卡片底下那行图例：只写**这张卡上出现过的**状态，没出现的不占地方；按优先级排。"""
    kinds = {display_status(s, seen_at) for s in sessions or ()}
    return SEP.join(DISPLAY_LABELS[k] for k in sorted(kinds, key=lambda k: DISPLAY_RANK.get(k, 9)))


def _safe_clean(clean, text: str) -> str:
    """脱敏失败不能让整张卡出不来（同 `_cleaned` 的兜底思路）。"""
    try:
        return clean(text or "")
    except Exception:  # noqa: BLE001
        return ""


def session_label(sess_or_target: Any, clean=None) -> str:
    """「Claude「修登录」」这种一眼能认的名字。给指示器和确认卡用。

    会话标题是**用户项目里的文字**（第一句话 / AI 起的标题），所以要过脱敏再截断
    （顺序见 `build_detail_card` 的红字）。
    """
    clean = clean or (lambda s: s)
    tool = getattr(sess_or_target, "tool", "")
    title = getattr(sess_or_target, "title", "") or ""
    sid = str(getattr(sess_or_target, "id", "") or "")
    name = short_title(clean(title), 24) if title else sid[:8]
    return f"{TOOL_LABELS.get(tool, tool)}「{name or sid[:8]}」"


# 对话模式的指示器。spec：对话模式中卡片/消息要**一直显示**「正在和 X 说话，退出发 /cancel」。
# 一句话里要同时有「在和谁」和「怎么退出」—— 少了后者，用户想停下时只能猜。
TALKING_PREFIX = "🗣 正在和"
# 🔴 11-05 用户原话「不要让用户输入 /cancel」⇒ 指示器里不再叫人打字，退出口是下面那排
# 「🚪 退出对话」按钮（bridge/fleet/actions.py）；`/cancel`、「取消」照样能用（兜底）。
TALKING_SUFFIX = "说话，你发的每一句都进它那边；要退出就点「退出对话」。"


def talking_banner(sess_or_target: Any, clean=None) -> str:
    return f"{TALKING_PREFIX} **{_escape(session_label(sess_or_target, clean))}** {TALKING_SUFFIX}"


def build_confirm_relay_card(sess_or_target: Any, redact=None) -> str:
    """进对话模式前的那**一次**确认（样式仿 `menu.build_confirm_loosen_card`）。

    🔴 为什么非要问一次：对话模式里用户打的每句话都由**电脑上那个会话**去做，用的是
    他在电脑上给它的那套权限（实测 21 个终端会话全是「跳过确认」）。手机这边的 `/mode`
    只读、审批卡片**一条都管不到它**。这和桥自己跑一轮是两件事，而误点的后果是
    「它不问你就动手」且**用户不会收到任何提示** —— 和放开权限那张卡同一个道理。

    确认过的会话 24 小时内不再问（`consent.RelayConsent`），不然连续多轮每句弹一张卡。
    """
    clean = redact or (lambda s: s)
    label = _escape(session_label(sess_or_target, clean))
    tool = getattr(sess_or_target, "tool", "")
    sid = str(getattr(sess_or_target, "id", "") or "")
    where = "电脑终端里" if TOOL_LABELS.get(tool) == "Claude" else "电脑上"
    elements = [
        _md(
            f"接下来你说的每一句都**直接进{where}那个会话**（{label}），由它去做。\n\n"
            "· 它的权限就是你在电脑上给它的那套 —— 多半是**不问你就改**\n"
            "· 手机这边的「只读」和确认卡片**管不到它**\n"
            "· 它干完会把文字回复转回这里；要停它手里那一轮，只能到电脑上按 Esc"
        ),
        _row([
            _button("确认，开始说话", relay_value("talk", tool, sid), primary=True),
            _button("算了", relay_value("no", tool, sid)),
        ]),
        _md("确认一次，这个会话 24 小时内不再问。进去之后每条回复下面都有「退出对话」。", small=True),
    ]
    return _card("⚠️ 直接跟电脑上那个会话说话", "orange", elements)


def _card(title: str, template: str, elements: list) -> str:
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )


# 不算组件的那几种 `tag`：按钮 / 标题里的文字对象、图标 —— 它们是组件的属性，不是组件
_NOT_COMPONENTS = {"plain_text", "lark_md", "standard_icon", "custom_icon"}


def _components(node: Any) -> int:
    """一张卡里有几个组件（带 `tag` 的 dict，文字对象和图标不算；面板标题那个 markdown 算上，宁多勿少）。"""
    if isinstance(node, dict):
        mine = 1 if "tag" in node and node.get("tag") not in _NOT_COMPONENTS else 0
        return mine + sum(_components(v) for v in node.values())
    if isinstance(node, list):
        return sum(_components(v) for v in node)
    return 0


def _fits(blob: str) -> bool:
    """放得下 = 不超 20KB，而且组件数不超飞书的上限（「全部列出来」那一组几十行时才会碰到）。"""
    if len(blob.encode("utf-8")) > MAX_CARD_BYTES:
        return False
    try:
        return _components(json.loads(blob)) <= MAX_CARD_COMPONENTS
    except ValueError:
        return False


def _fit(build, rows: int) -> str:
    """超了 20KB（或组件太多）就少放几行，直到放得下。"""
    blob = build(rows)
    while not _fits(blob) and rows > 1:
        rows -= 1
        blob = build(rows)
    return blob


def _cleaned(sess: Session, clean) -> Session:
    """标题先整段脱敏，再交给截断逻辑。Session 是 frozen 的 ⇒ 复制一份。"""
    import dataclasses

    try:
        return dataclasses.replace(sess, title=clean(sess.title or ""))
    except Exception:  # noqa: BLE001 —— 脱敏失败宁可不显示标题
        return dataclasses.replace(sess, title="")


def _busy(sess: Session, seen_at: Optional[float] = None) -> bool:
    """这一组要不要默认展开：等你批准 / 等你回答 / 出错了 / 在跑 / 跑完没看 ⇒ 展开；
    闲着 / 挂起 / 看不出 ⇒ 收起（第 28 轮用户原话；第 32 轮加了后三种）。"""
    return display_status(sess, seen_at) in NEEDS_YOU


_ALL_SAME = {"idle": "都闲着", "stopped": "都挂起了", "unknown": "都看不出在干什么"}


def group_summary(items: list, seen_at: Optional[float] = None) -> str:
    """折叠面板标题末尾那一截：收起时一眼看出这组要不要管。

    有要他管的 ⇒ 照实点数（按 `DISPLAY_RANK`，同 `overview_headline`）；
    一个都没有 ⇒「都闲着」「都挂起了」，混着几种 ⇒「没有在跑的」。数的是**整组**，不只是列出来的那几行。
    """
    parts = _count_parts(items, seen_at)
    if parts:
        return "，".join(parts)
    kinds = {display_status(s, seen_at) for s in items}
    if len(kinds) == 1:
        return _ALL_SAME.get(next(iter(kinds)), "没有在跑的")
    return "没有在跑的"


def panel_title(tool: str, items: list, shown: list, clean=None, seen_at: Optional[float] = None,
                brand: str = "") -> str:
    """例「🤖 **Claude** · carher-admin（6 个）· 🟢 1 个在跑」。前半截就是原来的 `group_heading`。"""
    return f"{group_heading(tool, items, shown, clean, brand)}· {group_summary(items, seen_at)}"


def more_button_label(hidden: list, seen_at: Optional[float] = None) -> str:
    """「还有 N 个没列出来」那个按钮（第 28 轮：原来是一句死字，点了没用）。藏着要他管的照样点名。"""
    extra = _hidden_extra(hidden, seen_at, short=True)
    # 🔴 按钮上的字手机竖屏一行放得下（≤ 48 格）：藏着要他管的那种写短一点（「其中」「没列出来」省掉），
    # 还超就从优先级低的那头一截截丢 —— 「点开看全部」不能被剪掉，那是这个按钮是干什么的。
    if extra:
        for n in range(len(extra), 0, -1):
            bits = extra[:n]
            label = f"⚠️ 还有 {len(hidden)} 个（{'，'.join(bits)}），点开看全部"
            if display_width(label) <= OVERVIEW_ROW_CELLS:
                return label
        return f"⚠️ 还有 {len(hidden)} 个，点开看全部"
    return f"还有 {len(hidden)} 个更早的没列出来，点开看全部"


def overflow_note(hidden: list, seen_at: Optional[float] = None) -> str:
    """点了「看全部」之后**还是**放不下的那几个（卡片 20KB / 200 个组件）。这里只能叫他回电脑上看。"""
    extra = _hidden_extra(hidden, seen_at)
    what = f"（其中 {'，'.join(extra)}）" if extra else ""
    return f"{'⚠️ ' if extra else ''}还有 {len(hidden)} 个这张卡放不下{what}，在电脑上看"


def _panel(title: str, expanded: bool, elements: list, brand: str = "") -> dict:
    """折叠面板：客户端本地展开 / 收起，不走回调（字段照主会话 2026-11-08 真发通过的那张）。

    `brand`（第 36 轮）= 这一家品牌图标的 img_key ⇒ 放在**标题 markdown 自己的 `icon`**（前缀图标，
    `custom_icon`）。header 的 `icon` 已经是右边的展开箭头，一个 header 只有一个 icon 位。
    2026-11-16 真发：这种写法飞书接受（code=0）；未知字段会被 200621 拒，所以它是认识的字段。
    """
    head = {"tag": "markdown", "content": title}
    if brand:
        head["icon"] = {"tag": "custom_icon", "img_key": brand}
    return {
        "tag": "collapsible_panel",
        "expanded": bool(expanded),
        "header": {
            "title": head,
            "width": "fill",
            "vertical_align": "center",
            "icon": dict(PANEL_ICON),
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "vertical_spacing": "8px",
        "elements": elements,
    }


def _brand_key(tool: str) -> str:
    """这一家的品牌图标 img_key（`fleet/brand_icons.py`）；没有 / 出任何错 ⇒ ""，标题照旧写 🤖。"""
    try:
        from .brand_icons import key_for

        return key_for(tool)
    except Exception:  # noqa: BLE001 —— 图标是锦上添花，总览绝不能因为它画不出来
        return ""


def _startable_tools() -> list:
    """本机装了的那几家（`newsession.available_tools`）。出任何错 ⇒ []：总览不能因为它出不来。"""
    try:
        from .newsession import available_tools

        return list(available_tools())
    except Exception:  # noqa: BLE001
        return []


def newsession_row_label(tool: str) -> str:
    return f"➕ 新开一个 {NEWSESSION_TOOL_NAMES.get(tool, tool)}"


def _newsession_row(tool: str) -> dict:
    """面板第一行「➕ 新开一个 Claude Code」：点了发一张**工具已定**的新会话卡（`begin` 带 `t`）。"""
    return _tap_row(escape_card(newsession_row_label(tool)), newsession_value("begin", tool=tool))


def build_overview_card(view: Overview, redact=None, expand: str = "",
                        seen_at: Any = _AUTO, viewer: str = OWNER_VIEWER, settled: Any = _AUTO) -> str:
    """总览：按工具分组，一行一个会话（点一行进详情），最后是读失败的说明 + 刷新。

    用户原话「文字太多 手机上显示不全」⇒ 一组里**一样的那几样**（工具名、项目）在
    分组标题上说一次，行里只留状态点 + 会话名 + 多久前（`row_label` / `group_heading`）。

    第 28 轮：每组是一个**折叠面板**。有在跑 / 等你的 ⇒ 默认展开；全闲着 ⇒ 收起，标题上写清
    「（5 个）· 都闲着」。每组最多列 `MAX_ROWS_PER_TOOL` 行，多出来的是一个「点开看全部」按钮
    （op=more）⇒ 回调原地换成 `expand=<那个工具>` 的这张卡：那一组展开、全列出来，别的组照旧。
    全列出来超 20KB / 200 个组件时少放几行，最后一句写清「还有 N 个放不下，在电脑上看」。

    第 32 轮：状态按 `display_status` 分得更细（等你批准 / 等你回答 / 出错了 / 跑完没看），
    每组按 `DISPLAY_RANK` 重排。`seen_at` = 上一次给这个人发总览卡的时间（`OVERVIEW_SEEN.before`）；
    不传 ⇒ 自己查 `OVERVIEW_SEEN`（`viewer` 名下、早于这次扫描的那一次），造完普通卡就把这次记上；
    「看全部」（`expand`）用的是同一次扫描，只查不记。显式传 None ⇒ 不判「跑完没看」，也不记。

    🔴 记的是「造好了这张卡」不是「飞书说发到了」：卡片发不出去时 app.py 会退成文字，
    那条文字里同样是这些会话，他照样看到了。

    第 33 轮：主人点过「✓ 结束」的会话不列（`settled`，默认 `fleet.settled.active()`；显式 None ⇒ 不藏）。
    有新动静的自己回来（`SettledStore.split` 顺手删掉它的记录）。底部写一句「已结束 N 个」。
    """
    record = seen_at is _AUTO and not expand
    if seen_at is _AUTO:
        seen_at = OVERVIEW_SEEN.before(viewer, getattr(view, "at", 0) or None)
    view, gone = _drop_settled(view, settled)
    try:
        return _build_overview_card(view, redact, expand, seen_at, gone)
    finally:
        if record:
            OVERVIEW_SEEN.mark(viewer, getattr(view, "at", 0))


SETTLED_NOTE = "✓ 已结束 {n} 个（有新动静会自己回来）"


def _drop_settled(view: Overview, settled: Any) -> tuple:
    """(去掉已结束的那份 Overview, 藏了几个)。记录读不出 / 出任何错 ⇒ 原样、0 —— 总览绝不能因为它出不来。"""
    import dataclasses

    if settled is None:
        return view, 0
    try:
        if settled is _AUTO:
            from .settled import active

            settled = active()
        shown, gone = settled.split(list(view.sessions or ()))
        if not gone:
            return view, 0
        return dataclasses.replace(view, sessions=shown), gone
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("feishu-bridge.fleet").exception("读「已结束」记录失败，这次全列出来")
        return view, 0


def _build_overview_card(view: Overview, redact, expand: str, seen_at: Optional[float],
                         gone: int = 0) -> str:
    clean = redact or (lambda s: s)
    groups = {tool: sort_for_overview(items, seen_at) for tool, items in view.by_tool().items()}
    expand = expand if expand in TOOLS and groups.get(expand) else ""
    # 第 36 轮 11-16 主人「新会话放在每个分类会话的上方，用户就不用去选择了」⇒ 顶上那个通用「➕ 新会话」拿掉，
    # 每个面板第一行是「➕ 新开一个 <这家>」（工具已定）。装了但一个会话都没有的那家也画一个面板，只放这一行。
    # `available_tools` 只是 which + 几个 isfile（本机实测 0.9ms）⇒ 每张卡现查一次，不缓存。
    can_start = _startable_tools()

    def build(rows: int, wide: int) -> str:
        """`rows`：普通组列几行；`wide`：被展开的那一组列几行。"""
        elements: list = []
        elements.append(_md(overview_headline(view.sessions, seen_at) + "点一行看最近几条、接着聊或停下。"))
        if not view.sessions and not gone:
            elements.append(_md("一个会话都没读到。", small=True))
        for tool in TOOLS:
            items = groups.get(tool) or []
            start_row = [_newsession_row(tool)] if tool in can_start else []
            if not items:
                if start_row:
                    brand = _brand_key(tool)
                    name = NEWSESSION_TOOL_NAMES.get(tool, tool)
                    head = f"**{escape_card(name)}** · 还没有会话" if brand else f"🤖 **{escape_card(name)}** · 还没有会话"
                    elements.append(_panel(head, False, start_row, brand))
                continue
            opened = tool == expand
            cap = wide if opened else rows
            shown = [_cleaned(s, clean) for s in items[:cap]]
            main = main_project(shown)
            tags = dup_tags(shown, row_names(shown, main, seen_at))
            inner: list = list(start_row)
            for sess, tag in zip(shown, tags):
                label = row_label(sess, shared_project=main, with_tool=False, dup_tag=tag, seen_at=seen_at)
                if from_phone(sess):
                    label = f"{PHONE_MARK} {label}"
                # 🔴 只有「等你」的那几行醒目（蓝边浅蓝底）。原来是「有进程或在跑」就主按钮
                # ⇒ 终端里闲着的会话也是蓝的，一屏全蓝，等于没有重点（2026-11-06 UX 侦察）。
                # 11-16「列表都向左靠起」⇒ 一行是左对齐的交互容器，不再是按钮（按钮字固定居中）
                inner.append(_tap_row(escape_card(label), fleet_value("open", tool, sess.id),
                                      urgent=display_status(sess, seen_at) in _URGENT))
                # 「等你」⇒ 紧跟一句它在问什么，省掉「点进详情才看得见」那一下（第 17 轮）。
                # 🔴 必须紧跟在**它自己**那个按钮后面：上下两行都在等你时才分得清谁问的。
                asked = overview_pending_line(sess, clean)
                if asked:
                    inner.append(_md(asked, small=True))
            hidden = items[cap:]
            if hidden and opened:
                # 已经是「看全部」了还放不下 ⇒ 再给一个按钮也只是同一张卡，照实叫他回电脑上看
                inner.append(_md(overflow_note(hidden, seen_at), small=True))
            elif hidden:
                inner.append(_button(more_button_label(hidden, seen_at), fleet_value("more", tool)))
            # 🔴 展开与否看**整组**（不只是列出来的那几行）：藏着的那几个要他管，也得展开让他看见那个按钮
            expanded = opened or any(_busy(s, seen_at) for s in items)
            brand = _brand_key(tool)
            elements.append(_panel(panel_title(tool, items, shown, clean, seen_at, brand), expanded, inner, brand))
        legend = status_legend(view.sessions, seen_at)
        if any(from_phone(s) for s in view.sessions):
            legend = (legend + "　" if legend else "") + PHONE_LEGEND
        if legend:
            elements.append(_md(legend, small=True))
        if gone:
            elements.append(_md(SETTLED_NOTE.format(n=gone), small=True))
        if view.notes:
            elements.append({"tag": "hr"})
            elements.append(_md("\n".join(
                "⚠️ " + escape_card(short_title(_safe_clean(clean, n), NOTE_CHARS), keep_format=True)
                for n in view.notes[:6]), small=True))
        elements.append(_button("🔄 刷新", fleet_value("refresh")))
        return _card("🖥 电脑上的会话", "blue", elements)

    if not expand:
        return _fit(lambda rows: build(rows, rows), MAX_ROWS_PER_TOOL)
    # 展开的那一组：别的组照普通卡的样子放（普通卡放得下几行就几行），剩下的地方全给这一组。
    base = MAX_ROWS_PER_TOOL
    while base > 1 and not _fits(build(base, base)):
        base -= 1
    if not _fits(build(base, base)):
        return build(base, base)  # 一行都放不下（只有极端的超长说明才会）⇒ 和普通卡同样的结局
    total = len(groups.get(expand) or [])
    # 二分：这一组最多能放几行（行数越多越大，单调）
    lo, hi = min(base, total), total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _fits(build(base, mid)):
            lo = mid
        else:
            hi = mid - 1
    return build(base, lo)


# ---------- 「等你」时它在问什么（第 8 轮）----------

# 每样最多显示多少字。整段的上限是为了不跟最近消息抢 20KB：最坏（全是要转义的字符）也就几 KB。
PENDING_QUESTIONS = 3
PENDING_OPTIONS = 6
PENDING_QUESTION_CHARS = 120
PENDING_HEADER_CHARS = 20
PENDING_LABEL_CHARS = 30
PENDING_DESC_CHARS = 40
PENDING_SUMMARY_CHARS = 160
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
# 🔴 第 2 步（手机上点选项回答）**不做**：relay 发的是会话间消息，能不能当 AskUserQuestion 的答案
# 没验过、很可能不能。所以这里不挂按钮，明说要回电脑上点 —— 别给一个点了没用的按钮。
PENDING_FOOT = "要在电脑上点：手机上还答不了这一步。"

# 总览里「它在问什么」那一句的预算。比详情卡**窄得多**：总览一屏要放 8 行 × 3 家，
# 这一句只是让他在列表上就看出「是不是我现在就得管」，看全文点进去就是（详情卡那段不变）。
OVERVIEW_PENDING_CHARS = 48
OVERVIEW_PENDING_OPTIONS = 4
OVERVIEW_OPTION_CHARS = 10


def _pending_tool_label(name: str) -> str:
    """工具的人话名（照 `status._TOOL_LABEL`）；不认识的原样放进行内代码。"""
    from ..status import _TOOL_LABEL

    return _TOOL_LABEL.get(name) or inline_code(str(name or "")[:60])


def _clip(clean, text: str, limit: int) -> str:
    """🔴 **先脱敏整段、再截断、最后转义**（顺序同 `build_detail_card` 的红字）。"""
    return _escape(short_title(clean(text or ""), limit))


def pending_elements(sess: Session, clean) -> list:
    """「等你」的会话卡在哪一步 → 详情卡上最近消息之前的那一段；没有 / 读不出来 ⇒ []。

    - AskUserQuestion ⇒「❓ 它在问你：〈题目〉」+ ①A ②B …（label 全给，description 截短）
    - 别的工具 ⇒「⏸ 它在等你确认：〈人话工具名〉〈一行摘要〉」
    - 末尾小字「要在电脑上点」

    🔴 这一段出任何错（脱敏函数炸了、字段形状不对）都只是**这一段不显示**，详情卡照常出。
    """
    pending = getattr(sess, "pending", None)
    if sess.status != "waiting" or pending is None:
        return []
    try:
        lines: list = []
        questions = tuple(pending.questions or ())[:PENDING_QUESTIONS]
        if questions:
            for q in questions:
                head = f"**{_clip(clean, q.header, PENDING_HEADER_CHARS)}** · " if q.header else ""
                multi = "（可多选）" if q.multi else ""
                lines.append(f"❓ 它在问你：{head}{_clip(clean, q.question, PENDING_QUESTION_CHARS)}{multi}")
                opts = tuple(q.options or ())
                for i, (label, desc) in enumerate(opts[:PENDING_OPTIONS]):
                    mark = _CIRCLED[i] if i < len(_CIRCLED) else f"({i + 1})"
                    row = f"{mark} {_clip(clean, label, PENDING_LABEL_CHARS)}"
                    if desc:
                        row += f"：{_clip(clean, desc, PENDING_DESC_CHARS)}"
                    lines.append(row)
                if len(opts) > PENDING_OPTIONS:
                    lines.append(f"…还有 {len(opts) - PENDING_OPTIONS} 个选项")
            if len(pending.questions) > PENDING_QUESTIONS:
                lines.append(f"…还有 {len(pending.questions) - PENDING_QUESTIONS} 道题")
        else:
            what = _pending_tool_label(pending.tool)
            summary = short_title(clean(pending.summary or ""), PENDING_SUMMARY_CHARS)
            if summary:
                # 行内代码里原样不转（`escape_card(keep_format=True)` 只拆 @）
                what += " " + escape_card(inline_code(summary), keep_format=True)
            lines.append(f"⏸ 它在等你确认：{what}")
        return [_md("\n".join(lines)), _md(PENDING_FOOT, small=True)]
    except Exception:  # noqa: BLE001
        return []


def overview_pending_line(sess: Session, clean) -> str:
    """总览里紧跟在那一行底下的一句「它在问你…」；没有 / 读不出来 ⇒ 空串。

    第 17 轮（`docs/iteration/r14-redundant-taps.md` 第 5 条）：原来「等你」在列表上只有一个
    🟡 点，**它在问什么要点进详情才看得到**（看一眼要 1 下）。同一段话点一下就出现在同一张
    卡的下一层 ⇒ 隐私上没有差别（第 7 轮「卡上不放内容」的理由是隐私 + 卡片大小），
    而大小这边自己有上限（上面那三个常量 + `_fit` 的收缩循环照旧兜底）。

    🔴 顺序和详情卡一样：**先整段脱敏、再截断、最后转义**（`_clip`）。
    🔴 出任何错都只是**这一句不显示**（同 `pending_elements`）—— 总览绝不能因为它发不出去。
    """
    pending = getattr(sess, "pending", None)
    if sess.status != "waiting" or pending is None:
        return ""
    try:
        questions = tuple(pending.questions or ())
        if questions:
            first = questions[0]
            text = _clip(clean, first.question, OVERVIEW_PENDING_CHARS)
            if not text:
                return ""
            opts = [_clip(clean, label, OVERVIEW_OPTION_CHARS)
                    for label, _desc in tuple(first.options or ())[:OVERVIEW_PENDING_OPTIONS]]
            line = f"❓ 它在问你：{text}"
            if opts:
                line += "　" + " ".join(f"{_CIRCLED[i]} {o}" for i, o in enumerate(opts) if o)
            more = len(questions) - 1
            return line + (f"（还有 {more} 道题）" if more > 0 else "")
        what = _pending_tool_label(pending.tool)
        summary = short_title(clean(pending.summary or ""), OVERVIEW_PENDING_CHARS)
        if summary:
            what += " " + escape_card(inline_code(summary), keep_format=True)
        return f"⏸ 它在等你确认：{what}"
    except Exception:  # noqa: BLE001
        return ""


def build_detail_card(sess: Session, messages: list, redact=None, note: str = "",
                      talking: bool = False, group_link: str = "") -> str:
    """详情：最近 4 条 + [接着聊]（`can_send` 或 `can_relay`）[停下]（can_stop）[刷新]。

    `can_send` 和 `can_relay` 是两条不同的路（见 `model.Session` 的红字），但在手机上
    **是同一个按钮** —— 用户要的只是「跟它接着说」，哪条路进去是我们的事。选路在
    `control.send()`。

    `talking=True`：这个聊天现在正和这个会话说话（对话模式）⇒ 卡片上要**一直显示**
    「正在和 X 说话，退出发 /cancel」。spec 要求这条指示器在对话模式里不能消失：
    不然用户看完回复随口接一句，那句会悄悄落到别的地方去。

    `group_link`：这个会话**已经有群**时那个群的直开链接（app 那边只在单聊、能接着聊、
    绑定表里有群时才给，不调接口）。有它 ⇒「接着聊」同一个按钮**跳进群 + 回调 op=go**，
    一下进群（第 14 轮）。链接不过白名单 ⇒ 退回只回调的老按钮。
    """
    clean = redact or (lambda s: s)
    can_talk = bool(sess.can_send or sess.can_relay)
    # 「等你」时它在问什么 / 等你确认哪一步。只算一次（下面收缩循环每轮都 build，脱敏别跑好几遍）
    pending = pending_elements(sess, clean)

    def build(chars: int) -> str:
        where = f"{TOOL_LABELS.get(sess.tool, sess.tool)} · {surface_label(sess)}"
        # 第 32 轮：详情卡也用细分后的说法（等你批准 / 等你回答 / 出错了）；不判「跑完没看」——点进来就是在看了
        where += f" · {inline_code(project_of(sess.cwd))} · {display_label(sess)}"
        if sess.age_text:
            where += f" · {sess.age_text}"
        if sess.id:
            where += f" · {inline_code(str(sess.id)[:8])}"
        elements: list = [_md(where, small=True)]
        if sess.title:
            elements.append(_md("**" + _escape(short_title(clean(sess.title), 60)) + "**"))
        if talking:
            elements.append(_md(talking_banner(sess, clean)))
        if note:
            # 🔴 `note` / `why` 是**外来的长文本**（adapter 把整段 stderr 塞进 last_error、
            # control.detail 把异常原文拼进 note），而下面那个收缩循环只缩消息正文 `chars` ⇒
            # 一条 4 万字的报错能让整张卡永远超 20KB、消息缩到 40 字也下不来。
            # 飞书对超大卡片是**静默丢弃** ⇒ 用户点了 [看看它] 什么都收不到（2026-11-04 审查）。
            # note 本来就是一句说明，先剪到和总览一样的预算。
            elements.append(_md("⚠️ " + _escape(short_title(clean(note), NOTE_CHARS)), small=True))
        if pending:
            # 放在最近消息**之前**：主人点进「等你」的会话，第一眼要看到的是它在等什么
            elements.append({"tag": "hr"})
            elements.extend(pending)
        elements.append({"tag": "hr"})
        if messages:
            for msg in messages[-4:]:
                who = ROLE_LABELS.get(getattr(msg, "role", ""), getattr(msg, "role", ""))
                # 🔴 **先脱敏整段、再截断**。反过来的话：私钥块的 END 行被截掉 ⇒
                # 整块匹配不上 ⇒ 密钥正文原样发进飞书；JSON 值被截断丢了收尾引号同理
                # （2026-11-04 安全审查实测）。
                # 每条单独一个 markdown 元素：一条里没关上的 ``` 染不到下一条（双保险，
                # message_md 本身也不会留半个代码块）。
                elements.append(_md(message_md(who, clean(getattr(msg, "text", "") or ""), chars,
                                               max(4, MSG_PREVIEW_LINES * chars // MSG_PREVIEW_CHARS))))
        else:
            elements.append(_md("（没读到消息）", small=True))
        elements.append({"tag": "hr"})
        buttons = []
        if can_talk:
            label = "💬 继续说" if talking else "💬 接着聊"
            jump = jump_callback_button(label, group_link, fleet_value("go", sess.tool, sess.id)) \
                if group_link else None
            buttons.append(jump or _button(label, fleet_value("send", sess.tool, sess.id), primary=True))
        if sess.can_stop:
            buttons.append(_button("⏹ 停下", fleet_value("stop", sess.tool, sess.id)))
        buttons.append(_button("🔄 刷新", fleet_value("refresh", sess.tool, sess.id)))
        if _can_settle(sess):
            # 第 33 轮：「这个我不管了」⇒ 总览不再列它。在跑 / 等你的不给（`settled.can_settle`）
            buttons.append(_button(SETTLE_LABEL, fleet_value("settle", sess.tool, sess.id)))
        for i in range(0, len(buttons), 2):
            elements.append(_row(buttons[i:i + 2]))
        why = []
        if not can_talk:
            # 🔴 两条路都走不通才贴拒绝理由，而且**优先贴 relay 那句**：
            # 终端里开着的会话现在走 relay，`why_not_send` 那句「会分叉成两段对话」
            # 是另一条路的理由，贴出来会让用户以为「开着就没法从手机说话」—— 那已经不对了。
            why_talk = sess.why_not_relay if sess.owner == "terminal" else ""
            why_talk = why_talk or sess.why_not_send or sess.why_not_relay
            if why_talk:
                why.append("💬 " + why_talk)
        if not sess.can_stop and sess.why_not_stop and (sess.pids or sess.status == "running"):
            why.append("⏹ " + sess.why_not_stop)
        if why:
            # 同 note：拒绝理由也可能是拼出来的长串（见上面红字），一条一条剪
            elements.append(_md("\n".join(
                escape_card(short_title(w, NOTE_CHARS), keep_format=True) for w in why), small=True))
            # 第 17 轮：拒绝理由里**叫他去点哪个按钮**，就把那个按钮放在这张卡上。
            # 实测（`docs/iteration/r14-redundant-taps.md` 第 6 条 + 本轮 tests/test_r17_taps.py）：
            # 「这个项目不在手机能用的目录里…点「📂 换项目」」—— 而「换项目」不在常驻菜单里
            # （`botmenu.MENU_TREE` 把它挪进了「全部按钮」）⇒ 照着做要 ⚡常用 → 全部按钮 →
            # 换项目，**3 下**。放在这里就是 1 下。
            #
            # 🔴 只给这一个、只按**这张卡上真写了那句话**判，不按理由的来源判（三家 adapter
            # 各有一句同义的，字面都带「换项目」）。别的「叫你去点」一律不补：
            # - 「🔑 权限」本来就是常驻菜单一级节点（1 下），放了省不下
            # - 「全授权」要走它自己的二次确认（`menu.build_confirm_loosen_card`），不许抄近道
            # - 「回到那个对话点「⏹ 停下」」说的是**另一个聊天**里的停下，补了会停错东西
            if any("换项目" in w for w in why):
                elements.extend(button_rows([do_button("dirs")]))
        # 原来这里是一行「退出对话模式发 /cancel；发「会话」回到总览」⇒ 换成按钮（actions.py）
        elements.extend(detail_nav_rows(talking))
        return _card("🖥 " + short_title(f"{TOOL_LABELS.get(sess.tool, sess.tool)} 会话", 30), "indigo", elements)

    chars = MSG_PREVIEW_CHARS
    blob = build(chars)
    while len(blob.encode("utf-8")) > MAX_CARD_BYTES and chars > 40:
        chars //= 2
        blob = build(chars)
    return blob


# ---------- 结束（第 33 轮，`fleet/settled.py`）----------

SETTLE_LABEL = "✓ 结束"


def _can_settle(sess: Session) -> bool:
    from .settled import can_settle

    return can_settle(sess)[0]


def build_settled_card(sess_or_target: Any, redact=None) -> dict:
    """点完「✓ 结束」原地换成的卡（dict，直接放进回调响应）：「✓ 已结束 · 撤销」。

    🔴 写清「只记在手机这边」：电脑上那个会话一点没动，别让他以为删掉了。
    """
    clean = redact or (lambda s: s)
    label = _escape(session_label(sess_or_target, clean))
    tool = getattr(sess_or_target, "tool", "")
    sid = str(getattr(sess_or_target, "id", "") or "")
    elements = [
        _md(f"{label} 不再出现在总览里。它有新动静会自己回来。"),
        _md("只记在手机这边，电脑上那个会话没动。", small=True),
        _row([
            _button("↩︎ 撤销", fleet_value("unsettle", tool, sid)),
            do_button("sessions", "🖥 回到总览"),
        ]),
    ]
    return json.loads(_card("✓ 已结束", "grey", elements))


def build_unsettled_card(sess_or_target: Any, redact=None) -> dict:
    """点了「撤销」之后原地换成的卡（dict）。"""
    clean = redact or (lambda s: s)
    label = _escape(session_label(sess_or_target, clean))
    tool = getattr(sess_or_target, "tool", "")
    sid = str(getattr(sess_or_target, "id", "") or "")
    elements = [
        _md(f"好，{label} 回到总览里了。"),
        _row([
            _button("👀 看看它", fleet_value("open", tool, sid), primary=True),
            do_button("sessions", "🖥 回到总览"),
        ]),
    ]
    return json.loads(_card("↩︎ 已撤销", "indigo", elements))


# ---------- 第 36 轮「➕ 新会话」：工具 → 项目 → 第一句话（回车就开） ----------
#
# 11-16 主人「交互太复杂了」⇒ 简化成：**一行两个下拉（工具 | 项目）+ 一个输入框**，最快 = 点「➕ 新会话」→ 打一句回车。
# 换工具 / 换项目都**原地换卡**（fleet-ops-release.md 第六节第一条）。规矩照「↑ 最近问过」那张（`bridge/history.py`）：
# - 第一句话 = **单行**输入框、**不在 form 里**、带 `behaviors` 回调 ⇒ 回车就发；
# - 两个下拉都设 `initial_option`（= 当前默认那个选项的 **value**）：不设的话手机上只显示 placeholder，看不出默认选的是谁。
#   字段名见飞书文档 https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/single-select-dropdown-menu
#   （「initial_option：下拉选择组件的初始选项的内容，覆盖 placeholder」）；本机 Lark messenger.asar 里 select 组件按
#   `option.value === initialOption` 找初始项 ⇒ 填 value 不填显示名；
# - 「同时在电脑上打开窗口」开关拿掉：Claude / Codex 一律开，Cursor 一律不开（11-16 主人拍板），卡上只留一行小字说明；
# - 下拉的值里**没有路径、没有原话**：工具是枚举、项目是下标 `p<i>`（`actions.resolve_newsession_click` + app 再核一次）。
NEWSESSION_INPUT = "ns_text"
NEWSESSION_MAX_CHARS = 1000        # 飞书卡片输入框上限（同 history.MAX_CHARS）；服务端再判一次
NEWSESSION_MAX_PROJECTS = 20       # 下拉里最多几个项目（最近用过的在前）
NEWSESSION_TOOL_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor"}
# 卡上那一行小字（只一行）。Cursor 不开窗口、Codex / Cursor 从手机只读 —— 群里第一句还会再发完整权限行（`app._rights_line`）
NEWSESSION_HINTS = {
    "claude": "回车后电脑上会打开一个窗口接着这个会话，回复发到群里",
    "codex": "回车后电脑上会打开一个窗口接着这个会话（手机发的话只读）",
    "cursor": "Cursor 在后台跑（只读），回复发到群里",
}
NEWSESSION_PROJECT_CELLS = 28      # 半屏宽的下拉，项目名截短一点
PHONE_MARK = "📱"
PHONE_LEGEND = "📱 从手机开的"


def from_phone(sess: Session) -> bool:
    """这个会话是不是从手机「➕ 新会话」开的（判断在 B 的 `newsession.started_from_phone`）。读不出 ⇒ 不标。"""
    try:
        from .newsession import started_from_phone

        return bool(started_from_phone(sess.tool, sess.id, sess.title or ""))
    except Exception:  # noqa: BLE001 —— 标记是锦上添花，总览不能因为它出不来
        return False


def newsession_open_window(tool: str) -> bool:
    """开不开电脑上的窗口：Claude / Codex 一律开，Cursor 一律不开（11-16 主人拍板，卡上不再有开关）。"""
    return tool in ("claude", "codex")


def _ns_select(placeholder: str, options: list, initial: str, op: str, nonce: str) -> dict:
    """form 外的单选下拉。`options`：[(显示名, value)]；`initial` = 当前默认那个的 value。"""
    node = {
        "tag": "select_static", "width": "fill",
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "options": [{"text": {"tag": "plain_text", "content": text}, "value": value} for text, value in options],
        "behaviors": [{"type": "callback", "value": {"act": "nsess", "op": op, "n": nonce}}],
    }
    if initial and any(value == initial for _, value in options):
        node["initial_option"] = initial
    return node


def build_newsession_card(nonce: str, tools: list, tool: str, projects: list, proj: Optional[int],
                          warn: str = "", fixed: bool = False) -> dict:
    """新会话卡（dict，回调里原地换卡直接用）：一行【工具 ▾】【项目 ▾】+ 一个「第一句话，回车就开」。

    `projects`：[(显示名, 最近用过)]，**只有显示名**，路径在服务端；`proj`：选中的下标（None = 还没选）。
    `tools`：本机装了的那几家（`newsession.available_tools()`），没装的不显示。
    `fixed`（11-16）：从总览面板里「➕ 新开一个 X」进来的 ⇒ 工具已定，**不画工具下拉**，
    标题写「➕ 新开一个 Claude Code」，只剩项目下拉 + 输入框。
    """
    if fixed and tools and tool in tools:
        return _newsession_fixed_card(nonce, tool, projects, proj, warn)
    elements: list = []
    if not tools:
        elements.append(_md("这台电脑上没找到 Claude Code、Codex 或 Cursor，装好之后再来。"))
        elements.extend(button_rows([do_button("sessions", "🖥 回到总览")]))
        return json.loads(_card("➕ 新会话", "grey", elements))
    tool_sel = _ns_select("工具", [(NEWSESSION_TOOL_NAMES.get(t, t), t) for t in tools], tool, "tsel", nonce)
    columns = [{"tag": "column", "width": "weighted", "weight": 2, "elements": [tool_sel]}]
    if projects:
        opts = [(clip_cells(name, NEWSESSION_PROJECT_CELLS), f"p{i}")
                for i, (name, _recent) in enumerate(projects[:NEWSESSION_MAX_PROJECTS])]
        initial = f"p{proj}" if proj is not None and 0 <= proj < len(opts) else ""
        columns.append({"tag": "column", "width": "weighted", "weight": 3,
                        "elements": [_ns_select("项目", opts, initial, "proj", nonce)]})
    elements.append({"tag": "column_set", "horizontal_spacing": "8px", "columns": columns})
    if not projects:
        elements.append(_md("手机能用的目录里一个项目都没扫到，先点「📂 换项目」看看。", small=True))
    if warn:
        elements.append(_md(f"⚠️ {escape_card(warn)}"))
    # 第一句话：单行、不在 form 里、带回调 ⇒ 回车就开
    elements.append({
        "tag": "input", "name": NEWSESSION_INPUT, "width": "fill", "max_length": NEWSESSION_MAX_CHARS,
        "placeholder": {"tag": "plain_text", "content": "第一句话，回车就开"},
        "behaviors": [{"type": "callback", "value": {"act": "nsess", "op": "send", "n": nonce}}],
    })
    hint = NEWSESSION_HINTS.get(tool)
    if hint:
        elements.append(_md(hint, small=True))
    return json.loads(_card("➕ 新会话", "blue", elements))


def _newsession_fixed_card(nonce: str, tool: str, projects: list, proj: Optional[int], warn: str) -> dict:
    """工具已定的新会话卡：【项目 ▾】（默认最近用过）+ 输入框。字段规矩同 `build_newsession_card`。"""
    elements: list = []
    if projects:
        opts = [(clip_cells(name, NEWSESSION_PROJECT_CELLS * 2), f"p{i}")
                for i, (name, _recent) in enumerate(projects[:NEWSESSION_MAX_PROJECTS])]
        initial = f"p{proj}" if proj is not None and 0 <= proj < len(opts) else ""
        elements.append(_ns_select("项目", opts, initial, "proj", nonce))
    else:
        elements.append(_md("手机能用的目录里一个项目都没扫到，先点「📂 换项目」看看。", small=True))
    if warn:
        elements.append(_md(f"⚠️ {escape_card(warn)}"))
    elements.append({
        "tag": "input", "name": NEWSESSION_INPUT, "width": "fill", "max_length": NEWSESSION_MAX_CHARS,
        "placeholder": {"tag": "plain_text", "content": "第一句话，回车就开"},
        "behaviors": [{"type": "callback", "value": {"act": "nsess", "op": "send", "n": nonce}}],
    })
    hint = NEWSESSION_HINTS.get(tool)
    if hint:
        elements.append(_md(hint, small=True))
    return json.loads(_card(newsession_row_label(tool), "blue", elements))


def build_newsession_started_card(tool: str, project: str, prompt_preview: str) -> dict:
    """回车之后原地换成这张：**没有输入框**（再回车就是又开一个）。`prompt_preview` 调用方先脱敏。"""
    name = NEWSESSION_TOOL_NAMES.get(tool, tool)
    lines = [f"正在电脑上新开 **{escape_card(name)}** 会话（{escape_card(project)}）……"]
    if prompt_preview:
        lines.append("第一句：" + escape_card(short_title(prompt_preview, 60)))
    elements = [_md("\n".join(lines)),
                _md("开好了会在这里说一声，并给它建一个群。", small=True)]
    elements.extend(button_rows([do_button("sessions", "🖥 回到总览")]))
    return json.loads(_card("⏳ 正在开新会话", "wathet", elements))


def build_newsession_cancelled_card() -> dict:
    elements = [_md("好，不开了。")]
    elements.extend(button_rows([do_button("sessions", "🖥 回到总览")]))
    return json.loads(_card("➕ 新会话", "grey", elements))


# ---------- 第 37 轮：Claude 会话群工具栏的回卡（🧠 模型 / 📜 进度） ----------
#
# 工具栏点击不带参数，要选项的一律回一张卡（`docs/r37/contract-toolkit.md`）。
TOOLBAR_RELAY_MODEL_NOTE = "终端开着的会话换不了模型和思考强度，在电脑上敲 /model。"
TOOLBAR_PROGRESS_MESSAGES = 4
TOOLBAR_DEFAULT_LABEL = "跟电脑上一样（不指定）"


def _tkm_select(placeholder: str, options: list, initial: str, op: str, nonce: str) -> dict:
    from .actions import toolbar_model_value

    node = {
        "tag": "select_static", "width": "fill",
        "placeholder": {"tag": "plain_text", "content": placeholder},
        "options": [{"text": {"tag": "plain_text", "content": text}, "value": value} for text, value in options],
        "behaviors": [{"type": "callback", "value": toolbar_model_value(op, nonce)}],
    }
    if initial and any(value == initial for _, value in options):
        node["initial_option"] = initial
    return node


def build_toolbar_model_card(label: str, nonce: str, model: str = "", effort: str = "",
                             relay: bool = False, note: str = "") -> dict:
    """「🧠 模型」卡：模型下拉 + 思考强度下拉，选了按群记住，下一轮起生效。

    `relay=True`：这个会话正开在电脑终端里 ⇒ **不画下拉**，只说换不了（relay 只能往它输入队列里放话，
    `/model` 送进去也不会被当命令执行，见 `docs/r37/claude-group-shortcuts-design.md` 1.1）。
    下拉的值只有下标（`m<i>` / `e<i>` / `d`），模型名不进卡片值。
    """
    from ..policy import MODEL_CHOICES
    from .chatbind import EFFORT_CHOICES

    head = f"🧠 {escape_card(label)}" if label else "🧠 模型"
    if relay:
        elements = [_md(TOOLBAR_RELAY_MODEL_NOTE)]
        if note:
            elements.append(_md(escape_card(note), small=True))
        return json.loads(_card(head, "grey", elements))
    model_opts = [(TOOLBAR_DEFAULT_LABEL, "d")] + [
        (f"{lab} · {name}", f"m{i}") for i, (name, lab, _why) in enumerate(MODEL_CHOICES)]
    effort_opts = [(TOOLBAR_DEFAULT_LABEL, "d")] + [
        (f"{lab}（{name}）· {why}", f"e{i}") for i, (name, lab, why) in enumerate(EFFORT_CHOICES)]
    m_init = next((f"m{i}" for i, (n, _l, _w) in enumerate(MODEL_CHOICES) if n == model), "d")
    e_init = next((f"e{i}" for i, (n, _l, _w) in enumerate(EFFORT_CHOICES) if n == effort), "d")
    now = f"现在：模型 **{escape_card(model or '不指定')}** · 思考强度 **{escape_card(effort or '不指定')}**"
    elements = [
        _md(now),
        {"tag": "column_set", "horizontal_spacing": "8px", "columns": [
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [_tkm_select("模型", model_opts, m_init, "model", nonce)]},
            {"tag": "column", "width": "weighted", "weight": 1,
             "elements": [_tkm_select("思考强度", effort_opts, e_init, "effort", nonce)]},
        ]},
        _md("选了只记在这个群上，下一句起生效。电脑上终端开着它时不生效（那时用电脑上的设置）。", small=True),
    ]
    if note:
        elements.append(_md(escape_card(note), small=True))
    return json.loads(_card(head, "blue", elements))


def build_toolbar_progress_card(sess: Session, messages: list, redact=None, note: str = "") -> str:
    """「📜 进度」：最近几条 + 它在等你什么。只读 jsonl 尾巴，不往会话里送任何话。

    先脱敏整段再截断（同 `build_detail_card`）。
    """
    clean = redact or (lambda s: s)
    elements: list = []
    shown = list(messages or [])[-TOOLBAR_PROGRESS_MESSAGES:]
    if not shown:
        elements.append(_md("最近没读到它说过的话。"))
    for msg in shown:
        who = "你" if getattr(msg, "role", "") == "user" else "它"
        text = short_title(clean(str(getattr(msg, "text", "") or "")), 300)
        elements.append(_md(f"**{who}**：{escape_card(text)}"))
    elements.extend(pending_elements(sess, clean))
    if note:
        elements.append(_md(escape_card(note), small=True))
    elements.append(_md("只是看一眼，没打扰它、没花一轮。", small=True))
    return _card(f"📜 {session_label(sess, clean)}", "wathet", elements)
