# -*- coding: utf-8 -*-
"""\u6309\u5c5e\u6027\u770b/\u627e/\u6539 \u2014\u2014 \u53f3\u4fa7\u680f"\u6309\u5c5e\u6027\u627e"\u4e0e"\u5c5e\u6027\u7f16\u8f91"\u7684\u540e\u7aef\u9a8c\u6536\u3002"""
import sys
import json
import time
import urllib.error
import urllib.parse
import urllib.request

# 可选位置参数：指向别的服务（临时服务可加载**新代码**，验收改动必须能换地址）
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
FAIL = []
KV, VV = "status", "\u9a8c\u6536\u5f85\u529e"


def call(path, method="GET", body=None, timeout=300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last = None
    for _ in range(20):
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "X-Writer": "tool:props-acc"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception as e:
            last = e
            time.sleep(3)
    raise last


def check(name, cond, extra=""):
    print("  %s %s%s" % ("\u2705" if cond else "\u274c", name, "" if cond else "  | " + str(extra)[:200]))
    if not cond:
        FAIL.append(name)


def tagline(text, key="status"):
    return next((x for x in text.splitlines() if x.startswith(key + ":")), "")


before = call("/api/health")[1]["stats"]["files"]
print("\u2460 \u5168\u5e93\u5c5e\u6027\u7d22\u5f15")
st, idx = call("/api/properties")
check("\u80fd\u5217\u51fa\u5c5e\u6027", st == 200 and idx.get("keys"), idx.get("keys") and len(idx["keys"]))
check("\u6709\u591a\u5c11\u7bc7\u5e26\u5c5e\u6027", (idx.get("files_with_properties") or 0) > 0, idx.get("files_with_properties"))
k0 = (idx.get("keys") or [{}])[0]
check("\u6bcf\u4e2a\u952e\u5e26\u503c\u4e0e\u8ba1\u6570", bool(k0.get("values")) and k0["values"][0].get("count", 0) > 0, k0)
st, one = call("/api/properties?key=" + k0["key"])
check("\u53ea\u770b\u4e00\u4e2a\u952e", len(one.get("keys") or []) == 1 and one["keys"][0]["key"] == k0["key"], one.get("keys"))
hits = call("/api/properties/find?key=%s&value=%s" % (k0["key"], urllib.parse.quote(k0["values"][0]["value"])))[1]
check("\u6309\u5c5e\u6027\u627e\u5f97\u5230\u7b14\u8bb0", hits.get("total", 0) >= 1 and hits["hits"][0]["id"], hits.get("total"))

print("\u2461 \u5efa\u4e00\u7bc7\u5e26\u5c5e\u6027\u7684\u7b14\u8bb0\uff0c\u80fd\u88ab\u7d22\u5f15\u5230")
body = "---\ntitle: \u5c5e\u6027\u53f3\u680f\u9a8c\u6536\ntags: [\u9a8c\u6536]\n%s: %s\n---\n\n# \u6b63\u6587\n\n\u4e00\u884c\n" % (KV, VV)
st, made = call("/api/files", "POST", {"name": "zz-\u5c5e\u6027\u53f3\u680f.md", "content": body})
fid = made.get("file_id")
check("\u5efa\u6210\u529f", bool(fid), made)
st, fi = call("/api/properties/find?key=%s&value=%s" % (KV, urllib.parse.quote(VV)))
check("\u65b0\u5efa\u7684\u90a3\u7bc7\u5728\u7d22\u5f15\u91cc", any(h["id"] == fid for h in (fi.get("hits") or [])), fi.get("total"))

print("\u2462 \u6539\u5c5e\u6027 \u2192 \u5199\u56de\u6587\u4ef6\u5934 YAML")
st, r = call("/api/files/%s/properties" % fid, "POST", {"set": {KV: "\u5df2\u9a8c", "owner": "\u8f9e\u7396"}})
check("\u6539\u6210\u529f", r.get("changed") is True, r)
c = call("/api/files/" + fid)[1]["file"]["content"]
check("YAML \u91cc\u65b0\u503c\u5728", (tagline(c, KV).endswith("\u5df2\u9a8c")) and "owner: \u8f9e\u7396" in c, c[:160])
check("\u5176\u5b83\u952e\u8fd8\u5728\uff08title/tags\uff09", "title: \u5c5e\u6027\u53f3\u680f\u9a8c\u6536" in c and "tags:" in c, c[:200])
st, r2 = call("/api/files/%s/properties" % fid, "POST", {"del": ["owner"]})
check("\u5220\u6389\u4e00\u4e2a\u952e", r2.get("changed") is True and "owner:" not in
      call("/api/files/" + fid)[1]["file"]["content"], r2)
st, idx2 = call("/api/properties?key=" + KV)
vals = [v["value"] for v in idx2["keys"][0]["values"]] if idx2.get("keys") else []
check("\u65b0\u503c\u8fdb\u4e86\u7d22\u5f15", "\u5df2\u9a8c" in vals, vals[:6])

print("\u2463 \u6e05\u573a")
call("/api/files/" + fid, "DELETE")
after = call("/api/health")[1]["stats"]["files"]
check("\u6587\u4ef6\u6570\u590d\u539f", after == before, "%s vs %s" % (after, before))

print("\n========  \u7ed3\u8bba\uff1a%s" % ("\u5168\u90e8\u901a\u8fc7" if not FAIL else "\u6709\u5931\u8d25\u9879\uff1a" + "\u3001".join(FAIL)))
raise SystemExit(1 if FAIL else 0)
