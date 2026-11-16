"""「每个会话一个群」真正调飞书的那一层：建群、改群名、拉主人进群、查群还在不在、开权限体检。

## 分工（别越界）

- `chatbind.py` 管**落盘绑定**和幂等（点两次不建两个群），它只认一个注入进来的 client。
- 这个文件就是那个 client：`GroupSetup` 实现 `chatbind.GroupClient` 的约定
  （`create` / `exists`），外加 `rename` / `add_owner`。
- HTTP 那一下也是注入的（`http=`）⇒ 测试里一个群都不会真建出来（spec 安全红线）。

## 开权限之前它会怎样（2026-11-05 实测，app 是本机这台）

| 要做的事 | 接口 | 现在 | 缺什么（飞书原话里的 scope，开其中**任一**即可）|
|---|---|---|---|
| 往群里发消息 | POST /im/v1/messages | ✅ 已有 | —（`im:message:send_as_bot` 已开）|
| 查群还在不在 | GET /im/v1/chats/:id | ❌ 99991672 | im:chat:read / im:chat:readonly / im:chat |
| 改群名 | PUT /im/v1/chats/:id | ❌ 99991672 | im:chat:update / im:chat |
| 拉主人进群 | POST /im/v1/chats/:id/members | ❌ 99991672 | im:chat.members:write_only / im:chat |
| 建群 | POST /im/v1/chats | ❌ 99991672（主会话实测）| im:chat:create / im:chat |
| 取「点一下进群」的链接 | POST /im/v1/chats/:id/link | ✅ 已有（开完权限后实测 code 0）| 跟着上面那几项一起开的 |
| 收群里**不 @ 机器人**的话 | 事件 im.message.receive_v1 | ❌ 没开 | im:message.group_msg（敏感权限）|

最后一行是关键：现在只开了单聊的收消息权限。「群里说的每一句都直达」要的是**群里所有消息**，
只开「@机器人的消息」的话，主人每句都得先 @ 它 —— 那就不是用户要的东西了。

## 体检为什么能做到「只读」

查改群名、拉人这几个能力时，故意对一个**不存在的群**（全 0 的 chat_id）发请求：
飞书先查权限再查群 —— 没权限回 `99991672` 并列出缺的 scope；有权限才轮到「群不存在」
（232006 之类）。两种情况都**什么都改不了**，因为那个群根本不存在。

🔴 **建群不在体检里**。建群接口要是真有权限，一个合法请求就会真建出一个群（外部可见、不可逆）。
用「故意填错参数」去试，飞书是先校验参数还是先建群，文档没写、也没法在没权限时实测 ⇒ 不赌。
建群和收群消息这两项，能读到应用权限列表时照列表判，读不到就照实说「要开完之后点一次才知道」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("feishu-bridge.fleet")

BASE = "https://open.feishu.cn/open-apis"
TIMEOUT = 15

# 飞书说「没这个权限」的码。99991672 = 应用身份缺 scope（实测）；99991679 = 用户身份缺 scope。
PERMISSION_CODES = frozenset({99991672, 99991679})
# 群不在了 / chat_id 不对。查群的接口回这几个 ⇒ 当群没了（chatbind 会忘掉这条、重建）。
# 232006 无效的 chat_id、232009 群已解散、232011 机器人不在群里了（被踢 ⇒ 收不到群消息，等于没了）。
GONE_CODES = frozenset({232006, 232009, 232011})

# 体检用的「不存在的群」。形状照真的 chat_id（oc_ + 32 位十六进制），免得先被格式校验挡掉、看不到权限那一层。
PROBE_CHAT_ID = "oc_" + "0" * 32
PROBE_OPEN_ID = "ou_" + "0" * 32

# 群名上限：建群文档原话「建议群名称不超过 60 字符」。chatbind 自己会压到 24，这里是兜底。
MAX_GROUP_NAME = 60
# 建群去重键：文档原话「持有相同 uuid + owner_id 的请求 10 小时内只可成功创建 1 个群聊」，最长 50。
UUID_MAX = 50
# 去重窗口。手抖连点两下（同一分钟里）只建一个；群被解散后隔一会儿再点能重建。
DEDUPE_BUCKET_SEC = 60

# 体检结果缓存多久。权限是用户在后台点出来的、几分钟才变一次；而体检要打 5 次飞书接口，
# 建一次群跑一遍太慢。开完权限最多这么久之后文案就跟上了。
REPORT_TTL_SEC = 300

# 入群链接的有效期。文档只认这三档（没有「一天」）。默认一周：链接是秒级用掉的，
# 不留永久 —— 它等于「谁拿到谁能进这个能看见源码的私有群」。
LINK_VALIDITY = ("week", "year", "permanently")
DEFAULT_LINK_VALIDITY = "week"

# ---------- 要开的权限（给人看的版本）----------
#
# 每项：(能力 key, 中文名, 推荐开的那一个 scope, 飞书后台里显示的权限名, 也能顶用的别的 scope)
# 推荐的那个都是**最窄**的一项：管理员审批时看得懂、批得快（文档「最小权限原则」）。
# `im:chat`（获取与更新群组信息）一项就能顶掉前四项，图省事可以只开它，但它也能改所有群的信息。
NEEDS = (
    ("create", "建群", "im:chat:create", "创建群", ("im:chat",)),
    ("rename", "改群名", "im:chat:update", "更新群信息", ("im:chat",)),
    ("add_owner", "把你拉进群", "im:chat.members:write_only", "添加、移除群成员", ("im:chat",)),
    ("read", "看群还在不在", "im:chat:read", "查看群信息", ("im:chat:readonly", "im:chat")),
    ("receive", "收群里你说的每一句（不用 @ 它）", "im:message.group_msg", "获取群组中所有消息",
     ("im:message.group_msg:readonly",)),
    ("send", "往群里回消息", "im:message:send_as_bot", "以应用的身份发消息",
     ("im:message", "im:message:send")),
)
NEED_KEYS = tuple(n[0] for n in NEEDS)


def _http_default(method: str, path: str, token: Optional[str], body: Any = None) -> dict:
    """标准库 HTTP（同 `botmenu._http`）。**不抛**：网络错误也包成 `{"code": -1}`。"""
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        # 飞书的业务错误（含 99991672）是带 HTTP 4xx 的 JSON，要读出来，不能当网络错误
        try:
            return json.load(exc)
        except Exception:  # noqa: BLE001
            return {"code": exc.code, "msg": str(exc.reason)}
    except (OSError, ValueError) as exc:
        # 原文只进日志：msg 今天没人上屏，但以后有人拼进句子就会把本机细节发出去（第 11 轮文案门禁）
        logger.warning("调飞书网络不通：%s", exc)
        return {"code": -1, "msg": "网络不通，过一会儿再点一次"}


def _code(out: Any) -> int:
    try:
        return int((out or {}).get("code") or 0) if isinstance(out, dict) else -1
    except (TypeError, ValueError):
        return -1


def missing_scopes(out: Any) -> list:
    """飞书错误信封里列出的缺失 scope（`error.permission_violations[].subject`）。"""
    if not isinstance(out, dict):
        return []
    err = out.get("error") or {}
    if not isinstance(err, dict):
        return []
    got = []
    for item in err.get("permission_violations") or []:
        subject = item.get("subject") if isinstance(item, dict) else ""
        if isinstance(subject, str) and subject and subject not in got:
            got.append(subject)
    return got


def safe_group_name(name: Any) -> str:
    """进飞书之前再洗一遍群名：去换行 / 控制字符 / 零宽字符，压到 60 字。

    🔴 调用方（`chatbind.group_name`）已经脱敏过；这里**不替它脱敏**，只防格式类的坑 ——
    零宽字符会让群名在列表里看起来是空的。洗完是空的就给个兜底名，别建一个「(无主题)」群。
    """
    flat = " ".join(str(name or "").split())
    kept = "".join(ch for ch in flat if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")).strip()
    return kept[:MAX_GROUP_NAME] or "电脑上的会话"


def dedupe_uuid(name: str, owner_open_id: str, now: float) -> str:
    """建群的去重键：同一个名字 + 同一个主人，**同一个分钟桶里**只建一个群。

    ⚠️ 是固定分桶，不是滑动窗口：正好跨在整分钟两边的两下会拿到不同的键。没关系 ——
    「点两次」本来就由 chatbind 先查后建挡住了，这一层只兜「回包丢了又重试」。

    为什么要这一层：`chatbind.ensure_group` 先查后建，它管得住「点两次」；管不住的是
    **请求发出去了、回包丢了**（超时）—— 那时候群其实建好了，我们却不知道，下一次点又建一个。
    飞书的 uuid 去重正好兜这一下。

    🔴 为什么不用「会话 id」当键、为什么要按分钟分桶：飞书对同一个 uuid **10 小时内只建一次**。
    键要是永远不变，主人把群解散了、隔一会儿再点，飞书会拒绝再建（或者回那个已经解散的群）
    ⇒ 「群被解散了要能重建」那条（spec 二 2）就破了。分钟桶 = 只挡手抖和重试，不挡重建。
    """
    bucket = int(float(now) // DEDUPE_BUCKET_SEC)
    raw = f"{owner_open_id}\n{name}\n{bucket}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:UUID_MAX]


class GroupSetup:
    """真实的建群 client。满足 `chatbind.GroupClient` 的约定：**所有方法都不抛**。

    - `http(method, path, token, body) -> dict`：注入点。默认是标准库 HTTP；测试给假的。
    - token 自己换、自己缓存（提前 5 分钟换新的），不和 lark SDK 的 client 抢。
    """

    def __init__(self, app_id: str, app_secret: str, *,
                 http: Callable[..., dict] = _http_default,
                 now: Callable[[], float] = time.time) -> None:
        self._app_id = str(app_id or "")
        self._app_secret = str(app_secret or "")
        self._http = http
        self._now = now
        self._lock = threading.Lock()
        self._token = ""
        self._token_until = 0.0
        self.last_error: dict = {}  # 最近一次失败的 {"code","msg","missing"}，给上层拼说明用
        self._report: Optional[Report] = None
        self._report_at = 0.0

    # ---------- token ----------

    def _tenant_token(self) -> str:
        with self._lock:
            if self._token and self._now() < self._token_until:
                return self._token
        out = self._call("POST", "/auth/v3/tenant_access_token/internal", None,
                         {"app_id": self._app_id, "app_secret": self._app_secret})
        token = out.get("tenant_access_token") if isinstance(out, dict) else ""
        if _code(out) or not token:
            self._remember(out)
            return ""
        try:
            ttl = float(out.get("expire") or 0)
        except (TypeError, ValueError):
            ttl = 0.0
        with self._lock:
            self._token = str(token)
            # 提前 5 分钟换；飞书没给有效期就只用这一次
            self._token_until = self._now() + max(ttl - 300, 0)
        return str(token)

    def _call(self, method: str, path: str, token: Optional[str], body: Any = None) -> dict:
        try:
            out = self._http(method, path, token, body)
        except Exception as exc:  # noqa: BLE001 —— 约定不抛；注入的 http 炸了也不能让点击崩掉
            logger.exception("调飞书抛异常 %s %s", method, path.split("?", 1)[0])
            return {"code": -1, "msg": f"{exc}"}
        return out if isinstance(out, dict) else {"code": -1, "msg": "飞书回的不是 JSON"}

    def _remember(self, out: Any) -> None:
        self.last_error = {"code": _code(out), "msg": str((out or {}).get("msg") or "")[:200]
                           if isinstance(out, dict) else "", "missing": missing_scopes(out)}

    def _authed(self, method: str, path: str, body: Any = None) -> dict:
        token = self._tenant_token()
        if not token:
            return {"code": self.last_error.get("code") or -1,
                    "msg": self.last_error.get("msg") or "拿不到飞书的访问凭证"}
        out = self._call(method, path, token, body)
        if _code(out):
            self._remember(out)
        return out

    # ---------- chatbind.GroupClient 约定 ----------

    def create(self, name: str, owner_open_id: str = "") -> tuple:
        """建一个只有主人和机器人的私有群 → `(chat_id, code, msg)`。

        一次请求里把三件事都做了（建群文档的字段，2026-11-05 查）：
        - `name`：群名（chatbind 已经脱敏、截断、重名加短 id）
        - `owner_id` + `user_id_list`：**主人当群主**并且在群里。主人当群主 ⇒ 他能自己解散、改名、
          退群，这是他的群；机器人不当群主。
        - `set_bot_manager=true`：机器人当管理员 ⇒ 之后会话改了名，机器人还能跟着改群名
          （群设置成「仅群主和管理员可编辑」时，普通成员改不了）。

        所以**建群本身只要一个权限**（im:chat:create）。改群名、拉人那两个权限是给
        「之后会话改名了 / 主人不小心退群了」用的，不开也能建群、能聊。

        `chat_type=private`：私有群，不会被同事搜到。`membership_approval` 不设 ——
        私有群本来只能被群里的人拉进来。
        """
        owner = str(owner_open_id or "")
        if not owner.startswith("ou_"):
            # 不知道主人是谁就别建：建出来是一个只有机器人自己的群，主人看不到
            return "", 0, "不知道你的飞书身份，没法把你拉进群"
        safe = safe_group_name(name)
        query = (f"?user_id_type=open_id&set_bot_manager=true"
                 f"&uuid={dedupe_uuid(safe, owner, self._now())}")
        body = {
            "name": safe,
            "description": "电脑上的一个会话。在这里说的话直达它。",
            "owner_id": owner,
            "user_id_list": [owner],
            "chat_mode": "group",
            "chat_type": "private",
            "group_message_type": "chat",
        }
        out = self._authed("POST", "/im/v1/chats" + query, body)
        code = _code(out)
        chat_id = ((out.get("data") or {}).get("chat_id") or "") if not code else ""
        if not code and not chat_id:
            return "", -1, "飞书说建好了，但没给群编号"
        return str(chat_id), code, str(out.get("msg") or "")

    def exists(self, chat_id: str) -> bool:
        """群还在吗。只有飞书**明确**说「没这个群 / 解散了 / 机器人不在群里」才返回 False。

        🔴 其余一律 True（含没权限、网络不通）：chatbind 的约定是「查不出来就别重建」——
        不然每次读失败都多建一个群。没开 im:chat:read 的时候它永远返回 True，
        后果只是「主人解散了群，再点那个会话不会自动重建」，不会多建。
        """
        cid = str(chat_id or "")
        if not cid.startswith("oc_"):
            return True
        out = self._authed("GET", f"/im/v1/chats/{cid}")
        code = _code(out)
        if code in GONE_CODES:
            return False
        if code:
            return True
        status = (out.get("data") or {}).get("chat_status") or ""
        return status not in ("dissolved", "dissolved_save")

    # ---------- 这个 chat 是群还是单聊 ----------

    def is_group(self, chat_id: str) -> Optional[bool]:
        """`True` = 群，`False` = 单聊，`None` = 问不出来（别猜）。

        🔴 2026-11-05 用户实测撞上的那个「点某个会话没反应」就是因为**我们在猜**：
        卡片回调只带 chat_id，分不出单聊和群；服务一重启 `_chat_ids`（只在内存里）就空了，
        于是聊天记录里所有旧卡片一点都报「服务刚重启过，先发一句话」。
        当天重启了 4 次 ⇒ 用户看到的就是「这个功能没了」。

        现在有「查看群信息」权限（11-05 已开并发版），这件事可以**直接问飞书**：
        `chat_mode` 是 `group` 就是群、`p2p` 就是单聊（实测对真群返回 `group`）。
        问不出来仍然返回 `None` —— 那时才轮到原来那套按记录推断的办法兜底。
        """
        cid = str(chat_id or "")
        if not cid.startswith("oc_"):
            # 单聊的 receiver 是 `ou_…`（人的 open_id），形状上就不是群
            return False if cid.startswith("ou_") else None
        out = self._authed("GET", f"/im/v1/chats/{cid}")
        if _code(out):
            return None
        mode = str(((out.get("data") or {}).get("chat_mode") or "")).strip().lower()
        if mode == "group":
            return True
        if mode in ("p2p", "topic"):
            # topic（话题群）当群处理：它也是多人的，不是单聊
            return mode == "topic"
        return None

    # ---------- 「点一下进群」的链接 ----------

    @staticmethod
    def open_link(chat_id: str) -> str:
        """**直接打开**这个群的 applink（主人本来就在群里，不用「加入」这一步）。

        用户原话：「补充新建群后，让用户自动跳转到群里（如果实现不了，可以点击某个按钮进群，
        不要让用户去找）」。

        🔴 这个比分享链接（`link()`）好，是建完群首选：
        - **不用调接口**（纯拼字符串）⇒ 建群那一下不多一次网络往返、不会失败
        - **不是凭据**：它只是「跳到这个 chat」，不带 token、转发给别人也进不去
          （私有群，不在群里的人打开只会看到进不去）。分享链接是「谁拿到谁能进」，
          那个要管有效期，这个不用。
        - 主人建群时就是群主、已经在群里 ⇒ 他要的是「跳过去」，不是「加入」。

        分享链接留着兜底：万一哪天群里没有他（他自己退了），`add_by_link` 才用得上。

        真正的「自动跳转」（不用点、直接把他拉到那个群）飞书没有这个能力 ——
        机器人发消息改不了用户客户端当前打开哪个会话。所以做成一个按钮，一点就到。
        """
        cid = str(chat_id or "")
        if not cid.startswith("oc_"):
            return ""
        return f"https://applink.feishu.cn/client/chat/open?openChatId={cid}"

    def link(self, chat_id: str, *, validity: str = DEFAULT_LINK_VALIDITY) -> tuple:
        """取群的分享链接 → `(链接, code, msg)`。拿不到就返回空串，**不抛**。

        用户原话：「建完群给我个链接 让我点下马上进群。」建群时主人已经是群主、本来就在群里，
        群**应该**自己出现在他的飞书列表里；这条链接是兜底 ——「建好了但列表里没刷出来」
        的时候点一下直接进。

        🔴 有效期默认 **7 天**，不是永久。这条链接是「谁拿到谁能进这个私有群」的凭据：
        群里能看到电脑那边回的全部内容（源码、路径、报错原文）。链接本来就是秒级用掉的
        （发到主人单聊里、他当场点），留永久只是多一个可以被转发出去的入口。
        过期了也不亏 —— 他那时早就在群里了。

        文档的 `validity_period` 只有 `week` / `year` / `permanently` 三档，没有「一天」。
        """
        cid = str(chat_id or "")
        if not cid.startswith("oc_"):
            return "", 0, "这不是一个群"
        period = validity if validity in LINK_VALIDITY else DEFAULT_LINK_VALIDITY
        out = self._authed("POST", f"/im/v1/chats/{cid}/link", {"validity_period": period})
        code = _code(out)
        if code:
            return "", code, str(out.get("msg") or "")
        url = ((out.get("data") or {}).get("share_link") or "")
        if not isinstance(url, str) or not url.startswith("https://"):
            # 飞书说成了但没给链接 / 给了个不是 https 的东西 ⇒ 当没拿到，别往卡片里放
            return "", -1, "飞书没给可用的入群链接"
        return url, 0, ""

    # ---------- 开权限体检（带缓存，给文案分叉用）----------

    def report(self, *, max_age: float = REPORT_TTL_SEC) -> Optional[Report]:
        """最近一次体检结果，`max_age` 秒内复用；拿不到 ⇒ None（调用方按**保守**那一支说话）。

        为什么要缓存：建群那张确认卡要按「收群消息权限开了没有」分叉文案
        （见 `RECEIVE_SURE` 的红字），而体检要打 5 次飞书接口。建一次群就跑一遍太慢，
        而权限是用户在后台点出来的、几分钟才变一次 ⇒ 缓存一会儿正合适。

        🔴 **不抛**：这是文案用的，查不出来只是话说得保守一点，不能让建群这一下崩掉。
        """
        now = self._now()
        with self._lock:
            if self._report is not None and now - self._report_at < float(max_age):
                return self._report
        try:
            got = probe(self)
        except Exception:  # noqa: BLE001
            logger.exception("开权限体检失败，文案按保守那一支走")
            return None
        with self._lock:
            self._report = got
            self._report_at = self._now()
        return got

    # ---------- 额外几个（之后会话改名 / 主人退群了用）----------

    def name_of(self, chat_id: str) -> tuple:
        """群现在叫什么 → `(群名, code, msg)`。读不出来 ⇒ 名字是空串，**不抛**。

        🔴 这是「要不要替主人改群名」的唯一依据（`chatbind.rename_group`）：群改绑到另一个
        会话时，群名得跟着改，否则主人看群名以为还在跟老会话说话。但群名也可能是**他自己改的**
        （群主是他），那就不能覆盖。分辨办法只有一个 —— 把现在的群名和我们当初起的那个
        （`Bound.name`）比一比，所以必须先读回来。

        读不出来（没开「查看群信息」、断网）⇒ 空串，调用方**什么都别改**：
        宁可群名一时对不上（消息里会说清改绑到谁了），也别把他起的名字冲掉（外部可见、他不一定发现）。
        """
        cid = str(chat_id or "")
        if not cid.startswith("oc_"):
            return "", 0, "这不是一个群"
        out = self._authed("GET", f"/im/v1/chats/{cid}")
        code = _code(out)
        if code:
            return "", code, str(out.get("msg") or "")
        name = (out.get("data") or {}).get("name")
        return (name if isinstance(name, str) else ""), 0, ""

    def rename(self, chat_id: str, name: str) -> tuple:
        """改群名 → `(成功, code, msg)`。只改名字，别的群设置一概不碰（文档：不传的字段不更新）。"""
        cid = str(chat_id or "")
        if not cid.startswith("oc_"):
            return False, 0, "这不是一个群"
        out = self._authed("PUT", f"/im/v1/chats/{cid}?user_id_type=open_id",
                           {"name": safe_group_name(name)})
        code = _code(out)
        return not code, code, str(out.get("msg") or "")

    def pin(self, message_id: str) -> tuple:
        """把群里一条消息置顶 → `(成功, code, msg)`。要 `im:message.pins:write_only` 一类权限；没开就失败，调用方降级。"""
        mid = str(message_id or "")
        if not mid.startswith("om_"):
            return False, 0, "消息编号不对"
        out = self._authed("POST", "/im/v1/pins", {"message_id": mid})
        code = _code(out)
        return not code, code, str(out.get("msg") or "")

    def add_owner(self, chat_id: str, owner_open_id: str) -> tuple:
        """把主人拉回群里 → `(成功, code, msg)`。只拉主人这一个人，不接受别的 id。"""
        cid, owner = str(chat_id or ""), str(owner_open_id or "")
        if not cid.startswith("oc_") or not owner.startswith("ou_"):
            return False, 0, "群或者人的编号不对"
        out = self._authed("POST", f"/im/v1/chats/{cid}/members?member_id_type=open_id",
                           {"id_list": [owner]})
        code = _code(out)
        return not code, code, str(out.get("msg") or "")


# ---------- 开权限体检（只读）----------

OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Need:
    """体检的一项：这个能力现在有没有。"""

    key: str
    label: str
    state: str  # OK / MISSING / UNKNOWN
    scope: str  # 推荐开的那一个
    console_name: str  # 飞书后台「权限管理」里搜得到的名字
    why: str = ""  # 判断依据（给人看的一句）
    feishu_says: tuple = ()  # 飞书错误信封里列的 scope 原文（任一即可）


@dataclass(frozen=True)
class Report:
    app_id: str
    needs: tuple = field(default_factory=tuple)
    note: str = ""  # 整体没查成时的一句话（比如凭据不对）

    def get(self, key: str) -> Optional[Need]:
        return next((n for n in self.needs if n.key == key), None)

    @property
    def missing(self) -> tuple:
        return tuple(n for n in self.needs if n.state == MISSING)

    @property
    def unknown(self) -> tuple:
        return tuple(n for n in self.needs if n.state == UNKNOWN)

    @property
    def ready(self) -> bool:
        """六项都**确认**有了才算好。有一项看不出来就不算 —— 宁可让用户多点一次验证。"""
        return bool(self.needs) and all(n.state == OK for n in self.needs) and not self.note

    def to_open(self) -> list:
        """要去后台开的 scope：缺的 + 看不出来的（看不出来的开了也不亏）。"""
        return [n.scope for n in self.needs if n.state != OK]

    def grant_link(self) -> str:
        """一键跳到「开通权限」页并预选好这几项。链接形状照飞书 99991672 报错里给的那条（实测）。"""
        scopes = self.to_open()
        if not self.app_id or not scopes:
            return ""
        return (f"https://open.feishu.cn/app/{self.app_id}/auth?q={','.join(scopes)}"
                f"&op_from=openapi&token_type=tenant")

    def render(self) -> str:
        """给人看的体检结果（纯文字，终端和飞书都能用）。"""
        lines = []
        if self.note:
            lines.append(f"⚠️ {self.note}")
        mark = {OK: "✅", MISSING: "❌", UNKNOWN: "❔"}
        for need in self.needs:
            tail = "" if need.state == OK else f"  ← 要开「{need.console_name}」{need.scope}"
            lines.append(f"{mark.get(need.state, '❔')} {need.label}{tail}")
            if need.why and need.state != OK:
                lines.append(f"     {need.why}")
        if self.ready:
            lines.append("全齐了：点电脑上的会话就能自动建群。")
        elif self.needs:
            if self.missing:
                lines.append(f"还差 {len(self.missing)} 项。开完要发一个新版本才生效。")
            if self.unknown:
                lines.append(f"有 {len(self.unknown)} 项现在看不出来，一起开上不亏。")
            link = self.grant_link()
            if link:
                lines.append(f"一键去开：{link}")
        return "\n".join(lines)


def _probe_live(setup: "GroupSetup", key: str) -> tuple:
    """对一个**不存在的群**打一下 → (state, why, 飞书列的 scope)。

    飞书先查权限再查群（实测：没权限一律 99991672，有权限时发消息回 230001「invalid receive_id」）。
    所以只要不是 99991672 就说明**权限这一关过了**，至于群不存在那是预料之中的。
    """
    if key == "read":
        out = setup._authed("GET", f"/im/v1/chats/{PROBE_CHAT_ID}")
    elif key == "rename":
        out = setup._authed("PUT", f"/im/v1/chats/{PROBE_CHAT_ID}?user_id_type=open_id",
                            {"name": "权限体检"})
    elif key == "add_owner":
        out = setup._authed("POST", f"/im/v1/chats/{PROBE_CHAT_ID}/members?member_id_type=open_id",
                            {"id_list": [PROBE_OPEN_ID]})
    elif key == "send":
        out = setup._authed("POST", "/im/v1/messages?receive_id_type=chat_id",
                            {"receive_id": PROBE_CHAT_ID, "msg_type": "text",
                             "content": json.dumps({"text": "权限体检"}, ensure_ascii=False)})
    else:
        return UNKNOWN, "这一项没法不动手就试出来", ()
    code = _code(out)
    if code in PERMISSION_CODES:
        return MISSING, "飞书说没开这个权限", tuple(missing_scopes(out))
    if code == -1:
        return UNKNOWN, f"没查成：{str(out.get('msg') or '')[:60]}", ()
    return OK, "", ()


# 体检时真的打一下的四项（全是对不存在的群，改不了任何东西）。建群和收群消息不打，见模块头。
LIVE_PROBES = ("read", "rename", "add_owner", "send")


def _granted_from_app_info(setup: "GroupSetup") -> Optional[set]:
    """读应用自己的权限列表（GET /application/v6/applications/:app_id）。读不到 ⇒ None。

    这个接口本身要 `application:application:self_manage` 或 `admin:app.info:readonly`
    （2026-11-05 实测本机这台两个都没有 ⇒ 99991672）。所以它只是**加分项**：
    能读到就能把「建群」「收群消息」两项从「看不出来」变成确定的。
    """
    if not setup._app_id:
        return None
    out = setup._authed("GET", f"/application/v6/applications/{setup._app_id}?lang=zh_cn")
    if _code(out):
        return None
    app = (out.get("data") or {}).get("app") or {}
    got = set()
    for item in app.get("scopes") or []:
        scope = item.get("scope") if isinstance(item, dict) else ""
        types = item.get("token_types") if isinstance(item, dict) else None
        # 只认应用身份（tenant）那一份：我们是用机器人自己的身份建群、收消息
        if isinstance(scope, str) and scope and (not types or "tenant" in types):
            got.add(scope)
    return got


def probe(setup: "GroupSetup") -> Report:
    """开权限体检。**只读**：不建群、不改配置、不发版本，也不往任何真实的群发东西。"""
    if not setup._tenant_token():
        err = setup.last_error
        return Report(setup._app_id, tuple(
            Need(key, label, UNKNOWN, scope, console)
            for key, label, scope, console, _alts in NEEDS
        ), note=f"换不到飞书的访问凭证（code={err.get('code')}），先确认 app_id / app_secret 对不对")
    granted = _granted_from_app_info(setup)
    needs = []
    for key, label, scope, console, alts in NEEDS:
        if key in LIVE_PROBES:
            state, why, says = _probe_live(setup, key)
        elif granted is not None:
            have = {scope, *alts} & granted
            state = OK if have else MISSING
            why = "" if have else "应用的权限列表里没有它"
            says = ()
        else:
            state, says = UNKNOWN, ()
            why = ("建群没法不动手就试出来（试一下就真建了一个群）；开完之后点一次会话就知道"
                   if key == "create" else
                   "收消息是飞书推过来的，没法主动试；开完之后在群里说一句就知道")
        needs.append(Need(key, label, state, scope, console, why, says))
    return Report(setup._app_id, tuple(needs))


# ---------- 卡片：群里的权限确认 + 单聊里的「先去开权限」----------
#
# 写法同 `cards.py`：schema 2.0、没有 action 容器、按钮回调走 behaviors。
# 只用 cards 的**公开**函数（relay_value / session_label），不碰它的下划线函数 —— 那个文件别人在改。

def _md(text: str, small: bool = False) -> dict:
    node = {"tag": "markdown", "content": text}
    if small:
        node["text_size"] = "notation"
    return node


def _btn(label: str, value: dict, primary: bool = False) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _pair(buttons: list) -> dict:
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [{"tag": "column", "width": "weighted", "weight": 1, "elements": [b]} for b in buttons],
    }


def _card(title: str, template: str, elements: list) -> str:
    return json.dumps(
        {"schema": "2.0", "config": {"wide_screen_mode": True},
         "header": {"title": {"tag": "plain_text", "content": title}, "template": template},
         "body": {"elements": elements}},
        ensure_ascii=False,
    )


# ---------- 「群里每一句都直达」这句话能不能说 ----------
#
# 🔴 2026-11-05 体验审查：这句是**承诺**，而它取决于一个我们没法自己验的权限
# （`im:message.group_msg`，「获取群组中所有消息」）。没开的话飞书只推**@ 了机器人**的消息 ⇒
# 主人在群里说一句，机器人一声不响，他只会以为建群这功能坏了。
# 所以文案按**实际有没有这个权限**分叉：确定有 ⇒ 照说；没有 / 看不出 ⇒ 说「先 @ 一下试试」。
#
# 看不出来（读不到应用权限列表）也走保守那一支：说小了只是让他多 @ 一次，说大了是直接失灵。

RECEIVE_SURE = "你在这个群里说的**每一句**都直接进那个会话"
RECEIVE_UNSURE = "你在这个群里 **@ 一下机器人**再说话，那句就直接进那个会话"
# 保守那一支要多说一句，否则他不知道「为什么还要 @」、也不知道怎么变成不用 @
RECEIVE_HINT = ("不 @ 它也想进去的话，得给这个机器人开「获取群组中所有消息」这项权限"
                "（在飞书开放平台，开完要发个新版本）。不确定开没开就先 @ 一下，准没错。")


def receive_granted(report: Optional[Report]) -> bool:
    """「不 @ 机器人也收得到群里的话」这件事**确定**成立吗。

    只有体检明确查到这一项 OK 才算。没体检过、看不出来、缺了 —— 一律 False（见上面红字）。
    """
    if report is None:
        return False
    need = report.get("receive")
    return need is not None and need.state == OK


def build_group_consent_card(sess_or_bound: Any, redact: Optional[Callable[[str], str]] = None,
                             *, report: Optional[Report] = None) -> str:
    """建好群之后，**在群里**发的第一张卡：确认了才开始转发。

    话术和单聊那张 `cards.build_confirm_relay_card` 是同一套（spec 二 4），多说两件群里才有的事：
    - **群里每一句都进去**，不用点 [接着聊]。所以这张卡比单聊那张更要紧：误进一次，之后每句都算数。
    - **别拉别人进来**。群里的人都能看到电脑那边回的内容；而且只有你说的话会被转过去
      （别人说的桥直接丢掉，`app.py` 只认主人）—— 拉人进来也指挥不了，只是能看。

    按钮值用 `cards.relay_value`，回调走现成的 `resolve_relay_click` 那道闸（id 必须在最近一次扫描里）。
    确认按 scope 记（`consent.RelayConsent`）⇒ 在群里确认过**不等于**单聊里确认过，反过来也一样。

    `report`：开权限体检结果。🔴 「每一句都直达」这句话按**实际有没有收群消息那项权限**分叉
    （见上面 `RECEIVE_SURE` 的红字）—— 没开的时候照说，群里就是一声不响。
    """
    from ..reply import escape_card
    from .cards import relay_value, session_label

    clean = redact or (lambda s: s)
    label = escape_card(session_label(sess_or_bound, clean))
    tool = str(getattr(sess_or_bound, "tool", "") or "")
    sid = str(getattr(sess_or_bound, "id", "") or "")
    sure = receive_granted(report)
    how = RECEIVE_SURE if sure else RECEIVE_UNSURE
    elements = [
        _md(
            f"这个群对应电脑上的 **{label}**。确认之后，{how}，由它去做。\n\n"
            "· 它的权限就是你在电脑上给它的那套 —— 多半是**不问你就改**\n"
            "· 手机和终端共用当前权限档位；需要收紧时请切到「只读」\n"
            "· 群里的人都能看到它回的内容 ⇒ **别拉别人进来**（别人说的话不会转过去，只是能看）"
        ),
        _pair([
            _btn("确认，开始说话", relay_value("talk", tool, sid), primary=True),
            _btn("算了", relay_value("no", tool, sid)),
        ]),
    ]
    if not sure:
        elements.append(_md(RECEIVE_HINT, small=True))
    # 「算了」那半句的措辞**取决于 app.py 怎么处理它**：绑过的群里点「算了」要解绑
    # （只关转发、群留着），那一段在 `app._on_relay_click`。这里只说点了之后立刻会怎样
    # （不发过去），不替那边承诺「以后再也不问」—— 承诺了而那边没解绑就是假话。
    elements.append(
        _md("确认一次，这个群 24 小时内不再问。点「算了」或者不确认，你在群里说的话都不会发过去。",
            small=True))
    return _card("⚠️ 这个群直达电脑上的会话", "orange", elements)


def build_need_permission_card(report: Optional[Report] = None, *, app_id: str = "") -> str:
    """点会话想建群、但机器人还没有建群权限时，发回**单聊**的那张卡：去哪开、开什么、开完怎么验。

    `report` 是体检结果；没有（体检本身没跑）就按「六项里缺建群那一组」给一张通用的。
    🔴 文案里不放 app_secret、不放 token；链接只带 app_id（它本来就在应用后台的地址栏里）。
    """
    if report is None:
        needs = tuple(Need(k, lbl, OK if k == "send" else MISSING, sc, con)
                      for k, lbl, sc, con, _a in NEEDS)
        report = Report(str(app_id or ""), needs)
    todo = [n for n in report.needs if n.state != OK]
    rows = "\n".join(f"· {n.label}：搜「{n.console_name}」（{n.scope}）" for n in todo) or "· 看起来都开了"
    steps = (
        "1. 打开飞书开发者后台，进这个机器人的应用 →「开发配置」→「权限管理」\n"
        "2. 点「开通权限」，搜下面这几项，勾上 →「确认开通权限」\n"
        f"{rows}\n"
        "3. 左边「应用发布」→「版本管理与发布」→「创建版本」→ 保存并申请发布"
        "（公司开了审核的话要等管理员批）\n"
        "4. 发布后大约 5 分钟生效，回这里点一次会话试试"
    )
    elements = [
        _md("想给每个会话建一个群，但这个机器人**还没有建群的权限**。要在飞书后台开一下，开完要发个新版本。"),
        _md(steps),
    ]
    link = report.grant_link()
    if link:
        elements.append({
            "tag": "button",
            "text": {"tag": "plain_text", "content": "🔑 去后台开权限"},
            "type": "primary",
            "width": "fill",
            "size": "medium",
            "behaviors": [{"type": "open_url", "default_url": link}],
        })
    elements.append(_md("「获取群组中所有消息」是敏感权限，管理员会多看一眼：没有它，你在群里得先 @ 机器人它才收得到。"
                        "不想开群也没关系，单聊里点 [接着聊] 照样能用。", small=True))
    return _card("🔑 建群要先开权限", "yellow", elements)


# ---------- 命令行：只读体检 ----------

def _load_creds(path: str) -> tuple:
    import os
    import tomllib

    with open(os.path.expanduser(path), "rb") as handle:
        raw = tomllib.load(handle)
    feishu = raw.get("feishu") or {}
    app_id = os.environ.get("FEISHU_BRIDGE_APP_ID") or feishu.get("app_id") or ""
    secret = os.environ.get("FEISHU_BRIDGE_APP_SECRET") or feishu.get("app_secret") or ""
    return str(app_id), str(secret)


def main(argv: list, *, http: Optional[Callable[..., dict]] = None) -> int:
    """`python -m bridge.fleet.groupsetup [config.toml]` —— 开权限前后各跑一次，看还差什么。

    只读：不建群、不改应用配置、不发版本。凭据只读不打印。
    退出码：0 = 全齐；1 = 还有没开的 / 看不出来的；2 = 配置读不出来。
    """
    path = argv[1] if len(argv) > 1 and not argv[1].startswith("-") else "~/.carher-feishu-agent/config.toml"
    try:
        app_id, secret = _load_creds(path)
    except (OSError, ValueError) as exc:
        print(f"读不出配置 {path}：{exc}")
        return 2
    if not app_id or not secret:
        print(f"配置 {path} 里没有 app_id / app_secret")
        return 2
    report = probe(GroupSetup(app_id, secret, http=http or _http_default))
    print(report.render())
    return 0 if report.ready else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    import sys

    sys.exit(main(sys.argv))
