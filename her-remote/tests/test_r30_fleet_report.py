"""第 30 轮：feishu-agent-oneclick/fleet-report.py（全公司版本表）。

全部是纯函数级测试 —— 不 ssh、不连飞书（lookup 用桩 http）。
"""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick", "fleet-report.py"))


@pytest.fixture(scope="module")
def fr():
    spec = importlib.util.spec_from_file_location("fleet_report", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=CST)
ID_A = "0123456789abcdef"
ID_B = "fedcba9876543210"
APP_A = "cli_aXXXXXXXXXXXXXX"
APP_B = "cli_a1b2c3d4e5f60718"
V_OLD = "20261107-877f027c"
V_NEW = "20261108-c8260b5e"


def line(ts: datetime, query: str = "", path: str = "/ccdl/feishu-agent/latest.json") -> str:
    uri = path + (("?" + query) if query else "")
    stamp = ts.strftime("%d/%b/%Y:%H:%M:%S %z")
    return f'10.0.0.1 - - [{stamp}] "GET {uri} HTTP/1.1" 200 368 "-" "Python-urllib/3.14"'


def q(v=V_NEW, id=ID_A, os="mac", u="ok", app=APP_A) -> str:
    return f"v={v}&id={id}&os={os}&u={u}&app={app}"


# ── 解析 ──────────────────────────────────────────────────────────────
def test_parse_report_line(fr):
    rec = fr.parse_line(line(NOW, q()))
    assert rec["kind"] == "report"
    assert (rec["v"], rec["id"], rec["os"], rec["u"], rec["app"]) == (V_NEW, ID_A, "mac", "ok", APP_A)
    assert rec["ts"] == NOW


def test_parse_dirty_version_and_empty_u(fr):
    rec = fr.parse_line(line(NOW, q(v="20261108-c23f0497-dirty-d7b6dc81", u="")))
    assert rec["kind"] == "report" and rec["u"] == "" and rec["v"].endswith("-dirty-d7b6dc81")


def test_parse_legacy_line_without_params(fr):
    assert fr.parse_line(line(NOW))["kind"] == "legacy"


def test_parse_other_paths_ignored(fr):
    assert fr.parse_line(line(NOW, path="/ccdl/feishu-agent/feishu-agent-mac.command")) is None
    assert fr.parse_line(line(NOW, path="/ccdl/other/latest.json")) is None


@pytest.mark.parametrize("junk", [
    "",
    "garbage",
    '10.0.0.1 - - [not a date] "GET /ccdl/feishu-agent/latest.json?v=x HTTP/1.1" 200 1 "-" "x"',
    "\x00\xff" * 50,
    '10.0.0.1 - - [09/Nov/2026:00:13:51 +0800] "GET /ccdl/feishu-agent/latest.json?%%%zz HTTP/1.1" 200 1',
])
def test_bad_lines_do_not_crash(fr, junk):
    fr.parse_line(junk)  # 不抛就行


def test_report_without_id_counted_separately(fr):
    recs = fr.parse_lines([line(NOW - timedelta(minutes=5), f"v={V_NEW}&os=mac&u=&app=")])
    assert recs[0]["kind"] == "noid"
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["machines"] == [] and rep["noid_reports_1h"] == 1 and rep["bad_reports"] == 0


def test_parse_matches_client_wire_format(fr):
    """CLIENT 那边（bridge/selfupdate.py `_query`）真实拼出来的串，这里要能认。"""
    import urllib.parse
    pairs = [("v", V_NEW), ("id", ID_A), ("os", "windows"), ("u", ""), ("app", APP_A)]
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    rec = fr.parse_line(line(NOW, query))
    assert rec["kind"] == "report" and rec["os"] == "windows" and rec["u"] == ""


def test_one_bad_line_does_not_break_report(fr):
    lines = [line(NOW - timedelta(minutes=5), q()), "完全坏掉的一行 [[[\"", "\udcff" * 3,
             line(NOW - timedelta(minutes=1), q(id=ID_B, os="windows", app=APP_B))]
    rep = fr.build_report(fr.parse_lines(lines), V_NEW, {}, NOW)
    assert {m["id"] for m in rep["machines"]} == {ID_A, ID_B}
    fr.render_text(rep, {})


