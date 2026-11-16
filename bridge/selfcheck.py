"""助手自己回答「我是哪一版、新不新、我现在正常吗、有问题你下一步做什么」。

## 为什么要有

2026-11-06 一天换了 3 次安装包附件，同事和领导都**无法回答「我手上是哪一版」**
⇒「我这边修好了」和「你那边没生效」分不开，一路靠猜。
已经有 `status.py` 的诊断卡，但它要人**主动发「诊断」**，而且一屏数字对小白不友好：
那张卡回答「这台机器出了什么毛病」（给 IT 看），这个模块回答
「你现在能不能用 + 你下一步做什么」（给他自己看）。

## 形状：三个纯函数

- `summary(ctx)` —— 一屏「我现在好不好」：总体一句话 + 几条明细 + 问题清单
- `version_line(ctx)` —— 「我是哪一版、新不新」
- `problems(ctx)` —— `[{"what": 人话, "why": 一句原因, "next": 他能做的下一步}]`

🔴 **自己不读任何全局状态**（不查网、不读盘、不看时钟、不 import app）。
`ctx` 由调用方攒好传进来 —— 这样这些判断才测得死，而且在"东西已经坏了"的时候
照样能跑完：一个探不出来的字段只是少一行，不会让整条回复发不出去。

## ctx 的字段（全部可缺、可 None、类型可错）

| 键 | 意思 | 取不到时 |
|---|---|---|
| `version` | 本机这一版的版本号（`status.build_id`） | None ⇒ 开发机，照实说「说不出版本号」 |
| `latest_version` | 服务端最新版（`selfupdate.latest()["build_id"]`） | None ⇒ 🔴**只能说「查不到」，绝不说「已是最新」** |
| `connected` | 长连接活着吗（`Bridge.connection_alive()`） | None ⇒ 当没连上（fail-closed） |
| `offline_sec` | 断了多少秒（`Bridge.silent_for`） | 缺 ⇒ 不说时长 |
| `uptime_sec` | 这个进程起来多久 | 缺 ⇒ 不说 |
| `restarts` | 日志里数出来的启动次数 | 缺 ⇒ 不判崩溃循环 |
| `owner_bound` | 主人绑上了吗（`PairingState.bound` / 配置里有没有主人） | None ⇒ 当没绑（fail-closed） |
| `menu_ready` | 常驻菜单配上了吗（`MenuHeal._done`） | None ⇒ 当没配 |
| `menu_reason` | 菜单没配上的短原因（`MenuHeal.last_reason`） | 任意脏字符串，只当**有没有**用，不上屏 |
| `mode` / `ceiling_mode` | 当前权限档 / 本机上限（`policy.MODE_LABELS` 的键） | 认不出 ⇒ 那一行不显示 |
| `workspace` | 现在在哪个项目（**已经缩成 `~/…`** 由调用方负责） | 带绝对路径 ⇒ 这里兜底不显示 |
| `last_error` | 最后一类错的**类名**（`status.ERROR_CLASSES` 的键） | 认不出 ⇒ 不显示 |

## 总体一句话：只有三种，判据在这里

- `DOWN`（🔴 现在用不了）：**他发消息也没用**的那些 ——
  没连上飞书（`connected` 不为真）、或没认主人（`owner_bound` 不为真）、
  或 `ctx` 整个是空/不是 dict（探不出来 ≠ 好的，见 `_unknown`）。
- `WARN`（⚠️ 能用但有问题）：能对话，但有一件事缺着 ——
  常驻菜单没配上、有新版还没装上、查不到最新版、说不出本机版本、
  启动次数多到像崩溃循环、最近有一类错。
- `OK`（✅ 一切正常）：上面都不成立。

🔴 **`DOWN` 压过 `WARN`**：断线同时菜单也没配，总体还是 🔴。
🔴 **「查不到最新版」永远不说成「已是最新」**。"我没观测到" ≠ "它不存在" ——
今天的现场恰恰是老版本的人装不到自动更新，而他以为自己是新的。

## 文案红线

给人看的字里不许出现内部名词、不许透传异常原文（`tests/test_copy_quality.py` 是闸门）。
所以 `menu_reason` / `last_error` 这类**来源不可控**的字段只用来判有没有，
上屏的话一律从下面写死的表里取（`_ERROR_HINT`），认不出就走通用那句。
"""

from __future__ import annotations

OK = "✅ 一切正常"
WARN = "⚠️ 能用但有问题"
DOWN = "🔴 现在用不了"

