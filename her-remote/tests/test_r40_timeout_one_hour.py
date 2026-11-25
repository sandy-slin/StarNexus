"""单轮超时 30 分钟 → 1 小时（第 40 轮，2026-11-22 主人「你再改成 1 个小时」）。

# 这是同一个坑的第三次

- r16：默认 900（15 分钟）
- r17（11-20）：某同事「15 分钟 timeout 这个有点太短了，设成默认 30 分钟吧」⇒ 改 1800。
  我**只改了安装器模板和代码兜底**，漏了 `carryover.CARRY` —— `turn_timeout_sec` 在那张表里
  ⇒ 每次自动更新把旧的 900 原样搬回来 ⇒ 11-21 他说「我早上仍然遇到这个 15 分钟超时」。
- r18（11-22）：改 3600。**判据照用 `defaults_rev`**，这次一次改齐四处。

# 为什么不能只看值

「旧文件里是 1800」有两种可能：r17 写死的默认值（该升），或者他自己手改成 1800（不许动）。
`defaults_rev` 就是为这件事建的：它说「这份文件的默认值是第几轮写的」。
- `rev < 18` 且值 == 1800 ⇒ r17 的默认 ⇒ 不带，用新模板的 3600
- `rev >= 18` ⇒ 这份文件是本轮之后写的，里面的值都是他自己的 ⇒ 照旧带
"""

from __future__ import annotations

import os
import re
import sys
import tomllib

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
# tests/ → feishu-agent-bridge/ → tools/ → 仓库根（三层）。
# 🔴 用 `__file__` 推，不要用 cwd：`build.sh` 跑测试时 cwd 是 `feishu-agent-oneclick/`
# （11-21 踩过：新测试用相对路径 ⇒ 只在发版第 2 步才红）。
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))

from bridge.app import DEFAULT_TURN_TIMEOUT_SEC  # noqa: E402
from bridge.carryover import DEFAULTS_REV, carry_over  # noqa: E402

ONE_HOUR = 3600

_NEW_TEMPLATE = """[feishu]
app_id = ""
[agent]
defaults_rev = 18
turn_timeout_sec = 3600
mode = "trusted"
"""


def _merge(old_text: str) -> int | None:
    merged, _carried = carry_over(old_text, _NEW_TEMPLATE)
    return tomllib.loads(merged)["agent"].get("turn_timeout_sec")


# ---------- 该升的 ----------

def test_r17_default_is_upgraded():
    """🔴 核心：r17 老用户（rev=17、值 1800）升级后拿到 3600。

    漏了这条，就是 11-21「我早上仍然遇到这个 15 分钟超时」的重演。
    """
    assert _merge('[agent]\ndefaults_rev = 17\nturn_timeout_sec = 1800\n') == ONE_HOUR


def test_r16_default_still_upgraded():
    """上一轮那条仍然要管：有人还停在 r16（无 rev、值 900）。"""
    assert _merge('[agent]\nturn_timeout_sec = 900\n') == ONE_HOUR


def test_file_without_the_key_gets_the_new_default():
    assert _merge('[agent]\nmode = "trusted"\n') == ONE_HOUR


# ---------- 不许动的 ----------

def test_hand_set_values_are_never_overwritten():
    """他自己设的值一律照旧带过去 —— 覆盖用户的设置比超时短更糟。"""
    assert _merge('[agent]\ndefaults_rev = 17\nturn_timeout_sec = 600\n') == 600
    assert _merge('[agent]\ndefaults_rev = 18\nturn_timeout_sec = 7200\n') == 7200


def test_this_generation_keeps_its_own_1800():
    """🔴 最刁的那条：`rev=18` 的人**手改回** 1800 ⇒ 必须保住。

    判据只看值的话这条会被当成「上一代默认」升掉，等于覆盖他的选择。
    """
    assert _merge('[agent]\ndefaults_rev = 18\nturn_timeout_sec = 1800\n') == 1800


# ---------- 四处必须一致 ----------

def test_three_places_agree():
    """🔴 代码兜底 / 两个安装器模板 / config.example.toml 四处的值要一样。

    r17 那次就是只改了一部分。任何一处漏了，用户拿到的就不是我们以为的那个数。
    """
    assert DEFAULT_TURN_TIMEOUT_SEC == ONE_HOUR, DEFAULT_TURN_TIMEOUT_SEC
    for rel in ("feishu-agent-oneclick/src/install-mac.sh",
                "feishu-agent-oneclick/src/install-windows.ps1",
                "tools/feishu-agent-bridge/config.example.toml"):
        path = os.path.join(_REPO, rel)
        text = open(path, encoding="utf-8").read()
        found = re.findall(r"^turn_timeout_sec = (\d+)$", text, re.M)
        assert found, f"{rel} 里找不到 turn_timeout_sec"
        assert all(int(v) == ONE_HOUR for v in found), f"{rel} 还是 {found}"


def test_defaults_rev_bumped_in_all_templates():
    """🔴 `DEFAULTS_REV` 和两个安装器模板里的 `defaults_rev` 必须相等。

    不一致 ⇒ 迁移判据失效：模板写 17 而代码判 18，新装的机器会被当成「上一代」。
    """
    assert DEFAULTS_REV == 18, DEFAULTS_REV
    for rel in ("feishu-agent-oneclick/src/install-mac.sh",
                "feishu-agent-oneclick/src/install-windows.ps1"):
        text = open(os.path.join(_REPO, rel), encoding="utf-8").read()
        found = re.findall(r"^defaults_rev = (\d+)$", text, re.M)
        assert found, f"{rel} 里找不到 defaults_rev"
        assert all(int(v) == DEFAULTS_REV for v in found), \
            f"{rel} 是 {found}，而代码里 DEFAULTS_REV={DEFAULTS_REV}"


def test_old_default_table_is_append_only():
    """迁移表每一代一行，加新一代只往里加 —— 别去改那个遍历的函数。"""
    from bridge.carryover import _OLD_DEFAULT_TIMEOUTS

    assert (900, 17) in _OLD_DEFAULT_TIMEOUTS
    assert (1800, 18) in _OLD_DEFAULT_TIMEOUTS
    # 每一代的 landed_in_rev 必须递增，且都 <= 当前 DEFAULTS_REV
    revs = [rev for _v, rev in _OLD_DEFAULT_TIMEOUTS]
    assert revs == sorted(revs), revs
    assert max(revs) <= DEFAULTS_REV, (revs, DEFAULTS_REV)
