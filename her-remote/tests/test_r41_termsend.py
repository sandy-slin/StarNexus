"""第三条路：把字 / 按键打进那个终端窗口（第 41 轮，2026-11-23）。

前两条路各自做不到的事（实测，见 `bridge/fleet/relay.py` 模块头和 `test_r40_slash_passthrough.py`）：
`-p --resume` 斜杠命令真能跑，但只对**没人占着**的会话能用；relay 对正开着的会话能用，
但上游入队时写死 `skipSlashCommands` ⇒ 斜杠命令跑不了，而且一个信号都不发给目标 ⇒ 打断不了。
第三条路是「往那个窗口里打字」= 等于主人自己敲的。

## 这个文件绝不碰真东西

**一个真进程都不起、一个字都不往任何窗口送。** 进程链全是手搓的假 `ProcTable`
（形状照本机 `LC_ALL=C ps -axww -o pid=,ppid=,stat=,lstart=,etime=,args=` 的原文抄的），
runner 是记账用的假的。主人那台 31 个会话在干活，往里送一个字就是事故。

## 盯住的几条

- `detect_terminal` 只认**父进程链**，认不出就是 `unknown`（**不猜**）
- `can_send` 的 `why` 空串 = 退回别的路 —— 这个约定崩了的话，今天能走 relay 的会话全被挡死
- 分派顺序：这条路在 relay **之前**，而且只接斜杠命令（按源码断言，防漂）
- 桩期（`WIRED=False`）行为和发版前**一个字不差**
"""

from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from bridge.fleet import termsend as ts  # noqa: E402
from bridge.fleet.model import ActionResult, Proc, ProcTable, RunResult  # noqa: E402

# 「这条用例自己管 subprocess」的名单：下面那道 autouse 闸（不许真起子进程）按**函数名**放过它们。
# 🔴 为什么不用 `@pytest.mark.own_subprocess`：这个仓没有 pytest.ini / pyproject，
# 自定义标记只能在 conftest 里登记；写在测试文件里的 `pytest_configure` pytest **不会调**
# （实测：标记照样 UnknownMarkWarning）。为一个标记去动全仓 conftest 不值，按名字放过就够。
_OWN_SUBPROCESS = frozenset({
    "test_process_env_only_keeps_the_three_cmux_keys",
    "test_process_env_never_raises",
    "test_closed_windows_are_dropped_from_the_second_source",
    "test_second_source_never_raises",
})

# ---------- 假进程链 ----------

L = "Tue Nov  4 19:02:35 2026"
SID = "11111111-1111-4111-8111-111111111111"
SID_OTHER = "22222222-2222-4222-8222-222222222222"


def P(pid: int, ppid: int, args: str, stat: str = "S") -> Proc:
    return Proc(pid=pid, ppid=ppid, stat=stat, lstart=L, etime_sec=600, args=args)


# claude 自己那行：照本机 pid 18228 的 ps 原文（带 cmux 塞的 --settings 路径）压短
CLAUDE_ARGS = ("/Users/x/.local/bin/claude --settings "
               "/Users/x/.cmuxterm/claude-settings/8d4b80bf.json --permission-mode bypassPermissions -r")
ZSH = "-/bin/zsh"
LOGIN = "/usr/bin/login -flp x /bin/bash --noprofile --norc -c exec -l /bin/zsh"

# 每条链的最后一跳 = 那个终端程序。`args` 都是本机 ps 真抄来的（标 [链]/[名] 的见 _TERM_MARKS）
CHAINS = {
    # [链] 本机 30/30 个在跑的会话都是这条（2026-11-23）
    ts.TERM_CMUX: "/Applications/cmux.app/Contents/MacOS/cmux",
    # [名] 本机 pid 80303 的 ps 原文
    # [名] 本机 ps 原文的完整形状（带参数）。🔴 **刻意不用 `tmux: server`**：
    # 那是 Linux 下 tmux 改写 argv 的写法，macOS 上 0 命中（本机 tmux 3.6a 实测，角色 1/2 复量过）
    # ⇒ 判据只剩 argv[0] 的名字。样本带上参数，是为了证明认的是 argv[0] 而不是整条串。
    ts.TERM_TMUX: "/opt/homebrew/bin/tmux -S /tmp/tmux-502/default new-session -d",
    # [名] 本机 pid 18387 的 ps 原文
    ts.TERM_TERMINAL_APP: "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal",
    ts.TERM_ITERM: "/Applications/iTerm.app/Contents/MacOS/iTerm2",
    ts.TERM_VSCODE: "/Applications/Visual Studio Code.app/Contents/MacOS/Electron",
    ts.TERM_CURSOR: "/Applications/Cursor.app/Contents/MacOS/Cursor",
}


def chain(host_args: str, *, claude_pid: int = 101, stopped: bool = False) -> ProcTable:
    """claude ← zsh ← login ← <host>，就是本机那条真链的形状。"""
    return ProcTable([
        P(claude_pid, 102, CLAUDE_ARGS, "T" if stopped else "S+"),
        P(102, 103, ZSH),
        P(103, 104, LOGIN),
        P(104, 1, host_args),
    ])


# ---------- detect_terminal：只认父进程链 ----------


@pytest.mark.parametrize("kind,host", sorted(CHAINS.items()))
def test_each_known_chain_is_recognised(kind, host):
    """阳性对照：每一种认得的终端，真链上都要判对。"""
    assert ts.terminal_of_pid(chain(host), 101) == kind


def test_cmux_chain_matches_the_real_machine():
    """🔴 主人那台的真链（2026-11-23 量的 30 个会话，一个不差）：claude ← zsh ← login ← cmux。"""
    assert ts.terminal_of_pid(chain(CHAINS[ts.TERM_CMUX]), 101) == ts.TERM_CMUX


def test_unknown_host_is_not_guessed():
    """🔴 认不出就是 unknown，**不许**兜底成「多半是 cmux」—— 每一条出路都要是 unknown。

    往窗口里打字认错终端 = 把主人的字打到别的地方；unknown 的代价只是退回今天的两条路。
    `terminal_of_pid` 有四个出口（链上没命中、父进程断了、成环、跳数用完），
    拿「兜底成 cmux」做反向对照时第一版只盯住了前一个 ⇒ 另外三个漏网，所以这里逐个走一遍。
    """
    cases = {
        "链上没命中": chain("/usr/local/bin/some-terminal-nobody-knows"),
        "父进程断了": ProcTable([P(101, 777, CLAUDE_ARGS, "S+")]),
        "直接挂在 init 下": ProcTable([P(101, 1, CLAUDE_ARGS, "S+")]),
        "父进程成环": ProcTable([P(101, 102, CLAUDE_ARGS, "S+"), P(102, 101, ZSH)]),
    }
    # 跳数用完：中间全是 shell，宿主在 `_HOPS` 之外
    long_chain = [P(101, 102, CLAUDE_ARGS, "S+")]
    pid = 102
    for _ in range(ts._HOPS + 3):
        long_chain.append(P(pid, pid + 1, ZSH))
        pid += 1
    long_chain.append(P(pid, 1, CHAINS[ts.TERM_CMUX]))
    cases["跳数用完"] = ProcTable(long_chain)

    for label, table in cases.items():
        assert ts.terminal_of_pid(table, 101) == ts.TERM_UNKNOWN, f"{label}：被猜成了某种终端"


def test_claude_own_cmdline_is_not_evidence():
    """🔴 判据是**父进程链**，不是 claude 自己那行命令行。

    本机 30 个会话的 claude 命令行里都带 `--settings ~/.cmuxterm/claude-settings/<hash>.json`
    ⇒ 拿它当判据会在第 0 跳就命中，把「它开在哪个窗口里」这个问题整个跳过去。
    手敲 `claude --settings ~/.cmuxterm/...` 的进程压根不在 cmux 窗口里，
    往「它的窗口」打字就打到别处了。第一版就是这么假绿的。
    """
    lone = ProcTable([P(201, 202, CLAUDE_ARGS, "S+"), P(202, 1, ZSH)])
    assert ts.terminal_of_pid(lone, 201) == ts.TERM_UNKNOWN


def test_missing_and_orphan_pids_are_unknown():
    """pid 不在表里 / 父进程断了 / 直接挂在 init 下 ⇒ unknown，不抛。"""
    table = chain(CHAINS[ts.TERM_CMUX])
    assert ts.terminal_of_pid(table, 99999) == ts.TERM_UNKNOWN
    assert ts.terminal_of_pid(table, None) == ts.TERM_UNKNOWN
    assert ts.terminal_of_pid(table, "abc") == ts.TERM_UNKNOWN
    orphan = ProcTable([P(301, 1, CLAUDE_ARGS, "S+")])
    assert ts.terminal_of_pid(orphan, 301) == ts.TERM_UNKNOWN


def test_parent_loop_does_not_hang():
    """父进程成环（pid 复用能造出来）⇒ 退出，不死循环。"""
    looped = ProcTable([P(401, 402, CLAUDE_ARGS, "S+"), P(402, 401, ZSH)])
    assert ts.terminal_of_pid(looped, 401) == ts.TERM_UNKNOWN


def test_deep_chain_within_hops_still_found():
    """中间垫几层 shell（nvm 的壳、嵌套 login）照样要找到；超过 `_HOPS` 就放弃。"""
    deep = [P(501, 502, CLAUDE_ARGS, "S+")]
    pid = 502
    for _ in range(ts._HOPS - 2):
        deep.append(P(pid, pid + 1, ZSH))
        pid += 1
    deep.append(P(pid, 1, CHAINS[ts.TERM_CMUX]))
    assert ts.terminal_of_pid(ProcTable(deep), 501) == ts.TERM_CMUX

    too_deep = [P(601, 602, CLAUDE_ARGS, "S+")]
    pid = 602
    for _ in range(ts._HOPS + 3):
        too_deep.append(P(pid, pid + 1, ZSH))
        pid += 1
    too_deep.append(P(pid, 1, CHAINS[ts.TERM_CMUX]))
    assert ts.terminal_of_pid(ProcTable(too_deep), 601) == ts.TERM_UNKNOWN


