"""第 15 轮角色 R4：让助手自己说出「我是哪一版、新不新、我现在正常吗、有问题你下一步做什么」。

## 这组测试盯的是今天的真实代价

2026-11-06 一天换了 3 次安装包附件，同事和领导**无法回答「我手上是哪一版」**
⇒「修好了」和「没生效」分不开。所以最要紧的一条不是"字段齐不齐"，而是
**服务端版本取不到的时候绝不许说「已是最新」** —— 那正是把"不知道"讲成"知道"
的那一步，一次就能把整轮诊断带偏（`test_unknown_latest_never_claims_up_to_date`）。

## 判据怎么钉

- 总体一句话只有三种，各一条（断开 = 🔴 / 连上但菜单没配 = ⚠️ / 都好 = ✅）；
- 每条 problems 的 `next` 必须非空，而且**每种能触发的问题形状各查一遍**；
- `ctx` 缺字段 / 是 None / 是垃圾类型都不许抛 —— 这三个函数在飞书回调里跑，
  抛了用户看到的就是一条报错；
- 文案闸门直接**复用 `test_copy_quality.py` 的正则**（不自己另写一套判据），
  并带阳性对照：同一个检查器必须能抓到一句故意写坏的话。
"""

from __future__ import annotations

import inspect
import os
import pathlib
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = pathlib.Path(os.path.dirname(_HERE))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, _HERE)

import test_copy_quality as gate  # noqa: E402

from bridge import selfcheck  # noqa: E402


# ---------- 夹具 ----------


def ctx(**over) -> dict:
    """一台「什么都正常」的机器。每条测试只改它关心的那几个字段。"""
    base = {
        "version": "20261106-1200",
        "latest_version": "20261106-1200",
        "connected": True,
        "offline_sec": 0,
        "uptime_sec": 3 * 3600 + 10 * 60,
        "restarts": 1,
        "owner_bound": True,
        "menu_ready": True,
        "menu_reason": "",
        "mode": "readonly",
        "ceiling_mode": "trusted",
        "workspace": "~/codes/demo",
        "last_error": "",
    }
    base.update(over)
    return base


def texts(data) -> list[str]:
    """一个 ctx 的全部出口文字（三个函数的所有字符串），给闸门扫。"""
    out = [selfcheck.version_line(data)]
    card = selfcheck.summary(data)
    out.append(str(card.get("headline", "")))
    out.append(str(card.get("text", "")))
    out.extend(str(x) for x in (card.get("lines") or ()))
    for item in selfcheck.problems(data):
        out.extend(str(item.get(k, "")) for k in ("what", "why", "next"))
    return [t for t in out if t]


# ---------- 1. 签名 ----------


def test_three_functions_take_one_ctx():
    for name in ("summary", "version_line", "problems"):
        fn = getattr(selfcheck, name)
        params = list(inspect.signature(fn).parameters)
        assert params == ["ctx"], f"{name} 的参数应该只有 ctx，现在是 {params}"


def test_module_touches_neither_network_nor_disk():
    """🔴 纯函数：它在飞书那 3 秒回调窗口里跑，不许自己去查网/读盘/读全局状态。

    判据是**源码里没有这些名字**，而不是"跑一遍没报错" —— 慢路径只在出故障那天才走到。
    """
    src = (_ROOT / "bridge" / "selfcheck.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    for banned in ("urllib", "requests", "socket", "subprocess",
                   "open(", "os.path", "os.environ", "time.monotonic", "import time"):
        assert banned not in code, f"模块里出现了 {banned} —— 它该是纯函数"


# ---------- 2. 总体一句话只有三种 ----------


def test_all_good_is_green():
    card = selfcheck.summary(ctx())
    assert card["headline"] == selfcheck.OK
    assert card["verdict"] == "ok"
    assert selfcheck.problems(ctx()) == []


def test_disconnected_is_down():
    """连接断着 = 🔴。这时候同事发的消息根本到不了，说「能用但有问题」是谎。"""
    card = selfcheck.summary(ctx(connected=False, offline_sec=39 * 60))
    assert card["headline"] == selfcheck.DOWN
    assert card["verdict"] == "down"


def test_connected_but_menu_missing_is_warn():
    card = selfcheck.summary(ctx(menu_ready=False, menu_reason="发布版本失败 code=210302"))
    assert card["headline"] == selfcheck.WARN
    assert card["verdict"] == "warn"


def test_owner_not_bound_is_down():
    """没认主人 = 谁发消息都被静默丢掉（今天 21:34 那两条就是）⇒ 这台机器现在用不了。"""
    card = selfcheck.summary(ctx(owner_bound=False))
    assert card["headline"] == selfcheck.DOWN


