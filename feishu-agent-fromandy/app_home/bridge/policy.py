"""能干什么、不能干什么 —— 闸门全在这一个文件里。

两条设计红线（都来自 2026-11-03 的调研，不是凭感觉定的）：

1. **闸门必须在代码里，不能靠 prompt。** 给 agent 的工具白名单是进程启动参数
   （`claude --allowed-tools`），不是系统提示词里的一句请求。实测验证过：让它写文件
   和 `rm`，两次都被 Claude Code 自己的权限层挡掉，文件没变，且拦截记录回传。

2. **档位的上限由本机文件定，聊天只能在上限以内动。**
   config.toml 的 mode = 本机上限；聊天里能在上限以内切换，收紧点一次，放开要二次确认。超过上限只能改 config.toml（聊天里点什么都提不上去）。
   调研里最贴合我们需求的那个开源项目（lark-channel-bridge，2784★）默认把 claude
   跑成 `bypassPermissions`，README 自己承认"工作目录不是文件系统沙箱"。我们反过来：
   安装默认 readonly；全授权下仍然守读写边界和红线（见 approval.py 钩子）。
"""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from dataclasses import dataclass

logger = logging.getLogger("feishu-bridge.policy")

# 只读模式的 Bash 白名单。
#
# 写法说明：Claude Code 的 `Bash(前缀:*)` 是**前缀匹配**，所以这里每一条都必须
# 自身无害、且不能被当成跳板。刻意排除的东西：
#   - `git` 裸给 ⇒ 会放进 git push / git reset --hard。只给明确的只读子命令。
#   - `find` ⇒ 带 -exec 就是任意执行。用 Glob 工具代替。
#   - `python`/`node`/`sh` ⇒ 直接是任意代码执行。
#   - `cat`/`head`/`tail` ⇒ 不是危险，是没必要：Read 工具更好，且不绕过路径限制。
READONLY_BASH = (
    "Bash(git status:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Bash(git show:*)",
    "Bash(git branch:*)",
    "Bash(git remote -v:*)",
    "Bash(git rev-parse:*)",
    "Bash(ls:*)",
    "Bash(wc:*)",
    "Bash(file:*)",
    "Bash(du -sh:*)",
)

READONLY_TOOLS = ("Read", "Glob", "Grep", "TodoWrite")


def readonly_command_prefixes() -> tuple[str, ...]:
    """从 READONLY_BASH 抽出纯命令前缀，给审批钩子判「这条要不要弹卡片」。

    两边必须是同一份来源 —— 钩子是独立进程、只用标准库，没法 import 这里，
    所以装钩子时把这份导出成 json。`tests/test_approval.py` 盯着两边一致。
    """
    out = []
    for entry in READONLY_BASH:
        inner = entry[len("Bash("):-1] if entry.startswith("Bash(") else entry
        if inner.endswith(":*"):
            inner = inner[:-2]
        out.append(inner)
    return tuple(out)

# write 模式额外给的。注意**仍然不给** git commit/push、不给 rm、不给任意解释器。
# 「能改文件」和「能把改动发出去」是两回事，后者不在这一层放开。
WRITE_EXTRA_TOOLS = ("Edit", "Write", "NotebookEdit")

