"""StarNexus 从 GitHub 自升级（1.2.0）：只认 `x.y.z` 标签。由 launchd 每 6 小时在服务外面跑一次。

流程（任一步失败 ⇒ 不动运行目录，或换回备份；见 docs/selfupgrade.md）：

1. `git ls-remote --tags` 取最新 `x.y.z` 标签；不比本机已装的新 / 是坏版本 ⇒ 结束。
2. 服务正在干活（`inflight.json` 非空，或最近 3 分钟有会话活动）⇒ 下一次再来。
   `--now` 跳过这一条：开发时要部署就立刻停服部署（起不来仍自动回滚）。
3. 在独立目录（`~/.carher-feishu-agent/upgrade/src`）浅克隆这个标签，不碰开发仓库。
4. 用服务的 venv 跑全部单元测试；不过 ⇒ 记坏版本，通知，结束。
5. 备份运行中的 `bridge/` → 同步新代码 → 核对哈希 → 重启服务。
6. 90 秒内日志出现新的「桥已启动」和长连接 connected，且之后 3 分钟服务一直 running、没再重启
   ⇒ 记为已装；否则换回备份、重启，记坏版本，通知。

状态文件 `~/.carher-feishu-agent/upgrade/state.json`：{installed, installed_sha, bad: {tag: 原因}, history: [...]}。
通知：私聊主人 + Remote control dev 群（用 Her remote · Pro 机器人发；机器人不在群里时用 lark-cli 以用户身份发群）。
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from typing import Callable, Optional

REPO_URL = "git@github.com:sandy-slin/StarNexus.git"
APP_HOME = os.path.expanduser("~/.carher-feishu-agent")
LABEL = "com.carher.feishu-agent"
DEV_CHAT = "oc_b4975c2589ec0b5930c18dff74778706"
TAG_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
START_WAIT_SEC = 90
STABLE_SEC = 180
BUSY_RECENT_SEC = 180
KEEP_BACKUPS = 5
SSH = ("ssh -o ConnectTimeout=20 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 "
       "-o ControlMaster=no -o ControlPath=none -o BatchMode=yes")


def log(msg: str) -> None:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [selfupgrade] {msg}", flush=True)


def version(tag: str) -> Optional[tuple]:
    hit = TAG_RE.match(tag or "")
    return tuple(int(x) for x in hit.groups()) if hit else None


def newest_tag(lines: str) -> tuple:
    """`git ls-remote --tags` 的输出 → (最新标签, 它指向的提交)。没有合规标签 ⇒ ("", "")。"""
    tags: dict = {}
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[1].startswith("refs/tags/"):
            continue
        name = parts[1][len("refs/tags/"):]
        peeled = name.endswith("^{}")
        name = name[:-3] if peeled else name
        if version(name) is None:
            continue
        if peeled or name not in tags:
            tags[name] = parts[0]
    if not tags:
        return "", ""
    best = max(tags, key=version)
    return best, tags[best]


class Upgrader:
    def __init__(self, home: str = APP_HOME, repo: str = REPO_URL, *, run: Callable = subprocess.run,
                 sleep: Callable = time.sleep, now: Callable = time.time, notify: Optional[Callable] = None,
                 label: str = LABEL) -> None:
        self.home = home
        self.repo = repo
        self.run = run
        self.sleep = sleep
        self.now = now
        self.label = label
        self.work = os.path.join(home, "upgrade")
        self.state_path = os.path.join(self.work, "state.json")
        self.notify = notify or self._notify_feishu
        self.python = os.path.join(home, "venv", "bin", "python")
        self.bridge = os.path.join(home, "bridge")
        self.log_path = os.path.join(home, "logs", "agent.log")

    # ---------- 状态 ----------

    def state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as fh:
                got = json.load(fh)
            return got if isinstance(got, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, state: dict) -> None:
        os.makedirs(self.work, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    def record(self, state: dict, tag: str, outcome: str, detail: str = "") -> None:
        hist = list(state.get("history") or [])[-19:]
        hist.append({"tag": tag, "outcome": outcome, "detail": detail[:300],
                     "at": datetime.datetime.now().isoformat(timespec="seconds")})
        state["history"] = hist
        self.save(state)

    # ---------- 小工具 ----------

    def sh(self, argv: list, *, cwd: str = "", timeout: float = 600, env: Optional[dict] = None):
        merged = dict(os.environ, GIT_SSH_COMMAND=SSH, **(env or {}))
        return self.run(argv, cwd=cwd or None, env=merged, capture_output=True, text=True, timeout=timeout)

    def service_pid(self) -> str:
        res = self.sh(["launchctl", "print", f"gui/{os.getuid()}/{self.label}"], timeout=20)
        hit = re.search(r"^\s*pid = (\d+)", res.stdout or "", re.M)
        return hit.group(1) if hit else ""

    def service_running(self) -> bool:
        res = self.sh(["launchctl", "print", f"gui/{os.getuid()}/{self.label}"], timeout=20)
        return res.returncode == 0 and re.search(r"^\s*state = running", res.stdout or "", re.M) is not None

    def restart(self) -> None:
        """重启服务前先清掉启动守卫的「连续崩溃」计数：新版本连崩过几次之后，守卫会先睡一段才拉起，
        换回旧版本时这一觉会让 90 秒的确认落空（launch-guard.sh 的 crashloop.state，安装器也是装完就删）。"""
        try:
            os.remove(os.path.join(self.home, "crashloop.state"))
        except OSError:
            pass
        self.sh(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{self.label}"], timeout=60)

    def log_size(self) -> int:
        try:
            return os.path.getsize(self.log_path)
        except OSError:
            return 0

    def log_since(self, offset: int) -> str:
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(offset)
                return fh.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def busy(self) -> str:
        """服务在干活 ⇒ 返回原因；空闲 ⇒ ""。"""
        try:
            with open(os.path.join(self.home, "inflight.json"), encoding="utf-8") as fh:
                if json.load(fh):
                    return "有一轮正在跑（inflight.json 非空）"
        except (OSError, ValueError):
            pass
        tail = self.log_since(max(self.log_size() - 200_000, 0))
        cutoff = datetime.datetime.fromtimestamp(self.now() - BUSY_RECENT_SEC)
        for line in reversed(tail.splitlines()):
            hit = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", line)
            if not hit:
                continue
            at = datetime.datetime.strptime(hit.group(1), "%Y-%m-%d %H:%M:%S")
            if at < cutoff:
                break
            if re.search(r"收到消息|发出 tool=|新建会话 发出|会话转发|正在跑", line):
                return "最近 3 分钟有会话活动"
        return ""

    @staticmethod
    def digest_tree(root: str) -> str:
        sha = hashlib.sha256()
        for base, dirs, files in sorted(os.walk(root)):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".pyc"):
                    continue
                path = os.path.join(base, name)
                sha.update(os.path.relpath(path, root).encode())
                with open(path, "rb") as fh:
                    sha.update(fh.read())
        return sha.hexdigest()

    # ---------- 通知 ----------

    def http_json(self, url: str, body: dict, token: str = "") -> dict:
        """POST JSON，返回飞书的 JSON 回包；HTTP 错误也读出回包（只含 code/msg，不含密钥）。"""
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                return json.loads(exc.read() or b"{}")
            except ValueError:
                return {"code": exc.code, "msg": "HTTP error"}

    def feishu_cfg(self) -> dict:
        """config.toml 里应用凭据和主人在 [feishu] 段下（兼容旧的顶层写法）。"""
        with open(os.path.join(self.home, "config.toml"), "rb") as fh:
            cfg = tomllib.load(fh)
        sec = cfg.get("feishu") if isinstance(cfg.get("feishu"), dict) else cfg
        return {"app_id": sec.get("app_id", ""), "app_secret": sec.get("app_secret", ""),
                "owners": list(sec.get("owner_open_ids") or [])}

    def _notify_feishu(self, text: str) -> None:
        """私聊主人 + Remote control dev 群，逐个目标发，一个失败不影响其他。
        机器人不在 dev 群里（230002）时，改用 lark-cli 以用户身份发到群。"""
        try:
            cfg = self.feishu_cfg()
        except (OSError, ValueError) as exc:
            log(f"飞书通知：读配置失败 {type(exc).__name__}")
            cfg = {"app_id": "", "app_secret": "", "owners": []}
        token = ""
        if cfg["app_id"] and cfg["app_secret"]:
            got = self.http_json("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                 {"app_id": cfg["app_id"], "app_secret": cfg["app_secret"]})
            token = got.get("tenant_access_token", "")
            if not token:
                log(f"飞书通知：取机器人令牌失败 code={got.get('code')} msg={got.get('msg')}")
        content = json.dumps({"text": text}, ensure_ascii=False)
        targets = [("open_id", o) for o in cfg["owners"]] + [("chat_id", DEV_CHAT)]
        for kind, rid in targets:
            ok = False
            if token:
                got = self.http_json(f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={kind}",
                                     {"receive_id": rid, "msg_type": "text", "content": content}, token)
                ok = got.get("code") == 0
                if not ok:
                    log(f"飞书通知：机器人发 {kind} 失败 code={got.get('code')} msg={got.get('msg')}")
            if not ok and kind == "chat_id":
                ok = self._notify_as_user(rid, text)
            log(f"飞书通知：{'私聊主人' if kind == 'open_id' else 'dev 群'} {'已发' if ok else '未发出'}")

    def _notify_as_user(self, chat_id: str, text: str) -> bool:
        lark = shutil.which("lark-cli") or os.path.expanduser("~/.local/bin/lark-cli")
        try:
            res = self.sh([lark, "im", "+messages-send", "--chat-id", chat_id, "--as", "user", "--text", text],
                          timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"飞书通知：lark-cli 不可用 {type(exc).__name__}")
            return False
        if res.returncode != 0:
            log(f"飞书通知：lark-cli 以用户身份发群失败 rc={res.returncode}")
        return res.returncode == 0

    # ---------- 主流程 ----------

    def check(self) -> tuple:
        res = self.sh(["git", "ls-remote", "--tags", self.repo], timeout=60)
        if res.returncode != 0:
            raise RuntimeError("连不上 GitHub：" + (res.stderr or "").strip()[:200])
        return newest_tag(res.stdout)

    def tick(self, force_tag: str = "", dry_run: bool = False, now: bool = False) -> str:
        """跑一次。返回一个词：noop / busy / bad / installed / rolled_back / failed / would_install。

        `now=True`（命令行 `--now`）：**立刻停服部署，不等空闲**（主人 2026-11-21 定的规则）。
        定时那一拍照旧让着人用（`busy()` 返回原因就等下一次）；开发完要马上验证新版本时用 `--now`：
        正在飞书里和它对话时 `busy()` 永远非空，等不到空闲窗口。
        跳过的只有「让着人用」这一条 —— 测试、备份、哈希核对、起不来自动回滚全都照跑。
        """
        state = self.state()
        tag, sha = (force_tag, "") if force_tag else self.check()
        installed = state.get("installed", "")
        if not tag:
            log("GitHub 上没有 x.y.z 标签")
            return "noop"
        if not force_tag and installed and version(tag) <= version(installed):
            log(f"已是最新：本机 {installed}，GitHub 最新 {tag}")
            return "noop"
        if tag in (state.get("bad") or {}) and not force_tag:
            log(f"{tag} 之前装失败过（{state['bad'][tag]}），等更新的标签")
            return "bad"
        if dry_run:
            log(f"发现新版本 {tag}（本机 {installed or '未记录'}）")
            return "would_install"
        why = "" if now else self.busy()
        if why:
            log(f"{tag} 待安装，但{why}，下一次再来")
            return "busy"
        if now:
            busy_now = self.busy()
            log(f"{tag} 立刻部署（--now）" + (f"：{busy_now}，照样停服" if busy_now else ""))
        return self.install(state, tag, sha)

    def install(self, state: dict, tag: str, sha: str = "") -> str:
        src = os.path.join(self.work, "src")
        shutil.rmtree(src, ignore_errors=True)
        os.makedirs(self.work, exist_ok=True)
        log(f"开始安装 {tag}：取代码")
        res = self.sh(["git", "clone", "-q", "--depth", "1", "--branch", tag, self.repo, src], timeout=300)
        if res.returncode != 0:
            return self.fail(state, tag, "取代码失败：" + (res.stderr or "").strip()[:200], mark_bad=False)
        head = self.sh(["git", "rev-parse", "HEAD"], cwd=src, timeout=20).stdout.strip()
        if sha and head and head != sha:
            return self.fail(state, tag, f"取到的提交 {head[:8]} 与标签 {sha[:8]} 不一致", mark_bad=False)
        log("跑单元测试")
        res = self.sh([self.python, "-m", "unittest", "discover", "-s", "tests", "-t", "."], cwd=src, timeout=900)
        summary = (res.stderr or "").strip().splitlines()[-3:]
        if res.returncode != 0:
            return self.fail(state, tag, "单元测试没过：" + " / ".join(summary))
        log("单元测试通过：" + " / ".join(s for s in summary if s.startswith(("Ran", "OK"))))
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.now()))
        backup = os.path.join(self.home, f"bridge.bak-upgrade-{stamp}")
        shutil.copytree(self.bridge, backup, symlinks=True)
        before_pid = self.service_pid()
        offset = self.log_size()
        res = self.sh(["rsync", "-a", "--checksum", "--delete", "--exclude", "__pycache__", os.path.join(src, "bridge") + "/",
                       self.bridge + "/"], timeout=120)
        if res.returncode != 0 or self.digest_tree(os.path.join(src, "bridge")) != self.digest_tree(self.bridge):
            self.rollback(backup)
            return self.fail(state, tag, "同步代码后哈希对不上，已换回")
        self.restart()
        ok, why = self.verify(offset, before_pid)
        if not ok:
            self.rollback(backup)
            mark = self.log_size()
            self.restart()
            back, _ = self.verify(mark, "", quick=True)
            detail = f"{why}；已换回旧版本" + ("，服务已恢复" if back else "，⚠️ 旧版本也没起来，需要人工处理")
            return self.fail(state, tag, detail, outcome="rolled_back")
        state["installed"] = tag
        state["installed_sha"] = head
        (state.get("bad") or {}).pop(tag, None)
        self.record(state, tag, "installed", head[:12])
        self.prune_backups()
        self.notify(f"✅ StarNexus 已自动升级到 {tag}（{head[:8]}）：单元测试通过，服务启动并连上飞书，"
                    f"{STABLE_SEC // 60} 分钟内稳定。")
        log(f"{tag} 安装完成")
        return "installed"

    def verify(self, offset: int, before_pid: str, quick: bool = False) -> tuple:
        end = self.now() + START_WAIT_SEC
        started = connected = False
        while self.now() < end:
            text = self.log_since(offset)
            started = started or "桥已启动" in text
            connected = connected or ("connected to wss://" in text)
            if started and connected:
                break
            self.sleep(3)
        if not (started and connected):
            return False, f"{START_WAIT_SEC} 秒内没看到服务启动并连上飞书"
        if quick:
            return True, ""
        pid = self.service_pid()
        if not pid or pid == before_pid:
            return False, "服务进程没有换成新的"
        end = self.now() + STABLE_SEC
        while self.now() < end:
            self.sleep(10)
            if not self.service_running() or self.service_pid() != pid:
                return False, f"启动后 {STABLE_SEC // 60} 分钟内服务退出或重启了"
        if self.log_since(offset).count("桥已启动") > 1:
            return False, "启动后又重启过"
        return True, ""

    def rollback(self, backup: str) -> None:
        log("换回备份 " + os.path.basename(backup))
        self.sh(["rsync", "-a", "--checksum", "--delete", "--exclude", "__pycache__", backup + "/", self.bridge + "/"], timeout=120)

    def fail(self, state: dict, tag: str, detail: str, *, outcome: str = "failed", mark_bad: bool = True) -> str:
        log(f"{tag} 安装失败：{detail}")
        if mark_bad:
            state.setdefault("bad", {})[tag] = detail[:200]
        self.record(state, tag, outcome, detail)
        self.notify(f"❌ StarNexus 自动升级到 {tag} 失败：{detail}。"
                    + ("这个版本不再自动重试，等更新的标签。" if mark_bad else "下一次（6 小时后）再试。"))
        return outcome

    def prune_backups(self) -> None:
        olds = sorted(p for p in os.listdir(self.home) if p.startswith("bridge.bak-upgrade-"))
        for name in olds[:-KEEP_BACKUPS]:
            shutil.rmtree(os.path.join(self.home, name), ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看有没有新标签")
    ap.add_argument("--tag", default="", help="指定装某个标签（人工；绕过坏版本记录）")
    ap.add_argument("--mark-installed", default="", help="把当前运行的版本记为这个标签（首次接管时用）")
    ap.add_argument("--now", action="store_true",
                    help="立刻停服部署，不等空闲（开发时用；测试和起不来回滚照跑）")
    args = ap.parse_args()
    up = Upgrader()
    os.makedirs(up.work, exist_ok=True)
    with open(os.path.join(up.work, "lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("上一次升级还在跑，这次跳过")
            return 0
        if args.mark_installed:
            if version(args.mark_installed) is None:
                log("标签格式必须是 x.y.z")
                return 2
            state = up.state()
            state["installed"] = args.mark_installed
            up.record(state, args.mark_installed, "marked", "人工登记当前运行版本")
            log(f"已登记本机版本 {args.mark_installed}")
            return 0
        try:
            outcome = up.tick(force_tag=args.tag, dry_run=args.dry_run, now=args.now)
        except Exception as exc:  # noqa: BLE001
            log(f"这一次没跑成：{exc}")
            return 1
    return 0 if outcome in ("noop", "busy", "bad", "installed", "would_install") else 1


if __name__ == "__main__":
    sys.exit(main())
