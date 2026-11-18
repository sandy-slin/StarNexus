"""假的 `claude -p --input-format stream-json`：读 stdin 的 user 消息，逐条回显，等一会儿看有没有插话，再出 result。

argv[1] = 第一条之后等多久收插话（秒）；argv[2] = "late" ⇒ 等待期间不收，result 之后再处理剩下的（模拟插话撞上收尾）。
用 os.read 自己切行：TextIOWrapper 的 readline 会把后面一行一起读进缓冲区，select 就看不到了。
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


first = next_line()
if not first:
    sys.exit(1)
take(first)
wait = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
late = len(sys.argv) > 2 and sys.argv[2] == "late"
if late:
    time.sleep(wait)
else:
    end = time.time() + wait
    while time.time() < end:
        nxt = next_line(max(0.0, end - time.time()))
        if not nxt:
            break
        take(nxt)
emit({"type": "result", "subtype": "success", "is_error": False, "result": "GOT:" + "|".join(got),
      "session_id": "11111111-2222-3333-4444-555555555555"})
while True:
    rest = next_line()
    if not rest:
        break
    take(rest)
    emit({"type": "result", "subtype": "success", "is_error": False, "result": "LATE:" + got[-1],
          "session_id": "11111111-2222-3333-4444-555555555555"})
