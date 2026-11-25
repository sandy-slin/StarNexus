"""装机时跑一次：验凭据 + 尽量自动认出"谁是主人"。

为什么要有这个文件：手工抄 open_id 是推广的最大摩擦点 —— 让几十个同事去
"先发条消息、再翻日志、复制一串 ou_xxx 粘回配置"，一定有人卡在这儿。

能自动取到就自动写白名单；取不到就退化成"第一条消息自动绑定"（见 app.py 的
autobind）。两条路都不需要用户手抄。

输出走 stdout 的 KEY=VALUE，给安装脚本 sed 解析：
    BOT_NAME=...            凭据有效，顺带拿到机器人名字
    OWNER_OPEN_ID=ou_...    认出创建者了
    FATAL=...               凭据无效之类的硬错，安装该停下
    NOTE=...                非致命说明
"""

from __future__ import annotations

import json
import ssl
import sys
import urllib.error
import urllib.request

BASE = "https://open.feishu.cn/open-apis"
TIMEOUT = 15


def _post(path: str, payload: dict) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def _get(path: str, token: str) -> dict:
    request = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("FATAL=用法: bootstrap.py <app_id> <app_secret>")
        return 2
    app_id, app_secret = argv[1], argv[2]

    # 1) 换 token —— 这一步同时验证凭据对不对
    try:
        data = _post(
            "/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
        )
    except urllib.error.URLError as exc:
        # 11-18 上报 183843-herremote-29a3f2:公司安全软件/代理做 HTTPS 检查 ⇒ 证书链里有自签证书,
        #   原来只甩一句 [SSL: CERTIFICATE_VERIFY_FAILED] 英文。⇒ 单独一句人话;仍以「连不上飞书」开头(安装器按它走网络分支)
        if isinstance(exc.reason, ssl.SSLCertVerificationError) \
                or "CERTIFICATE_VERIFY_FAILED" in str(exc.reason):
            print("FATAL=连不上飞书：证书对不上。这台电脑的网络在中间换了证书"
                  "（公司安全软件 / 代理在做 HTTPS 检查，或者开着抓包工具），Python 不认。"
                  "关掉 VPN / 代理 / 抓包工具再试；如果是公司网络强制的，找 IT 放行这条地址。")
            return 1
        print(f"FATAL=连不上飞书（{exc.reason}）。检查网络后重试。")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"FATAL=验证凭据时出错：{exc}")
        return 1

    code = data.get("code")
    if code != 0 or not data.get("tenant_access_token"):
        message = data.get("msg", "未知错误")
        # 把飞书的错误码翻译成人话 —— 这是同事最可能卡住的地方
        if code in {10003, 10013, 10014}:
            hint = "App ID 或 App Secret 填错了（注意别把两个贴反）"
        elif code == 10012:
            hint = "这个应用还没发布，或者审批没过"
        else:
            hint = f"飞书返回 code={code} msg={message}"
        print(f"FATAL={hint}")
        return 1

    token = data["tenant_access_token"]

    # 2) 机器人信息 —— 让用户看到名字，确认连的是自己那个应用而不是贴错了别人的
    try:
        info = _get("/bot/v3/info", token)
        if info.get("code") == 0:
            bot = info.get("bot") or {}
            name = bot.get("app_name") or ""
            if name:
                print(f"BOT_NAME={name}")
    except Exception:  # noqa: BLE001
        print("NOTE=拿不到机器人名字（不影响使用）")

    # 3) 创建者 open_id。
    #
    # 走的是应用自身信息接口，它在部分权限组合下会带 owner。拿不到是常态
    # （权限没审批、或接口不返回这个字段），所以这里失败不算错 —— 上层会退化
    # 成首条消息自动绑定。刻意不把这当硬失败，否则推广时一半人装不上。
    owner = ""
    try:
        app = _get(f"/application/v6/applications/{app_id}?lang=zh_cn", token)
        if app.get("code") == 0:
            payload = app.get("data", {}).get("app", {}) or {}
            owner = (
                payload.get("owner", {}).get("owner_id")
                or payload.get("owner_id")
                or ""
            )
    except Exception:  # noqa: BLE001
        pass

    if owner.startswith("ou_"):
        print(f"OWNER_OPEN_ID={owner}")
    else:
        print("NOTE=取不到创建者 open_id，改用首条消息绑定")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