@pytest.mark.parametrize("query", [
    q(id="ABCDEF0123456789"),            # 大写 / 不是本机生成的形状
    q(id="john-macbook-pro"),            # 主机名
    q(os="linux"),
    q(u="fail:permission%20denied%20/Users/x"),  # 带 detail（客户端会 percent-encode）
    q(app="cli_x-y"),
    q(app="AbCdEfGhIjKlMnOp1234"),       # 没有 cli_ 前缀（像 secret）
    q() + "&app_secret=s3cr3tS3cr3tS3cr3tS3cr3t",  # 多出来的键
    q() + "&id=" + ID_B,                 # 重复键
    # 🔴 11-24：`"v=&id=…&os=mac"`（缺版本号）**已从这张「该被拒」名单里搬走** ——
    #   它现在是**故意接受**的，见下面 test_empty_version_is_accepted_now。
    #   原因：`…2440a8` 13:09:28 那条 `u=crashloop` 的 `v=` 就是空的（崩在换版那一刻），
    #   旧判据把它整条判 `bad`、`id`/`u` 置空 ⇒ **任何桶都看不见它**，谁都没发现。
    #   ⛔ 别把它删掉了当完事：它是唯一守着「放宽 `v` 不许顺带把 `os` 也放开」的用例，
    #     所以搬到下面继续守着。
])
def test_malformed_report_rejected(fr, query):
    assert fr.parse_line(line(NOW, query))["kind"] == "bad"


def test_empty_version_is_accepted_now(fr):
    """空 `v` 要**收下**（崩在换版瞬间的上报），但闸不许因此开大。"""
    rec = fr.parse_line(line(NOW, "v=&id=" + ID_A + "&os=mac"))
    assert rec["kind"] == "report", "空版本号应被接受，否则崩在换版那刻的上报会整条消失"
    assert rec["v"] == "" and rec["id"] == ID_A and rec["os"] == "mac"

    # 🔴 反向：放宽只针对「空」，别的畸形版本号仍须判 bad；`os` 仍是必填。
    for bad_v in ("garbage", "<script>", "a" * 80, "2026112-abc", "20261124-ZZZ"):
        assert fr.parse_line(line(NOW, f"v={bad_v}&id={ID_A}&os=mac"))["kind"] == "bad", bad_v
    assert fr.parse_line(line(NOW, "v=&id=" + ID_A + "&os="))["kind"] == "bad", "os 不许跟着放开"


def test_empty_version_never_renders_as_blank(fr):
    """🔴 空版本号在**每条消费腿**上都要说「没报上来」，不许留白。

    11-24 现场：空 `v` 一路裸着走 ⇒ 正文那列是**空白**、`versions` 多一个**空字符串键**
    （打出来是「：mac 0 台，windows 1 台」前面什么都没有，像渲染坏了）、多维表格「版本」
    列也是空 ⇒ 看的人会读成「没装版本」。它真正的意思是「崩在换版那一刻、版本号没报上来」。
    """
    recs = fr.parse_lines([
        line(NOW - timedelta(minutes=3), q()),
        line(NOW - timedelta(minutes=2), "v=&id=" + ID_B + "&os=windows&u=crashloop&app=" + APP_B),
    ])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    rows = {r["id"]: r for r in rep["machines"]}

    assert rows[ID_B]["v"] == "", "报表内部仍保留原值（空），只在渲染时替换"
    assert rows[ID_B]["latest"] is None, "空版本号是「不知道」，不是「落后」"

    assert "" not in rep["versions"], "`versions` 不许有空字符串键"
    assert fr._V_UNKNOWN in rep["versions"], "空版本号要归到一个说得清的键名下"

    text = fr.render_text(rep, {})
    assert fr._V_UNKNOWN in text
    assert f"| windows |  |" not in text, "正文版本列不许留白"
    assert f"\n  ：mac" not in text, "`按版本` 不许出现裸的空键"

    assert fr.bitable_row(rows[ID_B])["版本"] == fr._V_UNKNOWN, "多维表格那条腿也要改"