def test_empty_table_and_ps_failure_are_unknown():
    """ps 读不到（空表 / 带 error）⇒ unknown。"""
    assert ts.terminal_of_pid(ProcTable(), 101) == ts.TERM_UNKNOWN
    assert ts.terminal_of_pid(ProcTable(error="读不到进程列表"), 101) == ts.TERM_UNKNOWN


# ---------- 假 adapter（只有 TermSend 真用到的那几样） ----------


class FakeRunner:
    """记账用：被叫过几次、拿到什么 argv。**永不起真进程。**"""

    def __init__(self, result: RunResult = None) -> None:
        self.calls: list = []
        self.result = result if result is not None else RunResult(returncode=0, stdout="")

    def run(self, argv, **kw):
        self.calls.append((list(argv), kw))
        return self.result


class FakeSession:
    def __init__(self, status: str = "idle") -> None:
        self.status = status


class FakeAdapter:
    """`_registry` / `_table` / `find` / `runner` —— TermSend 只碰这四样。"""

    def __init__(self, table: ProcTable, *, pid: int = 101, sid: str = SID,
                 source: str = "user", status: str = "idle", found: bool = True) -> None:
        self.table = table
        self.runner = FakeRunner()
        self._status = status
        self._found = found
        self._rows = [(source, {
            "sessionId": sid, "pid": pid,
            # 注册表里的 procStart 和 ps 的 lstart 要对得上，否则 same_process 判「进程没了」
            "procStart": L, "startedAt": L,
            "name": "probe", "messagingSocketPath": "/tmp/cc-socks/%d.sock" % pid,
        })]

    def _registry(self):
        return list(self._rows)

    def _table(self):
        return self.table

    def find(self, sid):
        return FakeSession(self._status) if self._found else None


def make(host: str = CHAINS[ts.TERM_CMUX], **kw) -> ts.TermSend:
    table = kw.pop("table", None)
    if table is None:
        table = chain(host, stopped=kw.pop("stopped", False))
    return ts.TermSend(FakeAdapter(table, **kw))


# ---------- 假的「进程环境」和假的 cmux ----------
#
# 🔴 `_process_env` 真跑 `ps eww` 读**本机真实进程**的环境。测试里一律 monkeypatch 掉：
# 不碰主人那 29 个会话一个字，也不让测试结果随这台机器当时开着什么而变。

SURFACE_A = "9A0EDFB1-47B4-430B-A080-4BACFD71F083"   # 形状照本机 ps 里真有的那个
SURFACE_B = "11112222-3333-4444-5555-666677778888"
TOKEN_A = "cap-aaaaaaaaaaaaaaaa-A1B2"
TOKEN_B = "cap-bbbbbbbbbbbbbbbb-C3D4"
SOCK_A = "/var/folders/g6/T/cmux-502/A/cmux.sock"
FAKE_CMUX = "/Applications/cmux.app/Contents/Resources/bin/cmux"


def envs(**by_pid):
    """{pid: {CMUX_…: 值}} → 一个能顶替 `_process_env` 的函数。"""
    table = {int(k): dict(v) for k, v in by_pid.items()}
    return lambda pid: dict(table.get(int(pid or 0), {}))


def cmux_env(surface: str, token: str = TOKEN_A, sock: str = SOCK_A) -> dict:
    """一个在 cmux 里跑的 claude 进程的那几个变量（29/29 实测都有这三样）。"""
    return {ts._ENV_SURFACE: surface, ts._ENV_CAPABILITY: token, ts._ENV_SOCKET: sock}


@pytest.fixture
def cmux(monkeypatch):
    """接上 cmux 这条路：三个真会碰外面的东西全部换成假的。

    默认只有 pid 101（= `chain()` 里那个 claude）有环境，窗口是 A，两源一致。

    🔴 **`_cmux_surface_of_session` 必须一起换掉**：它真的 `subprocess.run` 一条
    `cmux sessions list`。不换的话测试会去问本机真 cmux —— 它对我们这些假 sessionId
    一律答不出来 ⇒ `want` 恒为空串 ⇒ 两源交叉那道闸**在每一条测试里都被跳过**，
    于是「窗口只能来自目标自己」那组测试看起来绿、实际一行闸门代码都没走到。
    （这正是我差点交出去的那个假绿：角色 1 11-23 加完那道闸之后，我的用例一条都没变红。）
    """
    monkeypatch.setattr(ts, "_cmux_bin", lambda: FAKE_CMUX)
    monkeypatch.setattr(ts, "_process_env", envs(**{"101": cmux_env(SURFACE_A)}))
    monkeypatch.setattr(ts, "_cmux_surfaces_of_session",
                        lambda sid: {SID: {SURFACE_A.upper()},
                                     SID_OTHER: {SURFACE_B.upper()}}.get(sid, set()))
    return monkeypatch


@pytest.fixture(autouse=True)
def _no_cmux_or_ps_for_real(monkeypatch, request):
    """🔴 **这条路的两个出口一个真子进程都不许起**（主人 29 个会话在干活）。

    `termsend` 有两处真会 `subprocess.run`：`_process_env`（`ps eww` 读进程环境）和
    `_cmux_surface_of_session`（`cmux sessions list`）。autouse 把这两个函数本身换掉。

    🔴 **不能去 patch `ts.subprocess.run`**：那是全局共用的 `subprocess` 模块，
    一patch 就把别的库 import 期的 `platform.architecture()`（它内部跑 `file -b`）
    一起拦下 ⇒ 两条只读源码的用例无端变红。闸门要钉在自己这条路的出口上，不是整个世界。

    想自己喂假 `ps` 输出的用例（`_process_env` 那两条）记在 `_OWN_SUBPROCESS` 里跳过本闸。
    """
    if request.node.name in _OWN_SUBPROCESS:
        return

    def refuse_env(pid):
        raise AssertionError(f"测试里真去读了进程 {pid} 的环境")

    def refuse_cmux(sid):
        raise AssertionError(f"测试里真去问了 cmux（sid={str(sid)[:8]}）")

    monkeypatch.setattr(ts, "_process_env", refuse_env)
    monkeypatch.setattr(ts, "_cmux_surfaces_of_session", refuse_cmux)


def sent(sender: ts.TermSend) -> tuple:
    """runner 收到的那一次调用 → (argv, kwargs)。没被叫过就炸（别把「没发」读成「发对了」）。"""
    calls = sender.adapter.runner.calls
    assert len(calls) == 1, f"期望正好跑一次，实际 {len(calls)} 次"
    return calls[0]


# ---------- TermSend.detect_terminal：只认主人自己开的那个进程 ----------


def test_detect_terminal_on_the_owners_session():
    assert make().detect_terminal(SID) == ts.TERM_CMUX


def test_detect_terminal_rejects_bad_id():
    """不是会话编号 ⇒ unknown，不去读进程表。"""
    for bad in ("", "abc", None, "11111111-1111-4111-8111"):
        assert make().detect_terminal(bad) == ts.TERM_UNKNOWN


def test_detect_terminal_ignores_bridge_and_script_processes():
    """🔴 只认注册表里 `source == "user"` 的那条。

    桥自己起的信使、别的脚本的 `claude -p` 也在注册表里 —— 往它们的「窗口」打字没有意义
    （它们没有窗口），认成 cmux 就会把主人的字送进一个不该去的地方。
    """
    assert make(source="bridge").detect_terminal(SID) == ts.TERM_UNKNOWN


def test_detect_terminal_ignores_other_sessions():
    """别的会话的 pid 不算这个会话的（注册表里一行都对不上 ⇒ unknown）。"""
    assert make().detect_terminal(SID_OTHER) == ts.TERM_UNKNOWN


def test_detect_terminal_requires_the_same_process():
    """pid 被别的进程复用了（启动时间对不上）⇒ 不算它在跑。"""
    sender = make()
    sender.adapter._rows[0][1]["procStart"] = "Tue Nov  4 08:00:00 2026"
    sender.adapter._rows[0][1]["startedAt"] = "Tue Nov  4 08:00:00 2026"
    assert sender.detect_terminal(SID) == ts.TERM_UNKNOWN


def test_detect_terminal_unknown_when_ps_fails():
    """读不到进程表 ⇒ unknown（**不猜**）。"""
    assert make(table=ProcTable(error="读不到进程列表")).detect_terminal(SID) == ts.TERM_UNKNOWN


# ---------- can_send：空串 = 退回，有内容 = 真做不到 ----------


@pytest.fixture
def wired(monkeypatch):
    """把桩接上（只在这个测试进程里）：`WIRED=True` + cmux 当成有命令行接口。

    这样能在角色 1 的真命令落地之前，先把**接上之后**的每条分支验一遍。
    `_argv_*` 仍然是桩 ⇒ 真要送东西的那一步照旧 NotImplementedError，一个字都出不去。
    """
    monkeypatch.setattr(ts, "WIRED", True)
    monkeypatch.setattr(ts, "_SENDERS", (ts.TERM_CMUX,))


