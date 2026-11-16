"""往飞书回消息。这一层只解决一件事：**别让平台静默吞掉结果**。

为什么单独一个文件：lark-channel-bridge issue #152 是个真实损失案例 —— agent 跑完了、
花了 0.3~1.3 美元，卡片更新却失败（飞书返回 230099 / table number over limit），代码里
只记了条日志，用户在飞书里盯着「🧠 正在思考」等到永远。

所以这里的规矩是：
- **预算按最严的算**：卡片 30KB、纯文本 150KB（官方文档）。但「若消息中包含大量样式
  标签，实际消息体长度会大于传入的请求体长度」，所以不贴着上限走。
- **超长不截断就丢** —— 截断是有损的，长 diff 截一半比不发更误导。超了转纯文本，
  再超了明确告诉用户「输出 N 字符，太长没发全」，而不是假装发完了。
- **发送失败必须让用户看见**，不能只进日志。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger("feishu-bridge.reply")

# 官方：text 请求体上限 150KB，卡片/富文本 30KB，超限错误码 230025。
# 这里按字符留足余量 —— 中文一个字 3 字节，20000 字 ≈ 60KB，仍在 150KB 内。
TEXT_SAFE_CHARS = 40000

# 下限。配置里写 0 会让 chunk_text 死循环，写 5 会把一句话拆成几十条 ——
# 两种都不是用户想要的，统一落到这个值。
MIN_SAFE_CHARS = 500


# post 富文本一条的字节预算。官方（im-v1 发送消息，2026-11-05 复查）：「卡片和**富文本**
# 请求体最大不超过 30KB」，只有 text 是 150KB；「样式标签多时实际长度会大于传入的长度」⇒ 留余量。
RICH_MAX_BYTES = 24 * 1024
# 一段拆成富文本最多几条。再多就整段走纯文本（150KB），别把聊天刷爆。
RICH_MAX_PIECES = 6


# 审批卡点完之后原地换上的「已处理」卡（第 6 轮）。outcome → (标题, header 颜色, 一句说明)。
# 颜色：同意绿、拒绝灰、掐停红。说明只讲结果，不讲命令（见 build_settled_card）。
_SETTLED = {
    "approved": ("✅ 已同意", "green", "已经让它接着做了"),
    "denied": ("🚫 没让它做", "grey", "这一步跳过了"),
    "paused": ("⏸ 会话已暂停，没批", "red", "先点「继续」看清楚，再重新发一次要求"),
    "superseded": ("🚫 已作废，这一步没做", "grey", "你后来发了新消息，还要做就再说一次"),
    # 第 32 轮：卡挂着时他打了一句话 = 回答这张卡（没批），说明换成他那句话（调用方传 note）
    "replied": ("🚫 没让它做", "grey", "你回了话，按你说的来"),
}

def build_settled_card(outcome: str, now=None, note: str = "") -> dict:
    """审批卡点完后的替换卡：标题 + 一行北京时间，**没有按钮**。

    放进卡片回调响应体的 `card: {"type": "raw", "data": <这个 dict>}`，飞书原地替换原卡。
    🔴 不放命令原文：回调里拿不到原卡正文，要放就得另外落盘一份（命令可能带凭据）。
    """
    from datetime import datetime, timedelta, timezone

    title, template, default_note = _SETTLED[outcome]
    note = note or default_note
    moment = now or datetime.now(timezone.utc)
    hm = moment.astimezone(timezone(timedelta(hours=8))).strftime("%H:%M")
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"{note} · {hm}"},
            ]
        },
    }


def _rich_bytes(text: str) -> int:
    return len(
        json.dumps({"zh_cn": {"content": [[{"tag": "md", "text": text}]]}}, ensure_ascii=False).encode("utf-8")
    )


def _fit_rich(text: str) -> list[str] | None:
    """把一段切成每条都在富文本预算内的几条（代码块不切坏）；切不下返回 None（走纯文本）。

    🔴 原来按「富文本 150KB」算，`max_reply_chars` 默认 20000 字：中文 20000 字 ≈ 60KB，
    富文本必回 230025、静默退成纯文本 —— 长回复里的代码块全变成光秃秃的反引号。
    """
    if _rich_bytes(text) <= RICH_MAX_BYTES:
        return [text]
    for n in range(2, RICH_MAX_PIECES + 1):
        pieces = md_chunk(text, max(200, -(-len(text) // n)))
        if len(pieces) <= RICH_MAX_PIECES and all(_rich_bytes(p) <= RICH_MAX_BYTES for p in pieces):
            return pieces
    return None


def _id_type(receiver: str) -> str:
    """按 id 前缀决定 `receive_id_type`。

    🔴 菜单点击事件（application.bot.menu_v6）**只带 open_id、不带 chat_id**。
    服务刚重启、或用户还没发过一条消息时，我们手里只有 open_id（`ou_` 开头）。
    原来所有发送都写死 `receive_id_type=chat_id`，拿 open_id 当 chat_id 发 ⇒
    飞书回 `230001 invalid receive_id` ⇒ 用户点了菜单**看不到任何回应**。
    群聊/单聊的 chat_id 是 `oc_` 开头，open_id 是 `ou_` 开头，前缀不会撞。
    """
    return "open_id" if (receiver or "").startswith("ou_") else "chat_id"


def chunk_text(text: str, limit: int) -> list[str]:
    """按行切，尽量不在一行中间断开；单行超长才硬切。

    🔴 `limit` 必须先收敛成正数。原来直接信任调用方，`limit <= 0` 时下面那句
    `while len(line) > limit` 每轮切 `line[:0]`（空串）、行长不变 ⇒ **死循环**，
    表现是整个 asyncio 事件循环卡住：进程活着、不报错、日志不动，要等看门狗
    15 分钟后才把它打死。而 `max_reply_chars = 0` 是能从 config.toml 一路传进来的。
    """
    limit = max(1, int(limit))
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if buf:
                chunks.append("".join(buf))
                buf, size = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        if size + len(line) > limit:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(line)
        size += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


# 「收到」用哪个表情。飞书的 emoji_type 是枚举名，不是 unicode。
# 🔴 用 `OnIt`（「在办」）而不是 `DONE`（「完成」）：这个表情的意思是**我收到了、开始干**，
# 不是「干完了」—— 贴 DONE 会让他以为已经做完（而这一轮可能要跑几分钟）。
ACK_EMOJI = "OnIt"


class Replier:
    """只管发文本。第一版刻意不用卡片。

    理由：卡片带来 30KB 上限、CardKit 的 230099、流式模式下回调无法立即更新卡片
    这一串坑，而第一版不需要按钮。等要做「危险操作点按钮批准」时再引入卡片，
    那时候这些坑是必须面对的，现在不必提前背。
    """

    def __init__(self, client, max_chars: int) -> None:
        self._client = client
        # 消息 id → 「在办」表情的 reaction_id。办完撤掉要用（见 `unack`）。
        self._acks: dict[str, str] = {}
        # 🔴 第 19 轮：贴表情的 HTTP 还在路上的那几条，和「在路上时已经办完、回来就要撤」的那几条。
        # ack 跑在后台线程、unack 跑在事件循环那边 ⇒ 两边要一把锁。
        self._acking: set[str] = set()
        self._undo_on_arrival: set[str] = set()
        self._ack_lock = threading.Lock()
        # 配置里写 0 / 负数 / 小到没法用的值，都落回一个能用的下限：
        #   - 0 和负数会让 chunk_text 死循环（见那里的红字）
        #   - 个位数会把一句话拆成几十条，等于把聊天刷爆
        # 不信任调用方是故意的 —— 这个值直接来自用户手改的 config.toml。
        requested = int(max_chars)
        if requested < MIN_SAFE_CHARS:
            logger.warning(
                "max_reply_chars=%s 太小，按 %s 处理（0 或负数会让回复层卡死）",
                requested,
                MIN_SAFE_CHARS,
            )
            requested = MIN_SAFE_CHARS
        self._max_chars = min(requested, TEXT_SAFE_CHARS)

    def ack(self, message_id: str) -> bool:
        """给主人刚发的那条消息贴一个「收到」的表情。送达返回 True。

        用户原话：「我发消息后最后有个反应 比如 get 符号 你可以参考 openclaw」。
        手机上发一句话要等十几秒到几分钟，这段时间屏幕上什么都没有 ⇒ 他分不出
        「收到了正在干」和「根本没收到」。今天那几次「没反应」的调查成本，
        一半花在这个区分上。一个表情是最便宜的「我收到了」。

        🔴 **失败一律吞掉**：没权限 / 超时 / 接口拒绝，都只是少一个表情，
        绝不影响这一轮（同 `_greet_in_background`：少一条说明 ≠ 整个功能没了）。
        🔴 权限是 `im:message.reactions:write_only`（第 18 轮才申请的，
        老用户要等自愈补上，见 `register.py` 那段红字）—— 所以**没权限是常态**，
        不是异常，日志按 info 记、不按 error。
        """
        if not message_id:
            return False
        # 🔴 第 19 轮：同一条已经挂着 / 正在贴 ⇒ 不再贴。
        # 飞书会重推同一个 message_id（`on_message` 里「事件循环还没起来、不记去重」那条路），
        # 原来第二次会再 create 一个，并把第一个的 reaction_id 覆盖掉 ⇒ 第一个永远撤不掉。
        with self._ack_lock:
            if message_id in self._acks or message_id in self._acking:
                return True
            self._acking.add(message_id)
            self._undo_on_arrival.discard(message_id)
        try:
            return self._ack_send(message_id)
        finally:
            with self._ack_lock:
                self._acking.discard(message_id)
                undo = message_id in self._undo_on_arrival
                self._undo_on_arrival.discard(message_id)
            if undo:
                # 🔴 贴还在路上的时候这一轮就办完了（秒回的命令、很快的一轮）：
                # unack 那时拿不到 reaction_id、什么都没做 ⇒ 这里补撤，不然这条永远挂着「在办」。
                self.unack(message_id)

    def _ack_send(self, message_id: str) -> bool:
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageReactionRequest,
                CreateMessageReactionRequestBody,
                Emoji,
            )

            request = (
                CreateMessageReactionRequest.builder()
                .message_id(message_id)
                .request_body(
                    CreateMessageReactionRequestBody.builder()
                    .reaction_type(Emoji.builder().emoji_type(ACK_EMOJI).build())
                    .build()
                )
                .build()
            )
            response = self._client.im.v1.message_reaction.create(request)
        except Exception:  # noqa: BLE001 —— 见上面红字
            logger.info("「收到」表情没贴上（不影响这一轮）", exc_info=True)
            return False
        ok = bool(getattr(response, "success", lambda: False)())
        if not ok:
            logger.info("「收到」表情被拒：code=%s（多半是权限还没批下来）",
                        getattr(response, "code", "?"))
            return False
        # 🔴 记下 reaction_id：办完要撤掉它，而删除接口只认这个 id
        # （`DELETE …/messages/:message_id/reactions/:reaction_id`）。
        reaction_id = getattr(getattr(response, "data", None), "reaction_id", None)
        if reaction_id:
            with self._ack_lock:
                self._acks[message_id] = reaction_id
        return True

    def pending_ack(self, message_id: str):
        """这条消息上挂着的「在办」表情的 id；没挂 ⇒ None。"""
        return self._acks.get(message_id) if message_id else None

    def unack(self, message_id: str) -> bool:
        """办完了，把「在办」撤掉。撤成返回 True。

        用户原话：「办完了应该表情应该取消啊」—— 一直挂着的话，他回头翻聊天记录时
        分不出哪条还在跑、哪条早就完了。

        🔴 失败一律吞掉：撤不掉只是多挂一个表情，不能影响已经发出去的回答。
        从没加上过（没权限、加失败）⇒ 什么都不做。
        """
        if not message_id:
            return False
        with self._ack_lock:
            reaction_id = self._acks.pop(message_id, None)
            if not reaction_id and message_id in self._acking:
                # 贴还在路上 ⇒ 记下来，等它回来（`ack` 的 finally）再撤
                self._undo_on_arrival.add(message_id)
                return False
        if not reaction_id:
            return False
        try:
            from lark_oapi.api.im.v1 import DeleteMessageReactionRequest

            request = (DeleteMessageReactionRequest.builder()
                       .message_id(message_id).reaction_id(reaction_id).build())
            response = self._client.im.v1.message_reaction.delete(request)
        except Exception:  # noqa: BLE001
            logger.info("「在办」表情没撤掉（不影响回答）", exc_info=True)
            return False
        ok = bool(getattr(response, "success", lambda: False)())
        if not ok:
            logger.info("「在办」表情撤除被拒：code=%s", getattr(response, "code", "?"))
        return ok

    def send(self, chat_id: str, text: str) -> bool:
        """发一条（必要时拆多条）。返回是否全部送达。

        🔴 **所有出站文本都在这里过一遍脱敏**（见 redact.py）。
        这是唯一出口，凭据不管怎么被读出来都从这儿走。
        2026-11-03 实测：`rg --hidden <词> .` 能把 .env / *.pem / .ssh/id_rsa
        的整行原文带回正文 —— 不弹卡片、没有 denial，因为路径 deny 只在
        命令里写出那个路径时才触发。
        """
        from .redact import redact_with_notice

        if not text.strip():
            text = "（这一轮它什么都没写出来，换个说法再问一次）"
        else:
            text = redact_with_notice(text)

        # 🔴 顺序：先脱敏（上面）→ 再排版 → 再拆条。排版把成段的 diff / 日志 / 缩进代码
        # 补上 ``` 围栏（post 的 md 不渲染缩进代码块）；拆条在代码块中间断开时会把这一条
        # 关上、下一条重新开 —— 不然后面那条整条都被染成代码（见文件末尾「排版」一节）。
        chunks = md_chunk(normalize(text), self._max_chars)
        if len(chunks) > 4:
            # 拆太多条会把聊天刷爆，而且中间失败很难对账。
            kept = chunks[:4]
            dropped = sum(len(c) for c in chunks[4:])
            kept[-1] += f"\n\n…输出过长，还有约 {dropped} 字符没发出（没有截断前面的内容）"
            chunks = kept

        all_ok = True
        for part in chunks:
            # 先试富文本（markdown 能渲染），不行再退纯文本。
            # 降级是必要的：格式难看可以接受，发不出去不行。
            # 富文本只有 30KB ⇒ 一段可能要拆成几条；每条单独降级，失败的那条换纯文本补，
            # 不会重复、也不会少发。切不下（极端长）就整段走纯文本（150KB）。
            pieces = _fit_rich(part) or [None]
            for piece in pieces:
                if piece is not None and self._send_rich(chat_id, piece):
                    continue
                if not self._send_one(chat_id, part if piece is None else piece):
                    all_ok = False
        return all_ok

    def send_approval_card(
        self, chat_id: str, title: str, body: str, approve_value: dict, deny_value: dict
    ) -> bool:
        """发一张带「同意 / 不行」按钮的卡片。

        卡片有 **30KB 上限**（比纯文本的 150KB 严得多），而且超了飞书**静默丢弃**。
        所以正文在这里再截一刀 —— 审批卡片宁可少显示，也不能发不出去：
        发不出去等于 agent 永远停在那里等一个永远不会来的点击。
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        # 🔴 卡片正文也要脱敏。2026-11-04 review：这里原来直接用 describe_action() 的
        # 输出（最多 500 字的命令原文），而 send() 文档写着"唯一出口都过脱敏" ——
        # agent 读到 .env 后把 key 拼进下一条命令，key 就原样出现在审批卡上。
        from .redact import redact

        body, _ = redact(body)
        # 先脱敏（上面）→ 再排版 + 截断：「跑这条命令」那段放进 ```bash 代码块（原来一长行
        # 折得看不清），路径用行内代码，其余文字转义成实体（命令里的 `<at id=all>`、`[x](y)`
        # 不会变成 @所有人 / 链接）。截在命令中间也会补上收尾的 ```，不会把按钮染成代码。
        # 🔴 命令文本一个字不改：用户批准的必须就是会跑的那条。
        safe_body = action_card_md(body, limit=1500)

        # 🔴 schema 2.0 的按钮写法和 1.0 完全不同，2026-11-03 实打飞书验出来的：
        #   - **不能**用 `{"tag":"action","actions":[...]}` 容器 ⇒ 飞书回
        #     `code=230099 / ErrCode: 200861 / cards of schema V2 no longer support
        #     this capability; ErrorValue: unsupported tag action`
        #   - button 直接进 elements（或放进 column_set 让两个并排）
        #   - 回调参数走 `behaviors: [{"type":"callback","value":{...}}]`，不是顶层 `value`
        # 卡片发不出去 = agent 永远停在那里等一个永远不会来的点击，所以这条必须对。
        def button(text: str, style: str, value: dict) -> dict:
            return {
                "tag": "button",
                "text": {"tag": "plain_text", "content": text},
                "type": style,
                "width": "fill",
                "behaviors": [{"type": "callback", "value": value}],
            }

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "orange",
            },
            "body": {
                "elements": [
                    {"tag": "markdown", "content": safe_body},
                    {
                        "tag": "column_set",
                        "horizontal_spacing": "8px",
                        "columns": [
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [
                                    button("同意，去做", "primary", approve_value)
                                ],
                            },
                            {
                                "tag": "column",
                                "width": "weighted",
                                "weight": 1,
                                "elements": [button("不行", "danger", deny_value)],
                            },
                        ],
                    },
                ]
            },
        }

        body_obj = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(_id_type(chat_id))
            .request_body(body_obj)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            # 卡片发不出去 = agent 会永远等着。必须显眼，并且上层要退化成纯文本。
            logger.error(
                "审批卡片发送失败 code=%s msg=%s", response.code, response.msg
            )
            return False
        # 第 32 轮：成功时带回 message_id（真值），卡挂着时他打字 ⇒ 靠它把这张卡原地换掉。
        # 拿不到 id 时仍回 True，调用方只把它当「发出去了」。
        data = getattr(response, "data", None)
        return getattr(data, "message_id", None) or True

    def patch_settled_card(self, message_id: str, outcome: str, note: str = "") -> bool:
        """把一张审批卡原地换成「已处理」卡（没有按钮）。失败返回 False，调用方要有降级。

        点按钮时走的是回调响应体换卡（app._settled）；这里是**没人点、但已经有了结果**的时候
        （卡挂着时他打了一句话）用 PATCH 换。schema 2.0 卡默认可更新（同 patch_status_card）。
        """
        if not message_id or not isinstance(message_id, str):
            return False
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        card = build_settled_card(outcome, note=note)
        body = PatchMessageRequestBody.builder().content(json.dumps(card, ensure_ascii=False)).build()
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        try:
            response = self._client.im.v1.message.patch(request)
        except Exception:  # noqa: BLE001 —— 换卡失败不能连累这一轮
            logger.exception("审批卡原地更新抛异常")
            return False
        if not response.success():
            logger.warning("审批卡原地更新失败 code=%s msg=%s", response.code, response.msg)
            return False
        return True

    def send_card(self, chat_id: str, card_json: str) -> bool:
        """发一张现成的卡片（菜单之类）。失败返回 False，调用方要有降级。"""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(card_json)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(_id_type(chat_id))
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.warning("卡片发送失败 code=%s msg=%s", response.code, response.msg)
            return False
        return True

    def send_card_id(self, chat_id: str, card_json: str) -> str | None:
        """发一张现成的卡片，返回 message_id（会话群状态卡要靠它原地更新）。失败返回 None。"""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(card_json)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(_id_type(chat_id))
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.warning("卡片发送失败 code=%s msg=%s", response.code, response.msg)
            return None
        data = getattr(response, "data", None)
        return getattr(data, "message_id", None) if data else None

    def patch_card(self, message_id: str, card_json: str) -> bool:
        """原地换掉一张卡片的内容。失败返回 False。"""
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        body = PatchMessageRequestBody.builder().content(card_json).build()
        request = PatchMessageRequest.builder().message_id(message_id).request_body(body).build()
        response = self._client.im.v1.message.patch(request)
        if not response.success():
            logger.info("卡片原地更新失败 code=%s msg=%s", response.code, response.msg)
            return False
        return True

    def send_status_card(self, chat_id: str, text: str, with_stop: bool = True) -> str | None:
        """发一张状态卡，返回 message_id（之后靠它原地更新）。失败返回 None。

        状态卡很小（几百字节），不碰 30KB 上限。结果仍然走纯文本。
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        from .status import build_status_card

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(build_status_card(text, with_stop=with_stop))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(_id_type(chat_id))
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.warning("状态卡发送失败 code=%s msg=%s", response.code, response.msg)
            return None
        data = getattr(response, "data", None)
        return getattr(data, "message_id", None) if data else None

    def patch_status_card(self, message_id: str, text: str, with_stop: bool = True) -> bool:
        """原地更新状态卡。失败返回 False（调用方会停止再试）。"""
        from lark_oapi.api.im.v1 import (
            PatchMessageRequest,
            PatchMessageRequestBody,
        )

        from .status import build_status_card

        body = PatchMessageRequestBody.builder().content(build_status_card(text, with_stop=with_stop)).build()
        request = (
            PatchMessageRequest.builder()
            .message_id(message_id)
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.patch(request)
        if not response.success():
            logger.debug("状态卡更新失败 code=%s msg=%s", response.code, response.msg)
            return False
        return True

    def _send_rich(self, chat_id: str, text: str) -> bool:
        """用 `post` 富文本发，让 markdown 真的渲染出来。

        🔴 为什么不用纯 `text`：agent 的输出里到处是 `**粗体**`、`` `代码` ``、
        代码块和列表。用 `text` 类型发过去，用户看到的是光秃秃的星号和反引号 ——
        每条消息都这样，可读性很差。

        选 `post` 而不是卡片的 `markdown` 元素（两种实打都能发）：post 超限会**明确报错**
        （230025）、我们能退纯文本；卡片超限是**静默丢弃**。
        🔴 两者额度一样都是 **30KB**（官方发送消息文档，2026-11-05 复查；原来这里写 150KB
        是错的，150KB 只是 text）。所以 `send()` 先用 `_fit_rich` 把一段切到预算内。

        post 的 md 实际支持（官方 message-content-description）：CommonMark 0.31 + GFM，
        围栏代码块（```lang）、行内代码、表格、引用、列表；**不支持缩进代码块和 raw HTML**
        ⇒ 缩进代码由 `normalize` 转成围栏。

        失败返回 False，调用方会降级成纯文本 —— 宁可格式难看，不能发不出去。
        """
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        content = json.dumps(
            {"zh_cn": {"content": [[{"tag": "md", "text": text}]]}},
            ensure_ascii=False,
        )
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("post")
            .content(content)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(_id_type(chat_id))
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.warning(
                "富文本发送失败 code=%s msg=%s，降级成纯文本",
                response.code,
                response.msg,
            )
            return False
        return True

    def _send_one(self, chat_id: str, text: str) -> bool:
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(_id_type(chat_id))
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success():
            # 这条必须显眼：它代表「活干完了但用户看不到」，是最坑的失败模式。
            logger.error(
                "飞书回消息失败 code=%s msg=%s chars=%s",
                response.code,
                response.msg,
                len(text),
            )
            return False
        return True

# =====================================================================
# 排版：代码 / 命令输出 / diff / 日志包成代码块，截断不留半个代码块
# =====================================================================
#
# 飞书消息排版：代码 / 命令输出 / diff / 日志包成代码块，截断不留半个代码块。
#
# 用户原话（11-05）：「发卡片的时候，尽量优化下里面的内容，比如代码就用代码的 markdown 类似的格式。」
#
# ## 飞书实际支持什么（2026-11-05 查的官方文档，不是凭印象）
#
# - **卡片 JSON 2.0 的 markdown 组件**（content-components/rich-text）：支持 ```` ```lang ```` 代码块
#   （「语法必须在行首」，语言名不区分大小写，没写就是 plain_text）、行内 `` `code` ``、
#   4 空格缩进也会变代码块。特殊字符要显示原样，文档给的办法是 **HTML 实体**（`&#60;` 之类）。
# - **post 富文本的 `md` 标签**（im-v1 message-content-description）：CommonMark 0.31 + GFM，
#   支持围栏代码块和行内代码；**不支持缩进代码块、不支持任意 raw HTML**。
# - **大小**：发消息接口写明「卡片和富文本请求体不超过 30KB」，只有 text 是 150KB。
#
# ## 这个文件只做三件事
#
# 1. `normalize` —— 把代码块统一成 ```` ```lang ```` 一种写法；成段的 diff、带时间戳的日志、
#    Python traceback、缩进代码（post 里不渲染）补上围栏。
# 2. `truncate` / `chunk` —— 按块截断 / 拆条：**截在代码块里就先补上收尾的 ```**，
#    否则后面整张卡 / 整条消息都会被染成代码。
# 3. `render_card` —— 卡片里的不可信文字（会话消息、命令）：代码块原样，其余部分把会变成
#    标签 / 链接 / 图片 / 标题的字符转成实体。`<at id=all>` 这种原样进卡片的话，
#    没开「@所有人」权限的群里**整张卡发不出去**（文档原话）。
#
# 🔴 顺序：**先脱敏 → 再排版 → 再截断 → 最后转义**。脱敏必须在最前（截断会把私钥块的
# END 行切掉、整块匹配不上，见 `fleet/cards.py` 的红字）；转义必须在截断之后，不然截断会把
# `&#60;` 切成半个实体。
#
# 🔴 放在 reply.py 而不是单独一个模块：新模块要等 build.sh 重新出安装器产物才装得上，
# 产物没重出之前 test_install_completeness 会红；放进已经在装的文件里没有这个窗口期。

