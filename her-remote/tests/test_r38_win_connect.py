"""第 38 轮 · 11-17 14:34/14:37/14:40 同事实机（Win11 26300，PS 5.1）自动更新连回滚 3 次：
「新版 60 秒内没连上飞书…connect failed: （日志里没有 connect failed）」。

已证伪：「新版根本没起来」—— 198 nginx 上同一 install_id 在每次装完 60~90 秒内都有 v=新版 的 latest.json 请求，
          14:33 还有新版发来的运行时报错上报 ⇒ 新 python 起来了。
怀疑（未证实，那台的 agent.log 没拿到）：run-agent.ps1 的 `*>>` 在 PS5.1 默认写 UTF-16LE，安装器按 UTF-8 从文件中间读
          ⇒ `connected to` 永远匹配不上（connect failed 同理 ⇒ 文案里那半句是空的）。

这里测：
  - Get-NewLog 读 UTF-16LE（无 BOM、从中间开始）/ UTF-8 / 两者混写的切片，都认得出 connected to；
  - 回滚文案：没有 connect failed 时不再出现空的「connect failed:」；有飞书原话才带；附形状级定位（不带日志原文）；
  - 自动更新等 180 秒、手动装仍 60 秒。
阳性对照：原来那版 Get-NewLog（按 UTF-8 解、不抹 NUL）读同一份 UTF-16 切片 ⇒ 认不出（证明台架确实造出了那个形状）。
"""
import re

from tests.test_r38_win_selfupdate import (INSTALL, NEW, OLD, _CUT, _app, _build_of, _run_ps, _upd,
                                           need_pwsh)

_READ = _CUT + r'''
$logPath = $env:LOGPATH; $logMark = [long]$env:LOGMARK
. ([scriptblock]::Create((Cut '# 🔴 第 38 轮（11-17 同事实机 Win11 PS5.1 自动更新连回滚 3 次' '# 计划任务 → 建不了/改不了就退到登录自启')))
if ($env:OLDREAD -eq '1') {
  # 改之前的 Get-NewLog（照抄 git HEAD 那版，当阳性对照）
  function Get-NewLog {
    $stream = [IO.File]::Open($logPath, 'Open', 'Read', 'ReadWrite')
    try { $stream.Position = $logMark; return (New-Object IO.StreamReader($stream, [Text.Encoding]::UTF8)).ReadToEnd() } finally { $stream.Dispose() }
  }
}
$slice = Get-NewLog
Write-Output ("CONNECTED=" + [bool]($slice -match '(^|[^s])connected to'))
Write-Output ("FAILED=" + [bool]($slice -match 'connect failed'))
Write-Output ("RUNNER=" + [bool]($slice -match '\[runner\] 进程退出'))
'''

_OLD_ROUND = "2026-11-17 14:20:00 INFO feishu-bridge 桥已启动\n[Lark] [2026-11-17 14:20:01] [INFO] connected to wss://old\n"
_LARK_OK = "[Lark] [2026-11-17 14:33:05,123] [INFO] connected to wss://msg-frontier.feishu.cn/ws/v2?ticket=xxx\n"
_LARK_FAIL = "[Lark] [2026-11-17 14:33:05,123] [ERROR] connect failed, err: 1000040345: app_id is invalid\n"


def _write(tmp_path, chunks):
    """chunks = [(编码, 文本)]；返回 (路径, 「这一轮」起点 = 第一段之后)。"""
    p = tmp_path / "agent.log"
    data = b""
    mark = None
    for i, (enc, text) in enumerate(chunks):
        if enc == "utf-16-bom":
            b = b"\xff\xfe" + text.encode("utf-16-le")
        elif enc == "utf-16":
            b = text.encode("utf-16-le")
        else:
            b = text.encode("utf-8")
        data += b
        if i == 0:
            mark = len(data)
    p.write_bytes(data)
    return p, mark


