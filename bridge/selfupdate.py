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
- Windows 那条路还没在真机上跑过（`run_installer` 的 `WINDOWS_UNVERIFIED`）⇒ 第 30 轮起 `check()` 在
  Windows 上**不下载**，返回 `manual_only=True` 交给提醒那一侧（原来下载完才抛，每 6 小时白下 1.4MB）。

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
import tempfile
import time
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

WINDOWS_UNVERIFIED = "Windows 上的自动更新还没在真机上跑过"

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


# 2026-11-17：这台机是用仓库代码 rsync 上去的（不是安装包）。有它就不让安装包覆盖 ——
# 现场事故：04:43 自动更新装回 20261116 的旧包，把刚修好的 `codex queue` 参数、复制会话、
# 新建项目全冲掉了，群里发消息一律「没发进去」。文件内容随便，只用来表明「这里归 git 管」。
LOCAL_DEPLOY_MARK = ".local-deploy"


def local_deploy_mark(package_dir: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(package_dir)), LOCAL_DEPLOY_MARK)


def is_local_deploy(package_dir: str) -> bool:
    try:
        return os.path.exists(local_deploy_mark(package_dir))
    except Exception:  # noqa: BLE001
        return False


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
        if is_local_deploy(self.package_dir):
            # 仓库直接部署的机器：安装包会把本地新代码覆盖成旧版本（实测过一次）⇒ 一律不装
            self.last_error = "这台电脑跑的是仓库里的代码，更新用 git 部署，别用安装包"
            logger.info("本机有 %s，跳过自动更新（安装包会覆盖仓库代码）", LOCAL_DEPLOY_MARK)
            return None
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
        if installer_key() == "windows":
            # 🔴 第 30 轮：Windows 装不了（`WINDOWS_UNVERIFIED`），原来下载完才知道 ⇒ 每 6 小时白下一次。
            # 现在不下载，把「有新版、要人自己装」交给调用方（提醒那一侧 / 「更新」的回话）。
            return dict(info, manual_only=True)
        return info

    # ---------- 下载 + 核对 ----------

    def download(self, info: dict, dest_dir: str) -> str:
        """下载并核对 sha256。对不上 ⇒ 抛异常，文件删掉（见文件头红线）。"""
        part = info[installer_key()]
        url = f"{BASE_URL}/{part['file']}"
        data = _fetch(url, opener=self._opener)
        got = hashlib.sha256(data).hexdigest()
        if got != part["sha256"]:
            raise ValueError(f"下载回来的安装包校验不过（{got[:12]}… ≠ {part['sha256'][:12]}…）")
        path = os.path.join(dest_dir, part["file"])
        with open(path, "wb") as handle:
            handle.write(data)
        os.chmod(path, 0o700)
        return path

    # ---------- 装 ----------

    def run_installer(self, path: str) -> subprocess.CompletedProcess:
        """跑安装包。它自己会停掉旧服务、装新码、重启 —— 所以这个进程**可能被它杀掉**。"""
        if self._runner is not None:
            return self._runner(path)
        if platform.system() == "Windows":
            raise RuntimeError(WINDOWS_UNVERIFIED)
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
        log_path = os.path.join(os.path.dirname(self.state_path) or ".", "logs", "selfupdate-install.log")
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
        except OSError:
            log_path = os.devnull
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

    def update_now(self, info: dict) -> tuple[bool, str]:
        """下载 + 核对 + 装。返回 (成功?, 给人看的一句话)。"""
        build = info["build_id"]
        if info.get("manual_only"):
            # 这台不能自己装（Windows）⇒ 不下载、不记失败
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
                self._remember(build, "installing", **{"from": self.current_build()})
                proc = self.run_installer(path)
        except Exception as exc:  # noqa: BLE001 —— 更新失败绝不能把桥带崩
            self.installing = False
            logger.exception("自动更新失败 build=%s", build)
            self._remember(build, "fail", str(exc))
            self.last_error = str(exc)
            # 给人看的那句不带异常原文（可能是一长串英文栈）；原文进日志
            return False, "更新没装上，这一版先跳过了。原来那版照常能用，过几小时我再试一次。"
        self.installing = False
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-300:] or (proc.stderr or "")[-300:]
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
            logger.info("有新版 %s，这台（Windows）要人自己装，不下载", info["build_id"])
            return None
        if not self.is_idle():
            logger.info("有新版 %s，但现在有活在跑，等空闲再装", info["build_id"])
            return None
        return self.update_now(info)