FENCE = "```"
# 代码里出现的 ``` 会提前把块关掉。插一个零宽空格打断它 —— 看起来一样，只是不再是围栏。
_ZWSP = "​"

# ---------- 语言名 ----------

# 飞书卡片文档列出的语言（小写）。post 文档列的是其中常用的一部分，不区分大小写。
_LANGS = frozenset("""
plain_text abap ada apache apex assembly bash c_sharp cpp c cmake cobol css coffee_script d dart
delphi diff django docker_file erlang fortran gherkin go graphql groovy html htmlbars http haskell
json java javascript julia kotlin latex lisp lua matlab makefile markdown nginx objective_c
opengl_shading_language php perl powershell prolog properties protobuf python r ruby rust sas scss
sql scala scheme shell solidity swift toml thrift typescript vbscript visual_basic xml yaml
""".split())

_LANG_ALIASES = {
    "sh": "bash", "zsh": "bash", "console": "bash", "shell-session": "bash", "bat": "shell",
    "cmd": "shell", "ps1": "powershell", "pwsh": "powershell",
    "py": "python", "python3": "python", "py3": "python", "ipython": "python",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "node": "javascript",
    "ts": "typescript", "tsx": "typescript",
    "yml": "yaml", "jsonc": "json", "json5": "json", "jsonl": "json",
    "dockerfile": "docker_file", "docker": "docker_file",
    "c#": "c_sharp", "cs": "c_sharp", "csharp": "c_sharp", "c++": "cpp", "cc": "cpp", "hpp": "cpp",
    "h": "c", "objc": "objective_c", "objective-c": "objective_c",
    "md": "markdown", "rb": "ruby", "rs": "rust", "kt": "kotlin", "golang": "go",
    "patch": "diff", "udiff": "diff", "htm": "html", "vb": "visual_basic", "proto": "protobuf",
    "make": "makefile", "tf": "", "hcl": "",
    "text": "", "txt": "", "plain": "", "plaintext": "", "plain_text": "", "log": "",
    "output": "", "none": "",
}