# 任何模式下都明确拒掉的。deny 在 Claude Code 里优先于 allow，所以这是硬底线。
# 🔴 第 16 轮收窄（领导原话：「默认一定要是全控制的」「这个权限问题太严重了，非常麻烦」）。
# P2 审计实测：全授权档、工作目录里，这三条被这张清单**直接拒**，连卡都不弹 ——
#   `rm victim.txt`（删自己的文件）、`curl localhost:8900/health`（看本机服务）、
#   `git reset HEAD`（撤销 add，不丢改动）
# 「全控制」在这几条上名不副实。这张清单排在审批钩子之前、全授权也盖不过它。
#
# 收窄之后各自由谁接着管（不是放任）：
#   rm        → 钩子的工作目录检查（目录外要审批）+ `never_approvable` 的删家目录红线
#               🔴 `Bash(rm:*)` 原来是那条红线的兜底，撤掉之后红线是**唯一**一道 ⇒
#                  2026-11-07 已把它漏的 7 种写法补上（`rm -rf ~/`、`rm -r -f ~`、
#                  `find ~ -delete` …），测试在 `tests/test_r16_deny_narrow.py`
#                  和 `tests/test_r16_watch_baseline.py`，别再放松。
#   git reset → 只挡 `--hard`（丢改动不可逆）；`HEAD`/`--soft`/`--mixed` 放行
#   curl/wget → 从这里撤掉：WebFetch 本来就开着，禁它只是把人逼去绕路（P2 实测
#               `printf > a.py && python3 a.py` 照样放行）。
#               🔴 代价说清：出网从「只有专用工具」变成「命令行也能」。所以**外带形状
#               仍然要审批**（只读档弹卡）：`curl -d @文件 外网`、`nc`、`scp`、
#               `rsync -e ssh`、`curl … | bash` 都被判「会写/要问」，用例在
#               `test_r16_deny_narrow.py::test_exfiltration_shapes_still_need_approval`。
# sudo / git push / git clean / kubectl delete：**不动**，照旧硬拒。
ALWAYS_DENY = (
    "Bash(sudo:*)",
    "Bash(git push:*)",
    "Bash(git reset --hard:*)",
    "Bash(git clean:*)",
    "Bash(kubectl delete:*)",
    # 凭据：读不到就不会被回传进飞书。飞书是外部系统，发出去就等于发布了。
    "Read(**/.env*)",
    "Read(**/*.pem)",
    "Read(**/*.key)",
    "Read(**/id_rsa*)",
    "Read(**/.aws/**)",
    "Read(**/.ssh/**)",
    "Read(**/credentials*)",
    # 🔴 第 16 轮：上面那 7 条 `**/` 是**相对工作目录**的 glob，盖不到目录外。
    # 主会话实测（真 claude 2.1.319，凭据放在工作目录外，bypass 参数下）：
    #   `Read(**/.env*)` → 读到了；`Read(//**/.env*)` → DENIED
    # 现在线上没漏：读工具本来只允许工作目录（`Read(<ws>/**)`），目录外的另有一道拒
    # （拿桥全授权档的真实参数实测，修复前后工具层都拒、内容没进上下文）。
    # 但要给领导放开「看工作目录外」，这几条就是凭据唯一的闸 ⇒ 必须先写成 `//**/`。
    # 两份都留：老版本 CLI 认不认 `//` 没测过，多写一份不会更松。
    "Read(//**/.env*)",
    "Read(//**/*.pem)",
    "Read(//**/*.key)",
    "Read(//**/id_rsa*)",
    "Read(//**/.aws/**)",
    "Read(//**/.ssh/**)",
    "Read(//**/credentials*)",
)


class PolicyError(ValueError):
    """配置或请求越界。消息会原样回到飞书，所以不要往里塞敏感内容。"""


# 档位从松到紧排序。聊天里能切档，但**只能切到 config.toml 写的那一档以内**。
#
# 🔴 为什么是"上限"而不是"默认"：文件头第 2 条说"提权只能改配置文件"，
# 而用户要的是手机上点一下就能换（原话「打字太不方便」）。两者的调和点是
# **config.toml 定天花板，聊天在天花板以下自由动**：
#   - 公司给同事装的时候 IT 写 `mode = "readonly"` ⇒ 他在聊天里怎么点都只读
#   - 自己机器写 `mode = "trusted"` ⇒ 想临时收紧成只读，点一下就行
# 这样"聊天超不过本机上限"这条红线成立 —— 聊天只能在授权范围内动，
# 而范围是机器主人在本机文件里定的。
MODE_RANK = {"readonly": 0, "write": 1, "trusted": 2}

