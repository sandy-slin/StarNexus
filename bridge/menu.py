"""按钮菜单：手机上不用打字。

用户原话：「可以上来就给我按钮菜单吗？打字太不方便了。」

## 设计

一张卡片，几排按钮。点一下 = 发了一句话。两类按钮：

1. **常用问题**（点了就当成用户问了这句）：
   「有哪些项目」「哪里有改动」「最近改了什么」「找 TODO」
2. **控制**（点了就执行命令）：
   「换项目」「停下」「新话题」「现在状态」

刻意**不**做的：
- 不放危险操作的按钮（提交、推送、删除）。按钮太容易误点，
  而这些操作要么不可逆、要么该让用户自己说清楚要干什么。
- 不做多级菜单。手机上一层就够，多了找不到。

## 什么时候弹

- 绑定成功后（第一印象）
- 用户发 `/menu` 或 `菜单`
- 每轮回复后**不**自动带 —— 那会刷屏。用户要的时候叫一声就行。

## 点了之后

按钮的 value 里带 `{"act": "menu", "say": "<那句话>"}` 或 `{"act": "menu", "cmd": "/stop"}`。
回调里把它当成用户发了那条消息走正常流程 —— 这样鉴权、审查、审批全都照常生效，
按钮不会变成绕过闸门的后门。

## 飞书卡片 schema 2.0 的坑（踩过）

- **不能**用 `{"tag":"action","actions":[...]}` 容器 ⇒ `code=230099 / 200861`
- button 直接进 elements 或 column_set
- 回调走 `behaviors: [{"type":"callback","value":{...}}]`，不是顶层 `value`
"""

from __future__ import annotations

import difflib
import json
import re

# 常用问题。措辞要和用户自己会说的一样 —— 点了之后 agent 收到的就是这句。
# 第二个字段是按钮上显示的短标签（手机上一行放不下长字）。
#
# 🔴 措辞**只有一份**，在 botmenu.QUESTION_TEXT。常驻菜单和卡片菜单原来各存一份，
# 改了一边另一边不变，两个入口问出不一样的话而测试全绿（2026-11-04 review）。
from .botmenu import QUESTION_TEXT as _QUESTION_TEXT

# 🔴 第 28 轮：**顺序 = 卡上的顺序 = 用得多的在前**。数据来自本机 agent.log（11-04 ~ 11-08）
# 悬浮栏「收到菜单点击 key=…」：哪里有改动 5、有哪些项目 2、找 TODO 2；
# 后三条只在这张卡上有，日志不记卡上点的是哪个按钮（只记 act=menu）⇒ 没有数据，放后面。
_QUICK_LABELS = (
    ("q:changes", "✏️ 哪里有改动"),
    ("q:projects", "📂 有哪些项目"),
    ("q:todo", "📌 找 TODO"),
    ("q:recent", "🕐 最近改了啥"),
    ("q:tests", "🧪 测试怎么跑"),
    ("q:ports", "⚙️ 进程端口"),
)
QUICK_QUESTIONS = tuple((_QUESTION_TEXT[key], label) for key, label in _QUICK_LABELS)

# 控制按钮。cmd 必须是已有的斜杠命令（`known_commands()` 里有的），不新增入口。
#
# 为什么把「权限」「模型」放进来：对照豆包工作输入框下那一排
# （本地电脑 / 项目 / **按需确认** / 技能 / 插件 / **自动 高**）——
# 他们把"要不要每步问我"和"用多强的模型"当成一等公民摆在最外层。
# 我们原来这两项只能改 config.toml 再重启，手机上根本做不到。
#
# 🔴 第 28 轮：顺序按悬浮栏实际点击数排（agent.log 11-04 ~ 11-08，147 次）：
# 会话 60、权限 30、模型 18、最近问过 11、停下 5、换项目 5（换项目 11-04 之后挪出了悬浮栏）。
# 「会话」「最近问过」「运行诊断」原来**不在这张卡上** —— 叫「全部按钮」却缺了点得最多的那个。
# 标签和 `fleet.actions.DEFAULT_LABELS` 一字不差（帮助、别的卡叫他点的就是这几个字）。
#
# ⚠️ `resolve_click` 的 cmd 白名单就是这张表 ⇒ 加进来的三条同时进了白名单。
# 三条都只是「看」（发一张卡），不改任何设置、不动电脑上的东西；
# 而且早就在 `fleet.actions.DO_COMMANDS` 的按钮白名单里（同一套判据）。
CONTROLS = (
    ("/sessions", "🖥 看看会话"),
    ("/newsession", "➕ 新建会话"),
    ("/history", "↑ 最近问过"),
    ("/mode", "🔑 权限"),
    ("/model", "🧠 模型"),
    # 图标和 actions.DEFAULT_LABELS["dirs"]、帮助、回话里叫他点的那个统一成 📂（原来这里是 🔀，
    # 而帮助和 runner 教模型说的都是「📂 换项目」⇒ 用户照着找找不到一模一样的那个）。
    ("/dirs", "📂 换项目"),
    ("/stop", "⏹ 停下"),
    ("/new", "🆕 新话题"),
    ("/status", "ℹ️ 现在状态"),
    ("/诊断", "🩺 运行诊断"),
)