def _read(tmp_path, chunks, **env):
    p, mark = _write(tmp_path, chunks)
    _, kv = _run_ps(tmp_path, _READ, {"LOGPATH": str(p), "LOGMARK": str(mark), **env})
    return kv


@need_pwsh
def test_utf16_slice_from_the_middle_is_read(tmp_path):
    """PS5.1 `*>>` 的形状：文件开头一个 FF FE，之后整份 UTF-16LE；自动更新从中间读（没有 BOM）。"""
    kv = _read(tmp_path, [("utf-16-bom", _OLD_ROUND), ("utf-16", _LARK_OK)])
    assert kv["CONNECTED"] == "True", kv


@need_pwsh
def test_positive_control_old_reader_misses_utf16(tmp_path):
    """阳性对照：同一份切片给原来那版读 ⇒ 认不出 connected（= 11-17 那 3 次回滚的候选机制）。"""
    kv = _read(tmp_path, [("utf-16-bom", _OLD_ROUND), ("utf-16", _LARK_OK)], OLDREAD="1")
    assert kv["CONNECTED"] == "False", kv
    kv = _read(tmp_path, [("utf-16-bom", _OLD_ROUND), ("utf-16", _LARK_FAIL)], OLDREAD="1")
    assert kv["FAILED"] == "False", "原来那版连 connect failed 也认不出 ⇒ 文案里那半句是空的"


@need_pwsh
def test_mixed_utf8_runner_and_utf16_python(tmp_path):
    """[runner] 行是 Add-Content -Encoding UTF8，python 的输出是 UTF-16 ⇒ 同一个切片两种编码。"""
    kv = _read(tmp_path, [("utf-16-bom", _OLD_ROUND),
                          ("utf-8", "[runner] 进程退出，code=1，5 秒后重启 2026-11-17T14:33:00\n"),
                          ("utf-16", _LARK_FAIL + _LARK_OK)])
    assert kv == {**kv, "CONNECTED": "True", "FAILED": "True", "RUNNER": "True"}, kv


@need_pwsh
def test_plain_utf8_still_works(tmp_path):
    kv = _read(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", _LARK_OK)])
    assert kv["CONNECTED"] == "True", kv
    kv = _read(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "2026-11-17 14:33:00 INFO feishu-bridge 桥已启动\n")])
    assert kv["CONNECTED"] == "False", kv


@need_pwsh
def test_old_round_is_not_counted(tmp_path):
    """偏移照旧生效：上一轮的 connected 不算这一轮的（原来 $logMark 那条防线）。"""
    kv = _read(tmp_path, [("utf-16-bom", _OLD_ROUND), ("utf-16", "2026-11-17 14:33:00 INFO x 桥已启动\n")])
    assert kv["CONNECTED"] == "False", kv


# ─────────────── 回滚文案 + 定位 ───────────────

def _report(kv):
    return kv.get("REPORT", "")


@need_pwsh
def test_rollback_text_has_no_empty_connect_failed(tmp_path):
    app = _app(tmp_path)
    log, mark = _write(tmp_path, [("utf-16-bom", _OLD_ROUND),
                                  ("utf-16", "2026-11-17 14:33:00 INFO feishu-bridge 桥已启动\n")])
    r, kv = _upd(tmp_path, app, CARHER_AUTO_UPDATE="1", CONNECTED="0", SVC="1", PYALIVE="1",
                 LOGPATH=str(log), LOGMARK=str(mark))
    assert r.returncode == 1, (r.stdout, r.stderr)
    rep = _report(kv)
    assert "已经换回原来那版" in rep and _build_of(app / "bridge") == OLD, kv
    assert "connect failed" not in rep and "日志里没有" not in rep, rep
    # 定位：新 python 在不在、写了多少、编码、最后一行类别、出现过什么
    assert "新 python 在跑=1 个" in rep, rep
    assert re.search(r"这一轮日志 \d+ 字节，编码=UTF-16", rep), rep
    assert "最后一行=桥日志" in rep and "出现过=桥已启动" in rep, rep
    # 只有形状：日志原文、路径一个字都不带
    assert "feishu-bridge" not in rep and str(tmp_path) not in rep and "INFO" not in rep, rep


@need_pwsh
def test_rollback_text_carries_feishu_words_when_present(tmp_path):
    app = _app(tmp_path)
    log, mark = _write(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")])
    r, kv = _upd(tmp_path, app, CARHER_AUTO_UPDATE="1", CONNECTED="0", SVC="1",
                 CONNECT_ERR="1000040345: app_id is invalid", LOGPATH=str(log), LOGMARK=str(mark))
    rep = _report(kv)
    assert "飞书的原话：1000040345: app_id is invalid。" in rep, rep
    assert "新 python 在跑=0 个" in rep and "这一轮日志 0 字节，编码=无" in rep and "最后一行=空" in rep, rep


@need_pwsh
def test_rollback_still_happens_when_diag_breaks(tmp_path):
    """量具坏了（比如 Get-CimInstance 抛）也照样换回：判据不靠它。"""
    app = _app(tmp_path)
    r, kv = _upd(tmp_path, app, CARHER_AUTO_UPDATE="1", CONNECTED="0", SVC="1",
                 LOGPATH=str(tmp_path / "nope" / "agent.log"))
    assert r.returncode == 1 and _build_of(app / "bridge") == OLD, (r.stdout, r.stderr)
    assert "（定位：" in _report(kv), kv


def test_auto_update_waits_180_manual_60():
    text = INSTALL.read_text(encoding="utf-8-sig")
    i = text.index("$connectWait = 60")
    seg = text[i:i + 200]
    assert "if ($carherAuto) { $connectWait = 180 }" in seg, seg
    assert "if ($env:CONNECT_WAIT_SEC)" in seg, seg
    # 一连上就走（break），不多等
    j = text.index("for ($i = 0; $i -lt $connectWaitMax; $i++)")
    assert "$connected = $true; break" in text[j:j + 300]


_READ_ZH = _READ + r'''
Write-Output ("STARTED=" + [bool]($slice.Contains('桥已启动')))
'''


@need_pwsh
def test_utf16_chinese_after_odd_length_utf8_line(tmp_path):
    """UTF-8 写的 [runner] 行字节数是奇数 ⇒ 后面的 UTF-16 错位一个字节；中文判据（桥已启动）两种对齐都要试。"""
    runner = "[runner] 进程退出，code=1 x\n"
    assert len(runner.encode("utf-8")) % 2 == 1
    for pre in (runner, runner + "y"):
        p, mark = _write(tmp_path, [("utf-16-bom", _OLD_ROUND), ("utf-8", pre),
                                    ("utf-16", "2026-11-17 14:33:00 INFO feishu-bridge 桥已启动\n")])
        _, kv = _run_ps(tmp_path, _READ_ZH, {"LOGPATH": str(p), "LOGMARK": str(mark)})
        assert kv["STARTED"] == "True" and kv["RUNNER"] == "True", (pre, kv)


# ─────────────── 11-18：到点还在起 ⇒ 接着等 ───────────────
#
# 现场 094732-herremote-b79597（Win 26200，install_id 08d2…）：新版 180 秒到点时
# 「新 python 在跑=2 个；这一轮日志 0 字节，编码=无；最后一行=空；出现过=无」⇒ 被换回。
# 同一台换回旧版后，旧版第一次上报（桥起来 60 秒后才发）比预期晚约 5 分钟
# ⇒ 这台从起进程到真正开始干活要 4 分钟上下【按 198 nginx 的时间差推，那台的 agent.log 没拿到】。
# 所以这里**不是**放宽判据（照旧要日志里有 connected to），只在「python 活着 + 一个字节都没写」
# 这一个形状上多给时间；日志有字了却没连上 / 崩了 / python 没了 ⇒ 照旧到点就判。
#
# 这组是真跑：真的 Start-Sleep、真的数秒，不是读源码找字符串。

_WAIT_HARNESS = _CUT + r'''
$logPath = $env:LOGPATH; $logMark = [long]$env:LOGMARK
$carherAuto = ($env:CARHER_AUTO_UPDATE -eq '1')
. ([scriptblock]::Create((Cut '# 🔴 第 38 轮（11-17 同事实机 Win11 PS5.1 自动更新连回滚 3 次' '# 计划任务 → 建不了/改不了就退到登录自启')))
# 新 python 在不在由台架给：PYALIVE=1 一直在；PYALIVE=until:<n> 第 n 次之后没了（= 它退了）
$script:pyCalls = 0
function Get-CimInstance { [CmdletBinding()] param($ClassName, $Filter)
  $script:pyCalls++
  $spec = "$env:PYALIVE"
  $alive = switch -regex ($spec) {
    '^1$'        { $true }
    '^until:(\d+)$' { $script:pyCalls -le [int]$Matches[1] }
    default      { $false }
  }
  if ($alive) { [pscustomobject]@{ ProcessId = 4242; CommandLine = '"C:\x\python.exe" -m bridge.app'
                                   CreationDate = (Get-Date).AddSeconds(-$env:PYAGE) } }
}
# 第 n 秒往日志里追一段（台架模拟「桥终于写东西了」）
$script:tick = 0
$appendAt = if ($env:APPEND_AT) { [int]$env:APPEND_AT } else { 0 }
$appendText = "$env:APPEND_TEXT"
function Start-Sleep { param([int]$Seconds)
  $script:tick++
  if ($appendAt -gt 0 -and $script:tick -eq $appendAt -and $appendText) {
    [IO.File]::AppendAllText($logPath, $appendText, (New-Object Text.UTF8Encoding $false))
  }
}
$sw = [Diagnostics.Stopwatch]::StartNew()
. ([scriptblock]::Create((Cut '# CONNECT-WAIT-BEGIN' '# CONNECT-WAIT-END')))
Write-Output ("CONNECTED=" + [bool]$connected)
Write-Output ("WAITED=" + $connectWaited)
Write-Output ("TICKS=" + $script:tick)
Write-Output ("MAX=" + $connectWaitMax)
Write-Output ("DIAG=" + (Get-CarherConnectDiag))
'''


def _wait(tmp_path, chunks, **env):
    p, mark = _write(tmp_path, chunks)
    e = {"LOGPATH": str(p), "LOGMARK": str(mark), "PYALIVE": "0", "PYAGE": "0"}
    e.update(env)
    _, kv = _run_ps(tmp_path, _WAIT_HARNESS, e, name="wait.ps1")
    return kv


@need_pwsh
def test_still_starting_waits_past_the_deadline(tmp_path):
    """🔴 核心：到点时 python 活着、日志 0 字节 ⇒ 不判死，接着等；之后真连上了 ⇒ 判绿。

    台架：等到第 6 秒才往日志里写 connected（> CONNECT_WAIT_SEC=3）。
    """
    kv = _wait(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")],
               CARHER_AUTO_UPDATE="1", CONNECT_WAIT_SEC="3", CONNECT_WAIT_MAX_SEC="30",
               PYALIVE="1", PYAGE="200", APPEND_AT="6", APPEND_TEXT=_LARK_OK)
    assert kv["CONNECTED"] == "True", kv
    assert int(kv["WAITED"]) > 3, f"到点就停了 ⇒ 这台机器每一版都会被换回：{kv}"
    assert int(kv["MAX"]) == 30, kv


@need_pwsh
def test_deadline_still_bites_when_the_log_has_content(tmp_path):
    """🔴 反向：日志有字了却没连上 ⇒ 照旧到点就判（这才是「新版真起不来」，不许多等）。"""
    kv = _wait(tmp_path, [("utf-8", _OLD_ROUND),
                          ("utf-8", "2026-11-17 14:33:00 INFO feishu-bridge 桥已启动\n")],
               CARHER_AUTO_UPDATE="1", CONNECT_WAIT_SEC="3", CONNECT_WAIT_MAX_SEC="30",
               PYALIVE="1", PYAGE="200")
    assert kv["CONNECTED"] == "False" and int(kv["WAITED"]) == 3, kv


@need_pwsh
def test_python_gone_does_not_get_extra_time(tmp_path):
    """python 没了 ⇒ 不是「还在起」⇒ 到点就判。"""
    kv = _wait(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")],
               CARHER_AUTO_UPDATE="1", CONNECT_WAIT_SEC="3", CONNECT_WAIT_MAX_SEC="30",
               PYALIVE="0")
    assert kv["CONNECTED"] == "False" and int(kv["WAITED"]) == 3, kv


@need_pwsh
def test_extra_time_stops_when_python_dies_mid_wait(tmp_path):
    """多等的中途 python 退了 ⇒ 立刻停，不等满上限。"""
    kv = _wait(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")],
               CARHER_AUTO_UPDATE="1", CONNECT_WAIT_SEC="2", CONNECT_WAIT_MAX_SEC="60",
               PYALIVE="until:4")
    assert kv["CONNECTED"] == "False", kv
    assert 2 <= int(kv["WAITED"]) < 60, f"要么没停、要么等满了上限：{kv}"


@need_pwsh
def test_manual_install_gets_no_extra_time(tmp_path):
    """手动装不加时：人在电脑前等着，60 秒到了就把「还没连上」摆给他看。"""
    kv = _wait(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")],
               CONNECT_WAIT_SEC="3", PYALIVE="1", PYAGE="200")
    assert int(kv["MAX"]) == 3 and int(kv["WAITED"]) == 3, kv


@need_pwsh
def test_diag_says_how_long_the_new_python_has_been_up(tmp_path):
    """定位信息要带「跑了多久」—— 分得清「起得慢」和「刚被拉起/反复重启」。"""
    kv = _wait(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")],
               CARHER_AUTO_UPDATE="1", CONNECT_WAIT_SEC="2", CONNECT_WAIT_MAX_SEC="2",
               PYALIVE="1", PYAGE="240")
    assert re.search(r"新 python 在跑=1 个（最早那个已跑 2\d\d 秒）", kv["DIAG"]), kv


def test_auto_update_extends_only_while_starting():
    """源码侧：上限只在自动更新时放到 600 秒，判据函数就是「日志 0 字节 + python 活着」。"""
    text = INSTALL.read_text(encoding="utf-8-sig")
    i = text.index("$connectWaitMax = $connectWait")
    seg = text[i:i + 400]
    assert "if ($carherAuto) { $connectWaitMax = [Math]::Max($connectWait, 600) }" in seg, seg
    fn = text[text.index("function Test-CarherStillStarting"):]
    fn = fn[:fn.index("function Get-CarherConnectDiag")]
    assert "(Get-NewLogBytes).Length -gt 0" in fn and "Get-CarherNewPy" in fn, fn
    # 回滚文案里的秒数必须是**真等了多久**，不是那个常量
    assert "$waitedSec 秒内没连上飞书" in text and "if ($connectWaited) { $connectWaited }" in text


@need_pwsh
def test_auto_update_default_max_is_600_not_the_deadline(tmp_path):
    """不给 CONNECT_WAIT_MAX_SEC 时自动更新的上限就是 600 —— 真跑一遍读出来，不是读源码。

    （python 当场就不在 ⇒ 到点即停，这条跑得很快。）
    """
    kv = _wait(tmp_path, [("utf-8", _OLD_ROUND), ("utf-8", "")],
               CARHER_AUTO_UPDATE="1", CONNECT_WAIT_SEC="2", PYALIVE="0")
    assert int(kv["MAX"]) == 600, kv
    assert int(kv["WAITED"]) == 2, kv
