"""假的 `claude -p --input-format stream-json --output-format stream-json`（给 bridge/runner.py 用）。

argv[1] = 第一条之后等多久收插话（秒）。每收到一条 user 消息就回显；等完后出一条 assistant 文字和 result。
"""
import json
import os
import select
import sys
import time

got = []
buf = b""


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def next_line(timeout=None):
    global buf
    end = None if timeout is None else time.time() + timeout
    while b"\n" not in buf:
        left = None if end is None else max(0.0, end - time.time())
        ready, _, _ = select.select([0], [], [], left)
        if not ready:
            return None
        chunk = os.read(0, 65536)
        if not chunk:
            return ""
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return line.decode("utf-8")


def take(line):
    msg = json.loads(line)
    got.append(msg["message"]["content"])
    emit({"type": "user", "isReplay": True, "message": msg["message"]})


SID = "22222222-3333-4444-5555-666666666666"
first = next_line()
if not first:
    sys.exit(1)
emit({"type": "system", "subtype": "init", "session_id": SID})
take(first)
wait = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
end = time.time() + wait
while time.time() < end:
    nxt = next_line(max(0.0, end - time.time()))
    if not nxt:
        break
    take(nxt)
reply = "GOT:" + "|".join(got)
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": reply}]}})
emit({"type": "result", "subtype": "success", "result": reply, "session_id": SID, "stop_reason": "end_turn"})
rest = next_line()
while rest:
    take(rest)
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "LATE:" + got[-1]}]}})
    emit({"type": "result", "subtype": "success", "result": "LATE:" + got[-1], "session_id": SID})
    rest = next_line()
