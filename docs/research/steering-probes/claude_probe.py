import json, subprocess, threading, time, queue, os, sys
sys.path.insert(0, "/Users/LinSen/work/remote_control")
from bridge.fleet.claude import ClaudeAdapter
ad = ClaudeAdapter()
ENV = ad._send_env("")
_, top = ad._auth(); top = dict(top); top.pop("permissions", None)
cmd = ["claude","-p","--input-format","stream-json","--output-format","stream-json","--verbose","--replay-user-messages","--permission-mode","bypassPermissions","--setting-sources","","--strict-mcp-config","--allowedTools","Bash"] + (["--settings", json.dumps(top)] if top else [])
p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=open("/tmp/steer/claude_err.log","w"), text=True, bufsize=1, cwd="/tmp/steer/cwork", env=ENV)
q = queue.Queue()
def rd():
    for line in p.stdout:
        try: q.put(json.loads(line))
        except Exception: q.put({"raw": line})
    q.put({"type": "EOF"})
threading.Thread(target=rd, daemon=True).start()
logf = open("/tmp/steer/claude_events.jsonl", "w")
def send(text):
    p.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"); p.stdin.flush()
send("用 Bash 工具依次执行 10 次 `sleep 3 && date >> log.txt`，每次单独调用一次工具，全部做完后回复 DONE-ORIG。")
t0 = time.time(); steered = False; results = 0; bash = 0
while time.time() - t0 < 300:
    if not steered and time.time() - t0 > 20:
        send("改主意了：立即停止剩下的 sleep，不要再调用任何工具，只回复 STEER-OK。"); steered = True; ts = time.time(); print("steer sent", round(ts - t0, 1))
    try: m = q.get(timeout=2)
    except queue.Empty: continue
    logf.write(json.dumps(m, ensure_ascii=False) + "\n"); logf.flush()
    t = m.get("type")
    if t == "assistant":
        for c in m["message"].get("content", []):
            if c.get("type") == "tool_use": bash += 1; print("tool_use", round(time.time() - t0, 1))
            if c.get("type") == "text": print("text", round(time.time() - t0, 1), c["text"][:80])
    if t == "user" and m.get("isReplay"): print("replay ack", round(time.time() - t0, 1))
    if t == "result":
        results += 1; print("result", results, round(time.time() - t0, 1), str(m.get("result"))[:80], m.get("session_id"))
        if steered: break
    if t == "EOF": print("EOF"); break
print("bash calls", bash, "log lines", open("/tmp/steer/cwork/log.txt").read().count("\n") if os.path.exists("/tmp/steer/cwork/log.txt") else 0)
p.stdin.close(); p.wait(timeout=30); print("exit", p.returncode)
