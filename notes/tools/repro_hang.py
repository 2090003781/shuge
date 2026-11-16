# -*- coding: utf-8 -*-
"""\u590d\u73b0"\u5e93\u5361\u6b7b"\uff1a\u5bc6\u96c6\u955c\u50cf\u540c\u6b65 + \u5efa/\u6539/\u5220 \u4ea4\u9519\u3002

\u7528\u6cd5\uff1apython tools/repro_hang.py [\u8f6e\u6570]
\u6293\u6808\uff1a\u5148\u8c03 /api/debug/stack-dump?after=40 \u628a\u5b9a\u65f6\u5668\u88c5\u597d\uff0c\u5361\u4f4f\u65f6\u81ea\u5df1\u4f1a\u628a\u6808\u5199\u8fdb data/logs/notes.out.log
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8848"
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 12


def call(path, method="GET", body=None, timeout=120):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Writer": "tool:repro"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def health(timeout=5):
    try:
        return call("/api/health", timeout=timeout).get("ok") is True
    except Exception:
        return False


print("\u88c5\u6293\u6808\u5b9a\u65f6\u5668\uff1a40 \u79d2\u540e\u5f00\u59cb\u6bcf 40 \u79d2\u6253\u4e00\u6b21\u6808")
try:
    print(" ", call("/api/debug/stack-dump", "POST", {"after": 40, "repeat": 1}, timeout=30))
except Exception as e:
    print("  \u88c5\u4e0d\u4e0a\uff08\u65e7\u7248\u672c\uff1f\uff09\uff1a%s" % e)

stop = threading.Event()
syncs = {"n": 0, "err": 0}


def sync_loop():
    while not stop.is_set():
        try:
            call("/api/tree", timeout=300)      # 原先是 /api/mirror/sync（镜像已退役，换成树读压测）
            syncs["n"] += 1
        except Exception:
            syncs["err"] += 1
        time.sleep(0.3)


th = threading.Thread(target=sync_loop, daemon=True)
th.start()
made = []
for i in range(ROUNDS):
    t0 = time.time()
    try:
        fid = call("/api/files", "POST", {"name": "zz-\u5361\u6b7b\u590d\u73b0-%02d.md" % i,
                                           "content": "# \u5361\u6b7b\u590d\u73b0 %d\n\n\u4e00\u884c\u3002\n" % i})["file_id"]
        made.append(fid)
        call("/api/files/" + fid, "PUT",
             {"content": "# \u5361\u6b7b\u590d\u73b0 %d\n\n\u6539\u4e86\u4e00\u884c\uff08\u591a\u4e00\u70b9\u5b57\uff09\u3002\n" % i,
              "allow_shrink": True})
        if i % 3 == 2 and made:
            call("/api/files/" + made.pop(0), "DELETE")
        print("\u7b2c %2d \u8f6e %.1fs\uff08\u955c\u50cf %d \u6b21\uff09" % (i + 1, time.time() - t0, syncs["n"]), flush=True)
    except Exception as e:
        print("\u7b2c %2d \u8f6e\u51fa\u9519\uff1a%s" % (i + 1, str(e)[:90]), flush=True)
    if not health():
        print("!! \u5e93\u4e0d\u7b54\u4e86\uff08\u7b2c %d \u8f6e\uff09" % (i + 1), flush=True)
        break

print("\u6e05\u7406\u5269\u4e0b\u7684\u4e34\u65f6\u7b14\u8bb0\u2026\u2026")
for fid in made:
    try:
        call("/api/files/" + fid, "DELETE", timeout=300)
    except Exception as e:
        print("  \u5220\u4e0d\u6389\uff1a%s" % str(e)[:80])
stop.set()
print("\u7ed3\u675f\uff1a\u955c\u50cf %d \u6b21\uff08\u5931\u8d25 %d\uff09\uff1bhealth=%s" % (syncs["n"], syncs["err"], health()))