def test_can_send_fallback_is_an_empty_why(wired):
    """🔴 本轮最要紧的一条约定：**退回别的路一律 `why=""`**。

    和 `claude.can_relay` 同一个形状（见 `control._claude_relay_ready` 的红字）。
    把「这条路用不上」说成一句拒绝的话，后果是今天能走 relay 的会话全被这条新路挡死 ——
    用户只看到「❌ 没发进去：…」，而 relay 本来好着。

    🔴 **「它正忙着」不在这组里**（11-23 角色 1 改的契约，他改对了）：
    空串 ⇒ 退回 relay ⇒ relay 对斜杠命令只会说 `WHY_SLASH_IN_RELAY` 那句
    「…要是把那个窗口关掉，从这儿发就能跑了」—— 这条路接上之后那句话**对 cmux 会话是错的**：
    真原因是「它这会儿正忙」，真下一步是「等它闲下来」，照那句话去关掉自己正在干活的窗口
    纯白折腾。见 `test_a_busy_session_is_told_why_not_told_to_close_the_window`。
    """
    cases = {
        "认不出终端": make("/usr/local/bin/nobody-knows"),
        "会话没在跑": make(table=ProcTable()),
        "读不到进程表": make(table=ProcTable(error="读不到进程列表")),
        "桥自己的进程": make(source="bridge"),
    }
    for label, sender in cases.items():
        can, why = sender.can_send(SID)
        assert can is False, label
        assert why == "", f"{label}：why 该是空串（退回别的路），实际 {why!r}"
    # 阴性对照：这组里**不许**混进「正忙」那几例（它们现在该给话，不是退回）。
    # 「状态读不出来」（find 回 None）也归忙 —— `_busy` 读不出来时保守算忙。
    for label, sender in (("正忙", make(status="running")), ("状态读不出来", make(found=False))):
        assert sender.can_send(SID)[1] == ts.WHY_BUSY, \
            f"{label} 又退回空串了 ⇒ 用户会被 relay 那句话叫去关窗口"


def test_can_send_ok_on_an_idle_cmux_session(wired):
    """阳性对照：接上之后，闲着的 cmux 会话这条路是通的 —— 否则上面那串阴性全是恒绿的。"""
    can, why = make().can_send(SID)
    assert (can, why) == (True, "")


def test_can_send_rejects_bad_id_and_says_so(wired):
    """编号不对是**真做不到**，不是退回 ⇒ 要有话。"""
    can, why = make().can_send("not-a-session")
    assert can is False and why == ts.WHY_BAD_ID


def test_can_send_says_so_when_that_terminal_has_no_api(wired, monkeypatch):
    """认出来了、但那种终端插不上手 ⇒ 照实说（带那个终端的名字），不是退回。

    这是「认出来了但办不到」和「认不出」的分界：前者能把话说准，后者只能退回。
    """
    monkeypatch.setattr(ts, "_SENDERS", ())  # 一家都没接
    can, why = make().can_send(SID)
    assert can is False
    assert why == ts.WHY_TERM_NO_API.format(label="cmux")
    assert "cmux" in why


def test_can_send_rejects_a_suspended_session(wired):
    """被 Ctrl-Z 挂起的会话：打字进去不会有人理 ⇒ 照实说 + 给出路（敲 fg）。"""
    can, why = make(stopped=True).can_send(SID)
    assert can is False and why == ts.WHY_SUSPENDED
    assert "fg" in why


def test_a_busy_session_is_told_why_not_told_to_close_the_window(wired):
    """🔴 「它正忙」这道闸只管打字、**不管按键**，而且要**自己把话说对**。

    为什么不能退回空串（11-23 角色 1 改的契约，他走真代码实测过这个现场）：
    空串 ⇒ 退回 relay ⇒ relay 对斜杠命令只会说 `WHY_SLASH_IN_RELAY` 那句
    「斜杠命令送不进一个开着的会话…**要是把那个窗口关掉，从这儿发就能跑了**」。
    这条路接上之后那句话对 cmux 会话**是错的**：
      - 真原因不是「送不进开着的会话」（现在送得进，`/goal` 实测真跑），是「它这会儿正忙」
      - 真下一步是「等它闲下来再发一次」，**不是去关窗口** —— 照那句话关掉自己正在
        干活的窗口是纯白折腾，而且那一轮的活就断了
    Esc 不受这条限制：要打断的本来就是正在跑的那一轮，拿「它正忙着」挡住它这条路就白加了。
    """
    sender = make(status="running")
    can, why = sender.can_send(SID)
    assert (can, why) == (False, ts.WHY_BUSY)
    assert "等它" in why and "停下" in why, f"没给下一步：{why}"
    assert "关掉" not in why and "关窗" not in why, \
        f"🔴 又叫人去关窗口了 —— 这条路送得进开着的会话，关窗口是白折腾：{why}"
    assert sender.can_send(SID, for_key=True) == (True, "")


def test_plain_text_never_wants_the_terminal():
    """🔴 普通话一律不走这条路（relay 更稳），只有斜杠命令要它。"""
    for plain in ("你好", "把测试跑绿", "/Users/me/codes 里有什么", "/tmp/x", "", "   ",
                  "看看 /goal 是什么"):
        assert ts.wants_terminal(plain) is False, plain
    for cmd in ("/goal 看看天气", "/context", "/compact", "/权限"):
        assert ts.wants_terminal(cmd) is True, cmd


def test_wants_terminal_shares_the_relay_slash_shape():
    """判据和 relay / menu 那两处必须是**同一份实现**（三处一起改，不许各写一个正则）。"""
    from bridge.fleet.relay import _looks_like_slash_command
    from bridge.menu import _COMMAND_SHAPE

    assert ts.wants_terminal is not None
    assert ts._looks_like_slash_command is _looks_like_slash_command, "termsend 自己另写了一份形状"
    from bridge.fleet.relay import _SLASH_SHAPE
    assert _SLASH_SHAPE.pattern == _COMMAND_SHAPE.pattern, "两处形状漂了"


# ---------- 开关：接上了，而且回滚那条路还在 ----------


def test_the_lane_is_actually_wired():
    """🔴 cmux 这条路**真接上了**（11-23 角色 1 实测落地），不再是桩。

    这条测试原来是反方向的（盯着 `WIRED` 别在命令量出来之前提前打开）。那个意图已经完成，
    现在换成正方向的闸：开关开着、cmux 在名单里、而且 `_argv_*` 不再抛 NotImplementedError。
    三样缺一样都是「发版说能用了、实际一条都走不通」。
    """
    assert ts.WIRED is True, "WIRED 被关掉了 —— 要是故意回滚，连带把这条测试改了并写上原因"
    assert ts.TERM_CMUX in ts._SENDERS, "cmux 不在能送的名单里 ⇒ 主人 29 个会话一个都走不通"
    cmd = ts._argv_text(ts.TERM_CMUX, SURFACE_A, "hi")
    assert cmd is not None and cmd.argv, "_argv_text 还是桩"
    assert ts._argv_key(ts.TERM_CMUX, SURFACE_A, ts.KEY_ESC) is not None, "_argv_key 还是桩"


def test_turning_the_switch_off_sends_nothing(monkeypatch, cmux):
    """🔴 回滚开关：`WIRED=False` ⇒ 一律退回今天的两条路，**runner 一次都不许被叫**。

    出事时这是唯一的急救阀（不用改接线、不用回滚整个发版），所以它得有测试守着。
    `can_send` 回空串 = 退回 relay / resume，用户感觉不到这条路存在过。
    """
    monkeypatch.setattr(ts, "WIRED", False)
    sender = make()
    assert sender.can_send(SID) == (False, "")
    assert sender.can_send(SID, for_key=True) == (False, "")
    for res in (sender.send_text(SID, "/goal 看看天气"), sender.send_key(SID, ts.KEY_ESC)):
        assert res.ok is False
        assert res.delivered is False, "没送出去却说送到了 ⇒ 用户不会重试"
        assert res.text, "失败必须有一句话"
    assert sender.adapter.runner.calls == [], "关掉开关之后还是起了子进程"


def test_a_terminal_outside_the_senders_list_is_told_so(cmux, monkeypatch):
    """认出来了、但那种终端不在能送的名单里 ⇒ 照实说（带名字），**不去试一条注定失败的命令**。

    这条接的是角色 2 那边的降级：tmux / Terminal.app 还没量过怎么送，
    名单里没有它们，用户得到的是「插不上手，到那个窗口自己打」而不是一句技术错误。
    """
    monkeypatch.setattr(ts, "_SENDERS", (ts.TERM_CMUX,))
    sender = make(CHAINS[ts.TERM_TMUX])
    monkeypatch.setattr(ts, "_process_env", envs(**{"101": cmux_env(SURFACE_A)}))
    can, why = sender.can_send(SID)
    assert can is False
    assert why == ts.WHY_TERM_NO_API.format(label="tmux")
    res = sender.send_text(SID, "/goal 看看天气")
    assert res.ok is False and sender.adapter.runner.calls == []


def test_send_key_whitelist_only():
    """🔴 按键是**白名单**。Ctrl-C 会把主人整个会话关掉（实测），Ctrl-Z 会把它挂成后台
    （11-18 那次打掉 7 个窗口、弄死一个跑了 7 天半的）—— 一个都不许进来。
    """
    assert set(ts.ALLOWED_KEYS) == {"esc", "enter"}, (
        f"能按的键变成 {ts.ALLOWED_KEYS} —— 加键之前先读 termsend.ALLOWED_KEYS 那段红字")
    sender = make()
    for bad in ("ctrl-c", "ctrl-d", "ctrl-z", "C-c", "sigint", "", "q"):
        res = sender.send_key(SID, bad)
        assert res.ok is False and res.text == ts.WHY_BAD_KEY, bad
    assert sender.adapter.runner.calls == []


def test_send_text_rejects_empty():
    res = make().send_text(SID, "   ")
    assert res.ok is False and res.text == ts.WHY_EMPTY


def test_the_removed_marks_are_not_evidence():
    """🔴 两条判据被删掉了，这里钉住别有人凭「看起来像」加回来。

    - `cmux-claude-wrapper`：那个文件**在**、shim 脚本里也引用它，但**任何活进程的 argv 里
      都不出现**（角色 1/2 各自实测 0 命中）。文件存在 ≠ 这条路被走过。
    - `tmux: server`：Linux 下 tmux 改写 argv 的写法，macOS 上 0 命中（本机 tmux 3.6a）
      ⇒ 留着等于那条判据在 mac 上**永不生效**，而我们只在 mac 上跑。

    留着它们的代价不是多两个字符串：它们会让人以为「tmux 认得出」这件事有两道判据，
    而实际只有 argv[0] 一道 —— 下次有人把 argv[0] 那道改坏，看着像还有兜底。
    """
    marks = {kind: set(m) for kind, m, _exe in ts._TERM_MARKS}
    assert "cmux-claude-wrapper" not in marks[ts.TERM_CMUX], \
        "cmux-claude-wrapper 又加回来了 —— 实测任何活进程的 argv 里都没有它"
    assert "tmux: server" not in marks[ts.TERM_TMUX], \
        "tmux: server 又加回来了 —— 那是 Linux 的写法，mac 上永不命中"
    # 阳性对照：真正命中的那条还在（别把这条测试读成「把判据删空就对了」）
    assert "/cmux.app/" in marks[ts.TERM_CMUX]


