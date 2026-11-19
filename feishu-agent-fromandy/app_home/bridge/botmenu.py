"""飞书机器人自定义菜单：常驻在私聊输入框上方的那排按钮。

用户给的参照是豆包工作输入框下那一排（本地电脑 / 项目 / 按需确认 / 技能 / 插件 / 自动 高）。
第三方机器人能拿到的最接近的东西就是这个「机器人自定义菜单」（悬浮样式）：

- 常驻在私聊输入框上，不随消息滚走（菜单卡片做不到这点 —— 它是消息流里的一条）
- 点击推送 `application.bot.menu_v6` 事件，带 `event_key` 和点击人的 open_id，**能走长连接**
- **仅支持单聊**，群里不显示；飞书客户端要 7.22+

出处：https://open.feishu.cn/document/client-docs/bot-v3/bot-customized-menu
事件：https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/application-v6/bot/events/menu

## 🔴 真实限制：**一共 10 个节点**（文档没写）

2026-11-04 实打 `PATCH /application/v7/applications/:app_id/ability` 逐个试出来的。
文档说"5 个主菜单 × 每个 10 个子菜单"，照着写会被拒：

    code 99992402  field: bot.bot_menus  "the max len is 10"

`bot_menus` 是一个**拍平的数组**，父子都算一条，上限 10 条 —— 不是 5×10=50。
实测边界：
- 可切换样式（strategy 1/2）：平级 3 条过、4 条起报 `210011`
- 悬浮样式（strategy 3，我们用的）：5 条过、6 条拒
- 1 父 + 5 子过、6 子报 `210011`
- 3 父 × 3 子 = 12 节点 ⇒ `99992402 the max len is 10`

所以我们的形状是 **5 个主菜单 + 5 个子菜单 = 10**（见 `MENU_TREE`），一个不多。
加菜单项前先跑 `validate_tree()`，它按这个真实上限算。

## 另外三个实测出来的必填/类型坑

1. **`bot.enable` 必填。** 只给 `bot_menu_enable` 会被拒：
   `field: bot.enable  "bot.enable is required"`。
2. **`menu_content_type` 是数字 `[0,1,2,3,4]`，不是字符串。**
   传 `"event"` 得到 `menu_content_type is optional, options: [0,1,2,3,4]`。
   实测 `2` = 推事件（带 `event_key`），父节点用 `0`（只展开、不推事件）。
   `1` = 跳转链接（2026-11-10 读官方文档 + SDK 1.7.5 `BotMenuNode`，**没实打过**）：
   官方原文「菜单响应动作类型.0:未知，1：跳转链接，2：事件通知，3展开子菜单，4：发消息」；
   链接放在 `redirect_link: {"pc_url", "mobile_url"}`，文档写「在客户端内打开链接。当
   menu_content_type = 1 时必填」。没有 ios_url / android_url 之分，SDK 的
   `BotMenuRedirectLink` 也只有这两个字段。跳转由飞书客户端完成、**不推事件** ⇒ 电脑关着也能点。
   出处：open.feishu.cn 文档 `/uAjLw4CM/ukTMukTMukTM/application-v7/application-v7/application-ability/patch`
3. **`i18n_name` 必填。** 只给 `default_name` 会被 `210011` 拒掉，
   而这个错**不说是哪个字段**（error 里只有 log_id），很难查。
   只给 `zh_cn` 就能过，但两个都给更稳。

## 两个实测/读码确认过的坑

1. **事件里没有 chat_id，只有 open_id。** 回消息时 `receive_id_type` 必须用 `open_id`，
   按 chat_id 发会得到 `230001 invalid receive_id`（cc-connect PR #1031 踩过同一个坑；
   我们自己的日志里也出现过同一个错误码，是另一条路径）。
2. **菜单事件没有消息气泡**，不能 reply 某条消息，只能新发一条。

## 菜单本身能不能用 API 配

能。`PATCH /open-apis/application/v7/applications/:app_id/ability` 的 `bot` 字段有
`bot_menu_enable` / `bot_menus` / `bot_menu_display_strategy`（SDK 1.7.5 的
`AppAbilityBot` 模型里有这三个字段）。2026-11-04 实测：调这个接口返回
`99991672`，缺 `application:application:patch` 权限 —— 也就是说**开了这个权限就能
脚本一键配好**，不用每个人去后台手点。见本文件末尾的 `setup()`（安装器调它）
和 `scripts/apply_bot_menu.py`（运维入口）。

## 菜单分组（对照豆包截图，只有 10 个格子）

悬浮样式下主菜单能直接推事件，所以照豆包那排做成**一排平铺的 chip**：

| 豆包 | 我们 | 点了 |
|---|---|---|
| 项目 › | 🖥 会话 | 电脑上开着的 Claude / Codex / Cursor 会话（原「📁 项目」，换项目在全部按钮里） |
| （没有）| ↑ 最近 | 最近问过的几句 |
| （没有）| 🧠 模型 | 换模型 |
| 按需确认 › / 自动 高 › / 更多技能 › | ⚡ 常用 ›（5 子） | 权限、🌐 网页版（跳转链接，不经过电脑）、两句高频问题、全部按钮 |
| （没有）| ⏹ 停下 | 停掉正在跑的那一轮 |

刻意不做：云电脑（只有一台机器）、插件/企业知识（技能就是本机 skills）。
豆包那排是飞书一方产品（左侧导航独立模块）自己的输入框，chip 在框**里面**；
机器人只能放在框**上方**（docs/bot-menu-styles-research.md）。
"""

