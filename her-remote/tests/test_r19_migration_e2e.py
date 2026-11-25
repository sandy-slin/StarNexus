"""第 19 轮 S4：真实形状的老机器，从头到尾走一遍「自动更新」那条路，看最终落到哪一档。

## 为什么要这条

默认全控制改了三层（安装器默认 / carryover 迁移 / prefs 清旧只读），每层都有单元测试，
但**从来没把一台老机器从头跑到尾**。主人截图「更新后还是只读」那次，就是第三层漏了，
而前两层的单元测试当时全绿。

## 怎么跑（全部真跑，不查源码文本）

1. 在 `tmp_path` 下造假 HOME：`~/.carher-feishu-agent/{config.toml, prefs.json, menu_heal.json}`，
   形状照抄真实机器（本机 `~/.carher-feishu-agent/` 只读参考，以及本机留着的两份 r14 之前的
   安装包 `~/Desktop/feishu-agent-mac.command`、`~/.Trash/feishu-agent-mac.command` 里的 config 段）。
2. 从 `feishu-agent-oneclick/src/install-mac.sh` **抽原段落**用 bash 真跑：
   沿用旧机器人 → 沿用旧主人 → 沿用旧目录 → 写新 config → 调 carryover.py → 读 INSTALLED_MODE → 尾屏 case。
   （自动更新跑的就是这个安装包：`selfupdate.run_installer` = `/bin/bash 安装包`，stdin=/dev/null。）
3. 用同一个假 HOME 起一个**真的** `Bridge(Settings.load(...))`（换掉 replier，不碰飞书），
   挂一个真的事件循环，调启动时那一步 `_tell_about_migration()`，再看 `bridge.policy`。
   然后**再起一次**（看门狗重启是常态），看通知会不会重发。

IT 策略文件换成不存在的路径（否则在被 IT 管的机器上结果不确定）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.normpath(os.path.join(_HERE, ".."))
MAC = os.path.normpath(os.path.join(_REPO, "..", "..", "feishu-agent-oneclick", "src",
                                    "install-mac.sh"))
CARRYOVER = os.path.join(_REPO, "bridge", "carryover.py")

APP_ID = "cli_fake00000000r19"
OWNER = "ou_fakeowner_r19"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------- 老机器的 config.toml：照抄当时安装器写出来的样子 ----------

_HEAD = """# CarHer 飞书助手配置。由一键脚本生成，可以手改。
# 改完重启：launchctl kickstart -k gui/\\$(id -u)/com.carher.feishu-agent

[feishu]
app_id = "{app_id}"
app_secret = "fake-secret"
# 只有这些人能驱动这台电脑。空列表 = 谁都不行。
owner_open_ids = ["{owner}"]
# 一次性配对码：上面为空时，**只有**正文等于这个码的消息能把发送者绑成主人。
# 绑定后这几行会被自动删掉。过期或试错 5 次后作废，要重新生成见 README。
# 已经认出主人了，不需要配对码

[agent]
workspaces = ["{ws}"]
default_workspace = "{ws}"
"""
_TAIL = """model = "claude-sonnet-5-5"
turn_timeout_sec = 900
session_idle_reset_min = 30

[limits]
max_reply_chars = 20000
progress_first_notice_sec = 90
progress_escalate_sec = 420
progress_max_notices = 2

