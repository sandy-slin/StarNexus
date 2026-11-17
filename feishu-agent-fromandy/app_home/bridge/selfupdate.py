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
- Windows 那条路还没在真机上跑过（`run_installer` 的 `WINDOWS_UNVERIFIED`）。
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
import urllib.request
from typing import Callable, Optional

logger = logging.getLogger("feishu-bridge.selfupdate")

# 🔴 写死：见文件头红线。改这两行等于改「谁能在同事机器上跑代码」。
_ALLOWED_HOST = "chat.auto-link.com.cn"
BASE_URL = f"https://{_ALLOWED_HOST}/ccdl/feishu-agent"

CHECK_EVERY_SEC = 10 * 60
# 下载超时：安装包约 1.1MB，公司网最慢也就几秒；给足余量但不能挂住线程
_FETCH_TIMEOUT = 120
_MAX_BYTES = 8 * 1024 * 1024  # 包就 1.1MB；再大一定是出了别的事，别往磁盘上灌
# 装失败的同一个版本，隔这么久才再试一次
_RETRY_AFTER_SEC = 6 * 60 * 60

WINDOWS_UNVERIFIED = "Windows 上的自动更新还没在真机上跑过"


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


def latest(*, opener=None) -> Optional[dict]:
    """读服务端的 latest.json。读不到 / 格式不对 ⇒ None（当没有新版，不报错给用户）。"""
    try:
        data = json.loads(_fetch(f"{BASE_URL}/latest.json", opener=opener).decode("utf-8"))
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
    ) -> None:
        self.package_dir = package_dir
        self.state_path = state_path
        self.is_idle = is_idle
        self._opener = opener
        self._runner = runner
        self._now = now
        self.last_error = ""

    # ---------- 本机版本 ----------

    def current_build(self) -> str:
        from .status import build_id

        return build_id(self.package_dir)

    # ---------- 记账 ----------

    def _state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _remember(self, build: str, outcome: str, detail: str = "") -> None:
        state = self._state()
        state[build] = {"outcome": outcome, "at": self._now(), "detail": detail[:200]}
        # 只留最近 20 个版本的记录，别让这个文件无限长
        if len(state) > 20:
            for key in sorted(state, key=lambda k: state[k].get("at", 0))[:-20]:
                state.pop(key, None)
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            with open(self.state_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False)
        except OSError:
            logger.warning("写更新记录失败：%s", self.state_path)

    def _tried_recently(self, build: str) -> bool:
        """这个版本刚试过吗。成功过 ⇒ 永远不再装；失败过 ⇒ 隔 `_RETRY_AFTER_SEC` 再试。"""
        rec = self._state().get(build)
        if not isinstance(rec, dict):
            return False
        if rec.get("outcome") == "ok":
            return True
        return self._now() - float(rec.get("at") or 0) < _RETRY_AFTER_SEC

    # ---------- 查 ----------

    def check(self) -> Optional[dict]:
        """有新版且该装 ⇒ 返回 latest.json；否则 None。不下载、不安装。"""
        self.last_error = ""
        info = latest(opener=self._opener)
        if info is None:
            return None
        build = info["build_id"]
        current = self.current_build()
        if not current:
            # 从仓库直接跑的（没有 buildinfo.py）⇒ 这是开发机，绝不自动改它
            logger.info("本机没有版本号（开发机），不自动更新")
            return None
        if build == current:
            return None
        if self._tried_recently(build):
            return None
        part = info.get(installer_key())
        if not isinstance(part, dict) or not isinstance(part.get("sha256"), str) \
                or not isinstance(part.get("file"), str):
            self.last_error = "新版本的信息不全（缺文件名或校验值）"
            return None
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
        with open(os.devnull, "rb") as devnull:
            return subprocess.run(  # noqa: S603 —— path 是刚校验过 sha256 的文件
                ["/bin/bash", path],
                stdin=devnull,
                capture_output=True,
                text=True,
                timeout=20 * 60,
                start_new_session=True,
            )

    def update_now(self, info: dict) -> tuple[bool, str]:
        """下载 + 核对 + 装。返回 (成功?, 给人看的一句话)。"""
        build = info["build_id"]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = self.download(info, tmp)
                logger.info("开始装新版 %s", build)
                proc = self.run_installer(path)
        except Exception as exc:  # noqa: BLE001 —— 更新失败绝不能把桥带崩
            logger.exception("自动更新失败 build=%s", build)
            self._remember(build, "fail", str(exc))
            self.last_error = str(exc)
            # 给人看的那句不带异常原文（可能是一长串英文栈）；原文进日志
            return False, "更新没装上，这一版先跳过了。原来那版照常能用，过几小时我再试一次。"
        if proc.returncode != 0:
            tail = (proc.stdout or "")[-300:] or (proc.stderr or "")[-300:]
            logger.error("安装包退出码 %s build=%s", proc.returncode, build)
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
        if not self.is_idle():
            logger.info("有新版 %s，但现在有活在跑，等空闲再装", info["build_id"])
            return None
        return self.update_now(info)
