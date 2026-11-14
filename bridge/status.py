"""一张原地更新的状态卡，代替"刷消息流"。

为什么要有它：原来长任务只有两条「⏳ 还在跑（90s）」。用户看不到它在干什么，
也没法叫停。三个竞品（Claude Duet / OpenAI dots / Codex）全都收敛到
**一个持久的状态对象 + 一个停止按钮**，而不是往聊天流里追加消息。

数据全是现成的 —— `runner.Progress` 一直在数工具调用和记当前工具，只是以前
`on_progress` 没有任何调用方，算完就扔了。

## 两条平台约束决定了这个文件的形状

1. **卡片 30KB 上限，超了飞书静默丢弃**（不报错）。所以状态卡要小：几百字节，
   只放进度，不放结果。结果仍然走纯文本（150KB 额度）。
2. **更新卡片用 `im.v1.message.patch`**，要先有 `message_id`。所以第一次是
   `create`、之后都是 `patch`。patch 失败不能影响正事 —— 状态卡是锦上添花，
   它坏了不该让这一轮任务失败。

## 刻意不做的

- 不发按钮。飞书卡片按钮的回调要走 `card.action.trigger`，而「停下」用
  `/stop` 文字命令就够了，省掉一条回调路径和它的令牌校验。
- 不做流式 token 输出。飞书流式卡片正是 `230099` 那串失败的来源，
  状态卡以极小代价解决了"沉默"这个真问题。
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("feishu-bridge.status")


# ======================================================================
# 诊断卡（`/诊断`）：让"出问题了但没人看得见"变成能远程判的事
# ======================================================================
#
# 今天日志只在同事那台电脑的 `~/.carher-feishu-agent/logs/` 里。他说"坏了"的时候，
# 你和 IT 手上**一个字节都没有** —— 只能让他去双击自检脚本、再把窗口截图发来，
# 而最常见的两种故障（长连接静默失联、claude 跑不通）截图上看不出区别。
#
# 🔴 **白名单，不是黑名单。** 这张卡的内容只能由 `build_health_card` 从
# `HEALTH_FIELDS` 里**逐个字段取**，取不到就不显示。不许"把某个 dict 整个塞进来
# 再过滤掉敏感的" —— 那是黑名单，新增一个字段就默认外发，而谁都不会记得回来加规则。
# 对照 redact.py：那一层按"形状"认凭据，是**出口兜底**；这一层是**入口限额**，
# 两层方向相反、都要有。
#
# 永不进这张卡（逐条都有真实理由，不是凭空谨慎）：
#   - 用户/agent 的消息正文、prompt、回复、工具输出、会话标题
#     ⇒ 这张卡会被同事转发到群里给 IT 看，等于把他的工作内容发布出去
#   - `app_secret` / `approval.secret` / `auth.json` / `*.key` 的任何内容或长度
#   - 绝对路径（`/Users/<真名>/…` 泄露同事真名；只给 `~` 相对形式）
#   - `open_id` / `chat_id` / `message_id` / ws `ticket`+`access_key`
#     ⇒ 实测 `connected to wss://…?ticket=…` 整条就在日志里（见 collect_health 注释）
#   - 日志原文行（任何一行都可能带上面这些东西）⇒ 只给**计数**和**错误类名**
#
# 为什么够用：远程判故障要的是"哪个版本、活了多久、连着没、最后一类错是什么、
# 各类错多少条"。这五样全是定长的枚举/数字，没有一样需要带正文。

# 允许出现在诊断卡上的字段：键 → 这一行怎么渲染。
# 🔴 新增字段必须同时在这里和 `collect_health` 里登记，否则不显示 ——
# "忘了加白名单"的后果是少一行信息（安全），而不是多发一行（危险）。
HEALTH_FIELDS: tuple[tuple[str, str], ...] = (
    ("version", "🏷 版本 {}"),
    ("platform", "💻 系统 {}"),
    ("uptime", "⏱ 已运行 {}"),
    ("connection", "{}"),
    ("menu", "📌 常驻菜单 {}"),
    ("mode", "🔑 权限 {}"),
    ("model", "🧠 模型 {}"),
    ("workspaces", "📂 项目 {} 个"),
    ("last_error", "⚠️ 最后一类错 {}"),
    ("error_counts", "📊 错误计数 {}"),
    ("restarts", "🔁 重启 {} 次"),
    ("log_age", "🕐 日志最后动静 {}"),
    # 🔴 第 15 轮：「有人跟这台电脑说话，但他不是主人」。只给**个数**，绝不给是谁
    # （open_id 在永不进卡那张表里）。2026-11-06 现场：两条消息被静默丢弃，
    # 两边都没有任何线索 ⇒ 「他找错了机器人」查不出来。
    ("knocks", "🚪 有人找错这台电脑 {}"),
    # 第 15 轮：这个进程起来之后断过几次（平时不出现）
    ("reconnects", "🔌 断线重连 {}"),
)

# 每个值最多这么长。截断是**第二道**保险：万一哪天有人往 version 里塞了一整行日志，
# 卡片上也只会出现一小截，而不是把整行外发。
HEALTH_VALUE_MAX = 60


def health_lines(data: dict) -> list[str]:
    """按白名单把 `data` 渲染成几行字。不在 `HEALTH_FIELDS` 里的键**直接丢掉**。

    值一律 `str()` + 截断 + 过一遍 `redact`：
    - `str()` 保证塞进来一个 dict/对象也不会把它的内部结构铺开
    - `redact` 是出口兜底（和所有发飞书的文本同一层），不是这里的主要防线
    """
    from .redact import redact

    lines: list[str] = []
    for key, template in HEALTH_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if value is None or value == "":
            continue
        text = str(value).replace("\n", " ").strip()[:HEALTH_VALUE_MAX]
        text, _ = redact(text)
        if text:
            lines.append(template.format(text))
    return lines


# 日志里的错误**归类**。键是给人看的类名，值是认它的子串。
#
# 🔴 只回传类名和计数，**绝不回传原文行**。2026-11-05 在本机实测的日志里，
# `connected to wss://…?…&access_key=…&ticket=…` 整条就躺在 INFO 行里；
# `open_id=ou_…`、`审批钩子已装：/Users/<真名>/…` 也在。任何"带上最后一行报错"
# 的设计都会把这些一起外发，所以这里走分类计数，不走摘录。
ERROR_CLASSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("连不上飞书", ("connect failed",)),
    ("长连接断开", ("receive message loop exit", "keepalive ping timeout")),
    ("看门狗重启", ("长连接已静默",)),
    ("卡片发不出", ("卡片发送失败",)),
    ("消息发不出", ("飞书回消息失败", "回复发送失败")),
    ("凭据/权限被拒", ("99991672", "99991663", "10003", "10013")),
    ("助手没回话", ("零输出",)),
)


def build_id(package_dir: str) -> str:
    """安装器在装机那一刻写进 `bridge/buildinfo.py` 的版本号；没有就空串。

    🔴 用**读文件 + 正则**，不 `import`：`buildinfo.py` 不在仓库里（是构建产物），
    从仓库直接跑时它不存在，import 会抛 —— 而且一个能被写进任意内容的 .py
    拿来 import 就是执行它。这里只要那一个字符串。
    占位符没被替换（`@@` 开头）⇒ 当没有，别把占位符发给用户。
    """
    import os
    import re

    try:
        with open(os.path.join(package_dir, "buildinfo.py"), encoding="utf-8") as handle:
            text = handle.read(4096)
    except OSError:
        return ""
    match = re.search(r'^BUILD_ID\s*=\s*"([^"\n]{1,60})"', text, re.M)
    value = match.group(1).strip() if match else ""
    return "" if value.startswith("@@") else value


def version_label(package_dir: str, fingerprint: str = "") -> str:
    """诊断卡第一行显示的版本：有版本号就「版本号 (指纹)」，没有就只给指纹。

    版本号答「哪一版、新还是旧」，指纹答「这一版有没有被改过」—— 远程支持两样都要。
    指纹由调用方算好传进来（它要遍历整个目录，调用方有缓存）。
    """
    bid = build_id(package_dir)
    if bid and fingerprint:
        return f"{bid} ({fingerprint})"
    return bid or fingerprint


def code_fingerprint(package_dir: str) -> str:
    """装在这台机器上的这份代码的指纹（`bridge/**.py` 内容的 sha256 前 8 位）。

    为什么不是版本号：这个产品**没有版本号** —— 安装器把 `bridge/*.py` 的源码直接
    嵌进单文件脚本里写到同事电脑上，没有任何地方记下"这是哪一版"。
    远程支持第一个要问的恰恰是"你装的是哪一版" —— 不然同事报的毛病可能早就修了，
    你会去复查一个已经不存在的 bug。

    指纹按**内容**算，不看时间戳：同一份代码在两台机器上必须得出同一个值
    （mtime 会因为安装时间不同而不同，那样两台一样的机器会显示成两个版本）。
    算不出来就返回空串 ⇒ 那一行不显示，不编一个假值出来。
    """
    import hashlib
    import os

    digest = hashlib.sha256()
    try:
        names = []
        for root, _dirs, files in os.walk(package_dir):
            for name in files:
                if name.endswith(".py"):
                    names.append(os.path.join(root, name))
        if not names:
            return ""
        for path in sorted(names):
            # 相对名也进哈希：少装一个模块必须换出不同的指纹
            # （安装器漏装过模块，那种故障从行为上看像"某个功能没了"）。
            digest.update(os.path.relpath(path, package_dir).encode("utf-8"))
            with open(path, "rb") as handle:
                digest.update(handle.read())
    except OSError:
        return ""
    return digest.hexdigest()[:8]


def classify_log_errors(lines) -> dict:
    """把日志行归成 `ERROR_CLASSES` 里的类，返回 {类名: 条数}。

    输入是日志行，输出**只有枚举出来的类名和整数** —— 原文一个字符都不带出去。
    认不出的行一律忽略（宁可少报一类，也不要为了"兜住未知"而回传原文）。
    """
    counts: dict[str, int] = {}
    for line in lines or ():
        text = str(line)
        for name, needles in ERROR_CLASSES:
            if any(needle in text for needle in needles):
                counts[name] = counts.get(name, 0) + 1
                break
    return counts


def format_error_counts(counts: dict) -> str:
    """{类名: 条数} → `连不上飞书 24 · 长连接断开 3`。没有错就空串（那一行不显示）。"""
    if not counts:
        return ""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(f"{name} {n}" for name, n in ordered[:4])


def build_health_card(data: dict) -> str:
    """诊断卡的 JSON。内容只来自 `health_lines`，调用方塞别的键也进不来。"""
    lines = health_lines(data)
    if not lines:
        # 一个字段都取不到本身就是情报（桥刚起来 / 探测全失败），别发一张空卡。
        lines = ["读不出状态。桥可能刚启动，过一分钟再发一次「诊断」。"]
    elements: list = [{"tag": "markdown", "content": "\n".join(lines)}]
    elements.append(
        {
            "tag": "markdown",
            "content": "这张卡**不含**你的聊天内容、文件路径和密钥，可以直接转给管理员。",
            "text_size": "notation",
        }
    )
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🩺 运行诊断"},
                "template": "grey",
            },
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )


def _fmt_elapsed(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


# 工具名换成人话。用户不关心 Grep/Glob 这些内部名字。
_TOOL_LABEL = {
    "Read": "读文件",
    "Glob": "找文件",
    "Grep": "搜代码",
    "Bash": "跑命令",
    "Edit": "改文件",
    "Write": "写文件",
    "NotebookEdit": "改 notebook",
    "TodoWrite": "列计划",
    "Task": "开子任务",
    "WebFetch": "取网页",
    "WebSearch": "搜网络",
}


def _tool_code(name: str) -> str:
    from .reply import inline_code

    return inline_code(str(name or "")[:60])


class StatusCard:
    """一轮任务期间的状态显示。失败一律静默降级，不影响主流程。"""

    def __init__(self, replier, chat_id: str, escalate_sec: int = 0) -> None:
        self._replier = replier
        self._chat_id = chat_id
        self._message_id: str | None = None
        self._last_text = ""
        self._broken = False  # 发/改失败过就不再试，免得每次刷都打一发失败请求
        # 超过这么久就在卡上说一句「比平时久，可以先走开」。0 = 不说。
        # 🔴 2026-11-06：配置项 `progress_escalate_sec`（默认 420）原来**读进来就没人用** ——
        # 状态卡跑 40 秒和跑 40 分钟长得一模一样，主人在手机上不知道该等还是该走（竞品对标 E Top5）。
        self._escalate_sec = max(int(escalate_sec or 0), 0)

    async def update(self, elapsed: int, tool_calls: int, last_tool: str) -> None:
        if self._broken:
            return
        text = self._render(elapsed, tool_calls, last_tool)
        if text == self._last_text:
            return  # 没变化就不发，省掉无意义的请求和闪动
        self._last_text = text
        try:
            if self._message_id is None:
                self._message_id = self._replier.send_status_card(self._chat_id, text)
                if not self._message_id:
                    self._broken = True
            else:
                if not self._replier.patch_status_card(self._message_id, text):
                    self._broken = True
        except Exception:
            # 状态卡是锦上添花，坏了不该让这一轮任务失败
            logger.debug("状态卡更新失败", exc_info=True)
            self._broken = True

    def _render(self, elapsed: int, tool_calls: int, last_tool: str) -> str:
        parts = [f"⏳ 还在跑 · {_fmt_elapsed(elapsed)}"]
        if tool_calls:
            # 认不出的工具名（`mcp__x__y` 这种）用行内代码：裸写的话 `__` 会被当成粗体吃掉
            label = _TOOL_LABEL.get(last_tool) or _tool_code(last_tool)
            if label:
                parts.append(f"正在{label}")
            parts.append(f"第 {tool_calls} 步")
        # 停止按钮在卡片底下（status.build_status_card 加的），这里不再叫人打命令
        text = " · ".join(parts)
        if self._escalate_sec and elapsed >= self._escalate_sec:
            # 手机场景：通勤 / 开会时掏出来看一眼。要告诉他的是「你不用盯着」—— 跑完会自己回来。
            text += "\n这次比平时久。可以先去忙，跑完会在这里回你；不想等了就点停下。"
        return text

    async def finish(
        self, elapsed: int = 0, tool_calls: int = 0, outcome: str = "done"
    ) -> None:
        """这一轮结束：把状态卡收尾，别让它永远停在「还在跑」。

        刻意不删卡片 —— 删了用户就看不到这一轮花了多久。
        收尾文案带上耗时和步数：用户问过一个长任务之后，回来第一眼想知道的
        就是"它干了多久、干了多少"。只说「跑完了」等于把这个信息扔掉。
        """
        if self._broken or self._message_id is None:
            return
        # 🔴 按真实结果写。原来一律「✅ 跑完了」—— 用户点了停下、超时、
        # 或在等他确认时，手机上置顶那张卡也显示成功（2026-11-04 review）。
        parts = [_OUTCOME_HEAD.get(outcome, _OUTCOME_HEAD["done"])]
        if elapsed:
            parts.append(_fmt_elapsed(elapsed))
        if tool_calls:
            parts.append(f"{tool_calls} 步")
        try:
            # 跑完了 ⇒ 不再放「停下」（停一个已结束的任务只会得到「没有在跑的任务」）
            try:
                self._replier.patch_status_card(self._message_id, " · ".join(parts), with_stop=False)
            except TypeError:
                # 旧接口（没有 with_stop）：照旧收尾，只是少了「去掉停止按钮」这一步。
                # 不能让收尾整个失败 —— 那样卡片会永远停在「还在跑」。
                self._replier.patch_status_card(self._message_id, " · ".join(parts))
        except Exception:
            logger.debug("状态卡收尾失败", exc_info=True)


_OUTCOME_HEAD = {
    "done": "✅ 跑完了",
    "stopped": "⏹ 已停下",
    "failed": "❌ 没跑完",
    "waiting": "⏸ 等你确认",
}


def build_status_card(text: str, with_stop: bool = False) -> str:
    """状态卡的 JSON。

    `with_stop=True`（还在跑时）在底下放一个「⏹ 停下」按钮 —— 原来是一行
    「想停就发 /stop」，用户要求全按钮化（2026-11-05）。跑完收尾那次不放：
    停一个已经结束的任务只会得到「现在没有在跑的任务」。

    🔴 schema 2.0 的坑（2026-11-03 实打飞书验过）：**不能**用
    `{"tag":"action","actions":[...]}` 容器，会得到
    `code=230099 / ErrCode: 200861 / cards of schema V2 no longer support this
    capability`。按钮直接进 elements（走 fleet.actions 的现成写法）。
    """
    elements: list = [{"tag": "markdown", "content": text}]
    if with_stop:
        from .fleet.actions import button_rows, do_button

        elements.extend(button_rows([do_button("stop")]))
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )
