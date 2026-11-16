"""每个工具的模型目录、默认模型 / effort、会话级覆盖（产品定义 §5.4）。

- 模型列表从本机配置读，不写死：Claude 读 `~/.claude/settings.json` 的 `modelPicker.options`，
  Codex 读 `~/.codex/config.toml` 指向的 `model_catalog_json`（只取 `visibility == "list"`）。
  读不到就用内置兜底列表，保证菜单不空。
- 默认模型按工具分别存；会话级覆盖按 `tool:sid` 存，从下一轮生效，不影响默认。
- 落盘：临时文件 + `os.replace()`，读失败一律当没设过。
- 这里不 import policy（policy 反过来用这里的列表扩白名单）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger("feishu-bridge.catalog")

TOOLS = ("claude", "codex")
LABELS = {"claude": "Claude", "codex": "Codex"}
CLAUDE_EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORTS = ("low", "medium", "high", "xhigh")
EFFORT_LABELS = {"low": "低", "medium": "中", "high": "高", "xhigh": "很高", "max": "最高", "ultra": "极限",
                 "minimal": "最低", "none": "关"}
FALLBACK = {
    "claude": ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-opus-5"),
    "codex": ("gpt-6-sol", "gpt-6-luna", "gpt-6-astra", "gpt-5.5"),
}
MAX_LIST = 12
CACHE_SEC = 60.0
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")
_EFFORT_RE = re.compile(r"^[a-z]{2,10}$")


def safe_name(name: object) -> bool:
    return isinstance(name, str) and bool(_NAME_RE.match(name))


@dataclass(frozen=True)
class ModelInfo:
    name: str
    efforts: tuple = DEFAULT_EFFORTS
    default_effort: str = ""


class Catalog:
    """本机两家工具的模型目录。读文件有 60 秒缓存。"""

    def __init__(self, claude_settings: str = "~/.claude/settings.json", codex_home: str = "~/.codex",
                 now: Optional[Callable[[], float]] = None) -> None:
        import time

        self._claude = os.path.expanduser(claude_settings)
        self._codex_home = os.path.expanduser(codex_home)
        self._now = now or time.time
        self._cache: dict = {}
        self._lock = threading.Lock()

    def models(self, tool: str) -> tuple:
        with self._lock:
            hit = self._cache.get(tool)
            if hit and self._now() - hit[0] < CACHE_SEC:
                return hit[1]
        try:
            got = self._read(tool)
        except OSError as exc:
            logger.info("读不到模型目录，用内置列表 tool=%s：%s", tool, type(exc).__name__)
            got = ()
        except Exception:  # noqa: BLE001 —— 配置坏了也要有菜单
            logger.exception("读模型目录失败 tool=%s", tool)
            got = ()
        if not got:
            got = tuple(ModelInfo(n, CLAUDE_EFFORTS if tool == "claude" else DEFAULT_EFFORTS)
                        for n in FALLBACK.get(tool, ()))
        with self._lock:
            self._cache[tool] = (self._now(), got)
        return got

    def names(self, tool: str) -> tuple:
        return tuple(m.name for m in self.models(tool))

    def info(self, tool: str, name: str) -> Optional[ModelInfo]:
        return next((m for m in self.models(tool) if m.name == name), None)

    def efforts(self, tool: str, name: str = "") -> tuple:
        got = self.info(tool, name) if name else None
        if got is not None:
            return got.efforts
        return CLAUDE_EFFORTS if tool == "claude" else DEFAULT_EFFORTS

    def has(self, tool: str, name: str) -> bool:
        return name in self.names(tool)

    def _read(self, tool: str) -> tuple:
        if tool == "claude":
            return self._read_claude()
        if tool == "codex":
            return self._read_codex()
        return ()

    def _read_claude(self) -> tuple:
        with open(self._claude, encoding="utf-8") as handle:
            data = json.load(handle)
        opts = ((data.get("modelPicker") or {}).get("options") or []) if isinstance(data, dict) else []
        native, routed = [], []
        for opt in opts:
            name = opt.get("model") if isinstance(opt, dict) else None
            if not safe_name(name):
                continue
            (routed if opt.get("behavesAs") else native).append(name)
        names = list(dict.fromkeys(native + routed))[:MAX_LIST]
        current = data.get("model") if isinstance(data, dict) else None
        if safe_name(current) and current not in names:
            names.insert(0, current)
        return tuple(ModelInfo(n, CLAUDE_EFFORTS) for n in names)

    def _read_codex(self) -> tuple:
        path = ""
        cfg = os.path.join(self._codex_home, "config.toml")
        try:
            with open(cfg, encoding="utf-8") as handle:
                for line in handle:
                    match = re.match(r'^\s*model_catalog_json\s*=\s*"([^"]+)"', line)
                    if match:
                        path = match.group(1)
                        break
        except OSError:
            return ()
        if not path:
            return ()
        path = os.path.expanduser(path)
        if not os.path.isabs(path):
            path = os.path.join(self._codex_home, path)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        rows = data.get("models") if isinstance(data, dict) else data
        out = []
        for row in sorted((r for r in rows or [] if isinstance(r, dict)), key=lambda r: r.get("priority") or 999):
            name = row.get("slug")
            if not safe_name(name) or row.get("visibility") != "list":
                continue
            levels = []
            for level in row.get("supported_reasoning_levels") or []:
                effort = level.get("effort") if isinstance(level, dict) else level
                if isinstance(effort, str) and _EFFORT_RE.match(effort):
                    levels.append(effort)
            default = row.get("default_reasoning_level") or ""
            out.append(ModelInfo(name, tuple(levels) or DEFAULT_EFFORTS,
                                 default if isinstance(default, str) else ""))
            if len(out) >= MAX_LIST:
                break
        return tuple(out)


@dataclass(frozen=True)
class Choice:
    model: str = ""
    effort: str = ""

    def text(self) -> str:
        if not self.model and not self.effort:
            return "工具默认"
        effort = f" / {EFFORT_LABELS.get(self.effort, self.effort)}" if self.effort else ""
        return f"{self.model or '工具默认'}{effort}"


class ModelPrefs:
    """落盘的默认模型（按工具）和会话级覆盖（按 tool:sid）。"""

    MAX_SESSIONS = 300

    def __init__(self, path: str, catalog: Catalog) -> None:
        self._path = path
        self.catalog = catalog
        self._lock = threading.Lock()
        self._defaults: dict = {}
        self._sessions: dict = {}
        self.last_save_ok = True
        self._load()

    def _load(self) -> None:
        try:
            with open(self._path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for tool, row in (data.get("defaults") or {}).items():
            got = self._clean(tool, row)
            if got is not None:
                self._defaults[tool] = got
        for key, row in (data.get("sessions") or {}).items():
            tool = str(key).split(":", 1)[0]
            got = self._clean(tool, row)
            if got is not None:
                self._sessions[str(key)] = got

    @staticmethod
    def _clean(tool: str, row: object) -> Optional[Choice]:
        if tool not in TOOLS or not isinstance(row, dict):
            return None
        model = row.get("model") or ""
        effort = row.get("effort") or ""
        if model and not safe_name(model):
            return None
        if effort and not (isinstance(effort, str) and _EFFORT_RE.match(effort)):
            effort = ""
        return Choice(model, effort)

    def _save(self) -> bool:
        payload = {
            "defaults": {t: {"model": c.model, "effort": c.effort} for t, c in self._defaults.items()},
            "sessions": {k: {"model": c.model, "effort": c.effort} for k, c in self._sessions.items()},
        }
        try:
            directory = os.path.dirname(self._path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory or None, delete=False) as tmp:
                json.dump(payload, tmp, ensure_ascii=False)
                tmp_path = tmp.name
            os.replace(tmp_path, self._path)
            self.last_save_ok = True
        except OSError:
            logger.exception("模型设置落盘失败")
            self.last_save_ok = False
        return self.last_save_ok

    def _valid(self, tool: str, model: str, effort: str) -> bool:
        if tool not in TOOLS:
            return False
        if model and not self.catalog.has(tool, model):
            return False
        if effort and effort not in self.catalog.efforts(tool, model):
            return False
        return True

    def default(self, tool: str) -> Choice:
        with self._lock:
            return self._defaults.get(tool, Choice())

    def set_default(self, tool: str, model: Optional[str] = None, effort: Optional[str] = None) -> bool:
        with self._lock:
            old = self._defaults.get(tool, Choice())
            new = Choice(old.model if model is None else model, old.effort if effort is None else effort)
            if model is not None and effort is None and new.effort not in self.catalog.efforts(tool, new.model):
                new = Choice(new.model, "")
            if not self._valid(tool, new.model, new.effort):
                return False
            self._defaults[tool] = new
            return self._save()

    def session(self, tool: str, sid: str) -> Choice:
        with self._lock:
            return self._sessions.get(f"{tool}:{sid}", Choice())

    def set_session(self, tool: str, sid: str, model: Optional[str] = None, effort: Optional[str] = None) -> bool:
        if not sid:
            return False
        key = f"{tool}:{sid}"
        with self._lock:
            old = self._sessions.get(key, Choice())
            new = Choice(old.model if model is None else model, old.effort if effort is None else effort)
            if model is not None and effort is None and new.effort not in self.catalog.efforts(tool, new.model):
                new = Choice(new.model, "")
            if not self._valid(tool, new.model, new.effort):
                return False
            self._sessions.pop(key, None)
            self._sessions[key] = new
            while len(self._sessions) > self.MAX_SESSIONS:
                self._sessions.pop(next(iter(self._sessions)))
            return self._save()

    def effective(self, tool: str, sid: str = "") -> Choice:
        """这一轮实际用什么：会话覆盖 > 工具默认 > 工具自己的配置（空串）。"""
        base = self.default(tool)
        own = self.session(tool, sid) if sid else Choice()
        model = own.model or base.model
        effort = own.effort or (base.effort if not own.model or own.model == base.model else "")
        if effort and effort not in self.catalog.efforts(tool, model):
            effort = ""
        return Choice(model, effort)