# 一行最多这么长。和 `status.HEALTH_VALUE_MAX` 同一个理由：万一哪天有人往某个字段里
# 塞了一整行日志，卡上也只会出现一小截，而不是把整行外发。
LINE_MAX = 160
# 版本号这种定长字段单独一个更紧的上限（真实的是 `20261106-1200` 这种 13 个字符）。
VALUE_MAX = 40
# 一屏能看完。全坏的时候也不堆十几条 —— 小白只会关掉。
MAX_PROBLEMS = 4

# 启动次数超过这个数就当崩溃循环（跑了 50 秒 + 启动 40 次 是今天那类故障的形状）。
RESTART_ALARM = 10


def _text(value, limit: int = VALUE_MAX) -> str:
    """任意类型 → 一段能上屏的短字符串。None / 空 / 非字符串一律变空串。

    🔴 只收 `str`：`version` 被塞成 7 或 `{}` 时，`str()` 出来的 `7` / `{}` 上屏
    比不显示更糟（他会以为那就是版本号）。
    """
    if not isinstance(value, str):
        return ""
    out = value.replace("\n", " ").strip()
    return out[:limit]


def _flag(value) -> bool:
    """「这件事好了吗」。🔴 只有**真的是 True** 才算好 —— None / 缺字段 / 字符串
    `"yes"` 全当没好（fail-closed）。方向是故意的：说成好的会让他白等。
    """
    return value is True


