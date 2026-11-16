# -*- coding: utf-8 -*-
"""\u524d\u7f6e\u5c5e\u6027\uff08Obsidian "\u5c5e\u6027" = YAML frontmatter\uff09\u9a8c\u6536\uff1a
\u5bfc\u5165\u65b9\u5411\uff08tags/title/date \u5e26\u8fdb\u5e93\uff09+ \u5e93\u65b9\u5411\uff08\u6539\u6807\u7b7e\u5199\u56de YAML\uff09+ \u53cc\u5411\u5bf9\u8d34\u3002
"""
import sys
import json
import time
import urllib.error
import urllib.request

# 可选位置参数：指向别的服务（临时服务可加载**新代码**，验收改动必须能换地址）
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
FAIL = []
T1, T2 = "\u9a8c\u6536\u7532", "\u9a8c\u6536\u4e59"


def call(path, method="GET", body=None, timeout=300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    last = None
    for _try in range(20):                       # \u5e93\u88ab\u5185\u5b58\u538b\u529b\u62d6\u4f4f\u65f6\u4f1a\u5076\u53d1\u62d2\u8fde \u2192 \u7b49\u4e00\u4e0b\u91cd\u8bd5
        req = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "X-Writer": "tool:fm-acc"})
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


def tagline(text):
    return next((x for x in text.splitlines() if x.startswith("tags:")), "")


files_before = call("/api/health")[1]["stats"]["files"]
body = ("---\ntitle: \u524d\u7f6e\u5c5e\u6027\u9a8c\u6536\ntags: [%s, %s]\ncreated: 2026-03-04\nstatus: \u5f85\u529e\n---\n\n"
        "# \u6b63\u6587\n\n- [ ] \u5f85\u529e\u4e00\n- [x] \u5df2\u5b8c\u6210\u4e8c\n" % (T1, T2))
st, made = call("/api/files", "POST", {"name": "zz-\u524d\u7f6e\u5c5e\u6027.md", "content": body})
fid = made.get("file_id")
check("\u5efa\u7b14\u8bb0\u6210\u529f", bool(fid), made)

print("\u2460 \u5bfc\u5165\u65b9\u5411\uff1aYAML \u91cc\u7684\u6807\u7b7e/\u6807\u9898/\u65e5\u671f\u8fdb\u5e93")
st, d = call("/api/files/" + fid)
f = d["file"]
check("title \u7528\u524d\u7f6e\u5c5e\u6027\u7684\uff08\u4e0d\u662f\u6587\u4ef6\u540d\uff09", f.get("title") == "\u524d\u7f6e\u5c5e\u6027\u9a8c\u6536", f.get("title"))
check("\u6807\u7b7e\u8fdb\u4e86\u5e93", {T1, T2} <= set(f.get("tags") or []), f.get("tags"))
check("doc_date \u7528 YAML \u7684 created", f.get("doc_date") == "2026-03-04", f.get("doc_date"))
check("\u5176\u5b83\u952e\u4e5f\u5b58\u4e0b\u6765\u4e86\uff08status\uff09", (f.get("frontmatter") or {}).get("status") == "\u5f85\u529e",
      f.get("frontmatter"))
check("\u5185\u90e8\u8bb0\u8d26\u952e\u4e0d\u5bf9\u5916\u9732", all(not k.startswith("_") for k in (f.get("frontmatter") or {})),
      f.get("frontmatter"))
check("\u5f85\u529e\u539f\u6587\u4e00\u5b57\u672a\u52a8", "- [ ] \u5f85\u529e\u4e00" in (f.get("content") or ""))

print("\u2461 \u5e93\u65b9\u5411\uff1a\u6539\u6807\u7b7e \u2192 \u5199\u56de\u6587\u4ef6\u5934\u7684 YAML")
st, r = call("/api/files/%s/tags" % fid, "POST", {"tags": [T1, "\u65b0\u6807\u7b7e"], "mode": "set"})
check("\u6807\u7b7e\u5df2\u6539", set(r.get("tags") or []) == {T1, "\u65b0\u6807\u7b7e"}, r.get("tags"))
check("\u8bf4\u660e\u4e86\u5df2\u5199\u56de", r.get("frontmatter_synced") is True, r.get("frontmatter_synced"))
st, d2 = call("/api/files/" + fid)
c2 = d2["file"].get("content") or ""
tl = tagline(c2)
check("\u6b63\u6587\u91cc\u7684 YAML \u8ddf\u7740\u53d8\u4e86\uff08\u4e24\u4e2a\u6807\u7b7e\u90fd\u5728\uff09", T1 in tl and "\u65b0\u6807\u7b7e" in tl, tl)
check("\u5176\u5b83\u952e\u8fd8\u5728\uff08status/created \u6ca1\u88ab\u541e\uff09",
      "status: \u5f85\u529e" in c2 and "created: 2026-03-04" in c2, c2[:160])
check("\u5e93\u6807\u7b7e\u4e5f\u5bf9\u5f97\u4e0a", set(d2["file"].get("tags") or []) == {T1, "\u65b0\u6807\u7b7e"},
      d2["file"].get("tags"))

print("\u2462 \u53cc\u5411\u5bf9\u8d34\uff1a\u76f4\u63a5\u6539\u6b63\u6587\u91cc\u7684 YAML \u6807\u7b7e \u2192 \u5e93\u6807\u7b7e\u8ddf\u7740\u64a4")
c3 = "\n".join(("tags: [%s]" % T1) if x.startswith("tags:") else x for x in c2.splitlines())
st, put = call("/api/files/" + fid, "PUT", {"content": c3, "allow_shrink": True})
check("\u4fdd\u5b58\u6210\u529f", st == 200 and put.get("ok"), put.get("error"))
st, d3 = call("/api/files/" + fid)
check("\u5e93\u6807\u7b7e\u53ea\u5269\u9a8c\u6536\u7532\uff08\u65b0\u6807\u7b7e\u88ab\u64a4\uff09",
      set(d3["file"].get("tags") or []) == {T1}, d3["file"].get("tags"))

print("\u2463 \u6e05\u573a")
call("/api/files/" + fid, "DELETE")
after = call("/api/health")[1]["stats"]["files"]
check("\u6587\u4ef6\u6570\u590d\u539f", after == files_before, "%s vs %s" % (after, files_before))

print("\n========  \u7ed3\u8bba\uff1a%s" % ("\u5168\u90e8\u901a\u8fc7" if not FAIL else "\u6709\u5931\u8d25\u9879\uff1a" + "\u3001".join(FAIL)))
raise SystemExit(1 if FAIL else 0)
