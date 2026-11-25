"""第 36 轮：总览卡分组标题前换成三家自己的品牌图标（`bridge/fleet/brand_icons.py`）。

钉住：有 key ⇒ 面板标题 markdown 带 custom_icon + 那个 img_key、不再写 🤖；没 key ⇒ 原样 🤖；
上传失败不抛；缓存按 app_id 分格、0600；发总览被拒 ⇒ 关掉图标重造一张。
"""

from __future__ import annotations

import json
import os
import stat
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import Session, brand_icons  # noqa: E402
from bridge.fleet.cards import build_overview_card  # noqa: E402
from bridge.fleet.scan import Overview  # noqa: E402

KEY = "img_v3_0abc_r36-test-key"


def _sess(tool="claude", sid="11111111-2222-4333-8444-555555555555"):
    return Session(tool=tool, surface="cli", id=sid, title="修登录", cwd="/Users/x/codes/carher-admin",
                   status="idle", owner="none", updated_at=time.time() - 60, can_send=True)


def _view():
    return Overview(sessions=[_sess("claude"), _sess("codex", "22222222-2222-4333-8444-555555555555"),
                              _sess("cursor", "33333333-2222-4333-8444-555555555555")], notes=[])


def _panels(blob):
    card = json.loads(blob)
    return [e for e in card["body"]["elements"] if e.get("tag") == "collapsible_panel"]


@pytest.fixture(autouse=True)
def _unbind():
    brand_icons.bind(None)
    yield
    brand_icons.bind(None)


def _store(tmp_path, app_id="cli_me", keys=None):
    store = brand_icons.IconStore(str(tmp_path / "brand_icons.json"), app_id)
    for tool, key in (keys or {}).items():
        store.put(tool, key)
    return store


def test_pngs_are_real_images_for_all_three():
    for tool in ("claude", "codex", "cursor"):
        data = brand_icons.png(tool)
        assert data[:8] == b"\x89PNG\r\n\x1a\n", tool
        assert 1000 < len(data) < 20000, tool


def test_with_key_title_uses_custom_icon_and_drops_robot(tmp_path):
    brand_icons.bind(_store(tmp_path, keys={"claude": KEY}))
    panels = _panels(build_overview_card(_view(), seen_at=None, settled=None))
    claude = panels[0]["header"]["title"]
    assert claude["icon"] == {"tag": "custom_icon", "img_key": KEY}
    assert claude["content"].startswith("**Claude**")
    assert "🤖" not in claude["content"]
    # 右边的展开箭头还在
    assert panels[0]["header"]["icon"]["token"] == "down-small-ccm_outlined"
    # 没 key 的那两家照旧 🤖、不带 icon
    for p in panels[1:]:
        assert "icon" not in p["header"]["title"]
        assert p["header"]["title"]["content"].startswith("🤖 **")


def test_without_key_falls_back_to_emoji():
    blob = build_overview_card(_view(), seen_at=None, settled=None)
    assert "custom_icon" not in blob
    assert not brand_icons.card_uses_icons(blob)
    assert all(p["header"]["title"]["content"].startswith("🤖 **") for p in _panels(blob))


def test_disable_turns_icons_off(tmp_path):
    brand_icons.bind(_store(tmp_path, keys={"claude": KEY, "codex": KEY, "cursor": KEY}))
    assert brand_icons.card_uses_icons(build_overview_card(_view(), seen_at=None, settled=None))
    brand_icons.disable("测试")
    assert "custom_icon" not in build_overview_card(_view(), seen_at=None, settled=None)


def test_key_for_never_raises_on_broken_store():
    class Broken:
        def keys(self):
            raise RuntimeError("boom")

    brand_icons.bind(Broken())  # type: ignore[arg-type]
    assert brand_icons.key_for("claude") == ""
    assert "🤖" in build_overview_card(_view(), seen_at=None, settled=None)


def test_upload_failure_does_not_raise_and_stops_after_first(tmp_path):
    calls = []

    def boom(tool, data):
        calls.append(tool)
        raise RuntimeError("99991672")

    store = _store(tmp_path)
    assert brand_icons.ensure(store, boom) == {}
    assert calls == ["claude"]  # 第一张就失败 ⇒ 不再白试另外两张
    assert not os.path.exists(store.path)

    assert brand_icons.ensure(store, lambda t, d: None) == {}
    assert brand_icons.ensure(store, lambda t, d: "not-a-key") == {}


