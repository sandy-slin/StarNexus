"""第 11 轮角色 C：文案闸门本身的测试 + 帮助说明别再叫人打命令。

闸门（`test_copy_quality.py`）最怕的是**什么都不查也是绿的**：扫描范围被改窄、
判据正则被改坏、出口名单漏了一个 —— 结果都是"0 条命中"，和"文案全都干净"长得一模一样。
所以这里每条都是**阳性对照**或**下限**：

- 造一段假源码，里面故意写「请输入 /cancel」等坏文案，闸门必须抓到；
- 真扫一遍 `bridge/`，条数必须 ≥ 下限，`bridge/fleet/`、`menu.py`、`workspace.py` 必须都被扫到；
- review-r9 第 9 条列的那几种漏法（`_say_with_buttons`、`toast(`、跨行 `+` 拼接、f-string 里的 `{exc}`）
  每一种单独一条。

下限是 2026-11-06 实测的 1004 条（飞书 917 / 终端 87）往下留了余量，删文案不至于误红。
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = pathlib.Path(os.path.dirname(_HERE))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, _HERE)

import test_copy_quality as gate  # noqa: E402

# 实测 1004 / 917 / 87（2026-11-06）。往下留 ~15% 余量。
MIN_UNITS = 850
MIN_FEISHU = 780
MIN_FLEET_UNITS = 300  # bridge/fleet/ 实测 ~420 条


def _scan(src: str, relative: str = "bridge/fake.py"):
    return gate.scan_source(src, relative, gate.known_commands())


def _hits(src: str, relative: str = "bridge/fake.py"):
    return [h for u in _scan(src, relative) for h in gate.check_unit(u)]


def _checks(src: str, relative: str = "bridge/fake.py"):
    return {(h.check, h.token) for h in _hits(src, relative)}


# ---------- 阳性对照：坏文案必须被抓 ----------


def test_positive_control_please_type_cancel():
    """🔴 brief 点名的那一句：「请输入 /cancel」必须被抓到。"""
    src = 'def f(self, chat):\n    self._say(chat, "请输入 /cancel 退出")\n'
    assert ("斜杠命令", "/cancel") in _checks(src)


@pytest.mark.parametrize("outlet", ["_say", "_say_with_buttons", "_say_later", "_say_later_with_buttons"])
def test_every_outlet_name_is_scanned(outlet):
    """review-r9 #9：原来的正则 `_say\\(` 不匹配 `_say_with_buttons(` 等名字。"""
    src = f'def f(self, chat):\n    self.{outlet}(chat, "发 /stop 停下", [])\n'
    assert ("斜杠命令", "/stop") in _checks(src)


def test_toast_is_scanned():
    src = 'def f():\n    def toast(kind, content):\n        return content\n    return toast("info", "发 /resume 继续")\n'
    assert ("斜杠命令", "/resume") in _checks(src)


def test_english_only_outlet_argument_is_scanned():
    """出口实参不含汉字也要扫 —— `_say(chat, "/cancel")` 整条就是叫人打命令。"""
    src = 'def f(self, chat):\n    self._say(chat, "Send /cancel")\n'
    assert ("斜杠命令", "/cancel") in _checks(src)


def test_multiline_concatenation_is_scanned_as_one():
    """review-r9 #9：跨行 `fallback = ("…" + extra + "…")` 原来只取第一段。命令在最后一段也要抓到。"""
    src = (
        'def f(self, chat, extra):\n'
        '    fallback = (\n'
        '        "卡片发不出去，" \n'
        '        + extra +\n'
        '        "确认的话回一句 `/go!`，不然打 /new。"\n'
        '    )\n'
        '    self._say(chat, fallback)\n'
    )
    units = _scan(src)
    joined = [u for u in units if "卡片发不出去" in u.text]
    assert len(joined) == 1, "拼接要并成一条，不能拆成三条也不能只剩第一段"
    assert "/new" in joined[0].text and gate.HOLE in joined[0].text
    assert ("斜杠命令", "/new") in _checks(src)


def test_implicit_concatenation_is_scanned():
    """相邻字面量（Python 编译期合并的那种）—— 原来的正则只取第一个 `"…"`。"""
    src = 'X = (\n    "第一行没问题。"\n    "第二行叫人发 /dirs。"\n)\n'
    assert ("斜杠命令", "/dirs") in _checks(src)