# 短标签，给按钮和 /mode 用。按钮上放不下 describe() 那种长句。
MODE_LABELS = {
    "readonly": "只读",
    "write": "可改文件",
    "trusted": "全授权",
}


def clamp_mode(requested: str, ceiling: str) -> str:
    """把请求的档位收敛到不超过 ceiling。认不出的一律回最紧那档。

    fail-closed 是故意的：这个值的来源是按钮回调和落盘文件，两边都可能
    是旧的或坏的。读不懂时给只读，用户最多觉得"怎么不能改了"，
    而反过来（读不懂给全授权）是静默提权。
    """
    top = MODE_RANK.get(ceiling)
    if top is None:
        return "readonly"
    rank = MODE_RANK.get(requested)
    if rank is None:
        return "readonly"
    return requested if rank <= top else ceiling


def modes_up_to(ceiling: str) -> tuple[str, ...]:
    """这台机器允许的档位，从紧到松。给菜单列按钮用。"""
    top = MODE_RANK.get(ceiling, 0)
    return tuple(m for m, r in sorted(MODE_RANK.items(), key=lambda kv: kv[1]) if r <= top)


# 聊天里能切的模型。**必须是白名单** —— 这个字符串直接进 `claude --model` 的
# argv。不是 shell 所以没有注入，但一个拼错的名字会让每一轮都失败，
# 而用户只会看到"它不回我了"。名字取自网关 /v1/models（2026-11-04 实测 48 个，
# 这里只列日常用得上的四个，多了按钮一屏放不下）。
MODEL_CHOICES = (
    ("claude-sonnet-5-5", "均衡", "日常够用"),
    ("claude-opus-5-5", "最聪明", "难活用它，慢一些"),
    ("claude-haiku-5-5", "最快", "简单问题秒回"),
    ("claude-opus-5", "稳", "最聪明那个答不了时换这个"),
)

MODEL_LABELS = {name: label for name, label, _ in MODEL_CHOICES}


def known_model(name: str) -> bool:
    return name in MODEL_LABELS


