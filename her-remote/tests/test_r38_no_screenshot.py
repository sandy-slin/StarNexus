"""第 38 轮 · X「不许让用户截图」（主人定的规矩）。

① 给用户看的提示里不许叫人截图，也不许写「已自动发给管理员 / 已自动上报」。
   🔴 11-19 主人改口径：「不用截图，我们这边会跟进」「我们这边已经收到了」这类**也算禁句** ——
   会让人以为我们在收集他的个人信息，用户会紧张。统一改成「这是程序自身的问题，不是你操作的问题」。
② 上报本身照做（不因为屏幕不说了就停）：install.sh / install.ps1（一行命令）、两个 doctor、
   botmenu 210021（安装器里走 REPORT= 行，桥里走 errreport 收的 ERROR 日志）。
扫描范围：feishu-agent-oneclick/src/*、build.sh 的 bat 头、bridge/**/*.py、scripts/*（都会进安装包）。
注释、docstring、tests、docs 不算。上报只打本机假服务（127.0.0.1），绝不出网。
"""
from __future__ import annotations

import ast
import http.server
import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
SRC = REPO / "feishu-agent-oneclick" / "src"
BUILD = REPO / "feishu-agent-oneclick" / "build.sh"
BRIDGE = ROOT / "bridge"
SCRIPTS = ROOT / "scripts"

BANNED = ("已自动发给管理员", "自动发给管理员", "已自动上报", "不用截图",
          # 🔴 11-19：写「会跟进」和写「跟进」是同一句承诺，宽词才拦得住
          #（第一版只写了「我们这边会跟进」，preflight.py 里两句「我们这边跟进」就漏了，是别条会话指出的）
          "我们这边会跟进", "我们这边跟进", "我们这边已经收到", "会自动收到",
          "发回来")