@pytest.mark.parametrize("value", [None, "yes", 1, "", 0, "true"])
@pytest.mark.parametrize("field", ["connected", "owner_bound"])
def test_unproven_health_counts_as_broken(field, value):
    """🔴 fail-closed：只有**真的是 True** 才算好。

    探不出来（None / 缺字段）和探出来是好的，是两件事。方向是故意的 ——
    说成好的会让他白等一个永远不会来的回复，而说成坏的最坏只是多点一次诊断。
    `1` / `"yes"` 也不算：那是探测函数被换过之后的形状，不是"好了"的证据。
    """
    card = selfcheck.summary(ctx(**{field: value}))
    assert card["headline"] == selfcheck.DOWN, f"{field}={value!r} 被当成好的了：{card['headline']}"
    assert selfcheck.problems(ctx(**{field: value})), f"{field}={value!r} 没列成问题"


@pytest.mark.parametrize("value", [None, "yes", 1, ""])
def test_unproven_menu_is_not_reported_as_configured(value):
    items = selfcheck.problems(ctx(menu_ready=value))
    assert any("菜单" in p["what"] for p in items), f"menu_ready={value!r} 被当成配好了"
    assert selfcheck.summary(ctx(menu_ready=value))["headline"] != selfcheck.OK


def test_verdict_is_one_of_exactly_three():
    seen = set()
    for over in ({}, {"connected": False}, {"menu_ready": False}, {"owner_bound": False},
                 {"latest_version": None}, {"version": None, "latest_version": None},
                 {"restarts": 40}, {"connected": None}, {"last_error": "长连接断开"}):
        card = selfcheck.summary(ctx(**over))
        assert card["headline"] in (selfcheck.OK, selfcheck.WARN, selfcheck.DOWN), over
        seen.add(card["headline"])
    assert seen == {selfcheck.OK, selfcheck.WARN, selfcheck.DOWN}, "三种状态没全被覆盖"


def test_down_beats_warn():
    """断线 + 菜单也没配 ⇒ 还是 🔴，不许被降级成「能用但有问题」。"""
    card = selfcheck.summary(ctx(connected=False, menu_ready=False, latest_version=None))
    assert card["headline"] == selfcheck.DOWN
    assert card["problems"][0]["what"] == selfcheck.problems(ctx(connected=False))[0]["what"]


# ---------- 3. 版本：今天真正坏掉的那一项 ----------


def test_same_version_says_up_to_date():
    line = selfcheck.version_line(ctx())
    assert "20261106-1200" in line
    assert "最新" in line


def test_newer_available_says_it_installs_itself():
    """有新版 ⇒ 要说「马上自己装」。只说「有新版」会让他又去文档里下附件（今天 3 次）。"""
    line = selfcheck.version_line(ctx(latest_version="20261106-2030"))
    assert "20261106-1200" in line and "20261106-2030" in line
    assert "新版" in line
    assert "自己装" in line or "自动" in line


def test_unknown_latest_never_claims_up_to_date():
    """🔴 本轮最要紧的一条：查不到服务端版本 ≠ 已是最新。

    「我没观测到」不等于「它不存在」。说成「已是最新」会让他相信手上这版是好的，
    而今天的现场恰恰是：老版本的人装不到自动更新，而他以为自己是新的。
    """
    for latest in (None, "", "   "):
        line = selfcheck.version_line(ctx(latest_version=latest))
        assert "最新" not in line, f"latest={latest!r} 时说成了最新：{line}"
        assert "查不到" in line, line
        assert "20261106-1200" in line, "本机那一版还是要报出来"
        card = selfcheck.summary(ctx(latest_version=latest))
        assert "最新" not in card["text"], card["text"]


def test_dev_machine_without_version_says_so():
    """开发机（没打版本号）⇒ 照实说，不装作有版本、也不说成最新。"""
    line = selfcheck.version_line(ctx(version=None))
    assert "最新" not in line
    assert any(w in line for w in ("没有版本号", "认不出", "说不出"))
    assert "None" not in line


def test_no_version_and_no_latest_is_still_honest():
    line = selfcheck.version_line(ctx(version=None, latest_version=None))
    assert "最新" not in line
    assert line.strip()


def test_version_mismatch_is_a_problem_with_a_next_step():
    found = [p for p in selfcheck.problems(ctx(latest_version="20261106-2030"))
             if "新版" in p["what"]]
    assert found, "有新版却没列成一条问题"
    assert found[0]["next"].strip()


