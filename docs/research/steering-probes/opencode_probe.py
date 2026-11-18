import json, subprocess, time, os
def api(method, path, data=None, params=()):
    argv = ["opencode", "api", method, path]
    for p in params: argv += ["--param", p]
    if data is not None: argv += ["-d", json.dumps(data, ensure_ascii=False)]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    try: return json.loads(r.stdout)
    except Exception: return {"raw": r.stdout[:300], "err": r.stderr[:300]}
s = api("POST", "/api/session", {"title": "sn-steer-probe", "location": {"directory": "/tmp/steer/owork"},
        "model": {"providerID": "cpa-gui", "id": "gpt-5.6-luna"},
        "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]})
print("create", str(s)[:300])
sid = (s.get("data") or s).get("id")
r = api("POST", f"/api/session/{sid}/prompt", {"text": "用 bash 依次执行 10 次 `sleep 3 && date >> log.txt`，每次单独调用一次工具，全部做完后回复 DONE-ORIG。"})
print("prompt", str(r)[:300])
t0 = time.time()
while time.time() - t0 < 25:
    time.sleep(2)
time.sleep(0)
r = api("POST", f"/api/session/{sid}/prompt", {"text": "改主意了：立即停止剩下的 sleep，不要再调用任何工具，只回复 STEER-OK。", "delivery": "steer"})
print("steer", str(r)[:300], "lines-at-steer", open("/tmp/steer/owork/log.txt").read().count("\n") if os.path.exists("/tmp/steer/owork/log.txt") else 0)
end = time.time() + 200
while time.time() < end:
    time.sleep(4)
    act = api("GET", "/api/session/active")
    if sid not in (act.get("data") or {}): break
print("idle after", round(time.time() - t0, 1))
m = api("GET", f"/api/session/{sid}/message")
json.dump(m, open("/tmp/steer/oc_msgs.json", "w"), ensure_ascii=False)
print("log lines", open("/tmp/steer/owork/log.txt").read().count("\n") if os.path.exists("/tmp/steer/owork/log.txt") else 0)
print("sid", sid)
