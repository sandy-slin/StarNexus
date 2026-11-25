# 11-14:安装器失败会自动上报到线上 198(/ai/api/install-report)。测试里大量故意造失败,
# 而且很多用例给子进程**从零拼 env**(带不上 PYTEST_CURRENT_TEST)⇒ 第一次跑就往线上灌了 58 条假报错。
# ⇒ 在这里拦所有子进程:强制 CARHER_NO_REPORT=1。要测上报本身的用例自己把它改回 0。
import os
import subprocess

os.environ["CARHER_NO_REPORT"] = "1"
_orig_init = subprocess.Popen.__init__


def _init(self, *a, **kw):
    env = kw.get("env")
    if env is not None and "CARHER_NO_REPORT" not in env:
        kw["env"] = {**env, "CARHER_NO_REPORT": "1"}
    _orig_init(self, *a, **kw)


subprocess.Popen.__init__ = _init


# 11-16 总览每个面板第一行是「➕ 新开一个 X」，装了但没会话的那家也画一个空面板 ——
# 画哪几家看本机 `newsession.available_tools()`（which）⇒ 测试结果会随这台机器装没装 claude/codex/cursor 变。
# ⇒ 默认当「一家都没装」（总览和改版前一样），要测新开那一行的用例自己 monkeypatch `cards._startable_tools`。
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_host_tools_in_overview(monkeypatch):
    from bridge.fleet import cards

    monkeypatch.setattr(cards, "_startable_tools", lambda: [])