from __future__ import annotations

# event_key 上限 30 字符。值直接用"等价的用户输入"，这样分派走现成的命令/消息流程，
# 不新开一条处理路径 —— 新路径就是新的绕过闸门的机会。
# 🔴 一共只能有 10 个节点（父 + 子拍平后算）。改之前先读文件头那段实测。
#    现在是 5 主 + 5 子 = 10，满了。`validate_tree()` 按实测上限算。
# 悬浮样式下主菜单可以直接推事件（不必挂子菜单），所以照豆包那排做成
# **一排平铺的 chip**，点一下就出对应的卡片 —— 不用先展开再选。
# 只有「常用」挂子菜单（问句太多，一排放不下）。
# 节点数：5 主 + 5 子 = 10 ≤ 10（满）。
# 2026-11-08：用户要「最近问过放进悬浮栏，方便快速找到」。悬浮栏实测最多 5 主，已经满了 ⇒
# 让出一格：「🧠 模型」挪进「⚡ 常用」（日志 118 次菜单点击里它 18 次，切一次管很久；
# 「⏹ 停下」虽然只点过 5 次，但要停的时候必须一下就点到，不挪）。节点数仍是 5 主 + 5 子 = 10。
# 2026-11-10：加「🌐 网页版」（跳转链接，电脑关着也能用）。主人选的让位：「🔑 权限」挪进「⚡ 常用」。
# 「⚡ 常用」实测每父最多 5 子 ⇒ 权限进来就得挤出一条：挤「找 TODO」（agent.log 11-04~11-10
# 菜单点击：/mode 33、/model 25、/menu 9、q:changes 5、q:todo 2、q:projects 2）。
# 「找 TODO」还在「全部按钮」那张卡上露着（menu._PINNED_QUESTIONS），没删功能，只是多点一下。
# 节点数：5 主 + 5 子 = 10 ≤ 10（满）。
# 2026-11-10 主人改定：「🧠 模型」单独放回第一排；「🔑 权限」「🌐 网页版」都进「⚡ 常用」。
MENU_TREE = (
    # 原来是「📁 项目」(/dirs)。换项目仍能打 `/cd`、或在「全部按钮」卡片里点「换项目」。
    ("🖥 会话", "/sessions", ()),
    ("↑ 最近", "/history", ()),
    ("🧠 模型", "/model", ()),
    (
        "⚡ 常用",
        "",
        (
            ("🔑 权限", "/mode"),
            # 跳转链接（menu_content_type=1），key 只是本文件里的名字，不会作为事件发出去。
            ("🌐 网页版", "link:web"),
            ("哪里有改动", "q:changes"),
            ("有哪些项目", "q:projects"),
            ("全部按钮", "/menu"),
        ),
    ),
    ("⏹ 停下", "/stop", ()),
)

# 跳转链接型菜单项：key → 链接。飞书客户端自己打开，**不推事件、不经过电脑** ⇒ 电脑关机/合盖/断网也能点。
# 🔴 这些 key 绝不进 `ALLOWED_KEYS`：它们永远不该从事件里来；要是来了，说明有人在伪造。
LINK_URLS = {
    "link:web": "https://chat.auto-link.com.cn/",
}


def is_link(key: str) -> bool:
    return key in LINK_URLS

