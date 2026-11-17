"""扫码建应用：把"去开放平台点 5 步"压成"扫个码"。

这是官方能力，不是社区 hack —— `lark_oapi.register_app` 走 OAuth 2.0 设备授权流程
（RFC 8628），端点 `accounts.feishu.cn/oauth/v1/app/registration`，`archetype=PersonalAgent`。
一次扫码同时拿到三样东西：

    {"client_id": "cli_...", "client_secret": "...", "user_info": {"open_id": "ou_..."}}

`user_info.open_id` 就是**扫码那个人**的 open_id —— 这一条直接消灭了推广里最大的摩擦：
让几十个同事去"发消息、翻日志、复制 ou_xxx 粘回配置"。

`addons` 能把权限和事件预填到确认页，员工点一下就都开好了。但官方 README 明确写它
**需要平台灰度开启**，没开就整个参数被静默忽略 —— 所以创建完必须回读校验，不能假定
预填生效了（见 `verify_app_config`）。灰度没开时的兜底在 `botmenu.setup()`：
装机后回读权限、缺哪个用 API 补哪个，再发布。

输出走 stdout 的 KEY=VALUE，给安装脚本 sed 解析。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "https://open.feishu.cn/open-apis"
TIMEOUT = 15

# 🔴 建群那几项的**唯一真相**在 `fleet/groupsetup.py` 的 NEEDS（那边按它做体检、出「去开权限」卡）。
# 这里只 import，不抄 —— 2026-11-06 之前这里只写了 3 项，建群要的 5 项装机时一个都没申请，
# 每个新同事装完「一个会话一个群」都用不了，得有人去后台手动开 + 发版。
# 两种导入方式都要兼容（同 `_print_qr`）：安装器用 `python bridge/register.py` 脚本方式跑。
try:
    from .fleet.groupsetup import NEEDS as _GROUP_NEEDS  # 包方式
except ImportError:
    from fleet.groupsetup import NEEDS as _GROUP_NEEDS  # type: ignore[no-redef]  # 脚本方式

# 单聊本身要的（不在建群清单里的那几项）
_BASE_SCOPES = (
    "im:message.p2p_msg:readonly",  # 收你的私聊消息
    "im:message:send_as_bot",  # 给你回消息
    # 装机时自动补权限、配常驻菜单、发布版本都靠它。
    # 没有它，新同事装完只有聊天里的卡片菜单，没有常驻菜单（2026-11-04 review）。
    "application:application:patch",
    # ── 第 18 轮：两件用户催过多次的事，都要新权限 ──
    #
    # 🔴 能加这两项的前提是第 17 轮修好的「权限清单漂移」：
    # `menu_heal.scopes_fingerprint()` 把清单指纹和 `done` 记在一起，清单一变老用户的
    # 「已装好」标记就失效 ⇒ 下次重启自愈会重跑、把新权限补上。
    # 在那之前往这里加东西对老用户**完全无效**（装机只申请一次，done 之后再也不碰权限）。
    #
    # 1. 表情回复：用户发完消息立刻给个「收到」的表情（他原话「比如 get 符号」）。
    #    🔴 按**服务端的原话**要，不按文档那句中文要 —— 文档「权限要求」写的是
    #    「获取与发送单聊、群组消息」二者任一即可，而我们已经有那一组，真打一发仍然被拒：
    #        code=99991672  One of the following scopes is required:
    #        [im:message, im:message.reactions:write_only]
    #    两个里挑**窄**的那个：`im:message` 是单聊群聊全量读写，比我们要的宽得多。
    "im:message.reactions:write_only",
    # 2. 流式输出（第 17 轮角色 F 的方案）：建卡片实体 + 反复推全量文本。
    #    F 实测现有 13 项 scope 里 cardkit 一项都没有。
    "cardkit:card:write",
    # 3. FC-09 图片 / 文件输入：下载消息里的资源。2026-11-17 实测服务端原话：
    #        code=99991672  One of the following scopes is required:
    #        [im:message.history:readonly, im:message:readonly, im:message]
    #    挑只读的 `im:message:readonly`，不要可写的 `im:message`。
    "im:message:readonly",
    # 4. FC-10 长回复转飞书文档（2026-11-17 实测服务端原话，各挑最窄的一项）：
    #    建文档 [docx:document, docx:document:create]；加协作者 [drive:drive, …, docs:permission.member:create]。
    #    转块、写块用的 `docx:document.block:convert` / `docx:document:write_only` 本机已有，一并写进清单。
    "docx:document:create",
    "docx:document.block:convert",
    "docx:document:write_only",
    "docs:permission.member:create",
    # 2026-11-17 起需要人介入只 @ 主人，不再加急 ⇒ 不再申请 im:message.urgent:app_send。
)
# 这台机器需要的全部权限面：单聊 3 项 + 建群清单里推荐开的那一项（去重、保序）。
WANT_SCOPES = list(dict.fromkeys([*_BASE_SCOPES, *(need[2] for need in _GROUP_NEEDS)]))
# 推荐项 → 也能顶用的别的 scope（比如 `im:chat` 一项能顶掉建群那四项）。已经有替代项就不再申请。
SCOPE_ALTERNATIVES = {need[2]: tuple(need[4]) for need in _GROUP_NEEDS}
# 推荐项 → 飞书后台里显示的权限名（给人看）
SCOPE_CONSOLE_NAMES = {need[2]: need[3] for need in _GROUP_NEEDS}
# 敏感权限：管理员审批时会多看一眼。就是 NEEDS 里「收群里每一句」那项（获取群组中所有消息，
# groupsetup.py 文件头那张表标了「敏感权限」）—— 按能力 key 取，不写 scope 字面量。
SENSITIVE_SCOPES = frozenset(need[2] for need in _GROUP_NEEDS if need[0] == "receive")


def missing_scopes(have) -> list[str]:
    """WANT_SCOPES 里还没有的（推荐项和它的替代项都不在 `have` 里）。保持 WANT_SCOPES 的顺序。"""
    have = set(have or ())
    return [
        scope
        for scope in WANT_SCOPES
        if scope not in have and not (set(SCOPE_ALTERNATIVES.get(scope, ())) & have)
    ]


# 卡片按钮回调（审批「同意/不行」、权限/模型选择、菜单卡片）。
# 🔴 2026-11-04 review：`preset=False` 丢掉了默认模板，原来**没要这个回调** ⇒
# 新装的同事点任何卡片按钮，飞书都没地方推，按钮全部点了没反应。
# SDK 自带测试用的也是这个形状（lark_oapi/scene/registration/test_app_preset.py）。
WANT_CALLBACKS = ["card.action.trigger"]
# 🔴 菜单点击事件必须在这里一起订阅。2026-11-04 实测：只订了收消息时，
# 常驻菜单能显示、点了**没有任何反应**，日志里 0 次点击 —— 飞书根本没推。
# 代码里注册了处理函数也没用，订阅是应用配置那一侧的事。
WANT_EVENTS = ["im.message.receive_v1", "application.bot.menu_v6"]


def build_addons() -> dict:
    """预填确认页。preset=False 丢掉默认模板，只要机器人能力。"""
    return {
        "preset": False,
        "scopes": {"tenant": WANT_SCOPES},
        "events": {"items": {"tenant": WANT_EVENTS}},
        "callbacks": {"items": WANT_CALLBACKS},
    }


def register(app_name: str) -> dict:
    """走完扫码流程。返回 {app_id, app_secret, open_id}。"""
    import lark_oapi as lark

    def on_qr_code(info) -> None:
        url = info.get("url") if isinstance(info, dict) else getattr(info, "url", "")
        # 二维码画在终端里；同时给出链接，因为很多人用电脑飞书点链接比掏手机快
        print(f"QR_URL={url}", flush=True)
        _print_qr(url)

    def on_status_change(info) -> None:
        status = info.get("status") if isinstance(info, dict) else ""
        if status == "pending":
            return
        print(f"STATUS={status}", flush=True)

    try:
        result = lark.register_app(
            on_qr_code=on_qr_code,
            on_status_change=on_status_change,
            app_preset={"name": app_name, "desc": "在飞书里指挥我自己的电脑干活"},
            addons=build_addons(),
            create_only=True,  # 隐藏「选择已有应用」，防止误绑到别的应用上
            source="carher-feishu-agent",
        )
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"扫码注册失败：{exc}") from exc

    if not isinstance(result, dict):
        raise RuntimeError("注册返回格式不对")
    app_id = result.get("client_id") or ""
    app_secret = result.get("client_secret") or ""
    if not app_id or not app_secret:
        raise RuntimeError("注册没拿到 app_id / app_secret")
    open_id = (result.get("user_info") or {}).get("open_id") or ""
    return {"app_id": app_id, "app_secret": app_secret, "open_id": open_id}


def _print_qr(url: str) -> None:
    """终端里画二维码。画不出来就只给链接 —— 不为这个加硬依赖。

    Windows 上故意不画：终端默认字体渲染半角方块很糟，容易糊成一片扫不出来，
    给个链接让用户在电脑飞书里点更可靠。细节在 osenv.py。

    🔴 这里必须兼容两种导入方式：安装器用 `python bridge/register.py`（脚本方式，
    没有父包，相对导入直接 ImportError），服务跑的是 `python -m bridge.app`（包方式）。
    2026-11-03 实测踩过：只写相对导入时装机报
    「扫码注册失败：attempted relative import with no known parent package」。
    """
    try:
        from .osenv import print_qr  # 包方式
    except ImportError:
        from osenv import print_qr  # type: ignore[no-redef]  # 脚本方式

    if not print_qr(url):
        print("（终端不画码，直接点上面那条链接，或手机飞书扫）", flush=True)


def _token(app_id: str, app_secret: str) -> str:
    request = urllib.request.Request(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        data=json.dumps({"app_id": app_id, "app_secret": app_secret}).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        data = json.loads(response.read())
    if data.get("code") != 0:
        raise RuntimeError(f"换 token 失败 code={data.get('code')} msg={data.get('msg')}")
    return data["tenant_access_token"]


def verify_app_config(app_id: str, app_secret: str) -> dict:
    """回读实际配置，确认预填真的生效了。

    必须有这一步：`addons` 需要平台灰度，没开就被静默忽略 —— 员工会看到一个
    空权限的确认页，然后以为装好了，实际收不到任何消息。
    """
    report: dict = {"token_ok": False, "scopes_ok": None, "note": ""}
    try:
        token = _token(app_id, app_secret)
        report["token_ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["note"] = str(exc)
        return report

    # 能换到 token 说明应用存在且凭据对。权限是否齐，用"能不能真收消息"来验更准，
    # 但那要等用户发消息。这里退一步：试调一个需要 im 权限的接口看有没有被拒。
    try:
        request = urllib.request.Request(
            f"{BASE}/bot/v3/info",
            headers={"Authorization": f"Bearer {token}"},
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            info = json.loads(response.read())
        if info.get("code") == 0:
            report["bot_name"] = (info.get("bot") or {}).get("app_name", "")
    except Exception:  # noqa: BLE001
        report["note"] = "拿不到机器人信息（不影响使用）"
    return report


def main(argv: list[str]) -> int:
    # --check 只验 import 链和 addons 编码，**不发起扫码**。
    # 自检和测试用它：真注册会向飞书要一个 device code 然后轮询最多一小时，
    # 测试里直接起这个脚本会挂住（2026-11-03 踩过）。
    if "--check" in argv[1:]:
        import base64
        import gzip

        payload = json.dumps(build_addons(), ensure_ascii=False)
        encoded = base64.urlsafe_b64encode(gzip.compress(payload.encode(), mtime=0))
        print(f"CHECK_OK=addons_bytes={len(encoded)} scopes={len(WANT_SCOPES)}")
        return 0

    name = argv[1] if len(argv) > 1 else "Her remote"
    try:
        created = register(name)
    except RuntimeError as exc:
        print(f"FATAL={exc}")
        return 1

    print(f"APP_ID={created['app_id']}")
    print(f"APP_SECRET={created['app_secret']}")
    if created["open_id"]:
        print(f"OWNER_OPEN_ID={created['open_id']}")
    else:
        print("NOTE=没拿到扫码人 open_id，将退化为首条消息绑定")

    report = verify_app_config(created["app_id"], created["app_secret"])
    if report.get("bot_name"):
        print(f"BOT_NAME={report['bot_name']}")
    if not report["token_ok"]:
        print(f"NOTE=凭据回读失败：{report.get('note', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
