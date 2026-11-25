"""车队巡检：自己发现「桥停了 / 装不上 / 永远不升」，不打扰用户。

用户 11-21 原话：「能不能把这个做到用户无感知，自动化，不要总是去打扰用户。太折腾了」。

今天一整天的来回，本质是我把诊断成本转给了同事：让他跑体检、复制命令、重装。
而这些机器的状态**服务端本来就看得见** —— 桥每 10 分钟自己来查一次更新，
它来不来、带什么版本、上次装成功没有，全在 198 的 ccdl 日志里。缺的只是「有人定时去看」。

新增 `gone` 桶（1 小时没来查更新）：`stale` 要满 24 小时，中间这段谁都看不见。
实测加它那一刻就有 5 台卡在闸门下面，含 a-user 那台（停 2 小时）
和 3 台停了 20 多小时却还没到 24 小时。

🔴 **不往用户那边发任何东西** —— 产品承诺是「Her remote 只控制你自己的电脑」
（11-10 为此否掉过「失联提醒」）。巡检只写本地日志给管理员看。
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
_WATCH = os.path.join(_ONECLICK, "fleet-watch.sh")
_REPORT = os.path.join(_ONECLICK, "fleet-report.py")


def test_watch_syntax():
    assert subprocess.run(["bash", "-n", _WATCH]).returncode == 0


def test_report_syntax():
    import ast
    ast.parse(pathlib.Path(_REPORT).read_text(encoding="utf-8"))


# ---------- gone 这个桶 ----------

def test_gone_threshold_is_six_polls():
    """桥每 10 分钟查一次 ⇒ 阈值要是它的若干倍，一两轮没来不算（网络抖 / 睡眠唤醒）。"""
    src = pathlib.Path(_REPORT).read_text(encoding="utf-8")
    assert "GONE_AFTER = timedelta(minutes=60)" in src


def test_gone_computed_where_last_seen_is_still_a_datetime():
    """🔴 `rows` 里的 last_seen 已经 isoformat 成字符串 ⇒ 到下面再算会 TypeError。

    第一版就这么炸的（`unsupported operand type(s) for -: 'datetime' and 'str'`）。
    """
    src = pathlib.Path(_REPORT).read_text(encoding="utf-8")
    i = src.index('"stale": now - m["last_seen"] > STALE_AFTER')
    seg = src[i:i + 500]
    assert '"gone": (now - m["last_seen"] > GONE_AFTER' in seg, "gone 没和 stale 在同一处算"
    # build_report 那边只许读算好的字段
    assert '"gone": [r["id"] for r in rows if r["gone"]]' in src


def test_gone_excludes_stale():
    """超过 24 小时的归 `stale`，别两边都报（同一台出现两次会让人以为是两台）。"""
    src = pathlib.Path(_REPORT).read_text(encoding="utf-8")
    i = src.index('"gone": (now - m["last_seen"] > GONE_AFTER')
    assert "<= STALE_AFTER" in src[i:i + 200]


def test_report_renders_the_gone_line():
    src = pathlib.Path(_REPORT).read_text(encoding="utf-8")
    assert "桥停了（1 小时内没来查更新" in src


# ---------- 巡检本身 ----------

def test_watch_never_messages_users():
    """🔴 产品承诺：只控制自己的电脑。巡检不许往用户那边发任何东西。"""
    src = pathlib.Path(_WATCH).read_text(encoding="utf-8")
    for banned in ("lark-cli", "im/v1/messages", "send_message", "replier", "chat_id"):
        assert banned not in src, f"巡检里出现了 {banned} ⇒ 可能在给用户发消息"


def test_watch_lists_every_bucket():
    """每个桶都要有「这是什么」和「该怎么办」——光给一串机器号没用。"""
    src = pathlib.Path(_WATCH).read_text(encoding="utf-8")
    for key in ("failed_windows", "failed_mac", "gone", "not_applied",
                "silently_behind", "stale"):
        assert f'"{key}"' in src, f"巡检漏了 {key} 这一桶"


def test_stale_does_not_raise_an_alert():
    """放假 / 换机的机器不该天天报警 —— `stale` 只列出来，不计入「要管」。"""
    src = pathlib.Path(_WATCH).read_text(encoding="utf-8")
    assert 'if key != "stale":' in src


def test_watch_runs_and_reports(tmp_path):
    """端到端：真跑一轮，拿真实车队数据。"""
    # 🔴 11-24：`CARHER_FLEET_SNAP_DIR` 必须一起隔离 —— 原来只隔离了 WATCH_DIR，
    #   而 `fleet-report.py` 的快照目录当时写死在真 HOME 下
    #   ⇒ **每跑一次这条测试就往真快照目录写一份**（实测 1005→1006）。
    #   写的是真数据所以不脏，但「测试有写盘副作用 + 快照带进来的机器不过期」叠起来是雷。
    env = dict(os.environ, CARHER_FLEET_WATCH_DIR=str(tmp_path),
               CARHER_FLEET_SNAP_DIR=str(tmp_path / "snap"))
    r = subprocess.run(["bash", _WATCH], capture_output=True, text=True,
                       env=env, timeout=300)
    # 🔴 11-23：「198 连不上」和「巡检脚本坏了」是两件事，原来都报成红 ⇒
    # 198 的 ssh 抖了十几分钟（实测 5 次只通 0~2 次），发版被这条卡住三回，而本次改动
    # 和巡检一个字都不相干。连不上就 skip 并照实说，**不当成通过**。
    # 判据只认 `fleet-report.py:726` 那一句「✗ 拿不到 198 的 nginx 日志：」——它是
    # 「取 198 日志」这一步唯一的失败出口，连接被 reset（rc=255）和连上了但命令超时
    # 都从这里出，两种现场都撞过。巡检自己的任何别的失败照样红。
    if r.returncode == 1 and "拿不到 198 的 nginx 日志：" in (r.stderr or ""):
        pytest.skip("198 这会儿取不到日志（ssh 连不上或超时，网络问题，不是巡检坏了）——这条端到端没量到")
    assert r.returncode in (0, 2), f"巡检自己挂了：\n{r.stdout}\n{r.stderr}"
    assert "正在发的是" in r.stdout, r.stdout
    if r.returncode == 2:
        assert (tmp_path / "alerts.log").exists(), "有要管的却没落盘"
    assert (tmp_path / "last-round.log").exists()


def test_quiet_mode_is_silent_when_all_good(tmp_path, monkeypatch):
    """cron 用 --quiet：全好的时候不输出（否则每小时一封无用邮件）。"""
    src = pathlib.Path(_WATCH).read_text(encoding="utf-8")
    assert "if not quiet:" in src and "✓ 全部正常" in src


def test_installed_as_a_timer():
    """装成定时了吗 —— 脚本写好没装定时，等于没做（11-21 之前那条出口巡检就这样）。"""
    plist = pathlib.Path.home() / "Library/LaunchAgents/com.carher.fleet-watch.plist"
    if not plist.exists():
        pytest.skip("这台没装（别的机器上跑测试）")
    text = plist.read_text(encoding="utf-8")
    assert "fleet-watch.sh" in text
    assert "--quiet" in text
    assert "<integer>3600</integer>" in text, "间隔不是一小时"
