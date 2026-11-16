# -*- coding: utf-8 -*-
"""为什么 /api/tree 返回空树？—— 逐层对照。

对照四方：
  ① /api/tree 的实际返回（根/children/files）
  ② /api/folders（文件夹扁平表）
  ③ /api/health 的文件数
  ④ 直接调 db.tree()（绕开接口）
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

API = "http://127.0.0.1:8848"
DB = r"D:\core-notes\data\index.db"


def get(p):
    with urllib.request.urlopen(API + p, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 78)
print("① /api/tree 实际返回")
print("=" * 78)
t = get("/api/tree")
print("  顶层键：%s" % sorted(t.keys()))
print("  根：id=%r name=%r files=%d children=%d"
      % (t.get("id"), t.get("name"), len(t.get("files") or []), len(t.get("children") or [])))
print("  hidden_private=%s" % t.get("hidden_private"))
print()

print("=" * 78)
print("② /api/folders")
print("=" * 78)
try:
    f = get("/api/folders")
    items = f.get("items") or f.get("folders") or []
    print("  条目数：%d" % len(items))
    for x in items[:6]:
        print("     %s" % json.dumps(x, ensure_ascii=False)[:110])
except Exception as e:
    print("  ERR %s" % str(e)[:80])
print()

print("=" * 78)
print("③ /api/health 文件数")
print("=" * 78)
h = get("/api/health")
print("  files=%s chunks=%s" % (h["stats"].get("files"), h["stats"].get("chunks")))
print()

print("=" * 78)
print("④ 直接查库：folders / files 的 folder_id 分布")
print("=" * 78)
con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
print("  folders 总数：%d" % con.execute("SELECT COUNT(*) FROM folders").fetchone()[0])
print("  根（parent 为空）：")
for r in con.execute("SELECT id,name,parent_id FROM folders WHERE IFNULL(parent_id,'')=''"):
    print("     id=%s name=%r" % (r["id"][:8], r["name"]))
n_null = con.execute("SELECT COUNT(*) FROM files WHERE folder_id IS NULL").fetchone()[0]
n_tot = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
print("  files 总数 %d，其中 folder_id 为空的有 %d" % (n_tot, n_null))
print("  前 3 个文件的 folder_id：")
for r in con.execute("SELECT name,folder_id FROM files LIMIT 3"):
    print("     %-30s folder_id=%r" % (r["name"][:30], r["folder_id"]))
con.close()
print()

print("=" * 78)
print("⑤ 绕开接口直接调 db.tree()")
print("=" * 78)
import db as dbmod
d = dbmod.Database.__new__(dbmod.Database)
d.conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
d.conn.row_factory = sqlite3.Row
d.data_dir = r"D:\core-notes\data"
try:
    t2 = d.tree(hide_private=True)
    print("  tree(hide_private=True)：files=%d children=%d hidden=%s"
          % (len(t2.get("files") or []), len(t2.get("children") or []), t2.get("hidden_private")))
except Exception as e:
    print("  **抛异常**：%s: %s" % (type(e).__name__, str(e)[:80]))
try:
    t3 = d.tree(hide_private=False)
    print("  tree(hide_private=False)：files=%d children=%d"
          % (len(t3.get("files") or []), len(t3.get("children") or [])))
except Exception as e:
    print("  **抛异常**：%s: %s" % (type(e).__name__, str(e)[:80]))
d.conn.close()