def normalize_lang(info: str) -> str:
    """围栏后面的 info string → 飞书认的语言名；认不出就空（= 纯文本）。

    取第一个词（```` ```python title="x" ```` 这种写法），别名映射，不在飞书列表里一律空：
    纯文本显示总比一个飞书不认的语言名可靠。
    """
    word = (info or "").strip().split(" ", 1)[0].strip().lower()
    word = word.strip("{}.")
    if word in _LANG_ALIASES:
        return _LANG_ALIASES[word]
    return word if word in _LANGS and word != "plain_text" else ""


# ---------- 块 ----------


@dataclass
class Block:
    kind: str  # "text" | "code"
    body: str
    lang: str = ""


_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _defuse_at(text: str) -> str:
    """`<at id=all>` 在没开「@所有人」权限的群里会让**整张卡发不出去**（卡片文档原话）。
    代码块 / 行内代码里会不会被解析，文档没写 ⇒ 不赌，打断成 `<​at`（看起来一样）。"""
    return re.sub(r"<(\s*/?\s*at\b)", "<" + _ZWSP + r"\1", text, flags=re.I)


def _safe_code(body: str) -> str:
    """代码里的 ``` / ~~~ 打断成看起来一样、但不再是围栏的写法。"""
    body = re.sub(r"`{3,}", lambda m: _ZWSP.join(m.group(0)), body)
    body = re.sub(r"^( {0,3})(~{3,})", lambda m: m.group(1) + _ZWSP.join(m.group(2)), body, flags=re.M)
    return _defuse_at(body)


