# -*- coding: utf-8 -*-
"""验 _reveal_rows：legacy 原样 / package 合并包内条目。

判据：
  ① legacy：与 `file_rows("private")` **逐 id 一致**（零行为变化）
  ② package + 有 master：包内条目并入；库里没有的条目也出现（包是真相源）
  ③ package + 包读不出来：不抛、退化成库那套、留 unmatched
  ④ 合并后不重复（同 id 只出现一次）
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
import server

DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


class S:
    pass


print("=" * 78)
print("验 _reveal_rows")
print("=" * 78)

td = tempfile.mkdtemp(prefix="reveal2-")
try:
    for suf in ("", "-wal", "-shm"):
        s = DB + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(td, "index.db" + suf))
    d = dbmod.Database(td)
    d.set_manifest(library_dir=LIB)
    server.STATE = S()
    server.STATE.db = d

    # ① legacy 原样
    print("① legacy：原样")
    d.set_manifest(priv_mode="legacy")
    r = server._reveal_rows(None)
    base = d.file_rows("private")
    check("行数与 file_rows 一致", len(r["rows"]) == len(base),
          "%d vs %d" % (len(r["rows"]), len(base)))
    check("逐 id 一致", [x["id"] for x in r["rows"]] == [x["id"] for x in base])
    check("没有 ambiguous / unmatched", not r["ambiguous"] and not r["unmatched"])
    print()

    # ② package + 有 master（用替身喂包内条目）
    print("② package + 有 master：包内条目并入")
    d.set_manifest(priv_mode="package")
    items = [{"name": r2["name"], "folder": "", "mtime": "", "size": r2["size"] or 0,
              "hash": "", "_data": b""}
             for r2 in d.conn.execute("SELECT name,size FROM files WHERE visibility='private'")]
    items.append({"name": "只在包里的新篇.md", "folder": "", "mtime": "2026-10-15T00:00:00",
                  "size": 42, "hash": "", "_data": b""})
    real = pf.read_items
    pf.read_items = lambda *a, **kw: items
    try:
        r2 = server._reveal_rows(b"x")
    finally:
        pf.read_items = real
    names = [x.get("name") for x in r2["rows"]]
    check("库内 8 篇都在", sum(1 for n in names if n in
                              [x["name"] for x in base]) == 8, "%d 篇" % len(base))
    check("**只在包里的那篇也出现了**", "只在包里的新篇.md" in names)
    check("没有重复（同名只一次）", len(names) == len(set(names)), "%d 行" % len(names))
    check("来自包的行带 from_pack 标记",
          any(x.get("from_pack") for x in r2["rows"] if x.get("name") == "只在包里的新篇.md"))
    print()

    # ③ 包读不出来
    print("③ package + 包读不出来：不抛、退化、留痕")
    pf.read_items = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("模拟读包失败"))
    try:
        r3 = server._reveal_rows(b"x")
    finally:
        pf.read_items = real
    check("没抛异常", r3 is not None)
    check("退化成库那套", len(r3["rows"]) == len(base), "%d 行" % len(r3["rows"]))
    check("unmatched 留了痕", bool(r3["unmatched"]), str(r3["unmatched"])[:40])
    d.close()
finally:
    time.sleep(0.4)
    shutil.rmtree(td, ignore_errors=True)

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ _reveal_rows 成立：legacy 零变化；package 以包为准且失败不阻断解锁")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
