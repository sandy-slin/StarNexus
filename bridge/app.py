"""主程序：飞书长连接 → 本机 claude → 回飞书。

长连接模式（官方推荐、且仅自建应用可用）意味着**不需要公网 IP、不需要内网穿透**：
进程主动连出去，事件顺着这条连接进来。

四条硬纪律，每条都对应一个已证实的故障：

1. **handler 里 3 秒内必须返回。** 飞书规定收到事件 3 秒内处理完，否则触发重推。
   所以 handler 只做鉴权 + 入队，真活儿丢给后台 task。
2. **只能跑一个进程。** 飞书长连接是集群模式、不广播 —— 同一应用多个客户端在线时
   「只有其中随机一个会收到消息」。调试进程和正式进程同时开着，消息会随机漂走。
   这里用文件锁硬挡。
   🔴 2026-11-05 实测补上了这条的另一半：**一个应用最多 50 条长连接**
   （第 50 条起 `POST /callback/ws/endpoint` 回 `1000040350
   the number of connections exceeded the limit`，前 49 条全 OK）。
   这两条合起来决定了产品形态：**不能"IT 建一个应用、全公司共用"** ——
   不是因为连接数不够（50 台够了），而是因为集群模式**随机投递**，
   张三的消息会随机跑到李四的电脑上执行。所以必须一人一个应用。
   文件锁挡的是同一台机器开两个进程，一人一个应用挡的是跨机器串台。
3. **幂等用 message_id，不用 event_id。** 官方接收消息事件文档明确这么说
   （平台是至少一次投递，成功也可能重复）。
4. **必须外挂看门狗。** SDK issue #169（至今 open）：重连路径里一个 `requests.post`
   没设超时，而它全程持锁 ⇒ 半开连接下永久阻塞，整个重连循环停摆，**进程还活着、
   不报错、日志直接断掉**。报告者实测静默失联 3 天 13 小时。`auto_reconnect` 救不了
   这种情况，只能从外面看「多久没收到任何东西」然后重启自己。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import threading
import time
import tomllib
from dataclasses import dataclass

import lark_oapi as lark

from .approval import ApprovalGate, describe_action, install_hook
from .pairing import PairingGate
from .policy import (
    MODE_LABELS,
    MODE_RANK,
    MODEL_CHOICES,
    MODEL_LABELS,
    Policy,
    PolicyError,
    clamp_mode,
    effective_ceiling,
    modes_up_to,
)
from .prefs import PrefsStore
from .reply import Replier, build_settled_card
from .review import ApprovalStore, ReviewContext, Reviewer, Verdict
from .runner import ClaudeRunner
from .status import StatusCard, _fmt_elapsed
from . import status as status_mod
from . import workspace as workspace_mod
from .workspace import WorkspaceStore
from .sessions import SessionStore
from .fleet.actions import do_button as _do_button, fleet_button as _fleet_button

logger = logging.getLogger("feishu-bridge")

# 状态卡刷新间隔。飞书对同一用户发消息 5 QPS，单卡片实体操作上限 10 次/秒 ——
# 15 秒一次远在限额内，同时足够让用户感觉"它在动"。
_STATUS_REFRESH_SEC = 15
ACK_DELAY_SEC = 1.0   # 晚这么久再贴「在办」：秒回的命令不闪（第 19 轮，见 docs/iteration/r19-ack.md）
MENU_DEBOUNCE_SEC = 3.0   # 同一个常驻菜单键多久内的重复只算一次（第 22 轮）
LATE_NOTICE_SEC = 180   # 消息晚到超过这么久（秒）就在回复前说一句（第 22 轮，Mac 睡眠）

# 按钮点不动时给用户的话。每条都要说清**为什么**和**下一步做什么** ——
# 只说"失败了"会让人反复点同一个按钮。
_CARD_TOASTS = {
    ApprovalStore.USED: "这一步已经处理过了，不用再点",
    ApprovalStore.EXPIRED: "这张卡片过期了，重新发一次你的要求就行",
    ApprovalStore.STALE: "这一轮已经结束了，重新发一次你的要求就行",
    ApprovalStore.FORBIDDEN: "这台电脑只听它主人的，你的点击没有生效",
    ApprovalStore.MALFORMED: "这张卡片的数据不对劲，重新发一次你的要求",
    "": "这个按钮用不了了，重新发一次你的要求就行",
}

# 看门狗：多久没收到任何飞书事件就认为连接死了并自杀等人拉起。
# lark-oapi issue #169（未修）：重连路径里 `requests.post` 没设超时且全程持锁 ⇒
# 半开连接下永久阻塞，**进程还活着、不报错、日志直接断掉**（有人实测失联 3 天 13 小时）。
# `auto_reconnect` 覆盖不了这种，只能从外面看。
#
# 🔴 第 34 轮：15 分钟只剩**最后一道兜底**（新判据拿不到证据时才靠它）。
# 前面加了两条快判据（`stuck_reason()`，原理见 `bridge/wsfast.py` 第 34 轮那段）：
#   ① SDK 的事件循环卡住超过 `WATCHDOG_LOOP_STALL_SEC` —— #169 那个 `requests.post` 是同步的，
#      卡住它 = 整个循环停摆（ping 也发不出去）。这条不依赖飞书服务端。
#   ② 当前连接 `wsfast.pong_limit()` 秒（服务端 ping 间隔 90 ⇒ 2×90+30=210 秒）没收到飞书 pong。
# 两条都只用 monotonic（睡眠不计时），而且检测到睡醒（墙钟比进程时钟多走一截）就重新计时。
# 有任务在跑时快判据**不动手**，等那一轮结束再判（回复走 HTTP 接口，不靠长连接，能送达）。
WATCHDOG_SILENT_SEC = 15 * 60
WATCHDOG_TICK_SEC = 15
WATCHDOG_LOOP_STALL_SEC = 120
# 两拍之间墙钟比 monotonic 多走超过这么多秒 ⇒ 判成「刚睡醒」（合盖、DarkWake 维护唤醒）
WATCHDOG_SLEEP_JUMP_SEC = 30
# 「等你」推送多久扫一次（复用总控扫描，三家 0.2 秒扫完）
_NOTIFY_TICK_SEC = 10
# 建共用扫描（`Bridge._fleet_scanner`）那一下的锁：后台线程和事件循环线程可能同时第一次用到它
_SCANNER_LOCK = threading.Lock()
# 「重启前在对话模式」标记（`talk_mode.json`）的读写锁：卡片回调线程和事件循环线程都会写它
_TALK_MARK_LOCK = threading.Lock()
# 重启后「这句要发给它吗」那两个按钮，隔多久还算数（同确认前记着的话 `HELD_TTL_SEC`）
RESTART_OFFER_TTL_SEC = 10 * 60
# 有任务在跑时，静默阈值放宽到几倍 —— 宁可多等，不要腰斩结果。
_INFLIGHT_GRACE = 4

# 长连接握手被**明确拒绝**时的错误码。SDK 把这类错当 `ClientException` 直接抛出、
# 不重连（`lark_oapi/ws/client.py:187-189`），所以它们都是"重启也治不好"的死法。
#
# 🔴 1000040350 是 2026-11-05 实打出来的，不是抄文档：拿现有应用连到第 50 条时
#    `POST /callback/ws/endpoint` 回
#    `code=1000040350 msg=the number of connections exceeded the limit`
#    （前 49 条全 OK，加上当时在线的那一条正好 50）。官方文档写的也是
#    「每个应用最多建立 50 个连接」—— 这条数字决定了"一个应用能带多少台电脑"。
WS_CONN_LIMIT_CODE = 1000040350
WS_NO_CREDENTIAL_CODE = 1000040344


def explain_ws_failure(code: int | None) -> str:
    """把长连接握手的拒绝码翻成"下一步该干什么"。

    只认实测确认过的码；认不出就如实说认不出，不要编一个原因 ——
    猜错方向比不猜更贵（看门狗会每 15 分钟重启一次，掩盖真正的原因）。
    """
    if code == WS_CONN_LIMIT_CODE:
        return (
            "这个飞书应用的长连接数到顶了（每个应用最多 50 条，实测）。"
            "常见原因：同一个应用被多台电脑共用。"
            "注意飞书长连接是集群模式、不广播 —— 多台电脑共用一个应用时，"
            "消息只会随机进其中一台，所以**每台电脑要用自己的应用**。"
        )
    if code == WS_NO_CREDENTIAL_CODE:
        return "配置里的 app_id / app_secret 是空的，重跑一次安装就会补上。"
    if code == 403:
        return (
            "飞书拒绝了这条连接（403）。先确认应用的事件订阅方式选的是"
            "「使用长连接接收事件」，而且版本已经发布、管理员已经审批。"
        )
    return f"认不出这个拒绝原因（code={code}），把这一行连同上一行日志一起发给 IT。"


# 🔴 例句里**不许写死任何仓库名或文件名**。原来是
# 「carher-admin 有什么没提交的改动？」「`feishu_bridge.py` 是干什么的」
# 「跑一下 backend 的测试」—— 三条全是我们自己仓库的东西，同事机器上都不存在。
# 同 `pairing.BOUND_MESSAGE` 的红字；`tests/test_copy_quality.py` 两边一起盯。
# 帮助全文**只有一份**，在 menu.HELP（2026-11-06 第 11 轮 C：原来这里一份、menu 一份，
# 这份逐条教人打 /cd /mode /stop …，用户原话「不要让用户输入 /cancel 等类似的」）。
from .menu import HELP  # noqa: E402



# 会话总控（bridge/fleet）。不带斜杠的说法只认**整条消息就是这个词**，
# 免得「会话为什么断了」这种正常问题被当成命令吃掉。
FLEET_COMMANDS = frozenset({"/sessions", "/session", "/会话", "/总控"})
FLEET_WORDS = frozenset({"会话", "总控"})
CANCEL_COMMANDS = frozenset({"/cancel"})
# 🔴 2026-11-06 第 9 轮复审 #3：原来只认精确的「取消」两个字 ⇒ 手机输入法自动补的「取消。」、
# 按钮上的字「退出对话」「算了」被当成要发给会话的话转了进去（确认前那句还会被记着、确认后发出去）。
# 只认**整句**（去掉首尾空白 / 标点 / 表情之后完全相等），不认包含：「取消订单那个 bug」是正常的一句话。
CANCEL_WORDS = frozenset({"取消", "退出", "退出对话", "算了", "停止转发"})
# 原来那一个。别的几个只在「真有东西可取消」时才吃掉这句话（见 `_handle_command`），
# 不然单聊里跟桥自己的 claude 说一句「算了」会被回成「没有要取消的」。
_CANCEL_WORD_ALWAYS = "取消"
# 诊断卡：同事"坏了"的时候，能转给你和 IT 的那张脱敏卡（bridge/status.py 文件头）。
# 不带斜杠的「诊断」也要认 —— 同事在手机上打的就是这两个字。
DIAG_COMMANDS = frozenset({"/诊断", "/diag", "/diagnose"})
DIAG_WORDS = frozenset({"诊断"})
# 「更新」=「现在就去看有没有新版，有就装」（第 14 轮）。他不用等那 10 分钟一次的检查。
UPDATE_WORDS = frozenset({"更新", "升级", "更新一下", "检查更新"})
# 诊断只读日志尾部这么多字节。日志能长到几十 MB，而卡片回调要在 3 秒内返回。
_DIAG_LOG_TAIL_BYTES = 256 * 1024
# 我们自己那套时间戳（`2026-11-05 14:55:01,118`）。SDK 的 `[Lark] [...]` 里也是同一形状。
_DIAG_TS_RE = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _cancel_core(text: str) -> str:
    """去掉首尾空白、标点、表情（全角半角都算）之后剩下的那一段。`/` 留着（`/cancel` 要认得出）。"""
    import unicodedata

    def edge(ch: str) -> bool:
        if ch == "/":
            return False
        return ch.isspace() or unicodedata.category(ch)[0] in "PSZ"

    core = str(text or "")
    start, end = 0, len(core)
    while start < end and edge(core[start]):
        start += 1
    while end > start and edge(core[end - 1]):
        end -= 1
    return core[start:end]


def _is_cancel_word(text: str) -> bool:
    """这句话**整句**就是取消吗（「取消。」「🚪 退出对话」「/cancel」是；「取消订单那个 bug」不是）。"""
    core = _cancel_core(text)
    return core in CANCEL_WORDS or core.lower() in CANCEL_COMMANDS


def _is_fleet_word(text: str) -> bool:
    from .fleet.newsession import WORDS as _NEW_WORDS

    stripped = (text or "").strip()
    if stripped.startswith(("找会话 ", "搜会话 ")):
        return True
    return stripped in FLEET_WORDS | DIAG_WORDS | UPDATE_WORDS | _NEW_WORDS or _is_cancel_word(text)


def _redact_text(text: str) -> str:
    """卡片不走 replier.send，脱敏要自己过一遍（同一份 redact.py）。"""
    from .redact import redact

    return redact(str(text or ""))[0]


def _pending_fallback(sess) -> str:
    """详情卡发不出去时，文字版里那段「它在等什么」。没有就空串；出错也空串（不能让兜底文字发不出去）。"""
    try:
        pending = getattr(sess, "pending", None)
        if getattr(sess, "status", "") != "waiting" or pending is None:
            return ""
        if pending.questions:
            q = pending.questions[0]
            out = "\n❓ 它在问你：" + _redact_text(q.question)[:120]
            out += "".join(f"\n{i + 1}. {_redact_text(label)[:30]}"
                           for i, (label, _desc) in enumerate(q.options[:6]))
        else:
            out = f"\n⏸ 它在等你确认：{pending.tool} {_redact_text(pending.summary)[:160]}"
        return out + "\n要在电脑上点。"
    except Exception:  # noqa: BLE001
        return ""


# 「不 @ 也收得到群消息」这件事**被现场证明过**的落盘标记。
#
# 🔴 2026-11-06 用户截图：卡上写「要先 @ 一下机器人再说话（不 @ 它收不到）」，而他没 @、照样发进去了、也回了。
# 原因：`im:message.group_msg` 是推送类权限，体检**没法主动试**（`groupsetup.probe` 里它恒为 unknown）⇒
# `receive_granted` 恒为 False ⇒ 文案永远走「要先 @」那一支 —— 权限明明开着，每个群都在教人多 @ 一次。
# 能证明它的只有一件事：**真的收到过一条没 @ 机器人的群消息**。收到一次就落盘记下，之后都按「直接说」说。
GROUP_MSG_PROOF_FILE = "group_msg_proven.json"
KNOCKS_FILE = "stranger_knocks.json"   # 第 23 轮：敲门计数落盘（人只存哈希）
KNOCKS_RESET_SEC = 24 * 3600           # 24 小时没新敲门 ⇒ 清零


def _group_how_to_talk(report=None, here: bool = False, proven: bool = False) -> str:
    """「怎么在群里说话」那半句。

    🔴 按**实际有没有收群消息那项权限**分叉（`groupsetup.RECEIVE_SURE` 的红字）：
    没开的话飞书只推 @ 了机器人的消息，照说「每一句都直达」会让主人在群里说一句
    没人理，以为建群这功能坏了。查不出来也走保守那一支（多 @ 一次总比失灵好）。

    `here=False`：单聊里说「去那个群里……」；`here=True`：在群里说「在这个群里……」。
    🔴 群里点确认之后那句、24 小时内再点进来那句都要走这里（`here=True`），
    和确认卡**同一个判断** —— 2026-11-06 体验侦察实测：确认卡叫他 @，紧接着那句却写死
    「直接打字发过来就行」，照后一句做机器人一声不吭。
    """
    try:
        from .fleet.groupsetup import receive_granted

        sure = receive_granted(report)
    except Exception:  # noqa: BLE001 —— 漏装 groupsetup 时按保守那句说
        sure = False
    # 现场证明过（真收到过不 @ 的群消息）⇒ 不管体检怎么说，都按「直接说」（见 GROUP_MSG_PROOF_FILE 的红字）
    sure = sure or bool(proven)
    if here:
        if sure:
            return "你在这个群里说的每一句都直达它。"
        return "在这个群里要先 @ 一下机器人再说话，那句才会直达它（不 @ 它收不到）。"
    if sure:
        return "去那个群里说话，每一句都直达它；"
    return "去那个群里 @ 一下机器人再说话，那句就直达它；"


# 群里确认前说的话先记着，点确认后发过去（见 `Bridge._hold_for_consent`）。
# 隔太久就不自动发了：那句话是他十几分钟前的想法，现在未必还要（照实说没发）。
HELD_TTL_SEC = 10 * 60
# 确认前最多先记这么多句。再多说明他在群里聊别的，不是在等这张卡。
HELD_MAX = 5
# 确认卡上每句最多原样显示这么多字。再长就截，并明说后面还有多少字、也会一起发（卡片 30KB 上限）。
HELD_SHOW_MAX = 300


def _held_line(texts) -> str:
    """记着的那几句 → 卡上 / 消息里引用的说明（每一句都列出来）。没有 ⇒ 空串。原话先脱敏。"""
    texts = [str(t) for t in (texts or ()) if str(t or "").strip()]
    if not texts:
        return ""
    # 🔴 2026-11-06 第 9 轮复审 #2：底线是「主人点确认时屏幕上看到的，就是会发出去的全部」。
    # 原来只引用第一句、还截到 30 个字 ⇒ 卡上写一句，点确认发出去两句（或一句的后半截他没看到）。
    # 现在每一句都**原样**列出来（先脱敏）；单句超长才截，并且明说后面还有多少字、也会一起发。
    def show(text: str) -> str:
        clean = _redact_text(text).strip()
        if len(clean) <= HELD_SHOW_MAX:
            return f"「{clean}」"
        return f"「{clean[:HELD_SHOW_MAX]}…」（后面还有 {len(clean) - HELD_SHOW_MAX} 个字没显示，也会一起发）"

    if len(texts) == 1:
        return f"点确认之后，会把你刚才那句发过去，不用再打一遍：\n{show(texts[0])}"
    lines = "\n".join(f"{i}. {show(t)}" for i, t in enumerate(texts, 1))
    return f"点确认之后，会把你刚才说的这 {len(texts)} 句一起发过去，不用再打一遍：\n{lines}"


class _FleetWaitCard:
    """话发进电脑上的会话之后、等它回的那段时间：一张**原地刷新**的等待卡。

    🔴 2026-11-06 体验侦察 Top4：原来只有一句「📨 发给…了，等它回…」，之后最长 15 分钟
    （`turn_timeout_sec`）什么都没有 —— 手机锁屏再拿起来，分不清它在干活还是卡死了。
    桥自己那一轮有 `StatusCard`，这条路没有。这里照它的规矩：
      - 几秒就回的不发（第一张等 `progress_first_notice_sec`）
      - **只发一张**，之后 `patch` 同一张（不刷屏），没变化不发
      - 回来了收尾成真实结果，去掉「停下」
      - 发 / 改失败一次就不再试；任何异常都不影响那条消息本身
    「停下」只在**能停**的会话上放（终端会话点了必然停不了，Top5 同一个形状）。
    飞书接口是同步的 ⇒ 丢线程里调，别卡住事件循环（等的同时还要收他补的话）。
    """

    def __init__(self, replier, chat_id: str, label: str, *, can_stop: bool,
                 first_delay: float, escalate_sec: float = 0, limit_sec: float = 0,
                 progress=None) -> None:
        self._replier = replier
        # FC-14：返回进展几行（用了哪些工具、改了哪些文件、正在做什么）的函数；没有 ⇒ 只写等了多久
        self._progress = progress
        self._chat_id = chat_id
        self._label = label
        self._can_stop = bool(can_stop)
        self._first_delay = max(float(first_delay or 0), 0.0)
        self._escalate_sec = max(float(escalate_sec or 0), 0.0)
        self._limit_sec = max(float(limit_sec or 0), 0.0)  # 最多等多久（到点就不再等、也不补推）
        self._message_id = None
        self._last_text = ""
        self._broken = getattr(replier, "send_status_card", None) is None
        self._started = time.monotonic()

    def _render(self, elapsed: int, extra: list = ()) -> str:
        text = f"⏳ 已经发给{self._label} {_fmt_elapsed(elapsed)}，还在等它回"
        if extra:
            text += "\n" + "\n".join(extra)
        if self._escalate_sec and elapsed >= self._escalate_sec:
            text += "\n等得比平时久。可以先去忙，它回了会贴在这里"
            # 照实说上限：过了这个点就不再等，之后它回了也不会推过来
            text += f"（最多再等{_fmt_wait(self._limit_sec - elapsed)}）。" if self._limit_sec > elapsed else "。"
        return text

    async def _show(self, text: str, with_stop: bool) -> None:
        if self._broken or text == self._last_text:
            return
        self._last_text = text
        try:
            if self._message_id is None:
                self._message_id = await asyncio.to_thread(
                    self._replier.send_status_card, self._chat_id, text, with_stop=with_stop)
                ok = bool(self._message_id)
            else:
                ok = bool(await asyncio.to_thread(
                    self._replier.patch_status_card, self._message_id, text, with_stop=with_stop))
        except Exception:  # noqa: BLE001 —— 等待卡是锦上添花，坏了不该拖累那条消息
            logger.debug("等待卡发送/更新失败", exc_info=True)
            ok = False
        if not ok:
            self._broken = True

    async def run(self, stop: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=self._first_delay)
            return  # 第一张之前就回了：一张都不发
        except asyncio.TimeoutError:
            pass
        while not stop.is_set():
            extra: list = []
            if self._progress is not None:
                try:
                    extra = list(await asyncio.to_thread(self._progress) or [])
                except Exception:  # noqa: BLE001 —— 进展读坏了只少几行
                    logger.debug("读这一轮进展失败", exc_info=True)
            await self._show(self._render(int(time.monotonic() - self._started), extra), self._can_stop)
            try:
                await asyncio.wait_for(stop.wait(), timeout=_STATUS_REFRESH_SEC)
            except asyncio.TimeoutError:
                continue

    async def finish(self, result) -> None:
        """收尾：按真实结果写，去掉「停下」。没发过卡就什么都不做。"""
        if self._message_id is None or self._broken:
            return
        took = _fmt_elapsed(int(time.monotonic() - self._started))
        if result is not None and getattr(result, "ok", False):
            head = "✅ 它回了"
        elif result is not None and getattr(result, "delivered", False):
            head = "⚠️ 没等到它正常回复，看下面那条"
        else:
            head = "❌ 没发进去，看下面那条"
        self._last_text = ""  # 收尾那次一定要改，哪怕文字碰巧一样
        await self._show(f"{head} · {took}", with_stop=False)


def _fmt_wait(seconds: float) -> str:
    """剩下多久 → 「约 8 分钟」「不到 1 分钟」。手机上看一眼就懂，不要秒数。"""
    minutes = int(seconds // 60)
    return f"约 {minutes} 分钟" if minutes >= 1 else "不到 1 分钟"


def _outcome_of(result) -> str:
    """这一轮的结果 → 状态卡收尾用哪句话。没拿到结果（异常）算失败。"""
    if result is None:
        return "failed"
    if getattr(result, "cancelled", False):
        return "stopped"
    if getattr(result, "deferred", False):
        return "waiting"
    if not getattr(result, "ok", False):
        return "failed"
    return "done"


def _toast_for(text: str) -> str:
    """点了按钮后的提示。**不许露内部命令**（原来会显示「收到，/mode! trusted」）。"""
    parts = text.split(maxsplit=1)
    cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")
    if cmd in ("/mode", "/mode!") and arg:
        return f"收到，切到「{MODE_LABELS.get(arg, arg)}」"
    if cmd == "/model" and arg:
        return f"收到，换成「{MODEL_LABELS.get(arg, arg)}」"
    named = {
        "/mode": "看看权限", "/model": "看看模型", "/dirs": "看看有哪些项目",
        "/status": "看看现在状态", "/stop": "正在停", "/new": "开个新话题",
        "/menu": "打开菜单", "/help": "打开帮助", "/history": "调出最近问过的话", "/sessions": "看看电脑上的会话",
        "/诊断": "看看运行状态", "/newsession": "选一下用哪个",
    }
    if cmd in named:
        return f"收到，{named[cmd]}"
    return f"收到，「{text[:20]}」"


def _truthy(value) -> bool:
    """config.toml 里的开关：`false` / `"false"` / `"no"` / `0` 都算关，其余算开。"""
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "no", "off", "0")
    return bool(value)


@dataclass
class Settings:
    app_id: str
    app_secret: str
    owner_open_ids: frozenset[str]
    policy: Policy
    max_reply_chars: int
    session_idle_reset_min: int
    progress_first_notice_sec: int
    progress_escalate_sec: int
    progress_max_notices: int
    config_path: str = ""
    # 用户自己加的禁止项（正则）。只能更严，挡不住的东西它也放不开 ——
    # 抄 dots 的「Custom Rules 关不掉 auto-review」。
    custom_rules: tuple[str, ...] = ()
    # `[notify] waiting`：Claude 会话进入「等你」时往主人单聊推卡（bridge/fleet/notify.py）。默认开。
    notify_waiting: bool = True
    notify_group: bool = True

    @classmethod
    def load(cls, path: str) -> "Settings":
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)

        feishu = raw.get("feishu", {})
        agent = raw.get("agent", {})
        limits = raw.get("limits", {})

        # 环境变量优先：方便不把 secret 落盘。
        app_id = os.environ.get("FEISHU_BRIDGE_APP_ID") or feishu.get("app_id", "")
        app_secret = os.environ.get("FEISHU_BRIDGE_APP_SECRET") or feishu.get("app_secret", "")
        if not app_id or not app_secret:
            raise PolicyError("缺 app_id / app_secret（config.toml 或环境变量都没给）")

        workspaces = tuple(agent.get("workspaces") or [])
        # 🔴 2026-11-05：本机上限不只看 config.toml —— IT 放在
        # /Library/Application Support/CarHer/policy.toml 的那份能把它**收紧**
        # （只能更严，见 policy.py 末节「IT 统一管控」）。在这之前 doctor 向用户和
        # IT 两边都打印「由 IT 固定 mode=…」，而这里只打开了 config.toml 一个文件
        # ⇒ 用户把 mode 改成 trusted 就全程无审批，两边却都收到了「已经锁住」的信号。
        # 🔴 第 14 轮（同事截图：权限卡上只有「只读」一个按钮）：原来 `mode` 一个值**同时**当
        # 「起步档」和「本机上限」⇒ 安装器写 `mode = "readonly"` = 把每个同事锁死在只读，手机上切不上去。
        # 现在拆开：`max_mode` = 上限（安装器写 trusted），`mode` = 起步档（安装器写 readonly）。
        # 没有 `max_mode` 的老配置照旧按 `mode` 当上限（行为不变）。IT 策略文件照样只能往紧里收。
        start = agent.get("mode", "readonly")
        ceiling = effective_ceiling(agent.get("max_mode") or start)
        policy = Policy(
            # mode 不超过上限：config 写 trusted 而 IT 锁 readonly 时这一轮就从
            # readonly 开始（照原样传 config 的值会被 __post_init__ 当越顶抛错）。
            mode=clamp_mode(start, ceiling) if start in MODE_RANK and ceiling in MODE_RANK else ceiling,
            workspaces=workspaces,
            default_workspace=agent.get("default_workspace") or (workspaces[0] if workspaces else ""),
            model=agent.get("model", "claude-sonnet-5-5"),
            turn_timeout_sec=int(agent.get("turn_timeout_sec", 900)),
            ceiling_mode=ceiling,
        )

        return cls(
            app_id=app_id,
            app_secret=app_secret,
            owner_open_ids=frozenset(feishu.get("owner_open_ids") or []),
            policy=policy,
            max_reply_chars=int(limits.get("max_reply_chars", 20000)),
            session_idle_reset_min=int(agent.get("session_idle_reset_min", 30)),
            progress_first_notice_sec=int(limits.get("progress_first_notice_sec", 90)),
            progress_escalate_sec=int(limits.get("progress_escalate_sec", 420)),
            progress_max_notices=int(limits.get("progress_max_notices", 2)),
            config_path=path,
            custom_rules=tuple(raw.get("review", {}).get("deny_patterns") or []),
            notify_waiting=_truthy((raw.get("notify") or {}).get("waiting", True)),
            notify_group=_truthy((raw.get("notify") or {}).get("group", True)),
        )


def _late_minutes(create_time, now: float | None = None) -> int:
    """飞书消息的 create_time（毫秒字符串）比现在晚了几分钟；没超过阈值 / 读不出 ⇒ 0。

    读不出（缺字段、坏值、未来时间）一律 0：这只是一句提示，不能让一条消息因为它出错。
    """
    try:
        sent_at = int(str(create_time)) / 1000.0
    except (TypeError, ValueError):
        return 0
    lag = (time.time() if now is None else now) - sent_at
    if lag < LATE_NOTICE_SEC or lag > 7 * 24 * 3600:
        return 0
    return int(round(lag / 60))


STOP_REMIND_SEC = 600


class Bridge:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = (
            lark.Client.builder()
            .app_id(settings.app_id)
            .app_secret(settings.app_secret)
            .build()
        )
        self.replier = Replier(self.client, settings.max_reply_chars)
        self.runner = ClaudeRunner(settings.policy)
        self.sessions = SessionStore(
            settings.policy.default_workspace, settings.session_idle_reset_min
        )
        # 独立审查层。和工具白名单是两层：白名单管"工具能不能用"，
        # 这层看整句话、并且能在反复试探后掐停会话。
        # 还没主人时，只认装机时打印的一次性配对码（见 pairing.py）。
        self.pairing = PairingGate.load(settings.config_path)
        self.reviewer = Reviewer(custom_rules=settings.custom_rules)
        # scope -> 暂停原因。**落盘**：看门狗在长连接静默时会自杀重启
        # （lark-oapi 的重连会卡死），内存状态活不过一次重启 ⇒ 掐停等于没掐。
        self._paused: dict[str, str] = {}
        self._paused_path = ""

        # 审批：钩子落盘 + 令牌签名。
        home = os.path.dirname(settings.config_path) or os.path.expanduser(
            "~/.carher-feishu-agent"
        )
        self.gate = ApprovalGate(os.path.join(home, "approvals"))
        self._paused_path = os.path.join(home, "paused.json")
        self._load_paused()
        # 每个会话记住"在哪个目录"。落盘 —— 用户不该每次重启都回到默认目录。
        self.workspaces = WorkspaceStore(
            os.path.join(home, "workspaces.json"), settings.policy
        )
        # 聊天里改过的档位/模型。落盘，重启还在。
        # 🔴 config.toml 的 mode = 本机上限；聊天里能在上限以内切换，收紧点一次，放开要二次确认。
        # 范围由本机文件定 —— 聊天超不过它。
        self.prefs = PrefsStore(os.path.join(home, "prefs.json"), settings.policy)
        # 按工具的默认模型 / effort + 会话级覆盖（fleet/catalog.py，产品定义 §5.4）
        self.model_prefs = None
        try:
            from .fleet.catalog import Catalog, ModelPrefs

            self.model_prefs = ModelPrefs(os.path.join(home, "model_prefs.json"), Catalog())
        except Exception:  # noqa: BLE001
            logger.exception("模型设置没起来，会话照旧用工具自己的默认模型")
        # 🔴 令牌密钥**必须落盘**。
        # 原来用 os.urandom(32) 每次启动随机生成，注释里写"重启后旧卡片失效是对的，
        # 因为 claude 会话也续不上了"—— 这个推理是错的：会话 id 存在
        # 🔴 注意：sessions.py 里的会话 id **只在内存**，重启就没了（第 24 轮复审核实）。
        # 2026-11-03 实测后果：服务重启 14 次，用户点任何一张早先的卡片都得到
        # 「这个按钮已失效或你无权操作」，而他完全不知道为什么（卡片看起来是好的）。
        # 看门狗本来就会在长连接静默时自杀重启，所以这个窗口是**常态**不是例外。
        # 🔴 2026-11-06：用过的 nonce 落盘 ⇒ 「一张卡只能点一次」跨重启也成立
        # （原来只在内存 ⇒ 重启后同一张卡能再兑一次，`test_token_remains_single_use_across_restart`）。
        self.approvals = ApprovalStore(
            _load_or_create_secret(home), used_path=os.path.join(home, "approvals-used.txt")
        )
        # 待续跑的会话：点了同意之后要 resume 哪个
        self._awaiting: dict[str, dict] = {}
        # scope -> 取消事件。/stop 置位它，runner 杀掉子进程。
        # 必须能在**不拿会话锁**的情况下访问 —— 要打断的那一轮正持着锁。
        self._cancels: dict[str, asyncio.Event] = {}
        # scope -> 真实 chat_id。
        # 🔴 不能从 scope 反推：单聊 scope 是 `dm:<open_id>`，而发消息用的
        # `receive_id_type` 是 `chat_id` —— 两者不通用，飞书回
        # `code=230001 invalid receive_id`（2026-11-03 真实日志里出现过，
        # 表现是"活干完了但用户收不到"）。
        self._chat_ids: dict[str, str] = {}
        # 会话总控：看 / 接着聊 / 停 电脑上别的 Claude、Codex、Cursor 会话（bridge/fleet）。
        # 起不来不影响桥本身 —— 那时 /sessions 会如实说用不了。
        self.fleet = None
        self._fleet_inflight: dict[str, tuple] = {}  # scope -> (tool, id)：正在发的那一条
        # scope -> (tool, id)：弹了确认卡、还没点的那个。用来认 `/talk!` 这条文字确认通道
        # （卡片发不出去时的退路，同 `/mode!` 的道理）。
        self._pending_consent: dict[str, tuple] = {}
        # 进对话模式那一次确认，记 24 小时。落盘 —— 看门狗会重启，内存态活不过一次重启，
        # 用户每被重启一次就要重新点一次确认（而他不知道发生了什么）。
        self.relay_consent = None
        try:
            from .fleet.consent import RelayConsent

            self.relay_consent = RelayConsent(os.path.join(home, "relay_consent.json"))
        except Exception:  # noqa: BLE001 —— 存不下来不该让「接着聊」整个用不了
            logger.exception("对话模式的确认记录没起来，每次都会重新问一次")
        # 单聊「重启前在对话模式」的小标记（第 11 轮，复审第 5 条）。只记 scope → 会话 + 到期时间，
        # 不记说过的话。重启后第一句先问一声「这句要发给它吗」，**不凭它自动转发**（见 `_restart_talk_offer`）。
        self._talk_mark_path = os.path.join(home, "talk_mode.json")
        # 「一个群 = 一个会话」的长期绑定（bridge/fleet/chatbind.py）。
        # 🔴 **落盘**，而且和 5 分钟的待发目标是两件事：群里说的每一句都查这张表，
        # 不靠那个会过期的内存态（看门狗重启是常态）。起不来不影响别的 —— 那时群里的话
        # 按平常处理（走桥自己的 claude，它自己有只读闸门），不会乱发给某个会话。
        self.chat_binds = None
        try:
            from .fleet.chatbind import ChatBinds

            self.chat_binds = ChatBinds(os.path.join(home, "chat_binds.json"))
        except Exception:  # noqa: BLE001
            logger.exception("群绑定没起来，群里的消息按平常处理")
        # 建群那一下要调的飞书接口。**默认没有** —— 机器人现在还没有建群权限
        # （实测 `POST /open-apis/im/v1/chats` ⇒ 99991672），要用户在后台开 scope 再发版本。
        # 上线那条路把一个 `chatbind.GroupClient` 赋到这里就能用；为 None 时点会话还是老样子
        # （在当前聊天里进对话模式），不会给用户画一个点了必失败的按钮。
        # 建群 client。接上它，点会话才会真的建群（2026-11-05 体验审查：原来写死 None，
        # 权限体检说「全齐了，点会话就能建群」，实际点了什么都不会发生 —— 死代码）。
        # 建群要 `im:chat:create` 等权限，没开时 GroupSetup 自己返回 NO_PERMISSION，
        # 调用方会发「先去开权限」那张卡，不会崩。构造失败就退回 None（老路照样能聊）。
        try:
            from .fleet.groupsetup import GroupSetup

            self.group_client = GroupSetup(settings.app_id, settings.app_secret)
        except Exception:  # noqa: BLE001
            logger.exception("建群 client 没起来，群功能先不可用")
            self.group_client = None
        # 会话群置顶状态卡（fleet/statuscard.py，产品定义 §5.1）。起不来只是少一张卡。
        self.status_cards = None
        try:
            from .fleet.statuscard import STATE_FILE, StatusCardKeeper, StatusCards

            pinner = getattr(self.group_client, "pin", None) if self.group_client is not None else None
            self.status_cards = StatusCardKeeper(
                StatusCards(os.path.join(home, STATE_FILE)),
                send=self.replier.send_card_id,
                patch=self.replier.patch_card,
                pin=pinner,
            )
        except Exception:  # noqa: BLE001
            logger.exception("会话群状态卡没起来，群里不会有置顶状态卡")
        try:
            from .fleet.control import build_default

            self.fleet = build_default(
                policy=lambda: self.policy,
                bridge_home=self.runner.config_dir,
                hook_settings=self._fleet_hook,
                hook_path=self._hook_path,
            )
        except Exception:  # noqa: BLE001
            logger.exception("会话总控没起来，/sessions 先用不了")
        # 会话进入「等你」时推卡（第 7 轮，bridge/fleet/notify.py）。判断全在 notify 里，
        # 这里只接线；起不来只是少了推送，桥照常。
        self.waiting_notifier = None
        try:
            from .fleet.notify import WaitingNotifier

            self.waiting_notifier = WaitingNotifier(enabled=settings.notify_waiting)
        except Exception:  # noqa: BLE001
            logger.exception("「等你」推送没起来，会话等你时不会主动提醒")
        # 会话绑定的群里推「要人介入的事 + 重要进展」（bridge/fleet/grouppush.py）。
        self.group_pusher = None
        try:
            from .fleet.grouppush import GroupPusher

            self.group_pusher = GroupPusher(enabled=settings.notify_group)
        except Exception:  # noqa: BLE001
            logger.exception("群进展推送没起来，绑定群里不会主动推进展")
        # CEO 编排（bridge/fleet/ceo.py）：独立功能，只用总控现成的开会话 / 发指令。起不来不影响别的。
        self.ceo = None
        try:
            self.ceo = self._build_ceo(home)
            if self.ceo is not None:
                stale = self.ceo.recover()
                if stale:
                    logger.info("CEO：%d 个任务被重启打断，等「/ceo 继续」", stale)
        except Exception:  # noqa: BLE001
            logger.exception("CEO 编排没起来，/ceo 先用不了")
        # 第 12 轮：常驻菜单自愈。装机那一刻缺权限写不成菜单的，桥起来后自己补；成功一次永不再写
        # （2026-11-06 同事装完没有悬浮菜单：菜单只在装机时写一次，之后没人再写）。
        self.menu_heal = None
        try:
            from . import menu_heal

            self.menu_heal = menu_heal.MenuHeal(
                settings.app_id,
                settings.app_secret,
                menu_heal.state_path_for(settings.config_path),
            )
        except Exception:  # noqa: BLE001
            logger.exception("常驻菜单自愈没起来，装机时没配上的菜单不会自动补")
        # 第 14 轮（用户原话：「我发布了新版本 它那边能立即更新」）：自动更新。
        # 同事遇到的毛病有一半在我这边已经修好，只是没到他机器上 —— 一天让同一个人重装 3 次。
        self.updater = None
        try:
            from . import selfupdate

            self.updater = selfupdate.SelfUpdater(
                os.path.dirname(os.path.abspath(__file__)),
                os.path.join(os.path.dirname(settings.config_path or ""), "update_state.json"),
                is_idle=lambda: not self._work_in_flight(),
            )
        except Exception:  # noqa: BLE001
            logger.exception("自动更新没起来，新版本要人工重装")
        self._ws_client = None  # run() 里赋值；看门狗靠它探活
        # 长连接线程**被明确拒绝**时的原话（`_ws_thread` 写）。空 = 还没被拒过。
        # 诊断要用它区分两种"连不上"：被拒（重启治不好）vs 正在重连（等一下就好）。
        self._ws_fatal = ""
        self._seen_messages: dict[str, float] = {}
        self._last_event_at = time.monotonic()
        self._last_event_wall = time.time()   # 见 `silent_for` 的红字（睡眠期间 monotonic 不走）
        self._reconnecting_since = 0.0
        self._reconnects = 0
        # 诊断卡要回答"这个进程活了多久"。和 `_last_event_at` 分开：后者是
        # 看门狗的时钟（会被每条消息刷新），而"已运行"要的是**进程起点**，
        # 它和"重启 N 次"一起才看得出崩溃循环（跑了 40 秒 + 重启 38 次）。
        self._started_at = time.monotonic()
        self._started_wall = time.time()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def policy(self) -> Policy:
        """当前生效的 Policy = config.toml 那份 + 聊天里改过的档位/模型。

        用 getattr 取 prefs：`settings.policy` 是**必须**能拿到的东西（所有闸门
        都从它来），而 prefs 只是覆盖层。测试里构造的 Bridge 可能没有 prefs，
        那时候回落到配置那份 —— 回落的方向是更严，不是更松。
        """
        prefs = getattr(self, "prefs", None)
        if prefs is None:
            return self.settings.policy
        return prefs.policy()

    # ---------- 看门狗支撑 ----------

    def touch(self) -> None:
        self._last_event_at = time.monotonic()
        # 🔴 第 15 轮：墙上时钟一起记。见 `silent_for` 的红字。
        self._last_event_wall = time.time()

    @property
    def silent_for(self) -> float:
        """多久没收到飞书的任何事件。**两个时钟取大的那个。**

        🔴 2026-11-06 现场（主人这台）：20:35 连接断、21:14 才连上，39 分钟收不到消息，
        看门狗（15 分钟阈值）一次都没动。pmset 显示那段时间 Mac 合着盖子睡着。
        `time.monotonic()` 在 macOS 上是 `mach_absolute_time()`，**睡眠期间不前进** ——
        睡 8 小时醒来它可能只涨了几秒，于是「断了很久」永远判不出来。
        （cc-connect 把同一个坑写进了注释：醒来后 Go 的 monotonic 仍显示原时长未流逝。）

        🔴 **不能只换成墙上时钟**：NTP 校时回拨、用户手动改系统时间会让差值变成负数
        或者一个巨大的值 ⇒ 要么永远不自杀，要么一醒来就自杀。
        所以：monotonic 管「进程真的过了多久」，wall 管「墙上过了多久」，
        取大的那个；wall 差值为负（时钟被回拨）直接丢掉。
        """
        mono = time.monotonic() - self._last_event_at
        wall = time.time() - getattr(self, "_last_event_wall", 0.0)
        if wall < 0 or not getattr(self, "_last_event_wall", 0.0):
            return mono
        return max(mono, wall)

    # ---------- 重连状态（来自 SDK 自己，不是反推的）----------

    def _on_ws_reconnecting(self) -> None:
        """SDK 开始重连时它自己调这个（`lark.ws.Client.on_reconnecting`，本机实测是实例属性）。

        有了它，「正在重连」就是现场事实；原来只能靠「多久没收到消息」反推，
        而那个在用户一天不说话时也会涨。
        """
        self._reconnecting_since = time.time()
        self._reconnects = getattr(self, "_reconnects", 0) + 1
        logger.info("长连接断了，正在重连（第 %d 次）", self._reconnects)

    def _recently_disconnected(self, window_sec: float = 600) -> bool:
        """最近有没有本机自己看到的「断过」证据：重连过、或者这个进程刚起来。

        给「晚到」提示用：只凭飞书时间戳和本机时钟的差，时钟一快就会每条都误报。
        用墙上时钟：睡眠期间 monotonic 不走（见 `silent_for` 的红字）。
        """
        now = time.time()
        last = max(getattr(self, "_last_reconnected_wall", 0.0),
                   getattr(self, "_reconnecting_since", 0.0),
                   getattr(self, "_started_wall", 0.0))
        return bool(last) and 0 <= now - last <= window_sec

    def _on_ws_reconnected(self) -> None:
        self._reconnecting_since = 0.0
        self._last_reconnected_wall = time.time()
        # 🔴 **不喂 `touch()`**：那个时钟只许被飞书推进来的事件喂
        # （`tests/test_interference.py::test_touch_is_only_fed_by_inbound_feishu_events`，
        # 它是 lark-oapi #169 静默卡死的唯一兜底）。连上了由 `connection_alive()` 负责判。
        logger.info("长连接已经重新连上")

    def reconnect_state(self) -> str:
        """正在重连 ⇒ 给人看的一句话；没在重连 ⇒ 空串。"""
        since = getattr(self, "_reconnecting_since", 0.0)
        if not since:
            return ""
        return f"正在重连（断了 {_fmt_elapsed(int(time.time() - since))}）"

    def reconnect_total(self) -> int:
        """这个进程起来之后断过几次。诊断要用：「低流量 0 故障」不等于没问题。"""
        return int(getattr(self, "_reconnects", 0))

    # ---------- 事件入口（必须 3 秒内返回）----------

    def on_message(self, data) -> None:
        self.touch()
        event = data.event
        sender = event.sender
        message = event.message

        # 鉴权。顺序有意：先判是不是人（别的机器人发言也会进来），再查白名单。
        if getattr(sender, "sender_type", None) != "user":
            return
        open_id = getattr(sender.sender_id, "open_id", "") or ""

        # 幂等要用的 message_id 得提前读 —— 配对也要靠它防重放。
        message_id = getattr(message, "message_id", "") or ""

        if not self.settings.owner_open_ids:
            # 还没有主人 ⇒ 只认装机时打印的那个一次性配对码。
            #
            # 🔴 换掉了原来的「第一条消息自动绑定」：那个窗口里**任何**能给这个机器人
            # 发消息的人都能成为主人（自建应用对全员可见时，同租户陌生人抢先发一条即可），
            # 而受害者之后只会觉得机器人坏了 —— 因为非主人走静默丢弃。
            result = self.pairing.consider(
                open_id, self._extract_text(message), message_id
            )
            if result.bound:
                # 落盘已在 consider 里完成；这里让内存同步生效，别等重启。
                self.settings.owner_open_ids = frozenset({result.owner_open_id})
                bound_chat = getattr(message, "chat_id", "") or ""
                self._say(bound_chat, result.reply)
                # 绑定完**直接给菜单** —— 用户原话「上来就给我按钮菜单，打字太不方便」。
                # 第一印象就是一排能点的按钮，而不是一段要读的说明。
                scope = SessionStore.scope_for(bound_chat, "p2p", result.owner_open_id)
                try:
                    self._send_menu(bound_chat, scope)
                except Exception:  # noqa: BLE001 - 菜单是锦上添花，绑定已经成功了
                    logger.exception("绑定后发菜单失败")
            else:
                # 🔴 对飞书静默，但**日志必须说清楚**。
                # 2026-11-03 用户踩过：安装器被重跑覆盖了配置 ⇒ 主人身份被擦掉 ⇒
                # 他发的每条消息都走到这里被丢弃，飞书里一点反应都没有，
                # 而日志里**一个字都没有** —— 他只能来问我，我是靠
                # 「授权人数=0」这行启动日志反推出来的。
                # 静默是对外的安全要求，不该连自己也查不出来。
                logger.warning(
                    "丢弃消息：还没有主人，只认配对码。"
                    "发送者=%s 本次结果=%s 已试错=%d 次。"
                    "重新生成配对码：python -m bridge.pairing reissue <config.toml>",
                    open_id,
                    getattr(result.outcome, "value", result.outcome),
                    result.attempts_used,
                )
            # 无论绑没绑，这条消息都**不当任务执行** —— 码只是个码。
            # 对飞书保持静默（回了等于确认机器人存在、背后有东西）。
            return

        if open_id not in self.settings.owner_open_ids:
            # 静默丢弃 —— 不回「你没权限」。回了就等于确认这个机器人存在、背后有东西。
            #
            # 🔴 第 15 轮：静默**照旧**（防探测），但要让主人看见有人在敲门。
            # 2026-11-06 现场：21:34/21:35 两条 `丢弃非授权用户消息`，发的人那边毫无反应，
            # 主人手上也没有任何线索 ⇒ 「两个人用了同一个机器人 / 他找错了机器人」这种事
            # 谁都查不出来，只能靠我去翻日志（当天我两次把猜测当结论讲给用户）。
            # 诊断卡上多一行「有人在找这台电脑 N 次」，一眼就能看出来。
            # 🔴 只认**私聊**：群里任何非主人的发言走的也是这条 return。
            # R2 实测群里 4 条同事发言 ⇒ count=4、people=3 —— 这一行要答的是
            # 「有人找错了机器人」，群噪音让它永远亮着，看了也不信。
            if (getattr(message, "chat_type", "") or "p2p") == "p2p":
                self._note_stranger_knock(open_id)
            logger.info("丢弃非授权用户消息 open_id=%s", open_id)
            return

        # 幂等：用 message_id（官方明说别用 event_id）。message_id 在上面已读。
        now = time.monotonic()
        self._seen_messages = {
            key: ts for key, ts in self._seen_messages.items() if now - ts < 3600
        }
        if message_id and message_id in self._seen_messages:
            return
        # 「在办」表情（第 18 轮，用户原话「比如 get 符号」）不在这里贴了 ——
        # 🔴 第 19 轮 S3：原来在这里贴，下面图片 / 表情包 / 读不出正文 / 循环没起来
        # 这几条直接 return 的路**永远不撤**。现在挪进 `_handle_then_unack`：
        # 只有真派发出去的才贴，同一个 finally 里撤。
        # 🔴 第 15 轮：**派发成功之后才记去重**（见本函数末尾）。
        # R2 实测：原来这里先记，然后才提文字 / 派发。中间任何一步出错、或者事件循环
        # 还没起来，这条就没处理成；而飞书会**重推同一个 message_id**（官方：3 秒内要
        # 处理完且不抛异常，否则触发重推）⇒ 被自己的去重吃掉 ⇒ 主人这句话**永久静默**。
        # 两种场景各实测一次：进后台 0、回用户 0。

        # 🔴 发图片 / 文件 / 语音原来一声不响（R2 实测 6 种全部静默）。
        # 只回主人（走到这里已经过了鉴权），表情不回（他只是在逗，回了反而吵）。
        mtype = str(getattr(message, "message_type", "") or "text")
        from .fleet.inbox import attachments_of

        attachments = attachments_of(mtype, getattr(message, "content", "") or "")
        if mtype not in ("text", "post", "sticker") and message_id and not attachments:
            self._seen_messages[message_id] = now
            chat_for_reply = getattr(message, "chat_id", "") or ""
            if chat_for_reply:
                self._say_later(chat_for_reply, "我现在只看得懂文字。把要问的打出来就行。")
            return
        if mtype == "sticker":
            if message_id:
                self._seen_messages[message_id] = now
            return

        text = "" if mtype not in ("text", "post") else self._extract_text(message)
        if not text and not attachments:
            if message_id:
                self._seen_messages[message_id] = now   # 真没正文 ⇒ 处理完了，重推不必再看
            if mtype == "post":
                # 第 25 轮：只有截图、没写字的富文本 ⇒ 和发图片一样说一句，不静默
                chat_for_reply = getattr(message, "chat_id", "") or ""
                if chat_for_reply:
                    self._say_later(chat_for_reply, "我现在只看得懂文字。把要问的打出来就行。")
            return

        chat_id = getattr(message, "chat_id", "") or ""
        chat_type = getattr(message, "chat_type", "") or "p2p"
        scope = SessionStore.scope_for(chat_id, chat_type, open_id)
        # 群里收到一条**没 @ 机器人**的话 ⇒ 「获取群组中所有消息」权限是真开着的（现场证明，见 GROUP_MSG_PROOF_FILE）
        if chat_type == "group" and not (getattr(message, "mentions", None) or []):
            self._mark_group_msg_proven()
        if chat_id:
            # 回复要用真实 chat_id（见 `_chat_for` 的红字）。
            # 用 getattr 建表：这段在消息入口上，缺属性不该让整条消息丢掉。
            if not hasattr(self, "_chat_ids"):
                self._chat_ids = {}
            self._chat_ids[scope] = chat_id

        # 丢后台，立刻返回 —— 3 秒纪律。
        if self._loop is None:
            # 🔴 不记去重 ⇒ 飞书重推同一条时还能救回来（R2 实测原来这里永久丢）
            logger.error("事件循环还没起来，这一条先不处理，等飞书重推")
            return
        late_min = _late_minutes(getattr(message, "create_time", None))
        late_note = ""
        if late_min:
            # 🔴 第 22 轮（S2 Top 3）：Mac 睡着 = 掉线 11~39 分钟，醒来飞书补推，他不知道为什么这么慢。
            logger.info("收到消息 迟到=%d 分钟", late_min)
            # 🔴 第 24 轮（R-entry 复现）：只看两边时间戳的差，本机时钟快 3 分钟以上时**每一条**都会被说成
            # 「晚到了、电脑睡着了」。要再有一个本机自己的证据：刚断线重连过、或者进程刚起来（睡醒被拉起）。
            if self._recently_disconnected():
                late_note = f"（这条晚到了约 {late_min} 分钟 —— 电脑多半刚才睡着了，现在开始处理）"
        # 提示和回复走同一个协程、先发提示（R-entry：原来两路并行，菜单卡可能比「现在开始处理」先到）
        if attachments:
            coro = self._attachments_then_handle(scope, chat_id, text, message_id, attachments, late_note)
        else:
            coro = self._handle_then_unack(scope, chat_id, text, message_id, late_note=late_note)
        asyncio.run_coroutine_threadsafe(coro, self._loop)
        if message_id:
            self._seen_messages[message_id] = now   # 真派发出去了才算「处理过」

    def _load_paused(self) -> None:
        """从磁盘恢复暂停状态。读不到就当没有，不让它阻塞启动。"""
        try:
            with open(self._paused_path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self._paused = {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            self._paused = {}

    def _save_paused(self) -> None:
        """写临时文件再原子改名：中途断电不会留下半截 JSON。"""
        import tempfile

        if not self._paused_path:
            return
        try:
            directory = os.path.dirname(self._paused_path)
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory, delete=False
            ) as tmp:
                json.dump(self._paused, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self._paused_path)
        except OSError:
            logger.exception("暂停状态落盘失败")

    @staticmethod
    def _extract_text(message) -> str:
        import json as _json

        try:
            content = _json.loads(getattr(message, "content", "") or "{}")
        except _json.JSONDecodeError:
            return ""
        text = (content.get("text") or "").strip()
        if not text:
            # 🔴 第 25 轮（P-newbie 复现）：富文本（截图 + 一句话、带格式的消息）是 `post`，文字在
            # `content[行][段].text` 里，原来只读顶层 `text` ⇒ 读出空串 ⇒ 不派发、一句话不回。
            # 飞书 post 的 content 有两种外形：直接 `{"title":…, "content":[[…]]}`，或按语言包一层 `{"zh_cn": {…}}`。
            bodies = [content] + [v for v in content.values() if isinstance(v, dict)]
            parts: list[str] = []
            for body in bodies:
                title = body.get("title")
                if isinstance(title, str) and title.strip():
                    parts.append(title.strip())
                for line in body.get("content") or []:
                    if not isinstance(line, list):
                        continue
                    seg = "".join(str(el.get("text") or "") for el in line
                                  if isinstance(el, dict) and el.get("tag") in ("text", "a", "md"))
                    if seg.strip():
                        parts.append(seg.strip())
                if parts:
                    break
            text = "\n".join(parts).strip()
        # 群里 @ 机器人会留下 @_user_1 这类占位，去掉。
        parts = [p for p in text.split() if not p.startswith("@_user_")]
        return " ".join(parts).strip()

    # ---------- 真正干活 ----------

    def _inbox_cwd(self, scope: str) -> str:
        """附件存到哪个项目：群绑定的会话 > 待发目标的会话 > 桥自己这个聊天的工作目录。"""
        control = getattr(self, "fleet", None)
        target = None
        binds = getattr(self, "chat_binds", None)
        if control is not None and str(scope).startswith("group:") and binds is not None:
            try:
                target = binds.active_of_scope(scope)
            except Exception:  # noqa: BLE001 —— 读不出绑定就按没绑处理
                target = None
        if target is None and control is not None:
            target, _expired = control.peek_pending_fresh(scope)
        if target is not None and control is not None:
            cwd = control._cwd_of(target.tool, target.id)
            if cwd:
                return cwd
        workspaces = getattr(self, "workspaces", None)
        if workspaces is not None:
            try:
                return workspaces.get(scope)
            except Exception:  # noqa: BLE001
                pass
        return self.settings.policy.default_workspace

    async def _attachments_then_handle(self, scope: str, chat_id: str, text: str, message_id: str,
                                       attachments: list, late_note: str = "") -> None:
        """FC-09：先把图片 / 文件下载进 `<项目>/.starnexus/inbox/`，再把「原话 + 路径」当成这条消息处理。"""
        from .fleet import inbox

        cwd = await asyncio.to_thread(self._inbox_cwd, scope)
        saved: list = []
        failed = 0
        for kind, key, name in attachments:
            try:
                data, served_name, mime = await asyncio.to_thread(inbox.download, self.client, message_id, key, kind)
                directory = await asyncio.to_thread(inbox.inbox_dir, cwd)
                fname = inbox.safe_name(name or served_name, key, kind, mime)
                saved.append(await asyncio.to_thread(inbox.save, directory, fname, data))
            except Exception as exc:  # noqa: BLE001 —— 一个附件失败不影响别的
                failed += 1
                logger.warning("附件 下载失败 kind=%s 原因=%s", kind, _redact_text(str(exc)))
        logger.info("附件 收到=%d 存好=%d 失败=%d 目录=%s", len(attachments), len(saved), failed, cwd)
        if not saved:
            self._say(chat_id, "附件没能存到电脑上（下载失败），这条没发出去。稍后再发一次，或者把内容打成文字。")
            return
        if failed:
            self._say(chat_id, f"有 {failed} 个附件没下载下来，先把存好的 {len(saved)} 个发过去。")
        await self._handle_then_unack(scope, chat_id, inbox.compose(text, saved), message_id, late_note=late_note)

    async def _handle_then_unack(self, scope: str, chat_id: str, text: str,
                                 message_id: str, late_note: str = "") -> None:
        """跑完这一轮，然后把那条消息上的「在办」撤掉。

        用户原话：「办完了应该表情应该取消啊」。
        🔴 撤表情放在 `finally`：出错、被 /stop 停掉、超时的那一轮也要撤 ——
        不然恰恰是**出问题的那几条**一直挂着「在办」，而那正是他最需要看清状态的时候。
        撤除本身是一次 HTTP，丢线程里做，不占事件循环。
        """
        replier = getattr(self, "replier", None)
        ack = getattr(replier, "ack", None)
        state = {"phase": "sleeping"}

        async def ack_soon() -> None:
            # 晚 1 秒再贴：1 秒内就回了的（/help、菜单、「会话」）回复本身就是「收到」，
            # 再贴一个马上撤只会闪一下。不按「以 / 开头」判 ——
            # `/Users/me/codes 里有什么` 是要跑一轮的问题。
            await asyncio.sleep(ACK_DELAY_SEC)
            state["phase"] = "sending"
            await asyncio.to_thread(ack, message_id)

        acking = asyncio.create_task(ack_soon()) if ack is not None and message_id else None
        try:
            if late_note and chat_id:
                await asyncio.to_thread(self._say, chat_id, late_note)
            await self._handle(scope, chat_id, text)
        finally:
            if acking is not None and not acking.done():
                if state["phase"] == "sleeping":
                    acking.cancel()          # 还没贴就办完了 ⇒ 不贴
                else:
                    try:
                        await acking         # 贴的请求在路上 ⇒ 等它回来再撤
                    except Exception:  # noqa: BLE001
                        pass
            unack = getattr(replier, "unack", None)
            if unack is not None and message_id:
                try:
                    await asyncio.to_thread(unack, message_id)
                except Exception:  # noqa: BLE001 —— 撤不掉只是多挂一个表情
                    logger.info("「在办」表情没撤掉", exc_info=True)

    async def _handle(self, scope: str, chat_id: str, text: str) -> None:
        try:
            from .menu import is_menu_request

            if text.strip() in ("↑", "最近", "最近问过", "历史"):
                # 打一个「↑」也能调出来（和「菜单」两个字同一个思路）；这几个字不进历史（_skip 挡 <2 字和这里）
                self._send_history_card(scope, chat_id)
                return
            self._remember_input(scope, text)

            # 「菜单」「?」这类不带斜杠的触发词也要进命令分派 ——
            # 用户在手机上打的就是这两个字，不会去打 /menu。
            if text.startswith("/") or is_menu_request(text) or _is_fleet_word(text):
                # 🔴 命令必须在拿会话锁**之前**处理。/stop 的全部意义就是打断
                # 正在跑的那一轮 —— 要是它排在锁后面，等于要等那一轮自己跑完，
                # 那就什么都没打断。
                if await self._handle_command(scope, chat_id, text):
                    return

            # CEO 群里的话交给 CEO（等拍板时就是答复）。独立功能，不碰会话绑定那条路。
            if await self._maybe_ceo_text(scope, chat_id, text):
                return

            # 选好了「新建会话」的工具和项目 ⇒ 这一句是新会话的第一句
            if await self._maybe_start_new_session(scope, chat_id, text):
                return

            # 点过 [接着聊] ⇒ 这一条发到电脑上那个会话，不走自己的 claude
            if await self._maybe_send_to_fleet(scope, chat_id, text):
                return

            await self._run_locally(scope, chat_id, text)
        except Exception:
            logger.exception("处理消息时异常 scope=%s", scope)
            from .fleet.actions import do_button

            self._say_with_buttons(chat_id, "我这边出错了，没能完成。点「新话题」再试一次。",
                                   [do_button("new", primary=True)])
        finally:
            self._sync_talk_marker(scope)

    async def _run_locally(self, scope: str, chat_id: str, text: str) -> None:
        """交给桥自己的 claude（排队、换回记住的目录、拿会话锁）。"""
        session = self.sessions.get(scope)
        # SessionStore.get() 新建时写的是 default_workspace ⇒ 重启后第一条
        # 消息会在默认目录跑，落盘就白存了。这里取回记住的那个
        # （get() 内部再过一遍白名单，所以这行本身也是闸门）。
        session.workspace = self.workspaces.get(scope)

        if getattr(getattr(self, "updater", None), "installing", False):
            # 🔴 第 25 轮（P-sre）：安装器正在换掉这个进程（约十几秒）⇒ 现在开的轮会被半路杀掉
            self._say(chat_id, "我正在装新版，十几秒后就好。好了再发一次这句话。")
            return

        # 入队要立刻说一声、带排队深度。否则用户以为机器人死了。
        if session.lock.locked():
            session.waiting += 1
            self._say(chat_id, f"前面还有 {session.waiting} 条在跑，这条排上了")

        async with session.lock:
            session.waiting = max(0, session.waiting - 1)
            # 作废挂着的确认卡在 `_run_one` 里、审查放行之后做（见 `_supersede_pending_approvals`）。
            # 批准后的续跑（`_continue_after_approval`）带 superseding=False，不会把自己顶掉。
            # 用会话上的标记传、不加 _run_one 的参数：大量测试把 _run_one 换成三参数的假函数
            session.supersede_next = True
            # 第 32 轮：这句话本身就是对挂着那张卡的回答 ⇒ 同样用会话上的标记交给作废那一步
            session.supersede_text = text
            try:
                await self._run_one(session, chat_id, text)
            finally:
                session.supersede_next = False
                session.supersede_text = ""

    def _supersede_pending_approvals(self, scope: str, chat_id: str) -> None:
        """有确认卡挂着、他又发了一句新话 ⇒ 先把那张卡作废，并明说一句。

        🔴 第 20 轮（S2 日志挖掘 Top 1，8 次同意里 3 次中招，11-07 15:13 还在发生）：
        新消息 resume 同一个 claude 会话 ⇒ 被挂起的那条 tool_use 在会话里变成
        `Tool result missing`，模型绕过去接着答。之后点旧卡「同意」照样回「好，继续」、
        也续跑了，但**那一步再也不会跑**，`.ok` 躺在盘上 —— 他完全看不出批的事没做。
        选「作废 + 明说」而不是「排队等他点」：往更少发生的方向走（和 /stop 同一个取舍）。

        第 24 轮（R-approval）收紧：
        - 只认盘上还**没批**的（`.asked` / `.pending`）。`_awaiting` 有值但盘上只剩 `.ok`
          = 刚批过、续跑在排队 ⇒ 不作废、不说话（原来会把刚批的 `.ok` 清掉，还说「没做」）。
        - 按**卡**记（令牌 nonce），不按指纹记：同一条命令之后又被问到时指纹一样，
          按指纹记会让新卡被说成「已作废」、或让旧卡批动新卡。
        """
        gate = getattr(self, "gate", None)
        if gate is None:
            return
        try:
            names = os.listdir(gate.dir_for(scope))
        except OSError:
            names = []
        digests = {n.rsplit(".", 1)[0] for n in names if n.endswith((".asked", ".pending"))}
        if not digests:
            return
        getattr(self, "_awaiting", {}).pop(scope, None)
        issued = getattr(self, "_issued_cards", {}).pop(scope, {})
        card_msgs = getattr(self, "_approval_msgs", {}).pop(scope, {})

        # 🔴 第 32 轮（照 T3 Code：审批挂着时打的话 = 拒绝这一步、原话当理由交给模型）：
        # **只回第一张卡**。2026-11-09 真 claude 实测：defer 之后 `--resume` 加新话，Claude Code
        # 会用同一个 tool_use_id **再调一次钩子** ⇒ 钩子查到 `.deny` 就 deny 并带上原话，
        # 会话里那条 tool_result 从 `[Tool result missing due to internal error]` 变成「他没批，他说：…」。
        # 其余的卡照旧作废。不再说「那一步没做，等这条回完再说一次」：他这句话就是回答。
        try:
            text = getattr(self.sessions.get(scope), "supersede_text", "") or ""
        except Exception:  # noqa: BLE001
            text = ""
        first = gate.first_unanswered(scope, list(issued.values()))
        answered = bool(text.strip()) and gate.answer_with_reply(scope, first, text)
        if not answered:
            gate.clear(scope)

        if not hasattr(self, "_superseded"):
            self._superseded = {}
        voided = self._superseded.setdefault(scope, set())
        voided.update(nonce for nonce, digest in issued.items() if digest in digests)
        if len(voided) > 200:                     # 只在内存、重启清空；防一直不重启时无限长
            self._superseded[scope] = set(list(voided)[-200:])
        logger.info("新消息顶掉了 %d 张待确认的卡 scope=%s 当回答=%s", len(digests), scope[:16],
                    str(first)[:8] if answered else "-")

        # 卡片原地换掉（按钮消失）。第一张写「没让它做 · 你说：…」，其余写「已作废」。
        replier = getattr(self, "replier", None)
        patch = getattr(replier, "patch_settled_card", None)
        first_patched = False
        first_done = False
        for nonce, digest in issued.items():
            if digest not in digests:
                continue
            message_id = card_msgs.get(nonce) or ""
            if answered and digest == first and not first_done:
                first_done = True
                # 卡上的原话也先脱敏、截短（手机上一眼看完）
                from .redact import redact

                said, _ = redact(" ".join(text.split()))
                said = said if len(said) <= 60 else said[:60] + "…"
                ok = bool(patch and patch(message_id, "replied", f"你说：{said}"))
                first_patched = ok
            elif patch:
                patch(message_id, "superseded")
        if answered and not first_patched:
            # 换卡失败（拿不到 message_id、或飞书不让改）⇒ 一句话交代，免得那张卡的按钮看着还能点
            self._say(chat_id, "上面那张确认卡我当你没批，按你这句话来。")
        elif not answered:
            self._say(chat_id, "上面那张确认卡作废了（你发了新消息），那一步没做。")

    async def _run_one(self, session, chat_id: str, text: str) -> None:
        scope = session.scope

        # 第 32 轮：这一轮是不是「重启后接着干」（`_resume_after_restart` 在会话上挂的标记）。
        # 只认提示语本身那一轮 —— 排在它前面的别的话不能把标记吃掉。
        restart = None
        marker = getattr(session, "restart_continue", None)
        if marker and text == marker.get("prompt"):
            restart = marker
            session.restart_continue = None
            if session.turns > 0:
                # 重启后他已经先说了一句、那一轮也跑过了 ⇒ 上一轮的事由他那句接管，不再续
                logger.info("重启续跑让位给他新说的话 scope=%s", scope[:16])
                return

        # 会话被掐停了就只回原因，不执行 —— 抄 dots：发现问题直接暂停，
        # 要人明确解除（/resume）才继续。
        if scope in self._paused:
            from .fleet.actions import do_button

            self._say_with_buttons(
                chat_id,
                f"⏸ 这个会话已暂停：{self._paused[scope]}\n确认没问题的话点「继续」。",
                [do_button("resume", primary=True), do_button("new")],
            )
            return

        # 🔴 **这一轮的闸门在这里定格一次**，下面全用这个 `policy`。
        # 切档（/mode）和跑任务是两个并发事件：每处各读一次"当前档位"的话，
        # 中途切一下就会出现"审查层按只读判、工具白名单按全授权发"这种对不上账的情况。
        # 定格的代价是切档要下一轮才生效 —— 那正是用户期待的（这一轮按他发问时的档位跑）。
        policy = self.policy

        # 执行前审查。工具白名单看不到整句话，这一层看得到。
        decision = self.reviewer.review_request(
            ReviewContext(text=text, source="human", mode=policy.mode)
        )
        if decision.verdict is Verdict.DENY:
            self._say(chat_id, f"⛔️ {decision.reason}")
            return
        if decision.verdict is Verdict.ASK:
            # 不在这里拦 —— 放它跑，让审批钩子在**真正要动手的那一刻**挡下来，
            # 然后我们发卡片。这样卡片上写的是具体那条命令，而不是我们从
            # 自然语言猜的意图。
            self._say(chat_id, f"🔒 {decision.reason} —— 会先问你再动手。")

        if getattr(session, "supersede_next", False):
            # 🔴 第 24 轮（R-approval 第 5 条）：原来在审查**之前**作废 ⇒ 被审查层直接拒掉的话
            # （根本没 resume 会话）也把好好的卡作废了。现在只在「真要跑这一轮」时作废。
            self._supersede_pending_approvals(scope, chat_id)

        resume = session.resume_target(self.sessions.idle_reset_sec)
        # 第 33 轮：空闲超时开新轮 ⇒ 记下上一个会话 id，回复下面挂「↩︎ 接着上次」（只在内存，重启就没了）
        continue_offer = self._continue_offer_prepare(session, text, resume, restart)
        if resume is None and session.turns:
            # 刻意**不**单独发一条「开了新会话」的系统消息 —— 噪音。
            # 第 33 轮：改成在回复下面挂一个「↩︎ 接着上次」按钮，真要接着聊的人点一下就回去。
            logger.info("会话空闲超时，开新轮：%s", scope)

        # 第 32 轮：开跑就落一条「这一轮在跑」；进程被杀删不掉 ⇒ 下次启动续它（`bridge/inflight.py`）。
        # 用户自己说的话 ⇒ continue_of=0（链清零）；重启续出来的 ⇒ 接着原来那条链数。
        inflight = self._inflight_begin(scope, chat_id, session, text, resume, restart)

        def on_session(sid: str) -> None:
            # 🔴 跑在 runner 读子进程的循环里：同步、不 await（runner.py 文件头第 1 条）
            self._inflight_session(scope, inflight, sid)

        # 进度：runner 本来就在数工具调用、记当前工具、算耗时，但以前**一个都没传出来**
        # （`on_progress` 零调用方）。现在把它喂给一张原地更新的状态卡，
        # 替掉原来那两条干巴巴的「还在跑（90s）」。
        turn_started = time.monotonic()
        latest = {"tool_calls": 0, "last_tool": ""}

        def on_progress(progress) -> None:
            latest["tool_calls"] = progress.tool_calls
            latest["last_tool"] = progress.last_tool

        stop = asyncio.Event()
        cancel = asyncio.Event()
        self._cancels[scope] = cancel
        # 超过 progress_escalate_sec（默认 420s）在卡上说「比平时久，可以先去忙」。
        # 🔴 2026-11-06：这个配置项原来读进来就没人用（竞品对标 E Top5）。
        status = StatusCard(self.replier, chat_id,
                            escalate_sec=getattr(self.settings, "progress_escalate_sec", 0))

        async def ticker() -> None:
            """定期刷状态卡。

            第一次刷之前要等一会儿 —— 大多数问题几秒就answered，立刻发一张
            「还在跑」只是噪音。之后按固定间隔原地更新同一张卡，不刷消息流。
            """
            started_at = time.monotonic()
            first_delay = self.settings.progress_first_notice_sec
            try:
                await asyncio.wait_for(stop.wait(), timeout=first_delay)
                return  # 在第一次提示之前就跑完了，一张卡都不发
            except asyncio.TimeoutError:
                pass
            while not stop.is_set():
                # 🔴 第 19 轮 S1-A4：回答卡还在一行行往外冒字 ⇒ 他已经知道它活着，
                # 不再冒出一张「还在跑」（实测 122～135 秒两张卡同时在刷）。
                # 回答卡停住超过一个刷新间隔（工具调用中间实测能停 107 秒）⇒ 状态卡照常出来。
                # 🔴 第 24 轮（R-stream）：只按「回答卡在动」压的话，一直边说边干的长轮次**永远**不出状态卡 ——
                # 而「⏹ 停下」按钮和「比平时久，可以先去忙」都只在状态卡上（相对第 19 轮之前是回归）。
                # 到了 escalate 那条线（默认 420 秒）就不再压。
                idle = streamer.idle_sec() if streamer is not None else None
                elapsed_now = time.monotonic() - started_at
                escalate = getattr(self.settings, "progress_escalate_sec", 0) or 420
                if idle is None or idle >= _STATUS_REFRESH_SEC or elapsed_now >= escalate:
                    await status.update(
                        elapsed=int(time.monotonic() - started_at),
                        tool_calls=latest["tool_calls"],
                        last_tool=latest["last_tool"],
                    )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=_STATUS_REFRESH_SEC)
                except asyncio.TimeoutError:
                    continue

        watch = asyncio.create_task(ticker())
        result = None  # run_turn 抛异常时状态卡收尾要知道"没结果"
        streamer = None  # 第 19 轮：抛异常时也要把流式卡关掉，不能停在「生成中」
        try:
            # 按 mode 设全授权开关（钩子读这个文件决定要不要弹卡片）。
            # 每轮都设一次：用户切了档要立刻生效。
            self.gate.set_trusted(scope, policy.mode == "trusted")
            # 钩子要靠这份清单判"命令里的路径有没有伸出工作目录"。
            # 工具作用域管不到 Bash（实测 `cat <目录外文件>` 照样读到）。
            self.gate.write_workspaces(scope, policy.workspaces)
            # ── 第 18 轮：流式回答（用户催过多次）──
            # runner 那边 `on_text` 必须同步、只记不发（它跑在读子进程的循环里，
            # 插 await 会堵死队列）。所以这里只把最新全文存进 `pending_text`，
            # 由下面 `stream_pump` 这个独立协程按节流推给飞书。
            streamer = self._new_streamer(chat_id)
            pending_text = {"full": "", "sent": ""}

            def on_text(full: str) -> None:
                pending_text["full"] = full

            async def stream_pump() -> None:
                """每 0.2 秒看一眼有没有新文字。节流判据在 `StreamReply.should_push`。"""
                while not stop.is_set():
                    await asyncio.sleep(0.2)
                    full = pending_text["full"]
                    if not full or full == pending_text["sent"] or streamer.broken:
                        continue
                    if not streamer.live and not await streamer.start():
                        return          # 建不出来（多半没权限）⇒ 这一轮安静走老路
                    if await streamer.push(full):
                        pending_text["sent"] = full

            pump = asyncio.create_task(stream_pump())
            try:
                result = await self.runner.run_turn(
                    text,
                    session.workspace,
                    resume=resume,
                    approval_dir=self.gate.dir_for(scope),
                    on_progress=on_progress,
                    cancel=cancel,
                    policy=policy,
                    on_text=on_text,
                    on_session=on_session,
                )
            finally:
                stop.set()
                pump.cancel()
                try:
                    await pump
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        except (asyncio.CancelledError, GeneratorExit):
            # 进程在退 / 事件循环在拆 ⇒ 这一轮没做完，**留着**那条给下次启动续
            inflight = None
            raise
        finally:
            # 跑完 / 失败 / 被 /stop / 停在等确认（钩子已落 .pending）⇒ 这一轮都算结束了
            self._inflight_end(scope, inflight)
            stop.set()
            await watch
            self._cancels.pop(scope, None)
            await status.finish(
                elapsed=int(time.monotonic() - turn_started),
                tool_calls=latest["tool_calls"],
                outcome=_outcome_of(result),
            )
            if result is None and streamer is not None and not streamer.live:
                try:
                    await streamer.abandon()
                except Exception:  # noqa: BLE001
                    pass
            if result is None and streamer is not None and streamer.live:
                # 🔴 第 19 轮 S1-A3：run_turn 抛异常 ⇒ 原来回答卡一直「生成中」到飞书 10 分钟自动关
                try:
                    await streamer.finish("", footer="⚠️ 这一轮没跑完")
                except Exception:  # noqa: BLE001 —— 收卡失败不能盖掉原来那个异常
                    logger.exception("流式卡收尾失败（异常路径）")

        session.last_active = time.monotonic()
        session.turns += 1
        if result.session_id:
            session.claude_session_id = result.session_id

        # 🔴 第 19 轮 S1-A1/A2/A3：停下 / 失败 / 等确认 这三条路原来都不收流式卡 ⇒
        # 卡一直「生成中」、聊天列表挂着「正在回答…」，等确认那条还把同一段话再发一遍。
        # 现在先让卡收尾；卡已经交付了正文 ⇒ 不再发同一段消息。
        card_has_text = False
        if not streamer.live:
            await streamer.abandon()       # 推到一半坏了的卡：至少关掉流（第 24 轮）
        if getattr(result, "cancelled", False) and streamer.live:
            # 🔴 第 24 轮（R-stream 实测）：停在第二段中途 ⇒ result.text 只有第一段（runner 只认完整块），
            # 拿它重写会把卡上已经显示的第二段抹掉、也不补发。卡上的更长 ⇒ 就用卡上的。
            shown = streamer.shown_text()
            if len(shown) > len(result.text or ""):
                result.text = shown
        if streamer.live and (getattr(result, "cancelled", False) or not result.ok
                              or result.deferred):
            footer = ("⏹ 停了" if getattr(result, "cancelled", False)
                      else "⚠️ 没跑完，原因在下面" if not result.ok
                      else "⏸ 停在这一步，看下面")
            try:
                card_has_text = bool(await streamer.finish(
                    result.text if result.ok or getattr(result, "cancelled", False) else "",
                    footer=footer)) and bool(result.text)
            except Exception:  # noqa: BLE001 —— 收卡失败 ⇒ 照旧用消息发正文
                logger.exception("流式卡收尾失败")

        if getattr(result, "cancelled", False):
            # 用户自己停的，不是故障。已经跑出来的部分照样给他。
            if result.text and not card_has_text:
                self.replier.send(chat_id, result.text)
            self._say(chat_id, "⏹ 停了。有别的事直接说。")
            return

        if not result.ok:
            # 失败就如实说失败。零输出、超时都走这里，不伪装成成功。
            # 提示里说的「🆕 新话题」要真有这个按钮（2026-11-05：全按钮化）
            from .fleet.actions import do_button

            self._say_with_buttons(chat_id, f"❌ {result.error}", [do_button("new")])
            return

        if result.deferred:
            # 钩子把某个工具挡下来了，进程已经退出（不占资源、不会超时）。
            # 发卡片等人点；点了之后 resume 同一个 claude 会话接着跑。
            self._ask_approval(scope, chat_id, session, result, text_delivered=card_has_text)
            return

        body = result.text
        if result.denials:
            names = sorted({d.get("tool_name", "?") for d in result.denials})
            body += f"\n\n⛔️ 这几步被挡下了（当前权限不允许）：{', '.join(names)}"

        # 执行后审查：反复试探被拦的工具 ⇒ 掐停等人看。
        # 判据不是"有没有被拒"（偶尔撞边界正常），而是同一轮里被拒多次。
        outcome = self.reviewer.review_outcome(
            result.denials,
            ReviewContext(text=text, source="human", mode=policy.mode),
        )
        if outcome.verdict is Verdict.PAUSE:
            self._paused[scope] = outcome.reason
            self._save_paused()
            body += f"\n\n⏸ {outcome.reason}\n确认没问题的话点「继续」。"

        # 🔴 流式卡能装下全文 ⇒ 让它收尾（原地变成终态卡），**不再发一条消息** ——
        # 否则同一段话在聊天里出现两次。装不下 / 没流式 ⇒ 照旧发消息。
        # 终态一律用 `result.text` 全文重写，不信流式累加值（delta 可能丢）。
        ok = False
        if streamer.live:
            ok = await streamer.finish(body)
            if ok:
                logger.info("流式回答：这一轮推了 %d 帧，正文由卡片交付", streamer.pushes)
        if not ok:
            ok = self.replier.send(chat_id, body)
        if scope in self._paused:
            # 刚被掐停 ⇒ 下一步就是「继续」，给个按钮（文字 /resume 照样认）
            from .fleet.actions import do_button

            self._send_buttons(chat_id, [do_button("resume", primary=True), do_button("new")])
        elif ok and continue_offer:
            # 第 33 轮：这一轮是因为空闲超时才开的新会话 ⇒ 回复下面挂「↩︎ 接着上次」
            self._continue_offer_attach(scope, chat_id, continue_offer, result.session_id)
        if not ok:
            # 发送失败必须让用户知道，否则就是「活干完了但你永远看不到」。
            logger.error("回复发送失败 chat_id=%s chars=%s", chat_id, len(body))

    def _ask_approval(self, scope: str, chat_id: str, session, result,
                      text_delivered: bool = False) -> None:
        """发审批卡片。卡片发不出去就退化成纯文本 —— 不能让人无从得知。"""
        pending = self.gate.take_pending(scope)
        if not pending and result.text and not text_delivered:
            # 🔴 第 24 轮（R-stream 实测）：下面两个提前 return 原来都不发正文 ⇒ 卡装不下时卡上写着
            # 「完整内容在下面那条消息里」，下面却什么都没有（21000 字丢了 13000）。
            self.replier.send(chat_id, result.text)
            text_delivered = True
        if not pending and getattr(self.gate, "last_skipped_asked", None):
            # 🔴 第 14 轮：这一步的卡**已经发过、还挂在聊天里等他点**（take_pending 的红字）。
            # 不再发第二张（现场 2026-11-06 19:29 同一指纹两张卡），也不要报「没拿到要确认的内容」。
            # `_awaiting` 保持原样：他点那张老卡时照旧能续跑。
            logger.info("审批卡 已经发过、不重发 scope=%s digest=%s",
                        scope[:16], str(self.gate.last_skipped_asked[0])[:8])
            # 🔴 这一句必须有。R1 用真钩子跑完整链发现：这里原来只写日志就 return ⇒
            # 「点了同意但没存住 → 照提示重发 → 又 defer 到同一条 → 什么都不回」，
            # 一静默就是 `_ASKED_FRESH_SEC`=30 分钟。第 14 轮我为修「多一张卡」
            # 亲手造出了今天另一种「发了消息没反应」。
            from .fleet.actions import do_button

            self._say_with_buttons(
                chat_id,
                "这一步的确认卡还挂在上面等你点（往上翻一下就能看到）。\n"
                "点了没反应的话，点下面「🆕 新话题」重开一轮，再把它做一次。",
                [do_button("new", primary=True)],
            )
            return
        if not pending:
            # 钩子 defer 了但没留下待办：不知道在等什么，如实说，别假装在等。
            from .fleet.actions import do_button

            self._say_with_buttons(
                chat_id,
                "⚠️ 我停下来等你确认，但没拿到要确认的内容。点「新话题」重开一轮。",
                [do_button("new", primary=True)],
            )
            return

        if result.text and not text_delivered:   # 流式卡已经显示了这段 ⇒ 不再发一遍
            self.replier.send(chat_id, result.text)

        # 记住 resume 目标。点了同意之后要接着跑的就是这个会话。
        self._awaiting[scope] = {
            "chat_id": chat_id,
            "session_id": result.session_id,
            "count": len(pending),
        }

        for item in pending:
            action = describe_action(item.get("tool_name", "?"), item.get("tool_input", {}))
            token = self.approvals.issue(
                scope, item["digest"], {"digest": item["digest"], "scope": scope}
            )
            if not hasattr(self, "_issued_cards"):
                self._issued_cards = {}
            self._issued_cards.setdefault(scope, {})[token.nonce] = item["digest"]
            approve_value = {"act": "approve", **token.to_value()}
            deny_value = {"act": "deny", **token.to_value()}
            sent = self.replier.send_approval_card(
                chat_id, "要动手了，先问你一下", action, approve_value, deny_value
            )
            if isinstance(sent, str) and sent:
                # 第 32 轮：记下这张卡的 message_id ⇒ 卡挂着时他打字，能把它原地换成「没让它做」
                if not hasattr(self, "_approval_msgs"):
                    self._approval_msgs = {}
                self._approval_msgs.setdefault(scope, {})[token.nonce] = sent
            # 记下「出了一张审批卡」：工具名 + 指纹前 8 位 + 发没发出去。不记命令原文
            # （可能带凭据）。2026-11-05 体验审查：原来日志里只有「收到点击 act=approve」，
            # 还原不出「出卡 → 同意 → 又被问」这条链（正是那次生图反复要批准的现场）。
            logger.info(
                "发出审批卡 scope=%s tool=%s digest=%s 送达=%s",
                scope[:16], item.get("tool_name", "?"), str(item.get("digest", ""))[:8], bool(sent),
            )
            if not sent:
                # 卡片发失败 ⇒ 按钮这条路断了，明确告诉用户，别让它静默卡死
                self._say(
                    chat_id,
                    f"🔒 我想做这件事，但确认卡片发不出去：\n{action}\n"
                    "这一轮已停下。你自己在终端里做，或者点「🆕 新话题」重开。",
                )
                self._awaiting.pop(scope, None)
                self.gate.clear(scope)

    def _on_menu_click(self, event, clicker: str, value: dict):
        """菜单按钮被点了 ⇒ 当成用户发了那句话，走**正常**流程。

        🔴 不能走捷径直接执行：
        - 鉴权要重新判（点按钮的人可能不是主人 —— 卡片在群里谁都能点）
        - 审查层、审批钩子、读边界都要照常生效
        否则菜单就成了绕过所有闸门的后门。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .menu import resolve_click

        if clicker not in self.settings.owner_open_ids:
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": _CARD_TOASTS[ApprovalStore.FORBIDDEN]}}
            )
        text = resolve_click(value)
        # 记是哪个按钮（只记预设的 cmd / 问句键，不记正文）：第 28 轮想按真实点击给菜单卡排序时，
        # 日志里 33 次点击一次都看不出点的是哪个
        logger.info("菜单卡 点击 %s", (value.get("cmd") or ("问句" if value.get("say") else "?"))[:16]
                    if isinstance(value, dict) else "?")
        if not text:
            # 只认菜单里预设过的值 —— 伪造的回调不能让 agent 跑任意指令
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": _CARD_TOASTS[ApprovalStore.MALFORMED]}}
            )

        context = getattr(event, "context", None)
        chat_id = getattr(context, "open_chat_id", "") or ""
        if not chat_id:
            # 回调里没带 chat_id 就用单聊记下来的那个
            chat_id = getattr(self, "_chat_ids", {}).get(f"dm:{clicker}", "")
        if not chat_id:
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "找不到要回复的对话，直接打字问一句就好"}}
            )

        # 🔴 scope 要和「这个 chat 是单聊还是群」一致，且**不在卡片回调里改写 _chat_ids**
        # （同 _on_fleet_click 的红字：重启后在群里点旧卡片，原来会把单聊 scope 指向这个群）。
        known = getattr(self, "_chat_ids", {})
        group_scope = next((sc for sc, c in known.items() if c == chat_id and sc.startswith("group:")), "")
        if group_scope:
            scope = group_scope
        elif known.get(f"dm:{clicker}") in ("", None, chat_id, clicker):
            # 记过的单聊、重启后一次都没记过，或者**只点过常驻菜单**（菜单事件只带 open_id，
            # 记下的是 `ou_…` 而不是真 chat_id —— 同 _click_scope 的红字，2026-11-05 实测撞上）
            scope = SessionStore.scope_for(chat_id, "p2p", clicker)
            if not hasattr(self, "_chat_ids"):
                self._chat_ids = {}
            self._chat_ids[scope] = chat_id  # 补记真 chat_id，覆盖掉 open_id
        else:
            # 单聊记的是别的 chat ⇒ 这张卡在群里，而群还没记录过：不猜
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "服务刚重启过，先在这个聊天里发一句话再点"}}
            )
        # 🔴 第 33 轮（用户：「原来是最快，我点最聪明，下面说变换了，上面四个按钮没变，还是最快被选中」）：
        # 选模型 / 收紧权限原来只是当成一句命令丢后台 ⇒ 聊天里多一条「换成了…」，**这张卡原样不动**，
        # ✅ 还停在旧的那个上。现在这两种在回调里当场切、响应体带新卡**原地刷新**（同审批卡的 `_settled`）。
        # 放开权限不走这里：那要先弹二次确认卡（`_handle_mode` 的红字），照旧走命令。
        refreshed = self._menu_switch_in_place(value)
        if refreshed is not None:
            toast, card, said = refreshed
            body = {"toast": {"type": "info", "content": toast}}
            if card is not None:
                body["card"] = {"type": "raw", "data": json.loads(card)}
            # 用户：「『🧠 换成 稳（claude-opus-5）。下一条消息开始生效』这样的提示要依然留着」
            # ⇒ 卡原地刷新之外，聊天里照旧留一条（丢后台发：回调要 3 秒内返回）
            if said:
                self._say_later(chat_id, said)
            return P2CardActionTriggerResponse(body)
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._handle(scope, chat_id, text), self._loop)

        return P2CardActionTriggerResponse(
            {"toast": {"type": "info", "content": _toast_for(text)}}
        )

    def _menu_switch_in_place(self, value: dict):
        """模型卡 / 权限卡上点了一档 ⇒ 当场切，返回 (toast, 新卡 JSON)。不归这里管 ⇒ None。

        只处理两种：换模型；切到不比现在松的档位。别的（放开权限、问句、命令）返回 None 走原路。
        切不动（prefs 没有 / 名字不认识）也返回 None，原路会把原因说给他听。
        """
        from .menu import build_mode_card, build_model_card
        from .policy import MODE_LABELS, MODE_RANK, MODEL_LABELS, known_model

        prefs = getattr(self, "prefs", None)
        if prefs is None or not isinstance(value, dict):
            return None
        model = value.get("model")
        if isinstance(model, str):
            if not known_model(model) or not prefs.set_model(model):
                return None
            label = MODEL_LABELS.get(model, model)
            note = "" if prefs.last_save_ok else "（这次没存下来，重启后会回到原来那个）"
            said = f"🧠 换成 **{label}**（{model}）。下一条消息开始生效。{note}"
            return f"换成「{label}」了", build_model_card(self.policy), said
        mode = value.get("mode")
        if isinstance(mode, str) and mode in MODE_RANK:
            current = self.policy.mode
            if MODE_RANK[mode] > MODE_RANK.get(current, 0):
                return None                       # 放开 ⇒ 要二次确认，走原路
            applied, _clamped = prefs.set_mode(mode)
            config_path = getattr(self.settings, "config_path", "") or ""
            if config_path and os.path.exists(config_path):
                self._erase_migration_marker(config_path)   # 同 _handle_mode：他亲手选过档了
            label = MODE_LABELS.get(applied, applied)
            said = f"🔑 现在是 **{label}**\n{self.policy.describe()}\n（下一条消息开始生效）"
            if not prefs.last_save_ok:
                said += "\n⚠️ 这次没存下来，重启后会回到配置里那一档。"
            return f"现在是「{label}」", build_mode_card(self.policy), said
        return None

    def _send_menu(self, chat_id: str, scope: str) -> None:
        from .menu import build_menu_card

        where = ""
        sessions = getattr(self, "sessions", None)
        if sessions is not None:
            where = sessions.get(scope).workspace
            home = os.path.expanduser("~")
            if where.startswith(home):
                where = "~" + where[len(home):]
        sender = getattr(self.replier, "send_card", None)
        if sender is None or not sender(chat_id, build_menu_card(where, self.policy)):
            # 卡片发不出去就退化成文字帮助 —— 别让用户叫菜单却什么都没收到
            self._say(chat_id, HELP)

    def on_bot_menu(self, data) -> None:
        """常驻菜单（输入框上面那一排）被点了。

        🔴 **这个事件不带 chat_id，只带点击人的 open_id。**
        读 SDK 的 `P2ApplicationBotMenuV6` 确认过：字段只有
        `event_key` / `operator`(只有 operator_id) / `timestamp`。
        所以回消息必须用 `receive_id_type=open_id` ——
        按 chat_id 发会得到 `230001 invalid receive_id`（我们在别的路径上踩过同一个码）。
        这里先查 `_chat_ids`（单聊的 chat_id 之前记过），查不到就按 open_id 发。

        和卡片按钮一样：**不走捷径**，把点击变成"用户发了那句话"再走正常流程，
        鉴权 / 审查 / 审批钩子 / 读边界全部照常生效。
        """
        self.touch()
        try:
            event = getattr(data, "event", None)
            event_key = getattr(event, "event_key", "") or ""
            operator = getattr(event, "operator", None)
            clicker = getattr(operator, "operator_id", None)
            # operator_id 可能是个对象（带 open_id / union_id / user_id）
            if clicker is not None and not isinstance(clicker, str):
                clicker = getattr(clicker, "open_id", "") or ""
            clicker = clicker or ""
            logger.info("收到菜单点击 key=%s clicker=%s", event_key, clicker[:12])

            if clicker not in self.settings.owner_open_ids:
                # 静默丢弃 —— 和消息那条路一致，不给陌生人任何回应
                logger.info("菜单点击 丢弃：不是主人 key=%s", event_key)
                return

            from .botmenu import resolve_event_key

            text = resolve_event_key(event_key)
            if not text:
                logger.warning("菜单 key 不在预设里，忽略：%r", event_key)
                return

            # 🔴 第 22 轮（S2 Top 5）：同一个键 3 秒内连点，原来每下都单独跑 ⇒ 一连串一样的总览卡
            # （83 次点击里 11 次，11-07 18:45 `/sessions` 连 4 下）。手点和飞书重推都吃这一招。
            # /stop 不去抖：它要么第一下就停了，要么后面那下也该再试一次。
            now = time.monotonic()
            if not hasattr(self, "_menu_last"):
                self._menu_last = {}
            last = self._menu_last.get((clicker, event_key), 0.0)
            if now - last < MENU_DEBOUNCE_SEC and not text.startswith("/stop"):
                logger.info("菜单点击 去抖：%.1fs 内同一个键 key=%s", now - last, event_key)
                return
            self._menu_last[(clicker, event_key)] = now

            # 单聊 chat_id 之前记过就用它；没有就用 open_id 发
            scope = f"dm:{clicker}"
            chat_id = getattr(self, "_chat_ids", {}).get(scope, "") or clicker
            if not hasattr(self, "_chat_ids"):
                self._chat_ids = {}
            self._chat_ids.setdefault(scope, chat_id)

            logger.info("菜单点击 转成命令 key=%s 命令=%s 回到=%s", event_key, text.split()[0][:16],
                        "chat" if chat_id != clicker else "open_id")
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._handle(scope, chat_id, text), self._loop
                )
            else:
                logger.warning("菜单点击 丢弃：事件循环还没起来 key=%s", event_key)
        except Exception:
            logger.exception("处理菜单点击失败")

    def on_card_action(self, data):
        """卡片按钮回调。

        飞书回调是**同步**的、3 秒内必须返回，而且**不重推** —— 超时用户直接看到报错。
        所以这里只做核验 + 落一个批准标记 + 立刻回 toast，真正的续跑丢后台。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        self.touch()
        try:
            event = data.event
            operator = getattr(event, "operator", None)
            clicker = getattr(operator, "open_id", "") or ""
            value = getattr(getattr(event, "action", None), "value", None) or {}
            # 每次点击都记一行。之前日志里**一次点击都没有过**，
            # 没法判断是"回调没订阅"还是"收到了但处理错了"。
            logger.info(
                "收到卡片点击 act=%s clicker=%s",
                value.get("act") if isinstance(value, dict) else type(value).__name__,
                clicker[:12],
            )
            if not isinstance(value, dict):
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": _CARD_TOASTS[ApprovalStore.MALFORMED]}}
                )

            # 菜单按钮：在审批令牌核验**之前**分流 —— 它没有令牌，
            # 走审批那条路会被当成坏卡片。
            if value.get("act") == "menu":
                return self._on_menu_click(event, clicker, value)
            # 总控按钮同理：没有审批令牌，白名单 + 最近一次扫描结果才是它的闸门
            if value.get("act") == "fleet":
                return self._on_fleet_click(event, clicker, value)
            # 进对话模式那张确认卡：同一套闸门（主人 + 白名单 op + 扫描过的 id）
            if value.get("act") == "relay":
                return self._on_relay_click(event, clicker, value)
            # 重启后「这句要发给它吗」那两个按钮（第 11 轮）：只带一个序号，会话和原话在服务端记着
            if value.get("act") == "rtalk":
                return self._on_restart_talk_click(event, clicker, value)
            # 「点一下就行」的按钮（退出对话 / 停下 / 继续 …）：白名单 op ⇒ 等价斜杠命令
            if value.get("act") == "do":
                return self._on_do_click(event, clicker, value)
            if value.get("act") == "hist":
                return self._on_history_click(event, clicker, value)
            # 第 33 轮「↩︎ 接着上次」：只带一个序号，要换回哪个会话记在服务端
            if value.get("act") == "cont":
                return self._on_continue_click(event, clicker, value)
            # 「➕ 新建会话」那两张卡：只带工具名 / 服务端序号，目录不在卡上
            if value.get("act") == "new":
                return self._on_new_click(event, clicker, value)
            if value.get("act") == "mdl":
                return self._on_model_click(event, clicker, value)
            if value.get("act") == "sc":
                return self._on_status_card_click(event, clicker, value)
            if value.get("act") == "ch":
                return self._on_choice_click(event, clicker, value)

            # 点击时**重新**查白名单：卡片可能在群里被别人看到，
            # 而白名单在这期间可能已经变了。比"签发时查过"更严。
            redeemed, why = self.approvals.redeem_detailed(
                value, clicker, self.settings.owner_open_ids
            )
            if redeemed is None:
                # 🔴 **每种原因说不同的话，并且给下一步。**
                # 原来四种情况都回「这个按钮已失效或你无权操作」——
                # 用户看完完全不知道该重发消息、该等一会儿、还是自己没权限
                # （2026-11-03 他真的卡在这）。
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": _CARD_TOASTS.get(why, _CARD_TOASTS[""])}}
                )

            scope = redeemed["payload"]["scope"]
            digest = redeemed["payload"]["digest"]

            if str(value.get("nonce") or "") in getattr(self, "_superseded", {}).get(scope, set()):
                # 🔴 第 24 轮（R-approval 第 2/3 条）：这张卡已经作废。**同意和不行都不碰 gate**：
                # 同一条命令可能又挂了一张新卡（指纹一样），原来点旧卡同意会把新卡批掉、
                # 点旧卡不行会把新卡清掉还说「这一轮到此结束」。
                logger.info("点了已作废的卡 act=%s digest=%s", value.get("act"), str(digest)[:8])
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info",
                               "content": "这张卡已作废（你后来发了新消息），这一步没做"},
                     **self._settled("superseded")}
                )

            if str(scope).startswith("fleet:"):
                return self._on_fleet_approval_click(value, scope, digest)

            if value.get("act") == "deny":
                self.gate.clear(scope)
                waiting = self._awaiting.pop(scope, None)
                logger.info("审批 拒绝 digest=%s scope=%s 有人在等=%s",
                            str(digest)[:8], scope[:16], bool(waiting))
                if waiting:
                    # 不在回调线程里同步发 —— 回调 3 秒内必须返回，而发消息是阻塞 HTTP，
                    # 飞书慢一点用户就看到「操作失败」（2026-11-04 review）。
                    self._say_later(waiting["chat_id"], "好，不做了。这一轮到此结束。")
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info", "content": "好，不做这一步"},
                     **self._settled("denied")}
                )

            # 🔴 2026-11-06：**被掐停的会话不许写放行证**，同意也不行。
            # 掐停 = 审查层发现这一轮在系统性试探（同一轮被拦 ≥3 次），要人看过、明确发「继续」才解。
            # 原来这一闸是**意外**有的：重启后点旧卡片在 `redeemed["payload"]` 那里 KeyError，
            # 正好拦住了这条路。修好重启那条路之后它就漏了 —— 掐停期间点一张旧卡片会在磁盘上
            # 留下 `.ok`，「继续」之后下一轮跑到同样的命令直接放行，没有卡片、没有点击
            # （`tests/test_interference.py::test_paused_restored_from_disk_blocks_the_turn_not_the_card`）。
            # 拒绝（上面那个分支）照样放行：往「更少发生」的方向走永远安全。
            if scope in getattr(self, "_paused", {}):
                logger.info("审批 同意被拒：会话已暂停 digest=%s scope=%s", str(digest)[:8], scope[:16])
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error",
                               "content": "这个会话被暂停了，先点「继续」看清楚再批"},
                     **self._settled("paused")}
                )

            # 🔴 第 24 轮（R-approval 第 1 条，复现）：续跑要 `--resume` 同一个 claude 会话才接得上那一步。
            # 桥记的会话 id 只在内存（sessions.py 不落盘）⇒ 重启之后、或空闲超过 30 分钟之后点同意，
            # 原来续跑开的是一个只收到「继续」两个字的新会话 ⇒ 那一步永远不做，卡上却写「已同意」。
            # 钩子落待办时记了 claude 自己的 session_id（claude 的会话记录在它自己的盘上，桥重启不丢）⇒
            # 批准之前先把它取出来，续跑时强制 resume 它。
            hook_session = self.gate.pending_session_id(scope, digest)

            if not self.gate.approve(scope, digest):
                # 分两种：撞红线（说清为什么），或真的写不进去
                reason = getattr(self.gate, "last_block_reason", "")
                # 🔴 第 14 轮：同一步的第二张卡（他连点了好几下 ⇒ 曾经发过两张一样的）。
                # 那一步已经批过、正在做，原来却回「没存住你的批准，重新发一次你的要求」
                # —— 照着做会把已经在跑的活再提一遍（现场 2026-11-06 19:29:59）。
                done = not reason and self.gate.approved_recently(scope, digest)
                logger.info("审批 同意没生效 digest=%s scope=%s 原因=%s",
                            str(digest)[:8], scope[:16],
                            "红线" if reason else ("刚批过" if done else "没有未决项或写不进"))
                if done:
                    return P2CardActionTriggerResponse(
                        {"toast": {"type": "info", "content": "这一步已经批过了，正在做，不用再点"},
                         **self._settled("approved")}
                    )
                content = (
                    f"这一步不能做：{reason}。要做请自己到电脑前操作"
                    if reason
                    else "没存住你的批准，重新发一次你的要求"
                )
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "error", "content": content}}
                )

            target = self._pin_resume(scope, hook_session)
            if not target:
                # 哪儿都拿不到能接上的会话 id ⇒ 不拿「继续」两个字去开一个新会话假装在做。
                # `.ok` 留着（10 分钟有效）：他照提示再发一次，那一步不会再问。
                self._awaiting.pop(scope, None)
                logger.info("审批 同意已记下但接不上那一轮 digest=%s scope=%s", str(digest)[:8], scope[:16])
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info",
                               "content": "同意记下了，但那一轮接不上了（电脑上的服务重启过）。"
                                          "10 分钟内再发一次你的要求，这一步不会再问你"},
                     **self._settled("approved")}
                )
            waiting = self._awaiting.get(scope)
            logger.info("审批 同意已记下 digest=%s scope=%s 续跑=%s",
                        str(digest)[:8], scope[:16],
                        "接着等的那轮" if waiting else "从会话记录续")
            if waiting and self._loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._continue_after_approval(scope), self._loop
                )
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info", "content": "好，继续"},
                     **self._settled("approved")}
                )
            # 🔴 批准写下了，但 `_awaiting` 里没有待续跑的记录。
            # 最常见的情形：一轮里弹了两张卡片，点第一张时已经 resume 过，
            # `_awaiting` 被消费掉 ⇒ 点第二张原来只回「好，继续」然后**再无下文**。
            #
            # 用户批准的那一步**还没做**，所以正确行为是**真的接着跑**，
            # 而不是告诉他"这一轮走完了"。会话 id 从 sessions 里取（只在内存，
            # 重启就没了 —— 所以上面先判了「接得上吗」，走到这里一定接得上）。
            # 容错：拿不到会话也要让批准生效（`.ok` 已经落盘了），
            # 只是没法自动续跑。卡片回调跑在飞书的 3 秒窗口里，
            # 这里抛异常会让用户看到一个技术性的报错。
            session_id = None
            try:
                session_id = self.sessions.get(scope).claude_session_id
            except Exception:  # noqa: BLE001
                logger.debug("取会话 id 失败，批准仍然有效", exc_info=True)
            self._awaiting[scope] = {
                "chat_id": self._chat_for(scope),
                "session_id": session_id,
                "count": 1,
            }
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(
                    self._continue_after_approval(scope), self._loop
                )
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "好，继续"},
                 **self._settled("approved")}
            )
        except Exception:
            logger.exception("处理卡片点击失败")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "error", "content": "我这边出错了，重新发一次你的要求"}}
            )

    @staticmethod
    def _settled(outcome: str) -> dict:
        """审批卡点完之后的**替换卡**，并进回调响应体（第 6 轮）。

        原来点了「同意 / 不行」只弹 toast，卡片原样挂着、两个按钮还亮着 ⇒ 手机上往回翻
        分不清哪张批过（外部侦察 Top1：CC #92528「I hit it daily」）。
        响应体里带 `card` ⇒ 飞书原地把这张卡换成「✅ 已同意 · HH:MM」，按钮消失。

        🔴 只在**这次点击真的改变了结果**时用：同意成功、拒绝成功、被掐停拒。
        已处理过 / 过期 / 签名不对 / 不是主人 ⇒ 不换（非主人不许改主人的卡；
        过期卡换成「已同意」会让人以为刚批过）——那几条 return 不经过这里。

        格式 `{"type": "raw", "data": <卡片 JSON 对象>}`：SDK `CallBackCard{type, data: Any}`
        原样序列化（实测）；`raw`/对象写法按 scout-external.md 记的飞书文档示例，本轮没能再打开文档复核。
        造卡出错不能连累批准本身（`.ok` 已经落盘）⇒ 吞掉、只回 toast。
        """
        try:
            return {"card": {"type": "raw", "data": build_settled_card(outcome)}}
        except Exception:  # noqa: BLE001
            logger.exception("造已处理卡失败 outcome=%s，只回 toast", outcome)
            return {}

    def _knocks_path(self) -> str:
        """敲门计数落在 config 同目录。没有 config_path（测试里造的桥）⇒ 空串 = 只留内存。

        刻意**不**退回 `~/.carher-feishu-agent`：第 23 轮实测，退回的话跑一遍测试就往真机写一个文件。
        真跑的桥一定有 config_path（`Settings.load` 填的）。
        """
        config_path = getattr(getattr(self, "settings", None), "config_path", "") or ""
        directory = os.path.dirname(config_path)
        return os.path.join(directory, KNOCKS_FILE) if directory else ""

    def _load_knocks(self) -> dict:
        """读落盘的敲门计数。读不到 / 坏了 / 超过 24 小时没新敲门 ⇒ 从零开始。"""
        try:
            with open(self._knocks_path() or os.devnull + "/x", encoding="utf-8") as handle:
                data = json.load(handle)
            last_at = float(data.get("last_at") or 0.0)
            if time.time() - last_at > KNOCKS_RESET_SEC:
                raise ValueError("过期")
            return {"count": int(data.get("count") or 0), "last_at": last_at,
                    "who": set(str(x) for x in (data.get("who") or [])[:10])}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"count": 0, "last_at": 0.0, "who": set()}

    def _note_stranger_knock(self, open_id: str) -> None:
        """记一笔「有人跟这台电脑说话，但他不是主人」。

        🔴 第 23 轮（S2 Top 4）：原来只在内存里 —— 实测一天重启约 24 次、有一次敲门 38 秒后就重启了，
        计数活不到主人去看诊断卡。现在落一个小 json：只存次数、最后时间、人的**哈希**（不存 open_id 原文）。
        原子写；写失败吞掉（它在消息入口上，不能把一个诊断功能变成故障源）。
        """
        import hashlib
        import tempfile

        if not hasattr(self, "_knocks"):
            self._knocks = self._load_knocks()
        elif time.time() - self._knocks["last_at"] > KNOCKS_RESET_SEC:
            self._knocks = {"count": 0, "last_at": 0.0, "who": set()}
        self._knocks["count"] += 1
        self._knocks["last_at"] = time.time()
        # 只留前几个，防止被刷爆；个数本身也是信号（一个人找错 vs 一片人探测）
        who = hashlib.sha256(("carher-knock:" + open_id).encode()).hexdigest()[:16]
        if len(self._knocks["who"]) < 10:
            self._knocks["who"].add(who)
        path = self._knocks_path()
        if not path:
            return
        try:
            directory = os.path.dirname(path)
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                             delete=False) as tmp:
                json.dump({"count": self._knocks["count"], "last_at": self._knocks["last_at"],
                           "who": sorted(self._knocks["who"])}, tmp)
                tmp_name = tmp.name
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except Exception:  # noqa: BLE001
            logger.debug("敲门计数落盘失败（只影响诊断卡）", exc_info=True)

    def stranger_knocks(self) -> dict:
        """给诊断卡用：{count, last_at, people}。没人敲过 ⇒ count=0。重启后从盘上读回来。"""
        if not hasattr(self, "_knocks"):
            self._knocks = self._load_knocks()
        got = self._knocks
        if time.time() - float(got.get("last_at") or 0.0) > KNOCKS_RESET_SEC:
            got = {}
        return {
            "count": int(got.get("count") or 0),
            "last_at": float(got.get("last_at") or 0.0),
            "people": len(got.get("who") or ()),
        }

    def _chat_for(self, scope: str) -> str:
        """这个会话该往哪个 chat 回消息。

        🔴 **只能查表，不能从 scope 反推。**
        单聊的 scope 是 `dm:<open_id>`，但发消息用的 `receive_id_type` 是
        `chat_id` —— open_id 当 chat_id 用会被飞书拒收
        （`code=230001 invalid receive_id`，真实日志里出现过）。
        表在收消息时填（见 `on_message`）。查不到就返回空串，
        调用方会跳过发送 —— 宁可不发，也别发一个必然失败的请求。
        """
        known = getattr(self, "_chat_ids", {})
        if scope in known:
            return known[scope]
        if scope.startswith("group:"):
            # 群聊的 scope 后半段**就是** chat_id，可以直接用
            return scope.split(":", 1)[1]
        return ""

    def _pin_resume(self, scope: str, hook_session: str) -> str:
        """批准后续跑要 resume 的会话 id；拿不到返回空串。顺手让这个会话**不因空闲被重置**。

        优先用钩子记下的那个（就是被挂起那一步所在的会话），其次桥内存里的。
        批准 = 他明确要接着做那一步，所以空闲 30 分钟的「从头开」规则在这里不适用。
        """
        try:
            session = self.sessions.get(scope)
        except Exception:  # noqa: BLE001
            return ""
        waiting = (getattr(self, "_awaiting", {}) or {}).get(scope) or {}
        target = hook_session or str(waiting.get("session_id") or "") or (session.claude_session_id or "")
        if target:
            session.claude_session_id = target
            session.last_active = time.monotonic()
        return target

    async def _continue_after_approval(self, scope: str) -> None:
        """批准之后 resume 同一个 claude 会话，让它接着做那一步。"""
        waiting = self._awaiting.pop(scope, None)
        if waiting is None:
            return
        chat_id = waiting["chat_id"]
        session = self.sessions.get(scope)
        try:
            async with session.lock:
                await self._run_one(session, chat_id, "继续")
        except Exception:
            logger.exception("批准后续跑失败 scope=%s", scope)
            self._say(chat_id, "同意收到了，但接着跑的时候出错了。重新发一次你的要求。")

    def _clear_approvals_for_stop(self, scope: str) -> bool:
        """`/stop` 那一下：作废这个会话所有待批的审批卡 → 刚才有没有在等批的（决定说不说「卡片按钮已失效」）。

        🔴 2026-11-06 第 9 轮复审 #4：原来只在内存 `_awaiting` 有记录时才 `gate.clear` ——
        重启后 `_awaiting` 是空的，磁盘上的 `.asked` 还在 ⇒ 回「现在没有在跑的任务」，
        之后点那张旧卡「同意」照样写下 `.ok` 并续跑（说了停，那一步照样被执行）。
        现在**不论 `_awaiting` 有没有都清**（往「更少发生」走永远安全）；判「有没有在等」也看磁盘，
        和 `.ok` 同寿命。
        """
        waiting = getattr(self, "_awaiting", {}).pop(scope, None)
        had = bool(waiting)
        gate = getattr(self, "gate", None)
        if gate is None:
            return had
        try:
            directory = gate.dir_for(scope)
            had = had or any(name.endswith((".asked", ".pending")) for name in os.listdir(directory))
        except OSError:
            logger.debug("/stop 读待批目录失败", exc_info=True)
        gate.clear(scope)
        return had

    async def _handle_command(self, scope: str, chat_id: str, text: str) -> bool:
        from .menu import is_menu_request

        if is_menu_request(text):
            self._send_menu(chat_id, scope)
            return True

        command = text.split()[0].lower()
        stripped = text.strip()

        # 会话总控。放在换目录之前：`/sessions` 不该被任何别的分支吃掉。
        if command in FLEET_COMMANDS or stripped in FLEET_WORDS:
            await self._fleet_overview(chat_id)
            return True
        if await self._handle_newsession_command(scope, chat_id, text):
            return True
        if command == "/ceo":
            await self._handle_ceo_command(scope, chat_id, text)
            return True
        if command in CANCEL_COMMANDS or _is_cancel_word(text):
            # 绑过的群里「退出」= 解绑（只关转发，群留着）。不然下一句又按绑定接回去，退出是假的。
            if self._group_bind_quit(scope, chat_id):
                return True
            control = getattr(self, "fleet", None)
            target = control.clear_pending(scope) if control is not None else None
            # 等确认的那张卡也一起作废：用户说了取消，再去点那张卡不该还能进对话模式
            pending_consent = getattr(self, "_pending_consent", {}).pop(scope, None)
            # 确认前记着的话（群里那条路）一起作废
            dropped = self._drop_held(scope)
            # 重启前在对话模式、重启后还没说话就点了旧的「退出对话」⇒ 照实说断了（第 11 轮）
            restarted = self._forget_restart_talk(scope)
            new_armed = self._newsession_state().disarm(scope)
            # 🔴 记下「他说了退出」。有一句还在飞时，回复到了之后不许再把对话模式挂回来
            # （2026-11-04 审查：原来 /cancel 回「没有要取消的」，几秒后模式自己回来了）。
            flying = getattr(self, "_fleet_inflight", {}).get(scope)
            if flying is not None:
                if not hasattr(self, "_talk_quit"):
                    self._talk_quit = {}
                self._talk_quit[scope] = True
                label = target.label if target is not None else "它"
                self._say(chat_id, f"好，退出了，之后你说的话不再发给{label}。"
                                   "刚发出去那一句还在跑，它的回复还会带回来一次。")
                return True
            if target is not None:
                # 🔴 只退出对话模式，**不**停目标那一轮 —— 它可能已经在干活了，
                # 而且从外面停会关掉用户整个会话。说清这一点，否则他以为「取消」把活也撤了。
                from .fleet.actions import do_button

                self._say_with_buttons(chat_id, f"好，退出了，不再把你说的话发给{target.label}。"
                                                "它手里已经开始的那一轮还在跑，要停只能到电脑上按 Esc。",
                                       [do_button("sessions", "🖥 回到总览")])
            elif pending_consent is not None:
                self._say(chat_id, "好，不进对话模式。" + ("你刚才说的那几句也没发过去。" if dropped else ""))
            elif restarted:
                self._say(chat_id, f"好，退出了。服务刚才重启过，和{restarted}的对话已经断了，"
                                   "之后你说的话不会再发给它。")
            elif dropped:
                self._say(chat_id, "好，你刚才说的那几句没发过去。")
            elif new_armed:
                self._say(chat_id, "好，不新建会话了。")
            elif command in CANCEL_COMMANDS or _cancel_core(text) == _CANCEL_WORD_ALWAYS:
                self._say(chat_id, "没有要取消的。直接说要干什么就行。")
            else:
                # 「算了」「退出」这类说法、又没有任何东西可取消 ⇒ 就是一句普通的话，照平常处理
                return False
            return True
        if command == "/talk!":
            # 文字确认通道：确认卡发不出去时的退路（同 `/mode!` 的道理 —— 打出这句本身
            # 就是明确意图，而非主人的消息在鉴权那层就被丢了）。只对**刚刚弹过卡**的那个
            # 会话生效，不能凭空进任何会话。
            await self._confirm_talk_by_text(scope, chat_id)
            return True

        # 换目录。放最前面：这些命令不碰会话锁，也不该被别的分支吃掉。
        #
        # 用 getattr 取依赖：这段跑在命令分派的最前面，而命令处理是用户唯一的
        # 自救通道（/stop、/new）。缺任何一个依赖都不该让整条分派崩掉 ——
        # 宁可"换目录用不了"，不要"连 /stop 都发不出"。
        changed = None
        store = getattr(self, "workspaces", None)
        sessions = getattr(self, "sessions", None)
        if store is not None and sessions is not None:
            try:
                changed = workspace_mod.handle(
                    self.settings.policy,
                    store,
                    scope,
                    sessions.get(scope).workspace,
                    text,
                )
            except Exception:  # noqa: BLE001
                logger.exception("处理换目录命令失败")
                changed = None
        if changed is not None:
            if changed.ok and changed.workspace:
                # 🔴 Session.workspace 是 runner 的 cwd 来源，必须跟着改
                sessions.get(scope).workspace = changed.workspace
                # 换了目录就开新会话。`--resume` 会带着旧目录攒的上下文，
                # 模型会继续引用上一个仓库的文件路径 —— 用户看到的是
                # "它在说别的项目的事"。代价是丢上下文，但那比答错强。
                sessions.reset(scope)
                self._awaiting.pop(scope, None)
                gate = getattr(self, "gate", None)
                if gate is not None:
                    gate.clear(scope)
            self._say(chat_id, changed.reply)
            return True
        if command in {"/history", "/历史", "/最近"}:
            self._send_history_card(scope, chat_id)
            return True
        if command in {"/help", "/h"}:
            self._say_with_buttons(chat_id, HELP, self._help_buttons())
            return True
        if command in {"/mode", "/权限"}:
            self._handle_mode(chat_id, text)
            return True
        if command == "/mode!":
            # 放开确认卡上那一下。`!` 形式只从卡片回调来（menu.resolve_click），
            # 用户自己打这句也只会走到同一处 —— 而那时他已经明确表达了意图，
            # 和点了确认按钮等价，所以不必再拦一次。
            self._handle_mode(chat_id, text, confirmed=True)
            return True
        if command in {"/model", "/模型"}:
            self._handle_model(chat_id, text)
            return True
        if command == "/status":
            session = self.sessions.get(scope)
            home = os.path.expanduser("~")
            # 路径缩成 ~/... —— 全路径在手机上会折行，也没多给信息
            where = session.workspace
            if where.startswith(home):
                where = "~" + where[len(home):]
            # 刻意**不**显示"长连接静默多少秒"和"会话轮次"：
            # 那是内部指标，用户看了只会困惑。他要知道的是
            # 「在哪」「能做什么」「还活着吗」。
            policy = self.policy
            lines = [
                f"📁 现在在 **{where}**",
                f"🔑 {policy.describe()}",
                f"🧠 模型 {MODEL_LABELS.get(policy.model, policy.model)}",
            ]
            connected = self.connection_alive() or self.silent_for < 120
            lines.append("🟢 连着飞书，随时能用" if connected else "🔴 和飞书的连接断了，正在重连")
            paused = scope in self._paused
            if paused:
                lines.append(f"⏸ 已暂停：{self._paused[scope]}")
                lines.append("确认没问题的话点「继续」。")
            if self.settings.custom_rules:
                lines.append(f"🚫 你自己加了 {len(self.settings.custom_rules)} 条禁止项")
            control = getattr(self, "fleet", None)
            if control is not None:
                # 同总览：走共用扫描（撞上后台那一拍原来会说「在跑 0 个」）；没读全不说 0
                line = await asyncio.to_thread(self._fleet_status_line, self._fleet_scanner(), control)
                if line:
                    lines.append(line)
            # 关键动作给按钮（11-05 用户原话：不要让用户输入命令）。`/cd 项目名` 带参数，
            # 按钮做不了 ⇒ 给「换项目」，那边列出能去的项目。
            from .fleet.actions import do_button

            buttons = [do_button("resume", primary=True)] if paused else []
            if self._talking_to(scope) is not None:
                buttons.append(do_button("quit"))
            buttons += [do_button("dirs"), do_button("mode"), do_button("model"),
                        do_button("sessions")]
            # 🔴 诊断入口要挂在**连接不正常**的时候。`/status` 只会说
            # 「🔴 和飞书的连接断了」—— 同事看到这句之后无事可做，而这正是
            # 他需要把一张卡转给 IT 的那一刻。连接好的时候不放，免得这张卡越堆越长。
            if not connected:
                buttons.append(do_button("diag"))
            self._say_with_buttons(chat_id, "\n".join(lines), buttons)
            return True
        if command in DIAG_COMMANDS or stripped in DIAG_WORDS:
            # 和 `/status` 分开是**有意的**：`/status` 回答"我现在能干什么"（给同事自己看），
            # 诊断卡回答"这台机器出了什么毛病"（给你和 IT 看，同事转发过来）。
            # 混成一张的话，要么同事那张多了看不懂的计数，要么你那张少了判故障的数字。
            # 🔴 第 15 轮：先回一段**人话**（我现在好不好、哪一版、你下一步做什么），
            # 再发给 IT 看的那张数字卡。原来只有后者 —— 同事看不懂，只会原样转发，
            # 而 2026-11-06 一整天卡住的那个问题「你手上是哪一版」，那张卡第一行有、但没人会看。
            try:
                from . import selfcheck

                self._say(chat_id, selfcheck.summary(self._selfcheck_ctx())["text"])
            except Exception:  # noqa: BLE001 —— 人话那段出错不能让 IT 那张卡也发不出去
                logger.exception("自检人话那段没组出来")
            card = status_mod.build_health_card(self._collect_health(scope))
            self._send_card_or_text(
                chat_id, card,
                "\n".join(status_mod.health_lines(self._collect_health(scope)))
                or "读不出状态。过一分钟再发一次「诊断」。",
            )
            return True
        if command in {"/update", "/upgrade"} or stripped in UPDATE_WORDS:
            # 第 14 轮：他不用等那 10 分钟一次的检查。阻塞的活（查网、下包、跑安装包）丢线程里，
            # 这里先回一句 —— 不然他看着没反应，而安装包最长要跑几分钟。
            self._say(chat_id, "好，去看看有没有新版…")
            threading.Thread(target=self._update_on_demand, args=(chat_id,),
                             daemon=True, name="update-now").start()
            return True
        if command == "/new":
            self.sessions.reset(scope)
            if self._paused.pop(scope, None) is not None:
                self._save_paused()
            # 🔴 必须清掉待批准和已批准记录。2026-11-03 实测：批准一次 npm publish
            # 后发 /new，新会话里同样的命令被**直接放行**（钩子只看 .ok 在不在），
            # 等于一次点击换来一张跨会话的长期通行证。
            self._awaiting.pop(scope, None)
            self.gate.clear(scope)
            self._say(chat_id, "好，新话题。前面聊的不带了。")
            self._send_menu(chat_id, scope)
            return True
        if command in {"/find", "/搜会话", "/找会话"} or text.startswith(("找会话 ", "搜会话 ")):
            query = text.split(None, 1)[1] if len(text.split(None, 1)) > 1 else ""
            await self._fleet_search(scope, chat_id, query)
            return True
        if command in {"/fork", "/复制会话", "/archive", "/归档", "/rename", "/重命名"} \
                or text.strip() in ("复制会话", "归档会话"):
            await self._session_manage(scope, chat_id, command, text)
            return True
        if command in {"/stop", "/s"}:
            event = self._cancels.get(scope)
            # 🔴 「停」要停全部 —— 包括**停在等审批**的那一步。
            # 原来只置位取消事件，待批准的卡片还留着：用户说了停，
            # 过一会儿手滑点到那张卡片，那个操作照样执行了（实测会写下 .ok）。
            had_pending = self._clear_approvals_for_stop(scope)
            # 🔴 手机正在往电脑上某个会话发的那条也算「正在跑的」。
            # 2026-11-04 体验审查：原来 /stop 只看桥自己的那一轮 ⇒ 回「现在没有在跑的任务」，
            # 而远端那条一直跑到超时（最长 15 分钟）。屏幕上那张详情卡是发之前画的，也没有停下按钮。
            #
            # 🔴 **对话模式里的 /stop 多半停不了，而且必须照实说。** 目标是用户在电脑终端里
            # 开着的会话，从外面发 SIGINT 会把**整个会话**关掉（不是停这一轮，实测），
            # 所以桥一个信号都不发给它 —— `relay_stop` 只会给出「到电脑上按 Esc」那句话。
            # 不许假装能停：用户以为停了、继续等，而那边还在跑。
            fleet_target = (getattr(self, "_fleet_inflight", {}).get(scope)
                            or self._talking_to(scope))
            if fleet_target is not None and getattr(self, "fleet", None) is not None:
                tool, sid = fleet_target
                res = await asyncio.to_thread(self.fleet.stop, tool, sid, relay=True)
                lines = [("⏹ " if res.ok else "") + res.text]
                # 🔴 **本地那一轮也要停**。2026-11-04 审查：原来远端这条 return 掉了，
                # 用户先让桥自己跑一件慢活、再进对话模式，然后发 /stop —— 只回「到电脑上按 Esc」，
                # 而真正能停的本地那一轮一路跑到底（最长 15 分钟）。
                if event is not None and not event.is_set():
                    event.set()
                    lines.append("手机这边自己跑的那一轮也停了。")
                from .fleet.actions import do_button

                buttons = []
                if not res.ok:
                    # 停不了 ⇒ 给出**能做到**的替代：退出对话模式（点按钮，不用打 /cancel）
                    lines.append("要退出对话模式（不再把你说的话发过去）就点「退出对话」。")
                    buttons.append(do_button("quit", primary=True))
                buttons.append(do_button("sessions", "🖥 回到总览"))
                self._say_with_buttons(chat_id, "\n".join(lines), buttons)
                return True
            if event is not None and not event.is_set():
                event.set()
                self._say(chat_id, "好，正在停…")
            elif str(scope).startswith("dm:") and await self._offer_stop_choices(chat_id):
                pass
            elif had_pending:
                self._say(chat_id, "停了。刚才等你确认的那一步也取消了，卡片按钮已失效。")
            else:
                self._say(chat_id, "现在没有在跑的任务，直接说要干什么就行。")
            return True
        if command == "/resume":
            # 解除掐停必须是人**明确**发的一条指令 —— 不能自动恢复，
            # 否则"能掐停"就没意义了。
            if self._paused.pop(scope, None):
                self._save_paused()
                self._say(chat_id, "好，继续。")
            else:
                self._say(chat_id, "没有被暂停，直接说要干什么就行。")
            return True

        # 🔴 打错的斜杠命令在这里收口，别往下漏给 claude。
        # 2026-11-05 实测的缺口：`/sessoins` 这种会被当成普通消息发给 claude ⇒
        # 花钱、慢、答非所问。判据只看第一个 token，并且刻意放过带两层斜杠的东西
        # （`/Users/me/codes 里有什么` 是个正常问题，不是打错的命令）—— 见 menu.unknown_command。
        from .menu import unknown_command_reply

        guessed = unknown_command_reply(text)
        if guessed is not None:
            hint, buttons = guessed
            self._say_with_buttons(chat_id, hint, buttons)
            return True
        return False

    # ---------- 会话总控（bridge/fleet） ----------

    def _hook_path(self) -> str:
        return os.path.join(os.path.dirname(self.gate.root), "hooks", "approval_hook.py")

    def _fleet_hook(self) -> dict:
        """审批钩子那段 settings，给总控的 `claude --settings` 用。钩子脚本还没装 ⇒ 空（只读档才发）。"""
        from .approval import hook_settings

        try:
            path = self._hook_path()
        except Exception:  # noqa: BLE001
            return {}
        return hook_settings(path) if os.path.isfile(path) else {}

    def _selfcheck_ctx(self) -> dict:
        """给 `bridge/selfcheck.py` 攒现场（那个模块自己不读任何全局状态，见它的文件头）。

        🔴 每一项都单独兜住：这个函数在「东西已经坏了」的时候被调，
        探不出来的那一项留 None（selfcheck 会照实说「探不出」，不会说成「好的」）。
        `latest_version` 要查网 ⇒ 只在发「诊断」时查一次，失败就是 None。
        """
        ctx: dict = {}

        def put(key, fn):
            try:
                ctx[key] = fn()
            except Exception:  # noqa: BLE001
                ctx[key] = None

        updater = getattr(self, "updater", None)
        put("version", lambda: updater.current_build() if updater else None)

        def _latest():
            from . import selfupdate

            info = selfupdate.latest()
            return info["build_id"] if info else None

        put("latest_version", _latest)
        put("connected", self.connection_alive)
        put("offline_sec", lambda: None if self.connection_alive() else self.silent_for)
        put("uptime_sec", lambda: time.monotonic() - self._started_at)
        put("owner_bound", lambda: bool(self.settings.owner_open_ids))
        heal = getattr(self, "menu_heal", None)
        put("menu_ready", lambda: bool(getattr(heal, "_done", False)) if heal else None)
        put("mode", lambda: self.policy.mode)
        put("ceiling_mode", lambda: self.policy.ceiling_mode)
        return ctx

    def _collect_health(self, scope: str) -> dict:
        """诊断卡的数据。**只产出 `status.HEALTH_FIELDS` 里那几个键**。

        🔴 每一项单独 try —— 这个方法恰恰在"东西已经坏了"的时候跑。
        一项探不出来就少一行（`health_lines` 会跳过），绝不能让整张卡发不出去：
        那时候同事手上就又只剩"它没反应"这一句话了。

        🔴 这里是**白名单的入口端**：下面每个 `data[...]` 都是显式写出来的字段，
        没有任何一处是"把某个对象 dump 进去"。日志**只数不摘**（见
        `status.ERROR_CLASSES` 的红字：本机日志里真带着 ticket 和绝对路径）。
        """
        data: dict = {}

        try:
            # 缓存：指纹要读一遍 bridge/**.py，而它在进程生命期内不会变
            # （安装器覆盖了源码也要重启才生效）。
            fingerprint = getattr(self, "_code_fingerprint", None)
            if fingerprint is None:
                fingerprint = status_mod.code_fingerprint(os.path.dirname(__file__))
                self._code_fingerprint = fingerprint
            # 🔴 2026-11-06：版本号（BUILD_ID）放前面。指纹只能答「两台是不是同一份码」，
            # 答不出「哪台新哪台旧」—— 远程支持第一句要问的恰恰是后者。安装包早就打了版本号，
            # 诊断卡原来没读它。`version_label` 只**读**那个文件、不 import（见 status.build_id）。
            label = status_mod.version_label(os.path.dirname(__file__), fingerprint or "")
            if label:
                data["version"] = label
        except Exception:  # noqa: BLE001
            pass

        try:
            import platform as _platform

            # 只给系统大类 + 大版本。完整 `platform.platform()` 带机器名/内核串，
            # 那是能认出具体是谁那台电脑的信息。
            data["platform"] = f"{_platform.system()} {_platform.release().split('.')[0]}"
        except Exception:  # noqa: BLE001
            pass

        try:
            started = getattr(self, "_started_at", None)
            if started is not None:
                data["uptime"] = _fmt_elapsed(int(time.monotonic() - started))
        except Exception:  # noqa: BLE001
            pass

        try:
            # 和 `/status` 同一个判据（连接对象在不在，不是"多久没人说话"）。
            # 🔴 第 15 轮：「正在重连」改用 SDK 自己的回调（`reconnect_state`）。
            # 原来按「静默不到 120 秒」猜，而 SDK 首次重连前要随机等 0~30 秒、
            # 之后每 120 秒一次（实测断到连上 634s / 1155s / 1291s）⇒ 真在重连的那几分钟
            # 全被显示成「🔴 断了」，而刚断的 2 分钟里就算 SDK 根本没在重连也显示「正在重连」。
            retry = self.reconnect_state()
            if self.connection_alive():
                data["connection"] = "🟢 连着飞书"
            elif retry:
                data["connection"] = f"🟡 {retry}"
            else:
                data["connection"] = f"🔴 断了 {_fmt_elapsed(int(self.silent_for))}"
            # 断过几次：平时不显示，断过才出现（「低流量 0 故障」不等于没问题）
            if self.reconnect_total() > 0:
                data["reconnects"] = f"{self.reconnect_total()} 次"
        except Exception:  # noqa: BLE001
            pass

        try:
            policy = self.policy
            data["mode"] = MODE_LABELS.get(policy.mode, policy.mode)
            data["model"] = MODEL_LABELS.get(policy.model, policy.model)
            # **个数**，不是路径 —— 路径带同事真名（`/Users/<真名>/…`）。
            data["workspaces"] = len(getattr(policy, "workspaces", ()) or ())
        except Exception:  # noqa: BLE001
            pass

        try:
            heal = getattr(self, "menu_heal", None)
            if heal is not None:
                data["menu"] = heal.health_text()
        except Exception:  # noqa: BLE001
            pass

        try:
            data.update(self._scan_log_health())
        except Exception:  # noqa: BLE001
            logger.debug("读日志做诊断失败", exc_info=True)

        return data

    def _scan_log_health(self) -> dict:
        """从日志里数出错误分类、重启次数、最后有动静多久。**不带任何原文行。**

        只读尾部 `_DIAG_LOG_TAIL_BYTES` —— 日志会长到几十 MB，而诊断要在
        飞书那 3 秒回调窗口里跑完。
        """
        out: dict = {}
        path = os.path.join(
            os.path.dirname(self.gate.root), "logs", "agent.log"
        )
        if not os.path.exists(path):
            return out
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as handle:
                if size > _DIAG_LOG_TAIL_BYTES:
                    handle.seek(size - _DIAG_LOG_TAIL_BYTES)
                    handle.readline()  # 丢掉被切半的那行
                raw = handle.read()
        except OSError:
            return out
        lines = raw.decode("utf-8", errors="replace").splitlines()

        counts = status_mod.classify_log_errors(lines)
        formatted = status_mod.format_error_counts(counts)
        if formatted:
            out["error_counts"] = formatted
            # "最后一类错"比"最多的那类"有用：用户是**刚刚**发现它坏的。
            for line in reversed(lines):
                last = status_mod.classify_log_errors([line])
                if last:
                    out["last_error"] = next(iter(last))
                    break

        # 启动次数 = 崩溃循环的判据（自检脚本第 2 项用 launchctl 的 runs 看同一件事）。
        restarts = sum(1 for line in lines if "桥已启动" in line)
        if restarts:
            out["restarts"] = restarts

        # 日志最后一行的时间：进程活着但再也收不到消息（lark-oapi #169）时，
        # 只有这个数会露馅 —— 自检脚本第 10 项是同一个判据。
        stamp = ""
        for line in reversed(lines):
            match = _DIAG_TS_RE.search(line)
            if match:
                stamp = match.group(0)
                break
        if stamp:
            try:
                from datetime import datetime

                then = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                age = int((datetime.now() - then).total_seconds())
                out["log_age"] = _fmt_elapsed(age) + "前" if age > 0 else "刚刚"
            except ValueError:
                pass
        # 🔴 第 15 轮：有人跟这台电脑说话但不是主人（见 `_note_stranger_knock`）。
        # 只在**真有人敲过**时出现这一行：平时不出现 ⇒ 出现就是信号。
        # 只给个数和多久以前，不给是谁（open_id 在「永不进卡」那张表里）。
        try:
            knock = self.stranger_knocks()
            if knock["count"] > 0:
                ago = int(time.time() - knock["last_at"]) if knock["last_at"] else 0
                out["knocks"] = (f"{knock['count']} 次（{knock['people']} 个人，"
                                 f"最近 {_fmt_elapsed(ago)}前）")
        except Exception:  # noqa: BLE001 —— 诊断卡少一行也不能让它发不出去
            logger.debug("读敲门计数失败", exc_info=True)
        return out

    def _send_card_or_text(self, chat_id: str, card: str, fallback: str) -> None:
        """发卡片；发不出去（含抛异常）就退成文字 —— 不能让用户点了按钮什么都收不到。"""
        sender = getattr(self.replier, "send_card", None)
        try:
            sent = sender is not None and sender(chat_id, card)
        except Exception:  # noqa: BLE001 —— 飞书接口超时 / 网络抖动
            logger.exception("卡片发送抛异常，退成文字")
            sent = False
        if not sent:
            self._say(chat_id, fallback)

    async def _fleet_overview(self, chat_id: str) -> None:
        from .fleet.cards import build_overview_card, row_label

        control = getattr(self, "fleet", None)
        if control is None:
            self._say(chat_id, "会话总控这次没起来，看不了电脑上的会话。重启一下服务再试。")
            return
        # 🔴 第 11 轮（复审第 6 条）：走**共用扫描**。后台「等你」那一拍正扫着时，原来这里撞上
        # `Fleet._busy` ⇒ 这一家被跳过 ⇒ 卡上写「电脑上现在没有开着的会话」而实际有一个在等你。
        # 现在撞上就等那一次扫完、拿同一份结果（`notify.SharedScan`）。
        scanner = self._fleet_scanner()
        view = await asyncio.to_thread(scanner.overview)
        from .fleet.cards import _cleaned
        from .fleet.notify import fill_unread, unread_tools

        # 真「慢」（超时 / 上一次还卡着）等也等不来 ⇒ 那一家用上次读到的补上、照实注明；
        # 一次都没读到过 ⇒ 说不知道。底线：有会话的时候不许说「没有」。
        unread = unread_tools(view, control)
        view = fill_unread(view, control, scanner.read_at)
        # 标题先整段脱敏再截断（同 cards 的红字）
        # 🔴 文字兜底也要带上「它在问你…」（第 17 轮角色 E 指出）：卡片发不出去时走这条路，
        # 而那恰恰是最需要信息的时候。详情卡那边早有 `_pending_fallback` 做同一件事，总览漏了。
        # 用和卡片**同一个**函数（`cards.overview_pending_line`），不另拼一份 —— 两份会漂。
        from .fleet.cards import overview_pending_line

        lines = []
        for session in view.sessions[:12]:
            cleaned = _cleaned(session, _redact_text)
            lines.append(row_label(cleaned))
            asking = overview_pending_line(cleaned, _redact_text)
            if asking:
                lines.append("  " + asking)
        lines = lines \
            or [self._unread_headline(view, unread) if unread else "一个会话都没读到。"]
        fallback = "🖥 电脑上的会话\n" + "\n".join("· " + line for line in lines)
        if view.notes:
            fallback += "\n" + "\n".join("⚠️ " + _redact_text(n) for n in view.notes)
        card = self._honest_overview_card(
            build_overview_card(view, redact=_redact_text, row_button=self._overview_row_button(chat_id)),
            view, unread)
        if card:
            self._send_card_or_text(chat_id, card, fallback)
        else:
            self._say(chat_id, fallback)

    def _fleet_scanner(self):
        """后台「等你」推送、主人点「会话」、`/status` 共用的那一个扫描入口（`notify.SharedScan`）。

        总控换了（测试里会换）就重建一个。总控没起来 ⇒ None。
        """
        control = getattr(self, "fleet", None)
        if control is None:
            return None
        with _SCANNER_LOCK:
            scanner = getattr(self, "_shared_scan", None)
            if scanner is None or scanner.control is not control:
                from .fleet.notify import SharedScan

                scanner = SharedScan(control)
                self._shared_scan = scanner
        return scanner

    @staticmethod
    def _unread_headline(view, unread: list) -> str:
        """有一家这次没读出来、又一个开着的都没有 ⇒ 第一句照实说「不知道」，不说「没有」。"""
        from .fleet.scan import TOOL_LABELS

        names = "、".join(TOOL_LABELS.get(t, t) for t in unread)
        if getattr(view, "ok_tools", None):
            return f"读到的那几家现在没有开着的会话；{names} 这次没读出来，不知道它那边有没有。"
        return f"{names} 这次没读出来，不知道电脑上有没有开着的会话。"

    def _honest_overview_card(self, card: str, view, unread: list) -> str:
        """总览卡：有一家没读出来时，把「电脑上现在没有开着的会话」「一个会话都没读到」换成照实的那句。

        卡片的样子归 `fleet/cards.py`，这里只在**没读全**的时候改第一句、去掉「一个都没读到」那行；
        读全了（三家都按时读完）原样返回 —— 那时说「没有」就是真没有。
        """
        from .fleet.cards import overview_headline

        if not unread or not card:
            return card
        none_line = overview_headline([])
        if overview_headline(getattr(view, "sessions", None) or []) != none_line:
            return card  # 有开着的：第一句本来就不说「没有」
        honest = self._unread_headline(view, unread)
        try:
            data = json.loads(card)
            elements = data["body"]["elements"]
            kept = []
            for el in elements:
                content = el.get("content") if isinstance(el, dict) and el.get("tag") == "markdown" else None
                if isinstance(content, str) and content.strip() == "一个会话都没读到。":
                    continue
                if isinstance(content, str) and content.startswith(none_line):
                    el = dict(el, content=honest + content[len(none_line):])
                kept.append(el)
            data["body"]["elements"] = kept
            return json.dumps(data, ensure_ascii=False)
        except Exception:  # noqa: BLE001 —— 改不动就退成文字版（`_send_card_or_text` 那条路照实）
            logger.exception("总览卡没改成照实的说法，退成文字")
            return ""

    @staticmethod
    def _fleet_status_line(scanner, control) -> str:
        """`/status` 里那一行「电脑上在跑 N 个会话」。读不出来 ⇒ 空串（不写这一行）。"""
        from .fleet.notify import fill_unread, unread_tools

        try:
            view = scanner.overview()
            unread = unread_tools(view, control)
            count = fill_unread(view, control, scanner.read_at).running_count()
        except Exception:  # noqa: BLE001 —— 只是状态卡上的一行
            logger.exception("状态卡：扫描失败")
            return ""
        if unread and not count:
            return "🖥 电脑上的会话这次没读全，点「看看会话」再看一眼"
        return f"🖥 电脑上在跑 {count} 个会话" + ("（有一家这次没读出来，可能不止）" if unread else "")

    async def _fleet_detail(self, chat_id: str, tool: str, sid: str, scope: str = "") -> None:
        from .fleet.cards import build_detail_card, talking_banner

        control = self.fleet
        sess, messages, note = await asyncio.to_thread(control.detail, tool, sid)
        if sess is None:
            from .fleet.actions import do_button

            self._say_with_buttons(chat_id, note or "这个会话找不到了，点「回到总览」重新看一眼。",
                                   [do_button("sessions", "🖥 回到总览")])
            return
        # 正在和这个会话说话 ⇒ 卡片上要一直挂着指示器（spec：对话模式中不能看不出在跟谁说）
        talking = self._talking_to(scope) == (tool, sid) if scope else False
        # 先脱敏整段再截断（同 cards.build_detail_card 的红字）
        fallback = f"{_redact_text(sess.title or sess.id)}\n" + "\n".join(
            f"{m.role}: {_redact_text(m.text)[:200]}" for m in messages[-4:]
        )
        # 卡片发不出去时的文字版也要说清「它在等什么」（第 8 轮：卡片那边已有这一段）。
        # 先脱敏整段再截断（同 cards.pending_elements 的规矩）。
        fallback += _pending_fallback(sess)
        if talking:
            fallback += "\n" + talking_banner(sess, _redact_text)
        elif not (sess.can_send or sess.can_relay):
            fallback += f"\n💬 {sess.why_not_relay or sess.why_not_send}"
        self._send_card_or_text(
            chat_id,
            build_detail_card(sess, messages, redact=_redact_text, note=note, talking=talking,
                              group_link=self._session_group_link(scope, sess)),
            fallback,
        )

    def _overview_row_button(self, chat_id: str):
        """总览卡上每一行的按钮（产品定义 §5.7，FC-03）：能接着聊的会话 ⇒ 点一下就进它的群。

        - 已经有群 ⇒ 一个按钮同时跳进群 + 回调 `go`（后台核对群还在、不在就重建）
        - 还没有群 ⇒ 回调 `send`，单聊里点就建群（`ensure_group` 按会话串行，连点两下也只建一个）
        - 不能接着聊的会话 ⇒ None，照旧点开看详情
        只在单聊里这样做：群里的总览点一行是「改绑这个群」，跳去别的群就错了。建群能力没起来 ⇒ None。
        """
        binds = getattr(self, "chat_binds", None)
        client = getattr(self, "group_client", None)
        if binds is None or client is None or not chat_id:
            return None
        known = getattr(self, "_chat_ids", {}) or {}
        if any(c == chat_id and str(sc).startswith("group:") for sc, c in known.items()):
            return None
        try:
            if binds.of_chat(chat_id) is not None:
                return None
        except Exception:  # noqa: BLE001
            return None
        from .fleet.actions import _button, jump_callback_button, safe_link
        from .fleet.cards import fleet_value

        opener = getattr(client, "open_link", None)

        def make(sess, label: str, urgent: bool):
            if not (getattr(sess, "can_send", False) or getattr(sess, "can_relay", False)):
                return None
            bound = binds.of_session(sess.tool, sess.id)
            if bound is not None and opener is not None:
                link = safe_link(opener(bound.chat_id))
                if link:
                    return jump_callback_button(label, link, fleet_value("go", sess.tool, sess.id),
                                                primary=urgent)
            return _button(label, fleet_value("send", sess.tool, sess.id), urgent)

        return make

    def _session_group_link(self, scope: str, sess) -> str:
        """这个会话**已经有群**时，那个群的直开链接；否则空串（详情卡就用只回调的老按钮）。

        第 14 轮（用户原话：「用户点击接着聊，直接进群 … 而不是再弹出一个进群聊，让用户再点击一下」）。
        有链接 ⇒ 详情卡的「接着聊」一个按钮同时跳进群 + 回调（`actions.jump_callback_button`）⇒ 一下进群。

        几个只给空串的情况：
        - 不是单聊：群里点「接着聊」的意思是「把这个群改绑到它」，跳到别的群就错了
        - 这个会话不能接着聊：卡上本来就没这个按钮；🔴 也**不建群**（这里从不建群）
        - 没群：只看一眼不预先建群（会平白多一个群）⇒ 首次还是「接着聊 → 进群说话」两下
        - 建群能力没起来（`group_client` / `chat_binds` 是 None）：点了走单聊老路，跳过去反而不对

        只读绑定表 + 拼字符串（`GroupSetup.open_link`），**不调飞书接口**：详情卡这一下不多一次网络往返。
        群被主人解散了的话链接会打不开，同时那一下回调照样走 `_open_session_group` ⇒ 重建并给新群的按钮。
        """
        if not str(scope or "").startswith("dm:") or sess is None:
            return ""
        if not (getattr(sess, "can_send", False) or getattr(sess, "can_relay", False)):
            return ""
        binds = getattr(self, "chat_binds", None)
        client = getattr(self, "group_client", None)
        opener = getattr(client, "open_link", None)
        if binds is None or client is None or opener is None:
            return ""
        try:
            bound = binds.of_session(sess.tool, sess.id)
            if bound is None:
                return ""
            from .fleet.actions import safe_link

            return safe_link(opener(bound.chat_id))
        except Exception:  # noqa: BLE001 —— 只是少一下点击，不能让详情卡发不出来
            logger.exception("拼会话群的直开链接失败 tool=%s", getattr(sess, "tool", ""))
            return ""

    def _talking_to(self, scope: str) -> tuple | None:
        """这个聊天现在在和哪个会话说话 → (tool, id)；没有就 None。

        指示器和 `/stop` 都靠它。用 `peek_pending`（不取走）—— 取走会把下一句话的目标弄没。
        """
        control = getattr(self, "fleet", None)
        if control is None:
            return None
        target = control.peek_pending(scope)
        return (target.tool, target.id) if target is not None else None

    async def _session_manage(self, scope: str, chat_id: str, command: str, text: str) -> None:
        """会话群里的会话管理（§4「会话管理」）：复制（fork）、归档、重命名。"""
        bound = None
        binds = getattr(self, "chat_binds", None)
        if binds is not None and str(scope).startswith("group:"):
            try:
                bound = binds.of_chat(chat_id)
            except Exception:  # noqa: BLE001
                bound = None
        if bound is None or bound.tool not in ("claude", "codex"):
            self._say(chat_id, "这个命令要在会话群里用：复制会话 /fork、归档 /archive、重命名 /rename 新名字。")
            return
        word = text.strip()
        if command in ("/fork", "/复制会话") or word == "复制会话":
            await self._session_fork(scope, chat_id, bound)
        elif command in ("/archive", "/归档") or word == "归档会话":
            await self._session_archive(chat_id, bound)
        else:
            name = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""
            await asyncio.to_thread(self._session_rename, chat_id, bound, name)

    async def _session_fork(self, scope: str, chat_id: str, bound) -> None:
        cwd = ""
        sess = self.fleet.cached(bound.tool, bound.id) if getattr(self, "fleet", None) is not None else None
        cwd = getattr(sess, "cwd", "") if sess is not None else ""
        cwd = cwd or getattr(self, "_new_session_cwd", {}).get(bound.id, "")
        if not cwd or not self.policy.resolve_workspace(cwd):
            self._say(chat_id, "读不出这个会话在哪个项目里，没法复制。点状态卡上的刷新再试。")
            return
        choice = self._effective_model(bound.tool, bound.id)
        dm_scope = self._owner_dm_scope()
        target_scope = dm_scope or scope
        self._newsession_state().arm(target_scope, bound.tool, cwd, choice.model or "", choice.effort or "",
                                     fork_from=bound.id)
        logger.info("会话管理 复制 tool=%s 会话=%s", bound.tool, bound.id[:8])
        where = "私聊里" if dm_scope else "这里"
        self._say(chat_id, f"🧬 好，复制一份「{_redact_text(bound.name or bound.id[:8])}」：新会话带着它的全部上下文，"
                           f"另开一个群。到{where}说新会话的第一句（10 分钟内有效），原会话不受影响。")
        if dm_scope:
            dm_chat = getattr(self, "_chat_ids", {}).get(dm_scope, "")
            if dm_chat:
                self._say(dm_chat, f"🧬 下一句话会作为「{_redact_text(bound.name or bound.id[:8])}」复制出来的新会话的第一句。")

    def _owner_dm_scope(self) -> str:
        owners = sorted(getattr(getattr(self, "settings", None), "owner_open_ids", None) or ())
        known = getattr(self, "_chat_ids", {}) or {}
        for owner in owners:
            scope = f"dm:{owner}"
            if scope in known:
                return scope
        return ""

    async def _session_archive(self, chat_id: str, bound) -> None:
        control = getattr(self, "fleet", None)
        res = await asyncio.to_thread(control.archive, bound.tool, bound.id) if control is not None else None
        if res is not None and not res.ok and res.text:
            self._say(chat_id, f"❌ {res.text}")
            return
        try:
            self._fleet_settled_store().settle(bound.tool, bound.id)
        except Exception:  # noqa: BLE001
            logger.exception("归档后记结束失败")
        await asyncio.to_thread(self._mark_session_done, chat_id, True)
        tail = res.text if res is not None and res.ok else "Claude 没有归档命令：总览里不再列它，在群里再说一句就会重新出现。"
        logger.info("会话管理 归档 tool=%s 会话=%s 工具归档=%s", bound.tool, bound.id[:8], bool(res and res.ok))
        self._say(chat_id, f"🗄 已归档这个会话，群名加了 ✅。{tail}群保留着，聊天记录都在。")

    def _session_rename(self, chat_id: str, bound, name: str) -> None:
        from .fleet.chatbind import GROUP_PREFIX_SN, MAX_NAME_CHARS, _clean_name
        from .fleet.model import short_title
        from .fleet.scan import TOOL_LABELS

        clean = _clean_name(_redact_text(name))
        if not clean:
            self._say(chat_id, "要带新名字，比如：/rename 修登录 bug")
            return
        want = short_title("·".join([GROUP_PREFIX_SN, TOOL_LABELS.get(bound.tool, bound.tool), clean]), MAX_NAME_CHARS)
        client = getattr(self, "group_client", None)
        setter = getattr(client, "rename", None)
        if setter is None:
            self._say(chat_id, "这台电脑上改不了群名。")
            return
        ok, code, _msg = setter(chat_id, want)
        if not ok:
            self._say(chat_id, f"没改成群名（code={code}）。")
            return
        self.chat_binds.set_name(chat_id, want)
        self._refresh_status_card(chat_id, force=True)
        logger.info("会话管理 重命名 tool=%s 会话=%s", bound.tool, bound.id[:8])
        self._say(chat_id, f"✏️ 群名改成「{want}」。会话本身的标题在 Claude / Codex 里不变，这里只改飞书这边的名字。")

    async def _fleet_search(self, scope: str, chat_id: str, query: str) -> None:
        """`/find 关键字`：按标题 / 项目 / 编号找会话，点一下进它的群（没有群就建）。"""
        from .fleet.actions import do_button, fleet_button
        from .fleet.cards import session_label
        from .workspace import short_name

        control = getattr(self, "fleet", None)
        if control is None:
            self._say(chat_id, "会话总控这次没起来，重启一下服务再试。")
            return
        if not query.strip():
            self._say(chat_id, "要带关键字，比如：/find 登录 或 /find remote_control")
            return
        scanner = self._fleet_scanner()
        try:
            await asyncio.to_thread(scanner.overview if scanner is not None else control.overview)
        except Exception:  # noqa: BLE001 —— 扫不出来就按上次的找
            logger.debug("搜索会话 扫描失败", exc_info=True)
        hits = control.search(query)
        logger.info("搜索会话 词数=%d 命中=%d", len(query.split()), len(hits))
        if not hits:
            self._say_with_buttons(chat_id, f"没找到和「{_redact_text(query)[:30]}」有关的会话。换个词，或者看看总览。",
                                   [do_button("sessions", "🖥 看看会话")])
            return
        lines = [f"找到 {len(hits)} 个会话，点一下进它的群（还没有群就建一个）："]
        buttons = []
        for sess in hits:
            label = session_label(sess, _redact_text)
            proj = _redact_text(short_name(getattr(sess, "cwd", "") or "", self.policy)) if getattr(sess, "cwd", "") else ""
            lines.append(f"- {label}" + (f" · {proj}" if proj else ""))
            buttons.append(fleet_button("send", sess.tool, sess.id, f"💬 {label}"[:28]))
        buttons.append(do_button("sessions", "🖥 回到总览"))
        self._say_with_buttons(chat_id, "\n".join(lines), buttons)

    async def _offer_stop_choices(self, chat_id: str) -> bool:
        """私聊「停下」，手边没有在跑的 ⇒ 列出电脑上正在跑的会话，选一个停（FC-11/12）。没有 ⇒ False。

        能停的（手机发起的那一轮）给「⏹ 停下」；停不了的（终端 / App 里跑的）照实说原因，
        给「👀 看看它」，不发任何信号 —— 从外面打断会把整个终端会话关掉。
        """
        control = getattr(self, "fleet", None)
        if control is None:
            return False
        try:
            await asyncio.to_thread(control.overview)
        except Exception:  # noqa: BLE001 —— 扫不出来就按上次扫描的判
            logger.debug("停下 扫描失败，用上次扫描", exc_info=True)
        running = control.running()[:6]
        if not running:
            return False
        from .fleet.actions import do_button, fleet_button
        from .fleet.cards import session_label

        lines = ["电脑上有这些会话正在跑，点要停的那个："]
        buttons = []
        for sess in running:
            label = session_label(sess, _redact_text)
            if getattr(sess, "can_stop", False):
                lines.append(f"- {label}：手机发起的这一轮，可以直接停，会话保留")
                buttons.append(fleet_button("stop", sess.tool, sess.id, f"⏹ 停 {label}"[:28]))
            else:
                why = getattr(sess, "why_not_stop", "") or "它在电脑上跑，这里停不了"
                lines.append(f"- {label}：{_redact_text(why)}")
                buttons.append(fleet_button("remind", sess.tool, sess.id, f"⏰ 提醒我到电脑上停 {label}"[:30]))
                buttons.append(fleet_button("open", sess.tool, sess.id, f"👀 看看 {label}"[:28]))
        buttons.append(do_button("sessions", "🖥 回到总览"))
        logger.info("停下 私聊列出在跑的会话 数量=%d 能停=%d", len(running),
                    sum(1 for s in running if getattr(s, "can_stop", False)))
        self._say_with_buttons(chat_id, "\n".join(lines), buttons)
        return True

    async def _remind_stop_later(self, chat_id: str, tool: str, sid: str, delay: float = 0.0) -> None:
        """停不了的那一轮（终端 / App 里跑的）：过一会儿还在跑 ⇒ 私聊提醒到电脑上停（FC-12 的替代方法）。"""
        from .fleet.actions import fleet_button
        from .fleet.cards import session_label

        await asyncio.sleep(delay or STOP_REMIND_SEC)
        control = getattr(self, "fleet", None)
        if control is None:
            return
        sess, _why = await asyncio.to_thread(control.refresh, tool, sid)
        if sess is None or getattr(sess, "status", "") != "running":
            logger.info("停下提醒 不用提醒了：已经不在跑 tool=%s 会话=%s", tool, sid[:8])
            return
        label = session_label(sess, _redact_text)
        where = "Codex App" if getattr(sess, "owner", "") == "app" else "电脑上的终端"
        self._say_with_buttons(chat_id, f"⏰ {label}还在跑。要停的话到{where}里停（终端按 Esc，App 点停止）。",
                               [fleet_button("open", tool, sid, "👀 看看它现在")])

    async def _fleet_stop(self, chat_id: str, tool: str, sid: str) -> None:
        result = await asyncio.to_thread(self.fleet.stop, tool, sid)
        self._say(chat_id, ("⏹ " if result.ok else "") + result.text)

    def _fleet_approval_dir(self, tool: str, sid: str, policy) -> str:
        """总控这一次发送的批准目录：按目标会话分，和飞书对话自己的那份分开。

        每次发之前清空：总控这条路弹不了确认卡，留着的 .ok 不该让下一次直接放行。
        """
        gate = getattr(self, "gate", None)
        if gate is None:
            return ""
        scope = f"fleet:{tool}:{sid}"
        keep = getattr(self, "_fleet_keep_grants", set())
        if scope in keep:
            # 批准后续跑：刚写下的 .ok 要留给钩子兑换，不能清
            keep.discard(scope)
        else:
            gate.clear(scope)
        gate.set_trusted(scope, policy.mode == "trusted")
        gate.write_workspaces(scope, policy.workspaces)
        return gate.dir_for(scope)

    async def _maybe_send_to_fleet(self, scope: str, chat_id: str, text: str) -> bool:
        """这个聊天有待发目标 ⇒ 把这条发过去，回复贴回来。没有目标返回 False（走正常流程）。"""
        control = getattr(self, "fleet", None)
        if control is None:
            return False
        # 🔴 **群绑定在 5 分钟待发目标之前**。「一个群 = 一个会话」是**长期**的：
        # 这个群绑了谁查的是落盘的那张表（bridge/fleet/chatbind.py），不是那个会过期的内存态。
        # 这里只把待发目标摆好，下面的管道（在飞 / 暂停 / 审查 / 发 / 贴回复）一行都不用改。
        # 返回 True = 这条已经当场答复了（会话没了、还没确认权限），不往下走。
        if await self._group_bind_arm(scope, chat_id, text):
            return True
        # 🔴 **先看在不在对话模式，不要先把目标取走**。
        # 2026-11-04 三个审查角色都撞到同一个根因：原来第一句话开跑前就 take_pending 把目标
        # 清掉了，于是在飞的那几十秒~15 分钟里：
        #   - 用户补一句 ⇒ 查不到目标 ⇒ **悄悄**落进桥自己的 claude（另一个项目、另一套权限）
        #   - 用户发 /cancel ⇒ 回「没有要取消的」，而回复到了之后对话模式又自己挂回来
        #   - 刷新卡片 ⇒ 看不出在跟谁说话、也看不到怎么退出
        # 现在：peek 看，只有 /cancel、过期、真拒绝才清。
        #
        # 🔴 第 11 轮（复审第 5 条）：**重启前在对话模式**（单聊）⇒ 这一句先问「要发给它吗」，不悄悄落进
        # 桥自己的 claude，也**不自动转发**（重启后确认时的样子、在等的卡全没了，不能凭旧状态发）。
        if self._restart_talk_offer(scope, chat_id, text):
            return True
        target, expired = control.peek_pending_fresh(scope)
        if target is None:
            if expired:
                logger.info("对话模式 过期：目标超过时限，这条按平常处理 scope=%s", scope[:16])
                stale = control.clear_pending(scope)
                # 带一个能直接点的下一步：接着跟它聊（id 不在最近一次扫描里时按钮会提示过期，闸门不变）
                from .fleet.actions import do_button, fleet_button

                again = [do_button("sessions", "🖥 回到总览")]
                if stale is not None:
                    again.insert(0, fleet_button("send", stale.tool, stale.id, "💬 接着跟它聊", primary=True))
                self._say_with_buttons(chat_id, "刚才选的「接着聊」已经过了 5 分钟，这条就不发过去了，"
                                                "按平常的方式处理。要发给它的话点下面的按钮再说一次。", again)
            return False

        # 在飞的判断要**在取目标之后、动手之前**，这样第二句得到的是「上一条还在发」，
        # 而不是被另一个会话执行。
        if not hasattr(self, "_fleet_inflight"):
            self._fleet_inflight = {}
        if scope in self._fleet_inflight:
            busy_tool, busy_id = self._fleet_inflight[scope]
            same = (busy_tool, busy_id) == (target.tool, target.id)
            logger.info("对话模式 拒收：上一条还在发 tool=%s 会话=%s 同一个=%s 字数=%d",
                        target.tool, str(target.id)[:8], same, len(text))
            # 带下一步：不想等就退出 / 停（**能停才放**）。
            # 🔴 2026-11-06 体验侦察 Top5：原来不判断就放「⏹ 停下」，终端会话点了必然是「停不了」。
            # 按**在飞的那个会话**判（/stop 停的就是它），和回复下面那排同一个口径。
            self._say_with_buttons(chat_id, "上一条还在发，等它回来再说。" if same else
                                            "上一条发给别的会话的还在跑，等它回来再说。",
                                   self._talk_buttons(busy_tool, busy_id))
            return True

        # 🔴 2026-11-06 第 9 轮复审 #1：**单聊（和没绑定的群）也要做身份复查**，和群绑定那条路同一个判断
        # （`_talk_ident_same` / `_relay_granted` / `_talk_changed_why`，原来只接在 `_group_bind_arm` 上）。
        # 他确认的是「跟**这个样子**的会话、按**卡上那套权限**说话」：5 分钟里电脑上把它打开 / 关掉 /
        # 改名 / 换项目 ⇒ 下一句原来直接进去（关着时确认、打开后进了 bypassPermissions 的终端会话）。
        # 先现查一次（后台扫描 10 秒一拍，变了还没扫到也要认出来）；读不出来就按上次扫描的判。
        try:
            await asyncio.to_thread(control.refresh, target.tool, target.id)
        except Exception:  # noqa: BLE001 —— 现查失败按上次扫描的判，发之前 adapter 还会再查
            logger.debug("对话模式 发之前现查失败", exc_info=True)
        if not self._talk_ident_same(scope, target.tool, target.id) \
                and not self._relay_granted(scope, target.tool, target.id):
            changed = self._talk_changed_why(scope, target.tool, target.id)
            logger.info("对话模式 拒收：确认时的样子变了 tool=%s 会话=%s 看得出哪变了=%s",
                        target.tool, str(target.id)[:8], bool(changed))
            control.clear_pending(scope)
            if changed:
                self._say(chat_id, f"{changed}，这条先没发。点下面那张卡再确认一次，确认之后再说一遍就行。")
            else:
                self._say(chat_id, f"跟{target.label}说话的那次确认已经不算数了，这条先没发。"
                                   "点下面那张卡再确认一次，确认之后再说一遍就行。")
            self._ask_relay_consent(chat_id, scope, target.tool, target.id)
            return True

        if scope in getattr(self, "_paused", {}):
            logger.info("对话模式 拒收：会话已暂停 tool=%s 会话=%s", target.tool, str(target.id)[:8])
            from .fleet.actions import do_button

            self._say_with_buttons(chat_id, f"⏸ 这个对话已暂停：{self._paused[scope]}\n确认没问题的话点「继续」。",
                                   [do_button("resume", primary=True), do_button("quit")])
            return True
        policy = self.policy
        reviewer = getattr(self, "reviewer", None)
        if reviewer is not None:
            decision = reviewer.review_request(ReviewContext(text=text, source="human", mode=policy.mode))
            if decision.verdict is Verdict.DENY:
                logger.info("对话模式 拒收：审查拦下 tool=%s 会话=%s 字数=%d",
                            target.tool, str(target.id)[:8], len(text))
                self._say(chat_id, f"⛔️ {decision.reason}")
                return True

        inflight = self._fleet_inflight
        logger.info("对话模式 发出 tool=%s 会话=%s 字数=%d", target.tool, str(target.id)[:8], len(text))
        sent_at = time.monotonic()
        inflight[scope] = (target.tool, target.id)
        self._say(chat_id, f"📨 发给{target.label}了，等它回…")
        # 等的这段时间：一张原地刷新的等待卡（体验侦察 Top4，见 _FleetWaitCard）。能停才挂「停下」。
        try:
            known = control.cached(target.tool, target.id)
        except Exception:  # noqa: BLE001
            known = None
        settings = getattr(self, "settings", None)
        wait_card = _FleetWaitCard(
            self.replier, chat_id, target.label,
            can_stop=bool(getattr(known, "can_stop", False)),
            first_delay=getattr(settings, "progress_first_notice_sec", 90),
            escalate_sec=getattr(settings, "progress_escalate_sec", 0),
            limit_sec=float(getattr(policy, "turn_timeout_sec", 600) or 600),
            progress=self._turn_progress(control, target.tool, target.id),
        )
        wait_stop = asyncio.Event()
        wait_task = asyncio.create_task(wait_card.run(wait_stop))
        result = None
        try:
            if str(scope).startswith("group:"):
                keeper = getattr(self, "status_cards", None)
                if keeper is not None and keeper.finished(chat_id):
                    self._in_background(self._mark_session_done, chat_id, False)
            approval_dir = self._fleet_approval_dir(target.tool, target.id, policy)
            # 🔴 发之前 adapter 在锁里**再查一次**有没有人占着（五分钟里终端可能打开了它）。
            # `relay=True`：电脑上正开着的会话走「进会话聊天」那条路（消息进它的输入队列，
            # 不分叉、不打断）；没人占着的还是走 resume（同步拿回复，更快）。选路在 control.send。
            choice = self._effective_model(target.tool, target.id)
            result = await asyncio.to_thread(
                control.send, target.tool, target.id, text,
                policy=policy, approval_dir=approval_dir,
                timeout=float(getattr(policy, "turn_timeout_sec", 600) or 600),
                relay=True, model=choice.model, effort=choice.effort,
            )
        finally:
            inflight.pop(scope, None)
            pusher = getattr(self, "group_pusher", None)
            if pusher is not None and str(scope).startswith("group:"):
                try:
                    pusher.note_relay(target.tool, target.id, time.time() - (time.monotonic() - sent_at))
                except Exception:  # noqa: BLE001
                    logger.debug("记群里发进去的这一轮失败", exc_info=True)
            wait_stop.set()
            try:
                await wait_task
                await wait_card.finish(result)
            except Exception:  # noqa: BLE001 —— 等待卡收尾失败不能吞掉回复
                logger.debug("等待卡收尾失败", exc_info=True)
            if str(scope).startswith("group:"):
                self._refresh_status_card_later(chat_id)
        # 🔴 **接着聊的目标保持住**，下一句还发给它（5 分钟内、每发一次续期）。
        # 2026-11-04 体验审查：原来发一句就清掉目标 ⇒ 用户看完回复自然接一句「那你修一下」，
        # 这句**悄悄**落到了桥自己的 claude、而且在另一个项目里，可改档下可能动错仓库的文件。
        # 现在明说「还在和谁聊」「怎么退出」，退出用 /cancel 或「取消」。
        # 续期有两个前提（都来自 2026-11-04 审查）：
        # 1. 用户在这期间发过 /cancel ⇒ **不续期**，否则「我已经退出了」会被悄悄撤销
        # 2. 这期间他点开了别的会话 ⇒ 不要把目标覆盖回来，否则卡片说在和 B 说话、下一句进了 A
        quit_asked = getattr(self, "_talk_quit", {}).pop(scope, None)
        now_target = control.peek_pending(scope)
        switched = now_target is not None and (now_target.tool, now_target.id) != (target.tool, target.id)
        if quit_asked:
            control.clear_pending(scope)
        elif result.delivered or result.ok:
            if not switched:
                control.set_pending(scope, target.tool, target.id)
        else:
            control.clear_pending(scope)
        # 指示器：对话模式里**每一条**回复后面都挂着「在和谁说话 + 怎么退出」。
        # 原来只有一句「下一句还会发给 X」，用户看完回复往上翻找不到自己在跟谁说。
        from .fleet.cards import TALKING_PREFIX, TALKING_SUFFIX

        outcome = ("回复" if result.ok else "已送达未回完" if result.delivered else "没送达")
        logger.info(
            "对话模式 结果=%s tool=%s 会话=%s 回复字数=%d 耗时=%.1fs 退出=%s 切走=%s",
            outcome, target.tool, str(target.id)[:8], len(result.text or ""),
            time.monotonic() - sent_at, bool(quit_asked), switched,
        )
        # 11-05：「怎么退出」从「发 /cancel」换成回复下面那排按钮（[退出对话][停下][换个会话]）。
        from .fleet.actions import do_button, fleet_button

        if quit_asked:
            tail = "\n\n已经退出对话模式了，这条的回复带回来就结束。"
            buttons = [do_button("sessions", "🖥 回到总览")]
        elif switched:
            tail = f"\n\n注意：你已经切到{now_target.label}了，下一句发给它。"
            buttons = self._talk_buttons(now_target.tool, now_target.id)
        else:
            tail = f"\n\n{TALKING_PREFIX} {target.label} {TALKING_SUFFIX}"
            buttons = self._talk_buttons(target.tool, target.id)
        if result.ok:
            # 回复可能很长 ⇒ 走 replier.send：那里过 redact.py 的脱敏 + 按上限分段。
            # 纯文本 post 带不了按钮 ⇒ 后面跟一张只放按钮的小卡片（长回复不塞进卡片：30KB 上限）。
            # 🔴 先把回复本身的代码块收好口，再拼尾巴。2026-11-05 体验审查实测：回复里
            # 有一个没闭合的 ```，后面拼上的「正在和 X 说话」就被吞进代码块显示成代码 ——
            # 那正是用户判断自己在不在对话模式的唯一依据。
            from .reply import normalize as _md_normalize

            body = _md_normalize(result.text or "")
            await self._send_reply_maybe_doc(chat_id, f"🖥 {target.label}", body, tail)
            self._send_buttons(chat_id, self._choice_buttons(scope, chat_id, target.tool, target.id,
                                                             result.text or "") + buttons)
        elif result.delivered:
            # 消息**已经进了会话**，只是没正常回完（超时 / 被停 / 等确认 / 报错）。
            # 不能说「没发进去」—— 用户会重发，在他真实的会话里重复执行一次。
            look = fleet_button("open", target.tool, target.id, "👀 看看它现在")
            self._say_with_buttons(
                chat_id,
                f"⚠️ 已经发给{target.label}了，但没正常回完：{result.text}\n"
                "它可能已经动手了 —— 先点「看看它现在」看最新情况，别急着重发。" + tail,
                [look] + buttons,
            )
        else:
            # 真没进去 ⇒ 目标已经被清掉了（上面那个 if 没进），说清楚现在不在对话模式了
            # 不在手机能用的目录里（Claude 那家和 control 各有一句，开头一样）⇒ 配「换项目」
            retry = [do_button("sessions", "🖥 回到总览")]
            if (result.text or "").startswith("这个项目不在手机能用的目录里"):
                retry.insert(0, do_button("dirs"))
            self._say_with_buttons(chat_id, f"❌ 没发进去：{result.text}", retry)
        self._fleet_ask_approval(scope, chat_id, target.tool, target.id, f"fleet:{target.tool}:{target.id}")
        return True

    # ---------- 单聊对话模式中重启（第 11 轮，复审第 5 条） ----------
    #
    # 🔴 屏幕上最后一行写着「你发的每一句都进它那边」，重启后 `control._pending`（内存）没了 ⇒
    # 下一句原来**悄悄**进了桥自己的 claude，一句提示都没有。
    # 修法：单聊进 / 出对话模式时落一个很小的标记（scope → 哪个会话 + 到期时间，不记说过的话）；
    # 重启后这个聊天收到的**第一句**，标记还在 ⇒ 先问「这句要发给它吗？」+ [发给它][不用，就问你]。
    # **不自动转发**：重启后确认时的样子（`_talk_idents`）、在等的卡全没了，不能凭旧状态发。
    # 点「发给它」⇒ 现查会话 + 按**现在的样子**查 24 小时确认记录，过了才走正常管道；不过 ⇒ 弹确认卡。

    def _talk_mark_state(self) -> tuple:
        """(这个进程里的标记表, 上一个进程留下、还没处理的那几条)。第一次用时从磁盘读一次。"""
        if getattr(self, "_talk_marks", None) is None:
            loaded: dict = {}
            path = getattr(self, "_talk_mark_path", "")
            if path:
                try:
                    with open(path, encoding="utf-8") as handle:
                        data = json.load(handle)
                    if isinstance(data, dict):
                        for scope, mark in data.items():
                            if (isinstance(scope, str) and scope.startswith("dm:") and isinstance(mark, dict)
                                    and isinstance(mark.get("tool"), str) and isinstance(mark.get("id"), str)
                                    and mark.get("id")):
                                loaded[scope] = mark
                except (OSError, ValueError):
                    loaded = {}
            self._talk_marks = dict(loaded)
            self._talk_restart = dict(loaded)
        return self._talk_marks, self._talk_restart

    def _save_talk_marks(self, marks: dict) -> None:
        """写临时文件再原子改名（同 `_save_paused`）。存不下来 ⇒ 只是重启后少一句提示。"""
        import tempfile

        path = getattr(self, "_talk_mark_path", "")
        if not path:
            return
        try:
            directory = os.path.dirname(path)
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as tmp:
                json.dump(marks, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, path)
        except OSError:
            logger.exception("对话模式标记落盘失败（重启后少一句提示）")

    def _sync_talk_marker(self, scope: str) -> None:
        """按这个单聊**现在**在不在对话模式，更新落盘标记。群不记（群绑定本来就落盘）。"""
        if not str(scope or "").startswith("dm:"):
            return
        control = getattr(self, "fleet", None)
        try:
            target = control.peek_pending(scope) if control is not None else None
        except Exception:  # noqa: BLE001
            target = None
        with _TALK_MARK_LOCK:
            marks, leftover = self._talk_mark_state()
            before = marks.get(scope)
            if target is not None:
                leftover.pop(scope, None)  # 又进了对话模式 ⇒ 重启那件事过去了
                marks[scope] = {"tool": target.tool, "id": target.id, "title": target.title or "",
                                "expires_at": float(target.expires_at)}
            elif scope not in leftover:
                marks.pop(scope, None)
            # 上一个进程留下、还没问过的那条：留在盘上（这个进程又重启一次也还要问）
            if marks.get(scope) != before:
                self._save_talk_marks(dict(marks))

    def _talk_mark_label(self, mark: dict) -> str:
        from .fleet.model import short_title
        from .fleet.scan import TOOL_LABELS

        tool, sid = str(mark.get("tool") or ""), str(mark.get("id") or "")
        control = getattr(self, "fleet", None)
        sess = None
        try:
            sess = control.cached(tool, sid) if control is not None else None
        except Exception:  # noqa: BLE001
            sess = None
        title = (getattr(sess, "title", "") if sess is not None else "") or str(mark.get("title") or "")
        name = short_title(_redact_text(title), 24) if title else sid[:8]
        return f"{TOOL_LABELS.get(tool, tool)}「{name or sid[:8]}」"

    def _forget_restart_talk(self, scope: str) -> str:
        """说了「退出」：重启前那次对话模式的标记 + 还没点的「要发给它吗」一起作废。返回那个会话的名字（没有 ⇒ 空串）。"""
        offer = getattr(self, "_restart_offers", {}).pop(scope, None)
        label = offer["label"] if offer is not None else ""
        with _TALK_MARK_LOCK:
            marks, leftover = self._talk_mark_state()
            mark = leftover.pop(scope, None)
            if mark is not None:
                label = label or self._talk_mark_label(mark)
                if marks.pop(scope, None) is not None:
                    self._save_talk_marks(dict(marks))
        return label

    def _restart_talk_offer(self, scope: str, chat_id: str, text: str) -> bool:
        """重启后这个单聊的第一句：重启前在对话模式 ⇒ 先问一声，返回 True（这句当场答复了，没发、也没交给桥）。

        只问一次：问过了（或者已经过期、已经又进了对话模式）之后的话按平常走。
        """
        if not str(scope or "").startswith("dm:"):
            return False
        control = getattr(self, "fleet", None)
        with _TALK_MARK_LOCK:
            marks, leftover = self._talk_mark_state()
            mark = leftover.pop(scope, None)
            if mark is None:
                return False
            if marks.pop(scope, None) is not None:
                self._save_talk_marks(dict(marks))
        try:
            if control is not None and control.peek_pending(scope) is not None:
                return False  # 这个进程里已经又进了对话模式：照常发
        except Exception:  # noqa: BLE001
            pass
        label = self._talk_mark_label(mark)
        try:
            expired = float(mark.get("expires_at") or 0) < time.time()
        except (TypeError, ValueError):
            expired = True
        if expired:
            logger.info("对话模式 重启前已过期 tool=%s 会话=%s", mark.get("tool"), str(mark.get("id"))[:8])
            self._say(chat_id, f"刚才服务重启过。和{label}的「接着聊」重启前就已经过了 5 分钟，"
                               "这条按平常的方式处理，不发给它。")
            return False
        import secrets

        from .fleet.model import short_title

        nonce = secrets.token_hex(6)
        if not hasattr(self, "_restart_offers"):
            self._restart_offers = {}
        self._restart_offers[scope] = {"n": nonce, "tool": str(mark["tool"]), "id": str(mark["id"]),
                                       "text": str(text), "at": time.time(), "label": label}
        logger.info("对话模式 重启后第一句：先问要不要发 tool=%s 会话=%s 字数=%d",
                    mark.get("tool"), str(mark.get("id"))[:8], len(text))
        quoted = short_title(_redact_text(text), 60)
        self._say(chat_id, f"刚才服务重启过，和{label}的对话断了。这句要发给它吗？\n「{quoted}」")
        from .fleet.actions import _button

        # 按钮值只有一个序号：会话和原话记在服务端（`_restart_offers`），卡片值伪造不出要发什么、发给谁
        buttons = [
            _button("📨 发给它", {"act": "rtalk", "op": "send", "n": nonce}, primary=True),
            _button("不用，就问你", {"act": "rtalk", "op": "no", "n": nonce}),
        ]
        if not self._send_buttons(chat_id, buttons):
            # 按钮发不出去：这句不替他做主。告诉他两条都能怎么走（「会话」「直接再说」都不用打命令）
            self._say(chat_id, "按钮没发出来：要发给它就发「会话」重新挑它再说一次；不用的话直接再说一次，我来回答。")
        return True

    def _on_restart_talk_click(self, event, clicker: str, value: dict):
        """「这句要发给它吗」那两个按钮。3 秒内必须返回 ⇒ 只核验，发 / 交给桥都丢后台。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        raw_op = str(value.get("op") or "")[:12] if isinstance(value, dict) else ""

        def toast(kind: str, content: str, result: str = ""):
            logger.info("重启后要不要发 点击 op=%s 结果=%s", raw_op, result or kind)
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN], "拒绝:不是主人")
        if not isinstance(value, dict) or set(value) != {"act", "op", "n"} \
                or value.get("op") not in ("send", "no") \
                or not isinstance(value.get("n"), str) or len(value["n"]) > 32:
            return toast("error", _CARD_TOASTS[ApprovalStore.MALFORMED], "拒绝:形状不对")
        chat_id, scope = self._click_scope(event, clicker)
        offers = getattr(self, "_restart_offers", {})
        offer = offers.get(scope) if scope else None
        if offer is None or offer["n"] != value["n"]:
            return toast("error", "这个按钮已经失效了，要发给它就发「会话」重新挑它", "拒绝:没有在等的")
        offers.pop(scope, None)
        if time.time() - offer["at"] > RESTART_OFFER_TTL_SEC:
            self._say_later(chat_id, "这句隔得久了，这次没处理。还要的话再说一次。")
            return toast("info", "隔得久了，这次没处理", "过期")
        if value["op"] == "no":
            self._spawn(self._restart_talk_local(scope, chat_id, offer["text"]))
            return toast("info", "好，我来回答", "交给桥")
        self._spawn(self._restart_talk_send(scope, chat_id, offer))
        return toast("info", "好，先看一眼它还在不在", "发给它")

    async def _restart_talk_local(self, scope: str, chat_id: str, text: str) -> None:
        try:
            await self._run_locally(scope, chat_id, text)
        except Exception:  # noqa: BLE001
            logger.exception("重启后交给桥自己的 claude 失败")
            self._say_with_buttons(chat_id, "我这边出错了，没能完成。点「新话题」再试一次。",
                                   [_do_button("new", primary=True)])

    async def _restart_talk_send(self, scope: str, chat_id: str, offer: dict) -> None:
        """点了「发给它」：**现查**会话 + 按现在的样子查确认记录。过了 ⇒ 回到对话模式、走正常管道发这一句。

        🔴 不凭重启前的状态发：会话没了 / 读不出来 ⇒ 照实说没发；确认记录对不上（改名、换项目、开关变了、
        过了 24 小时）⇒ 记着这句、弹确认卡，卡上引用它，点确认之后才发（`_release_held` 那条现成的路）。
        """
        tool, sid, text = offer["tool"], offer["id"], offer["text"]
        control = getattr(self, "fleet", None)
        if control is None:
            self._say(chat_id, "会话总控这次没起来，这句没发。重启一下服务再试。")
            return
        try:
            sess, why = await asyncio.to_thread(control.refresh, tool, sid)
        except Exception:  # noqa: BLE001
            logger.exception("重启后现查会话失败")
            sess, why = None, "读不出来"
        if sess is None:
            msg = ("现在读不出它的状态，这句没发。过一会儿点「看看会话」重新挑它再说一次。" if why
                   else f"电脑上{offer['label']}已经关掉或删了，这句没发。")
            self._say_with_buttons(chat_id, msg, [_do_button("sessions", "🖥 看看会话")])
            return
        if not self._relay_granted(scope, tool, sid):
            changed = self._talk_changed_why(scope, tool, sid)
            logger.info("对话模式 重启后要发：确认记录对不上，重新问 tool=%s 会话=%s", tool, sid[:8])
            self._hold_for_consent(scope, tool, sid, text)
            self._say(chat_id, (f"{changed}，" if changed else "刚才那次确认已经不算数了，")
                      + "这句先没发。点下面那张卡确认之后，会把它发过去。")
            self._ask_relay_consent(chat_id, scope, tool, sid)
            return
        control.set_pending(scope, tool, sid)
        self._sync_talk_marker(scope)
        # 正常管道：在飞 / 身份复查 / 暂停 / 审查 / 发 / 贴回复全在里面
        await self._handle(scope, chat_id, text)

    # ---------- 一个群 = 一个会话（bridge/fleet/chatbind.py） ----------

    async def _group_bind_arm(self, scope: str, chat_id: str, text: str = "") -> bool:
        """群里来了一条消息：这个群绑了会话 ⇒ 把待发目标摆到那个会话上。

        `text`：这条消息本身。还没确认权限时**先记着**（`_hold_for_consent`），点确认后发过去 ——
        原来直接丢掉、叫他「再说一次」（2026-11-06 体验侦察）。

        返回 True = 这一条已经当场答复了（会话没了 / 读不出来 / 还没确认权限），**别往下走**；
        返回 False = 没绑、解绑过、或者已经摆好目标 —— 交给后面那套对话模式管道照常处理。

        🔴 为什么「会话没了」也要返回 True 而不是落回桥自己的 claude：主人在这个群里说话，
        心里想的就是电脑上那个会话。悄悄换成另一个 agent（另一个项目、另一套权限）去做，
        比不做更糟（spec 二 5：不能让用户对着一个死群说话，要明说）。
        """
        # 🔴 单聊一律不看这张表（spec 二 7）。先用字符串判，**不 import**：这段在每条消息的路上，
        # 安装器漏装了 chatbind.py 时 import 会抛 ⇒ 每条消息都变成「我这边出错了」。
        if not str(scope or "").startswith("group:"):
            return False
        binds = getattr(self, "chat_binds", None)  # 漏装 / 起不来时是 None ⇒ 按平常处理
        control = getattr(self, "fleet", None)
        if binds is None or control is None:
            return False
        try:
            bound = binds.active_of_scope(scope)
        except Exception:  # noqa: BLE001 —— 读不出来当没绑过，按平常处理
            logger.exception("读群绑定失败，这条按平常处理")
            return False
        if bound is None:
            return False

        from .fleet.actions import do_button

        sess, why = await asyncio.to_thread(control.refresh, bound.tool, bound.id)
        if sess is None and not why:
            # 确实没了：电脑上关掉 / 删掉了。照实说 + 给两条路，不自动解散群（不可逆的外部动作）。
            logger.info("群绑定 目标会话没了 tool=%s 会话=%s", bound.tool, bound.id[:8])
            control.clear_pending(scope)
            self._say_with_buttons(
                chat_id,
                f"这个群对应的{self._bound_label(bound)}在电脑上已经不在了（关掉或删掉了），这条没发出去。\n"
                "可以换个会话接着聊，或者把这个群解绑（群留着，只是不再转发）。",
                [do_button("sessions", "🔀 换个会话", primary=True), do_button("quit", "🔓 解绑这个群")],
            )
            return True
        if sess is None:
            # 读不出来 ≠ 没了。上次扫描里还有它就照常往下走（发之前 adapter 在锁里还会再查一次）；
            # 连上次都没有就不硬发 —— 不知道它在不在，就不往里塞话。
            if control.cached(bound.tool, bound.id) is None:
                logger.info("群绑定 读不出目标会话 tool=%s 会话=%s", bound.tool, bound.id[:8])
                self._say_with_buttons(
                    chat_id,
                    f"现在读不出{self._bound_label(bound)}的状态，这条先没发：{_redact_text(why)}\n过一会儿再说一次。",
                    [do_button("sessions", "🖥 回到总览")],
                )
                return True

        # 🔴 **权限确认按群单独记**（群里能看到的人更多；群里确认过不等于单聊确认过）。
        # 绑定是长期的，确认不是：照旧 24 小时问一次，名字 / 项目变了重新问。
        # 例外：这个群的待发目标已经是它 ⇒ 这一轮对话里已经确认过了（目标只在确认之后才会被摆上），
        # 不然确认记录存不下来时会每句都弹一次卡。
        # 🔴 用 `peek_pending_fresh`（过期的不算）：`peek_pending` 不看过期，群空了一天再说话
        # 会被当成「还在这一轮里」，那 24 小时的确认就被绕过去了。
        #
        # 🔴 2026-11-06（角色 F，修 D 实测复现的两条）：这条捷径原来**无限期跳过身份复查** ——
        #   1. 确认之后在电脑上改名 / 换项目（sessionId 不变），群里下一句照样直接进去；
        #   2. 电脑上关掉了（终端 → 没人占着），下一句悄悄换成「后台接着跑」，权限也换了一套。
        # 用户确认的是「跟**这个样子**的会话、按**卡上那套权限**说话」。所以捷径只在
        # 名字 / 项目 / 开没开着都和确认时一样时才走；变了就回到确认记录（指纹也变了 ⇒ 重新问）。
        # 只看这三样：状态（在跑 / 在等你）、pid、更新时间变了都不算，不多弹卡。
        current, _expired = control.peek_pending_fresh(scope)
        talking = current is not None and (current.tool, current.id) == (bound.tool, bound.id) \
            and self._talk_ident_same(scope, bound.tool, bound.id)
        if not talking and not self._relay_granted(scope, bound.tool, bound.id):
            changed = self._talk_changed_why(scope, bound.tool, bound.id)
            logger.info("群绑定 还没确认权限 tool=%s 会话=%s 变了=%s", bound.tool, bound.id[:8], bool(changed))
            control.clear_pending(scope)
            # 🔴 这句**先记着，不丢**（2026-11-06 体验侦察：原来丢掉、要他「再说一次」，单手打字最贵）。
            # 不直接发：他打这句的时候还没看过权限说明。卡上会引用原话、明说「点确认后会发过去」，
            # 他点确认的那一下就包括了这句。点「算了」/ 说取消 / 隔太久 ⇒ 不发，并照实说。
            self._hold_for_consent(scope, bound.tool, bound.id, text)
            if changed:
                self._say(chat_id, f"{changed}，这条先没发。点下面那张卡再确认一次，确认之后会把它发过去。")
            else:
                self._say(chat_id, f"这个群直达{self._bound_label(bound)}，但还没确认过权限，这条先没发。"
                                   "点下面那张卡确认之后，会把它发过去，不用再打一遍。")
            self._ask_relay_consent(chat_id, scope, bound.tool, bound.id)
            return True
        control.set_pending(scope, bound.tool, bound.id)
        return False

    async def _open_session_group(self, chat_id: str, scope: str, clicker: str, tool: str, sid: str,
                                  jumped: bool = False) -> None:
        """单聊里点了某个会话的 [接着聊]：给它建（或复用）一个群，在群里先问一次权限。

        `jumped=True`：那一下点的按钮**已经带着他跳进了这个群**（详情卡 op=go，第 14 轮）。
        复用到的正是那个群 ⇒ 单聊里不再发「已经有群了 + ➡️ 进群说话」（那就是用户要去掉的第二下）。
        群没了重建出新群 ⇒ 他跳去的是个死群，单聊里**照样**给新群的按钮（不能是死路）。

        - 同一个会话点两次 ⇒ 复用同一个群（`chatbind.ensure_group` 的幂等）
        - 群被主人解散了 ⇒ 重建一个
        - 建不了（没权限 / 飞书报错）⇒ 照实说是哪一种，然后**退回老路**：就在这个单聊里进对话模式。
          不能让用户点了一下什么都没得到。
        - 🔴 群里先发权限说明卡、**确认之后才转发**（scope 是这个群自己的，单聊里确认过不算）
        """
        from .fleet.actions import do_button
        from .fleet.cards import session_label
        from .fleet.chatbind import CREATED, NO_PERMISSION, ensure_group

        control = self.fleet
        sess = control.cached(tool, sid)
        title = getattr(sess, "title", "") if sess is not None else ""
        project = getattr(sess, "cwd", "") if sess is not None else ""
        if not project:
            project = getattr(self, "_new_session_cwd", {}).get(sid, "")
        label = session_label(sess, _redact_text) if sess is not None else f"{tool} 会话"
        result = await asyncio.to_thread(
            ensure_group, self.chat_binds, self.group_client, tool, sid, title,
            owner_open_id=clicker, clean=_redact_text, project=project,
        )
        logger.info("群绑定 建群 结果=%s tool=%s 会话=%s code=%s", result.how, tool, sid[:8], result.code)
        if not result.ok:
            if result.how == NO_PERMISSION:
                why = ("机器人还没有建群的权限，这次先不建群，就在这里跟它聊。"
                       "要用「一个会话一个群」得先在飞书开放平台给这个应用开建群权限并发布新版本。")
            else:
                why = "这次没建成群（飞书那边报错了，过一会儿再点一次试试），先就在这里跟它聊。"
            self._say(chat_id, why)
            # 退回老路：单聊里的对话模式，权限确认照旧
            if not self._relay_granted(scope, tool, sid):
                self._ask_relay_consent(chat_id, scope, tool, sid)
                return None
            target = control.set_pending(scope, tool, sid)
            self._sync_talk_marker(scope)
            self._say_with_buttons(chat_id, self._enter_talk_text(target, scope),
                                   self._talk_buttons(target.tool, target.id))
            return None

        bound = result.bound
        group_scope = bound.scope
        if result.how != CREATED and not bound.on:
            self.chat_binds.turn_on(bound.chat_id)
        if result.how == CREATED:
            # 新建的群（含「他跳去的那个群已经被解散、这里重建了」）⇒ 他手上没有能进这个群的东西，必须给按钮
            enter = await self._group_enter_button(bound.chat_id)
            self._say_with_buttons(
                chat_id,
                f"给{label}建了一个群「{bound.name}」。{_group_how_to_talk(self._group_perm_report(), proven=self._group_msg_proven())}"
                "点下面「进群说话」就能开始。",
                [enter, do_button("sessions", "🖥 回到总览")],
            )
        elif jumped:
            # 第 14 轮：点的那一下已经跳进这个群了 ⇒ 单聊里什么都不补（群里那句「现在就能说」下面照发）
            logger.info("群绑定 一下进群 tool=%s 会话=%s", tool, sid[:8])
        else:
            enter = await self._group_enter_button(bound.chat_id)
            self._say_with_buttons(
                chat_id,
                f"{label}已经有群了：「{bound.name}」，到那个群里接着说就行（没再建新的）。",
                [enter, do_button("sessions", "🖥 回到总览")],
            )
        # 🔴 2026-11-06 用户原话：「建群后自动进入对话，而不是再让用户点一次确认开始对话」。
        # 主人在单聊里点「接着聊」本身就是明确的意思表示（这条路只认主人，`_on_fleet_click` 白名单把关），
        # 群又是**为这一下点击专门建的** ⇒ 再弹一张「确认，开始说话」只是多点一次。
        # 所以：直接把这次点击记成确认（按**现在**的样子记 —— 身份复查那套照旧生效：
        # 之后改名 / 换项目 / 关终端，群里还是会重新问），群里发一张**说明**（不要确认）就开始。
        # 说明卡里那几条安全提示（电脑上那套权限、别拉别人进来）一条不少，只是不再挡着。
        # 复用（REUSED）的群也一样：主人刚点了「接着聊」，意思就是现在要聊。
        auto = False
        if not self._relay_granted(group_scope, tool, sid):
            self._grant_by_open_click(group_scope, tool, sid)
            auto = True
            # 摆上待发目标：和点「确认」那条路一样（`_on_relay_click` 确认分支的 `set_pending`）。
            # 不摆的话，确认记录存不下来时（`relay_consent` 起不来）群里第一句会落到「还没确认」那支 ——
            # 而这一下点击就是他的确认，内存里那份身份记录（`_talk_idents`）够撑起这一段对话。
            control.set_pending(group_scope, tool, sid)
        if auto or self._relay_granted(group_scope, tool, sid):
            # 🔴 说法和确认卡同一个判断（要不要 @）；退出给按钮，不叫人打「取消」
            # （11-05 用户原话：「不要让用户输入 "/cancel" 等类似的 尽量用按钮的形式」）。
            # 原来确认卡上那几条安全提示**照样说一遍**（只是不再要他点确认）：
            # 手机和终端共用当前权限档位；群里的人能看到回复，别拉无关人员进来。
            self._say_with_buttons(
                bound.chat_id,
                f"这个群直达{label}，现在就能说。"
                f"{_group_how_to_talk(self._group_perm_report(), here=True, proven=self._group_msg_proven())}\n"
                f"{self._rights_line(tool, sid)}"
                "· 群里的人都能看到它的回复，别拉别人进来\n"
                "要停止转发点下面的「退出对话」。",
                self._talk_buttons(tool, sid),
            )
        else:
            self._ask_relay_consent(bound.chat_id, group_scope, tool, sid)
        try:
            await asyncio.to_thread(self._refresh_status_card, bound.chat_id)
        except Exception:  # noqa: BLE001 —— 状态卡失败不能连累进群
            logger.exception("建群后发状态卡失败")
        return bound

    async def _group_enter_button(self, group_chat_id: str):
        """建好（或复用）群之后那个「点一下就跳进群」的按钮。拿不到 ⇒ None（这排少一个按钮）。

        用户原话：「补充新建群后，让用户自动跳转到群里（如果实现不了，可以点击某个按钮进群，
        不要让用户去找）」。

        🔴 **真正的自动跳转飞书做不到**：机器人发消息改不了你客户端当前打开哪个会话，
        没有这个接口。所以做成「一点就到」—— 这是这件事能做到的最近距离。

        优先用 `open_link`（`client/chat/open?openChatId=…`）：
        - 纯拼字符串、**不调接口** ⇒ 建群这一下不多一次网络往返、不会失败
        - 主人建群时就是群主、已经在群里 ⇒ 他要的是「跳过去」，不是「加入」
        - 它不是凭据（不带 token），转发给别人也进不去那个私有群

        `link()`（`add_by_link`，带 token）只在 open_link 拼不出来时兜底 ——
        那是「谁拿到谁能进」的凭据，能不发就不发。
        """
        from .fleet.actions import url_button

        client = getattr(self, "group_client", None)
        if client is None or not group_chat_id:
            return None
        opener = getattr(client, "open_link", None)
        if opener is not None:
            try:
                direct = opener(group_chat_id)
            except Exception:  # noqa: BLE001
                logger.exception("拼直开链接失败 chat=%s", str(group_chat_id)[:12])
                direct = ""
            if direct:
                button = url_button("➡️ 进群说话", direct)
                if button is not None:
                    return button
        # 兜底：带 token 的分享链接（要调飞书，慢 ⇒ 丢线程，别卡住点击的回包）
        getter = getattr(client, "link", None)
        if getter is None:
            return None
        try:
            url, code, msg = await asyncio.to_thread(getter, group_chat_id)
        except Exception:  # noqa: BLE001 —— 取链接失败不能影响已经建好的群
            logger.exception("取入群链接抛异常 chat=%s", str(group_chat_id)[:12])
            return None
        if not url:
            # 不报错、不换说法：群已经建好了，链接只是个捷径。少一个按钮比多一句
            # 「链接没取到」要好（后者会让人以为群也没建成）
            logger.info("没取到入群链接 code=%s msg=%s", code, str(msg)[:80])
            return None
        return url_button("➡️ 点这里进群", url)

    def _group_bind_click(self, chat_id: str, scope: str, tool: str, sid: str) -> None:
        """在**群里**点了某个会话的 [接着聊]：这个群绑过 ⇒ 改绑到它 / 重新接上。

        没绑过的普通群不动（还是老的 5 分钟对话模式）。一个群只对一个会话 ⇒ 点了另一个就改绑，
        群名不跟着改（改群名是外部可见动作，用户也可能自己改过），在消息里说一声。
        """
        binds = getattr(self, "chat_binds", None)
        if binds is None or not str(scope or "").startswith("group:"):
            return
        group_chat = scope.split(":", 1)[1]
        try:
            bound = binds.of_chat(group_chat)
            if bound is None:
                return
            if (bound.tool, bound.id) == (tool, sid):
                if not bound.on:
                    binds.turn_on(group_chat)
                    logger.info("群绑定 重新接上 tool=%s 会话=%s", tool, sid[:8])
                return
            binds.bind(group_chat, tool, sid, bound.name)
            logger.info("群绑定 改绑 tool=%s 会话=%s", tool, sid[:8])
            renamed = self._group_rename_after_rebind(group_chat, tool, sid)
            # 🔴 这个会话原来绑在别的群上 ⇒ 那些群现在不转发了，**必须当场说清**并清掉
            # 它们的待发目标。不然主人在旧群里继续说话：5 分钟内还进这个会话，
            # 5 分钟后悄悄落到桥自己的 claude（两份审查都指出这一条）。
            for old_chat in getattr(binds, "last_displaced", ()) or ():
                control0 = getattr(self, "fleet", None)
                if control0 is not None:
                    try:
                        control0.clear_pending(f"group:{old_chat}")
                    except Exception:  # noqa: BLE001
                        logger.exception("清旧群待发目标失败")
                self._say_later(
                    old_chat,
                    "这个会话已经挪到另一个群了，这里不再往它转发。"
                    "想接着在这个群聊，发「会话」挑一个会话绑上来。",
                )
        except Exception:  # noqa: BLE001 —— 绑不上不该让这一下点击崩掉，老路照样能聊
            logger.exception("群绑定 改绑失败")
            return
        control = getattr(self, "fleet", None)
        sess = control.cached(tool, sid) if control is not None else None
        from .fleet.cards import session_label

        label = session_label(sess, _redact_text) if sess is not None else f"{tool} 会话"
        # 🔴 群名要么跟过去、要么说清为什么没跟 —— 不能让群名写着老会话的名字
        # 而每句都进新的那个（2026-11-05 两份 review 的同一条）。
        if renamed:
            tail = f"群名也改成「{_redact_text(renamed)}」了。"
        else:
            tail = "群名还是原来那个（你自己改过的名字我不动），想改可以自己改。"
        self._say_later(chat_id, f"这个群改成直达{label}了。{tail}")

    def _group_rename_after_rebind(self, group_chat: str, tool: str, sid: str) -> str:
        """群改绑之后把群名跟到新会话上 → 改成了就返回新群名，没改返回空串。

        判「该不该改」的那套在 `chatbind.rename_group`：只有**飞书上现在的群名**和
        我们当初起的那个（`Bound.name`）一模一样时才改 —— 主人自己改过的名字一个字都不碰
        （群主是他，改群名是外部可见动作）。读不出群名也不改。
        """
        binds = getattr(self, "chat_binds", None)
        client = getattr(self, "group_client", None)
        if binds is None or client is None:
            return ""
        try:
            from .fleet.chatbind import rename_group

            bound = binds.of_chat(group_chat)
            if bound is None:
                return ""
            control = getattr(self, "fleet", None)
            sess = control.cached(tool, sid) if control is not None else None
            title = str(getattr(sess, "title", "") or "")
            project = str(getattr(sess, "cwd", "") or "")
            got = rename_group(binds, client, bound, title, clean=_redact_text, project=project)
        except Exception:  # noqa: BLE001 —— 改不了群名不该让这一下点击崩掉，绑定已经改好了
            logger.exception("改绑后改群名失败")
            return ""
        logger.info("群绑定 改群名 结果=%s chat=%s", got.how, group_chat[:12])
        return got.name if got.changed else ""

    def _group_bind_quit(self, scope: str, chat_id: str) -> bool:
        """在绑过的群里点「退出对话」/ 说「取消」= **解绑**：只关转发，群留着（不自动解散）。

        不这么做的话「退出」在群里是假的：下一句话又会按绑定重新摆上目标。返回 True = 已处理。
        """
        # 不 import chatbind：这段在「取消」的路上，是用户的自救通道，漏装了也不能崩
        binds = getattr(self, "chat_binds", None)
        if binds is None or not str(scope or "").startswith("group:"):
            return False
        group_chat = scope.split(":", 1)[1]
        try:
            bound = binds.active_of_scope(scope)
            if bound is None:
                return False
            binds.turn_off(group_chat)
        except Exception:  # noqa: BLE001
            logger.exception("群绑定 解绑失败")
            return False
        logger.info("群绑定 解绑 tool=%s 会话=%s", bound.tool, bound.id[:8])
        control = getattr(self, "fleet", None)
        if control is not None:
            control.clear_pending(scope)
        getattr(self, "_pending_consent", {}).pop(scope, None)
        # 确认前记着的那几句也作废：解绑了还在之后某次确认时冒出去，就是替他说话
        dropped = self._drop_held(scope)
        flying = getattr(self, "_fleet_inflight", {}).get(scope)
        if flying is not None:
            if not hasattr(self, "_talk_quit"):
                self._talk_quit = {}
            self._talk_quit[scope] = True
        from .fleet.actions import do_button

        tail = "刚发出去那一句还在跑，它的回复还会带回来一次。\n" if flying is not None else ""
        if dropped:
            tail += "你确认前说的那几句也没发过去。\n"
        self._say_with_buttons(
            chat_id,
            f"好，这个群不再转发给{self._bound_label(bound)}了。群留着没解散，之后在这里说的话按平常处理。\n"
            f"{tail}要重新接上：在总览里点它的 [接着聊]（会接着用这个群）。",
            [do_button("sessions", "🖥 回到总览")],
        )
        return True

    def _click_scope(self, event, clicker: str) -> tuple[str, str]:
        """卡片回调 → (chat_id, scope)。scope 为空串 = 对不上，别猜。

        🔴 **只读 _chat_ids，不在卡片回调里写它。**
        2026-11-04 安全审查：服务重启后 _chat_ids 是空的（只在内存里），群里点旧卡片时
        原来会把待发目标记到 `dm:<点的人>`，**还把单聊 scope 指向这个群** ——
        之后他在群里照提示打字，消息落进了群自己的 claude；他私聊发的下一句又被当成
        要转发给那个会话的话。scope 必须和「下一条消息会落到哪」一致：
          - 这个 chat 是记录过的单聊 ⇒ dm scope
          - 记录过的群 ⇒ group scope
          - 两边都对不上（重启后第一次）⇒ 不猜，让他在这个聊天里先发一句「会话」
        """
        context = getattr(event, "context", None)
        chat_id = getattr(context, "open_chat_id", "") or ""
        known = getattr(self, "_chat_ids", {})
        dm_scope = f"dm:{clicker}"
        if not chat_id:
            chat_id = known.get(dm_scope, "")
        if not chat_id:
            return "", ""
        group_scope = next((sc for sc, c in known.items() if c == chat_id and sc.startswith("group:")), "")
        if group_scope:
            return chat_id, group_scope
        # 「一个群 = 一个会话」建出来的群：绑定表是落盘的，重启后 _chat_ids 是空的也认得出它是群
        # （那是我们自己建的群，chat_id 由飞书建群接口给回）。只读，不写 _chat_ids。
        binds = getattr(self, "chat_binds", None)
        if binds is not None:
            try:
                if binds.of_chat(chat_id) is not None:
                    return chat_id, f"group:{chat_id}"
            except Exception:  # noqa: BLE001 —— 读不出来就走下面的老判断
                logger.exception("读群绑定失败")
        recorded = known.get(dm_scope, "")
        if recorded == chat_id:
            return chat_id, dm_scope
        # 🔴 **重启后只点过菜单、没打过字**：常驻菜单的事件只带 open_id、不带 chat_id，
        # 于是 _chat_ids 里记的是 `ou_…`，而卡片回调带的是真 chat_id `oc_…` —— 永远对不上，
        # 每一下 [接着聊] 都报「服务刚重启过，先发一句话」（2026-11-05 用户实测撞上）。
        # 这种情况**能确定**是单聊：记的那个就是点的人自己的 open_id，常驻菜单也只在单聊里有。
        # 所以这里认它，并把真 chat_id 补记上；记的是别的 `oc_…`（说明这张卡在另一个群里）才拒。
        if recorded == clicker:
            self._chat_ids[dm_scope] = chat_id
            return chat_id, dm_scope
        # 🔴 一条都没记过（重启后第一次点卡片）：原来到这里就**拒**，回「服务刚重启过，
        # 先发一句话」。而这个桥重启是常态（看门狗自杀等拉起 + 每次上线，11-05 一晚 4 次）
        # ⇒ 用户聊天记录里的旧卡片全死，他看到的是「点某个会话这个功能没了」（11-05 实测报修）。
        #
        # 现在不用猜了：**直接问飞书这个 chat 是群还是单聊**（`im:chat:read` 11-05 已开）。
        # 问出来是单聊 ⇒ 按单聊走并补记；是群 ⇒ group scope；问不出来（没权限 / 网络不通）
        # 才回到原来那条「不猜」的路上。
        decided = self._chat_is_group(chat_id)
        if decided is False:
            self._chat_ids[dm_scope] = chat_id
            return chat_id, dm_scope
        if decided is True:
            return chat_id, f"group:{chat_id}"
        # 记的是另一个 chat、或者飞书也问不出来：卡片回调分不出单聊和群，不猜
        return chat_id, ""

    def _chat_is_group(self, chat_id: str):
        """这个 chat 是群吗（True/False/None=问不出来）。失败一律 None，**绝不猜**。

        猜错的后果是两条路都错：把群当单聊 ⇒ 他私聊的下一句被当成要转发的话；
        把单聊当群 ⇒ scope 指向一个不存在的群。所以问不出来时宁可让他重新点一下。
        """
        getter = getattr(getattr(self, "group_client", None), "is_group", None)
        if getter is None:
            return None
        try:
            return getter(chat_id)
        except Exception:  # noqa: BLE001 —— 只影响「要不要让他重点一次」，不能让点击崩掉
            logger.exception("问不出这个 chat 是群还是单聊 chat=%s", str(chat_id)[:12])
            return None

    def _fleet_expanded_card(self, control, tool: str):
        """「看全部」那张替换卡（dict）：最近一次扫描 + 和 `_fleet_overview` 同一套补全 / 照实改写。

        没有上次的扫描、或者造卡出了错 ⇒ None（调用方退回后台扫描 + 新发一张）。
        🔴 只读内存里的上次结果，不现扫：卡片回调 3 秒内必须返回。
        """
        from .fleet.cards import build_overview_card
        from .fleet.notify import fill_unread, unread_tools

        view = getattr(control, "last", None)
        if view is None:
            return None
        try:
            scanner = self._fleet_scanner()
            unread = unread_tools(view, control)
            view = fill_unread(view, control, getattr(scanner, "read_at", None))
            card = self._honest_overview_card(
                build_overview_card(view, redact=_redact_text, expand=tool,
                                    row_button=self._overview_row_button(chat_id)), view, unread)
            return json.loads(card) if card else None
        except Exception:  # noqa: BLE001 —— 换不了卡就退回新发一张，别让这一下什么都没有
            logger.exception("「看全部」造卡失败 tool=%s，退回重新扫描", tool)
            return None

    def _fleet_settled_store(self):
        """「结束 / 稍后提醒」的记录：config 同目录 settled.json；没有 config_path ⇒ 只在内存。

        绑成 `settled.active()` ⇒ 总览卡和后台推送读到的是同一份。
        """
        from .fleet import settled

        config_path = getattr(getattr(self, "settings", None), "config_path", "") or ""
        return settled.bind(settled.store_for(config_path))

    def _fleet_mark_click(self, control, click: dict, toast):
        """op = settle / unsettle / snooze（只被 `_on_fleet_click` 调）。3 秒内返回：只写记录 + 原地换卡。

        🔴 只记在我们这边（`fleet/settled.py`），不碰 Claude / Codex / Cursor 的库。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.cards import build_settled_card, build_unsettled_card
        from .fleet.notify import build_snoozed_card
        from .fleet.settled import can_settle

        op, tool, sid = click["op"], click["tool"], click["id"]
        sess = control.cached(tool, sid)
        if sess is None:
            return toast("error", "这张卡片过期了，点「回到总览」重新看一眼", "拒绝:缓存里没有")
        try:
            store = self._fleet_settled_store()
            if op == "settle":
                ok, why = can_settle(sess)
                if not ok:
                    return toast("error", why, "拒绝:在跑或等你")
                store.settle(tool, sid)
                card, word = build_settled_card(sess, _redact_text), "结束了，总览里不再列它"
            elif op == "unsettle":
                store.unsettle(tool, sid)
                card, word = build_unsettled_card(sess, _redact_text), "撤销了"
            else:
                store.snooze(tool, sid, click.get("q", ""))
                card, word = build_snoozed_card(sess, _redact_text), "好，1 小时后再提醒"
        except Exception:  # noqa: BLE001 —— 记录 / 造卡出错不能让回调炸
            logger.exception("总控点击 op=%s 处理失败", op)
            return toast("error", "这一下没记上，再点一次", "记录没写上")
        logger.info("总控点击 op=%s tool=%s 会话=%s 结果=原地换卡", op, tool, sid[:8])
        return P2CardActionTriggerResponse({
            "toast": {"type": "info", "content": word},
            "card": {"type": "raw", "data": card},
        })

    def _on_fleet_click(self, event, clicker: str, value: dict):
        """总控卡片上的按钮。3 秒内必须返回 ⇒ 只核验 + 记目标，扫描 / 读消息 / 停都丢后台。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.cards import resolve_fleet_click

        # 每个分支一行：op / tool / 会话短 id / 结果。不记卡片上的任何正文。
        raw_op = str(value.get("op") or "")[:12]

        def toast(kind: str, content: str, result: str = ""):
            logger.info("总控点击 op=%s tool=%s 会话=%s 结果=%s", raw_op,
                        str(value.get("tool") or "")[:10], str(value.get("id") or "")[:8] or "-",
                        result or kind)
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN], "拒绝:不是主人")
        control = getattr(self, "fleet", None)
        if control is None:
            return toast("error", "会话总控这次没起来，重启一下服务再试", "拒绝:总控没起来")
        click = resolve_fleet_click(value, control.known_keys())
        if click is None:
            # 伪造的、或者是很久以前那张卡（id 不在最近一次扫描里）。
            # 🔴 最常见的原因是**服务重启过**（`_known` 只在内存里）⇒ toast 一闪就没，
            # 用户看到的是「点了没反应」（2026-11-06 体验侦察 Top3）。补一条带按钮的消息。
            self._stale_card_notice(event, clicker)
            return toast("error", "这张卡片过期了，点「回到总览」重新看一眼", "拒绝:不在白名单或已过期")
        if click["op"] in ("settle", "unsettle", "snooze"):
            # 第 33 轮：结束 / 撤销 / 稍后提醒 —— 只改我们这边的记录 + 原地换卡，不扫描、不回消息
            return self._fleet_mark_click(control, click, toast)

        chat_id, scope = self._click_scope(event, clicker)
        if not chat_id:
            return toast("error", "找不到要回复的对话，直接发「会话」就好", "拒绝:找不到chat")
        needs_scope = click["op"] in ("send", "go")
        if needs_scope and not scope:
            self._unknown_chat_notice(chat_id)
            return toast("error", "服务刚重启过，先在这个聊天里发一句「会话」再点", "拒绝:scope对不上")

        op, tool, sid = click["op"], click["tool"], click["id"]

        def later(coro) -> None:
            if self._loop is None:
                coro.close()
                return
            fut = asyncio.run_coroutine_threadsafe(coro, self._loop)

            # 🔴 结果别丢。2026-11-04 稳定性审查：飞书接口超时 / 网络抖一下时异常存在
            # 被丢掉的 Future 上 ⇒ 用户看到「收到」toast 之后什么都没有，日志里也没有。
            def done(f) -> None:
                exc = f.exception()
                if exc is None:
                    return
                logger.error("会话总控按钮处理失败 op=%s tool=%s", click.get("op"), click.get("tool"),
                             exc_info=(type(exc), exc, exc.__traceback__))
                try:
                    self._say_with_buttons(chat_id, "这一下没处理成（可能网络抖了一下），再点一次，或者点「回到总览」重新看。",
                                           [_do_button("sessions", "🖥 回到总览")])
                except Exception:  # noqa: BLE001 —— 报错本身也失败就只能留日志了
                    logger.exception("连报错消息都没发出去")

            fut.add_done_callback(done)

        if op == "refresh" and not sid:
            later(self._fleet_overview(chat_id))
            return toast("info", "收到，重新看一眼", "已排队:总览")
        if op == "more":
            # 「还有 N 个 · 点开看全部」（第 28 轮）：**原地换卡**。3 秒内必须返回 ⇒ 不现扫，
            # 用总控记着的最近一次扫描（`FleetControl.last`）。没有（刚重启）⇒ 退回后台扫一次、新发一张。
            card = self._fleet_expanded_card(control, tool)
            if card is None:
                later(self._fleet_overview(chat_id))
                return toast("info", "这张卡太旧了，重新看一眼，新的马上发过来", "已排队:总览(无缓存)")
            logger.info("总控点击 op=more tool=%s 结果=原地换卡", tool)
            return P2CardActionTriggerResponse({
                "toast": {"type": "info", "content": "这一组全列出来了"},
                "card": {"type": "raw", "data": card},
            })
        if op in ("open", "refresh"):
            later(self._fleet_detail(chat_id, tool, sid, scope))
            return toast("info", "收到，正在读", "已排队:详情")
        if op == "stop":
            later(self._fleet_stop(chat_id, tool, sid))
            return toast("info", "收到，正在停", "已排队:停")
        if op == "remind":
            later(self._remind_stop_later(chat_id, tool, sid))
            return toast("info", f"好，{STOP_REMIND_SEC // 60} 分钟后还在跑就提醒你", "已排队:提醒")
        # op == "send"：进对话模式。
        # 单聊里点、而且能建群 ⇒ 给这个会话建（或复用）一个群，之后在那个群里说的话直达它。
        # 建群要调飞书接口（慢）⇒ 丢后台；能不能建由 `group_client` 定，没开权限时它是 None，走老路。
        # op == "go"：详情卡上那个按钮**同时**带了跳进群的链接（第 14 轮），客户端已经在往群里跳了
        # ⇒ 后台照样走 `_open_session_group`（群里第一句、记确认、群没了就重建），只是单聊里不再补进群按钮。
        if scope.startswith("dm:") and getattr(self, "group_client", None) is not None \
                and getattr(self, "chat_binds", None) is not None:
            later(self._open_session_group(chat_id, scope, clicker, tool, sid, jumped=op == "go"))
            if op == "go":
                return toast("info", "收到，进群接着说", "已排队:跳进群")
            return toast("info", "收到，正在给它建群", "已排队:建群")
        # 在绑过的群里点了另一个会话 ⇒ 改绑（这个群以后直达新点的那个）
        self._group_bind_click(chat_id, scope, tool, sid)
        # 🔴 没确认过**先弹确认卡、不记目标** —— 记了就等于他下一句话会直接进那个会话，
        # 而他还没被告知「那边的权限不是手机这边这套」。确认过的 24 小时内直接进。
        if not self._relay_granted(scope, tool, sid):
            self._ask_relay_consent(chat_id, scope, tool, sid)
            return toast("info", "先确认一下权限", "弹确认卡")
        target = control.set_pending(scope, tool, sid)
        self._sync_talk_marker(scope)
        self._say_later_with_buttons(chat_id, self._enter_talk_text(target, scope),
                                     self._talk_buttons(target.tool, target.id))
        return toast("info", "收到，说吧", "进对话模式")

    # ---------- 手机上「➕ 新建会话」（bridge/fleet/newsession.py） ----------

    def _newsession_state(self):
        state = getattr(self, "_new_sessions", None)
        if state is None:
            from .fleet.newsession import NewSessionState

            state = self._new_sessions = NewSessionState()
        return state

    def _in_background(self, fn, *args) -> None:
        """丢进线程池跑，立刻返回（卡片回调 3 秒纪律）。没起循环就同步跑。"""
        loop = getattr(self, "_loop", None)
        if loop is None or not hasattr(loop, "call_soon_threadsafe"):
            fn(*args)
            return
        loop.call_soon_threadsafe(lambda: loop.run_in_executor(None, fn, *args))

    def _new_current_dir(self, scope: str) -> str:
        sessions = getattr(self, "sessions", None)
        try:
            return sessions.get(scope).workspace if sessions is not None else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---------- CEO 编排 ----------

    def _build_ceo(self, home: str):
        """CEO 本体。大脑用 Claude（无工具、结构化输出），认证键和总控的 Claude 同一份。"""
        control = getattr(self, "fleet", None)
        if control is None:
            return None
        from .fleet.ceo import ClaudeBrain, MissionStore, Orchestrator

        adapter = None
        try:
            adapter = control.fleet.adapter("claude")
        except Exception:  # noqa: BLE001
            adapter = None

        def brain_env() -> dict:
            if adapter is not None and hasattr(adapter, "_send_env"):
                return adapter._send_env("")
            return dict(os.environ)

        def brain_settings() -> dict:
            if adapter is not None and hasattr(adapter, "_auth"):
                return dict(adapter._auth()[1])
            return {}

        def projects() -> list:
            policy = self.policy
            dirs = workspace_mod.recent_repos(policy, "", 8)
            return [(workspace_mod.short_name(d, policy), d) for d in dirs]

        def approval_dir(tool: str, key: str) -> str:
            return self._fleet_approval_dir(tool, key, self.policy) if tool in ("claude", "codex") else ""

        return Orchestrator(
            MissionStore(os.path.join(home, "ceo_missions.json")),
            ClaudeBrain(env=brain_env, settings=brain_settings),
            control,
            self._say,
            policy=lambda: self.policy,
            projects=projects,
            approval_dir=approval_dir,
            timeout=lambda: float(getattr(self.policy, "turn_timeout_sec", 600) or 600),
        )

    async def _handle_ceo_command(self, scope: str, chat_id: str, text: str) -> None:
        from .fleet.ceo import CEO_HELP, parse_command

        ceo = getattr(self, "ceo", None)
        if ceo is None:
            self._say(chat_id, "CEO 编排现在用不了（会话总控没起来）。")
            return
        action, arg = parse_command(text)
        in_group = str(scope or "").startswith("group:")
        if action == "help":
            self._say(chat_id, CEO_HELP)
            return
        if action == "mark":
            if not in_group:
                self._say(chat_id, "CEO 群要在群里设：先建一个群、把机器人拉进去，再在群里发「/ceo 群」。")
                return
            binds = getattr(self, "chat_binds", None)
            bound = binds.of_chat(chat_id) if binds is not None else None
            if bound is not None and getattr(bound, "on", True):
                self._say(chat_id, "这个群已经绑了一个会话，不能同时当 CEO 群。换一个群，或先在这里发「退出」解绑。")
                return
            ceo.mark_chat(chat_id)
            self._say(chat_id, "🧭 这个群现在是 **CEO 群**。直接说任务就行，CEO 会拆分、派给各个会话、汇总，"
                               "要你拍板时在这里问你。发「/ceo 状态」看进度，「/ceo 退出」取消。")
            return
        if action == "unmark":
            gone = ceo.unmark_chat(chat_id)
            self._say(chat_id, "好，这个群不再是 CEO 群。" if gone else "这个群本来就不是 CEO 群。")
            return
        if action == "status":
            self._say(chat_id, ceo.status_text(chat_id))
            return
        if action == "stop":
            self._say(chat_id, await asyncio.to_thread(ceo.cancel, chat_id))
            return
        if action == "resume":
            self._say(chat_id, await asyncio.to_thread(ceo.resume, chat_id))
            return
        ok, msg = await asyncio.to_thread(ceo.start, chat_id, arg)
        if not ok:
            self._say(chat_id, msg)

    async def _maybe_ceo_text(self, scope: str, chat_id: str, text: str) -> bool:
        ceo = getattr(self, "ceo", None)
        if ceo is None or not str(scope or "").startswith("group:"):
            return False
        if not ceo.is_ceo_chat(chat_id):
            return False
        return bool(await asyncio.to_thread(ceo.handle_text, chat_id, text))

    async def _handle_newsession_command(self, scope: str, chat_id: str, text: str) -> bool:
        """`/newsession [工具] [项目]`、「新建会话」。不是这条 ⇒ False。"""
        from .fleet.newsession import COMMANDS, TOOL_ALIASES, WORDS

        stripped = (text or "").strip()
        parts = stripped.split()
        command = parts[0].lower() if parts else ""
        if command not in COMMANDS and stripped not in WORDS:
            return False
        args = parts[1:] if command in COMMANDS else []
        state = self._newsession_state()
        if args and args[0].lower() in ("cancel", "取消"):
            had = state.disarm(scope)
            self._say_with_buttons(chat_id, "好，不建了。" if had else "现在没有在新建的会话。",
                                   [_do_button("sessions")])
            return True
        if getattr(self, "fleet", None) is None:
            self._say(chat_id, "会话总控这次没起来，重启一下服务再试。")
            return True
        if not args:
            self._send_new_tool_card(chat_id)
            return True
        if args[0].lower() in ("project", "项目") and len(args) >= 2 and args[1].lower() in ("new", "新建"):
            await self._new_project(scope, chat_id, args[2:])
            return True
        tool = TOOL_ALIASES.get(args[0].lower())
        if tool is None:
            self._say(chat_id, "认不出要用哪个，能用的是 claude / codex。")
            return True
        if len(args) == 1:
            await asyncio.to_thread(self._send_new_dir_card, scope, chat_id, tool)
            return True
        from .fleet.newsession import pick_effort, pick_model

        rest = args[1:]
        model, effort = "", ""
        for _ in range(2):
            if not rest:
                break
            if not effort and pick_effort(tool, rest[-1]):
                effort = pick_effort(tool, rest[-1])
                rest = rest[:-1]
            elif not model and pick_model(tool, rest[-1]):
                model = pick_model(tool, rest[-1])
                rest = rest[:-1]
        if not rest:
            await asyncio.to_thread(self._send_new_dir_card, scope, chat_id, tool, model, effort)
            return True
        found = workspace_mod.resolve_request(self.policy, self._new_current_dir(scope), " ".join(rest))
        if not found.ok or not found.workspace:
            if found.outcome == workspace_mod.Outcome.MISSING:
                created = workspace_mod.create_missing_workspace(self.policy, " ".join(rest))
                if created.ok:
                    found = created
                else:
                    self._say(chat_id, created.reply or found.reply.replace("/cd ", f"/newsession {tool} "))
            if not found.ok or not found.workspace:
                return True
        self._arm_new_session(scope, chat_id, tool, found.workspace, model, effort)
        return True

    async def _new_project(self, scope: str, chat_id: str, args: list) -> None:
        """`/ns project new <路径> [--git] [claude|codex]`：建项目目录，然后接着选工具（或直接进第一句）。"""
        from .fleet.newsession import TOOL_ALIASES

        git = any(a.lower() in ("--git", "git") for a in args)
        tool = next((TOOL_ALIASES[a.lower()] for a in args if a.lower() in TOOL_ALIASES), "")
        path = " ".join(a for a in args if a.lower() not in ("--git", "git") and a.lower() not in TOOL_ALIASES)
        made = await asyncio.to_thread(workspace_mod.create_project, self.policy, path, git=git)
        if not made.ok or not made.workspace:
            self._say(chat_id, made.reply)
            return
        if tool:
            self._say(chat_id, made.reply)
            self._arm_new_session(scope, chat_id, tool, made.workspace)
            return
        self._say(chat_id, made.reply + "。接着选用哪个工具，选完说第一句就开工。")
        self._remember_new_project(scope, made.workspace)
        self._send_new_tool_card(chat_id)

    def _remember_new_project(self, scope: str, path: str) -> None:
        if not hasattr(self, "_new_projects"):
            self._new_projects = {}
        self._new_projects[scope] = path

    def _send_new_tool_card(self, chat_id: str) -> None:
        from .fleet.newsession import build_tool_card

        sender = getattr(getattr(self, "replier", None), "send_card", None)
        if sender is None or not sender(chat_id, build_tool_card(_do_button("newcancel"))):
            self._say(chat_id, "选工具的卡片发不出去。也可以直接发：/newsession claude（或 codex）")

    def _send_new_dir_card(self, scope: str, chat_id: str, tool: str, model: str = "", effort: str = "") -> None:
        from .fleet.newsession import MAX_DIRS, build_dir_card

        policy = self.policy
        dirs = workspace_mod.recent_repos(policy, self._new_current_dir(scope), MAX_DIRS)
        if not dirs:
            self._say(chat_id, "没找到手机能用的项目目录。要放开得在电脑上改这个助手的设置。")
            return
        nonce = self._newsession_state().offer(scope, tool, dirs)
        if model or effort:
            self._newsession_models()[scope] = (tool, model, effort)
        else:
            self._newsession_models().pop(scope, None)
        labels = [_redact_text(workspace_mod.short_name(d, policy)) for d in dirs]
        card = build_dir_card(tool, labels, nonce, _do_button("newcancel"))
        sender = getattr(getattr(self, "replier", None), "send_card", None)
        if sender is None or not sender(chat_id, card):
            self._say(chat_id, f"选项目的卡片发不出去。也可以直接发：/newsession {tool} 项目名")

    def _newsession_models(self) -> dict:
        """选项目卡发出去时带的模型（`/newsession claude opus` 再点卡上的项目）。scope → 模型。"""
        if not hasattr(self, "_new_models"):
            self._new_models = {}
        return self._new_models

    def _arm_new_session(self, scope: str, chat_id: str, tool: str, cwd: str, model: str = "",
                         effort: str = "") -> None:
        from .fleet.catalog import EFFORT_LABELS
        from .fleet.newsession import model_hint, permission_line
        from .fleet.scan import TOOL_LABELS

        policy = self.policy
        carried = self._newsession_models().pop(scope, None)
        if carried and carried[0] == tool:
            model = model or carried[1]
            effort = effort or (carried[2] if len(carried) > 2 else "")
        self._newsession_state().arm(scope, tool, cwd, model, effort)
        logger.info("新建会话 选好 tool=%s model=%s effort=%s scope=%s", tool, model or "-", effort or "-", scope[:16])
        short = _redact_text(workspace_mod.short_name(cwd, policy))
        mode = MODE_LABELS.get(policy.mode, policy.mode)
        base = self._effective_model(tool, "")
        effort_text = f" / {EFFORT_LABELS.get(effort, effort)}" if effort else ""
        if model:
            model_line = f"模型：**{model}{effort_text}**\n"
        else:
            default_text = base.text() if (base.model or base.effort) else "工具默认"
            model_line = (f"模型：默认（{default_text}）{effort_text}。要指定就发"
                          f"「/ns {tool} 项目名 模型 [强度]」，可选 {model_hint(tool)}\n")
        self._say_with_buttons(
            chat_id,
            f"好，在「{short}」新开一个 **{TOOL_LABELS.get(tool, tool)}** 会话。\n"
            f"{permission_line(tool, mode)}\n"
            + model_line
            + "**下一句话就是它的第一句**，直接打字发过来（10 分钟内有效）。开好之后就进对话模式，接着说都发给它。",
            [_do_button("newcancel")],
        )

    # ---------- 对话模式：那一次权限确认 ----------

    def _on_new_click(self, event, clicker: str, value: dict):
        """「➕ 新建会话」卡上的按钮。闸门：主人 + 白名单形状 + 服务端记的序号（目录不在卡上）。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.newsession import resolve_click

        def toast(kind: str, content: str, result: str):
            logger.info("新建会话 点击 op=%s 结果=%s", str(value.get("op") or "")[:8], result)
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN], "拒绝:不是主人")
        click = resolve_click(value)
        if click is None:
            return toast("error", _CARD_TOASTS[ApprovalStore.MALFORMED], "拒绝:形状不对")
        chat_id, scope = self._click_scope(event, clicker)
        if not chat_id:
            return toast("error", "找不到要回复的对话，直接发「新建会话」就好", "拒绝:找不到chat")
        if not scope:
            self._unknown_chat_notice(chat_id)
            return toast("error", "服务刚重启过，先在这个聊天里发一句「新建会话」", "拒绝:scope对不上")
        if click["op"] == "tool":
            project = getattr(self, "_new_projects", {}).pop(scope, "")
            if project and self.policy.resolve_workspace(project):
                # 刚用 `/ns project new` 建的项目 ⇒ 直接用它，不再让他选一遍目录
                self._in_background(self._arm_new_session, scope, chat_id, click["tool"], project)
                return toast("info", "好，用刚建的项目，说第一句吧", "已选好:新项目")
            self._in_background(self._send_new_dir_card, scope, chat_id, click["tool"])
            return toast("info", "收到，选一下项目", "已排队:选项目")
        tool, cwd = self._newsession_state().take(scope, click["n"], click["i"])
        if tool is None:
            return toast("error", cwd, "拒绝:序号对不上")
        if not self.policy.resolve_workspace(cwd):
            return toast("error", "这个项目现在不在手机能用的目录里了", "拒绝:目录越界")
        self._in_background(self._arm_new_session, scope, chat_id, tool, cwd)
        return toast("info", "好，说第一句吧", "已选好")

    async def _maybe_start_new_session(self, scope: str, chat_id: str, text: str) -> bool:
        """选好了工具和项目 ⇒ 这一句是新会话的第一句。开成了就进对话模式。没在新建 ⇒ False。"""
        from .fleet.newsession import INFLIGHT_ID
        from .fleet.scan import TOOL_LABELS

        state = self._newsession_state()
        armed, expired = state.peek(scope)
        if armed is None:
            if expired:
                self._say_with_buttons(chat_id, "刚才选的「新建会话」超过 10 分钟了，这句就按平常处理。"
                                                "要新建的话再点一次。", [_do_button("newsession")])
            return False
        control = getattr(self, "fleet", None)
        if control is None:
            state.disarm(scope)
            return False
        inflight = self._fleet_inflight
        if scope in inflight:
            self._say(chat_id, "上一条还在发，等它回来再说。")
            return True
        if scope in getattr(self, "_paused", {}):
            self._say_with_buttons(chat_id, f"⏸ 这个对话已暂停：{self._paused[scope]}\n确认没问题的话点「继续」。",
                                   [_do_button("resume", primary=True), _do_button("newcancel")])
            return True
        policy = self.policy
        reviewer = getattr(self, "reviewer", None)
        if reviewer is not None:
            decision = reviewer.review_request(ReviewContext(text=text, source="human", mode=policy.mode))
            if decision.verdict is Verdict.DENY:
                self._say(chat_id, f"⛔️ {decision.reason}")
                return True
        tool, cwd = armed["tool"], armed["cwd"]
        state.disarm(scope)
        label = TOOL_LABELS.get(tool, tool)
        inflight[scope] = (tool, INFLIGHT_ID)
        logger.info("新建会话 发出 tool=%s 字数=%d", tool, len(text))
        self._say(chat_id, f"📨 正在新开 {label} 会话，等它回…")
        settings = getattr(self, "settings", None)
        wait_card = _FleetWaitCard(
            self.replier, chat_id, f"新的 {label} 会话", can_stop=False,
            first_delay=getattr(settings, "progress_first_notice_sec", 90),
            escalate_sec=getattr(settings, "progress_escalate_sec", 0),
            limit_sec=float(getattr(policy, "turn_timeout_sec", 600) or 600),
        )
        wait_stop = asyncio.Event()
        wait_task = asyncio.create_task(wait_card.run(wait_stop))
        result = None
        try:
            approval_dir = (self._fleet_approval_dir(tool, "new-" + scope, policy)
                            if tool in ("claude", "codex") else "")
            base = self._effective_model(tool, "")
            start_model = armed.get("model", "") or base.model
            start_effort = armed.get("effort", "") or (base.effort if start_model == base.model else "")
            result = await asyncio.to_thread(
                control.start, tool, cwd, text, policy=policy, approval_dir=approval_dir,
                timeout=float(getattr(policy, "turn_timeout_sec", 600) or 600),
                model=start_model, effort=start_effort,
                **({"fork_from": armed["fork_from"]} if armed.get("fork_from") else {}),
            )
        finally:
            inflight.pop(scope, None)
            wait_stop.set()
            try:
                await wait_task
                await wait_card.finish(result)
            except Exception:  # noqa: BLE001
                logger.debug("等待卡收尾失败", exc_info=True)
        logger.info("新建会话 结果 ok=%s tool=%s 会话=%s", result.ok, tool, str(result.session_id)[:8] or "-")
        if not result.session_id:
            self._say_with_buttons(chat_id, f"❌ 没开成：{result.text}",
                                   [_do_button("newsession", "➕ 再试一次"), _do_button("sessions")])
            return True
        # 🔴 新会话是主人自己从手机开的，权限说明在选项目时已经给过 ⇒ 记成确认（和建群那条同一套记法），
        # 之后会话改名 / 换项目 / 在电脑上打开，照旧会再问。
        clicker = scope[3:] if scope.startswith("dm:") else ""
        mp = getattr(self, "model_prefs", None)
        if mp is not None and (armed.get("model") or armed.get("effort")):
            mp.set_session(tool, result.session_id, model=armed.get("model") or None,
                           effort=armed.get("effort") or None)
        if not hasattr(self, "_new_session_cwd"):
            self._new_session_cwd = {}
        self._new_session_cwd[result.session_id] = cwd
        group_bound = await self._open_session_group(chat_id, scope, clicker, tool, result.session_id)
        target_scope = group_bound.scope if group_bound is not None else scope
        target_chat_id = group_bound.chat_id if group_bound is not None else chat_id
        target = control.set_pending(target_scope, tool, result.session_id)
        self._sync_talk_marker(target_scope)
        from .fleet.cards import TALKING_PREFIX, TALKING_SUFFIX
        from .reply import normalize as _md_normalize

        tail = f"\n\n{TALKING_PREFIX} {target.label} {TALKING_SUFFIX}"
        if result.ok:
            body = _md_normalize(result.text or "")
            await self._send_reply_maybe_doc(target_chat_id, f"🆕 {target.label}", body, tail)
            choice = self._choice_buttons(target_scope, target_chat_id, tool, result.session_id,
                                          result.text or "")
            if choice:
                self._send_buttons(target_chat_id, choice)
        else:
            self._say(target_chat_id, f"⚠️ 会话已经开了，但这一句没正常回完：{result.text}{tail}")
        self._send_buttons(target_chat_id, self._talk_buttons(target.tool, target.id))
        if tool in ("claude", "codex"):
            self._fleet_ask_approval(target_scope, target_chat_id, tool, result.session_id,
                                     f"fleet:{tool}:new-{scope}")
        return True

    # ---------- 会话里的审批卡（FC-06 / FC-13，§5.3） ----------
    #
    # 会话（Claude / Codex）跑到要批准的一步 ⇒ 钩子在 `fleet:<tool>:<sid>` 目录落待办 ⇒
    # 这一轮结束后在会话所在的聊天里出审批卡并 @ 主人。点同意 ⇒ 写 .ok，所有卡都处理完后
    # 按原会话续跑一轮（批准目录这一次不清，钩子兑换 .ok 放行）；点拒绝 ⇒ 清掉，不续跑。

    FLEET_CONTINUE = {
        "claude": "继续",
        "codex": "主人已在飞书批准了刚才被拦下的那一步。请原样重新执行同一条命令（一个字都不要改），然后继续完成任务。",
    }

    @staticmethod
    def _turn_progress(control, tool: str, sid: str):
        """这一轮进展的读取函数：发之前记下会话记录的字节数，之后每次只读新增那段（FC-14）。"""
        from .fleet.progress import mark, read_progress
        from .redact import redact

        try:
            start_path = control.record_path(tool, sid)
        except Exception:  # noqa: BLE001
            return None
        start = {"path": start_path, "offset": mark(start_path)}

        def read() -> list:
            path = control.record_path(tool, sid) or start["path"]
            offset = start["offset"] if path == start["path"] else 0
            return read_progress(path, offset).lines(lambda t: redact(t)[0])

        return read

    async def _send_reply_maybe_doc(self, chat_id: str, label: str, body: str, tail: str) -> None:
        """FC-10：会话回复超过 1500 字 ⇒ 全文进飞书文档，群里只发摘要和链接。建文档失败 ⇒ 照旧分段发原文。"""
        from .fleet import longreply

        if longreply.is_long(body):
            try:
                url = await asyncio.to_thread(self._publish_long_reply, label, body)
                self.replier.send(chat_id, longreply.group_text(label, body, url) + tail)
                logger.info("长回复 已转文档 字数=%d", len(body))
                return
            except Exception as exc:  # noqa: BLE001 —— 文档建不出来也不能丢回复
                logger.warning("长回复 转文档失败，改为分段发原文：%s", _redact_text(str(exc)))
        self.replier.send(chat_id, f"{label} 回复：\n\n{body}{tail}")

    def _publish_long_reply(self, label: str, body: str) -> str:
        from . import botmenu as bm
        from .fleet import longreply
        from .redact import redact

        token = bm.tenant_token(self.settings.app_id, self.settings.app_secret)
        owner = next(iter(sorted(self.settings.owner_open_ids)), "")
        clean, _hits = redact(body)
        return longreply.publish(bm._http, token, longreply.title_for(label.lstrip("🖥🆕 "), clean), clean, owner)

    def _choice_buttons(self, scope: str, chat_id: str, tool: str, sid: str, text: str) -> list:
        """回复结尾是「选一个」⇒ 每个选项一个按钮（FC-07）。选项文字记在服务端，按钮只带序号。"""
        from .fleet.actions import _button
        from .fleet.choices import choice_value, extract_choices

        options = extract_choices(text)
        if not options or not sid:
            return []
        import secrets

        nonce = secrets.token_hex(6)
        if not hasattr(self, "_choice_offers"):
            self._choice_offers = {}
        self._choice_offers[scope] = {"n": nonce, "options": options, "tool": tool, "sid": sid,
                                      "chat_id": chat_id}
        from .redact import redact

        return [_button(f"👉 {redact(label)[0]}", choice_value(nonce, i), primary=(i == 0))
                for i, label in enumerate(options)]

    def _on_choice_click(self, event, clicker: str, value: dict):
        """点了回复下面的选项按钮 ⇒ 把那一项当成主人的回答发回同一个会话。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.choices import resolve_choice_click

        def toast(kind: str, content: str, result: str):
            logger.info("选项按钮点击 结果=%s", result)
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN], "拒绝:不是主人")
        nonce, index = resolve_choice_click(value)
        if not nonce:
            return toast("error", _CARD_TOASTS[ApprovalStore.MALFORMED], "拒绝:形状不对")
        offers = getattr(self, "_choice_offers", {})
        found = next(((scope, o) for scope, o in offers.items() if o["n"] == nonce), None)
        if found is None or index >= len(found[1]["options"]):
            return toast("info", "这组选项已经过期了（之后又有新回复），直接打字回答就行", "拒绝:过期")
        scope, offer = found
        offers.pop(scope, None)
        answer = offer["options"][index]
        control = getattr(self, "fleet", None)
        if control is None or self._loop is None:
            return toast("error", "我这边还没准备好，直接打字回答就行", "拒绝:没起来")
        control.set_pending(scope, offer["tool"], offer["sid"])
        asyncio.run_coroutine_threadsafe(
            self._maybe_send_to_fleet(scope, offer["chat_id"], answer), self._loop)
        return toast("info", f"好，回答：{answer[:20]}", "发出")

    def _fleet_continue_text(self, tool: str, commands: list) -> str:
        """批准后续跑那一句。Codex 没有 defer，要把批过的命令原样告诉它重跑（钩子按指纹放行一次）。"""
        base = self.FLEET_CONTINUE.get(tool, "继续")
        if tool != "codex" or not commands:
            return base
        lines = "\n".join(f"- {c}" for c in commands[:5])
        return base + "\n批准的命令（审批钩子这一次会放行，原样执行即可）：\n" + lines

    def _fleet_ask_approval(self, chat_scope: str, chat_id: str, tool: str, sid: str,
                            fleet_scope: str) -> int:
        """这一轮留下的待批操作 ⇒ 在聊天里出审批卡并 @ 主人。返回发出的卡数。"""
        gate = getattr(self, "gate", None)
        if gate is None or not sid:
            return 0
        try:
            pending = gate.take_pending(fleet_scope)
        except Exception:  # noqa: BLE001
            logger.exception("取会话待批操作失败")
            return 0
        if not pending:
            return 0
        if fleet_scope != f"fleet:{tool}:{sid}":
            # 新建会话那一轮的批准目录按聊天分；续跑走会话自己的目录 ⇒ 待办挪过去
            fleet_scope = self._move_fleet_pending(fleet_scope, f"fleet:{tool}:{sid}", pending)
        if not hasattr(self, "_fleet_awaiting"):
            self._fleet_awaiting = {}
        commands = [str((item.get("tool_input") or {}).get("command") or "") for item in pending]
        self._fleet_awaiting[fleet_scope] = {"chat_id": chat_id, "chat_scope": chat_scope,
                                             "tool": tool, "sid": sid,
                                             "commands": [c for c in commands if c]}
        from .fleet.escalate import at_owner_line

        owners = getattr(getattr(self, "settings", None), "owner_open_ids", None) or ()
        at = at_owner_line(owners)
        label = {"claude": "Claude", "codex": "Codex"}.get(tool, tool)
        note = f"🔒 {label} 要做 {len(pending)} 步需要你批准的操作，下面审批卡点同意后它接着做。"
        sender = getattr(self.replier, "send_md_id", None)
        sent_note = False
        if at and sender is not None:
            try:
                sent_note = bool(sender(chat_id, f"{at} {note}"))
            except Exception:  # noqa: BLE001
                sent_note = False
        if not sent_note:
            self._say(chat_id, note)
        sent = 0
        for item in pending:
            action = describe_action(item.get("tool_name", "?"), item.get("tool_input", {}))
            token = self.approvals.issue(fleet_scope, item["digest"],
                                         {"digest": item["digest"], "scope": fleet_scope})
            ok = self.replier.send_approval_card(
                chat_id, f"{label} 要动手了，先问你一下", action,
                {"act": "approve", **token.to_value()}, {"act": "deny", **token.to_value()},
            )
            logger.info("会话审批卡 scope=%s tool=%s digest=%s 送达=%s", fleet_scope[:24],
                        item.get("tool_name", "?"), str(item.get("digest", ""))[:8], bool(ok))
            sent += 1 if ok else 0
        if not sent:
            self._say(chat_id, "🔒 审批卡发不出去，这一步没做。你可以到电脑上处理，或者重新发一次要求。")
            self._fleet_awaiting.pop(fleet_scope, None)
            gate.clear(fleet_scope)
        return sent

    def _move_fleet_pending(self, src: str, dst: str, pending: list) -> str:
        """把新建那一轮的待办（`.asked`）挪到会话自己的批准目录。挪不动就留在原处用原 scope。"""
        gate = self.gate
        try:
            src_dir, dst_dir = gate.dir_for(src), gate.dir_for(dst)
            for item in pending:
                name = str(item["digest"]) + ".asked"
                os.replace(os.path.join(src_dir, name), os.path.join(dst_dir, name))
            return dst
        except OSError:
            logger.exception("挪新会话的待批操作失败，按原目录处理")
            return src

    def _on_fleet_approval_click(self, value: dict, scope: str, digest: str):
        """会话审批卡被点了（鉴权、令牌已在上层核过）。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        awaiting = getattr(self, "_fleet_awaiting", {})
        waiting = awaiting.get(scope)
        if value.get("act") == "deny":
            self.gate.clear(scope)
            awaiting.pop(scope, None)
            logger.info("会话审批 拒绝 digest=%s scope=%s", str(digest)[:8], scope[:24])
            if waiting:
                self._say_later(waiting["chat_id"], "好，这一步不做。会话还在，可以接着跟它说。")
            return P2CardActionTriggerResponse(
                {"toast": {"type": "info", "content": "好，不做这一步"}, **self._settled("denied")})
        if not self.gate.approve(scope, digest):
            reason = getattr(self.gate, "last_block_reason", "")
            done = not reason and self.gate.approved_recently(scope, digest)
            logger.info("会话审批 同意没生效 digest=%s 原因=%s", str(digest)[:8],
                        "红线" if reason else ("刚批过" if done else "没有未决项"))
            if done:
                return P2CardActionTriggerResponse(
                    {"toast": {"type": "info", "content": "这一步已经批过了，正在做"},
                     **self._settled("approved")})
            content = (f"这一步不能做：{reason}" if reason
                       else "这一步已经不在等批准了，重新发一次你的要求")
            return P2CardActionTriggerResponse({"toast": {"type": "error", "content": content}})
        logger.info("会话审批 同意已记下 digest=%s scope=%s", str(digest)[:8], scope[:24])
        if waiting and not self._has_waiting_approval(scope) and self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._fleet_continue_after_approval(scope), self._loop)
            toast = "好，继续"
        elif waiting:
            toast = "记下了，其余几张也点完它就接着做"
        else:
            toast = "同意记下了；那一轮接不上了，10 分钟内再发一次要求，这一步不会再问"
        return P2CardActionTriggerResponse(
            {"toast": {"type": "info", "content": toast}, **self._settled("approved")})

    async def _fleet_continue_after_approval(self, scope: str) -> None:
        """全部批完 ⇒ 让原会话接着跑一轮（这一次不清批准目录）。"""
        waiting = getattr(self, "_fleet_awaiting", {}).pop(scope, None)
        control = getattr(self, "fleet", None)
        if waiting is None or control is None:
            return
        if not hasattr(self, "_fleet_keep_grants"):
            self._fleet_keep_grants = set()
        self._fleet_keep_grants.add(scope)
        tool, sid = waiting["tool"], waiting["sid"]
        try:
            control.set_pending(waiting["chat_scope"], tool, sid)
            await self._maybe_send_to_fleet(waiting["chat_scope"], waiting["chat_id"],
                                            self._fleet_continue_text(tool, waiting.get("commands") or []))
        except Exception:  # noqa: BLE001
            logger.exception("会话批准后续跑失败 scope=%s", scope[:24])
            self._say(waiting["chat_id"], "同意收到了，但接着跑的时候出错了。重新发一次你的要求。")
        finally:
            self._fleet_keep_grants.discard(scope)

    def _relay_ident(self, tool: str, sid: str) -> str:
        """会话现在的身份指纹（名字 + 目录）。确认绑在它上面 —— 改了名、换了项目就重新问。"""
        from .fleet.consent import session_ident

        sess = None
        control = getattr(self, "fleet", None)
        if control is not None:
            try:
                sess = control.cached(tool, sid)
            except Exception:  # noqa: BLE001
                sess = None
        if sess is None:
            return ""
        return session_ident(getattr(sess, "title", ""), getattr(sess, "cwd", ""),
                             background=self._runs_in_background(sess))

    @staticmethod
    def _runs_in_background(sess) -> bool:
        """这个会话的下一句会走「后台接着跑」那条路吗（电脑上没开着的 Claude 会话）。

        🔴 2026-11-06（角色 F）：开着 ⇒ 话送进电脑上那个会话，用**它在电脑上那套权限**；
        关掉了（jsonl 还在）⇒ 桥起 `claude -p --resume` 接着跑，用**手机这边的档位**。
        选路在 `control.send` 里按现查的结果走，用户在聊天里感觉不到换了路 ——
        所以这一位要进确认的指纹：路变了，他确认过的就不是这回事了。
        Codex 跟随当前权限档位；Cursor 保持自身的权限映射。开没开着不改变权限规则，不算会话身份变化。
        """
        return getattr(sess, "tool", "") == "claude" and getattr(sess, "owner", "") == "none"

    def _remember_talk_ident(self, scope: str, tool: str, sid: str, ident: str) -> None:
        """记下「这个聊天现在是按哪个样子的会话确认的」（内存）。群里那条捷径靠它复查。"""
        if not hasattr(self, "_talk_idents"):
            self._talk_idents = {}
        self._talk_idents[scope] = (tool, sid, ident)

    def _talk_ident_same(self, scope: str, tool: str, sid: str) -> bool:
        """会话现在的样子（名字 / 项目 / 开没开着）还和确认时一样吗。没记过 ⇒ 当成不一样（再查确认记录）。"""
        got = getattr(self, "_talk_idents", {}).get(scope)
        if got is None or (got[0], got[1]) != (tool, sid):
            return False
        return got[2] == self._relay_ident(tool, sid)

    def _consented_ident(self, scope: str, tool: str, sid: str) -> str:
        """点「确认」时要记下的样子 = **弹卡那一刻**的样子，不是点的这一刻的。

        卡是关着的时候弹的（写的是「权限按手机这边的」），点之前终端里又打开了 ——
        他同意的是卡上那一套。按点的这一刻记，就等于替他同意了一张他没看过的卡。
        """
        shown = getattr(self, "_consent_shown", {}).pop(scope, None)
        if shown is not None and (shown[0], shown[1]) == (tool, sid) and shown[2]:
            return shown[2]
        return self._relay_ident(tool, sid)

    def _talk_changed_why(self, scope: str, tool: str, sid: str) -> str:
        """为什么又要确认（大白话，半句）。看不出来 / 第一次 ⇒ 空串（走「还没确认过」那句）。"""
        from .fleet.consent import split_ident
        from .fleet.model import short_title

        before = None
        got = getattr(self, "_talk_idents", {}).get(scope)
        if got is not None and (got[0], got[1]) == (tool, sid):
            before = got[2]
        if before is None:
            store = getattr(self, "relay_consent", None)
            try:
                before = store.latest_ident(scope, tool, sid) if store is not None else None
            except Exception:  # noqa: BLE001 —— 只影响一句解释
                before = None
        now = self._relay_ident(tool, sid)
        if not before or not now or before == now:
            return ""
        old_name, old_cwd, old_bg = split_ident(before)
        new_name, new_cwd, new_bg = split_ident(now)
        if new_bg and not old_bg:
            mode = MODE_LABELS.get(self.policy.mode, self.policy.mode)
            return f"电脑上那个会话已经关了，接下来要在后台接着跑，权限按手机这边的「{mode}」，和刚才不一样"
        if old_bg and not new_bg:
            return "电脑上又打开了那个会话，接下来直接进它那边，用的是你在电脑上给它的权限，和刚才不一样"
        if old_name != new_name:
            old = short_title(_redact_text(old_name), 24) if old_name else ""
            return f"它在电脑上改了名（原来叫「{old}」）" if old else "它在电脑上改了名"
        if old_cwd != new_cwd:
            return "它在电脑上换了一个项目"
        return ""

    def _relay_granted(self, scope: str, tool: str, sid: str) -> bool:
        """这个聊天在这个会话上点过确认、还在 24 小时内吗？

        记录起不来（磁盘满、目录没权限）⇒ 当成**没确认过**，每次都问一次。
        方向是安全的：多问一次只是啰嗦，少问一次是用户没同意就让远端会话动了手。
        """
        store = getattr(self, "relay_consent", None)
        if store is None:
            return False
        try:
            return bool(store.granted(scope, tool, sid, self._relay_ident(tool, sid)))
        except Exception:  # noqa: BLE001
            logger.exception("读对话模式的确认记录失败，再问一次")
            return False

    def _ask_relay_consent(self, chat_id: str, scope: str, tool: str, sid: str) -> None:
        """弹那张确认卡。卡片发不出去就退成文字 + 一条能打的命令（不能变成死路）。"""
        from .fleet.cards import build_confirm_relay_card, session_label

        sess = self.fleet.cached(tool, sid) if getattr(self, "fleet", None) is not None else None
        label = session_label(sess, _redact_text) if sess is not None else f"{tool} 会话"
        # 🔴 群里要用**群专用**那张卡（2026-11-05 体验审查：原来群里也发单聊那张，
        # 「群里每一句都会被转进去」「别拉别人进来」这两句群里才要紧的话根本没出现）。
        in_group = str(scope or "").startswith("group:")
        # 记下**这张卡是按什么样子的会话弹的**：点确认时按它记（见 `_consented_ident`）
        if not hasattr(self, "_consent_shown"):
            self._consent_shown = {}
        self._consent_shown[scope] = (tool, sid, self._relay_ident(tool, sid))
        card = ""
        # 🔴 电脑上没开着 ⇒ 走「后台接着跑」，权限是手机这边的。那两张卡写的都是
        # 「它的权限就是你在电脑上给它的那套」—— 对这条路是假话（2026-11-06 角色 F）。
        background = sess is not None and self._runs_in_background(sess)
        if background and not getattr(sess, "can_send", True) and not getattr(sess, "can_relay", False):
            # 🔴 第 14 轮复审（review-r13 🔴3）：App 关掉后改走后台，要过目录白名单 ——
            # 原来照样弹「确认，接着跑」，点了才被拒。现在先说清为什么，不弹一张点了必失败的卡。
            from .fleet.actions import do_button

            why = getattr(sess, "why_not_send", "") or "现在没法在后台接着跑它。"
            self._say_with_buttons(chat_id, f"{label}在电脑上已经关了，要在后台接着跑，但{why}",
                                   [do_button("dirs", "📂 换项目"), do_button("sessions", "🖥 回到总览")])
            return
        if background:
            card = self._build_background_consent_card(sess, in_group)
        elif sess is not None:
            if in_group:
                try:
                    from .fleet.groupsetup import build_group_consent_card

                    # 🔴 「群里每一句都直达」这句话取决于有没有 `im:message.group_msg`。
                    # 没开的话飞书只推 @ 了机器人的消息 ⇒ 照说，主人说一句没人理
                    # （2026-11-05 体验审查）。体检结果带缓存，不会每次建群都打一遍飞书。
                    card = build_group_consent_card(sess, redact=_redact_text,
                                                    report=self._group_perm_report())
                except Exception:  # noqa: BLE001 —— 漏装 groupsetup 时退回单聊那张
                    logger.exception("群确认卡没组出来，退回单聊那张")
            if not card:
                card = build_confirm_relay_card(sess, redact=_redact_text)
        # 确认前在群里说过话 ⇒ 卡上引用原话、明说确认后会发过去（他点确认时看得到要发的是什么）
        note = self._held_note(scope, tool, sid)
        card = self._with_held_note(card, note)
        extra ="这个群里你说的每一句都会进去；群里其他人也能看到它的回复，别把不相关的人拉进来。\n" if in_group else ""
        if background:
            mode = MODE_LABELS.get(self.policy.mode, self.policy.mode)
            fallback = (
                f"{label}在电脑上没开着。接下来你说的话会在后台接着这个会话跑，"
                f"权限按**手机这边的「{mode}」**（不是你在电脑上给它的那套）。\n"
                + extra +
                "卡片发不出去：确认的话回一句 `/talk!`，不确认什么都不用做。"
            )
        else:
            fallback = (
                f"要直接跟{label}说话：你之后说的每一句都进电脑上那个会话，"
                "手机和终端共用当前权限档位；当前默认是全授权，需要收紧时请切到「只读」。\n"
                + extra +
                "卡片发不出去：确认的话回一句 `/talk!`，不确认什么都不用做。"
            )
        if note:
            fallback += "\n" + note
        # 每张卡带一个序号：只有**最新那张**的「确认」算数（第 9 轮复审 #2，见 `_stamp_consent_card`）
        card = self._stamp_consent_card(scope, card)
        # 卡片没组出来（缓存里没有这个会话）就只发文字版，一样能确认
        if not hasattr(self, "_pending_consent"):
            self._pending_consent = {}
        self._pending_consent[scope] = (tool, sid)
        if not card:
            self._say_later(chat_id, fallback)
            return

        def go() -> None:
            self._send_card_or_text(chat_id, card, fallback)

        loop = getattr(self, "_loop", None)
        if loop is None or not hasattr(loop, "call_soon_threadsafe"):
            go()
        else:
            loop.call_soon_threadsafe(lambda: loop.run_in_executor(None, go))

    def _stamp_consent_card(self, scope: str, card: str) -> str:
        """给这张确认卡的按钮盖一个序号（`n`），记下「这个聊天现在算数的是哪一张」。

        🔴 2026-11-06 第 9 轮复审 #2：群里先说一句（卡 1 引用它）、再补一句（卡 2 引用两句）⇒
        两张卡按钮值一模一样，往回翻点卡 1 发出了**卡 1 上没写**的那句。
        修法：每弹一张就换一个序号，**只有最新那张的「确认」算数**；点旧卡 ⇒ 说「这张过时了」、
        在最下面再弹一张最新的（上面列着点确认后会发的全部）。每记一句话就会弹一张新卡，
        所以最新那张上写的永远就是会发出去的全部。

        没盖成（卡片 JSON 解析不了）⇒ 原样返回，点它的时候按「没带序号」那条判。
        """
        import secrets

        if not hasattr(self, "_consent_cards"):
            self._consent_cards = {}
        # 上一张还在等（没点、没取消）⇒ 这一轮已经弹过几张了；否则从 1 数起
        prev = self._consent_cards.get(scope) if scope in getattr(self, "_pending_consent", {}) else None
        nonce = secrets.token_hex(6)
        self._consent_cards[scope] = (nonce, (prev[1] + 1) if prev else 1)
        if not card:
            return card
        try:
            data = json.loads(card)

            def walk(node) -> None:
                if isinstance(node, dict):
                    value = node.get("value")
                    if isinstance(value, dict) and value.get("act") == "relay":
                        value["n"] = nonce
                    for child in node.values():
                        walk(child)
                elif isinstance(node, list):
                    for child in node:
                        walk(child)

            walk(data)
            return json.dumps(data, ensure_ascii=False)
        except Exception:  # noqa: BLE001 —— 盖不上就发原卡（按「没带序号」判，不会放宽）
            logger.exception("确认卡没盖上序号")
            return card

    def _consent_card_current(self, scope: str, given) -> bool:
        """点的是不是这个聊天**最新**弹出的那张确认卡。

        没带序号的点击（老卡 / 盖章失败）：这一轮只弹过一张 ⇒ 就是它；弹过好几张 ⇒ 分不出点的是哪张，不猜。
        """
        got = getattr(self, "_consent_cards", {}).get(scope)
        if got is None:
            return True  # 没记过盖章（卡是走文字兜底发的，或者测试里手搭的）⇒ 只有 `_pending_consent` 那道闸
        nonce, issued = got
        if given is None:
            return issued <= 1
        return isinstance(given, str) and given == nonce

    def _build_background_consent_card(self, sess, in_group: bool) -> str:
        """电脑上没开着的会话：确认卡写**手机这边的权限**（后台接着跑那条路）。

        按钮值和另外两张一样（`relay_value`），回调走同一道闸 `resolve_relay_click`。
        组不出来 ⇒ 空串，调用方退回文字版（同样能确认）。
        """
        try:
            from .fleet.cards import relay_value, session_label
            from .reply import escape_card

            label = escape_card(session_label(sess, _redact_text))
            tool = str(getattr(sess, "tool", "") or "")
            sid = str(getattr(sess, "id", "") or "")
            policy = self.policy
            mode = MODE_LABELS.get(policy.mode, policy.mode)
            where = "你在这个群里说的每一句" if in_group else "接下来你说的每一句"
            lines = [
                f"**{label}** 在电脑上没开着（关掉了，对话记录还在）。",
                f"{where}会在后台接着这个会话跑，由它去做：",
                f"· 权限按**手机这边的「{mode}」**：{escape_card(policy.describe())}",
                "· 不是你在电脑上给它的那套；之后你在电脑上重新打开它，会再问你一次",
                "· 跑完把回复转回这里",
            ]
            if in_group:
                lines.append("· 群里的人都能看到它回的内容 ⇒ **别拉别人进来**")

            def button(text: str, op: str, primary: bool = False) -> dict:
                return {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": text},
                    "type": "primary" if primary else "default",
                    "width": "fill",
                    "size": "medium",
                    "behaviors": [{"type": "callback", "value": relay_value(op, tool, sid)}],
                }

            elements = [
                {"tag": "markdown", "content": "\n".join(lines)},
                {
                    "tag": "column_set",
                    "horizontal_spacing": "8px",
                    "columns": [
                        {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]}
                        for b in (button("确认，接着跑", "talk", primary=True), button("算了", "no"))
                    ],
                },
                {"tag": "markdown", "text_size": "notation",
                 "content": "点「算了」或者不确认，你说的话都不会发过去。"},
            ]
            return json.dumps(
                {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": {"title": {"tag": "plain_text", "content": "⚠️ 电脑上那个会话关了"},
                               "template": "orange"},
                    "body": {"elements": elements},
                },
                ensure_ascii=False,
            )
        except Exception:  # noqa: BLE001 —— 组不出来就退文字版，不能让确认变成死路
            logger.exception("后台接着跑那张确认卡没组出来，退成文字")
            return ""

    def _rights_line(self, tool: str, sid: str) -> str:
        """说明里「它按哪套权限干活」那一行 —— 🔴 **按真实走的那条路说**，三种情况说法不同：

        - Claude 电脑上开着 ⇒ 话送进那个会话，用**它在电脑上那套权限**（多半不问就改）
        - Claude 电脑上关着 ⇒ 后台接着跑，用**手机这边的档位**（`_runs_in_background`，F 那套）
        - Claude / Codex ⇒ 跟随当前权限档位（trusted 可写，write 限定工作区，readonly 只读）
        写死一句「按电脑上那套」在后两种情况都是错的，而且错在「以为它能改」那个方向反了也不安全。
        """
        sess = None
        try:
            sess = self.fleet.cached(tool, sid)
        except Exception:  # noqa: BLE001
            sess = None
        if tool == "codex":
            mode = MODE_LABELS.get(getattr(self.policy, "mode", "trusted"), "全授权")
            return f"· 按当前权限档位 **{mode}** 处理，手机和终端一致\n"
        if sess is not None and self._runs_in_background(sess):
            return "· 电脑上这个会话现在关着，会在后台接着跑，权限按当前档位处理\n"
        mode = MODE_LABELS.get(getattr(self.policy, "mode", "trusted"), "全授权")
        return f"· 手机和终端共用当前权限档位（当前：{mode}）\n"

    def _grant_by_open_click(self, scope: str, tool: str, sid: str) -> None:
        """把「主人在单聊点了接着聊 → 建群」这一下当成确认。走**和点「确认」完全一样**的记法。

        和 `_on_relay_click` 确认分支同一套三步（记身份 → 落盘 24h → 摆目标），不另写一份：
        - `_relay_ident`：按**现在**的名字 + 目录 + 开没开着记。之后变了 ⇒ `_talk_ident_same` 不成立 ⇒ 群里重新问（F 那套照旧）
        - `relay_consent.grant`：落盘，重启后照样认
        存不下来 ⇒ 下一句会弹确认卡（往「多问一次」那边走，安全）。
        """
        ident = self._relay_ident(tool, sid)
        self._remember_talk_ident(scope, tool, sid, ident)
        store = getattr(self, "relay_consent", None)
        if store is None:
            return
        try:
            store.grant(scope, tool, sid, ident)
            logger.info("建群即进对话：点接着聊记成确认 tool=%s 会话=%s", tool, str(sid)[:8])
        except Exception:  # noqa: BLE001
            logger.exception("建群后自动确认没存下来，这个群第一句会再问一次")

    def _group_msg_proof_path(self) -> str:
        # 和审批目录 / paused.json 同一套取法（`__init__` 里 `home = …`）：config_path 是相对的
        # 「config.toml」时 dirname 是空串 ⇒ 退回 ~/.carher-feishu-agent。原来没这层兜底 ⇒ 路径空、
        # 证明永远读不到 ⇒ 永远叫人 @（主会话 2026-11-06 拿部署那份代码模拟时撞上）。
        config_path = getattr(getattr(self, "settings", None), "config_path", "") or ""
        home = os.path.dirname(config_path) or os.path.expanduser("~/.carher-feishu-agent")
        return os.path.join(home, GROUP_MSG_PROOF_FILE)

    def _group_msg_proven(self) -> bool:
        """「不 @ 也收得到群消息」被现场证明过吗。读不到一律 False（话说保守一点，不影响功能）。"""
        if getattr(self, "_group_msg_proven_cache", False):
            return True
        path = self._group_msg_proof_path()
        ok = bool(path) and os.path.isfile(path)
        if ok:
            self._group_msg_proven_cache = True
        return ok

    def _mark_group_msg_proven(self) -> None:
        """记下「真收到过不 @ 的群消息」。只记一次；写不下去也不影响这条消息（它在消息入口上，3 秒纪律）。"""
        if getattr(self, "_group_msg_proven_cache", False):
            return
        self._group_msg_proven_cache = True
        path = self._group_msg_proof_path()
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                import json as _json_mod  # 本文件的 _json 只是另一个函数里的局部名（漏 import ⇒ NameError，py_compile 查不出）

                handle.write(_json_mod.dumps({"at": int(time.time())}))
            logger.info("收到过不 @ 机器人的群消息 ⇒ 群消息权限确认开着，之后文案按「直接说」")
        except OSError:
            logger.warning("群消息权限的证明标记写不下去，文案仍按保守那一支说")

    def _group_perm_report(self):
        """建群那套权限现在到位了吗（给群里的文案分叉用）。查不出来 ⇒ None（文案走保守那一支）。"""
        getter = getattr(getattr(self, "group_client", None), "report", None)
        if getter is None:
            return None
        try:
            return getter()
        except Exception:  # noqa: BLE001 —— 只影响措辞，不能让确认卡发不出去
            logger.exception("读建群权限体检失败，群里的文案按保守那一支走")
            return None

    def _enter_talk_text(self, target, scope: str = "") -> str:
        """进对话模式那一句。🔴 群里和单聊说法不一样：

        单聊里机器人收得到每一句 ⇒ 「直接打字」。群里收不收得到取决于有没有开「获取群组中所有消息」，
        要和确认卡**同一个判断**（`_group_how_to_talk` → `groupsetup.receive_granted`）——
        原来写死「直接打字发过来就行」，而确认卡刚叫他 @ ⇒ 照后一句做机器人一声不吭（2026-11-06 体验侦察）。
        """
        from .fleet.cards import TALKING_PREFIX, TALKING_SUFFIX

        if str(scope or "").startswith("group:"):
            return (
                f"{TALKING_PREFIX} **{target.label}** 说话。"
                f"{_group_how_to_talk(self._group_perm_report(), here=True, proven=self._group_msg_proven())}\n"
                "要退出就点「退出对话」。它停不了的那一轮只能在电脑上按 Esc。"
            )
        return (
            f"{TALKING_PREFIX} **{target.label}** {TALKING_SUFFIX}\n"
            "直接打字发过来就行。它停不了的那一轮只能在电脑上按 Esc。"
        )

    # ---------- 群里确认前说的话：先记着，确认后发 ----------

    def _bound_label(self, bound) -> str:
        """群里那几句提到「这个群直达谁」时用的名字。

        🔴 优先用**会话本身**（`session_label`）：`Bound.name` 是群名，带着「Claude·」前缀、
        还被截到群名上限 ⇒ 原来拼出「Claude「Claude · 198 litellm 5x…」」（2026-11-06 体验侦察）。
        拿不到会话（重启后还没扫到）才退回 `bound.label`（那边也剥了前缀）。
        """
        control = getattr(self, "fleet", None)
        sess = None
        if control is not None:
            try:
                sess = control.cached(bound.tool, bound.id)
            except Exception:  # noqa: BLE001 —— 只影响一个名字
                sess = None
        if sess is not None:
            from .fleet.cards import session_label

            return session_label(sess, _redact_text)
        return _redact_text(bound.label)

    def _hold_for_consent(self, scope: str, tool: str, sid: str, text: str) -> None:
        """还没确认权限时群里说的话：**先记着**（内存，不落盘），点确认后发过去。

        只记最近 `HELD_MAX` 句；换了目标会话 ⇒ 之前记的作废（那是对另一个会话说的）。
        重启就没了 —— 那时卡也作废了（`_pending_consent` 也只在内存），点卡会被要求重挑，不会错发。
        """
        if not str(text or "").strip():
            return
        if not hasattr(self, "_held"):
            self._held = {}
        got = self._held.get(scope)
        items = list(got[2]) if got is not None and (got[0], got[1]) == (tool, sid) else []
        items.append((time.time(), str(text)))
        self._held[scope] = (tool, sid, items[-HELD_MAX:])

    def _held_texts(self, scope: str, tool: str, sid: str) -> list:
        got = getattr(self, "_held", {}).get(scope)
        if got is None or (got[0], got[1]) != (tool, sid):
            return []
        return [text for _, text in got[2]]

    def _held_note(self, scope: str, tool: str, sid: str) -> str:
        """卡上那句「点确认之后会把你刚才那句『…』发过去」。没记着话 ⇒ 空串。"""
        return _held_line(self._held_texts(scope, tool, sid))

    def _drop_held(self, scope: str) -> int:
        """作废记着的话（说了取消 / 点了算了 / 解绑）。返回作废了几句（调用方据此照实说）。"""
        got = getattr(self, "_held", {}).pop(scope, None)
        return len(got[2]) if got is not None else 0

    def _release_held(self, scope: str, chat_id: str, tool: str, sid: str) -> tuple:
        """点了确认：取出记着的话 → `(要发的, 隔太久不发的)`。取了就清，**只发一次**。

        记的是别的会话 ⇒ 当成「不发」（他确认的是这一个）。
        """
        got = getattr(self, "_held", {}).pop(scope, None)
        if got is None:
            return [], []
        if (got[0], got[1]) != (tool, sid):
            # 那几句是对另一个会话说的（中间改绑过）⇒ 不发给这个；也不再提（他已经换了对象）
            logger.info("群里确认前的话 对象换了，不发 条数=%d", len(got[2]))
            return [], []
        now = time.time()
        fresh = [text for at, text in got[2] if now - at <= HELD_TTL_SEC]
        stale = [text for at, text in got[2] if now - at > HELD_TTL_SEC]
        logger.info("群里确认前的话 发=%d 隔太久不发=%d chat=%s", len(fresh), len(stale), str(chat_id)[:12])
        return fresh, stale

    def _send_held(self, scope: str, chat_id: str, target, held: tuple):
        """确认后把记着的话发出去 → 一个协程；没记着话 ⇒ None（调用方照旧发进入提示）。

        🔴 发走的是**正常管道** `self._handle`（身份复查、审查层、在飞、暂停全在里面），
        不另开一条直发的路。卡弹完之后会话改了名 ⇒ 那里会再记一次、再弹一次卡，不会错发。
        """
        fresh, stale = held if held else ([], [])
        if not fresh and not stale:
            return None

        async def go() -> None:
            from .fleet.model import short_title

            lines = [self._enter_talk_text(target, scope)]
            if stale:
                first = short_title(_redact_text(stale[0]), 30)
                # 隔太久 / 当时是对另一个会话说的 ⇒ 不替他发，照实说
                lines.append(f"你确认前说的「{first}」隔得久了，这次没发过去，还要的话再发一次。")
            self._say_with_buttons(chat_id, "\n".join(lines), self._talk_buttons(target.tool, target.id))
            if fresh:
                text = "\n".join(fresh)
                await self._handle(scope, chat_id, text)

        return go()

    def _spawn(self, coro) -> None:
        """从卡片回调线程（3 秒内要返回）把协程丢进事件循环。没起循环（测试）⇒ 当场跑完。"""
        loop = getattr(self, "_loop", None)
        if isinstance(loop, asyncio.AbstractEventLoop):
            fut = asyncio.run_coroutine_threadsafe(coro, loop)

            def done(f) -> None:
                exc = f.exception()
                if exc is not None:
                    logger.error("确认后补发失败", exc_info=(type(exc), exc, exc.__traceback__))

            fut.add_done_callback(done)
            return
        if loop is not None:
            coro.close()
            logger.warning("事件循环不可用，确认后补发没跑")
            return
        try:
            asyncio.run(coro)
        except RuntimeError:
            coro.close()
            logger.exception("确认后补发没跑起来")

    @staticmethod
    def _with_held_note(card: str, note: str) -> str:
        """把「点确认后会发你刚才那句」塞进确认卡正文第一段后面（按钮之前，点之前一定看得到）。"""
        if not card or not note:
            return card
        try:
            from .reply import escape_card

            data = json.loads(card)
            elements = data["body"]["elements"]
            elements.insert(1, {"tag": "markdown", "content": escape_card(note)})
            return json.dumps(data, ensure_ascii=False)
        except Exception:  # noqa: BLE001 —— 塞不进去就发原卡；进入提示里还会再说一次
            logger.exception("确认卡没塞进那句说明")
            return card

    async def _confirm_talk_by_text(self, scope: str, chat_id: str) -> None:
        """`/talk!`：用文字完成那一次确认。只认刚刚弹过卡的那个会话。"""
        pending = getattr(self, "_pending_consent", {}).pop(scope, None)
        control = getattr(self, "fleet", None)
        if pending is None or control is None:
            self._say_with_buttons(chat_id, "现在没有在等确认的会话。点「看看会话」挑一个，点 [接着聊] 就会问你。",
                                   [_do_button("sessions")])
            return
        tool, sid = pending
        if (tool, sid) not in control.known_keys():
            self._say_with_buttons(chat_id, "那个会话已经不在刚才的列表里了，点「回到总览」重新看一眼。",
                                   [_do_button("sessions", "🖥 回到总览")])
            return
        # 按**弹卡那一刻**的样子记（卡上写的是哪套权限，他同意的就是哪套）
        ident = self._consented_ident(scope, tool, sid)
        self._remember_talk_ident(scope, tool, sid, ident)
        store = getattr(self, "relay_consent", None)
        if store is not None:
            try:
                store.grant(scope, tool, sid, ident)
            except Exception:  # noqa: BLE001
                logger.exception("对话模式的确认没存下来")
        target = control.set_pending(scope, tool, sid)
        held = self._release_held(scope, chat_id, tool, sid)
        job = self._send_held(scope, chat_id, target, held)
        if job is not None:
            await job
            return
        self._say_with_buttons(chat_id, self._enter_talk_text(target, scope),
                               self._talk_buttons(target.tool, target.id))

    def _on_relay_click(self, event, clicker: str, value: dict):
        """进对话模式那张确认卡上的按钮。3 秒内必须返回 ⇒ 只记确认 + 记目标。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.cards import resolve_relay_click

        raw_op = str(value.get("op") or "")[:12]

        def toast(kind: str, content: str, result: str = ""):
            logger.info("对话确认点击 op=%s tool=%s 会话=%s 结果=%s", raw_op,
                        str(value.get("tool") or "")[:10], str(value.get("id") or "")[:8] or "-",
                        result or kind)
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN], "拒绝:不是主人")
        control = getattr(self, "fleet", None)
        if control is None:
            return toast("error", "会话总控这次没起来，重启一下服务再试", "拒绝:总控没起来")
        # 卡片序号 `n` 是 app.py 自己盖的（`_stamp_consent_card`）：在白名单之前摘掉，别的多余键照旧被拒
        stamp = value.get("n") if isinstance(value, dict) else None
        if stamp is not None and (not isinstance(stamp, str) or len(stamp) > 32):
            return toast("error", "这张卡片过期了，点「回到总览」重新看一眼", "拒绝:序号不对")
        value = {k: v for k, v in value.items() if k != "n"} if isinstance(value, dict) else value
        click = resolve_relay_click(value, control.known_keys())
        if click is None:
            # 重启后 `_known` 是空的 ⇒ 先落在这里，下面那条「失效了 + 按钮」走不到（同 _on_fleet_click）
            self._stale_card_notice(event, clicker)
            return toast("error", "这张卡片过期了，点「回到总览」重新看一眼", "拒绝:不在白名单或已过期")
        chat_id, scope = self._click_scope(event, clicker)
        if not scope:
            self._unknown_chat_notice(chat_id)
            return toast("error", "服务刚重启过，先在这个聊天里发一句「会话」再点", "拒绝:scope对不上")
        tool, sid = click["tool"], click["id"]
        # 🔴 **这张卡还在等吗**。2026-11-05 安全审查：用户说了「取消」之后，聊天记录里
        # 那张旧确认卡上的「确认，开始说话」照样能进对话模式，还顺手写下 24 小时免确认 ——
        # 他以为自己退出了，下一句直接进了那个「不问就改」的会话。文字通道
        # `_confirm_talk_by_text` 本来就是这么拒的，两条路要一致。
        # 🔴 2026-11-06 第 9 轮复审 #2：**点的是不是最新那张**。旧卡上的「确认」不算数 ——
        # 旧卡只引用了它弹出那一刻记着的话，之后又记了新的；按旧卡确认就会发出他没在那张卡上看到的话。
        # 「算了」不看这个（往「更少发生」走永远安全，旧卡上点也照样作废）。
        if click["op"] == "talk" and getattr(self, "_pending_consent", {}).get(scope) == (tool, sid) \
                and not self._consent_card_current(scope, stamp):
            logger.info("对话确认 点的是过时的卡 tool=%s 会话=%s", tool, sid[:8])
            self._say_later(chat_id, "你点的那张卡已经过时了（后面又说过话），这次没发。"
                                     "下面重新弹了一张，上面列着点确认之后会发出去的全部内容。")
            self._ask_relay_consent(chat_id, scope, tool, sid)
            return toast("info", "这张已经过时了，看下面那张新的", "拒绝:过时的卡")
        waiting = getattr(self, "_pending_consent", {}).pop(scope, None)
        if waiting != (tool, sid):
            # `None` 也要拒：说了「取消」之后那张卡的记录已经被弹掉，这时点它等于
            # 凭空进一个没在问的会话。服务重启过也落在这里（内存态没了）—— 让他重新挑一个，
            # 比让一张来历不明的卡片换来 24 小时免确认安全。
            #
            # 🔴 但**不能只给一个 toast 就完**：toast 是一闪而过的弹窗，而这条路最常见的
            # 触发原因不是攻击，是**服务重启过**（看门狗在长连接静默时会自杀等拉起，
            # 本机实测已重启 49 次）。只弹一下「失效了」，用户看到的就是「按钮点了没反应」——
            # 11-05 他已经报过一次这个形状的问题。所以再补一条带按钮的消息，给他下一步。
            self._say_later_with_buttons(
                chat_id,
                "刚才那张卡片已经失效了（多半是服务重启过）。点下面重新挑一个会话就行。",
                [_do_button("sessions", "🖥 看看会话")],
            )
            return toast("error", "这张卡片已经失效了，点「看看会话」重新挑一个", "拒绝:没有在等的确认")
        if click["op"] == "no":
            control.clear_pending(scope)
            self._sync_talk_marker(scope)
            # 🔴 绑过的群里点「算了」= **解绑**（只关转发、群留着）。只清待发目标的话
            # 下一句又会按绑定接回去、再弹一次卡 —— 用户以为断了，其实没断
            # （2026-11-05 体验审查）。确认前记着的话在那里面一起作废并照实说。
            if self._group_bind_quit(scope, chat_id):
                return toast("info", "好，这个群不转发了", "不进+解绑")
            dropped = self._drop_held(scope)
            self._say_later_with_buttons(chat_id, "好，不进对话模式。"
                                         + ("你刚才说的那几句也没发过去。" if dropped else ""),
                                         [_fleet_button("open", tool, sid, "👀 看看它在干什么"),
                                          _do_button("sessions", "🖥 回到总览")])
            return toast("info", "好，不进了", "不进")
        # 按**弹卡那一刻**的样子记（卡上写的是哪套权限，他同意的就是哪套）
        ident = self._consented_ident(scope, tool, sid)
        self._remember_talk_ident(scope, tool, sid, ident)
        store = getattr(self, "relay_consent", None)
        if store is not None:
            try:
                store.grant(scope, tool, sid, ident)
            except Exception:  # noqa: BLE001 —— 存不下来只是下次再问一次，这一次照样进
                logger.exception("对话模式的确认没存下来")
        target = control.set_pending(scope, tool, sid)
        self._sync_talk_marker(scope)
        # 确认前在群里说的那几句（卡上已经引用过原话、写明「点确认后会发过去」）⇒ 现在发
        held = self._release_held(scope, chat_id, tool, sid)
        job = self._send_held(scope, chat_id, target, held)
        if job is not None:
            self._spawn(job)
            return toast("info", "好，刚才那句这就发过去" if held[0] else "好，说吧", "进对话模式+补发")
        self._say_later_with_buttons(chat_id, self._enter_talk_text(target, scope),
                                     self._talk_buttons(target.tool, target.id))
        return toast("info", "好，说吧", "进对话模式")

    def _handle_mode(self, chat_id: str, text: str, confirmed: bool = False) -> None:
        """`/mode` 看当前档位 + 给可选项；`/mode readonly` 直接切。

        🔴 **超不过本机上限。** config.toml 的 mode 是本机天花板（见 policy.py
        的 MODE_RANK 红字）：IT 给同事装成 readonly，他在聊天里点什么都提不上去。
        被天花板压住时要**明说**，不能静默给个更紧的档还显示成功 ——
        用户会以为提权成功了，然后奇怪为什么还是不能改文件。

        🔴 **放开要点两次，收紧一次就够。** 误点一下从"每步问我"变成"不问就改"，
        之后几十个写操作静默发生，用户不会收到任何提示。竞品一致这么做
        （Codex 的 fullAccessWarning、Duet 默认 manual）。
        `confirmed=True` 表示已经过了那张确认卡。
        """
        from .menu import build_confirm_loosen_card, build_mode_card
        from .policy import MODE_RANK

        prefs = getattr(self, "prefs", None)
        policy = self.policy
        parts = text.split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if not arg:
            card = build_mode_card(policy)
            sender = getattr(self.replier, "send_card", None)
            if sender is not None and sender(chat_id, card):
                return
            # 卡片发不出去就退化成文字 —— 别让用户问了权限却什么都没收到
            options = "、".join(MODE_LABELS[m] for m in modes_up_to(policy.ceiling_mode))
            self._say(
                chat_id,
                f"🔑 现在是 **{MODE_LABELS[policy.mode]}**\n{policy.describe()}\n\n"
                f"能切：{options}。点「🔑 权限」就能选。",
            )
            return

        # 中文也认 —— 用户在手机上不会打英文
        alias = {
            "只读": "readonly", "readonly": "readonly", "ro": "readonly",
            "可改": "write", "可改文件": "write", "write": "write", "w": "write",
            "全授权": "trusted", "trusted": "trusted", "全开": "trusted",
        }
        target = alias.get(arg)
        if target is None:
            self._say_with_buttons(chat_id, f"不认识「{arg}」。点「权限」看有哪些档位。", [_do_button("mode")])
            return
        if prefs is None:
            self._say(chat_id, "这台电脑的权限是装的时候定下的，手机上切不了。想放开找给你装的那位同事。")
            return

        # 往松的方向走 ⇒ 先要一次明确确认。
        # 判据是**档位排序**而不是"是不是 trusted"：write 相对 readonly 也是放开
        # （能直接改文件了），同样该确认。
        loosening = MODE_RANK[target] > MODE_RANK[policy.mode]
        if loosening and not confirmed:
            card = build_confirm_loosen_card(target, policy)
            sender = getattr(self.replier, "send_card", None)
            if sender is not None and sender(chat_id, card):
                return
            # 卡片发不出去就**不要**静默提权 —— 退化成让用户再发一次明确指令。
            # 这里 fail-closed：宁可多一步，不要在他没看到任何警告的情况下放开。
            self._say(
                chat_id,
                f"⚠️ 要放开到「{MODE_LABELS[target]}」。确认卡片发不出去，"
                f"你再发一次 `/mode! {target}` 就生效（这一步是故意要你确认的）。",
            )
            return

        applied, clamped = prefs.set_mode(target)
        # 🔴 第 24 轮（R-install 复现）：迁移通知没送达期间 `migrated_from` 还留着；他这时自己挑了档，
        # 下次启动 `_tell_about_migration` 会照「留下的只读只可能是旧噪音」清掉他刚选的只读 ⇒ 静默升回全授权。
        # 他已经亲手选过档了 ⇒ 那句「升级把权限放开了」也没必要再说 ⇒ 擦掉标记。
        config_path = getattr(self.settings, "config_path", "") or ""
        if config_path and os.path.exists(config_path):
            self._erase_migration_marker(config_path)
        policy = self.policy
        lines = [f"🔑 现在是 **{MODE_LABELS[applied]}**", policy.describe()]
        if applied == "trusted":
            # 放开之后要**一直看得见**。仿 Codex 的 "Full access is on" 横幅：
            # 用户可能在几小时后才回来发消息，那时他不记得自己点过什么。
            lines.append("⚠️ 之后我不再逐步问你。想收回随时点「收回到只读」。")
        if clamped:
            # 说清是**谁**挡的、以及怎么才能真的放开 —— 否则用户只会觉得坏了
            lines.insert(
                1,
                f"⚠️ 这台电脑最多放到「{MODE_LABELS[policy.ceiling_mode]}」，"
                f"所以没能切到「{MODE_LABELS[target]}」。还想再往上，找给你装的那位同事放开 —— 手机上点不开。",
            )
        if not prefs.last_save_ok:
            lines.append("⚠️ 这次没存下来，重启后会回到配置里那一档。")
        lines.append("（下一条消息开始生效）")
        # 放开了 ⇒ 一键收回（收紧不用二次确认，走菜单现成的 {"act":"menu","mode":"readonly"}）
        if applied != "readonly":
            from .fleet.actions import readonly_button

            self._say_with_buttons(chat_id, "\n".join(lines), [readonly_button()])
        else:
            self._say(chat_id, "\n".join(lines))

    def _effective_model(self, tool: str, sid: str):
        """这一轮该用的模型 / effort：会话覆盖 > 工具默认 > 工具自己的配置。"""
        from .fleet.catalog import Choice

        mp = getattr(self, "model_prefs", None)
        if mp is None or tool not in ("claude", "codex"):
            return Choice()
        try:
            return mp.effective(tool, sid)
        except Exception:  # noqa: BLE001
            logger.exception("读模型设置失败，用工具默认")
            return Choice()

    def _model_session_of(self, chat_id: str):
        """这个聊天是会话群 ⇒ (tool, sid, 标签)；不是 ⇒ None。"""
        binds = getattr(self, "chat_binds", None)
        if binds is None or not chat_id:
            return None
        try:
            bound = binds.of_chat(chat_id)
        except Exception:  # noqa: BLE001
            return None
        if bound is None or bound.tool not in ("claude", "codex"):
            return None
        return bound.tool, bound.id, bound.name or bound.tool

    def _on_model_click(self, event, clicker: str, value: dict):
        """「🧠 模型」卡上的按钮。闸门：主人 + 形状白名单；会话按点击所在群现查绑定。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.catalog import LABELS
        from .fleet.modelcard import home_card, list_card, resolve_click

        def reply(kind: str, text: str, card: str = ""):
            body = {"toast": {"type": kind, "content": text}}
            if card:
                body["card"] = {"type": "raw", "data": json.loads(card)}
            return P2CardActionTriggerResponse(body)

        if clicker not in self.settings.owner_open_ids:
            return reply("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN])
        click = resolve_click(value)
        mp = getattr(self, "model_prefs", None)
        if click is None or mp is None:
            return reply("error", _CARD_TOASTS[ApprovalStore.MALFORMED])
        chat_id = getattr(getattr(event, "context", None), "open_chat_id", "") or ""
        session = self._model_session_of(chat_id)
        op, to, tool = click["op"], click["to"], click["tool"]
        if to == "s":
            if session is None:
                return reply("error", "这里不是会话群，只能设默认模型", home_card(mp, None))
            tool, sid, label = session
        else:
            sid, label = "", ""
        logger.info("模型卡 点击 op=%s to=%s tool=%s", op, to, tool or "-")
        if op == "home":
            return reply("info", "模型总览", home_card(mp, session))
        if op == "open":
            return reply("info", f"选 {LABELS.get(tool, tool)} 的模型", list_card(mp, to, tool, sid, label))
        if op == "m":
            names = mp.catalog.names(tool)
            if click["i"] >= len(names):
                return reply("error", "模型列表变了，重新选一次", list_card(mp, to, tool, sid, label))
            name = names[click["i"]]
            ok = mp.set_session(tool, sid, model=name) if to == "s" else mp.set_default(tool, model=name)
            if ok and to == "d" and tool == "claude" and getattr(self, "prefs", None) is not None:
                from .policy import known_model

                if known_model(name):
                    self.prefs.set_model(name)
        else:
            effort = click["e"]
            ok = mp.set_session(tool, sid, effort=effort) if to == "s" else mp.set_default(tool, effort=effort)
        if not ok:
            return reply("error", "这一项现在不能选，刷新后再试", list_card(mp, to, tool, sid, label))
        now = mp.effective(tool, sid) if to == "s" else mp.default(tool)
        where = "这个会话" if to == "s" else f"{LABELS.get(tool, tool)} 默认"
        tail = "下一轮开始生效。" if to == "s" else "以后新建的会话用它。"
        if chat_id:
            self._say_later(chat_id, f"🧠 {where}：**{now.text()}**。{tail}")
            if to == "s":
                self._refresh_status_card_later(chat_id)
        return reply("success", f"{where}：{now.text()}", list_card(mp, to, tool, sid, label))

    def _status_card_info(self, chat_id: str):
        """会话群 ⇒ 状态卡要显示的东西（全部服务端现查）；不是会话群 ⇒ None。"""
        from .fleet.cards import project_of
        from .fleet.model import short_title
        from .fleet.statuscard import CardInfo

        binds = getattr(self, "chat_binds", None)
        if binds is None or not chat_id:
            return None
        try:
            bound = binds.of_chat(chat_id)
        except Exception:  # noqa: BLE001
            return None
        if bound is None:
            return None
        sess = None
        try:
            sess = self.fleet.cached(bound.tool, bound.id)
        except Exception:  # noqa: BLE001
            sess = None
        cwd = getattr(sess, "cwd", "") if sess is not None else ""
        if not cwd:
            cwd = getattr(self, "_new_session_cwd", {}).get(bound.id, "")
        title = getattr(sess, "title", "") if sess is not None else ""
        choice = self._effective_model(bound.tool, bound.id)
        model = choice.text() if (choice.model or choice.effort) else ""
        if not model:
            model = "工具默认"
        mode = MODE_LABELS.get(getattr(getattr(self, "policy", None), "mode", ""), "")
        progress = ""
        pending = getattr(sess, "pending", None) if sess is not None else None
        if pending is not None:
            from .fleet.grouppush import _pending_text

            progress = "等你：" + short_title(_redact_text(_pending_text(sess)), 60)
        keeper = getattr(self, "status_cards", None)
        return CardInfo(
            tool=bound.tool, sid=bound.id,
            title=short_title(_redact_text(title), 24) if title else "",
            project=_redact_text(project_of(cwd)) if cwd else "",
            model=model, mode=mode,
            status=getattr(sess, "status", "unknown") if sess is not None else "unknown",
            last_turn=getattr(sess, "last_turn", "") if sess is not None else "",
            last_turn_at=float(getattr(sess, "last_turn_at", 0) or 0) if sess is not None else 0.0,
            progress=progress,
            finished=bool(keeper.finished(chat_id)) if keeper is not None else False,
        )

    def _refresh_status_card(self, chat_id: str, force: bool = False) -> str:
        """会话群的状态卡：没有就发并置顶，有就原地更新。返回 keeper 的结果（测试和日志用）。"""
        keeper = getattr(self, "status_cards", None)
        if keeper is None:
            return "off"
        info = self._status_card_info(chat_id)
        if info is None:
            return "not_group"
        how = keeper.ensure(chat_id, info, force=force)
        if how not in ("same",):
            logger.info("状态卡 %s chat=%s tool=%s", how, chat_id[:12], info.tool)
        return how

    def _status_cards_tick(self) -> None:
        """后台每拍：所有开着的会话群的状态卡跟着会话状态更新（内容没变不发请求）。"""
        binds = getattr(self, "chat_binds", None)
        if getattr(self, "status_cards", None) is None or binds is None:
            return
        try:
            rows = list(binds.all())
        except Exception:  # noqa: BLE001
            return
        for bound in rows:
            if not getattr(bound, "on", True):
                continue
            try:
                self._refresh_status_card(bound.chat_id)
            except Exception:  # noqa: BLE001
                logger.exception("状态卡这一拍更新失败 chat=%s", str(bound.chat_id)[:12])

    def _mark_session_done(self, chat_id: str, done: bool) -> str:
        """✅ 完成 / ↩︎ 重新打开：状态卡改状态，群名加或去掉 ✅ 前缀（主人自己改过的群名不碰）。"""
        from .fleet.statuscard import DONE_PREFIX

        keeper = getattr(self, "status_cards", None)
        binds = getattr(self, "chat_binds", None)
        if keeper is None or binds is None:
            return "off"
        bound = binds.of_chat(chat_id)
        if bound is None:
            return "not_group"
        keeper.set_finished(chat_id, done)
        client = getattr(self, "group_client", None)
        getter = getattr(client, "name_of", None)
        setter = getattr(client, "rename", None)
        renamed = ""
        if getter is not None and setter is not None and bound.name:
            try:
                current, _code, _msg = getter(chat_id)
            except Exception:  # noqa: BLE001
                current = ""
            if current and current.strip() == bound.name.strip():
                from .fleet.chatbind import MAX_NAME_CHARS
                from .fleet.model import short_title

                base = bound.name[len(DONE_PREFIX):] if bound.name.startswith(DONE_PREFIX) else bound.name
                # 加 ✅ 后要整体塞进群名上限，否则记下的名字会被截断、和飞书上对不上，
                # 下次「重新打开」就会误判成主人改过名而不动（2026-11-17 实测）。
                want = (DONE_PREFIX + short_title(base, MAX_NAME_CHARS - len(DONE_PREFIX))) if done else base
                if want != bound.name:
                    try:
                        ok, _code, _msg = setter(chat_id, want)
                    except Exception:  # noqa: BLE001
                        ok = False
                    if ok:
                        binds.set_name(chat_id, want)
                        renamed = want
        self._refresh_status_card(chat_id, force=True)
        logger.info("会话收尾 done=%s chat=%s 改名=%s", done, chat_id[:12], bool(renamed))
        return renamed or "kept"

    def _on_status_card_click(self, event, clicker: str, value: dict):
        """状态卡上的 🔄 刷新 / ✅ 完成 / ↩︎ 重新打开。闸门：主人 + 形状白名单 + 点在会话群里。"""
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.statuscard import resolve_click

        def toast(kind: str, content: str):
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN])
        op = resolve_click(value)
        if not op:
            return toast("error", _CARD_TOASTS[ApprovalStore.MALFORMED])
        chat_id = getattr(getattr(event, "context", None), "open_chat_id", "") or ""
        if self._model_session_of(chat_id) is None and (
                getattr(self, "chat_binds", None) is None or self.chat_binds.of_chat(chat_id) is None):
            return toast("error", "这里不是会话群")
        logger.info("状态卡 点击 op=%s", op)
        if op == "refresh":
            self._in_background(self._refresh_status_card, chat_id, True)
            return toast("info", "正在刷新")
        done = op == "done"
        self._in_background(self._mark_session_done, chat_id, done)
        if done:
            self._say_later(chat_id, "✅ 这个会话标记为已完成。之后在群里再说一句，就会重新打开接着做。")
        return toast("success", "已标记完成" if done else "已重新打开")

    def _refresh_status_card_later(self, chat_id: str) -> None:
        """会话群的状态卡跟着刷新（状态卡模块接上之前是空操作）。"""
        fn = getattr(self, "_refresh_status_card", None)
        if fn is None:
            return
        try:
            self._in_background(fn, chat_id)
        except Exception:  # noqa: BLE001
            logger.debug("状态卡刷新排队失败", exc_info=True)

    def _handle_model(self, chat_id: str, text: str) -> None:
        """`/model` 看当前模型 + 给可选项；`/model claude-opus-5-5` 切换。

        模型名是**白名单**（policy.MODEL_CHOICES）。不是怕注入（不走 shell），
        是怕拼错：一个不存在的名字会让之后每一轮都失败，而用户只看到"它不回我"。
        """
        from .menu import build_model_card

        prefs = getattr(self, "prefs", None)
        policy = self.policy
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if not arg:
            mp = getattr(self, "model_prefs", None)
            sender = getattr(self.replier, "send_card", None)
            if mp is not None and sender is not None:
                from .fleet.modelcard import home_card

                if sender(chat_id, home_card(mp, self._model_session_of(chat_id))):
                    return
            card = build_model_card(policy)
            if sender is not None and sender(chat_id, card):
                return
            lines = [f"🧠 现在用 **{policy.model}**", ""]
            for name, label, why in MODEL_CHOICES:
                mark = "· " if name != policy.model else "✅ "
                lines.append(f"{mark}{label}（{name}）— {why}")
            lines.append("")
            lines.append("点「🧠 模型」就能选。")
            self._say(chat_id, "\n".join(lines))
            return

        # 认短标签（「最快」）也认全名
        by_label = {label: name for name, label, _ in MODEL_CHOICES}
        target = by_label.get(arg, arg)
        if prefs is None or not prefs.set_model(target):
            names = "、".join(label for _, label, _ in MODEL_CHOICES)
            self._say(chat_id, f"不认识「{arg}」。能选：{names}")
            return
        label = next((l for n, l, _ in MODEL_CHOICES if n == target), target)
        note = "" if prefs.last_save_ok else "\n⚠️ 这次没存下来，重启后会回到配置里那个。"
        self._say(chat_id, f"🧠 换成 **{label}**（{target}）。下一条消息开始生效。{note}")

    # ---------- 按钮（bridge/fleet/actions.py） ----------

    def _say_with_buttons(self, chat_id: str, text: str, buttons) -> bool:
        """发一句话，后面跟一张只放按钮的小卡片（纯文本 post 带不了按钮）。

        卡片发不出去 ⇒ 补一句「也可以直接发 /xxx」—— 文字命令是兜底，不能变成死路。
        replier 根本不会发卡片 ⇒ 兜底那句直接并进这条，不多发一条。
        """
        from .fleet.actions import build_buttons_card, typed_fallback

        buttons = [b for b in (buttons or ()) if b]
        if not buttons:
            return self._say(chat_id, text)
        if getattr(getattr(self, "replier", None), "send_card", None) is None:
            hint = typed_fallback(buttons)
            return self._say(chat_id, text + ("\n\n" + hint if hint else ""))
        ok = self._say(chat_id, text)
        self._send_buttons(chat_id, buttons)
        return ok

    def _send_buttons(self, chat_id: str, buttons) -> bool:
        """只发那张按钮小卡片（前面那条话已经发过了，比如走 replier.send 的长回复）。"""
        from .fleet.actions import build_buttons_card, typed_fallback

        buttons = [b for b in (buttons or ()) if b]
        if not buttons or not chat_id:
            return False
        sender = getattr(getattr(self, "replier", None), "send_card", None)
        try:
            sent = sender is not None and bool(sender(chat_id, build_buttons_card(buttons)))
        except Exception:  # noqa: BLE001 —— 飞书接口超时 / 网络抖动
            logger.exception("按钮卡发送抛异常，退成文字")
            sent = False
        if not sent:
            hint = typed_fallback(buttons)
            if hint:
                self._say(chat_id, hint)
        return sent

    def _say_later_with_buttons(self, chat_id: str, text: str, buttons) -> None:
        """同 `_say_later`：给 3 秒回调用。"""
        loop = getattr(self, "_loop", None)
        if loop is None or not hasattr(loop, "call_soon_threadsafe"):
            self._say_with_buttons(chat_id, text, buttons)
            return
        loop.call_soon_threadsafe(
            lambda: loop.run_in_executor(None, self._say_with_buttons, chat_id, text, buttons)
        )

    def _stale_card_notice(self, event, clicker: str) -> None:
        """主人点了一张认不出来的卡（多半是服务重启过）⇒ 聊天里留一条带「回到总览」的话。

        🔴 toast 一闪就没，单手看手机很容易错过，看到的就是「点了没反应」（2026-11-06 体验侦察 Top3）。
        只取 chat_id，**不判 scope、不写 `_chat_ids`**（scope 的闸门在 `_click_scope`，这里不碰）；
        「回到总览」是不需要 scope 的按钮（`UNSCOPED_OPS`），重启后也点得动。
        调用方已经核过是主人。
        """
        context = getattr(event, "context", None)
        chat_id = getattr(context, "open_chat_id", "") or ""
        if not chat_id:
            recorded = getattr(self, "_chat_ids", {}).get(f"dm:{clicker}", "")
            chat_id = recorded if str(recorded).startswith("oc_") else ""
        if not chat_id:
            return
        self._say_later_with_buttons(
            chat_id,
            "刚才那张卡片已经失效了（多半是服务重启过）。点下面「回到总览」重新挑一个会话就行。",
            [_do_button("sessions", "🖥 回到总览")],
        )

    def _unknown_chat_notice(self, chat_id: str) -> None:
        """服务刚重启、认不出这个聊天是群还是单聊（`_click_scope` 不猜）⇒ 留一句话说清怎么办。

        不配按钮：要解开的是「这个聊天是哪」，只有他在这里说一句话才认得出来，按钮帮不上。
        """
        if not chat_id:
            return
        self._say_later(chat_id, "服务刚重启过，我还认不出这个聊天。在这里先发一句「会话」，再点刚才那个按钮就行。")

    def _talk_buttons(self, tool: str, sid: str) -> list:
        """对话模式里每条回复下面那排：[退出对话] [停下]（能停才放）[换个会话]。"""
        from .fleet.actions import do_button

        buttons = [do_button("quit", primary=True)]
        control = getattr(self, "fleet", None)
        sess = None
        if control is not None:
            try:
                sess = control.cached(tool, sid)
            except Exception:  # noqa: BLE001
                sess = None
        if sess is not None and getattr(sess, "can_stop", False):
            buttons.append(do_button("stop"))
        buttons.append(do_button("sessions", "🔀 换个会话"))
        return buttons

    def _on_do_click(self, event, clicker: str, value: dict):
        """「点一下就行」的按钮 ⇒ 当成主人打了那条斜杠命令，走**正常**分派。

        🔴 同 `_on_menu_click`：不走捷径。鉴权重新判、暂停 / 审批 / 读边界照常；
        value 只认 `actions.DO_COMMANDS` 里预设的 op、不带参数、多一个键都不认。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.actions import TOASTS, UNSCOPED_OPS, op_of, resolve_do_click

        def toast(kind: str, content: str):
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            logger.info("按钮点击被拒 act=do result=not_owner")
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN])
        text = resolve_do_click(value)
        if not text:
            logger.info("按钮点击被拒 act=do result=unknown_op")
            return toast("error", _CARD_TOASTS[ApprovalStore.MALFORMED])
        op = op_of(value)
        chat_id, scope = self._click_scope(event, clicker)
        if not chat_id:
            logger.info("按钮点击没处理 act=do op=%s result=no_chat", op)
            return toast("error", "找不到要回复的对话，直接打字问一句就好")
        if not scope and op not in UNSCOPED_OPS:
            # 卡片回调分不出单聊和群（见 _click_scope）⇒ 退出 / 停 / 新话题对错了聊天就是事故，不猜
            logger.info("按钮点击没处理 act=do op=%s result=no_scope", op)
            return toast("error", "服务刚重启过，先在这个聊天里发一句话再点")
        loop = getattr(self, "_loop", None)
        if loop is None:
            logger.info("按钮点击没处理 act=do op=%s result=no_loop", op)
            return toast("error", "我这边还没准备好，过几秒再点一次")
        if scope:
            asyncio.run_coroutine_threadsafe(self._handle(scope, chat_id, text), loop)
        elif op == "sessions":
            asyncio.run_coroutine_threadsafe(self._fleet_overview(chat_id), loop)
        else:  # help
            self._say_later_with_buttons(chat_id, HELP, self._help_buttons())
        logger.info("按钮点击 act=do op=%s scoped=%s result=dispatched", op, bool(scope))
        return toast("info", TOASTS.get(op, "收到"))

    # ---------- 「↑ 最近问过」（bridge/history.py 有调研结论） ----------

    def _history(self):
        store = getattr(self, "_input_history", None)
        if store is None:
            from .history import HISTORY_FILE, InputHistory

            config_path = getattr(getattr(self, "settings", None), "config_path", "") or ""
            directory = os.path.dirname(config_path)
            store = InputHistory(os.path.join(directory, HISTORY_FILE) if directory else "")
            self._input_history = store
        return store

    def _remember_input(self, scope: str, text: str) -> None:
        try:
            self._history().add(scope, text)
        except Exception:  # noqa: BLE001 —— 记不下历史不能影响这条消息
            logger.debug("记输入历史失败", exc_info=True)

    def _send_history_card(self, scope: str, chat_id: str) -> None:
        import json as _json_mod

        from .history import build_card

        where = "这个群" if scope.startswith("group:") else "这个聊天"
        entries = self._history().entries(scope)
        warn = self._history_hold_reason(scope, entries[0][0]) if entries else ""
        card = _json_mod.dumps(build_card(entries, where=where, warn=warn), ensure_ascii=False)
        sender = getattr(getattr(self, "replier", None), "send_card", None)
        if sender is None or not sender(chat_id, card):
            self._say(chat_id, "「最近问过」的卡片发不出去。直接打字问就好。")

    def _history_hold_reason(self, scope: str, text: str) -> str:
        """这一句**不该**一回车就发 ⇒ 返回给他看的原因；空串 = 可以直接发。

        第 27 轮风险审查（RISK）：选中即发时这一下回车就是唯一的确认 ⇒ 三种情况退回「先填进框、再点发送」：
        - 有确认卡在等：一发就会把那张卡作废（`_supersede_pending_approvals`），等批的那一步就丢了
        - 正在「接着聊」电脑上的会话：这句会发进那个会话（全权限的终端会话也可能），而不是桥自己
        - 全授权档 + 句子带「删/改/提交/推/部署…」：钩子不会再弹卡，当初那句的指代现在可能已经不对了
        """
        from .history import looks_like_write

        gate = getattr(self, "gate", None)
        try:
            names = os.listdir(gate.dir_for(scope)) if gate is not None else []
        except OSError:
            names = []
        if any(n.endswith((".asked", ".pending")) for n in names):
            return "上面有一张确认卡在等你，这句一发出去它就作废了。"
        control = getattr(self, "fleet", None)
        try:
            target, _expired = control.peek_pending_fresh(scope) if control is not None else (None, False)
        except Exception:  # noqa: BLE001 —— 判不出就当不在对话模式（多问一次的方向在上面两条兜）
            target = None
        if target is not None:
            return "你现在在「接着聊」电脑上的会话，回车后这句会发给那个会话。"
        if getattr(getattr(self, "policy", None), "mode", "") == "trusted" and looks_like_write(text):
            return "这句会改东西，全授权下回车后它不问就做。"
        return ""

    def _on_history_click(self, event, clicker: str, value: dict):
        """「↑ 最近问过」卡上的点击（第 27 轮：选中即发）。

        - pick：下拉里选中一句（桌面端 ↑↓ 挪高亮、回车才算选中，由客户端保证）⇒ 直接发；
          卡原地换成「✅ 已发 · 时间 + 原话」（上面不再有下拉，免得再点开回车重发）
        - edit / list：改一改再发 / 换一句
        - send：改一改那张卡上的「发送」
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .history import build_card, build_sent_card, find, picked_fingerprint, submitted_text

        def reply(kind: str, content: str, card=None):
            body = {"toast": {"type": kind, "content": content}}
            if card is not None:
                body["card"] = {"type": "raw", "data": card}
            return P2CardActionTriggerResponse(body)

        if clicker not in self.settings.owner_open_ids:
            return reply("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN])
        chat_id, scope = self._click_scope(event, clicker)
        if not chat_id or not scope:
            return reply("error", "服务刚重启过，先在这个聊天里发一句话再点")
        store = self._history()
        entries = store.entries(scope)
        items = [t for t, _ in entries]
        action = getattr(event, "action", None)
        op = value.get("op")

        def card(index: int = 0):
            """选句卡；框里那句要是会撞上确认卡 / 接着聊 / 全授权改东西，框上面写清楚。"""
            warn = self._history_hold_reason(scope, items[index]) if items else ""
            return build_card(entries, index, warn=warn)

        def send(text: str):
            loop = getattr(self, "_loop", None)
            if loop is None:
                return reply("error", "我这边还没准备好，过几秒再点一次")
            # 🔴 去重：同一句 10 秒内只发一次（客户端选同一项会再触发回调；手快连点两下）
            now = time.monotonic()
            recent = getattr(self, "_hist_sent", {})
            self._hist_sent = recent
            key = (scope, text)
            if now - recent.get(key, -1e9) < 10:
                return reply("info", "这句刚发过", build_sent_card(text))
            recent[key] = now
            session = self.sessions.get(scope) if getattr(self, "sessions", None) else None
            queued = bool(session is not None and session.lock.locked())
            logger.info("最近问过 发出 op=%s chars=%d 排队=%s", op, len(text), queued)

            async def go():
                # 回显：卡片发的话在聊天里看不见，而回答出现在最底下 ⇒ 先说一句「你问：…」
                await asyncio.to_thread(self._say, chat_id, f"↑ 你问：{text}")
                await self._handle(scope, chat_id, text)

            asyncio.run_coroutine_threadsafe(go(), loop)
            return reply("info", "排上了，前面还在跑" if queued else "发出去了", build_sent_card(text, queued=queued))

        if op == "pick":
            # 第 28 轮：下拉只负责把那句换进框里，发送交给框的回车（用户：回车要像编辑框一样发）
            index = find(items, picked_fingerprint(action))
            if index is None:
                return reply("info", "列表有更新，重新选一下", card())
            return reply("info", "放进框里了，回车就发", card(index))
        if op == "list":
            return reply("info", "选一句", card())
        if op == "edit":
            index = find(items, str(value.get("h") or ""))
            if index is None:
                return reply("info", "列表有更新，重新选一下", card())
            return reply("info", "改好了回车就发", card(index))
        if op == "send":
            text = submitted_text(action)
            return send(text) if text else reply("error", "框里是空的")
        return reply("error", _CARD_TOASTS[ApprovalStore.MALFORMED])

    def _help_buttons(self) -> list:
        from .fleet.actions import do_button

        return [do_button("menu", primary=True), do_button("sessions"),
                do_button("history"), do_button("status"), do_button("mode")]

    def _say_later(self, chat_id: str, text: str) -> None:
        """在事件循环里发，立刻返回。给 3 秒回调用。没起循环就退回同步发。"""
        loop = getattr(self, "_loop", None)
        if loop is None or not hasattr(loop, "call_soon_threadsafe"):
            self._say(chat_id, text)
            return
        loop.call_soon_threadsafe(
            lambda: loop.run_in_executor(None, self._say, chat_id, text)
        )

    def _say(self, chat_id: str, text: str) -> bool:
        """往飞书发一条。返回是否送达。

        🔴 失败必须**看得见**。这个方法有 22 处调用，原来全部吞掉结果 ——
        用户的体验是"它不回我"，而日志里看不出是哪一条没发出去。
        真实日志里出现过 `code=230001 invalid receive_id`（单聊 scope 里
        存的是 open_id，而发消息按 chat_id 发），当时就是这么被埋掉的。

        空 chat_id 直接跳过：发一个必然失败的请求只会污染日志。
        """
        if not chat_id:
            logger.warning("没有 chat_id，这条消息发不出去：%s", text[:60])
            return False
        try:
            ok = self.replier.send(chat_id, text)
        except Exception:
            logger.exception("发送失败 chat_id=%s chars=%d", chat_id, len(text))
            return False
        if not ok:
            logger.error(
                "消息没送达 chat_id=%s chars=%d 开头=%s",
                chat_id, len(text), text[:40],
            )
        return ok

    # ---------- 启动 ----------

    def run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop

        # 装审批钩子。每次启动都重写，保证钩子脚本和当前版本一致。
        hook_path = os.path.join(
            os.path.dirname(self.gate.root), "hooks", "approval_hook.py"
        )
        # 注意不传 approval_dir：那个值必须由 run_turn 按会话传进进程环境，
        # 写进 settings.json 会覆盖它（实测），导致所有会话共用批准目录。
        install_hook(
            hook_path,
            os.path.join(self.runner.config_dir, "settings.json"),
        )
        # 总控的 `claude --settings` 也挂同一个钩子（见 _fleet_hook）
        logger.info("审批钩子已装：%s", hook_path)

        handler = (
            lark.EventDispatcherHandler.builder("", "")  # 长连接下这两个参数必须是空串
            .register_p2_im_message_receive_v1(self.on_message)
            .register_p2_card_action_trigger(self.on_card_action)
            # 常驻菜单（输入框上面那一排）。订阅它不要求菜单已经配好 ——
            # 先接上，等菜单配上就直接能用，省掉"配完还得改代码重启"这一步。
            .register_p2_application_bot_menu_v6(self.on_bot_menu)
            .build()
        )
        # 🔴 第 15 轮：重连窗口从 18~21 分钟压到半分钟内（`bridge/wsfast.py` 文件头有实测数据）。
        # 裸的 `lark.ws.Client` 首次重连前随机等 0~30 秒、之后每 120 秒一次 ⇒
        # 主人这台实测断到连上 634s / 1155s / 1291s，这期间点按钮就是
        # 「目标回调服务当前未在线」。
        from .wsfast import make_client

        ws_client = make_client(
            self.settings.app_id,
            self.settings.app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )
        # 存起来给看门狗探活。实测 SDK 内部用 `_conn` 存连接，断开时置回 None
        # （`lark_oapi/ws/client.py`），所以"连接对象在不在"比"多久没收到消息"
        # 准得多 —— 后者在用户空闲时也会一路涨。
        # 🔴 第 15 轮：接上 SDK 自己的重连回调（实测 `lark_oapi/ws/client.py` 里是
        # 两个默认 no-op 的实例属性）⇒ 「正在重连」有了现场事实，不用靠静默时长反推。
        try:
            ws_client.on_reconnecting = self._on_ws_reconnecting
            ws_client.on_reconnected = self._on_ws_reconnected
        except Exception:  # noqa: BLE001 —— SDK 换版本没这两个属性也不能影响启动
            logger.info("这个版本的飞书 SDK 没有重连回调，诊断里不显示重连状态")
        self._ws_client = ws_client

        threading.Thread(
            target=self._ws_thread, args=(ws_client,), daemon=True, name="lark-ws"
        ).start()
        threading.Thread(target=self._watchdog_loop, daemon=True, name="watchdog").start()
        # Claude 会话进入「等你」⇒ 主动推卡（第 7 轮）。开关关了线程照起，每拍直接返回。
        threading.Thread(target=self._notify_waiting_loop, daemon=True, name="notify-waiting").start()
        # 常驻菜单自愈（第 12 轮）：启动 1 分钟后查标记，没配好就写 + 发版；成功就退出线程。
        threading.Thread(target=self._menu_heal_loop, daemon=True, name="menu-heal").start()
        # FC-15：旧会话群补上 `SN·` 前缀，飞书里搜「SN·」能列出全部会话群。只改我们起的、没人动过的群名。
        threading.Thread(target=self._migrate_group_prefix, daemon=True, name="sn-prefix").start()
        threading.Thread(target=self._selfupdate_loop, daemon=True, name="selfupdate").start()
        # 第 32 轮：上次被重启打断的那一轮，等长连接连上后接着做（`bridge/inflight.py`）。
        # 钩子上面已经装好；连上之前不续 —— 续了也发不出「我让它接着做」和结果。
        threading.Thread(target=self._resume_after_restart_when_connected, args=(loop,),
                         daemon=True, name="restart-resume").start()

        # 开场消息。🔴 2026-11-05 实测的缺口：**推荐路径（扫码）装完，飞书里一个字都不发。**
        # 扫码那条路直接把主人的 open_id 写进 config ⇒ 配对码那条路不走 ⇒
        # `BOUND_MESSAGE` 永远发不出去，而这里原来没有任何主动问候。
        # 唯一的说明只在终端最后一屏，关了窗口就没了 —— 等于把唯一的介绍绑在了降级路径上。
        #
        # 丢后台线程：它要等长连接连上（最多 3 分钟），而这会儿 `run_forever()` 还没起来，
        # 同步等就是启动卡住。发成功才写标记（看门狗会反复重启，不能每次都发一遍）。
        self._greet_in_background()
        # 第 16 轮：档位被从「只看」升到全控制时告诉主人一次（见 `_tell_about_migration`）
        self._tell_about_migration()

        logger.info(
            "桥已启动。模式=%s 工作目录=%s 授权人数=%d",
            self.settings.policy.mode,
            self.settings.policy.default_workspace,
            len(self.settings.owner_open_ids),
        )
        loop.run_forever()

    # ---------- 第 33 轮：闲太久开了新会话 ⇒ 回复下面挂「↩︎ 接着上次」（只被 _run_one / on_card_action 调） ----------
    #
    # 上一个会话 id 只在内存（`_continue_offers` + 会话上的 `continue_last_text`）⇒ 重启就没了，按钮也就不出。
    # 按钮值只带一个随机序号，要换回哪个会话记在服务端 ⇒ 卡片值伪造不出「换到任意会话」（同 `rtalk`）。

    def _continue_offer_prepare(self, session, text: str, resume, restart):
        """开跑前调：这一轮是不是「因为空闲超时才开的新会话」。是 ⇒ 返回 {prev, head}，否则 None。

        顺手记下这一轮他说的话（下次超时时按钮上显示的就是它）。只认他自己说的话（`_run_locally` 进来的，
        `supersede_next` 为真）—— 批准后的「继续」、重启续跑的提示语都不算。
        `/new` 会把 `claude_session_id` 和 `turns` 清零 ⇒ 这里自然不出。
        """
        try:
            user_turn = bool(getattr(session, "supersede_next", False)) and not restart
            prev_text = str(getattr(session, "continue_last_text", "") or "")
            if user_turn:
                session.continue_last_text = text
            if not user_turn or resume is not None or not session.turns:
                return None
            prev = session.claude_session_id
            if not prev:
                return None
            return {"prev": str(prev), "head": prev_text}
        except Exception:  # noqa: BLE001 —— 按钮是锦上添花，不能挡住这一轮
            logger.debug("「接着上次」准备失败", exc_info=True)
            return None

    def _continue_offer_attach(self, scope: str, chat_id: str, offer: dict, new_sid) -> None:
        """回复已经发出去之后调：记下这次的提议、发按钮。新会话没拿到 id ⇒ 不出（点了也核不了）。"""
        if not offer or not new_sid or str(new_sid) == offer.get("prev"):
            return
        import secrets

        from .fleet.actions import continue_button

        nonce = secrets.token_hex(6)
        if not hasattr(self, "_continue_offers"):
            self._continue_offers = {}
        self._continue_offers[scope] = {"n": nonce, "prev": offer["prev"], "new": str(new_sid)}
        head = " ".join(_redact_text(offer.get("head") or "").split())
        logger.info("空闲超时开了新会话，挂「接着上次」 scope=%s 上一个=%s", scope[:16], offer["prev"][:8])
        self._send_buttons(chat_id, [continue_button(nonce, head)])

    def _on_continue_click(self, event, clicker: str, value: dict):
        """点「↩︎ 接着上次」⇒ 把这个聊天的会话 id 换回上一个。**不跑任何东西**，下一句话才 resume 它。

        闸门：主人 + 形状只认 `{"act","n"}` + 序号对得上服务端记的那条 +
        现在的会话还是那次新开的（之后 `/new` 过、或又换过 ⇒ 这个按钮作废）。
        正在跑 / 有确认卡在等 ⇒ 不换（换了会被那一轮的结果盖回去，或批准续跑接不上）。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        from .fleet.actions import resolve_continue_click

        def toast(kind: str, content: str, result: str):
            logger.info("「接着上次」点击 结果=%s", result)
            return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

        if clicker not in self.settings.owner_open_ids:
            return toast("error", _CARD_TOASTS[ApprovalStore.FORBIDDEN], "拒绝:不是主人")
        nonce = resolve_continue_click(value)
        if not nonce:
            return toast("error", _CARD_TOASTS[ApprovalStore.MALFORMED], "拒绝:形状不对")
        chat_id, scope = self._click_scope(event, clicker)
        offers = getattr(self, "_continue_offers", {})
        offer = offers.get(scope) if scope else None
        if not chat_id or offer is None or offer["n"] != nonce:
            return toast("error", "这个按钮已经失效了（可能服务重启过）。要接着聊就把上次的事再说一遍", "拒绝:没有在等的")
        session = self.sessions.get(scope)
        if session.claude_session_id != offer["new"]:
            offers.pop(scope, None)
            return toast("info", "这个按钮已经失效了（之后又开过新话题）", "拒绝:会话已变")
        if session.lock.locked():
            return toast("info", "这一轮还在跑，等它说完再点", "拒绝:在跑")
        if scope in getattr(self, "_awaiting", {}) or self._has_waiting_approval(scope):
            return toast("info", "上面还有确认卡在等你，先处理它再点", "拒绝:等确认")
        offers.pop(scope, None)
        session.claude_session_id = offer["prev"]
        session.last_active = time.monotonic()   # 别一换回去又被「空闲 30 分钟从头开」顶掉
        logger.info("「接着上次」换回上一个会话 scope=%s 会话=%s", scope[:16], offer["prev"][:8])
        self._say_later(chat_id, "好，接着上次那个话题。")
        return toast("info", "好，接着上次", "换回")

    # ---------- 第 32 轮：重启后接着干（判断在 bridge/inflight.py；只被 _run_one / run 调） ----------

    def _inflight_store(self):
        """`inflight.json` 在 config 同目录；没有 config 路径（测试里造的桥）⇒ 只在内存。"""
        store = getattr(self, "_inflight", None)
        if store is None:
            from .inflight import INFLIGHT_FILE, InflightStore

            config_path = getattr(getattr(self, "settings", None), "config_path", "") or ""
            directory = os.path.dirname(config_path)
            store = InflightStore(os.path.join(directory, INFLIGHT_FILE) if directory else "")
            self._inflight = store
        return store

    def _inflight_begin(self, scope: str, chat_id: str, session, text: str, resume, restart):
        """开跑写一条。失败只记日志、返回 None（续不了而已，这一轮照跑）。"""
        try:
            from .inflight import next_chain

            continue_of, chain_started_at = 0, None
            source_text = text
            if restart:
                continue_of, chain_started_at = next_chain(restart.get("entry") or {}, time.time())
                source_text = (restart.get("entry") or {}).get("text") or ""   # 提示里说的还是他的原话
            return self._inflight_store().start(
                scope, chat_id, getattr(session, "workspace", "") or "", source_text,
                claude_session_id=resume, continue_of=continue_of,
                chain_started_at=chain_started_at,
            )
        except Exception:  # noqa: BLE001
            logger.warning("在跑的轮次没记下来（只影响重启后续跑）", exc_info=True)
            return None

    def _inflight_session(self, scope: str, entry, sid: str) -> None:
        if not entry or not sid:
            return
        try:
            self._inflight_store().set_session(scope, sid, entry.get("started_at"))
        except Exception:  # noqa: BLE001
            logger.warning("会话 id 没补进在跑记录", exc_info=True)

    def _inflight_end(self, scope: str, entry) -> None:
        if not entry:
            return
        try:
            self._inflight_store().finish(scope, entry.get("started_at"))
        except Exception:  # noqa: BLE001
            logger.warning("在跑记录没删掉（下次启动可能多续一次）", exc_info=True)

    def _resume_after_restart_when_connected(self, loop) -> None:
        """跑在自己的线程里：等长连接连上，再把续跑丢进事件循环。

        连不上就一直等（每秒看一眼），等满 30 分钟还没连上 ⇒ 这些残留按「太旧」本来也不续了。
        """
        from .inflight import MAX_AGE_SEC

        deadline = time.monotonic() + MAX_AGE_SEC
        while not self.connection_alive():
            if time.monotonic() > deadline:
                logger.info("长连接 30 分钟没连上，重启前那几轮不续了")
                return
            time.sleep(1)
        try:
            asyncio.run_coroutine_threadsafe(self._resume_after_restart(), loop)
        except Exception:  # noqa: BLE001 —— 续跑是锦上添花，不能把桥带崩
            logger.exception("重启续跑没排上")

    def _has_waiting_approval(self, scope: str) -> bool:
        """盘上有没有这个会话**还没批**的确认（`.pending` 钩子刚落 / `.asked` 卡已发出）。"""
        gate = getattr(self, "gate", None)
        if gate is None:
            return False
        try:
            return any(n.endswith((".pending", ".asked")) for n in os.listdir(gate.dir_for(scope)))
        except OSError:
            return False

    async def _resume_after_restart(self) -> None:
        """读残留的「在跑」记录，能续的用同一个 claude 会话 + 固定提示接着做。"""
        from .inflight import (CHAIN_STOP, CHAIN_STOP_NOTE, CONTINUE_PROMPT, RESUME,
                               RESUMING_NOTE, head_of, plan)

        store = self._inflight_store()
        try:
            leftovers = store.leftovers()
        except Exception:  # noqa: BLE001
            logger.exception("读在跑记录失败，重启前那几轮不续了")
            return
        jobs = []
        for entry in leftovers:
            scope = entry["scope"]
            chat_id = str(entry.get("chat_id") or "") or self._chat_for(scope)
            verdict, why = plan(entry, time.time(),
                                has_pending=self._has_waiting_approval(scope),
                                paused=scope in getattr(self, "_paused", {}))
            # 先删掉旧条：续出来那一轮会自己写新的；不续的也不该下次再看到
            store.finish(scope, entry.get("started_at"))
            if verdict == CHAIN_STOP:
                logger.warning("重启续跑停下 scope=%s：%s", scope[:16], why)
                if chat_id:
                    self._say(chat_id, CHAIN_STOP_NOTE)
                continue
            if verdict != RESUME or not chat_id:
                logger.info("重启前那一轮不续 scope=%s：%s", scope[:16], why or "不知道往哪个聊天回")
                continue
            session = self.sessions.get(scope)
            session.claude_session_id = entry["claude_session_id"]
            session.last_active = time.monotonic()   # 别被「空闲 30 分钟从头开」规则顶掉
            session.restart_continue = {"prompt": CONTINUE_PROMPT, "entry": entry}
            if not hasattr(self, "_chat_ids"):
                self._chat_ids = {}
            self._chat_ids.setdefault(scope, chat_id)
            logger.info("重启续跑 scope=%s 第 %d 次", scope[:16], int(entry.get("continue_of") or 0) + 1)
            self._say(chat_id, RESUMING_NOTE.format(head=head_of(entry.get("text") or "")))
            jobs.append(self._resume_one(scope, chat_id, CONTINUE_PROMPT))
        if jobs:
            await asyncio.gather(*jobs)

    async def _resume_one(self, scope: str, chat_id: str, prompt: str) -> None:
        """走正常那条路（串行锁、审查、流式卡照常）。🔴 发的是提示语，不是原话。"""
        try:
            await self._run_locally(scope, chat_id, prompt)
        except Exception:  # noqa: BLE001
            logger.exception("重启续跑出错 scope=%s", scope[:16])
            self._say(chat_id, "重启后接着做的时候出错了。要继续就直接说一句，或者重新发一次你的要求。")

    # ---------- 会话进入「等你」⇒ 主动推卡（第 7 轮，判断在 bridge/fleet/notify.py） ----------

    def _migrate_group_prefix(self) -> None:
        binds = getattr(self, "chat_binds", None)
        client = getattr(self, "group_client", None)
        if binds is None or client is None:
            return
        try:
            from .fleet.chatbind import RENAMED, migrate_sn_prefix

            results = migrate_sn_prefix(binds, client)
            done = [r for r in results if r[1] == RENAMED]
            logger.info("群名前缀 SN· 迁移：改了 %d 个 / 看了 %d 个", len(done), len(results))
        except Exception:  # noqa: BLE001 —— 改群名失败不影响别的
            logger.exception("群名前缀 SN· 迁移失败，下次启动再试")

    def _menu_heal_loop(self) -> None:
        """跑在自己的 daemon 线程里。节奏、标记、失败不发版都在 bridge/menu_heal.py。"""
        heal = getattr(self, "menu_heal", None)
        if heal is None:
            return
        try:
            self.menu_heal.run_forever()
        except Exception:  # noqa: BLE001 —— 菜单是锦上添花，不许把桥带崩
            logger.exception("常驻菜单自愈线程退出")

    def _selfupdate_loop(self) -> None:
        """启动 `FIRST_CHECK_AFTER_SEC` 后先查一次，之后每 `CHECK_EVERY_SEC` 一次；有新版且空闲就装上。

        第 30 轮：原来先睡满 10 分钟 ⇒ 刚装完 / 刚重启的这台 10 分钟内不上报、也不跟版本。
        🔴 一拍失败不能让循环死掉：更新是锦上添花，桥本身照旧要能用。
        """
        from . import selfupdate

        wait = selfupdate.FIRST_CHECK_AFTER_SEC
        while True:
            time.sleep(wait)
            wait = selfupdate.CHECK_EVERY_SEC
            if self.updater is None:
                return
            self._updater_learn_app_id()
            try:
                done = self.updater.tick()
            except Exception:  # noqa: BLE001
                logger.exception("自动更新这一拍失败，下一拍照常")
                continue
            # 第 32 轮：新版没装上（重启后还是旧版 / 安装器自己退回了）⇒ 主动告诉主人一句
            self._tell_update_not_applied()
            if done is not None and done[0]:
                # 装成功了 ⇒ 这个进程马上会被安装器重启掉，不用自己退
                logger.info("自动更新完成：%s", done[1])

    def _tell_update_not_applied(self) -> None:
        """只被 `_selfupdate_loop` 调。有「新版没装上」还没说过 ⇒ 往主人单聊发一句；发到了才记「说过」。

        🔴 发不出去不记 ⇒ 下一拍再试（和开场消息同一条纪律：发成功才写标记）。
        """
        take = getattr(self.updater, "take_notice", None)
        if take is None:
            return
        try:
            notice = take()
            if notice is None:
                return
            build, text = notice
            owners = sorted(getattr(self.settings, "owner_open_ids", None) or ())
            sent = False
            for owner in owners:
                sent = bool(self._say(owner, text)) or sent
            if sent:
                self.updater.mark_told(build)
            else:
                logger.info("「新版没装上」这句没发出去（没主人或发送失败），下一拍再试")
        except Exception:  # noqa: BLE001 —— 提醒是锦上添花，不许把更新循环带崩
            logger.exception("「新版没装上」提醒失败")

    def _update_on_demand(self, chat_id: str) -> None:
        """他在飞书里发「更新」⇒ 立刻查 + 装，结果如实回他。跑在自己的线程里。"""
        if self.updater is None:
            self._say(chat_id, "这台机器上的自动更新没起来，要人工重新跑一次安装包。")
            return
        self._updater_learn_app_id()
        try:
            info = self.updater.check(manual=True)
        except Exception:  # noqa: BLE001
            logger.exception("按需查新版失败")
            self._say(chat_id, "查新版本的时候出错了，过一会儿再发「更新」试试。")
            return
        if info is None:
            why = self.updater.last_error
            current = self.updater.current_build() or "（这台没有版本号，多半是开发机）"
            self._say(chat_id, f"已经是最新的了（{current}）。" if not why else f"没查到能装的新版：{why}")
            return
        if info.get("manual_only"):
            # 第 30 轮：Windows 上自动装还没在真机上跑过 ⇒ 不下载，给他链接
            self._say(chat_id, "Windows 上我还不能自己装，点这里下载："
                               f"{self.updater.download_url(info)}，下载完双击一次就好")
            return
        if not self.updater.is_idle():
            self._say(chat_id, f"有新版 {info['build_id']}，但现在有活在跑。等它跑完我自己装，不用你管。")
            return
        self._say(chat_id, f"正在更新到 {info['build_id']}…装完我会重启一下，期间几秒收不到消息。")
        ok, text = self.updater.update_now(info)
        sent = self._say(chat_id, ("✅ " if ok else "❌ ") + text)
        if not ok and sent and getattr(self.updater, "last_error", "") == "rolled_back":
            # 第 32 轮：刚才那句就是「新版没装上，已经退回」⇒ 更新循环别再说一遍
            try:
                self.updater.mark_told(info["build_id"])
            except Exception:  # noqa: BLE001
                logger.debug("标记「没装上已告知」失败", exc_info=True)

    def _notify_waiting_loop(self) -> None:
        """后台每 10 秒扫一次。跑在自己的 daemon 线程里（扫描是阻塞的，别占事件循环）。

        🔴 一拍失败不能让循环死掉，更不能让桥崩 —— 这里兜一层，`_notify_tick` 里面再兜一层。
        """
        while True:
            time.sleep(_NOTIFY_TICK_SEC)
            try:
                self._notify_tick()
            except Exception:  # noqa: BLE001 —— 推送是锦上添花
                logger.exception("「等你」推送这一拍失败，下一拍照常")

    def _notify_tick(self) -> None:
        """扫一次 → 问 notifier 这一拍该推谁 → 往主人单聊发卡（发不出卡退文字）。

        扫描复用总控现成的 `FleetControl.overview()`：顺带刷新按钮白名单 ⇒ 卡上「看看它」点得动。
        """
        notifier = getattr(self, "waiting_notifier", None)
        control = getattr(self, "fleet", None)
        pusher = getattr(self, "group_pusher", None)
        waiting_on = notifier is not None and notifier.enabled
        group_on = pusher is not None and pusher.enabled
        if control is None or not (waiting_on or group_on):
            return
        try:
            # 和主人点「会话」共用一次扫描（第 11 轮）：撞上时谁后到谁等，不再互相把对方那一家「跳过」
            view = self._fleet_scanner().overview()
        except Exception:  # noqa: BLE001 —— 扫描失败 = 这一拍不推
            logger.exception("「等你」推送：扫描失败，这一拍跳过")
            return
        if group_on:
            try:
                self._group_push_tick(view)
            except Exception:  # noqa: BLE001 —— 群推送出错不能拖累单聊那条
                logger.exception("群进展推送这一拍失败")
        try:
            self._status_cards_tick()
        except Exception:  # noqa: BLE001
            logger.exception("状态卡这一拍失败")
        try:
            self._escalate_tick(view)
        except Exception:  # noqa: BLE001
            logger.exception("加急这一拍失败")
        if not waiting_on:
            return
        try:
            due = notifier.due(view)
        except Exception:  # noqa: BLE001
            logger.exception("「等你」推送：判断出错，这一拍跳过")
            return
        if not due:
            return
        from .fleet.notify import build_waiting_card, waiting_fallback_text

        owners = sorted(self.settings.owner_open_ids or ())
        for sess in due:
            ok = False
            try:
                card = build_waiting_card(sess, redact=_redact_text)
                text = waiting_fallback_text(sess, redact=_redact_text)
                # 主人单聊：按 open_id 发（replier 按 `ou_` 前缀选 receive_id_type=open_id）
                for owner in owners:
                    ok = self._send_card_or_text_ok(owner, card, text) or ok
            except Exception:  # noqa: BLE001
                logger.exception("「等你」推送：发送出错")
            notifier.record(sess.key, ok)
            # 只记工具和短 id，不记标题（标题是用户项目里的文字）
            logger.info("「等你」推送 tool=%s 会话=%s 送达=%s", sess.tool, str(sess.id)[:8], ok)

    def _group_push_tick(self, view) -> None:
        """绑定群：要人介入的一定推；做完一轮推结果摘要；群里自己发进去的那轮不重复推。"""
        pusher = getattr(self, "group_pusher", None)
        binds = getattr(self, "chat_binds", None)
        control = getattr(self, "fleet", None)
        if pusher is None or binds is None or control is None:
            return
        pushes = pusher.due(view, binds)
        if not pushes:
            return
        from .fleet.cards import project_of, session_label
        from .fleet.grouppush import build_push_text

        for push in pushes:
            sess = push.sess
            last_reply = ""
            if push.kind == "done":
                try:
                    _fresh, messages, _note = control.detail(sess.tool, sess.id, 4)
                    for msg in reversed(messages or []):
                        if getattr(msg, "role", "") == "assistant" and getattr(msg, "text", ""):
                            last_reply = msg.text
                            break
                except Exception:  # noqa: BLE001
                    logger.debug("群进展推送：读最后一条回复失败", exc_info=True)
            try:
                label = session_label(sess, _redact_text)
            except Exception:  # noqa: BLE001
                label = sess.tool
            project = _redact_text(project_of(getattr(sess, "cwd", "") or ""))
            text = build_push_text(push, label, project, last_reply, redact=_redact_text)
            if push.kind in ("waiting", "failed"):
                ok = self._push_needs_you(push, text)
            else:
                ok = self._say(push.chat_id, text)
            logger.info("群进展推送 类型=%s tool=%s 会话=%s 送达=%s", push.kind, sess.tool,
                        str(sess.id)[:8], ok)

    def _escalator(self):
        esc = getattr(self, "_needs_you_escalator", None)
        if esc is None:
            from .fleet.escalate import Escalator

            esc = self._needs_you_escalator = Escalator()
        return esc

    def _push_needs_you(self, push, text: str) -> bool:
        """需要人介入的推送：@ 主人；「在等你」记下 message_id，10 分钟没处理加急一次（§5.8）。"""
        from .fleet.escalate import at_owner_line
        from .fleet.grouppush import pending_key

        owners = getattr(getattr(self, "settings", None), "owner_open_ids", None) or ()
        at = at_owner_line(owners)
        body = f"{at} {text}" if at else text
        sender = getattr(getattr(self, "replier", None), "send_md_id", None)
        mid = None
        if sender is not None:
            try:
                mid = sender(push.chat_id, body)
            except Exception:  # noqa: BLE001
                logger.exception("带 @ 的推送发送抛异常，退成普通消息")
                mid = None
        if not mid:
            return bool(self._say(push.chat_id, text))
        if push.kind == "waiting":
            self._escalator().track((push.sess.tool, push.sess.id), push.chat_id, mid,
                                    pending_key(push.sess) or "-")
        return True

    def _escalate_tick(self, view) -> None:
        """每拍：还在等、过了 10 分钟、没加急过的那件事 ⇒ 加急一次。"""
        from .fleet.grouppush import pending_key

        esc = getattr(self, "_needs_you_escalator", None)
        if esc is None or not esc.pending():
            return
        waiting = {}
        for sess in list(getattr(view, "sessions", None) or []):
            if getattr(sess, "status", "") == "waiting":
                waiting[(sess.tool, sess.id)] = pending_key(sess) or "-"
        owners = getattr(getattr(self, "settings", None), "owner_open_ids", None) or ()
        urge = getattr(getattr(self, "group_client", None), "urgent", None)
        for item in esc.due(waiting):
            ok, code = False, 0
            if urge is not None:
                try:
                    ok, code, _msg = urge(item.message_id, owners)
                except Exception:  # noqa: BLE001
                    logger.exception("加急抛异常")
            logger.info("需要人介入 加急 chat=%s 成功=%s code=%s", item.chat_id[:12], ok, code)
            if not ok:
                self._say(item.chat_id, "⏰ 这件事已经等了 10 分钟还没人处理，请尽快看一下。")

    def _send_card_or_text_ok(self, chat_id: str, card: str, fallback: str) -> bool:
        """同 `_send_card_or_text`，但告诉调用方到底送没送到（限流要知道）。"""
        sender = getattr(getattr(self, "replier", None), "send_card", None)
        try:
            if sender is not None and sender(chat_id, card):
                return True
        except Exception:  # noqa: BLE001
            logger.exception("卡片发送抛异常，退成文字")
        return bool(self._say(chat_id, fallback))

    def _ws_thread(self, ws_client) -> None:
        """跑 `ws_client.start()`，并且**把它的死法记下来**。

        🔴 2026-11-05 实测：`start()` 里 `ClientException` 是直接 `raise e`
        （`lark_oapi/ws/client.py:187-189`，不重连、不降级）。它跑在 daemon 线程里，
        所以线程死了、主进程照常活着 ⇒ 用户看到的是"服务在跑，但机器人不理我"，
        日志里只有一行 SDK 的 `connect failed`，没有一句说明该怎么办。

        实测复现（monkeypatch `_get_conn_url` 抛 1000040350 后）：
            ws 线程还活着吗: False / 连接对象 _conn: None / 主进程照常活着

        看门狗会在静默 15 分钟后退出等重启，这是对的 —— 但**重启治不好**
        连接数到顶和凭据被拒这两种死法，重启只会每 15 分钟重来一次。
        所以这里按错误码把原因写清楚，让日志和 doctor 能直接给出下一步。
        """
        try:
            ws_client.start()
        except BaseException as exc:  # noqa: BLE001 - 线程边界，什么都不能往外抛
            code = getattr(exc, "code", None)
            self._ws_fatal = f"{code}: {exc}" if code is not None else str(exc)
            logger.error("长连接线程退出（%s）：%s", type(exc).__name__, exc)
            logger.error("%s", explain_ws_failure(code))

    def should_self_exit(self, threshold_sec: float | None = None) -> bool:
        """静默太久了该不该自杀。**单独拆出来是为了能被测**。

        看门狗是整个方案的核心保险（lark-oapi 的重连会静默卡死，进程活着但再也
        收不到消息），而 15 分钟的阈值在这之前从来只是读代码读出来的 ——
        想在真进程上观测它，要么等 15 分钟、要么制造一次真的半开连接，
        两条都不适合放进测试。所以把判断和循环分开，判断这部分测死。

        🔴 **"静默"必须指「连接不通」，不能指「用户没说话」。**
        2026-11-03 查出来的缺陷：`touch()` 原来只有两个调用方 —— 收到消息、
        点了卡片。SDK 的心跳 / pong **不喂这个时钟**。后果是用户一整天不发消息
        （完全正常），服务每 15 分钟自杀一次；更糟的是日志里会写满
        「长连接已静默」，把真正的 #169 故障埋进噪音里。
        所以这里先探活连接：连接对象还在就视为健康，只在**连接真的没了**
        之后才按静默时长判。
        """
        limit = WATCHDOG_SILENT_SEC if threshold_sec is None else threshold_sec
        # 🔴 第 34 轮：先看快判据（SDK 循环卡死 / 心跳回包停了）。有任务在跑就先不动手 ——
        # 回复走 HTTP 接口能送达，腰斩它才是真丢；那一轮结束后下一拍再判。
        reason = self.stuck_reason()
        if reason and not self._work_in_flight():
            self._watchdog_reason = reason
            return True
        if self.connection_alive():
            self._last_alive_at = time.monotonic()
            return False
        down_for = self._down_for()
        # 🔴 有任务正在跑就宽限。连接断开时自杀会**腰斩那一轮**，结果全丢 ——
        # 用户看到的是「发了消息，状态卡停住，再也没有回复」，比多等一会儿更糟。
        # 而 SDK 的重连往往几秒就成了，没必要在这个窗口里动手。
        if self._work_in_flight() and down_for < limit * _INFLIGHT_GRACE:
            return False
        # 还要求同时这么久没收到任何飞书事件：事件还在进来就说明连接其实是通的（探活认不出的时候）。
        if down_for > limit and self.silent_for > limit:
            self._watchdog_reason = f"长连接连续断了 {down_for:.0f}s 没连回来（阈值 {limit:.0f}s）"
            return True
        return False

    def _down_for(self) -> float:
        """长连接**连续**断了多久 = 离「最后一次看见它活着」多久。

        🔴 第 35 轮（主人 11-10 同意）：兜底原来按「多久没收到消息」判 ⇒ 用户空闲 15 分钟以上时，
        看门狗这一拍恰好碰上一次正常重连（断开 0.2 秒、刚连上 61 秒）就会被判死 ——
        本机 agent.log 11 次「长连接已静默」里多数是这种误杀。
        看门狗每拍（15 秒）看到连接活着就记一笔，所以一次正常重连只会算出十几秒。
        - 只用 monotonic：睡眠期间不走 ⇒ 刚睡醒给 SDK 一个完整窗口去重连。
        - 这个进程还**从没**看见过连接活着（刚起来就连不上）⇒ 退回原来的静默时长。
        """
        last_alive = getattr(self, "_last_alive_at", None)
        if last_alive is None:
            return self.silent_for
        return time.monotonic() - max(last_alive, self._last_event_at)

    def _watchdog_note_clock(self) -> None:
        """每拍记一次两个时钟。墙钟比 monotonic 多走一截 ⇒ 刚睡醒 ⇒ 快判据重新计时。

        🔴 不重新计时的后果：DarkWake（合盖时每隔十几分钟醒 45 秒做维护，本机 pmset 里一晚几十次）
        会让 monotonic 一点点涨，而那几十秒里网络常常还没起来（本机日志里 DarkWake 期间
        `NameResolutionError` 22 次）⇒ 醒一次就可能被判成「心跳停了」。
        """
        mono, wall = time.monotonic(), time.time()
        last = getattr(self, "_watchdog_clock", None)
        self._watchdog_clock = (mono, wall)
        if not last:
            return
        jump = (wall - last[1]) - (mono - last[0])
        if jump > WATCHDOG_SLEEP_JUMP_SEC:
            self._watchdog_awake_since = mono
            probe = getattr(self, "_loop_probe", None)
            if probe is not None:
                probe.reset()
            self._loop_stall = None
            logger.info("电脑刚睡醒（睡了约 %.0f 秒），卡死检测重新计时", jump)

    def stuck_reason(self) -> str:
        """快判据：有**现场证据**说长连接卡死了 ⇒ 返回一句原因；没证据 / 拿不到 ⇒ 空串。

        空串的意思是「不知道」，不是「活着」—— 后面还有 `connection_alive()` 和 15 分钟静默兜底。
        跑在看门狗线程里 ⇒ 任何异常都吞掉返回空串。
        """
        try:
            from . import wsfast

            now = time.monotonic()
            awake_for = now - getattr(self, "_watchdog_awake_since", float("-inf"))
            stall = getattr(self, "_loop_stall", None)
            if stall is not None and min(stall, awake_for) > WATCHDOG_LOOP_STALL_SEC:
                return f"飞书 SDK 的事件循环 {stall:.0f}s 没转（阈值 {WATCHDOG_LOOP_STALL_SEC}s，多半卡在无超时的重连里）"
            client = getattr(self, "_ws_client", None)
            if client is None or not self.connection_alive():
                return ""
            age = wsfast.pong_age(client, now)
            limit = wsfast.pong_limit(client)
            if age is not None and min(age, awake_for) > limit:
                return f"当前连接 {age:.0f}s 没收到飞书心跳回包（阈值 {limit:.0f}s）"
        except Exception:  # noqa: BLE001 —— 看门狗线程里绝不能抛
            logger.debug("快判据探测失败", exc_info=True)
        return ""

    def _work_in_flight(self) -> bool:
        """有没有正在跑的轮次（会话锁被持着；或有审批卡在等、而且重启会把它的续跑弄丢）。

        🔴 第 30 轮：原来只要 `_awaiting` 非空就算有活 ⇒ 一张没人点的审批卡让这台**永远不更新**。
        审批卡在等人点时 claude 那一轮已经结束了，重启不腰斩任何东西；点旧卡续跑靠的是钩子落盘的
        `session_id`（`gate.pending_session_id`，第 24 轮）——重启后照样接得上。
        所以只有「盘上**找不到**能接上的会话 id」的那种等审批才算有活（见 `_awaiting_resume_would_be_lost`）。

        这个方法跑在**看门狗线程**里，所以任何异常都不能往外抛 ——
        抛了会让看门狗线程死掉，而它是对 lark-oapi 静默卡死的唯一保险。
        探不出来就返回 False（按"没活在身"处理，让静默阈值正常生效）。
        """
        try:
            sessions = getattr(getattr(self, "sessions", None), "_sessions", {}) or {}
            for session in list(sessions.values()):
                lock = getattr(session, "lock", None)
                if lock is not None and lock.locked():
                    return True
            awaiting = getattr(self, "_awaiting", None) or {}
            return any(self._awaiting_resume_would_be_lost(scope) for scope in list(awaiting))
        except Exception:  # noqa: BLE001 - 看门狗线程里绝不能抛
            logger.debug("探测在跑任务失败", exc_info=True)
            return False

    def _awaiting_resume_would_be_lost(self, scope: str) -> bool:
        """这个会话在等审批；现在重启，点旧卡还接得上那一轮吗？接不上 ⇒ True（当有活，别重启）。

        接得上 = 盘上有这个会话的未决记录（`.asked` / `.pending`）且里面有钩子记下的 `session_id` ——
        正是 `on_card_action` 重启后走的那条路（`gate.pending_session_id` → `_pin_resume`）。
        探不出来一律按「会丢」处理（往「少重启」走永远安全）。
        """
        try:
            directory = self.gate.dir_for(scope)
            for name in os.listdir(directory):
                stem, dot, suffix = name.rpartition(".")
                if dot and suffix in ("asked", "pending") and self.gate.pending_session_id(scope, stem):
                    return False
        except Exception:  # noqa: BLE001
            logger.debug("探测审批续跑记录失败", exc_info=True)
        return True

    def _updater_learn_app_id(self) -> None:
        """把这台的飞书 app_id 交给自动更新（只用来上报对上人；app_secret 绝不给它）。"""
        try:
            if self.updater is not None and not getattr(self.updater, "app_id", ""):
                self.updater.app_id = str(getattr(self.settings, "app_id", "") or "")
        except Exception:  # noqa: BLE001 —— 上报是锦上添花
            logger.debug("取 app_id 失败，这次上报不带", exc_info=True)

    def _greet_in_background(self) -> None:
        """扫码装完那条开场消息（实现和文案在 `pairing.py`）。

        这里只做接线，所有判断都在 `pairing.greet_once` 里：没主人不发、连不上先等、
        发成功才写标记、最多试 `MAX_GREET_TRIES` 次（兜「发出去了但标记没落盘」）。

        🔴 失败一律吞掉：开场消息发不出去最多是用户少看到一条介绍，
        **不能让它挡住服务启动**（那是把「少一条说明」换成「整个功能没了」）。
        """
        try:
            from .pairing import GREETING_STATE_FILENAME, GreetingState, greet_in_background

            state_path = os.path.join(
                os.path.dirname(self.settings.config_path or ""), GREETING_STATE_FILENAME
            )
            greet_in_background(
                GreetingState(state_path),
                owner_open_ids=self.settings.owner_open_ids,
                connection_alive=self.connection_alive,
                send=self._say_with_buttons,
            )
        except Exception:  # noqa: BLE001 —— 开场消息不值得让桥起不来
            logger.exception("开场消息没能起来，跳过")

    def _new_streamer(self, chat_id: str):
        """造这一轮的流式卡片对象。造不出来 ⇒ 一个「永远 broken」的壳，调用方不用判空。

        🔴 失败不抛：流式是增益，缺权限（老用户都缺）/SDK 版本不对都只该让这一轮
        安静回到「跑完发一条」。
        """
        try:
            from .stream import StreamReply

            return StreamReply(self._client_for_cards(), chat_id,
                               enabled=self._streaming_on())
        except Exception:  # noqa: BLE001
            logger.info("流式回答没起来，这一轮用普通回复", exc_info=True)

            class _Off:
                broken = True
                live = False
                pushes = 0

                async def start(self):
                    return False

                async def push(self, _text):
                    return False

                async def finish(self, _text, footer=""):
                    return False

                def idle_sec(self):          # ticker 每轮都调（第 24 轮：缺这个 ⇒ 90 秒后整轮崩）
                    return None

                async def abandon(self):
                    return None

                def shown_text(self):
                    return ""

            return _Off()

    def _client_for_cards(self):
        """发卡片用的 SDK 客户端。和 `Replier` 用同一个，不另建连接。"""
        return getattr(self, "client", None) or getattr(self.replier, "_client", None)

    def _streaming_on(self) -> bool:
        """流式总开关。配置里 `[agent] streaming = false` 可以关掉。

        默认开：没权限时它自己会安静退回，所以开着不会有坏处。
        """
        raw = getattr(self.settings, "streaming", None)
        return True if raw is None else bool(raw)

    def _tell_about_migration(self) -> None:
        """这次升级把他从「只看」挪到了全控制 ⇒ 告诉他一次（`migrated_from` 由 carryover 写）。

        🔴 悄悄改一个人的权限档是不行的。自动更新在后台跑、没人看安装器的屏幕，
        所以只能由桥来说。说过一次就把那行从配置里删掉，别每次重启都刷一遍。
        """
        try:
            import tomllib

            from .carryover import MIGRATED_KEY
            from .selfcheck import migration_notice

            path = self.settings.config_path or ""
            if not path or not os.path.exists(path):
                return
            with open(path, "rb") as handle:
                agent = (tomllib.load(handle).get("agent") or {})
            migrated_from = str(agent.get(MIGRATED_KEY) or "")
            if migrated_from == "readonly":
                # 🔴 先清掉落盘的旧只读档位，再说话。
                # 现场（2026-11-07 14:04 用户截图）：配置迁移成全控制了，但 `prefs.json` 里
                # 老版本留下的 `{"mode":"readonly"}` 把它顶掉 ⇒ 卡片还显示「现在是 只读」。
                # `PrefsStore` 只按上限收敛、不看 config 里的档，所以不清就白迁移。
                from .prefs import clear_readonly_override

                if clear_readonly_override(os.path.join(
                        os.path.dirname(path), "prefs.json")):
                    store = getattr(self, "prefs", None)
                    if store is not None and hasattr(store, "reload"):
                        try:
                            store.reload()
                        except Exception:  # noqa: BLE001
                            logger.debug("重读档位设置失败，下次重启生效", exc_info=True)
            text = migration_notice(migrated_from, self.policy.mode)
            if migrated_from and not text:
                # 迁移记录在、但这台现在不是放开状态（比如 IT 策略锁了只读）⇒ 没什么可说的。
                # 🔴 第 19 轮（S4 缺口 3）：原来这里直接 return、**不擦**标记 ⇒ 哪天 IT 放开，
                # 桥会说一句已经过时的「这次升级把权限放开了」。没什么可说就把记录擦掉。
                self._erase_migration_marker(path)
                return
            if not text:
                return
            from .fleet.actions import do_button

            # 🔴 第 19 轮（S4 缺口 1）：**至少一个主人确实收到了**才擦标记。
            # 原来走 `_say_later_with_buttons`（异步投递，拿不到结果）然后无条件擦 ⇒
            # S4 实测：发送失败时日志两条「消息没送达」，标记照样被擦，下次启动 0 条通知，
            # 档位已是全授权 ⇒ **他被悄悄从只读升到了全控制**，这正是这段代码要防的事。
            # 改成同步发、看返回值；一个都没送达 ⇒ 留着标记，下次启动再说。
            # 还没绑主人 ⇒ 也留着，等配对绑定后再说。
            # 同步发会占启动的一小会儿：只在迁移后的那一次启动发生，可以接受。
            delivered = False
            for owner in sorted(self.settings.owner_open_ids):
                chat = self._chat_for(f"dm:{owner}") or owner
                try:
                    if self._say_with_buttons(chat, text, [do_button("mode", primary=True)]):
                        delivered = True
                except Exception:  # noqa: BLE001 —— 这一个人没送达，看下一个
                    logger.info("档位变更通知没送达 owner=%s", owner[:12], exc_info=True)
            if not delivered:
                logger.info("档位变更通知一个主人都没送达，留着记录下次再说")
                return
            self._erase_migration_marker(path)
        except Exception:  # noqa: BLE001 —— 少说一句话不能让桥起不来
            logger.exception("档位变更通知没发出去")

    def _erase_migration_marker(self, path: str) -> None:
        """把配置里 `migrated_from` 那一行删掉（整行删，不动别的键；原子替换）。"""
        try:
            from .carryover import MIGRATED_KEY
            with open(path, encoding="utf-8") as handle:
                lines = handle.readlines()
            kept = [ln for ln in lines if not re.match(rf"^\s*{MIGRATED_KEY}\s*=", ln)]
            if len(kept) != len(lines):
                import tempfile

                directory = os.path.dirname(path)
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                                 delete=False) as tmp:
                    tmp.write("".join(kept))
                    temp_name = tmp.name
                os.chmod(temp_name, 0o600)
                os.replace(temp_name, path)
        except Exception:  # noqa: BLE001
            logger.exception("擦迁移记录失败（下次启动会再说一次）")

    def connection_alive(self) -> bool:
        """长连接是否还活着。

        判据是 SDK 客户端内部那个连接对象还在不在 —— 比"多久没收到消息"准得多，
        因为后者在用户空闲时也会一路涨。拿不到就返回 False，交给静默时长兜底
        （宁可多重启一次，也不要静默失联三天）。
        """
        client = getattr(self, "_ws_client", None)
        if client is None:
            return False
        for attr in ("_conn", "conn", "_connection"):
            conn = getattr(client, attr, None)
            if conn is None:
                continue
            # 🔴 第 15 轮：原来是「取 `closed`，取不到就 return True」——
            # 本机 websockets **15.0.1 的 ClientConnection 根本没有 `closed`**
            # （实测 `hasattr(..., "closed") is False`，只有 `state`/`close_code`）
            # ⇒ 那一行是**永真** ⇒ `should_self_exit()` 第一行就 return False
            # ⇒ 看门狗（lark-oapi #169 静默卡死的唯一兜底）从来没真正生效过。
            # R1 用真连接验过：关掉之后它仍返回 True，`state` 是 `State.CLOSED`(3)。
            # 现在优先按 `state` 判，老版本的 `closed` 照旧认，两样都没有才退回
            # 「有对象就算活着」（那是认不出，交给静默时长兜底）。
            state = getattr(conn, "state", None)
            if state is not None:
                value = getattr(state, "value", state)   # enum 和裸 int 都认
                try:
                    return int(value) == 1               # websockets State.OPEN
                except (TypeError, ValueError):
                    return True
            closed = getattr(conn, "closed", None)
            if closed is None:
                return True
            return not closed
        return False

    def _watchdog_loop(self) -> None:
        """见文件头第 4 条：SDK 自己可能静默卡死，只能从外面看。

        注意这里只负责**退出**，重启交给外面的 launchd/supervisor。进程自己拉自己
        起来是不可靠的 —— 要重启的恰恰是已经不正常的那个进程。
        """
        from .wsfast import LoopProbe

        self._loop_probe = LoopProbe()
        while True:
            time.sleep(WATCHDOG_TICK_SEC)
            self._watchdog_tick()

    def _watchdog_tick(self) -> None:
        """看门狗的一拍（拆出来给测试用）：更新时钟和探针 ⇒ 判 ⇒ 该退就退。"""
        try:
            self._watchdog_note_clock()
            probe = getattr(self, "_loop_probe", None)
            self._loop_stall = probe.tick() if probe is not None else None
        except Exception:  # noqa: BLE001 —— 看门狗线程里绝不能抛
            logger.debug("看门狗探针失败", exc_info=True)
            self._loop_stall = None
        self._watchdog_reason = ""
        if self.should_self_exit():
            # 🔴 「长连接已静默」这几个字别改：`bridge/status.py` 的 ERROR_CLASSES 靠它数「看门狗重启」
            logger.error(
                "长连接已静默 %.0fs，判定卡死：%s。主动退出等待重启",
                self.silent_for,
                getattr(self, "_watchdog_reason", "") or "原因未知",
            )
            os._exit(75)  # EX_TEMPFAIL：告诉 supervisor 这是可重试的故障


def _load_or_create_secret(home: str) -> bytes:
    """审批令牌的签名密钥，落盘复用。

    放在 0600 的文件里。拿不到就退化成进程内随机 —— 那样旧卡片会失效，
    但至少不影响新卡片能用（宁可少一个便利，不要整层挂掉）。
    """
    path = os.path.join(home, "approval.secret")
    try:
        with open(path, "rb") as handle:
            secret = handle.read()
        if len(secret) >= 32:
            return secret
    except OSError:
        pass
    secret = os.urandom(32)
    try:
        os.makedirs(home, exist_ok=True)
        # 先写临时文件再改名，并且创建时就是 0600 —— 别出现"短暂可读"的窗口
        fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secret)
        os.replace(path + ".tmp", path)
    except OSError:
        logger.warning("审批密钥写不进磁盘，重启后旧卡片会失效")
    return secret


def _acquire_single_instance_lock() -> object:
    """飞书长连接是随机投递 —— 跑两个进程消息会随机漂走。用文件锁硬挡。

    Windows 没有 flock，换 msvcrt；细节在 osenv.py。
    """
    from .osenv import SingleInstanceError, acquire_single_instance

    try:
        return acquire_single_instance()
    except SingleInstanceError as exc:
        raise SystemExit(str(exc)) from exc


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # SDK 的 `[Lark]` handler 在 import lark_oapi 时就建好了；两边的 handler 都要过滤（见 redact.LogScrubFilter）
    from .redact import install_log_scrubber
    install_log_scrubber()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.environ.get("FEISHU_BRIDGE_CONFIG") or os.path.join(here, "config.toml")
    if not os.path.exists(config_path):
        raise SystemExit(
            f"没找到配置文件 {config_path}\n先 cp config.example.toml config.toml 再填。"
        )

    lock = _acquire_single_instance_lock()  # 要一直持有，别让它被 GC
    settings = Settings.load(config_path)
    bridge = Bridge(settings)
    try:
        bridge.run()
    except KeyboardInterrupt:
        logger.info("收到中断，退出")
    finally:
        del lock


if __name__ == "__main__":
    sys.exit(main())
