"""每日同步官方 feishu-agent 安装包到 StarNexus 的 feishu-agent 分支。只下载、核对、解包，绝不运行安装包。

用法：
    python3 tools/sync.py                         # 查官方 latest.json，有新版就导入、提交、推送
    python3 tools/sync.py --file X.command --build <build_id> [--published <时间>]   # 导入本地安装包
    选项：--no-push 不推送；--no-ai 不调 AI（用规则生成的摘要）；--dry-run 只看有没有新版
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

BASE = "https://chat.auto-link.com.cn/ccdl/feishu-agent"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # feishu-agent-fromandy/
REPO = os.path.dirname(ROOT)                      # 分支工作区根目录
META = os.path.join(ROOT, "meta", "BUILD")
CODE_DIRS = ("app_home", "other")
SSH = ("ssh -o ConnectTimeout=20 -o ServerAliveInterval=10 -o ServerAliveCountMax=3 "
       "-o ControlMaster=no -o ControlPath=none")
DIFF_LIMIT = 60000

FLAGS = {
    "权限": re.compile(r"read-only|只读|sandbox|--mode ask|bypass|approval|policy\.mode|permission|权限"),
    "自升级": re.compile(r"selfupdate|latest\.json|install\.sh|launchctl|bridge\.prev|\.local-deploy|自动更新|ROLLBACK"),
    "上报": re.compile(r"REPORT_URL|install-report|errreport|crashloop|上报|telemetry|/ai/api/"),
}


def log(msg: str) -> None:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [fa-sync] {msg}", flush=True)


def git(*args: str, check: bool = True, capture: bool = True) -> str:
    env = dict(os.environ, GIT_SSH_COMMAND=SSH)
    res = subprocess.run(["git", "-C", REPO, *args], env=env, text=True,
                         stdout=subprocess.PIPE if capture else None, stderr=subprocess.PIPE)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{res.stderr.strip()[:400]}")
    return (res.stdout or "").strip()


def read_meta() -> dict:
    out: dict = {}
    if os.path.exists(META):
        for line in open(META, encoding="utf-8"):
            if ":" in line:
                key, val = line.split(":", 1)
                out[key.strip()] = val.strip()
    return out


def fetch(url: str, dest: str, timeout: int) -> None:
    if not url.startswith(BASE + "/"):
        raise RuntimeError(f"拒绝非官方地址：{url}")
    with urllib.request.urlopen(url, timeout=timeout) as resp, open(dest, "wb") as out:
        shutil.copyfileobj(resp, out)


def sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract(pkg: str, out_dir: str) -> int:
    for sub in CODE_DIRS:
        shutil.rmtree(os.path.join(out_dir, sub), ignore_errors=True)
    res = subprocess.run([sys.executable, os.path.join(HERE, "extract.py"), pkg, out_dir],
                         text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if res.returncode != 0:
        raise RuntimeError(f"解包失败：{res.stdout[-400:]}")
    return sum(len(files) for _, _, files in os.walk(os.path.join(out_dir, "app_home")))


def write_meta(build: str, digest: str, size: int, published: str) -> None:
    os.makedirs(os.path.dirname(META), exist_ok=True)
    with open(META, "w", encoding="utf-8") as fh:
        fh.write(f"build_id: {build}\nsha256: {digest}\nbytes: {size}\npublished_at: {published}\n")


def flagged(diff: str) -> dict:
    hits: dict = {}
    for name, pat in FLAGS.items():
        lines = [ln for ln in diff.splitlines()
                 if ln[:1] in "+-" and ln[:3] not in ("+++", "---") and pat.search(ln)]
        if lines:
            hits[name] = len(lines)
    return hits


def rule_summary(prev: str, build: str, stat: str, hits: dict) -> str:
    head = f"upstream {build}" if prev else f"upstream {build}（首个导入版本）"
    lines = [head, ""]
    if prev:
        lines.append(f"对比上一版 {prev}。改动文件：")
        lines.append(stat.strip() or "（无文件级差异）")
    else:
        lines.append("首次导入，无可对比的上一版。")
    lines.append("")
    lines.append("风险扫描（命中改动行数）：" + ("，".join(f"{k} {v}" for k, v in hits.items()) if hits else "无"))
    return "\n".join(lines)


def ai_summary(prev: str, build: str, diff: str, stat: str, hits: dict) -> str:
    codex = shutil.which("codex") or os.path.expanduser("~/.carher-npm-global/bin/codex")
    if not os.path.exists(codex):
        return ""
    prompt = (
        "你在维护官方 feishu-agent 安装包的版本跟踪仓库。下面是官方从上一版到新版的 git diff。"
        "请只输出一段中文 git 提交说明，不要解释你在做什么，不要用代码块。\n"
        f"第一行固定写：upstream {build}\n"
        "空一行后分三部分：『新增/改动』用要点列出主要变化；"
        "『风险』单独列出权限变化、自升级变化、向官方服务器上报的变化（没有就写无）；"
        "『影响 StarNexus』一句话说明是否需要我们跟进。\n"
        f"已知命中：{hits or '无'}。文件级统计：\n{stat}\n\n----- diff 开始 -----\n{diff}"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as pf:
        pf.write(prompt)
        prompt_path = pf.name
    out_path = prompt_path + ".out"
    try:
        res = subprocess.run(
            [codex, "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
             "-o", out_path, "-"],
            stdin=open(prompt_path, "rb"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=240)
        if res.returncode == 0 and os.path.exists(out_path):
            text = open(out_path, encoding="utf-8").read().strip()
            if text.startswith("upstream"):
                return text
    except Exception as exc:  # noqa: BLE001
        log(f"AI 摘要失败，改用规则摘要：{exc}")
    finally:
        for path in (prompt_path, out_path):
            try:
                os.unlink(path)
            except OSError:
                pass
    return ""


def import_build(pkg: str, build: str, published: str, use_ai: bool, push: bool) -> bool:
    prev = read_meta().get("build_id", "")
    if prev == build:
        log(f"已是最新 {build}，跳过")
        return False
    count = extract(pkg, ROOT)
    write_meta(build, sha256(pkg), os.path.getsize(pkg), published)
    git("add", "-A")
    if not git("status", "--porcelain"):
        log("解包后无文件变化，跳过")
        return False
    stat = git("diff", "--cached", "--stat")
    diff = git("diff", "--cached", "--", *CODE_DIRS)
    hits = flagged(diff)
    msg = ""
    if use_ai and prev:
        msg = ai_summary(prev, build, diff[:DIFF_LIMIT], stat, hits)
    if not msg:
        msg = rule_summary(prev, build, stat, hits)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as mf:
        mf.write(msg)
        msg_path = mf.name
    try:
        git("commit", "-q", "-F", msg_path, capture=False)
    finally:
        os.unlink(msg_path)
    log(f"已提交 {build}（{count} 个文件）：\n{msg.splitlines()[0]}")
    if push:
        git("pull", "--rebase", "-q", "origin", "feishu-agent", check=False)
        git("push", "-q", "origin", "feishu-agent", capture=False)
        log("已推送 origin/feishu-agent")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--build")
    ap.add_argument("--published", default="")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--no-ai", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    push = not args.no_push
    if args.file:
        build = args.build or os.path.basename(args.file)
        return 0 if import_build(args.file, build, args.published, not args.no_ai, push) else 0
    with tempfile.TemporaryDirectory() as tmp:
        latest = os.path.join(tmp, "latest.json")
        fetch(f"{BASE}/latest.json", latest, 30)
        info = json.load(open(latest))
        build = info["build_id"]
        published = info.get("published_at", "")
        if read_meta().get("build_id", "") == build:
            log(f"官方仍是 {build}，无新版")
            return 0
        if args.dry_run:
            log(f"发现新版 {build}（本地 {read_meta().get('build_id','无')}）")
            return 0
        mac = info["mac"]
        pkg = os.path.join(tmp, mac["file"])
        log(f"下载 {mac['file']}（新版 {build}）")
        fetch(f"{BASE}/{mac['file']}", pkg, 300)
        got = sha256(pkg)
        if got != mac["sha256"]:
            raise RuntimeError(f"sha256 不符：期望 {mac['sha256']}，实际 {got}")
        log("sha256 校验通过，开始解包（不运行安装包）")
        import_build(pkg, build, published, not args.no_ai, push)
    return 0


if __name__ == "__main__":
    sys.exit(main())
