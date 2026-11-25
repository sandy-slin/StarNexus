"""老用户的 `turn_timeout_sec = 900` 要跟着默认值升到 1800。

用户（某同事）11-20：「15 分钟 timeout 这个有点太短了，设成默认 30 分钟吧」。
🔴 11-21 他又说「我早上仍然遇到这个 15 分钟超时」——
我 11-20 只改了安装器模板和代码兜底，**漏了 `carryover.py`**：
`turn_timeout_sec` 在 `CARRY` 里 ⇒ 每次自动更新都把旧的 900 原样搬回来。

实测复现（拿真 config 走一遍 `carry_over`）：模板 1800 + 旧文件 900 ⇒ 合并后 **900**。
我自己这台也一样（已更新到最新版，配置里仍是 900）。

和第 16 轮改默认档位时同一个坑 —— 那次注释就写着
「光改安装器的默认值**对老用户无效**」。这次用它建的 `defaults_rev` 机制解决。

判据：`defaults_rev < 17` **且** 值正好等于 900（r16 模板写死的那个）⇒ 算旧默认，不带。
其余一律照旧带（他自己设过的值绝不覆盖）。
"""

from __future__ import annotations

import os
import pathlib

import pytest

from bridge.carryover import CARRY, DEFAULTS_REV, _migrating_turn_timeout, carry_over

_HERE = os.path.dirname(os.path.abspath(__file__))
_INSTALLER = os.path.normpath(os.path.join(_HERE, "..", "..", "..",
                                           "feishu-agent-oneclick", "src", "install-mac.sh"))


@pytest.fixture(scope="module")
def template() -> str:
    """从安装器里抽出 config 模板那一段（线上真正写给用户的那份）。"""
    ins = pathlib.Path(_INSTALLER).read_text(encoding="utf-8")
    i = ins.index(f"defaults_rev = {DEFAULTS_REV}")
    j = ins.index("\n", ins.index("progress_max_notices", i)) + 1
    return "[agent]\n" + ins[i:j]


def _merged_timeout(old_toml: str, template: str) -> int:
    merged, _ = carry_over(old_toml, template)
    line = [l for l in merged.splitlines() if "turn_timeout_sec" in l][0]
    return int(line.split("=")[1].strip())


def test_installer_and_code_agree():
    """🔴 比常量，别写死数字（11-22 改成 3600 时这条才不用再动一次）。"""
    from bridge.app import DEFAULT_TURN_TIMEOUT_SEC
    ins = pathlib.Path(_INSTALLER).read_text(encoding="utf-8")
    assert f"turn_timeout_sec = {DEFAULT_TURN_TIMEOUT_SEC}" in ins
    assert f"defaults_rev = {DEFAULTS_REV}" in ins, "模板的 defaults_rev 没跟上代码"


def test_defaults_rev_bumped():
    """改了默认值就要进一轮 —— 不进的话下次更新会把已经升过的再迁一遍。

    11-22（r18，1800 → 3600）又进了一轮，所以这里是 >= 18。
    """
    assert DEFAULTS_REV >= 18


def test_old_default_is_upgraded(template):
    """🔴 他的现场：r16 老文件里的 900 是写死的旧默认 ⇒ 升到**当代**默认。

    11-22 起当代是 3600；判据取常量，以后再改默认值这条不用动。
    """
    from bridge.app import DEFAULT_TURN_TIMEOUT_SEC
    old = '[agent]\nmode = "trusted"\nturn_timeout_sec = 900\n'
    assert _merged_timeout(old, template) == DEFAULT_TURN_TIMEOUT_SEC


@pytest.mark.parametrize("value", [60, 600, 1200, 5400, 7200])
def test_user_set_value_is_kept(value, template):
    """阳性对照：他自己设过的值**一个都不许动**。

    🔴 11-22：样本里的 3600 换成 5400 —— 3600 现在正是新默认值，
    留着它这条就测不出「保住了他的值」和「恰好撞上新默认」的区别。
    """
    old = f'[agent]\nmode = "trusted"\nturn_timeout_sec = {value}\n'
    assert _merged_timeout(old, template) == value


def test_900_set_after_r17_is_kept(template):
    """最容易做错的一条：r17 之后他**手动**设成 900 ⇒ 那是他的选择，不许改。"""
    old = '[agent]\ndefaults_rev = 17\nmode = "trusted"\nturn_timeout_sec = 900\n'
    assert _merged_timeout(old, template) == 900


def test_missing_key_just_uses_the_new_default(template):
    """旧文件里压根没这一行 ⇒ 用新模板的值（原有行为，别改坏）。"""
    from bridge.app import DEFAULT_TURN_TIMEOUT_SEC
    old = '[agent]\nmode = "trusted"\n'
    assert _merged_timeout(old, template) == DEFAULT_TURN_TIMEOUT_SEC


@pytest.mark.parametrize("rev", [None, 0, 15, 16])
def test_migrating_predicate_on_old_revs(rev):
    agent = {"turn_timeout_sec": 900}
    if rev is not None:
        agent["defaults_rev"] = rev
    assert _migrating_turn_timeout({"agent": agent}) is True


@pytest.mark.parametrize("rev", [17, 18, 99])
def test_migrating_predicate_stops_at_r17(rev):
    agent = {"defaults_rev": rev, "turn_timeout_sec": 900}
    assert _migrating_turn_timeout({"agent": agent}) is False


def test_migrating_predicate_survives_junk():
    """`defaults_rev` 被人写成鬼东西 ⇒ 当没有（不许抛）。"""
    for junk in ("abc", "", [], {}, None):
        assert _migrating_turn_timeout(
            {"agent": {"defaults_rev": junk, "turn_timeout_sec": 900}}) is True
    assert _migrating_turn_timeout({}) is False
    assert _migrating_turn_timeout({"agent": "not a dict"}) is False


def test_timeout_still_in_carry_list():
    """它必须留在 CARRY 里 —— 否则用户自己设的值会被整个丢掉（比不升级更糟）。"""
    assert "turn_timeout_sec" in CARRY["agent"]


def test_mode_migration_still_works(template):
    """第 16 轮那条迁移不能被这次的改动弄坏。"""
    old = '[agent]\nmode = "readonly"\nturn_timeout_sec = 900\n'
    merged, _ = carry_over(old, template)
    assert 'mode = "trusted"' in merged, "r16 的档位迁移坏了"
