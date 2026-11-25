"""自动更新：发布了新版本，同事那台自己装上。

## 为什么要有

同事（包括领导）用的是飞书文档里的安装包。修一个毛病要他重新下载、重新双击 ——
2026-11-06 一天里让同一个人重装了 3 次，每次都要讲一遍怎么做。他遇到的问题里有
一半在我这边已经修好了，只是没到他机器上。

## 怎么做

- 更新源：`https://chat.auto-link.com.cn/ccdl/feishu-agent/latest.json`（公网可读、不要登录，
  2026-11-06 实测 200）。发布脚本 `feishu-agent-oneclick/publish-update.sh` 往那儿传。
- 每 `CHECK_EVERY_SEC` 查一次 `latest.json` 的 `build_id`，和本机 `bridge/buildinfo.py` 的不一样就更新。
- 下载安装包 → **核对 sha256** → 在空闲时跑它 → 安装器自己会重启服务。

## 红线

- 🔴 **只认 HTTPS + 固定域名**（`_ALLOWED_HOST`）。这个脚本会在用户机器上以他的身份执行，
  下载地址被换掉 = 远程执行任意代码。域名写死在代码里，不从配置、不从环境变量读。
- 🔴 **sha256 必须对**。对不上就不跑、不删旧的、下次再来。半个文件（传输中断）的形状和
  被换掉的包在这一层是同一种处理。
- 🔴 **正在干活时不装**。安装器会 `launchctl bootout` 把自己停掉 ⇒ 腰斩用户那一轮，
  他看到的是「发了消息，再也没有回复」。所以要 `is_idle()` 为真才动手。
- 🔴 **只在同一台机器上降级要拦住**：`build_id` 不一样就更新，不比大小 —— 回滚时服务端
  挂回旧包，同事那边要跟着回滚。但**同一个 build_id 装过一次就不再装**（`_state`），
  不然装失败会变成每 10 分钟重装一次。
- 第 30~37 轮 Windows 只提醒不装（`manual_only`）。第 38 轮（主人原话「windows 你要搞定，让他们能够自动更新」）
  起 Windows 和 Mac 走同一条路：下载 → 核 sha256 → 空闲时装 → 安装器自己换新码、拉起、60 秒没连上就换回。
  Windows 怎么跑安装器、为什么这么跑，见 `run_installer` 的 Windows 分支和 `_windows_installer_argv`。
  ⚠️ 这条路**没在真 Windows 上跑过**（只在 Mac 的 pwsh 7 台架上跑过安装器那几段）。

## Windows 自动更新（第 38 轮）

- 不能直接跑 `.bat`：bat 头末尾是 `pause`（`build.sh` HEAD），后台跑会挂在那儿等回车直到超时。
  照 bat 头自己的做法：powershell 读出 `##CARHER-PS1` 之后那段，当 scriptblock 跑（`_PS_BOOT`）。
- 只给本进程 `-ExecutionPolicy Bypass`，不改系统执行策略；`-NonInteractive` ⇒ 万一走到 `Read-Host` 立刻失败，
  不挂住；不提权（安装器全程只写 `%LOCALAPPDATA%` 和当前用户自己的计划任务 / HKCU）。
- 不弹窗：`CREATE_NO_WINDOW`。🔴 不能用 `DETACHED_PROCESS`：没有控制台的 powershell 再起 python / pip
  这些控制台程序时，**每个都会新开一个看得见的黑窗口**。
- 输出写 `logs/selfupdate-install.log`，不接管道（Mac 11-09 SIGPIPE 那次，见 skill fleet-ops-release 第三节）。
- 安装器在自动更新时（`CARHER_AUTO_UPDATE=1`）**只结束桥的 python**，不动外面那层 `run-agent.ps1` 循环，
  循环 5 秒后用新码把它拉起来（`install-windows.ps1` 的 `Invoke-CarherSoftRestart`）。所以安装器是桥的子进程也没关系：
  Windows 上结束父进程不会带走子进程。再加 `CREATE_BREAKAWAY_FROM_JOB` 尽量脱离计划任务的作业对象
  （作业不许脱离时 CreateProcess 报拒绝访问 ⇒ 去掉这一位再起一次）。
- 老版本（11-09 ~ 11-16 那几版）的 updater 在 Windows 上**代码里**就返回 `manual_only`，latest.json 里写什么都改不了它
  ⇒ 那几台要重新双击一次新安装包（或在 PowerShell 里跑一行命令），之后才走这条自动路。

## 自动上报（第 30 轮，用户原话「不可能靠人来报」）

查 latest.json 时带 5 个查询参数，**一个都不许多**（隐私红线）：
`v`=本机版本号、`id`=本机首次生成的 16 位随机十六进制（`install_id`，不从主机名/MAC/用户名推导）、
`os`=mac|windows、`u`=**最近一次尝试**那条更新记录的结果一个词（ok/fail/installing/not_applied/空，
绝不带 detail）、`app`=这台用的飞书 app_id（`cli_…`，不是凭据；app_secret 绝不发）。服务端 nginx 日志记完整 URI，
从那里就能数出全公司每台在哪个版本。

## 没装上要说出来（第 32 轮，照 T3 `updateMachine.ts:220-226`）

原来 `installing` 记录只按目标版本做键、不记从哪版来 ⇒ 安装器中途死了 / 新版起不来被退回，
重启后还是旧版 ⇒ 6 小时后又自动重装同一版，fleet 表上是「落后 + u=ok/空」，看不出卡住了。

- 开装前记 `{目标: installing, from: 当前}`。
- 启动后查的时候：`installing` 且 `from == 当前` 且 `目标 != 当前` ⇒ 这一版**没装上**，改记 `not_applied`。
  安装器自己退回旧版时记 `rolled_back`（`install-mac.sh` 第 6 步），同样当 `not_applied`。
- `not_applied` 的那一版**自动不再装**，只有他手动发「更新」才装；主动告诉主人一句（`pending_notice`）。
- `u` 取最近一次尝试的结果 ⇒ fleet 表上这台显示 `not_applied`，不再是看不出来的「落后」。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import subprocess
import time
import tempfile
import re
import secrets
import urllib.parse
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger("feishu-bridge.selfupdate")

# 🔴 写死：见文件头红线。改这两行等于改「谁能在同事机器上跑代码」。
_ALLOWED_HOST = "chat.auto-link.com.cn"
BASE_URL = f"https://{_ALLOWED_HOST}/ccdl/feishu-agent"

CHECK_EVERY_SEC = 10 * 60
# 启动后先等这么久查第一次（第 30 轮：原来先睡满 10 分钟 ⇒ 刚装完 / 刚重启的那台 10 分钟里谁都看不见它）
FIRST_CHECK_AFTER_SEC = 60
# 下载超时：安装包约 1.1MB，公司网最慢也就几秒；给足余量但不能挂住线程
_FETCH_TIMEOUT = 120
_MAX_BYTES = 8 * 1024 * 1024  # 包就 1.1MB；再大一定是出了别的事，别往磁盘上灌
# 装失败的同一个版本，隔这么久才再试一次
_RETRY_AFTER_SEC = 6 * 60 * 60

WINDOWS_UNVERIFIED = "Windows 上的自动更新还没在真机上跑过"   # 第 38 轮起不再抛；留着给老测试 import

# ---------- Windows 怎么跑安装器（见文件头「Windows 自动更新」） ----------

# 照 bat 头（build.sh HEAD）那一行：读自己 → 找 `##CARHER-PS1` → 后面整段当 scriptblock 跑。
# 退出码：安装器 `Die` 是 `exit 1`、没接住的错误 trap 里 break ⇒ 这里 catch 成 1；正常跑完 ⇒ 0。
# 🔴 不能写 `exit $LASTEXITCODE`：那是安装器里**最后一个原生命令**的退出码（比如探测 python 的那一下），和装没装上无关。
# 🔴 这串里不许出现 `bridge.app` / `run-agent.ps1`：安装器按命令行认旧进程去结束，认错了会把自己结束掉。
_PS_BOOT = (
    "$c=[IO.File]::ReadAllText($env:CARHER_SELF,[Text.Encoding]::UTF8);"
    "$i=$c.IndexOf('#'+'#CARHER-PS1');if($i -lt 0){exit 9};"
    "try{& ([scriptblock]::Create($c.Substring($i)))}catch{exit 1};exit 0"
)
# Windows 的 CreateProcess 标志（subprocess 只在 Windows 上导出这些常量 ⇒ 写数值，Mac 上的测试也能 import）
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000
_DETACHED_PROCESS = 0x00000008   # 🔴 不用（见文件头）；列在这里给测试断言「没用它」
# Windows 安装器开头抢不到 `Local\CarHerFeishuAgent.install` 这把锁（另一个安装窗口在跑，多半是他正手动重装）时退出 3：
# 不算装失败（不记 fail、不进 6 小时冷却），下一拍再试（install-windows.ps1 的 INSTALL-LOCK 段）
WIN_INSTALLER_BUSY_RC = 3


# 🔴 11-20：自动更新装失败时，**退出码必须进得了上报**。
# 现场：Windows 4 台（…<机器> / …<机器> / …<机器> / …<机器>）连着几天同一个指纹
# f31f81c6b682「安装包退出码 %s build=%s」@ selfupdate.py:636，而 errreport 只发
# **源码常量模板**（`record.msg`，不是格式化后的），所以 `%s` 的实参一个都没传回来
# ⇒ 退出码是几、安装器说了什么，查不到，只能猜。
# 上报那条通道（report_params）只许 5 个字段、`u` 只能是一个词、detail 明确不发，
# 所以也不能从那儿带。
# 修法：把退出码**编进常量模板本身**（一句一个码），这样上报拿到的就是带码的那句话。
# 码是我们自己的程序的退出码，不含路径、用户名、机器信息 ⇒ 不违反「只发常量」。
# 新增退出码记得加一行；没列到的落到 _RC_OTHER（至少能看出是「别的码」而不是没数据）。
_INSTALLER_RC_LOGS = {
    1: "安装包退出码=1（安装器自己报失败）",
    2: "安装包退出码=2（参数不对 / 用法错）",
    3: "安装包退出码=3（另一个安装器在跑）",
    4: "安装包退出码=4（下载或校验没过）",
    5: "安装包退出码=5（装完了但有警告）",
    9: "安装包退出码=9（没装上，环境缺东西）",
    -1: "安装包退出码=-1（被信号杀掉 / 没跑起来）",
    -9: "安装包退出码=-9（被 SIGKILL 杀掉）",
    -15: "安装包退出码=-15（被 SIGTERM 杀掉）",
}

# 🔴 11-20 第二轮：原来「没列到的码」只有一句**不带数字**的常量
# （`安装包退出码=其他（没列到的码）`）⇒ 落到这个桶等于「知道是别的码」但仍然不知道是几，
# eba1ced9 想达成的「退出码必须进得了上报」在这条路上**没达成**（「一键安装」会话指出，
# 当时 3 台 Windows 正卡在这个桶里）。
#
# 🔴 它建议的 `f"安装包退出码=其他({rc})"` **行不通，我实测过**：errreport 要求模板
# **整句**出现在源码常量集合里（`item_from_record` 那段红字），f-string 拼出来的不在
# ⇒ 整条被替换成 `<动态消息>`，连「其他」都看不到，比现在更糟。
# 🔴 我的第一版同病：用字典推导 `{rc: f"…{rc}…" for rc in range(256)}` 也**不是**源码字面量
# ⇒ 实测同样变 `<动态消息>`。能上报的只有**真写在这个文件里**的句子。
# ⇒ 所以下面是手写的。不铺 0~255（过度设计）：安装器实际只发 0/1/3（`install-windows.ps1`
# 的 `exit 1`@:109/:2341、`exit 3`@:138），`_PS_BOOT` 那层 0/1/9；
# 另外几个是 Python `returncode` 的常见形状（被信号杀：负值；127=命令找不到；137=SIGKILL）。
# 真的落到 `_INSTALLER_RC_OTHER` 时至少知道「码不在这张表里」，再按需加一行。
_INSTALLER_RC_EXTRA = {
    0: "安装包退出码=0（没单独归类的码）",
    6: "安装包退出码=6（没单独归类的码）",
    7: "安装包退出码=7（没单独归类的码）",
    8: "安装包退出码=8（没单独归类的码）",
    10: "安装包退出码=10（没单独归类的码）",
    126: "安装包退出码=126（找到了但起不来 / 没有执行权限）",
    127: "安装包退出码=127（命令找不到）",
    130: "安装包退出码=130（被 Ctrl-C 中断）",
    137: "安装包退出码=137（被强杀 SIGKILL，常见于杀毒软件或内存不足）",
    143: "安装包退出码=143（被 SIGTERM 终止）",
    255: "安装包退出码=255（退出码溢出 / -1 的无符号形式）",
    -2: "安装包退出码=-2（被 SIGINT 杀掉）",
    -11: "安装包退出码=-11（段错误 SIGSEGV）",
    0x80073D28: "安装包退出码=0x80073D28（MSIX 要管理员权限）",
    # 🔴 Windows **进程层**的码（11-20「一键安装」会话实测我上一版 7/7 全落兜底）。
    # 为什么是这组：已知那三台的码不在 {1,2,3,4,5,9,…} 里，而安装器正文只发 1/3、
    # `_PS_BOOT` 只发 0/1/9 ⇒ 码不是我们任何一层脚本给的 ⇒ 只能是 powershell.exe
    # 没起来 / 起来就崩 / 被外部杀掉，那就是下面这些 NTSTATUS。
    # Python 的 returncode 在 Windows 上可能是有符号也可能是无符号，两种形式都列。
    -1073741819: "安装包退出码=0xC0000005（访问冲突，进程崩了）",
    3221225477: "安装包退出码=0xC0000005（访问冲突，进程崩了；无符号形式）",
    -1073741510: "安装包退出码=0xC000013A（进程被终止，常见于被外部杀掉）",
    3221225786: "安装包退出码=0xC000013A（进程被终止；无符号形式）",
    -1073740791: "安装包退出码=0xC0000409（栈缓冲区溢出）",
    3221226505: "安装包退出码=0xC0000409（栈缓冲区溢出；无符号形式）",
    -1073741502: "安装包退出码=0xC0000142（DLL 初始化失败，起不来）",
    3221225794: "安装包退出码=0xC0000142（DLL 初始化失败；无符号形式）",
    -1073741515: "安装包退出码=0xC0000135（缺 DLL，起不来）",
    3221225781: "安装包退出码=0xC0000135（缺 DLL；无符号形式）",
    4294967295: "安装包退出码=0xFFFFFFFF（-1 的无符号形式，多半没起来）",
}
_INSTALLER_RC_OTHER = "安装包退出码=这张表里没有的码（看 emsg 里的数字）"
# 安装器**抛异常**那条路（超时 / 起不来）用的常量。必须是整句写死的字面量：
# `errreport` 只发 `record.msg`，f-string 拼出来的会变 `<动态消息>`（11-21 实测过两次）。
_INSTALLER_DIED_WITH_EXC = "安装器抛异常退出（跑了多少秒看 emsg 的 sec=）"


class _RcOnly(Exception):
    """只用来把退出码塞进上报的 `emsg`。

    `errreport` 对**模板**（`record.msg`）要求「整句是源码常量」，所以带数字的句子
    只能手写、列不全；但 `emsg`（异常消息）走的是 `_scrub` 脱敏那条路，没有这个限制。
    ⇒ 挂一个只含 `rc=<数字>` 的异常，任何退出码都能完整传回来，
    不用再追着往码表里加（11-21）。
    🔴 这个异常**只装退出码**，一个字别多放：它会原样（脱敏后）发到 198。
    """


def installer_rc_log(rc: object) -> str:
    """退出码 ⇒ 一句**源码常量**（上报只认整句在源码里的模板）。

    先查有解释的那张表，再查「全码表」拿到带数字的那句，都不中（rc 不是数）才兜底。
    """
    # 🔴 只认真正的整数：`int(1.5)` = 1 ⇒ 会把一个莫名的浮点报成「安装器自己报失败」，
    # 那比「读不出来」更糟（给出一个错的归因）。bool 是 int 的子类，也排掉。
    if isinstance(rc, bool) or not isinstance(rc, int):
        try:
            code = int(str(rc).strip())      # 允许 "9" 这种字符串形式
        except (TypeError, ValueError):
            return _INSTALLER_RC_OTHER
    else:
        code = rc
    known = _INSTALLER_RC_LOGS.get(code)
    if known:
        return known
    return _INSTALLER_RC_EXTRA.get(code, _INSTALLER_RC_OTHER)


def _windows_powershell() -> str:
    """系统自带的 Windows PowerShell 5.1（和安装器 `$psExe` 同一个），找不到退回 PATH 上的。"""
    root = os.environ.get("SystemRoot") or os.environ.get("SYSTEMROOT") or ""
    if root:
        exe = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        if os.path.exists(exe):
            return exe
    return "powershell.exe"


def _windows_installer_argv() -> list[str]:
    return [_windows_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-WindowStyle", "Hidden", "-Command", _PS_BOOT]

# ---------- 上报（见文件头「自动上报」） ----------

# 🔴 只许这 5 个键。加一个 = 越过隐私红线（tests/test_r30_selfupdate.py 钉着）
REPORT_KEYS = ("v", "id", "os", "u", "app")
_REPORT_MAX_BYTES = 300
_INSTALL_ID_FILE = "install_id"
_INSTALL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
# app_id 的形状。不像就不发 —— 防的是哪天有人把 app_secret 传进来（secret 没有 `cli_` 前缀）
_APP_ID_RE = re.compile(r"^cli_[A-Za-z0-9]{1,40}$")
# 🔴 新增一个词要同步：`fleet-report.py` 的 FIELD_RE["u"]、brief 里的允许词表（隐私红线）
# 第 34 轮加 crashloop：**只由启动守卫**（安装器写的 launch-guard.sh，纯 bash）发 —— 桥一启动就崩、
# 连续 5 次活不过 60 秒时报一次。桥自己不写这个词；列在这里是为了词表只有一张（和 fleet-report 同步）。
# 🔴 加词要同步三处：这里 + `fleet-report.py` 的 `FIELD_RE["u"]` 和 `_U_LABEL` + `launch_guard.sh`。
# 🔴 `rolled_back` **故意不在这里**：它只是磁盘上的中间状态，`_reconcile()` 在下次启动时
# 把它转成 `not_applied` 再上报（见那个函数里 `outcome == ROLLED_BACK` 那支）。
# 11-20 我一度以为这是「漏了同步」并擅自加进来 ⇒ `test_r38_win_selfupdate.py::
# test_u_words_match_fleet_report` 直接拦住（它写着「rolled_back 不上报」）。已撤回。
# 判「这个词该不该上报」先看 `_reconcile` 会不会把它转掉，别只看「state 里出现过」。
_OUTCOME_WORDS = frozenset({"ok", "fail", "installing", "not_applied", "crashloop"})
# 安装器退回旧版时写的词（`install-mac.sh` UPDATE-ROLLBACK 段）。不上报：启动后转成 not_applied
ROLLED_BACK = "rolled_back"
NOT_APPLIED = "not_applied"
# 版本号只留这些字符、最多这么长（`buildinfo` 本来就是 `日期-commit-dirty-指纹`）
_SAFE_VALUE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _query(params: Optional[dict]) -> str:
    """拼查询串：只认 `REPORT_KEYS`、值全部 URL 编码、总长不超过 `_REPORT_MAX_BYTES`。"""
    if not params:
        return ""
    pairs = [(key, str(params[key])) for key in REPORT_KEYS if key in params and params[key] is not None]
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    if len(query.encode("utf-8")) > _REPORT_MAX_BYTES:
        # 正常值加起来不到 150 字节；超了 = 哪个值形状不对 ⇒ 这次不上报，查新版照常
        logger.info("上报参数超长，这次不带")
        return ""
    return query


def _at(rec) -> float:
    """记录里的时间戳；坏记录（不是 dict / at 不是数）当 0，别让一条脏记录把整个判断带崩。"""
    try:
        return float(rec.get("at") or 0) if isinstance(rec, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _url_ok(url: str) -> bool:
    return url.startswith(f"https://{_ALLOWED_HOST}/")


def _fetch(url: str, *, opener=None) -> bytes:
    """只走 HTTPS + 写死的域名。返回原始字节；超过 _MAX_BYTES 直接当失败。"""
    if not _url_ok(url):
        raise ValueError(f"更新地址不在允许范围内：{url}")
    open_fn = opener or urllib.request.urlopen
    with open_fn(url, timeout=_FETCH_TIMEOUT) as resp:  # noqa: S310 —— 地址已过白名单
        data = resp.read(_MAX_BYTES + 1)
    if len(data) > _MAX_BYTES:
        raise ValueError("更新包比预期大得多，不收")
    return data


def latest(*, opener=None, params: Optional[dict] = None) -> Optional[dict]:
    """读服务端的 latest.json。读不到 / 格式不对 ⇒ None（当没有新版，不报错给用户）。

    `params`：上报字段（见文件头），只认 `REPORT_KEYS`；不给就是不上报的裸请求。
    """
    query = _query(params)
    url = f"{BASE_URL}/latest.json" + (f"?{query}" if query else "")
    try:
        data = json.loads(_fetch(url, opener=opener).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 网络/格式问题不该把桥搞崩
        logger.info("查新版本没成功（当没有新版）：%s", exc)
        return None
    if not isinstance(data, dict) or not isinstance(data.get("build_id"), str):
        logger.info("latest.json 格式不对，当没有新版")
        return None
    return data


def installer_key() -> str:
    """这台机器要哪个包。"""
    return "windows" if platform.system() == "Windows" else "mac"


class SelfUpdater:
    """查新版 → 下载 → 核对 → 空闲时装上。

    `is_idle`：现在能不能装（有活在跑就不装，见文件头红线）。
    `state_path`：记「哪个版本装过 / 失败过」，重启也在 —— 不然装失败会每 10 分钟重来。
    `run`：真正执行安装包的函数，测试里换掉。
    """

    def __init__(
        self,
        package_dir: str,
        state_path: str,
        *,
        is_idle: Callable[[], bool],
        opener=None,
        runner: Optional[Callable[[str], subprocess.CompletedProcess]] = None,
        now: Callable[[], float] = time.time,
        app_id: str = "",
    ) -> None:
        self.package_dir = package_dir
        # 这台用的飞书 app_id（只拿来上报对上人）。由桥传进来，这里不读 config.toml
        self.app_id = app_id
        self._install_id = ""
        self.state_path = state_path
        self.is_idle = is_idle
        # 🔴 第 25 轮（P-sre，本机 11-08 00:12 真发生过）：「空闲」只在开装前查一次，安装器从开跑到
        # 换掉进程约 13 秒，这期间进来的新一轮会被半路杀掉、没有任何提示。装的时候把这个置上，
        # 桥看到它就先不开新轮（回一句「正在装新版」）。
        self.installing = False
        # 上一次跑安装器花了多少秒。装失败时跟着上报：45 秒 vs 6 秒能分出「不是超时」
        # （见 `_run_installer_windows` 里那段红字）。
        self._last_installer_sec = -1
        self._opener = opener
        self._runner = runner
        self._now = now
        self.last_error = ""

    # ---------- 本机版本 ----------

    def current_build(self) -> str:
        from .status import build_id

        return build_id(self.package_dir)

    # ---------- 上报 ----------

    def install_id(self) -> str:
        """本机的随机编号；读不到 / 写不进 ⇒ 空串（这次不带 id，绝不让更新因为它失败）。

        存在 `package_dir` 的上一级（`~/.carher-feishu-agent/install_id`，和 `update_state.json` 同目录）：
        重装会覆盖 `bridge/`，不会动这个文件 ⇒ 同一台机器编号不变。
        """
        if self._install_id:
            return self._install_id
        path = os.path.join(os.path.dirname(os.path.abspath(self.package_dir)), _INSTALL_ID_FILE)
        try:
            with open(path, encoding="utf-8") as handle:
                value = handle.read(64).strip()
            if _INSTALL_ID_RE.match(value):
                self._install_id = value
                return value
        except OSError:
            pass
        value = secrets.token_hex(8)
        tmp = f"{path}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, value.encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, path)
        except OSError:
            logger.info("本机编号写不进去，这次上报不带 id")
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return ""
        self._install_id = value
        return value

    def report_params(self, current: str) -> dict:
        """查新版时顺带上报的 5 个字段（文件头「自动上报」）。任何一项拿不到就留空 / 不带，不抛。"""
        params = {"v": _SAFE_VALUE_RE.sub("", current or "")[:60]}
        try:
            install_id = self.install_id()
        except Exception:  # noqa: BLE001 —— 编号是锦上添花
            install_id = ""
        if install_id:
            params["id"] = install_id
        params["os"] = installer_key()
        # 🔴 第 32 轮：取**最近一次尝试**的结果，不取当前版本那条 —— 没装上的时候「当前版本那条」
        # 是旧版以前装成功的 ok，fleet 表上就是「落后 + ok」，看不出卡住了。
        _key, rec = self._latest_attempt()
        outcome = rec.get("outcome") if isinstance(rec, dict) else ""
        params["u"] = outcome if outcome in _OUTCOME_WORDS else ""
        app = str(self.app_id or "")
        params["app"] = app if _APP_ID_RE.match(app) else ""
        return params

    def download_url(self, info: dict) -> str:
        """这台机器该下的安装包地址（Windows 让人自己点）。"""
        part = info.get(installer_key()) or {}
        return f"{BASE_URL}/{part.get('file', '')}"

    # ---------- 记账 ----------

    def _state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, build: str, outcome: str, detail: str = "", **extra) -> None:
        state = self._state()
        state[build] = {"outcome": outcome, "at": self._now(), "detail": detail[:200], **extra}
        self._write_state(state)

    def _write_state(self, state: dict) -> None:
        # 只留最近 20 个版本的记录，别让这个文件无限长
        if len(state) > 20:
            for key in sorted(state, key=lambda k: _at(state[k]))[:-20]:
                state.pop(key, None)
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False)
        except OSError:
            logger.warning("写更新记录失败：%s", self.state_path)

    def _latest_attempt(self) -> tuple[str, Optional[dict]]:
        """最近一次尝试的那条记录（按 `at`，同一时刻取后写的）。没有 ⇒ ("", None)。"""
        best_key, best, best_at = "", None, None
        for key, rec in self._state().items():
            if not isinstance(rec, dict):
                continue
            at = _at(rec)
            if best_at is None or at >= best_at:
                best_key, best, best_at = key, rec, at
        return best_key, best

    def _reconcile(self, current: str) -> None:
        """启动后（每次查之前）把「在装」「被退回」这两种悬着的记录落定。

        第 32 轮，照 T3 `updateMachine.ts:220-226`（`fromVersion === appVersion && targetVersion !== appVersion`）：
        - `installing` + 目标就是当前版本 ⇒ 装上了（成功那条路的收尾跑不到，见 update_now）⇒ ok
        - `installing` + 记着的 from 就是当前版本 + 目标不是 ⇒ 安装器没把新版换上来 ⇒ not_applied
        - `rolled_back`（安装器第 6 步新版没连上、自己换回旧版）⇒ not_applied
        - 老记录没有 from ⇒ 判不了，照旧不动（不猜）
        """
        if not current or self.installing:
            # 本进程正在跑安装器（手动「更新」和自动那一拍撞上）⇒ 那条 installing 是活的，别判
            return
        state = self._state()
        changed = False
        for key, rec in state.items():
            if not isinstance(rec, dict):
                continue
            outcome = rec.get("outcome")
            if outcome in ("installing", NOT_APPLIED) and key == current:
                # not_applied 但现在跑的就是它（后来手动装上了 / 盘上版本号晚到）⇒ 其实装上了
                rec["outcome"] = "ok"
                changed = True
                logger.info("已更新到 %s（上一个进程被安装器换掉前没来得及记）", key)
            elif (outcome == "installing" and rec.get("from") == current) or outcome == ROLLED_BACK:
                if key == current:
                    continue            # 退回的就是现在这版？形状不对，不动
                rec["outcome"] = NOT_APPLIED
                rec["told"] = False
                changed = True
                logger.warning("新版 %s 没装上（来自 %s，现在还是 %s）", key, outcome, current)
        if changed:
            self._write_state(state)

    def not_applied(self, build: str) -> bool:
        """这一版上次没装上 ⇒ 自动更新不再装它（只有他手动发「更新」才装）。"""
        rec = self._state().get(build)
        return isinstance(rec, dict) and rec.get("outcome") == NOT_APPLIED

    def take_notice(self) -> Optional[tuple[str, str]]:
        """还没告诉主人的「新版没装上」⇒ (版本号, 一句话)；没有 ⇒ None。发出去后调 `mark_told`。"""
        current = self.current_build()
        for key, rec in sorted(self._state().items(), key=lambda kv: -_at(kv[1])):
            if isinstance(rec, dict) and rec.get("outcome") == NOT_APPLIED and rec.get("told") is False:
                now_on = current or rec.get("from") or "原来那版"
                return key, f"新版 {key} 没装上，还在用 {now_on}。发「更新」再试一次。"
        return None

    def mark_told(self, build: str) -> None:
        state = self._state()
        rec = state.get(build)
        if isinstance(rec, dict):
            rec["told"] = True
            self._write_state(state)

    def _installed_half(self, build: str) -> bool:
        """版本号已经是它、但这一版的安装记的是失败 ⇒ 装到一半（第 25 轮 P-sre 第 5 条）。

        安装器第 3 步就写 buildinfo.py，第 5 步才可能因为连不上飞书退出 ⇒ 盘上是新版号、
        跑的还是旧码、下一拍 `build == current` 永远不再试。用失败记录把这种认出来。
        """
        rec = self._state().get(build)
        return isinstance(rec, dict) and rec.get("outcome") == "fail"

    def _tried_recently(self, build: str, current: str = "") -> bool:
        """这个版本刚试过吗。失败过 ⇒ 隔 `_RETRY_AFTER_SEC` 再试。

        🔴 第 30 轮：原来「成功过 ⇒ 永远不再装」⇒ 服务端回滚到这台**以前装成功过**的旧版，
        这台永远跟不过去。现在 `ok` 只在 `build == current` 时算「不用再装」；
        版本号不是它 ⇒ 只有「刚装成功、而且之后没装过别的」才挡一下（防盘上版本号没跟上时每 10 分钟重装），
        之后装过别的版本（= 现在是回滚）⇒ 照样装。
        """
        state = self._state()
        rec = state.get(build)
        if not isinstance(rec, dict):
            return False
        at = float(rec.get("at") or 0)
        if rec.get("outcome") == "ok":
            if build == current:
                return True
            moved_on = any(isinstance(other, dict) and float(other.get("at") or 0) > at
                           for key, other in state.items() if key != build)
            if moved_on:
                return False
        return self._now() - at < _RETRY_AFTER_SEC

    # ---------- 查 ----------

    def check(self, manual: bool = False) -> Optional[dict]:
        """有新版且该装 ⇒ 返回 latest.json；否则 None。不下载、不安装。

        🔴 第 25 轮（P-sre 复现）：返回 None 时 `last_error` 必须说清是哪一种 —— 原来「更新源连不上」
        「这一版刚装失败在冷却」都留空串 ⇒ 用户发「更新」被回「已经是最新的了」，而服务端早有新版
        （`selfcheck.py` 自己的红线：查不到绝不能说成已是最新）。
        `manual=True`（他亲手发「更新」）⇒ 绕过失败冷却：他就是要现在再试一次。
        """
        self.last_error = ""
        current = self.current_build()
        # 第 32 轮：先把「在装」「被退回」落定，上报的 `u` 才是这次真实的结果（not_applied）
        self._reconcile(current)
        # 开发机（没有版本号）不上报：也免得在仓库里生成 install_id
        info = latest(opener=self._opener, params=self.report_params(current) if current else None)
        if info is None:
            self.last_error = "查不到更新源（网络不通或服务端没响应），过一会儿再发「更新」"
            return None
        build = info["build_id"]
        if not current:
            # 从仓库直接跑的（没有 buildinfo.py）⇒ 这是开发机，绝不自动改它
            logger.info("本机没有版本号（开发机），不自动更新")
            return None
        if build == current and not self._installed_half(build):
            # 「installing」+ 版本号已经是它 = 装成功：上面 `_reconcile` 已经把它记成 ok
            return None
        if self.not_applied(build) and not manual:
            # 🔴 第 32 轮：这一版上次没装上（重启后还是旧版 / 安装器自己退回了）⇒ 自动那一拍不再装，
            # 原来 6 小时后又重装同一版、再坏一次。他亲手发「更新」才装（manual 绕过）。
            self.last_error = f"新版 {build} 上次没装上，还在用 {current}。发「更新」再试一次。"
            return None
        if self._tried_recently(build, current):
            rec = self._state().get(build) or {}
            if rec.get("outcome") == "ok" and build == current:
                return None
            if rec.get("outcome") == "ok" and not manual:
                # 刚装成功、盘上版本号还没跟上（之后也没装过别的）⇒ 不是失败，别这么说
                self.last_error = f"{build} 刚装过，等它重启生效；还不对就发「更新」"
                return None
            if not manual:
                self.last_error = f"{build} 刚才装失败了，过几小时自己再试；想现在试就发「更新」"
                return None
        part = info.get(installer_key())
        if not isinstance(part, dict) or not isinstance(part.get("sha256"), str) \
                or not isinstance(part.get("file"), str):
            self.last_error = "新版本的信息不全（缺文件名或校验值）"
            return None
        # 第 38 轮：Windows 不再 `manual_only`，和 Mac 一样往下走（见文件头「Windows 自动更新」）
        return info

    # ---------- 下载 + 核对 ----------

    def download(self, info: dict, dest_dir: str) -> str:
        """下载并核对 sha256。对不上 ⇒ **重读一次 latest.json 再试**，还不对才抛。

        🔴 11-21：为什么要重试一次。发版不是原子的 —— `publish-update.sh` 先换包、
        最后写 `latest.json`（那个顺序防的是「json 新、包旧」）。但**反方向有个真窗口**：
        198 实测 `feishu-agent-windows.bat` mtime `12:42:34.638`、`latest.json` `12:42:39.723`
        ⇒ 这 **5.1 秒**里来查更新的客户端拿到的是**旧 json 的 sha + 已经换好的新包**，
        sha 必然对不上。现场：`…<机器>` 11-20 23:20 的 `ValueError: 下载回来的安装包校验不过`
        （上报指纹 `856d29f0669c`），而它 23:20:07 正落在那次发版的窗口里。
        原来这里只下一次、对不上就抛 ⇒ 这一拍记成 fail，白等一轮。
        重试**不放松校验**：重新取一份 latest.json，拿新的 sha 再核一次，还不对照旧抛。
        调顺序消不掉这个窗口（两头各有一个），根治要给包名带上 build_id、新旧并存。
        """
        for attempt in (1, 2):
            part = info[installer_key()]
            url = f"{BASE_URL}/{part['file']}"
            data = _fetch(url, opener=self._opener)
            got = hashlib.sha256(data).hexdigest()
            if got == part["sha256"]:
                break
            if attempt == 2:
                raise ValueError(
                    f"下载回来的安装包校验不过（{got[:12]}… ≠ {part['sha256'][:12]}…）")
            # 发版那几秒的错位 ⇒ 重新读一次清单（它这会儿多半已经写好了）。
            # 🔴 直接用 `latest()`，**不走 `check()`** —— 那个会判冷却、会 `_reconcile`，
            # 在这一步里都是副作用；这里只要一份新鲜的清单。不带上报参数（这不是一次轮询）。
            logger.info("安装包 sha 对不上，重读一次清单再试（多半撞上发版那几秒）")
            fresh = latest(opener=self._opener)
            if not fresh or installer_key() not in fresh:
                raise ValueError(
                    f"下载回来的安装包校验不过（{got[:12]}… ≠ {part['sha256'][:12]}…）")
            info = fresh
        path = os.path.join(dest_dir, part["file"])
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o700)
        return path

    # ---------- 装 ----------

    def run_installer(self, path: str) -> subprocess.CompletedProcess:
        """跑安装包。它自己会停掉旧服务、装新码、重启 —— 所以这个进程**可能被它杀掉**。"""
        started = time.monotonic()
        try:
            return self._run_installer(path)
        finally:
            # 🔴 记时放在**这一层**，不放各平台那支里：这里是 mac / windows / 测试注入
            # （`self._runner`）三条路的唯一入口 ⇒ 一处就全覆盖。
            # 第一版只在 `_run_installer_windows` 里记 ⇒ mac 恒 -1、注入 runner 的测试也恒 -1，
            # 而这个数字正是用来分「被杀 vs 超时」的，少一条腿等于那条腿上查不了。
            # `finally`：安装器把桥自己杀掉那种情况走不到这里（没关系，那种本来就不上报失败），
            # 但**抛异常**（timeout / OSError）那条路也要留下秒数 —— 超时 20 分钟和 3 秒被杀，
            # 上报里得分得开。
            self._last_installer_sec = int(time.monotonic() - started)

    def _run_installer(self, path: str) -> subprocess.CompletedProcess:
        if self._runner is not None:
            return self._runner(path)
        if platform.system() == "Windows":
            return self._run_installer_windows(path)
        # 🔴 **重装这条路一句都不问**（2026-11-06 核过 install-mac.sh）：配置已经在
        # ⇒ 沿用原机器人（`EXIST_ID`）、沿用上次的目录（`KEEP_WS`），`read` 那几行都走不到。
        # 真要问起来也不会挂住：stdin 给 /dev/null ⇒ read 立刻失败，安装器报错退出，
        # 这一版记成失败、6 小时后再试，服务照旧活着。
        # 🔴 **必须脱离桥自己的进程组**（`start_new_session=True`）。
        # 安装器第 6 步是 `launchctl bootout` 旧服务 → 写 plist → `bootstrap` 新的，
        # 而这个安装器是桥的子进程 ⇒ bootout 会把桥**连着它的子进程一起收走**，
        # `bootstrap` 那一行永远跑不到 ⇒ 装到一半、服务再也起不来。
        # R1 的 A/B 探针（真 plist + 真 bootout）实测：
        #   不加 ⇒ 日志停在「要 bootout 了」、job 已不在、bootstrap 没执行
        #   加了 ⇒ bootout 之后安装器还活着 → bootstrap 成功 → 服务回来
        env = dict(os.environ, CARHER_AUTO_UPDATE="1")   # 安装器据此跳过「重新发布应用版本」（第 25 轮）
        # 🔴 第 33 轮（本机 11-09 21:57 现场：自动更新后 bridge.prev 一直留着）：安装器输出原来接管道
        # （capture_output）。安装器第 6 步把**这个进程**（旧桥）换掉 ⇒ 管道读端没了 ⇒ 安装器之后
        # 第一次 echo 就吃 SIGPIPE 死掉 ⇒ 「连上了就删 bridge.prev / 没连上就换回旧版」那段**永远跑不到**
        # ⇒ 自动回退形同虚设。本机实测：子进程在父进程死后写管道，跑不到结尾。
        # 改成写日志文件：文件描述符跟着安装器走，旧桥死了照样写得进去。
        log_path = self._install_log_path()
        with open(os.devnull, "rb") as devnull, open(log_path, "w", encoding="utf-8") as out:
            proc = subprocess.run(  # noqa: S603 —— path 是刚校验过 sha256 的文件
                ["/bin/bash", path],
                stdin=devnull,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
                timeout=20 * 60,
                start_new_session=True,
            )
        tail = ""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-2000:]
        except OSError:
            pass
        return subprocess.CompletedProcess(getattr(proc, "args", ["/bin/bash", path]),
                                           getattr(proc, "returncode", 1), tail, "")

    def _install_log_path(self) -> str:
        log_path = os.path.join(os.path.dirname(self.state_path) or ".", "logs", "selfupdate-install.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
        except OSError:
            log_path = os.devnull
        return log_path

    def _run_installer_windows(self, path: str) -> subprocess.CompletedProcess:
        """Windows：powershell 跑 .bat 里的 PS 正文（见文件头「Windows 自动更新」）。

        和 Mac 那支同一套纪律：stdin 给空、输出写文件（绝不接管道）、`CARHER_AUTO_UPDATE=1`。
        安装成功时它会结束**这个进程**（桥的 python）、让外面那层循环用新码拉起 ⇒ 下面的返回不一定跑得到，
        结果靠 `update_state.json`（`installing` / `rolled_back`）在新进程里落定（`_reconcile`）。
        """
        env = dict(os.environ, CARHER_AUTO_UPDATE="1", CARHER_SELF=os.path.abspath(path))
        argv = _windows_installer_argv()
        log_path = self._install_log_path()
        flags = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
        with open(os.devnull, "rb") as devnull, open(log_path, "w", encoding="utf-8") as out:
            def _go(creationflags: int):
                return subprocess.run(  # noqa: S603 —— path 是刚校验过 sha256 的文件
                    argv, stdin=devnull, stdout=out, stderr=subprocess.STDOUT, env=env,
                    timeout=20 * 60, creationflags=creationflags,
                )
            # 🔴 11-22：记下它**跑了多久**。这个数字有鉴别力，不是凑数的 ——
            # 「一键安装」会话今天在一台手动装的机器上拿到两条同形上报（我核过原文）：
            #   17:38 子安装器 codex  退出码 -1/0xFFFFFFFF，跑了 **45 秒**
            #   17:43 子安装器 claude 退出码 -1/0xFFFFFFFF，跑了 **6 秒**
            # 同一台机器、两个不同的子安装器、都被结束 ⇒ **固定超时不会一次 45 秒一次 6 秒**，
            # 所以「超时」这个解释被排掉了，剩下的是「有东西在反复杀 powershell.exe」。
            # 我们这三台卡住的（…<机器> / …<机器> / …<机器>）走的是自动更新，
            # 没有那层「上报 + 当前窗口重试」的兜底，所以直接卡住、而且连这个秒数都没采。
            # 🔴 采在**桥这边**（`run_installer` 那一层前后记时间），不进 `_PS_BOOT`：
            # 退出码 -1 意味着 powershell 自己被杀，`-Command` 那段字符串根本没机会跑 ——
            # 把诊断放进去等于白做（这是「一键安装」会话建议的那一刀，我核过它对这个码无效）。
            try:
                proc = _go(flags | _CREATE_BREAKAWAY_FROM_JOB)
            except OSError:
                # 计划任务的作业对象不许脱离 ⇒ CreateProcess 拒绝访问。不脱离照样能装（安装器只结束 python，不动作业里那层循环）
                logger.info("安装器脱离不了计划任务的作业，留在作业里跑")
                proc = _go(flags)
        tail = ""
        try:
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-2000:]
        except OSError:
            pass
        return subprocess.CompletedProcess(argv, getattr(proc, "returncode", 1), tail, "")

    def update_now(self, info: dict) -> tuple[bool, str]:
        """下载 + 核对 + 装。返回 (成功?, 给人看的一句话)。"""
        build = info["build_id"]
        if info.get("manual_only"):
            # 第 38 轮起 check() 不再给这个标记；留着防外面有人手拼（不下载、不记失败）
            return False, f"这台要自己装：{self.download_url(info)}"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = self.download(info, tmp)
                if not self.is_idle():
                    # 下载那几秒里来了活 ⇒ 这次不装，下一拍再看（不记失败）
                    logger.info("下载完发现有活在跑，先不装 %s", build)
                    return False, "有活在跑，先不装，等它跑完我自己装。"
                self.installing = True
                logger.info("开始装新版 %s", build)
                # 安装成功时这个进程会被安装器换掉，下面那几行（记成功、清临时目录）不一定跑得到；
                # 先记一笔「在装」，失败分支会覆盖成 fail。
                # 第 32 轮：记上从哪版来 ⇒ 重启后还是这一版 = 没装上（`_reconcile`）
                before = self._state().get(build)
                self._remember(build, "installing", **{"from": self.current_build()})
                proc = self.run_installer(path)
        except Exception as exc:  # noqa: BLE001 —— 更新失败绝不能把桥带崩
            self.installing = False
            logger.exception("自动更新失败 build=%s", build)
            # 🔴 抛异常这条路也要带秒数：`TimeoutExpired`（20 分钟）和「3 秒就被杀」
            # 在上报里必须分得开，而上面那句 `exception` 的 emsg 是异常自己的原文，
            # 不一定含时间（`OSError` 就不含）。
            # `sec=-1` 是有意义的值：**安装器一次都没跑**（下载 / 校验就挂了）⇒ 别去查杀软。
            if self._last_installer_sec >= 0:
                logger.error(_INSTALLER_DIED_WITH_EXC,
                             exc_info=_RcOnly(f"installer raised sec={self._last_installer_sec}"))
            self._remember(build, "fail", str(exc))
            self.last_error = str(exc)
            # 给人看的那句不带异常原文（可能是一长串英文栈）；原文进日志
            return False, "更新没装上，这一版先跳过了。原来那版照常能用，过几小时我再试一次。"
        self.installing = False
        if proc.returncode == WIN_INSTALLER_BUSY_RC and installer_key() == "windows":
            # 撞上另一个安装器（第 38 轮）⇒ 这次什么都没动：把「在装」那笔撤掉（恢复原来那条），不记 fail
            state = self._state()
            if isinstance(before, dict):
                state[build] = before
            else:
                state.pop(build, None)
            self._write_state(state)
            logger.info("另一个安装器在跑，这次不装 %s，下一拍再试", build)
            self.last_error = "另一个安装窗口还在跑"
            return False, "另一个安装窗口还在跑，等它装完我再自己试。"
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-300:] or (proc.stderr or "")[-300:]
            # 🔴 两句都打：带码那句是**常量**（上报只认常量模板，`%s` 的实参传不回来），
            # 带 build 和输出尾巴那句留在本机日志里给排查用。
            # 11-20 之前只有下面那句 ⇒ 4 台 Windows 连着几天失败，退出码谁都不知道。
            # 🔴 11-21：码**一定**要能传回来，而不是靠「这张表列全了没有」。
            # 上一版的做法是手写码表 ⇒ 列不到的就只报「表里没有的码」，
            # 而「一键安装」会话实测 Windows 进程层那 7 个码我一个都没列到
            # ⇒ 发了一版等于白发。追着往表里加是打地鼠。
            # 通道在这里：`errreport` 的 `emsg` 走 `_scrub` 脱敏，**不要求是源码常量**
            # （只有 `msg`/模板那条有这个红线）⇒ 挂一个只含退出码的异常，
            # 数字就能完整上报（本轮实测 -1073741819 / 3221225477 都原样回来）。
            # 异常里只放 `rc=<数字>`：不含路径、用户名、机器信息。
            # 表继续留着是为了「一眼看懂」（0xC0000005 比 -1073741819 好认），
            # 但**能不能查到码**已经不依赖它了。
            # 🔴 11-21：顺带把**安装器日志的形状**也带上（不是内容）。
            # 现场：…<机器> 装 `04f9541a` 拿到 `rc=4294967295`（0xFFFFFFFF = -1），
            # 而那一版对 Windows 安装器只改了一个数字（`defaults_rev` 16→17，纯 config 模板），
            # 且同一版 11 台（含 3 台 Windows）全部 `u=ok` ⇒ **不是包的问题**。
            # `rc=-1` 的形状是「powershell.exe 没起来 / 起来就死」，这时安装器正文一行都没跑到，
            # 所以正文里的 `Die`/`trap` 上报点一个都碰不到（今天那台 0 条 her-remote 安装上报，吻合）。
            # 分「没起来」和「起来就死」只差一个判据：**那个日志文件有没有被写过**。
            #   `log=empty` ⇒ 进程根本没跑（杀软拦下 / 作业对象限制 / 可执行文件没了）
            #   `log=<字节数>` ⇒ 起来了但死在半路，那就该去读 state 里的 detail
            # 🔴 只发「空 / 多少字节」这个形状，**绝不发日志内容**（里面有路径和用户名）。
            # 🔴 11-22 第三样：`sec=<跑了多少秒>`。为什么它有鉴别力——
            # 「一键安装」会话今天在一台**手动**装的机器上拿到两条同形上报（我核过原文）：
            #   17:38 子安装器 codex  rc=-1/0xFFFFFFFF，跑了 45 秒
            #   17:43 子安装器 claude rc=-1/0xFFFFFFFF，跑了  6 秒
            # 同一台机器、两个子安装器、都被结束 ⇒ 固定超时不可能一次 45 秒一次 6 秒，
            # 「超时」这个解释当场被排掉，剩下的是「有东西在反复杀 powershell.exe」。
            # 而**走自动更新**的那三台（…<机器> / …<机器> / …<机器>）没有那层「上报 + 当前窗口
            # 重试」的兜底 ⇒ 连这个秒数都没采到，所以查不动。这一刀就是补这个。
            # 同样只能走 `emsg`：带数字的句子进不了 `msg`（模板必须整句是源码常量）。
            logger.error(
                installer_rc_log(proc.returncode),
                exc_info=_RcOnly(
                    f"installer exited rc={proc.returncode} "
                    f"log={'empty' if not tail else len(tail)} sec={self._last_installer_sec}"))
            logger.error("安装包退出码 %s build=%s", proc.returncode, build)
            if (self._state().get(build) or {}).get("outcome") == ROLLED_BACK:
                # 安装器第 6 步新版没起来、已经换回旧版并记了 rolled_back ⇒ 别用 fail 盖掉它
                #（fail 6 小时后会自动重装；没装上的那一版只该手动再试）
                self._reconcile(self.current_build())
                self.last_error = ROLLED_BACK     # 调用方据此知道「这句已经是没装上的提醒」
                return False, (f"新版 {build} 没装上，已经退回原来那版，照常能用。"
                               "发「更新」再试一次。")
            self._remember(build, "fail", tail)
            self.last_error = f"installer exited rc={proc.returncode}"  # 内部用，不发给用户
            return False, "更新没装上，这一版先跳过了。原来那版照常能用，过几小时我再试一次。"
        self._remember(build, "ok")
        logger.info("已更新到 %s", build)
        return True, f"已经更新到 {build}，助手刚重启过一次"

    def tick(self) -> Optional[tuple[bool, str]]:
        """循环里的一次。有新版 + 现在空闲 ⇒ 装。返回 None 表示这次什么都没做。"""
        info = self.check()
        if info is None:
            return None
        if info.get("manual_only"):
            # 第 38 轮起 check() 不再给这个标记（同 update_now 那条）
            return None
        if not self.is_idle():
            logger.info("有新版 %s，但现在有活在跑，等空闲再装", info["build_id"])
            return None
        return self.update_now(info)