def _num(value):
    """数字字段 → float 或 None。`"一会儿"` 这种一律 None（那一行不显示）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dict(ctx) -> dict:
    return ctx if isinstance(ctx, dict) else {}


def _unknown(ctx) -> bool:
    """什么都没传进来。🔴 这种情况不许回「一切正常」—— 探不出来和探出来是好的是两件事。"""
    data = _dict(ctx)
    return not any(key in data for key in
                   ("version", "connected", "owner_bound", "menu_ready", "uptime_sec"))


def _elapsed(seconds) -> str:
    """秒 → 人话。给小白看，不给 `39m04s` 这种。"""
    value = _num(seconds)
    if value is None or value < 0:
        return ""
    total = int(value)
    if total < 60:
        return f"{total} 秒"
    if total < 3600:
        return f"{total // 60} 分钟"
    hours, minutes = divmod(total // 60, 60)
    return f"{hours} 小时" if not minutes else f"{hours} 小时 {minutes} 分"


# ======================================================================
# 版本
# ======================================================================


def version_line(ctx: dict) -> str:
    """「我是哪一版、新不新」一句话。

    四种结果，一一对应今天那四种现场：
    - 本机 == 服务端 ⇒ 「现在这版 X，已是最新」
    - 不等 ⇒ 「现在这版 X，有新版 Y，我会自己装上」（他不用再去文档下附件）
    - 服务端取不到 ⇒ 🔴「现在这版 X，查不到有没有更新的（可能没连上公司网络）」
      **不许说成已是最新**
    - 本机没有版本号（从仓库直接跑的开发机）⇒ 照实说，不装作有版本

    拿不到任何东西也返回一句完整的话（这句会直接进回复，不能是空串）。
    """
    data = _dict(ctx)
    mine = _text(data.get("version"))
    newest = _text(data.get("latest_version"))

    if not mine:
        # 开发机：装出来的包一定带版本号，没有就是从源码目录直接跑的
        if newest:
            return f"🏷 我说不出自己是哪一版（这台机器不是装出来的），公司现在发的是 {newest}"
        return "🏷 我说不出自己是哪一版（这台机器不是装出来的）"
    if not newest:
        return f"🏷 现在这版 {mine}，查不到公司有没有更新的 —— 可能这台电脑没连上公司网络"
    if newest == mine:
        return f"🏷 现在这版 {mine}，已是最新"
    return f"🏷 现在这版 {mine}，公司已经发了新版 {newest} —— 空下来我会自己装上，不用你再下一次"


# ======================================================================
# 问题清单
# ======================================================================

# 最后一类错 → 给他看的一句话 + 下一步。
#
# 🔴 键是 `status.ERROR_CLASSES` 里写死的类名，**不是**日志原文。值也写死在这里 ——
# 任何来自日志/异常的字符串都不上屏（文案闸门：不许透传异常原文）。
# 认不出的类名走通用那条（`_ERROR_FALLBACK`），不把原字符串拼进去。
_ERROR_HINT = {
    "连不上飞书": ("刚才有几次连不上飞书",
                "这台电脑的网络断过，或者公司网关那会儿不通",
                "现在能收到这条回复就说明已经好了。要是再不回你，点「运行诊断」把卡片转给管理员"),
    "长连接断开": ("和飞书的连接断过",
                "多半是这台电脑睡过去了 —— 合盖期间飞书发来的消息不会补发",
                "要让它一直在线，把这台电脑的盖子打开、电源插上"),
    "看门狗重启": ("它自己重启过",
                "连接静默太久，它主动重启了一次来恢复",
                "现在能回你就说明恢复好了。重启那会儿发的那句重新发一遍"),
    "卡片发不出": ("有几张卡片发不出去",
                "飞书那边把卡片拒了，多半是应用权限还在等管理员审批",
                "找管理员把这个应用的权限批一下；在那之前我会用纯文字回你"),
    "消息发不出": ("有几条回复没发出去",
                "发消息的时候被飞书拒了，多半是权限还没批完",
                "找管理员把这个应用的权限批一下"),
    "凭据/权限被拒": ("飞书那边拒了我的身份",
                   "应用的凭据过期了，或者权限被收回了",
                   "把这台电脑上的安装包重新双击一次；还不行就找管理员看这个应用的权限"),
    "助手没回话": ("有几轮它一个字都没回",
                "电脑上那个助手那次没出结果，常见是额度用完或者模型那边超时",
                "把刚才那句重新发一遍；连着几次都这样，点「运行诊断」把卡片转给管理员"),
}
_ERROR_FALLBACK = ("最近出过一次错",
                   "出错那一类已经记下来了，但这里说不清是哪种",
                   "点「运行诊断」把那张卡片转给管理员，上面有判断要用的东西")


def problems(ctx: dict) -> list:
    """现在有哪些问题，每条带「他能做的下一步」。全好 ⇒ 空列表。

    排序 = 严重程度：发消息根本到不了的在最前，缺一件事的在后。
    🔴 每条的 `next` 必须是他**真能做到**的动作（点某个按钮 / 发某两个字 /
    找谁说一句 / 重新双击安装包）。没有别的办法时就直说只能重装，不讲原理。
    """
    data = _dict(ctx)
    out: list = []

    def add(what: str, why: str, nxt: str) -> None:
        out.append({"what": _text(what, LINE_MAX),
                    "why": _text(why, LINE_MAX),
                    "next": _text(nxt, LINE_MAX)})

    # ---------- 🔴 这几条意味着「他发消息也没用」 ----------

    if _unknown(ctx):
        add("我读不出自己的状态",
            "刚启动的时候这些还没来得及凑齐",
            "过一分钟再发「诊断」问我一次；一直这样就把这台电脑上的安装包重新双击一次")
        return out[:MAX_PROBLEMS]

    if not _flag(data.get("connected")):
        how_long = _elapsed(data.get("offline_sec"))
        why = "这台电脑和飞书的连接断了" + (f"，断了 {how_long}" if how_long else "")
        add("现在收不到你的消息",
            why + "。断开期间飞书不会把消息补发给我",
            # 第 15 轮把重连窗口压到半分钟内（`bridge/wsfast.py`），这句跟着改：
            # 原来写「几分钟 / 十几分钟」是照 SDK 默认节奏（实测 10~21 分钟）写的。
            "我正在自己重连，一般半分钟内就好。过几分钟还这样，"
            "多半是这台电脑睡着了或者断了网：把盖子打开、确认连着网")

    if not _flag(data.get("owner_bound")):
        add("我还不知道谁是主人",
            "装完还没完成认领，所以谁发消息我都不敢回 —— 这就是「发了没反应」的样子",
            "回到装它的那台电脑，把屏幕上那串认领码发给我；找不到了就把安装包重新双击一次，会给一串新的")

    # ---------- ⚠️ 能用，但缺一件事 ----------

    if not _flag(data.get("menu_ready")):
        # 🔴 `menu_reason` 的来源是飞书错误码拼出来的句子，一个字都不往上放。
        add("左下角那排常驻菜单还没出来",
            "装的时候菜单没配上，多半是这个应用的权限还在等管理员审批",
            "先直接打字跟我说话，一样好用。要那排菜单就找管理员把这个应用批一下，批完会自己补上")

    mine = _text(data.get("version"))
    newest = _text(data.get("latest_version"))
    if mine and newest and mine != newest:
        add(f"有新版 {newest} 还没装上",
            f"你手上这版是 {mine}，公司已经发了更新的",
            "不用你管，我空下来会自己装；想马上装就发「更新」两个字")
    elif mine and not newest:
        add("查不到公司现在发到哪一版",
            "这台电脑这会儿连不上放更新的地方，所以我说不出你手上这版新不新",
            "连上公司网络之后发「更新」两个字，我再查一次")
    elif not mine:
        add("我说不出自己是哪一版",
            "这台机器上的助手不是用安装包装的，所以没有版本号",
            "要能对版本就把安装包重新双击一次装一遍")

    restarts = _num(data.get("restarts"))
    uptime = _num(data.get("uptime_sec"))
    if restarts is not None and restarts >= RESTART_ALARM and (uptime is None or uptime < 3600):
        add(f"它今天自己重启了 {int(restarts)} 次",
            "起来一会儿就退、又被拉起来，这种情况下你发的消息有一部分会丢",
            "点「运行诊断」把那张卡片转给管理员 —— 这个要看日志才判得出来")

    hint = _ERROR_HINT.get(_text(data.get("last_error")))
    if hint is None and _text(data.get("last_error")):
        hint = _ERROR_FALLBACK
    if hint is not None:
        add(*hint)

    return out[:MAX_PROBLEMS]


# ======================================================================
# 一屏卡
# ======================================================================


def _detail_lines(ctx: dict) -> list:
    """「现在是什么样」几条明细。每条单独取、取不到就少一行（绝不编一个值出来）。"""
    data = _dict(ctx)
    lines: list = [version_line(ctx)]

    if _flag(data.get("connected")):
        lines.append("🟢 连着飞书，随时能用")
    else:
        how_long = _elapsed(data.get("offline_sec"))
        lines.append(f"🔴 和飞书的连接断了{f'（{how_long}）' if how_long else ''}，正在重连")

    up = _elapsed(data.get("uptime_sec"))
    if up:
        lines.append(f"⏱ 这次已经连着跑了 {up}")

    # 权限档：只认写死的档名，认不出就不显示（这一行说的是"它敢动你什么东西"，不能瞎猜）
    labels = {"readonly": "只读：默认只看，要改东西会先弹卡片问你",
              "write": "可改文件：能编辑代码，不会提交也不会推送",
              "trusted": "全授权：不问就做（仍挡着少数不可逆的操作）"}
    mode = labels.get(_text(data.get("mode")))
    if mode:
        lines.append(f"🔑 {mode}")

    where = _text(data.get("workspace"), 60)
    # 兜底：带绝对路径的不显示（里面有同事真名，而这张卡的用途就是转发出去）
    if where and not where.startswith("/") and "\\" not in where:
        lines.append(f"📂 现在在 {where}")

    if _flag(data.get("menu_ready")):
        lines.append("📌 左下角那排常驻菜单已经配好")

    return [line[:LINE_MAX] for line in lines if line]


def summary(ctx: dict) -> dict:
    """一屏「我现在好不好」。

    返回：
    - `verdict`：`"ok"` / `"warn"` / `"down"`（给调用方挑卡片颜色用）
    - `headline`：总体一句话，只可能是 `OK` / `WARN` / `DOWN` 三个常量之一
    - `lines`：几条明细（版本、连接、已跑多久、权限、项目、菜单）
    - `problems`：就是 `problems(ctx)` 本身（🔴 不另算一套，两套会漂）
    - `text`：上面全部拼成的一段话，调用方可以直接发出去

    判据见文件头。`DOWN` 压过 `WARN`。
    """
    data = _dict(ctx)
    items = problems(ctx)

    blocked = (_unknown(ctx)
               or not _flag(data.get("connected"))
               or not _flag(data.get("owner_bound")))
    if blocked:
        verdict, headline = "down", DOWN
    elif items:
        verdict, headline = "warn", WARN
    else:
        verdict, headline = "ok", OK

    lines = _detail_lines(ctx)

    parts = [headline, ""]
    parts.extend(lines)
    if items:
        parts.append("")
        for item in items:
            parts.append(f"**{item['what']}**")
            parts.append(f"· 为什么：{item['why']}")
            parts.append(f"· 你可以：{item['next']}")
    elif verdict == "ok":
        parts.append("")
        parts.append("有事直接说，不用记命令；不想打字就点「菜单」。")

    return {
        "verdict": verdict,
        "headline": headline,
        "lines": lines,
        "problems": items,
        "text": "\n".join(parts).strip(),
    }


# ── 第 16 轮：档位被升级了要告诉用户 ──

def migration_notice(migrated_from: str, now_mode: str) -> str:
    """这次升级把他从只读挪到了全控制 ⇒ 一句话告诉他，并说清怎么改回去。

    🔴 悄悄改一个人的权限档是不行的。自动更新在后台跑，没人看安装器的屏幕，
    所以只能由桥在启动后说（`migrated_from` 由 `bridge/carryover.py` 写进配置）。
    不是从只读迁来的（空值 / 别的值）⇒ 空串，什么都不说。
    """
    if str(migrated_from or "") != "readonly":
        return ""
    if str(now_mode or "") not in ("trusted", "write"):
        return ""
    strong = "现在它不问就动手" if now_mode == "trusted" else "现在它能直接改文件"
    return (
        "🔑 这次升级把权限放开了：之前是「只看」，每一步都要你点同意；"
        f"{strong}（删家目录照旧挡着；推代码、读凭据、提权会先问你）。\n"
        "想回到「只看」：点一下「🔑 权限」挑「只读」，立刻生效。"
    )
