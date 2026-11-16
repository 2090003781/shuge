# -*- coding: utf-8 -*-
"""查：8 篇私密篇的库内现状 + 位置与标记是否一致。"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
con = sqlite3.connect("file:D:/core-notes/data/index.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
fold = {r["id"]: (r["parent_id"], r["name"]) for r in con.execute("SELECT id,parent_id,name FROM folders")}


def path_of(fid):
    parts, seen = [], set()
    while fid and fid in fold and fid not in seen:
        seen.add(fid)
        p, n = fold[fid]
        parts.append(n)
        fid = p
    return "/".join(reversed([x for x in parts if x and x.strip()]))


print("=== 8 篇私密篇现状 ===")
print("   %-30s %-9s %-9s %-26s %s" % ("名字", "visibility", "size", "位置", "mtime"))
for r in con.execute("""SELECT name,folder_id,size,visibility,enc_algo,mtime,
                               length(content) AS L, storage
                        FROM files WHERE visibility='private' ORDER BY name"""):
    print("   %-30s %-9s %-9s %-26s %s"
          % (r["name"][:30], r["visibility"], r["size"], path_of(r["folder_id"])[:26], (r["mtime"] or "")[:19]))

print()
print("=== 注意对照：上面 size 是明文长度，L 是库内存储长度 ===")
for r in con.execute("SELECT name,size,length(content) L,storage,enc_algo FROM files WHERE visibility='private'"):
    d = (r["L"] or 0) - (r["size"] or 0)
    print("   %-30s size=%-7s L=%-7s 差=%+d  %s" % (r["name"][:30], r["size"], r["L"], d,
          "✓ 密文=明文+28" if d == 28 else "← 不是 +28，要看"))

print()
print("=== 位置 vs 标记：你定的是『位置决定私密』（都该在 个人/私密/）===")
offsider = []
for r in con.execute("SELECT name,folder_id FROM files WHERE visibility='private'"):
    p = path_of(r["folder_id"])
    if "个人/私密" not in p:
        offsider.append((r["name"], p))
print("   在 个人/私密/ 之外的：%d 篇" % len(offsider))
for n, p in offsider:
    print("      %-30s 在 %s" % (n[:30], p))

print()
print("=== 反向：个人/私密/ 里有没有『不是私密』的篇 ===")
pid = None
for r in con.execute("SELECT id,name FROM folders WHERE name='私密'"):
    pid = r["id"]
if pid:
    rows = list(con.execute("SELECT name,visibility FROM files WHERE folder_id=?", (pid,)))
    nonpriv = [r["name"] for r in rows if r["visibility"] != "private"]
    print("   该文件夹共 %d 篇；其中非私密的：%s" % (len(rows), nonpriv or "无"))
con.close()