# 卡上**直接露出来**的那几个；其余收进底下的折叠面板（点开才看见，不走回调）。
# 判据：悬浮栏上真有人点过的才露出来（数据见上面两段注释）。
# 「换项目」点得不多也露着：别的卡、帮助都叫他「点📂 换项目」，而它不在悬浮栏里 ——
# 再收进面板就从 3 下变 4 下（tests/test_r17_taps.py 的那套算法）。
_PINNED_CONTROLS = ("/sessions", "/newsession", "/history", "/mode", "/model", "/dirs", "/stop")
_PINNED_QUESTIONS = ("q:changes", "q:projects", "q:todo")

# 折叠面板标题右边的小箭头。和 `fleet.cards.PANEL_ICON` 同一个（主会话 2026-11-08 真发过、
# 飞书接受的写法）；不 import 是因为那个文件同一轮有别人在改，菜单卡不能跟着它一起坏。
# tests/test_r28_menu_card.py 盯着两份一致。
_PANEL_ICON = {"tag": "standard_icon", "token": "down-small-ccm_outlined", "size": "16px 16px"}

# 触发菜单的说法。除了 /menu，用户在手机上会直接打这几个字。
TRIGGERS = frozenset({"/menu", "/m", "菜单", "menu", "帮助菜单", "?", "？"})


def is_menu_request(text: str) -> bool:
    return (text or "").strip().lower() in TRIGGERS


