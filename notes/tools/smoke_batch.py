"""统一批量入口 `POST /api/batch` 的验收。

辞玖 2026-10-04 定的规矩：批量**只许有一个入口**（`apidoc.BATCH_OPS` 是白名单唯一真源），
新增操作登记进目录就自动可批量 —— 不许再为某个操作单开 `*_batch` 路由。

    python tools/smoke_batch.py
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
FAILED = []


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    if not cond:
        FAILED.append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


made_f, made_d = [], []
try:
    print("① 造三篇 + 一个文件夹")
    ids = []
    for i in (1, 2, 3):
        st, d = call("/api/files", "POST", {"name": "zz-批量-%d" % i, "format": "md",
                                            "content": "# 批量 %d\n" % i})
        made_f.append(d.get("file_id"))
        ids.append(d["file_id"])
    st, fo = call("/api/folders", "POST", {"name": "zz-批量-去处"})
    made_d.append(fo.get("folder_id"))
    check("三篇 + 一个文件夹建好", len(ids) == 3 and fo.get("folder_id"), ids)

    print("② dry_run：只列要做什么，不动手")
    st, d = call("/api/batch", "POST", {"op": "tag", "ids": ids, "dry_run": True,
                                        "args": {"tags": ["zz-批"], "mode": "add"}})
    f0 = call("/api/files/" + ids[0])[1]["file"]
    check("回执说 dry_run 且列了 3 个目标",
          st == 200 and d.get("dry_run") and d.get("count") == 3 and len(d.get("results") or []) == 3, d)
    check("真的没动（标签还是空的）", not f0["tags"], f0["tags"])

    print("③ 真跑：批量打标签")
    st, d = call("/api/batch", "POST", {"op": "tag", "ids": ids,
                                        "args": {"tags": ["zz-批"], "mode": "add"}})
    got = [call("/api/files/" + i)[1]["file"]["tags"] for i in ids]
    check("三篇都打上了", st == 200 and d.get("done") == 3 and all("zz-批" in t for t in got), got)

    print("④ 批量进出检索库 + 批量移动")
    st, d = call("/api/batch", "POST", {"op": "rag", "ids": ids, "args": {"on": False}})
    rags = [call("/api/files/" + i)[1]["file"]["rag"] for i in ids]
    check("三篇都出了检索库", d.get("done") == 3 and all(x == "off" for x in rags), rags)
    st, d = call("/api/batch", "POST", {"op": "move", "ids": ids,
                                        "args": {"folder_id": fo["folder_id"]}})
    fol = [call("/api/files/" + i)[1]["file"]["path"] for i in ids]
    check("三篇都挪进那个文件夹", d.get("done") == 3 and all("zz-批量-去处" in p for p in fol), fol)

    print("⑤ 出错怎么办：continue（默认）vs stop")
    st, d = call("/api/batch", "POST", {"op": "visibility",
                                        "ids": ["no-such-id"] + ids[:2],
                                        "args": {"visibility": "internal"}})
    check("默认 continue：坏的那个进 failed，后面两个照做",
          d.get("done") == 2 and len(d.get("failed") or []) == 1, d)
    st, d = call("/api/batch", "POST", {"op": "visibility", "on_error": "stop",
                                        "ids": ["no-such-id"] + ids[:2],
                                        "args": {"visibility": "internal"}})
    check("stop：第一处错就停（后面一个都没做）",
          d.get("done") == 0 and len(d.get("failed") or []) == 1 and d.get("count") == 3, d)

    print("⑥ 该拒的拒")
    st, d = call("/api/batch", "POST", {"op": "没这个操作", "ids": ids})
    check("不认识的 op → 400 且列出白名单",
          st == 400 and "rag" in (d.get("error") or ""), d.get("error"))
    st, d = call("/api/batch", "POST", {"op": "tag", "ids": [], "args": {}})
    check("没给目标 → 400", st == 400, d.get("error"))
    st, d = call("/api/batch", "POST", {"op": "delete", "ids": [ids[0]], "dry_run": True})
    check("危险操作也能 dry_run（delete 在白名单里，先看不动手）",
          st == 200 and d.get("dry_run") and not d.get("done"), d)
    st, d = call("/api/files/" + ids[0])
    check("dry_run 过的还没被删", st == 200, st)
finally:
    print("⑦ 收摊")
    st, d = call("/api/batch", "POST", {"op": "delete", "ids": [i for i in made_f if i]})
    print("   批量删掉 %s 篇（done=%s failed=%s）" % (len(made_f), d.get("done"), len(d.get("failed") or [])))
    for x in [i for i in made_d if i]:
        call("/api/folders/" + x, "DELETE")

print("=" * 70)
print("通过" if OK else "失败：%s" % "、".join(FAILED))
sys.exit(0 if OK else 1)
