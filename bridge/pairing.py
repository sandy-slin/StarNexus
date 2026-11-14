"""一次性配对码：装完之后，**只有拿着码的那个人**能成为这台电脑的主人。

## 它替掉了什么

旧行为是「第一条消息的发送者自动成为主人」（autobind）。那个窗口在实际推广里几乎
必然打开：安装器只在拿不到创建者 open_id 时才降级到 autobind，而 bootstrap.py 自己
写明「拿不到是常态」（权限没审批、或接口不返回这个字段）—— 也就是说，手工建应用
那条路**默认**就是 autobind。

危害不是理论上的：自建应用对全员可见时，同租户任何人私聊这个机器人、抢在主人之前
发一条消息，就拿下了这台电脑（只读模式也能读 home 目录，输出全部回传到飞书）。
而真正的主人之后发消息会走「静默丢弃」，只会觉得机器人坏了。

## 新形状

安装器生成一个 6 位码写进 config.toml 并在终端显眼打印；机器人只在收到的消息
**正文等于那个码**时才绑定发送者。绑定后码立刻从配置里删掉。

## 几条刻意的设计

- **过期 + 次数上限。** 6 位码（31 个字符的字母表）约 8.9 亿组合，单看足够；
  但没有上限时攻击者可以慢慢猜，而且他知道窗口一直开着。30 分钟 + 5 次把这条路堵死。
- **次数要落盘。** 只记在内存里等于没有上限 —— 程序自带看门狗，长连接静默会主动
  退出等 launchd 拉起，重启一次计数就清零了。
- **不是码的消息不算一次尝试。** 否则同事随口几句「在吗」就把主人的码烧没了。
- **常量时间比较。** `==` 在第一个不同字符处短路。远程计时攻击不现实，但这是零成本
  的正确写法，没有理由不写。
- **全程静默。** 码不对、过期、用尽都不回任何话。回一句「你没权限」就等于向陌生人
  确认「这里有个机器人，背后有东西」。
- **落盘失败 ⇒ 绝不算绑上。** 最坏的结果是「干了活但没记住主人」：重启后白名单
  还是空的，下一个人又能来抢。
- **码不进日志。** 日志里只记「第 N 次失败」。
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import tomllib
import unicodedata
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("feishu-bridge.pairing")

# 去掉 0/O/1/l/I —— 用户要照着终端念/手打，混一次就白烧一次尝试次数。
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LEN = 6
DEFAULT_TTL_MIN = 30
DEFAULT_MAX_ATTEMPTS = 5
STATE_FILENAME = "pairing.state"

# 🔴 例句里**不许出现任何具体仓库名**。原来第二句写的是
# 「carher-admin 有什么没提交的改动？」—— 那是我们自己的仓库，同事机器上根本没有。
# 他照着问会得到「那个目录不在范围内」，第一印象就是"这不是给我用的"。
# 三条换成在任何仓库都成立的问法（它自己会去找项目名），
# `tests/test_copy_quality.py::test_first_message_has_no_hardcoded_repo` 盯着这条。
#
# 🔴 这三条是**实测筛过**的（2026-11-05，只读档 + 一个三项目的 workspace，各跑 3 次）：
# 三条都 3/3 直接答出来、一张审批卡都不弹。被淘汰的是「哪些仓库有没提交的改动」——
# 同样的问法 3 次里有 1 次走到 `git -C` 遍历那条路，弹一张带 shell 一行命令的审批卡
# （正文是 `for d in . */ */*/; do ...`）。开场三条的作用是让他第一次就看到"能用"，
# 第一条就被挡住教给他的是相反的事。那条问法留在菜单里（点按钮本来就是另一种预期）。
#
# 另外补了「现在是只读」那一句：装完默认 readonly（安装器写死的），
# 而用户第一件想干的事往往是"帮我改一下" —— 不先说清，他会以为机器人坏了。
# 实测只读档下要它改文件：第一轮它自己回"只读模式改不了"，用户再坚持一句就弹审批卡，
# 点同意后文件**真的被改**（走完 ApprovalGate.approve + resume 验过）。
# 所以这句话必须写"会先问你"，不能写"不能改"。
#
# 🔴 例句**只有这一份**。开场消息现在有两条路（配对码那条发 BOUND_MESSAGE、
# 扫码那条发 GREETING），两边各存一份的话改了一边另一边不变 ——
# 和 botmenu.QUESTION_TEXT 当初那个坑同一个形状（2026-11-04 review）。
# 🔴 2026-11-06 换了一版：原来三条全是程序员的话（「我有哪些项目 / 测试怎么跑 / TODO 和 FIXME」），
# 推给行政 / 产品同事时他不知道第一句该问什么（新同事侦察 Top3）。新三条照 11-05 的办法实测筛过：
# 只读档 + 带真实审批钩子环境 + 一个放了周报 / 会议纪要 / 名单的目录，每条 3 次 ——
# 三条都 3/3 直接答出来、0 张审批卡（`docs/iteration/evidence/examples-round2.txt`，脚本 `measure_examples.py`）。
# 淘汰的两条：「最近改过哪些文件」3/3 都弹一张卡（要跑 stat 那类命令）；
# 「把会议纪要整理成三条要点」3/3 反问「把内容发给我」而不是去找文件 —— 第一句就被反问，教给他的是相反的事。
# ⚠️ 第一轮量的时候忘了给钩子传审批目录，`ls` 被判成「审批目录没配置」⇒ 假红，重量了一次。
#
# 🔴 例句**不点名具体文件**（「名单」「周报」是测试文件夹里正好有的，同事电脑上不一定有 ——
# 问一个不存在的东西它回「没找到」，第一印象又是「不能用」，同 11-05「别写死仓库名」那个坑）。
# 所以换成「让它自己去找」的问法。最终三条都是**实测**（每条 3 次，钩子环境同线上）：
#   「这个文件夹里都有什么？」              3/3，0 卡，约 7~8 秒
#   「随便挑一个文档，用三句话告诉我它在讲什么」 3/3，0 卡，约 9~10 秒（Glob → Read）
#   「这里面有没有表格？里面大概是什么数据」     3/3，0 卡，约 11~15 秒
# 也量了但淘汰的：「挑一个**最近**的文件…」0/3 —— 「最近」让它去跑按时间排序的命令，每次弹一张卡。
# 一个字（「最近」）就从 3/3 变 0/3：**改例句措辞必须重量，不能凭感觉换**。
FIRST_EXAMPLES = (
    "这个文件夹里都有什么？",
    "随便挑一个文档，用三句话告诉我它在讲什么",
    "这里面有没有表格？里面大概是什么数据",
)
_EXAMPLE_LINES = "\n".join(f"· {line}" for line in FIRST_EXAMPLES)

# 「现在是哪一档」怎么说。两条开场路共用（同上，不许各写一份）。
#
# 🔴 第 17 轮：这句原来是写死的只读那一版（`_READONLY_NOTE`），而第 16 轮已经把
# 安装默认翻成了全控制 ⇒ 每个新用户收到的**第一句话**都在撒谎，而且撒在危险的方向上：
# 他以为「它动手前会问我」，于是放心把它指到一个仓库上，它不问就改。
#
# 现在按**实际档位**生成。现场数据（真跑 `approval.HOOK_SOURCE`，2026-11-07）：
#   全授权 + 工作目录内：改文件 / 删文件 / 跑命令 / git commit  全是 allow，一张卡都不弹
#   全授权 + 目录外读：  `ls ~/Desktop`、`cat ~/.zsh_history`   allow
#   全授权 + 目录外写：  `echo x > ~/Desktop/p.txt`             defer（弹卡）
#   任何档：             `git push`、`sudo`、`rm -rf ~`、读私钥  deny（点同意也不许）
#
# 🔴 措辞上的两条纪律：
# - 不写「不能改文件」那一类绝对承诺（2026-11-05 实测证伪：只读档点同意就改了）。
#   只有**点同意也不许**的那几样才敢写「永远不做」。
# - 全控制那句必须紧跟着说清挡什么 + 怎么切回只读（领导原话：「要安全的话让他
#   单独设定只读权限」），否则用户只收到「它什么都能干」，不知道边界也不知道出路。
_NOTES = {
    "readonly": (
        "现在是**只读**：能看代码、查 git、搜文件。要它改东西会先弹一张卡片问你，"
        "点「同意，去做」才动手。想让它直接动手，点「🔑 权限」放开。"
    ),
    "write": (
        "现在是**可改文件**：能改项目里的文件，每次动手前先弹一张卡片问你，"
        "点「同意，去做」才改。删家目录、推代码、读私钥这几样永远不做。"
    ),
    "trusted": (
        "现在是**全授权**：看文件、跑命令、改项目里的东西都不用再问你。"
        "删家目录、推代码、读私钥这几样永远不做；要动项目以外的东西还会先问你。"
        "想更安全，点「🔑 权限」切到「只读」，它就每步都先问你。"
    ),
}


def permission_note(mode: str) -> str:
    """开场消息里「现在是哪一档」那句。认不出的档位按最松那档说。

    🔴 兜底方向**刻意和别处的 fail-closed 相反**：说「会先问你」而实际不问，
    用户按这句话放心 —— 那是安全语义上的谎；说「不问就做」而实际会问，
    最坏只是多几张卡片。
    """
    return _NOTES.get(str(mode or ""), _NOTES["trusted"])


def bound_message(mode: str) -> str:
    """配对码那条路绑定成功后的第一条消息。

    ⚠️ 两条写法上的约束，都来自
    `tests/test_copy_quality.py::test_binding_reply_onboards_the_user` ——
    它在 pairing.py 的**源码**上从 `✅` 开始、到第一个右圆括号为止抓这条消息：
    1. 那句档位说明先放进局部变量，不要内联成函数调用（调用的右括号会把它截断在
       「菜单」那行之前 ⇒「有没有给他指路」那条判据假红）；
    2. 这段 docstring 和下面的正文里都不要出现那个正则的字面量，否则它先被抓到。
    """
    note = permission_note(mode)
    return (
        "✅ 绑好了，这台电脑现在只听你的。\n"
        "\n"
        "**试试这么问**（直接说就行，不用记命令）：\n"
        f"{_EXAMPLE_LINES}\n"
        "\n"
        f"{note}\n"
        "\n"
        "不想打字就发「菜单」，一排按钮点一下就问。完整说明点「❓ 帮助」。"
    )


# 老名字留着：`app.py`（主会话的文件）和几处测试还按常量引用。
# 🔴 它是**全控制**那一版 —— 和安装器默认一致；真实档位由
# `bound_message(policy.mode)` 给（切过档、或 IT 锁只读的机器会不一样）。
BOUND_MESSAGE = bound_message("trusted")


# 扫码装完之后的那条开场消息。
#
# 🔴 2026-11-05 实测的缺口：**推荐路径（扫码）装完，飞书里一个字都不发。**
# 扫码那条路直接拿到主人的 open_id 写进 config（`install-mac.sh` 把
# `register.py` 回来的 open_id 填进 owner_open_ids）⇒ 配对码那条路根本不走
# ⇒ `BOUND_MESSAGE` 永远发不出去，而 `run()` 里没有任何主动问候。
# 唯一的说明只在终端最后一屏，关了窗口就没了，手机上更看不到。
# 也就是说：**唯一的介绍被绑在了降级路径上**，按推荐方式装完的人打开飞书
# 什么都看不到，不知道能干什么、怎么开始。
#
# 文案和 BOUND_MESSAGE 共用例句和「现在是哪一档」那句（上面的红字）。开头一句不一样是有意的：
# 配对那条是"刚绑好"，扫码这条是"装好了"，用户在两条路上看到的是各自真实发生的事。
def greeting_message(mode: str) -> str:
    """扫码装完那条开场消息。"""
    note = permission_note(mode)
    return (
        "👋 装好了，我在你的电脑上干活，结果发回这里。\n"
        "\n"
        "**试试这么问**（直接说就行，不用记命令）：\n"
        f"{_EXAMPLE_LINES}\n"
        "\n"
        f"{note}\n"
        "\n"
        "下面的按钮点一下就能用，不想打字就点「菜单」。"
    )


# 同 BOUND_MESSAGE：老名字留着，默认那一版是全控制。
GREETING = greeting_message("trusted")

# 开场消息的落盘标记。**单独一个文件**，不并进 prefs.json / chat_binds.json /
# relay_consent.json：那三个各有自己的 schema 和"读失败一律当没设过"的约定，
# 而那条约定放到开场标记上就是"读失败 ⇒ 再发一遍"（骚扰）。
# 一个文件一个主人，也不会因为别人那份写失败把这个标记带下去。
GREETING_STATE_FILENAME = "greeting.json"

# 最多试几次。看门狗在长连接静默时会自杀等 launchd 拉起（到 2026-11-05 已重启 48 次），
# 所以"重启"是常态 —— 没有上限的话，**标记写不下去**的机器会每次重启都发一遍。
# 发送成功才写 `greeted`，所以正常情况下只会发一次；这个上限兜的是
# "发出去了但标记没落盘"那种最坏情况：最多 3 条，不是 48 条。
MAX_GREET_TRIES = 3

# 等长连接连上最多等多久 / 每隔多久看一次。
# 启动瞬间 ws 还没连上，这时候发必然失败（而失败之后标记不写 ⇒ 下次重启再试，
# 但试的次数有上限 ⇒ 不能靠"下次再说"兜着），所以这里**等连上再发**。
GREET_WAIT_SEC = 180
GREET_TICK_SEC = 2.0


def generate_code(length: int = CODE_LEN) -> str:
    """CSPRNG。不用 `random`：装机时间是可猜的，种子也就是可猜的。"""
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def _normalize(text: str) -> str:
    """宽容地认码：大小写、空格、全角都当同一个码。

    NFKC 是为了全角字符 —— 用户在飞书里从终端复制粘贴，输入法可能给出全角。
    """
    folded = unicodedata.normalize("NFKC", text or "").upper()
    return re.sub(r"[^0-9A-Z]", "", folded)


class Outcome(Enum):
    BOUND = "bound"                  # 绑定成功
    NO_WINDOW = "no_window"          # 没有待配对窗口（没码 / 已有主人）
    NOT_A_CODE = "not_a_code"        # 这条消息根本不是码，不计次数
    WRONG = "wrong"                  # 码不对，计一次
    EXPIRED = "expired"              # 码过期了
    EXHAUSTED = "exhausted"          # 尝试次数用尽，窗口作废
    PERSIST_FAILED = "persist_failed"  # 码对，但写不进配置 ⇒ 不算绑上


@dataclass(frozen=True)
class PairingResult:
    outcome: Outcome
    owner_open_id: str = ""
    reply: str = ""
    attempts_used: int = 0

    @property
    def bound(self) -> bool:
        return self.outcome is Outcome.BOUND

    @property
    def should_run(self) -> bool:
        """配对消息永远不当任务执行 —— 它只是个码。"""
        return False


def _atomic_write(path: str, text: str) -> None:
    """写临时文件 + 原子改名。中途断电也不会留下半截配置（那会让下次启动起不来）。

    权限跟着原文件走：配置里有 app_secret，重写不能把它放宽。
    """
    directory = os.path.dirname(path) or "."
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=directory, delete=False
        ) as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)  # 不留垃圾，否则下次重写会看到一堆半成品
            except OSError:
                pass
        raise


def _strip_pairing_keys(text: str) -> str:
    """把配对相关的行从配置里删掉。一次性 = 重启后也不能再用。"""
    return re.sub(
        r"^pairing_(code|issued_at|ttl_min|max_attempts)\s*=.*\n?",
        "",
        text,
        flags=re.M,
    )


def _write_owner(config_path: str, open_id: str) -> bool:
    """把主人写进白名单并删掉配对码，一次原子写完成。

    返回 False 时调用方**必须**当作没绑上。
    """
    if not open_id or '"' in open_id or "\n" in open_id:
        logger.error("open_id 形状不对，拒绝写入配置")
        return False
    try:
        with open(config_path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        logger.exception("读不到配置文件，无法绑定主人")
        return False

    # 先 assert 再写：算出来的 old 匹配不到时必须报错，不能静默什么都没改。
    replaced, count = re.subn(
        r"^owner_open_ids\s*=\s*\[\s*\]\s*$",
        f'owner_open_ids = ["{open_id}"]',
        text,
        count=1,
        flags=re.M,
    )
    if count != 1:
        logger.error("配置里没有空的 owner_open_ids 可写（匹配到 %d 处）", count)
        return False

    replaced = _strip_pairing_keys(replaced)
    try:
        _atomic_write(config_path, replaced)
    except Exception:
        logger.exception("写配置失败")
        return False
    return True


def _owners_of(owner_open_ids) -> tuple[str, ...]:
    """主人名单 → 去空、排序的元组。

    允许传一个**可调用对象**：`Settings.owner_open_ids` 在配对成功时会被整个换掉
    （`app.on_message` 里那句赋值），捕获住 frozenset 本身的话，等连接那几十秒里
    发生的绑定就看不见了 —— 而"等待期间主人变了"正是要重新判的那一条。
    """
    if callable(owner_open_ids):
        try:
            owner_open_ids = owner_open_ids()
        except Exception:  # noqa: BLE001 —— 读不到就按"还没有主人"处理，不发
            logger.debug("读主人名单失败", exc_info=True)
            return ()
    return tuple(sorted(str(o) for o in (owner_open_ids or ()) if o))


class GreetingState:
    """「开场消息发过了吗」的落盘标记。

    为什么必须落盘：看门狗在长连接静默时会自杀等 launchd 拉起，到 2026-11-05
    已经重启 48 次。只记内存 ⇒ 每次重启都发一遍开场 = 48 条骚扰。

    为什么单独一个文件（不并进 prefs.json / chat_binds.json / relay_consent.json）：
    那三个的共同约定是"读失败一律当没设过、回落默认值"，而这条约定套到开场标记上
    就变成"读不出来就再发一遍"。这里要的是反过来的默认值，混在一起迟早被改回去。

    读失败时按**最保守**处理：当成已经发过（`greeted=True`）。
    代价是最坏少发一条介绍，而反过来是每次重启骚扰一次 —— 后者严重得多。
    """

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            # 文件不存在 = 还没发过。这是唯一能确定"没发过"的情形。
            return {}
        except (OSError, ValueError):
            # 读得到文件但读不懂（半截 JSON、权限问题）⇒ 当成发过了，别骚扰。
            logger.warning("开场标记读不出来，按已发过处理：%s", self._path)
            return {"greeted": True}
        return data if isinstance(data, dict) else {"greeted": True}

    def _write(self, state: dict) -> bool:
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            _atomic_write(self._path, json.dumps(state))
            return True
        except Exception:  # noqa: BLE001 —— 落盘失败不能让启动挂掉
            logger.exception("开场标记落盘失败：%s", self._path)
            return False

    @property
    def greeted(self) -> bool:
        return bool(self._read().get("greeted"))

    @property
    def tries(self) -> int:
        try:
            return int(self._read().get("tries") or 0)
        except (TypeError, ValueError):
            return 0

    def note_attempt(self) -> int:
        """记一次「准备发了」。**必须在发之前调用**。

        🔴 顺序是有意的：放到发送之后的话，"发出去了但进程紧接着被看门狗打死"
        这一种情况下次数永远不增长 ⇒ 重启几十次就发几十条。
        先记次数、再发，最坏是少发一条（而上限 3 次也兜住了标记写不下去的机器）。
        """
        with self._lock:
            state = self._read()
            try:
                tries = int(state.get("tries") or 0) + 1
            except (TypeError, ValueError):
                tries = 1
            state["tries"] = tries
            self._write(state)
            return tries

    def mark_sent(self) -> None:
        """开场消息确认送达了（或者配对那条路已经发过 BOUND_MESSAGE）。

        **只在真的发出去之后调用。** 发送失败就别写 —— 写了就等于永远不发了，
        而那正是我们要修的那个 bug 的另一半。
        """
        with self._lock:
            state = self._read()
            state["greeted"] = True
            state.setdefault("tries", 0)
            self._write(state)

    def should_greet(self, owner_open_ids) -> str:
        """现在该发开场吗。返回空串 = 该发；否则是不发的原因（给日志和测试看）。"""
        if not _owners_of(owner_open_ids):
            # 还没有主人（配对码那条路还没走完）⇒ 发给谁？什么都不做。
            return "no_owner"
        state = self._read()
        if state.get("greeted"):
            return "already_greeted"
        try:
            tries = int(state.get("tries") or 0)
        except (TypeError, ValueError):
            tries = 0
        if tries >= MAX_GREET_TRIES:
            return "tries_exhausted"
        return ""


def greeting_buttons(mode: str = "") -> list:
    """开场消息下面那排按钮。

    开场三条例句的作用是"让他第一次就看到能用"，而手机上打字最费劲 ——
    所以第一屏就给按钮，点一下就有东西发生，不用先读完一段说明再想怎么开始。
    只放**点了不会让电脑上发生任何事**的（同 `fleet/actions.py` 那份白名单的判据）：
    菜单是主入口、换项目回答"它在看哪"、帮助是全文。

    🔴 第 17 轮多一个「🔒 切到只读」（只在不是只读档时给）。理由是第 16 轮留下的缺口：
    那个一键按钮原来**只挂在「切到全授权」之后那条回复上**，而默认就是全控制的人
    从来不走那一步 ⇒ 他永远看不到它，切只读得自己翻「🔑 权限」卡。
    方向是收紧，点错了最坏只是多问几次（放开仍然要走二次确认卡），所以
    放进开场不违反上面那条「点了不让电脑上发生任何事」的精神。
    """
    from .fleet.actions import do_button, readonly_button

    buttons = [
        do_button("menu", primary=True),
        do_button("dirs"),
        do_button("help"),
    ]
    if str(mode or "") != "readonly":
        buttons.append(readonly_button("🔒 切到只读"))
    return buttons


def greet_once(
    state: GreetingState,
    *,
    owner_open_ids,
    connection_alive,
    send,
    mode: str = "",
    wait_sec: float = GREET_WAIT_SEC,
    tick_sec: float = GREET_TICK_SEC,
    sleep=time.sleep,
) -> str:
    """装完第一次打开飞书就能看到的那条话。返回一个结果词（日志 + 测试判据）。

    `send(receiver, text, buttons) -> bool` 由调用方给（签名就是
    `Bridge._say_with_buttons`），收件人是主人的 open_id —— 扫码那条路手里只有它、
    没有 chat_id，而 `reply._id_type` 会按 `ou_` 前缀切成
    `receive_id_type=open_id`（和常驻菜单那条路同一个做法）。

    🔴 **等连上再发**。启动瞬间 ws 还没连上，这时候发必然失败；而"失败就算了、
    下次重启再试"配合次数上限会把 3 次机会在几秒内烧光（看门狗重启很快）。
    所以这里轮询连接状态最多等 `wait_sec`，连上了才发。
    探活抛异常按"还没连上"处理 —— 这是启动路径，不能让它把线程带走。

    `mode` = 这台机器现在真正生效的档位，决定开场怎么说权限、给不给「切到只读」按钮。
    ⚠️ 不传时按安装器默认（全控制）说 —— 调用方（`app.Bridge._greet_in_background`）
    **应该传 `self.policy.mode`**，否则 IT 锁只读的机器开场会说成全控制。
    那个文件不归本角色改，已写进报告交给主会话。
    """
    reason = state.should_greet(owner_open_ids)
    if reason:
        return reason

    # 先查一次再睡（不是先睡再查）：连接已经好了的常见情况下不该白等一个 tick。
    waited = 0.0
    alive = False
    while True:
        try:
            alive = bool(connection_alive())
        except Exception:  # noqa: BLE001 —— 探活失败不该让开场把线程带走
            logger.debug("探测长连接状态失败", exc_info=True)
            alive = False
        if alive or waited >= wait_sec:
            break
        sleep(tick_sec)
        waited += max(tick_sec, 0.001)  # tick 给 0 也不能变成死循环
    if not alive:
        # 一直没连上：**不记次数、不写标记**，下次重启再来。
        # 连不上是机器/凭据的问题（凭据被拒重启也治不好），不该消耗开场的重试额度 ——
        # 否则一台连不上的机器会在 3 次重启里把额度烧光，之后真连上了也永远不发。
        logger.warning("长连接等了 %.0f 秒还没通，开场消息这次不发，下次启动再试", waited)
        return "no_connection"

    # 再查一次：等待期间主人可能被别的路径改了（配对绑定、或 BOUND_MESSAGE 已发过）
    reason = state.should_greet(owner_open_ids)
    if reason:
        return reason

    state.note_attempt()
    try:
        buttons = greeting_buttons(mode)
    except Exception:  # noqa: BLE001 —— 按钮是锦上添花，话本身要发出去
        logger.exception("开场按钮构造失败，只发文字")
        buttons = []
    text = greeting_message(mode)
    sent = False
    for owner in _owners_of(owner_open_ids):
        try:
            sent = bool(send(owner, text, buttons)) or sent
        except Exception:  # noqa: BLE001 —— 飞书接口超时 / 网络抖动
            logger.exception("开场消息发送抛异常")
    if not sent:
        # 没送达就不写标记 ⇒ 下次重启还会试（最多 MAX_GREET_TRIES 次）。
        # 🔴 反过来写就是「标记已写但消息没发出去 = 永远不发了」。
        logger.warning("开场消息没送达，下次启动再试（已试 %d 次）", state.tries)
        return "send_failed"
    state.mark_sent()
    logger.info("开场消息已发给主人")
    return "sent"


def greet_in_background(
    state: GreetingState,
    *,
    owner_open_ids,
    connection_alive,
    send,
    **kwargs,
) -> threading.Thread:
    """把 `greet_once` 丢到 daemon 线程里跑。

    不能在 `run()` 里同步跑：它要等长连接连上（最多 3 分钟），
    而那段时间 `loop.run_forever()` 还没起来 —— 同步等就等于启动卡住，
    连不上的机器会卡满 3 分钟才开始服务。
    """

    def _body() -> None:
        try:
            greet_once(
                state,
                owner_open_ids=owner_open_ids,
                connection_alive=connection_alive,
                send=send,
                **kwargs,
            )
        except Exception:  # noqa: BLE001 —— 线程边界，什么都不能往外抛
            logger.exception("开场消息线程异常退出")

    thread = threading.Thread(target=_body, daemon=True, name="greeting")
    thread.start()
    return thread


class PairingGate:
    """配对窗口。整个生命周期：装机写码 → 用户发码 → 绑定 → 窗口永久关闭。"""

    def __init__(
        self,
        config_path: str,
        *,
        code: str = "",
        issued_at: float = 0.0,
        ttl_sec: int = DEFAULT_TTL_MIN * 60,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        owner_already_set: bool = False,
        state_path: str = "",
        mode: str = "",
    ) -> None:
        self.config_path = config_path
        # 绑定成功那条消息里「现在是哪一档」要说对（见 `permission_note` 的红字）。
        # 空串 ⇒ 按安装器默认（全控制）说。
        self.mode = str(mode or "")
        self._code = _normalize(code)
        self.issued_at = float(issued_at or 0)
        self.ttl_sec = int(ttl_sec)
        self.max_attempts = int(max_attempts)
        self._owner_already_set = owner_already_set
        self._mem_failed = 0
        self._lock = threading.Lock()
        # 计数落盘在配置旁边：进程重启（看门狗会主动退出等拉起）不能让上限清零。
        self.state_path = state_path or os.path.join(
            os.path.dirname(os.path.abspath(config_path)) or ".", STATE_FILENAME
        )

    # ---------- 构造 ----------

    @classmethod
    def load(cls, config_path: str) -> "PairingGate":
        """从 config.toml 读出窗口状态。读不到配置 = 窗口关着（fail-closed）。"""
        try:
            with open(config_path, "rb") as handle:
                raw = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            logger.warning("读不到配置，配对窗口按关闭处理")
            raw = {}
        feishu = raw.get("feishu", {}) if isinstance(raw, dict) else {}

        def _int(key: str, default: int) -> int:
            try:
                return int(feishu.get(key) or default)
            except (TypeError, ValueError):
                return default

        # 配对只在**刚装完还没主人**时走得到，这时聊天里还没切过档，
        # 所以配置里的起步档就是绑定那一刻真正生效的档（IT 策略文件只会更紧 ⇒
        # 那种机器照旧说只读，由 `Settings.load` 那条路管，这里取不到也不会说松）。
        agent = raw.get("agent", {}) if isinstance(raw, dict) else {}
        return cls(
            config_path,
            code=str(feishu.get("pairing_code") or ""),
            issued_at=float(feishu.get("pairing_issued_at") or 0),
            ttl_sec=_int("pairing_ttl_min", DEFAULT_TTL_MIN) * 60,
            max_attempts=_int("pairing_max_attempts", DEFAULT_MAX_ATTEMPTS),
            owner_already_set=bool(feishu.get("owner_open_ids") or []),
            mode=str(agent.get("mode") or "") if isinstance(agent, dict) else "",
        )

    @classmethod
    def reissue(cls, config_path: str, *, ttl_min: int = DEFAULT_TTL_MIN) -> str:
        """重新生成一个码（码过期/用尽之后的出路，不用重装）。

        已经有主人时抛 PermissionError —— 否则这就是一条提权后门：
        任何能写配置的路径都能重新打开「谁发码谁是主人」的窗口。
        """
        with open(config_path, "rb") as handle:
            raw = tomllib.load(handle)
        if (raw.get("feishu", {}) or {}).get("owner_open_ids"):
            raise PermissionError("已经绑定过主人了，不能重开配对窗口")

        with open(config_path, encoding="utf-8") as handle:
            text = handle.read()
        code = generate_code()
        body = _strip_pairing_keys(text)
        block = (
            f'pairing_code = "{code}"\n'
            f"pairing_issued_at = {int(time.time())}\n"
            f"pairing_ttl_min = {int(ttl_min)}\n"
            f"pairing_max_attempts = {DEFAULT_MAX_ATTEMPTS}\n"
        )
        body, count = re.subn(
            r"^(owner_open_ids\s*=.*\n)", r"\1" + block, body, count=1, flags=re.M
        )
        if count != 1:
            raise ValueError("配置里找不到 owner_open_ids，没法写配对码")
        _atomic_write(config_path, body)

        gate = cls(config_path)
        gate._reset_state()
        return code

    # ---------- 状态 ----------

    @property
    def is_open(self) -> bool:
        """还能配对吗。码、主人、过期、次数四个条件都要过。"""
        if self._owner_already_set or not self._code:
            return False
        if self._expired() or self.failed_attempts >= self.max_attempts:
            return False
        return True

    def _expired(self) -> bool:
        # 没有签发时间 ⇒ 当成过期。fail-closed：绝不把它读成「永久有效」。
        if self.issued_at <= 0:
            return True
        return (time.time() - self.issued_at) > self.ttl_sec

    def _read_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_state(self, state: dict) -> None:
        try:
            _atomic_write(self.state_path, json.dumps(state))
            os.chmod(self.state_path, 0o600)
        except Exception:
            # 写不下也不能让上限失效 ⇒ 内存计数兜住本进程。
            logger.exception("写配对尝试计数失败，本进程改用内存计数")

    @property
    def failed_attempts(self) -> int:
        """落盘计数和内存计数取大。

        落盘是为了挡「杀进程清零」（程序自带看门狗，长连接静默时会主动退出等
        launchd 拉起 —— 只记内存等于没有上限）。内存是为了兜「落盘失败」。
        """
        try:
            disk = int(self._read_state().get("failed") or 0)
        except (TypeError, ValueError):
            disk = 0
        return max(disk, self._mem_failed)

    def _reset_state(self) -> None:
        self._mem_failed = 0
        try:
            if os.path.exists(self.state_path):
                os.unlink(self.state_path)
        except OSError:
            pass

    def _note_failure(self, message_id: str) -> int:
        """记一次失败。同一个 message_id 只记一次 —— 飞书是至少一次投递，
        平台重推同一条错码不能算两次（否则重推能把主人的码烧光）。
        """
        state = self._read_state()
        seen = state.get("seen")
        seen = [str(x) for x in seen] if isinstance(seen, list) else []
        if message_id and message_id in seen:
            return self.failed_attempts
        try:
            failed = int(state.get("failed") or 0) + 1
        except (TypeError, ValueError):
            failed = 1
        failed = max(failed, self._mem_failed + 1)
        self._mem_failed = failed
        if message_id:
            seen = (seen + [message_id])[-32:]
        self._write_state({"failed": failed, "seen": seen})
        return failed

    # ---------- 判一条消息 ----------

    def consider(
        self, open_id: str, text: str, message_id: str = ""
    ) -> PairingResult:
        """这条消息是配对码吗？是且有效就绑定发送者为主人。

        **只在白名单为空时才该调用。** 所有拒绝分支的 `reply` 都是空串：
        未绑定状态下不回任何话，回了就等于向陌生人确认「这里有个机器人」。

        整个判断 + 落盘在一把锁里：事件是从 lark 的长连接线程进来的，两条消息
        同时到达时，「查窗口 → 写配置」之间会有缝 —— 两个人都拿着码时后写的会
        盖掉先写的。进程只会跑一个（文件锁硬挡），所以一把线程锁就够。
        """
        with self._lock:
            return self._consider_locked(open_id, text, message_id)

    def _consider_locked(
        self, open_id: str, text: str, message_id: str
    ) -> PairingResult:
        if self._owner_already_set or not self._code:
            return PairingResult(Outcome.NO_WINDOW)

        candidate = _normalize(text)
        # 长度不对就根本不是在配对 —— 不计次数，否则同事几句闲聊就把码烧没了。
        if len(candidate) != len(self._code):
            return PairingResult(Outcome.NOT_A_CODE)

        # 🔴 顺序有意：先判用尽、再判过期，**都在比较码之前**。
        # 放到后面的话，「次数用尽后给对码」会先匹配成功再被拦，任何一处早返回
        # 漏掉就变成提权。
        used = self.failed_attempts
        if used >= self.max_attempts:
            logger.warning("配对尝试已达上限（%d 次），窗口作废，拒绝绑定", used)
            return PairingResult(Outcome.EXHAUSTED, attempts_used=used)
        if self._expired():
            logger.warning("配对码已过期，拒绝绑定")
            return PairingResult(Outcome.EXPIRED, attempts_used=used)

        # 常量时间比较：`==` 在第一个不同字符处短路。
        if not hmac.compare_digest(candidate, self._code):
            used = self._note_failure(message_id)
            # 只记次数，绝不记码本身（码进日志就是泄漏）。
            logger.warning("配对失败第 %d 次（上限 %d）", used, self.max_attempts)
            return PairingResult(Outcome.WRONG, attempts_used=used)

        # 码对了。**先落盘再宣布成功** —— 绝不能出现「说绑上了但没记住主人」，
        # 那样重启后白名单还是空的，下一个人又能来抢。
        if not _write_owner(self.config_path, open_id):
            logger.error("配对码正确但写配置失败，不算绑定（码仍然有效，可重试）")
            return PairingResult(Outcome.PERSIST_FAILED, attempts_used=used)

        self._code = ""          # 内存里也立刻作废，别等重启
        self._owner_already_set = True
        self._reset_state()
        logger.warning("配对成功，主人已绑定：%s", open_id)
        return PairingResult(
            Outcome.BOUND,
            owner_open_id=open_id,
            reply=bound_message(self.mode),
            attempts_used=used,
        )


def main(argv: list[str]) -> int:
    """`python -m bridge.pairing reissue <config.toml>` —— 码过期后重新生成。

    给用户一条命令行出路，不用重跑整个安装器。两个安装器最后那屏都打印这条。
    """
    if len(argv) < 3 or argv[1] != "reissue":
        print("用法: python -m bridge.pairing reissue <config.toml>")
        return 2
    config_path = argv[2]
    try:
        code = PairingGate.reissue(config_path)
    except PermissionError as exc:
        print(f"✗ {exc}")
        print("  要换主人：先手动把 config.toml 里的 owner_open_ids 改成 []，再跑一次。")
        return 1
    except (OSError, ValueError) as exc:
        print(f"✗ 生成失败：{exc}")
        return 1
    print()
    print("  在飞书里把这个码发给你的机器人（30 分钟内有效，试错 5 次作废）：")
    print()
    print(f"          {code}")
    print()
    print("  改完要重启服务才生效：")
    print("    mac      launchctl kickstart -k gui/$(id -u)/com.carher.feishu-agent")
    print("    Windows  Stop-ScheduledTask -TaskName CarHerFeishuAgent; "
          "Start-ScheduledTask -TaskName CarHerFeishuAgent")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv))
