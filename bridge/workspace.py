"""工作目录：`/cd` 去哪、`/dirs` 能去哪、以及「这个会话上次在哪」。

为什么要单独补这个文件：`Policy.workspaces` 是个**元组**，`resolve_workspace()`
写好了却**零调用方**（2026-11-03 grep 确认，只有 `__post_init__` 自己校验用了它），
而 `Session.workspace` 从 `default_workspace` 设一次之后**永不改变** —— 配了多个
目录也只能用第一个，用户想问另一个仓库的事只能去改 config.toml 再重启服务。

四条设计红线，每条都对应一个具体后果：

1. **路径收敛只有一个实现，就是 `Policy.resolve_workspace()`。** 这里绝不自己拼
   `startswith` 判越界 —— 那个函数用 realpath 比较，软链、`..`、同前缀兄弟目录
   （`/tmp/ws-evil` 不在 `/tmp/ws` 里）都已经抹平。多一份实现就多一个绕过口。

2. **先判越界，再判存不存在。** 顺序反了整个 `/cd` 就是个文件系统探测器：白名单
   外的路径会因为「不存在」和「不是目录」拿到不同回话，等于把外面的目录结构
   一句一句念给飞书听。飞书是外部系统，发出去就等于发布了。

3. **模糊匹配到多个绝不自己挑一个。** 挑错的后果是他在**另一个仓库**上干活，而
   回话里只有「已切到 xxx」一行 —— 他不会立刻发现，等发现时改动已经落在错的地方。

4. **切到哪儿必须落盘。** 看门狗在长连接静默时会自杀等重启（lark-oapi #169），
   内存状态活不过一次重启 ⇒ 他每次都被弹回默认目录，而他根本不知道发生了什么。
   落盘失败也**不许静默成功**：那等于悄悄降级成「这次能用、下次没了」。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time

from dataclasses import dataclass

from .policy import Policy

logger = logging.getLogger("feishu-bridge")

# 模糊匹配往下钻几层。3 层能覆盖 `~/codes/<repo>`、`~/work/<客户>/<repo>`
# 这类常见摆法；再深就不是"项目名"而是在遍历源码树了 —— 一个 node_modules
# 就能让一次 `/cd` 扫掉几万个目录，把飞书那条 3 秒纪律直接撞掉。
MAX_SCAN_DEPTH = 3

# 一次扫描最多看多少个目录项。深度限不住横向爆炸（`~` 当 workspace 时，
# 家目录下面一层就可能有上千个目录），所以另加一个硬上限。
MAX_SCAN_ENTRIES = 4000

# 候选列表给用户看几条。超了只说「还有 N 个」—— 列二十条让他自己数不如让他打全一点。
MAX_CANDIDATES_SHOWN = 8

# `/dirs` 列几个仓库。按最近修改排序，所以前几条就是他最可能要去的。
MAX_DIRS_LISTED = 15

# 扫描时跳过的目录名。这些里面不会有"用户想切进去的项目"，但个个都能把
# 目录数撑爆（node_modules 尤其）。注意这只影响**模糊匹配和列目录**，
# 不影响用户显式打出完整路径 —— 他要进 node_modules 是他的事。
_SKIP_DIRS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".next", ".nuxt",
    "target", "vendor", ".tox", ".gradle", "Library", ".Trash", ".cache",
    "site-packages", ".idea", ".vscode",
})


class Outcome:
    """`/cd` 的结果类型。用字符串是为了日志和测试里一眼能看懂。"""

    OK = "ok"                    # 切换成功
    SAME = "same"                # 已经在这儿了，什么都没变
    OUTSIDE = "outside"          # 越界 —— 白名单外，或根本解析不出来
    MISSING = "missing"          # 白名单内但目录不存在
    NOT_DIR = "not_dir"          # 白名单内但是个文件
    NO_ACCESS = "no_access"      # 白名单内、是目录，但读不进去
    AMBIGUOUS = "ambiguous"      # 模糊匹配命中多个，要他自己选
    EMPTY = "empty"              # `/cd` 后面没给东西
    SAVE_FAILED = "save_failed"  # 切了但没落盘 ⇒ 重启会丢，必须说出来


@dataclass(frozen=True)
class CdResult:
    """一次 `/cd` 的全部结果。`reply` 可以直接发给飞书。

    `workspace` 只在 `ok` / `same` 时有值 —— 其余情况调用方**不要**拿它去跑
    任务。把"成了没有"和"切到哪儿"放在两个字段里是故意的：调用方只判
    `if result.ok:` 就不会误用，比返回 `str | None` 再靠调用方记得判空安全。
    """

    outcome: str
    reply: str
    workspace: str = ""
    candidates: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """能不能拿 `workspace` 去跑任务。

        `SAVE_FAILED` 也算 ok —— 目录确实切了，只是没记住。瞒着他更糟：
        他会以为重启后还在这儿。
        """
        return self.outcome in {Outcome.OK, Outcome.SAME, Outcome.SAVE_FAILED}


# 这个模块认领的命令。放在模块里而不是 app.py 里，是为了加命令时只动一个文件。
COMMANDS = frozenset({"/cd", "/dirs", "/ls", "/pwd", "/where"})

# 原来这里有一份 `HELP_LINES`（叫人打 `/dirs`、`/cd carher-admin`），零调用方，
# 而且例句写死了我们自己的仓库名 —— 第 11 轮删掉。帮助全文只有一份：`menu.HELP`。

# 菜单 / 按钮上「换项目」那个按钮的字。回话里叫他去点的就是它，两边必须一致。
DIRS_BUTTON = "「📂 换项目」"


def _looks_like_path(raw: str) -> bool:
    """这串东西是"路径"还是"项目名"。

    判据只看**写法**，不看磁盘：带 `/`、以 `~` 或 `.` 开头 ⇒ 他心里想的是一个
    具体位置，应当严格按路径解析、不做模糊匹配（猜一个别的目录给他更糟）。
    反之 `carher-admin` 这种裸名字才进模糊匹配。
    """
    return (
        raw.startswith("/")
        or raw.startswith("~")
        or raw.startswith(".")
        or "/" in raw
    )


def _readable_dir(path: str) -> bool:
    """能不能真的进去看。

    要 R_OK **和** X_OK：目录少了执行位就进不去（列得出名字、读不了内容），
    这种半通状态下让 agent 在里面跑，它会一路报"文件不存在"，而真因是权限。
    """
    return os.access(path, os.R_OK | os.X_OK)


def _scan(policy: Policy, want_git_only: bool) -> list[str]:
    """在**白名单内**广度优先扫目录，返回 realpath 列表。

    两条非功能性约束比"扫得全"更重要：

    - **起点只能是 `policy.workspaces`**，且每一条都先过 `resolve_workspace()`。
      配置可能写了个已经不存在、或指向白名单外软链的根，不能因为它在配置里
      就当它可信。
    - **带预算**（深度 + 总目录项数）。`~` 当 workspace 是安装器的默认回退，
      家目录下无预算地递归会把一次 `/cd` 变成几万次 stat，撞飞书 3 秒纪律。

    `want_git_only=True` 时只收有 `.git` 的目录（`/dirs` 用），但**仍然继续往下钻** ——
    `~/work/<客户>/<repo>` 这种摆法里，中间那层没有 `.git`。
    """
    roots: list[str] = []
    for configured in policy.workspaces:
        root = policy.resolve_workspace(configured)
        if root and os.path.isdir(root) and _readable_dir(root):
            roots.append(root)

    found: list[str] = []
    seen: set[str] = set()
    # 🔴 白名单根自己也算候选。安装器只问一个目录，而用户填的那个常常**就是**
    # 那个仓库（`~/codes/carher-admin`）⇒ 只收集子目录的话，`/cd carher-admin`
    # 一个都匹不上，而他明明就在里面。
    for root in roots:
        seen.add(root)
        if not want_git_only or os.path.exists(os.path.join(root, ".git")):
            found.append(root)

    budget = MAX_SCAN_ENTRIES
    # (目录, 还能往下几层)
    queue: list[tuple[str, int]] = [(r, MAX_SCAN_DEPTH) for r in roots]

    while queue and budget > 0:
        current, depth = queue.pop(0)
        if depth <= 0:
            continue
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            # 读不进去就跳过。这里不能抛 —— 白名单里有一个坏目录不该让
            # 整个 /dirs 变成"我这边出错了"。
            continue
        for entry in entries:
            budget -= 1
            if budget <= 0:
                logger.info("目录扫描触达预算上限 %d，结果可能不全", MAX_SCAN_ENTRIES)
                break
            if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            # 🔴 软链一律不跟。`~/codes/x -> /etc` 这种链子跟下去，realpath
            # 会把它收敛掉（resolve_workspace 返回 None），但在那之前已经
            # 白扫了一遍外面的目录树。直接不跟，连扫都不扫。
            path = os.path.join(current, entry.name)
            real = os.path.realpath(path)
            if policy.resolve_workspace(real) is None:
                continue
            if real in seen:
                continue
            seen.add(real)
            # `.git` 用 exists 不用 isdir：git worktree 和 submodule 里
            # `.git` 是**一个文件**（内容是 `gitdir: …`），按 isdir 判会把
            # 这些目录整个漏掉，而用户照样会想切进去。
            if not want_git_only or os.path.exists(os.path.join(real, ".git")):
                found.append(real)
            queue.append((real, depth - 1))
    return found


def _fuzzy_match(policy: Policy, name: str) -> list[str]:
    """按项目名在白名单内找候选。优先级：同名 > 唯一前缀 > 包含。

    分三档而不是一把 `in` 匹配，是因为**精确同名必须压掉模糊命中**：
    同时有 `carher` 和 `carher-admin` 时，`/cd carher` 应当去 `carher`，
    而不是报"有两个候选"让他再打一遍 —— 他已经打准了。
    """
    needle = name.lower()
    pool = _scan(policy, want_git_only=False)

    exact = [p for p in pool if os.path.basename(p).lower() == needle]
    if exact:
        # 同名可能有多个（`~/codes/foo` 和 `~/work/foo`），这时该让他选，
        # 所以这里不截断成一个。
        return sorted(set(exact))

    prefix = [p for p in pool if os.path.basename(p).lower().startswith(needle)]
    if prefix:
        return sorted(set(prefix))

    contains = [p for p in pool if needle in os.path.basename(p).lower()]
    return sorted(set(contains))


class WorkspaceStore:
    """记住每个 scope 在哪个目录。落盘，重启还在。

    为什么必须落盘：看门狗在长连接静默时会**自杀等重启**（lark-oapi #169 的
    唯一保险），所以"重启"在这套东西里是常态不是例外。只记内存 ⇒ 他昨天切到
    `~/work/x`、今天第一条消息就在默认目录跑了，而回话里不会有任何提示。

    写法抄 `app.py` 的 `_save_paused()`：临时文件 + `os.replace()`。直接 `open(w)`
    的话，中途断电会留下半截 JSON，下次启动读失败 ⇒ 所有会话的目录一起丢。

    读失败**一律当空**：这个文件是便利设施，不是权威。读不出来就回默认目录，
    不能让它阻塞启动（启动不了比回默认目录严重得多）。
    """

    def __init__(self, path: str, policy: Policy) -> None:
        self._path = path
        self._policy = policy
        self._by_scope: dict[str, str] = {}
        self.last_save_ok = True  # 最近一次落盘成不成。调用方据此决定要不要告诉用户
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            self._by_scope = {}
            return
        if not isinstance(data, dict):
            self._by_scope = {}
            return
        # 🔴 落盘的路径**必须重新过一遍白名单**，不能信磁盘。
        # config.toml 里的 workspaces 会被改小（用户收窄授权、或安装器重跑覆盖），
        # 而这个文件还留着旧目录 —— 直接采信等于让一次旧的 /cd 永久绕过新边界。
        clean: dict[str, str] = {}
        for scope, path in data.items():
            resolved = self._policy.resolve_workspace(str(path))
            if resolved:
                clean[str(scope)] = resolved
            else:
                logger.info("丢弃落盘的工作目录（已不在白名单内）scope=%s", scope)
        self._by_scope = clean

    def _save(self) -> bool:
        """返回落盘成不成 —— 调用方要把失败说给用户，不许静默降级。"""
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=directory or None, delete=False
            ) as tmp:
                json.dump(self._by_scope, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self._path)
            return True
        except OSError:
            logger.exception("工作目录落盘失败：%s", self._path)
            return False

    def get(self, scope: str) -> str:
        """这个 scope 该在哪。没记过、或记的已经越界 ⇒ 回默认目录。"""
        remembered = self._by_scope.get(scope)
        if remembered and self._policy.resolve_workspace(remembered):
            return remembered
        return self._policy.resolve_workspace(self._policy.default_workspace) or (
            self._policy.default_workspace
        )

    def set(self, scope: str, workspace: str) -> bool:
        """记下来。**自己再过一遍白名单** —— 这是最后一道闸门。

        调用方（`handle`）已经判过一次，这里重复判是故意的：以后万一有别的
        调用方直接 set，不能靠"调用方记得先校验"。
        """
        resolved = self._policy.resolve_workspace(workspace)
        if resolved is None:
            logger.warning("拒绝记录越界的工作目录 scope=%s", scope)
            self.last_save_ok = True  # 没写盘，但不是落盘失败
            return False
        self._by_scope[scope] = resolved
        self.last_save_ok = self._save()
        return True

    def forget(self, scope: str) -> None:
        """回默认目录（给 `/new` 用，如果将来想让新会话回默认的话）。"""
        if self._by_scope.pop(scope, None) is not None:
            self.last_save_ok = self._save()


def _short(path: str, policy: Policy) -> str:
    """给用户看的短名字。

    全路径在手机上会折行两次，`~/codes/carher-admin` 他一眼就认得。只在
    确实能缩的时候缩 —— 猜不出来就原样给，不编。
    """
    home = os.path.expanduser("~")
    if path == home:
        return "~"
    if home and path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def resolve_request(policy: Policy, current: str, raw: str) -> CdResult:
    """把用户打的那串东西解析成一个合法工作目录。**不落盘、不改状态。**

    拆成纯函数是为了能单独测 —— 路径收敛是安全边界，不该只能通过
    「构造一个 store、再构造一个 scope」才测得到。

    判定顺序是安全要求，不是风格：
    **先越界、再存在性。** 反了的话，白名单外的路径会因为"不存在"和"不是目录"
    拿到不同回话 ⇒ `/cd` 变成一个文件系统探测器，用户能一句一句把外面的目录
    结构问出来，而这些话是发到飞书（外部系统）的。
    """
    raw = (raw or "").strip().strip("'\"")
    if not raw:
        return CdResult(
            Outcome.EMPTY,
            # 他已经在打 `/cd` 了（只是没带名字）⇒ 告诉他怎么补全；不知道有哪些就去点按钮。
            "要去哪个项目？在 /cd 后面空一格打项目名就行。"
            f"不知道有哪些，点菜单里的{DIRS_BUTTON}（或者发 /dirs）。",
        )

    if _looks_like_path(raw):
        # 他写的是个路径 ⇒ 严格按路径算，不做模糊匹配。写了路径还给他换个
        # 别的目录，比直接说"不行"更容易让他在错地方干活。
        expanded = os.path.expanduser(raw)
        if not os.path.isabs(expanded):
            expanded = os.path.join(current, expanded)
        return _verify(policy, expanded, raw)

    candidates = _fuzzy_match(policy, raw)
    if not candidates:
        # 一个都没匹上时，再按"当前目录下的子目录"试一次 —— 这样能给出
        # 「不存在」还是「越界」的准话，而不是笼统一句"没找到"。
        return _verify(policy, os.path.join(current, raw), raw)
    if len(candidates) > 1:
        # 🔴 绝不自己挑。挑错 ⇒ 他在另一个仓库上干活，而回话只有"已切到 xxx"
        # 一行，他不会立刻发现。
        shown = candidates[:MAX_CANDIDATES_SHOWN]
        lines = [f"「{raw}」匹配到 {len(candidates)} 个，你要哪个："]
        lines += [f"  /cd {_short(p, policy)}" for p in shown]
        if len(candidates) > len(shown):
            lines.append(f"  …还有 {len(candidates) - len(shown)} 个，名字打全一点")
        return CdResult(
            Outcome.AMBIGUOUS, "\n".join(lines), candidates=tuple(candidates)
        )
    return _verify(policy, candidates[0], raw)


def _verify(policy: Policy, candidate: str, raw: str) -> CdResult:
    """收敛 + 存在性检查。越界的回话里**不能泄露**那个路径存不存在。"""
    resolved = policy.resolve_workspace(candidate)
    if resolved is None:
        # 统一一句话。不说"不存在"也不说"在外面" —— 区别对待就是信息泄露。
        # 但要告诉他**怎么办**，否则他只会把同一条命令再打一遍。
        allowed = [_short(w, policy) for w in policy.workspaces]
        return CdResult(
            Outcome.OUTSIDE,
            # 🔴 不叫他去改 config.toml：手机用户碰不到那个文件（同 menu.build_mode_card 的红字）。
            # 只说他**真能做**的那件事：找给他装的人。
            f"⛔️ 「{raw}」不在我能看的范围里。\n"
            f"能去的地方：{', '.join(allowed)}\n"
            "想让我看更多文件夹，找给你装的那位同事加一下 —— 这是装的时候定下的，手机上点不开。",
        )

    # 到这里路径已经在白名单内，可以放心把真实情况说清楚了。
    if not os.path.exists(resolved):
        return CdResult(
            Outcome.MISSING,
            f"这个目录不存在：{_short(resolved, policy)}\n点菜单里的{DIRS_BUTTON}看现在有哪些。",
        )
    if not os.path.isdir(resolved):
        return CdResult(
            Outcome.NOT_DIR,
            f"这是个文件、不是目录：{_short(resolved, policy)}",
        )
    if not _readable_dir(resolved):
        return CdResult(
            Outcome.NO_ACCESS,
            f"这个目录我进不去（权限不够）：{_short(resolved, policy)}\n"
            "在电脑上看看这个文件夹是不是只有别的账号能打开，或者换一个。",
        )
    return CdResult(Outcome.OK, "", workspace=resolved)


def list_dirs(policy: Policy, current: str) -> str:
    """`/dirs`：列出能去哪，按最近修改排序，标出现在在哪。

    只列**有 `.git` 的目录**：用户想切过去的几乎一定是个仓库，把每个子目录都
    列出来等于不筛。按 mtime 倒排是因为他最可能要去的就是刚动过的那个。

    末尾单独把白名单根列一遍（它们可能自己就没有 `.git`）—— 否则"回到顶上"
    这件事就没有入口，而且他也看不到自己的授权边界到底划在哪。
    """
    repos = _scan(policy, want_git_only=True)

    def mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    repos.sort(key=mtime, reverse=True)
    shown = repos[:MAX_DIRS_LISTED]

    lines = [f"现在在：{_short(current, policy)}", ""]
    if shown:
        lines.append("能去的仓库（最近动过的在前）：")
        now = time.time()
        for path in shown:
            age_days = int((now - mtime(path)) // 86400)
            when = "今天" if age_days <= 0 else f"{age_days} 天前"
            here = " ← 当前" if path == current else ""
            # 给短路径而不是只给 basename：短路径能**直接粘回 /cd**，
            # 而两个根下有同名仓库时 basename 会让他拿到"要你选"的回话。
            lines.append(f"  {_short(path, policy)}  ({when}){here}")
        if len(repos) > len(shown):
            lines.append(f"  …还有 {len(repos) - len(shown)} 个")
    else:
        # 一个仓库都没扫到不代表配置坏了（可能白名单根本身就是那个仓库，
        # 或者仓库在第 4 层以下）。说清楚是"没扫到"而不是"没有"。
        lines.append(f"没在底下 {MAX_SCAN_DEPTH} 层里扫到 git 仓库。")

    lines += ["", f"根目录：{', '.join(_short(w, policy) for w in policy.workspaces)}"]
    # 按钮带不了参数（actions.do_value 只认无参 op），所以切到**某一个**项目只能打字 ——
    # 给他一句能照抄的，名字就是上面列的那串。
    lines.append("要换到哪个项目，发 /cd 加上面的名字（照抄就行）。")
    return "\n".join(lines)


def handle(
    policy: Policy,
    store: WorkspaceStore,
    scope: str,
    current: str,
    text: str,
) -> CdResult | None:
    """`app.py` 的唯一入口。认领了这条命令就返回 `CdResult`，没认领返回 `None`。

    返回 `None` 而不是抛异常，是为了和 `_handle_command()` 现有的
    `return True/False` 风格对上：`None` ⇒ 这条不是我的，你接着往下判。

    切换成功时 `reply` 已经写好（含落盘失败的告警），调用方直接发出去即可；
    **但 `session.workspace` 得调用方自己赋值** —— 这个模块不碰 Session，
    免得和并行改 sessions.py 的人撞上。
    """
    parts = text.split(maxsplit=1)
    command = parts[0].lower() if parts else ""
    if command not in COMMANDS:
        return None

    if command in {"/dirs", "/ls"}:
        return CdResult(Outcome.SAME, list_dirs(policy, current), workspace=current)

    if command in {"/pwd", "/where"}:
        return CdResult(
            Outcome.SAME,
            f"现在在：{_short(current, policy)}\n想换就点菜单里的{DIRS_BUTTON}。",
            workspace=current,
        )

    # /cd
    raw = parts[1] if len(parts) > 1 else ""
    result = resolve_request(policy, current, raw)
    if result.outcome != Outcome.OK:
        # 空参数、越界、不存在、要他选 —— 一律原样回，不落盘、不改状态。
        return result

    if result.workspace == current:
        return CdResult(
            Outcome.SAME,
            f"已经在这儿了：{_short(current, policy)}",
            workspace=current,
        )

    # 落盘。失败要**说出来** —— 悄悄成功等于骗他"重启后还在这儿"。
    saved = store.set(scope, result.workspace)
    if not saved:
        # set() 自己又判了一次白名单并拒掉了。走到这里说明 resolve_request
        # 和 store.set 的判断不一致，属于代码层面的 bug，不能当成功。
        logger.error(
            "工作目录两次校验结果不一致：%s（resolve 通过、store 拒绝）",
            result.workspace,
        )
        return CdResult(
            Outcome.OUTSIDE,
            f"⛔️ 这个目录没通过最后一道检查，没有切。换一个试试，或者点{DIRS_BUTTON}从列表里挑。",
        )

    reply = f"好，切到 {_short(result.workspace, policy)}"
    if not store.last_save_ok:
        reply += (
            "\n⚠️ 但没记到磁盘上（写文件失败）——"
            f"服务一重启就会回到默认目录，到时候再点{DIRS_BUTTON}换一次。"
        )
        return CdResult(Outcome.SAVE_FAILED, reply, workspace=result.workspace)
    return CdResult(Outcome.OK, reply, workspace=result.workspace)