def test_exception_passthrough_in_fstring_is_caught():
    """review-r9 #8/#9：`f"没发出去：{exc}"` 把异常原文（含本机路径）发到手机上。"""
    src = (
        'def f():\n'
        '    try:\n'
        '        pass\n'
        '    except OSError as exc:\n'
        '        return f"没发出去：{exc}"\n'
    )
    assert ("透传异常", "exc") in _checks(src)


@pytest.mark.parametrize("shape", ["{exc.reason}", "{str(exc)}", "{err}"])
def test_exception_passthrough_shapes(shape):
    name = "err" if "err" in shape else "exc"
    src = (
        'def f():\n'
        '    try:\n'
        '        pass\n'
        f'    except OSError as {name}:\n'
        f'        return f"读不出来：{shape}"\n'
    )
    assert any(h.check == "透传异常" for h in _hits(src)), shape


def test_translated_exception_is_not_flagged():
    """阴性对照：过了 `friendly_error(exc, …)` 的不算透传 —— 否则闸门会逼人把翻译层也拆掉。"""
    src = (
        'def f():\n'
        '    try:\n'
        '        pass\n'
        '    except OSError as exc:\n'
        '        return f"读不出来：{friendly_error(exc)}"\n'
    )
    assert not any(h.check == "透传异常" for h in _hits(src))


@pytest.mark.parametrize("bad, check", [
    ("详情见 app.py 第 3 行", "内部名词"),
    ("改一下 config.toml 就好", "内部名词"),
    ("调 _handle_command 出错了", "内部名词"),
    ("owner_open_ids 是空的", "内部名词"),
    ("跑一下 resolve_workspace() 看看", "内部名词"),
    ("路径是 /Users/somebody/x", "内部名词"),
    ("agent 没回话", "内部名词"),
    ("看本机日志", "内部名词"),
    ("只读档改不了文件", "说改不了"),
    ("现在只能看，不能改", "说改不了"),
])
def test_positive_controls_for_other_checks(bad, check):
    src = f'def f(self, chat):\n    self._say(chat, "{bad}")\n'
    assert any(h.check == check for h in _hits(src)), f"没抓到：{bad}"


# ---------- 阴性对照：不该抓的不抓（否则闸门会被人整个关掉） ----------


@pytest.mark.parametrize("src", [
    'def f():\n    """叫人打 /cancel 是不对的。"""\n',                           # docstring
    'def f():\n    logger.info("收到 /cancel，config.toml 读失败：%s", 1)\n',     # 日志
    'def f(t):\n    return t in {"/cancel", "取消"}\n',                          # 拿来匹配的
    'def f(t):\n    return t == "退出 /cancel"\n',                               # 比较
    'def f():\n    raise ValueError("改不了 config.toml")\n',                     # raise
    'D = {"/诊断": "诊断"}\n',                                                    # 字典键
    'X = "/诊断"\n',                                                              # 整条就是一个命令
])
def test_non_user_facing_positions_are_skipped(src):
    assert not _hits(src), src


def test_paths_are_not_mistaken_for_commands():
    src = 'def f(self, chat):\n    self._say(chat, "在 ~/codes/x 里，或者 https://a.b/c 这个链接")\n'
    assert not any(h.check == "斜杠命令" for h in _hits(src))


def test_terminal_strings_are_counted_but_not_held_to_feishu_rules():
    """安装器终端里的话照样收，但不按"手机上没按钮"那套规矩查斜杠命令。"""
    src = 'def main():\n    print("用法：python bridge/botmenu.py <config.toml>")\n'
    units = _scan(src)
    assert units and all(u.channel == "terminal" for u in units)
    assert not [h for u in units for h in gate.check_unit(u) if h.check == "斜杠命令"]


# ---------- 下限：范围没被改窄 ----------


@pytest.fixture(scope="module")
def real_units():
    return gate.scan_bridge()


def test_scan_count_floor(real_units):
    """🔴 扫到的条数 ≥ 下限。扫描范围被改窄（只扫 app.py、只认 `_say`）时这条先红。"""
    feishu = [u for u in real_units if u.channel == "feishu"]
    assert len(real_units) >= MIN_UNITS, f"只扫到 {len(real_units)} 条，范围被改窄了？"
    assert len(feishu) >= MIN_FEISHU, f"飞书那边只剩 {len(feishu)} 条，判据被改窄了？"


