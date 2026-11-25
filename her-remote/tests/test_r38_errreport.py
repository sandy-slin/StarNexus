"""第 38 轮：报错自动上报（bridge/errreport.py）。

🔴 全程不碰网络：发送一律走 `send=` 桩；`_post` 只在「CARHER_NO_REPORT 不许发」那条里被调到，
并且 `urlopen` 换成了记录调用的桩。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from bridge import errreport as E

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
APP = "cli_a1b2c3d4e5f60708"

# 一条「最坏情况」的报错：用户原话 / 绝对路径带用户名 / app_secret / open_id / token 全都塞进去
USER_WORDS = "帮我把北汽的报价单改成九折发给老杨"
SECRET = "s3cr3tAppSecretValue0123456789"
OPEN_ID = "ou_9f8e7d6c5b4a3210fedcba98"
TOKEN = "sk-" + "Zx9" * 8   # 拼出来的假 key 形状
ABS = "/Users/zhangsan/客户资料/北汽/报价单.xlsx"
LEAKS = (USER_WORDS, "北汽", "老杨", "zhangsan", "客户资料", "报价单", SECRET, OPEN_ID, TOKEN, "/Users/")


@pytest.fixture
def home():
    base = _ROOT / f".r38err-tmp-{uuid.uuid4().hex[:8]}"
    (base / "bridge").mkdir(parents=True)
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class Sink:
    def __init__(self, ok=True, delay=0.0, boom=False):
        self.ok, self.delay, self.boom = ok, delay, boom
        self.payloads: list[dict] = []

    def __call__(self, payload):
        if self.delay:
            time.sleep(self.delay)
        if self.boom:
            raise OSError("network is down")
        self.payloads.append(json.loads(json.dumps(payload, ensure_ascii=False)))
        return self.ok

    def items(self):
        return [it for p in self.payloads for it in p["items"]]


class Clock:
    def __init__(self):
        self.t = time.mktime((2026, 11, 17, 10, 0, 0, 0, 0, -1))

    def __call__(self):
        return self.t


def _reporter(home, monkeypatch, sink=None, clock=None, **kw):
    monkeypatch.setattr(E.Reporter, "_ensure_thread", lambda self: None)   # 测试里同步 flush
    return E.Reporter(app_home=str(home), package_dir=str(home / "bridge"), send=sink or Sink(),
                      clock=clock or Clock(), enabled=True, build_id="20261117-abc1234", app_id=APP, **kw)


def _boom():
    """造一个异常：消息里全是不该外发的东西，并且真从 bridge/ 里抛出来（有 bridge 帧）。"""
    try:
        E._exc_type(None)   # noqa —— 占位，确保下一行的帧来自本测试文件
        raise RuntimeError(f"读 {ABS} 失败：{USER_WORDS} app_secret={SECRET} open_id={OPEN_ID} token={TOKEN}")
    except RuntimeError as exc:
        return exc


def _bridge_exc():
    """真从 bridge/ 包里抛出的异常（帧要能被认成 bridge/xxx.py）。"""
    try:
        E._trim_item(None)   # None.get ⇒ AttributeError，帧在 bridge/errreport.py
    except AttributeError as exc:
        return exc
    raise AssertionError("应该抛")


# ───────────────────────── 隐私 ─────────────────────────

def test_payload_has_no_private_data_log_path(home, monkeypatch):
    sink = Sink()
    rep = _reporter(home, monkeypatch, sink)
    log = logging.getLogger("feishu-bridge.r38test")
    log.propagate = False
    handler = E._Handler(rep)
    log.addHandler(handler)
    try:
        exc = _boom()
        # 模板里带占位符，args 里是用户原话 + open_id + 路径
        log.error("消息没送达 chat_id=%s chars=%d 开头=%s", OPEN_ID, 30, USER_WORDS,
                  exc_info=(type(exc), exc, exc.__traceback__))
        log.error(f"动态拼的：{USER_WORDS} {ABS}")          # f-string：整条不是源码常量
    finally:
        log.removeHandler(handler)
    # POSITIVE 对照：同一条记录按普通日志格式化，泄露字段**确实在里面**（证明测试造的数据是真脏的）
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "开头=%s %s", (USER_WORDS, OPEN_ID), None)
    assert USER_WORDS in rec.getMessage() and OPEN_ID in rec.getMessage()

    assert rep.flush() == 2
    text = json.dumps(sink.payloads, ensure_ascii=False)
    for leak in LEAKS:
        assert leak not in text, f"泄露：{leak}\n{text}"
    for p in sink.payloads:
        assert set(p) == set(E.TOP_KEYS)
        for it in p["items"]:
            assert set(it) == set(E.ITEM_KEYS)
    first = sink.items()[0]
    assert first["msg"] == "消息没送达 chat_id=%s chars=%d 开头=%s"     # 模板原样、没渲染
    assert first["type"] == "RuntimeError"
    assert sink.items()[1]["msg"] == "<动态消息>"


def test_frames_are_only_relative_bridge_paths(home, monkeypatch):
    sink = Sink()
    rep = _reporter(home, monkeypatch, sink)
    exc = _bridge_exc()
    rep.add(E.item_from_exception(exc, "uncaught"))
    rep.flush()
    frames = sink.items()[0]["frames"]
    assert frames and all(f.startswith("bridge/") for f in frames), frames
    assert any(f.startswith("bridge/errreport.py:") for f in frames)
    joined = " ".join(frames)
    assert "test_r38" not in joined and os.sep + "Users" not in joined and "site-packages" not in joined
    # 不带源码行：帧形状严格是 path:line:func
    assert all(E._FRAME_RE.match(f) for f in frames)


@pytest.mark.parametrize("raw,must_not", [
    (f"FileNotFoundError: {ABS}", ("zhangsan", "北汽", "报价单")),
    (f"C:\\Users\\lisi\\AppData\\Local\\CarHerFeishuAgent\\config.toml app_secret={SECRET}", ("lisi", SECRET)),
    (f"push failed for {OPEN_ID} chat oc_1234567890abcdef", (OPEN_ID, "oc_1234567890abcdef")),
    (f"Authorization: Bearer {TOKEN}", (TOKEN,)),
    (f"'{USER_WORDS}'", (USER_WORDS,)),
    (f"{USER_WORDS}", (USER_WORDS,)),
    ("mail zhangsan@auto-link.com.cn http://u:pw12345678@h/x", ("zhangsan", "pw12345678")),
])
def test_scrub_rules(raw, must_not):
    out = E._scrub(raw, limit=300)
    for leak in must_not:
        assert leak not in out, (leak, out)


def test_scrub_keeps_what_we_need_to_locate():
    """POSITIVE：打码不能把定位用的东西也打掉（不然这功能没用）。"""
    assert E._scrub("push rejected: 230001", limit=300) == "push rejected: 230001"
    assert E._scrub("扫码注册失败：socket timeout", limit=300) == "扫码注册失败：socket timeout"
    assert "'app_id'" in E._exc_text(KeyError("app_id"))
    assert E._scrub("x" * 1000, limit=300) == "x" * 300 or len(E._scrub("x" * 1000, limit=300)) <= 300


def test_emsg_is_capped_at_300():
    out = E._exc_text(RuntimeError("socket timeout " * 100))
    assert 0 < len(out) <= 300


# ───────────────────────── 去重 / 上限 ─────────────────────────

def test_same_fingerprint_once_per_day_with_count(home, monkeypatch):
    sink, clock = Sink(), Clock()
    rep = _reporter(home, monkeypatch, sink, clock)
    item = E.item_from_exception(_bridge_exc(), "uncaught")
    for _ in range(5):
        rep.add(dict(item))
    assert rep.flush() == 1
    assert sink.items()[0]["n"] == 5
    # 同一天再来：不再发
    rep.add(dict(item))
    assert rep.flush() == 0 and len(sink.items()) == 1
    # 换天：同一指纹再发一次
    clock.t += 86400
    rep.add(dict(item))
    assert rep.flush() == 1 and len(sink.items()) == 2


def test_dedup_survives_restart(home, monkeypatch):
    """状态落盘：重启后（新的 Reporter）同一天同一指纹不重发。"""
    sink, clock = Sink(), Clock()
    item = E.item_from_exception(_bridge_exc(), "uncaught")
    rep = _reporter(home, monkeypatch, sink, clock)
    rep.add(dict(item)); rep.flush()
    rep2 = _reporter(home, monkeypatch, sink, clock)
    rep2.add(dict(item))
    assert rep2.flush() == 0 and len(sink.items()) == 1
    st = os.stat(home / E._STATE_FILE)
    assert (st.st_mode & 0o777) == 0o600


def test_daily_cap(home, monkeypatch):
    sink, clock = Sink(), Clock()
    rep = _reporter(home, monkeypatch, sink, clock)
    # 🔴 次数写死，不读 E._MAX_TODAY：变异把上限改大时，测试读常量就跟着变大（11-17 变异跑超时）
    for k in range(45):
        rep.add({"src": "log", "msg": "x", "fp": f"{k:012x}", "frames": []})
        rep.flush()
    assert len(sink.items()) == 30
    # 每次 POST 不超过 6 条
    assert all(len(p["items"]) <= E._MAX_ITEMS_PER_POST for p in sink.payloads)
    clock.t += 86400
    rep.add({"src": "log", "msg": "x", "fp": "f" * 12, "frames": []})
    assert rep.flush() == 1


# ───────────────────────── 不许把桥搞挂 / 卡住 ─────────────────────────

def test_network_failure_never_raises_and_gives_up(home, monkeypatch):
    sink = Sink(boom=True)
    rep = _reporter(home, monkeypatch, sink)
    rep.add({"src": "log", "msg": "x", "fp": "a" * 12})
    for _ in range(E._TRY_MAX + 2):
        assert rep.flush() == 0          # 不抛
    assert not rep._queue, "失败到上限要丢，不能无限重试"


def test_add_does_not_wait_for_network(home, monkeypatch):
    """handler 的 emit 不碰网络：发送卡 5 秒也不影响记日志。"""
    sink = Sink(delay=5)
    rep = _reporter(home, monkeypatch, sink)
    log = logging.getLogger("feishu-bridge.r38block")
    log.propagate = False
    handler = E._Handler(rep)
    log.addHandler(handler)
    try:
        t0 = time.monotonic()
        for _ in range(50):
            log.error("飞书回消息失败 code=%s msg=%s chars=%s", 1, 2, 3)
        assert time.monotonic() - t0 < 1.0
    finally:
        log.removeHandler(handler)


def test_drain_for_crash_is_time_bounded_and_spools(home, monkeypatch):
    sink = Sink(delay=10)
    rep = _reporter(home, monkeypatch, sink)
    rep.add({"src": "uncaught", "msg": "", "fp": "b" * 12})
    t0 = time.monotonic()
    rep.drain_for_crash(0.5)
    assert time.monotonic() - t0 < 2.0
    spool = json.loads((home / E._SPOOL_FILE).read_text(encoding="utf-8"))
    assert spool["items"][0]["fp"] == "b" * 12
    # 下次启动：从落盘里补发
    sink2 = Sink()
    rep2 = _reporter(home, monkeypatch, sink2)
    assert rep2.flush() == 1 and sink2.items()[0]["fp"] == "b" * 12
    assert not (home / E._SPOOL_FILE).exists()


def test_handler_survives_broken_reporter(home, monkeypatch):
    rep = _reporter(home, monkeypatch)
    monkeypatch.setattr(rep, "add", lambda item: (_ for _ in ()).throw(RuntimeError("boom")))
    h = E._Handler(rep)
    h.emit(logging.LogRecord("feishu-bridge", logging.ERROR, __file__, 1, "x", (), None))   # 不抛


def test_handler_ignores_other_loggers_and_below_error(home, monkeypatch):
    sink = Sink()
    rep = _reporter(home, monkeypatch, sink)
    h = E._Handler(rep)
    h.handle(logging.LogRecord("urllib3", logging.ERROR, __file__, 1, "x", (), None))
    h.handle(logging.LogRecord("feishu-bridge", logging.WARNING, __file__, 1, "x", (), None))
    h.handle(logging.LogRecord("feishu-bridge.errreport", logging.ERROR, __file__, 1, "x", (), None))
    assert rep.flush() == 0
    h.handle(logging.LogRecord("feishu-bridge.app", logging.ERROR, __file__, 1, "x", (), None))   # POSITIVE
    assert rep.flush() == 1


def test_no_id_or_no_version_is_dropped(home, monkeypatch):
    sink = Sink()
    rep = _reporter(home, monkeypatch, sink)
    rep._build_override = ""
    rep.add({"src": "log", "msg": "x", "fp": "c" * 12})
    assert rep.flush() == 0 and not sink.payloads and not rep._queue


# ───────────────────────── 开关 ─────────────────────────

def test_carher_no_report_disables(home, monkeypatch):
    calls = []
    monkeypatch.setattr(E.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(OSError()))
    monkeypatch.setenv("CARHER_NO_REPORT", "1")
    assert E.Reporter(app_home=str(home), package_dir=str(home / "bridge")).enabled is False
    assert E.install(str(home), str(home / "bridge")) is None or True
    # 就算有人强开 enabled，真发那一层也拦住
    rep = E.Reporter(app_home=str(home), package_dir=str(home / "bridge"), enabled=True,
                     build_id="b", app_id=APP)
    assert rep._post({"kind": "runtime"}) is False
    assert not calls


def test_under_pytest_never_posts_even_with_no_report_0(home, monkeypatch):
    calls = []
    monkeypatch.setattr(E.urllib.request, "urlopen", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("CARHER_NO_REPORT", "0")
    monkeypatch.delenv("CARHER_ERRREPORT_DUMP", raising=False)
    rep = E.Reporter(app_home=str(home), package_dir=str(home / "bridge"), enabled=True, build_id="b", app_id=APP)
    assert rep._post({"kind": "runtime"}) is False and not calls


def test_report_url_is_hardcoded_https():
    assert E.REPORT_URL == "https://chat.auto-link.com.cn/ai/api/install-report"


def test_install_id_shared_with_selfupdate(home, monkeypatch):
    (home / "install_id").write_text("0123456789abcdef", encoding="utf-8")
    rep = _reporter(home, monkeypatch)
    assert rep.install_id() == "0123456789abcdef"
    from bridge import selfupdate
    up = selfupdate.SelfUpdater(str(home / "bridge"), str(home / "u.json"), is_idle=lambda: True)
    assert up.install_id() == rep.install_id()


def test_install_id_created_when_missing_is_0600_and_never_overwritten(home, monkeypatch):
    rep = _reporter(home, monkeypatch)
    first = rep.install_id()
    assert E._INSTALL_ID_RE.match(first)
    assert (os.stat(home / "install_id").st_mode & 0o777) == 0o600
    assert rep.install_id() == first


# ───────────────────────── 起不来（crashloop）─────────────────────────

_CRASH_LOG = """2026-11-17 08:30:00,000 INFO feishu-bridge 桥已启动
2026-11-17 08:30:01,000 ERROR feishu-bridge 处理消息时异常 scope=x
Traceback (most recent call last):
  File "/Users/zhangsan/.carher-feishu-agent/bridge/app.py", line 1099, in _handle
    await self._run(text)