[review]
# 你自己加的禁止项（正则）。只能更严 —— 写什么都放不开上面那些硬拒项。
# 例：deny_patterns = ["生产|prod", "客户数据"]
deny_patterns = []
"""

# r14 之前（本机两份老安装包里逐字一致）：只有 mode，没有 max_mode / defaults_rev
_AGENT_PRE_R14 = """# readonly = 默认只看；要改文件会先在飞书里弹卡片问你，点同意才动手
#            （2026-11-05 按实测行为改的说法：只读**不等于**改不了）
# write    = 可以改文件（仍不能 git 提交/推送、不能删）
mode = "readonly"
"""

# r14~r15：拆出 max_mode（上限 trusted），起步档仍写死 readonly（tests/test_r14_max_mode.py 当时钉的形状）
_AGENT_R14 = """# readonly = 默认只看；要改文件会先在飞书里弹卡片问你，点同意才动手
# write    = 可以改文件（仍不能 git 提交/推送、不能删）
mode = "readonly"
# max_mode = 手机上「🔑 权限」最多能切到哪一档。上面 mode 是起步档。
max_mode = "trusted"
"""

# 自己锁过只读：r14~r15 的文件，把 max_mode 手改成 readonly
_AGENT_LOCKED = _AGENT_R14.replace('max_mode = "trusted"', 'max_mode = "readonly"')

# r16 之后装的（当前安装器写的形状）
_AGENT_R16 = """defaults_rev = 16
mode = "trusted"
max_mode = "trusted"
"""


def _config(agent_block: str, ws: str) -> str:
    return _HEAD.format(app_id=APP_ID, owner=OWNER, ws=ws) + agent_block + _TAIL


def _menu_heal_json() -> str:
    # 照抄本机真实 menu_heal.json 的形状（app_id 换成假的）
    return json.dumps({"apps": {APP_ID: {
        "done": True, "version": "1.20261106.120000", "at": 1794000000.0,
        "last_error": "", "scopes": "195a2383148f"}}}, indent=2)


# ---------- 第 1 步：抽安装器原段落，在假 HOME 下真跑 ----------


def _segment(text: str, start: str, end: str, *, include_end: bool = False) -> str:
    i = text.index(start)
    j = text.index(end, i)
    if include_end:
        j = text.index("\n", j) + 1
    return text[i:j]


def _installer_script() -> str:
    """把安装器里「重装 → 写配置 → carryover → 读档位 → 尾屏」那几段原样抽出来拼成一个脚本。

    跳过的只有：下载/建 venv/装依赖/扫码/注册机器人/launchctl/发菜单（都要网络或动 launchd）。
    这些在重装路径上不碰 config.toml 的 [agent] 段，跳过不影响档位。
    """
    text = _read(MAC)
    prelude = "\n".join(
        re.search(pattern, text, re.M).group(0)
        for pattern in (
            r"^BLUE=.*$", r"^say\(\).*$", r"^ok\(\).*$", r"^warn\(\).*$", r"^die\(\).*$",
            r"^APP_HOME=.*$", r"^VENV=.*$", r"^CONFIG=.*$", r"^LABEL=.*$",
        )
    )
    bot = _segment(text, 'EXIST_ID=""\n', 'if [ -z "${APP_ID:-}" ]; then')
    owner = _segment(text, 'EXISTING_OWNER=""\n', 'WORKSPACE_DEFAULT="$HOME"')
    write = _segment(text, 'WORKSPACE_DEFAULT="$HOME"', "INSTALLED_MODE=", include_end=True)
    screen = _segment(text, 'case "${INSTALLED_MODE:-trusted}" in', "esac", include_end=True)
    return (
        "set -euo pipefail\n" + prelude + "\n"
        'PREBAKED=""\nOWNER_OPEN_ID=""\n'
        + bot + owner + write
        + 'echo "@@INSTALLED_MODE=${INSTALLED_MODE}"\necho "@@SCREEN-BEGIN"\n'
        + screen + 'echo "@@SCREEN-END"\n'
    )


def _fake_home(tmp_path, agent_block, prefs=None):
    home = tmp_path / "home"
    app = home / ".carher-feishu-agent"
    (app / "bridge").mkdir(parents=True)
    (app / "venv" / "bin").mkdir(parents=True)
    (app / "venv" / "bin" / "python").symlink_to(sys.executable)
    # 安装器第 4 步把 bridge/carryover.py 原样嵌进包里（@@EMBED）再写到这里 ⇒ 拷仓库里那份等价
    (app / "bridge" / "carryover.py").write_text(_read(CARRYOVER), encoding="utf-8")
    # 第 19 轮：尾屏改成照桥同一套顺序算档位（bridge/prefs.py::installed_mode）⇒ 这两个也要在
    (app / "bridge" / "__init__.py").write_text("", encoding="utf-8")
    for name in ("policy.py", "prefs.py"):
        (app / "bridge" / name).write_text(_read(os.path.join(_REPO, "bridge", name)), encoding="utf-8")
    ws = home / "codes"
    ws.mkdir()
    (app / "config.toml").write_text(_config(agent_block, str(ws)), encoding="utf-8")
    os.chmod(app / "config.toml", 0o600)
    if prefs is not None:
        (app / "prefs.json").write_text(json.dumps(prefs), encoding="utf-8")
    (app / "menu_heal.json").write_text(_menu_heal_json(), encoding="utf-8")
    return home, app


def _run_installer(home, extra_env=None) -> dict:
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "en_US.UTF-8",
           **(extra_env or {})}
    proc = subprocess.run(["/bin/bash", "-c", _installer_script()], capture_output=True,
                          text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, f"安装器段落没跑完 rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    out = proc.stdout
    mode = re.search(r"^@@INSTALLED_MODE=(.*)$", out, re.M).group(1)
    screen = out.split("@@SCREEN-BEGIN", 1)[1].split("@@SCREEN-END", 1)[0]
    return {"stdout": out, "installed_mode": mode, "screen": screen}


def _screen_says(screen: str) -> str:
    """尾屏说的是哪一档（按方括号里那个词认，不钉整句）。"""
    found = re.findall(r"【(只读|改文件|全授权)】", screen)
    assert len(found) == 1, f"尾屏应该只说一个档位：{screen!r}"
    return {"只读": "readonly", "改文件": "write", "全授权": "trusted"}[found[0]]


# ---------- 第 2 步：用同一个假 HOME 真起一个 Bridge ----------


class _FakeReplier:
    """只记录、不发。send_card 有 ⇒ 走「文字 + 按钮小卡片」那条路（和线上一样）。"""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.texts: list[tuple[str, str]] = []
        self.cards: list[tuple[str, str]] = []

    def send(self, chat_id, text):
        self.texts.append((chat_id, text))
        return self.ok

    def send_card(self, chat_id, card_json):
        self.cards.append((chat_id, card_json))
        return self.ok

    def ack(self, *_a, **_k):
        return True

    def unack(self, *_a, **_k):
        return True


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """IT 策略文件指到不存在的地方；HOME 指到假 HOME（Bridge 里有几处 expanduser）。"""
    from bridge import policy as policy_mod

    monkeypatch.setattr(policy_mod, "system_policy_file",
                        lambda: str(tmp_path / "no-such-it-policy.toml"))
    monkeypatch.delenv("CARHER_POLICY_FILE", raising=False)
    monkeypatch.delenv("FEISHU_BRIDGE_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_BRIDGE_APP_SECRET", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


def _start_bridge(app_dir, replier_ok=True):
    """照 `Bridge.run()` 的顺序：构造（读 config + prefs）→ 设事件循环 → `_tell_about_migration`。

    不起长连接、不起线程、不 run_forever；通知走真的 `_say_later_with_buttons`
    （事件循环 + executor），跑到发完为止。
    """
    import asyncio

    from bridge.app import Bridge, Settings

    settings = Settings.load(str(app_dir / "config.toml"))
    bridge = Bridge(settings)
    replier = _FakeReplier(ok=replier_ok)
    bridge.replier = replier
    loop = asyncio.new_event_loop()
    bridge._loop = loop
    try:
        mode_before_tell = bridge.policy.mode
        bridge._tell_about_migration()
        # call_soon_threadsafe 排进来的回调跑一拍 ⇒ 它们把发送丢进默认 executor；
        # shutdown_default_executor 等这些线程跑完 ⇒ 发没发、发了几条是确定的。
        loop.run_until_complete(asyncio.sleep(0))
        loop.run_until_complete(loop.shutdown_default_executor())
    finally:
        loop.close()
    return {
        "bridge": bridge,
        "replier": replier,
        "mode_before_tell": mode_before_tell,
        "mode": bridge.policy.mode,
        "ceiling": bridge.policy.ceiling_mode,
        "model": bridge.policy.model,
    }


def _has_migration_line(app_dir) -> bool:
    return bool(re.search(r"^\s*migrated_from\s*=", (app_dir / "config.toml").read_text("utf-8"),
                          re.M))


def _prefs(app_dir):
    path = app_dir / "prefs.json"
    return json.loads(path.read_text("utf-8")) if path.exists() else None


def _upgrade(tmp_path, agent_block, prefs=None, replier_ok=True, installer_env=None):
    """一台老机器走一遍：自动更新跑安装器 → 桥启动 → 看门狗再重启一次。"""
    home, app = _fake_home(tmp_path, agent_block, prefs)
    menu_before = (app / "menu_heal.json").read_text("utf-8")
    inst = _run_installer(home, installer_env)
    after_install = {
        "migrated_line": _has_migration_line(app),
        "prefs": _prefs(app),
    }
    first = _start_bridge(app, replier_ok=replier_ok)
    after_first = {"migrated_line": _has_migration_line(app), "prefs": _prefs(app)}
    second = _start_bridge(app)
    return {
        "app": app, "inst": inst, "after_install": after_install,
        "first": first, "after_first": after_first, "second": second,
        "menu_untouched": (app / "menu_heal.json").read_text("utf-8") == menu_before,
    }


# ====================================================================
# 四种老机器（任务要求）。期望值 = 领导原话「默认全控制，要安全单独设只读」
# ====================================================================

CASES = {
    # 名字: (旧 [agent] 段, 旧 prefs.json, 期望生效档, 期望上限, 该不该通知)
    "A-r14前没动过": (_AGENT_PRE_R14, None, "trusted", "trusted", True),
    "B-r14~15没动过": (_AGENT_R14, None, "trusted", "trusted", True),
    "C-同B但prefs留着只读": (_AGENT_R14, {"mode": "readonly"}, "trusted", "trusted", True),
    "D-自己锁过只读": (_AGENT_LOCKED, None, "readonly", "readonly", False),
}


@pytest.fixture(params=list(CASES), ids=list(CASES))
def upgraded(request, isolated):
    agent, prefs, want_mode, want_ceiling, want_notice = CASES[request.param]
    result = _upgrade(isolated, agent, prefs)
    result.update(name=request.param, want_mode=want_mode, want_ceiling=want_ceiling,
                  want_notice=want_notice)
    return result


def test_final_mode_after_auto_update(upgraded):
    """🔴 核心：自动更新 + 桥启动之后，**真正生效**的是哪一档（`bridge.policy`，每一轮 claude 用的就是它）。"""
    first, second = upgraded["first"], upgraded["second"]
    assert first["mode"] == upgraded["want_mode"], (
        f"{upgraded['name']}：升级后生效 {first['mode']}，应为 {upgraded['want_mode']}")
    assert first["ceiling"] == upgraded["want_ceiling"], upgraded["name"]
    # 看门狗再重启一次也不能回退
    assert second["mode"] == upgraded["want_mode"], f"{upgraded['name']}：第二次启动后变成了 {second['mode']}"


def test_installer_writes_the_same_mode_it_will_run(upgraded):
    """安装器写进配置的那一档（INSTALLED_MODE，读在 carryover 之后）= 桥最后生效的那一档。"""
    assert upgraded["inst"]["installed_mode"] == upgraded["want_mode"], upgraded["inst"]["stdout"]


def test_final_screen_matches_the_real_mode(upgraded):
    """尾屏说的那一档 = 桥启动后真正生效的那一档（四种都要对上，不只是和 config 对上）。"""
    said = _screen_says(upgraded["inst"]["screen"])
    assert said == upgraded["first"]["mode"], (
        f"{upgraded['name']}：尾屏说 {said}，实际 {upgraded['first']['mode']}\n{upgraded['inst']['screen']}")


def test_migration_notice_sent_exactly_once(upgraded):
    """被升档的人收到一次（话 + 按钮卡），发给主人；重启后不再发。没被升档的人一条都不收。"""
    first, second = upgraded["first"], upgraded["second"]
    texts, cards = first["replier"].texts, first["replier"].cards
    if upgraded["want_notice"]:
        assert len(texts) == 1 and len(cards) == 1, (upgraded["name"], texts, cards)
        assert texts[0][0] == OWNER and cards[0][0] == OWNER
        # 语义：说清「之前只看 → 现在放开」，并给了改回去的入口（按钮卡里有 /mode 那个按钮）
        assert "只看" in texts[0][1]
        # 原来 `"mode" in card_json` 恒真（schema 里就有别的 mode 字样）⇒ 改成按结构找那颗按钮
        assert '"op": "mode"' in cards[0][1].replace("'", '"'), cards[0][1]
    else:
        assert texts == [] and cards == [], (upgraded["name"], texts, cards)
    assert second["replier"].texts == [] and second["replier"].cards == [], \
        f"{upgraded['name']}：重启后又发了一遍"


def test_migration_marker_written_then_erased(upgraded):
    """carryover 写 `migrated_from`（只在真迁移时），桥说完把它擦掉。"""
    assert upgraded["after_install"]["migrated_line"] is upgraded["want_notice"], upgraded["name"]
    assert upgraded["after_first"]["migrated_line"] is False, upgraded["name"]


def test_upgrade_does_not_touch_menu_heal_state(upgraded):
    """menu_heal.json 照旧（否则每次升级都会重写一遍菜单、重发一次版本）。"""
    assert upgraded["menu_untouched"], upgraded["name"]


def test_rest_of_config_survives(upgraded):
    """顺带：主人、机器人、目录都沿用（升级不能把人解绑）。"""
    import tomllib

    raw = tomllib.loads((upgraded["app"] / "config.toml").read_text("utf-8"))
    assert raw["feishu"]["app_id"] == APP_ID
    assert raw["feishu"]["owner_open_ids"] == [OWNER]
    assert raw["agent"]["default_workspace"].endswith("/codes")


# ---------- C 的细节：清的是 mode 一个键 ----------


def test_c_prefs_readonly_cleared_but_model_kept(isolated):
    """C 形状再加一个模型选择：只读档被清掉、模型选择留着。"""
    result = _upgrade(isolated, _AGENT_R14, {"mode": "readonly", "model": "claude-haiku-5-5"})
    # 第 19 轮起清旧只读前移到安装器（尾屏要按真实档位说）⇒ 装完 prefs 里就只剩模型了
    assert result["after_install"]["prefs"] == {"model": "claude-haiku-5-5"}, "安装器没清旧只读"
    assert result["first"]["mode"] == "trusted"
    assert result["after_first"]["prefs"] == {"model": "claude-haiku-5-5"}
    assert result["first"]["model"] == "claude-haiku-5-5"


# ---------- 反向用例：不该升的不许升 ----------


def test_locked_user_with_prefs_readonly_stays_readonly(isolated):
    """D + prefs 里也是只读：prefs 不许被清（没发生迁移），档位不许升。"""
    result = _upgrade(isolated, _AGENT_LOCKED, {"mode": "readonly"})
    assert result["first"]["mode"] == "readonly"
    assert result["after_first"]["prefs"] == {"mode": "readonly"}
    assert result["first"]["replier"].texts == []


def test_r16_user_who_chose_readonly_on_phone_stays_readonly(isolated):
    """r16 之后装的、在手机上自己切到只读（prefs 落盘 readonly）⇒ 再更新也不许被升。"""
    result = _upgrade(isolated, _AGENT_R16, {"mode": "readonly"})
    assert result["first"]["mode"] == "readonly"
    assert result["after_first"]["prefs"] == {"mode": "readonly"}
    assert result["first"]["replier"].texts == []


def test_it_policy_still_wins_over_migration(isolated):
    """IT 策略锁只读的机器：迁移改的是 config，生效档仍被 IT 压在只读，且不发「放开了」。"""
    it = isolated / "it-policy.toml"
    it.write_text('mode = "readonly"\n', encoding="utf-8")
    from bridge import policy as policy_mod

    policy_mod.system_policy_file = lambda: str(it)   # isolated 的 monkeypatch 会在结束时还原
    result = _upgrade(isolated, _AGENT_R14)
    assert result["first"]["mode"] == "readonly" and result["first"]["ceiling"] == "readonly"
    assert result["first"]["replier"].texts == [], "IT 锁着只读，却告诉用户「放开了」"


# ====================================================================
# 已知缺口（xfail strict：修好那天会变成 XPASS ⇒ 红，提醒删掉 xfail）
# ====================================================================


def test_marker_kept_when_notice_not_delivered(isolated):
    """飞书发送失败（replier 返回 False）⇒ 用户没收到「权限放开了」，标记却已经擦了，
    下次启动也不会再说 ⇒ 被悄悄从只读升到全授权。"""
    result = _upgrade(isolated, _AGENT_R14, replier_ok=False)
    assert result["first"]["mode"] == "trusted"
    assert result["after_first"]["migrated_line"] is True, "没送达就把标记擦了"


def test_final_screen_matches_real_mode_when_prefs_override(isolated):
    """r16 之后在手机上切过只读的人重装：尾屏说【全授权】，桥实际是只读。"""
    result = _upgrade(isolated, _AGENT_R16, {"mode": "readonly"})
    assert _screen_says(result["inst"]["screen"]) == result["first"]["mode"]


def test_final_screen_matches_real_mode_under_it_lock(isolated):
    """IT 策略锁只读：尾屏原来照 config 说【全授权】，桥实际只读（S4 记录的同方向缺口）。"""
    it = isolated / "it-policy.toml"
    it.write_text('mode = "readonly"\n', encoding="utf-8")
    from bridge import policy as policy_mod

    policy_mod.system_policy_file = lambda: str(it)
    result = _upgrade(isolated, _AGENT_R14, installer_env={"CARHER_POLICY_FILE": str(it)})
    assert result["first"]["mode"] == "readonly"
    assert _screen_says(result["inst"]["screen"]) == "readonly"


@pytest.mark.parametrize("name", list(CASES))
def test_final_screen_matches_real_mode_every_shape(isolated, name):
    """四种老机器逐个对：尾屏说的 = 桥起来后真正生效的。"""
    agent, prefs, *_ = CASES[name]
    result = _upgrade(isolated, agent, prefs)
    assert _screen_says(result["inst"]["screen"]) == result["first"]["mode"], name


# ====================================================================
# 第 24 轮 R-install 复现的两条「静默升档」
# ====================================================================


def _undelivered(tmp_path):
    """老只读机器自动更新 → 桥启动但通知没送达（标记留着）。不走 `_upgrade`：它第二次启动是能送达的。"""
    home, app = _fake_home(tmp_path, _AGENT_R14)
    _run_installer(home)
    first = _start_bridge(app, replier_ok=False)
    assert first["mode"] == "trusted" and _has_migration_line(app), "前提：没送达、标记留着"
    return home, app


def test_marker_survives_next_auto_update_when_notice_never_arrived(isolated):
    """通知没送达 ⇒ 下一次自动更新（旧文件已有 defaults_rev）不能把标记丢掉。"""
    home, app = _undelivered(isolated)
    _run_installer(home)                                   # 第二次自动更新
    assert _has_migration_line(app), "第二次更新把没送达的标记丢了 ⇒ 永远不通知"
    again = _start_bridge(app)
    assert len(again["replier"].texts) == 1, "送得到了却没说「放开了」"
    assert _has_migration_line(app) is False


def test_marker_not_duplicated_by_repeated_updates(isolated):
    home, app = _undelivered(isolated)
    _run_installer(home)
    _run_installer(home)
    text = (app / "config.toml").read_text("utf-8")
    assert len(re.findall(r"^\s*migrated_from\s*=", text, re.M)) == 1, text


def test_user_choosing_readonly_while_marker_pending_is_not_undone(isolated):
    """没送达期间他自己在手机上切到只读 ⇒ 下次启动不许把他的只读清掉。"""
    import asyncio

    home, app = _undelivered(isolated)
    b = _start_bridge(app, replier_ok=False)["bridge"]
    b._say = lambda *a, **k: True
    b._say_with_buttons = lambda *a, **k: True
    asyncio.run(b._handle_command("dm:" + OWNER, OWNER, "/mode readonly"))
    assert _prefs(app) == {"mode": "readonly"}, f"前提：只读没落盘 {_prefs(app)}"
    nxt = _start_bridge(app)
    assert nxt["mode"] == "readonly", "他刚选的只读被下次启动清掉了 ⇒ 静默升回全授权"
    assert _prefs(app) == {"mode": "readonly"}
    assert nxt["replier"].texts == [], "他已经亲手选过档，不该再说「权限放开了」"
