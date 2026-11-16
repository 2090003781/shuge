# -*- coding: utf-8 -*-
"""为什么自愈没触发？—— 直接在进程内走一遍路由判据。

（库进程在跑，我这里另开一个进程读同一份库 —— 判据是纯函数式的，
  不依赖库进程的内存状态，所以能这样查。）
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import priv_folder as pf

DB = r"D:\core-notes\data\index.db"

d = dbmod.Database.__new__(dbmod.Database)
d.conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
d.conn.row_factory = sqlite3.Row
d.data_dir = os.path.dirname(DB)

print("=" * 78)
print("路由判据逐条查")
print("=" * 78)
man = d.manifest()
print("  priv_mode(manifest) = %r" % man.get("priv_mode"))
print("  priv_mode()         = %r" % d.priv_mode())
print("  priv_effective_mode = %r" % d.priv_effective_mode())
print("  library_dir         = %r" % man.get("library_dir"))
print("  priv_package_ready  = %s" % d.priv_package_ready())
print()

row = d.conn.execute("SELECT id,name,enc_algo,folder_id FROM files"
                     " WHERE name LIKE 'DSH-人设.md%' LIMIT 1").fetchone()
fid = row["id"]
rel = d.disk_rel_of(fid)
print("  样本：%s" % row["name"])
print("     id        = %s" % fid[:8])
print("     enc_algo  = %r" % row["enc_algo"])
print("     disk_rel  = %r" % rel)
print("     in_priv_dir = %s" % pf.is_private_location(rel))
print("     inside_rel  = %r" % pf.inside_rel(rel))
print("     should_route_to_pack = %s" % d.priv_should_route_to_pack(fid))
print()

# 关键：包里的条目名（不解密列不出，但可以看"读包里有没有它会怎样"）
lib = man.get("library_dir")
blob, p = pf.load_pack(lib)
print("  包：%s" % ("%d 字节" % len(blob) if blob else "无"))
print()

print("=" * 78)
print("结论判读：")
print("  · 若 should_route_to_pack=False → 读根本不走包，自愈自然不触发")
print("  · 若 True → 那问题在 _priv_read_from_pack 内部（看它有没有跑到自愈分支）")
print("=" * 78)
d.conn.close()
