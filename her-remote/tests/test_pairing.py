"""一次性配对码 —— 替掉「第一条消息的发送者自动成为主人」。

旧行为的洞：装完到用户发第一条消息之间，**任何能给这个机器人发消息的人**抢先发一条
就成了主人（自建应用对全员可见 ⇒ 同租户任何人）。而手工建应用那条路几乎必然落到
autobind（bootstrap.py 自己写明「拿不到创建者 open_id 是常态」）。

新行为：安装器生成一个 6 位码，只有**正文等于这个码**的消息才能绑定发送者。

这里每一条 DENY 都配一条同形状的阳性对照 —— 否则"被拒"可能只是因为桩不完整
（本仓踩过：鉴权测试的 DENY 用例最会假通过）。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.pairing import (  # noqa: E402
    ALPHABET,
    BOUND_MESSAGE,
    bound_message,
    CODE_LEN,
    GREETING,
    MAX_GREET_TRIES,
    GreetingState,
    Outcome,
    PairingGate,
    generate_code,
    greet_once,
    greeting_buttons,
)

OWNER = "ou_owner"
STRANGER = "ou_stranger"
CODE = "7KMQ3F"

CONFIG_TEMPLATE = """\
[feishu]
app_id = "cli_test"
app_secret = "s3cret"
owner_open_ids = []
pairing_code = "{code}"
pairing_issued_at = {issued_at}
pairing_ttl_min = {ttl_min}
pairing_max_attempts = {max_attempts}

[agent]
workspaces = ["/tmp/bridge-test-ws"]
default_workspace = "/tmp/bridge-test-ws"
mode = "readonly"
model = "claude-haiku-5-5"
turn_timeout_sec = 60
session_idle_reset_min = 30

[limits]
max_reply_chars = 5000
"""


def write_config(
    tmp_path,
    code: str = CODE,
    issued_at: float | None = None,
    ttl_min: int = 30,
    max_attempts: int = 5,
    owners: str = "",
) -> str:
    if issued_at is None:
        issued_at = int(time.time())
    text = CONFIG_TEMPLATE.format(
        code=code,
        issued_at=int(issued_at),
        ttl_min=ttl_min,
        max_attempts=max_attempts,
    )
    if owners:
        text = text.replace("owner_open_ids = []", f'owner_open_ids = ["{owners}"]')
    path = os.path.join(str(tmp_path), "config.toml")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


# ---------- 阳性对照放最前：对的码必须能绑 ----------


def test_correct_code_binds_the_sender(tmp_path):
    """这条过不了，下面所有 DENY 测试都没有意义。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    result = gate.consider(OWNER, CODE, message_id="om_1")
    assert result.bound, result.outcome
    assert result.outcome is Outcome.BOUND
    assert result.owner_open_id == OWNER


def test_binding_is_persisted_to_disk(tmp_path):
    """只改内存不落盘 = 重启后主人丢了，下一个人又能抢。"""
    path = write_config(tmp_path)
    PairingGate.load(path).consider(OWNER, CODE)
    text = read(path)
    assert f'owner_open_ids = ["{OWNER}"]' in text


def test_code_is_removed_from_config_after_binding(tmp_path):
    """码必须一次性：绑完就从配置里消失，否则重启后它还能再用一次。"""
    path = write_config(tmp_path)
    PairingGate.load(path).consider(OWNER, CODE)
    assert CODE not in read(path)
    assert not PairingGate.load(path).is_open


def test_case_and_spaces_are_tolerated(tmp_path):
    """阳性对照：用户手打小写/带空格也要能过，否则下面「错码不绑」可能只是太严。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    assert gate.consider(OWNER, f"  {CODE.lower()} ").bound


# ---------- 错码不绑 ----------


def test_wrong_code_does_not_bind(tmp_path):
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    result = gate.consider(STRANGER, "AAAAAA")
    assert not result.bound
    assert result.outcome is Outcome.WRONG
    assert "owner_open_ids = []" in read(path)
    assert gate.is_open, "错一次不该把窗口关掉"


def test_ordinary_message_is_not_a_code(tmp_path):
    """旧洞就在这：随便一句话过去就成主人。现在必须什么都不发生。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    result = gate.consider(STRANGER, "这个目录里有什么项目？")
    assert not result.bound
    assert result.outcome is Outcome.NOT_A_CODE
    assert "owner_open_ids = []" in read(path)