def fence(code: str, lang: str = "") -> str:
    """包成一个代码块。代码里本来就有的 ``` 不会把块提前关掉。"""
    code = (code or "").rstrip("\n")
    return f"{FENCE}{normalize_lang(lang)}\n{_safe_code(code)}\n{FENCE}"


def inline_code(text: str) -> str:
    """行内反引号（路径、命令、会话 id）。里面的反引号换成 ' —— 行内代码里没法转义。"""
    flat = " ".join(str(text or "").replace("`", "'").split())
    return f"`{flat}`" if flat else ""


def split_blocks(text: str) -> list[Block]:
    """markdown → 文字块 / 代码块。没关上的围栏按 CommonMark 一直算到结尾。"""
    blocks: list[Block] = []
    buf: list[str] = []
    code: list[str] = []
    opener = ""  # 当前围栏（``` 或 ~~~ 串）；空 = 不在代码块里
    lang = ""
    for line in (text or "").split("\n"):
        if not opener:
            m = _OPEN_RE.match(line)
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                if buf:
                    blocks.append(Block("text", "\n".join(buf)))
                    buf = []
                opener, lang, code = m.group(1), normalize_lang(m.group(2)), []
                continue
            buf.append(line)
            continue
        stripped = line.strip()
        if (stripped and set(stripped) == {opener[0]} and len(stripped) >= len(opener)
                and len(line) - len(line.lstrip(" ")) <= 3):
            blocks.append(Block("code", "\n".join(code), lang))
            opener = ""
            continue
        code.append(line)
    if opener:
        blocks.append(Block("code", "\n".join(code), lang))
    elif buf:
        blocks.append(Block("text", "\n".join(buf)))
    return blocks