def test_dropped_sample_says_which_field_is_missing(fr):
    """🔴 「**没带这个字段**」≠「字段带了但是空的」—— 样例不许把前者说成后者。

    11-24 现场：样例写成 `id=%s u=%s v=%r`，三个值都走 `… or ""` ⇒ 匹配不到回 None 被
    渲染成 `v=''`，报表上那句话变成「有 5 条上报 v= 是空的」。实测那 5 条**三个字段一个
    都没带**，是我们自己 `curl` 的探活请求（`?t=`、`?nocache=`）⇒ 样例要给**能认出是谁
    在发**的东西（源 IP / 查询串 / UA），而不是三遍「没这个字段」。
    """
    fr.DROPPED["n"] = 0
    fr.DROPPED["samples"] = []
    fr.parse_lines([line(NOW, "t=18352")])          # 探活：三个字段都没带
    assert int(fr.DROPPED["n"]) == 1
    s = fr.DROPPED["samples"][0]
    assert "10.0.0.1" in s and "?t=18352" in s, s
    assert "三个字段都没带" in s, s
    assert "v=''" not in s and "v=(空)" not in s, "没带字段不许说成「空」：" + s

    # 反向：字段**带了但是空的**那种要说「空」，不能说「没这个字段」。
    fr.DROPPED["n"] = 0
    fr.DROPPED["samples"] = []
    fr.parse_lines([line(NOW, "v=" + "Z" * 80 + "&id=" + ID_A + "&u=&os=mac")])
    s2 = fr.DROPPED["samples"][0]
    assert "带了 id/u/v" in s2, s2

    fr.DROPPED["n"] = 0
    fr.DROPPED["samples"] = []


# ── 同一个 id 取最后一条 ────────────────────────────────────────────────
def test_latest_per_id_takes_last_and_since(fr):
    recs = fr.parse_lines([
        line(NOW - timedelta(hours=3), q(v=V_OLD)),
        line(NOW - timedelta(hours=2), q(v=V_NEW, u="installing")),
        line(NOW - timedelta(minutes=10), q(v=V_NEW, u="ok")),
        line(NOW - timedelta(hours=1), q(v=V_NEW, u="ok")),  # 乱序也要按时间
    ])
    m = fr.latest_per_id(recs)[ID_A]
    assert m["v"] == V_NEW and m["u"] == "ok"
    assert m["last_seen"] == NOW - timedelta(minutes=10)
    assert m["since"] == NOW - timedelta(hours=2)


# ── 是不是最新 ─────────────────────────────────────────────────────────
def test_is_latest(fr):
    assert fr.is_latest(V_NEW, V_NEW) is True
    assert fr.is_latest(V_OLD, V_NEW) is False
    # BUILD_ID 不可比大小：比最新「更大」的号也不算最新
    assert fr.is_latest("20991231-ffffffff", V_NEW) is False
    assert fr.is_latest(V_NEW, None) is None


def test_report_summary(fr):
    recs = fr.parse_lines([
        line(NOW - timedelta(minutes=3), q()),
        line(NOW - timedelta(hours=30), q(id=ID_B, v=V_OLD, os="windows", u="fail", app=APP_B)),
        line(NOW - timedelta(minutes=20)),
        line(NOW - timedelta(minutes=50)),
        line(NOW - timedelta(hours=2)),  # 超出 1 小时，不计
    ])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    rows = {r["id"]: r for r in rep["machines"]}
    assert rows[ID_A]["latest"] is True and rows[ID_B]["latest"] is False
    assert rep["versions"] == {V_NEW: {"mac": 1, "windows": 0}, V_OLD: {"mac": 0, "windows": 1}}
    assert rep["stale"] == [ID_B]
    assert rep["failed_windows"] == [ID_B] and rep["failed_mac"] == []
    assert rep["legacy_requests_1h"] == 2
    text = fr.render_text(rep, {})
    assert "最近 1 小时 2 次请求" in text
    assert "…" + ID_A[-6:] in text and ID_A not in text  # 只显示末 6 位


# ── 名字映射 ──────────────────────────────────────────────────────────
def test_names_file_and_display(fr, tmp_path):
    p = tmp_path / "names.json"
    p.write_text(json.dumps({APP_A: "张三", "not-an-app": "x", APP_B: 3}), encoding="utf-8")
    names = fr.load_names(p)
    assert names == {APP_A: "张三"}
    assert fr.display_name(APP_A, names) == f"张三（{APP_A}）"
    assert fr.display_name(APP_B, names) == APP_B
    assert fr.load_names(tmp_path / "missing.json") == {}
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert fr.load_names(tmp_path / "broken.json") == {}


def test_manual_names_win_over_auto(fr):
    assert fr.merge_names({APP_A: "手填"}, {APP_A: "自动", APP_B: "李四的助手"}) == {
        APP_A: "手填", APP_B: "李四的助手"}


