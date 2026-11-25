"""第 14 轮：同事截图「权限」卡上只有「只读」一个按钮 —— 安装器写的 mode=readonly 被当成了本机上限。

拆成两个键：`mode` = 起步档，`max_mode` = 手机上最多能切到哪一档。老配置没有 max_mode ⇒ 行为不变。
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bridge.app import Settings
from bridge.carryover import CARRY

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _cfg(tmp_path, agent_lines: str) -> str:
    p = tmp_path / "config.toml"
    p.write_text(
        '[feishu]\napp_id = "cli_x"\napp_secret = "s"\nowner_open_ids = []\n'
        f'[agent]\nworkspaces = ["{tmp_path}"]\ndefault_workspace = "{tmp_path}"\n{agent_lines}\n'
        'model = "claude-sonnet-5-5"\n', encoding="utf-8")
    return str(p)


def _load(path, monkeypatch, tmp_path):
    # 不让本机真的 IT 策略文件影响结果
    monkeypatch.setattr("bridge.policy.policy_files", lambda: (str(tmp_path / "no-policy.toml"),))
    return Settings.load(path).policy


def test_installer_shape_starts_readonly_but_can_reach_trusted(tmp_path, monkeypatch):
    pol = _load(_cfg(tmp_path, 'mode = "readonly"\nmax_mode = "trusted"'), monkeypatch, tmp_path)
    assert pol.mode == "readonly"
    assert pol.ceiling_mode == "trusted"
    assert pol.can_switch_mode()
    assert pol.with_mode("trusted").mode == "trusted"


def test_old_config_without_max_mode_unchanged(tmp_path, monkeypatch):
    pol = _load(_cfg(tmp_path, 'mode = "readonly"'), monkeypatch, tmp_path)
    assert pol.mode == "readonly" and pol.ceiling_mode == "readonly"


def test_explicit_lock_still_locks(tmp_path, monkeypatch):
    pol = _load(_cfg(tmp_path, 'mode = "readonly"\nmax_mode = "readonly"'), monkeypatch, tmp_path)
    assert not pol.can_switch_mode()


def test_start_above_max_is_clamped(tmp_path, monkeypatch):
    pol = _load(_cfg(tmp_path, 'mode = "trusted"\nmax_mode = "write"'), monkeypatch, tmp_path)
    assert pol.mode == "write" and pol.ceiling_mode == "write"


def test_it_policy_file_still_wins(tmp_path, monkeypatch):
    it = tmp_path / "it.toml"
    it.write_text('mode = "readonly"\n', encoding="utf-8")
    monkeypatch.setattr("bridge.policy.policy_files", lambda: (str(it),))
    pol = Settings.load(_cfg(tmp_path, 'mode = "readonly"\nmax_mode = "trusted"')).policy
    assert pol.ceiling_mode == "readonly"


def test_both_installers_write_max_mode_trusted():
    for name in ("install-mac.sh", "install-windows.ps1"):
        text = (ROOT / "feishu-agent-oneclick" / "src" / name).read_text(encoding="utf-8")
        assert 'max_mode = "trusted"' in text, name
        assert 'mode = "readonly"' in text, name


def test_reinstall_carries_max_mode():
    assert "max_mode" in CARRY["agent"]