# ---------- 扫描器 ----------
def shell_screen_lines(text: str) -> list[tuple[int, str]]:
    """sh / ps1：整行注释不算，行尾注释（空白 + # + 空白）剥掉，ps1 的 <# … #> 块注释不算。"""
    text = re.sub(r"<#.*?#>", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append((no, re.split(r"\s#\s", line, maxsplit=1)[0]))
    return out


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def py_screen_strings(text: str) -> list[tuple[int, str]]:
    """Python：所有字符串常量（含 f-string 片段），docstring 除外；注释本来就不在 AST 里。"""
    tree = ast.parse(text)
    skip = _docstring_nodes(tree)
    return [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in skip]


def violations(pieces: list[tuple[int, str]]) -> list[tuple[int, str]]:
    bad = []
    for no, s in pieces:
        if "截图" in s or any(b in s for b in BANNED):
            bad.append((no, s.strip()[:120]))
    return bad


def scan_text(name: str, text: str) -> list[tuple[int, str]]:
    if name.endswith(".py"):
        return violations(py_screen_strings(text))
    return violations(shell_screen_lines(text))


def shipped_files() -> list[Path]:
    files = [p for p in SRC.iterdir() if p.is_file() and p.suffix in (".sh", ".ps1")]
    files += [p for p in BRIDGE.rglob("*.py") if "__pycache__" not in p.parts]
    files += [p for p in SCRIPTS.iterdir() if p.is_file() and p.suffix in (".py", ".sh")]
    return sorted(files)


# ---------- ① 现在一处都没有 ----------
@pytest.mark.parametrize("path", shipped_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_no_screenshot_or_auto_report_wording(path):
    hits = scan_text(path.name, path.read_text(encoding="utf-8-sig"))
    assert not hits, f"{path.name} 还在叫用户截图 / 说已自动上报：{hits}"


def test_bat_header_does_not_ask_for_a_screenshot():
    """build.sh 的 bat 头（纯 ASCII 英文）原来是 send this screen to IT。"""
    head = BUILD.read_text(encoding="utf-8")
    head = head[head.index("HEAD = ("):head.index("##CARHER-PS1\\r\\n")]
    assert not re.search(r"(?i)screenshot|send this screen", head)
    assert "Zhao Hongtao" in head, "要人放行的那句得说清找谁"
    assert head.isascii()


def test_scan_covers_the_files_the_patrol_named():
    names = {p.name for p in shipped_files()}
    assert {"install.sh", "install.ps1", "install-mac.sh", "install-windows.ps1",
            "doctor-mac.sh", "doctor-windows.ps1", "botmenu.py", "errreport.py"} <= names


# ---------- 阳性对照 / 变异：扫描器真能抓 ----------
def test_scanner_teeth_on_synthetic_lines():
    sh = "\n".join([
        '  die "✗ 安装失败，把这个窗口截图发给 IT"',     # 抓
        '  # 注释里的截图不算',                          # 不抓
        '  echo "不用截图，我们这边会跟进"',             # 抓（11-19 起算禁句）
        '  warn "已自动上报"   # 尾注释',               # 抓（BANNED）
        '  ok "x"   # 尾注释里的截图不算',               # 不抓
    ])
    assert [no for no, _ in scan_text("a.sh", sh)] == [1, 3, 4]
    ps = '<#\n 块注释里的截图\n#>\nWrite-Host "整屏截图发给管理员"\n'
    assert [no for no, _ in scan_text("a.ps1", ps)] == [4]
    py = '"""docstring 里的截图不算"""\nX = {1: "把这句截图发给同事"}\nY = f"{1} 已自动发给管理员"\n# 注释截图\n'
    assert [no for no, _ in scan_text("a.py", py)] == [2, 3]


MUTATIONS = [
    # (文件, 把哪句改回违规的样子)
    (SRC / "doctor-mac.sh", "还这样就把安装包重跑一次", "还这样就把这个窗口截图发给管理员"),
    (SRC / "doctor-windows.ps1", "换了网络还这样就把安装包重跑一次", "换了网络还这样，就把这个窗口截图发给管理员"),
    (SRC / "install.sh", "（期望 ${want:0:12}", "截图发给 IT（期望 ${want:0:12}"),
    (SRC / "install-windows.ps1", "  3. 还不行就再重跑一次安装包\n", "  3. 还不行就把这个窗口截图，贴到网页「提交反馈」\n"),
    (BRIDGE / "botmenu.py", "写不了它的菜单", "写不了它的菜单，把这句截图发给给你装的同事"),
    (SRC / "install.ps1", "重跑一次这条命令;这是程序自身的问题", "重跑一次这条命令;这次的报错已自动发给管理员"),
]


@pytest.mark.parametrize("path,good,bad", MUTATIONS, ids=lambda v: v.name if isinstance(v, Path) else None)
def test_scanner_catches_mutation_of_real_file(path, good, bad):
    text = path.read_text(encoding="utf-8-sig")
    assert good in text, f"变异锚点不在 {path.name} 里了（改了文案就同步这里）"
    assert not scan_text(path.name, text)
    assert scan_text(path.name, text.replace(good, bad, 1)), f"{path.name} 注入违规文案没被抓到"


# ---------- ② 说了「我们这边会跟进」⇒ 真报上来 ----------
class _Sink:
    def __init__(self):
        self.bodies: list[dict] = []
        sink = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                sink.bodies.append(json.loads(raw))   # 不是合法 JSON 就让测试红
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def do_GET(self):  # noqa: N802
                body = (sink.root / self.path.lstrip("/")).read_bytes() if (sink.root / self.path.lstrip("/")).is_file() else None
                self.send_response(200 if body is not None else 404)
                self.end_headers()
                if body is not None:
                    self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.root = Path("/nonexistent")
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def sink(tmp_path):
    s = _Sink()
    s.root = tmp_path / "dl"
    s.root.mkdir()
    yield s
    s.close()


def _env(tmp_path, sink, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": str(tmp_path),
           "LANG": "en_US.UTF-8", "CARHER_REPORT_URL": sink.url + "/ai/api/install-report",
           "CARHER_NO_REPORT": "0"}
    env.update(extra)
    return env, home


def test_oneliner_bad_latest_json_reports_and_says_no_screenshot(tmp_path, sink):
    (sink.root / "latest.json").write_text('{"build_id":"b1","mac":{"file":"../evil.command","sha256":"ab"}}')
    env, home = _env(tmp_path, sink, CARHER_FEISHU_AGENT_DL_BASE=sink.url)
    r = subprocess.run(["/bin/bash", str(SRC / "install.sh")], capture_output=True, text=True, env=env,
                       timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 1
    assert "这是程序自身的问题，不是你操作的问题" in r.stderr and "重跑" in r.stderr
    assert len(sink.bodies) == 1, "说了「我们这边会跟进」却没报上来"
    body = sink.bodies[0]
    assert body["tool"] == "her-remote-oneliner" and "读不懂" in body["msg"]
    assert "查最新版本" in body["logs"]["screen.txt"] and "evil.command" in body["logs"]["diag.txt"]
    assert "host" not in body and "user" not in body and str(home) not in json.dumps(body, ensure_ascii=False)


def test_oneliner_no_report_switch_sends_nothing(tmp_path, sink):
    (sink.root / "latest.json").write_text('{"build_id":"b1","mac":{"file":"../evil.command","sha256":"ab"}}')
    env, _ = _env(tmp_path, sink, CARHER_FEISHU_AGENT_DL_BASE=sink.url, CARHER_NO_REPORT="1")
    r = subprocess.run(["/bin/bash", str(SRC / "install.sh")], capture_output=True, text=True, env=env,
                       timeout=60, stdin=subprocess.DEVNULL)
    assert r.returncode == 1 and sink.bodies == []


def test_doctor_mac_reports_red_screen_without_secrets(tmp_path, sink):
    env, home = _env(tmp_path, sink)
    app = home / ".carher-feishu-agent"
    (app / "logs").mkdir(parents=True)
    (app / "install_id").write_text("abcdef0123456789")
    (app / "config.toml").write_text('[feishu]\napp_id = "cli_a1b2c3d4e5f60718"\n'
                                     'app_secret = "SECRET-NOT-REAL-xyz"\nowner_open_ids = []\n')
    # 🔴 绝不出网：doctor 自己会 curl open.feishu.cn / latest.json ⇒ PATH 最前面放一个只放行 127.0.0.1 的 curl
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "curl").write_text('#!/bin/bash\nfor a in "$@"; do case "$a" in http://127.0.0.1:*) exec /usr/bin/curl "$@";; esac; done\nexit 7\n')
    (fake / "curl").chmod(0o755)
    env["PATH"] = f"{fake}:{env['PATH']}"
    r = subprocess.run(["/bin/bash", str(SRC / "doctor-mac.sh")], capture_output=True, text=True, env=env,
                       timeout=120, stdin=subprocess.DEVNULL)
    assert "这是程序自身的问题，不是你操作的问题" in r.stdout and "截图发给" not in r.stdout
    assert len(sink.bodies) == 1
    body = sink.bodies[0]
    dumped = json.dumps(body, ensure_ascii=False)
    assert body["tool"] == "her-remote-doctor" and "项红" in body["msg"]
    assert "install_id=abcdef0123456789" in body["logs"]["diag.txt"]
    assert "SECRET-NOT-REAL" not in dumped and "cli_a1b2c3d4e5f60718" not in dumped and str(home) not in dumped


def test_doctor_windows_reports_only_when_red_or_promised():
    text = (SRC / "doctor-windows.ps1").read_text(encoding="utf-8-sig")
    assert "if ($script:failed -ne 0 -or $script:needReport) { Send-DoctorReport }" in text
    # 每一句「我们这边会跟进」所在行都要置 needReport（不然没红项时不报）
    for no, line in shell_screen_lines(text):
        if "这是程序自身的问题" in line and "Hint" in line:
            assert "$script:needReport = $true" in line, f"doctor-windows.ps1:{no} 说了跟进却不报"
    mac = (SRC / "doctor-mac.sh").read_text(encoding="utf-8")
    for no, line in shell_screen_lines(mac):
        if "这是程序自身的问题" in line and "hint" in line:
            assert "NEED_REPORT=1" in line, f"doctor-mac.sh:{no} 说了跟进却不报"


# ---------- botmenu 210021 ----------
def test_210021_wording_and_report_line():
    from bridge import botmenu

    human = botmenu._human({"code": 210021, "msg": "raw"}, botmenu.CONFIG_ERRORS)
    assert "开发者后台" in human and "不用截图" not in human and "截图发给" not in human
    calls = []

    def fake(method, path, token, body=None):
        calls.append(path)
        if path.startswith("/auth/"):
            return {"code": 0, "tenant_access_token": "t"}
        if path.endswith("/ability"):
            return {"code": 210021, "msg": "app not created by console"}
        if path.endswith("/publish"):
            return {"code": 0, "data": {"version": "1", "version_id": ""}}
        return {"code": 0, "data": {}}

    lines = botmenu.setup("cli_test_only_000", "s", "1.0.0", http=fake)
    assert any(line.startswith("REPORT=常驻菜单没配上 code=210021") for line in lines), lines
    # 别的码不报（例如缺权限 = 等管理员审批，是预期）
    assert botmenu.report_code({"code": 210020}) is None and botmenu.report_code({"code": "x"}) is None


def test_installers_forward_report_lines():
    mac = (SRC / "install-mac.sh").read_text(encoding="utf-8")
    seg = mac[mac.index("# MENU-VERDICT-BEGIN"):mac.index("# MENU-VERDICT-END")]
    script = ('set -uo pipefail\nok(){ :; }\nwarn(){ :; }\nsay(){ :; }\nLOG_DIR=/nonexistent\n'
              'carher_fail_report(){ printf "REPORTED %s|%s\\n" "$1" "$2"; }\n'
              'MENU_OUT="WARN=常驻菜单没配上：x\nREPORT=常驻菜单没配上 code=210021 m\nPUBLISH=failed"\n' + seg)
    out = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=30).stdout
    assert "REPORTED her-remote|✗ 常驻菜单没配上 code=210021 m" in out, out
    win = (SRC / "install-windows.ps1").read_text(encoding="utf-8-sig")
    assert "elseif ($s.StartsWith('REPORT=')) { Send-CarherFailReport 'her-remote'" in win


def test_menu_heal_210021_logs_an_error_errreport_will_send(tmp_path, caplog):
    """桥里自愈撞上 210021 ⇒ ERROR + 源码常量模板 ⇒ errreport 发的是模板本身（不是 <动态消息>）。"""
    from bridge import errreport, menu_heal

    def fake(method, path, token, body=None):
        if path.startswith("/auth/"):
            return {"code": 0, "tenant_access_token": "t"}
        if path.endswith("/ability"):
            return {"code": 210021, "msg": "raw"}
        return {"code": 0, "data": {}}

    heal = menu_heal.MenuHeal("cli_test_only_000", "s", str(tmp_path / "menu.json"), http=fake,
                              clock=lambda: 1_800_000_000.0)
    with caplog.at_level(logging.WARNING, logger="feishu-bridge"):
        assert heal.attempt() is False
    errs = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errs) == 1, [r.getMessage() for r in caplog.records]
    item = errreport.item_from_record(errs[0])
    assert item["msg"] == errs[0].msg and "210021" not in item["msg"] and item["msg"] != "<动态消息>"


