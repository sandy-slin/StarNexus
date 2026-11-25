"""第 11 轮 A：一键安装全自动 —— 建机器人时把建群那 5 个权限一起申请、补齐、发布，并分清「上线 / 等审批」。

## 为什么有这组测试

11-06 之前 `register.WANT_SCOPES` 只有 3 项：「一个会话一个群」要的 5 项（建群、改群名、拉人、查群、
收群消息）装机时一个都没申请 ⇒ 每个新同事装完，建群那个功能都用不了，得有人去后台手动开 + 发版。

🔴 所有飞书接口都走注入的假 http（`FakeHttp`），并且把真 `urllib.request.urlopen` 换成一碰就炸的桩：
**一个真请求都不发**（本机那个飞书应用的写接口更不能碰）。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import urllib.request

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bridge import botmenu, register  # noqa: E402
from bridge.fleet import groupsetup  # noqa: E402

_SRC = os.path.normpath(os.path.join(_ROOT, "..", "..", "feishu-agent-oneclick", "src"))
MAC = os.path.join(_SRC, "install-mac.sh")
WIN = os.path.join(_SRC, "install-windows.ps1")

GROUP_SCOPES = [n[2] for n in groupsetup.NEEDS]
BASE = ["im:message.p2p_msg:readonly", "im:message:send_as_bot", "application:application:patch"]
# 🔴 第 18 轮：这个文件原来按「正好 8 项」写死（函数名里都是 `all_eight`）。
# 第 18 轮为了「收到」表情和流式输出又加了两项（`im:message.reactions:write_only`、
# `cardkit:card:write`）⇒ 9 条测试全红。
# 判据改成**从真清单推**：要钉的是「装机申请的 == 代码要的，一项不漏」，
# 而不是「正好几项」—— 后者每加一项功能就得改一次测试，而且改的时候很容易顺手把
# 「有没有漏」那层判据一起放松。数量断言换成「至少包含这些」+ 逐项检查。
ALL_WANT = list(register.WANT_SCOPES)
ALL8 = list(dict.fromkeys(BASE + GROUP_SCOPES))
NEW5 = ["im:chat:create", "im:chat:update", "im:chat.members:write_only", "im:chat:read",
        "im:message.group_msg"]


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """真网络一碰就炸：漏注入 http 的地方会当场红，而不是悄悄打到飞书。"""
    def boom(*_a, **_k):
        raise AssertionError("测试里发了真网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(botmenu, "_http", boom)


class FakeHttp:
    """按 (method, path 前缀) 回包；记下每一次调用。没配的路径回 code 0。"""

    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}
        self.calls: list[tuple] = []

    def __call__(self, method, path, token, body=None):
        self.calls.append((method, path, token, body))
        for (m, prefix), out in self.routes.items():
            if m == method and path.startswith(prefix):
                return out(body) if callable(out) else out
        if path.startswith("/auth/"):
            return {"code": 0, "tenant_access_token": "t-fake"}
        return {"code": 0, "data": {}}

    def steps(self) -> list[str]:
        """调用序列翻成步骤名，方便断言顺序。"""
        names = []
        for method, path, _t, body in self.calls:
            if path.startswith("/auth/"):
                names.append("token")
            elif path == "/application/v6/scopes":
                names.append("read_scopes")
            elif path.endswith("/config") and body and "scope" in body:
                names.append("add_scopes")
            elif path.endswith("/config"):
                names.append("subscribe")
            elif path.endswith("/ability"):
                names.append("menu")
            elif path.endswith("/publish"):
                names.append("publish")
            elif "/app_versions/" in path:
                names.append("version_status")
            else:
                names.append(f"{method} {path}")
        return names

    def added(self) -> list[str]:
        for _m, path, _t, body in self.calls:
            if path.endswith("/config") and body and "scope" in body:
                return [i["scope_name"] for i in body["scope"]["add_scopes"]]
        return []


def _scopes_reply(names, granted=True, kind="tenant"):
    return {"code": 0, "data": {"scopes": [
        {"scope_name": n, "grant_status": 1 if granted else 0, "scope_type": kind} for n in names]}}


APP = "cli_test_only_000"


def _run(fake: FakeHttp) -> list[str]:
    return botmenu.setup(APP, "secret-not-real", "1.20261106.000000", http=fake)


# ---------- 单一真相：权限清单从 groupsetup.NEEDS 来 ----------


def test_want_scopes_has_all_eight():
    """装机申请的清单要盖住「基础 + 建群 + 后来加的」每一项，一个不漏。"""
    for scope in ALL8:
        assert scope in register.WANT_SCOPES, f"装机没申请 {scope}"
    for scope in NEW5:
        assert scope in register.WANT_SCOPES, f"装机没申请 {scope}"
    assert len(register.WANT_SCOPES) == len(set(register.WANT_SCOPES)), "清单里有重复"


def test_build_addons_prefills_all_eight_scopes():
    """扫码确认页（平台灰度开了的话）一次勾上**全部**要申请的项 —— 和 WANT_SCOPES 一致。"""
    tenant = register.build_addons()["scopes"]["tenant"]
    assert sorted(tenant) == sorted(ALL_WANT), "确认页预填的和代码要的不一致"


def test_group_scopes_are_imported_not_copied():
    """🔴 建群那几项只许在 groupsetup.py 里写一次 —— register.py 里出现字面量 = 抄了一份，会漂。"""
    src = _read(os.path.join(_ROOT, "bridge", "register.py"))
    for scope in NEW5:
        assert f'"{scope}"' not in src and f"'{scope}'" not in src, f"register.py 抄了 {scope}"
    assert "from .fleet.groupsetup import NEEDS" in src


def test_a_new_need_in_groupsetup_flows_into_the_install(monkeypatch):
    """groupsetup 加一项 ⇒ 装机自动跟着申请（证明是真的从 NEEDS 派生，不是碰巧相等）。"""
    import importlib

    extra = ("pin", "置顶", "im:chat.top_notice:write_only", "群置顶", ())
    monkeypatch.setattr(groupsetup, "NEEDS", groupsetup.NEEDS + (extra,))
    try:
        reloaded = importlib.reload(register)
        assert "im:chat.top_notice:write_only" in reloaded.WANT_SCOPES
        assert "im:chat.top_notice:write_only" in reloaded.build_addons()["scopes"]["tenant"]
    finally:
        monkeypatch.undo()
        importlib.reload(register)
    assert "im:chat.top_notice:write_only" not in register.WANT_SCOPES


def test_register_check_runs_as_a_script_with_eight_scopes():
    """安装器用 `python bridge/register.py` 脚本方式跑：没有父包，import groupsetup 也得能走通。"""
    proc = subprocess.run([sys.executable, os.path.join(_ROOT, "bridge", "register.py"), "--check"],
                          capture_output=True, text=True, timeout=60)
    assert "Traceback" not in proc.stderr, proc.stderr[-600:]
    assert f"scopes={len(ALL_WANT)}" in proc.stdout, proc.stdout   # 不写死数量，见文件头红字


def test_botmenu_reads_the_list_in_script_mode():
    """安装器用 `python bridge/botmenu.py <config>` 跑 ⇒ 脚本方式下也得拿到同一份 8 项。"""
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import botmenu\n"
            "print(len(botmenu._reg().WANT_SCOPES))\n") % os.path.join(_ROOT, "bridge")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == str(len(ALL_WANT)), proc.stdout + proc.stderr[-600:]


# ---------- setup()：补权限 → 菜单 → 订阅 → 发布 ----------


def test_setup_order_scopes_before_publish():
    """🔴 补权限必须在发布之前，否则新权限不进这个版本。"""
    fake = FakeHttp({("GET", "/application/v6/scopes"): _scopes_reply(BASE)})
    _run(fake)
    steps = fake.steps()
    assert steps[:6] == ["token", "read_scopes", "add_scopes", "menu", "subscribe", "publish"], steps
    assert steps.index("add_scopes") < steps.index("publish")


def test_missing_scopes_are_really_added():
    """老应用只有单聊 3 项 ⇒ 真调了 add_scopes，补的正好是建群那 5 项，身份是应用（tenant）。"""
    fake = FakeHttp({("GET", "/application/v6/scopes"): _scopes_reply(BASE)})
    lines = _run(fake)
    # 补的是「清单里除了已有那 3 项的全部」—— 不写死是哪几项（第 18 轮又加了 2 项）。
    # 🔴 比**集合**不比顺序：新加的两项插在建群那几项前面，比列表会因为顺序红，
    # 而顺序不是这条测试要守的东西（它守的是「该补的都补了、没多补」）。
    assert set(fake.added()) == {s for s in ALL_WANT if s not in BASE}
    body = next(b for _m, p, _t, b in fake.calls if b and "scope" in b)
    assert {i["token_type"] for i in body["scope"]["add_scopes"]} == {"tenant"}
    path = next(p for _m, p, _t, b in fake.calls if b and "scope" in b)
    assert path == f"/application/v7/applications/{APP}/config"
    want = len([s for s in ALL_WANT if s not in BASE])
    assert any(line.startswith(f"OK=已申请权限 {want} 项") for line in lines), lines


def test_nothing_added_when_everything_is_there():
    fake = FakeHttp({("GET", "/application/v6/scopes"): _scopes_reply(ALL_WANT)})
    lines = _run(fake)
    assert "add_scopes" not in fake.steps()
    assert any("都已申请过" in line for line in lines)


def test_applied_but_not_yet_granted_counts_as_there():
    """已申请、还没批（grant_status=0）⇒ 不重复申请，差的是管理员那一步。"""
    fake = FakeHttp({("GET", "/application/v6/scopes"): _scopes_reply(ALL_WANT, granted=False)})
    _run(fake)
    assert "add_scopes" not in fake.steps()


def test_broad_im_chat_covers_the_four_narrow_ones():
    """有人图省事开了 `im:chat` ⇒ 建群/改名/拉人/查群那 4 项不再申请（groupsetup 的替代项）。"""
    fake = FakeHttp({("GET", "/application/v6/scopes"): _scopes_reply(BASE + ["im:chat"])})
    _run(fake)
    # `im:chat` 顶掉建群那 4 项 ⇒ 剩下该补的是「收群消息」+ 第 18 轮加的那两项
    covered = set(BASE) | {"im:chat:create", "im:chat:update",
                           "im:chat.members:write_only", "im:chat:read",
                               "im:chat"}  # 第 37 轮 WANT_SCOPES 加了 im:chat 本身（已有 ⇒ 不补）
    assert set(fake.added()) == {s for s in ALL_WANT if s not in covered}


def test_user_identity_scope_does_not_count():
    """只有用户身份那一份的 scope 不算（我们用机器人身份建群）。"""
    fake = FakeHttp({("GET", "/application/v6/scopes"): {"code": 0, "data": {"scopes": [
        *_scopes_reply(BASE)["data"]["scopes"],
        {"scope_name": "im:chat:create", "grant_status": 1, "scope_type": "user"}]}}})
    _run(fake)
    assert "im:chat:create" in fake.added()


def test_cannot_read_scopes_then_add_all():
    """读不到现状 ⇒ 全部申请一遍（add 是增量的）。"""
    fake = FakeHttp({("GET", "/application/v6/scopes"): {"code": 99991672, "msg": "denied"}})
    _run(fake)
    assert set(fake.added()) == set(ALL_WANT), "读不到现状时要把清单里每一项都申请一遍"


def test_add_scopes_failure_says_so_and_still_publishes():
    fake = FakeHttp({
        ("GET", "/application/v6/scopes"): _scopes_reply(BASE),
        ("PATCH", f"/application/v7/applications/{APP}/config"):
            lambda body: ({"code": 210040, "msg": "under review"} if "scope" in body else {"code": 0}),
    })
    lines = _run(fake)
    warn = [line for line in lines if line.startswith("WARN=") and "权限" in line]
    assert warn and "审核中" in warn[0], lines
    assert any("auth?q=" in line for line in lines), "没给手动开权限的链接"
    assert "publish" in fake.steps()


def test_sensitive_scope_is_called_out():
    """🔴 `im:message.group_msg` 是敏感权限，管理员审批时会多看一眼 —— 照实说一句。"""
    fake = FakeHttp({("GET", "/application/v6/scopes"): _scopes_reply(BASE)})
    lines = _run(fake)
    assert any("敏感" in line and "群组中所有消息" in line for line in lines), lines

    quiet = FakeHttp({("GET", "/application/v6/scopes"): _scopes_reply(ALL_WANT)})
    assert not any("敏感" in line for line in _run(quiet)), "早就批过了还在提醒"


def test_subscribe_also_adds_receive_message_event():
    """确认页预填被忽略时收消息事件也会漏 ⇒ 订阅那一步把 register.WANT_EVENTS 全补上。"""
    fake = FakeHttp()
    _run(fake)
    body = next(b for _m, p, _t, b in fake.calls if p.endswith("/config") and b and "event" in b)
    assert set(register.WANT_EVENTS) <= set(body["event"]["add_events"])
    assert body["event"]["subscription_type"] == "websocket"
    assert set(register.WANT_CALLBACKS) <= set(body["callback"]["add_callbacks"])


# ---------- 发布之后：上线 / 等审批 / 看不出来 / 失败 ----------


def _published(status_reply):
    return FakeHttp({
        ("GET", "/application/v6/scopes"): _scopes_reply(ALL_WANT),
        ("POST", f"/application/v7/applications/{APP}/publish"):
            {"code": 0, "data": {"version_id": "oav_fake", "version": "1.20261106.000000"}},
        ("GET", f"/application/v6/applications/{APP}/app_versions/oav_fake"): status_reply,
    })


def _state(lines):
    got = [line[len("PUBLISH="):] for line in lines if line.startswith("PUBLISH=")]
    assert len(got) == 1, lines
    return got[0]


def test_online_when_version_is_approved():
    lines = _run(_published({"code": 0, "data": {"app_version": {"status": 1}}}))
    assert _state(lines) == "online"
    assert any("免审核" in line and "5 分钟" in line for line in lines)


def test_review_when_version_is_under_review():
    lines = _run(_published({"code": 0, "data": {"app_version": {"status": 3}}}))
    assert _state(lines) == "review"
    assert any("等管理员批" in line for line in lines)
    assert any("审批" in line and line.startswith("NOTE=") for line in lines), "没说审批在哪"


def test_rejected_version_is_a_failure():
    lines = _run(_published({"code": 0, "data": {"app_version": {"status": 2}}}))
    assert _state(lines) == "failed"


def test_cannot_read_status_says_both_possibilities():
    """读不到版本状态（本机实测缺权限）⇒ 两种可能都说，不替飞书下结论。"""
    lines = _run(_published({"code": 99991672, "msg": "denied"}))
    assert _state(lines) == "submitted"
    note = " ".join(line for line in lines if line.startswith("NOTE="))
    assert "免审核" in note and "审核" in note
    assert not any("免审核，约 5 分钟后生效" in line for line in lines), "看不出来还说成上线了"


def test_status_query_uses_the_returned_version_id():
    fake = _published({"code": 0, "data": {"app_version": {"status": 1}}})
    _run(fake)
    assert any("/app_versions/oav_fake" in p and m == "GET" for m, p, _t, _b in fake.calls)


@pytest.mark.parametrize("code, words", [
    (210302, "审批"),
    (210303, "版本号"),
    (-1, "连不上"),
])
def test_publish_failure_is_translated(code, words):
    fake = FakeHttp({("POST", f"/application/v7/applications/{APP}/publish"):
                     {"code": code, "msg": "raw"}})
    lines = _run(fake)
    assert _state(lines) == "failed"
    warn = next(line for line in lines if line.startswith("WARN=没能自动发布"))
    assert words in warn, warn
    assert "code=" not in warn, f"认得的错误码还在吐原始码：{warn}"


def test_publish_missing_permission_names_the_scope():
    fake = FakeHttp({("POST", f"/application/v7/applications/{APP}/publish"): {
        "code": 99991672, "msg": "denied",
        "error": {"permission_violations": [{"subject": "application:application:patch"}]}}})
    warn = next(line for line in _run(fake) if line.startswith("WARN=没能自动发布"))
    assert "application:application:patch" in warn


def test_bad_credentials_stop_early_and_report_failed():
    fake = FakeHttp({("POST", "/auth/"): {"code": 10003, "msg": "invalid"}})
    lines = _run(fake)
    assert fake.steps() == ["token"]
    assert _state(lines) == "failed"


# ---------- 调用点：真的接上了（查调用形状，不查名字）----------


def test_call_site_setup_calls_each_step():
    src = _read(os.path.join(_ROOT, "bridge", "botmenu.py"))
    body = src[src.index("def setup("):src.index('if __name__ == "__main__":')]
    for shape in ("read_scopes(token, http=call)", "add_scopes(app_id, token, todo, http=call)",
                  "write_menu(app_id, token, http=call)", "subscribe(app_id, token, http=call)",
                  "publish(app_id, token, version,", "version_status(app_id, token,"):
        assert shape in body, f"setup() 里没调 {shape}"
    assert body.index("add_scopes(app_id") < body.index("publish(app_id"), "补权限排到发布后面了"


def test_call_site_main_runs_setup():
    src = _read(os.path.join(_ROOT, "bridge", "botmenu.py"))
    main = src[src.index('if __name__ == "__main__":'):]
    assert "in setup(" in main


def test_call_site_register_passes_addons():
    src = _read(os.path.join(_ROOT, "bridge", "register.py"))
    assert "addons=build_addons()," in src


@pytest.mark.parametrize("path, call, parse", [
    (MAC, '"$VENV/bin/python" "$APP_HOME/bridge/botmenu.py" "$CONFIG"', 'PUBLISH=*) PUBLISH_STATE='),
    (WIN, "& $venvPy (Join-Path $bridgeDir 'botmenu.py') $config", "$publishState = $s.Substring(8)"),
])
def test_call_site_installers_run_setup_and_read_the_verdict(path, call, parse):
    text = _read(path)
    assert call in text, "安装器不再跑 botmenu.py"
    assert parse in text, "安装器不读 PUBLISH= 那行 ⇒ 最后一屏分不清上线还是等审批"


@pytest.mark.parametrize("path, case", [
    (MAC, ("online)", "review)", "submitted)", "failed)")),
    (WIN, ("'online'", "'review'", "'submitted'", "'failed'")),
])
def test_final_screen_branches_on_the_publish_verdict(path, case):
    text = _read(path)
    tail = text[text.rindex("装好了，它现在已经在听你的飞书消息"):]
    for label in case:
        assert label in tail[:1500], f"最后一屏没处理 {label}"


# ---------- 安装器少问几次 ----------


@pytest.mark.parametrize("path", [MAC, WIN])
def test_no_upfront_scan_or_manual_choice(path):
    """没预烤的直接走扫码；手工那条藏到扫码失败之后。（注释里说「原来怎样」不算）"""
    code = _code_lines(path)
    assert "选 [1/2" not in code
    assert "两种办法" not in code


@pytest.mark.parametrize("path", [MAC, WIN])
def test_no_press_enter_to_upgrade(path):
    """升级不再要按一下回车；换机器人改成 NEW_BOT=1。"""
    code = _code_lines(path)
    assert "直接按回车继续升级" not in code
    assert "NEW_BOT" in code


def _code_lines(path: str) -> str:
    return "\n".join(line for line in _read(path).splitlines() if not line.lstrip().startswith("#"))


def test_sensitive_scope_comes_from_needs():
    assert register.SENSITIVE_SCOPES == {n[2] for n in groupsetup.NEEDS if n[0] == "receive"}
    assert "im:message.group_msg" in register.SENSITIVE_SCOPES


def test_windows_prompt_count():
    """Windows 剩下的 Read-Host：只有 App ID、App Secret —— 都在「扫码没走通」之后的手工那条路上。

    2026-11-06 用户原话「用户都是小白…争取一键搞定」⇒ 选目录那两处删了（不问，用默认 / 沿用 / IT 指定）。
    """
    lines = [line for line in _read(WIN).splitlines()
             if "Read-Host" in line and not line.lstrip().startswith("#")]
    assert len(lines) == 2, lines


def test_mac_prompt_count():
    lines = [line for line in _read(MAC).splitlines()
             if re.search(r"\bread -r", line) and not line.lstrip().startswith("#")
             and "while IFS= read -r line" not in line]
    assert len(lines) == 2, lines  # App ID、App Secret —— 只在手工那条（选目录 11-06 删了，不问）


@pytest.fixture
def here_dir():
    """夹具放测试目录旁边，不放 tmp_path（macOS 上是 pathguard 豁免前缀）。"""
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".r11a-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _mac_block(start: str, end: str) -> str:
    text = _read(MAC)
    a = text.index(start)
    return text[a:text.index(end, a)]


def _bash(here_dir, body: str, env: dict) -> dict:
    tty = here_dir / "tty"
    tty.write_text("\n", encoding="utf-8")
    script = ("set -uo pipefail\nok(){ :; }; warn(){ :; }; say(){ :; }\nexec 3>&1 1>&2\n"
              + body.replace("</dev/tty", f"<{tty}")
              + '\nprintf "APP_ID=%s\\nAPP_SECRET=%s\\nCREATE_MODE=%s\\nWORKSPACE=%s\\n"'
                ' "${APP_ID:-}" "${APP_SECRET:-}" "${CREATE_MODE:-}" "${WORKSPACE:-}" >&3\n')
    clean = {k: v for k, v in os.environ.items()
             if k not in ("APP_ID", "APP_SECRET", "NEW_BOT", "MANUAL_SETUP")}
    clean.update(env)
    out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, env=clean,
                         timeout=60)
    return dict(line.split("=", 1) for line in out.stdout.strip().splitlines() if "=" in line)


def test_mac_reinstall_reuses_the_bot_without_asking(here_dir):
    config = here_dir / "config.toml"
    config.write_text('app_id = "cli_old_one"\napp_secret = "old_secret"\n', encoding="utf-8")
    block = _mac_block('PREBAKED=""', 'if [ -z "${APP_ID:-}" ]; then')
    got = _bash(here_dir, f'CONFIG="{config}"\n' + block, {})
    assert got["APP_ID"] == "cli_old_one" and got["APP_SECRET"] == "old_secret"

    fresh = _bash(here_dir, f'CONFIG="{config}"\n' + block, {"NEW_BOT": "1"})
    assert fresh["APP_ID"] == "", "NEW_BOT=1 还在沿用旧机器人"


def test_mac_fresh_machine_goes_straight_to_scan(here_dir):
    block = _mac_block('if [ -z "${APP_ID:-}" ]; then', 'if [ "$CREATE_MODE" = 1 ]; then')
    assert _bash(here_dir, 'APP_ID=""\n' + block + "fi\n", {})["CREATE_MODE"] == "1"
    manual = _bash(here_dir, 'APP_ID=""\n' + block + "fi\n", {"MANUAL_SETUP": "1"})
    assert manual["CREATE_MODE"] == "2"


def test_mac_reinstall_keeps_the_workspace_without_asking(here_dir):
    """旧配置里的目录还在 ⇒ 不扫、不问；目录名里带 \\ 和 \" 也读得对（tomllib 解析）。"""
    venv_bin = here_dir / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "python").symlink_to(sys.executable)
    ws = here_dir / 'we"ird\\dir'
    ws.mkdir()
    escaped = str(ws).replace("\\", "\\\\").replace('"', '\\"')
    config = here_dir / "config.toml"
    config.write_text(f'[agent]\ndefault_workspace = "{escaped}"\n', encoding="utf-8")
    block = _mac_block('KEEP_WS=""', "fi  # KEEP_WS")
    got = _bash(here_dir, f'CONFIG="{config}"\nVENV="{here_dir / "venv"}"\n' + block + "fi\n", {})
    assert got["WORKSPACE"] == str(ws)

    # 阳性对照：目录没了 ⇒ 照旧问（tty 里只有回车 ⇒ 落到默认值）
    import shutil

    shutil.rmtree(ws)
    again = _bash(here_dir, f'CONFIG="{config}"\nVENV="{here_dir / "venv"}"\n'
                  f'WORKSPACE_DEFAULT="{here_dir}"\n' + block + "fi\n", {})
    assert again["WORKSPACE"] == str(here_dir)


def test_manual_guide_lists_the_patch_scope():
    """手工那条只让人开 3 项：有了 application:application:patch，剩下的安装器自己补。"""
    for path in (MAC, WIN):
        text = _read(path)
        guide = text[text.index("手工建（约 3 分钟"):][:1500]
        assert "application:application:patch" in guide
