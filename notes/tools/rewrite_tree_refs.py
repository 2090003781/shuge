# -*- coding: utf-8 -*-
"""搬迁后的**路径式引用**批量改锚点（项目/第二大脑 → 第二大脑项目/第二大脑 等）。

为什么还要单独做一遍：`move_folder` 只改 **edges 表里登记过的**路径式锚点（`@a/b/c` 那种）。
散文里的裸路径（`- 项目/第二大脑/待办清单.md`）不在 edges 里，没人认领，得扫。

**只改"活的"文档**：`记录/日志`、`记录/日报`、`记录/周报` 是当时的历史快照，
里面写的 `项目/第二大脑` 在**那一刻是对的**，改了反而是篡改历史 → 跳过。

用法：python tools/rewrite_tree_refs.py          预演
      python tools/rewrite_tree_refs.py --apply  真改
"""
import argparse
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

BASE = "http://127.0.0.1:8848"

# 历史快照目录：不动
FROZEN_PREFIX = ("vault/记录/日志", "vault/记录/日报", "vault/记录/周报")

# (旧, 新) —— 长的先替，免得多半截
RULES = [
    ("项目/第二大脑/", "第二大脑项目/第二大脑/"),
    ("项目/第二大脑", "第二大脑项目/第二大脑"),
    ("second-brain-system/", "第二大脑项目/second-brain-system/"),
    ("系统设计/功能更新/", "第二大脑项目/系统设计/功能更新/"),
    ("系统设计/Bot系统-v2/", "第二大脑项目/系统设计/Bot系统-v2/"),
]


def api(path, body=None, method="GET"):
    url = BASE + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.path.join(HERE, "..", "data", "index.db"))
    a = ap.parse_args()

    import sqlite3
    c = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(a.db).replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    folders = {r["id"]: dict(r) for r in c.execute("SELECT id,parent_id,name FROM folders")}

    def path_of(fid):
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

    hits = []
    for r in c.execute("SELECT id,name,folder_id FROM files"):
        name = r["name"] or ""
        if not name.endswith((".md", ".txt")):
            continue
        rel = path_of(r["folder_id"]) if r["folder_id"] else ""
        if any(rel.startswith(f) for f in FROZEN_PREFIX):
            continue
        try:
            got = api("/api/files/%s?mode=full" % r["id"])
        except Exception:
            continue
        f = got.get("file") or {}
        txt = f.get("content") or f.get("text") or ""
        if not isinstance(txt, str) or not txt:
            continue
        n = 0
        for old, new in RULES:
            if old in txt:
                n += txt.count(old)
                txt = txt.replace(old, new)
        if n:
            hits.append((r["id"], rel + "/" + name, n, txt))

    print("扫到 %d 篇要改锚点：" % len(hits))
    for fid, path, n, _ in hits:
        print("  %-72s %d 处" % (path, n))
    if not a.apply:
        print()
        print("== 预演（加 --apply 才真改）==")
        return 0

    for fid, path, n, txt in hits:
        try:
            r = api("/api/files/%s" % fid, {"content": txt}, method="PUT")
            print("  [已改] %-66s %s" % (path, "ok" if r.get("ok") else "失败"))
        except Exception as e:
            print("  [失败] %-66s %s" % (path, e))
    print()
    print("== 完事 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