def test_unknown_latest_is_not_alarming():
    """查不到服务端版本本身不该把总体判成 🔴 —— 他的机器可能只是不在公司网里。"""
    assert selfcheck.summary(ctx(latest_version=None))["headline"] != selfcheck.DOWN


# ---------- 4. 每条问题都要有他真能做的下一步 ----------

# 每种能触发问题的 ctx 形状各来一条。缺哪种就少盯一种 ⇒ 这张表要和 selfcheck 里的判据一一对上。
PROBLEM_SHAPES = [
    ("断线", {"connected": False, "offline_sec": 39 * 60}),
    ("刚断", {"connected": False, "offline_sec": 10}),
    ("没认主人", {"owner_bound": False}),
    ("菜单没配", {"menu_ready": False, "menu_reason": "缺权限 im:chat"}),
    ("有新版", {"latest_version": "20261106-2030"}),
    ("查不到最新版", {"latest_version": None}),
    ("没版本号", {"version": None}),
    ("重启太多", {"restarts": 40, "uptime_sec": 50}),
    ("最后一类错", {"last_error": "长连接断开"}),
]


@pytest.mark.parametrize("name,over", PROBLEM_SHAPES, ids=[n for n, _ in PROBLEM_SHAPES])
def test_every_problem_has_a_next_step(name, over):
    """🔴 「下一步」为空 = 他看完只知道坏了、不知道干什么 —— 和没说一样。"""
    items = selfcheck.problems(ctx(**over))
    assert items, f"{name} 这种形状一条问题都没列出来"
    for item in items:
        assert set(item) == {"what", "why", "next"}, item
        for key in ("what", "why", "next"):
            assert isinstance(item[key], str) and item[key].strip(), f"{name} 的 {key} 是空的：{item}"


def test_next_step_is_something_he_can_actually_do():
    """下一步必须是**他能做到**的动作：点某个按钮 / 发某两个字 / 找某个人说一句。

    不许出现「等等看」「检查一下配置」这种他做不了也判不了的话。
    """
    # 这个产品真正能让他做的动作就这几类：点按钮、发几个字、重新发一遍刚才那句、
    # 找管理员说一句、重新双击安装包、动这台电脑本身（开盖/插电/连网）。
    doable = ("点", "发「", "重新发", "找", "重新双击", "打开", "插上", "连上")
    for _name, over in PROBLEM_SHAPES:
        for item in selfcheck.problems(ctx(**over)):
            assert any(k in item["next"] for k in doable), f"这条下一步他做不了：{item}"


def test_problems_are_capped_so_the_card_stays_readable():
    """一屏能看完。全坏的时候也不许堆十几条 —— 小白只会关掉。"""
    worst = ctx(connected=False, owner_bound=False, menu_ready=False, menu_reason="缺权限 im:chat",
                version=None, latest_version=None, restarts=40, last_error="长连接断开")
    assert len(selfcheck.problems(worst)) <= 4


def test_summary_carries_the_same_problems():
    """一屏卡里的明细就是 `problems()`，不许另算一套（两套会漂）。"""
    data = ctx(connected=False, menu_ready=False)
    assert selfcheck.summary(data)["problems"] == selfcheck.problems(data)


def test_good_machine_still_gets_a_one_screen_card():
    card = selfcheck.summary(ctx())
    assert card["lines"], "全好的时候也要有几条明细（版本/连接/权限），不能只有一句话"
    assert any("20261106-1200" in line for line in card["lines"])


# ---------- 5. 烂 ctx 不许抛 ----------

# 调用方是飞书回调。这里抛一下，用户看到的就是"它报错了"，而他本来是来问"你还好吗"的。
JUNK = [
    {},
    None,
    {"version": None, "latest_version": None, "connected": None, "offline_sec": None,
     "uptime_sec": None, "restarts": None, "owner_bound": None, "menu_ready": None,
     "menu_reason": None, "mode": None, "ceiling_mode": None, "workspace": None,
     "last_error": None},
    {"connected": "yes", "offline_sec": "一会儿", "restarts": "几次", "uptime_sec": [],
     "version": 7, "latest_version": {}, "menu_ready": 1, "owner_bound": "真"},
    {"version": "x" * 500, "latest_version": "y" * 500, "menu_reason": "z" * 500},
    "不是 dict",
    [],
    42,
]


