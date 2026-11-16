# -*- coding: utf-8 -*-
"""\u9a8c\u6536\uff1a\u67e5\u8be2\u7f13\u5b58 + \u5411\u91cf\u7f13\u5b58\u589e\u91cf\u5237\u65b0\uff08\u5199\u5b8c\u7acb\u523b\u53ef\u68c0\u7d22\uff0c\u4e14\u4e0d\u6574\u8868\u91cd\u8f7d\uff09"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8848"
FAIL = []
TOKEN = "ZZQ\u7f13\u5b58\u9a8c\u8bc1\u4ee4\u724c2026"


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "X-Writer": "tool:cache-acc"})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


def check(name, cond, extra=""):
    print("  %s %s%s" % ("\u2705" if cond else "\u274c", name, "" if cond else "  | " + str(extra)[:200]))
    if not cond:
        FAIL.append(name)


# 验收前先热一次：把向量缓存建起来（否则"没整表重载"这条就不成立：
# 它本来就还没建，第一查必然要读一次）。跑在合集里时前面的用例会把缓存打掉，更需要这一步。
call("/api/search?q=%s&limit=3&rerank=0" % urllib.parse.quote("热一下缓存"))

before = call("/api/health")[1]["stats"]["files"]
print("\u2460 \u5efa\u4e00\u7bc7\u4e34\u65f6\u7b14\u8bb0\u5e76\u6539\u4e00\u7248\uff08\u6362\u5757 \u2192 \u5411\u91cf\u4e5f\u6362\uff09")
st, made = call("/api/files", "POST", {"name": "zz-\u7f13\u5b58\u9a8c\u8bc1.md",
                                       "content": "# \u7f13\u5b58\u9a8c\u8bc1\n\n\u5148\u7a7a\u7740\u3002\n"})
fid = made.get("file_id")
check("\u65b0\u5efa\u6210\u529f", bool(fid), made)
st, put = call("/api/files/" + fid, "PUT",
               {"content": "# \u7f13\u5b58\u9a8c\u8bc1\n\n\u8fd9\u4e00\u884c\u53ea\u6709\u65b0\u7248\u6709\uff1a%s \u9760\u8fd1\u4e49\u8bed\u4e49\u627e\u5f97\u5230\u3002\n" % TOKEN,
                "allow_shrink": True})
check("\u4fdd\u5b58\u6210\u529f\uff08\u6362\u4e86\u5757\uff09", st == 200 and put.get("ok"), put)

time.sleep(0.2)
print("\u2461 \u4fdd\u5b58\u540e\u7acb\u523b\u68c0\u7d22\uff1a\u5411\u91cf\u901a\u9053\u770b\u5f97\u89c1\u65b0\u5757\uff0c\u4e14\u6ca1\u6574\u8868\u91cd\u8f7d")
q = urllib.parse.quote(TOKEN)
t0 = time.time()
d = call("/api/search?q=%s&limit=5&timing=1&rerank=0" % q)[1]
wall = (time.time() - t0) * 1000
tim = dict(d.get("timing") or [])
hits = d.get("hits") or []
mine = [h for h in hits if h.get("file_id") == fid]
check("\u65b0\u5185\u5bb9\u80fd\u88ab\u68c0\u7d22\u5230", bool(mine), hits[:2])
check("\u800c\u4e14\u8d70\u7684\u662f\u5411\u91cf\u901a\u9053", bool(mine) and "vec" in (mine[0].get("channels") or []),
      mine[0].get("channels") if mine else None)
check("\u6ca1\u6574\u8868\u91cd\u8f7d\u5411\u91cf\uff08\u8f7d\u5165\u7f13\u5b58 < 50ms\uff09", tim.get("  \u5411\u91cf\u00b7\u8f7d\u5165\u7f13\u5b58", 9e9) < 50,
      tim.get("  \u5411\u91cf\u00b7\u8f7d\u5165\u7f13\u5b58"))
print("     \u5899\u4e0a %.0f ms\uff1b\u5404\u6bb5\uff1a%s" % (wall, tim))

print("\u2462 \u540c\u4e00\u53e5\u8bdd\u518d\u67e5\u4e00\u6b21\uff1a\u67e5\u8be2\u5411\u91cf\u8d70\u7f13\u5b58")
d2 = call("/api/search?q=%s&limit=5&timing=1&rerank=0" % q)[1]
tim2 = dict(d2.get("timing") or [])
check("\u67e5\u8be2\u5411\u91cf\u5316 < 20ms\uff08\u547d\u4e2d\u7f13\u5b58\uff09", tim2.get("  \u5411\u91cf\u00b7\u67e5\u8be2\u5411\u91cf\u5316", 9e9) < 20,
      tim2.get("  \u5411\u91cf\u00b7\u67e5\u8be2\u5411\u91cf\u5316"))
check("\u7b2c\u4e8c\u6b21\u4e5f\u80fd\u627e\u5230", bool([h for h in (d2.get("hits") or []) if h.get("file_id") == fid]))

print("\u2463 \u5220\u6389\u4e34\u65f6\u7b14\u8bb0\uff1a\u6587\u4ef6\u6570\u590d\u539f + \u5411\u91cf\u7f13\u5b58\u91cc\u4e5f\u6ca1\u4e86")
call("/api/files/" + fid, "DELETE")
call("/api/search?q=%s&limit=5&timing=1&rerank=0" % q)
after = call("/api/health")[1]["stats"]["files"]
check("\u6587\u4ef6\u6570\u590d\u539f", after == before, "%s vs %s" % (after, before))
d3 = call("/api/search?q=%s&limit=5&timing=1&rerank=0" % q)[1]
check("\u5220\u540e\u4e0d\u518d\u547d\u4e2d", not [h for h in (d3.get("hits") or []) if h.get("file_id") == fid])

print("\n========  \u7ed3\u8bba\uff1a%s" % ("\u5168\u90e8\u901a\u8fc7" if not FAIL else "\u6709\u5931\u8d25\u9879\uff1a" + "\u3001".join(FAIL)))
raise SystemExit(1 if FAIL else 0)
