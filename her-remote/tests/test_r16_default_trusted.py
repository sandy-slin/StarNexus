"""第 16 轮：默认改成全控制，老用户重装 / 自动更新也要跟着升。

## 领导原话

> 这个权限问题太严重了，非常麻烦。默认一定要是全控制的，如果用户要求安全的话，
> 让他单独设定只读权限

## 为什么光改安装器不够（P3 审计实测）

重装会把旧 config 里的 `mode = "readonly"` **原样带回来**（`carryover.CARRY["agent"]` 里有 `mode`）。
自动更新走的也是安装器 + carryover ⇒ 发版 10 分钟内所有老同事自动更新，然后**继续留在只读**，
领导的抱怨不会消失。

## 怎么分清「默认值」和「用户自己选的」

r15 及以前的安装器**不给选项、写死** `mode = "readonly"`（`test_menu.py` 有一条测试盯着这点），
所以旧 config 里的 readonly **不是用户选的**。用户主动选只读只有三种痕迹：
`max_mode = "readonly"`（自己锁了上限）、IT 策略文件、或者手改成了别的档。

迁移规则（新安装器在 `[agent]` 写一行 `defaults_rev`）：
旧文件**没有** `defaults_rev` + 旧 `mode == "readonly"` + 旧 `max_mode != "readonly"`
⇒ 认定是旧默认值，**不带 `mode`**，用新文件的 trusted。其余一律照旧带。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from bridge.carryover import DEFAULTS_REV, carry_over

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick", "src"))


def _new(mode="trusted", rev=True):
    rev_line = f"defaults_rev = {DEFAULTS_REV}\n" if rev else ""
    return ('[feishu]\napp_id = "cli_new"\napp_secret = "s"\nowner_open_ids = []\n'
            '[agent]\nworkspaces = ["/tmp"]\ndefault_workspace = "/tmp"\n'
            f'{rev_line}mode = "{mode}"\nmax_mode = "trusted"\nmodel = "claude-sonnet-5-5"\n'
            # carryover 是**按行替换**，新文件里没有的键它不会凭空加 ⇒ 脚手架要带齐
            # 安装器真会写的那几行（真安装器里这些都在，见 install-mac.sh 的 [agent] 段）
            'turn_timeout_sec = 900\nsession_idle_reset_min = 30\n')


def _old(**kw):
    """造一份旧 config。🔴 默认键要能被 kw 覆盖 —— 第一版直接拼在后面，
    `default_workspace` 出现了两次 ⇒ 旧文件不是合法 TOML ⇒ `carry_over` 原样返回新文件，
    于是测试红的原因是我的脚手架坏了，不是代码坏了（当场差点改错东西）。
    """
    fields = {"workspaces": ["/tmp"], "default_workspace": "/tmp"}
    fields.update(kw)
    body = '[feishu]\napp_id = "cli_old"\napp_secret = "s"\nowner_open_ids = ["ou_x"]\n[agent]\n'
    for k, v in fields.items():
        if isinstance(v, str):
            body += f'{k} = "{v}"\n'
        elif isinstance(v, list):
            body += f"{k} = {v!r}\n".replace("'", '"')
        else:
            body += f"{k} = {v}\n"
    return body


def test_unparseable_old_config_does_not_migrate():
    """旧文件读不懂时不许「顺手迁移」。
    🔴 第 32 轮：也不许原样用新文件（默认全授权）——那会把他选的只读静默放开。
    改成抛错给 main，由 main 压到只读并说清楚（tests/test_carryover.py 那条真跑 main）。"""
    import pytest

    from bridge.carryover import OldConfigUnreadable, fail_closed

    with pytest.raises(OldConfigUnreadable):
        carry_over("[agent]\nmode = readonly broken", _new())
    assert _mode_of(fail_closed(_new())) == "readonly"


def _mode_of(text):
    import tomllib

    return tomllib.loads(text).get("agent", {}).get("mode")


# ---------- 老用户重装：没动过默认值的要升上去 ----------

def test_r14_era_default_readonly_is_migrated_to_trusted():
    """P3 的 C 行：r14~r15 装的、没动过（readonly + max_mode=trusted）⇒ 升到 trusted。"""
    out, carried = carry_over(_old(mode="readonly", max_mode="trusted"), _new())
    assert _mode_of(out) == "trusted", out
    assert "agent.mode" not in carried


def test_pre_r14_default_readonly_is_migrated():
    """P3 的 B 行：r14 之前装的，只有 mode=readonly、没有 max_mode。"""
    out, _ = carry_over(_old(mode="readonly"), _new())
    assert _mode_of(out) == "trusted"


# ---------- 用户主动选过的，不许动 ----------

def test_user_locked_readonly_is_respected():
    """🔴 自己把上限锁成只读 = 主动选的，照旧带回来。"""
    out, carried = carry_over(_old(mode="readonly", max_mode="readonly"), _new())
    assert _mode_of(out) == "readonly", out
    assert "agent.mode" in carried


def test_hand_edited_write_mode_is_respected():
    out, _ = carry_over(_old(mode="write", max_mode="trusted"), _new())
    assert _mode_of(out) == "write"


def test_readonly_chosen_after_this_round_is_respected():
    """r16 之后装的（有 defaults_rev）再改成只读 ⇒ 那是他自己选的，照旧带。"""
    out, _ = carry_over(_old(mode="readonly", max_mode="trusted", defaults_rev=DEFAULTS_REV), _new())
    assert _mode_of(out) == "readonly"


def test_migration_only_touches_mode():
    """迁移不许顺手改别的键（项目目录、模型、超时都要照旧带）。"""
    old = _old(mode="readonly", model="claude-opus-5-5", turn_timeout_sec=1234,
               default_workspace="/tmp/mine")
    out, carried = carry_over(old, _new())
    import tomllib

    agent = tomllib.loads(out)["agent"]
    assert agent["model"] == "claude-opus-5-5"
    assert agent["turn_timeout_sec"] == 1234
    assert agent["default_workspace"] == "/tmp/mine"
    assert agent["mode"] == "trusted"


def test_migration_is_recorded_so_the_user_can_be_told():
    """🔴 悄悄把人从只读升到全授权是不行的 —— 要留痕，桥启动时好告诉他。"""
    out, _ = carry_over(_old(mode="readonly", max_mode="trusted"), _new())
    import tomllib

    assert tomllib.loads(out)["agent"].get("migrated_from") == "readonly"


def test_no_migration_record_when_nothing_migrated():
    out, _ = carry_over(_old(mode="readonly", max_mode="readonly"), _new())
    import tomllib

    assert "migrated_from" not in tomllib.loads(out).get("agent", {})


# ---------- 安装器默认 ----------

@pytest.mark.parametrize("name", ["install-mac.sh", "install-windows.ps1"])
def test_installer_writes_trusted_by_default(name):
    text = open(os.path.join(_SRC, name), encoding="utf-8").read()
    assert 'mode = "trusted"' in text, f"{name} 默认还不是全授权"
    assert f"defaults_rev = {DEFAULTS_REV}" in text, f"{name} 没写 defaults_rev，迁移规则分不出默认值"


@pytest.mark.parametrize("name", ["install-mac.sh", "install-windows.ps1"])
def test_installer_tells_how_to_go_readonly(name):
    """领导说「要安全的话让他单独设定只读」⇒ 安装器要说清怎么切。"""
    text = open(os.path.join(_SRC, name), encoding="utf-8").read()
    assert "只读" in text and 'mode = "readonly"' in text


# ---------- 迁移了要告诉用户（不许悄悄升档） ----------


def test_bridge_reads_the_migration_record():
    """🔴 悄悄把人从只读升到全控制不行。桥启动后要能看出「这次被迁移了」并告诉他。

    自动更新那条路上没有人看安装器的屏幕（`selfupdate` 在后台跑），所以只能由桥来说。
    """
    import inspect

    from bridge import app as app_mod

    cls = next(v for v in vars(app_mod).values()
               if inspect.isclass(v) and hasattr(v, "_watchdog_loop"))
    src = inspect.getsource(cls)
    assert "migrated_from" in src, "桥不读迁移记录 ⇒ 用户不会知道自己的档位被改了"


def test_migration_notice_says_what_changed_and_how_to_undo(tmp_path):
    """那句话要说清：变成了什么、怎么改回去。"""
    from bridge.selfcheck import migration_notice

    text = migration_notice("readonly", "trusted")
    assert text, "没给出通知文案"
    assert "只读" in text and ("全" in text or "不问" in text)
    assert "权限" in text, "没告诉他去哪儿改回来"
    for bad in ("migrated_from", "readonly", "trusted", "config.toml"):
        assert bad not in text, f"给用户看的话里有内部名词：{bad}"


def test_no_notice_when_not_migrated():
    from bridge.selfcheck import migration_notice

    assert migration_notice("", "trusted") == ""
    assert migration_notice(None, "trusted") == ""


# ---------- 🔴 迁移了，但磁盘上那份旧档位把它顶掉了 ----------


def test_prefs_readonly_is_cleared_when_migrating(tmp_path):
    """现场：用户更新完，卡片还显示「现在是 只读」（2026-11-07 14:04 截图）。

    配置里已经是 trusted、`migrated_from = "readonly"` 也写上了，但 `prefs.json` 里
    还留着老版本点过的 `{"mode": "readonly"}`，而 `PrefsStore` 只按**上限**收敛、不看
    config 里的档 ⇒ 它原样顶掉新默认。实测：config=trusted + prefs=readonly ⇒ 生效 readonly。

    P3 的审计报告第 2c 节第 3 步明写了「只有真的发生迁移时，才把 prefs 里的
    `"mode":"readonly"` 删掉」，并且用 C 行实测标了「所以第 3 步不能省」——我漏做了这一步。

    为什么删它是安全的：旧默认下**主动选只读在 prefs 里不留痕迹**（`set_mode` 在目标档
    等于 config 档时会清掉覆盖，P3 实测过），所以留下 `{"mode":"readonly"}` 的只可能是
    更早版本点了一下当前档留下的噪音。`trusted` / `write` 一律保留。
    """
    from bridge.prefs import PrefsStore, clear_readonly_override

    path = tmp_path / "prefs.json"
    path.write_text('{"mode": "readonly", "model": "claude-opus-5"}', encoding="utf-8")
    assert clear_readonly_override(str(path)) is True
    import json as _json

    left = _json.loads(path.read_text(encoding="utf-8"))
    assert "mode" not in left, left
    assert left.get("model") == "claude-opus-5", "顺手把模型选择也删了"
    assert isinstance(PrefsStore, type)


def test_prefs_other_modes_are_kept(tmp_path):
    """阳性对照：他自己切到全授权/可改文件的记录不许动。"""
    from bridge.prefs import clear_readonly_override

    for mode in ("trusted", "write"):
        path = tmp_path / f"prefs-{mode}.json"
        path.write_text('{"mode": "%s"}' % mode, encoding="utf-8")
        assert clear_readonly_override(str(path)) is False
        assert mode in path.read_text(encoding="utf-8")


def test_missing_or_broken_prefs_is_not_an_error(tmp_path):
    from bridge.prefs import clear_readonly_override

    assert clear_readonly_override(str(tmp_path / "nope.json")) is False
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert clear_readonly_override(str(bad)) is False


def test_bridge_clears_the_override_on_migration():
    """调用点还在：桥发现这次是迁移来的 ⇒ 要连 prefs 里那份旧档位一起清掉。"""
    import inspect

    from bridge import app as app_mod

    cls = next(v for v in vars(app_mod).values()
               if inspect.isclass(v) and hasattr(v, "_tell_about_migration"))
    src = inspect.getsource(cls._tell_about_migration)
    assert "clear_readonly_override" in src, "迁移时没清掉 prefs 里的旧只读 ⇒ 用户看到的还是只读"
