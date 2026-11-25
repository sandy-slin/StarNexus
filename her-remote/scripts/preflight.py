"""装机前体检：把「装到一半才炸」换成「一屏列全所有拦路的东西」。

## 为什么要有这个

现在两个安装器都是**边装边查**：第 1 步查 claude、第 2 步查 Python、第 2 步末尾才
下依赖、第 5 步才问飞书认不认凭据。后果是同事那边典型的一次安装长这样：

    [1/6] ✓    [2/6] ✓    …… 等一分钟下依赖 ……    [5/6] ✗ 飞书不认这对凭据

他为此等了一分钟、建了个飞书应用、粘了两串凭据，才知道第一步就该先让管理员审批。
下一次他还要从头再来一遍 —— 而每一轮都要等那一分钟。

更糟的一类是**装完了但起不来，而安装器报绿**。实测（2026-11-05，见 findings）：
launchd 的 job 只要注册成功，`launchctl print` 就返回 0，安装器照此打
「✓ 已经在后台跑起来了」；而进程可以在 1 秒内崩掉、KeepAlive 每 5 秒重拉一次。
配置里一个带反斜杠的目录名就够了 —— tomllib 在启动时抛 TOMLDecodeError，
日志里**既没有** `connected to` **也没有** `connect failed`，于是安装器落到
「它还在重试连接」那条分支，叫用户「等两分钟」。他等不出结果。

所以体检的设计原则有三条：

1. **先查完再动手。** 所有不花钱、不改东西就能查的前置条件，一次全查，一屏列全。
2. **每一条都带修法。** 只说「缺 Python」等于把人扔在原地（见 test_install_ux）。
3. **只读。** 体检自己绝不装东西、不写配置、不碰 `~/.carher-feishu-agent`
   —— 它要能在「我装之前先看看行不行」这个心态下被随便双击。

## 这一轮的范围

本模块只实现**判定逻辑**和**渲染**，由测试驱动，不被任何安装器调用
（`bridge/*.py` 被 build.sh 的 check_complete 按目录 glob 盯着，往那里加文件
会立刻让产物完整性测试变红，而本轮不许跑 build.sh）。
接进安装器是下一步的事，接法写在 findings 里。
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field

# 判定三档。order 决定渲染顺序：挡路的排最前，用户不用自己找。
BLOCK = "block"  # 不解决装不上 / 装上也跑不起来
WARN = "warn"  # 能装，但会在某个时刻咬人（且用户自己想不到）
OK = "ok"

_SEVERITY_ORDER = {BLOCK: 0, WARN: 1, OK: 2}


@dataclass(frozen=True)
class Finding:
    """一条体检结果。

    `fix` 是**用户下一步要做的动作**，不是故障描述 —— BLOCK 和 WARN 必须有它，
    由 `Report.invariants()` 兜住（和 test_install_ux 那条「每个 die 都要给下一步」
    同一条纪律，只是这里能在构造时就拦住）。
    """

    key: str
    severity: str
    title: str
    fix: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.severity not in _SEVERITY_ORDER:
            raise ValueError(f"未知档位：{self.severity}")
        if self.severity in (BLOCK, WARN) and not self.fix.strip():
            raise ValueError(f"{self.key}：{self.severity} 必须给修法，不能只说「错了」")


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)

    @property
    def blockers(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == BLOCK]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    def can_install(self) -> bool:
        return not self.blockers

    def sorted_findings(self) -> list[Finding]:
        """挡路的排最前，同档位内保持检查顺序（检查顺序按「同事最可能卡在哪」排的）。

        `sorted` 是稳定排序 ⇒ 同档位的相对次序就是加入次序，不用额外记序号。
        """
        return sorted(self.findings, key=lambda f: _SEVERITY_ORDER[f.severity])

    def invariants(self) -> None:
        """自检这份报告本身能不能见人。渲染前调用。

        盯两件事，都是实测踩过的形状：
          - 每条 BLOCK/WARN 都有修法（Finding 构造时已拦，这里是第二道）
          - key 不重复 —— 重复意味着同一个问题在一屏里说两遍，用户会以为是两个问题
        """
        seen = set()
        for finding in self.findings:
            if finding.severity in (BLOCK, WARN) and not finding.fix.strip():
                raise ValueError(f"{finding.key} 没有修法")
            if finding.key in seen:
                raise ValueError(f"重复的检查项：{finding.key}")
            seen.add(finding.key)


# ─────────────────────── 单项检查 ───────────────────────
#
# 每个 check_* 都是**纯函数**：事实从参数进来，不去摸真机器。
# 这样测试能把「同事那台机器」的各种形状直接造出来 —— 包括我手上这台 Mac
# 根本造不出的那些（Windows、公司代理、OneDrive 重定向过的家目录）。

PY_MIN_MINOR = 11  # tomllib 要 3.11+，和两个安装器里的常量一致

ONECLICK_URL = "https://chat.auto-link.com.cn/ai/"


def check_claude(claude_path: str | None, service_path_dirs: list[str] | None = None) -> Finding:
    """claude 在不在，**以及常驻服务那份 PATH 里认不认得它**。

    🔴 第二问才是这一项存在的理由。安装器查的是「我这个 shell 能不能找到 claude」，
    但真正跑活的是 launchd / 计划任务里那个进程，它的 PATH 是装机那一刻**写死**进
    plist 的（mac 侧是 `dirname $CLAUDE_BIN` 加四个固定目录）。两者分叉时的后果：
    装完全绿、飞书里能聊、一问活就答不出来 —— 子进程起不来，
    实测抛的是 `FileNotFoundError: [Errno 2] No such file or directory: 'claude'`。
    """
    if not claude_path:
        return Finding(
            key="claude",
            severity=BLOCK,
            title="没有 claude 命令",
            fix=f"先按 {ONECLICK_URL} 装好 Claude Code，装完「关掉这个窗口重开一个」再来",
            detail="这个助手靠 Claude Code 干活；不重开窗口的话系统还找不到它",
        )
    if service_path_dirs is not None:
        home = os.path.dirname(claude_path)
        if home not in service_path_dirs:
            return Finding(
                key="claude",
                severity=BLOCK,
                title="后台服务那份 PATH 里找不到 claude",
                fix="重新双击安装器（它会按现在的位置重写开机自启配置）",
                detail=(
                    f"claude 在 {home}，而后台服务的 PATH 是 "
                    f"{os.pathsep.join(service_path_dirs)}。"
                    "不一致的话：飞书里能聊，但一让它干活就答不出来"
                ),
            )
    return Finding(key="claude", severity=OK, title=f"claude 在 {claude_path}")


# claude 探活的四种结果。shell 侧**只回一个 token**，不回 stderr 原文 ——
# 原文里有 `/Users/<真名>/…`，而这几句话会被同事截图转发给 IT。
CLAUDE_RUN_OK = "ok"
CLAUDE_RUN_NO_INTERPRETER = "no-interpreter"
CLAUDE_RUN_FAILED = "failed"
CLAUDE_RUN_SKIPPED = "skipped"


def check_claude_launch(token: str) -> Finding:
    """在**后台服务那份 PATH 下真跑一次** `claude --version` 的结果。

    🔴 2026-11-05 实测，这一项是为了纠正 `check_claude` 的一个盲点：
    mac 的 plist 把 PATH 写成 `dirname $CLAUDE_BIN` 加四个固定目录，Windows 的
    runner 把 `$claudeDir` 拼在 `$env:PATH` 前面 —— 也就是说**两边都保证
    claude 自己找得到**，`check_claude` 那条字符串比较在现版安装器下永远不会红。
    真正会崩的是 claude **依赖的别的程序**：当天实测 npm 装出来的 claude 是
    `#!/usr/bin/env node` 脚本，而 nvm 的 node 不在那四个固定目录里 ——
    喂一份只有 shim 目录的 PATH 得到 `env: node: No such file or directory`、rc=127，
    而装机全程是绿的、飞书里也能聊，只有一让它干活才答「我这边出错了」。

    ⚠️ 同一天 22:29 claude 自动升级成**单文件编译产物**（`bin/claude.exe`，
    Mach-O arm64，不再需要 node），那个具体形状在本机当下已经不成立。
    **这恰好是这一项该「真跑一次」的理由**：claude 的打包方式会变（本机一天之内
    变过一次），而「在后台那份环境下到底跑不跑得起来」这个问题不变。所以这里
    只认退出码，不认任何一种具体形状。实测跑一次 0.015 秒，买得起。
    """
    if token == CLAUDE_RUN_OK:
        return Finding(key="claude_launch", severity=OK, title="后台服务那份环境下 claude 能跑")
    if token == CLAUDE_RUN_SKIPPED:
        # 探不了就别吭声：这一项只负责抓真实失败，不负责制造「可能有问题」的焦虑
        return Finding(key="claude_launch", severity=OK, title="claude 可用性：没查")
    if token == CLAUDE_RUN_NO_INTERPRETER:
        return Finding(
            key="claude_launch",
            severity=BLOCK,
            title="后台跑的时候 claude 会少一个它依赖的程序",
            fix=f"按 {ONECLICK_URL} 重装一次 Claude Code（它会装到后台也找得到的位置），"
            "装完关掉这个窗口重开一个再来",
            detail="现在这样装下去：飞书里能聊，但一让它干活就只会说「我这边出错了」",
        )
    return Finding(
        key="claude_launch",
        severity=BLOCK,
        title="claude 在后台服务那份环境下跑不起来",
        fix="双击 doctor 自检一遍，照它给的下一步做；或按 "
        f"{ONECLICK_URL} 重装一次 Claude Code 再来",
        detail="现在这样装下去：飞书里能聊，但一让它干活就只会说「我这边出错了」",
    )


def _py_fix(reinstall: bool = False) -> str:
    """只给这台电脑能用的那一句(原来 Mac/Windows 两句挤一行,Mac 那句还要求先有 brew)。"""
    import sys
    if sys.platform == "darwin":
        return "重新双击一次 feishu-agent-mac.command，安装器会自己下载一份专用 Python，不用你装"
    if sys.platform.startswith("win"):
        return ("去 https://www.python.org/downloads/ " + ("重装一遍" if reinstall else "装最新版") +
                "，安装第一屏勾上「Add python.exe to PATH」，装完关掉这个窗口重新双击安装文件")
    return "用系统的包管理器装 Python 3.12（例如 sudo apt install python3.12 python3.12-venv）"


def check_python(version: tuple[int, int] | None, has_venv: bool = True) -> Finding:
    """Python 够不够新、venv 模块在不在。

    `has_venv` 单独一问：系统上装了 3.11+ 但缺 venv 模块是真实形状（精简版、
    某些发行渠道），而那会在「正在准备独立的 Python 环境」那一步才炸。
    """
    if version is None:
        return Finding(
            key="python",
            severity=BLOCK,
            title=f"没有 Python 3.{PY_MIN_MINOR} 或更新的版本",
            fix=_py_fix(),
            detail="系统自带的那个太老（macOS 自带 3.9，缺 tomllib）",
        )
    if version < (3, PY_MIN_MINOR):
        return Finding(
            key="python",
            severity=BLOCK,
            title=f"Python {version[0]}.{version[1]} 太老（要 3.{PY_MIN_MINOR}+）",
            fix=_py_fix(),
            detail="读配置用的 tomllib 是 3.11 才进标准库的",
        )
    if not has_venv:
        return Finding(
            key="python",
            severity=BLOCK,
            title=f"Python {version[0]}.{version[1]} 装得不全（缺 venv 模块）",
            fix=_py_fix(reinstall=True),
            detail="没有 venv 就建不了独立环境，会卡在「正在准备独立的 Python 环境」",
        )
    return Finding(
        key="python", severity=OK, title=f"Python {version[0]}.{version[1]}"
    )


def check_disk(free_bytes: int | None, need_bytes: int = 400 * 1024 * 1024) -> Finding:
    """盘够不够。

    400MB 不是猜的：这台机器上实测 `~/.carher-feishu-agent` 占 146M（venv 144M），
    一个全新 venv + lark-oapi 也是 142M。留一倍多余量给 pip 的下载缓存和日志。
    放在**下载依赖之前**查 —— 盘满的后果是下到一半失败，留一个半装的 venv。
    """
    if free_bytes is None:
        return Finding(
            key="disk", severity=OK, title="磁盘空间：没查（读不到，不拦）"
        )
    if free_bytes < need_bytes:
        return Finding(
            key="disk",
            severity=BLOCK,
            title=f"磁盘只剩 {free_bytes // (1024 * 1024)}MB",
            fix=f"清出至少 {need_bytes // (1024 * 1024)}MB 再装",
            detail="装好大约占 150MB（独立 Python 环境），下载过程还要一些临时空间",
        )
    return Finding(
        key="disk", severity=OK, title=f"磁盘剩 {free_bytes // (1024 * 1024 * 1024)}GB"
    )


def check_network(pip_index_ok: bool, feishu_api_ok: bool) -> Finding:
    """两个出口分开判，因为它们坏起来的样子完全不同，修法也不同。

    实测（2026-11-05 本机）：`pypi.org:443` 不通、`pypi.tuna.tsinghua.edu.cn` 通、
    `open.feishu.cn` 的取 token 接口 200。也就是说安装器那条「国内源失败就退回
    官方源」的兜底，在这条网络上是**退回一个不通的地方** —— 实测确实能装上，
    但走的是 pip 自己 15 次重试，总耗时 5 分 29 秒（而界面上写的是「再约 1 分钟」）。
    """
    if not feishu_api_ok:
        return Finding(
            key="network",
            severity=BLOCK,
            title="连不上飞书（open.feishu.cn）",
            fix="先关掉 VPN / 代理再试；公司网络限制的话把这句发给 IT："
            "「需要放通 open.feishu.cn 的 443 和 WebSocket 长连接」",
            detail="飞书长连接必须直连，走代理连不上 —— 这是机器人收消息的唯一通道",
        )
    if not pip_index_ok:
        return Finding(
            key="network",
            severity=WARN,
            title="下载源连不上，装依赖会很慢或失败",
            fix="挂了 VPN 就先关掉；还不行就换个网络再试",
            detail="两个源都不通的话会卡在「正在下载机器人需要的组件」那一步",
        )
    return Finding(key="network", severity=OK, title="网络通（飞书 + 下载源）")


def check_credentials(code: int | None) -> Finding:
    """拿 app_id / app_secret 去换一次 token，**在建应用和粘凭据之后、装环境之前**。

    错误码到人话的映射和 doctor 第 5 项同一套（那边实测过）：
    10003/10013/10014 = 凭据不对，10012 = 没发布/没审批。
    `code is None` = 还没有凭据（第一次装），不算问题。
    """
    if code is None:
        return Finding(key="credentials", severity=OK, title="凭据：还没填（第一次装正常）")
    if code == 0:
        return Finding(key="credentials", severity=OK, title="凭据有效")
    if code in (10003, 10013, 10014):
        return Finding(
            key="credentials",
            severity=BLOCK,
            title=f"App ID 或 App Secret 不对（code {code}）",
            fix="回 open.feishu.cn「凭证与基础信息」用复制按钮重新复制，"
            "注意两串别弄反（App ID 是 cli_ 开头的短串）",
        )
    if code == 10012:
        return Finding(
            key="credentials",
            severity=BLOCK,
            title=f"应用还没发布，或管理员还没审批（code {code}）",
            fix="去 open.feishu.cn 右上角「创建版本」→「申请发布」，然后催一下飞书管理员",
            detail="这不是你装错了。管理员点同意之前，机器人收不到任何消息",
        )
    return Finding(
        key="credentials",
        severity=BLOCK,
        title=f"飞书不认这对凭据（code {code}）",
        fix="双击 doctor 自检一遍，照它给的下一步做（自检里会带着这个错误码）",
    )


def check_workspace_path(path: str) -> Finding:
    """工作目录这个字符串，能不能安全地写进 config.toml。

    🔴 实测（2026-11-05）：目录名里一个反斜杠就够了 ——
    生成的 `workspaces = ["/tmp/back\\slash"]` 让 tomllib 抛
    `Unescaped '\\' in a string`；带双引号的目录名抛 `Unclosed array`。
    而这个错**不在安装那一刻报**，是在服务启动时报：
    launchd 照样把 job 注册上（`launchctl print` 返回 0 ⇒ 安装器打绿），
    进程起来就崩、KeepAlive 每 5 秒重拉一次，日志里既没有 `connected to`
    也没有 `connect failed` ⇒ 安装器落到「它还在重试连接，等两分钟」那条分支。
    用户永远等不出结果。所以这一项必须在**写配置之前**拦住。

    空格和中文是安全的（实测 tomllib 解析正常），不报警 —— 误报会让人不敢用中文目录。
    """
    for char, why in (("\\", "反斜杠"), ('"', "双引号")):
        if char in path:
            return Finding(
                key="workspace",
                severity=BLOCK,
                title=f"目录名里有{why}，配置文件会写坏",
                fix="换一个不带这个字符的目录，或先把目录改名",
                detail=f"{path} —— 带{why}的路径写进 config.toml 后读不出来，"
                "后台服务会起来就崩，而安装器会误报成「还在重试连接」",
            )
    return Finding(key="workspace", severity=OK, title=f"工作目录可用：{path}")


def check_feishu_client(version: str | None, minimum: str = "7.22") -> Finding:
    """飞书客户端版本。只影响输入框上方那排常驻菜单，不影响能不能聊。

    所以是 WARN 不是 BLOCK —— 报成 BLOCK 会把一个能正常用的人挡在门外。
    读不到版本号时不吭声（别为一个猜不准的东西制造焦虑）。
    """
    if not version:
        return Finding(key="feishu_client", severity=OK, title="飞书客户端版本：没查")
    if _version_tuple(version) < _version_tuple(minimum):
        return Finding(
            key="feishu_client",
            severity=WARN,
            title=f"飞书客户端 {version} 偏旧，看不到输入框上方那排常驻菜单",
            fix=f"把飞书升到 {minimum} 或更新；不升也能用 —— 在聊天里发「菜单」一样有按钮",
        )
    return Finding(key="feishu_client", severity=OK, title=f"飞书客户端 {version}")


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", text)) or (0,)


def check_quarantine(quarantined: bool, platform: str = "darwin") -> Finding:
    """下载下来的脚本被打了隔离标记 ⇒ 双击时系统弹「无法打开，来自未验证的开发者」。

    实测（2026-11-05 本机）：Downloads 里经飞书 / 微信 / QQ 收到的文件**都带**
    `com.apple.quarantine`；我们的产物没有签名，`spctl -a -t exec` 判
    `rejected (source=no usable signature)`，连 `xattr -d` 去掉标记后仍然 rejected。
    但 `bash <文件>` 一路畅通 —— 这正是门户上别的安装器让用户「粘一行命令到终端」
    而不是「双击」的原因，而飞书助手的脚本自己写的是「双击本文件」。
    """
    if platform != "darwin" or not quarantined:
        return Finding(key="quarantine", severity=OK, title="没有下载隔离标记")
    return Finding(
        key="quarantine",
        severity=WARN,
        title="这个文件带「从网上下载」的标记，双击可能被系统拦住",
        fix="被拦住就在「终端」里跑：bash <把文件拖进来>  （拖一下会自动填好路径）",
        detail="弹「无法打开，来自未验证的开发者」时不是装坏了，是 macOS 的门禁",
    )


# ─────────────────────── 渲染 ───────────────────────


def render(report: Report) -> str:
    """一屏。挡路的在最前面，每条底下紧跟一句修法。

    刻意不打任何 launchctl / Scheduled-Task 命令（和 test_install_ux 第 3 条同一纪律：
    噪音会把「你真正要做的那件事」挤出视线）。
    """
    report.invariants()
    lines: list[str] = ["", "===== 装机前体检 =====", ""]
    for finding in report.sorted_findings():
        mark = {BLOCK: "✗", WARN: "⚠", OK: "✓"}[finding.severity]
        lines.append(f"  {mark} {finding.title}")
        if finding.detail:
            lines.append(f"      {finding.detail}")
        if finding.fix:
            lines.append(f"      → {finding.fix}")
    lines.append("")
    blockers = report.blockers
    if blockers:
        lines.append(f"  有 {len(blockers)} 项必须先解决，现在装下去会失败，所以停在这里。")
        lines.append("  按上面的 → 处理完，重新双击一次就行（已经做过的步骤不会重做）。")
    elif report.warnings:
        lines.append(f"  可以装。有 {len(report.warnings)} 项提醒，装完再看也行。")
    else:
        lines.append("  都没问题，开始装。")
    lines.append("")
    return "\n".join(lines)


# ─────────────────────── 采集（只读） ───────────────────────


def gather_local(service_path: str = "") -> Report:
    """摸一遍这台机器。**只读** —— 不装东西、不写配置、不碰 ~/.carher-feishu-agent。

    刻意只收**不花钱、不改状态**的那几项：网络探测和凭据校验要发请求，
    由调用方决定什么时候做（装之前问一次就够，不该每次 import 都打飞书接口）。
    """
    import sys

    report = Report()

    claude_path = shutil.which("claude")
    service_dirs = service_path.split(os.pathsep) if service_path else None
    report.add(check_claude(claude_path, service_dirs))

    version: tuple[int, int] | None = (sys.version_info[0], sys.version_info[1])
    try:
        import venv  # noqa: F401

        has_venv = True
    except ImportError:
        has_venv = False
    report.add(check_python(version, has_venv))

    try:
        free = shutil.disk_usage(os.path.expanduser("~")).free
    except OSError:
        free = None
    report.add(check_disk(free))

    return report


# ─────────────────────── 安装器入口 ───────────────────────
#
# 两个安装器都在第 0 步调它：`python3 preflight.py --service-path ... --claude-launch ...`
#
# 退出码就是契约（shell 侧只认这个，不解析屏幕文字）：
#   0 = 可以装（全绿，或只有提醒）
#   2 = 有挡路项，别装
#   其它 = 体检自己炸了 ⇒ **安装器必须照常往下走**（见 main 的 except）
#
# 🔴 为什么「体检炸了要放行」：体检是**附加的一道闸门**，不是安装的前提。
# 它崩了而安装器停住，等于我们亲手造出一个新的「装不上」—— 而且是个用户完全
# 无法自救的形状（他看不懂 Python 的报错，也不知道可以跳过）。
# 宁可漏报一次，不可凭体检自己的 bug 挡住一台能装的机器。

_EXIT_BLOCKED = 2


def _parse_args(argv: list[str]) -> dict:
    """手写解析，不用 argparse。

    理由：argparse 遇到不认识的参数会 `sys.exit(2)` —— 而 2 正好是我们「有挡路项」
    的退出码。将来谁给安装器加一个新参数、忘了同步这边，安装器就会把「参数拼错」
    读成「体检不让装」，然后对着一屏空白的体检结果停住。
    """
    out: dict = {"service_path": "", "claude_launch": CLAUDE_RUN_SKIPPED, "workspace": ""}
    keys = {"--service-path": "service_path", "--claude-launch": "claude_launch",
            "--workspace": "workspace"}
    index = 0
    while index < len(argv):
        key = keys.get(argv[index])
        if key and index + 1 < len(argv):
            out[key] = argv[index + 1]
            index += 2
        else:
            index += 1  # 不认识的参数直接跳过，绝不 exit
    return out


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(argv if argv is not None else []))
    report = gather_local(args["service_path"])
    report.add(check_claude_launch(args["claude_launch"]))
    if args["workspace"]:
        report.add(check_workspace_path(args["workspace"]))
    print(_fit_console(render(report)))
    return _EXIT_BLOCKED if report.blockers else 0


def _harden_stdio() -> None:
    """让 stdout/stderr 遇到编不出来的字符时换成 `?`，而不是抛 UnicodeEncodeError。

    🔴 2026-11-13 22:03 同事 Windows 实机（门户一键安装第 4/4 个）：stdout 被重定向时
    Python 用本机 ANSI 代码页（中文 Windows = GBK），而 render() 里的 ✓ ⚠ ✗ ⇒ GBK 编不出来 ⇒
    `print(render(report))` 抛 UnicodeEncodeError；兜底 except 里那句也带 ⚠，又抛一次 ⇒
    整个体检以 Python 堆栈收场。安装器那边会设 PYTHONIOENCODING=utf-8，这里是第二道：
    不管谁、用什么编码起它，都不许因为编码崩。
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - None（pythonw）/ 不是 TextIOWrapper：保持原样
            pass


