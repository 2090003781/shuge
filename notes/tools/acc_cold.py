# -*- coding: utf-8 -*-
"""\u9a8c\u6536\uff1a\u51b7\u542f\u52a8\u671f\u95f4\u5e93\u4e5f\u80fd\u7528\uff08\u964d\u7ea7\u4e3a\u5173\u952e\u8bcd+\u5173\u7cfb\uff09\uff0c\u6a21\u578b\u5c31\u7eea\u540e\u81ea\u52a8\u5e26\u8bed\u4e49/\u7cbe\u6392\u3002

\u7528\u6cd5\uff1apython tools/acc_cold.py    \uff08\u4f1a\u91cd\u542f\u5e93\uff1a\u6740\u6389\u5f53\u524d\u5b9e\u4f8b\uff0c\u5bbf\u4e3b\u4f1a\u62c9\u56de\uff09
"""
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8848"
LOG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "data", "logs", "notes.out.log")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
FAIL = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("\u2705" if cond else "\u274c", name, "" if cond else "  | " + str(extra)[:200]))
    if not cond:
        FAIL.append(name)


def pids():
    out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                         creationflags=NO_WINDOW).stdout
    got = set()
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[0] == "TCP" and f[1].endswith(":8848") and f[3] == "LISTENING" and f[4] != "0":
            got.add(f[4])
    return got


def search(tag=""):
    q = urllib.parse.quote("\u77e5\u8bc6\u5e93 \u68c0\u7d22")
    t0 = time.time()
    try:
        with urllib.request.urlopen(BASE + "/api/search?q=%s&limit=5&timing=1" % q, timeout=600) as r:
            d = json.loads(r.read().decode("utf-8"))
        return (time.time() - t0) * 1000.0, d
    except Exception as e:
        return None, {"error": "%s: %s" % (type(e).__name__, str(e)[:80])}


print("\u2460 \u91cd\u542f\u5e93\uff08\u6740\u6389\u5f53\u524d\u5b9e\u4f8b\uff0c\u5bbf\u4e3b\u4f1a\u62c9\u56de\uff09")
size = os.path.getsize(LOG) if os.path.isfile(LOG) else 0
for p in pids():
    subprocess.run(["taskkill", "/PID", p, "/F"], capture_output=True, creationflags=NO_WINDOW)
t0 = time.time()
ready = None
while time.time() - t0 < 60:
    time.sleep(0.5)
    try:
        urllib.request.urlopen(BASE + "/api/health", timeout=5)
        ready = time.time() - t0
        break
    except Exception:
        pass
check("\u5e93\u7aef\u53e3\u53c8\u80fd\u7b54\u4e86", ready is not None, ready)
print("     health 200 \u7528\u4e86 %.1f \u79d2" % (ready or -1))

print("\u2461 \u521a\u8d77\u6765\u5c31\u67e5\u4e00\u6b21\uff1a\u4e0d\u80fd\u5361\u5728\u7b49\u6a21\u578b")
ms, d = search()
tim = dict(d.get("timing") or {})
check("\u7b2c\u4e00\u67e5\u5f88\u5feb\uff08< 8 \u79d2\uff09", ms is not None and ms < 8000, ms)
check("\u6709\u547d\u4e2d\uff08\u964d\u7ea7\u4e5f\u8981\u7ed9\u7ed3\u679c\uff09", bool(d.get("hits")), d.get("hits"))
print("     \u7b2c\u4e00\u67e5 %.0f ms\uff1bdegraded=%s rerank=%s \u5404\u6bb5=%s" % (ms or -1, d.get("degraded"),
      d.get("rerank"), tim))
first_degraded = d.get("degraded")

print("\u2462 \u7b49\u6a21\u578b\u5c31\u7eea\uff08\u67e5\u5230 degraded \u4e3a\u7a7a\uff09")
t1 = time.time()
full = None
while time.time() - t1 < 240:
    time.sleep(3)
    ms2, d2 = search()
    if d2.get("degraded") is None:
        full = time.time() - t1
        break
check("\u6a21\u578b\u5c31\u7eea\u540e\u4e0d\u518d\u964d\u7ea7", full is not None, d2.get("degraded"))
if full is not None:
    print("     \u5168\u529f\u80fd\u5c31\u7eea\uff1a\u542f\u52a8\u540e %.1f \u79d2\uff1b\u8fd9\u4e00\u67e5 %.0f ms\uff08\u7cbe\u6392=%s\uff09"
          % ((ready or 0) + full, ms2 or -1, d2.get("rerank")))
    check("\u5168\u529f\u80fd\u65f6\u4e00\u67e5 < 6 \u79d2", ms2 is not None and ms2 < 6000, ms2)
    tim2 = dict(d2.get("timing") or {})
    check("\u5411\u91cf\u7f13\u5b58\u5df2\u9884\u70ed\uff08\u8f7d\u5165 < 50ms\uff09",
          tim2.get("  \u5411\u91cf\u00b7\u8f7d\u5165\u7f13\u5b58", 9e9) < 50, tim2.get("  \u5411\u91cf\u00b7\u8f7d\u5165\u7f13\u5b58"))
    print("      \u5404\u6bb5\uff1a%s" % tim2)
if first_degraded is None:
    print("     \uff08\u6ce8\uff1a\u7b2c\u4e00\u67e5\u65f6\u6a21\u578b\u5df2\u7ecf\u5c31\u7eea\uff0c\u672c\u673a\u52a0\u8f7d\u5f88\u5feb\u6216\u9884\u70ed\u5df2\u5b8c\u6210\uff09")

print("\n========  \u7ed3\u8bba\uff1a%s" % ("\u5168\u90e8\u901a\u8fc7" if not FAIL else "\u6709\u5931\u8d25\u9879\uff1a" + "\u3001".join(FAIL)))
raise SystemExit(1 if FAIL else 0)