# ---------- 帮助全文 ----------
#
# 🔴 2026-11-06 第 11 轮：用户原话「不要让用户输入 "/cancel" 等类似的 尽量用按钮的形式」。
# 原来 `app.HELP` 整段是命令手册（`/cd 项目名`、`/dirs`、`/mode`、`/stop`、`/new`、`/resume`），
# 每一行都在教他打斜杠。现在每件事都说**点哪个按钮**，按钮的字和 `CONTROLS` /
# `actions.DEFAULT_LABELS` / 常驻菜单（botmenu.MENU_TREE）上的一模一样 ——
# `tests/test_r11_copy.py` 逐个核对，改了按钮名这里会红。
#
# 斜杠命令**只在最后一行出现一次**：按钮卡发不出去（网络抖、飞书限流）时那是唯一的路，
# 不能删成死路；但它是兜底，不是教程。这一行在文案闸门 `ALLOWED` 里登记了理由。
#
# 例句里**不许写死任何仓库名或文件名**（同 `pairing.BOUND_MESSAGE` 的红字）。
# 这段帮助会被 `/help`、「❓ 帮助」按钮、菜单卡发不出去时三个地方发出去（都在 app.py）。
HELP = """**我在你的电脑上干活，结果发回这里。**

直接说要干什么，不用记命令：
· 这个文件夹里都有什么？
· 随便挑一个文档，用三句话告诉我它在讲什么
· 这里面有没有表格？里面大概是什么数据

**不想打字就点按钮**
输入框上面那一排常驻菜单随时能点；想要全部按钮，发「菜单」两个字。
· 「📂 换项目」— 换到别的项目，列出来点一下就行
· 「🔑 权限」（在「⚡ 常用」里）— 现在是「全授权」，项目里的事不问就做；想让它每步先问你，切到「只读」
· 「🧠 模型」— 用哪个模型（均衡 / 最聪明 / 最快）
· 「ℹ️ 现在状态」— 看现在在哪、什么权限
· 「⏹ 停下」— 停下正在跑的那一轮
· 「🆕 新话题」— 开个新话题（忘掉前面聊的）
· 被暂停时，那句话下面会带「▶️ 继续」

**电脑上的其他会话**
点「🖥 会话」（或发「会话」两个字）— 看电脑上开着的 Claude / Codex 会话，点一个看最近几条
点「➕ 新建会话」（或发「新建会话」）— 选 Claude / Codex 和项目，在电脑上新开一个会话，之后直接在这里接着聊
点 [接着聊] 会给这个会话建一个群，点「进群说话」进去直接打字就行：每一句都进电脑上那个会话，它的回复发回群里
· 群里第一句会说清它用哪套权限（手机和终端共用当前权限档位，默认全授权；需要收紧时切到只读）
· 会话在电脑上改了名、换了项目或关了终端，会再问你一次
· 不想聊了点「🚪 退出对话」，或说「取消」「退出」「算了」
· 它手里那一轮**停不了** —— 只能到电脑上按 Esc（点「⏹ 停下」也会照实告诉你）

**它做不到的**
· 只在你指定的项目里动手；要动项目以外的东西会先问你（想换项目点「📂 换项目」）
· 不会删掉你的整个家目录，点同意也不行
· 读私钥/密码这类凭据、推代码、提权（sudo）会先弹卡问你，点同意才做；凭据内容回到飞书前会打码

电脑睡着时收不到消息，醒了也不会补发 —— 发了没回先看看电脑是不是睡了。
电脑不在身边时，点输入框上面「⚡ 常用」里的「🌐 网页版」，在网页里问（不经过电脑，查不了电脑上的文件）。

按钮出不来的时候，也可以直接发文字：/stop 停下、/sessions 看会话、/cd 加项目名 换项目。
"""


# ---------- 打错的斜杠命令 ----------
#
# 🔴 2026-11-05：`/sessoins` 这种打错一个字母的命令，`_handle_command` 全部分支都
# 不认 ⇒ 返回 False ⇒ 被当成**普通问题发给 claude 跑一整轮**。代价是真金白银：
# 一轮 agent 的钱、几十秒的等待，最后得到一句答非所问（它会去猜 "sessoins" 是什么）。
# 所以这里加兜底：看起来是命令、但不在已知表里 ⇒ 当场回一句 + 给按钮，不开跑。
#
# 已知命令表**必须和 `app._handle_command` 的分支保持一致**，而它们天然会漂。
# 所以：能 import 的就 import（下面三处），只有 app.py 自己那几个字面量在
# `_APP_LITERALS` 里重抄一遍，并由 `tests/test_menu.py::test_known_commands_cover_app_branches`
# 扫 app.py 源码盯着 —— 那条测试红了就是有人加了命令没登记，而不是等用户踩到。
_APP_LITERALS = frozenset({
    "/help", "/h",
    "/mode", "/权限", "/mode!",
    "/model", "/模型",
    "/status",
    "/new",
    "/stop", "/s",
    "/resume",
    "/talk!",
    "/session", "/sessions", "/会话", "/总控",
    "/cancel",
    "/诊断", "/diag", "/diagnose",
    "/update", "/upgrade",   # 第 14 轮：自动更新（「更新」两个字也认，见 app.UPDATE_WORDS）
    "/history", "/历史", "/最近",   # 「↑ 最近问过」（bridge/history.py）
})

# 一个 token 像不像"命令"：`/` 后面跟字母或汉字，中间不能再有 `/`。
#
# 中间那条排除掉绝对路径（`/Users/…/x` 里有第二个斜杠）—— 用户发
# 「/Users/me/codes 里有什么」是个正常问题，不该被回「没有这个命令」。
# 单段的路径（`/tmp`）仍会被当成打错的命令，代价是一条多余的回复 +
# 一排按钮；反过来放过去才是白跑一整轮 agent，那个贵得多。
_COMMAND_SHAPE = re.compile(r"^/[A-Za-z一-鿿][A-Za-z0-9一-鿿_!·-]{0,19}$")