@dataclass(frozen=True)
class Policy:
    mode: str
    workspaces: tuple[str, ...]
    default_workspace: str
    model: str
    turn_timeout_sec: int
    # 本机允许的最松档位（来自 config.toml 的 mode）。聊天里切档不能超过它。
    # 不传时等于 mode —— 老代码构造出来的 Policy 行为完全不变（切不动档）。
    ceiling_mode: str = ""

    def __post_init__(self) -> None:
        if not self.ceiling_mode:
            # frozen dataclass 只能这么写默认值
            object.__setattr__(self, "ceiling_mode", self.mode)
        if self.mode not in MODE_RANK:
            raise PolicyError(
                f"mode 只能是 readonly / write / trusted，拿到 {self.mode!r}"
            )
        if self.ceiling_mode not in MODE_RANK:
            raise PolicyError(f"ceiling_mode 不认识：{self.ceiling_mode!r}")
        if MODE_RANK[self.mode] > MODE_RANK[self.ceiling_mode]:
            # 构造时就不许越顶 —— 让越界在这里炸，而不是在某个工具调用放行之后
            raise PolicyError(
                f"mode={self.mode!r} 超过了本机上限 {self.ceiling_mode!r}"
            )
        if not self.workspaces:
            raise PolicyError("workspaces 不能为空")
        for path in self.workspaces:
            if not os.path.isabs(path):
                raise PolicyError(f"workspace 必须是绝对路径: {path!r}")
        if self.resolve_workspace(self.default_workspace) is None:
            raise PolicyError("default_workspace 不在 workspaces 里")
        if self.turn_timeout_sec <= 0:
            raise PolicyError("turn_timeout_sec 必须为正")

    def with_mode(self, requested: str) -> "Policy":
        """换档，返回**新的** Policy。超过本机上限就压到上限。

        刻意返回新对象而不是改自己：`Policy` 是 frozen 的，而 runner / 钩子
        / 审查层都各自拿着它。原地改会让"这一轮用的是哪一档"变得不可考 ——
        而那恰恰是出事后最需要说清的一件事。
        """
        target = clamp_mode(requested, self.ceiling_mode)
        if target == self.mode:
            return self
        return Policy(
            mode=target,
            workspaces=self.workspaces,
            default_workspace=self.default_workspace,
            model=self.model,
            turn_timeout_sec=self.turn_timeout_sec,
            ceiling_mode=self.ceiling_mode,
        )

    def with_model(self, name: str) -> "Policy":
        """换模型，返回新的 Policy。不认识的名字原样返回自己（调用方负责报错）。"""
        if not known_model(name) or name == self.model:
            return self
        return Policy(
            mode=self.mode,
            workspaces=self.workspaces,
            default_workspace=self.default_workspace,
            model=name,
            turn_timeout_sec=self.turn_timeout_sec,
            ceiling_mode=self.ceiling_mode,
        )

    def can_switch_mode(self) -> bool:
        """这台机器有没有第二个档位可切。只有一档时菜单里就不该出现「权限」。"""
        return len(modes_up_to(self.ceiling_mode)) > 1

    def resolve_workspace(self, candidate: str) -> str | None:
        """把请求的目录收敛到白名单里的某个 workspace，越界返回 None。

        用 realpath 比较是故意的：软链接、`..`、大小写差异都在这里被抹平，
        免得「看起来在白名单里」的路径实际指到外面。
        """
        if not candidate:
            return None
        target = os.path.realpath(os.path.expanduser(candidate))
        for allowed in self.workspaces:
            root = os.path.realpath(os.path.expanduser(allowed))
            if target == root or target.startswith(root + os.sep):
                return target
        return None

    def _scoped_read_tools(self) -> tuple[str, ...]:
        """把读类工具限定在 workspace 内。

        🔴 2026-11-03 实测：不加作用域时 agent 能读 workspace 之外的任意绝对路径
        （`Read /tmp/outside/secret.txt` 直接成功，没有 denial、没有卡片）。
        用户以为"它只能看这个项目"，而 `resolve_workspace()` 只决定"这轮在哪跑"。

        实测 `Read(<绝对路径>/**)` 这个形式有效：目录外被拒、目录内正常读
        （两边都验过，含阳性对照）。

        ⚠️ **Bash 绕不过这一层** —— `cat <目录外文件>` 实测仍能读到，
        因为命令里的路径是运行时才知道的，没法用工具作用域表达。
        那条路由 `cmdclass` 的路径检查兜（见 `cmdclass.outside_workspace`）。
        """
        # 🔴 第 16 轮：**全授权档放开到整台电脑**（用户原话：「终端历史、桌面、下载、
        # 别的项目都在外面，这些都是基本的查阅操作，为啥还有弹卡呢，要做放开到整台电脑」）。
        # `//**` 是「整台机器」的写法（`**/` 是相对工作目录的，主会话实测过差别）。
        # 凭据由 `ALWAYS_DENY` 的 `Read(//**/...)` 七条挡着，deny 优先于 allow；
        # Bash 那条路由 `cmdclass._touches_credentials` 的红线挡（点同意也不许）。
        # 只读 / 可改档**不跟着放开**：那两档的语义就是「先问你」。
        if self.mode == "trusted":
            return tuple(f"{tool}(//**)" for tool in ("Read", "Glob", "Grep"))
        scoped: list[str] = []
        for tool in ("Read", "Glob", "Grep"):
            for workspace in self.workspaces:
                scoped.append(f"{tool}({workspace.rstrip('/')}/**)")
        return tuple(scoped)

    def _scoped_write_tools(self) -> tuple[str, ...]:
        """写类工具限定在 workspace 内。写法和读类一样（`Edit(<目录>/**)`）。"""
        scoped: list[str] = []
        for tool in WRITE_EXTRA_TOOLS:
            for workspace in self.workspaces:
                scoped.append(f"{tool}({workspace.rstrip('/')}/**)")
        return tuple(scoped)

    def allowed_tools(self) -> tuple[str, ...]:
        # 读类工具带 workspace 作用域（见 _scoped_read_tools 的红字）
        tools = ("TodoWrite",) + self._scoped_read_tools() + READONLY_BASH
        if self.mode == "trusted":
            # 全授权：工具全开、**不弹审批卡片**。
            # 适合"这是我自己的机器、我信它"的场景 —— 也是 Claude Code
            # 在开发者机器上的默认体验（只读操作本来就不问）。
            #
            # 仍然保留 `disallowed_tools()` 那组硬红线（见下）：
            # 不是不信任用户，是防 prompt injection —— agent 可能读到一个仓库里的
            # 恶意 README 被诱导，而那几条（删家目录、推代码、读凭据）
            # 一旦发生就不可逆，且用户事后很难发现。
            # 全授权也守住读边界 —— "不问就做"不等于"能翻遍整台电脑"。
            # 🔴 写类工具**也带作用域**（2026-11-04 review 实测：裸 Write/Edit
            # 能写到 ~/.carher-feishu-agent/config.toml，把攻击者加进主人名单）。
            # 钩子那边还有一道 `_write_inside` 兜底（不依赖这里的作用域格式是否被 CLI 认）。
            return (
                ("Bash", "TodoWrite", "WebFetch", "WebSearch", "Task")
                + self._scoped_write_tools()
                + self._scoped_read_tools()
            )
        if self.mode == "write":
            tools += self._scoped_write_tools()
        return tools

    def disallowed_tools(self) -> tuple[str, ...]:
        return ALWAYS_DENY

    def describe(self) -> str:
        """给用户看的一句话，用于 /status。说清楚「不能做什么」比「能做什么」有用。"""
        if self.mode == "readonly":
            # 🔴 2026-11-05 用户拍板：按**真实行为**说，不说「不能改文件」。
            # 实测只读档下坚持一句会弹审批卡，点「同意」文件真的被改了
            # （走完 `ApprovalGate.approve` + resume）。原来四处文案都承诺「不能改」，
            # 而 `test_copy_quality.py` 的注释里早就写明了能改 ——
            # 说的和做的不一样，用户按「它改不了」来放心，这是安全语义上的谎。
            # 选「改说法」而不是「改行为」：改行为要连 Bash 写文件一起拒
            # （`echo > x`、`sed -i`），那样 mkdir / npm install / 跑测试写缓存全硬拒，
            # 可用性代价大得多；而「默认只看、要改先问你」本身就是个好语义。
            return "只读：默认只看 —— 能看代码、查 git、搜文件。要它改东西会先弹卡片问你，点同意才动手"
        if self.mode == "trusted":
            return (
                "全授权：不问就做。仍挡着少数不可逆操作"
                "（删家目录、git push、读 .env/私钥）"
            )
        return "可改文件：能编辑代码；仍不能 git 提交/推送、不能删文件、不能跑任意命令"


