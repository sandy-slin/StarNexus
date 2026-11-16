"""「一个群 = 一个会话」的落盘绑定表：这个群里说的每一句直达哪个会话。

## 为什么要有这个文件

用户原话：「点击一个会话的时候 最好自动建立一个群。以会话名命名。这样在那个会话名命名的群里
主人发的任何消息直达那个会话里。」—— 不用再点 [接着聊]，也不用记自己在不在对话模式。

## 和 5 分钟「待发目标」的区别（**别混**）

| | 存在哪 | 活多久 | 怎么停 |
|---|---|---|---|
| 待发目标（对话模式）| `FleetControl._pending`（内存）| 5 分钟，每发一次续期 | 「取消」/ [退出对话] |
| 群绑定（这个文件）| 磁盘 | **长期**，主人自己解绑才停 | 在群里点 [退出对话]：只关转发，群留着 |

绑定是权威，待发目标只是下游的管道：群里每来一条消息，先按绑定把待发目标摆好
（`app._group_bind_gate`），所以那 5 分钟在群里永远不会过期。

## 为什么落盘

看门狗在长连接静默时**会自杀等重启**（lark-oapi #169 的唯一保险），"重启"在这套东西里是
常态不是例外。只放内存 ⇒ 重启后群里说的话会悄悄落进桥自己的 claude（另一个项目、另一套权限）。
写法抄 `prefs.py`：临时文件 + `os.replace()`，直接 `open(w)` 中途断电会留下半截 JSON。

## 读失败怎么办

**当成没绑过**：那条消息按平常处理（走桥自己的 claude，它自己有只读闸门），
而不是硬往一个猜出来的会话里发。

## 三条红线

1. **不碰单聊**。这张表只认 `group:<chat_id>` 的 scope，`of_scope("dm:…")` 一定是 None；
   `bind()` 还拒收 `ou_` 开头的 id（那是人的 open_id，不是群）。2026-11-05 踩过一次：
   群的 chat_id 被写进了 `dm:` 那张表，之后单聊里的话被当成要转发给那个会话。
2. **不自己解散群**。解散是不可逆的外部动作。主人解绑 ⇒ `turn_off()` 只关转发、记录留着
   （再点那个会话还是用这个群）；只有"群真的没了"才 `forget()`。
3. **权限确认另算**。群里能看到的人更多 ⇒ 进群不等于确认过。绑定长期有效，
   那一次权限确认照旧按 scope 记、24 小时问一次（`consent.RelayConsent`）。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import unicodedata
from dataclasses import replace as _dc_replace, dataclass
from typing import Any, Callable, Iterable, Optional

from .model import TOOLS, short_title
from .scan import TOOL_LABELS


def _replace(bound, **kw):
    """Bound 是 frozen 的 ⇒ 改字段要复制一份。"""
    import dataclasses

    return dataclasses.replace(bound, **kw)


logger = logging.getLogger("feishu-bridge.fleet")

# 群名上限。手机上一行看得全的宽度按 22~24 个汉字算（spec 一），群名再长列表里会被截。
MAX_NAME_CHARS = 40
# 所有会话群统一前缀，飞书里搜「SN·」就能列出全部会话群（产品定义 §5.1 群名规范）。
GROUP_PREFIX_SN = "SN"
PROJECT_NAME_CHARS = 16
# 重名时加的短 id 长度。会话 id 是 uuid，前 6 位足够分开同名的两个会话。
SHORT_ID_CHARS = 6
# 记这么多条就够了（一个人手机上不会有几百个群）。超了清最旧的，别让文件无限长。
MAX_ENTRIES = 200
_ID_MAX = 128
_CHAT_ID_MAX = 128

GROUP_PREFIX = "group:"
# 🔴 人的 open_id 长这样。它**不是** chat_id，绑进来就等于把单聊当群用（11-05 踩过）。
_OPEN_ID_PREFIX = "ou_"

# ensure_group 的四种结果。调用方要按它说人话 —— 「没权限」和「飞书报错」是两件事：
# 前者要用户去后台开 scope（主会话写的那份清单），后者等一会儿再点就好。
REUSED = "reused"          # 这个会话已经有群了，直接用（点两次不建两个群）
CREATED = "created"        # 新建了一个
NO_PERMISSION = "no_perm"  # 机器人没有建群权限（实测 99991672）
FAILED = "failed"          # 别的原因没建成

# 飞书「没有这个 scope」的错误码。实测 `POST /open-apis/im/v1/chats` ⇒ 99991672。
PERMISSION_CODES = frozenset({99991672, 99991679, 19001})


@dataclass(frozen=True)
class Bound:
    """一条绑定：这个群直达哪个会话。"""

    chat_id: str
    tool: str
    id: str
    name: str
    created_at: float
    on: bool = True  # False = 主人解绑过，只关转发，群和这条记录都留着

    @property
    def label(self) -> str:
        """「Claude「修登录」」。🔴 `name` 是**群名**（`group_name` 起的「Claude · 修登录」），
        本身已经带了工具前缀 ⇒ 先剥掉再套，不然就是「Claude「Claude · 修登录」」（2026-11-06 体验侦察）。

        调用方能拿到会话本身时优先用 `cards.session_label`（标题不受群名上限截断，
        见 `app._bound_label`）；这里是重启后还没扫到会话时的兜底。
        """
        head = TOOL_LABELS.get(self.tool, self.tool)
        name = self.name or ""
        prefix = f"{head}·"
        if head and name.startswith(prefix):
            name = name[len(prefix):].strip()
        name = name or self.id[:SHORT_ID_CHARS]
        return f"{head}「{name}」"

    @property
    def scope(self) -> str:
        return GROUP_PREFIX + self.chat_id


def _clean_name(text: Any) -> str:
    """群名里不要控制字符 / 零宽字符 / 换行。emoji 留着（用户自己起的名字就带）。

    飞书群名是在群列表里显示的，换行和零宽字符会让它看起来是空的或者错位。
    """
    flat = " ".join(str(text or "").split())
    kept = [ch for ch in flat if unicodedata.category(ch) not in ("Cc", "Cf", "Cs", "Co")]
    return "".join(kept).strip()


def project_label(project: Any, clean: Optional[Callable[[str], str]] = None) -> str:
    """项目目录 → 群名里的项目段：只取最后一级目录名，脱敏、去控制字符、截短。"""
    raw = str(project or "").rstrip("/")
    if not raw:
        return ""
    base = raw.rsplit("/", 1)[-1]
    try:
        base = _clean_name((clean or (lambda x: x))(base))
    except Exception:  # noqa: BLE001
        logger.exception("项目名脱敏失败，群名不带项目")
        return ""
    base = base.replace("·", "-")
    return base[:PROJECT_NAME_CHARS]


def group_name(tool: str, title: str, sid: str = "", *, taken: Iterable[str] = (),
               clean: Optional[Callable[[str], str]] = None, limit: int = MAX_NAME_CHARS,
               project: str = "") -> str:
    """会话 → 群名。规则：`Claude · 修登录`，超长截断，重名加短 id。

    - `clean`：脱敏。会话标题是**用户项目里的文字**（第一句话 / AI 起的标题），而群里能看到的人
      更多 ⇒ 必须先过 `redact.py` 再进群名（顺序同 `cards.build_detail_card` 的红字：
      先脱敏整段，再截断）。
    - 没名字的会话用短 id 兜底，不让群叫「Claude · 」。
    - `taken`：已经用掉的群名。撞上就加 ` #<短 id>` —— 两个会话都叫「修登录」时，
      群列表里必须分得出哪个是哪个。
    """
    clean = clean or (lambda s: s)
    try:
        safe = _clean_name(clean(str(title or "")))
    except Exception:  # noqa: BLE001 —— 脱敏失败宁可不要标题，也不能把原文发进群名
        logger.exception("群名脱敏失败，这个群用短 id 命名")
        safe = ""
    head = TOOL_LABELS.get(tool, tool) or "会话"
    tail = safe or f"会话 {str(sid or '')[:SHORT_ID_CHARS]}".strip()
    proj = project_label(project, clean)
    parts = [GROUP_PREFIX_SN, head] + ([proj] if proj else []) + [tail]
    name = short_title("·".join(parts), limit)
    used = {str(t) for t in (taken or ())}
    if name not in used:
        return name
    mark = " #" + str(sid or "")[:SHORT_ID_CHARS]
    # 加短 id 之后还要塞得进上限 ⇒ 先把正文截短，再拼后缀
    return short_title(name, max(limit - len(mark), 1)) + mark


def is_group_scope(scope: Any) -> bool:
    return isinstance(scope, str) and scope.startswith(GROUP_PREFIX) and len(scope) > len(GROUP_PREFIX)


def chat_of_scope(scope: Any) -> str:
    """`group:oc_xxx` → `oc_xxx`；不是群 scope ⇒ 空串（**不猜**）。"""
    return scope.split(":", 1)[1] if is_group_scope(scope) else ""


class ChatBinds:
    """落盘的「群 ↔ 会话」绑定表。一个群只绑一个会话，一个会话只占一个群。

    键是 chat_id（`oc_…`），不是 scope —— scope 由它派生（`Bound.scope`），这样就不可能
    往 `dm:` 那张表里写东西。
    """

    def __init__(self, path: str, *, now: Callable[[], float] = time.time) -> None:
        self._path = path
        self._now = now
        self._lock = threading.Lock()
        self._by_chat: dict = {}  # chat_id → Bound
        self.last_save_ok = True
        # 上一次 bind() 顺带关掉转发的那些群（同一个会话原来绑在它们上面）。
        # 调用方必须据此清掉它们的待发目标并在群里说明白 —— 见 bind() 的红字。
        self.last_displaced: tuple = ()
        self._load()

    # ---------- 落盘 ----------

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return  # 没有 / 半截 JSON ⇒ 当成没绑过（那条消息按平常处理，方向是安全的）
        if not isinstance(data, dict):
            return
        got: dict = {}
        for chat_id, row in (data.get("binds") or {}).items():
            bound = self._row_to_bound(chat_id, row)
            if bound is not None:
                got[bound.chat_id] = bound
        with self._lock:
            self._by_chat = got

    def _row_to_bound(self, chat_id: Any, row: Any) -> Optional[Bound]:
        """磁盘上的一行 → Bound。**每个字段都要重新过一遍闸门**，不能信磁盘：

        这个文件决定「群里的话发给谁」。被手改过、或者旧版本写进去的 `ou_…`，
        采信就等于把单聊当群用。认不出来的行直接丢掉（丢了只是要重新点一次那个会话）。
        """
        if not isinstance(chat_id, str) or not _usable_chat_id(chat_id):
            return None
        if not isinstance(row, dict):
            return None
        tool, sid = row.get("tool"), row.get("id")
        if not isinstance(tool, str) or tool not in TOOLS:
            return None
        if not isinstance(sid, str) or not sid or len(sid) > _ID_MAX:
            return None
        try:
            created = float(row.get("created_at") or 0.0)
        except (TypeError, ValueError):
            created = 0.0
        return Bound(chat_id=chat_id, tool=tool, id=sid,
                     name=_clean_name(row.get("name"))[:MAX_NAME_CHARS],
                     created_at=created, on=bool(row.get("on", True)))

    def _save(self) -> bool:
        with self._lock:
            payload = {
                "binds": {
                    b.chat_id: {"tool": b.tool, "id": b.id, "name": b.name,
                                "created_at": b.created_at, "on": b.on}
                    for b in self._by_chat.values()
                }
            }
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
            # 存不下来不影响这一次（内存里已经绑上了），只是重启后这个群要重新点一次
            logger.exception("群绑定没存下来：%s", self._path)
            self.last_save_ok = False
            return False

    # ---------- 查 ----------

    def of_chat(self, chat_id: str) -> Optional[Bound]:
        """这个群绑了哪个会话。**解绑过的也返回** —— 调用方要能说「这个群已经解绑了」。"""
        if not isinstance(chat_id, str) or not chat_id:
            return None
        with self._lock:
            return self._by_chat.get(chat_id)

    def of_scope(self, scope: str) -> Optional[Bound]:
        """scope → 绑定。🔴 单聊 scope 一律 None：这张表不碰 `dm:`。"""
        chat_id = chat_of_scope(scope)
        return self.of_chat(chat_id) if chat_id else None

    def active_of_scope(self, scope: str) -> Optional[Bound]:
        """只要还在转发的那一条。解绑过的 ⇒ None（群里的话按平常处理）。"""
        bound = self.of_scope(scope)
        return bound if bound is not None and bound.on else None

    def of_session(self, tool: str, sid: str) -> Optional[Bound]:
        """这个会话已经有群了吗（幂等的依据）。解绑过的也算有 —— 重新点就复用它。"""
        with self._lock:
            for bound in self._by_chat.values():
                if (bound.tool, bound.id) == (tool, sid):
                    return bound
        return None

    def names(self, *, skip_chat: str = "") -> set:
        """已经用掉的群名。`skip_chat`：算「这个群改个名」时要**排除它自己**，
        不然它现在的名字会被当成「被占了」，改完白白多一截 `#短 id`。
        """
        with self._lock:
            return {b.name for b in self._by_chat.values() if b.name and b.chat_id != skip_chat}

    def all(self) -> list:
        with self._lock:
            return sorted(self._by_chat.values(), key=lambda b: b.created_at)

    # ---------- 改 ----------

    def bind(self, chat_id: str, tool: str, sid: str, name: str = "") -> Optional[Bound]:
        """记下「这个群 = 这个会话」。闸门没过 ⇒ None，**什么都不写**。

        🔴 `chat_id` 必须是真的群 id。`ou_…`（人的 open_id）、空串、超长的一律拒 ——
        这是「别污染单聊」那条红线在写入端的闸门，不能只靠调用方自觉。
        """
        if not _usable_chat_id(chat_id):
            logger.warning("拒绝绑定：这不是群的 chat_id（%s）", str(chat_id)[:12])
            return None
        if tool not in TOOLS or not sid or len(str(sid)) > _ID_MAX:
            logger.warning("拒绝绑定：认不出这个会话 tool=%s", str(tool)[:10])
            return None
        bound = Bound(chat_id=chat_id, tool=tool, id=str(sid),
                      name=_clean_name(name)[:MAX_NAME_CHARS], created_at=float(self._now()), on=True)
        displaced: list = []
        with self._lock:
            # 一个群只绑一个会话：重绑直接覆盖（群名由调用方改，这里只管映射）。
            # 同一个会话在别的群里绑过 ⇒ 那个群要**关掉转发并记下来**，不能直接删记录。
            #
            # 🔴 2026-11-05 安全 + 体验两份审查都指出同一个坑：原来是 `pop` 掉旧记录 ⇒
            # 那个群从「绑着 B」变成「谁都不绑」，而它的待发目标还在，接下来 5 分钟里
            # 主人在旧群里说的话继续进 B，5 分钟后**悄悄落到桥自己的 claude**（另一个项目）。
            # 现在改成 turn_off：记录留着（`on=False`），`of_chat` 仍能认出这个群，
            # 调用方据此清掉它的待发目标、并在旧群里说明白。
            for old, value in list(self._by_chat.items()):
                if (value.tool, value.id) == (bound.tool, bound.id) and old != chat_id:
                    self._by_chat[old] = _replace(value, on=False)
                    displaced.append(old)
            self._by_chat[chat_id] = bound
            self._prune_locked()
        self._save()
        self.last_displaced = tuple(displaced)
        return bound

    def set_name(self, chat_id: str, name: str) -> Optional[Bound]:
        """记下「这个群现在叫什么」——**只记我们自己改成的那个名字**。

        🔴 主人自己改的名字**不要**记进来：`Bound.name` 是「我们上次起的名」，
        它是 `rename_group` 判断「他动过没动过」的唯一尺子。把他改的名字记下来，
        下一次改绑就会认为「现在的名字是我们起的」⇒ 把他起的名字冲掉。
        """
        with self._lock:
            bound = self._by_chat.get(chat_id)
            if bound is None:
                return None
            bound = _dc_replace(bound, name=_clean_name(name)[:MAX_NAME_CHARS])
            self._by_chat[chat_id] = bound
        self._save()
        return bound

    def turn_off(self, chat_id: str) -> Optional[Bound]:
        """主人解绑：**只关转发**，群和这条记录都留着。

        🔴 不自动解散群 —— 解散是不可逆的外部动作（spec 二 8）。留着记录还有个好处：
        他再点那个会话时复用这个群，而不是又建一个同名的。
        """
        with self._lock:
            bound = self._by_chat.get(chat_id)
            if bound is None or not bound.on:
                return bound
            import dataclasses

            bound = dataclasses.replace(bound, on=False)
            self._by_chat[chat_id] = bound
        self._save()
        return bound

    def turn_on(self, chat_id: str) -> Optional[Bound]:
        with self._lock:
            bound = self._by_chat.get(chat_id)
            if bound is None:
                return None
            if not bound.on:
                import dataclasses

                bound = dataclasses.replace(bound, on=True)
                self._by_chat[chat_id] = bound
        self._save()
        return bound

    def forget(self, chat_id: str) -> bool:
        """这条记录彻底去掉。只在**群真的不在了**（被主人解散）时用 —— 下次点会重建一个。"""
        with self._lock:
            existed = self._by_chat.pop(chat_id, None) is not None
        if existed:
            self._save()
        return existed

    def _prune_locked(self) -> None:
        if len(self._by_chat) <= MAX_ENTRIES:
            return
        for chat_id in sorted(self._by_chat, key=lambda c: self._by_chat[c].created_at)[
            : len(self._by_chat) - MAX_ENTRIES
        ]:
            self._by_chat.pop(chat_id, None)


def _usable_chat_id(chat_id: Any) -> bool:
    """能当群 id 用吗。`ou_…` 是人的 open_id ⇒ 一律不行（别污染单聊那条红线）。"""
    if not isinstance(chat_id, str):
        return False
    value = chat_id.strip()
    if not value or len(value) > _CHAT_ID_MAX:
        return False
    return not value.startswith(_OPEN_ID_PREFIX)


# ---------- 建群（真正调飞书的那一层在外面） ----------


@dataclass(frozen=True)
class GroupResult:
    """ensure_group 的结果。`how` 是上面那四个常量之一。"""

    how: str
    bound: Optional[Bound] = None
    code: int = 0
    msg: str = ""

    @property
    def ok(self) -> bool:
        return self.how in (REUSED, CREATED) and self.bound is not None

    @property
    def fresh(self) -> bool:
        return self.how == CREATED


class GroupClient:
    """建群那一下的接口。**这个文件自己不碰飞书**。

    实现由接入层给（`bridge/app.py` 那边的真实 lark client，由上线那条路负责）；
    测试注入假的 ⇒ 一个群都不会真建出来（spec 安全红线：建群是外部可见的不可逆动作）。

    约定：
    - `create(name, owner_open_id)` → `(chat_id, code, msg)`。成功时 chat_id 非空、code=0；
      失败时 chat_id 为空、code 是飞书的错误码（没权限那几个见 `PERMISSION_CODES`）。
    - `exists(chat_id)` → bool。群被主人解散了要能看出来（返回 False ⇒ 重建）。
      不确定时**返回 True** —— 查不出来就别重建，不然每次读失败都多一个群。
    - 两个方法都**不许抛**；真抛了 `ensure_group` 会当成 FAILED 处理。
    """

    def create(self, name: str, owner_open_id: str = "") -> tuple:  # pragma: no cover - 接口
        raise NotImplementedError

    def exists(self, chat_id: str) -> bool:  # pragma: no cover - 接口
        return True


_ENSURE_LOCKS: dict = {}
_ENSURE_GUARD = threading.Lock()


def _ensure_lock(tool: str, sid: str) -> "threading.Lock":
    with _ENSURE_GUARD:
        lock = _ENSURE_LOCKS.get((tool, sid))
        if lock is None:
            lock = _ENSURE_LOCKS[(tool, sid)] = threading.Lock()
        return lock


def ensure_group(binds: ChatBinds, client: Any, tool: str, sid: str, title: str = "",
                 *, owner_open_id: str = "", clean: Optional[Callable[[str], str]] = None,
                 project: str = "") -> GroupResult:
    """同一个会话串行建群：连点两下时第二下等第一下建完，然后复用它（FC-03）。"""
    with _ensure_lock(tool, sid):
        return _ensure_group(binds, client, tool, sid, title, owner_open_id=owner_open_id,
                             clean=clean, project=project)


def _ensure_group(binds: ChatBinds, client: Any, tool: str, sid: str, title: str = "",
                  *, owner_open_id: str = "", clean: Optional[Callable[[str], str]] = None,
                  project: str = "") -> GroupResult:
    """「这个会话要有一个群」。**幂等**：同一个会话点两次复用同一个群（spec 二 2）。

    顺序是有讲究的：
    1. 已经绑过 ⇒ 先问飞书「这个群还在吗」。在 ⇒ 直接复用（连名字都不改，用户可能自己改过）。
       不在（主人解散了）⇒ 忘掉这条记录，往下走重建。
    2. 建群。没权限（实测 `99991672`）要和「飞书报错」分开报：前者得用户去后台开 scope
       再发版本，后者等一会儿再点就好。说错了用户会去改一个没问题的地方。
    3. 建成了才落盘。反过来（先记后建）会留下一条指向不存在的群的绑定。

    群名由 `group_name` 定（脱敏 → 截断 → 重名加短 id），避开已经用掉的名字。
    """
    existing = binds.of_session(tool, sid)
    if existing is not None:
        if _group_alive(client, existing.chat_id):
            return GroupResult(REUSED, existing)
        # 群没了（主人解散）⇒ 这条记录指向一个不存在的群，留着只会让消息发进空气
        logger.info("这个会话的群已经不在了，重建一个 tool=%s 会话=%s", tool, str(sid)[:8])
        binds.forget(existing.chat_id)

    name = group_name(tool, title, sid, taken=binds.names(), clean=clean, project=project)
    try:
        chat_id, code, msg = client.create(name, owner_open_id)
    except Exception as exc:  # noqa: BLE001 —— 接口约定不抛；它抛了也不能让点击崩掉
        logger.exception("建群抛异常 tool=%s", tool)
        return GroupResult(FAILED, None, 0, f"{exc}")
    try:
        code = int(code or 0)
    except (TypeError, ValueError):
        code = 0
    if not chat_id:
        how = NO_PERMISSION if code in PERMISSION_CODES else FAILED
        logger.warning("没建成群 how=%s code=%s msg=%s", how, code, str(msg)[:80])
        return GroupResult(how, None, code, str(msg or ""))
    bound = binds.bind(chat_id, tool, sid, name)
    if bound is None:
        # 闸门没过（飞书给回了一个认不出来的 chat_id）⇒ 不落盘，照实说没成
        return GroupResult(FAILED, None, code, "飞书给回的群编号认不出来")
    return GroupResult(CREATED, bound, code, str(msg or ""))


# ---------- 改绑之后把群名跟过去 ----------

# rename_group 的几种结果。调用方据此决定在群里说哪句话。
RENAMED = "renamed"    # 改成了新名字
KEPT_OWN = "kept_own"  # 主人自己改过群名 ⇒ 不碰（他的群、他起的名）
SAME = "same"          # 本来就该叫这个
NO_RENAME = "no_rename"  # 改不了（没权限 / 读不出群名 / 没这个能力）


@dataclass(frozen=True)
class RenameResult:
    how: str
    name: str = ""       # 改完（或保持）之后群实际叫什么
    code: int = 0
    msg: str = ""

    @property
    def changed(self) -> bool:
        return self.how == RENAMED


def rename_group(binds: ChatBinds, client: Any, bound: Bound, title: str = "",
                 *, clean: Optional[Callable[[str], str]] = None, project: str = "") -> RenameResult:
    """群改绑到另一个会话之后，把群名也跟过去。

    ## 为什么非要改（2026-11-05 两份 review 都提了同一条）

    一个群只对一个会话：在群里点了另一个会话就改绑。群名要是还写着**老会话**的名字，
    主人看群名以为还在跟老的说话，而每一句其实都进了新的那个 —— 这比不改名更糟。

    ## 自己改过的群名绝不覆盖

    群主是主人（`GroupSetup.create` 的 `owner_id`），他随时能改群名。改群名是**外部可见**的
    动作，冲掉他起的名字他不一定马上发现。分辨办法：`Bound.name` 记着**我们上次起的那个名**，
    和飞书上现在的名字比一比 ——
    - 一样 ⇒ 这个名字是我们起的，没人动过，可以改
    - 不一样 ⇒ 他自己改过 ⇒ `KEPT_OWN`，一个字都不碰
    - **读不出来**（没开「查看群信息」、断网）⇒ 也按 `NO_RENAME` 不碰：
      不知道他有没有动过的时候，别赌。调用方在群里把改绑的事说清楚就够了。
    """
    getter = getattr(client, "name_of", None)
    setter = getattr(client, "rename", None)
    if getter is None or setter is None:
        return RenameResult(NO_RENAME, bound.name, 0, "这台电脑上改不了群名")
    try:
        current, code, msg = getter(bound.chat_id)
    except Exception as exc:  # noqa: BLE001 —— 约定不抛；抛了也不能让改绑这一下崩掉
        logger.exception("读群名失败 chat=%s", str(bound.chat_id)[:12])
        return RenameResult(NO_RENAME, bound.name, 0, f"{exc}")
    if not current:
        # 读不出来 ⇒ 不知道他动过没动过 ⇒ 不碰
        return RenameResult(NO_RENAME, bound.name, code, str(msg or "读不出这个群现在叫什么"))
    if _clean_name(current) != _clean_name(bound.name):
        logger.info("群名是主人自己改的，不覆盖 chat=%s", str(bound.chat_id)[:12])
        return RenameResult(KEPT_OWN, current)
    want = group_name(bound.tool, title, bound.id,
                      taken=binds.names(skip_chat=bound.chat_id), clean=clean, project=project)
    if _clean_name(want) == _clean_name(current):
        binds.set_name(bound.chat_id, want)
        return RenameResult(SAME, want)
    try:
        ok, code, msg = setter(bound.chat_id, want)
    except Exception as exc:  # noqa: BLE001
        logger.exception("改群名失败 chat=%s", str(bound.chat_id)[:12])
        return RenameResult(NO_RENAME, current, 0, f"{exc}")
    if not ok:
        logger.info("没改成群名 code=%s msg=%s", code, str(msg)[:80])
        return RenameResult(NO_RENAME, current, int(code or 0), str(msg or ""))
    # 🔴 **改成了才记**。先记后改的话，改名失败会让下一次以为「现在的名字是我们起的」，
    # 于是把主人起的名字（其实飞书上还是老名字）冲掉。
    binds.set_name(bound.chat_id, want)
    return RenameResult(RENAMED, want)


def _group_alive(client: Any, chat_id: str) -> bool:
    """群还在吗。查不出来 ⇒ **当它还在**（宁可少建一个，也别每次读失败都多一个群）。"""
    checker = getattr(client, "exists", None)
    if checker is None:
        return True
    try:
        return bool(checker(chat_id))
    except Exception:  # noqa: BLE001
        logger.exception("查群还在不在失败，当它还在 chat=%s", str(chat_id)[:12])
        return True