# 常用问题的完整措辞 —— 菜单上显示短标签，发给 agent 的是这句。
#
# 保留了菜单里放不下的几条：按钮菜单卡片（`/menu`）上有全部 6 条，
# 所以这里不删 —— `resolve_event_key` 和卡片共用这份措辞。
QUESTION_TEXT = {
    "q:projects": "我有哪些项目？各自是什么",
    "q:changes": "哪些仓库有没提交的改动",
    "q:recent": "最近一周改了哪些文件",
    "q:todo": "找一下所有 TODO 和 FIXME",
    "q:tests": "这个项目的测试怎么跑",
    "q:ports": "看看有哪些进程和端口在用",
}

# 飞书实测出来的硬上限（文档没写）。见文件头。
MAX_NODES = 10
MAX_TOP = 5  # 悬浮样式（strategy=3）实测 5 主过、6 主拒
# 文件头实测「1 父 + 5 子过、6 子报 210011」；API 文档也写「悬浮菜单……每级菜单最多支持五个节点」。
# 原来这里写 10（照抄后台文档的「每个主菜单 10 个子菜单」），校验器放过了 6 子 ⇒ 改成实测的 5。
MAX_CHILDREN = 5

# `menu_content_type` 的数字含义
CONTENT_TYPE_NONE = 0  # 只展开子菜单，不推事件（实测）
CONTENT_TYPE_LINK = 1  # 跳转链接，带 redirect_link（官方文档 + SDK，没实打过，见文件头）
CONTENT_TYPE_EVENT = 2  # 推 application.bot.menu_v6，带 event_key（实测）

# `bot_menu_display_strategy`：**菜单显示在哪**。实测取值 [0,1,2,3]，
# 其中 0 直接被拒（210011），1 和 2 都接受。
#
# 🔴 不给这个字段也能写入成功，但用的是飞书默认值 —— 2026-11-04 第一次 apply
# 就漏了它，返回 code=0、发布完**仍然看不到菜单**。
# 「接口返回成功」不等于「用户看得见」，这个字段是中间差的那一环。
# 🔴 **3 = 悬浮菜单（常驻在输入框上方）**。2026-11-04 实测 + 官方文档对上的：
#
# 官方文档（open.feishu.cn/document/client-docs/bot-v3/bot-customized-menu.md）说两种样式：
#   可切换菜单：菜单和输入框在**同一位置**，点左侧按钮切换；最多 3 主 × 5 子；5.27+
#   悬浮菜单：  菜单**悬浮在输入框上方**、常驻；最多 5 主 × 10 子；7.22+
# 实测哪个值接受 4、5 个主菜单（只有悬浮允许超过 3 个）：
#   strategy=1 → 4 主就 210011　strategy=2 → 4 主就 210011　strategy=3 → 4、5 主都过、6 主拒
# 所以 1/2 是可切换那一族，3 是悬浮。
# 后来在 v7 ability PATCH 的官方文档里找到了原文定义，和实测一致：
#   0 未知（不推荐）  1 可切换-默认展示菜单  2 可切换-默认展示输入框  3 悬浮菜单
# 我们第一版用的 2 = 可切换 —— 用户说
# "看到了但和豆包不一样"，就是因为菜单占了输入框的位置、要点按钮切。
#
# ⚠️ 总节点数仍然卡在 10（`99992402 the max len is 10`），和文档的 5×10 对不上。
# 文档是后台手配的上限，API 这条路更严。不要按文档写。
DISPLAY_FLOAT = 3


def _all_event_keys() -> set[str]:
    keys = set()
    for _, key, children in MENU_TREE:
        if key and not is_link(key):
            keys.add(key)
        for _, child_key in children:
            if not is_link(child_key):
                keys.add(child_key)
    # 常用问题的 key 全部允许 —— 卡片菜单上有菜单里放不下的那几条
    keys.update(QUESTION_TEXT)
    return keys


ALLOWED_KEYS = frozenset(_all_event_keys())


def resolve_event_key(event_key: str) -> str:
    """菜单事件 → 等价的用户输入。认不出返回空串。

    🔴 只认菜单里**预设过的** key。事件来自飞书，但内容不该被直接信任 ——
    要是把 event_key 原样当消息执行，任何能伪造这个事件的东西就能让 agent 跑任意指令。
    """
    key = (event_key or "").strip()
    if key not in ALLOWED_KEYS:
        return ""
    if key.startswith("q:"):
        return QUESTION_TEXT.get(key, "")
    return key


