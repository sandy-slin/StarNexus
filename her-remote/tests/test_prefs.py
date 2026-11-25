"""聊天里改档位/模型。

这组测试的重点**不是**"能不能改"，而是**改不动的那条边**：
config.toml 的 mode 是本机天花板，聊天里只能往紧的方向动。
这条如果破了，整套"闸门在代码里、提权只能改配置文件"的设计就空了。
"""

from __future__ import annotations

import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.policy import (  # noqa: E402
    MODE_RANK,
    Policy,
    PolicyError,
    clamp_mode,
    modes_up_to,
)
from bridge.prefs import PrefsStore  # noqa: E402


def _policy(mode: str, ceiling: str = "") -> Policy:
    return Policy(
        mode=mode,
        workspaces=("/tmp/ws",),
        default_workspace="/tmp/ws",
        model="claude-sonnet-5-5",
        turn_timeout_sec=900,
        ceiling_mode=ceiling or mode,
    )


# ---------- 天花板 ----------


def test_ceiling_defaults_to_config_mode():
    """不传 ceiling_mode 时等于 mode ⇒ 老代码构造的 Policy 切不动档。"""
    p = _policy("write")
    assert p.ceiling_mode == "write"
    assert p.with_mode("trusted").mode == "write"


@pytest.mark.parametrize("ceiling", ["readonly", "write", "trusted"])
def test_can_never_exceed_ceiling(ceiling):
    p = _policy(ceiling)
    for requested in MODE_RANK:
        got = p.with_mode(requested).mode
        assert MODE_RANK[got] <= MODE_RANK[ceiling], f"{requested} 越过了 {ceiling}"


def test_tightening_always_allowed():
    """自己机器上 trusted，想临时收成只读要能点一下就行。"""
    p = _policy("trusted")
    assert p.with_mode("readonly").mode == "readonly"
    assert p.with_mode("write").mode == "write"


def test_tightened_policy_can_come_back_up_to_ceiling():
    """收紧之后还得能放回去 —— 天花板不变。"""
    p = _policy("trusted").with_mode("readonly")
    assert p.ceiling_mode == "trusted"
    assert p.with_mode("trusted").mode == "trusted"


@pytest.mark.parametrize(
    "bad", ["", "ROOT", "trusted ", "readonly;trusted", "bypassPermissions", None]
)
def test_unknown_mode_fails_closed(bad):
    """🔴 读不懂的档位一律回最紧那档。

    这个值的来源是按钮回调和落盘文件，两边都可能是旧的或坏的。
    反过来（读不懂给全授权）就是静默提权。
    """
    assert clamp_mode(bad, "trusted") == "readonly"


def test_constructing_over_ceiling_raises():
    """越界要在**构造时**炸，不能等到某个工具调用被放行之后。"""
    with pytest.raises(PolicyError):
        _policy("trusted", ceiling="readonly")


def test_allowed_tools_follow_the_switched_mode():
    """切档必须真的改变工具白名单 —— 不然只是改了个显示。"""
    trusted = _policy("trusted")
    assert "Bash" in trusted.allowed_tools()
    readonly = trusted.with_mode("readonly")
    assert "Bash" not in readonly.allowed_tools()
    assert "Write" not in readonly.allowed_tools()
    # 读边界在**任何**档位下都还在
    for policy in (trusted, readonly):
        assert any(t.startswith("Read(") for t in policy.allowed_tools())


# ---------- 落盘 ----------


def test_prefs_roundtrip(tmp_path):
    path = str(tmp_path / "prefs.json")
    store = PrefsStore(path, _policy("trusted"))
    assert store.policy().mode == "trusted"
    store.set_mode("readonly")
    assert PrefsStore(path, _policy("trusted")).policy().mode == "readonly"


def test_prefs_only_records_the_override(tmp_path):
    """没改过就不写键 —— 之后 config.toml 改了，没被覆盖的项要跟着变。"""
    path = str(tmp_path / "prefs.json")
    store = PrefsStore(path, _policy("trusted"))
    store.set_model("claude-haiku-5-5")
    data = json.loads(open(path).read())
    assert data == {"model": "claude-haiku-5-5"}, "不该把没改过的档位也写进去"