def test_scan_covers_every_bridge_file_with_copy(real_units):
    """review-r9 #9：原来根本不看 `bridge/fleet/`、`menu.py`、`workspace.py`。"""
    files = {u.file for u in real_units}
    for must in ("bridge/app.py", "bridge/menu.py", "bridge/workspace.py", "bridge/pairing.py",
                 "bridge/fleet/cards.py", "bridge/fleet/claude.py", "bridge/fleet/control.py",
                 "bridge/fleet/notify.py", "bridge/fleet/relay.py"):
        assert must in files, f"{must} 没被扫到"
    fleet = [u for u in real_units if u.file.startswith("bridge/fleet/")]
    assert len(fleet) >= MIN_FLEET_UNITS, f"bridge/fleet/ 只扫到 {len(fleet)} 条"


def test_scan_walks_the_tree_not_a_list():
    """文件列表来自 `rglob`，新加的 `bridge/**/x.py` 自动进来，不靠有人记得登记。"""
    found = {p.relative_to(_ROOT).as_posix() for p in gate.bridge_files()}
    on_disk = {p.relative_to(_ROOT).as_posix() for p in (_ROOT / "bridge").rglob("*.py")}
    assert found == on_disk


def test_real_outlets_are_recognised(real_units):
    """真代码里每一种出口都至少认出一处 —— 出口名单被改坏时这条先红。"""
    outlets = {u.outlet for u in real_units if u.outlet}
    assert {"_say", "_say_with_buttons", "toast"} <= outlets, outlets


def test_review_r9_examples_are_now_seen(real_units):
    """review-r9 #9 点名漏掉的两句（`/talk!` 兜底、`/mode!` 兜底）现在都扫得到。"""
    feishu = [u.text for u in real_units if u.channel == "feishu" and u.file == "bridge/app.py"]
    assert any("/talk!" in t for t in feishu)
    assert any("/mode!" in t for t in feishu)


def test_terminal_classification_never_swallows_a_feishu_outlet(real_units):
    """被判成"终端"的字符串里，不许有一条是飞书出口的直接实参 —— 不然 TERMINAL_* 就成了后门。"""
    leaked = [f"{u.file}:{u.line}" for u in real_units if u.channel == "terminal" and u.outlet]
    assert not leaked, leaked


def test_terminal_share_stays_small(real_units):
    """终端那一类是少数（实测 87 / 1004）。比例突然变大 = 有人把整个文件塞进 TERMINAL_FILES 躲闸门。"""
    terminal = [u for u in real_units if u.channel == "terminal"]
    assert len(terminal) <= 0.15 * len(real_units), len(terminal)


def test_new_bad_copy_in_an_allowlisted_file_is_still_caught():
    """🔴 豁免要四样都对上（文件 + 项 + 词 + 那句里的一段）才放过。

    `app.py` 里有 `/talk!` 兜底的豁免；同一个文件里新写一句「请发 /talk! 确认」**不是**那句兜底，
    必须照样红。只按文件、或只按文件 + 词放过的话，这条先红。
    """
    unit = gate.Unit("bridge/app.py", 1, "请发 /talk! 确认", "Bridge.f", "feishu", "_say")
    hits = gate.check_unit(unit)
    assert hits, "阳性对照本身没被抓到"
    assert gate.unexplained(hits), "新写的一句被旧豁免顺手放过了"
    # 阳性对照：真正那句兜底确实被放过（不是"什么都不放过"才通过的）
    real = gate.Unit("bridge/app.py", 1, "卡片发不出去：确认的话回一句 `/talk!`", "Bridge.f", "feishu", "_say")
    assert gate.check_unit(real) and not gate.unexplained(gate.check_unit(real))


def test_positive_control_flows_through_the_whole_gate():
    """造的坏文案要一路走到闸门的最终判断（`unexplained`），不只是 `check_unit` 抓到。"""
    src = 'def f(self, chat):\n    self._say(chat, "请输入 /cancel")\n'
    bad = gate.unexplained(_hits(src, "bridge/app.py"))
    assert [h.token for h in bad] == ["/cancel"]


def test_allowlist_entries_carry_a_reason():
    for key, why in {**gate.ALLOWED, **gate.PENDING}.items():
        assert len(key) == 4 and all(key), key
        assert isinstance(why, str) and len(why) >= 4, f"{key} 没写理由"


# ---------- 帮助：说点哪个按钮，不教命令 ----------


def _help():
    from bridge.menu import HELP

    return HELP