def join_blocks(blocks: list[Block]) -> str:
    out = []
    for b in blocks:
        out.append(fence(b.body, b.lang) if b.kind == "code" else b.body)
    return "\n".join(out)


# ---------- 没包围栏的成段代码 ----------

_DIFF_LINE = re.compile(r"^(diff --git |index [0-9a-f]|--- |\+\+\+ |@@ |[-+ ]|\\ No newline|$)")
_DIFF_ANCHOR = re.compile(r"^(diff --git |@@ -\d|\+\+\+ )")
# 要带秒：「2026-11-05 11:37 做了 X」这种时间线是正文，不是日志
_LOG_LINE = re.compile(r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
_TRACEBACK = "Traceback (most recent call last):"
_LIST_LINE = re.compile(r"^\s*([-*+]|\d+[.)])\s")


def _grab_diff(lines: list[str], i: int) -> int:
    """从 i 开始的 diff 段到哪结束（不含）。必须带 `diff --git` / `@@` / `+++` 才算 ——
    不然「- 第一条」这种普通列表会被误认成 diff。"""
    j = i
    while j < len(lines) and lines[j] != "" and _DIFF_LINE.match(lines[j]):
        j += 1
    seg = lines[i:j]
    if len(seg) >= 3 and any(_DIFF_ANCHOR.match(s) for s in seg):
        return j
    return i


def _grab_log(lines: list[str], i: int) -> int:
    j = i
    while j < len(lines) and _LOG_LINE.match(lines[j]):
        j += 1
    return j if j - i >= 2 else i


def _grab_traceback(lines: list[str], i: int) -> int:
    if lines[i].strip() != _TRACEBACK:
        return i
    j = i + 1
    while j < len(lines) and lines[j][:1] in (" ", "\t"):
        j += 1
    if j < len(lines) and lines[j].strip():
        j += 1  # 最后那行「XxxError: …」
    return j


def _grab_indented(lines: list[str], i: int, prev_nonblank: str) -> int:
    """缩进 4 格的代码（post 不渲染，要转成围栏）。上一段是列表 / 本身也缩进 ⇒ 是续行，不动。"""
    if not (lines[i].startswith("    ") or lines[i].startswith("\t")):
        return i
    if i > 0 and lines[i - 1].strip():
        return i  # 段落里的缩进是续行，不是代码块（CommonMark 规则）
    if prev_nonblank and (_LIST_LINE.match(prev_nonblank) or prev_nonblank[:1] in (" ", "\t")):
        return i
    j = i
    while j < len(lines) and (lines[j].startswith("    ") or lines[j].startswith("\t") or not lines[j].strip()):
        j += 1
    while j > i and not lines[j - 1].strip():
        j -= 1
    return j


def _dedent(seg: list[str]) -> str:
    return "\n".join(s[4:] if s.startswith("    ") else s[1:] if s.startswith("\t") else s for s in seg)


def _wrap_loose(text: str) -> list[Block]:
    lines = text.split("\n")
    out: list[Block] = []
    buf: list[str] = []
    prev_nonblank = ""
    i = 0

    def flush():
        if buf:
            out.append(Block("text", "\n".join(buf)))
            buf.clear()

    while i < len(lines):
        for grab, lang, dedent in (
            (_grab_diff, "diff", False),
            (_grab_traceback, "", False),
            (_grab_log, "", False),
        ):
            j = grab(lines, i)
            if j > i:
                break
        else:
            j = _grab_indented(lines, i, prev_nonblank)
            lang, dedent = "", True
        if j > i:
            flush()
            seg = lines[i:j]
            out.append(Block("code", _dedent(seg) if dedent else "\n".join(seg), lang))
            prev_nonblank = ""
            i = j
            continue
        buf.append(lines[i])
        if lines[i].strip():
            prev_nonblank = lines[i]
        i += 1
    flush()
    return out


def parse(text: str, detect: bool = True) -> list[Block]:
    """markdown → 块；`detect=True` 时把没包围栏的 diff / 日志 / traceback / 缩进代码也认成代码块。"""
    blocks: list[Block] = []
    for b in split_blocks(text):
        if b.kind == "text" and detect:
            blocks.extend(_wrap_loose(b.body))
        else:
            blocks.append(b)
    return blocks


def normalize(text: str, detect: bool = True) -> str:
    """统一代码块写法 + 补围栏。给 post 富文本（回复）用。

    post 的 md 支持 `<at user_id="all">` 扩展语法 ⇒ agent 输出里带一句（读到的网页、
    文档里有）就会真的 @ 全群。全文打断，代码块里外都一样。
    """
    return _defuse_at(join_blocks(parse(text, detect)))


# ---------- 截断 / 拆条 ----------

TRUNC_MARK = "…（后面还有，没显示全）"
_NORM_OPEN = re.compile(r"^```[a-z_]*$")


def truncate_blocks(blocks: list[Block], limit: int, max_lines: int = 0) -> tuple[list[Block], bool]:
    """按字符数（和行数）截断。截在代码块里 ⇒ 这块照样会被 `fence()` 关上，不会漏 ```。

    尽量在行尾截；一行就超了才在行中间截。返回 (块, 有没有截掉东西)。
    """
    limit = max(1, int(limit))
    out: list[Block] = []
    used = 0
    lines_used = 0
    for b in blocks:
        room = limit - used
        line_room = (max_lines - lines_used) if max_lines else 10 ** 9
        if room <= 0 or line_room <= 0:
            return out, True
        body_lines = b.body.split("\n")
        if len(b.body) <= room and len(body_lines) <= line_room:
            out.append(b)
            used += len(b.body) + 1
            lines_used += len(body_lines) + (2 if b.kind == "code" else 0)
            continue
        kept: list[str] = []
        size = 0
        for line in body_lines[:max(line_room, 1)]:
            if size + len(line) > room:
                if not kept:
                    kept.append(line[:room].rstrip())
                break
            kept.append(line)
            size += len(line) + 1
        body = "\n".join(kept).rstrip()
        if body:
            out.append(Block(b.kind, body, b.lang))
        return out, True
    return out, False


def truncate(text: str, limit: int, max_lines: int = 0, detect: bool = True) -> str:
    """截断 markdown，不留没闭合的代码块。截了就在后面补一句「没显示全」。"""
    blocks, cut = truncate_blocks(parse(text, detect), limit, max_lines)
    out = join_blocks(blocks)
    return (out + "\n" + TRUNC_MARK) if cut else out


def md_chunk(text: str, limit: int) -> list[str]:
    """拆成若干条，每条大约 ≤ limit 字符，**每条里的代码块都是闭合的**。

    在代码块中间拆 ⇒ 这一条末尾补 ```，下一条开头重新开 ```lang。尽量按行拆，一行就超长才硬切。
    输入要先过 `normalize`（那之后围栏只有行首的 ``` 一种，代码里的 ``` 已被打断）。
    """
    limit = max(1, int(limit))
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    opener = ""  # 当前在哪个代码块里（"```lang" 那一行）；空 = 不在

    def emit(reopen: bool) -> None:
        nonlocal buf, size
        if opener and buf and buf[-1] == opener:
            buf.pop()  # 刚开了块还没内容就要拆：这一行挪到下一条，别留个空代码块
        fences = [line for line in buf if line == FENCE or _NORM_OPEN.match(line)]
        if any(line.strip() and line not in fences for line in buf):
            # 这一条里围栏是奇数 = 结尾停在代码块里 ⇒ 补上收尾
            chunks.append("\n".join(buf + ([FENCE] if len(fences) % 2 else [])))
        buf = [opener] if (opener and reopen) else []
        size = sum(len(line) + 1 for line in buf)

    for line in text.split("\n"):
        # normalize 之后的围栏只有两种行：开 = ```lang（lang 只含小写字母和 _），关 = ```
        is_fence = (line == FENCE) if opener else bool(_NORM_OPEN.match(line))
        # 在代码块里：要给这一条末尾的 ``` 留位置
        reserve = len(FENCE) + 1 if (opener and not is_fence) else 0
        cap = max(1, limit - (len(opener) + 1 if opener else 0) - reserve - 1)
        pieces = [line[i:i + cap] for i in range(0, len(line), cap)] or [""]
        for piece in pieces:
            if buf and size + len(piece) + 1 + reserve > limit:
                emit(reopen=True)
            buf.append(piece)
            size += len(piece) + 1
        if is_fence:
            opener = "" if opener else line
    emit(reopen=False)
    return chunks or [text[:limit]]


# ---------- 卡片里的不可信文字 ----------

# 飞书卡片文档的转义表（HTML 实体）。只转「会变成格式 / 标签 / 链接 / 图片 / 标题」的那些。
_CARD_ENTITIES = {
    "\\": "&#92;", "<": "&#60;", ">": "&#62;", "[": "&#91;", "]": "&#93;",
    "#": "&#35;", "_": "&#95;", "~": "&sim;", "*": "&#42;", "`": "&#96;",
}
_KEEP_FORMAT = frozenset("*`")  # 消息正文里留着粗体和行内代码
_INLINE_CODE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)")
# 原文里本来就有的「&#60;」「&amp;」：不转的话会被渲染成 < / &，显示的不是原文
_ESCAPE_RE = re.compile(r"&(?=#?\w+;)|[" + re.escape("".join(_CARD_ENTITIES)) + "]")