def test_disk_prefs_reclamped_against_new_ceiling(tmp_path):
    """🔴 磁盘上的档位**必须**重新过天花板，不能信磁盘。

    真实场景：用户自己机器上点过 trusted；后来 IT 把 config.toml 收成
    readonly（或安装器重跑覆盖）。直接采信那个文件 = 一次旧的点击永久提权。
    """
    path = str(tmp_path / "prefs.json")
    with open(path, "w") as handle:
        json.dump({"mode": "trusted"}, handle)
    store = PrefsStore(path, _policy("readonly"))
    assert store.policy().mode == "readonly"


def test_set_mode_reports_when_clamped(tmp_path):
    """被天花板压住要**明说** —— 静默给个更紧的档，用户会以为提权成功了。"""
    store = PrefsStore(str(tmp_path / "p.json"), _policy("write"))
    applied, clamped = store.set_mode("trusted")
    assert (applied, clamped) == ("write", True)
    applied, clamped = store.set_mode("readonly")
    assert (applied, clamped) == ("readonly", False)


@pytest.mark.parametrize(
    "junk", ['{"mode": "trusted"', "", "null", "[]", '{"mode": 7}', "not json"]
)
def test_corrupt_prefs_falls_back_to_config(tmp_path, junk):
    """读不出来就用 config.toml 那份，不能阻塞启动。"""
    path = str(tmp_path / "prefs.json")
    with open(path, "w") as handle:
        handle.write(junk)
    store = PrefsStore(path, _policy("write"))
    assert store.policy().mode == "write"


def test_missing_file_is_fine(tmp_path):
    store = PrefsStore(str(tmp_path / "nope" / "prefs.json"), _policy("readonly"))
    assert store.policy().mode == "readonly"


def test_unwritable_path_reports_failure(tmp_path):
    """落盘失败要让调用方知道 —— 用户得听到"重启后会回到原来那档"。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a dir")
    store = PrefsStore(str(blocker / "prefs.json"), _policy("trusted"))
    store.set_mode("readonly")
    assert store.last_save_ok is False
    # 本次仍然生效（只是存不下来）
    assert store.policy().mode == "readonly"


# ---------- 模型 ----------


def test_model_whitelist_rejects_unknown(tmp_path):
    """拼错的模型名会让之后每一轮都失败，而用户只看到"它不回我"。"""
    store = PrefsStore(str(tmp_path / "p.json"), _policy("trusted"))
    assert store.set_model("claude-opus-5-5") is True
    assert store.set_model("gpt-4o") is False
    assert store.policy().model == "claude-opus-5-5"


def test_model_switch_does_not_touch_mode(tmp_path):
    store = PrefsStore(str(tmp_path / "p.json"), _policy("trusted"))
    store.set_mode("readonly")
    store.set_model("claude-haiku-5-5")
    policy = store.policy()
    assert (policy.mode, policy.model) == ("readonly", "claude-haiku-5-5")


def test_disk_model_outside_whitelist_ignored(tmp_path):
    path = str(tmp_path / "prefs.json")
    with open(path, "w") as handle:
        json.dump({"model": "evil-model"}, handle)
    store = PrefsStore(path, _policy("trusted"))
    assert store.policy().model == "claude-sonnet-5-5"


def test_modes_up_to_is_ordered_tight_to_loose():
    assert modes_up_to("trusted") == ("readonly", "write", "trusted")
    assert modes_up_to("readonly") == ("readonly",)


def test_tapping_the_config_value_does_not_pin_it(tmp_path):
    """🔴 点「✅ 只读」（当前档）不能被记成覆盖 —— 否则之后 config.toml 放开也不生效。"""
    path = str(tmp_path / "prefs.json")
    store = PrefsStore(path, _policy("readonly"))
    store.set_mode("readonly")
    store.set_model("claude-sonnet-5-5")
    # IT 把 config.toml 放开到 write 重启
    raised = PrefsStore(path, _policy("write"))
    assert raised.policy().mode == "write", "被一次无意义的点击钉死在 readonly"
    assert json.loads(open(path).read()) == {}


def test_switching_back_to_config_value_clears_the_override(tmp_path):
    path = str(tmp_path / "prefs.json")
    store = PrefsStore(path, _policy("trusted"))
    store.set_mode("readonly")
    assert json.loads(open(path).read()) == {"mode": "readonly"}
    store.set_mode("trusted")
    assert json.loads(open(path).read()) == {}