# ─────────────────── IT 统一管控：策略文件定的本机上限 ───────────────────
#
# ## 为什么有这一段（2026-11-05）
#
# `doctor-mac.sh` / `doctor-windows.ps1` 早就在向**两边**宣称锁住了：用户看到
# 「这台机器的权限由管理员统一管，改 config.toml 的 mode 不生效」，IT 看到
# 「由 IT 固定：mode=readonly」。而 `Settings.load()` 当时只打开 config.toml 一个
# 文件（2026-11-05 用 `builtins.open` 探针实测）⇒ 用户把 config.toml 的 mode 改成
# trusted 就全程无审批。**这比没有这个功能更糟**：IT 和用户都收到了「已经锁住」
# 的信号，两边都不会再去查。这几个函数就是把那句话补成真的。
#
# ## 为什么写在 policy.py 里而不是新建 bridge/itpolicy.py
#
# 新建一个 `bridge/*.py` 必须同时往两个安装器模板里加嵌入段并重出产物，
# （🔴 这里**不许**写出那个嵌入占位符的字面量：本文件会被原样嵌进安装器，
#  build.sh 出产物前查「还有没替换的占位符」，注释里一个字面量就让整次 build 失败 ——
#  2026-11-05 实测就是这么挂的。）
# 否则新机器一键装完根本没有这个文件（`tests/test_install_completeness.py`
# 2026-11-04 就是为这个建的：当时 19 个模块只嵌了 11 个）。
# 而「桥里有、用户机器上没有」正是我要修的那类失败的另一个形态 ——
# 所以这段代码放进一个**已经在两个安装器里的**文件，和 `ceiling_mode` 做邻居。
#
# ## 方向只有一条：只能更严
#
# 策略文件写 trusted、用户 config 写 readonly ⇒ 结果还是 readonly。
# IT 不替用户放开权限：放开的后果（不弹审批就改文件、跑命令）落在用户那台机器上，
# 而收紧的后果（少几个档位）用户在菜单里看得见、也知道找谁。所以这里永远取**最严**
# 的那一档 —— 不是「策略文件覆盖 config」，是「两边取紧」。
#
# ## 只认 `mode` 一个字段
#
# `workspaces` / `deny_patterns` 刻意不做：
#   - `workspaces` 由 IT 远程定会直接改变「它能看哪些目录」，而用户装机时填的那几个
#     才是他本机真实存在的路径。IT 写错一个目录 = `Policy.__post_init__` 直接
#     `PolicyError`（default_workspace 不在 workspaces 里）⇒ 整台机器的桥起不来。
#   - `deny_patterns` 只能更严、不会致命，以后真要加就在这里多一个读法。但现在没有
#     需求，少一个字段就少一处 IT 能写错的地方。
# `mode` 是**唯一已经有人（doctor 的两个脚本）向用户承诺过**的字段，先把它做实。
#
# ## 不热加载
#
# 只在 `Settings.load()` 时读一次。IT 改了策略要等桥重启才生效 —— 重启在这套东西里
# 是常态（看门狗在长连接静默时会自杀等拉起，见 prefs.py 开头）。
# 【推断：没实测过「IT 改文件」到「生效」的实际间隔。】

