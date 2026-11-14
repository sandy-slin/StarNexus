"""「点一下就行」的按钮：替掉所有叫用户打 `/cancel`、`/stop`、`/resume` 的提示。

用户原话：「不要让用户输入 "/cancel" 等类似的，尽量用按钮的形式。」

## 怎么用

- 卡片里要加一排按钮 ⇒ `button_rows([do_button("quit"), ...])`，返回的元素直接 extend 进 elements。
- 纯文本回复（`post`）带不了按钮 ⇒ 在它后面跟一张 `build_buttons_card(...)` 小卡片。
- 文字命令照样能用（卡片发不出去时的兜底），只是提示语里不再叫人去打。

## 回调值（白名单，同 `menu.resolve_click` 的思路）

`{"act": "do", "op": "<下面 DO_COMMANDS 里的一个>"}`，**只有这两个键**。
点了 = 主人打了对应那条斜杠命令，走 `Bridge._handle_command` 正常分派 ——
鉴权、暂停、审批全都照常，按钮不是绕过闸门的后门。

🔴 value 来自飞书，理论上能伪造。所以：
- op 只认 `DO_COMMANDS` 里预设的，**不带任何参数**（没有 `/cd x`、`/mode trusted` 这种）
- 多一个键都不认（不许夹带 text / tool / id）
- 要对着某个会话的按钮（重新接着聊、看看它）**不走这里**，走 `cards.fleet_value`，
  那条路要求 (tool, id) 在最近一次扫描里出现过
- 「收回到只读」走菜单那条现成的 `{"act":"menu","mode":"readonly"}`（`menu.resolve_click` 只认已知档名）

为什么 `resume` 能进白名单：它只挂在「已暂停」那句话后面，点的人必须是主人，
效果和他打 `/resume` 一样；解除暂停**不发任何批准**（gate 不动，见 test_token_lifecycle 组合 D）。
放开权限那种「点错了就不问你直接改」的操作一个都不在这里。
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Optional

ACT = "do"

# op → 等价的斜杠命令。全是「只显示」或「往更少发生的方向走」的命令，外加主人明确解除暂停。
DO_COMMANDS = {
    "quit": "/cancel",      # 退出对话模式
    "stop": "/stop",        # 停下（对话模式里会照实说停不停得了）
    "sessions": "/sessions",  # 回到会话总览
    "new": "/new",          # 新话题
    "resume": "/resume",    # 解除暂停
    "status": "/status",
    "mode": "/mode",        # 只打开档位选择卡，本身不改设置
    "model": "/model",      # 同上
    "dirs": "/dirs",
    "menu": "/menu",
    "help": "/help",
    # 诊断卡。纯显示、且内容走 status.HEALTH_FIELDS 白名单 ⇒ 点错了最坏只是多发一张卡。
    "diag": "/诊断",
    "history": "/history",  # 「↑ 最近问过」卡（只显示，不改任何设置）
    # 「➕ 新建会话」：只打开选工具卡；真正开会话要再点工具、点项目、再说一句话（fleet/newsession.py）
    "newsession": "/newsession",
    "newcancel": "/newsession cancel",  # 不建了：只清掉选到一半的新建会话，不碰对话模式
}

# 这几个只是「看」，不看是哪个聊天也能做；其余的要对准是哪个聊天（退出 / 停 / 新话题 / 继续）。
# `diag` 报的是**整台机器**的状态，和是哪个聊天无关 ⇒ 不要求 scope。
UNSCOPED_OPS = frozenset({"sessions", "help", "diag"})

DEFAULT_LABELS = {
    "quit": "🚪 退出对话",
    "stop": "⏹ 停下",
    "sessions": "🖥 看看会话",
    "new": "🆕 新话题",
    "resume": "▶️ 继续",
    "status": "ℹ️ 现在状态",
    "mode": "🔑 权限",
    "model": "🧠 模型",
    "dirs": "📂 换项目",
    "menu": "📋 菜单",
    "help": "❓ 帮助",
    "diag": "🩺 运行诊断",
    "history": "↑ 最近问过",
    "newsession": "➕ 新建会话",
    "newcancel": "✖️ 不建了",
}

# 点了之后的 toast。不许露内部命令（同 app._toast_for 的红字）。
TOASTS = {
    "quit": "好，退出对话",
    "stop": "收到，正在停",
    "sessions": "收到，看看电脑上的会话",
    "new": "收到，开个新话题",
    "resume": "收到，继续",
    "status": "收到，看看现在状态",
    "mode": "收到，看看权限",
    "model": "收到，看看模型",
    "dirs": "收到，看看有哪些项目",
    "menu": "收到，打开菜单",
    "help": "收到，打开帮助",
    "diag": "收到，看看运行状态",
    "history": "收到，调出最近问过的话",
    "newsession": "收到，选一下用哪个",
    "newcancel": "好，不建了",
}

MAX_BUTTONS_CARD_BYTES = 8 * 1024


def do_value(op: str) -> dict:
    if op not in DO_COMMANDS:
        raise ValueError(f"不认识的按钮 op：{op!r}")
    return {"act": ACT, "op": op}


def resolve_do_click(value: Any) -> str:
    """回调 value → 等价的斜杠命令；认不出（含伪造、多带了键）⇒ 空串。"""
    if not isinstance(value, dict) or value.get("act") != ACT:
        return ""
    if set(value) != {"act", "op"}:
        return ""
    op = value.get("op")
    if not isinstance(op, str):
        return ""
    return DO_COMMANDS.get(op, "")


def op_of(value: Any) -> str:
    """日志用：回调里的 op（已过白名单才有意义）。"""
    return str(value.get("op", ""))[:16] if isinstance(value, dict) else ""


# 入群链接只接受飞书自己的 applink 域名。
#
# 🔴 为什么要白名单：这个按钮的 URL 来自**飞书接口的回包**，点下去是用户在自己的飞书客户端里打开。
# 回包被串改、或者以后有人把别处的字符串接到这里，就成了一个「主人一定会点」的任意跳转。
# 域名写死 ⇒ 最坏只能跳到飞书自己的页面。
_URL_BUTTON_HOSTS = ("https://applink.feishu.cn/", "https://applink.larksuite.com/")


def safe_link(url: Any) -> str:
    """能放进按钮的入群链接；形状不对一律空串（调用方拿到空串就别放这个按钮）。"""
    value = str(url or "").strip()
    if len(value) > 512 or any(ch.isspace() for ch in value):
        return ""
    return value if value.startswith(_URL_BUTTON_HOSTS) else ""


def url_button(label: str, url: str, primary: bool = True) -> Optional[dict]:
    """一个「点一下就跳过去」的按钮。URL 不过白名单 ⇒ 返回 None（按钮直接不放）。

    和别的按钮不一样：它**不回调**我们，所以不走 `resolve_do_click` 那道闸 ——
    闸门换成了上面的 `safe_link`。
    """
    link = safe_link(url)
    if not link:
        return None
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "open_url", "default_url": link}],
    }


def jump_callback_button(label: str, url: str, value: dict, primary: bool = True) -> Optional[dict]:
    """**一个按钮两样都做**：客户端跳到 `url`，同时回调我们 `value`。URL 不过白名单 ⇒ None。

    第 14 轮（用户原话：「点击接着聊，直接进群 … 而不是再弹出一个进群聊，让用户再点击一下」）。
    飞书按钮文档 behaviors 字段：「配置交互类型和具体交互行为。支持同时生效跳转链接和回传交互」
    （open.feishu.cn 按钮组件 JSON 2.0 文档，11-06 现查）。真机上两样是不是都生效**没在真机点过**（推断）。

    跳转闸门同 `url_button`（`safe_link`）；回调闸门同别的回调按钮（`value` 由调用方用
    `cards.fleet_value` 造，进来照样过 `resolve_fleet_click`）。
    """
    link = safe_link(url)
    if not link or not isinstance(value, dict):
        return None
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "open_url", "default_url": link}, {"type": "callback", "value": value}],
    }


def _button(label: str, value: dict, primary: bool = False) -> dict:
    # 和 menu / cards 同一种写法：schema 2.0、没有 action 容器、回调走 behaviors
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
    }


def do_button(op: str, label: Optional[str] = None, primary: bool = False) -> dict:
    return _button(label or DEFAULT_LABELS[op], do_value(op), primary)


def fleet_button(op: str, tool: str, sid: str, label: str, primary: bool = False) -> dict:
    """对着某个会话的按钮（重新接着聊 / 看看它）。回调走 `resolve_fleet_click` 那道闸。"""
    from .cards import fleet_value

    return _button(label, fleet_value(op, tool, sid), primary)


# 第 33 轮「↩︎ 接着上次」：闲太久开了新会话，点它换回上一个。
# 回调值只有 `{"act": "cont", "n": <序号>}`：要换回哪个会话记在服务端（`Bridge._continue_offers`），
# 卡片值伪造不出「换到任意会话」。序号对不上 / 多带键 ⇒ 拒（`resolve_continue_click`）。
CONTINUE_ACT = "cont"
CONTINUE_HEAD_CHARS = 12


def continue_label(head: str) -> str:
    """「↩︎ 接着上次（<上次那句话前 12 字>）」。调用方先脱敏；没有那句话 ⇒ 不带括号。"""
    one = " ".join(str(head or "").split())
    if not one:
        return "↩︎ 接着上次"
    short = one if len(one) <= CONTINUE_HEAD_CHARS else one[:CONTINUE_HEAD_CHARS] + "…"
    return f"↩︎ 接着上次（{short}）"


def continue_button(nonce: str, head: str = "") -> dict:
    return _button(continue_label(head), {"act": CONTINUE_ACT, "n": str(nonce)})


def resolve_continue_click(value: Any) -> str:
    """回调 value → 序号；形状不对（多键、不是字符串、太长）⇒ 空串。对不对得上由调用方查服务端记录。"""
    if not isinstance(value, dict) or value.get("act") != CONTINUE_ACT or set(value) != {"act", "n"}:
        return ""
    nonce = value.get("n")
    if not isinstance(nonce, str) or not nonce or len(nonce) > 32:
        return ""
    return nonce


def readonly_button(label: str = "🔒 收回到只读") -> dict:
    """一键收紧。走菜单现成的 value，`menu.resolve_click` 只认已知档名；收紧不用二次确认。"""
    return _button(label, {"act": "menu", "mode": "readonly"})


def button_rows(buttons: Iterable[dict]) -> list:
    """两个一排（手机竖屏三个就挤了）。空列表 ⇒ 空列表。"""
    items = [b for b in buttons if b]
    rows = []
    for i in range(0, len(items), 2):
        pair = items[i:i + 2]
        rows.append({
            "tag": "column_set",
            "horizontal_spacing": "8px",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]} for b in pair
            ],
        })
    return rows


def detail_nav_rows(talking: bool) -> list:
    """会话详情卡最底下那排：对话模式里 [退出对话][回到总览]，否则只有 [回到总览]。

    给 `cards.build_detail_card` 用 —— 那边原来是一行「退出对话模式发 /cancel；发「会话」回到总览」。
    """
    buttons = [do_button("sessions", "🖥 回到总览")]
    if talking:
        buttons.insert(0, do_button("quit"))
    return button_rows(buttons)


def _label_of(button: dict) -> str:
    text = ((button or {}).get("text") or {}).get("content", "")
    # 去掉开头的图标，只留字
    return text.split(" ", 1)[-1] if " " in text else text


def typed_fallback(buttons: Iterable[dict]) -> str:
    """按钮卡发不出去时的兜底：同样的事用文字怎么做。只在**卡片失败**时才发给用户。"""
    hints = []
    seen = set()
    for button in buttons or ():
        try:
            behavior = button["behaviors"][0]
        except (KeyError, IndexError, TypeError):
            continue
        # 跳转按钮（入群链接）没有 value：卡片发不出去时就把链接**原样贴出来**，
        # 飞书会把它渲染成可点的。少了这一下，建完群的链接会随着卡片一起没了。
        if isinstance(behavior, dict) and behavior.get("type") == "open_url":
            link = safe_link(behavior.get("default_url"))
            hint = f"{_label_of(button)}：{link}" if link else ""
            if hint and hint not in seen:
                seen.add(hint)
                hints.append(hint)
            continue
        value = behavior.get("value") if isinstance(behavior, dict) else None
        act = value.get("act") if isinstance(value, dict) else None
        if act == ACT:
            hint = f"`{DO_COMMANDS.get(value.get('op'), '')}` {_label_of(button)}"
        elif act == "fleet":
            hint = "发「会话」重新挑一个"
        elif act == "menu" and value.get("mode") == "readonly":
            hint = "`/mode 只读` 收回"
        else:
            continue
        if hint not in seen and not hint.startswith("`` "):
            seen.add(hint)
            hints.append(hint)
    if not hints:
        return ""
    return "（按钮没发出来，也可以直接发：" + "、".join(hints) + "）"


def build_buttons_card(buttons: Iterable[dict], note: str = "") -> str:
    """跟在纯文本回复后面的小卡片：一句小字（可选）+ 按钮。

    `note` 是**我们自己写的**文案；要放会话名之类的外来文字，调用方先脱敏、再转义。
    """
    elements: list = []
    if note:
        elements.append({"tag": "markdown", "content": note, "text_size": "notation"})
    elements.extend(button_rows(buttons))
    return json.dumps(
        {"schema": "2.0", "config": {"wide_screen_mode": True}, "body": {"elements": elements}},
        ensure_ascii=False,
    )
