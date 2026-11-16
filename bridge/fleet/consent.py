"""「进对话模式」的一次性确认，记 24 小时。

## 为什么要确认这一下

进对话模式之后，用户在手机上打的每一句都**直接进电脑上那个会话**，由那个会话用
**它自己的权限**去做 —— 用户在电脑上给它的是「不问就改」（实测用户 21 个终端会话
全是 `bypassPermissions`），手机入口也会按当前权限档位工作；确认卡只确认你要把消息送进这个电脑会话。
这和桥自己跑一轮是两件完全不同的事，所以要像 `menu.build_confirm_loosen_card`
那样明确问一次：误点一下的后果是「它不问你就动手」，而用户不会收到任何提示。

## 为什么记 24 小时而不是永久

对话模式是连续多轮的，每句都弹卡等于没法用；但权限这种事也不该一次点击管一辈子 ——
隔天再进来，电脑上那个会话可能已经是另一个人、另一个项目了。24 小时是 spec 定的。

## 为什么落盘

看门狗在长连接静默时**会自杀等重启**（lark-oapi #169 的唯一保险），所以「重启」在这套
东西里是常态。只放内存 ⇒ 用户每被重启一次就要重新点一次确认，而他根本不知道发生了什么。
写法抄 `prefs.py`：临时文件 + `os.replace()`，半截 JSON 下次读不出来会把确认全丢掉。

## 读失败怎么办

**当成没确认过**（再问一次）。这个方向是安全的：多问一次只是啰嗦，少问一次是
用户没同意就让远端会话动了手。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger("feishu-bridge.fleet")

TTL_SEC = 24 * 60 * 60
MAX_ENTRIES = 200  # 够用了；超了清最旧的，别让文件无限长


def _key(scope: str, tool: str, sid: str, ident: str = "") -> str:
    """一条确认属于「哪个聊天 + 哪个会话 + 这个会话当时是什么样」。

    带上 scope 是故意的：同一台电脑上的会话，在群里点过确认不代表单聊里也确认过
    （群里能看到的人更多）。

    🔴 还要带 `ident`（名字 + 工作目录）。2026-11-04 安全审查：只按 sessionId 记时，
    用户在电脑上给那个会话改了名、或者把它切到另一个项目接着用，sessionId 不变 ⇒
    隔十几小时再点 [接着聊] **一张确认卡都不弹**，而确认卡上写的还是旧标题，
    正文却进了改过名、换过项目的那个会话。名字或目录变了就当新会话，重新问一次。
    """
    return "\n".join((str(scope or ""), str(tool or ""), str(sid or ""), str(ident or "")))


# 「电脑上没开着 ⇒ 在后台接着跑」那条路的标记，拼在指纹末尾。
# 🔴 2026-11-06（角色 F）：关掉 / 打开会话不改名字也不改目录，但**换了一条路、换了一套权限**：
#   开着 ⇒ 送进电脑上那个会话，用它在电脑上那套权限（多半不问就改）
#   关着 ⇒ 桥在后台接着跑，用手机这边的档位
# 用户确认的是卡上写的那一套。路变了 = 确认的不是这回事 ⇒ 指纹必须变。
# 开着的那条**不加后缀**：升级前记下的确认（名字|目录）对开着的会话照样有效，不让所有人重点一遍。
BACKGROUND_TAG = "后台接着跑"


def session_ident(name: str = "", cwd: str = "", background: bool = False) -> str:
    """会话身份指纹：名字 + 工作目录真实路径（+ 走后台那条路时的标记）。任一变化都要重新确认。"""
    import os

    try:
        real = os.path.realpath(os.path.expanduser(str(cwd or ""))) if cwd else ""
    except (OSError, ValueError):
        real = str(cwd or "")
    base = f"{str(name or '').strip()}|{real}"
    return f"{base}|{BACKGROUND_TAG}" if background else base


def split_ident(ident: str) -> tuple:
    """`session_ident` 的反向 → (名字, 目录, 是否走后台)。只给「哪里变了」那句文案用，不做闸门。"""
    text = str(ident or "")
    background = text.endswith("|" + BACKGROUND_TAG)
    if background:
        text = text[: -len(BACKGROUND_TAG) - 1]
    name, _, cwd = text.rpartition("|")
    return name, cwd, background


class RelayConsent:
    """谁在哪个会话上点过「确认进入」，24 小时内不再问。"""

    def __init__(self, path: str, *, ttl: float = TTL_SEC,
                 now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._ttl = float(ttl)
        self._now = now
        self._lock = threading.Lock()
        self._at: dict = {}
        self.last_save_ok = True
        self._load()

    # ---------- 落盘 ----------

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return  # 没有 / 读不出来 ⇒ 当成没确认过（再问一次，方向是安全的）
        if not isinstance(data, dict):
            return
        got = {}
        for key, stamp in (data.get("granted") or {}).items():
            try:
                when = float(stamp)
            except (TypeError, ValueError):
                continue
            if isinstance(key, str) and when > 0:
                got[key] = when
        with self._lock:
            self._at = got

    def _save(self) -> bool:
        with self._lock:
            payload = {"granted": dict(self._at)}
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory or None, delete=False
            ) as tmp:
                json.dump(payload, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self._path)
            self.last_save_ok = True
            return True
        except OSError:
            # 存不下来不影响这一次（内存里已经记住了），只是重启后要再问一次
            logger.exception("对话模式的确认没存下来：%s", self._path)
            self.last_save_ok = False
            return False

    # ---------- 对外 ----------

    def granted(self, scope: str, tool: str, sid: str, ident: str = "") -> bool:
        key = _key(scope, tool, sid, ident)
        with self._lock:
            when = self._at.get(key, 0.0)
        if not when:
            return False
        if self._now() - when >= self._ttl:
            with self._lock:
                self._at.pop(key, None)
            return False
        return True

    def latest_ident(self, scope: str, tool: str, sid: str) -> Optional[str]:
        """这个聊天在这个会话上最近一次（没过期的）确认时，它是什么样。没有 ⇒ None。

        只给「它哪里变了」那句话用（重启后内存里没有，靠盘上这份说清楚为什么又要确认）。
        """
        prefix = _key(scope, tool, sid, "")
        now = self._now()
        with self._lock:
            hits = [(when, key) for key, when in self._at.items()
                    if key.startswith(prefix) and now - when < self._ttl]
        if not hits:
            return None
        return max(hits)[1][len(prefix):]

    def grant(self, scope: str, tool: str, sid: str, ident: str = "") -> None:
        """记下这一次确认，**并作废同一个会话在这个聊天里的旧确认**。

        🔴 2026-11-06（角色 F）：最近一次确认说了算。用户在终端开着时确认过（电脑那套权限），
        后来关掉 ⇒ 又按「后台接着跑、手机这边的权限」确认了一次 ⇒ 再打开时，旧的那条
        不该还算数 —— 他最后看到、点过的是「手机这边的权限」那张卡。改名改回去同理。
        """
        prefix = _key(scope, tool, sid, "")
        with self._lock:
            for key in [k for k in self._at if k.startswith(prefix)]:
                self._at.pop(key, None)
            self._at[_key(scope, tool, sid, ident)] = float(self._now())
            self._prune_locked()
        self._save()

    def revoke(self, scope: str, tool: str, sid: str) -> None:
        """作废这个聊天在这个会话上的**所有**确认（不管当时它叫什么、在哪个项目、开没开着）。

        原来按不带 ident 的键删 ⇒ 带 ident 记下的确认一条都删不到。
        """
        prefix = _key(scope, tool, sid, "")
        with self._lock:
            hit = [k for k in self._at if k.startswith(prefix)]
            for key in hit:
                self._at.pop(key, None)
        if hit:
            self._save()

    def _prune_locked(self) -> None:
        now = self._now()
        for key, when in list(self._at.items()):
            if now - when >= self._ttl:
                self._at.pop(key, None)
        if len(self._at) > MAX_ENTRIES:
            for key in sorted(self._at, key=lambda k: self._at[k])[: len(self._at) - MAX_ENTRIES]:
                self._at.pop(key, None)
