'''Claude 会话群输入框上方的快捷工具栏（第 37 轮）：建按钮、绑到群、收点击。

契约见 `docs/r37/contract-toolkit.md`「桥：工具栏供给 + 轮询」。这里只做三件事：

1. **供给**：`GET /im/v1/toolkits` 列出本应用的组件 → 按 `action_key` + 我们自己的 webhook 认出自己的 →
   缺的 `POST` 建（`toolkit_type="call_back"`）→ 落盘 `~/.carher-feishu-agent/toolkit.json`（0600）。
   按钮表 + webhook 基址的指纹没变、5 个 id 都在 ⇒ 一个请求都不发。
2. **补绑**：`chat_binds.json` 里 `tool == "claude"` 且 `on` 的群 ⇒ `PUT /im/v1/chats/:id {"toolkit_ids": 全量}`。
   替换还是追加 11-16 没验证 ⇒ 每次都发全量。绑过同一套的跳过；单个群失败就跳过它。
3. **轮询**：飞书点击只能回调公网 webhook（`call_back` 不填 webhook ⇒ 232001），而且回调**没有签名头**
   ⇒ 198 上的收件箱 `/her-tk/` 按随机 `hook_id` 收下，桥带 `poll_key` 长轮询取走。
   防伪造只靠 hook_id 保密 + 这里核 payload 形状（action 在表里、字段齐、log_id 去重、120 秒内）。

🔴 绝不让桥崩：所有入口都不抛。没 `im:chat`（99991672）⇒ 记一行 info，10 分钟后再试。
🔴 hook_id 先落盘再建按钮：落盘失败就不建 —— 不然每次重启换一个 hook_id，飞书上多 5 个按钮。
'''

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Optional

logger = logging.getLogger("feishu-bridge.fleet")

STATE_NAME = "toolkit.json"
INBOX_BASE = "https://chat.auto-link.com.cn/her-tk"
WEBHOOK_BASE = INBOX_BASE + "/hook/"
# 按钮表（唯一真相）。顺序 = 工具栏上的顺序 = PUT 的 toolkit_ids 顺序。
TOOLKIT_BUTTONS = (
    ("stop", "⏹ 停下"),
    ("go_on", "▶️ 接着干"),
    ("model", "🧠 模型"),
    ("progress", "📜 进度"),
    ("changes", "📂 改动"),
)
ACTIONS = frozenset(key for key, _ in TOOLKIT_BUTTONS)
# 飞书「没这个 scope」。GET /im/v1/toolkits 只认 `im:chat`（11-16 实测）。
PERMISSION_CODES = frozenset({99991672, 99991679})

FIRST_DELAY_SEC = 15
RETRY_EVERY_SEC = 600           # 没权限 / 飞书报错 ⇒ 10 分钟后再试
POLL_TIMEOUT_SEC = 30           # 收件箱最多挂 25 秒
BACKOFF_MIN_SEC = 2
BACKOFF_MAX_SEC = 300
CLICK_MAX_AGE_SEC = 120         # 收件箱只存 120 秒；比这老的不是这一下点的
SEEN_MAX = 512
LIST_PAGES_MAX = 10

OK, NO_PERM, FAILED = "ok", "no_perm", "failed"

_HOOK_RE = re.compile(r"^[0-9a-f]{32}$")
_CHAT_RE = re.compile(r"^oc_[A-Za-z0-9]{1,64}$")
_OPEN_RE = re.compile(r"^ou_[A-Za-z0-9]{1,64}$")
_ID_RE = re.compile(r"^[0-9]{1,24}$")


@dataclass(frozen=True)
class ToolbarClick:
    action: str
    chat_id: str
    open_id: str
    tenant_key: str
    log_id: str
    at: float


def fingerprint() -> str:
    """按钮表 + webhook 基址。任何一个变了 ⇒ 重新认 / 建按钮、所有群重绑。"""
    material = json.dumps({"buttons": TOOLKIT_BUTTONS, "webhook": WEBHOOK_BASE},
                          ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]


def _code(out: Any) -> int:
    try:
        return int((out or {}).get("code") or 0) if isinstance(out, dict) else -1
    except (TypeError, ValueError):
        return -1


def _toolkit_id(value: Any) -> str:
    text = str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
    return text if _ID_RE.match(text) else ""


def _created_id(out: Any) -> str:
    """POST 回包里的 toolkit_id。形状没文档 ⇒ `data.toolkit_id` 和 `data.toolkit.toolkit_id` 都认。"""
    data = (out or {}).get("data") if isinstance(out, dict) else None
    if not isinstance(data, dict):
        return ""
    inner = data.get("toolkit") if isinstance(data.get("toolkit"), dict) else {}
    return _toolkit_id(data.get("toolkit_id")) or _toolkit_id(inner.get("toolkit_id"))