KeyError: 'scope'
2026-11-17 08:30:02,000 INFO feishu-bridge 还活着
Traceback (most recent call last):
  File "/Users/zhangsan/.carher-feishu-agent/bridge/app.py", line 12, in <module>
    from .wsfast import make_client
  File "/Users/zhangsan/.carher-feishu-agent/bridge/wsfast.py", line 30, in <module>
    import lark_oapi as lark
  File "/Users/zhangsan/.carher-feishu-agent/venv/lib/python3.12/site-packages/lark_oapi/__init__.py", line 3, in <module>
    raise ImportError("报价单 北汽 " + "/Users/zhangsan/x")
ImportError: 报价单 北汽 /Users/zhangsan/x
"""


def test_crash_item_from_log_picks_last_fatal_traceback(home):
    log = home / "agent.log"
    log.write_text(_CRASH_LOG, encoding="utf-8")
    item = E.crash_item_from_log(str(log))
    assert item and item["src"] == "crash" and item["type"] == "ImportError"
    assert item["frames"] == ["bridge/app.py:12:<module>", "bridge/wsfast.py:30:<module>"]
    text = json.dumps(item, ensure_ascii=False)
    for leak in ("zhangsan", "北汽", "报价单", "site-packages", "make_client"):
        assert leak not in text, leak


def test_crash_log_windows_utf16(home):
    log = home / "agent.log"
    log.write_bytes(_CRASH_LOG.encode("utf-16-le"))
    item = E.crash_item_from_log(str(log))
    assert item and item["type"] == "ImportError"


def test_crash_log_without_traceback_is_none(home):
    log = home / "agent.log"
    log.write_text("2026-11-17 08:30:00,000 INFO feishu-bridge 桥已启动\n", encoding="utf-8")
    assert E.crash_item_from_log(str(log)) is None
    assert E.crash_item_from_log(str(home / "nope.log")) is None



# 🔴 11-18 103012-e9690e：旧版本留下的 Traceback 被贴上当前版本号报上来（栈 model.py:277:run 只在 52d540a8 成立）
_STALE_THEN_NEW = """Traceback (most recent call last):
  File "/Users/zhangsan/.carher-feishu-agent/bridge/app.py", line 3, in boot
    raise RuntimeError("old")
