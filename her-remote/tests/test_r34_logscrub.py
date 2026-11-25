"""第 34 轮：本机日志里 ws 地址的 access_key / ticket 不许是原文（11-10 实测 agent.log 每天几十行）。"""

from __future__ import annotations

import io
import logging

import lark_oapi  # noqa: F401  —— 让 SDK 先建好它自己的 `[Lark]` handler，和真进程一样

from bridge import redact

URL = ("connected to wss://msg-frontier.feishu.cn/ws/v2?fpid=493&aid=552564&device_id=77065489"
       "&access_key=8c950aca5425deadbeef&service_id=33554678&ticket=abcd-1234-efgh [conn_id=77]")


def test_scrub_removes_both_values_and_keeps_the_rest():
    out = redact.scrub_log_text(URL)
    assert "8c950aca" not in out and "abcd-1234" not in out
    assert "access_key=***" in out and "ticket=***" in out
    assert "service_id=33554678" in out and "[conn_id=77]" in out


def test_scrub_leaves_normal_lines_alone():
    line = "桥已启动。模式=trusted 工作目录=/x 授权人数=1"
    assert redact.scrub_log_text(line) == line


def _capture(logger_name: str, *args):
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    try:
        redact.install_log_scrubber((logger_name,))
        logger.warning(*args)
    finally:
        logger.removeHandler(handler)
    return buf.getvalue()


def test_filter_on_handler_scrubs_message_and_format_args():
    assert "8c950aca" not in _capture("r34.scrub.a", URL)
    assert "8c950aca" not in _capture("r34.scrub.b", "%s", URL)


def test_sdk_lark_handler_gets_the_filter():
    redact.install_log_scrubber()
    sdk = logging.getLogger("Lark")
    assert sdk.handlers, "lark_oapi 应在 import 时给 Lark logger 建 handler"
    for handler in sdk.handlers:
        assert any(isinstance(f, redact.LogScrubFilter) for f in handler.filters)


def test_install_is_idempotent():
    redact.install_log_scrubber()
    redact.install_log_scrubber()
    for handler in logging.getLogger("Lark").handlers:
        assert sum(isinstance(f, redact.LogScrubFilter) for f in handler.filters) == 1


def test_main_installs_the_scrubber():
    """🔴 测试绿 ≠ 被调用：main() 里必须真的调了它。"""
    import inspect
    from bridge import app

    src = inspect.getsource(app.main)
    assert "install_log_scrubber()" in src
    assert src.index("basicConfig") < src.index("install_log_scrubber()")