# 🔴 这个路径必须和 doctor 读的是同一个。两边飘开就又回到
# 「doctor 说锁了、桥没读」那个状态，而这次没人会再怀疑它。
# `tests/test_policy.py::test_policy_path_matches_what_doctor_reads` 盯着两边一致。
MAC_POLICY_FILE = "/Library/Application Support/CarHer/policy.toml"

POLICY_FILE_ENV = "CARHER_POLICY_FILE"


def system_policy_file() -> str:
    """IT 放策略文件的固定位置。

    选系统目录而不是用户目录是这个管控唯一的立足点：普通用户写不进
    `/Library/Application Support`（2026-11-05 本机实测：该目录 `root:admin 0755`，
    非 root 用户 `test -w` 为假）。放在用户家目录下的话用户自己就能改掉，
    那就又是一个「看起来锁了」的摆设。
    """
    if sys.platform.startswith("win"):
        # 和 doctor-windows.ps1 的 `Join-Path $env:ProgramData 'CarHer\\policy.toml'`
        # 同一个位置。ProgramData 没设时按默认盘符兜 —— 读不到就是「没有策略」，不会崩。
        base = os.environ.get("ProgramData") or r"C:\ProgramData"
        return os.path.join(base, "CarHer", "policy.toml")
    return MAC_POLICY_FILE


def policy_files() -> tuple[str, ...]:
    """按顺序要看的策略文件（可能不存在，读的时候再说）。

    🔴 `CARHER_POLICY_FILE` 是**追加**一个来源，不是替换系统位置。替换的话，
    用户给自己的 LaunchAgent plist 加一行环境变量就能把 IT 的策略整个绕掉 ——
    那个 plist 在他自己家目录里，改它不需要管理员权限。
    追加 + 取最严（见 `ceiling_from_files`）⇒ 这个变量只能把机器调得更紧：
    测试和 IT 调试够用，拿它提权不行。
    """
    paths = [system_policy_file()]
    override = os.environ.get(POLICY_FILE_ENV, "").strip()
    if override:
        paths.append(os.path.expanduser(override))
    return tuple(paths)


