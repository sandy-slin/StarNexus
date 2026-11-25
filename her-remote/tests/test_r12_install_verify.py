"""第 12 轮 W：装完那一刻说清「悬浮菜单写没写上」，doctor 能查出来。

## 为什么有这组测试

2026-11-06 一位同事按文档装完、也发了版，单聊输入框上面就是没有那排悬浮按钮。
安装器第 5 步跑 `botmenu.py setup()`，它会打 `WARN=常驻菜单没配上：缺权限 …` ——
但这行埋在一堆输出里，最后一屏只看 `PUBLISH=`，还会说「约 5 分钟后菜单和建群就都能用」；
doctor 也没有任何一项查菜单。

- 安装器（Mac）：抠出「解析 MENU_OUT」和「最后一屏菜单那行」两段**真跑**，
  MENU_OUT 用**真 `botmenu.setup()`** 配假 http 生成（botmenu 改了措辞，这里会红）。
- doctor（Mac）：抠出 [5b] 那段真跑，python 换成带假 `urlopen` 的包装 ⇒ **一个真请求都不发**，
  并且记下每个请求的方法，断言除了换 token 只有 GET。
- Windows：没有 pwsh ⇒ 静态核对和 Mac 同一套判据、同一句话、同一段 Python。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from bridge import botmenu  # noqa: E402

_SRC = os.path.normpath(os.path.join(_ROOT, "..", "..", "feishu-agent-oneclick", "src"))
MAC = os.path.join(_SRC, "install-mac.sh")
WIN = os.path.join(_SRC, "install-windows.ps1")
DOC_MAC = os.path.join(_SRC, "doctor-mac.sh")
DOC_WIN = os.path.join(_SRC, "doctor-windows.ps1")

APP = "cli_test_only_000"
PERM_LINE = "悬浮按钮还没装上：等管理员批完权限，它会自己补上（不用重装）"
NOT_YET = "悬浮按钮还没装上"
PROMISE_MENU = "菜单和建群就都能用"


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _block(path: str, name: str) -> str:
    text = _read(path)
    start = text.index(f"# {name}-BEGIN")
    end = text.index(f"# {name}-END", start)
    return text[start:end]


@pytest.fixture
def here_dir():
    """夹具放测试目录旁边，不放 tmp_path（macOS 上是 pathguard 豁免前缀）。"""
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".r12w-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """真网络一碰就炸：`botmenu.setup()` 漏注入 http 会当场红，而不是悄悄打到飞书。"""
    def boom(*_a, **_k):
        raise AssertionError("测试里发了真网络请求")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    monkeypatch.setattr(botmenu, "_http", boom)


# ---------- 用真 botmenu.setup() 生成 MENU_OUT ----------


class FakeHttp:
    def __init__(self, routes: dict | None = None):
        self.routes = routes or {}

    def __call__(self, method, path, token, body=None):
        for (m, prefix), out in self.routes.items():
            if m == method and path.startswith(prefix):
                return out
        if path.startswith("/auth/"):
            return {"code": 0, "tenant_access_token": "t-fake"}
        return {"code": 0, "data": {}}


_ABILITY = ("PATCH", f"/application/v7/applications/{APP}/ability")
MENU_REPLIES = {
    "ok": {},
    "perm": {_ABILITY: {"code": 99991672, "msg": "denied", "error": {
        "permission_violations": [{"subject": "application:application:patch"}]}}},
    "other": {_ABILITY: {"code": 210041, "msg": "bot ability not enabled"}},
    "token": {("POST", "/auth/"): {"code": 10003, "msg": "invalid"}},
}


def _menu_out(kind: str) -> str:
    lines = botmenu.setup(APP, "secret-not-real", "1.20261106.000000", http=FakeHttp(MENU_REPLIES[kind]))
    return "\n".join(lines)


def _bash(script: str, env: dict | None = None) -> str:
    stubs = (
        "set -uo pipefail\n"
        'say(){ printf "SAY %s\\n" "$1"; }\n'
        'ok(){ printf "OK %s\\n" "$1"; }\n'
        'warn(){ printf "WARN %s\\n" "$1"; }\n'
        'bad(){ printf "BAD %s\\n" "$1"; }\n'
        'hint(){ printf "HINT %s\\n" "$1"; }\n'
    )
    proc = subprocess.run(["/bin/bash", "-c", stubs + script], capture_output=True, text=True,
                          env={**os.environ, **(env or {})}, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def _verdict(menu_out: str) -> tuple[str, str]:
    """跑安装器「解析 MENU_OUT」那段 ⇒ (MENU_STATE, 打到屏上的东西)。"""
    out = _bash(_block(MAC, "MENU-VERDICT") + '\nprintf "STATE=%s\\n" "$MENU_STATE"\n',
                {"MENU_OUT": menu_out})
    state = re.search(r"^STATE=(.*)$", out, re.M).group(1)
    return state, out


def _screen(state: str) -> str:
    return _bash(_block(MAC, "MENU-SCREEN"), {"MENU_STATE": state})


# ---------- 安装器（Mac 真跑）----------


@pytest.mark.parametrize("kind, state", [
    ("ok", "ok"), ("perm", "perm"), ("other", "other"), ("token", "other"),
])
def test_mac_verdict_reads_menu_state_from_real_setup_output(kind, state):
    got, out = _verdict(_menu_out(kind))
    assert got == state, out
    if kind == "perm":
        assert "缺权限 application:application:patch" in out, "WARN 那行原话没打出来"


def test_mac_verdict_empty_output_is_not_silent():
    """botmenu.py 崩了 / 没输出：原来一行都不打。"""
    got, out = _verdict("Traceback (most recent call last):\nModuleNotFoundError: x")
    assert got == ""
    assert "配菜单这一步没跑完" in out and "ModuleNotFoundError" in out


def test_mac_final_screen_says_menu_missing_on_its_own_line_when_permission_pending():
    """同事那台的形状：写菜单缺权限 ⇒ 最后一屏单独一行说悬浮按钮没装上 + 下一步。"""
    state, _ = _verdict(_menu_out("perm"))
    out = _screen(state)
    assert f"WARN    {PERM_LINE}" in out.splitlines(), out
    assert "「菜单」两个字照样有按钮" in out


@pytest.mark.parametrize("state", ["other", ""])
def test_mac_final_screen_other_failures_say_reinstall(state):
    out = _screen(state)
    assert NOT_YET in out
    assert "等管理员批完权限" not in out, "不是缺权限就别叫人等管理员"
    assert "重新双击 feishu-agent-mac.command" in out


def test_mac_final_screen_quiet_when_menu_written():
    state, _ = _verdict(_menu_out("ok"))
    assert _screen(state).strip() == ""


@pytest.mark.parametrize("state, promised", [("ok", True), ("perm", False), ("other", False), ("", False)])
def test_mac_online_publish_does_not_promise_menu_it_does_not_have(state, promised):
    """免审核上线了，但这个版本里没菜单 ⇒ 不能再说「菜单和建群就都能用」（第 12 轮现场被这句骗了）。"""
    text = _read(MAC)
    start = text.index('case "$PUBLISH_STATE" in')
    end = text.index("\n  esac", start) + len("\n  esac")
    out = _bash(text[start:end], {"MENU_STATE": state, "PUBLISH_STATE": "online"})
    assert (PROMISE_MENU in out) is promised, out
    assert "建群" in out


def test_mac_menu_line_sits_outside_the_connected_branch():
    """连没连上都要说：块必须在「连上 / 没连上」那个 if 收尾之后、分隔线之前。"""
    text = _read(MAC)
    begin = text.index("# MENU-SCREEN-BEGIN")
    tail_of_if = '诊断结果会直接告诉你卡在哪一步"\n  fi\nfi\n'
    assert text.index(tail_of_if) + len(tail_of_if) == begin
    assert text[text.index("# MENU-SCREEN-END", begin):].splitlines()[1].startswith('say "=====')


def test_call_site_mac_verdict_follows_the_setup_run():
    """调用形状：跑 botmenu.py 的那一行后面紧跟解析段；最后一屏读 MENU_STATE。"""
    text = _read(MAC)
    run = text.index('MENU_OUT="$("$VENV/bin/python" "$APP_HOME/bridge/botmenu.py" "$CONFIG" 2>&1)"')
    assert run < text.index("# MENU-VERDICT-BEGIN") < run + 300
    assert 'case "$MENU_STATE" in' in _block(MAC, "MENU-SCREEN")


# ---------- 安装器（Windows 静态核对，和 Mac 同一套）----------


def test_windows_parses_the_same_prefixes_into_the_same_states():
    mac = _block(MAC, "MENU-VERDICT")
    win = _read(WIN)
    loop = win[win.index("$menuOut = & $venvPy (Join-Path $bridgeDir 'botmenu.py') $config 2>&1"):]
    loop = loop[:loop.index("} catch {")]
    assert "OK=常驻菜单已写入*)" in mac and "WARN=常驻菜单*)" in mac and "*缺权限*) MENU_STATE=perm" in mac
    assert "$s.StartsWith('OK=常驻菜单已写入')) { Ok (\"OK \" + $s.Substring(3)); $menuState = 'ok' }" in loop
    assert "$s.StartsWith('WARN=常驻菜单')" in loop
    assert "if ($s.Contains('缺权限')) { $menuState = 'perm' } else { $menuState = 'other' }" in loop
    # 菜单那两条必须排在通用 OK= / WARN= 前面，否则永远落进通用分支、状态恒空
    assert loop.index("'OK=常驻菜单已写入'") < loop.index("$s.StartsWith('OK=')) {")
    assert loop.index("'WARN=常驻菜单'") < loop.index("$s.StartsWith('WARN=')) {")
    assert "$menuState = ''" in win[:win.index("$menuOut = & $venvPy")]


def test_windows_final_screen_says_the_same_line_outside_the_connected_branch():
    win = _read(WIN)
    tail_of_if = '诊断结果会直接告诉你卡在哪一步"\n  }\n}\n'
    begin = win.index(tail_of_if) + len(tail_of_if)
    block = win[begin:win.index('Say "=====', begin)]
    assert "if ($menuState -eq 'perm') {" in block
    assert f'Warn "   {PERM_LINE}"' in block
    assert "} elseif ($menuState -ne 'ok') {" in block
    assert "重新双击 feishu-agent-windows.bat" in block
    # 两边那句一个字不差
    assert PERM_LINE in _block(MAC, "MENU-SCREEN")


def test_windows_online_publish_does_not_promise_menu_unconditionally():
    win = _read(WIN)
    online = win[win.index("'online'    {"):win.index("'review'    {")]
    assert "if ($menuState -eq 'ok')" in online
    assert online.index(PROMISE_MENU) < online.index("else"), "许诺菜单那句跑到 else 里去了"
    assert PROMISE_MENU not in online[online.index("else"):]


# ---------- doctor [5b]（Mac 真跑，假 urlopen）----------


_FAKE_SITE = r'''
import io, json, os, urllib.error, urllib.request

_ROUTES = json.loads(os.environ["MENU_FAKE_ROUTES"])
_LOG = os.environ["MENU_FAKE_LOG"]


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake(req, timeout=None):
    url = req.full_url
    with open(_LOG, "a") as fh:
        fh.write(req.get_method() + " " + url + "\n")
    for prefix, (status, body) in _ROUTES.items():
        if prefix in url:
            raw = json.dumps(body).encode()
            if status != 200:
                raise urllib.error.HTTPError(url, status, "x", {}, io.BytesIO(raw))
            return _Resp(raw)
    raise urllib.error.URLError("no fake route for " + url)


urllib.request.urlopen = _fake
'''

_DENIED = (400, {"code": 99991672, "msg": "Access denied", "error": {"permission_violations": [
    {"subject": "application:application.app_version:readonly"}]}})
_TOKEN = {"/auth/v3/tenant_access_token/internal": (200, {"code": 0, "tenant_access_token": "t-fake"})}


def _versions(*items):
    return {"/app_versions": (200, {"code": 0, "data": {"items": list(items)}})}


def _scopes(granted: bool):
    return {"/application/v6/scopes": (200, {"code": 0, "data": {"scopes": [
        {"scope_name": "application:application:patch", "scope_type": "tenant",
         "grant_status": 1 if granted else 0}]}})}


MENU = {"bot_menu_enable": True, "bot_menus": [{"menu_id": f"m{i}"} for i in range(9)]}


def _run_doctor(here_dir, routes: dict, block_path: str = DOC_MAC) -> tuple[str, list[str]]:
    site = here_dir / "site"
    site.mkdir(exist_ok=True)
    (site / "sitecustomize.py").write_text(_FAKE_SITE, encoding="utf-8")
    venv = here_dir / "venv"
    (venv / "bin").mkdir(parents=True, exist_ok=True)
    py = venv / "bin" / "python"
    py.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
    py.chmod(0o755)
    config = here_dir / "config.toml"
    config.write_text(f'[feishu]\napp_id = "{APP}"\napp_secret = "secret-not-real"\n', encoding="utf-8")
    log = here_dir / "calls.log"
    log.write_text("", encoding="utf-8")
    out = _bash(f'CONFIG="{config}"\nVENV="{venv}"\n' + _block(block_path, "MENU-CHECK"), {
        "PYTHONPATH": str(site), "MENU_FAKE_ROUTES": json.dumps(routes), "MENU_FAKE_LOG": str(log)})
    return out, log.read_text(encoding="utf-8").splitlines()


def _assert_read_only(calls: list[str]):
    assert calls, "一个请求都没发 ⇒ 这一项根本没查"
    for line in calls:
        method, url = line.split(" ", 1)
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            assert method == "POST"
        else:
            assert method == "GET", f"doctor 发了写请求：{line}"


def test_doctor_live_version_with_menu_is_green(here_dir):
    routes = {**_TOKEN, **_versions(
        {"version": "1.0.1", "status": 1, "publish_time": "100", "ability": {"bot": {}}},
        {"version": "1.0.2", "status": 1, "publish_time": "200", "ability": {"bot": MENU}},
        {"version": "1.0.3", "status": 3, "publish_time": "300", "ability": {"bot": {}}},
    )}
    out, calls = _run_doctor(here_dir, routes)
    assert out.splitlines()[1] == "OK 悬浮菜单在（线上版本 1.0.2，9 个按钮）", out
    _assert_read_only(calls)


def test_doctor_live_version_without_menu_is_red(here_dir):
    """同事那台的形状（如果读得到）：发了版，但版本里没有菜单。"""
    routes = {**_TOKEN, **_versions(
        {"version": "1.0.2", "status": 1, "publish_time": "200",
         "ability": {"bot": {"bot_menu_enable": False}}})}
    out, calls = _run_doctor(here_dir, routes)
    assert "BAD 线上版本里没有悬浮菜单（版本 1.0.2）" in out, out
    assert "重新双击 feishu-agent-mac.command" in out
    _assert_read_only(calls)


def test_doctor_cannot_read_and_patch_scope_not_granted_is_yellow_wait_for_admin(here_dir):
    routes = {**_TOKEN, "/app_versions": _DENIED, **_scopes(granted=False)}
    out, calls = _run_doctor(here_dir, routes)
    assert "WARN 悬浮菜单还写不进去：缺「application:application:patch」权限，等管理员批" in out, out
    assert "BAD" not in out
    _assert_read_only(calls)


def test_doctor_cannot_read_but_scope_granted_is_yellow_unknown(here_dir):
    """本机现状（2026-11-06 只读实测）：版本接口 99991672、patch 已批 ⇒ 看不出来，叫人看一眼。"""
    routes = {**_TOKEN, "/app_versions": _DENIED, **_scopes(granted=True)}
    out, calls = _run_doctor(here_dir, routes)
    assert "WARN 看不出悬浮菜单在不在" in out, out
    assert "BAD" not in out
    _assert_read_only(calls)


def test_doctor_bad_credentials_is_yellow_and_points_to_5(here_dir):
    routes = {"/auth/v3/tenant_access_token/internal": (200, {"code": 10003, "msg": "invalid"})}
    out, calls = _run_doctor(here_dir, routes)
    assert "WARN 查不了悬浮菜单（token code=10003）" in out, out
    assert len(calls) == 1


def test_doctor_python_is_ascii_and_identical_on_windows():
    """Windows 那份跑的是同一段 Python：逐字一致，且只用 ASCII（PS 5.1 管道会把非 ASCII 变 ?）。"""
    mac = _block(DOC_MAC, "MENU-CHECK")
    mac_py = mac[mac.index("<<'PY'\n") + len("<<'PY'\n"):mac.index("\nPY\n)")]
    win = _read(DOC_WIN)
    win_py = win[win.index("$menuPy = @'\n") + len("$menuPy = @'\n"):win.index("\n'@\n")]
    assert mac_py == win_py
    assert mac_py.isascii()
    methods = re.findall(r'call\("(\w+)", "([^"]+)"', mac_py)
    assert methods, "正则过时了 —— 一个 call 都没抓到"
    for method, path in methods:
        assert method == "GET" or (method, path) == ("POST", "/auth/v3/tenant_access_token/internal"), (
            method, path)


def test_call_site_doctors_run_the_check_between_credentials_and_owners():
    mac = _read(DOC_MAC)
    assert mac.index('say "[5] 机器人凭据"') < mac.index('say "[5b] 悬浮菜单') < mac.index('say "[6] 授权用户"')
    assert 'MENU_CHECK="$("$VENV/bin/python" - "$CONFIG" 2>/dev/null <<\'PY\'' in _block(DOC_MAC, "MENU-CHECK")
    win = _read(DOC_WIN)
    assert win.index('Say "[5] 机器人凭据"') < win.index('Say "[5b] 悬浮菜单') < win.index('Say "[6] 授权用户"')
    assert "& $venvPy $menuPyFile $config 2>$null" in win
    for state in ("'ok'", "'missing'", "'noperm'", "'unreadable'", "default"):
        assert f"    {state} {{" in win, f"Windows doctor 没处理 {state}"
    # missing 报红（计入失败项）、noperm / unreadable 只是黄
    switch = win[win.index("switch ($menuKind)"):]
    assert switch[switch.index("'missing'"):switch.index("'noperm'")].count("Bad ") == 1
    assert "Bad " not in switch[switch.index("'noperm'"):switch.index("default")]
