"""刚在电脑上开了窗口 ⇒ 判据是「窗口里的进程起来没有」，不是「开窗口多久了」。

现场（11-20，用户第二次撞上）：新建会话、进群、发消息，连着几句全是
「❌ 没发进去：电脑上刚给它开了窗口，还在启动；为了不分叉先不发，等一分钟再说」。

原来 `control.send` 开头只看 `window_just_opened(sid)` —— 它只比「开窗口到现在
多久了」和 `WINDOW_GRACE_SEC=90`：
- 窗口 2 秒就起好了 ⇒ 还得干等 88 秒（用户看到的就是这个）
- 窗口压根没起来 ⇒ 90 秒后照样放行（该防的分叉其实没防住）

而真判据代码里本来就有：`_claude_relay_ready` → `adapter.can_relay(sid)`，
直接查终端有没有占着那个会话。现在起来了就立刻走 relay（消息进它的输入队列，
不分叉），只有「还没起来」才用那 90 秒兜着。
"""

from __future__ import annotations

import pytest

from bridge.fleet import newsession as ns
from bridge.fleet.control import FleetControl
from bridge.fleet.model import ActionResult

SID = "SID-window-grace"


class _Adapter:
    claude_bin = "claude"

    def __init__(self, can_relay: bool):
        self._can = can_relay
        self.sent_via = ""

    def can_relay(self, sid):
        return (self._can, "" if self._can else "终端占着它")

    def relay(self, sid, text, timeout=None):
        self.sent_via = "relay"
        return ActionResult(True, "relay-ok", sid)

    def send(self, sid, text, **kw):
        self.sent_via = "send"
        return ActionResult(True, "send-ok", sid)


class _Fleet:
    def __init__(self, ad):
        self._ad = ad

    def adapter(self, tool):
        return self._ad


def _control(can_relay: bool):
    c = FleetControl.__new__(FleetControl)
    ad = _Adapter(can_relay)
    c.fleet = _Fleet(ad)
    c.cached = lambda tool, sid: None
    return c, ad


@pytest.fixture(autouse=True)
def _clean():
    ns.clear_window_opened(SID)
    yield
    ns.clear_window_opened(SID)


def test_process_up_sends_immediately():
    """🔴 这条就是用户撞的那个：窗口起来了就该立刻能发，不用等满 90 秒。"""
    ns.note_window_opened(SID)
    c, ad = _control(True)
    res = c.send("claude", SID, "hi", relay=True)
    assert res.ok, res
    assert ad.sent_via == "relay", "起来了就该走 relay（进输入队列、不分叉）"


def test_process_up_clears_the_grace_mark():
    """清掉标记，省得后面每个调用点各判一次。"""
    ns.note_window_opened(SID)
    c, _ = _control(True)
    c.send("claude", SID, "hi", relay=True)
    assert not ns.window_just_opened(SID)


def test_process_not_up_is_still_refused():
    """阴性对照：还没起来照旧拒 —— 这道闸防的分叉是真的，别一起撤了。"""
    ns.note_window_opened(SID)
    c, ad = _control(False)
    res = c.send("claude", SID, "hi", relay=True)
    assert not res.ok, res
    assert ad.sent_via == "", "没起来却发出去了 ⇒ 会和窗口里那个撞上"


def test_refusal_copy_does_not_promise_a_minute():
    """原来写「等一分钟再说」，而实际几秒就好 —— 别让人干等。"""
    ns.note_window_opened(SID)
    c, _ = _control(False)
    res = c.send("claude", SID, "hi", relay=True)
    assert "等一分钟" not in (res.text or ""), res.text
    assert "几秒" in (res.text or ""), res.text


def test_no_window_opened_is_unaffected():
    """没开过窗口的会话不受这道闸影响。"""
    c, ad = _control(True)
    res = c.send("claude", SID, "hi", relay=True)
    assert res.ok and ad.sent_via == "relay"


def test_grace_still_expires_on_its_own():
    """90 秒到了标记自己过期（原有行为，没动）。"""
    ns.note_window_opened(SID)
    assert ns.window_just_opened(SID)
    import time
    assert not ns.window_just_opened(SID, now=time.monotonic() + ns.WINDOW_GRACE_SEC + 1)


def test_non_relay_path_keeps_the_grace():
    """relay=False（比如别的工具）⇒ 没有「起来了」这个判据可用，照旧按时间兜。"""
    ns.note_window_opened(SID)
    c, ad = _control(True)
    res = c.send("claude", SID, "hi", relay=False)
    assert not res.ok, "没有 relay 判据时不许放行（会走 -p --resume ⇒ 分叉）"
    assert ad.sent_via == ""
