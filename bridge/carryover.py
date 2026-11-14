"""重装/升级时，把用户自己改过的设置从旧配置带到新配置。

## 为什么需要

2026-11-04 review 实测：安装器第 5 步无条件重写 config.toml，
升级界面却写着「会保留：机器人、绑定关系、历史会话」。用户自己加的
`deny_patterns`（安装器注释还邀请他加）、改过的 `mode` / `model` / `workspaces`
全部被静默重置成默认值。`deny_patterns` 丢了是**安全方向**的问题 —— 他以为还挡着的东西不挡了。

## 做法

安装器照常写新文件，然后调这个：

    python bridge/carryover.py <旧配置备份> <新配置>

只带这几个键（其余一律以新文件为准 —— 新版本可能改了默认值）：

    [agent]  workspaces / default_workspace / mode / model / turn_timeout_sec / session_idle_reset_min
    [review] deny_patterns
    [notify] waiting      （新文件里没有这一段时会追加，见 APPEND_IF_MISSING）

**不碰 `[feishu]`**：凭据和主人名单由安装器这一轮决定（可能换了机器人）。

## 写法

不引入 TOML 写库（安装器 venv 里只有 lark-oapi）。按**行**替换新文件里
`key = ...` 那一行，值用 json.dumps 写 —— TOML 的字符串、数组、整数和 JSON 写法兼容
（字符串双引号转义一致，数组 `["a", "b"]` 一致）。替换不到就不动，不往文件里乱追加。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import tomllib

CARRY = {
    "agent": (
        "workspaces",
        "default_workspace",
        "mode",
        "max_mode",
        "model",
        "turn_timeout_sec",
        "session_idle_reset_min",
    ),
    "review": ("deny_patterns",),
    # 🔴 2026-11-06：主动提醒的开关。用户关掉了，重装不能悄悄给他打开（写使用指南时发现：
    # 原来这里没有 [notify]、安装器也不写这一段 ⇒ 重装后提醒恢复成开，而他以为自己关过）。
    "notify": ("waiting",),
}

# 新文件里**没有**这一段时，追加到末尾的那几个段（只给开关类的、追加了也不会改变别的行为的用）。
# 为什么要这一条：上面的「按行替换」原则是「替换不到就不动、不乱追加」—— 对已有的键没错，
# 但新加的段落（[notify] 是 11-06 才有的）在**老版本安装器写出的新文件**里根本不存在，
# 按行替换永远替换不到 ⇒ 用户关掉的东西重装后又开了。只对这里列的段放开追加。
APPEND_IF_MISSING = {"notify"}

# ── 第 16 轮：默认档从只读改成全控制，老用户要跟着升 ──
#
# 领导原话：「这个权限问题太严重了，非常麻烦。默认一定要是全控制的，
# 如果用户要求安全的话，让他单独设定只读权限」。
#
# 🔴 光改安装器的默认值**对老用户无效**：重装会把旧 config 的 `mode = "readonly"` 原样带回来，
# 而自动更新走的也是安装器 + 这个文件 ⇒ 发版 10 分钟内所有老同事自动更新、然后继续留在只读。
#
# 怎么分清「那只是上一代的默认值」和「他自己选的只读」：
# r15 及以前的安装器**不给选项、写死** readonly（`tests/test_menu.py` 有一条盯着这点），
# 所以旧文件里的 readonly 不是他选的。他主动选只读只有三种痕迹：
#   `max_mode = "readonly"`（自己锁了上限）/ IT 策略文件（另一条路，这里管不到也绕不过）/
#   手改成了别的档（那旧值就不等于旧默认）。
# 所以从这一轮起，新 config 里写一行 `defaults_rev`，意思是「这份文件的默认值是第几轮的」。
# 以后再改默认值，靠它就能分清，不用再猜。
DEFAULTS_REV = 16
# 迁移发生时在 `[agent]` 留一行，桥启动后好告诉用户「你的档位被升了」——
# 🔴 悄悄把人从只读升到全授权是不行的，他得知道。
MIGRATED_KEY = "migrated_from"


def _migrating_from_old_default(old: dict) -> bool:
    """旧文件里的 readonly 是不是「上一代的默认值」（⇒ 不带它，用新文件的全控制）。"""
    agent = old.get("agent")
    if not isinstance(agent, dict):
        return False
    if agent.get("defaults_rev") is not None:
        return False                      # 这一轮之后写的文件 ⇒ 里面的值都是他自己的
    if agent.get("mode") != "readonly":
        return False                      # 他手改成了别的档 ⇒ 照旧带
    if agent.get("max_mode") == "readonly":
        return False                      # 自己锁了上限 = 主动选的只读 ⇒ 照旧带
    return True


def _value_ok(value) -> bool:
    """只带"安全可写回"的类型 —— 字符串、整数、布尔、字符串数组。"""
    if isinstance(value, bool) or isinstance(value, int) or isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(isinstance(item, str) for item in value)
    return False


class OldConfigUnreadable(ValueError):
    """旧配置读不懂。调用方要**说出来**并按最严的档装，不能当成「没有旧设置」。"""


def fail_closed(new_text: str) -> str:
    """旧配置读不懂时的新配置：档位和上限都压到只读。

    🔴 第 32 轮（PRODUCT 复现）：原来读不懂就原样返回新文件（默认全授权），一句话不说、退出码 0，
    安装器随后还把旧文件删了 ⇒ 他自己选的「只读」、加的禁止项静默变回全授权，而他以为还挡着。
    照 T3 的取舍：读不出设置宁可停在最严那档并说清楚，也不拿默认值顶上。
    """
    out = []
    for line in new_text.splitlines(keepends=True):
        if re.match(r"^\s*mode\s*=", line):
            line = 'mode = "readonly"\n'
        elif re.match(r"^\s*max_mode\s*=", line):
            line = 'max_mode = "readonly"\n'
        out.append(line)
    return "".join(out)


def carry_over(old_text: str, new_text: str) -> tuple[str, list[str]]:
    """返回（合并后的新文件内容, 带过来的键列表）。旧文件读不懂 ⇒ 抛 OldConfigUnreadable。"""
    try:
        old = tomllib.loads(old_text)
    except tomllib.TOMLDecodeError as exc:
        raise OldConfigUnreadable(str(exc)) from exc

    migrate = _migrating_from_old_default(old)
    # 🔴 第 24 轮（R-install 复现）：上次迁移的通知还没送达（标记还在旧文件里）时又来一次自动更新 ⇒
    # 新文件里没有这一行、旧文件又有 defaults_rev 不算迁移 ⇒ 标记被丢、永远不再通知、档位已是全授权。
    # 旧文件里有标记 ⇒ 原样带过去（插的位置和迁移时一样）。
    pending_marker = (not migrate and isinstance(old.get("agent"), dict)
                      and str(old["agent"].get(MIGRATED_KEY) or "") == "readonly")

    lines = new_text.splitlines(keepends=True)
    section = ""
    carried: list[str] = []
    for index, line in enumerate(lines):
        header = re.match(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*$", line)
        if header:
            section = header.group(1)
            continue
        keys = CARRY.get(section)
        if not keys:
            continue
        match = re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*=\s*)", line)
        if not match or match.group(2) not in keys:
            continue
        key = match.group(2)
        if migrate and section == "agent" and key == "mode":
            continue      # 见 `_migrating_from_old_default`：旧值只是上一代的默认值，不带
        if key not in old.get(section, {}):
            continue
        value = old[section][key]
        if not _value_ok(value):
            continue
        rendered = json.dumps(value, ensure_ascii=False)
        newline = "\n" if line.endswith("\n") else ""
        replacement = f"{match.group(1)}{key}{match.group(3)}{rendered}{newline}"
        if replacement != line:
            lines[index] = replacement
            carried.append(f"{section}.{key}")
    merged = "".join(lines)
    if migrate or pending_marker:
        # 在 `[agent]` 段里插一行留痕（放在 mode 那一行后面，找不到就插在段首）
        out: list[str] = []
        done = False
        in_agent = False
        for line in merged.splitlines(keepends=True):
            out.append(line)
            header = re.match(r"^\s*\[([A-Za-z0-9_.-]+)\]\s*$", line)
            if header:
                in_agent = header.group(1) == "agent"
                continue
            if in_agent and not done and re.match(r"^\s*mode\s*=", line):
                out.append(f'{MIGRATED_KEY} = "readonly"\n')
                done = True
        merged = "".join(out)
        if migrate:
            carried.append("agent.mode→全控制（原来是上一代的默认只读）")
        elif done:
            # 必须进 carried：`main()` 见 carried 为空就不落盘（那样标记还是丢了）
            carried.append(f"agent.{MIGRATED_KEY}（上次的权限变更还没通知到）")
    # 新文件里压根没有那一段（老安装器写的）⇒ 把旧值追加上（只限 APPEND_IF_MISSING）
    try:
        new_sections = tomllib.loads(merged)
    except tomllib.TOMLDecodeError:
        new_sections = {}
    for section in sorted(APPEND_IF_MISSING):
        if section in new_sections or not isinstance(old.get(section), dict):
            continue
        keep = [(k, old[section][k]) for k in CARRY.get(section, ()) if k in old[section]
                and _value_ok(old[section][k])]
        if not keep:
            continue
        if merged and not merged.endswith("\n"):
            merged += "\n"
        merged += f"\n[{section}]\n" + "".join(
            f"{k} = {json.dumps(v, ensure_ascii=False)}\n" for k, v in keep)
        carried.extend(f"{section}.{k}" for k, _ in keep)
    # 合并结果必须还是合法 TOML，否则宁可不带（服务起不来比丢设置严重）
    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError:
        return new_text, []
    return merged, carried


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("用法: python bridge/carryover.py <旧配置> <新配置>")
        return 2
    old_path, new_path = argv[1], argv[2]
    try:
        with open(old_path, encoding="utf-8") as handle:
            old_text = handle.read()
        with open(new_path, encoding="utf-8") as handle:
            new_text = handle.read()
    except OSError as exc:
        print(f"WARN=没能读旧设置，按默认值装：{exc}")
        return 0
    try:
        merged, carried = carry_over(old_text, new_text)
    except OldConfigUnreadable:
        # 旧文件留着（备份成 .unreadable），新文件压到只读，说清楚怎么办
        backup = old_path + ".unreadable"
        try:
            os.replace(old_path, backup)
        except OSError:
            backup = old_path
        merged, carried = fail_closed(new_text), ["agent.mode→只读（旧设置读不懂，先按最严的装）"]
        _write(new_path, merged)
        print(f"WARN=旧设置文件读不懂，这次先按「只读」装（原文件留在 {backup}）。"
              "要放开就在飞书里点「🔑 权限」换档。")
        return 0
    if not carried:
        return 0
    _write(new_path, merged)
    print("OK=保留了你之前的设置：" + "、".join(carried))
    return 0


def _write(new_path: str, text: str) -> None:
    directory = os.path.dirname(new_path) or "."
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, delete=False
    ) as tmp:
        tmp.write(text)
        tmp_path = tmp.name
    try:
        os.chmod(tmp_path, os.stat(new_path).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp_path, new_path)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv))
