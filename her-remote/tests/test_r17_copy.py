"""第 17 轮：默认档从只读翻成全控制之后，文案必须跟着说真话。

## 为什么有这个文件

第 16 轮把安装默认改成了全授权（`install-mac.sh` / `install-windows.ps1` 的
`mode = "trusted"`），但开场消息、`/help`、权限卡、放开确认卡还在说
「现在是只读、要改会先弹卡片问你」。那几句现在是**假的**，而且假在危险的方向上：
用户以为「它动手前会问我」，于是放心把它指到一个仓库上，它不问就改。

## 判据从哪来（不是凭感觉写的）

下面每一条「必须说对」的事，都对着**真跑一遍钩子**的结果（`_decide`）：
同一份 `approval.HOOK_SOURCE`、同一个 TRUSTED 标记、真 subprocess。
2026-11-07 实测（全授权档、工作目录 = 临时目录）：

    ls ~/Desktop               allow   目录外看一眼  ⇒ 不许再说「看不到目录外」
    cat ~/.zsh_history         allow   同上
    rm <工作目录>/a.txt         allow   目录内删    ⇒ 不许再说「不会删东西」
    git commit -m x            allow   目录内提交
    echo x > ~/Desktop/p.txt   defer   目录外写    ⇒ 「要动项目外的东西会先问你」成立
    cat ~/.ssh/id_rsa          deny    凭据
    rm -rf ~                   deny    删家目录
    git push                   deny    推代码
    sudo ls                    deny    提权

⚠️ 跑钩子**必须给 `CARHER_APPROVAL_DIR`**，否则每条都回 ask「审批目录没配置」，
看上去像「什么都要问」⇒ 假红（本轮踩过一次）。

## 刻意的一条反直觉判据

档位认不出来时，文案要按**最松**那档说（见 `test_unknown_mode_copy_errs_toward_warning`）。
别处的 fail-closed 是「读不懂就给只读」，而这里反过来：说「每步问你」而实际不问，
用户会按「它会先问我」来放心 —— 那是安全语义上的谎。说「不问就做」而实际会问，
最坏只是多几张卡片。
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = pathlib.Path(os.path.dirname(_HERE))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, _HERE)  # 为了 `import test_copy_quality`（同 test_r11_copy.py 的做法）

_SRC = _ROOT.parents[1] / "feishu-agent-oneclick" / "src"

MODES = ("readonly", "write", "trusted")

# 用户看得见的字里一个都不许出现（同 tests/test_copy_quality.py 的闸门）。
JARGON = ("readonly", "trusted", "config.toml", "open_id", "max_mode", "prefs",
          "defaults_rev", "migrated_from", "TRUSTED", "mode =")

# 「承诺做不到的事」那一类说法。历史教训：只读档曾经承诺「不能改文件」，
# 而实测点一下同意文件真的被改了（2026-11-05 用户拍板改说法）。
LIES = ("改不了", "不能改", "没法改", "改不动", "不能编辑", "无法修改", "不会改", "只能看")


# ---------- 现场：真跑钩子 ----------


def _decide(command: str, *, trusted: bool, workspace: str, directory: str) -> str:
    """跑一遍真钩子，返回 allow / deny / defer / ask。"""
    from bridge.approval import HOOK_SOURCE

    hook = os.path.join(directory, "hook.py")
    with open(hook, "w", encoding="utf-8") as handle:
        handle.write(HOOK_SOURCE)
    approvals = os.path.join(directory, "approvals")
    os.makedirs(approvals, exist_ok=True)
    with open(os.path.join(approvals, "workspaces.json"), "w", encoding="utf-8") as handle:
        json.dump([workspace], handle)
    marker = os.path.join(approvals, "TRUSTED")
    if trusted:
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write("1")
    elif os.path.exists(marker):
        os.unlink(marker)
    event = {"tool_name": "Bash", "tool_input": {"command": command},
             "cwd": workspace, "session_id": "s1"}
    proc = subprocess.run(
        [sys.executable, hook], input=json.dumps(event), capture_output=True,
        text=True, timeout=120,
        # 🔴 不给这个变量 ⇒ 钩子一律回 ask「审批目录没配置」⇒ 假红
        env={**os.environ, "CARHER_APPROVAL_DIR": approvals},
    )
    try:
        return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]
    except Exception:  # noqa: BLE001
        return f"解析不出来：{proc.stdout[:80]}{proc.stderr[:80]}"


@pytest.fixture
def box():
    with tempfile.TemporaryDirectory() as tmp:
        workspace = os.path.join(tmp, "ws")
        os.makedirs(workspace)
        yield workspace, tmp


# ---------- 取文案 ----------


def _note(mode: str) -> str:
    """开场消息里「现在是哪一档」那句，按档位生成。"""
    from bridge import pairing

    build = getattr(pairing, "permission_note", None)
    assert build, (
        "pairing 里没有「按档位生成那句话」的函数 ⇒ 开场消息还是写死的只读那一版，"
        "而默认档已经是全控制了"
    )
    return build(mode)


def _describe(mode: str) -> str:
    from bridge.policy import Policy

    return Policy(mode=mode, workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
                  model="claude-sonnet-5-5", turn_timeout_sec=900,
                  ceiling_mode="trusted").describe()


def _help_line(marker: str) -> str:
    from bridge.menu import HELP

    for line in HELP.splitlines():
        if marker in line:
            return line
    return ""


def _mode_card(mode: str, ceiling: str = "") -> str:
    from bridge.menu import build_mode_card
    from bridge.policy import Policy

    return build_mode_card(Policy(
        mode=mode, workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
        model="claude-sonnet-5-5", turn_timeout_sec=900, ceiling_mode=ceiling or mode))


def _loosen_card(target: str, now: str = "readonly") -> str:
    from bridge.menu import build_confirm_loosen_card
    from bridge.policy import Policy

    return build_confirm_loosen_card(target, Policy(
        mode=now, workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
        model="claude-sonnet-5-5", turn_timeout_sec=900, ceiling_mode="trusted"))


# ═════════ 一、开场消息：不许再承诺「默认只看」 ═════════
#
# 每个新用户收到的第一句话。装完默认是全控制，而这句还在说只读 ——
# r16-defaults.md 把它列为 Top 1（W1）。


def test_greeting_note_is_generated_per_mode_not_hardcoded():
    """🔴 那句话必须**按实际档位生成**。

    写死任何一档都是错的：IT 把机器锁在只读时开场该说只读，默认装机时该说全控制。
    判据查「三档给出三句不一样的话」，不查字面量 —— 措辞还会改。
    """
    said = {mode: _note(mode) for mode in MODES}
    for mode, text in said.items():
        assert text.strip(), f"{mode} 档没给出那句话"
    assert len(set(said.values())) == 3, f"三档说的是同一句话 ⇒ 等于写死：{said}"


def test_greeting_in_full_control_does_not_promise_it_will_ask_first():
    """🔴 本轮的核心一条：全控制下不许说「默认只看」「要改会先问你」。

    实测（见文件头）：全授权 + 工作目录内，改文件 / 删文件 / 跑命令全是 allow，
    一张卡都不弹。说「会先问你」就是在安全方向上撒谎。
    """
    note = _note("trusted")
    for lie in ("默认只看", "只看一眼", "要改东西会先弹", "点同意才动手", "点「同意，去做」才动手"):
        assert lie not in note, f"全控制下还在承诺「会先问你」：{lie!r} —— {note!r}"
    for lie in LIES:
        assert lie not in note, f"全控制下承诺做不到的事：{lie!r} —— {note!r}"
    assert "不用" in note or "不问" in note or "不再" in note, (
        f"没说清「不再每步问你」这个最要紧的事实：{note!r}"
    )


def test_greeting_in_full_control_names_the_red_lines():
    """说了「不问就做」就必须紧跟着说清**挡着什么** —— 否则用户不知道边界在哪。"""
    note = _note("trusted")
    assert "家目录" in note or "删" in note, f"没说挡着删家目录：{note!r}"
    assert "推代码" in note or "推送" in note or "提交到远" in note, f"没说挡着推代码：{note!r}"
    assert "密钥" in note or "私钥" in note or "凭据" in note, f"没说挡着读密钥：{note!r}"


def test_greeting_in_full_control_tells_how_to_get_safer():
    """领导原话「要安全的话让他单独设定只读」⇒ 第一句话就要说清怎么切。"""
    note = _note("trusted")
    assert "只读" in note, f"没告诉他能切到只读：{note!r}"
    assert "权限" in note, f"没指出按哪个按钮（「🔑 权限」）：{note!r}"


def test_greeting_in_readonly_still_says_it_asks_first():
    """阳性对照：IT 锁只读的机器，开场仍然要说「会先问你」。

    这条不过的话，上面那些「全控制不许说会问」的判据可能只是因为那句话被删空了。
    """
    note = _note("readonly")
    assert "只读" in note or "只看" in note, f"只读档没说清是哪一档：{note!r}"
    assert "问你" in note or "卡片" in note, f"只读档没说「会先问你」：{note!r}"
    for lie in LIES:
        assert lie not in note, f"只读档又把「改不了」这句谎话写回来了：{lie!r} —— {note!r}"


def test_greeting_messages_use_the_generated_note():
    """两条开场路（配对码 / 扫码）都得用生成的那句，不许各写一份。

    同 `FIRST_EXAMPLES` 当初那个坑：两份的话改了一边另一边不变，而测试全绿。
    """
    from bridge.pairing import bound_message, greeting_message

    for mode in MODES:
        note = _note(mode)
        for name, text in (("绑定消息", bound_message(mode)), ("开场消息", greeting_message(mode))):
            assert note in text, f"{name}（{mode} 档）没用生成的那句话"


def test_greeting_offers_a_one_tap_way_to_go_readonly():
    """🔴 全控制下第一屏就要能一下点到「切到只读」。

    默认就是全控制的人**从来不走切档那一步**，所以他永远看不到切档回话里那个
    「🔒 …只读」按钮（r16-defaults.md 第 4 节）。只读档不给这个按钮（已经在只读了）。
    """
    from bridge.pairing import greeting_buttons

    def labels(mode):
        return [b["text"]["content"] for b in greeting_buttons(mode)]

    trusted = labels("trusted")
    assert any("只读" in label for label in trusted), f"全控制下开场没给切只读的按钮：{trusted}"
    assert not any("只读" in label for label in labels("readonly")), (
        "已经在只读了还给「切到只读」按钮"
    )


def test_greeting_buttons_are_still_all_harmless():
    """🔴 开场多一个按钮，不许顺手放宽「点了不会让电脑上发生任何事」这条。

    新加的那个是收紧方向（切只读），和 `fleet/actions.py` 那份白名单同一个判据。
    """
    from bridge.fleet.actions import DO_COMMANDS
    from bridge.pairing import greeting_buttons

    for mode in MODES:
        for button in greeting_buttons(mode):
            value = button["behaviors"][0]["value"]
            if value.get("act") == "do":
                assert DO_COMMANDS[value["op"]] in {"/menu", "/help", "/dirs", "/status"}, (
                    f"开场按钮只许放纯显示的命令：{value}"
                )
            else:
                # 切档那个：只许往紧的方向，且不带别的键
                assert value == {"act": "menu", "mode": "readonly"}, (
                    f"开场按钮里出现了会改设置的值：{value}"
                )


# ═════════ 二、权限卡：「全授权」那格要说清挡什么 ═════════


def test_full_control_description_says_outside_is_no_different():
    """🔴 第 39 轮（用户 11-19 拍板「都改成 bypass」，红线保留）：全授权下项目外**也不问了**。

    第 17 轮这条测的是反面（「项目外还会先问你」必须写出来），那是当时的真实行为。
    行为改了文案必须跟着改，否则就是第 17 轮那个毛病倒过来：说会问、实际不问 ——
    这是安全语义上的谎（用户按「项目外会先问我」来放心，见文件头）。
    """
    text = _describe("trusted")
    assert "里外都一样" in text or "项目里外" in text, f"没说清项目外也不问：{text!r}"
    for stale in ("项目以外的东西还会先问", "项目外还会先问", "以外的东西会先问"):
        assert stale not in text, f"还在承诺项目外会先问你（行为已经不是这样了）：{text!r}"

def test_full_control_description_still_names_the_red_lines():
    """阳性对照：原来说对的那部分（删家目录 / 推代码 / 读密钥）不许在改写里丢掉。"""
    text = _describe("trusted")
    assert "家目录" in text
    assert "推" in text or "push" in text
    assert "密钥" in text or "私钥" in text or "凭据" in text


def test_descriptions_never_promise_what_it_cannot_keep():
    """三档都不许出现「改不了 / 不能改 / 只能看」那一类说法。

    只读档承诺「不能改文件」在 2026-11-05 被实测证伪（点同意就改了）；
    可改档原来写「不能删文件、不能跑任意命令」，而实测全授权下
    `rm <工作目录>/a.txt` 是 allow ⇒ 这类绝对承诺本身就不该写。
    """
    for mode in MODES:
        text = _describe(mode)
        for lie in LIES:
            assert lie not in text, f"{mode} 档承诺做不到的事：{lie!r} —— {text!r}"


def test_descriptions_carry_no_internal_words():
    for mode in MODES:
        text = _describe(mode)
        for word in JARGON:
            assert word not in text, f"{mode} 档的说明里有内部名词 {word!r}：{text!r}"


def test_unknown_mode_copy_errs_toward_warning():
    """🔴 认不出档位时按**最松**那档说（和别处的 fail-closed 刻意相反）。

    理由在文件头：说「会先问你」而实际不问，用户按这句话放心 —— 那是安全上的谎；
    说「不问就做」而实际会问，最坏只是多几张卡片。
    """
    from bridge.policy import Policy

    policy = Policy(mode="write", workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
                    model="claude-sonnet-5-5", turn_timeout_sec=900, ceiling_mode="trusted")
    object.__setattr__(policy, "mode", "这是个没见过的档")
    text = policy.describe()
    assert text.strip(), "认不出档位就什么都不说 ⇒ 卡片上空一块"
    for lie in ("默认只看", "每步", "点同意才动手"):
        assert lie not in text, f"认不出档位却承诺「会先问你」：{lie!r} —— {text!r}"
    for word in JARGON:
        assert word not in text, f"兜底那句露了内部名词 {word!r}：{text!r}"


def test_mode_card_shows_the_full_control_caveat(box):
    """权限卡上「现在是全授权」那块，要和实测对得上。

    卡片正文直接用 `describe()`，所以这条同时盯「卡片真的把那句话放进去了」。
    前提先实测一遍：前提一变，这条就该红、文案判据要重写（第 17 轮写下的那句提醒，第 39 轮兑现了）。
    """
    workspace, tmp = box
    assert _decide("echo x > ~/Desktop/p.txt", trusted=True,
                   workspace=workspace, directory=tmp) == "allow", (
        "前提变了：全授权下往项目外写又要弹卡了 ⇒ 这条文案判据要重写"
    )
    card = _mode_card("trusted")
    assert "全授权" in card
    assert "里外都一样" in card or "项目里外" in card, "卡上没说清项目外也不问"
    assert "项目以外的东西还会先问" not in card, "卡上还在承诺项目外会先问你"

def test_locked_readonly_card_no_longer_blames_the_installer():
    """🔴 只剩一档时那句话：默认改了之后，「这一档是装的时候定下的」不再成立。

    现在走到这里只有两种：IT 放了策略文件，或者有人主动写了锁死那一行
    （安装器默认写的是全控制 —— `test_installer_default_is_full_control` 盯着）。
    所以要说「被锁在只读」，并且告诉他找谁。
    """
    card = _mode_card("readonly")
    assert "装的时候定下的" not in card, "默认已经不是只读了，这句话是假的"
    assert "锁" in card, f"没说清这台电脑是被锁在只读的：{card}"
    assert "只读" in card
    # 下面这两条是上一轮的判据，不许在改写里丢掉（手机用户改不了文件、要告诉他找谁）
    assert "config.toml" not in card
    assert "同事" in card or "管理员" in card
    for dead in ("重跑", "重新安装", "再装一次", "重装"):
        assert dead not in card, f"又把用户打发去重跑安装：{dead}"


def test_loosen_card_tells_what_still_asks():
    """放开确认卡上「不再问你」那段：第 39 轮起项目外也不问，只剩红线（同 describe）。"""
    card = _loosen_card("trusted")
    assert "不再问你" in card, "阳性对照：这张卡本来就该说清放开之后会发生什么"
    assert "里外都一样" in card, "没说清项目外也不问了"
    assert "项目以外的东西还会先问" not in card, "还在承诺项目外会先问你"
    for red in ("家目录", "推代码", "私钥", "sudo"):
        assert red in card, f"放开卡上没写红线 {red!r}：误点一下的后果要让他看清"

def test_loosen_card_to_write_does_not_overpromise():
    """可改档那段原来写「仍不能删、不能跑任意命令」—— 换成按真实行为说。"""
    card = _loosen_card("write", now="readonly")
    for lie in LIES:
        assert lie not in card, f"可改档那段承诺做不到的事：{lie!r}"


# ═════════ 三、帮助：要说清怎么切回只读 ═════════


def test_help_says_the_default_is_full_control_and_how_to_tighten():
    """🔴 `/help` 里「🔑 权限」那行原来只是罗列三档，没说默认是哪一档。

    默认只读的时候「要不要每步问你（只读 / 可改文件 / 全授权）」还算中性；
    默认全控制之后，用户看不出「现在它不问就动手」，也不知道怎么让它开始问。
    """
    line = _help_line("🔑 权限")
    assert line, "帮助里找不到「🔑 权限」那一行"
    # 🔴 判据不能只查「只读」「问你」在不在 —— 旧那行
    # 「要不要每步问你（只读 / 可改文件 / 全授权）」两个词都有，变异验证里假绿过一次。
    # 要分辨的是「有没有说出现在是哪一档」：罗列三个档名不算。
    assert "全授权" in line, f"没说清现在是哪一档：{line!r}"
    assert "现在" in line or "默认" in line, (
        f"只罗列了档名，没说出「现在就是全授权」这个事实：{line!r}"
    )
    assert "只读" in line, f"没说清怎么切到只读：{line!r}"
    assert "问你" in line or "先问" in line, f"没说清切了之后有什么不一样：{line!r}"


def test_help_no_longer_promises_it_will_not_change_things():
    """🔴 帮助「它做不到的」那段原来写「不会删东西」「看不到指定目录之外的东西」。

    两句都和实测对不上（全控制下目录内 `rm` 是 allow、目录外 `ls` 是 allow）。
    """
    from bridge.menu import HELP

    for lie in ("不会删东西", "不会改"):
        assert lie not in HELP, f"帮助里承诺做不到的事：{lie!r}"
    assert "看不到你指定目录之外的东西" not in HELP, (
        "全控制下能看整台电脑（实测 `ls ~/Desktop` → allow）"
    )


def test_help_keeps_the_promises_that_are_still_true(box):
    """阳性对照 + 现场核对：仍然成立的那几条红线，帮助里不许删掉。"""
    from bridge.menu import HELP

    workspace, tmp = box
    assert _decide("git push", trusted=True, workspace=workspace, directory=tmp) == "deny"
    assert _decide("cat ~/.ssh/id_rsa", trusted=True,
                   workspace=workspace, directory=tmp) == "deny"
    assert _decide("sudo ls", trusted=True, workspace=workspace, directory=tmp) == "deny"
    assert "推代码" in HELP or "推送" in HELP, "红线少了「不会推代码」"
    assert "提权" in HELP, "红线少了「不会提权」"
    assert "私钥" in HELP or "密钥" in HELP or "凭据" in HELP, "红线少了「不读凭据」"


def test_help_still_passes_the_old_shape_checks():
    """帮助整段的老判据（示例 / 边界 / 兜底那一行在最后）不许被这一轮改坏。

    和 `test_r11_copy.py` 那几条重复是**有意的**：这一轮动的就是这段文字，
    在同一个文件里留一份判据，改坏时不用去别的文件里找原因。
    """
    import test_copy_quality as gate
    from bridge.menu import HELP

    body, _, last = HELP.rstrip().rpartition("\n")
    assert "按钮出不来" in last, "兜底那一行要留在最后"
    assert not gate.SLASH.findall(body), f"正文里又在教命令：{gate.SLASH.findall(body)}"
    assert "做不到" in HELP or "看不到" in HELP, "要说清边界"


def test_help_button_names_still_exist_on_the_cards():
    """帮助里点名的「…」必须都是真有的按钮字 —— 改文案最容易把名字改歪。"""
    from bridge.botmenu import MENU_TREE
    from bridge.fleet.actions import DEFAULT_LABELS
    from bridge.menu import HELP, CONTROLS

    real = ({label for _, label in CONTROLS} | set(DEFAULT_LABELS.values())
            | {name for name, _, _ in MENU_TREE}
            | {child for _, _, kids in MENU_TREE for child, _ in kids})
    named = re.findall(r"「([^」]*[\U0001F300-\U0001FAFFℹ⏹▶][^」]*)」", HELP)
    missing = [n for n in named if n not in real]
    assert not missing, f"帮助里点名的按钮在卡片上不存在：{missing}"


# ═════════ 四、闸门：改完的文案自己也得过 ═════════


def test_all_new_copy_passes_the_project_copy_gate():
    """🔴 这一轮新写的每一句都要过 `test_copy_quality.py` 那张闸门。

    那张闸门扫整个 `bridge/`，这条只盯本角色改的三个文件 ——
    红了能直接看出是这一轮写坏的，不用在全量命中里翻。
    """
    import test_copy_quality as gate

    mine = {"bridge/menu.py", "bridge/pairing.py", "bridge/policy.py"}
    hits = [h for h in gate.unexplained(gate.all_hits()) if h.unit.file in mine]
    assert not hits, "这一轮的文案被闸门抓到：\n" + "\n".join(h.where() for h in hits)


def test_installer_default_is_full_control():
    """🔴 上面所有「要按全控制说」的事实依据。

    哪天默认又翻回只读，这条先红 —— 提醒把文案一起改回去，
    而不是让用户先收到一句假话（同 `test_menu.py` 那条「安装器没有选档位的分支」的用法）。
    """
    for name in ("install-mac.sh", "install-windows.ps1"):
        path = _SRC / name
        if not path.exists():
            pytest.skip("本地没有安装器源码")
        text = path.read_text(encoding="utf-8")
        assert 'mode = "trusted"' in text, f"{name} 的默认档不是全控制了 ⇒ 文案要跟着改回去"



def test_trusted_prompt_does_not_tell_the_model_outside_is_off_limits():
    """🔴 第 39 轮真 claude 实测：只放开闸门、不改这段提示 ⇒ 项目外写 / 读 4/4 **一个工具都不调**，
    直接回「不在我的范围内，请换项目」。闸门放行了，模型被自己的系统提示拦住 —— 用户看到的还是「做不了」。"""
    from bridge.policy import Policy
    from bridge.runner import _context_for

    pol = Policy(mode="trusted", workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
                 model="claude-sonnet-5-5", turn_timeout_sec=900, ceiling_mode="trusted")
    text = _context_for("/tmp/ws", pol)
    for stale in ("你只能读写这些目录", "范围外的路径不要去试", "在你的范围外"):
        assert stale not in text, f"全授权档的提示还在叫模型别碰项目外：{stale!r}"
    assert "整台电脑" in text
    for red in ("家目录", "git push", "私钥", "sudo"):
        assert red in text, f"提示里没写红线 {red!r}：模型被拒时会当成用户拒了"
    # 阳性对照：只读档照旧说范围
    ro = Policy(mode="readonly", workspaces=("/tmp/ws",), default_workspace="/tmp/ws",
                model="claude-sonnet-5-5", turn_timeout_sec=900, ceiling_mode="trusted")
    assert "你只能读写这些目录" in _context_for("/tmp/ws", ro)
