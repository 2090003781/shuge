# -*- coding: utf-8 -*-
"""根下那个重复的「个人」文件夹：谁建的？有多少？有害吗？

判据：
  ① 根下（parent 为空或空名根）有哪几个文件夹
  ② 那个重复「个人」里装着什么（文件/子目录）
  ③ 它下面有没有私密篇（有的话，位置判据会把它当私密文件夹）
  ④ 「私密」这个文件夹名一共出现了几次（多了的话 pack_tree_rows 的查询会取到错的那个）
"""
from __future__ import annotations

import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

DB = r"D:\core-notes\data\index.db"
con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row

fold = {r["id"]: (r["parent_id"], r["name"]) for r in con.execute("SELECT id,parent_id,name FROM folders")}
ROOT = [r["id"] for r in con.execute("SELECT id FROM folders WHERE IFNULL(parent_id,'')=''")]


def path_of(fid):
    parts, seen = [], set()
    while fid and fid in fold and fid not in seen:
        seen.add(fid)
        p, n = fold[fid]
        parts.append(n)
        fid = p
    return "/".join(reversed([x for x in parts if x and x.strip()]))


print("=" * 78)
print("① 根（parent 为空）下的文件夹")
print("=" * 78)
for r in con.execute("SELECT id,name FROM folders WHERE IFNULL(parent_id,'')='' ORDER BY name"):
    n = con.execute("SELECT COUNT(*) c FROM files WHERE folder_id=?", (r["id"],)).fetchone()["c"]
    nc = con.execute("SELECT COUNT(*) c FROM folders WHERE parent_id=?", (r["id"],)).fetchone()["c"]
    print("   %-24s id=%s  文件 %d 子目录 %d" % (r["name"] or "(空名根)", r["id"][:8], n, nc))
print()

print("=" * 78)
print("② 空名根的直接子级（= 你截图看到的根目录那几项）")
print("=" * 78)
rootid = None
for r in con.execute("SELECT id FROM folders WHERE IFNULL(parent_id,'')=''"):
    rootid = r["id"]
if rootid:
    for r in con.execute("SELECT id,name FROM folders WHERE parent_id=? ORDER BY name", (rootid,)):
        n = con.execute("SELECT COUNT(*) c FROM files WHERE folder_id=?", (r["id"],)).fetchone()["c"]
        print("   %-24s id=%s  文件 %d  路径=%r" % (r["name"], r["id"][:8], n, path_of(r["id"])))
print()

print("=" * 78)
print("③ 那个重复「个人」里装了什么")
print("=" * 78)
dup = con.execute("SELECT id FROM folders WHERE name='个人' AND parent_id=?", (rootid,)).fetchone()
if dup:
    did = dup["id"]
    print("   文件：")
    for r in con.execute("SELECT name,visibility,enc_algo FROM files WHERE folder_id=?", (did,)):
        print("      %-28s vis=%-9s enc=%s" % (r["name"][:28], r["visibility"], r["enc_algo"]))
    print("   子目录：")
    for r in con.execute("SELECT id,name FROM folders WHERE parent_id=?", (did,)):
        print("      %-24s id=%s 路径=%r" % (r["name"], r["id"][:8], path_of(r["id"])))
else:
    print("   （根下没有「个人」）")
print()

print("=" * 78)
print("④ 「私密」文件夹一共几个（多了的话 pack_tree_rows 会取错）")
print("=" * 78)
for r in con.execute("SELECT id,parent_id,name FROM folders WHERE name='私密'"):
    n = con.execute("SELECT COUNT(*) c FROM files WHERE folder_id=?", (r["id"],)).fetchone()["c"]
    print("   id=%s parent=%s(%r) 文件 %d 路径=%r"
          % (r["id"][:8], (r["parent_id"] or "")[:8],
             fold.get(r["parent_id"], (None, None))[1], n, path_of(r["id"])))
con.close()
