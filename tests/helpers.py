import os
import sys
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bridge.fleet.model import Session  # noqa: E402


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, sec):
        self.t += sec


@dataclass
class View:
    sessions: list
    ok_tools: tuple = ("claude", "codex")
    notes: list = field(default_factory=list)
    at: float = 0.0


@dataclass
class FakeBound:
    chat_id: str
    tool: str
    id: str
    on: bool = True
    name: str = ""


class FakeBinds:
    def __init__(self, rows=()):
        self.rows = {(r.tool, r.id): r for r in rows}

    def of_session(self, tool, sid):
        return self.rows.get((tool, sid))


def sess(tool="claude", sid="s1", status="idle", last_turn="", last_turn_at=0.0, **kw):
    return Session(tool=tool, surface="cli", id=sid, status=status, last_turn=last_turn,
                   last_turn_at=last_turn_at, title=kw.pop("title", "任务"), cwd=kw.pop("cwd", "/tmp/proj"), **kw)
