# -*- coding: utf-8 -*-
"""验 pack_tree_rows：包内条目 → 前端 insertFilesIntoTree 能吃的行。

判据：
  ① 8 篇都出得来，folder_id 都是**私密文件夹的真 id**
  ② file_id 用的是**库里那行的真 id**（点得开的前提，P42）
  ③ 名字/大小与包内条目一致
  ④ 排序稳定
  ⑤ 库里找不到对应行时 file_id 为空串（不编造 id）
  ⑥ 包内有子目录时，pack_folder 与 dirs 如实给出
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import priv_folder as pf
import privpack as pp

DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def fresh():
    td = tempfile.mkdtemp(prefix="treeRows-")
    for suf in ("", "-wal", "-shm"):
        s = DB + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(td, "index.db" + suf))
    d = dbmod.Database(td)
    d.set_manifest(library_dir=LIB)
    return d, td


def make_items(d, with_sub=False):
    """按库里的私密篇造包内条目（用接口取明文太慢，直接读库内密文对应的明文长度即可）
    这里只需要"名字 + size"，不需要真正文。"""
    out = []
    rows = d.conn.execute("SELECT name,size,mtime FROM files WHERE visibility='private'"
                          " ORDER BY name").fetchall()
    for i, r in enumerate(rows):
        folder = "子目录" if (with_sub and i == 0) else ""
        out.append({"name": r["name"], "folder": folder,
                    "mtime": (r["mtime"] or "")[:19], "size": r["size"] or 0,
                    "hash": "x" * 64, "_data": b""})
    return out


print("=" * 78)
print("验 pack_tree_rows（第⑤条：解锁后私密文件夹显示完整树）")
print("=" * 78)

d, td = fresh()
try:
    items = make_items(d)
    real_read = pf.read_items
    pf.read_items = lambda *a, **kw: items          # 只替"解密取条目"
    try:
        res = pf.pack_tree_rows(d, master=b"x", vault_priv_raw=None)
    finally:
        pf.read_items = real_read

    rows, pfid = res["rows"], res["pack_dir_id"]
    print("  私密文件夹 id：%s" % (pfid[:8] if pfid else "(空)"))
    print()

    check("① 8 篇都出了行", len(rows) == 8, "%d 行" % len(rows))
    check("① 私密文件夹 id 找到了", bool(pfid))
    # 位置用**库里的真实位置**：那 3 篇 DSH 人设的库内位置仍在 second-brain-system/dsh，
    # 不该被硬塞成私密文件夹（那样树会显示错位置、file_id 也查不到）。
    in_priv = [r for r in rows if r["folder_id"] == pfid]
    check("① 8 篇全挂在私密文件夹下（迁移后）", len(in_priv) == 8, "%d 篇" % len(in_priv))
    check("① 没有挂在别处的", len(rows) - len(in_priv) == 0,
          "%d 篇" % (len(rows) - len(in_priv)))

    # ② 真 id（与库里对得上）
    db_ids = {r["name"]: r["id"] for r in d.conn.execute("SELECT id,name FROM files")}
    db_folders = {r["name"]: r["folder_id"] for r in
                  d.conn.execute("SELECT name,folder_id FROM files")}
    bad = [r["name"] for r in rows if not r["id"] or db_ids.get(r["name"]) != r["id"]]
    check("② file_id 用的是库里那行的真 id", not bad, ("对不上: %s" % bad[:3]) if bad else "8/8")
    check("② 行里没有编造的 id（都非空）", all(r["id"] for r in rows))
    # 重名篇（索引.md）必须取到**私密文件夹里那一篇**的位置，不是知识库那篇
    idx = [r for r in rows if r["name"] == "索引.md"]
    check("② 重名的 索引.md 取到了私密文件夹那篇",
          bool(idx) and idx[0]["folder_id"] == pfid,
          "folder_id=%s（私密=%s）" % ((idx[0]["folder_id"][:8] if idx else "-"), pfid[:8]))

    # ③ 名字/大小
    want = {it["name"]: it["size"] for it in items}
    mis = [r["name"] for r in rows if want.get(r["name"]) != r["size"]]
    check("③ 名字与大小与包内条目一致", not mis, ("不符: %s" % mis[:3]) if mis else "8/8")
    check("③ 都带上 from_pack 标记", all(r.get("from_pack") is True for r in rows))
    check("③ visibility 都是 private", all(r["visibility"] == "private" for r in rows))

    # ④ 排序
    keyed = [(r["pack_folder"], r["name"]) for r in rows]
    check("④ 排序稳定（folder,name）", keyed == sorted(keyed))
    check("④ 名字能直接给前端（disp 非空）", all(r["disp"] for r in rows))

    # ⑤ 库里没有对应行时不编造 id
    items2 = [{"name": "库里没有的篇.md", "folder": "", "mtime": "2026-01-01T00:00:00",
               "size": 12, "hash": "y" * 64, "_data": b""}] + items
    pf.read_items = lambda *a, **kw: items2
    try:
        res2 = pf.pack_tree_rows(d, master=b"x")
    finally:
        pf.read_items = real_read
    ghost = [r for r in res2["rows"] if r["name"] == "库里没有的篇.md"]
    check("⑤ 库里没有对应行 → file_id 为空串", ghost and ghost[0]["id"] == "",
          repr(ghost[0]["id"]) if ghost else "(没找到)" if ghost else "(没找到)")
    check("⑤ unmatched 如实列出它", "库里没有的篇.md" in (res2.get("unmatched") or []),
          str(res2.get("unmatched")))
    check("⑤ ambiguous 如实列出重名篇", "索引.md" in (res2.get("ambiguous") or {}),
          "ambiguous=%d 项" % len(res2.get("ambiguous") or {}))

    # ⑥ 包内子目录
    items3 = make_items(d, with_sub=True)
    pf.read_items = lambda *a, **kw: items3
    try:
        res3 = pf.pack_tree_rows(d, master=b"x")
    finally:
        pf.read_items = real_read
    subrows = [r for r in res3["rows"] if r["pack_folder"] == "子目录"]
    check("⑥ 子目录条目带 pack_folder", len(subrows) == 1, "%d 条" % len(subrows))
    check("⑥ dirs 里如实列出子目录", res3["dirs"] == ["子目录"], str(res3["dirs"]))
    check("⑥ 摘要可用", pf.pack_meta(items3)["count"] == 8,
          str(pf.pack_meta(items3)))
finally:
    d.close(); time.sleep(0.4); shutil.rmtree(td, ignore_errors=True)

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ pack_tree_rows 成立：私密文件夹在解锁后能长出完整树，且点得开（真 id）")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
