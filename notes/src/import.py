"""导入命令行：python src/import.py --from <目录> [--exclude 日志,Archive] [--vectors] [--limit N]

流程：导入（按 content_hash 去重）→ 分块 + 全文索引 →（可选）向量化。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from db import Database, DEFAULT_DATA_DIR   # noqa: E402
from events import EventLog                 # noqa: E402
from importer import import_from_dir        # noqa: E402
from indexer import Indexer                 # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把磁盘目录导入库")
    ap.add_argument("--from", dest="src", required=True, help="源目录")
    ap.add_argument("--data", default=None, help="库目录（默认 %s）" % DEFAULT_DATA_DIR)
    ap.add_argument("--exclude", default="", help="额外跳过的子目录名，逗号分隔（如 日志,tmp）")
    ap.add_argument("--limit", type=int, default=0, help="最多导入多少个文件（0=不限）")
    ap.add_argument("--vectors", action="store_true", help="导入后做向量嵌入（慢，语义检索用）")
    ap.add_argument("--under", default="", help="导入到指定的库内目录下（如 vault）")
    ap.add_argument("--tag", default="", help="给本次导入的所有文件打标签")
    ap.add_argument("--tag-dir", action="append", default=[],
                    help="按顶层目录打标签，格式 目录名=标签（可重复）")
    ap.add_argument("--no-index", action="store_true", help="只导入，不重建派生层")
    a = ap.parse_args()

    import importer
    if a.exclude:
        importer.EXCLUDE_DIRS = set(importer.EXCLUDE_DIRS) | {x.strip() for x in a.exclude.split(",") if x.strip()}

    tag_dirs = {}
    for item in a.tag_dir:
        if "=" not in item:
            print("--tag-dir 格式应为 目录名=标签：%s" % item)
            return 2
        k, v = item.split("=", 1)
        tag_dirs[k.strip()] = v.strip()

    db = Database(a.data or DEFAULT_DATA_DIR)
    log = EventLog(db)
    t0 = time.time()
    print("源目录：%s" % os.path.abspath(a.src))
    with log.action("note.import", reason="cli", expect_within_ms=3600000):
        res = import_from_dir(db, a.src, limit=a.limit, events=log,
                              tag=a.tag, tag_dirs=tag_dirs, under=a.under)
    print("导入：%d 个（跳过 %d，失败 %d，打标 %d，转码 %d），用时 %.1fs"
          % (res["imported"], res["skipped"], res["failed"], res.get("tagged", 0),
             res.get("reencoded", 0), time.time() - t0))

    if not a.no_index:
        t1 = time.time()
        idx = Indexer(db)
        idx.events = log
        with log.action("index.rebuild", reason="import", expect_within_ms=3600000):
            r = idx.reindex_all()
        print("索引：%d 个文件 / %d 块，用时 %.1fs（失败 %d）"
              % (r["files"], r["chunks"], time.time() - t1, len(r["errors"])))

    if a.vectors:
        from retriever import VectorIndexer
        t2 = time.time()
        with log.action("index.vectors", reason="import", expect_within_ms=7200000):
            v = VectorIndexer(db).index(progress=True)
        print("向量：%s，用时 %.1fs" % (v, time.time() - t2))

    print("库现状：%s" % db.stats())
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