def parse_click(item: Any, now: float) -> Optional[ToolbarClick]:
    """收件箱里的一条 `{"at": epoch, "body": 飞书原 JSON}` → ToolbarClick；不合格 ⇒ None。不管去重。"""
    if not isinstance(item, dict) or not isinstance(item.get("body"), dict):
        return None
    try:
        at = float(item.get("at"))
    except (TypeError, ValueError):
        return None
    if abs(now - at) > CLICK_MAX_AGE_SEC:
        return None
    body = item["body"]
    info = body.get("action_info") if isinstance(body.get("action_info"), dict) else {}
    extra = body.get("extra") if isinstance(body.get("extra"), dict) else {}
    action, chat, who = body.get("action_key"), info.get("open_chat_id"), info.get("open_id")
    tenant, log_id = info.get("tenant_key"), extra.get("log_id")
    if action not in ACTIONS:
        return None
    if not (isinstance(chat, str) and _CHAT_RE.match(chat)):
        return None
    if not (isinstance(who, str) and _OPEN_RE.match(who)):
        return None
    if not (isinstance(tenant, str) and 0 < len(tenant) <= 64):
        return None
    if not (isinstance(log_id, str) and 0 < len(log_id) <= 128):
        return None
    return ToolbarClick(action=action, chat_id=chat, open_id=who, tenant_key=tenant, log_id=log_id, at=at)

# ---------- 落盘：{app_id: {hook_id, poll_key, fingerprint, ids:{action: id}, bound:{chat_id: fp}}} ----------