def test_non_code_message_does_not_burn_attempts(tmp_path):
    """闲聊不该烧次数，否则同事随口几句就把主人的码作废了（DoS）。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    for _ in range(10):
        gate.consider(STRANGER, "在吗")
    assert gate.failed_attempts == 0
    assert gate.consider(OWNER, CODE).bound, "闲聊之后对的码还要能用"


# ---------- 过期不绑 ----------


def test_expired_code_does_not_bind(tmp_path):
    path = write_config(tmp_path, issued_at=time.time() - 31 * 60, ttl_min=30)
    gate = PairingGate.load(path)
    result = gate.consider(OWNER, CODE)
    assert not result.bound
    assert result.outcome is Outcome.EXPIRED
    assert "owner_open_ids = []" in read(path)


def test_code_inside_ttl_still_binds(tmp_path):
    """阳性对照：同样形状、只是还没过期 ⇒ 必须能绑。"""
    path = write_config(tmp_path, issued_at=time.time() - 29 * 60, ttl_min=30)
    assert PairingGate.load(path).consider(OWNER, CODE).bound


def test_missing_issued_at_is_treated_as_expired(tmp_path):
    """fail-closed：配置里没有签发时间 ⇒ 当成过期，不当成永久有效。"""
    path = write_config(tmp_path, issued_at=0)
    result = PairingGate.load(path).consider(OWNER, CODE)
    assert not result.bound
    assert result.outcome is Outcome.EXPIRED


# ---------- 次数用尽后即使给对码也不绑 ----------


def test_exhausted_attempts_block_even_the_right_code(tmp_path):
    """六位码 = 31^6 约 8.9 亿，但没有次数上限就能被慢慢猜。

    关键断言是**最后那一发给的是对的码**：次数用尽后窗口必须整个作废。
    """
    path = write_config(tmp_path, max_attempts=5)
    gate = PairingGate.load(path)
    for i in range(5):
        assert gate.consider(STRANGER, "AAAAA" + ALPHABET[i]).outcome is Outcome.WRONG
    result = gate.consider(OWNER, CODE)
    assert not result.bound, "次数用尽后连对的码都不能绑"
    assert result.outcome is Outcome.EXHAUSTED
    assert "owner_open_ids = []" in read(path)


def test_right_code_before_exhaustion_still_binds(tmp_path):
    """阳性对照：差一次用尽时，对的码必须还能绑。"""
    path = write_config(tmp_path, max_attempts=5)
    gate = PairingGate.load(path)
    for i in range(4):
        gate.consider(STRANGER, "AAAAA" + ALPHABET[i])
    assert gate.consider(OWNER, CODE).bound


def test_attempt_counter_survives_restart(tmp_path):
    """次数只在内存里 = 杀进程就清零 ⇒ 上限形同虚设（而看门狗会自动重启进程）。"""
    path = write_config(tmp_path, max_attempts=5)
    for i in range(5):
        PairingGate.load(path).consider(STRANGER, "AAAAA" + ALPHABET[i])
    fresh = PairingGate.load(path)  # 相当于进程重启
    assert fresh.failed_attempts >= 5
    assert fresh.consider(OWNER, CODE).outcome is Outcome.EXHAUSTED


# ---------- 绑定后窗口关闭 ----------


def test_second_person_with_same_code_is_rejected(tmp_path):
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    assert gate.consider(OWNER, CODE).bound
    result = gate.consider(STRANGER, CODE)
    assert not result.bound
    assert result.outcome is Outcome.NO_WINDOW
    assert STRANGER not in read(path)


def test_second_person_rejected_after_restart(tmp_path):
    """重启后窗口也必须是关的 —— 靠的是码已从配置里删掉。"""
    path = write_config(tmp_path)
    PairingGate.load(path).consider(OWNER, CODE)
    assert PairingGate.load(path).consider(STRANGER, CODE).outcome is Outcome.NO_WINDOW


def test_no_window_when_owner_already_set(tmp_path):
    """配置里已经有主人了，配对窗口一律不开（哪怕码还在）。"""
    path = write_config(tmp_path, owners="ou_existing")
    gate = PairingGate.load(path)
    assert not gate.is_open
    assert gate.consider(STRANGER, CODE).outcome is Outcome.NO_WINDOW
    assert "ou_existing" in read(path)


def test_no_window_when_no_code_configured(tmp_path):
    """fail-closed：没配码 ⇒ 谁都绑不上（而不是退回「第一条消息」）。"""
    path = write_config(tmp_path, code="")
    gate = PairingGate.load(path)
    assert not gate.is_open
    assert gate.consider(OWNER, "随便什么").outcome is Outcome.NO_WINDOW
    assert gate.consider(OWNER, "").outcome is Outcome.NO_WINDOW


# ---------- 落盘失败 ⇒ 绝不算绑上 ----------


def test_persist_failure_does_not_report_bound(tmp_path):
    """🔴「干了活但没记住主人」是最坏的一种：下一个人发消息又会被当成主人。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    os.unlink(path)  # 配置文件没了 ⇒ 写不进去
    result = gate.consider(OWNER, CODE)
    assert not result.bound
    assert result.outcome is Outcome.PERSIST_FAILED


def test_persist_failure_keeps_window_open_for_retry(tmp_path):
    """阳性对照：落盘失败不能把码烧掉 —— 修好文件后同一个码还要能用。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    body = read(path)
    os.unlink(path)
    assert gate.consider(OWNER, CODE).outcome is Outcome.PERSIST_FAILED
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    assert gate.consider(OWNER, CODE).bound


def test_persist_failure_when_allowlist_not_writable_slot(tmp_path):
    """配置里没有空的 owner_open_ids 可写 ⇒ 报失败，不静默什么都没改。

    对应仓库里那条尺子：脚本化 replace 的 old 可能匹配 0 处，必须先 assert。
    """
    path = write_config(tmp_path)
    body = read(path).replace("owner_open_ids = []", "# owner_open_ids 被注释掉了")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    gate = PairingGate.load(path)
    assert gate.is_open, "阳性对照：窗口得是开的，否则下面的 PERSIST_FAILED 另有原因"
    result = gate.consider(OWNER, CODE)
    assert result.outcome is Outcome.PERSIST_FAILED


def test_config_keeps_other_settings_after_rewrite(tmp_path):
    """重写配置不能把别的设置弄丢（app_secret / mode / 工作目录）。"""
    path = write_config(tmp_path)
    PairingGate.load(path).consider(OWNER, CODE)
    text = read(path)
    assert 'app_secret = "s3cret"' in text
    assert 'mode = "readonly"' in text
    assert "/tmp/bridge-test-ws" in text


def test_rewrite_is_atomic_and_mode_600(tmp_path):
    """配置里有 app_secret ⇒ 重写后不能把权限放宽，也不能留下半截临时文件。"""
    path = write_config(tmp_path)
    os.chmod(path, 0o600)
    PairingGate.load(path).consider(OWNER, CODE)
    assert os.stat(path).st_mode & 0o777 == 0o600
    leftovers = [n for n in os.listdir(str(tmp_path)) if n != "config.toml"]
    assert leftovers == [], f"留下了临时文件：{leftovers}"


# ---------- 码不能进日志 ----------


def test_code_never_appears_in_logs(tmp_path, caplog):
    """配对码进日志 = 泄漏。日志里只能记「失败 N 次」。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    with caplog.at_level(logging.DEBUG):
        gate.consider(STRANGER, "AAAAAA")
        gate.consider(STRANGER, "BBBBBB")
        gate.consider(OWNER, CODE)
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert CODE not in blob, f"配对码出现在日志里：\n{blob}"
    assert "AAAAAA" not in blob, f"用户猜的码也不该进日志：\n{blob}"
    assert blob.strip(), "阳性对照：该有日志（失败计数），不能一行都不记"