def test_tmux_is_recognised_by_argv0_alone():
    """tmux 现在只靠 argv[0] 的名字认 ⇒ 带不带参数都要认得出，别的程序不许被带上。"""
    for args in ("/opt/homebrew/bin/tmux",
                 "/opt/homebrew/bin/tmux -S /tmp/tmux-502/default new-session -d",
                 "/usr/local/bin/tmux attach -t work"):
        assert ts.terminal_of_pid(chain(args), 101) == ts.TERM_TMUX, args
    # 阴性：名字里带 tmux 但不是它（argv[0] 的文件名要**整个**等于 tmux）
    for other in ("/usr/local/bin/tmuxinator start work", "/opt/homebrew/bin/tmux-mem-cpu-load"):
        assert ts.terminal_of_pid(chain(other), 101) == ts.TERM_UNKNOWN, other


# ---------- 终端主动拦住：和「窗口没了」分开说 ----------


def _blocked(stderr: str = "", stdout: str = "") -> RunResult:
    return RunResult(returncode=1, stdout=stdout, stderr=stderr)


def test_a_held_draft_is_not_reported_as_a_missing_window(cmux):
    """🔴 终端**主动拦住**时退出码也是非 0 ⇒ 不许说成「窗口可能已经关掉了」。

    那两种情况窗口好得很，该让用户知道的是「你有半句话没发」/「有个问题等你答」。
    说「窗口可能关了」会让他跑去电脑前找一个根本没关的窗口。
    """
    cases = {
        "有人正打字": ("the agent prompt in surface X holds text someone is typing, "
                   "so nothing was sent", "打了一半"),
        "有弹窗等着答": ("surface X is waiting on a question or dialog, so nothing was sent",
                    "等你"),
    }
    for label, (err, want) in cases.items():
        sender = make()
        sender.adapter.runner.result = _blocked(stderr=err)
        res = sender.send_text(SID, "/goal 看看天气")
        assert res.ok is False, label
        assert res.delivered is False, f"{label}：没送出去却说送到了"
        assert want in res.text, (label, res.text)
        assert res.text != ts.WHY_NO_WINDOW.format(label="cmux"), \
            f"{label}：被误报成「窗口可能关了」"
        # 露不露内部概念：那两句话里不许出现 surface / 窗口编号
        assert "surface" not in res.text.lower() and "X" not in res.text


def test_other_errors_are_not_mistaken_for_a_held_draft(cmux):
    """🔴 阴性对照：**别的**错和空串都不许被判成那两种。

    判据是 cmux 自己那两句话里的原文。要是写宽了（比如只认 `typing` / `waiting`），
    一条无关的报错就会被说成「你有半句话没发」—— 用户去电脑前找一句他没打的话。
    """
    for junk in ("Error: Invalid surface handle",
                 "error: connection refused",
                 "waiting",                      # 单个词不算（判据要整句）
                 "someone is typing",            # 半截也不算
                 "", "   ", "panic: runtime error"):
        assert ts._blocked_reason(_blocked(stderr=junk)) == "", junk
        sender = make()
        sender.adapter.runner.result = _blocked(stderr=junk)
        res = sender.send_text(SID, "/goal 看看天气")
        assert res.text == ts.WHY_NO_WINDOW.format(label="cmux"), (junk, res.text)
    # 阳性对照：证明这把尺子真能看见那两句（不是恒回空串）
    assert ts._blocked_reason(_blocked(stderr="holds text someone is typing"))
    # stdout 和 stderr 两边都要认（不同版本往哪条管道写没量准）
    assert ts._blocked_reason(_blocked(stdout="waiting on a question or dialog"))


def test_blocked_reason_never_raises():
    """`result` 缺字段 / 是 None ⇒ 空串，不抛（它跑在报错路径上，再炸一次最糟）。"""
    class Bare:
        pass

    for weird in (None, Bare(), RunResult(returncode=1)):
        assert ts._blocked_reason(weird) == ""


def test_run_failures_map_to_distinct_copy(cmux):
    """跑那条命令失败的几种形状各有各的一句话（起不来 / 超时 / 退出码非 0），而且都不号称送到了。"""
    shapes = {
        "起不来": (RunResult(returncode=-1, error="没能启动"), ts.WHY_NO_CLI),
        "超时": (RunResult(returncode=-1, timed_out=True), ts.WHY_UNREACHABLE),
        "退出码非 0": (RunResult(returncode=2, stderr="no such window"), ts.WHY_NO_WINDOW),
    }
    for label, (result, template) in shapes.items():
        sender = make()
        sender.adapter.runner.result = result
        res = sender.send_text(SID, "/goal 看看天气")
        assert res.ok is False, label
        assert res.delivered is False, f"{label}：没送出去却说送到了"
        assert res.text == template.format(label="cmux"), (label, res.text)


def test_successful_send_is_delivered_but_claims_no_reply(cmux):
    """打进去了 ⇒ ok + delivered，但**不带回复**：这条路没有回执，不许编一句「它收到了」。"""
    sender = make()
    res = sender.send_text(SID, "/goal 看看天气")
    assert (res.ok, res.delivered, res.text) == (True, True, "")
    assert len(sender.adapter.runner.calls) == 1


def test_no_window_id_means_we_do_not_send(cmux, monkeypatch):
    """🔴 读不到那个进程的窗口 id ⇒ 照实说，**绝不拿别处的窗口顶上**。

    这是上面「令牌不按 surface 分权」那条的直接后果：这儿要是兜底成「随便挑一个窗口」，
    就等于把主人的话打进他另一个会话。取不到就是取不到。
    """
    monkeypatch.setattr(ts, "_process_env", envs())  # 一个进程都读不到环境
    sender = make()
    res = sender.send_text(SID, "/goal 看看天气")
    assert res.ok is False and res.text == ts.WHY_NO_WINDOW.format(label="cmux")
    assert sender.adapter.runner.calls == [], "窗口都没找到就起了子进程"


def test_missing_capability_means_we_do_not_even_try(cmux, monkeypatch):
    """有窗口 id、但读不到连上去要的那把凭据 ⇒ 不去试一条注定被拒的命令。

    实测：不带 `CMUX_SOCKET_CAPABILITY` 直连一律被拒（和令牌错同一句）⇒ 试了只是白跑一次。
    """
    monkeypatch.setattr(ts, "_process_env",
                        envs(**{"101": {ts._ENV_SURFACE: SURFACE_A}}))
    sender = make()
    res = sender.send_text(SID, "/goal 看看天气")
    assert res.ok is False and sender.adapter.runner.calls == []


# ---------- 🔴 安全：窗口只能来自目标自己，令牌和正文都不进 argv ----------


def test_surface_comes_from_the_target_process_not_the_caller(cmux, monkeypatch):
    """🔴 本轮最大的事故面：**送错窗口 = 把主人的话打进他另一个会话。**

    实测（角色 1，11-23）：cmux 的令牌**对整个 socket 有效、不按 surface 分权** ——
    拿 A 会话的令牌去操作 B 的 surface 照样成功。上游那一层压根不拦，
    所以边界只能我们自己守：窗口 id 必须从**这个 sessionId 自己那个进程**的
    `CMUX_SURFACE_ID` 里取，调用方一个字都插不上手。

    这里造两个真会话 A / B（各自的 pid、各自的窗口），断言发给 A 时命令里的窗口是 A 的、
    **一次都没出现过 B 的**。
    """
    table = ProcTable([
        P(101, 102, CLAUDE_ARGS, "S+"), P(102, 103, ZSH), P(103, 104, LOGIN),
        P(201, 202, CLAUDE_ARGS, "S+"), P(202, 203, ZSH), P(203, 104, LOGIN),
        P(104, 1, CHAINS[ts.TERM_CMUX]),
    ])
    adapter = FakeAdapter(table)
    adapter._rows = [
        ("user", {"sessionId": SID, "pid": 101, "procStart": L, "startedAt": L}),
        ("user", {"sessionId": SID_OTHER, "pid": 201, "procStart": L, "startedAt": L}),
    ]
    monkeypatch.setattr(ts, "_process_env", envs(**{
        "101": cmux_env(SURFACE_A, TOKEN_A),
        "201": cmux_env(SURFACE_B, TOKEN_B),
    }))
    sender = ts.TermSend(adapter)

    res = sender.send_text(SID, "/goal 看看天气")
    assert res.ok, res.text
    argv, kw = sent(sender)
    assert SURFACE_A in argv, f"发给 A 却没带 A 的窗口：{argv}"
    assert SURFACE_B not in argv, f"🔴 带上了 B 的窗口 ⇒ 打进主人另一个会话：{argv}"
    # 令牌也得是 A 自己那把。`.join("")` 那种写法恒等于空串 ⇒ 这条断言会恒绿，别那么写
    env = kw.get("env") or {}
    assert env.get(ts._ENV_CAPABILITY) == TOKEN_A, "没用 A 自己那把令牌"
    assert TOKEN_B not in " ".join(f"{k}={v}" for k, v in env.items()), "B 的令牌漏进环境了"

    # 反向：发给 B 就该是 B 的窗口（证明上面那条不是「恒等于 A」蒙对的）
    adapter.runner.calls.clear()
    assert sender.send_text(SID_OTHER, "/context").ok
    argv_b, _ = sent(sender)
    assert SURFACE_B in argv_b and SURFACE_A not in argv_b, argv_b