# 控制台编不出 ✓ ✗ ⚠ ⇒ 时的替身（GBK 里都有）。光靠 errors="replace" 三个标记会全变成 `?`，
# 用户分不出哪条是挡路的 ✗、哪条是 ✓。
_CONSOLE_FALLBACK = str.maketrans({"✓": "√", "✗": "×", "⚠": "!", "⇒": "=>", "️": ""})


def _fit_console(text: str) -> str:
    """stdout 的编码编得出就原样；编不出（GBK 控制台）就换成替身字符。"""
    import sys

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except (UnicodeEncodeError, LookupError):
        return text.translate(_CONSOLE_FALLBACK)


def _say_safely(text: str) -> None:
    """兜底那句话本身也不许再崩：编码失败就退成纯 ASCII 再打一次，还不行就不打。"""
    try:
        print(_fit_console(text))
    except Exception:  # noqa: BLE001
        try:
            print(text.encode("ascii", "replace").decode("ascii"))
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":  # pragma: no cover - 由安装器调用
    import sys as _sys

    _harden_stdio()
    try:
        raise SystemExit(main(_sys.argv[1:]))
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        # 🔴 体检自己炸了**不许**挡住安装（见上面那段）。只给错误类名：
        # 异常消息里可能带路径（`/Users/<真名>/…`），而这一屏会被截图转发。
        _say_safely(f"\n  ⚠ 装机前体检没跑完（{type(exc).__name__}），跳过它继续装。\n")
        raise SystemExit(0) from None