def _stub_http(app_resp):
    calls = []

    def http(url, *, body=None, token=None):
        calls.append(url)
        if url.endswith("/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "t-stub"}
        return app_resp(url)
    return http, calls


def test_lookup_success(fr):
    http, calls = _stub_http(lambda url: {"code": 0, "data": {"app": {"app_name": "张三的电脑助手"}}})
    found, problem = fr.lookup_app_names([APP_A], creds=("cli_x", "s"), http=http)
    assert found == {APP_A: "张三的电脑助手"} and problem == ""
    assert any("/application/v6/applications/" + APP_A in c for c in calls)


def test_lookup_missing_scope_says_which(fr):
    http, _ = _stub_http(lambda url: {"code": 99991672, "msg": "Access denied"})
    found, problem = fr.lookup_app_names([APP_A, APP_B], creds=("cli_x", "s"), http=http)
    assert found == {} and "admin:app.info:readonly" in problem


def test_lookup_without_creds(fr):
    found, problem = fr.lookup_app_names([APP_A], creds=None)
    assert found == {} and problem


def test_lookup_does_not_leak_secret(fr):
    secret = "S3cr3tValueThatMustNotShow1234"

    def http(url, *, body=None, token=None):
        return {"code": 10014, "msg": f"bad secret {secret}"}
    found, problem = fr.lookup_app_names([APP_A], creds=("cli_x", secret), http=http)
    assert secret not in problem


# ── 快照 diff ────────────────────────────────────────────────────────
def test_snapshot_roundtrip_and_diff(fr, tmp_path):
    r1 = fr.build_report(fr.parse_lines([line(NOW - timedelta(hours=5), q(v=V_OLD))]),
                         V_OLD, {}, NOW - timedelta(hours=4))
    r2 = fr.build_report(fr.parse_lines([
        line(NOW - timedelta(hours=1), q(v=V_NEW)),
        line(NOW - timedelta(minutes=5), q(id=ID_B, os="windows", app=APP_B, v=V_OLD)),
    ]), V_NEW, {}, NOW)
    fr.save_snapshot(fr.snapshot_of(r1), tmp_path)
    fr.save_snapshot(fr.snapshot_of(r2), tmp_path)
    snaps = fr.load_snapshots(tmp_path)
    assert len(snaps) == 2
    changes = fr.diff_snapshots(snaps[0], snaps[1])
    by = {c["id"]: c for c in changes}
    assert by[ID_A]["kind"] == "upgrade" and by[ID_A]["from"] == V_OLD and by[ID_A]["to"] == V_NEW
    assert by[ID_A]["at"].startswith((NOW - timedelta(hours=1)).isoformat()[:16])
    assert by[ID_B]["kind"] == "new"
    hist = fr.render_history(snaps, {APP_A: "张三"})
    assert f"{V_OLD} → {V_NEW}" in hist and "张三" in hist


def test_snapshot_carries_machines_past_log_rotation(fr):
    old = fr.build_report(fr.parse_lines([line(NOW - timedelta(days=20), q(v=V_OLD))]),
                          V_OLD, {}, NOW - timedelta(days=19))
    carried = fr.carried_from(fr.snapshot_of(old))
    rep = fr.build_report([], V_NEW, {}, NOW, carried=carried)
    row = rep["machines"][0]
    assert row["id"] == ID_A and row["from_snapshot"] and row["stale"]


def test_corrupt_snapshot_skipped(fr, tmp_path):
    (tmp_path / "20261101-000000.json").write_text("{not json", encoding="utf-8")
    assert fr.load_snapshots(tmp_path) == []


# ── 不回显凭据 ────────────────────────────────────────────────────────
def test_fake_secret_in_log_is_never_echoed(fr, tmp_path, capsys, monkeypatch):
    # 不读不写管理员真实的名字文件 / 快照目录
    monkeypatch.setattr(fr, "NAMES_FILE", tmp_path / "names.json")
    monkeypatch.setattr(fr, "SNAP_DIR", tmp_path / "snaps")
    secret = "AbCdEfGhIjKlMnOpQrStUvWxYz012345"
    log = tmp_path / "access.log"
    log.write_text("\n".join([
        line(NOW - timedelta(minutes=2), q() + f"&app_secret={secret}"),
        line(NOW - timedelta(minutes=2), q(u=f"fail:{secret}")),
        line(NOW - timedelta(minutes=2), q(app=secret)),
        line(NOW - timedelta(minutes=2), f"v={secret}&id={ID_B}&os=mac"),
        line(NOW - timedelta(minutes=1), q(id=ID_B)),
    ]) + "\n", encoding="utf-8")
    for extra in ([], ["--json"]):
        rc = fr.main(["--log-file", str(log), "--latest-build", V_NEW,
                      "--no-lookup", "--no-snapshot", *extra])
        out = capsys.readouterr()
        assert rc == 0
        assert secret not in out.out and secret not in out.err
    rep = json.loads(out.out)
    assert rep["bad_reports"] == 4 and [m["id"] for m in rep["machines"]] == [ID_B]


def test_bad_requests_counted_once_not_twice(fr):
    """🔴 同一批坏请求不许在正文里出现两次、还各讲一个故事。

    11-24 现场：`kind=='bad'` 的行**同时**进 `DROPPED`（`parse_lines` 里 `:164`）和
    `bad_reports`（`build_report` 里 `:437`）⇒ 正文一行写「有 5 条没通过字段校验」、
    4 行之后又写「另有 4 条带参数但格式不对」。两句都是真的，但并排读会被**相加成 9**，
    真实只有 5 条（实测真日志：bad 4 + parse 回 None 的探活 1 = 5，闭合）。
    """
    lines = [
        line(NOW - timedelta(minutes=5), q()),                      # 一台正常机器
        line(NOW, q(os="linux")),                                   # bad：os 不认
        line(NOW, q(id="john-macbook-pro")),                        # bad：id 形状不对
        # 探活：真日志里那条是 **HEAD**（`parse_line` 只认 GET ⇒ 回 None），
        # 换成 GET 会被判 `bad`，就不是这个场景了 —— 实测差别就在这个动词上。
        line(NOW, "t=18352").replace('"GET ', '"HEAD '),
    ]
    recs = fr.parse_lines(lines)
    n_bad = sum(1 for r in recs if r.get("kind") == "bad")
    assert n_bad == 2, n_bad
    assert int(fr.DROPPED["n"]) == 3, "2 条 bad + 1 条探活都该进 DROPPED"

    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["bad_reports"] == 2
    text = fr.render_text(rep, {})

    # ① 那个数只许出现在**一行**里。
    hits = [ln for ln in text.splitlines() if "格式不对" in ln or "没通过字段校验" in ln]
    assert len(hits) == 1, "坏请求被报了 %d 行（并排会被相加）：%r" % (len(hits), hits)

    # ② 这一行要把**从属关系**写出来，否则两个数并排仍然会被当成两批。
    assert "其中 2 条" in hits[0], hits[0]
    assert "3 条请求" in hits[0], hits[0]

    # ③ 反向：`render_text` 被**单独调用**时（`DROPPED` 没被这次解析填过）
    #    这个数不许凭空消失 —— 那是另一种假绿。
    fr.DROPPED["n"] = 0
    fr.DROPPED["samples"] = []
    text2 = fr.render_text(rep, {})
    hits2 = [ln for ln in text2.splitlines() if "格式不对" in ln]
    assert len(hits2) == 1, "单独渲染时坏请求数不许没人报：%r" % (hits2,)


# ---------- 同步到飞书多维表格 ----------

def _fr():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[3] / "feishu-agent-oneclick" / "fleet-report.py"
    spec = importlib.util.spec_from_file_location("fleet_report_bt", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bitable_upsert_by_machine_id():
    fr = _fr()
    machines = [
        {"id": "aaaa000000000001", "app": "cli_x", "os": "mac", "v": "B", "latest": True, "u": "ok",
         "last_seen": "2026-11-09T01:00:00+08:00", "since": "2026-11-08T01:00:00+08:00", "stale": False, "name": "张三"},
        {"id": "bbbb000000000002", "app": "", "os": "windows", "v": "A", "latest": False, "u": "fail",
         "last_seen": "2026-11-09T01:00:00+08:00", "since": "", "stale": True, "name": ""},
        {"id": "", "v": "X"},
    ]
    create, update = fr.plan_bitable(machines, {"aaaa000000000001": "rec1"})
    assert list(update) == ["rec1"] and update["rec1"]["是谁"] == "张三"
    assert len(create) == 1 and create[0]["机器号"] == "bbbb000000000002"
    assert create[0]["是不是最新"] == "⚠️ 落后" and create[0]["超过24小时没上线"] is True
    assert create[0]["首次看到"] is None and isinstance(update["rec1"]["最后在线"], int)


# ── 1121：在线但一次都没试过的落后机器（u 恒空）────────────────────────
def test_silently_behind_counts_online_machines_that_never_attempted(fr):
    """🔴 11-21 02:5x 实测缺口：…bfd0d6 停在 `20261110-03fe140f` 已 11 天，每 10 分钟准点轮询
    （11-21 当天 18 条、4 天 512 条**全是 `u=`**），但三个桶一个都不收它 ——
    `failed_windows` 只认 `u=fail`、`not_applied` 只认 `u=not_applied`、`stale` 要 24h 不出现
    （它分钟级在线）⇒ 报表上「更新失败 0 / 没装上 0」,人看不出有台机器十一天没跟上。

    它的旧桥（`03fe140f:selfupdate.py:438-441`）在 Windows 上返回 `manual_only=True`,
    `tick()` 只打一行本机日志、**既不下载也不记 state** ⇒ `u` 永远是空串,不是它坏了,
    而是这个口径从来不看「在线 + 落后 + 没有任何尝试记录」这一种。
    """
    recs = fr.parse_lines([
        # 在线（3 分钟前）但落后、u 恒空 ⇒ 本条要被收进 silently_behind
        line(NOW - timedelta(minutes=3), q(id=ID_B, v=V_OLD, os="windows", u="", app=APP_B)),
        # 阴性对照①：在线 + u 空 + 但已是最新 ⇒ 不该收（刚装好还没有尝试记录是正常的）
        line(NOW - timedelta(minutes=3), q(id=ID_A, v=V_NEW, os="mac", u="", app=APP_A)),
    ])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["silently_behind"] == [ID_B], "落后 + 无任何尝试记录的机器要单独列出来"
    text = fr.render_text(rep, {})
    assert "…" + ID_B[-6:] in text and "一次都没试过" in text
    # 🔴 措辞不许比判据更强：桶的条件是「24 小时内出现过」,不是「此刻在线」
    assert "在线但落后" not in text


def test_silently_behind_excludes_failed_not_applied_and_stale(fr):
    """阴性对照②③④：已经被别的桶收走的、以及 24h 不出现的,都不许重复进这一桶。"""
    far = "1111111111111111"
    recs = fr.parse_lines([
        line(NOW - timedelta(minutes=3), q(id=ID_A, v=V_OLD, os="windows", u="fail", app=APP_A)),
        line(NOW - timedelta(minutes=3), q(id=ID_B, v=V_OLD, os="windows", u="not_applied", app=APP_B)),
        line(NOW - timedelta(hours=30), q(id=far, v=V_OLD, os="windows", u="", app=APP_B)),
    ])
    rep = fr.build_report(recs, V_NEW, {}, NOW)
    assert rep["failed_windows"] == [ID_A] and rep["not_applied"] == [ID_B]
    assert rep["stale"] == [far]
    assert rep["silently_behind"] == [], "fail / not_applied / 超 24h 的三类不重复计入"


def test_silently_behind_unknown_latest_build_counts_nobody(fr):
    """阴性对照⑤：没读到正在发的版本（`latest=None`）⇒ 判不了落后,一台都不许进桶（禁猜）。"""
    recs = fr.parse_lines([line(NOW - timedelta(minutes=3), q(id=ID_B, v=V_OLD, os="windows", u="", app=APP_B))])
    rep = fr.build_report(recs, None, {}, NOW)
    assert rep["silently_behind"] == []


def test_bitable_row_only_carries_report_fields():
    """🔴 和上报同一个白名单：表里不出现任何上报之外的东西。"""
    fr = _fr()
    row = fr.bitable_row({"id": "a" * 16, "v": "B", "os": "mac", "u": "fail", "app": "cli_x", "latest": True,
                          "last_seen": "", "since": "", "stale": False, "detail": "/Users/zhangsan/secret",
                          "app_secret": "S3CRET"})
    assert "S3CRET" not in str(row) and "zhangsan" not in str(row)
    assert set(row) == {"机器号", "是谁", "机器人编号", "系统", "版本", "是不是最新", "上次更新",
                        "最后在线", "首次看到", "超过24小时没上线"}