def known_commands() -> frozenset[str]:
    """所有认得的斜杠命令。给兜底判断用，也是那条防漂测试的判据。"""
    from .fleet.actions import DO_COMMANDS
    from .workspace import COMMANDS as WORKSPACE_COMMANDS

    known = set(_APP_LITERALS)
    known |= {c for c in WORKSPACE_COMMANDS}
    known |= {c for c in DO_COMMANDS.values() if c}
    known |= {t for t in TRIGGERS if t.startswith("/")}
    return frozenset(known)


def unknown_command(text: str) -> str:
    """这条消息是"打错的命令"吗。是 ⇒ 返回那个 token；不是 ⇒ 空串。

    判据刻意只看**第一个 token**：`/cd 项目名` 带参数，而参数里有什么都不影响
    "这个命令认不认识"。
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return ""
    token = stripped.split()[0]
    if not _COMMAND_SHAPE.match(token):
        return ""
    if token.lower() in {c.lower() for c in known_commands()}:
        return ""
    return token


def _closest_command(token: str) -> str:
    """最像的那个已知命令。没有足够像的返回空串。

    cutoff 给到 0.7：低了会把 `/x` 之类随手打的东西硬配到某个命令上，
    然后我们言之凿凿地问"你是想发 /status 吗"，比不猜更糟。
    只在**英文命令**里找 —— 汉字命令（`/权限`）打错的形状是输入法选错字，
    和字母距离没有关系，猜出来的建议一定是错的。
    """
    pool = [c for c in known_commands() if c.isascii()]
    matches = difflib.get_close_matches(token.lower(), pool, n=1, cutoff=0.7)
    return matches[0] if matches else ""


def unknown_command_reply(text: str):
    """打错的命令 → （一句话, 一排按钮）。不是打错的命令返回 None。

    给用户看的那句话里**不解释"命令表"这种内部概念**：他要的是
    "我刚打的那个没用，接下来点哪儿"。所以一句话 + 按钮，按钮占主位。
    """
    token = unknown_command(text)
    if not token:
        return None
    from .fleet.actions import DO_COMMANDS, do_button

    guess = _closest_command(token)
    # 猜中的那条命令有对应按钮就放第一个（点一下就等于他本来想打的那句）。
    # `DO_COMMANDS` 是 op → 命令，这里要反过来查；有两个 op 指向同一命令时取先出现的。
    op_of_command = {}
    for op, cmd in DO_COMMANDS.items():
        op_of_command.setdefault(cmd, op)
    ops = []
    guessed_op = op_of_command.get(guess, "")
    if guessed_op:
        ops.append(guessed_op)
    # 菜单和帮助是**认不出时唯一有用的两个出口**，始终给。
    # 猜中的就是它们之一时不重复放（去重按 op，不按按钮长相）。
    for op in ("menu", "help"):
        if op not in ops:
            ops.append(op)
    buttons = [do_button(op, primary=(i == 0)) for i, op in enumerate(ops)]

    line = f"没有「{token}」这个用法。"
    if guess:
        line += f"你是想发 `{guess}` 吗？下面点一下就行。"
    else:
        line += "点下面的按钮，或者直接把要做的事说给我。"
    return line, buttons


def _button(label: str, value: dict, primary: bool = False) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if primary else "default",
        "width": "fill",
        "size": "medium",
        "behaviors": [{"type": "callback", "value": value}],
    }


def _row(buttons: list[dict]) -> dict:
    """两个按钮一排 —— 手机竖屏上三个就挤了。"""
    return {
        "tag": "column_set",
        "horizontal_spacing": "8px",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [b]}
            for b in buttons
        ],
    }


def build_menu_card(workspace_label: str = "", policy=None) -> str:
    """菜单卡片 JSON（「全部按钮」）。

    从上到下：当前状态 → 常用控制（点得多的在前）→ 常用问题 → 折叠面板「更多」。
    折叠面板是客户端本地展开的，不走回调；面板里的按钮和外面的一样走 `resolve_click`。

    顶上一行是**当前状态**：在哪个目录、哪一档权限、哪个模型。
    这是照着豆包工作那排抄的 —— 他们把这三样直接写在输入框下面，
    用户不用点进去就知道"我现在是什么配置"。
    不然他点了「哪里有改动」会不知道查的是哪个目录，
    点了「改一下」才发现是只读。
    """
    elements: list[dict] = []
    status_bits = []
    if workspace_label:
        # 路径用行内代码：`~/codes/my_app` 裸写的话 `_app` 会被当成斜体
        from .reply import inline_code

        status_bits.append(f"📁 {inline_code(workspace_label)}")
    if policy is not None:
        from .policy import MODE_LABELS, MODEL_LABELS

        status_bits.append(f"🔑 {MODE_LABELS.get(policy.mode, policy.mode)}")
        status_bits.append(f"🧠 {MODEL_LABELS.get(policy.model, policy.model)}")
    if status_bits:
        elements.append({"tag": "markdown", "content": "　".join(status_bits)})

    # 🔴 第 28 轮：常用的放最上面（数据见 CONTROLS 的注释），没数据的收进折叠面板。
    # 点得最多的「看看会话」（悬浮栏 60 / 147）用主色，一眼找到。
    def rows(buttons: list[dict]) -> list[dict]:
        return [_row(buttons[i : i + 2]) for i in range(0, len(buttons), 2)]

    pinned_says = {_QUESTION_TEXT[key] for key in _PINNED_QUESTIONS}
    controls = [
        _button(label, {"act": "menu", "cmd": cmd}, primary=(cmd == _PINNED_CONTROLS[0]))
        for cmd, label in CONTROLS
        if cmd in _PINNED_CONTROLS
    ]
    elements.extend(rows(controls))

    elements.append({"tag": "markdown", "content": "**点一下就问：**"})
    questions = [
        _button(label, {"act": "menu", "say": say})
        for say, label in QUICK_QUESTIONS
        if say in pinned_says
    ]
    elements.extend(rows(questions))

    more_questions = [
        _button(label, {"act": "menu", "say": say})
        for say, label in QUICK_QUESTIONS
        if say not in pinned_says
    ]
    more_controls = [
        _button(label, {"act": "menu", "cmd": cmd})
        for cmd, label in CONTROLS
        if cmd not in _PINNED_CONTROLS
    ]
    if more_questions or more_controls:
        # 标题里把面板里有什么写出来 —— 收起来的东西不说名字，就等于没有。
        names = [label.split(" ", 1)[-1] for cmd, label in CONTROLS if cmd not in _PINNED_CONTROLS]
        if more_questions:
            names.append("更多问题")
        elements.append(
            {
                "tag": "collapsible_panel",
                "expanded": False,
                "header": {
                    "title": {"tag": "markdown", "content": "**更多**：" + " · ".join(names)},
                    "width": "fill",
                    "icon": _PANEL_ICON,
                    "icon_position": "right",
                    "icon_expanded_angle": -180,
                },
                "elements": rows(more_controls) + rows(more_questions),  # 和标题的先后一致
            }
        )

    elements.append(
        {
            "tag": "markdown",
            "content": "也可以直接打字问别的。再叫出这个菜单发「菜单」。",
            "text_size": "notation",
        }
    )

    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "想让我干点啥？"},
                "template": "blue",
            },
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )


def build_confirm_loosen_card(target: str, policy) -> str:
    """放开权限的**第二次**确认。

    🔴 收紧一步到位，放开要点两次。来源是竞品一致的做法（docs/composer-menu-research.md
    第八节）：Codex 桌面对 Full access 有专门的 `composer.fullAccessWarning`，
    Duet 起会话默认 manual 并明说 "It starts in manual mode"。
    理由很实际：误点一下就从"每步问我"变成"不问就改"，而**用户不会收到任何提示**，
    之后几十个写操作静默发生，他事后很难知道是哪一刻失守的。
    收紧方向不需要确认 —— 误点的后果只是多问几次，而且立刻看得见。
    """
    from .policy import MODE_LABELS

    label = MODE_LABELS.get(target, target)
    if target == "trusted":
        # 🔴 第 17 轮补上「项目外还会问你」—— 那是放开之后**唯一还会弹卡**的情况
        # （实测：`echo x > ~/Desktop/p.txt` 在全授权下仍然弹卡）。
        # 原来这段只写红线，用户读完以为再也不弹卡，真弹了一张时不知道为什么。
        what = (
            "项目里的事**不再问你**：改文件、跑命令、装依赖都直接做。\n"
            "要动项目以外的东西还会先问你一下；"
            "删家目录永远不做；推代码、读凭据、提权会先弹卡问你。"
        )
    else:
        # 🔴 写清"仍要逐次确认"。原来写"能直接编辑文件"，而钩子只在全授权下跳过确认 ——
        # 用户点了两下放开，结果每次改文件还是弹卡片（2026-11-04 review）。
        # 🔴 第 17 轮：末句原来是「仍不能提交、不能删、不能跑任意命令」—— 和只读档
        # 那句「不能改文件」同一个毛病（点同意它就做了）。只留**点同意也不许**的那几样。
        what = (
            "能改项目里的文件，**每次动手之前先问你一下**（点同意才做）。\n"
            "删家目录永远不做；推代码、读凭据、提权会先弹卡问你。"
        )
    elements = [
        {
            "tag": "markdown",
            "content": f"要从「{MODE_LABELS.get(policy.mode, policy.mode)}」"
            f"放开到「**{label}**」。\n\n{what}",
        },
        _row(
            [
                _button("确认放开", {"act": "menu", "mode2": target}, primary=True),
                _button("算了", {"act": "menu", "cmd": "/mode"}),
            ]
        ),
        {
            "tag": "markdown",
            "content": "想收回，再点一次「🔑 权限」选「只读」。",
            "text_size": "notation",
        },
    ]
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "⚠️ 确认放开权限"},
                "template": "orange",
            },
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )


def build_mode_card(policy) -> str:
    """权限档位选择卡。只列这台机器允许的档位。

    🔴 列表来自 `modes_up_to(policy.ceiling_mode)`，不是全部三档。
    把点不动的按钮画出来是最差的交互 —— 用户点了、没反应、以为坏了。
    IT 锁成 readonly 时这张卡就只有一个选项，并明说为什么。
    """
    from .policy import MODE_LABELS, modes_up_to

    available = modes_up_to(policy.ceiling_mode)
    elements: list[dict] = [
        {
            "tag": "markdown",
            "content": f"现在是 **{MODE_LABELS[policy.mode]}**\n{policy.describe()}",
        },
        {"tag": "hr"},
    ]
    buttons = []
    for mode in available:
        label = MODE_LABELS[mode]
        if mode == policy.mode:
            label = f"✅ {label}"
        buttons.append(
            _button(label, {"act": "menu", "mode": mode}, primary=(mode == policy.mode))
        )
    for i in range(0, len(buttons), 2):
        elements.append(_row(buttons[i : i + 2]))

    if len(available) == 1:
        elements.append(
            {
                "tag": "markdown",
                # 🔴 手机用户改不了 config.toml —— 告诉他找谁，而不是让他去改文件
                # （2026-11-04 review：默认安装都是只读，这一格对新用户是死路）。
                #
                # 🔴 2026-11-05 复查：上一版写的「在电脑上重跑一次安装选『可改文件』」
                # 同样是死路 —— **安装器根本没有这个选项**，`mode = "readonly"` 是写死的
                # （`feishu-agent-oneclick/src/install-mac.sh:694`，Windows 那份同样）。
                # 用户照着重跑一遍安装，从头到尾看不到那个选项，最后还是来问 IT，
                # 而中间白白等了一次安装。所以这里只留他**真能执行**的那一条：找人。
                # 🔴 2026-11-05 用户拍板：不说「不会改你的文件」—— 实测只读档下坚持一句
                # 会弹审批卡，点同意文件真的被改了。按真实行为说（同 policy.describe）。
                #
                # 🔴 第 17 轮：原来结尾写「这一档是装的时候定下的」。第 16 轮把安装默认
                # 翻成了全控制之后那句话是假的 —— 装出来的就是全控制，走到这一格只剩两种：
                # 管理员放了策略文件，或者有人主动把这台机器锁死在只读。
                # 所以改成「被锁在只读」，并且照旧只给他**真能执行**的那条下一步：找人。
                "content": "这台电脑被锁在「只读」：默认只看、只查，要改东西会先弹卡片问你。"
                "想让它不用问就能改代码，找管理员或给你装的那位同事放开一下 —— "
                "手机上点不开。",
                "text_size": "notation",
            }
        )
    else:
        elements.append(
            {
                "tag": "markdown",
                # 🔴 第 11 轮：原来写「再往上只能改本机 config.toml」—— 同上面只读那一格的红字，
                # 手机用户碰不到那个文件。说他真能做的：找给他装的人。
                "content": f"这台电脑最多能放到「{MODE_LABELS[policy.ceiling_mode]}」。"
                "还想再往上，找给你装的那位同事放开 —— 手机上点不开。",
                "text_size": "notation",
            }
        )
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🔑 要不要每步问你"},
                "template": "indigo",
            },
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )


def build_model_card(policy) -> str:
    """模型选择卡。对照豆包那个「自动 高」的位置。"""
    from .policy import MODEL_CHOICES, MODEL_LABELS

    elements: list[dict] = [
        {"tag": "markdown", "content": f"现在用 **{MODEL_LABELS.get(policy.model, policy.model)}**"},
        {"tag": "hr"},
    ]
    buttons = []
    for name, label, why in MODEL_CHOICES:
        shown = f"✅ {label}" if name == policy.model else label
        buttons.append(
            _button(shown, {"act": "menu", "model": name}, primary=(name == policy.model))
        )
    for i in range(0, len(buttons), 2):
        elements.append(_row(buttons[i : i + 2]))
    elements.append(
        {
            "tag": "markdown",
            "content": "\n".join(
                f"**{label}** — {why}" for _, label, why in MODEL_CHOICES
            ),
            "text_size": "notation",
        }
    )
    return json.dumps(
        {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "🧠 用哪个模型"},
                "template": "indigo",
            },
            "body": {"elements": elements},
        },
        ensure_ascii=False,
    )


def resolve_click(value: dict) -> str:
    """按钮点击 → 等价的用户消息。认不出返回空串。

    🔴 只认菜单里**预设过的**值。回调里的 value 来自飞书，理论上能被伪造 ——
    如果直接把 `value["say"]` 当消息执行，伪造的回调就能让 agent 跑任意指令，
    等于绕过了"只认主人"的鉴权之外的所有判断。所以必须在白名单里。

    ⚠️ 切档/换模型也走这里、也变成一条**命令文本**（`/mode readonly`），
    不走捷径直接去改 prefs。好处是一条路径：鉴权、落盘、天花板收敛、
    回话措辞全都只有一份实现，不会"按钮那条路忘了判天花板"。
    """
    from .policy import MODE_RANK, known_model

    if not isinstance(value, dict) or value.get("act") != "menu":
        return ""
    say = value.get("say")
    if isinstance(say, str):
        allowed = {q for q, _ in QUICK_QUESTIONS}
        return say if say in allowed else ""
    cmd = value.get("cmd")
    if isinstance(cmd, str):
        allowed_cmds = {c for c, _ in CONTROLS}
        return cmd if cmd in allowed_cmds else ""
    mode = value.get("mode")
    if isinstance(mode, str):
        # 只认已知档名。真正的天花板收敛在 prefs.set_mode 里 ——
        # 这里放过去不等于放行，命令那条路会再压一次。
        return f"/mode {mode}" if mode in MODE_RANK else ""
    # `mode2` = 已经过了放开确认那一张卡。走 `/mode! x`，它跳过二次确认。
    # 主人自己打 `/mode! x` 也会生效 —— 这是**有意的**文字确认通道（确认卡发不出去时
    # 回话里就教他这么打，见 app._handle_mode）。打出带 `!` 的命令本身就是明确意图；
    # 非主人发的消息在鉴权那层就被丢了。超过本机上限照样压回去。
    confirmed = value.get("mode2")
    if isinstance(confirmed, str):
        return f"/mode! {confirmed}" if confirmed in MODE_RANK else ""
    model = value.get("model")
    if isinstance(model, str):
        return f"/model {model}" if known_model(model) else ""
    return ""