def test_upload_success_caches_by_app_id_0600(tmp_path):
    seen = {}

    def ok(tool, data):
        seen[tool] = data[:4]
        return f"img_v3_{tool}_k"

    store = _store(tmp_path, "cli_me")
    got = brand_icons.ensure(store, ok)
    assert got == {t: f"img_v3_{t}_k" for t in ("claude", "codex", "cursor")}
    assert all(v == b"\x89PNG" for v in seen.values())
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600

    # 同一个文件、别的应用 ⇒ 看不到这台的 key，自己传的也不覆盖别人那格
    other = brand_icons.IconStore(store.path, "cli_other")
    assert other.keys() == {}
    other.put("claude", "img_v3_other")
    data = json.load(open(store.path))
    assert data["apps"]["cli_me"]["claude"] == "img_v3_claude_k"
    assert data["apps"]["cli_other"] == {"claude": "img_v3_other"}
    assert brand_icons.IconStore(store.path, "cli_me").keys()["claude"] == "img_v3_claude_k"

    # 已经齐了 ⇒ 不再上传
    assert brand_icons.ensure(brand_icons.IconStore(store.path, "cli_me"),
                              lambda t, d: pytest.fail("不该再传")) == got


def test_corrupt_cache_reads_as_empty(tmp_path):
    path = tmp_path / "brand_icons.json"
    path.write_text("{not json")
    assert brand_icons.IconStore(str(path), "cli_me").keys() == {}
    path.write_text(json.dumps({"apps": {"cli_me": {"claude": "../../etc/passwd", "evil": KEY}}}))
    assert brand_icons.IconStore(str(path), "cli_me").keys() == {}


def test_start_never_raises_and_binds_cached_keys(tmp_path):
    _store(tmp_path, "cli_me", {"claude": KEY, "codex": KEY, "cursor": KEY})
    brand_icons._started = False
    try:
        assert brand_icons.start(object(), "cli_me", str(tmp_path)) is None  # 已齐 ⇒ 不起线程
        assert brand_icons.key_for("cursor") == KEY
    finally:
        brand_icons._started = False


def test_overview_retries_without_icons_when_rejected(tmp_path):
    """带图标的总览卡被飞书拒 ⇒ 关掉图标、重造一张不带图标的再发。"""
    from bridge import app as app_mod

    brand_icons.bind(_store(tmp_path, keys={"claude": KEY, "codex": KEY, "cursor": KEY}))
    sent = []

    class Rep:
        def send_card(self, chat_id, card):
            sent.append(card)
            return not brand_icons.card_uses_icons(card)

    bridge = app_mod.Bridge.__new__(app_mod.Bridge)
    bridge.replier = Rep()
    card = build_overview_card(_view(), seen_at=None, settled=None)
    assert bridge._send_branded_overview("ou_x", card) is False
    assert brand_icons.key_for("claude") == ""
    again = build_overview_card(_view(), seen_at=None, settled=None)
    assert "custom_icon" not in again
    assert bridge._send_branded_overview("ou_x", again) is False  # 没图标 ⇒ 不试发，走老路
    assert len(sent) == 1


def test_upload_retries_until_permission_arrives(tmp_path):
    """权限在启动后才补上：头两轮 99991672（None），第三轮成功 ⇒ 三家都有 key，且之后不再睡。"""
    from bridge.fleet import brand_icons as bi

    store = bi.IconStore(str(tmp_path / "brand_icons.json"), "cli_test")
    rounds = {"n": 0}
    sleeps = []

    def upload(tool, data):
        return None if rounds["n"] < 2 else f"img_v3_{tool}_0000000000000000"

    def sleep(sec):
        sleeps.append(sec)
        rounds["n"] += 1

    have = bi._ensure_with_retry(store, upload, sleep=sleep)
    assert set(have) == {"claude", "codex", "cursor"}
    assert len(sleeps) == 2


def test_register_asks_for_image_upload_scope():
    from bridge import register

    assert "im:resource" in register.WANT_SCOPES
    assert "im:resource:upload" not in register.WANT_SCOPES  # 11-16 实测这个名字被 add_scopes 静默丢弃