def test_menu_heal_missing_scope_is_not_reported(tmp_path, caplog):
    """对照：缺权限（等管理员审批）是预期，只 WARNING，不进 errreport。"""
    from bridge import menu_heal

    def fake(method, path, token, body=None):
        if path.startswith("/auth/"):
            return {"code": 0, "tenant_access_token": "t"}
        if path.endswith("/ability"):
            return {"code": 99991672, "msg": "denied",
                    "error": {"permission_violations": [{"subject": "application:application:patch"}]}}
        return {"code": 0, "data": {}}

    heal = menu_heal.MenuHeal("cli_test_only_000", "s", str(tmp_path / "menu.json"), http=fake,
                              clock=lambda: 1_800_000_000.0)
    with caplog.at_level(logging.WARNING, logger="feishu-bridge"):
        heal.attempt()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_admin_who_rule_in_oneliner_ps1():
    """要别人动手的那句（公司网络放行）按 CARHER_ADMIN_WHO 说找谁，没有就两句一起说。"""
    text = (SRC / "install.ps1").read_text(encoding="utf-8")
    assert "IT 管理员(无锡车联的同事找赵宏涛)" in text and "请${who}放行 $base" in text


@pytest.mark.skipif(not os.access(REPO / ".work/ai-portal/installer/pwsh/pwsh", os.X_OK), reason="本机没有 pwsh")
def test_oneliner_ps1_reports_via_pwsh(tmp_path, sink):
    (sink.root / "latest.json").write_text('{"build_id":"b1","windows":{"file":"../evil.bat","sha256":"ab"}}')
    env, home = _env(tmp_path, sink, CARHER_FEISHU_AGENT_DL_BASE=sink.url, CARHER_FEISHU_AGENT_ALLOW_ANY_OS="1",
                     USERPROFILE=str(tmp_path / "home"))
    r = subprocess.run([str(REPO / ".work/ai-portal/installer/pwsh/pwsh"), "-NoProfile", "-c",
                        f". '{SRC / 'install.ps1'}'; \"RC=$global:CARHER_HER_RC\""],
                       capture_output=True, text=True, env=env, timeout=120)
    assert "RC=1" in r.stdout and "这是程序自身的问题" in r.stdout, r.stdout + r.stderr
    assert len(sink.bodies) == 1 and sink.bodies[0]["tool"] == "her-remote-oneliner"
    assert "查最新版本" in sink.bodies[0]["logs"]["screen.txt"]


