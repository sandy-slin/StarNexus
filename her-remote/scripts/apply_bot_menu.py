#!/usr/bin/env python3
"""把常驻菜单配到飞书机器人上（输入框上面那一排）。

    python3 scripts/apply_bot_menu.py              # 只渲染本地菜单树（不发任何请求）
    python3 scripts/apply_bot_menu.py --apply      # 真的写上去

## 这个脚本改的是**你的真实飞书应用配置**

所以默认只读。`--apply` 才动，而且动之前把将要写的内容整个打出来。

## 需要的权限

`application:application:patch`。没开会得到：

    code 99991672 — Access denied. One of the following scopes is required:
    [application:application:patch]

开通路径（两分钟）：
  https://open.feishu.cn/app/<app_id>/auth → 搜 "application:application:patch"
  → 开通 → 回到「版本管理与发布」创建版本并发布（权限要发布后才生效）

不想开权限也行，手配一遍是等价的：
  开发配置 → 应用能力 → 机器人 → 机器人自定义菜单 → 按下面打印的那棵树填

## 客户端要求

悬浮样式的菜单需要飞书 **7.22+**。版本不够的客户端上菜单不显示 ——
这时候聊天里发「菜单」还是能拿到按钮卡片，两条路并存是故意的。

## 菜单点击怎么回到我们这里

`application.bot.menu_v6` 事件，走长连接（和消息同一条连接，不用公网 IP）。
🔴 这个事件**只带点击人的 open_id，不带 chat_id** —— 回消息必须用
`receive_id_type=open_id`，按 chat_id 发会得到 `230001 invalid receive_id`。
处理在 `bridge/app.py` 的 `on_bot_menu`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge import botmenu  # noqa: E402

BASE = "https://open.feishu.cn/open-apis"

# 菜单点击事件。不订阅它，菜单能显示但点了飞书不会推任何东西过来。
MENU_EVENT = "application.bot.menu_v6"


def _token(app_id: str, app_secret: str) -> str:
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    data = json.load(urllib.request.urlopen(req, timeout=20))
    if data.get("code"):
        raise SystemExit(f"拿 token 失败：{data}")
    return data["tenant_access_token"]


def _call(method: str, url: str, token: str, body=None) -> dict:
    payload = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as exc:
        try:
            return json.load(exc)
        except Exception:  # noqa: BLE001
            return {"code": exc.code, "msg": exc.reason}


def _load_config(path: str) -> tuple[str, str]:
    import tomllib

    with open(path, "rb") as handle:
        raw = tomllib.load(handle)
    feishu = raw.get("feishu", {})
    app_id = os.environ.get("FEISHU_BRIDGE_APP_ID") or feishu.get("app_id", "")
    secret = os.environ.get("FEISHU_BRIDGE_APP_SECRET") or feishu.get("app_secret", "")
    if not app_id or not secret:
        raise SystemExit(f"{path} 里没有 app_id / app_secret")
    return app_id, secret


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default=os.path.expanduser("~/.carher-feishu-agent/config.toml"),
    )
    parser.add_argument("--apply", action="store_true", help="真的写上去")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="写完顺手发布一个新版本（菜单要发布后才对用户生效）。隐含 --apply",
    )
    args = parser.parse_args()
    if args.publish:
        args.apply = True

    problems = botmenu.validate_tree()
    if problems:
        for line in problems:
            print(f"  ✗ {line}")
        raise SystemExit("菜单不满足飞书的限制，先改 bridge/botmenu.py")

    app_id, secret = _load_config(args.config)
    token = _token(app_id, secret)
    nodes = botmenu.build_bot_menus()

    print("要配的菜单（主菜单 / 子菜单 → 点了等于说什么）：")
    for name, key, children in botmenu.MENU_TREE:
        if not children:
            print(f"  {name:<12} → {botmenu.LINK_URLS[key] if botmenu.is_link(key) else botmenu.resolve_event_key(key)}")
            continue
        print(f"  {name}")
        for child_name, child_key in children:
            says = (botmenu.LINK_URLS[child_key] if botmenu.is_link(child_key)
                    else botmenu.resolve_event_key(child_key))
            print(f"      {child_name:<14} → {says}")
    print(f"\n共 {len(nodes)} 个节点（飞书实测上限 {botmenu.MAX_NODES}，父子加起来算）。")

    url = f"{BASE}/application/v7/applications/{app_id}/ability"
    if not args.apply:
        print("\n这是预演。真的写上去加 --apply。")
        # 🔴 预览**不发任何请求**。原来这里用空 body PATCH 探权限 ——
        # 那是对线上应用配置的写调用，和"只读预览"的承诺矛盾（2026-11-04 review）。
        return 0

    body = {
        "bot": {
            # 🔴 `enable` 必填 —— 只给 bot_menu_enable 会被拒：
            #    `field: bot.enable  "bot.enable is required"`
            "enable": True,
            "bot_menu_enable": True,
            # 🔴 **这个字段决定菜单显示成哪种样式**。
            #    3 = 悬浮（常驻输入框上方）；1/2 = 可切换（占输入框位置、点按钮切）。
            #    实测依据见 botmenu.DISPLAY_FLOAT 的注释。
            "bot_menu_display_strategy": botmenu.DISPLAY_FLOAT,
            "bot_menus": nodes,
        }
    }
    out = _call("PATCH", url, token, body)
    if out.get("code"):
        print(f"\n✗ 失败 code={out.get('code')} msg={out.get('msg')}")
        err = out.get("error") or {}
        if err.get("permission_violations"):
            print(f"  缺权限：{err['permission_violations']}")
        return 1
    print("\n✓ 菜单已写入（接口 code=0）。")

    # 🔴 **配了菜单 ≠ 点了有反应。** 还要订阅「机器人自定义菜单事件」，
    # 飞书才会把点击推过来。2026-11-04 实测：菜单显示正常、点了没反应，
    # 日志里 0 次点击 —— 不是我们没处理，是飞书根本没推（没订阅）。
    # 代码里 register_p2_application_bot_menu_v6 只是"推过来我能接"，
    # 订阅是应用配置那一侧的事，两边都要有。
    # 走长连接（websocket），和收消息同一条连接，不需要公网地址。
    sub = _call(
        "PATCH",
        f"{BASE}/application/v7/applications/{app_id}/config",
        token,
        {"event": {"subscription_type": "websocket", "add_events": [MENU_EVENT]}},
    )
    if sub.get("code"):
        print(f"\n✗ 订阅点击事件失败 code={sub.get('code')} msg={sub.get('msg')}")
        print("  菜单能看到但点了不会有反应。手动：事件与回调 → 添加事件 → 机器人自定义菜单事件")
        return 1
    print(f"✓ 已订阅点击事件 {MENU_EVENT}（长连接）。")
    if not args.publish:
        print("   菜单要**发布一个新版本**才对用户生效：")
        print(f"   https://open.feishu.cn/app/{app_id}/publish")
        print("   或者重跑本脚本加 --publish。")
        return 0
    return _publish(app_id, token)


def _publish(app_id: str, token: str) -> int:
    """发布一个新版本。

    `POST /application/v7/applications/:app_id/publish`，三个字段必填
    （2026-11-04 逐个探出来的，SDK 模型里都是 Optional，看不出来）：
    `version` / `changelog` / `remark`，都是字符串。

    🔴 版本号必须比现有的大，否则会被拒。用时间戳当版本号省掉"先查现有版本"
    这一步 —— 查版本要另一个权限，没开。

    ⚠️ 自建应用发布可能要**管理员审核**。接口返回成功只代表"提交了"，
    审核没过之前用户仍然看不到。脚本会如实打印返回内容，不替它下结论。
    """
    import time

    version = time.strftime("1.%Y%m%d.%H%M%S")
    body = {
        "version": version,
        "changelog": "更新机器人常驻菜单（项目 / 权限 / 模型 / 常用 / 停下）",
        "remark": "由 apply_bot_menu.py 自动发布",
        # 🔴 这两个也是必填，但缺了只回 `210304 Missing required fields`，不说是哪个。
        #    SDK 模型里是 Optional，看不出来。实测可选值 [gadget, web_app, bot]；
        #    我们是纯机器人应用，填 bot。
        "mobile_default_ability": "bot",
        "pc_default_ability": "bot",
    }
    url = f"{BASE}/application/v7/applications/{app_id}/publish"
    out = _call("POST", url, token, body)
    if out.get("code"):
        print(f"\n✗ 发布失败 code={out.get('code')} msg={out.get('msg')}")
        err = out.get("error") or {}
        for v in err.get("permission_violations") or []:
            print(f"  缺权限：{v.get('subject')}")
        for v in err.get("field_violations") or []:
            print(f"  字段：{v.get('field')} — {v.get('description')}")
        print(f"\n  手动发布：https://open.feishu.cn/app/{app_id}/publish")
        return 1
    data = out.get("data") or {}
    print(f"\n✓ 发布请求已提交：版本 {data.get('version') or version}")
    if data:
        print(f"   返回：{json.dumps(data, ensure_ascii=False)}")
    print("   ⚠️ 如果企业开了应用审核，要管理员批过才生效。")
    print("   生效后在飞书里退出这个机器人的会话再进来。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