def test_help_teaches_buttons_not_commands():
    """用户原话「不要让用户输入 "/cancel" 等类似的 尽量用按钮的形式」。

    斜杠命令只许出现在最后那一行兜底里，正文一个都不许有。
    """
    help_text = _help()
    body, _, last = help_text.rstrip().rpartition("\n")
    assert "按钮出不来" in last, "兜底那一行要在最后"
    assert not gate.SLASH.findall(body), f"正文里还在教命令：{gate.SLASH.findall(body)}"


def test_help_button_names_match_the_real_buttons():
    """帮助里叫他点的每一个「…」，都必须是真有的按钮字 —— 名字对不上他就找不到。"""
    from bridge.botmenu import MENU_TREE
    from bridge.fleet.actions import DEFAULT_LABELS
    from bridge.menu import CONTROLS

    real = ({label for _, label in CONTROLS} | set(DEFAULT_LABELS.values()) | {n for n, _, _ in MENU_TREE}
            | {child for _, _, kids in MENU_TREE for child, _ in kids})
    named = re.findall(r"「([^」]*[\U0001F300-\U0001FAFFℹ⏹▶][^」]*)」", _help())
    assert len(named) >= 7, f"帮助里点名的按钮太少：{named}"
    missing = [n for n in named if n not in real]
    assert not missing, f"帮助里点名的按钮在卡片上不存在：{missing}（真有的：{sorted(real)}）"


def test_help_still_covers_what_old_tests_rely_on():
    """原来几条测试盯着 HELP 的那些事，换成按钮说法后照样都在。"""
    text = _help()
    assert "接着聊" in text and "退出对话" in text
    assert "权限" in text and "Esc" in text
    assert "做不到" in text
    for button in ("停下", "会话", "换项目"):
        assert button in text


def test_dirs_button_label_is_one_icon_everywhere():
    """「换项目」在卡片菜单（CONTROLS）、按钮（DEFAULT_LABELS）、帮助、回话里是同一个图标。

    原来卡片菜单是 🔀、别处是 📂 —— 帮助叫他点「📂 换项目」，菜单卡上找不到一模一样的那个。
    """
    from bridge.fleet.actions import DEFAULT_LABELS
    from bridge.menu import CONTROLS
    from bridge.workspace import DIRS_BUTTON

    label = dict(CONTROLS)["/dirs"]
    assert label == DEFAULT_LABELS["dirs"]
    assert f"「{label}」" == DIRS_BUTTON
    assert DIRS_BUTTON in _help()


def test_app_help_call_sites_still_send_a_help_text():
    """调用点还在：app.py 里 `/help`、帮助按钮、菜单卡兜底三处都发 `HELP`（查调用形状，不查名字）。

    app.py 归角色 D。换成 `from .menu import HELP` 之前这三处发的还是 app.HELP —— 这条只盯调用点。
    """
    tree = ast.parse((_ROOT / "bridge/app.py").read_text(encoding="utf-8"))
    sends = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in gate.OUTLETS:
            if any(isinstance(a, ast.Name) and a.id == "HELP" for a in node.args):
                sends += 1
    assert sends >= 3, f"app.py 里发 HELP 的调用只剩 {sends} 处"


# ---------- workspace / menu 修过的那几句 ----------


def test_workspace_replies_point_to_the_button():
    """换目录相关的回话不再叫人发 `/dirs` 当主路，而是点「📂 换项目」。"""
    import bridge.workspace as ws
    from bridge.policy import Policy

    here = _ROOT / "tests"
    policy = Policy(mode="readonly", workspaces=(str(here),), default_workspace=str(here),
                    model="claude-haiku-5-5", turn_timeout_sec=60)
    missing = ws._verify(policy, str(here / "no-such-dir-r11"), "no-such-dir-r11")
    assert missing.outcome == ws.Outcome.MISSING
    assert ws.DIRS_BUTTON in missing.reply and "/dirs" not in missing.reply

    outside = ws._verify(policy, "/definitely/outside", "/definitely/outside")
    assert outside.outcome == ws.Outcome.OUTSIDE
    for bad in ("config.toml", "[agent]", "改不了"):
        assert bad not in outside.reply, f"越界那句又出现了 {bad}"
    assert "同事" in outside.reply, "要告诉他找谁"


def test_mode_card_under_ceiling_does_not_send_user_to_config_file():
    import types

    from bridge.menu import build_mode_card

    policy = types.SimpleNamespace(mode="readonly", ceiling_mode="write",
                                   describe=lambda: "只读")
    card = build_mode_card(policy)
    assert "config.toml" not in card
    assert "同事" in card