@pytest.mark.parametrize("junk", JUNK, ids=range(len(JUNK)))
def test_junk_ctx_never_raises(junk):
    line = selfcheck.version_line(junk)
    card = selfcheck.summary(junk)
    items = selfcheck.problems(junk)
    assert isinstance(line, str)
    assert isinstance(card, dict) and card["headline"] in (
        selfcheck.OK, selfcheck.WARN, selfcheck.DOWN)
    assert isinstance(items, list)
    for item in items:
        assert item["next"].strip()


def test_empty_ctx_is_not_reported_as_healthy():
    """🔴 什么都没传 ⇒ 不许回「一切正常」。探不出来和探出来是好的，是两件事。

    而且要说成「我读不出自己的状态」，不是罗列一串「连接断了、没认主人」——
    后者会让同事去修一堆根本没发生的事。这个分支单独钉住，因为它只在这时走。
    """
    for empty in ({}, None, "不是 dict", []):
        card = selfcheck.summary(empty)
        assert card["headline"] != selfcheck.OK
        items = card["problems"]
        assert len(items) == 1, items
        assert "读不出" in items[0]["what"], items[0]
        assert any("重连" not in p["what"] for p in items), "空 ctx 被当成「连接断了」去修了"


def test_long_values_are_truncated():
    """一个字段被塞进一整行日志时，卡上也只能出现一小截（同 status.HEALTH_VALUE_MAX 的理由）。

    查的是**每一行**和整段的总长，不是 `text` 这一个拼好的大段 ——
    第一版这里把 `text` 也一起比 200，必红，而它本来就该是整屏那么长。
    """
    card = selfcheck.summary(ctx(version="v" * 400, menu_reason="r" * 400, menu_ready=False))
    for line in card["lines"] + [card["headline"]]:
        assert len(line) <= selfcheck.LINE_MAX, line
    for item in card["problems"]:
        for key in ("what", "why", "next"):
            assert len(item[key]) <= selfcheck.LINE_MAX, item
    # 整屏也要有个上限：一个 400 字的字段不许把整段撑到手机上翻不完
    assert len(card["text"]) <= 1200, len(card["text"])
    assert "v" * 60 not in card["text"], "版本号那一截没截断"


# ---------- 6. 文案闸门（复用 test_copy_quality 的判据 + 阳性对照） ----------


def _gate_hits(data):
    units = [gate.Unit("bridge/selfcheck.py", 0, t, "summary", "feishu", "_say")
             for t in texts(data)]
    return [h for u in units for h in gate.check_unit(u)]


@pytest.mark.parametrize("name,over", PROBLEM_SHAPES + [("全好", {})],
                         ids=[n for n, _ in PROBLEM_SHAPES] + ["全好"])
def test_copy_passes_the_gate(name, over):
    """🔴 不许出现内部名词、叫人打斜杠命令、说改不了。判据直接用闸门那套正则。"""
    hits = _gate_hits(ctx(**over))
    assert not hits, f"{name}：\n" + "\n".join(h.where() for h in hits)


def test_the_gate_check_is_actually_wired():
    """阳性对照：同一个检查器必须能抓到一句故意写坏的话。

    不然"0 条命中"和"检查器没接上"长得一模一样（第 11 轮踩过）。
    """
    bad = gate.Unit("bridge/selfcheck.py", 0,
                    "退出码 3，发 /diag 看 open_id，config.toml 改不了", "x", "feishu", "_say")
    assert gate.check_unit(bad), "闸门检查器没接上 —— 上面那组测试什么都没查"


def test_copy_never_leaks_internal_words():
    """再单独钉一遍今天明确点名的那几个词（闸门正则以后万一被改窄，这条还在）。"""
    banned = ("open_id", "scope", "digest", "指纹", "退出码", "returncode",
              "build_id", "latest.json", "sha256", "traceback", "None", "menu_heal")
    for _name, over in PROBLEM_SHAPES + [("全好", {})]:
        for text in texts(ctx(**over)):
            for word in banned:
                assert word not in text, f"{word!r} 出现在：{text!r}"


def test_copy_never_passes_through_an_exception_text():
    """🔴 异常原文不许上屏。`menu_reason` 这类字段的来源就可能是一长串英文。"""
    raw = "Traceback (most recent call last): ConnectionResetError(54, 'Connection reset by peer')"
    for text in texts(ctx(menu_ready=False, menu_reason=raw, last_error=raw)):
        assert "Traceback" not in text, text
        assert "ConnectionResetError" not in text, text


def test_absolute_paths_never_reach_the_card():
    """绝对路径带着同事真名，而这张卡的用途就是转发给 IT。"""
    for text in texts(ctx(workspace="/Users/zhangsan/codes/demo")):
        assert "/Users/" not in text, text