def test_two_sources_must_agree_on_the_window(cmux, monkeypatch):
    """🔴 两源打架 ⇒ 一个字都不送（角色 1 11-23 加的那道闸，他原话「这是最后一道闸，别删」）。

    为什么需要第二源：pid 是从**文件**（会话注册表）来的，而 `same_process` 只能保证
    「这个 pid 还是当初那个进程」，保证不了「它就是这个 sessionId 的进程」——
    进程没了、pid 被系统回收给别人、文件没来得及更新，都会让它指到另一个会话上。
    而令牌对整个连接有效、不按窗口分权 ⇒ **pid 指错直接等于把主人的话打进他另一个会话**。
    所以要一份独立证据：终端程序自己按 sessionId 给出的窗口（和 pid 无关），对不上就不送。

    🔴 这条测试是补上来的：角色 1 加完这道闸之后我的用例**一条都没变红**（反向对照 M8
    把 `if want and surface != want` 改成 `if False`，69 条全绿）—— 因为我的 fixture
    让两源恒等。闸门没有测试 = 下一个人重构时静默删掉它。
    """
    # 进程身上说是 B，而终端程序按 sessionId 查出来是 A ⇒ 打架
    monkeypatch.setattr(ts, "_process_env", envs(**{"101": cmux_env(SURFACE_B, TOKEN_B)}))
    monkeypatch.setattr(ts, "_cmux_surfaces_of_session", lambda sid: {SURFACE_A.upper()})
    sender = make()
    res = sender.send_text(SID, "/goal 看看天气")
    assert res.ok is False, "两源打架还是把字送出去了"
    assert sender.adapter.runner.calls == [], "🔴 两源打架还起了子进程 ⇒ 可能打进别人的会话"
    assert res.text == ts.WHY_NO_WINDOW.format(label="cmux")

    # 阳性对照：两源一致就放行（证明上面那条不是「什么都不让过」蒙对的）
    monkeypatch.setattr(ts, "_cmux_surfaces_of_session", lambda sid: {SURFACE_B.upper()})
    assert sender.send_text(SID, "/goal 看看天气").ok
    argv, _ = sent(sender)
    assert SURFACE_B in argv and SURFACE_A not in argv, argv


def test_closed_windows_are_dropped_from_the_second_source(monkeypatch):
    """🔴 已经关掉的窗口不许进那组交叉证据。

    实测（角色 1）：窗口关掉之后那条查询命令**照样回它**，`surface_id` 完整、
    `active_for_surface` 还是 true ⇒ 只有 `stored_pid_exists` 分得出死活。
    收进来的后果：一个早就关了的窗口会让「两源一致」这道闸恒成立，
    等于那道闸白守 —— 而它正是挡「打进别人会话」的最后一道。

    这条喂一份假的查询结果（活的 + 死的各一行），不碰本机真 cmux。
    """
    import json

    payload = json.dumps({"sessions": [
        {"session_id": SID, "surface_id": SURFACE_A, "stored_pid_exists": True},
        {"session_id": SID, "surface_id": SURFACE_B, "stored_pid_exists": False,
         "active_for_surface": True},          # 关掉了，但它照样回
        {"session_id": SID_OTHER, "surface_id": "CCCC", "stored_pid_exists": True},
    ]})

    class FakeRun:
        stdout = payload

    monkeypatch.setattr(ts, "_cmux_bin", lambda: FAKE_CMUX)
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **kw: FakeRun())
    got = ts._cmux_surfaces_of_session(SID)
    assert got == {SURFACE_A.upper()}, f"死窗口 / 别的会话漏进来了：{got}"


def test_second_source_never_raises(monkeypatch):
    """那条查询命令不在 / 超时 / 回了一坨非 JSON ⇒ 空集合，**不抛**（⇒ 交叉闸不卡，照旧能送）。"""
    monkeypatch.setattr(ts, "_cmux_bin", lambda: FAKE_CMUX)

    class Junk:
        stdout = "Error: unknown flag --json\n"

    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **kw: Junk())
    assert ts._cmux_surfaces_of_session(SID) == set()

    def boom(*a, **kw):
        raise OSError("no cmux")

    monkeypatch.setattr(ts.subprocess, "run", boom)
    assert ts._cmux_surfaces_of_session(SID) == set()
    monkeypatch.setattr(ts, "_cmux_bin", lambda: "")
    assert ts._cmux_surfaces_of_session(SID) == set()


def test_a_missing_second_source_does_not_block_the_lane(cmux, monkeypatch):
    """终端程序那边查不到（空串）⇒ 这道交叉闸**不卡**，照旧按进程身上那份送。

    cmux 没装命令行工具、版本不认 `--json`、那张表还没刷新都会让它空 ——
    拿「读不到」当「不一致」会把整条路堵死（29 个会话一个都走不通）。
    """
    monkeypatch.setattr(ts, "_cmux_surfaces_of_session", lambda sid: set())
    sender = make()
    assert sender.send_text(SID, "/goal 看看天气").ok
    argv, _ = sent(sender)
    assert SURFACE_A in argv, argv


def test_a_session_open_in_two_windows_is_not_a_conflict(cmux, monkeypatch):
    """🔴 同一个会话开在两个窗口里**不算两源打架**（实测见过这种情况）。

    那条查询命令对这种会话只回一行、静默挑一个 ⇒ 要是拿「那一个」去比对，
    另一个窗口的正常请求会被判成打架挡掉。所以第二源收的是**一组**窗口，
    闸门只要求「进程身上那个窗口在这组里」。
    """
    monkeypatch.setattr(ts, "_cmux_surfaces_of_session",
                        lambda sid: {SURFACE_A.upper(), SURFACE_B.upper()})
    monkeypatch.setattr(ts, "_process_env", envs(**{"101": cmux_env(SURFACE_B, TOKEN_B)}))
    sender = make()
    assert sender.send_text(SID, "/goal 看看天气").ok, "同一会话的第二个窗口被误判成打架"
    argv, _ = sent(sender)
    assert SURFACE_B in argv, argv


def test_window_comparison_ignores_case(cmux, monkeypatch):
    """两源大小写不同不算打架（UUID 一边大写一边小写是实测见过的形状）。"""
    monkeypatch.setattr(ts, "_cmux_surfaces_of_session", lambda sid: {SURFACE_A.upper()})
    monkeypatch.setattr(ts, "_process_env", envs(**{"101": cmux_env(SURFACE_A.lower())}))
    sender = make()
    assert sender.send_text(SID, "/goal 看看天气").ok, "只是大小写不同就被判成打架了"


def test_the_token_never_appears_in_argv_or_logs(cmux, caplog):
    """🔴 令牌只经**环境变量**传给子进程。

    `argv` 在 `ps` 里全机可见（任何用户都看得到），日志会被打包带走、会进上报。
    那把令牌能操作主人**整个** cmux（不按 surface 分权）⇒ 漏一次等于把整台机器的终端交出去。
    """
    import logging

    caplog.set_level(logging.DEBUG)
    sender = make()
    assert sender.send_text(SID, "/goal 看看天气").ok
    argv, kw = sent(sender)
    assert TOKEN_A not in " ".join(argv), f"令牌进了 argv：{argv}"
    assert SOCK_A not in " ".join(argv), f"连接路径进了 argv：{argv}"
    # 真的是靠环境传过去的（否则这条命令根本连不上，等于悄悄全退回）
    assert (kw.get("env") or {}).get(ts._ENV_CAPABILITY) == TOKEN_A, "令牌没经环境传过去"
    blob = caplog.text + " ".join(str(r.msg) for r in caplog.records)
    assert TOKEN_A not in blob, "令牌进了日志"
    assert SOCK_A not in blob, "连接路径进了日志"


def test_the_users_words_never_appear_in_argv(cmux):
    """🔴 用户那句话只走 stdin。`ps` 全机可见 ⇒ 他在手机上说的话不该出现在那儿。"""
    secret = "把数据库密码改成 hunter2"
    sender = make()
    assert sender.send_text(SID, secret).ok
    argv, kw = sent(sender)
    assert secret not in " ".join(argv), f"正文进了 argv：{argv}"
    assert kw.get("input_text") == secret, "正文没走 stdin ⇒ 那它去哪了"


def test_never_passes_force(cmux):
    """🔴 **永不加 `--force`**（这条写死，理由是实测出来的）。

    cmux 文档说输入框里有半句草稿就不送、要 `--force` 才送。角色 1 实测：在主人那种
    「普通终端 surface 里跑 claude」的会话上，那道闸**一个会话都不生效** ——
    `paste --submit` 直接 rc=0，把「主人的半句草稿 + 我们的字」拼好**替他提交出去了**。
    那道闸认的是 cmux 自己管的 agent surface，主人 29 个会话一个都不是。
    ⇒ 草稿这件事只能靠我们自己的 `_busy` 守，而 `--force` 正好是「连草稿也照送」的开关。
    """
    sender = make()
    assert sender.send_text(SID, "/goal 看看天气").ok
    argv, _ = sent(sender)
    assert "--force" not in argv, f"🔴 加了 --force ⇒ 会把主人没发的半句话替他发出去：{argv}"

    sender.adapter.runner.calls.clear()
    assert sender.send_key(SID, ts.KEY_ESC).ok
    argv_key, _ = sent(sender)
    assert "--force" not in argv_key, argv_key


def test_the_draft_guard_is_ours_not_upstreams(cmux):
    """🔴 「目标不空闲就不送字」这道闸必须在**我们**码里（上游那道对主人不生效）。

    `_busy` 为真 ⇒ `can_send(for_key=False)` 回 `(False, WHY_BUSY)`、runner 零调用；
    而 Esc（`for_key=True`）不受这条限制 —— 要打断的本来就是正在跑的那一轮。
    「读不出状态」也算忙（保守：宁可少送一次，也别和主人抢输入框）。
    """
    for status in ("running", "waiting", "unknown"):
        sender = make(status=status)
        assert sender.can_send(SID) == (False, ts.WHY_BUSY), status
        res = sender.send_text(SID, "/goal 看看天气")
        assert res.ok is False, status
        assert res.text == ts.WHY_BUSY, (status, res.text)
        assert sender.adapter.runner.calls == [], f"{status}：目标正忙还是把字送出去了"
        # Esc 照旧放行（阳性对照：证明上面那条不是「什么都不让过」）
        assert sender.can_send(SID, for_key=True) == (True, ""), status
        assert sender.send_key(SID, ts.KEY_ESC).ok, status