# ---------- ③ 我们自己的故障，不许让用户去找 IT ----------
#
# 11-18 102940-herremote-dc83a5（Win 26300）：安装器报「它装上了，但在反复重启」，文案第 2 条写的是
# 「把诊断结果发给 IT，告诉他们『服务在反复重启』」—— 程序在这台机器上待不住是**我们的故障**，
# 让用户去找 IT 等于把我们的活推给他，而且 IT 拿到诊断也看不出什么。
#
# 分两类（这条闸门只禁后一类）：
#   ① 要权限：「把这句话发给 IT：『需要允许…』」⇒ 留着，这是真要人放行（同主人定的「请 X 放行」）。
#   ② 我们的故障：诊断结果 / 日志 / 错误码 / 「这是程序的问题」往 IT 那儿递 ⇒ 禁。自动上报已经在做了。
OUR_FAULT_TO_IT = (
    "诊断结果一起发给 IT", "诊断结果发给 IT", "诊断发给 IT", "诊断，连这个错误码一起发给 IT",
    "日志一起发给 IT", "install-pip.log 发给 IT", "程序的问题，把这句发给 IT",
    "发给 IT，告诉他们",
)


def our_fault_hits(pieces: list[tuple[int, str]]) -> list[tuple[int, str]]:
    return [(no, s.strip()[:120]) for no, s in pieces
            if any(b in s for b in OUR_FAULT_TO_IT)]