def _policy_file_modes(path: str) -> tuple[str, ...]:
    r"""文件里所有看起来是 mode 的值，**原样**返回（不校验、不排序）。

    顶层 `mode` 和任意一级表里的 `mode` 都算，不是只认顶层。这不是为了灵活，
    是为了和 doctor 对齐：doctor 两边都是**按行**抓的
    （mac `sed -n 's/^mode *= *"\(.*\)"/\1/p'`、Windows
    `Select-String '^\s*mode\s*=\s*"(.*)"'`），所以 IT 照着 config.toml 的样子写
    `[agent]` + `mode = "readonly"` 时，doctor 照样打印「由 IT 固定：mode=readonly」。
    桥这边只认顶层的话，这种写法又变成「doctor 说锁了、其实没锁」——
    同一个 bug 的第二形态。
    """
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except FileNotFoundError:
        return ()  # 绝大多数机器都走这条：没有 IT 策略，不值得记日志
    except OSError as exc:
        # 没权限读是真实情况（IT 把文件放成 0600 root）。当没有策略 ——
        # 但要留痕，否则这台机器「为什么没锁住」无从查。
        logger.warning("IT 策略文件读不了，当没有策略：%s（%s）", path, exc)
        return ()
    except tomllib.TOMLDecodeError as exc:
        logger.warning("IT 策略文件不是合法 TOML，当没有策略：%s（%s）", path, exc)
        return ()

    found: list[str] = []
    top = raw.get("mode")
    if isinstance(top, str) and top:
        found.append(top)
    for value in raw.values():
        if isinstance(value, dict):
            nested = value.get("mode")
            if isinstance(nested, str) and nested:
                found.append(nested)
    return tuple(found)


def ceiling_from_files(paths) -> str:
    """这些策略文件合起来定的上限。没有任何策略时返回空串。

    多个来源取**最严**：任何一个来源说紧就按紧的算（见 `policy_files` 的红字）。

    🔴 读到了 mode 但值不认识（`mode = "read-only"` 这种拼错）⇒ 压到 readonly，
    **不是**当没有策略。当没有策略在这里等于把一次拼错翻译成**静默放开**，
    那正是这一段要消灭的那类失败。方向和「文件坏了」刻意不同：文件坏了是根本
    没读到意图，而写了 mode 就是意图明确要收紧。readonly 不会让服务起不来，
    用户最多觉得「怎么不能改了」，然后去找 IT —— doctor 会把策略文件路径打给他。
    """
    strictest = ""
    for path in paths:
        for raw in _policy_file_modes(path):
            if raw not in MODE_RANK:
                logger.warning(
                    "IT 策略文件 %s 里的 mode=%r 不认识，按最紧的 readonly 算", path, raw
                )
                # readonly 已经是最紧的一档，没必要再看别的来源
                return "readonly"
            if not strictest or MODE_RANK[raw] < MODE_RANK[strictest]:
                strictest = raw
    return strictest


def effective_ceiling(config_mode: str, paths: tuple[str, ...] | None = None) -> str:
    """config.toml 的 mode 被 IT 策略收紧之后的本机上限。

    `config_mode` 不认识时**原样返回**，不在这里翻译成 readonly —— 那是用户的
    config.toml 写错了，该让 `Policy.__post_init__` 抛 PolicyError 把话说清楚
    （「mode 只能是 readonly / write / trusted」）。在这里悄悄改成 readonly，
    一个拼错就会表现成「它怎么不能改文件了」，而配置文件看上去毫无问题。
    """
    ceiling = ceiling_from_files(policy_files() if paths is None else paths)
    if not ceiling or config_mode not in MODE_RANK:
        return config_mode
    return clamp_mode(config_mode, ceiling)