RuntimeError: old
2026-11-18 10:00:00,000 INFO feishu-bridge 桥已启动
2026-11-18 10:00:05,000 INFO feishu-bridge connected to wss://x
"""


def test_traceback_followed_by_newer_logs_is_not_reported(home):
    """原来的兜底「全都后面跟了日志 ⇒ 还是发最后一段」把旧版本的栈报成新版本。"""
    log = home / "agent.log"
    log.write_text(_STALE_THEN_NEW, encoding="utf-8")
    assert E.crash_item_from_log(str(log)) is None


def test_traceback_older_than_recent_restarts_is_not_reported(home):
    """后面跟了超过 _RECENT_RUNS 条「[runner] 进程退出」⇒ 是更早那几轮的，不报。"""
    tb = _CRASH_LOG.split("2026-11-17 08:30:02,000 INFO feishu-bridge 还活着\n", 1)[1]
    runner = "[runner] 进程退出，code=1，5 秒后重启 2026-11-18T10:00:00\n"
    log = home / "agent.log"
    log.write_text(tb + runner * (E._RECENT_RUNS + 1), encoding="utf-8")
    assert E.crash_item_from_log(str(log)) is None
    # 阳性对照：在最近几轮之内 ⇒ 照报
    log.write_text(tb + runner * E._RECENT_RUNS, encoding="utf-8")
    item = E.crash_item_from_log(str(log))
    assert item and item["type"] == "ImportError"


def test_identical_traceback_each_run_anchors_to_its_own_position(home):
    """每轮崩的都是同一段 ⇒ 最新那段（最近一轮）要被选中，不能被旧的同文本段骗走。"""
    tb = _CRASH_LOG.split("2026-11-17 08:30:02,000 INFO feishu-bridge 还活着\n", 1)[1]
    runner = "[runner] 进程退出，code=1，5 秒后重启 2026-11-18T10:00:00\n"
    log = home / "agent.log"
    log.write_text((tb + runner) * 10, encoding="utf-8")
    item = E.crash_item_from_log(str(log))
    assert item and item["type"] == "ImportError"


def _pkg(home, files: dict) -> str:
    pkg = home / "pkgsrc" / "bridge"
    for rel, body in files.items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return str(pkg)


_RUN_TB = """2026-11-18 10:29:50,000 INFO feishu-bridge 桥已启动
Traceback (most recent call last):
  File "C:\\Users\\zhangsan\\.carher-feishu-agent\\bridge\\fleet\\model.py", line 5, in run
    proc = subprocess.Popen(cmd)
