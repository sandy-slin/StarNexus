"""第 34 轮：feishu-agent-oneclick/fleet-offline-alert.py（Her remote 失联提醒）。

纯函数 + main() 桩 http —— 不 ssh、不连飞书、不发任何消息。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick",
                                        "fleet-offline-alert.py"))


@pytest.fixture(scope="module")
def oa():
    spec = importlib.util.spec_from_file_location("fleet_offline_alert", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["fleet_offline_alert"] = mod  # dataclass 要能在 sys.modules 里找到自己
    spec.loader.exec_module(mod)
    return mod


CST = timezone(timedelta(hours=8))
TUE = datetime(2026, 11, 10, tzinfo=CST)          # 星期二
SAT = datetime(2026, 11, 14, tzinfo=CST)          # 星期六
ID_A, ID_B, ID_C, ID_D = "0123456789abcdef", "fedcba9876543210", "00000000000000aa", "00000000000000bb"
APP_A, APP_B = "cli_aXXXXXXXXXXXXXX", "cli_a1b2c3d4e5f60718"
V = "20261108-c8260b5e"
OWNER = "on_0000000000000000000000000000aaaa"


def at(day: datetime, hh: int, mm: int = 0) -> datetime:
    return day.replace(hour=hh, minute=mm)


def line(ts: datetime, mid: str = ID_A, app: str = APP_A, os_: str = "mac") -> str:
    stamp = ts.strftime("%d/%b/%Y:%H:%M:%S %z")
    q = f"v={V}&id={mid}&os={os_}&u=ok&app={app}"
    return f'10.0.0.1 - - [{stamp}] "GET /ccdl/feishu-agent/latest.json?{q} HTTP/1.1" 200 368 "-" "x"'


def every10(start: datetime, end: datetime, **kw) -> list[str]:
    out, t = [], start
    while t <= end:
        out.append(line(t, **kw))
        t += timedelta(minutes=10)
    return out


def recs(oa, lines):
    return oa.fr.parse_lines(lines)


def alive_peer(now: datetime) -> list[str]:
    """一台一直在线的别人的机器：让「日志停了」刹车不误触发。"""
    return every10(now - timedelta(hours=3), now, mid=ID_D, app="cli_peer0000000000")


def one(oa, lines, now, state=None, cfg=None):
    vs, brake = oa.decide(recs(oa, lines), state or {}, now, cfg or oa.Config())
    return {v.id: v for v in vs}, brake


# ── 判定 ─────────────────────────────────────────────────────────────
def test_online_machine_is_not_alerted(oa):
    now = at(TUE, 15)
    vs, brake = one(oa, every10(at(TUE, 9), now - timedelta(minutes=5)) + alive_peer(now), now)
    assert vs[ID_A].action == "online" and brake == ""


def test_workday_afternoon_offline_over_hour_alerts(oa):
    now = at(TUE, 16, 5)
    vs, brake = one(oa, every10(at(TUE, 9), at(TUE, 15)) + alive_peer(now), now)
    assert vs[ID_A].action == "alert" and brake == ""


def test_offline_under_hour_is_not_yet(oa):
    now = at(TUE, 15, 50)
    vs, _ = one(oa, every10(at(TUE, 9), at(TUE, 15)) + alive_peer(now), now)
    assert vs[ID_A].action == "online"


def test_lunch_gap_waits_two_hours(oa):
    lines = every10(at(TUE, 9), at(TUE, 12)) + alive_peer(at(TUE, 14, 30))
    vs, _ = one(oa, lines, at(TUE, 13, 30))
    assert vs[ID_A].action == "skip" and "午饭" in vs[ID_A].reason
    vs, _ = one(oa, lines, at(TUE, 14, 5))
    assert vs[ID_A].action == "alert"


def test_evening_shutdown_is_quiet(oa):
    now = at(TUE, 18, 30)  # 17:20 走人，18:30 已超 1 小时 —— 下班时段开始的不算
    vs, _ = one(oa, every10(at(TUE, 9), at(TUE, 17, 20)) + alive_peer(now), now)
    assert vs[ID_A].action == "skip" and "下班" in vs[ID_A].reason


def test_no_nag_after_send_window_or_next_morning(oa):
    lines = every10(at(TUE, 9), at(TUE, 17, 30))
    vs, _ = one(oa, lines + alive_peer(at(TUE, 19, 10)), at(TUE, 19, 10))
    assert vs[ID_A].action == "skip"
    nxt = at(TUE + timedelta(days=1), 9, 30)
    vs, _ = one(oa, lines + alive_peer(nxt), nxt)
    assert vs[ID_A].action == "skip"


def test_weekend_is_quiet(oa):
    now = at(SAT, 16)
    vs, _ = one(oa, every10(at(SAT - timedelta(days=4), 9), at(SAT - timedelta(days=4), 17))
                + every10(at(SAT, 10), at(SAT, 14)) + alive_peer(now), now)
    assert vs[ID_A].action == "skip" and "周末" in vs[ID_A].reason


def test_fresh_install_is_not_counted(oa):
    now = at(TUE, 16)
    vs, _ = one(oa, every10(at(TUE, 14), at(TUE, 14, 40)) + alive_peer(now), now)
    assert vs[ID_A].action == "skip" and "刚装好" in vs[ID_A].reason


def test_same_bot_online_on_other_computer_is_quiet(oa):
    now = at(TUE, 16, 30)
    lines = every10(at(TUE, 9), at(TUE, 15)) + every10(at(TUE, 9), now, mid=ID_B) + alive_peer(now)
    vs, _ = one(oa, lines, now)
    assert vs[ID_A].action == "skip" and "另一台" in vs[ID_A].reason
    assert vs[ID_B].action == "online"


def test_habit_of_leaving_early_is_quiet(oa):
    lines = []
    for back in (1, 2, 3):  # 周五、周一……前几个工作日都 16:20 左右走
        d = TUE - timedelta(days=back)
        if d.weekday() < 5:
            lines += every10(at(d, 9), at(d, 16, 20))
    lines += every10(at(TUE - timedelta(days=4), 9), at(TUE - timedelta(days=4), 16, 20))  # 上周五
    lines += every10(at(TUE, 9), at(TUE, 16, 10))
    now = at(TUE, 17, 20)
    vs, _ = one(oa, lines + alive_peer(now), now)
    assert vs[ID_A].action == "skip" and "习惯" in vs[ID_A].reason
    # 同样这个人，今天上午 10 点就没了 ⇒ 不是习惯
    lines2 = [l for l in lines if "10/Nov" not in l] + every10(at(TUE, 9), at(TUE, 10))
    vs, _ = one(oa, lines2 + alive_peer(at(TUE, 11, 30)), at(TUE, 11, 30))
    assert vs[ID_A].action == "alert"


def test_once_per_episode_then_new_episode_alerts_again(oa):
    lines = every10(at(TUE, 9), at(TUE, 10))
    now = at(TUE, 11, 30)
    state = {"machines": {ID_A: {"episode": at(TUE, 10).isoformat(), "alerted_at": now.isoformat()}}}
    vs, _ = one(oa, lines + alive_peer(at(TUE, 12)), at(TUE, 12), state)
    assert vs[ID_A].action == "done"
    lines += every10(at(TUE, 12, 10), at(TUE, 14, 30))  # 回来了又掉
    vs, _ = one(oa, lines + alive_peer(at(TUE, 15, 40)), at(TUE, 15, 40), state)
    assert vs[ID_A].action == "alert"


def test_recovery_only_when_enabled(oa):
    now = at(TUE, 14)
    lines = every10(at(TUE, 9), now) + alive_peer(now)
    state = {"machines": {ID_A: {"episode": "x", "alerted_at": "x", "recovered_at": ""}}}
    vs, _ = one(oa, lines, now, state)
    assert vs[ID_A].action == "online"
    vs, _ = one(oa, lines, now, state, oa.Config(recovery=True))
    assert vs[ID_A].action == "recover"
    state["machines"][ID_A]["recovered_at"] = "y"
    vs, _ = one(oa, lines, now, state, oa.Config(recovery=True))
    assert vs[ID_A].action == "online"


def test_quiet_recovery_closes_the_episode(oa):
    now = at(TUE, 14)
    state = {"machines": {ID_A: {"episode": "x", "alerted_at": "x", "recovered_at": ""}}}
    vs, _ = oa.decide(recs(oa, every10(at(TUE, 9), now)), state, now, oa.Config())
    oa.close_quiet_recoveries(state, vs, now)
    assert state["machines"][ID_A]["recovered_at"] == now.isoformat()


# ── 刹车：我们这边坏了不许群发 ───────────────────────────────────────
def test_log_stopped_brakes_everything(oa):
    now = at(TUE, 16, 30)
    lines = every10(at(TUE, 9), at(TUE, 15)) + every10(at(TUE, 9), at(TUE, 15), mid=ID_B, app=APP_B)
    _, brake = one(oa, lines, now)
    assert "一条都不发" in brake


def test_mass_offline_brakes(oa):
    now = at(TUE, 16, 30)
    lines = []
    for mid, app in ((ID_A, APP_A), (ID_B, APP_B), (ID_C, "cli_c000000000000000")):
        lines += every10(at(TUE, 9), at(TUE, 15), mid=mid, app=app)
    _, brake = one(oa, lines + alive_peer(now), now)
    assert "同时要提醒" in brake


def test_future_lines_ignored_by_main_now(oa, tmp_path, capsys):
    log = tmp_path / "a.log"
    log.write_text("\n".join(every10(at(TUE, 9), at(TUE, 20))), encoding="utf-8")
    assert oa.main(["--log-file", str(log), "--now", at(TUE, 12).isoformat(),
                    "--state", str(tmp_path / "s.json")], http=_no_http) == 0
    assert "online 1" in capsys.readouterr().out


# ── 收件人 ───────────────────────────────────────────────────────────
def _no_http(*a, **k):
    raise AssertionError("dry-run 不该连飞书")


def app_info(owner_type=2, owner_id=OWNER, creator=""):
    return {"code": 0, "data": {"app": {"owner": {"type": owner_type, "owner_id": owner_id},
                                        "creator_id": creator}}}


def test_lookup_owner_uses_enterprise_owner_then_creator(oa):
    uid, p = oa.lookup_owner(APP_A, "t", lambda url, **k: app_info())
    assert (uid, p) == (OWNER, "")
    uid, _ = oa.lookup_owner(APP_A, "t", lambda url, **k: app_info(owner_type=0, owner_id="x",
                                                                    creator=OWNER))
    assert uid == OWNER


def test_lookup_owner_missing_permission_is_explained(oa):
    uid, p = oa.lookup_owner(APP_A, "t", lambda url, **k: {"code": 99991672})
    assert uid == "" and "admin:app.info:readonly" in p


def test_lookup_asks_for_union_id(oa):
    seen = []
    oa.lookup_owner(APP_A, "t", lambda url, **k: seen.append(url) or app_info())
    assert "user_id_type=union_id" in seen[0]


def test_manual_owner_wins_and_cache_is_reused(oa):
    now = at(TUE, 15)
    calls = []

    def http(url, **k):
        calls.append(url)
        return app_info()
    st: dict = {}
    r = oa.resolve_recipients([APP_A, APP_B], {APP_B: "a@b.com"}, st, now, "t", http,
                              [APP_A, APP_B], oa.Config())
    assert r == {APP_A: (OWNER, ""), APP_B: ("a@b.com", "")} and len(calls) == 1
    oa.resolve_recipients([APP_A], {}, st, now + timedelta(days=1), "t", http, [APP_A], oa.Config())
    assert len(calls) == 1  # 7 天内用缓存


def test_batch_created_apps_are_not_auto_sent(oa):
    now = at(TUE, 15)
    apps = [f"cli_batch{i:011d}" for i in range(3)]
    r = oa.resolve_recipients(apps[:1], {}, {}, now, "t", lambda url, **k: app_info(), apps, oa.Config())
    # 只查了要发的那个 ⇒ 名下只看到 1 个，照发
    assert r[apps[0]][0] == OWNER
    st = {"owners": {a: {"id": OWNER, "at": now.isoformat()} for a in apps}}
    r = oa.resolve_recipients(apps[:1], {}, st, now, "t", lambda url, **k: app_info(), apps, oa.Config())
    assert r[apps[0]][0] == "" and "批量" in r[apps[0]][1]


def test_load_owners_drops_bad_rows(oa, tmp_path):
    p = tmp_path / "o.json"
    p.write_text(json.dumps({APP_A: "ou_x", APP_B: "张三", "not_an_app": "ou_y"}), encoding="utf-8")
    assert oa.load_owners(p) == {APP_A: "ou_x"}


# ── 计划 / 文案 ──────────────────────────────────────────────────────
def _verdict(oa, mid, app=APP_A, os_="mac", action="alert"):
    return oa.Verdict(mid, action, "", {"id": mid, "app": app, "os": os_,
                                        "last_seen": at(TUE, 14)})


def test_two_computers_one_person_one_message(oa):
    vs = [_verdict(oa, ID_A), _verdict(oa, ID_B, app=APP_B)]
    msgs = oa.plan_messages(vs, {APP_A: (OWNER, ""), APP_B: (OWNER, "")}, {}, at(TUE, 15))
    assert len(msgs) == 1 and sorted(msgs[0].ids) == sorted([ID_A, ID_B]) and "2 台电脑" in msgs[0].text


def test_one_alert_per_person_per_day(oa):
    st = {"owner_day": {OWNER: TUE.date().isoformat()}}
    msgs = oa.plan_messages([_verdict(oa, ID_A)], {APP_A: (OWNER, "")}, st, at(TUE, 15))
    assert msgs[0].problem and not msgs[0].text


def test_no_owner_means_no_message(oa):
    msgs = oa.plan_messages([_verdict(oa, ID_A, app="")], {}, {}, at(TUE, 15))
    assert msgs[0].problem and msgs[0].to == ""


def test_alert_text_has_web_and_install_command(oa):
    t = oa.alert_text([{"os": "mac"}])
    assert t.startswith("你电脑上的 Her remote 失联了")
    assert "https://chat.auto-link.com.cn " in t
    assert "curl -fsSL https://chat.auto-link.com.cn/ccdl/feishu-agent/install.sh | bash" in t
    assert "install.ps1 | iex" in oa.alert_text([{"os": "windows"}])


def test_alert_text_carries_no_machine_data(oa):
    t = oa.alert_text([{"os": "mac", "id": ID_A, "app": APP_A, "v": V}])
    assert ID_A not in t and APP_A not in t and V not in t


# ── main：dry-run 默认、--send 才发 ──────────────────────────────────
def _scenario(tmp_path):
    log = tmp_path / "a.log"
    now = at(TUE, 16, 5)
    log.write_text("\n".join(every10(at(TUE, 9), at(TUE, 15)) + alive_peer(now)), encoding="utf-8")
    owners = tmp_path / "owners.json"
    owners.write_text(json.dumps({APP_A: OWNER}), encoding="utf-8")
    return ["--log-file", str(log), "--now", now.isoformat(), "--owners", str(owners),
            "--state", str(tmp_path / "state.json")]


def test_dry_run_is_default_and_writes_nothing(oa, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FEISHU_ALERT_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_ALERT_APP_SECRET", raising=False)
    assert oa.main(_scenario(tmp_path), http=_no_http) == 0
    out = capsys.readouterr().out
    assert "将发给 union_id:" + OWNER in out and "dry-run" in out
    assert not (tmp_path / "state.json").exists()


def test_send_posts_once_and_remembers(oa, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FEISHU_ALERT_APP_ID", "cli_sender0000000000")
    monkeypatch.setenv("FEISHU_ALERT_APP_SECRET", "s")
    posts = []

    def http(url, body=None, token=None):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t"}
        posts.append((url, body))
        return {"code": 0}
    args = _scenario(tmp_path) + ["--send"]
    assert oa.main(args, http=http) == 0
    assert len(posts) == 1 and "receive_id_type=union_id" in posts[0][0]
    assert posts[0][1]["receive_id"] == OWNER
    assert "失联" in json.loads(posts[0][1]["content"])["text"]
    st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert st["machines"][ID_A]["alerted_at"]
    assert oa.main(args, http=http) == 0
    assert len(posts) == 1  # 第二拍不重发


def test_failed_send_is_not_remembered(oa, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("FEISHU_ALERT_APP_ID", "cli_sender0000000000")
    monkeypatch.setenv("FEISHU_ALERT_APP_SECRET", "s")

    def http(url, body=None, token=None):
        if "tenant_access_token" in url:
            return {"code": 0, "tenant_access_token": "t"}
        return {"code": 230013}
    assert oa.main(_scenario(tmp_path) + ["--send"], http=http) == 2
    assert "可用范围" in capsys.readouterr().out
    st = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert not (st.get("machines") or {}).get(ID_A, {}).get("alerted_at")


def test_send_without_credentials_does_not_crash(oa, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FEISHU_ALERT_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_ALERT_APP_SECRET", raising=False)
    assert oa.main(_scenario(tmp_path) + ["--send"], http=_no_http) == 2
    assert "凭据" in capsys.readouterr().out


def test_env_file_credentials(oa, tmp_path, monkeypatch):
    monkeypatch.delenv("FEISHU_ALERT_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_ALERT_APP_SECRET", raising=False)
    p = tmp_path / "e.env"
    p.write_text("FEISHU_ALERT_APP_ID=cli_x\nFEISHU_ALERT_APP_SECRET='sec'\n", encoding="utf-8")
    assert oa.load_sender(str(p)) == ("cli_x", "sec")
    assert oa.load_sender(None) is None


# ── 回放：工作日 40~50 分钟的空档（午饭/开会）不吵人 ────────────────
def test_replay_short_daytime_gaps_never_alert(oa):
    lines = (every10(at(TUE, 9), at(TUE, 9, 25)) + every10(at(TUE, 10, 5), at(TUE, 12, 29))
             + every10(at(TUE, 13, 10), at(TUE, 17, 50)))
    lines += every10(at(TUE, 8), at(TUE, 19), mid=ID_B, app=APP_B)
    cfg, state, t, hits = oa.Config(), {}, at(TUE, 8), []
    allrecs = recs(oa, lines)
    while t <= at(TUE, 19):
        vs, brake = oa.decide([r for r in allrecs if r["ts"] <= t], state, t, cfg)
        hits += [v.id for v in vs if v.action == "alert" and not brake]
        t += timedelta(minutes=10)
    assert hits == []
