"""这一轮在做什么（FC-14，§5.4）：读会话记录里**发出之后新增**的那段，汇总成进展卡上的几行。

形状以实测为准（2026-11-17，本机记录）：
- Claude jsonl：`{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":…}}]}}`
- Codex rollout：`{"type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\\"cmd\\":…}"}}`，
  改文件走 `custom_tool_call` name=apply_patch，input 里有 `*** Update File: <路径>`。
只读新增部分（发之前记下字节数），一次最多读 2MB；读坏了就当没进展，不影响等待卡本身。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

MAX_READ = 2 * 1024 * 1024
LAST_CHARS = 60
FILES_SHOWN = 3

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)
_TOOL_LABEL = {"Bash": "跑命令", "exec_command": "跑命令", "shell": "跑命令", "Read": "读文件",
               "Edit": "改文件", "Write": "写文件", "MultiEdit": "改文件", "apply_patch": "改文件",
               "Grep": "搜索", "Glob": "找文件", "WebFetch": "查网页", "WebSearch": "搜网页",
               "Task": "派子任务", "TodoWrite": "列计划"}


@dataclass
class Progress:
    tools: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    last: str = ""
    delivered: bool = False
    replying: bool = False

    @property
    def stage(self) -> str:
        """§5.2 发送三步：sent（已发送）→ delivered（会话记录里出现了这句）→ replying（它开始回了）。"""
        if self.replying:
            return "replying"
        return "delivered" if self.delivered else "sent"

    def add_tool(self, name: str, detail: str = "") -> None:
        label = _TOOL_LABEL.get(name, name)
        self.tools[label] = self.tools.get(label, 0) + 1
        one = " ".join(str(detail or "").split())
        self.last = f"{label}：{one[:LAST_CHARS]}" if one else label

    def add_file(self, path: str) -> None:
        path = str(path or "").strip()
        if path and path not in self.files:
            self.files.append(path)

    def lines(self, clean=lambda s: s) -> list:
        if not self.tools:
            return []
        used = "、".join(f"{k}×{v}" for k, v in self.tools.items())
        out = [f"🔧 用了：{used}"]
        if self.files:
            names = [os.path.basename(p) or p for p in self.files[-FILES_SHOWN:]]
            more = f" 等 {len(self.files)} 个" if len(self.files) > FILES_SHOWN else ""
            out.append(f"📝 改了：{'、'.join(names)}{more}")
        if self.last:
            out.append(f"▶️ 正在：{self.last}")
        return [clean(x) for x in out]


def mark(path: str) -> int:
    try:
        return os.path.getsize(path) if path else 0
    except OSError:
        return 0


def _new_records(path: str, offset: int):
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, offset))
            blob = handle.read(MAX_READ)
    except OSError:
        return
    for raw in blob.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            yield rec


def _detail(inp) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "cmd", "file_path", "pattern", "url", "query", "description"):
        value = inp.get(key)
        if isinstance(value, list):
            value = " ".join(map(str, value))
        if isinstance(value, str) and value.strip():
            return value
    return ""


_CODEX_REPLY_ITEMS = {"reasoning", "function_call", "custom_tool_call", "local_shell_call", "web_search_call"}
_CODEX_REPLY_EVENTS = {"agent_message", "agent_reasoning", "exec_command_begin"}


def _mark_stage(got: Progress, rec: dict) -> None:
    kind = rec.get("type")
    if rec.get("isSidechain"):
        return
    if kind == "user":
        content = (rec.get("message") or {}).get("content")
        if isinstance(content, str) or any(
                isinstance(b, dict) and b.get("type") == "text" for b in (content if isinstance(content, list) else ())):
            got.delivered = True
        return
    if kind == "assistant":
        got.delivered = got.replying = True
        return
    payload = rec.get("payload")
    if not isinstance(payload, dict):
        return
    sub = payload.get("type")
    if kind == "event_msg":
        if sub in ("user_message", "task_started"):
            got.delivered = True
        elif sub in _CODEX_REPLY_EVENTS:
            got.delivered = got.replying = True
    elif kind == "response_item":
        if sub == "message" and payload.get("role") == "user":
            got.delivered = True
        elif (sub == "message" and payload.get("role") == "assistant") or sub in _CODEX_REPLY_ITEMS:
            got.delivered = got.replying = True


def read_progress(path: str, offset: int) -> Progress:
    got = Progress()
    if not path:
        return got
    for rec in _new_records(path, offset):
        _mark_stage(got, rec)
        if rec.get("type") == "assistant" and not rec.get("isSidechain"):
            content = (rec.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else ():
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name, inp = str(block.get("name") or ""), block.get("input")
                    got.add_tool(name, _detail(inp))
                    if name in _EDIT_TOOLS and isinstance(inp, dict):
                        got.add_file(inp.get("file_path") or inp.get("notebook_path") or "")
            continue
        payload = rec.get("payload")
        if rec.get("type") != "response_item" or not isinstance(payload, dict):
            continue
        kind, name = payload.get("type"), str(payload.get("name") or "")
        if kind == "function_call":
            try:
                args = json.loads(payload.get("arguments") or "{}")
            except ValueError:
                args = {}
            got.add_tool(name, _detail(args))
        elif kind == "custom_tool_call":
            text = str(payload.get("input") or "")
            if name == "apply_patch":
                files = _PATCH_FILE.findall(text)
                got.add_tool(name, ", ".join(os.path.basename(f) for f in files))
                for f in files:
                    got.add_file(f)
            else:
                got.add_tool(name, text)
        elif kind == "local_shell_call":
            got.add_tool("shell", _detail((payload.get("action") or {})))
    return got
