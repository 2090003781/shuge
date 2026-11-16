#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""看一眼库里的《项目记录.md》最后一段（只读；用来对齐"下一节编号"和写法）。

用法：python scripts\peek_record2.py [末尾字符数]
"""
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
DB = r"D:\core-notes\data\index.db"
FID = "87d927cb-1d38-4416-98a8-ae30a9b9606c"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 3000

con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
cols = [x[1] for x in con.execute("PRAGMA table_info(files)")]
print("列：", cols)
row = con.execute("SELECT * FROM files WHERE id=?", (FID,)).fetchone()
d = dict(zip(cols, row))
print("name=%s storage=%s n=%s" % (d.get("name"), d.get("storage"), len(d.get("content") or "")))
c = d.get("content") or ""
if isinstance(c, bytes):
    c = c.decode("utf-8", "replace")
print("---- 末尾 %d 字 ----" % N)
print(c[-N:])
