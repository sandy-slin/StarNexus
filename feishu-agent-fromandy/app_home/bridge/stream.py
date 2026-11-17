"""流式回答：一边生成一边往飞书卡片上推，不用等一整轮跑完。

用户原话（催过多次）：「在和飞书机器人交流的时候改成流式输出」
「流式信息发送还没有吗？我都提了好多次了」。

## 为什么值得做（实测，第 17 轮角色 F）

桥的生产模型是 `claude-sonnet-5-5`，同一句 prompt 跑三次：首个文字 2.6~12.2 秒出现，
之后在 20 多秒里陆续到齐。也就是说**原来那 20 多秒屏幕上是一片空白**，而文字其实
早就在往外吐了。（haiku 上是结尾一次性爆发，流式没收益但也不坏。）

## 这个文件的规矩：坏了就安静退回老路

照 `status.py` 那条：**失败一律静默降级，绝不拖垮这一轮**。流式是增益，不是替换 ——
缺权限、限频、超时、接口变了，任何一样都只会让这一轮回到「跑完发一条」，
用户最多少看到打字效果，绝不会少看到答案。`broken` 一旦为真就再也不碰卡片。

🔴 **老用户大概率没有权限**（`cardkit:card:write` 是第 18 轮才加进申请清单的），
所以「没权限」是常态、不是异常：日志按 info 记，不报错、不打扰用户。

## 两条必须守的

1. **脱敏在推之前，而且要扣尾**。中间帧天然是前缀，`sk-…` 这种凑不够长度就匹配不上
   ⇒ 半截凭据已经上屏，而飞书服务端收到就等于发布了。判据和实测数字在
   `tests/test_r17_stream_redact.py`（逐帧前缀里**任何一帧**都不许含凭据片段）。
2. **序号全局串行**。卡片的每次更新都要带递增 `sequence`，收尾和最后一帧并发就会撞
   `300317`。所以所有调用都从同一个计数器取号、在同一把锁里发出。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Optional

logger = logging.getLogger("feishu-bridge.stream")

# 节流：既不能太密（限频 10 次/秒一张卡，而且手机上刷得眼花），
# 也不能太疏（那就失去「在动」的意义）。F 实测建议 ≥0.6 秒且 ≥24 字，
# 段末（换行）提前推一次 —— 不按「句子边界」：中文长答里句末标点会连着来，把节流打穿。
MIN_INTERVAL = 0.6
MIN_CHARS = 24
# 🔴 第 19 轮：两帧之间的**硬下限**，段末提前推也不许低于它。
# 实测（r19-stream.md §5）：段末那条捷径不看 MIN_INTERVAL ⇒ 真正挡住「单卡 10 次/秒」的
# 只剩 app.py 里 stream_pump 的 0.2 秒轮询。把轮询改成 0.05 秒，纯英文短段落就是 22 次/秒。
# 0.25 秒 ⇒ 推文字 ≤4 次/秒，加上收尾那两发（写终态 + 关流）一秒内也 ≤6 次。
MIN_GAP = 0.25
# 单卡正文上限（字数）。
MAX_BODY = 12000
# 🔴 第 19 轮：**按字节**也要限。飞书写的是「卡片大小控制在 30KB 以内」，而中文一个字
# UTF-8 是 3 字节 ⇒ 12000 个中文字 = 36KB，`MAX_BODY` 那道字数闸门根本拦不住（实测
# 假 client 推上去的最后一帧 12017 字 / 35527 字节）。按 JSON 转义后的字节数算，
# 留 6KB 给卡片结构和收尾那行。中文约 8000 字（正是第 17 轮 §5 建议的 8000 字）。
MAX_BYTES = 24000
# 收尾时卡片被限频 / 正在被点 ⇒ 等一下再写。全失败就返回「没交付」，调用方照旧发消息。
_FINAL_RETRY_DELAYS = (0.3, 0.8, 1.5)
# 元素 id：建卡时写死，之后每次推都往这一个元素里塞全量文本。
ELEMENT_ID = "answer"

# 扣尾：这一帧末尾那个**还没写完的词**先别推，下一帧补上。
#
# 🔴 第 19 轮热修：原来靠「凭据字符集」判（`[A-Za-z0-9_\-+/=:@.~]`），值守实测漏了 ——
# `密码 password=Xy9#kL2$mN8!qR` 有一帧推上去的是 `密码 password=Xy9#`（泄了前 3 位），
# 因为 `#` 不在字符集里，扣尾停在了它前面。**枚举字符集永远会漏**（下一个人的密码
# 带 `%`、`^`、`&`…），所以改成「从最后一个分隔处之后全扣」，不管是什么字符。
#
# 分隔处 = 空白 或 中文标点。中文为什么要单独算：中文句子里没有空格，只认空白的话
# 中文那一段会被**整段**扣住直到出现换行，流式就白做了；而凭据不会紧贴着中文字写。
_BOUNDARY = re.compile(r"[\s，。！？；：、（）【】「」《》“”‘’…]")


def hold_tail(text: str) -> str:
    """扣住末尾还没写完的那个词（见上面）。只用于**中间帧**。"""
    last = None
    for last in _BOUNDARY.finditer(text):
        pass
    if last is None:
        return ""                 # 一个分隔都没有 ⇒ 整段都可能是凭据，先一个字都别推
    return text[: last.end()]


def _size(text: str) -> int:
    """这段正文进卡片后占多少字节（JSON 转义后、UTF-8）。换行 / 引号转义后会变长。"""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def _too_long(text: str) -> bool:
    return len(text) > MAX_BODY or _size(text) > MAX_BYTES


def _head(text: str, note: str) -> str:
    """装不下 ⇒ 留开头 + 一句说明，整体保证装得下（字数和字节都算）。"""
    budget_chars = MAX_BODY
    budget_bytes = MAX_BYTES - _size("\n\n" + note)
    lo, hi = 0, min(len(text), budget_chars)
    while lo < hi:                      # 二分找「最多能留多少字」
        mid = (lo + hi + 1) // 2
        if _size(text[:mid]) <= budget_bytes:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "\n\n" + note


# 装不下时卡上那句话。终态和中间帧说法不同：中间帧时全文还没写完。
_NOTE_FINAL_OVERFLOW = "…（太长了，卡片放不下，完整内容在下面那条消息里）"
_NOTE_LIVE_OVERFLOW = "…（太长了，卡片放不下，写完后完整内容会另发一条）"


def _redacted(text: str) -> str:
    from .redact import redact

    got = redact(text)
    return got[0] if isinstance(got, (tuple, list)) else got


def _safe_final(text: str) -> str:
    """**终态**全文：只脱敏 + 限长，**不扣尾**。

    🔴 第 19 轮热修：原来终态也走 `_safe_body`（里面有扣尾）⇒ 答案结尾被吞掉：
        'All done. See README.md' → 'All done. See '；'Done.' → ''
    而 `app.py` 拿到 finish 成功就不再发消息 ⇒ 用户看到的就是截断版、没有兜底。
    扣尾只对中间帧有意义（下一帧会补上）；终态没有下一帧了。
    """
    clean = _redacted(text)
    if _too_long(clean):
        clean = _head(clean, _NOTE_FINAL_OVERFLOW)
    return clean


def _hold_open_secret_value(text: str) -> str:
    """末尾有一个**还没闭合**的 `"<凭据键名>": "…` ⇒ 从值的开头扣到末尾。

    🔴 第 24 轮（R-stream 实测）：`{"password": "correct horse battery staple"}` 这种带空格的值，
    `_JSON_FIELD` 要等到收尾引号才匹配，而 `hold_tail` 在空格处放行 ⇒ 中间帧逐词把口令推上了飞书。
    """
    from .redact import _SECRET_KEY_WORDS

    found = None
    for found in re.finditer(
            rf"\"[A-Za-z0-9_-]*(?:{_SECRET_KEY_WORDS})[A-Za-z0-9_-]*\"\s*:\s*\"",
            text, re.IGNORECASE):
        pass
    if found is not None and '"' not in text[found.end():]:
        return text[: found.end()]
    return text


def _safe_body(text: str) -> str:
    """**中间帧**推之前过一遍：脱敏 → 扣尾 → 限长。顺序不能换（扣尾要在脱敏之后）。"""
    clean = hold_tail(_hold_open_secret_value(_redacted(text)))
    if _too_long(clean):
        clean = _head(clean, _NOTE_LIVE_OVERFLOW)
    return clean


def _preview(text: str) -> str:
    """聊天列表里那行预览：正文第一行的开头。

    🔴 第 19 轮：飞书文档原话「关闭流式更新模式并不会改变开发者自定义的摘要文本」——
    建卡时写的「正在回答…」在关流之后**还会一直挂在聊天列表里**，必须收尾时自己改掉。
    """
    for line in _redacted(text).splitlines():
        line = line.strip().lstrip("#>*- ").strip()
        if line:
            return line[:40]
    return "（回答）"


# `_put_content` 的返回值之一：这一帧被飞书跳过了（限频 / 卡片正在被点）。只在本文件里流转。
_SKIPPED = "skipped"


class StreamReply:
    """一轮对话的流式卡片。用不了就 `broken`，调用方回到老路。

    `client` 是 lark 的 SDK 客户端；`replier` 只用来在装不下时退回发文本。
    `enabled=False` ⇒ 整个关掉（给开关和测试用）。
    """

    def __init__(self, client: Any, chat_id: str, *, enabled: bool = True,
                 now=time.monotonic) -> None:
        self._client = client
        self._chat_id = chat_id
        self._enabled = bool(enabled)
        self._now = now
        self._card_id: Optional[str] = None
        self._message_id: Optional[str] = None
        self._seq = 0
        self._lock = asyncio.Lock()
        self._broken = not self._enabled
        self._last_push = 0.0
        self._last_len = 0
        self._shown = ""         # 卡上现在显示的那段（已脱敏）。停下 / 没跑完时收尾用它
        self._closed = False
        self._final_retry_delays = _FINAL_RETRY_DELAYS
        self.pushes = 0          # 给测试和日志看：这一轮真推了几帧

    @property
    def broken(self) -> bool:
        return self._broken

    @property
    def live(self) -> bool:
        """卡片已经建起来、还能推。"""
        return bool(self._card_id) and not self._broken

    def idle_sec(self) -> Optional[float]:
        """离上一次真推上去过了几秒。卡没起来 / 一帧都没推过 ⇒ None。

        给 app.py 的状态卡用（第 19 轮 §1）：回答卡还在动 ⇒ 用户已经知道它活着，
        不必再冒出一张「还在跑」；回答卡停住了（实测工具调用中间能停 107 秒）⇒ 状态卡该出来。
        """
        if not self.live or not self.pushes:
            return None
        return max(self._now() - self._last_push, 0.0)

    # ---------- 内部：所有卡片调用都走这里 ----------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    async def _call(self, what: str, fn, *args) -> Any:
        """在锁里串行发一次卡片请求。抛异常 ⇒ 记一行 info 并返回 None（不往外抛）。"""
        try:
            async with self._lock:
                return await asyncio.to_thread(fn, *args)
        except Exception:  # noqa: BLE001 —— 见文件头「坏了就安静退回老路」
            logger.info("流式卡片 %s 失败，这一轮回到普通回复", what, exc_info=True)
            self._broken = True
            return None

    # ---------- 懒开：第一段可见文字到了才建卡 ----------

    async def start(self) -> bool:
        """建卡片实体 + 发出去。成功返回 True。

        🔴 **懒开**：不在一轮开头就建卡 —— 那样「它没说话就先出现一张空卡」，
        而且多数轮次（纯工具调用、一句话回答）根本用不上流式。
        """
        if self._broken or self._card_id:
            return self.live
        card = {
            "schema": "2.0",
            "config": {"streaming_mode": True,
                       "summary": {"content": "正在回答…"}},
            "body": {"elements": [{"tag": "markdown", "element_id": ELEMENT_ID,
                                   "content": "…"}]},
        }
        created = await self._call("建卡", self._create_card, json.dumps(card, ensure_ascii=False))
        if not created:
            # 🔴 建不出来就**别再试**：最常见的原因是没权限（老用户都没有），
            # 每一轮重试一次等于每一轮白打一发请求、日志刷满。
            self._broken = True
            return False
        self._card_id = created
        sent = await self._call("发卡", self._send_card, created)
        if not sent:
            self._card_id = None
            self._broken = True
            return False
        self._message_id = sent
        return True

    # ---------- 推 ----------

    def should_push(self, full_text: str) -> bool:
        """这一帧要不要真发出去（节流判据，纯函数、好测）。"""
        if not self.live:
            return False
        grew = len(full_text) - self._last_len
        if grew <= 0:
            return False
        if self._now() - self._last_push < MIN_GAP:
            return False                     # 硬下限，段末捷径也不许穿过（见 MIN_GAP）
        if full_text.endswith("\n") and grew > 0:
            return True                      # 段末提前推一次，手感更像在写
        if grew < MIN_CHARS:
            return False
        return self._now() - self._last_push >= MIN_INTERVAL

    async def push(self, full_text: str) -> bool:
        """把「到目前为止的全部文字」推上去。被节流掉或失败 ⇒ False。

        🔴 推的是**全量**不是增量：飞书那个接口就是整段覆盖，而且多轮工具调用会有
        多个文字块，累加值才是用户该看到的东西。
        """
        if not self.should_push(full_text):
            return False
        body = _safe_body(full_text)
        if not body:
            return False
        if body == self._shown:
            # 新长出来的字全被扣尾扣住了 / 已经超长、卡上停在「放不下」那句 ⇒
            # 推上去也是同一段话，白占一次限频（实测一轮工具调用里出现过 1 次）。
            return False
        ok = await self._call("推文字", self._put_content, self._card_id, body, self._next_seq())
        if ok is None or ok == _SKIPPED:
            return False                     # 跳过的那帧不记账：下一帧带全量追上
        self._last_push = self._now()
        self._last_len = len(full_text)
        self._shown = body
        self.pushes += 1
        return True

    # ---------- 收尾 ----------

    async def finish(self, final_text: str, footer: str = "") -> bool:
        """关掉流式、把**全文**重写一遍。返回「正文已经交付」。

        🔴 用 `result.text` 全文重写，不信任流式累加值（delta 可能丢）。
        装不下 ⇒ 卡上留开头 + 「完整内容在下面」，返回 False，调用方照旧用
        `replier.send` 发全文（第 19 轮：原来什么都不写就关流，卡停在半截中间帧上）。

        `final_text` 为空（被停下 / 没跑完 / 等确认时 `result.text` 可能是空的）⇒
        保留卡上已经显示的那段，只补 `footer` 并关流 —— 不能让卡停在「正在回答」。
        这种情况返回值表示「卡收好了」。
        """
        if not self.live:
            return False
        delivered = True
        if final_text.strip():
            if _too_long(_redacted(final_text)):
                logger.info("流式卡装不下全文（%d 字），正文照旧用消息发", len(final_text))
                delivered = False
            body = _safe_final(final_text)
        else:
            body = self._shown
        if footer and delivered:
            body = (body + "\n\n" + footer) if body else footer
        # 卡上已经是这段 ⇒ 不再写一遍（省一次限频）
        wrote = await self._write_final(body) if body and body != self._shown else True
        await self._close(body or footer)
        return bool(wrote) and delivered

    async def abandon(self) -> None:
        """推到一半坏掉了（`broken`）的卡：尽力关一次流，别让它挂着「正在回答…」10 分钟。

        🔴 第 24 轮（R-stream 实测）：中间某帧超时 ⇒ `broken` ⇒ `live` 变 False ⇒ 所有收尾路径
        都跳过 `finish` ⇒ 卡停在半截、聊天列表一直「正在回答…」，正常那条路还会再发一条全文。
        只关流不写字（字已经写不进去了）。关不掉就算了。
        """
        if not self._card_id or self._closed:
            return
        self._closed = True
        try:
            await asyncio.to_thread(self._set_streaming, self._card_id, False, self._next_seq())
        except Exception:  # noqa: BLE001
            logger.info("坏掉的流式卡关流也失败了", exc_info=True)

    def shown_text(self) -> str:
        """卡上现在显示的那段（已脱敏、扣过尾）。"""
        return self._shown

    async def _write_final(self, body: str) -> bool:
        """终态那一帧。被限频 / 卡正在被点 ⇒ 等一下再试。

        🔴 第 19 轮：原来 `_put_content` 遇到限频返回 True（中间帧这么做是对的：下一帧会追上）⇒
        **终态**那一帧被限频时 `finish` 也报「交付了」⇒ app.py 不再发消息 ⇒ 用户看到的是
        扣过尾的中间帧，而且没有任何兜底。终态没有下一帧，跳过就等于丢了。
        """
        for delay in (0.0, *self._final_retry_delays):
            if delay:
                await asyncio.sleep(delay)
            got = await self._call("写终态", self._put_content, self._card_id, body,
                                   self._next_seq())
            if got is None:
                return False
            if got != _SKIPPED:
                self._shown = body
                return True
        logger.info("流式卡终态一直被限频，正文照旧用消息发")
        return False

    async def _close(self, text: str) -> None:
        """关流，顺手把聊天列表里那行预览改成正文开头。

        改预览被拒（这个字段放在 settings 里**没在真飞书上验过**，红线不许写请求）⇒
        退回只关流。关流本身必须成功：不关的话聊天列表一直挂着「正在回答…」、
        卡片不能转发，10 分钟后飞书才自动关。
        """
        self._closed = True
        ok = await self._call("关流", self._set_streaming, self._card_id, False,
                              self._next_seq(), _preview(text))
        if ok is False:
            await self._call("关流", self._set_streaming, self._card_id, False, self._next_seq())

    # ---------- 和 SDK 打交道的四个小函数（单独拆出来便于替换/测试）----------

    def _create_card(self, card_json: str) -> Optional[str]:
        from lark_oapi.api.cardkit.v1 import CreateCardRequest, CreateCardRequestBody

        request = (CreateCardRequest.builder()
                   .request_body(CreateCardRequestBody.builder()
                                 .type("card_json").data(card_json).build())
                   .build())
        response = self._client.cardkit.v1.card.create(request)
        if not response.success():
            logger.info("建流式卡被拒：code=%s（多半是权限还没批下来）", response.code)
            return None
        return getattr(response.data, "card_id", None)

    def _send_card(self, card_id: str) -> Optional[str]:
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        from .reply import _id_type

        # 🔴 第 20 轮（S2 Top 2 读码发现）：原来写死 chat_id ⇒ 菜单点击在重启后拿 open_id
        # 当回复目标（日志里 83 次有 25 次）⇒ 建卡必 230001 ⇒ 那一轮静默没有流式。
        request = (CreateMessageRequest.builder().receive_id_type(_id_type(self._chat_id))
                   .request_body(CreateMessageRequestBody.builder()
                                 .receive_id(self._chat_id).msg_type("interactive")
                                 .content(content).build())
                   .build())
        response = self._client.im.v1.message.create(request)
        if not response.success():
            logger.info("流式卡发不出去：code=%s", response.code)
            return None
        return getattr(response.data, "message_id", None)

    def _put_content(self, card_id: str, content: str, sequence: int) -> bool:
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest,
            ContentCardElementRequestBody,
        )

        request = (ContentCardElementRequest.builder()
                   .card_id(card_id).element_id(ELEMENT_ID)
                   .request_body(ContentCardElementRequestBody.builder()
                                 .content(content).sequence(sequence).build())
                   .build())
        response = self._client.cardkit.v1.card_element.content(request)
        if not response.success():
            # 限频 / 用户正在点卡片 ⇒ 跳这一帧，不降级（中间帧：下一帧会带着全量追上；
            # 终态：`_write_final` 看到 _SKIPPED 会等一下再试）
            if int(getattr(response, "code", 0) or 0) in (230020, 200810):
                logger.debug("流式这一帧跳过：code=%s", response.code)
                return _SKIPPED
            logger.info("流式推送被拒：code=%s", response.code)
            raise RuntimeError(f"push rejected: {response.code}")
        return True

    def _set_streaming(self, card_id: str, on: bool, sequence: int,
                       summary: str = "") -> bool:
        from lark_oapi.api.cardkit.v1 import SettingsCardRequest, SettingsCardRequestBody

        config: dict = {"streaming_mode": on}
        if summary:
            config["summary"] = {"content": summary}
        settings = json.dumps({"config": config}, ensure_ascii=False)
        request = (SettingsCardRequest.builder().card_id(card_id)
                   .request_body(SettingsCardRequestBody.builder()
                                 .settings(settings).sequence(sequence).build())
                   .build())
        response = self._client.cardkit.v1.card.settings(request)
        return bool(response.success())
