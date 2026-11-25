"""`/诊断` 那张脱敏健康卡。

## 这组测试在守什么

今天同事那台电脑的日志只在 `~/.carher-feishu-agent/logs/`，你和 IT 一个字节都看不到。
诊断卡是把"坏了但没人看得见"变成能远程判的那一步 —— 它会被**同事转发到群里**，
所以它的内容面必须是**白名单**：只有 `status.HEALTH_FIELDS` 里登记过的键能出现。

黑名单（"把敏感的过滤掉"）在这里是错的：新加一个字段默认就外发了，而没有人会
记得回来补规则。所以下面第一组用例是"塞进去的东西出不来"，而不是"敏感词被挡了"。

真实的泄漏面不是假想的 —— 本机日志里实打实躺着这些（2026-11-05 查的）：
`connected to wss://…?access_key=…&ticket=…`、`open_id=ou_…`、
`审批钩子已装：/Users/<真名>/.carher-feishu-agent/…`。任何"带上最后几行日志"
的设计都会把它们一起发出去，所以日志只准**数**不准**摘**。
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

import pytest  # noqa: E402

from bridge import status as status_mod  # noqa: E402


# 🔴 第 30 轮：诊断卡要填「最新版本」，`app.py` 里调的是真的 `selfupdate.latest()` ⇒ 这个文件原来每跑一次
# 就真打一遍 198 的 latest.json（198 日志里约 80% 是测试流量，把全公司版本统计污染掉）。
# 整个文件一律把网络换成抛错的桩：任何一条想出网都只会拿到「查不到」，绝不发真请求。
_NET_CALLS: list = []


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import urllib.request

    _NET_CALLS.clear()

    def refuse(url, *a, **kw):
        _NET_CALLS.append(getattr(url, "full_url", url))
        raise OSError("测试里不许出网")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    yield


# ---------- 白名单：没登记的键一个都出不来 ----------


def test_unlisted_keys_are_dropped():
    """🔴 核心用例：塞进来的任何没登记的键都不该出现在卡上。

    这些键名就是最可能被"顺手加上"的那几样：聊天正文、路径、密钥、open_id。
    """
    leaky = {
        "version": "abc12345",
        "transcript": "帮我把 payments 里的密码改成 hunter2",
        "last_reply": "我改好了 config.py",
        "cwd": "/Users/zhangsan/codes/secret-project",
        "app_secret": "ZZZZaaaabbbbccccdddd1111",
        "open_id": "ou_ac0e2dbe531ef8aa11223344",
        "chat_id": "oc_9f8e7d6c5b4a",
        "log_tail": "connected to wss://x/ws/v2?ticket=TTTTuuuu1111",
        "session_titles": ["重构支付", "查线上报错"],
    }
    out = "\n".join(status_mod.health_lines(leaky))
    assert "abc12345" in out, "登记过的字段要留下"
    for forbidden in (
        "hunter2", "payments", "config.py", "zhangsan", "secret-project",
        "ZZZZaaaabbbbccccdddd1111", "ou_ac0e2dbe531ef8aa11223344",
        "oc_9f8e7d6c5b4a", "ticket=", "重构支付",
    ):
        assert forbidden not in out, f"没登记的内容漏出来了：{forbidden}"


def test_whitelist_is_a_closed_set():
    """反向：卡上出现的行数不能超过登记的字段数。"""
    everything = {key: "x" for key, _ in status_mod.HEALTH_FIELDS}
    everything.update({f"bogus_{i}": "y" for i in range(20)})
    assert len(status_mod.health_lines(everything)) == len(status_mod.HEALTH_FIELDS)


def test_no_field_name_looks_like_content():
    """闸门：登记字段里不许出现"会带正文/路径/身份"的名字。

    这条是写给**下一个人**的 —— 他想加一行"最后一条消息"时会在这里红。
    """
    banned = (
        "transcript", "message", "text", "reply", "prompt", "content",
        "path", "cwd", "dir", "secret", "token", "key", "open_id", "chat",
        "title", "tail", "log_line",
    )
    for key, _ in status_mod.HEALTH_FIELDS:
        for word in banned:
            assert word not in key, (
                f"字段 {key!r} 的名字像是会带 {word} —— 诊断卡只准放计数和枚举"
            )


def test_values_are_truncated_and_single_line():
    """超长值截断、换行压掉：万一有人把一整行日志塞进登记字段，也只漏一小截。"""
    lines = status_mod.health_lines({"version": "A" * 500 + "\nSECRET-SECOND-LINE"})
    assert len(lines) == 1
    assert "SECRET-SECOND-LINE" not in lines[0]
    assert len(lines[0]) < status_mod.HEALTH_VALUE_MAX + 20


def test_values_still_go_through_redact():
    """出口兜底仍然生效（白名单是入口限额，redact 是出口兜底，两层都要有）。"""
    lines = status_mod.health_lines({"last_error": "sk-ant-abcdefghijklmnopqrstuvwx"})
    assert "sk-ant-abcdefghijklmnopqrstuvwx" not in "\n".join(lines)


def test_empty_and_none_values_are_skipped():
    """探不出来的项不显示 —— 空行比没有这一行更让人以为"这里本来有东西"。

    注意 `0` **不**算空：`📂 项目 0 个` 是真情报（白名单配错了，同事什么都问不了），
    而"重启 0 次"这种没意义的零由采集那边不写进来，不在这一层判。
    """
    assert status_mod.health_lines({"version": "", "uptime": None}) == []
    assert status_mod.health_lines({"workspaces": 0}) == ["📂 项目 0 个"]


# ---------- 卡片形状 ----------


def test_card_is_schema_2_without_action_container():
    """回归：schema 2.0 不支持 action 容器（code 230099 / 200861）。"""
    blob = status_mod.build_health_card({"version": "abc12345", "uptime": "3m00s"})
    card = json.loads(blob)
    assert card["schema"] == "2.0"
    assert '"tag": "action"' not in blob
    assert '"actions"' not in blob


def test_card_fits_the_30kb_limit_even_with_garbage_in():
    """卡片超 30KB 飞书**静默丢弃** —— 诊断卡在故障时发不出去等于没有。"""
    blob = status_mod.build_health_card({k: "x" * 400 for k, _ in status_mod.HEALTH_FIELDS})
    assert len(blob.encode("utf-8")) < 30 * 1024


def test_empty_data_still_sends_something_actionable():
    """一个字段都探不出来时不发空卡 —— 那等于又回到"它没反应"。"""
    blob = status_mod.build_health_card({})
    assert "诊断" in blob or "状态" in blob


def test_card_says_it_is_safe_to_forward():
    """同事要敢转给 IT，卡上就得写明它不含聊天内容。"""
    blob = status_mod.build_health_card({"version": "abc12345"})
    assert "不含" in blob


# ---------- 日志只数不摘 ----------


# 这几行是从本机真实日志里取的形状（值已换成假的）。
REAL_LOG_SHAPES = [
    "2026-11-05 14:55:01,118 INFO Lark connected to wss://ws.feishu.cn/ws/v2"
    "?fpid=1&aid=22&device_id=741234567890&access_key=AAAAbbbbCCCC1111&ticket=ZZZZyyyy2222",
    "2026-11-05 14:55:01,120 INFO feishu-bridge 审批钩子已装："
    "/Users/zhangsan/.carher-feishu-agent/hooks/approval_hook.py",
    "2026-11-05 14:55:02,001 INFO feishu-bridge 丢弃非授权用户消息 open_id=ou_ac0e2dbe531ef8aa",
    "2026-11-05 14:56:00,000 ERROR Lark connect failed, err: [Errno 22] Invalid argument",
    "2026-11-05 14:57:00,000 ERROR Lark connect failed, err: Connection reset by peer",
    "2026-11-05 15:00:00,000 ERROR feishu-bridge 长连接已静默 901s（阈值 900s）",
    "2026-11-05 15:00:05,000 INFO feishu-bridge 桥已启动。模式=trusted 工作目录=/Users/zhangsan/codes",
]


def test_classify_returns_only_counts_never_text():
    """🔴 分类的输出里不许出现任何原文片段。"""
    counts = status_mod.classify_log_errors(REAL_LOG_SHAPES)
    assert counts, "这些行里有能认出来的故障"
    blob = json.dumps(counts, ensure_ascii=False)
    for forbidden in ("ticket", "access_key", "zhangsan", "ou_", "wss://", "Errno"):
        assert forbidden not in blob, f"分类结果带出了原文：{forbidden}"
    assert all(isinstance(v, int) for v in counts.values())
    assert all(name in dict(status_mod.ERROR_CLASSES) for name in counts)


def test_classify_counts_each_line_once():
    counts = status_mod.classify_log_errors(REAL_LOG_SHAPES)
    assert counts["连不上飞书"] == 2
    assert counts["看门狗重启"] == 1
    assert sum(counts.values()) <= len(REAL_LOG_SHAPES)


def test_unrecognized_lines_are_ignored_not_echoed():
    """认不出的行一律丢掉 —— 不许为了"兜住未知"把原文带出去。"""
    assert status_mod.classify_log_errors(["某种全新的报错 /Users/zhangsan/x sk-ant-xxxx"]) == {}


def test_format_error_counts_is_bounded():
    many = {f"类{i}": i for i in range(1, 30)}
    text = status_mode_format = status_mod.format_error_counts(many)
    assert text.count("·") <= 3, "最多列 4 类，免得卡片越来越长"
    assert status_mod.format_error_counts({}) == ""


def test_format_error_counts_orders_by_count():
    text = status_mod.format_error_counts({"少": 1, "多": 9})
    assert text.startswith("多 9")


# ---------- 代码指纹 ----------


def test_fingerprint_is_content_based_not_mtime(tmp_path):
    """同一份代码在两台机器上必须得出同一个值（否则两台一样的机器显示成两个版本）。"""
    one = tmp_path / "a"
    two = tmp_path / "b"
    for root in (one, two):
        root.mkdir()
        (root / "app.py").write_text("print(1)\n", encoding="utf-8")
        (root / "redact.py").write_text("print(2)\n", encoding="utf-8")
    os.utime(two / "app.py", (0, 0))  # 改 mtime，不改内容
    assert status_mod.code_fingerprint(str(one)) == status_mod.code_fingerprint(str(two))


def test_fingerprint_changes_when_a_module_is_missing(tmp_path):
    """安装器漏装模块 = 静默少一层闸门。指纹必须变，否则远程查版本查不出来。"""
    full = tmp_path / "full"
    full.mkdir()
    (full / "app.py").write_text("print(1)\n", encoding="utf-8")
    (full / "redact.py").write_text("print(2)\n", encoding="utf-8")
    before = status_mod.code_fingerprint(str(full))
    (full / "redact.py").unlink()
    assert status_mod.code_fingerprint(str(full)) != before


def test_fingerprint_is_short_and_opaque(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    (root / "app.py").write_text("x\n", encoding="utf-8")
    fingerprint = status_mod.code_fingerprint(str(root))
    assert len(fingerprint) == 8 and fingerprint.isalnum()


def test_fingerprint_returns_empty_when_unreadable(tmp_path):
    """算不出来就空串（那一行不显示）—— 不编一个假版本号出来。"""
    assert status_mod.code_fingerprint(str(tmp_path / "nope")) == ""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert status_mod.code_fingerprint(str(empty)) == ""


# ---------- 入口：按钮和命令 ----------


def test_diag_button_is_whitelisted_and_unscoped():
    from bridge.fleet import actions

    assert actions.resolve_do_click({"act": "do", "op": "diag"}) == "/诊断"
    assert "diag" in actions.UNSCOPED_OPS, "诊断报整机状态，和是哪个聊天无关"
    assert actions.do_button("diag")["text"]["content"]


def test_diag_button_cannot_carry_extra_keys():
    """回归 actions 的红字：多一个键都不认，按钮不能变成夹带参数的后门。"""
    from bridge.fleet import actions

    assert actions.resolve_do_click({"act": "do", "op": "diag", "path": "/etc/passwd"}) == ""


def test_every_do_op_has_label_and_toast():
    """加 op 时忘了配文案 ⇒ do_button 会 KeyError，点一下整条回调就挂了。"""
    from bridge.fleet import actions

    for op in actions.DO_COMMANDS:
        assert op in actions.DEFAULT_LABELS, f"{op} 少 label"
        assert op in actions.TOASTS, f"{op} 少 toast"


@pytest.mark.parametrize("word", ["诊断", "/诊断", "/diag", "/diagnose"])
def test_diag_words_reach_command_dispatch(word):
    """不带斜杠的「诊断」也要进命令分派 —— 同事在手机上打的就是这两个字。"""
    from bridge import app as app_mod

    stripped = word.strip()
    assert (
        stripped.startswith("/") or app_mod._is_fleet_word(stripped)
    ), "这句话进不了命令分派，会被当成普通提问发给 agent"


def test_diag_word_is_exact_match_only():
    """「诊断一下线上报错」是正常提问，不能被命令分派吃掉。"""
    from bridge import app as app_mod

    assert not app_mod._is_fleet_word("诊断一下线上报错")
    assert not app_mod._is_fleet_word("帮我诊断")


# ---------- 端到端：命令真的走到、而且日志原文真的没出去 ----------


def _diag_bridge(tmp_path, log_text: str):
    """造一个只够跑 `/诊断` 的 Bridge（不连网络、不起 claude）。"""
    import asyncio
    import types

    from bridge.app import Bridge
    from bridge.policy import Policy

    home = tmp_path / ".carher-feishu-agent"
    (home / "logs").mkdir(parents=True)
    (home / "approvals").mkdir(parents=True)
    (home / "logs" / "agent.log").write_text(log_text, encoding="utf-8")

    sent: list = []
    cards: list = []
    bridge = Bridge.__new__(Bridge)
    bridge.gate = types.SimpleNamespace(root=str(home / "approvals"))
    bridge._started_at = 0.0
    bridge._last_event_at = 0.0
    bridge._ws_client = None
    bridge.settings = types.SimpleNamespace(
        policy=Policy(
            mode="readonly",
            workspaces=(str(tmp_path),),
            default_workspace=str(tmp_path),
            model="sonnet",
            turn_timeout_sec=600,
        ),
        custom_rules=(),
    )
    bridge.prefs = None
    bridge.replier = types.SimpleNamespace(
        send=lambda chat, text: sent.append(text) or True,
        send_card=lambda chat, card: cards.append(card) or True,
    )
    return bridge, sent, cards, asyncio


@pytest.mark.parametrize("word", ["诊断", "/诊断", "/diag"])
def test_diag_command_is_handled_and_sends_a_card(tmp_path, word):
    """🔴 命令分派真的认它。没有这条，白名单写得再好也没人能触发。"""
    bridge, _sent, cards, asyncio = _diag_bridge(
        tmp_path, "\n".join(REAL_LOG_SHAPES) + "\n"
    )
    handled = asyncio.run(bridge._handle_command("dm:owner", "oc_1", word))
    assert handled is True, f"{word} 没被当成命令 ⇒ 会被发给 agent"
    assert len(cards) == 1, "该发一张诊断卡"


def test_real_log_shapes_never_reach_the_card(tmp_path):
    """🔴 最重要的一条：拿**真实形状**的日志喂进去，卡上不许出现里面的任何凭据/路径/身份。"""
    bridge, sent, cards, asyncio = _diag_bridge(
        tmp_path, "\n".join(REAL_LOG_SHAPES) + "\n"
    )
    asyncio.run(bridge._handle_command("dm:owner", "oc_1", "诊断"))
    blob = cards[0] + "".join(sent)
    for forbidden in (
        "AAAAbbbbCCCC1111", "ZZZZyyyy2222", "ticket=", "access_key=",
        "zhangsan", "ou_ac0e2dbe531ef8aa", "wss://", "approval_hook.py",
        "device_id", "/Users/",
    ):
        assert forbidden not in blob, f"诊断卡带出了日志原文：{forbidden}"


def test_card_carries_the_numbers_support_actually_needs(tmp_path):
    """反向：该有的情报要在 —— 版本、连接状态、错误分类、重启次数。"""
    bridge, _sent, cards, asyncio = _diag_bridge(
        tmp_path, "\n".join(REAL_LOG_SHAPES) + "\n"
    )
    asyncio.run(bridge._handle_command("dm:owner", "oc_1", "诊断"))
    blob = cards[0]
    assert "版本" in blob
    assert "连" in blob, "要能看出连上没"
    assert "连不上飞书 2" in blob, "错误分类计数要在"
    assert "重启" in blob


def test_diag_card_sends_no_real_request(tmp_path):
    """🔴 诊断那条路查「最新版本」走的是被换掉的 urlopen（证明没出网），而且查不到也照样出卡。"""
    bridge, _sent, cards, asyncio = _diag_bridge(tmp_path, "\n".join(REAL_LOG_SHAPES) + "\n")
    asyncio.run(bridge._handle_command("dm:owner", "oc_1", "诊断"))
    assert len(cards) == 1
    assert _NET_CALLS, "诊断没走 urlopen —— 这条测试的前提变了，重看诊断怎么查最新版本"
    assert all("latest.json" in str(u) for u in _NET_CALLS), _NET_CALLS


def test_diag_still_answers_when_there_is_no_log(tmp_path):
    """日志不存在（刚装完 / 被清过）时也要回一张卡，不能静默失败。"""
    bridge, sent, cards, asyncio = _diag_bridge(tmp_path, "")
    os.unlink(str(tmp_path / ".carher-feishu-agent" / "logs" / "agent.log"))
    handled = asyncio.run(bridge._handle_command("dm:owner", "oc_1", "诊断"))
    assert handled is True
    assert cards or sent


def test_diag_survives_a_broken_log_file(tmp_path):
    """乱码/二进制日志不能让诊断炸掉 —— 它恰恰在东西已经坏了的时候跑。"""
    path = tmp_path / ".carher-feishu-agent" / "logs" / "agent.log"
    bridge, sent, cards, asyncio = _diag_bridge(tmp_path, "x")
    path.write_bytes(b"\xff\xfe\x00broken\x00\xff connect failed")
    handled = asyncio.run(bridge._handle_command("dm:owner", "oc_1", "诊断"))
    assert handled is True
    assert cards or sent


def test_diag_reads_only_the_log_tail(tmp_path):
    """日志能长到几十 MB，而卡片回调要 3 秒内返回 ⇒ 只读尾部。"""
    from bridge import app as app_mod

    filler = ("2026-11-05 10:00:00,000 INFO feishu-bridge 桥已启动。模式=readonly\n"
              * 20000)
    bridge, _sent, cards, asyncio = _diag_bridge(
        tmp_path, filler + "2026-11-05 15:00:00,000 ERROR Lark connect failed, err: x\n"
    )
    log = tmp_path / ".carher-feishu-agent" / "logs" / "agent.log"
    assert log.stat().st_size > app_mod._DIAG_LOG_TAIL_BYTES, "这个用例要日志超过尾部窗口"
    asyncio.run(bridge._handle_command("dm:owner", "oc_1", "诊断"))
    # 尾部窗口内的「桥已启动」条数 < 全文条数 ⇒ 证明没有整文件读进来
    assert "重启 20000 次" not in cards[0]
    assert "连不上飞书 1" in cards[0], "尾部那条错还是要数到"


# ---------- 版本号（BUILD_ID）----------
#
# 🔴 2026-11-06 写推广文档时发现：安装包早就打了版本号，诊断卡却还只显示「代码指纹」——
# 远程支持只能答「两台是不是同一份码」，答不出「哪台新哪台旧」。


@pytest.fixture
def pkg_dir():
    """夹具放测试目录旁边（不放 tmp_path，同本仓其他路径类用例）。"""
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".buildid-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_build_id_is_read_from_buildinfo(pkg_dir):
    (pkg_dir / "buildinfo.py").write_text('"""x"""\n\nBUILD_ID = "20261106-abc1234"\n', encoding="utf-8")
    assert status_mod.build_id(str(pkg_dir)) == "20261106-abc1234"
    assert status_mod.version_label(str(pkg_dir), "deadbeef") == "20261106-abc1234 (deadbeef)"


def test_unreplaced_placeholder_is_not_shown(pkg_dir):
    """build 没替换占位符（从仓库直接跑）⇒ 不许把占位符发给用户，退回指纹。"""
    (pkg_dir / "buildinfo.py").write_text('BUILD_ID = "@@' + 'BUILD_ID@@"\n', encoding="utf-8")
    assert status_mod.build_id(str(pkg_dir)) == ""
    assert status_mod.version_label(str(pkg_dir), "deadbeef") == "deadbeef"


def test_missing_buildinfo_falls_back_to_fingerprint(pkg_dir):
    assert status_mod.version_label(str(pkg_dir), "deadbeef") == "deadbeef"
    assert status_mod.version_label(str(pkg_dir), "") == ""


def test_buildinfo_is_read_not_executed(pkg_dir):
    """🔴 那个文件能被写进任意内容 ⇒ 只能读、不能 import（import = 执行）。"""
    marker = pkg_dir / "executed.flag"
    (pkg_dir / "buildinfo.py").write_text(
        f'open({str(marker)!r}, "w").write("x")\nBUILD_ID = "20261106-abc1234"\n', encoding="utf-8")
    assert status_mod.build_id(str(pkg_dir)) == "20261106-abc1234"
    assert not marker.exists(), "build_id() 执行了 buildinfo.py 里的代码"


def test_build_id_cannot_smuggle_a_long_line(pkg_dir):
    (pkg_dir / "buildinfo.py").write_text('BUILD_ID = "' + "x" * 500 + '"\n', encoding="utf-8")
    assert status_mod.build_id(str(pkg_dir)) == "", "超长值不该被当成版本号"


def test_diagnostics_card_actually_uses_the_build_id():
    """🔴 「调用点还在」：status 里有 version_label 不代表诊断卡用了它。

    2026-11-06 写推广文档时发现：安装包打了版本号，诊断卡第一行仍是代码指纹 ——
    函数写好了、没人调，就是那个形状。
    """
    import inspect

    from bridge.app import Bridge

    src = inspect.getsource(Bridge._collect_health)
    assert "status_mod.version_label(" in src, "诊断卡没读版本号 ⇒ 远程支持分不出新旧"


# ---------- 长任务：「比平时久，可以先走开」（2026-11-06 第 3 轮，竞品对标 E Top5）----------


def _render(escalate_sec, elapsed):
    card = status_mod.StatusCard(replier=None, chat_id="oc_x", escalate_sec=escalate_sec)
    return card._render(elapsed, 3, "Bash")


def test_long_running_task_tells_the_owner_he_can_walk_away():
    text = _render(420, 600)
    assert "比平时久" in text and "先去忙" in text and "停下" in text


def test_short_task_says_nothing_extra():
    """阳性对照：没到阈值不多一句（不然每张状态卡都在念叨）。"""
    assert "比平时久" not in _render(420, 60)


def test_zero_threshold_disables_the_hint():
    assert "比平时久" not in _render(0, 99999)


def test_status_card_is_built_with_the_escalation_threshold():
    """🔴 「调用点还在」：StatusCard 支持 escalate_sec 不代表 app.py 传了。

    第 3 轮差点漏掉：主会话把传参交给实现角色，消息没送到，参数一直没传 ——
    status.py 那几条测试全绿，线上那句话永远不出现。
    """
    import inspect
    import re

    from bridge.app import Bridge

    src = inspect.getsource(Bridge)
    calls = [src[m.start():m.start() + 160] for m in re.finditer(r"StatusCard\(", src)]
    assert calls, "app.py 里找不到 StatusCard( 调用"
    assert all("escalate_sec" in c for c in calls), calls