def test_failed_attempts_are_logged_as_count(tmp_path, caplog):
    """阳性对照：失败要留痕，否则暴力猜测在日志里完全看不见。"""
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    with caplog.at_level(logging.WARNING):
        gate.consider(STRANGER, "AAAAAA")
    blob = "\n".join(record.getMessage() for record in caplog.records)
    assert "配对" in blob and "1" in blob, blob


# ---------- 常量时间比较 ----------


def test_comparison_uses_compare_digest():
    """`==` 会在第一个不同字符处短路 ⇒ 理论上可按字符逐位试探。

    用源码判据而不是计时判据：计时测试在 CI 上必然 flaky。
    """
    import inspect

    import bridge.pairing as pairing_module

    source = inspect.getsource(pairing_module)
    assert "compare_digest" in source, "配对码比较必须用 hmac.compare_digest"


def test_no_plain_equality_on_the_code():
    import inspect
    import re as _re

    import bridge.pairing as pairing_module

    source = inspect.getsource(pairing_module)
    offenders = [
        line.strip()
        for line in source.splitlines()
        if _re.search(r"(candidate|guess)\s*[!=]=\s*(self\._code|expected)", line)
        or _re.search(r"self\._code\s*[!=]=\s*(candidate|guess)", line)
    ]
    assert not offenders, "拿 == 比配对码：\n" + "\n".join(offenders)


# ---------- 码本身的形状 ----------


def test_generated_code_avoids_confusable_chars():
    """0/O/1/l/I 混起来用户照着念会打错，然后白烧一次尝试次数。"""
    for bad in "0O1lI":
        assert bad not in ALPHABET, f"字母表里有易混字符 {bad}"
    for _ in range(50):
        code = generate_code()
        assert len(code) == CODE_LEN
        assert all(ch in ALPHABET for ch in code)


def test_generated_codes_are_not_predictable():
    """阳性对照兼基本熵检查：50 个码里不该大量重复。"""
    codes = {generate_code() for _ in range(50)}
    assert len(codes) >= 45


def test_generate_code_uses_secrets():
    """用 random 模块就是可预测的（装机时间能猜 ⇒ 种子能猜）。"""
    import inspect

    import bridge.pairing as pairing_module

    source = inspect.getsource(pairing_module)
    assert "secrets." in source
    assert "random.choice" not in source and "random.randint" not in source


# ---------- 消息不能当任务执行 ----------


def test_bound_result_carries_reply_and_not_a_task(tmp_path):
    """绑定成功那条消息只是个码，不能被当成任务跑。"""
    path = write_config(tmp_path)
    result = PairingGate.load(path).consider(OWNER, CODE)
    assert result.bound
    # 🔴 不比常量全文。第 17 轮起这条消息**按档位不同**（`pairing.bound_message(mode)`），
    # 而 `BOUND_MESSAGE` 只是全授权那一版；这个 fixture 的配置是只读档 ⇒ 两者本来就不等。
    # 我第一版把它写成 `== BOUND_MESSAGE` 还红了一次 —— 那是**我的断言错**，不是代码错。
    # 这条测试的本意在函数名里：carries_reply_and_not_a_task。
    assert result.reply and "绑好了" in result.reply
    assert result.reply == bound_message("readonly"), "回的档位和这台机器实际的档位对不上"
    assert result.should_run is False


def test_rejected_results_are_silent(tmp_path):
    """未绑定状态下不是码的消息：静默丢弃，不回「你没权限」。

    回了就等于确认这个机器人存在、背后有东西。
    """
    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    for text in ("你好", "AAAAAA", ""):
        result = gate.consider(STRANGER, text)
        assert not result.bound
        assert result.reply == "", f"{text!r} 不该有回复，却回了 {result.reply!r}"
        assert result.should_run is False


def test_expired_and_exhausted_are_also_silent(tmp_path):
    """过期/用尽同样不能回话 —— 回了就把「这里有个机器人」确认给了陌生人。"""
    expired = PairingGate.load(write_config(tmp_path, issued_at=time.time() - 3600))
    assert expired.consider(STRANGER, CODE).reply == ""

    other = tmp_path / "second"
    other.mkdir()
    gate = PairingGate.load(write_config(other, max_attempts=1))
    gate.consider(STRANGER, "AAAAAA")
    assert gate.consider(STRANGER, CODE).reply == ""