def _node(name: str, sort: int, **extra) -> dict:
    """一个菜单节点。

    `i18n_name` **必填** —— 只给 `default_name` 会被 `210011` 拒掉，
    而那个错不说是哪个字段（见文件头）。
    """
    node = {
        "default_name": name,
        "i18n_name": {"zh_cn": name, "en_us": name},
        "sort": sort,
    }
    node.update(extra)
    return node


def _action(key: str) -> dict:
    """叶子节点点了做什么：跳转链接（不推事件）或推事件。"""
    if is_link(key):
        url = LINK_URLS[key]
        # 文档：type=1 时 redirect_link 必填。手机和电脑给同一个链接。
        return {"menu_content_type": CONTENT_TYPE_LINK,
                "redirect_link": {"pc_url": url, "mobile_url": url}}
    return {"menu_content_type": CONTENT_TYPE_EVENT, "event_key": key}


def build_bot_menus() -> list[dict]:
    """生成 `PATCH .../ability` 里 `bot.bot_menus` 要的结构。

    是个**拍平的数组**（父子都是其中一条，靠 `parent_menu_id` 关联），
    不是嵌套的树 —— 所以 10 条上限是父子加起来算。
    """
    nodes: list[dict] = []
    for index, (name, key, children) in enumerate(MENU_TREE):
        parent_id = f"m{index}"
        if children:
            # 父节点只展开，不推事件
            nodes.append(
                _node(name, index, menu_id=parent_id, menu_content_type=CONTENT_TYPE_NONE)
            )
        else:
            # 平铺 chip：点了直接推事件 / 直接打开链接
            nodes.append(_node(name, index, menu_id=parent_id, **_action(key)))
        for child_index, (child_name, child_key) in enumerate(children):
            nodes.append(
                _node(
                    child_name,
                    child_index,
                    menu_id=f"{parent_id}c{child_index}",
                    parent_menu_id=parent_id,
                    **_action(child_key),
                )
            )
    return nodes


def _link_problem(label: str, key: str) -> str:
    url = LINK_URLS.get(key, "")
    if not url.startswith("https://"):
        return f"「{label}」的链接 {url!r} 不是 https"
    return ""


def validate_tree() -> list[str]:
    """检查菜单是否满足飞书的**实测**硬限制。返回问题列表，空表示 OK。

    这些数字不是抄文档的 —— 文档写的 5×10 是错的，实测总数上限 10。
    见文件头那段。
    """
    problems = []
    total = len(build_bot_menus())
    if total > MAX_NODES:
        problems.append(
            f"一共 {total} 个节点，上限 {MAX_NODES}（父子加起来算）"
            " —— 要加新项必须先去掉一项"
        )
    if len(MENU_TREE) > MAX_TOP:
        problems.append(f"主菜单 {len(MENU_TREE)} 个，实测上限 {MAX_TOP}")
    for name, key, children in MENU_TREE:
        if len(children) > MAX_CHILDREN:
            problems.append(f"「{name}」子菜单 {len(children)} 个，实测上限 {MAX_CHILDREN}")
        if key and children:
            problems.append(f"「{name}」既有事件又有子菜单（有子菜单时父节点不推事件）")
        if not key and not children:
            problems.append(f"「{name}」没事件也没子菜单（点了什么都不会发生）")
        if key and is_link(key):
            if _link_problem(name, key):
                problems.append(_link_problem(name, key))
        elif key and not resolve_event_key(key):
            problems.append(f"「{name}」的 key {key!r} 解不出要发什么")
        if key and len(key) > 30:
            problems.append(f"event_key 超 30 字符：{key}")
        if len(name) > 60:
            problems.append(f"「{name}」名字太长")
        for child_name, child_key in children:
            if len(child_key) > 30:
                problems.append(f"event_key 超 30 字符：{child_key}")
            if len(child_name) > 60:
                problems.append(f"「{child_name}」名字太长")
            if is_link(child_key):
                if _link_problem(child_name, child_key):
                    problems.append(_link_problem(child_name, child_key))
            elif not resolve_event_key(child_key):
                problems.append(f"「{child_name}」的 key {child_key!r} 解不出要发什么")
    return problems


# ---------- 一键配好：菜单 + 点击事件 + 卡片回调 + 发布 ----------
#
# 安装器在扫码建完应用后跑这个（`python bridge/botmenu.py <config.toml>`），
# 让新同事装完**直接**有常驻菜单、点了有反应，不用去开发者后台。
# `scripts/apply_bot_menu.py` 是同一份逻辑的运维入口（带预览 / 只写不发布）。
#
# 只用标准库 HTTP：安装器跑它时 venv 刚建好，不想多一层 SDK 版本差异；
# 每个接口的必填项和坑都是 2026-11-04 实打出来的，见文件头。

