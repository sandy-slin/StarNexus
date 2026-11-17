"""/ns 起的会话名和目录落盘，重启后群名、状态卡、对话标签照样用这个名字。"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading

logger = logging.getLogger("feishu-bridge")

STATE_FILE = "session_names.json"
MAX_ROWS = 500


class SessionNames:
    """sid → {"name", "cwd"}。线程安全，原子写盘；读坏了当成空表。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._rows: dict = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:  # noqa: BLE001
            logger.exception("会话名记录读不出来，当成没有")
            return
        if isinstance(raw, dict):
            self._rows = {str(k): v for k, v in raw.items() if isinstance(v, dict)}

    def _save(self) -> None:
        if not self.path:
            return
        folder = os.path.dirname(self.path) or "."
        try:
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".session_names.")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._rows, fh, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:  # noqa: BLE001
            logger.exception("会话名记录写不进去")

    def put(self, sid: str, *, name: str = "", cwd: str = "") -> None:
        if not sid or not (name or cwd):
            return
        with self._lock:
            row = dict(self._rows.get(sid) or {})
            if name:
                row["name"] = str(name)
            if cwd:
                row["cwd"] = str(cwd)
            self._rows.pop(sid, None)
            self._rows[sid] = row
            while len(self._rows) > MAX_ROWS:
                self._rows.pop(next(iter(self._rows)))
            self._save()

    def names(self) -> dict:
        with self._lock:
            return {k: v["name"] for k, v in self._rows.items() if v.get("name")}

    def cwds(self) -> dict:
        with self._lock:
            return {k: v["cwd"] for k, v in self._rows.items() if v.get("cwd")}