# ---------- 幂等 ----------


def test_same_message_id_does_not_burn_two_attempts(tmp_path):
    """飞书是至少一次投递：平台重推同一条错码不能算两次尝试。"""
    path = write_config(tmp_path, max_attempts=2)
    gate = PairingGate.load(path)
    gate.consider(STRANGER, "AAAAAA", message_id="om_dup")
    gate.consider(STRANGER, "AAAAAA", message_id="om_dup")
    assert gate.failed_attempts == 1
    assert gate.consider(OWNER, CODE).bound, "重推不该把次数烧光"


# ---------- 并发 ----------


def test_concurrent_same_code_binds_exactly_one(tmp_path):
    """事件从 lark 的长连接线程进来：两个人同时发对的码，只能绑上一个。

    没有锁的话「查窗口 → 写配置」之间有缝，后写的会盖掉先写的。
    """
    import threading

    path = write_config(tmp_path)
    gate = PairingGate.load(path)
    results: list = []
    start = threading.Barrier(8)

    def attempt(i: int) -> None:
        start.wait()
        results.append(gate.consider(f"ou_racer_{i}", CODE, f"om_{i}"))

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    bound = [r for r in results if r.bound]
    assert len(bound) == 1, f"绑上了 {len(bound)} 个主人"
    assert f'owner_open_ids = ["{bound[0].owner_open_id}"]' in read(path)


# ---------- 重新生成（码过期后的出路）----------


def test_reissue_writes_a_new_code_and_resets_counter(tmp_path):
    """过期/用尽之后得有一条出路，否则用户只能重装。"""
    path = write_config(tmp_path, max_attempts=1)
    gate = PairingGate.load(path)
    gate.consider(STRANGER, "AAAAAA")
    assert gate.consider(OWNER, CODE).outcome is Outcome.EXHAUSTED

    fresh = PairingGate.reissue(path)
    assert len(fresh) == CODE_LEN
    assert fresh != CODE
    reloaded = PairingGate.load(path)
    assert reloaded.is_open
    assert reloaded.failed_attempts == 0
    assert reloaded.consider(OWNER, fresh).bound


def test_reissue_refuses_when_owner_already_bound(tmp_path):
    """已经有主人了就不许再开窗口 —— 否则这是一条提权后门。"""
    path = write_config(tmp_path, owners="ou_existing")
    with pytest.raises(PermissionError):
        PairingGate.reissue(path)
    assert "ou_existing" in read(path)


def test_cli_reissue_prints_a_usable_code(tmp_path, capsys):
    """安装器最后那屏打的就是这条命令，它必须真能跑通。"""
    from bridge.pairing import main

    path = write_config(tmp_path, code="")
    assert main(["pairing", "reissue", path]) == 0
    printed = capsys.readouterr().out
    code = next(
        (w for w in printed.split() if len(w) == CODE_LEN and all(c in ALPHABET for c in w)),
        "",
    )
    assert code, f"CLI 没打出可用的码：\n{printed}"
    assert PairingGate.load(path).consider(OWNER, code).bound


def test_cli_reissue_refuses_when_owner_bound(tmp_path, capsys):
    """阳性对照方向相反：已有主人时 CLI 必须非零退出，不能悄悄开窗口。"""
    from bridge.pairing import main

    path = write_config(tmp_path, owners="ou_existing")
    before = read(path)
    assert main(["pairing", "reissue", path]) == 1
    assert read(path) == before, "被拒的 reissue 不能改配置"
    assert not PairingGate.load(path).is_open


# ---------- 安装器 ----------

_ONECLICK = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "feishu-agent-oneclick"))
MAC_INSTALLER = os.path.join(_ONECLICK, "src", "install-mac.sh")
WIN_INSTALLER = os.path.join(_ONECLICK, "src", "install-windows.ps1")


