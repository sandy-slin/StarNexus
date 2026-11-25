"""安装器最后那句「装好了」必须以**真连上**为判据。

实测（2026-11-03，隔离 HOME + 假 app_id）：安装器打印绿色「装好了」，而日志里是
`connect failed, err: 1000040346: app_id is invalid`，长连接线程已经抛死。

根因：`桥已启动` 是主线程在 `ws_client.start()` **之后立刻**打的，不等握手结果 ——
它只证明「进程起来了」，不证明「连上了」。用它当判据，凭据错/没发布/被代理挡住
这几种最常见的失败全都会被报成成功，用户带着一个永远收不到消息的机器人走。

SDK 真连上时会打 `connected to <url>`（lark_oapi/ws/client.py），失败打
`connect failed`。判据要用这两条。
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(
    os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick")
)
INSTALLER = os.path.join(_ONECLICK, "feishu-agent-mac.command")
SRC = os.path.join(_ONECLICK, "src", "install-mac.sh")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def test_verdict_does_not_rely_on_bridge_started_line():
    """`桥已启动` 在握手之前就打了，不能当「连上了」的证据。

    🔴 2026-11-05 判据改过一次。原来只抓 `grep -q "…" "$LOG_DIR/agent.log"` 这一种写法，
    而安装器已经改成从 `new_log`（只看本轮新日志）里 grep ⇒ 那个正则**一条都匹配不到**，
    assert 对着空列表恒绿。这就是「测试还在、但已经不看任何东西」的形状。
    改成：把所有 grep 的模式串都收上来，不管它从哪里取的输入。
    """
    # 🔴 第 15 轮：自检被整份嵌进了安装器，产物里 grep 的模式也包含自检的。
    # 产物在 build.sh 第一步（先跑测试）时还是**上一次**生成的 ⇒ 读产物会被旧内容卡住，
    # 新改的模板永远生成不出来（死锁）。判据看**模板**：安装器 + 自检两份模板都是
    # build.sh 的输入，产物只是它们的拼接。
    for path in (SRC, os.path.join(os.path.dirname(SRC), "doctor-mac.sh")):
        if not os.path.exists(path):
            continue
        text = _read(path)
        checks = re.findall(r'grep -q "([^"]+)"', text)
        assert checks, f"{os.path.basename(path)} 里一条 grep 判据都没有 —— 正则又过时了"
        assert "桥已启动" not in checks, (
            f"{os.path.basename(path)} 用「桥已启动」判连通性 —— "
            "它只证明进程起来了，凭据错也会打这一行"
        )


def test_verdict_looks_for_real_connect_markers():
    """必须正面找 SDK 的 `connected`，并且认出 `connect failed`。"""
    for path in (SRC, INSTALLER):
        if not os.path.exists(path):
            continue
        text = _read(path)
        assert "connected" in text, (
            f"{os.path.basename(path)} 没有检查 SDK 的 connected 标记"
        )
        assert "connect failed" in text, (
            f"{os.path.basename(path)} 没有检查 SDK 的 connect failed 标记"
        )


def _verdict_for(log_body: str) -> str:
    """把安装器末尾那段判定逻辑抽出来单独跑，喂一份假日志看它怎么判。"""
    text = _read(INSTALLER)
    match = re.search(
        r"(# 自检：长连接有没有真的连上.*?)\necho\n(say \"=+\"\n.*?\nsay \"=+\")",
        text,
        re.S,
    )
    assert match, "没找到安装器的连通性自检段落，测试要跟着改"
    snippet = match.group(1) + "\n" + match.group(2)

    # 🔴 2026-11-05：判定段落现在只看「这一轮」新写的日志（`new_log`），而 `new_log()`
    # 和 `LOG_MARK` 定义在段落**上面**，不在抠出来的这一段里。原来这里只抠判定段 ⇒
    # `new_log` 是个不存在的命令 ⇒ `|| true` 吞掉报错 ⇒ 什么都读不到 ⇒ 真连上也报「没连上」。
    # 这条红一直藏着：产物没重出时安装器还是老的整文件 grep，重出之后才第一次撞上。
    # 所以从产物里把**真实那一行** `new_log()` 抠出来带上（不抄一份，抄了会和安装器飘开），
    # `LOG_MARK=1` = 整份假日志都算「这一轮」写的。
    helper = re.search(r"^new_log\(\) \{.*\}$", text, re.M)
    assert helper, "安装器里找不到 new_log() 那一行 —— 测试要跟着改"
    snippet = "LOG_MARK=1\n" + helper.group(0) + "\n" + snippet

    with tempfile.TemporaryDirectory() as tmp:
        log_dir = os.path.join(tmp, "logs")
        os.makedirs(log_dir)
        with open(os.path.join(log_dir, "agent.log"), "w", encoding="utf-8") as handle:
            handle.write(log_body)
        preamble = (
            "set -uo pipefail\n"
            "BLUE=''; GREEN=''; YELLOW=''; RED=''; NC=''\n"
            "say(){ printf '%s\\n' \"$1\"; }\n"
            "ok(){ printf '%s\\n' \"$1\"; }\n"
            "warn(){ printf '%s\\n' \"$1\"; }\n"
            f'LOG_DIR="{log_dir}"\n'
            # 第 32 轮：判定段后面接了 UPDATE-VERDICT（连上删旧版 / 自动更新没连上就退回）。
            # 这里只测「怎么判连没连上」，退回那段由 tests/test_r32_update_safety.py 真跑 ⇒ 这里给个无害的桩
            f'PREV_BRIDGE="{tmp}/no-such-prev"\n'
            "rollback_to_prev(){ return 1; }\n"
        )
        proc = subprocess.run(
            ["/bin/bash", "-c", preamble + snippet],
            capture_output=True,
            text=True,
        )
        return proc.stdout


CONNECTED_LOG = (
    "2026-11-03 16:48:13,513 INFO feishu-bridge 桥已启动。模式=readonly\n"
    "[Lark] [2026-11-03 16:48:14,714] [INFO] connected to wss://open.feishu.cn/x\n"
)
BAD_CRED_LOG = (
    "2026-11-03 16:48:13,513 INFO feishu-bridge 桥已启动。模式=readonly\n"
    "[Lark] [2026-11-03 16:48:14,714] [ERROR] connect failed, err: "
    "1000040346: app_id is invalid\n"
)


def test_bad_app_id_is_not_reported_as_success():
    """回归：凭据错时日志里有 `桥已启动`，但绝不能报「装好了」。"""
    out = _verdict_for(BAD_CRED_LOG)
    assert "装好了" not in out, f"假绿：凭据无效却报装好了\n{out}"
    assert "app_id" in out or "凭据" in out or "没连上" in out, (
        f"失败了但没说清为什么\n{out}"
    )


def test_real_connection_is_reported_as_success():
    """阳性对照：真连上必须报成功，否则上一条可能只是永远报失败。"""
    out = _verdict_for(CONNECTED_LOG)
    assert "装好了" in out, f"真连上了却没报成功\n{out}"


# ---------- 第 14 轮：首连网络抖一下 ≠ 连不上（同事重装后最后一屏「还没能连上飞书」） ----------
# 本机日志实测：首连 `HTTPSConnectionPool … Max retries exceeded` / `[Errno 22]`，之后 SDK 自己重连上了。
# 原来见到任何 connect failed 就停、报「飞书拒绝了这台电脑」。

NET_BLIP_THEN_OK = (
    "2026-11-04 18:50:07,601 INFO feishu-bridge 桥已启动。模式=trusted\n"
    "[Lark] [2026-11-04 18:50:07,605] [ERROR] connect failed, err: HTTPSConnectionPool(host='open.feishu.cn', "
    "port=443): Max retries exceeded with url: /callback\n"
    "[Lark] [2026-11-04 18:50:20,963] [INFO] connected to wss://msg-frontier.feishu.cn/ws/v2\n"
)
NET_DOWN = (
    "2026-11-04 18:03:57,230 INFO feishu-bridge 桥已启动。模式=trusted\n"
    "[Lark] [2026-11-04 18:03:57,499] [ERROR] connect failed, err: [Errno 22] Invalid argument\n"
)


def _verdict_env(log_body: str, wait: int) -> str:
    os.environ["CONNECT_WAIT_SEC"] = str(wait)
    try:
        return _verdict_for(log_body)
    finally:
        os.environ.pop("CONNECT_WAIT_SEC", None)


def test_product_network_blip_then_connected_is_success():
    out = _verdict_env(NET_BLIP_THEN_OK, 3)
    assert "装好了" in out, out


def test_product_network_failure_is_not_reported_as_feishu_rejection():
    out = _verdict_env(NET_DOWN, 2)
    assert "装好了" not in out
    assert "飞书拒绝了" not in out, out
    assert "还在重试" in out and "VPN" in out, out


def test_product_credential_rejection_stops_immediately():
    import time as _t

    t0 = _t.monotonic()
    out = _verdict_env(BAD_CRED_LOG, 30)
    assert _t.monotonic() - t0 < 10, "凭据被拒还在干等"
    assert "飞书拒绝了" in out and "app_id" in out, out


def test_windows_waits_past_network_errors():
    text = _read(os.path.join(_ONECLICK, "src", "install-windows.ps1"))
    assert "$connectWait = 60" in text
    assert "if ($connectErr -match '^\\d+:') { break }" in text


def test_product_keeps_waiting_through_network_error_until_connected(monkeypatch):
    """真正的形状：先只有网络错，过几秒 SDK 重连上才写 connected。原来第一眼看到 connect failed 就停。"""
    import threading
    import time as _t

    real_open = open
    holder = {}

    def late_append():
        _t.sleep(2.5)
        with real_open(holder["log"], "a", encoding="utf-8") as fh:
            fh.write("[Lark] [2026-11-04 18:50:20,963] [INFO] connected to wss://msg-frontier.feishu.cn/ws/v2\n")

    orig_mkdirs = os.makedirs

    def spy_makedirs(path, *a, **kw):
        orig_mkdirs(path, *a, **kw)
        if path.endswith("logs") and "log" not in holder:
            holder["log"] = os.path.join(path, "agent.log")
            threading.Thread(target=late_append, daemon=True).start()

    monkeypatch.setattr(os, "makedirs", spy_makedirs)
    out = _verdict_env(NET_DOWN, 8)
    assert "装好了" in out, out
