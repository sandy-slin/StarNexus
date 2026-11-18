import json, subprocess, threading, time, sys, queue
p = subprocess.Popen(["codex","app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open("/tmp/steer/codex_err.log","w"), text=True, bufsize=1, cwd="/tmp/steer/work")
q = queue.Queue()
def rd():
    for line in p.stdout:
        try: q.put(json.loads(line))
        except Exception: q.put({"raw": line})
threading.Thread(target=rd, daemon=True).start()
nid = [0]
def req(method, params):
    nid[0] += 1
    p.stdin.write(json.dumps({"id": nid[0], "method": method, "params": params}) + "\n"); p.stdin.flush()
    return nid[0]
def wait_resp(i, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        m = q.get(timeout=end - time.time())
        log(m)
        if m.get("id") == i and ("result" in m or "error" in m): return m
log_f = open("/tmp/steer/codex_events.jsonl", "w")
def log(m): log_f.write(json.dumps(m, ensure_ascii=False) + "\n"); log_f.flush()
r = wait_resp(req("initialize", {"clientInfo": {"name": "sn-probe", "version": "0"}}))
print("init", str(r)[:200])
p.stdin.write(json.dumps({"method": "initialized"}) + "\n"); p.stdin.flush()
r = wait_resp(req("thread/start", {"cwd": "/tmp/steer/work", "sandbox": "workspace-write", "approvalPolicy": "never", "ephemeral": True}))
print("thread", str(r)[:300])
tid = r["result"]["thread"]["id"]
r = wait_resp(req("turn/start", {"threadId": tid, "input": [{"type": "text", "text": "用 shell 依次执行 10 次 `sleep 3 && date >> log.txt`，每次单独执行一条命令，全部做完后回复 DONE-ORIG。"}]}))
print("turn", str(r)[:300])
turn = r["result"]["turn"]["id"]
time.sleep(12)
t0 = time.time()
r = wait_resp(req("turn/steer", {"threadId": tid, "expectedTurnId": turn, "input": [{"type": "text", "text": "改主意了：立即停止剩下的 sleep，不要再执行任何命令，只回复 STEER-OK。"}]}))
print("steer", str(r)[:300], "at", round(t0, 1))
end = time.time() + 240; last = None; kinds = {}
while time.time() < end:
    try: m = q.get(timeout=5)
    except queue.Empty: continue
    log(m); meth = m.get("method", "")
    kinds[meth] = kinds.get(meth, 0) + 1
    if meth == "item/completed":
        it = m["params"]["item"]
        if it.get("type") == "agentMessage": last = it.get("text")
        if it.get("type") == "commandExecution": print("cmd done", round(time.time() - t0, 1), it.get("command", "")[:60])
    if meth == "turn/completed":
        print("turn completed", m["params"]["turn"]["id"] == turn, m["params"]["turn"].get("status")); break
print("last agent msg:", last)
print("log lines:", open("/tmp/steer/work/log.txt").read().count("\n") if __import__("os").path.exists("/tmp/steer/work/log.txt") else 0)
p.terminate()