def test_cmux_argv_shape_is_the_measured_one(cmux):
    """命令形状钉住（都是实测定下来的，改之前先读 `_argv_text` 的 docstring）。

    送文字 `paste --surface <窗口> --submit -`：不是 `send` —— 实测 `send 'A\\nB'` 会把
    字面 `\\n` 当回车、把「A」提交掉只剩「B」在框里，用户随口一句带换行就被截断并提前发出去。
    打断 `send-key --surface <窗口> escape`：实测把那一轮打成「Interrupted」，进程还活着。
    """
    sender = make()
    assert sender.send_text(SID, "/goal 看看天气").ok
    argv, kw = sent(sender)
    assert argv[1:] == ["paste", "--surface", SURFACE_A, "--submit", "-"], argv
    assert argv[0].endswith("cmux"), argv[0]
    assert kw.get("input_text") == "/goal 看看天气"

    sender.adapter.runner.calls.clear()
    assert sender.send_key(SID, ts.KEY_ESC).ok
    argv_key, kw_key = sent(sender)
    assert argv_key[1:] == ["send-key", "--surface", SURFACE_A, "escape"], argv_key
    assert not kw_key.get("input_text"), "按键不该带 stdin"


def test_a_newline_heavy_message_goes_through_stdin_untouched(cmux):
    """带换行的消息原样走 stdin（这正是选 `paste` 而不是 `send` 的理由）。"""
    text = "第一行\n第二行\n第三行"
    sender = make()
    assert sender.send_text(SID, text).ok
    argv, kw = sent(sender)
    assert kw.get("input_text") == text
    assert "\n" not in " ".join(argv), argv


def test_no_cmux_cli_says_so_without_running_anything(cmux, monkeypatch):
    """电脑上找不到 cmux 的命令行工具 ⇒ 照实说，不去跑一条空命令。"""
    monkeypatch.setattr(ts, "_cmux_bin", lambda: "")
    sender = make()
    res = sender.send_text(SID, "/goal 看看天气")
    assert res.ok is False and res.text
    assert sender.adapter.runner.calls == []


def test_process_env_only_keeps_the_three_cmux_keys(monkeypatch):
    """🔴 读进程环境**只挑那三个**，别的一个都不带回来。

    那片环境里还有别的程序的凭据 —— 一股脑读回来再塞给子进程，等于顺手把它们也扩散出去
    （`CMUX_SOCKET_PASSWORD` 就在隔壁，实测它和令牌是两样东西）。

    🔴 判据必须是**真跑一遍 `_process_env`**，不是只看 `_ENV_WANTED` 这个常量。
    第一版只断言常量 ⇒ 把过滤那一行删掉（`env[key] = …` 不再判 `in _ENV_WANTED`）测试照样绿
    （反向对照抓到的）。这里喂一份带杂物的假 `ps` 输出，看它只挑出三样。
    """
    assert set(ts._ENV_WANTED) == {ts._ENV_SURFACE, ts._ENV_CAPABILITY, ts._ENV_SOCKET}
    assert ts._ENV_SURFACE not in ts._ENV_CREDS, "窗口 id 是参数不是凭据，别混进环境那一组"

    class FakeRun:
        stdout = (
            f"TERM=xterm-256color {ts._ENV_SURFACE}={SURFACE_A} "
            f"{ts._ENV_CAPABILITY}={TOKEN_A} {ts._ENV_SOCKET}={SOCK_A} "
            "CMUX_SOCKET_PASSWORD=pw-must-not-leak CMUX_CUA_TELEMETRY_ENABLED=false "
            "ANTHROPIC_API_KEY=sk-must-not-leak AWS_SECRET_ACCESS_KEY=also-secret "
            "/Users/x/.local/bin/claude -r\n"
        )

    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **kw: FakeRun())
    env = ts._process_env(101)
    assert env == {ts._ENV_SURFACE: SURFACE_A, ts._ENV_CAPABILITY: TOKEN_A,
                   ts._ENV_SOCKET: SOCK_A}, env
    blob = " ".join(f"{k}={v}" for k, v in env.items())
    for secret in ("pw-must-not-leak", "sk-must-not-leak", "also-secret"):
        assert secret not in blob, f"把 {secret} 也带回来了"


def test_process_env_never_raises(monkeypatch):
    """`ps` 不在 / 超时 / 读出乱码 ⇒ 空字典，**不抛**（上层照实走「找不到窗口」）。"""
    def boom(*a, **kw):
        raise OSError("no ps")

    monkeypatch.setattr(ts.subprocess, "run", boom)
    assert ts._process_env(101) == {}
    for bad in (None, 0, -1, "abc", ""):
        assert ts._process_env(bad) == {}


# ---------- 绝不模拟全局键盘 ----------


