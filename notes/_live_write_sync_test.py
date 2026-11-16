# -*- coding: utf-8 -*-
"""验：那些"活跃写入"的篇，盘上与库内是否同步（B6 之后新写的）。只读。

判据：库内 content 非空、非私密的那几篇，盘上那份必须与库内**逐字节相同**；
不一致就是漂移（那正是 `write_through` 该防的）。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-46s %s" % ("✓" if cond else "✗", name, detail))


con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
fold = {r["id"]: (r["parent_id"], r["name"]) for r in con.execute("SELECT id,parent_id,name FROM folders")}


def rel_of(r):
    parts, fid, seen = [], r["folder_id"], set()
    while fid and fid in fold and fid not in seen:
        seen.add(fid)
        p, n = fold[fid]
        parts.append(n)
        fid = p
    names = [n for n in reversed(parts) if n and n.strip()]
    if names and names[0] == "vault":
        names = names[1:]
    return "/".join(names + [r["name"]])


print("=" * 78)
print("活跃写入篇：盘 vs 库 是否同步")
print("=" * 78)
rows = list(con.execute("""SELECT id,name,folder_id,size,content,mtime FROM files
                           WHERE content IS NOT NULL AND enc_algo IS NULL ORDER BY mtime"""))
con.close()

same = diff = miss = 0
for r in rows:
    raw = r["content"] if isinstance(r["content"], bytes) else (r["content"] or "").encode("utf-8")
    rel = rel_of(r)
    p = os.path.join(LIB, *[x for x in rel.split("/") if x])
    if not os.path.isfile(p):
        miss += 1
        print("  ✗ %-28s 盘上没有！ %s" % (r["name"][:28], rel))
        continue
    disk = open(p, "rb").read()
    if disk == raw:
        same += 1
        print("  ✓ %-28s 一致  %d 字节" % (r["name"][:28], len(disk)))
    else:
        diff += 1
        print("  ✗ %-28s 漂移！盘 %d / 库 %d（差 %+d）"
              % (r["name"][:28], len(disk), len(raw), len(disk) - len(raw)))

print()
check("全部盘 == 库（无漂移、无缺失）", diff == 0 and miss == 0,
      "一致 %d ｜ 漂移 %d ｜ 缺失 %d" % (same, diff, miss))

print()
print("=" * 78)
if not FAIL:
    print("★ 活跃写入方虽然持续造库内正文，但**盘上同步是跟得上的** —— 没有漂移")
    print("  （这正说明 write_through 的 data 参数那条修复是必要的且有效）")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