def escape_card(text: str, keep_format: bool = False) -> str:
    """卡片 markdown 里原样显示一段文字（转成实体）。

    `keep_format=True`（会话消息正文）：`**粗体**` 和 `` `行内代码` `` 照常渲染，
    行内代码里面原样不转；其它会变成标签 / 链接 / 图片 / 标题的字符照样转。
    """
    text = str(text or "")
    if not keep_format:
        return _escape_plain(text, keep=frozenset())
    out: list[str] = []
    pos = 0
    for m in _INLINE_CODE.finditer(text):
        out.append(_escape_plain(text[pos:m.start()]))
        out.append(_defuse_at(m.group(0)))
        pos = m.end()
    out.append(_escape_plain(text[pos:]))
    return "".join(out)


def _escape_plain(text: str, keep: frozenset = _KEEP_FORMAT) -> str:
    """一遍扫完：替换出来的实体不会被再转一次（`&#60;` 里的 # 不会变成 `&#35;`）。"""
    def sub(m: re.Match) -> str:
        ch = m.group(0)
        if ch == "&":
            return "&#38;"  # 只有「像实体的 &」才会匹配到这里
        return ch if ch in keep else _CARD_ENTITIES[ch]

    return _ESCAPE_RE.sub(sub, text)


def render_card(blocks: list[Block], keep_format: bool = True) -> str:
    """块 → 卡片 markdown：代码块原样包围栏，文字部分转义。"""
    out = []
    for b in blocks:
        if b.kind == "code":
            out.append(fence(b.body, b.lang))
        else:
            out.append(escape_card(b.body, keep_format=keep_format))
    return "\n".join(out)