def _code_strings_and_names(mod) -> set:
    """这个模块**真会跑的**那些字面量和标识符（注释和 docstring 不算）。

    🔴 判据必须跳过注释和 docstring，这和第 40 轮 `test_no_clear_bang_escape_hatch`
    踩的是同一个坑：源码里有一句注释写着「绝不模拟全局键盘（AppleScript / System Events）」，
    直接 `in src` 会抓到那句**禁令自己** ⇒ 第一版就这么假红的。
    """
    import ast

    tree = ast.parse(open(mod.__file__, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            first = body[0] if body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                docstrings.add(id(first.value))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            out.add(node.value)
        elif isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            out.add(getattr(node, "module", "") or "")
            out.update(a.name for a in node.names)
    return {s for s in out if s}


def test_the_subprocess_guard_covers_every_real_exit(monkeypatch):
    """🔴 那道「不许真起子进程」的闸，必须盖住这条路**所有**真会 `subprocess.run` 的出口。

    现在是两个：`_process_env`（`ps eww`）和 `_cmux_surface_of_session`（`cmux sessions list`）。
    以后加了第三个而忘了加进 fixture ⇒ 测试会悄悄去问本机真 cmux / 真进程，
    那道闸就从「不许碰」变成「碰了也没人知道」。这条按源码数出口，对不上就红。
    """
    import ast

    tree = ast.parse(open(ts.__file__, encoding="utf-8").read())
    runners = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Attribute) and inner.attr in ("run", "Popen") \
                    and isinstance(inner.value, ast.Name) and inner.value.id == "subprocess":
                runners.add(node.name)
    assert runners == {"_process_env", "_cmux_surfaces_of_session"}, (
        f"这条路真起子进程的出口变成 {sorted(runners)} —— 把新的也加进 `_no_cmux_or_ps_for_real`")


def test_the_own_subprocess_optout_list_is_not_stale():
    """`_OWN_SUBPROCESS` 里每个名字都得是本文件真有的用例（改名之后那条就不再被放过了）。"""
    here = {name for name in globals() if name.startswith("test_")}
    missing = _OWN_SUBPROCESS - here
    assert not missing, f"这些名字已经不存在了，删掉或改名：{sorted(missing)}"


def test_no_applescript_anywhere_in_this_lane():
    """🔴 主人明确禁过 AppleScript / System Events 模拟键盘。

    它打进的是**当时聚焦的那个窗口**，不是我们想送的那个 —— 这种自动化打进过他正在用的
    Claude 会话和一个付款页（feedback_no_keystroke_automation_for_measurement）。
    这条路只许走那个终端程序自己的命令行接口（它只认自己的窗口）。
    这条测试盯着别有人图省事加回来。
    """
    code = _code_strings_and_names(ts)
    for banned in ("osascript", "applescript", "system events", "cgevent", "pyautogui",
                   "keystroke", "quartz", "pynput"):
        hit = [s for s in code if banned in s.lower()]
        assert not hit, f"termsend.py 真会跑的代码里出现了 {banned}：{hit} —— 这是被明确禁的"
    # 阳性对照：这把尺子真能看见这个文件的代码（不是解析出个空集恒绿）
    assert "cmux" in code and "ActionResult" in code


# ---------- 分派顺序：按源码断言，防漂 ----------


def _src_of(mod) -> str:
    return open(mod.__file__, encoding="utf-8").read()


def test_terminal_lane_is_tried_before_relay():
    """🔴 顺序：这条路要排在 relay **之前**。

    反了的话斜杠命令先被 relay 接走，上游写死的 `skipSlashCommands` 让它当一句闲话处理
    （第 40 轮的现场：目标回「这条命令是别的会话来的、不是你打的，我没跑」），
    这条新路永远不会被试到。
    """
    import bridge.fleet.control as control_mod

    src = _src_of(control_mod)
    i_term = src.index("termsent = self._claude_term_first(")
    i_relay = src.index("return adapter.relay(sid, text, timeout=timeout)")
    assert i_term < i_relay, "终端那条路排到 relay 后面了 ⇒ 斜杠命令会被 relay 吃掉"


def test_terminal_lane_is_tried_before_resume():
    """也要排在 `-p --resume` 之前 —— 终端占着的会话那条路会分叉，它自己会拒，但顺序别反。"""
    import bridge.fleet.control as control_mod

    src = _src_of(control_mod)
    i_term = src.index("termsent = self._claude_term_first(")
    i_resume = src.index("return adapter.send(sid, text, timeout=timeout, policy=policy")
    assert i_term < i_resume


def test_the_lane_choice_lives_in_control_not_in_app():
    """🔴 选路和判据必须在**同一个地方**（`control.send`），不能在 `app.py`。

    第 40 轮当天就撞了：拦在 `app._maybe_send_to_fleet` ⇒ 主人打 `/goal 看看天气`，
    回「斜杠命令发不进 Claude「198 k3s 节点硬盘」」，他问「是我没更新吗？」——
    **那条会话本来能跑**。现在多了第三条路，这个坑更深（app 里判不出该走哪条）。
    """
    import bridge.app as app_mod

    app_src = _src_of(app_mod)
    for leaked in ("wants_terminal", "detect_terminal", "can_term_send", "term_send("):
        assert leaked not in app_src, f"{leaked} 跑到 app.py 里了 ⇒ 选路又被拆成两处"


def test_fallback_to_relay_is_a_none_not_a_failure():
    """🔴 `_claude_term_first` 用 `None` 表示「这条路不管」，不是返回一个失败的结果。

    返回失败 ⇒ relay / resume 压根不会被试到，等于把两条好路一起拦了。
    """
    import bridge.fleet.control as control_mod

    src = _src_of(control_mod)
    start = src.index("def _claude_term_first")
    body = src[start:src.index("\n    def ", start + 10)]
    assert "if not wants_terminal(text):\n            return None" in body, \
        "普通消息没有退回 None ⇒ 它会被终端那条路吃掉"
    assert "if not why:\n            return None" in body, \
        "why 空串没有退回 None ⇒ 退回约定崩了，relay 接不到"


def test_destructive_commands_are_blocked_upstream_of_this_lane():
    """🔴 这条路**真会执行**斜杠命令 ⇒ `/clear` `/init` 那道闸必须排在它前面。

    第 40 轮那道闸在 `app._handle_command` 里（`destructive_command`），而
    `_handle_command` 跑在 `_maybe_send_to_fleet` 之前（r40 的
    `test_command_dispatch_runs_before_talk_mode` 盯着这个顺序）。
    这里补一条本轮特有的：relay 那条路上 `/clear` 送进去也不会跑（上游关着），
    所以顺序反了在第 40 轮只是「白花一轮钱」；**到这条路上就是真把主人的上下文清了**，
    而他在手机上只看到「这一轮没出结果」。
    """
    import bridge.app as app_mod

    src = _src_of(app_mod)
    i_destructive = src.index("blocked = destructive_command(text)")
    i_fleet = src.index("if await self._maybe_send_to_fleet(scope, chat_id, text):")
    i_dispatch = src.index("if await self._handle_command(scope, chat_id, text):")
    assert i_dispatch < i_fleet, "命令分派排到对话模式后面了 ⇒ /clear 会走到发送那条路上"
    # 那道闸确实在 _handle_command 体内（不是别处同名的一行）
    import ast

    tree = ast.parse(src)
    line = src[:i_destructive].count("\n") + 1
    holder = [n.name for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.lineno <= line <= (n.end_lineno or 0)]
    assert "_handle_command" in holder, f"写死拦下的两条跑到 {holder} 里了"
    # 这条路自己也不许把 /clear 当成「归我管」之外的特例放过
    assert ts.wants_terminal("/clear") is True, "形状判据漂了（上面那道闸靠它之前就拦掉）"


def test_stop_tries_esc_before_saying_it_cannot_stop():
    """🔴 `/stop`：先试在那个窗口里按 Esc，**再**退回「只能到电脑上按 Esc」那句。

    顺序反了的话这条路白加 —— 用户永远只看到那句「停不了」。
    """
    import bridge.fleet.control as control_mod

    src = _src_of(control_mod)
    i_esc = src.index("stopped = self._claude_term_stop(adapter, sid)")
    i_fallback = src.index('method = (getattr(adapter, "relay_stop", None) if relay else None)')
    assert i_esc < i_fallback, "按 Esc 排到「停不了」后面了"


# ---------- 分派：真跑一遍 control.send，不只读源码 ----------


class OldLaneSpy:
    """**今天**那个 adapter 的形状：只有 relay 和 resume 两条路，压根没有终端那三个方法。

    拆成两个类是刻意的：想测「老 adapter 照样好用」，就不能从带着那三个方法的类继承再删 ——
    删子类的那份父类的又露出来，测试照样绿（这个台架第一版就是这么假绿的，`hasattr` 自检抓到了）。
    """

    tool = "claude"

    def __init__(self, *, term_ok: bool = False, term_why: str = "", relay_ok: bool = True) -> None:
        self.term_ok = term_ok
        self.term_why = term_why
        self.relay_ok = relay_ok
        self.calls: list = []
        self.last_error = ""

    # —— 总控扫描要的几样 ——
    def list(self, limit=20):
        return []

    def running(self):
        return []

    def find(self, sid):
        return None

    def read(self, sid, n=4):
        return []

    def can_relay(self, sid):
        self.calls.append(("can_relay", None))
        return self.relay_ok, "" if self.relay_ok else "现在送不进去。到电脑上和它说吧。"

    def relay(self, sid, text, timeout=600, **kw):
        self.calls.append(("relay", text))
        return ActionResult(True, "relay 回复", sid, delivered=True)

    def relay_stop(self, sid):
        self.calls.append(("relay_stop", None))
        return ActionResult(False, "只能到电脑上按 Esc", sid)

    def send(self, sid, text, timeout=600, **kw):
        self.calls.append(("send", text))
        return ActionResult(True, "resume 回复", sid)

    def stop(self, sid):
        self.calls.append(("stop", None))
        return ActionResult(False, "停不了", sid)


class LaneSpy(OldLaneSpy):
    """接上第三条路之后的形状。`term_ok` / `term_why` 模拟 `can_term_send` 的两种答案
    （空 why = 退回别的路 / 有内容 = 真做不到）。"""

    def can_term_send(self, sid, *, for_key=False):
        self.calls.append(("can_term_send", for_key))
        return self.term_ok, self.term_why

    def term_send(self, sid, text):
        self.calls.append(("term_send", text))
        return ActionResult(True, "", sid, delivered=True)

    def term_key(self, sid, key):
        self.calls.append(("term_key", key))
        return ActionResult(True, "", sid, delivered=True)


def _control(adapter: OldLaneSpy):
    from bridge.fleet.control import FleetControl
    from bridge.fleet.scan import Fleet

    return FleetControl(Fleet({"claude": adapter}, timeout=1.0))


def _kinds(spy: LaneSpy) -> list:
    return [c[0] for c in spy.calls]


def test_plain_message_never_touches_the_terminal_lane():
    """🔴 普通一句话：终端那条路**连问都不问**，直接 relay。

    问一下也要花一次 ps —— 但更要紧的是行为：这条路对普通话永远不该被考虑。
    """
    spy = LaneSpy(term_ok=True)  # 故意让它「能发」：真被问到就会走错
    res = _control(spy).send("claude", SID, "把测试跑绿", relay=True)
    assert res.ok and res.text == "relay 回复"
    assert "term_send" not in _kinds(spy) and "can_term_send" not in _kinds(spy)


def test_slash_command_goes_to_the_terminal_lane_first():
    """阳性对照：斜杠命令 + 这条路能办 ⇒ 打进窗口，relay **不许**被叫到。"""
    spy = LaneSpy(term_ok=True)
    res = _control(spy).send("claude", SID, "/goal 看看天气", relay=True)
    assert res.ok and res.delivered
    assert _kinds(spy) == ["can_term_send", "term_send"]


def test_slash_command_falls_back_to_relay_on_an_empty_why():
    """🔴 `why` 空串 ⇒ 退回 relay，用户照旧拿到 relay 那句说清为什么的话。

    这是桩期的实际路径（`WIRED=False` 一律回空串）—— 也就是说**发版后行为和今天一样**。
    """
    spy = LaneSpy(term_ok=False, term_why="")
    res = _control(spy).send("claude", SID, "/goal 看看天气", relay=True)
    assert res.ok and res.text == "relay 回复"
    assert _kinds(spy) == ["can_term_send", "can_relay", "relay"]


def test_slash_command_reports_a_real_obstacle_without_calling_relay():
    """`why` 有内容 = 真做不到（比如那种终端插不上手）⇒ 照实说，不再去试 relay。

    去试 relay 的话，用户拿到的是「斜杠命令送不进开着的会话」那句 —— 对的是上一条路，
    而这里真正的原因是他那个终端我们插不上手。
    """
    spy = LaneSpy(term_ok=False, term_why=ts.WHY_TERM_NO_API.format(label="tmux"))
    res = _control(spy).send("claude", SID, "/goal 看看天气", relay=True)
    assert res.ok is False and "tmux" in res.text
    assert "relay" not in _kinds(spy)


def test_adapter_without_the_lane_behaves_exactly_as_before():
    """🔴 adapter 上没有这三个方法（老 adapter、测试里的假货）⇒ 原样走今天的两条路。

    `control` 全靠 `getattr` 找它们，少一个都不许炸。
    """
    spy = OldLaneSpy(term_ok=True)
    for name in ("can_term_send", "term_send", "term_key"):
        assert not hasattr(spy, name), f"台架失效：{name} 还在，这条测试恒绿"

    res = _control(spy).send("claude", SID, "/goal 看看天气", relay=True)
    assert res.ok and res.text == "relay 回复"
    assert "term_send" not in _kinds(spy)
    out = _control(spy).stop("claude", SID, relay=True)
    assert out.ok is False and "Esc" in out.text


def test_stop_presses_esc_and_only_then_falls_back():
    """`/stop`：能按 Esc 就按（按的是 esc，不是别的键）；按不了才退回那句「停不了」。"""
    spy = LaneSpy(term_ok=True)
    res = _control(spy).stop("claude", SID, relay=True)
    assert res.ok
    assert ("term_key", ts.KEY_ESC) in spy.calls
    assert "relay_stop" not in _kinds(spy)

    quiet = LaneSpy(term_ok=False, term_why="")
    out = _control(quiet).stop("claude", SID, relay=True)
    assert out.ok is False and "Esc" in out.text
    assert _kinds(quiet) == ["can_term_send", "relay_stop"]


def test_a_successful_esc_comes_back_with_words():
    """🔴 按成了也得**带一句话**。

    `term_key` 成功时 `text` 是空串（它只管送、不编文案），而出口那边拼的是
    `("⏹ " if ok else "") + text` ⇒ 原样返回的话用户只看到一个光秃秃的「⏹」。
    这条路**没有回执**（没人回报「我停了」），所以那句话只敢说「按下去了」，
    不许号称「已经停住了」—— 那一轮可能正好在收尾。
    """
    from bridge.fleet.control import WHY_STOPPED_BY_ESC

    res = _control(LaneSpy(term_ok=True)).stop("claude", SID, relay=True)
    assert res.ok and res.text.strip(), "按成了却什么都不说 ⇒ 用户只看到一个「⏹」"
    assert res.text == WHY_STOPPED_BY_ESC
    assert "Esc" in res.text, "没说清替他做了什么"
    assert "已经停" not in res.text and "停住了" not in res.text, \
        "号称一定停住了 —— 这条路没有回执，不能这么说"


def test_stop_asks_with_for_key_so_busy_does_not_block_it():
    """🔴 查的时候必须带 `for_key=True`：要停的本来就是正在跑的那一轮。

    不带的话 `can_send` 的「它正忙着」会把 Esc 挡掉 —— 这条路对 `/stop` 就白加了。
    """
    spy = LaneSpy(term_ok=True)
    _control(spy).stop("claude", SID, relay=True)
    assert ("can_term_send", True) in spy.calls, "查 Esc 没带 for_key ⇒ 会被「它正忙」挡住"


def test_adapter_exposes_the_lane_with_the_relay_shaped_api():
    """接口形状定住：`can_term_send` / `term_send` / `term_key` 三件，和 relay 那三件对齐。

    `control` 全靠 `getattr` 找它们 —— 改名字就是静默失效（这条路悄悄不走了，没人会红）。
    """
    from bridge.fleet.claude import ClaudeAdapter

    for name in ("can_term_send", "term_send", "term_key"):
        assert callable(getattr(ClaudeAdapter, name, None)), f"adapter 上少了 {name}"


def test_termsend_does_not_write_its_own_liveness_check():
    """🔴 判活只能有**一份实现**（adapter 那份）。

    这里再写一套 ⇒ 两份会漂，而漂的后果是往一个已经关掉的窗口打字 / 或者挡住一个好会话。
    和 `relay.Relay` 同一条规矩（它的 docstring 里写明了「这里不另写一套判活逻辑」）。
    """
    src = _src_of(ts)
    assert "PS_ARGV" not in src and "ProcTable.snapshot" not in src, \
        "termsend 自己跑了一次 ps ⇒ 判活有了第二份实现"
    assert "self.adapter._registry()" in src and "table.same_process(" in src, \
        "没用 adapter 那份注册表 / 判活"


# ---------- 文案闸门 ----------


def _lane_copy() -> dict:
    """这条路里**所有**给用户看的话。

    🔴 判据不能只按 `WHY_*` 这个名字收（第一版就是）：角色 1 11-23 加的
    `_BLOCKED_MARKS`（终端主动拦住时那两句）叫别的名字，于是整整两句上屏的话
    **漏在闸外** —— 是他自己提醒我才发现的，而他第一版 `WHY_BUSY` 超长正是这道闸抓到的，
    也就是说漏掉的那两句没人查。
    按名字收的闸永远落后于下一个人起的名字 ⇒ 改成**按形状**收：
    模块里任何含汉字的字符串常量都算（含元组 / 字典里嵌的），这条路的文案全是中文。
    """
    out = {}
    for name in dir(ts):
        if name.startswith("__"):
            continue
        value = getattr(ts, name)
        if isinstance(value, str):
            if _has_cjk(value):
                out[name] = value
        elif isinstance(value, (tuple, list)):
            for i, item in enumerate(value):
                # `_BLOCKED_MARKS` 是 ((特征串, 给用户的话), …) 这种形状
                parts = item if isinstance(item, (tuple, list)) else (item,)
                for j, part in enumerate(parts):
                    if isinstance(part, str) and _has_cjk(part):
                        out[f"{name}[{i}][{j}]"] = part
    return out


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def test_copy_never_claims_we_received_or_will_follow_up():
    """🔴 主人明确禁过的话术：屏幕上不许出现「我们收到你的信息」「会跟进」这一类。

    （feedback_installer_no_auto_report_wording_and_admin_who：「不用截图，我们这边会跟进」
    「我们这边已经收到了」「会把情况发回来」一律删。）
    """
    banned = ("我们收到", "我们这边", "已经收到", "会跟进", "跟进", "已反馈", "自动上报",
              "发给管理员", "不用截图")
    for name, text in _lane_copy().items():
        for word in banned:
            assert word not in text, f"{name} 里有被禁的话术「{word}」：{text}"


def test_copy_never_leaks_internal_concepts():
    """不许露内部概念：窗口编号、连接路径、进程、命令名、下划线标识符。

    判据和 `tests/test_copy_quality.py` 的「内部名词」一组对齐（那张闸门扫整个 bridge/，
    这里再按这条路特有的词查一遍：surface / socket / pid 那些只有这条路会碰）。
    """
    import re

    banned = ("surface", "socket", "sock", "pid", "argv", "subprocess", "session id",
              "sessionId", "/tmp/", "stderr", "exit code", "退出码")
    for name, text in _lane_copy().items():
        low = text.lower()
        for word in banned:
            assert word.lower() not in low, f"{name} 露了内部概念「{word}」：{text}"
        assert not re.search(r"(?<![\w‹{])_[A-Za-z]\w*", text), f"{name} 里有下划线标识符：{text}"
        assert "/Users/" not in text and "~/." not in text, f"{name} 里有绝对路径：{text}"


def test_copy_is_one_plain_sentence_with_a_next_step():
    """每句话都要有下一步（主人的规矩：一句大白话 + 下一步该干什么）。

    「还没接上」那句例外：它压根到不了用户眼前（`can_send` 在桩期一律退回别的路），
    真被看到也没有「他该做什么」—— 是我们这边没接完。
    """
    # 🔴 这张表是「下一步」的**说法**清单，不是语义判断 —— 它只能抓「整句没给出路」这一种毛病。
    # 11-23 它误报过一次：角色 1 的新 `WHY_BUSY` 明明给了两条出路（等它干完再发一次 / 点「停下」），
    # 但一个说法都不在表里 ⇒ 判成「没给下一步」。表要跟着真文案长，别反过来逼文案迁就表。
    nexts = ("到电脑", "在电脑上", "那个窗口", "先在电脑上", "先不", "再说", "这次没",
             "再发一次", "等它", "点「", "删掉", "先把")
    for name, text in _lane_copy().items():
        assert text.strip(), name
        assert len(text) <= 120, f"{name} 太长了（{len(text)} 字），手机上看不完：{text}"
        if name in ("WHY_BAD_ID", "WHY_EMPTY", "WHY_BAD_KEY", "WHY_NOT_WIRED"):
            continue  # 这几条是「输入不对 / 这条路还没接上」，没有「他该做什么」
        assert any(hint in text for hint in nexts), f"{name} 没给下一步：{text}"


def test_terminal_specific_copy_names_the_terminal():
    """按终端分情况说的那几句，要把那个终端的名字填进去（「这个会话开在 cmux 里…」）。"""
    for template in (ts.WHY_TERM_NO_API, ts.WHY_NO_CLI, ts.WHY_UNREACHABLE, ts.WHY_NO_WINDOW):
        assert "{label}" in template, template
        assert "cmux" in template.format(label="cmux")
        assert "tmux" in template.format(label="tmux")


def test_unknown_terminal_copy_does_not_invent_a_name():
    """🔴 认不出终端时**不许编一个名字**，整句换成「看不出开在哪种窗口里」。

    「这个会话开在 里」这种空洞是第一版的形状（`.format(label="")`）。
    """
    for template in (ts.WHY_TERM_NO_API, ts.WHY_NO_CLI, ts.WHY_UNREACHABLE, ts.WHY_NO_WINDOW):
        assert ts._fill(template, ts.TERM_UNKNOWN) == ts.WHY_TERM_UNKNOWN
        assert ts._fill(template, "一种没见过的终端") == ts.WHY_TERM_UNKNOWN
    # 阳性对照：认得出的照旧填名字
    assert "cmux" in ts._fill(ts.WHY_NO_CLI, ts.TERM_CMUX)


def test_no_dead_copy_constants():
    """🔴 每句上屏的话都要真有地方用到它。

    本轮第一版写了 4 句用不上的（`WHY_NOT_TERMINAL` / `WHY_NOT_LIVE` / `WHY_PS_FAILED` 等）
    —— 那几条路径按设计都是**退回别的路**（`why=""`），这几句永远到不了用户眼前。
    留着的代价不是多几行：下次有人改文案时会去改它们，以为改的是用户看到的话。

    只查**顶层常量**：`_BLOCKED_MARKS` 那种嵌在元组里的（`_lane_copy` 给的名字带 `[0][1]`）
    本身就是数据，整个表有人用就够了，不该要求每个元素名出现在代码里。
    """
    code = _code_strings_and_names(ts)
    dead = [name for name in _lane_copy() if "[" not in name and name not in code]
    assert not dead, f"这几句没有任何地方用到，删掉：{dead}"
    # 嵌在表里的那几句，整张表得有人用（否则它们也是死的）
    for name in _lane_copy():
        if "[" in name:
            assert name.split("[", 1)[0] in code, f"{name} 所在的那张表没人用"
    # 阳性对照：尺子真能分辨（故意查一个不存在的名字，它必须算「死的」）
    assert "WHY_DEFINITELY_NOT_THERE" not in code


def test_every_known_terminal_has_a_human_name():
    """`_TERM_MARKS` 里每一种都要有个给人看的叫法，否则文案会填出一个空洞。"""
    for kind, _marks, _exes in ts._TERM_MARKS:
        assert ts.TERM_LABELS.get(kind), f"{kind} 没有给人看的名字"
    assert ts.TERM_UNKNOWN not in ts.TERM_LABELS, "unknown 不该有名字（它走 WHY_TERM_UNKNOWN）"
