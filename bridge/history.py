"""「↑ 最近问过」：飞书里没法按方向键调历史，用一张带输入框的卡片代替。

用户原话：「当我选中输入框，按键盘的向下和向上键的时候，能否把历史记录输入进去，可以参考 codex app 的设计」。

## 为什么是卡片（调研结论，2026-11-08）

- 飞书自己的输入框不归机器人管：机器人只在消息**发出之后**收到它，碰不到输入框、收不到按键。
  本机 Lark 桌面版的程序里，输入框收到 ↑ 只是在多行文字里挪光标，没有「调出上一条」。
- 飞书卡片 2.0 有自己的输入框（`input`，可预填 `default_value`、多行、≤1000 字），
  放在表单容器（`form`）里配一个提交按钮，点了以后框里的字通过 `card.action.trigger`
  的 `action.form_value` 回传（官方组件文档原文，188 上拉的）。

所以：卡片里一个预填好上一句话的输入框 + 「⬆ 上一条 / ⬇ 下一条」 + 「发送」。
⬆⬇ 点一下，桥把同一张卡**原地换成**相邻那句（回调响应体里带新卡）。

## 规矩

- 只记**他自己打的话**，不记机器人的回复；斜杠命令、「菜单」这类不记。
- 按会话分（私聊和每个群各一份），Codex 有人专门抱怨过跨会话混在一起（openai/codex#14151）。
- 记之前先脱敏；存在本机 config 同目录，0600、原子写；每个会话最多留 50 句。
- 坏文件 / 写不进 ⇒ 当没有历史，绝不让收消息那条路出错。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from typing import Optional

logger = logging.getLogger("feishu-bridge.history")

HISTORY_FILE = "input_history.json"
KEEP = 50            # 每个会话最多留几句
MAX_CHARS = 1000     # 飞书卡片输入框上限（官方文档 max_length 默认 1000）
FORM_NAME = "hist_form"
INPUT_NAME = "hist_text"


def _skip(text: str) -> bool:
    """不进历史的：命令、菜单触发词、空话。"""
    from .menu import is_menu_request

    t = (text or "").strip()
    return (not t) or t.startswith("/") or is_menu_request(t) or len(t) < 2


class InputHistory:
    """一个文件、按 scope 分的「他问过的话」，新的在前。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, data: dict) -> None:
        if not self.path:
            return
        directory = os.path.dirname(self.path) or "."
        try:
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             delete=False) as tmp:
                json.dump(data, tmp, ensure_ascii=False)
                name = tmp.name
            os.chmod(name, 0o600)
            os.replace(name, self.path)
        except OSError:
            logger.debug("输入历史写不进去（只影响「最近问过」）", exc_info=True)

    def add(self, scope: str, text: str) -> None:
        if not scope or _skip(text):
            return
        from .redact import redact

        clean = redact(text.strip())[0][:MAX_CHARS]
        with self._lock:
            data = self._load()
            items = [x for x in data.get(scope, []) if isinstance(x, dict) and x.get("t") != clean]
            items.insert(0, {"t": clean, "at": int(time.time())})
            data[scope] = items[:KEEP]
            self._save(data)

    def list(self, scope: str) -> list[str]:
        return [t for t, _ in self.entries(scope)]

    def entries(self, scope: str) -> list[tuple[str, int]]:
        """[(原话, 时间戳)]，新的在前。"""
        with self._lock:
            items = self._load().get(scope, [])
        return [(x["t"], int(x.get("at") or 0)) for x in items
                if isinstance(x, dict) and isinstance(x.get("t"), str)]


LIST = 20           # 下拉里列几句（下拉高 248px，约 6~7 行可见，其余滚动）
LABEL_CHARS = 40    # 下拉里每句显示前几个字
SELECT_NAME = "hist_pick"


def fingerprint(text: str) -> str:
    """一句话的短指纹。卡片里用它指代那句话 —— 不用序号：卡发出去之后他又问了一句，
    历史整体往后挪一位，序号就指到别的话上了。也不把原话放进回传值（不信卡片回传的文字）。"""
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def find(items: list[str], fp: str) -> Optional[int]:
    for i, text in enumerate(items):
        if fingerprint(text) == fp:
            return i
    return None


def _label(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= LABEL_CHARS else one[:LABEL_CHARS] + "…"


def ago(at: int, now: Optional[float] = None) -> str:
    """「刚刚 / 3 分钟前 / 2 小时前 / 昨天 / 11-05」。"""
    if not at:
        return ""
    now = time.time() if now is None else now
    gap = max(0, int(now - at))
    if gap < 60:
        return "刚刚"
    if gap < 3600:
        return f"{gap // 60} 分钟前"
    then, today = time.localtime(at), time.localtime(now)
    if then.tm_yday == today.tm_yday and then.tm_year == today.tm_year:
        return f"{gap // 3600} 小时前"
    if gap < 2 * 86400 and (today.tm_yday - then.tm_yday) % 366 == 1:
        return "昨天"
    return time.strftime("%m-%d", then)


def _picker(entries: list[tuple[str, int]], now: Optional[float] = None) -> dict:
    """下拉：换一句填进下面的框。**不再选中即发**（第 28 轮：用户实测下拉里回车没起作用；
    发送统一交给输入框的回车）。下拉必须在 form 外（form 里只记值不回调）。"""
    return {
        "tag": "select_static", "width": "fill",
        "placeholder": {"tag": "plain_text", "content": "换一句（点开后 ↑ ↓ 选，也能打字搜）"},
        "options": [{"text": {"tag": "plain_text",
                              "content": _label(t) + (f"　· {ago(at, now)}" if at else "")},
                     "value": fingerprint(t)}
                    for t, at in entries[:LIST]],
        "behaviors": [{"type": "callback", "value": {"act": "hist", "op": "pick"}}],
    }


def _header(title: str, template: str, subtitle: str = "") -> dict:
    h = {"title": {"tag": "plain_text", "content": title}, "template": template}
    if subtitle:
        h["subtitle"] = {"tag": "plain_text", "content": subtitle}
    return h


def _card(header: dict, elements: list) -> dict:
    return {"schema": "2.0", "config": {"update_multi": True}, "header": header,
            "body": {"elements": elements}}


def _cbtn(label: str, value: dict, primary: bool = False) -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": label},
            "type": "primary" if primary else "default", "width": "fill", "size": "medium",
            "behaviors": [{"type": "callback", "value": value}]}