def card_snippet(text: str, limit: int, max_lines: int = 0) -> str:
    """一段不可信文字 → 卡片 markdown：排版 → 截断 → 转义（调用方先脱敏）。"""
    blocks, cut = truncate_blocks(parse(text), limit, max_lines)
    out = render_card(blocks)
    return (out + "\n" + TRUNC_MARK) if cut else out


# ---------- 审批卡片 ----------

_ACTION_BASH = re.compile(r"^(跑这条命令：)\n(.*)$", re.S)
_ACTION_FILE = re.compile(r"^((?:新建/覆盖|修改)文件：)(.+)$")
_ACTION_JSON = re.compile(r"^([A-Za-z_][\w.:-]*：)(\{.*)$", re.S)


def action_card_md(body: str, limit: int = 1500) -> str:
    """审批卡「要动手了」那段 → 卡片 markdown。命令放进代码块，路径用行内代码。

    认的是 `approval.describe_action()` 的三种写法；认不出（或已经带了代码块）就按普通
    不可信文字排版。🔴 调用方先脱敏；截断在这里（截在命令中间也会补上收尾的 ```）。
    🔴 命令文本**一个字都不改**（不折行、不加 `\\`）：用户批准的必须就是会跑的那条。
    """
    body = str(body or "")
    # 命令 / JSON 里自带的 ``` 由 fence() 打断，不会把块提前关掉 ⇒ 先按这三种写法认
    m = _ACTION_BASH.match(body)
    if m:
        return _head_and_code(m.group(1), m.group(2), "bash", limit)
    m = _ACTION_FILE.match(body)
    if m and "\n" not in body:
        return escape_card(m.group(1)) + _defuse_at(inline_code(_clip(m.group(2), limit)))
    m = _ACTION_JSON.match(body)
    if m:
        return _head_and_code(m.group(1), m.group(2), "json", limit)
    return card_snippet(body, limit)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(limit - 1, 1)] + "…"


def _head_and_code(head: str, code: str, lang: str, limit: int) -> str:
    code = code.rstrip("\n")
    cut = len(code) > limit
    if cut:
        code = code[:limit]
    out = escape_card(head) + "\n" + fence(code, lang)
    return out + ("\n" + TRUNC_MARK if cut else "")