_BASE = "https://open.feishu.cn/open-apis"
MENU_EVENT = "application.bot.menu_v6"
CARD_CALLBACK = "card.action.trigger"


def _reg():
    """权限 / 事件 / 回调清单的出处：`register.py`（它的建群那几项再从 `fleet/groupsetup.py` 的 NEEDS 取）。

    两种导入方式都要兼容：安装器用 `python bridge/botmenu.py` 脚本方式跑（没有父包）。
    放在函数里 import：app.py 只为 `resolve_event_key` 导入本模块，不该为此多拉一串。
    """
    try:
        from . import register as reg  # 包方式
    except ImportError:
        import register as reg  # type: ignore[no-redef]  # 脚本方式
    return reg


def _http(method: str, path: str, token: str | None, body=None) -> dict:
    import json as _json
    import urllib.error
    import urllib.request

    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = _json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(_BASE + path, data=payload, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return _json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            return _json.load(exc)
        except Exception:  # noqa: BLE001
            return {"code": exc.code, "msg": str(exc.reason)}
    except OSError as exc:
        return {"code": -1, "msg": f"网络不通：{exc}"}


def tenant_token(app_id: str, app_secret: str, *, http=None) -> str:
    out = (http or _http)(
        "POST",
        "/auth/v3/tenant_access_token/internal",
        None,
        {"app_id": app_id, "app_secret": app_secret},
    )
    if out.get("code"):
        raise RuntimeError(f"拿不到 token：code={out.get('code')} {out.get('msg')}")
    return out["tenant_access_token"]


def _missing_scopes(out: dict) -> list[str]:
    err = out.get("error") or {}
    return [v.get("subject", "") for v in err.get("permission_violations") or []]


def read_scopes(token: str, *, http=None) -> dict | None:
    """应用身份（tenant）已申请的权限 → {scope: 已授权?}。读不到 ⇒ None。

    `GET /application/v6/scopes`（「查询租户授权状态」）。文档写「权限要求：无」，
    2026-11-06 只读实测本机应用：code 0，列出全部已申请的 scope + `grant_status`（1 = 已授权）。
    🔴 已申请但还没批（grant_status≠1）也算「有了」：再 add 一次没用，差的是管理员审批那一步。
    """
    out = (http or _http)("GET", "/application/v6/scopes", token)
    if out.get("code"):
        return None
    got: dict = {}
    for item in (out.get("data") or {}).get("scopes") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("scope_name")
        kind = item.get("scope_type")
        if isinstance(name, str) and name and (not kind or kind == "tenant"):
            got[name] = got.get(name, False) or item.get("grant_status") == 1
    return got


def add_scopes(app_id: str, token: str, scopes: list[str], *, http=None) -> dict:
    """给应用加权限（应用身份）。`add_scopes` 是增量语义，不会删掉已有的。

    11-05 主会话在本机应用上用的就是这个形状，申请 + 发版后实测生效
    （`references/fleet-and-groups.md` 第三节）。这个接口本身要 `application:application:patch`。
    """
    return (http or _http)(
        "PATCH",
        f"/application/v7/applications/{app_id}/config",
        token,
        {"scope": {"add_scopes": [{"scope_name": s, "token_type": "tenant"} for s in scopes]}},
    )


def write_menu(app_id: str, token: str, *, http=None) -> dict:
    """写菜单树（悬浮样式）。返回原始响应。"""
    return (http or _http)(
        "PATCH",
        f"/application/v7/applications/{app_id}/ability",
        token,
        {
            "bot": {
                "enable": True,  # 必填，见文件头
                "bot_menu_enable": True,
                "bot_menu_display_strategy": DISPLAY_FLOAT,
                "bot_menus": build_bot_menus(),
            }
        },
    )


def subscribe(app_id: str, token: str, *, http=None) -> dict:
    """订阅收消息 + 菜单点击事件 + 卡片按钮回调（长连接）。

    `add_events` / `add_callbacks` 是**增量**语义（字段名就是 add_ / remove_），
    不会清掉已经订阅的。事件清单和扫码确认页预填的是同一份（`register.WANT_EVENTS`）：
    确认页预填要平台灰度、没开就被静默忽略 ⇒ 这里再补一遍，收消息那条也不能漏。
    """
    reg = _reg()
    events, callbacks = list(reg.WANT_EVENTS), list(reg.WANT_CALLBACKS)
    for must, have in ((MENU_EVENT, events), (CARD_CALLBACK, callbacks)):
        if must not in have:
            have.append(must)
    return (http or _http)(
        "PATCH",
        f"/application/v7/applications/{app_id}/config",
        token,
        {
            "event": {"subscription_type": "websocket", "add_events": events},
            "callback": {"callback_type": "websocket", "add_callbacks": callbacks},
        },
    )


def publish(app_id: str, token: str, version: str, changelog: str, *, http=None) -> dict:
    """发布一个版本。5 个字段都必填（缺后两个只回 210304 不说哪个）。"""
    return (http or _http)(
        "POST",
        f"/application/v7/applications/{app_id}/publish",
        token,
        {
            "version": version,
            "changelog": changelog,
            "remark": "Her remote自动配置",
            "mobile_default_ability": "bot",
            "pc_default_ability": "bot",
        },
    )


# ---------- 发布之后：上线了，还是进了管理员审批 ----------
#
# 🔴 发布接口的回包**分不出来**：官方文档（提交发布自建应用，2026-11-06 读）的响应体只有
# `version_id` + `version`，没有任何状态字段 ⇒ code 0 只代表「提交了」。
# 唯一能分清的是「获取应用版本信息」`GET /application/v6/applications/:app_id/app_versions/:version_id`
# 的 `status`（文档：0 未知 / 1 审核通过 / 2 审核拒绝 / 3 审核中 / 4 未提交审核）。
# 但它要 `application:application:self_manage` 或 `application:application.app_version:readonly`，
# 2026-11-06 只读实测本机应用两个都没有 ⇒ 99991672。读不到时**照实说两种可能**，不猜是哪一种。
VERSION_ONLINE = 1
VERSION_REJECTED = 2
VERSION_REVIEWING = 3
VERSION_NOT_SUBMITTED = 4

# 发布失败的错误码 → 人话 + 下一步。码和原意来自官方文档的错误码表（2026-11-06 读）。
PUBLISH_ERRORS = {
    210302: "上一个版本还在等管理员审批，新版本提交不上去 —— 等管理员批完上一个，重新双击安装就会接着发",
    210303: "版本号被拒（要 X.X.X 且比上一个大）—— 隔一分钟重新双击安装即可（版本号按时间生成）",
    210304: "发布请求缺字段 —— 这是程序的问题，不用截图，我们这边会跟进",
    210001: "这个应用不是在开发者后台建的，不能用接口发布 —— 去开发者后台「版本管理与发布」手动点一下",
    210036: "凭据和应用对不上（只能发布自己）—— 检查配置里的 app_id",
    210003: "飞书那边内部错误 —— 过几分钟重新双击安装",
}
CONFIG_ERRORS = {
    # 写菜单（ability PATCH）那个接口的码。2026-11-06 第 12 轮排查角色 D 查官方错误码表发现原来没收 ⇒
    # 撞上时屏幕上是原始「code=210021」——而这两个码恰好就是「装完没有悬浮菜单」最难分清的那两条原因。
    210020: "应用正在审核中，这时候写不进菜单 —— 等管理员批完手上那个版本，菜单会自己补上（或重新双击安装）",
    210021: "这个应用不是在开发者后台建的，接口写不了它的菜单 —— 不用截图，我们这边会跟进；聊天里发「菜单」照样有按钮",
    210040: "应用正在审核中，这时候改不了配置 —— 等管理员批完，重新双击安装",
    210041: "应用还没开机器人能力 —— 去开发者后台「添加应用能力」加上「机器人」",
    210043: "这个应用不是在开发者后台建的，接口改不了它的配置",
    210035: "这个应用不是在开发者后台建的，接口改不了它的配置",
    210037: "你不是这个应用的所有者或管理员，改不了它的配置",
}
# 第 38 轮（主人：不许让用户截图，报错我们这边自动上报）：上面文案说「我们这边会跟进」的码 ⇒ 必须真报上来。
#   桥里（menu_heal）按 ERROR 记一条固定模板 ⇒ bridge/errreport.py 收走；
#   安装器里（setup() 的输出）多打一行 `REPORT=…` ⇒ install-mac.sh / install-windows.ps1 调各自的失败上报。
REPORT_CODES = frozenset({210021})


def report_code(out: dict) -> int | None:
    """这次失败要不要报给我们：码在 REPORT_CODES 里就返回码，否则 None。"""
    try:
        code = int(out.get("code"))
    except (TypeError, ValueError):
        return None
    return code if code in REPORT_CODES else None


def _human(out: dict, table: dict) -> str:
    code = out.get("code")
    if code == -1:
        return f"连不上飞书（{out.get('msg')}）—— 检查网络 / VPN 后重新双击安装"
    missing = [s for s in _missing_scopes(out) if s]
    if missing:
        return (f"应用还没有「{'、'.join(missing)}」这项权限 —— 去开发者后台「权限管理」开一下，"
                "再重新双击安装")
    try:
        code = int(code)
    except (TypeError, ValueError):
        pass
    if code in table:
        return table[code]
    return f"飞书返回 code={out.get('code')} {out.get('msg')}"


def version_status(app_id: str, token: str, version_id: str, *, http=None) -> int | None:
    """查刚发的那个版本的审核状态。读不到（多半是缺权限）⇒ None。**只读**。"""
    if not version_id:
        return None
    out = (http or _http)(
        "GET",
        f"/application/v6/applications/{app_id}/app_versions/{version_id}?lang=zh_cn",
        token,
    )
    if out.get("code"):
        return None
    status = ((out.get("data") or {}).get("app_version") or {}).get("status")
    return status if isinstance(status, int) and not isinstance(status, bool) else None


# 审批在哪：管理员在飞书「审批」应用里（或管理后台「工作台 → 应用审核」）收到这条申请。
# 依据 `docs/productize-plan.md`：管理员能把审核方式设成「在飞书审批中审核」。没在本机实测过审批那一侧。
_REVIEW_WHERE = "管理员会在飞书「审批」里（或管理后台「应用审核」）收到「Her remote」的发布申请"


def setup(app_id: str, app_secret: str, version: str, *, http=None) -> list[str]:
    """装机用：补权限 → 写菜单 → 订阅事件和回调 → 发布 → 判「上线了 / 等审批」。返回给人看的结果行。

    🔴 顺序是定的：**补权限必须在发布之前**，否则新加的权限不进这个版本、等于没申请。
    🔴 **每一步失败都要说出来**，不许"装完了"而菜单其实没配上 ——
    那正是 2026-11-04 review 指出的：自检只验 token，事件和回调漏了也报绿。
    行首 `OK=` / `WARN=` / `NOTE=` 给安装器解析；`PUBLISH=online|review|submitted|failed` 给安装器判下一步。
    `http` 注入进来：测试里一个真请求都不发。
    """
    call = http or _http
    lines: list[str] = []
    try:
        token = tenant_token(app_id, app_secret, http=call)
    except RuntimeError as exc:
        return [f"WARN=常驻菜单和权限都没配上：{exc}", "PUBLISH=failed"]

    # 1. 补权限：扫码确认页的预填要平台灰度，没开就被静默忽略 ⇒ 这里回读、缺哪个补哪个。
    reg = _reg()
    have = read_scopes(token, http=call)
    # 读不到现状 ⇒ 全部申请一遍（add 是增量的，已有的不受影响）
    todo = reg.missing_scopes(have or ())
    sensitive, names = reg.SENSITIVE_SCOPES, reg.SCOPE_CONSOLE_NAMES
    added: list[str] = []
    if todo:
        out = add_scopes(app_id, token, todo, http=call)
        if out.get("code"):
            lines.append(f"WARN=建群和收消息要的权限没能自动申请：{_human(out, CONFIG_ERRORS)}")
            lines.append(f"NOTE=还差的权限：{', '.join(todo)}")
            lines.append(f"NOTE=手动开：https://open.feishu.cn/app/{app_id}/auth?q={','.join(todo)}")
        else:
            added = todo
            shown = "、".join(names.get(s, s) for s in todo)
            lines.append(f"OK=已申请权限 {len(todo)} 项（{shown}）")
    else:
        lines.append(f"OK=需要的 {len(reg.WANT_SCOPES)} 项权限都已申请过")
    if set(added) & set(sensitive) or (have is not None and set(sensitive) & {
        s for s, granted in have.items() if not granted
    }):
        lines.append("NOTE=其中「获取群组中所有消息」是飞书标的敏感权限，管理员审批时会多看一眼 ——"
                     "它是用来让你在群里不用 @ 机器人、直接说话的")

    # 2. 菜单
    out = write_menu(app_id, token, http=call)
    if out.get("code"):
        missing = _missing_scopes(out)
        if missing:
            lines.append(
                f"WARN=常驻菜单没配上：缺权限 {', '.join(missing)}（管理员批完后助手会自己补上，不用重装）"
            )
        else:
            lines.append(f"WARN=常驻菜单没配上：{_human(out, CONFIG_ERRORS)}")
            if report_code(out) is not None:
                lines.append(f"REPORT=常驻菜单没配上 code={report_code(out)} {str(out.get('msg') or '')[:200]}")
    else:
        lines.append("OK=常驻菜单已写入")

    # 3. 订阅
    sub = subscribe(app_id, token, http=call)
    if sub.get("code"):
        lines.append(
            f"WARN=按钮点击订阅失败 code={sub.get('code')} {sub.get('msg')}"
            " —— 菜单和卡片按钮会点了没反应"
        )
    else:
        lines.append("OK=收消息和按钮点击已订阅")

    # 4. 发布（必须在补权限之后）
    pub = publish(app_id, token, version, "配置权限、常驻菜单和按钮回调", http=call)
    if pub.get("code"):
        lines.append(f"WARN=没能自动发布：{_human(pub, PUBLISH_ERRORS)}")
        lines.append(f"NOTE=手动发布：https://open.feishu.cn/app/{app_id}/publish")
        lines.append("PUBLISH=failed")
        return lines

    data = pub.get("data") or {}
    shown_version = data.get("version") or version
    status = version_status(app_id, token, data.get("version_id") or "", http=call)
    if status == VERSION_ONLINE:
        lines.append(f"OK=已发布版本 {shown_version}，你们企业免审核，约 5 分钟后生效")
        lines.append("PUBLISH=online")
    elif status == VERSION_REVIEWING:
        lines.append(f"OK=已提交版本 {shown_version}，等管理员批一下")
        lines.append(f"NOTE={_REVIEW_WHERE}；批完约 5 分钟后生效，你这边不用再做什么")
        lines.append("PUBLISH=review")
    elif status == VERSION_REJECTED:
        lines.append(f"WARN=版本 {shown_version} 被管理员拒了 —— 问一下管理员原因，处理完重新双击安装")
        lines.append("PUBLISH=failed")
    else:
        # 读不到状态（常态：缺版本读取权限）⇒ 两种可能都说，不替飞书下结论
        lines.append(f"OK=已提交版本 {shown_version}")
        lines.append("NOTE=如果你们企业免审核：约 5 分钟后生效。"
                     f"如果要审核：{_REVIEW_WHERE}，批了才生效")
        lines.append("PUBLISH=submitted")
    return lines


def menu_landed(lines: list[str]) -> bool:
    """`setup()` 的结果行里：菜单写成 + 订阅成 + 版本提交成（不论上线还是等审批）。

    给 `menu_heal` 落「已配好」标记用 —— 🔴 装机这次成功了必须落，否则桥一启动又写一遍菜单、
    又发一个版本，企业要审核时管理员平白多收一条审批（第 12 轮）。
    """
    return (
        "OK=常驻菜单已写入" in lines
        and "OK=收消息和按钮点击已订阅" in lines
        and "PUBLISH=failed" not in lines
        and any(line.startswith("PUBLISH=") for line in lines)
    )


def _mark_installed(config_path: str, app_id: str, version: str) -> None:
    """安装器这次配成了 ⇒ 落 `menu_heal` 的标记。落不成只少一次省审批，不影响安装。"""
    try:
        try:
            from . import menu_heal  # 包方式
        except ImportError:
            import menu_heal  # type: ignore[no-redef]  # 脚本方式（安装器）
        menu_heal.mark_done(menu_heal.state_path_for(config_path), app_id, version)
    except Exception as exc:  # noqa: BLE001
        print(f"NOTE=常驻菜单已配好，但没记下来（{type(exc).__name__}）—— 桥启动后可能会再发一次版本",
              flush=True)


if __name__ == "__main__":  # pragma: no cover - 安装器入口
    import sys
    import time
    import tomllib

    if len(sys.argv) != 2:
        print("用法: python bridge/botmenu.py <config.toml>")
        raise SystemExit(2)
    with open(sys.argv[1], "rb") as handle:
        feishu = tomllib.load(handle).get("feishu", {})
    _version = time.strftime("1.%Y%m%d.%H%M%S")
    _lines: list[str] = []
    for line in setup(
        feishu.get("app_id", ""),
        feishu.get("app_secret", ""),
        _version,
    ):
        print(line, flush=True)
        _lines.append(line)
    if menu_landed(_lines):
        _mark_installed(sys.argv[1], feishu.get("app_id", ""), _version)