class _State:
    """只认自己 app_id 那一格；写的时候保留别的 app_id（同一台机器换过应用）。"""

    def __init__(self, path: str, app_id: str) -> None:
        self.path = path
        self.app_id = str(app_id or "")

    def _read_all(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001 —— 坏了 = 当没有（hook_id 会重新生成）
            logger.info("工具栏状态读不出来，当没有", exc_info=True)
            return {}

    def load(self) -> dict:
        raw = self._read_all().get(self.app_id)
        raw = raw if isinstance(raw, dict) else {}
        hook, key = raw.get("hook_id"), raw.get("poll_key")
        ids = raw.get("ids") if isinstance(raw.get("ids"), dict) else {}
        bound = raw.get("bound") if isinstance(raw.get("bound"), dict) else {}
        return {
            "hook_id": hook if isinstance(hook, str) and _HOOK_RE.match(hook) else "",
            "poll_key": key if isinstance(key, str) and 32 <= len(key) <= 128 else "",
            "fingerprint": str(raw.get("fingerprint") or ""),
            "ids": {a: _toolkit_id(v) for a, v in ids.items() if a in ACTIONS and _toolkit_id(v)},
            "bound": {c: str(f) for c, f in bound.items() if isinstance(c, str) and _CHAT_RE.match(c)},
        }

    def save(self, mine: dict) -> bool:
        if not self.app_id:
            return False
        data = self._read_all()
        data[self.app_id] = mine
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".toolkit.", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
                os.chmod(name, 0o600)
                os.replace(name, self.path)
            except Exception:
                try:
                    os.unlink(name)
                except OSError:
                    pass
                raise
            return True
        except Exception:  # noqa: BLE001
            logger.info("工具栏状态写不进去：%s", self.path, exc_info=True)
            return False


# ---------- HTTP（两条都注入；默认实现不抛）----------


def lark_feishu(client) -> Callable[[str, str, Any], dict]:
    """`(method, path, body) -> dict`：走桥现成的 lark client（tenant token 由 SDK 自己换、自己缓存）。

    path 形如 `/im/v1/toolkits?page_size=20`（不带 `/open-apis`，同 groupsetup）；飞书 4xx 也是 JSON（含 99991672），照样解析出来。
    """

    def call(method: str, path: str, body: Any = None) -> dict:
        try:
            import lark_oapi as lark

            uri, _, query = path.partition("?")
            builder = (lark.BaseRequest.builder()
                       .http_method(getattr(lark.HttpMethod, method))
                       .uri("/open-apis" + uri)
                       .token_types({lark.AccessTokenType.TENANT})
                       .headers({"Content-Type": "application/json; charset=utf-8"})
                       .queries(urllib.parse.parse_qsl(query)))
            if body is not None:
                builder = builder.body(body)
            resp = client.request(builder.build())
            raw = getattr(getattr(resp, "raw", None), "content", b"") or b""
            try:
                out = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                out = {}
            if not isinstance(out, dict) or "code" not in out:
                out = {"code": getattr(resp, "code", -1) or 0, "msg": str(getattr(resp, "msg", "") or "")}
            return out
        except Exception as exc:  # noqa: BLE001
            logger.info("工具栏调飞书没通 %s %s：%s", method, path.split("?", 1)[0], exc)
            return {"code": -1, "msg": "网络不通"}

    return call


def urllib_inbox(method: str, url: str, headers: dict, body: Any, timeout: float) -> tuple:
    """`-> (http_status, json_dict)`；网络错误 ⇒ `(0, {})`。198 收件箱用。"""
    import urllib.error
    import urllib.request

    data = json.dumps(body).encode("utf-8") if body is not None else None
    hdrs = {"Content-Type": "application/json", "User-Agent": "her-remote-toolkit/1"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read(1 << 20)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, b""
    except Exception as exc:  # noqa: BLE001
        logger.info("工具栏收件箱没通：%s", exc)
        return 0, {}
    try:
        out = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        out = {}
    return status, out if isinstance(out, dict) else {}

# ---------- 供给 + 补绑 + 轮询 ----------


def _as_int_ids(ids) -> list:
    """SDK 的 `toolkit_ids` 是 `List[int]`、GET 群回读也是 int ⇒ 发给飞书时转成 int。"""
    return [int(x) for x in ids if _toolkit_id(x)]


class Toolkit:
    """一台桥一个。所有公开方法都不抛。

    - `feishu(method, path, body) -> dict`：调开放平台（tenant token），默认 `lark_feishu(client)`。
    - `inbox(method, url, headers, body, timeout) -> (status, dict)`：调 198 收件箱，默认 `urllib_inbox`。
    """

    def __init__(self, app_id: str, home: str, *, feishu: Callable[..., dict],
                 inbox: Callable[..., tuple] = urllib_inbox,
                 on_click: Optional[Callable[[ToolbarClick], None]] = None,
                 now: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.app_id = str(app_id or "")
        self.home = home
        self._state = _State(os.path.join(home, STATE_NAME), self.app_id)
        self._feishu = feishu
        self._inbox = inbox
        self._on_click = on_click
        self._now = now
        self._sleep = sleep
        self._lock = threading.RLock()
        self._mine = self._state.load()
        self._seen: "OrderedDict[str, float]" = OrderedDict()
        self.registered = False

    # ----- 状态 -----

    def webhook(self) -> str:
        return WEBHOOK_BASE + self._mine["hook_id"] if self._mine.get("hook_id") else ""

    def ensure_hook(self) -> bool:
        """没有 hook_id / poll_key 就生成并**先落盘**。落不下来 ⇒ False（别建按钮）。"""
        with self._lock:
            if self._mine["hook_id"] and self._mine["poll_key"]:
                return True
            self._mine.update(hook_id=secrets.token_hex(16), poll_key=secrets.token_urlsafe(32),
                              fingerprint="", ids={}, bound={})
            return self._state.save(self._mine)

    def ids(self) -> list:
        """5 个都在且是当前指纹 ⇒ 按按钮表顺序的 id 列表；否则 []（不给半套）。"""
        with self._lock:
            ids = self._mine.get("ids") or {}
            if self._mine.get("fingerprint") != fingerprint() or any(a not in ids for a in ACTIONS):
                return []
            return [ids[a] for a, _ in TOOLKIT_BUTTONS]

    # ----- 供给 -----

    def _list_mine(self) -> tuple:
        """GET 列出 → `(status, {action: id})`。认自己的：webhook 完全等于本机的、action 在表里、名字是当前的。"""
        hook, labels, found, token = self.webhook(), dict(TOOLKIT_BUTTONS), {}, ""
        for _ in range(LIST_PAGES_MAX):
            path = "/im/v1/toolkits?page_size=50" + (f"&page_token={urllib.parse.quote(token)}" if token else "")
            out = self._feishu("GET", path, None)
            code = _code(out)
            if code in PERMISSION_CODES:
                return NO_PERM, {}
            if code:
                logger.info("工具栏列不出来 code=%s msg=%s", code, str((out or {}).get("msg") or "")[:120])
                return FAILED, {}
            data = out.get("data") if isinstance(out.get("data"), dict) else {}
            for item in data.get("items") or []:
                if not isinstance(item, dict):
                    continue
                cb = item.get("callback") if isinstance(item.get("callback"), dict) else {}
                action, tid = cb.get("action_key"), _toolkit_id(item.get("toolkit_id"))
                if (tid and action in ACTIONS and cb.get("webhook") == hook
                        and item.get("toolkit_name") == labels[action] and action not in found):
                    found[action] = tid
            token = str(data.get("page_token") or "")
            if not data.get("has_more") or not token:
                break
        return OK, found

    def provision(self) -> str:
        """保证 5 个按钮在飞书上都有、id 落盘。指纹对上 + 5 个都在 ⇒ 不发请求。"""
        try:
            if not self.ensure_hook():
                return FAILED
            fp = fingerprint()
            with self._lock:
                if self.ids():
                    return OK
                changed = self._mine.get("fingerprint") != fp
            status, found = self._list_mine()
            if status != OK:
                if status == NO_PERM:
                    logger.info("快捷工具栏要 im:chat 权限，还没开；%d 分钟后再试", RETRY_EVERY_SEC // 60)
                return status
            for action, label in TOOLKIT_BUTTONS:
                if action in found:
                    continue
                out = self._feishu("POST", "/im/v1/toolkits", {
                    "toolkit_name": label, "toolkit_type": "call_back",
                    "callback": {"webhook": self.webhook(), "action_key": action}})
                code = _code(out)
                tid = _created_id(out) if not code else ""
                if code in PERMISSION_CODES:
                    logger.info("建工具栏按钮没权限（im:chat）；%d 分钟后再试", RETRY_EVERY_SEC // 60)
                    status = NO_PERM
                    break
                if not tid:
                    logger.info("建工具栏按钮 %s 没成 code=%s msg=%s", action, code,
                                str((out or {}).get("msg") or "")[:120])
                    status = FAILED
                    break
                found[action] = tid
            with self._lock:
                # 建成一半也落盘：下次 GET 能认回来，但不重复 POST 的保险在这里多一层
                self._mine["ids"] = dict(found)
                if status == OK:
                    self._mine["fingerprint"] = fp
                    if changed:
                        self._mine["bound"] = {}   # 按钮换了 ⇒ 所有群重绑
                self._state.save(self._mine)
            return status
        except Exception:  # noqa: BLE001
            logger.info("工具栏供给出错", exc_info=True)
            return FAILED

    # ----- 补绑 -----

    def _claude_chats(self) -> list:
        """chat_binds.json 里 tool == claude 且 on 的群（只读）。"""
        try:
            from .chatbind import ChatBinds

            binds = ChatBinds(os.path.join(self.home, "chat_binds.json"))
            return [b.chat_id for b in binds.all() if b.tool == "claude" and b.on]
        except Exception:  # noqa: BLE001
            logger.info("工具栏读不出群绑定", exc_info=True)
            return []

    def bind_chat(self, chat_id: str, *, force: bool = False) -> str:
        ids = self.ids()
        if not ids or not (isinstance(chat_id, str) and _CHAT_RE.match(chat_id)):
            return FAILED
        fp = fingerprint()
        with self._lock:
            if not force and self._mine["bound"].get(chat_id) == fp:
                return OK
        out = self._feishu("PUT", f"/im/v1/chats/{chat_id}", {"toolkit_ids": _as_int_ids(ids)})
        code = _code(out)
        if code in PERMISSION_CODES:
            return NO_PERM
        if code:
            logger.info("群 %s… 绑工具栏没成 code=%s msg=%s", chat_id[:10], code,
                        str((out or {}).get("msg") or "")[:120])
            return FAILED
        with self._lock:
            self._mine["bound"][chat_id] = fp
            self._state.save(self._mine)
        return OK

    def bind_all(self) -> dict:
        """老群补绑。单个失败跳过；没权限就整轮停（后面的也一样会被拒）。"""
        got: dict = {}
        try:
            if not self.ids():
                return got
            for chat in self._claude_chats():
                got[chat] = self.bind_chat(chat)
                if got[chat] == NO_PERM:
                    break
        except Exception:  # noqa: BLE001
            logger.info("工具栏补绑出错", exc_info=True)
        return got

    def ensure_bound(self, chat_id: str) -> str:
        """建群之后调：只绑 chat_binds 里 claude + on 的群。"""
        try:
            if chat_id not in self._claude_chats():
                return FAILED
            return self.bind_chat(chat_id)
        except Exception:  # noqa: BLE001
            logger.info("工具栏绑新群出错", exc_info=True)
            return FAILED

    def provision_loop(self, first_delay: float = FIRST_DELAY_SEC) -> None:
        """供给 → 补绑；没成就 10 分钟后再来。成了就退出线程。"""
        self._sleep(first_delay)
        while True:
            try:
                if self.provision() == OK:
                    self.bind_all()
                    return
            except Exception:  # noqa: BLE001
                logger.info("工具栏供给线程出错", exc_info=True)
            self._sleep(RETRY_EVERY_SEC)

    # ----- 轮询 -----

    def register(self) -> int:
        """→ HTTP 状态（0 = 网络不通）。"""
        if not self.ensure_hook():
            return 0
        key_hash = hashlib.sha256(self._mine["poll_key"].encode("utf-8")).hexdigest()
        status, out = self._inbox("POST", INBOX_BASE + "/register", {},
                                  {"hook_id": self._mine["hook_id"], "key_hash": key_hash}, 15)
        self.registered = status == 200 and bool(out.get("ok"))
        if status == 409:
            logger.warning("工具栏收件箱说这个 hook 已被别的 key 注册（本机状态可能被还原过）")
        return status

    def accept(self, item: Any) -> Optional[ToolbarClick]:
        """校验 + 去重。合格的才回调。"""
        click = parse_click(item, self._now())
        if click is None:
            logger.info("工具栏收到一条不合格的点击，丢掉")
            return None
        with self._lock:
            if click.log_id in self._seen:
                return None
            self._seen[click.log_id] = click.at
            while len(self._seen) > SEEN_MAX:
                self._seen.popitem(last=False)
        if self._on_click is not None:
            try:
                self._on_click(click)
            except Exception:  # noqa: BLE001
                logger.info("工具栏点击处理出错", exc_info=True)
        return click

    def poll_once(self) -> int:
        """一次长轮询 → HTTP 状态（0 = 网络不通）。401 ⇒ 标记要重新 register。"""
        url = INBOX_BASE + "/poll?hook_id=" + self._mine["hook_id"]
        status, out = self._inbox("GET", url, {"Authorization": "Bearer " + self._mine["poll_key"]},
                                  None, POLL_TIMEOUT_SEC)
        if status == 401:
            self.registered = False
        elif status == 200:
            for item in (out.get("clicks") or [])[:100]:
                self.accept(item)
        return status

    def poll_loop(self, *, rounds: Optional[int] = None) -> None:
        """register → 长轮询；断网指数退避（≤ 5 分钟）。`rounds` 只给测试用。"""
        backoff, n = BACKOFF_MIN_SEC, 0
        while rounds is None or n < rounds:
            n += 1
            try:
                if not self.registered:
                    status = self.register()
                    if not self.registered:
                        self._sleep(RETRY_EVERY_SEC if status == 409 else backoff)
                        backoff = min(backoff * 2, BACKOFF_MAX_SEC)
                        continue
                status = self.poll_once()
                if status == 200:
                    backoff = BACKOFF_MIN_SEC
                    continue
                if status != 401:
                    self._sleep(backoff)
                    backoff = min(backoff * 2, BACKOFF_MAX_SEC)
            except Exception:  # noqa: BLE001
                logger.info("工具栏轮询出错", exc_info=True)
                self._sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_SEC)


# ---------- 对外接口（app.py 调；契约 docs/r37/contract-toolkit.md）----------

_lock = threading.Lock()
_active: Optional[Toolkit] = None
_started = False


def start(client, app_id: str, home: str, on_click: Callable[[ToolbarClick], None]) -> None:
    """后台线程，不阻塞，不抛。进程内只起一次。"""
    global _active, _started
    try:
        with _lock:
            if _started:
                return
            _started = True
        kit = Toolkit(app_id, home, feishu=lark_feishu(client), on_click=on_click)
        with _lock:
            _active = kit
        if not kit.ensure_hook():
            logger.info("工具栏状态落不了盘，这次不起")
            return
        threading.Thread(target=kit.provision_loop, daemon=True, name="toolkit-provision").start()
        threading.Thread(target=kit.poll_loop, daemon=True, name="toolkit-poll").start()
    except Exception:  # noqa: BLE001
        logger.info("快捷工具栏没起来", exc_info=True)


def ensure_bound(chat_id: str) -> None:
    """建群后调（后台）。没起来 / 按钮还没建好 ⇒ 什么都不做（启动补绑会兜）。"""
    try:
        kit = _active
        if kit is None or not kit.ids():
            return
        threading.Thread(target=kit.ensure_bound, args=(chat_id,), daemon=True,
                         name="toolkit-bind").start()
    except Exception:  # noqa: BLE001
        logger.info("工具栏绑新群没起来", exc_info=True)


def toolkit_ids() -> list:
    """建群 body 用；没有 ⇒ []。"""
    try:
        kit = _active
        return list(kit.ids()) if kit is not None else []
    except Exception:  # noqa: BLE001
        return []
