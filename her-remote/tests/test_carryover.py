"""重装/升级时带过用户自己改过的设置（2026-11-04 review：原来全被静默重置）。"""

from __future__ import annotations

import os
import sys
import tomllib

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.carryover import carry_over  # noqa: E402

NEW = '''[feishu]
app_id = "cli_new"
app_secret = "s"
owner_open_ids = ["ou_new"]

[agent]
workspaces = ["/Users/x"]
default_workspace = "/Users/x"
# readonly = 只能看
mode = "readonly"
model = "claude-sonnet-5-5"
turn_timeout_sec = 900
session_idle_reset_min = 30

[review]
# 例：deny_patterns = ["生产|prod"]
deny_patterns = []
'''

OLD = '''[feishu]
app_id = "cli_old"
app_secret = "old"
owner_open_ids = ["ou_old"]

[agent]
workspaces = ["/Users/x/codes", "/Users/x/work"]
default_workspace = "/Users/x/codes"
mode = "write"
model = "claude-opus-5-5"
turn_timeout_sec = 1200

[review]
deny_patterns = ["生产|prod", "客户\\"数据"]
'''


def test_user_settings_survive_reinstall():
    merged, carried = carry_over(OLD, NEW)
    data = tomllib.loads(merged)
    assert data["agent"]["mode"] == "write"
    assert data["agent"]["model"] == "claude-opus-5-5"
    assert data["agent"]["workspaces"] == ["/Users/x/codes", "/Users/x/work"]
    assert data["agent"]["turn_timeout_sec"] == 1200
    # 🔴 安全方向：用户加的禁止项不能丢
    assert data["review"]["deny_patterns"] == ["生产|prod", '客户"数据']
    assert "review.deny_patterns" in carried


def test_feishu_section_is_never_carried():
    """凭据和主人名单由这一轮安装决定（可能换了机器人）。"""
    data = tomllib.loads(carry_over(OLD, NEW)[0])
    assert data["feishu"] == {"app_id": "cli_new", "app_secret": "s", "owner_open_ids": ["ou_new"]}


def test_keys_missing_from_old_keep_new_defaults():
    data = tomllib.loads(carry_over(OLD, NEW)[0])
    assert data["agent"]["session_idle_reset_min"] == 30


def test_comments_are_kept():
    merged, _ = carry_over(OLD, NEW)
    assert "# readonly = 只能看" in merged
    assert '# 例：deny_patterns = ["生产|prod"]' in merged, "注释里的同名键不能被替换"


def test_broken_old_config_is_not_silently_replaced_by_defaults(tmp_path):
    """🔴 第 32 轮：旧配置读不懂 ⇒ 原来原样返回新文件（默认全授权）、一句话不说 ⇒ 他选的只读静默变全授权。
    现在：抛错给 main；main 把新配置压到只读、说清楚、旧文件留着。"""
    import subprocess
    import sys

    import pytest

    from bridge.carryover import OldConfigUnreadable

    with pytest.raises(OldConfigUnreadable):
        carry_over("this is [not toml", NEW)
    old, new = tmp_path / "old.toml", tmp_path / "new.toml"
    old.write_text('[agent]\nmode = "readonly"\nbroken = [\n', encoding="utf-8")
    new.write_text('[agent]\nmode = "trusted"\nmax_mode = "trusted"\n', encoding="utf-8")
    script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bridge", "carryover.py")
    out = subprocess.run([sys.executable, script, str(old), str(new)], capture_output=True, text=True).stdout
    assert out.startswith("WARN=") and "只读" in out
    text = new.read_text(encoding="utf-8")
    assert 'mode = "readonly"' in text and 'max_mode = "readonly"' in text
    assert (tmp_path / "old.toml.unreadable").exists(), "旧文件被删了"


def test_unsafe_value_types_are_skipped():
    old = NEW.replace('mode = "readonly"', 'mode = {inline = "table"}')
    merged, carried = carry_over(old, NEW)
    assert "agent.mode" not in carried
    assert tomllib.loads(merged)["agent"]["mode"] == "readonly"


def test_live_config_round_trips():
    """拿本机真实配置当"旧配置"跑一遍，结果必须是合法 TOML 且凭据不被带过去。"""
    live = os.path.expanduser("~/.carher-feishu-agent/config.toml")
    if not os.path.exists(live):
        import pytest

        pytest.skip("本机没装")
    with open(live, encoding="utf-8") as handle:
        old = handle.read()
    merged, _ = carry_over(old, NEW)
    data = tomllib.loads(merged)
    assert data["feishu"]["app_id"] == "cli_new"


# ---------- [notify] 开关跨重装（2026-11-06，写使用指南时发现）----------

_NEW_WITHOUT_NOTIFY = '[feishu]\napp_id = "cli_x"\n\n[agent]\nmode = "readonly"\n'


def test_turned_off_reminder_stays_off_after_reinstall():
    """🔴 用户关掉主动提醒 ⇒ 重装不能悄悄打开。安装器写的新文件里**没有** [notify] 这一段。"""
    from bridge.carryover import carry_over
    import tomllib

    old = _NEW_WITHOUT_NOTIFY + '\n[notify]\nwaiting = false\n'
    merged, carried = carry_over(old, _NEW_WITHOUT_NOTIFY)
    assert tomllib.loads(merged)["notify"]["waiting"] is False, merged
    assert "notify.waiting" in carried


def test_reminder_never_configured_adds_nothing():
    """阳性对照：从没设过就不追加（默认开由代码决定，不往配置里塞一行）。

    🔴 第 16 轮：这个 fixture 的 mode 是 `readonly` 且没有 `defaults_rev`，正好命中
    新加的「上一代默认值 ⇒ 升到全控制」迁移（`carryover._migrating_from_old_default`），
    于是 `carried` 不再是空的。这条测试盯的是 `[notify]` 不被追加，和 mode 无关，
    所以只把 mode 这一项排掉，别放松 `[notify]` 那半边。
    """
    from bridge.carryover import carry_over

    merged, carried = carry_over(_NEW_WITHOUT_NOTIFY, _NEW_WITHOUT_NOTIFY)
    assert "[notify]" not in merged
    assert [c for c in carried if "mode" not in c] == []


def test_existing_notify_section_is_replaced_not_duplicated():
    from bridge.carryover import carry_over
    import tomllib

    new = _NEW_WITHOUT_NOTIFY + '\n[notify]\nwaiting = true\n'
    merged, _ = carry_over(new.replace("true", "false"), new)
    assert merged.count("[notify]") == 1
    assert tomllib.loads(merged)["notify"]["waiting"] is False
