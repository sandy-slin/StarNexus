"""第 40 轮（1116 系列）：`u=crashloop` 的机器在 fleet-report 里**一个桶都进不了**。

同事/巡检之前看到什么：11-21 每轮巡检只按 `u=fail` 数机器，报「自动更新失败仍 3 台」。
而 …<机器>（windows，`cli_aXXXXXXXXXXXXXX`）17:40:26 报了一条 `u=crashloop`
（`20261121-edc925df`），17:41~20:41 的四轮巡检**没有任何一轮记到它**。

**假设**：`build_report()` 的桶只认 `fail` / `not_applied` / `u == ""` / stale / gone，
`crashloop` 不在任何一个桶的条件里 ⇒ 一台正在反复崩溃、但装着最新版、还在按点轮询的机器，
报表上只出现在明细表格那一行，所有「要盯的桶」都是 0。

**证伪条件**：把一条 `u=crashloop` 的在线记录喂进 `build_report()`，若 `failed_*` /
`not_applied` / `silently_behind` / `gone` / `stale` 里至少一个包含它，则假设不成立。

**数据**（修前跑 `.pre` 原始文件）：`failed_windows ['bbbb…890c8']`（阳性对照 `fail` 进了桶），
`failed_mac / not_applied / silently_behind / gone / stale` **全空** ——
被测的 `crashloop` 一个桶都没进。按「最后出现多久」再扫：5 分钟前 ⇒ 无桶；
70 分钟前 ⇒ 只进 `gone`；25 小时前 ⇒ 只进 `stale`。

本文件全部是纯函数级测试 —— 不 ssh、不连飞书。
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.normpath(os.path.join(
    _HERE, "..", "..", "..", "feishu-agent-oneclick", "fleet-report.py"))


@pytest.fixture(scope="module")
def fr():
    spec = importlib.util.spec_from_file_location("fleet_report_r40", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 21, 20, 45, 0, tzinfo=CST)
# 真实那台（…<机器>）的形状：windows、装着正在发的版本、分钟级轮询
ID_CRASH = "5e07cf2b79755a0f"
ID_FAIL = "bbbb0000000890c8"
APP_CRASH = "cli_aXXXXXXXXXXXXXX"
APP_FAIL = "cli_a1b2c3d4e5f60718"
V_NEW = "20261121-edc925df"
V_OLD = "20261110-03fe140f"
_BUCKETS = ("failed_mac", "failed_windows", "not_applied", "silently_behind", "gone", "stale")


def line(ts: datetime, *, v=V_NEW, id=ID_CRASH, os_="windows", u="crashloop", app=APP_CRASH) -> str:
    """一行真形状的 nginx combined 日志（和 198 上 ccdl 的那条一字不差地同形）。"""
    stamp = ts.strftime("%d/%b/%Y:%H:%M:%S %z")
    uri = f"/ccdl/feishu-agent/latest.json?v={v}&id={id}&os={os_}&u={u}&app={app}"
    return (f'10.0.0.1 - - [{stamp}] "GET {uri} HTTP/1.1" 200 698 "-" '
            '"Mozilla/5.0 (Windows NT; Windows NT 10.0; zh-CN) WindowsPowerShell/5.1.26100.9818"')


def test_crashloop_online_machine_lands_in_the_crashloop_bucket(fr):
    """修前红（`KeyError: 'crashloop'`）：在线 + 最新版 + 反复崩溃 ⇒ 要有一个桶收它。"""
    recs = fr.parse_lines([line(NOW - timedelta(minutes=5))])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["crashloop"] == [ID_CRASH]


def test_crashloop_used_to_fall_in_no_bucket_at_all(fr):
    """钉死这个缺口本身：它以前**一个**现有桶都进不了 ⇒ 现在必须恰好进新桶、且只进新桶。"""
    recs = fr.parse_lines([line(NOW - timedelta(minutes=5))])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert [k for k in _BUCKETS if rep[k]] == [], "在线的 crashloop 不该被老桶收走（口径变了）"
    assert rep["crashloop"] == [ID_CRASH], "新桶必须收它，否则又回到「所有要盯的桶都是 0」"
    # 明细行里它仍是那台（没被丢掉），而且 u 原样留着
    assert [(m["id"], m["u"]) for m in rep["machines"]] == [(ID_CRASH, "crashloop")]


def test_render_text_has_the_crashloop_line(fr):
    """渲染里要有这一行，而且带上是谁 —— 光一个数字没法去找人。"""
    recs = fr.parse_lines([line(NOW - timedelta(minutes=5))])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    text = fr.render_text(rep, {APP_CRASH: "张三"})
    assert "反复崩溃" in text, text
    hit = [ln for ln in text.splitlines() if ln.startswith("反复崩溃")]
    assert len(hit) == 1 and "：1 台" in hit[0] and "…<机器>" in hit[0] and "张三" in hit[0], hit
    # 一台都不崩的时候只出现数字，不许挂空的「—— 」尾巴
    clean = fr.build_report(
        fr.parse_lines([line(NOW - timedelta(minutes=5), u="ok")]), V_NEW, {}, NOW)
    clean_line = [ln for ln in fr.render_text(clean, {}).splitlines() if ln.startswith("反复崩溃")]
    assert clean_line == ["反复崩溃（桥起来就崩，版本看上面那张表）：0 台"], clean_line


def test_render_wording_does_not_outrun_the_criterion(fr):
    """🔴 桶的条件里没有看 `latest` ⇒ 文案不许说「装的版本是对的」。

    数据：11-17 08:37 …6d6e8d 那条 `u=crashloop` 带的是 `20261110-03fe140f`，
    而那一小时车队 189 条上报里 144 条是 `20261116-f06ffd64` ⇒ 崩的机器也可能是落后版本。
    """
    recs = fr.parse_lines([line(NOW - timedelta(minutes=5), v=V_OLD)])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["crashloop"] == [ID_CRASH], "落后版本的 crashloop 同样要进桶（条件只看 u）"
    text = fr.render_text(rep, {})
    assert "装的版本是对的" not in text and "正在崩" not in text


def test_fail_still_only_lands_in_failed_windows(fr):
    """阳性对照：`u=fail` 的行为一个字都不许变（没被我们改坏）。"""
    recs = fr.parse_lines([
        line(NOW - timedelta(minutes=5)),
        line(NOW - timedelta(minutes=5), id=ID_FAIL, u="fail", app=APP_FAIL),
    ])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["failed_windows"] == [ID_FAIL]
    assert rep["crashloop"] == [ID_CRASH], "fail 不许混进新桶"
    assert rep["failed_mac"] == [] and rep["not_applied"] == []
    assert rep["silently_behind"] == [] and rep["gone"] == [] and rep["stale"] == []


def test_crashloop_bucket_follows_fail_on_gone_and_stale(fr):
    """边界：口径照 `fail` 办 —— `fail` 现在允许同时进 `gone`/`stale`，新桶也一样。

    不顺手改 `fail` 的行为（那就是扩范围）：这里把两者放同一份报告里对比，
    断言「新桶 ∩ gone/stale 的形状和 fail 完全一致」。
    """
    for delta, other in ((timedelta(minutes=70), "gone"), (timedelta(hours=25), "stale")):
        recs = fr.parse_lines([
            line(NOW - delta),
            line(NOW - delta, id=ID_FAIL, u="fail", app=APP_FAIL),
        ])
        rep = fr.build_report(recs, V_NEW, {}, NOW)
        assert rep["crashloop"] == [ID_CRASH], f"{other}：crashloop 还得在自己的桶里"
        assert rep["failed_windows"] == [ID_FAIL], f"{other}：fail 还得在自己的桶里"
        # 两台都同时出现在 gone/stale 里 ⇒ 口径一致（fail 的既有行为就是允许重复计）
        assert sorted(rep[other]) == sorted([ID_CRASH, ID_FAIL]), (other, rep[other])


def test_crashloop_is_overwritten_by_a_later_ok(fr):
    """阴性对照：桥恢复后下一条 `u=ok` 要盖掉它（`latest_per_id` 只留最晚那条）。

    真数据：…<机器> 17:40:26 报 crashloop，17:43:20 起每 10 分钟 `u=ok` 到 20:53。
    这桶是「最后已知状态还在崩」，不是「今天崩过」—— 否则恢复了还天天报警。
    """
    recs = fr.parse_lines([
        line(NOW - timedelta(hours=3, minutes=5)),                      # crashloop
        line(NOW - timedelta(hours=3), u="ok"),                          # 三分钟后恢复
        line(NOW - timedelta(minutes=2), u="ok"),
    ])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["crashloop"] == []
    assert [k for k in _BUCKETS if rep[k]] == []


def test_watch_script_lists_the_new_bucket():
    """🔴 报表加了桶但巡检不读 ⇒ 对巡检等于没加（`fleet-watch.sh` 只遍历它自己那张 BUCKETS 表）。

    `fleet-watch.sh` 的 `BUCKETS` 是**硬编码的六项**（failed_windows / failed_mac / gone /
    not_applied / silently_behind / stale），不是遍历报告里所有的键 ⇒ 不同步加 `crashloop`，
    `rep["crashloop"]` 既不会被打印、也不计入「要管」（退出码 2 / alerts.log），
    而这条腿是每小时真在跑的 LaunchAgent（`com.carher.fleet-watch`，`StartInterval=3600`）。

    11-21 20:4x 巡检成对实测（把这段内嵌 python 抽出来喂合成报告）：
      带 crashloop 桶 ⇒ 改前 **rc=0「✓ 全部正常」**、改后 **rc=2** 并打出那一行；
      同一台改成 `u=fail` ⇒ 两边都 rc=2（阳性对照）；全 `u=ok` ⇒ 两边都 rc=0（阴性对照）。
    """
    watch = pathlib.Path(_SCRIPT).with_name("fleet-watch.sh")
    src = watch.read_text(encoding="utf-8")
    assert '"crashloop"' in src, "fleet-watch.sh 的 BUCKETS 里还没有 crashloop（见本测试 docstring）"