def test_installers_no_longer_enable_autobind():
    """🔴 回归：autobind 就是那个洞，安装器不能再写它。"""
    offenders = []
    for path in (MAC_INSTALLER, WIN_INSTALLER):
        if not os.path.exists(path):
            continue
        for lineno, line in enumerate(read(path).splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if "autobind_first_sender" in line:
                offenders.append(f"{os.path.basename(path)}:{lineno}")
    assert not offenders, "安装器还在写 autobind_first_sender：\n" + "\n".join(offenders)


def test_installers_write_pairing_fields():
    for path in (MAC_INSTALLER, WIN_INSTALLER):
        if not os.path.exists(path):
            continue
        text = read(path)
        name = os.path.basename(path)
        assert "pairing_code" in text, f"{name} 没写 pairing_code"
        assert "pairing_issued_at" in text, f"{name} 没写签发时间（没有它就无法过期）"


def _code_lines(path: str) -> list[str]:
    """去掉注释行 —— 判据要看真正会执行的代码，不是说明文字。"""
    return [
        line
        for line in read(path).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_mac_installer_generates_the_code_with_python_secrets():
    """码必须用 CSPRNG 生成。`$RANDOM` 只有 15 位熵，且按 pid/时间播种。"""
    lines = _code_lines(MAC_INSTALLER)
    assigns = [line for line in lines if "PAIRING_CODE=" in line]
    assert assigns, "mac 安装器没有生成配对码"
    assert any("secrets" in line for line in assigns), (
        "配对码不是用 secrets 生成的：\n" + "\n".join(assigns)
    )
    assert not any("$RANDOM" in line for line in lines), "用 $RANDOM 生成配对码（熵太低）"


def test_mac_installer_prints_the_code_to_the_user():
    """码不显眼 = 用户不知道要发什么 ⇒ 这个功能等于把人挡在门外。"""
    printed = [
        line
        for line in _code_lines(MAC_INSTALLER)
        if "PAIRING_CODE}" in line
        and any(line.lstrip().startswith(cmd) for cmd in ("echo", "ok", "say", "warn", "printf"))
    ]
    assert printed, "配对码没有被打印给用户看"


def test_mac_installer_syntax_is_valid():
    """bash -n。嵌入式 heredoc 的坑它查不出来，但语法错它能查。"""
    proc = subprocess.run(
        ["/bin/bash", "-n", MAC_INSTALLER], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr


def test_mac_installer_has_no_var_adjacent_to_fullwidth():
    """bash 3.2 会把全角字符首字节吞进变量名 ⇒ 新加的提示行必须写 ${VAR}。

    和 test_oneclick_scripts.py 同一条判据，这里只盯配对码相关的新行。
    """
    import re as _re

    pattern = _re.compile(r"(?<!\\)\$([A-Za-z_][A-Za-z0-9_]*)(?=[^\x00-\x7f])")
    offenders = []
    for lineno, line in enumerate(read(MAC_INSTALLER).splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        for name in pattern.findall(line):
            offenders.append(f"install-mac.sh:{lineno} ${name}")
    assert not offenders, "\n".join(offenders)


# ---------- 开场消息（扫码装完那条路）----------
#
# 2026-11-05 实测的缺口：推荐路径（扫码）直接把扫码人的 open_id 写进 owner_open_ids
# ⇒ 配对码那条路不走 ⇒ BOUND_MESSAGE 永远发不出去，而 run() 里没有任何主动问候。
# 结果是按推荐方式装完的每个人，打开飞书什么都看不到。
#
# 这组测试盯四条边：发得出去、不重复、没主人不发、没连上不发。


@pytest.fixture
def here_dir():
    """落盘夹具放在测试目录旁边，**不放 tmp_path**。

    macOS 的 tmp_path 在 /private/var/folders/ 下，是 pathguard 的系统豁免前缀 ⇒
    路径类用例会假绿。同 tests/test_groupsetup.py 的同名夹具。
    """
    import pathlib
    import shutil
    import uuid

    base = pathlib.Path(_HERE).parent / f".greeting-tmp-{uuid.uuid4().hex[:8]}"
    base.mkdir()
    try:
        yield base
    finally:
        shutil.rmtree(base, ignore_errors=True)


class _Recorder:
    """假的发送口。签名和 Bridge._say_with_buttons 一致：(receiver, text, buttons) -> bool。"""

    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list = []

    def __call__(self, receiver, text, buttons=None) -> bool:
        self.calls.append((receiver, text, buttons))
        return self.ok


def _greet(here_dir, **kwargs):
    """跑一次 greet_once。默认：有主人、连接已通、发得出去、不真睡。"""
    state = kwargs.pop("state", None) or GreetingState(str(here_dir / "greeting.json"))
    send = kwargs.pop("send", None) or _Recorder()
    outcome = greet_once(
        state,
        owner_open_ids=kwargs.pop("owner_open_ids", frozenset({OWNER})),
        connection_alive=kwargs.pop("connection_alive", lambda: True),
        send=send,
        wait_sec=kwargs.pop("wait_sec", 1.0),
        tick_sec=kwargs.pop("tick_sec", 0.01),
        sleep=kwargs.pop("sleep", lambda _s: None),
        **kwargs,
    )
    return outcome, state, send


# 阳性对照放最前：这条过不了，下面所有"不该发"的用例都没有意义。


def test_greeting_is_sent_to_the_owner(here_dir):
    """扫码装完（配置里直接有主人、没有配对码）必须主动发一条开场。"""
    outcome, _, send = _greet(here_dir)
    assert outcome == "sent", outcome
    assert len(send.calls) == 1
    receiver, text, buttons = send.calls[0]
    assert receiver == OWNER, "要发给主人的 open_id（扫码那条路手里只有它）"
    assert text == GREETING
    assert buttons, "开场就该给按钮 —— 手机上打字最费劲"


def test_greeting_tells_the_user_what_to_say(here_dir):
    """开场的作用是"让他第一次就看到能用"：要有例句、要说清现在哪一档。"""
    assert "试试" in GREETING
    # 🔴 第 17 轮换过判据。默认档从只读改成**全授权**（用户原话：「默认一定要是全控制的」），
    # 所以「要改东西会先弹卡片问你、点同意才动手」这句话在默认情况下**不再成立** ——
    # 旧断言要求开场出现「问你」+「同意」，那是在逼文案说一句假话。
    # 现在钉的是：说清**当前是哪一档**、说清**挡什么**、给出**怎么变安全**。
    assert "全授权" in GREETING or "只读" in GREETING, "不先说清哪一档，他会不知道它会不会动手"
    for guard in ("删家目录", "推代码", "私钥"):
        assert guard in GREETING, f"开场没说清永远不做什么：缺「{guard}」"
    assert "只读" in GREETING and "权限" in GREETING, "没告诉他怎么切到更安全的那一档"
    # 2026-11-06 例句换成非程序员也能问的那三条（实测筛过，见 pairing.FIRST_EXAMPLES 的红字）。
    # 不在这里抄一份字面量：只查「FIRST_EXAMPLES 每一条都真的进了开场消息」，
    # 改例句时这条测试不用跟着改，也不会因为抄错而假绿。
    from bridge.pairing import FIRST_EXAMPLES

    assert len(FIRST_EXAMPLES) == 3
    for example in FIRST_EXAMPLES:
        assert example in GREETING, f"开场少了例句：{example}"
    for jargon in ("TODO", "FIXME", "测试怎么跑", "git"):
        assert jargon not in "".join(FIRST_EXAMPLES), f"例句又带程序员黑话了：{jargon}"


def test_greeting_shares_its_examples_with_the_bound_message():
    """🔴 例句只能有一份。两份的话改了一边另一边不变，而测试全绿。

    （同 botmenu.QUESTION_TEXT 当初那个坑。）
    """
    from bridge.pairing import FIRST_EXAMPLES

    for example in FIRST_EXAMPLES:
        assert example in GREETING
        assert example in BOUND_MESSAGE


def test_greeting_has_no_absolute_paths():
    """🔴 绝对路径会泄露同事真名（`/Users/<真名>/…`），而这条消息会被转发。"""
    import re as _re

    assert "/Users/" not in GREETING
    assert not _re.search(r"/home/\w", GREETING)
    assert "config.toml" not in GREETING, "手机用户改不了这个文件，提它就是死路"


def test_greeting_buttons_are_all_harmless():
    """开场按钮点一下都不该让电脑上发生任何事。

    🔴 第 17 轮换过判据。原来是「每个按钮都必须是 `{"act":"do","op":…}` 形状，
    且命令在那 4 个只显示的里」。默认档改成全授权之后，开场卡要给一个**一键收紧**的出口
    （「🔒 切到只读」），它是 `{"act":"menu","mode":"readonly"}` ⇒ 没有 `op` 键 ⇒ 老断言
    直接 `KeyError`。

    判据改成**按权限等级判**，不是按字面量：解析出来的命令要么是纯显示的，
    要么是**收紧**权限的。为什么不能简单放宽成「允许 act=menu」——
    实测 `{"act":"menu","mode2":"trusted"}` 会解析成 `/mode! trusted`，
    那个 `!` 正是「已经过了确认卡」的意思 ⇒ 放宽成按 act 判，将来有人往开场塞 `mode2`
    就没人挡了。所以判据落在「解析后的命令是不是在放开」。

    另：`/mode trusted`（不带 `!`）这条路本身也不会静默提权 —— `app.py` 里
    `loosening = MODE_RANK[target] > MODE_RANK[policy.mode]` 会先弹确认卡。
    这条测试守的是「开场卡上不许出现提权入口」，比那道闸更靠前。
    """
    from bridge.fleet.actions import DO_COMMANDS
    from bridge.menu import resolve_click
    from bridge.policy import MODE_RANK

    buttons = greeting_buttons()
    assert buttons, "开场要给按钮"
    display_only = {"/menu", "/help", "/dirs", "/status"}
    for button in buttons:
        value = button["behaviors"][0]["value"]
        op = value.get("op")
        if op is not None:
            assert DO_COMMANDS[op] in display_only, (
                f"开场按钮只许放纯显示的命令：{op} → {DO_COMMANDS.get(op)}"
            )
            continue
        command = resolve_click(value)
        assert command, f"开场按钮解析不出命令：{value}"
        assert "!" not in command, (
            f"开场按钮不许走跳过确认那条路（mode2）：{value} → {command}"
        )
        parts = command.split()
        assert parts[0] == "/mode" and len(parts) == 2, (
            f"开场按钮只许是纯显示或切档位：{value} → {command}"
        )
        target = parts[1]
        assert target in MODE_RANK, f"认不出的档位：{command}"
        # 只许**收紧**到最紧那一档；任何往上放的都不许出现在开场卡上
        assert MODE_RANK[target] == min(MODE_RANK.values()), (
            f"开场按钮在放开权限，这是一键提权：{command}"
        )


# ---------- 防重复（看门狗已重启 48 次）----------


def test_greeting_is_sent_only_once_across_restarts(here_dir):
    """🔴 看门狗在长连接静默时自杀等拉起（到 11-05 已 48 次）。每次重启都发 = 骚扰。"""
    path = str(here_dir / "greeting.json")
    first, _, send1 = _greet(here_dir, state=GreetingState(path))
    assert first == "sent"
    for _ in range(5):  # 相当于又被重启 5 次
        outcome, _, send = _greet(here_dir, state=GreetingState(path))
        assert outcome == "already_greeted", outcome
        assert send.calls == [], "重启后又发了一遍开场"
    assert len(send1.calls) == 1


def test_marker_is_not_written_when_sending_failed(here_dir):
    """🔴 反过来写就是「标记已写但消息没发出去 = 永远不发了」。

    这是 brief 里点名的那条边：落盘标记必须**只在真送达之后**写。
    """
    path = str(here_dir / "greeting.json")
    outcome, state, send = _greet(here_dir, state=GreetingState(path), send=_Recorder(ok=False))
    assert outcome == "send_failed", outcome
    assert send.calls, "阳性对照：这一次得真的试过发"
    assert not state.greeted, "没送达却写了标记 ⇒ 之后永远不会再发"
    # 下一次启动（这次发得出去）必须真的发
    again, _, send2 = _greet(here_dir, state=GreetingState(path))
    assert again == "sent", again
    assert len(send2.calls) == 1


def test_repeated_send_failures_stop_after_the_cap(here_dir):
    """发不出去也不能无限试：标记写不下去的机器会跟着看门狗重启几十次。"""
    path = str(here_dir / "greeting.json")
    for _ in range(MAX_GREET_TRIES):
        outcome, _, _ = _greet(here_dir, state=GreetingState(path), send=_Recorder(ok=False))
        assert outcome == "send_failed", outcome
    outcome, _, send = _greet(here_dir, state=GreetingState(path))
    assert outcome == "tries_exhausted", outcome
    assert send.calls == [], f"超过 {MAX_GREET_TRIES} 次还在发"


def test_attempt_is_counted_before_sending(here_dir):
    """🔴 次数要在**发之前**记。

    放到发送之后的话，「发出去了但进程紧接着被看门狗打死」这一种
    次数永远不增长 ⇒ 重启几十次就发几十条。
    """
    path = str(here_dir / "greeting.json")
    state = GreetingState(path)
    seen: list[int] = []

    def send(receiver, text, buttons=None):
        seen.append(GreetingState(path).tries)  # 重新读盘，模拟另一个进程看到的值
        return True

    greet_once(
        state,
        owner_open_ids=frozenset({OWNER}),
        connection_alive=lambda: True,
        send=send,
        wait_sec=1.0,
        tick_sec=0.01,
        sleep=lambda _s: None,
    )
    assert seen == [1], f"发的时候盘上的次数应该已经是 1，实际 {seen}"


# ---------- 没有主人 / 没连上，都不能发 ----------


def test_no_greeting_without_an_owner(here_dir):
    """配对还没完成 ⇒ 发给谁？什么都不做。"""
    outcome, state, send = _greet(here_dir, owner_open_ids=frozenset())
    assert outcome == "no_owner", outcome
    assert send.calls == []
    assert not state.greeted, "没发就不能写标记，否则绑定之后也永远不发了"


def test_no_greeting_before_the_connection_is_up(here_dir):
    """启动瞬间 ws 还没连上，这时候发必然失败。"""
    outcome, state, send = _greet(
        here_dir, connection_alive=lambda: False, wait_sec=0.05, tick_sec=0.01
    )
    assert outcome == "no_connection", outcome
    assert send.calls == []
    assert not state.greeted


def test_waiting_for_the_connection_does_not_burn_a_try(here_dir):
    """🔴 连不上不该消耗重试额度。

    凭据被拒那种死法重启治不好（`_ws_thread` 的红字），3 次重启就会把额度烧光 ⇒
    之后真修好了也永远不发。
    """
    path = str(here_dir / "greeting.json")
    for _ in range(MAX_GREET_TRIES + 2):
        outcome, _, _ = _greet(
            here_dir,
            state=GreetingState(path),
            connection_alive=lambda: False,
            wait_sec=0.05,
            tick_sec=0.01,
        )
        assert outcome == "no_connection", outcome
    assert GreetingState(path).tries == 0
    # 连上之后照样要发
    outcome, _, send = _greet(here_dir, state=GreetingState(path))
    assert outcome == "sent", outcome
    assert len(send.calls) == 1


def test_greeting_waits_for_the_connection_instead_of_giving_up(here_dir):
    """连接晚几秒才通（常态）⇒ 要等，不能"失败就算了"。"""
    ticks = {"n": 0}

    def alive():
        ticks["n"] += 1
        return ticks["n"] >= 4

    outcome, _, send = _greet(
        here_dir, connection_alive=alive, wait_sec=5.0, tick_sec=0.01
    )
    assert outcome == "sent", outcome
    assert ticks["n"] >= 4, "没有真的轮询等待"
    assert len(send.calls) == 1


def test_owner_list_is_read_live_not_captured(here_dir):
    """主人名单要能传**可调用的**。

    `Settings.owner_open_ids` 在配对成功时被整个换掉（`app.on_message` 里那句赋值），
    捕获住 frozenset 本身的话，等连接那几十秒里发生的变化就看不见了。
    """
    owners = {"v": frozenset({OWNER})}
    outcome, _, send = _greet(here_dir, owner_open_ids=lambda: owners["v"])
    assert outcome == "sent", outcome
    assert send.calls[0][0] == OWNER


def test_pairing_path_does_not_greet_at_startup(here_dir):
    """配对码那条路启动时还没主人 ⇒ 这条线什么都不做，交给 BOUND_MESSAGE。

    两条路各发一次 = 新用户收到两段几乎一样的介绍。
    """
    outcome, state, send = _greet(here_dir, owner_open_ids=lambda: frozenset())
    assert outcome == "no_owner", outcome
    assert send.calls == []
    assert not state.greeted


def test_greeted_during_the_wait_is_rechecked(here_dir):
    """🔴 等连接的那几十秒里 BOUND 那条路可能已经发过介绍 ⇒ 连上后要重新判一次。

    只在进等待之前判的话，这里会补发第二条。
    """
    path = str(here_dir / "greeting.json")
    ticks = {"n": 0}

    def alive():
        ticks["n"] += 1
        if ticks["n"] >= 2:
            GreetingState(path).mark_sent()  # 相当于用户此刻发了配对码
        return ticks["n"] >= 3

    outcome, _, send = _greet(
        here_dir, state=GreetingState(path), connection_alive=alive,
        wait_sec=5.0, tick_sec=0.01,
    )
    assert outcome == "already_greeted", outcome
    assert send.calls == []


def test_bound_message_path_suppresses_the_greeting(here_dir):
    """配对码那条路已经发过 BOUND_MESSAGE ⇒ 下次启动不许再发一条开场。

    两条路的开场是**同一件事**，各发一次等于新用户收到两段几乎一样的介绍。
    """
    path = str(here_dir / "greeting.json")
    GreetingState(path).mark_sent()  # app 在 BOUND 分支里会这么做
    outcome, _, send = _greet(here_dir, state=GreetingState(path))
    assert outcome == "already_greeted", outcome
    assert send.calls == []


# ---------- 标记文件本身 ----------


def test_marker_lives_in_its_own_file(here_dir):
    """🔴 落盘位置不许和现有那几个 json 打架。"""
    from bridge.pairing import GREETING_STATE_FILENAME

    assert GREETING_STATE_FILENAME not in {
        "prefs.json", "chat_binds.json", "relay_consent.json",
        "paused.json", "workspaces.json",
    }
    outcome, _, _ = _greet(here_dir, state=GreetingState(str(here_dir / GREETING_STATE_FILENAME)))
    assert outcome == "sent"
    assert (here_dir / GREETING_STATE_FILENAME).exists()
    assert sorted(p.name for p in here_dir.iterdir()) == [GREETING_STATE_FILENAME], (
        "开场标记动了别的文件，或留下了临时文件"
    )


def test_unreadable_marker_is_treated_as_already_greeted(here_dir):
    """读得到文件但读不懂（半截 JSON）⇒ 按发过处理。

    最坏少发一条介绍；反过来（当成没发过）是每次重启骚扰一次。
    """
    path = here_dir / "greeting.json"
    path.write_text("{这不是 json", encoding="utf-8")
    outcome, _, send = _greet(here_dir, state=GreetingState(str(path)))
    assert outcome == "already_greeted", outcome
    assert send.calls == []


def test_missing_marker_file_means_not_greeted_yet(here_dir):
    """阳性对照方向相反：文件不存在是唯一能确定"没发过"的情形，必须发。"""
    path = here_dir / "greeting.json"
    assert not path.exists()
    outcome, _, send = _greet(here_dir, state=GreetingState(str(path)))
    assert outcome == "sent", outcome
    assert len(send.calls) == 1


def test_send_exception_does_not_escape(here_dir):
    """发送抛异常（飞书超时）不能把启动线程带走，而且不能写标记。"""
    def boom(receiver, text, buttons=None):
        raise RuntimeError("飞书超时")

    path = str(here_dir / "greeting.json")
    outcome, state, _ = _greet(here_dir, state=GreetingState(path), send=boom)
    assert outcome == "send_failed", outcome
    assert not state.greeted


def test_background_greeting_runs_off_the_startup_path(here_dir):
    """run() 里不能同步等连接（最多 3 分钟）⇒ 必须丢后台线程。"""
    from bridge.pairing import greet_in_background

    send = _Recorder()
    thread = greet_in_background(
        GreetingState(str(here_dir / "greeting.json")),
        owner_open_ids=frozenset({OWNER}),
        connection_alive=lambda: True,
        send=send,
        wait_sec=1.0,
        tick_sec=0.01,
        sleep=lambda _s: None,
    )
    assert thread.daemon, "启动线程不是 daemon ⇒ 退出时会挂住进程"
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(send.calls) == 1


# ---------- 接线（🔴 这一节是「写好了但没人调」的闸门）----------
#
# 2026-11-05 踩过：`GREETING` / `greet_in_background` 全写好了、38 条测试全绿，
# 而 `bridge/app.py` 里**一行都没调** ⇒ 整个功能是死代码，而且测试一条都不红。
# 同一天 `group_client` 写死 None 也是这个形状（体检报「全齐了」，功能其实没通）。
# 所以这几条不测实现，只测**调用点还在不在**。


def test_run_actually_calls_the_greeting():
    """`run()` 必须真的去调开场消息 —— 不然上面 38 条测的都是死代码。"""
    import inspect

    from bridge.app import Bridge

    src = inspect.getsource(Bridge.run)
    assert "_greet_in_background" in src, "run() 里没有调开场消息 ⇒ 扫码装完还是一个字都不发"


def test_the_greeting_hook_passes_the_real_sender_and_owners():
    """接线要接对：发送口和主人名单都得是真的，不能写死或留占位。"""
    import inspect

    from bridge.app import Bridge

    src = inspect.getsource(Bridge._greet_in_background)
    assert "greet_in_background" in src
    assert "self.settings.owner_open_ids" in src, "主人名单不是真的"
    assert "self._say_with_buttons" in src, "发送口不是真的（开场消息要带按钮）"
    assert "self.connection_alive" in src, "没把真实的连接判据传进去 ⇒ 启动瞬间发必然失败"


def test_unknown_command_fallback_is_wired_into_dispatch():
    """打错的斜杠命令要在 `_handle_command` 里收口。

    不接线的后果不是「少一条提示」，是 `/sessoins` 被当成普通消息发给 claude ⇒
    花钱、慢、答非所问（2026-11-05 实测的缺口）。
    """
    import inspect

    from bridge.app import Bridge

    src = inspect.getsource(Bridge._handle_command)
    # 🔴 不能只查「这个名字出现过」—— `from .menu import unknown_command_reply`
    # 那一行就能把断言喂饱，而真正的调用被改成 `guessed = None` 也照样绿
    # （写这条测试时第一版就是这么假绿的，变异验证当场抓出来）。要查**调用**。
    assert "unknown_command_reply(text)" in src, "打错的命令还会漏给 claude"
