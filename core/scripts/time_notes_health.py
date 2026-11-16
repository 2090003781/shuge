#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把 /api/health 里那几条查询**逐条计时**（只读连接，不碰服务）。

用法：python scripts\time_notes_health.py [库目录]
"""
import os
import sqlite3
import sys
import time

NOTES = sys.argv[1] if len(sys.argv) > 1 else r"D:\core-notes"
DB = os.path.join(NOTES, "data", "index.db")

QUERIES = [
    ("folders       ", "SELECT COUNT(*) FROM folders"),
    ("files         ", "SELECT COUNT(*) FROM files"),
    ("chunks        ", "SELECT COUNT(*) FROM chunks"),
    ("blocks        ", "SELECT COUNT(*) FROM blocks"),
    ("rag_off       ", "SELECT COUNT(*) FROM files WHERE IFNULL(rag,'on')<>'on'"),
    ("stale_sig     ", "SELECT COUNT(*) FROM files WHERE IFNULL(indexed_sig,'')=''"),
    ("bytes(SUM)    ", "SELECT IFNULL(SUM(size),0) FROM files"),
    ("pending_vectors", "SELECT COUNT(*) c FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
                        " WHERE v.chunk_id IS NULL OR v.model<>'x'"),
]


def main():
    print("库：%s" % DB)
    print("大小：%.1f MB" % (os.path.getsize(DB) / 1048576))
    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    conn.row_factory = sqlite3.Row
    total = 0.0
    for name, q in QUERIES:
        t0 = time.time()
        try:
            conn.execute(q).fetchone()
            ms = (time.time() - t0) * 1000
        except Exception as e:
            print("  %s 失败：%s" % (name, e))
            continue
        total += ms
        print("  %-15s %7.1f ms" % (name, ms))
    # 再整体跑一遍（看缓存/冷热差）
    t0 = time.time()
    for _, q in QUERIES:
        try:
            conn.execute(q).fetchone()
        except Exception:
            pass
    print("  全部再来一遍：%7.1f ms（第一遍合计 %.1f ms）" % ((time.time() - t0) * 1000, total))

    print()
    print("相关索引：")
    for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index'"):
        print("   %-34s on %s" % (r["name"], r["tbl_name"]))
    conn.close()


if __name__ == "__main__":
    main()
