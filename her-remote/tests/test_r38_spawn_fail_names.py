"""11-17：Windows 上 9 次 FileNotFoundError 只报到「没能启动 %s」——起不来的子进程要带上是哪个程序（源码常量模板）。"""
import logging

from bridge import errreport
from bridge.fleet import model


def test_known_names_map_to_constant_templates():
    assert model._spawn_fail_log("codex") == "起子进程失败 程序=codex"
    assert model._spawn_fail_log("C:/x/codex.CMD".rsplit("/", 1)[-1]) == "起子进程失败 程序=codex"
    assert model._spawn_fail_log("ps") == "起子进程失败 程序=ps"
    assert model._spawn_fail_log("my-secret-tool") == model._SPAWN_FAIL_OTHER


def test_spawn_failure_logs_program_name_that_errreport_keeps(caplog):
    caplog.set_level(logging.ERROR, logger="feishu-bridge.fleet")
    res = model.SubprocessRunner().run(["/definitely/not/here/codex"], timeout=5)
    assert not res.ok and "没能启动 codex" in res.error
    recs = [r for r in caplog.records if r.msg == "起子进程失败 程序=codex"]
    assert recs, [r.msg for r in caplog.records]
    item = errreport.item_from_record(recs[0])
    assert item["msg"] == "起子进程失败 程序=codex"      # 源码常量 ⇒ 原样上报
    assert "/definitely" not in str(item)


def test_unknown_program_path_is_not_sent(caplog):
    caplog.set_level(logging.ERROR, logger="feishu-bridge.fleet")
    model.SubprocessRunner().run(["/Users/zhangsan/bin/私人工具"], timeout=5)
    recs = [r for r in caplog.records if r.msg == model._SPAWN_FAIL_OTHER]
    assert recs
    item = errreport.item_from_record(recs[0])
    assert "zhangsan" not in str(item) and "私人工具" not in str(item)