def _row(*buttons: dict) -> dict:
    return {"tag": "column_set", "horizontal_spacing": "8px", "columns": [
        {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]} for b in buttons]}


def _box(text: str) -> dict:
    """🔴 回车就发的输入框。本机 Lark 客户端卡片 input 的实现（messenger.asar）：
    `onKeyUp: Enter && !输入法组词中 ⇒ es()`，`es = () => { 空 || 出错 || 在表单里 || 回调({input_value}) }`。
    所以必须同时满足三条，回车才会发：**单行**（multiline_text 的回车只插一个空格）、
    **不放进 form**（在表单里回车什么都不做 —— 上一版就是栽在这）、配 `behaviors` 回调。
    输入框右边客户端会自带一个发送小图标，点它也一样。
    """
    return {
        "tag": "input", "name": INPUT_NAME, "width": "fill", "max_length": MAX_CHARS,
        "default_value": text,
        "placeholder": {"tag": "plain_text", "content": "改一改，回车就发"},
        "behaviors": [{"type": "callback", "value": {"act": "hist", "op": "send"}}],
    }


def build_card(entries, index: int = 0, now: Optional[float] = None, where: str = "这个聊天",
               warn: str = "") -> dict:
    """「↑ 最近问过」：上面一个下拉换句子，下面一个**回车就发**的框（预填第 index 句，默认最近那句）。"""
    entries = [(e, 0) if isinstance(e, str) else (e[0], int(e[1] or 0)) for e in entries]
    if not entries:
        return _card(_header("↑ 最近问过", "blue"), [{"tag": "markdown", "content":
                     f"还没有记录。你在{where}问过的话会记在这台电脑上（只存本机），"
                     "下次点悬浮栏的「↑ 最近」或者打一个「↑」，就能一键再问。"}])
    index = max(0, min(index, len(entries) - 1))
    text, at = entries[index]
    sub = f"第 {index + 1} / {len(entries)} 句" + (f" · {ago(at, now)}" if at else "")
    elements = [_picker(entries, now)]
    if warn:
        # 回车就发 ⇒ 这一下就是唯一的确认。不拦，但把会发生什么写在框的正上方（第 27 轮 RISK）
        elements.append({"tag": "markdown", "content": f"⚠️ {warn}"})
    return _card(_header("↑ 最近问过", "orange" if warn else "blue", sub), elements + [
        _box(text),
        {"tag": "markdown", "text_size": "notation",
         "content": "点一下框，回车就发（可以先改）。要别的句子从上面的下拉换。"},
    ])


def build_edit_card(items: list[str], index: int, why: str = "") -> dict:
    """兼容旧调用：现在「改一改」就是同一张卡（框本来就能改）。"""
    return build_card(items, index)


def build_sent_card(text: str, at: Optional[float] = None, queued: bool = False) -> dict:
    """发完原地换成这张：写清发的哪句、几点发的。🔴 上面**没有下拉** —— 再点开回车就是重发
    （客户端选同一项也会再触发回调）。"""
    when = time.strftime("%H:%M", time.localtime(at or time.time()))
    note = "前面还有活在跑，这句排上了" if queued else ""
    elements = [{"tag": "markdown", "content": text}]
    if note:
        elements.append({"tag": "markdown", "text_size": "notation", "content": note})
    elements.append(_row(
        _cbtn("↑ 再选一句", {"act": "hist", "op": "list"}, primary=True),
        _cbtn("✏️ 改一改再发", {"act": "hist", "op": "edit", "h": fingerprint(text)}),
        _cbtn("⏹ 停下", {"act": "do", "op": "stop"}),
    ))
    return _card(_header(f"✅ 已发 · {when}", "green"), elements)


# 全授权档下，这些意思的句子一回车就会真去改东西 ⇒ 退回「先填进框、再点发送」多一步确认。
# 其它档位钩子会弹确认卡，已经有第二次确认，不用再拦。误伤的代价只是多点一下。
_WRITE_WORDS = re.compile(
    r"删|移除|清空|清理|覆盖|重命名|改成|改掉|修改|替换|提交|推送|推一下|推到|合并|部署|发布|上线|"
    r"重启|关掉|停掉|杀掉|卸载|安装|升级|回滚|格式化|"
    r"\b(rm|mv|commit|push|merge|deploy|release|reset|drop|delete|kill|install|uninstall)\b",
    re.IGNORECASE)


def looks_like_write(text: str) -> bool:
    return bool(_WRITE_WORDS.search(text or ""))


def picked_fingerprint(action) -> str:
    """下拉选中的那句的指纹（回调 `action.option`）。"""
    raw = getattr(action, "option", None)
    return raw if isinstance(raw, str) else ""


def submitted_text(action) -> Optional[str]:
    """从卡片回调里拿出输入框里的字（他可能改过）。拿不到 ⇒ None。"""
    form = getattr(action, "form_value", None) or {}
    if isinstance(form, dict):
        raw = form.get(INPUT_NAME)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()[:MAX_CHARS]
    raw = getattr(action, "input_value", None)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:MAX_CHARS]
    return None
