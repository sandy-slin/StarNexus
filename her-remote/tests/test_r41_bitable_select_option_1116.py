"""第 41 轮（1116 系列）：多维表格里缺一个单选项 ⇒ **全表 32 台一起停更 9 天**。

同事/用户之前看到什么：飞书多维表格《CarHer 电脑助手 · 全公司版本》里「最后在线」一列
全部停在 **2026-11-13**，版本列还写着 `20261110-03fe140f`——而同一时刻 `fleet-report` 文本版
是对的（最新 `20261121-edc925df`，mac 12 + win 5）。表看着「有数据」，所以没人发现它冻住了。

**假设**：`bitable_row()` 给「上次更新」写了 `反复崩溃`（`_U_LABEL["crashloop"]`，随 55034477
于 11-10 22:14 上线），而多维表格那一列是 **select**，选项只有 `ok` / `fail` / `installing` / `—`
⇒ 飞书对不存在的单选项**整批拒绝**（不是跳过那一格）⇒ 只要有任意一台在 `crashloop`，
这一轮 32 台**全部**写不进去。

**证伪条件**：若飞书只拒那一格 / 只拒那一行，则同一轮里别的机器应当照样更新
⇒ 表里的「最后在线」会继续走，不会停在 11-13。

**数据**（可复现）：
1. `lark-cli base +field-list --base-token … --table-id … --as user`：
   「上次更新」`type=select`，`options = ['ok','fail','installing','—']`，**没有**「反复崩溃」。
2. `~/Library/Logs/carher-fleet-bitable.log` 全量相关性（565 轮）：
   报表里出现 `| crashloop` 的 **349 轮全部** `✗ 多维表格没同步上`；
   没出现的 **214 轮全部** `多维表格已同步` —— 相关性 100%，首次失败 **11-13 20:29**。
3. `+record-list` 读回表内容：32 行，「最后在线」最大值 `2026-11-13`。
4. 阳性对照（这把尺子不是恒红）：同一轮 `fail` / `ok` / `—` 三种值都在选项里，
   11-13 之前 214 轮同一条代码路径全是 OK。

另修：报错原文被 `stderr[-300:]` **取尾部**，把 `code`/`message` 切掉只留 `hint` 末尾
`… fail\\n- installing\\n- —.` ⇒ 9 天没人看出是「缺选项」。

本文件全部是纯函数级测试 —— 不 ssh、不连飞书、不写多维表格。
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.normpath(os.path.join(
    _HERE, "..", "..", "..", "feishu-agent-oneclick", "fleet-report.py"))

# 线上那张表「上次更新」真有的选项（+field-list 实测，11-22 03:5x）
LIVE_OPTS = {"上次更新": {"ok", "fail", "installing", "—"},
             "是不是最新": {"✅ 最新", "⚠️ 落后", "不知道"},
             "系统": {"mac", "windows"}}


@pytest.fixture(scope="module")
def fr():
    spec = importlib.util.spec_from_file_location("fleet_report_r41", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(fr, *us):
    return [fr.bitable_row({"id": f"m{i}", "os": "windows", "v": "20261121-edc925df",
                            "latest": True, "u": u, "last_seen": "", "since": ""})
            for i, u in enumerate(us)]


def test_crashloop_row_would_be_rejected_by_live_table(fr):
    """修前红：`crashloop` 这一行写出来的值不在线上选项里（= 飞书整批拒绝的原因）。"""
    row = _rows(fr, "crashloop")[0]
    assert row["上次更新"] == "反复崩溃"
    assert row["上次更新"] not in LIVE_OPTS["上次更新"]


def test_coerce_keeps_everyone_else_syncable(fr):
    """修后绿：缺选项的那一格退到表里有的值，**别的机器一个字都不动**。"""
    rows, notes = fr.coerce_selects(_rows(fr, "crashloop", "ok", "fail", ""), LIVE_OPTS)
    assert [r["上次更新"] for r in rows] == ["fail", "ok", "fail", "—"]
    for r in rows:
        assert r["上次更新"] in LIVE_OPTS["上次更新"]
    assert len(notes) == 1 and "反复崩溃" in notes[0] and "加上" in notes[0]


def test_not_applied_also_covered(fr):
    """`没装上`（not_applied）同样不在线上选项里 —— 同一个病，一起兜住。"""
    rows, notes = fr.coerce_selects(_rows(fr, "not_applied"), LIVE_OPTS)
    assert rows[0]["上次更新"] in LIVE_OPTS["上次更新"]
    assert any("没装上" in n for n in notes)


def test_in_table_values_untouched_and_silent(fr):
    """阳性对照:选项都在表里 ⇒ 一个字不改、一句话不说(别拿兜底把正常轮次也吵起来)。"""
    rows, notes = fr.coerce_selects(_rows(fr, "ok", "fail", "installing", ""), LIVE_OPTS)
    assert [r["上次更新"] for r in rows] == ["ok", "fail", "installing", "—"]
    assert notes == []


def test_no_options_means_change_nothing(fr):
    """字段列没拿到(量不到)⇒ 什么都不改。宁可让飞书报错,也不要把真值悄悄换掉。"""
    rows, notes = fr.coerce_selects(_rows(fr, "crashloop"), {})
    assert rows[0]["上次更新"] == "反复崩溃"
    assert notes == []


def test_added_option_stops_the_fallback(fr):
    """用户在表里加上「反复崩溃」之后,兜底必须自动让路(不然真值永远写不进去)。"""
    opts = {"上次更新": LIVE_OPTS["上次更新"] | {"反复崩溃"}}
    rows, notes = fr.coerce_selects(_rows(fr, "crashloop"), opts)
    assert rows[0]["上次更新"] == "反复崩溃"
    assert notes == []


def test_lark_error_keeps_the_code_not_just_the_hint_tail(fr):
    """修前红:报错取 stderr 尾部 300 字节 ⇒ `code`/`message` 被切掉,只剩 hint 末尾。"""
    stderr = ('{\n "ok": false,\n "error": {\n  "type": "api",\n  "code": 1254045,\n'
              '  "message": "FieldNameNotFound",\n  "hint": "选项必须是下面之一:\\n- ok\\n'
              '- fail\\n- installing\\n- —."\n }\n}')
    msg = fr._lark_error({}, stderr)
    assert "FieldNameNotFound" in msg and "1254045" in msg
    assert msg != stderr.strip()[-300:]


def test_lark_error_prefers_parsed_stdout_json(fr):
    """stdout 里本来就解出了 error ⇒ 直接用它,不去碰 stderr。"""
    data = {"ok": False, "error": {"code": 99, "message": "boom"}}
    msg = fr._lark_error(data, "irrelevant stderr")
    assert "boom" in msg and "irrelevant" not in msg


def test_lark_error_falls_back_to_head_of_plain_stderr(fr):
    """非 JSON 的 stderr ⇒ 留**头部**(原因在前面),别再取尾部。"""
    msg = fr._lark_error({}, "connection refused by dcdn\n" + "x" * 2000)
    assert msg.startswith("connection refused")
