"""opencode 的会话：第三个工具，用来验证适配层（产品定义 §5.9）。

2026-11-17 实测（opencode v2.0.15）：
- 列 / 读：`~/.local/share/opencode/opencode.db` 的 `session_v2`（id、title、directory、time_updated）
  和 `session_message`（type = user / assistant / idle；assistant 的 `data.content` 是 `[{type:text,text}]`）。
  一律 `?mode=ro`，绝不写。
- 新建：`opencode run --format json [--auto] [-m provider/model] <第一句>`，cwd = 项目目录；
  每行一个 JSON 事件，`sessionID` 在每行上，回复是 `type=text` 的 `part.text`。
- 接着聊：同上加 `-s <sessionID>`；复制：再加 `--fork`。
- 权限：trusted / write 档加 `--auto`（不是显式拒绝的都自动批准，和 Codex 的 trusted 同一个口径）；
  readonly 不加，需要批准的操作直接失败。
- 停：只停桥自己起的进程（`FleetJobs`）。终端里开着的 opencode 不发信号。
- 进度：opencode 没有 Claude / Codex 那样的会话记录文件，进展卡只显示「在跑」，不列工具调用。
- 中途插话（1.8.0，2026-11-18 实测）：接着聊时如果本机常驻服务在（`opencode service status` 给出地址），
  就不加 `--standalone`，走常驻服务（它按会话自己的 directory 干活，实测写对了目录）。这样一轮在跑时
  `opencode api POST /api/session/<id>/prompt {delivery: steer}` 能插进**同一个服务**里的这一轮。
  🔴 走常驻服务时只杀 `opencode run` 客户端**停不下**那一轮（实测杀掉后服务里照样在跑），
  所以 stop 先调 `/interrupt` 再收进程。新建会话仍走 `--standalone`（第一轮不能插话）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import urllib.parse
from typing import Any, Callable, Optional

from .model import ActionResult, Adapter, FleetJobs, Message, Session, SubprocessRunner, friendly_error, short_title

logger = logging.getLogger("feishu-bridge.fleet.opencode")

TOOL = "opencode"
_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{8,64}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{0,95}$")
SQLITE_TIMEOUT_SEC = 1.0
DEFAULT_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
STOPPED_BY_OWNER = "这一轮已按你的要求停下。会话还在，直接说下一句就能接着聊。"
STEER_OK = "↪️ 已插进正在跑的这一轮，它做完手上这一步就会看到。"
WHY_STEER_NOT_RUNNING = "这一轮已经结束或不是从飞书起的，插不进去。"
# 2026-11-17 实测：opencode 自己的默认模型（本机是 grok-4.6）走一次工具调用要 4~8 分钟，
# 甚至一个「建个文件」都超时；`cpa-gui/gpt-5.6-luna` 同样的事 5 秒做完。
# 默认用它，想换就设 CARHER_OPENCODE_MODEL。
DEFAULT_MODEL = os.environ.get("CARHER_OPENCODE_MODEL", "cpa-gui/gpt-5.6-luna")


def is_session_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_RE.match(value))


def parse_run_output(stdout: str) -> tuple:
    """`opencode run --format json` 的输出 ⇒ (sessionID, 回复文本, 失败说明)。"""
    sid, texts, failure = "", [], ""
    for line in str(stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        sid = sid or str(event.get("sessionID") or "")
        kind = event.get("type")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind == "text" and isinstance(part.get("text"), str):
            texts.append(part["text"])
        elif kind == "error":
            err = event.get("error")
            failure = str((err.get("data") or {}).get("message") or err.get("name") or "")[:200] \
                if isinstance(err, dict) else str(err or "出错了")[:200]
    return sid, "\n\n".join(t for t in texts if t.strip()).strip(), failure


class OpencodeAdapter(Adapter):
    tool = TOOL

    def __init__(self, jobs: Optional[FleetJobs] = None, runner: Optional[SubprocessRunner] = None,
                 db_path: str = "", binary: str = "") -> None:
        super().__init__(jobs, runner)
        self.db_path = db_path or DEFAULT_DB
        self.binary = binary
        self._send_guard = threading.Lock()
        self._sending: set = set()
        # 这几轮走的是常驻服务（能插话、停要调 /interrupt）
        self._via_service: set = set()

    # ---------- 读 ----------

    def _conn(self) -> Optional[sqlite3.Connection]:
        if not os.path.exists(self.db_path):
            return None
        uri = "file:" + urllib.parse.quote(self.db_path) + "?mode=ro"
        try:
            return sqlite3.connect(uri, uri=True, timeout=SQLITE_TIMEOUT_SEC)
        except sqlite3.Error as exc:
            self.last_error = friendly_error(exc, "读 opencode 会话失败")
            return None

    def _session(self, row: tuple) -> Optional[Session]:
        sid, title, directory, updated = row
        if not is_session_id(str(sid or "")):
            return None
        own = bool(self.jobs.jobs(tool=TOOL, session_id=str(sid)))
        return Session(tool=TOOL, surface="cli", id=str(sid), title=short_title(title or "", 40),
                       cwd=str(directory or ""), status="running" if own else "idle",
                       owner="bridge" if own else "none", updated_at=float(updated or 0) / 1000.0,
                       can_send=not own, can_stop=own,
                       why_not_send="手机上发的上一句还在跑，等它回来。" if own else "",
                       why_not_stop="" if own else "它现在没有在跑从手机上发的任务，没有可停的。")

    def list(self, limit: int = 20) -> list:
        conn = self._conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "select id, title, directory, time_updated from session_v2 where parent_id is null "
                "order by time_updated desc limit ?", (int(limit),)).fetchall()
        except sqlite3.Error as exc:
            self.last_error = friendly_error(exc, "读 opencode 会话失败")
            return []
        finally:
            conn.close()
        return [s for s in (self._session(r) for r in rows) if s is not None]

    def running(self) -> list:
        return [s for s in self.list(50) if s.status == "running"]

    def find(self, id: str) -> Optional[Session]:
        if not is_session_id(id):
            return None
        conn = self._conn()
        if conn is None:
            return None
        try:
            row = conn.execute("select id, title, directory, time_updated from session_v2 where id = ?",
                               (id,)).fetchone()
        except sqlite3.Error:
            row = None
        finally:
            conn.close()
        return self._session(row) if row else None

    def read(self, id: str, n: int = 4) -> list:
        if not is_session_id(id):
            return []
        conn = self._conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "select type, time_created, data from session_message where session_id = ? "
                "and type in ('user', 'assistant') order by seq desc limit ?", (id, int(n) * 2)).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        out: list = []
        for kind, at, data in reversed(rows):
            try:
                body = json.loads(data)
            except ValueError:
                continue
            if kind == "user":
                text = str(body.get("text") or "")
            else:
                text = "\n".join(str(p.get("text") or "") for p in body.get("content") or []
                                 if isinstance(p, dict) and p.get("type") == "text")
            if text.strip():
                out.append(Message(role=kind, text=text.strip(), at=float(at or 0) / 1000.0))
        return out[-n:]

    # ---------- 发 ----------

    def _bin(self) -> str:
        """本机装了好几份时挑版本最新的（同 `codex._codex`）。

        🔴 2026-11-17 实测：后台服务 PATH 先找到 `~/.local/bin/opencode`（1.18.32，不认 `--standalone`），
        终端里是 `/opt/homebrew/bin/opencode`（v2.0.15）⇒ 手机上新建会话报参数错。
        """
        if self.binary:
            return self.binary
        cached = getattr(self, "_bin_pick", "")
        if cached:
            return cached
        from .codex import _codex_version

        cands: list = []
        for cand in (shutil.which("opencode"), "/opt/homebrew/bin/opencode", "/usr/local/bin/opencode",
                     os.path.expanduser("~/.local/bin/opencode")):
            if cand and os.path.exists(cand) and cand not in cands:
                cands.append(cand)
        best, best_ver = "", ()
        for cand in cands:
            ver = _codex_version(cand)
            if ver > best_ver or not best:
                best, best_ver = cand, ver
        self._bin_pick = best or "opencode"
        logger.info("opencode 命令 选用=%s 版本=%s 候选=%d", self._bin_pick, ".".join(map(str, best_ver)) or "?",
                    len(cands))
        return self._bin_pick

    def _service_up(self) -> bool:
        """本机常驻服务在不在。读不出来一律当不在（退回 `--standalone`，只是不能插话）。"""
        try:
            res = self.runner.run([self._bin(), "service", "status"], timeout=10)
        except Exception:  # noqa: BLE001
            return False
        out = str(getattr(res, "stdout", "") or "").strip()
        return getattr(res, "returncode", 1) == 0 and out.startswith("http")

    def _api(self, method: str, path: str, data: Optional[dict] = None, timeout: float = 30) -> dict:
        argv = [self._bin(), "api", method, path]
        if data is not None:
            argv += ["-d", json.dumps(data, ensure_ascii=False)]
        res = self.runner.run(argv, timeout=timeout)
        if getattr(res, "returncode", 1) != 0:
            return {}
        try:
            got = json.loads(getattr(res, "stdout", "") or "{}")
        except ValueError:
            return {}
        return got if isinstance(got, dict) else {}

    def _argv(self, text: str, mode: Optional[str], model: str, sid: str = "", fork: bool = False,
              standalone: bool = True) -> list:
        # `--standalone`：用它自己的私有服务，**按我们给的 cwd** 干活。不加的话它连本机常驻服务，
        # 实测**新会话**的文件写到了那个服务当前开的项目里（我的实测把 oc.txt 写进了 remote_control）。
        # 接着聊一个已有会话时，常驻服务按会话自己的 directory 干活（1.8.0 实测），可以不加。
        argv = [self._bin(), "run"] + (["--standalone"] if standalone else []) + ["--format", "json"]
        if (mode or "readonly") in ("trusted", "write"):
            argv.append("--auto")
        argv += ["-m", model or DEFAULT_MODEL]
        if sid:
            argv += ["-s", sid]
        if fork:
            argv.append("--fork")
        argv += ["--", text]
        return argv

    def _run(self, argv: list, cwd: str, timeout: float, key: str,
             on_pid: Optional[Callable[[int], None]] = None) -> tuple:
        spawned: list = []

        def on_spawn(pid: int) -> None:
            spawned.append(pid)
            self.jobs.register_spawned(pid, TOOL, key)
            if on_pid is not None:
                try:
                    on_pid(pid)
                except Exception:  # noqa: BLE001
                    pass

        env = dict(os.environ)
        extra = [os.path.dirname(argv[0])] if os.path.isabs(argv[0]) else []
        extra += [os.path.expanduser("~/.local/bin"), "/opt/homebrew/bin", "/usr/local/bin"]
        parts = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
        env["PATH"] = os.pathsep.join(parts + [p for p in extra if p and p not in parts])
        # 🔴 2026-11-17 实测：opencode 认的是 **PWD 环境变量**，不是子进程的 cwd ——
        # 只给 cwd 时它把文件写进了启动服务那个目录（我的实测写进了 remote_control）。
        if cwd:
            env["PWD"] = cwd
        try:
            res = self.runner.run(argv, timeout=max(1.0, float(timeout)), cwd=cwd, env=env, on_spawn=on_spawn)
        finally:
            for pid in spawned:
                self.jobs.forget(pid)
        return res, bool(spawned)

    def _result(self, res: Any, spawned: bool, fallback_sid: str) -> ActionResult:
        sid, reply, failure = parse_run_output(getattr(res, "stdout", ""))
        sid = sid or fallback_sid
        if self.jobs.take_stopped(TOOL, fallback_sid or sid) and not reply:
            return ActionResult(False, STOPPED_BY_OWNER, sid, delivered=spawned)
        if getattr(res, "error", ""):
            return ActionResult(False, "没能启动 opencode：" + str(res.error), sid, delivered=spawned)
        if getattr(res, "timed_out", False):
            return ActionResult(False, "opencode 太久没回完，已经让它停下了。", sid, delivered=spawned)
        if getattr(res, "returncode", 1) != 0 or (failure and not reply):
            err = [ln for ln in (getattr(res, "stderr", "") or "").strip().splitlines() if ln.strip()]
            detail = short_title(failure or (err[-1] if err else ""), 120)
            return ActionResult(False, "opencode 没回成功" + (f"：{detail}" if detail else "。"), sid, delivered=spawned)
        return ActionResult(True, reply or "opencode 跑完了，但没有文字回复。", sid, delivered=spawned)

    def start(self, cwd: str, text: str, timeout: float = 600, mode: Optional[str] = "trusted",
              model: str = "", on_pid: Optional[Callable[[int], None]] = None, effort: str = "",
              approval_dir: str = "", fork_from: str = "") -> ActionResult:
        text = str(text or "").strip()
        if not text:
            return ActionResult(False, "要发的话是空的。", "")
        if not cwd or not os.path.isdir(cwd):
            return ActionResult(False, "这个项目目录不在了，换一个再开。", "")
        if model and not _MODEL_RE.match(model):
            return ActionResult(False, f"模型名不对：{short_title(model, 40)}", "")
        if fork_from and not is_session_id(fork_from):
            return ActionResult(False, "要复制的会话编号不对。", "")
        argv = self._argv(text, mode, model, sid=fork_from, fork=bool(fork_from))
        try:
            res, spawned = self._run(argv, cwd, timeout, "", on_pid)
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, "没能启动 opencode"), "")
        out = self._result(res, spawned, "")
        if not out.session_id or (fork_from and out.session_id == fork_from):
            return ActionResult(False, out.text if not out.ok else "opencode 没开成新会话。", "")
        return out

    def send(self, id: str, text: str, timeout: float = 600, mode: Optional[str] = None,
             model: str = "", effort: str = "", approval_dir: str = "") -> ActionResult:
        if not is_session_id(id):
            return ActionResult(False, "会话编号不对，发不了。", str(id or ""))
        text = str(text or "").strip()
        if not text:
            return ActionResult(False, "要发的话是空的。", id)
        if model and not _MODEL_RE.match(model):
            return ActionResult(False, f"模型名不对：{short_title(model, 40)}", id)
        sess = self.find(id)
        if sess is None:
            return ActionResult(False, "找不到这个会话，可能已经删掉了。", id)
        if not sess.cwd or not os.path.isdir(sess.cwd):
            return ActionResult(False, "这个会话的项目目录不在了。", id)
        with self._send_guard:
            if id in self._sending:
                return ActionResult(False, "手机上发的上一句还在跑，等它回来。", id)
            self._sending.add(id)
        via_service = self._service_up()
        if via_service:
            with self._send_guard:
                self._via_service.add(id)
        try:
            argv = self._argv(text, mode or "readonly", model, sid=id, standalone=not via_service)
            res, spawned = self._run(argv, sess.cwd, timeout, id)
        except Exception as exc:
            return ActionResult(False, friendly_error(exc, "没发出去"), id)
        finally:
            with self._send_guard:
                self._sending.discard(id)
                self._via_service.discard(id)
        return self._result(res, spawned, id)

    # ---------- steer：一轮在跑时中途插话 ----------

    def steerable(self, id: str) -> bool:
        with self._send_guard:
            return id in self._sending and id in self._via_service

    def steer(self, id: str, text: str) -> ActionResult:
        """把一句话插进常驻服务里正在跑的这一轮（`delivery: steer`）。

        只认桥自己发起、走常驻服务、还没回来的那一轮；服务里这一轮已经不在跑 ⇒ 不发
        （发了会在没人等的情况下另起一轮，回复就丢了）。
        """
        sid = str(id or "")
        text = str(text or "").strip()
        if not text:
            return ActionResult(False, "要发的话是空的。", sid)
        if not is_session_id(sid) or not self.steerable(sid):
            return ActionResult(False, WHY_STEER_NOT_RUNNING, sid)
        active = self._api("GET", "/api/session/active", timeout=10)
        if sid not in (active.get("data") or {}):
            return ActionResult(False, WHY_STEER_NOT_RUNNING, sid)
        got = self._api("POST", f"/api/session/{sid}/prompt", {"text": text, "delivery": "steer"})
        data = got.get("data") if isinstance(got.get("data"), dict) else {}
        if data.get("delivery") != "steer":
            logger.info("opencode 插话 没插进 会话=%s", sid[:12])
            return ActionResult(False, WHY_STEER_NOT_RUNNING, sid)
        logger.info("opencode 插话 写入 会话=%s 字数=%d", sid[:12], len(text))
        return ActionResult(True, STEER_OK, sid, delivered=True)

    def stop(self, id: str) -> ActionResult:
        sid = str(id or "")
        if self.jobs.jobs(tool=TOOL, session_id=sid):
            with self._send_guard:
                via_service = sid in self._via_service
            if via_service and is_session_id(sid):
                # 🔴 只杀客户端停不下常驻服务里的那一轮（实测），先让服务中断它
                got = self._api("POST", f"/api/session/{sid}/interrupt", timeout=15)
                logger.info("opencode 停下 走常驻服务 interrupted=%s 会话=%s", got.get("interrupted"), sid[:12])
            return self.stop_own(sid)
        return ActionResult(False, "它现在没有在跑从手机上发的任务，没有可停的。", sid)
