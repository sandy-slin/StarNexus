"""假的 `codex app-server`（stdio JSON-RPC），给 tests/test_steering_codex.py 用。

argv[1] = turn/start 之后等多久才收尾（秒）。等待期间收 turn/steer / turn/interrupt。
回复文字 = 最后一条插话（没有插话就是第一句）。argv[2] = "fail" ⇒ 这一轮以 failed 结束。
启动参数原样写进环境变量 FAKE_ARGV_OUT 指的文件，方便断言。
"""
import json
import os
import select
import sys
import time

TURN = "turn-1"
wait = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
mode = sys.argv[2] if len(sys.argv) > 2 else ""
if os.environ.get("FAKE_ARGV_OUT"):
    with open(os.environ["FAKE_ARGV_OUT"], "w") as fh:
        json.dump(sys.argv, fh)
buf = b""
log = []


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def next_msg(timeout):
    global buf
    end = time.time() + timeout
    while b"\n" not in buf:
        left = end - time.time()
        if left <= 0:
            return None
        ready, _, _ = select.select([0], [], [], left)
        if not ready:
            return None
        chunk = os.read(0, 65536)
        if not chunk:
            sys.exit(0)
        buf += chunk
    line, buf = buf.split(b"\n", 1)
    return json.loads(line.decode("utf-8")) if line.strip() else {}


first = ""
turn_started_at = None
interrupted = False
while True:
    timeout = 30.0 if turn_started_at is None else max(0.0, turn_started_at + wait - time.time())
    msg = next_msg(timeout)
    if msg is None:
        break
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    log.append({"method": method, "params": params})
    if method == "initialize":
        emit({"id": rid, "result": {"userAgent": "fake"}})
    elif method == "thread/resume":
        emit({"id": rid, "result": {"thread": {"id": params["threadId"]}}})
    elif method == "turn/start":
        first = params["input"][0]["text"]
        emit({"id": rid, "result": {"turn": {"id": TURN, "status": "inProgress"}}})
        turn_started_at = time.time()
    elif method == "turn/steer":
        if params.get("expectedTurnId") != TURN:
            emit({"id": rid, "error": {"code": -32600, "message": "expected turn mismatch"}})
        else:
            first = params["input"][0]["text"]
            emit({"id": rid, "result": {"turnId": TURN}})
    elif method == "turn/interrupt":
        emit({"id": rid, "result": {}})
        interrupted = True
        break
if os.environ.get("FAKE_LOG_OUT"):
    with open(os.environ["FAKE_LOG_OUT"], "w") as fh:
        json.dump(log, fh, ensure_ascii=False)
if interrupted:
    emit({"method": "turn/completed", "params": {"threadId": "x", "turn": {"id": TURN, "status": "interrupted"}}})
elif mode == "fail":
    emit({"method": "turn/completed", "params": {"threadId": "x", "turn": {
        "id": TURN, "status": "failed", "error": {"message": "额度用完了"}}}})
else:
    emit({"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "回：" + first}}})
    emit({"method": "turn/completed", "params": {"threadId": "x", "turn": {"id": TURN, "status": "completed"}}})
# 等 stdin 关掉再退（和真的 app-server 一样）
while True:
    ready, _, _ = select.select([0], [], [], 5)
    if not ready or not os.read(0, 65536):
        break