def scan_our_fault(name: str, text: str) -> list[tuple[int, str]]:
    if name.endswith(".py"):
        return our_fault_hits(py_screen_strings(text))
    return our_fault_hits(shell_screen_lines(text))


@pytest.mark.parametrize("path", shipped_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_our_own_failures_do_not_send_the_user_to_it(path):
    hits = scan_our_fault(path.name, path.read_text(encoding="utf-8-sig"))
    assert not hits, f"{path.name} 把我们自己的故障推给 IT 了（自动上报已经在做）：{hits}"


def test_asking_it_for_a_permission_is_still_allowed():
    """阳性对照：要权限那一类不许被这条闸门带走 —— 带走了就没人告诉用户该找谁放行。"""
    keep = [
        '  1. 把这句话发给 IT：「需要允许 PowerShell 启动 $Exe」',
        '     「需要放行 https://chat.auto-link.com.cn/ccdl/」',
    ]
    assert scan_our_fault("a.ps1", "\n".join(keep)) == []
    # 而「我们的故障」那一类要被抓住
    bad = '  2. 把诊断结果发给 IT，告诉他们「服务在反复重启」'
    assert [no for no, _ in scan_our_fault("a.ps1", bad)] == [1]


def test_the_three_restart_verdicts_say_we_follow_up():
    """三条「装上了但待不住」的结论:11-19 起屏幕上不再说「我们这边已经收到了」,但仍不许把用户推给 IT。"""
    win = (SRC / "install-windows.ps1").read_text(encoding="utf-8-sig")
    for anchor in ("X 它装上了，但一启动就崩了", "X 它装上了，但在反复重启",
                   "X 后台服务没能留在运行状态"):
        i = win.index(anchor)
        block = win[i:i + 600]
        assert "我们这边已经收到" not in block, f"{anchor} 还在说「我们收到了」：{block[:200]}"
        assert "发给 IT" not in block, f"{anchor} 还在让用户找 IT"
    mac = (SRC / "install-mac.sh").read_text(encoding="utf-8")
    for anchor in ("✗ 后台服务没能留在运行状态", "✗ 它装上了，但在反复重启"):
        i = mac.index(anchor)
        block = mac[i:i + 600]
        assert "我们这边已经收到" not in block, f"mac {anchor} 还在说「我们收到了」"
        assert "发给 IT" not in block, f"mac {anchor} 还在让用户找 IT"


# ---------- ④ 诊断里「每一类报错各多少次」必须真的发回来 ----------
#
# 11-18 115035-herremotedoc-a75cb3：上报里只有「! 日志里有 3866 条报错。最近这些属于：」这一句，
# 下面「连不上飞书 N 次 / 长连接断开 N 次」一行都没有 —— 那几行当时是 printf / Write-Host 直接打的，
# 没进 SCREEN；Mac 侧更糟：循环体在管道的子 shell 里，就算 note 也带不出来。
# 这几行恰恰是我们定位最需要的（总数说明不了是哪一类）。

def test_doctor_mac_sends_the_per_class_error_counts(tmp_path, sink):
    """真跑一遍 doctor-mac.sh：造一份带两类报错的 agent.log，上报里必须有这两类各多少次。"""
    env, home = _env(tmp_path, sink)
    app = home / ".carher-feishu-agent"
    (app / "logs").mkdir(parents=True)
    (app / "install_id").write_text("abcdef0123456789")
    (app / "config.toml").write_text('[feishu]\napp_id = "cli_a1b2c3d4e5f60718"\n'
                                     'app_secret = "SECRET-NOT-REAL-xyz"\nowner_open_ids = []\n')
    log = app / "logs" / "agent.log"
    lines = []
    for _ in range(3):
        lines.append("2026-11-18 09:00:00 ERROR Lark connect failed, err: timeout")
    for _ in range(2):
        lines.append("2026-11-18 09:01:00 ERROR Lark receive message loop exit")
    log.write_text("\n".join(lines) + "\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "curl").write_text('#!/bin/bash\nfor a in "$@"; do case "$a" in http://127.0.0.1:*) exec /usr/bin/curl "$@";; esac; done\nexit 7\n')
    (fake / "curl").chmod(0o755)
    env["PATH"] = f"{fake}:{env['PATH']}"
    r = subprocess.run(["/bin/bash", str(SRC / "doctor-mac.sh")], capture_output=True, text=True, env=env,
                       timeout=120, stdin=subprocess.DEVNULL)
    assert "连不上飞书 3 次" in r.stdout, r.stdout[-2000:]
    assert len(sink.bodies) == 1
    screen = sink.bodies[0]["logs"]["screen.txt"]
    # 🔴 判据就是「上报那一份里有没有」——屏幕上有、上报里没有，正是 11-18 那条的形状
    assert "连不上飞书 3 次" in screen, f"每一类的次数没进上报：{screen}"
    assert "长连接断开 2 次" in screen, f"只带了一类：{screen}"
    # 只带类别名和次数，不带日志原文
    assert "err: timeout" not in screen and "Lark" not in screen, screen


def test_both_doctors_record_detail_lines_into_the_report():
    """源码侧：明细行必须走 note/Note（它同时记进上报），不许再用裸 printf / Write-Host。"""
    win = (SRC / "doctor-windows.ps1").read_text(encoding="utf-8-sig")
    assert "function Note($m)" in win and "$script:screen.AppendLine" in win
    i = win.index('Warn "日志里有 $($errs.Count) 条报错')
    seg = win[i:i + 700]
    assert "Note " in seg, f"Windows 侧明细行没走 Note：{seg[:300]}"
    assert "Write-Host \"        - " not in seg, "Windows 侧还在用裸 Write-Host 打明细"
    mac = (SRC / "doctor-mac.sh").read_text(encoding="utf-8")
    assert "note() {" in mac and 'SCREEN="${SCREEN:-}    $1"' in mac
    j = mac.index('warn "日志里有 ${ERRS} 条报错')
    seg = mac[j:j + 900]
    assert "note " in seg, f"Mac 侧明细行没走 note：{seg[:300]}"
    # 🔴 这一段不许再在管道里循环：子 shell 的 SCREEN 带不出来（11-18 的第二个坑）
    assert "| while IFS=" not in seg, "Mac 侧明细循环又回到管道里了 ⇒ note 攒的东西会丢"
    assert "done <<" in seg, "Mac 侧明细循环要用 here-doc 喂（循环体留在当前 shell）"
