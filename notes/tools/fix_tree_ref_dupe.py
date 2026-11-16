# -*- coding: utf-8 -*-
"""修那次级联重写留下的 `第二大脑第二大脑项目` —— **只做确定性修补，不再做"还原+重做"**。

为什么不能用「还原成旧串再重做一遍」：还原会把**本来就正确**的 `第二大脑项目/第二大脑`
也一并改回旧串，等于把修好的又弄坏；下一遍重做只补回一部分 → 全库反而多出一倍锚点
（2026-10-15 实测：某篇 3 处 → 6 处）。这类"撤销 + 重放"只有能证明幂等时才能用。

这里只干一件事：把那个重复前缀吃掉。
    `第二大脑第二大脑项目/第二大脑` → `第二大脑项目/第二大脑`
（以及 second-brain-system 那一支的同款产物）。它是**幂等**的：改完再扫不会再有。
"""
import argparse
import json
import os
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8848"
FROZEN_PREFIX = ("vault/记录/日志", "vault/记录/日报", "vault/记录/周报")

FIXES = [
    ("第二大脑第二大脑项目/第二大脑", "第二大脑项目/第二大脑"),
    ("第二大脑第二大脑项目/second-brain-system", "第二大脑项目/second-brain-system"),
    ("第二大脑第二大脑项目/系统设计", "第二大脑项目/系统设计"),
]


def api(path, body=None, method="GET"):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def path_of(folders, fid):
    parts, cur = [], fid
    for _ in range(60):
        f = folders.get(cur)
        if not f:
            break
        if f["name"]:
            parts.append(f["name"])
        cur = f["parent_id"]
        if not cur:
            break
    return "/".join(reversed(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=r"D:\core-notes\data\index.db")
    a = ap.parse_args()

    c = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(a.db).replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    folders = {r["id"]: dict(r) for r in c.execute("SELECT id,parent_id,name FROM folders")}

    todo = []
    for r in c.execute("SELECT id,name,folder_id FROM files"):
        name = r["name"] or ""
        if not name.endswith((".md", ".txt")):
            continue
        rel = path_of(folders, r["folder_id"]) if r["folder_id"] else ""
        if any(rel.startswith(f) for f in FROZEN_PREFIX):
            continue
        try:
            f = (api("/api/files/%s" % r["id"]).get("file") or {})
        except Exception:
            continue
        txt = f.get("content") or ""
        if not isinstance(txt, str) or not txt:
            continue
        n = 0
        for old, new in FIXES:
            if old in txt:
                n += txt.count(old)
                txt = txt.replace(old, new)
        if n:
            todo.append((r["id"], rel + "/" + name, n, txt))

    print("要修 %d 篇 / %d 处 `第二大脑第二大脑项目`：" % (len(todo), sum(x[2] for x in todo)))
    for _, p, n, _ in todo:
        print("  %-72s %d" % (p, n))
    if not a.apply:
        print()
        print("== 预演（加 --apply 才真改）==")
        return 0

    ok = bad = 0
    for fid, p, n, txt in todo:
        try:
            r = api("/api/files/%s" % fid, {"content": txt}, method="PUT")
            ok += 1 if r.get("ok") else 0
            bad += 0 if r.get("ok") else 1
        except Exception as e:
            bad += 1
            print("  [异常]", p, e)
    print()
    print("== 完事：成功 %d / 失败 %d ==" % (ok, bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