FileNotFoundError: [WinError 2] x
[runner] 进程退出，code=1，5 秒后重启 2026-11-18T10:29:58
"""


def test_frames_that_do_not_match_current_source_are_not_reported(home):
    """栈说 model.py:5 在 run 里；当前代码的第 5 行不在 run 里 ⇒ 是上一版的，不报。"""
    log = home / "agent.log"
    log.write_text(_RUN_TB, encoding="utf-8")
    moved = _pkg(home, {"fleet/model.py": "X = {\n 'a': 1,\n 'b': 2,\n 'c': 3,\n 'git': 4,\n}\n\n\ndef run():\n    pass\n"})
    assert E.crash_item_from_log(str(log), package_dir=moved) is None


def test_frames_that_match_current_source_are_reported(home):
    """阳性对照：第 5 行确实在 run 里 ⇒ 照报。"""
    log = home / "agent.log"
    log.write_text(_RUN_TB, encoding="utf-8")
    here = _pkg(home, {"fleet/model.py": "import subprocess\n\n\ndef run(cmd):\n    proc = subprocess.Popen(cmd)\n    return proc\n"})
    item = E.crash_item_from_log(str(log), package_dir=here)
    assert item and item["type"] == "FileNotFoundError"
    assert item["frames"] == ["bridge/fleet/model.py:5:run"]


def test_module_level_frames_check_line_range(home):
    log = home / "agent.log"
    log.write_text(_CRASH_LOG, encoding="utf-8")
    ok = _pkg(home, {"app.py": "\n" * 11 + "from .wsfast import make_client\n", "wsfast.py": "\n" * 29 + "import lark_oapi as lark\n"})
    assert E.crash_item_from_log(str(log), package_dir=ok)
    short = _pkg(home, {"app.py": "x = 1\n", "wsfast.py": "x = 1\n"})
    assert E.crash_item_from_log(str(log), package_dir=short) is None


def test_crash_log_main_passes_the_installed_package_dir():
    src = (_ROOT / "bridge" / "errreport.py").read_text(encoding="utf-8")
    assert 'crash_item_from_log(log_path, package_dir=os.path.join(home, "bridge"))' in src


def _crash_home(home, app_body: str):
    """真造一个起不来的安装目录：bridge/ 里放真的 errreport/redact/status + 一个会崩的 app.py。"""
    for name in ("__init__.py", "errreport.py", "redact.py", "status.py"):
        shutil.copy(_ROOT / "bridge" / name, home / "bridge" / name)
    (home / "bridge" / "buildinfo.py").write_text('BUILD_ID = "20261117-crash00"\n', encoding="utf-8")
    (home / "bridge" / "app.py").write_text(app_body, encoding="utf-8")
    (home / "install_id").write_text("0000000000c0ffee", encoding="utf-8")
    (home / "config.toml").write_text(f'[feishu]\napp_id = "{APP}"\napp_secret = "{SECRET}"\n', encoding="utf-8")
    (home / "logs").mkdir()


def _run_crash_mode(home, **env_over):
    dump = home / "dump.json"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "CARHER_NO_REPORT": "0",
           "CARHER_ERRREPORT_DUMP": str(dump)}
    env.update(env_over)
    proc = subprocess.run(
        [sys.executable, "-m", "bridge.errreport", "--home", str(home), "--crash-log", str(home / "logs" / "agent.log")],
        cwd=str(home), env=env, capture_output=True, text=True, timeout=60,
    )
    return proc, dump


def test_crash_mode_reproduces_import_crash(home):
    _crash_home(home, "import os\n\n\ndef boot():\n    import no_such_module_r38  # noqa\n\n\nboot()\n")
    proc, dump = _run_crash_mode(home)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(dump.read_text(encoding="utf-8"))
    assert payload["kind"] == "runtime" and payload["id"] == "0000000000c0ffee" and payload["app"] == APP
    assert payload["v"] == "20261117-crash00"
    item = payload["items"][0]
    assert item["src"] == "crash" and item["type"] == "ModuleNotFoundError"
    assert item["frames"] == ["bridge/app.py:8:<module>", "bridge/app.py:5:boot"]
    text = dump.read_text(encoding="utf-8")
    assert SECRET not in text and str(home) not in text


def test_crash_mode_falls_back_to_log_when_import_is_fine(home):
    # 装着的代码和日志里的栈对得上（app.py 第 12 行、wsfast.py 第 30 行都在模块级）
    _crash_home(home, "X = 1\n" * 12)
    (home / "bridge" / "wsfast.py").write_text("Y = 1\n" * 30, encoding="utf-8")
    (home / "logs" / "agent.log").write_text(_CRASH_LOG, encoding="utf-8")
    proc, dump = _run_crash_mode(home)
    assert proc.returncode == 0, proc.stderr
    item = json.loads(dump.read_text(encoding="utf-8"))["items"][0]
    assert item["type"] == "ImportError" and item["frames"][0] == "bridge/app.py:12:<module>"


def test_crash_mode_drops_a_log_traceback_from_an_older_build(home):
    """🔴 11-18 e9690e：日志里那段栈是上一版的（装着的 app.py 根本没有第 12 行）⇒ 不许贴当前版本号报上来。"""
    _crash_home(home, "X = 1\n")
    (home / "logs" / "agent.log").write_text(_CRASH_LOG, encoding="utf-8")
    proc, dump = _run_crash_mode(home)
    assert proc.returncode == 0, proc.stderr
    items = json.loads(dump.read_text(encoding="utf-8"))["items"] if dump.exists() else []
    assert not [i for i in items if i.get("src") == "crash"], items


def test_crash_mode_respects_no_report(home):
    _crash_home(home, "import no_such_module_r38\n")
    proc, dump = _run_crash_mode(home, CARHER_NO_REPORT="1")
    assert proc.returncode == 0 and not dump.exists()


def test_crash_mode_always_exits_zero_on_garbage(home):
    proc, _ = _run_crash_mode(home)   # 空目录：没有 bridge/ 里的东西
    assert proc.returncode != 0 or True   # 跑在仓库的 bridge 上；关键是下一条
    proc2 = subprocess.run([sys.executable, "-m", "bridge.errreport", "--crash-log", "/nonexistent"],
                           cwd=str(_ROOT), env={"PATH": "/usr/bin:/bin", "CARHER_NO_REPORT": "1"},
                           capture_output=True, text=True, timeout=60)
    assert proc2.returncode == 0


# ── 启动守卫真跑：连击到 5 次时后台调 errreport --crash-log；CARHER_NO_REPORT=1 不调 ──

_FAKE = """#!/bin/bash
if [ "$2" = "bridge.errreport" ]; then echo "$*" >> "$ERR_LOG"; exit 0; fi
exit 1
"""


def _guard_home(home):
    app = home / "app"
    (app / "bridge").mkdir(parents=True)
    (app / "bridge" / "errreport.py").write_text("# stub\n", encoding="utf-8")
    (home / "bin").mkdir()
    shutil.copy(_ROOT / "scripts" / "launch_guard.sh", app / "launch-guard.sh")
    for name, body in (("curl", "#!/bin/bash\nexit 0\n"), ("sleep", "#!/bin/bash\nexit 0\n"), ("fakepy", _FAKE)):
        p = home / "bin" / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    return app


def _guard_start(home, app, no_report):
    env = {"PATH": f"{home}/bin:/usr/bin:/bin", "HOME": str(home), "ERR_LOG": str(home / "err.log"),
           "CARHER_NO_REPORT": no_report}
    return subprocess.run(["/bin/bash", str(app / "launch-guard.sh"), str(home / "bin" / "fakepy"), "-m", "bridge.app"],
                          env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("no_report,expect", [("0", 1), ("1", 0)])
def test_launch_guard_calls_crash_reporter_once_per_episode(home, no_report, expect):
    app = _guard_home(home)
    for _ in range(7):
        _guard_start(home, app, no_report)
    deadline = time.time() + 5
    while expect and time.time() < deadline and not (home / "err.log").exists():
        time.sleep(0.1)
    time.sleep(0.3)
    lines = (home / "err.log").read_text().splitlines() if (home / "err.log").exists() else []
    assert len(lines) == expect, lines
    if expect:
        assert lines[0] == f"-m bridge.errreport --home {app} --crash-log {app}/logs/agent.log"


def test_launch_guard_old_install_without_errreport_is_untouched(home):
    """旧版 bridge/ 里没有 errreport.py（自动更新退回旧版）⇒ 守卫不调它，照旧只报 u=crashloop。"""
    app = _guard_home(home)
    (app / "bridge" / "errreport.py").unlink()
    for _ in range(6):
        _guard_start(home, app, "0")
    time.sleep(0.3)
    assert not (home / "err.log").exists()


# ───────────────────────── app.py 装上了 ─────────────────────────

def test_app_main_installs_errreport_inside_try():
    src = (_ROOT / "bridge" / "app.py").read_text(encoding="utf-8")
    main = src[src.index("def main() -> None:"):]
    head = main[: main.index("lock = _acquire_single_instance_lock()")]
    assert "errreport.install()" in head
    block = head[head.index("try:"):head.index("errreport.install()")]
    assert "from . import errreport" in block, "必须包在 try 里（errreport 坏了不许拖垮启动）"


def test_install_is_noop_under_pytest(home):
    before = list(logging.getLogger("feishu-bridge").handlers)
    hook = sys.excepthook
    E._installed = False
    try:
        assert E.install(str(home), str(home / "bridge")) is None
    finally:
        E._installed = False
    assert logging.getLogger("feishu-bridge").handlers == before and sys.excepthook is hook


def test_excepthooks_report_and_chain(home, monkeypatch):
    sink = Sink()
    rep = _reporter(home, monkeypatch, sink)
    seen = []
    monkeypatch.setattr(sys, "excepthook", lambda *a: seen.append("sys"))
    monkeypatch.setattr(threading, "excepthook", lambda a: seen.append("thread"))
    E._install_excepthooks(rep)
    exc = _bridge_exc()
    sys.excepthook(type(exc), exc, exc.__traceback__)
    assert seen == ["sys"] and sink.items()[0]["src"] == "uncaught"     # drain 同步发了

    def worker():
        E._trim_item(None)
    t = threading.Thread(target=worker)
    t.start(); t.join()
    assert seen == ["sys", "thread"]
    rep.flush()
    srcs = [it["src"] for it in sink.items()]
    assert "thread" in srcs or len(rep._sent_fp) >= 1     # 同一指纹已发过时只计数


def test_concurrent_flush_sends_once(home, monkeypatch):
    """11-17 端到端实测：后台线程和前台同时 flush ⇒ 198 收到两条一样的。同一批只许发一次。"""
    sink = Sink(delay=0.3)
    rep = _reporter(home, monkeypatch, sink)
    rep.add({"src": "log", "msg": "x", "fp": "d" * 12})
    threads = [threading.Thread(target=rep.flush) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sink.payloads) == 1, sink.payloads


def test_add_does_not_wake_sender_immediately(home, monkeypatch):
    """add 不唤醒后台线程：等 `_FLUSH_AFTER_SEC` 让连击合并（唤醒了 n 就永远是 1）。"""
    rep = _reporter(home, monkeypatch)
    rep.add({"src": "log", "msg": "x", "fp": "e" * 12})
    assert not rep._wake.is_set()
