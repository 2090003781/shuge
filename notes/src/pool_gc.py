"""池清理（FORMAT §5.3）：列出库里没有引用的池文件，人工确认后再删。

    python src/pool_gc.py                 # 只看：列出待删清单
    python src/pool_gc.py --delete        # 真删
    python src/pool_gc.py --min-age 10    # 只考虑创建超过 10 分钟的（默认 60）

规定：只提供命令，不自动跑。**不碰** data/events/blobs（那是账本引用的大载荷）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from db import Database, DEFAULT_DATA_DIR  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="清理池里的孤儿文件")
    ap.add_argument("--data", default=None, help="库目录（默认 %s）" % DEFAULT_DATA_DIR)
    ap.add_argument("--min-age", type=int, default=60, help="只处理创建超过 N 分钟的（默认 60）")
    ap.add_argument("--delete", action="store_true", help="真的删除（默认只列清单）")
    a = ap.parse_args()

    db = Database(a.data or DEFAULT_DATA_DIR)
    pool = db.data_dir / "pool"
    if not pool.is_dir():
        print("没有池目录：%s" % pool)
        db.close()
        return 0

    refs = {r["storage_ref"] for r in db.conn.execute(
        "SELECT storage_ref FROM files WHERE storage='pool' AND storage_ref IS NOT NULL")}
    cutoff = time.time() - a.min_age * 60
    orphans, total, freed = [], 0, 0
    for root, _dirs, files in os.walk(pool):
        for fn in files:
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, pool).replace("\\", "/")
            total += 1
            if rel in refs:
                continue
            st = os.stat(fp)
            if st.st_mtime > cutoff:
                continue                      # 太新的先不动：可能正写着
            orphans.append((rel, st.st_size))
            freed += st.st_size

    print("池文件 %d 个，被引用 %d 个，可清理 %d 个（%.1f KB）"
          % (total, len(refs), len(orphans), freed / 1024))
    for rel, size in orphans[:40]:
        print("  %s  %d B" % (rel, size))
    if len(orphans) > 40:
        print("  …（共 %d 个）" % len(orphans))

    if a.delete and orphans:
        for rel, _size in orphans:
            try:
                os.remove(pool / rel)
            except OSError as e:
                print("  删除失败 %s: %s" % (rel, e))
        print("已删除 %d 个" % len(orphans))
    elif orphans:
        print("（只列清单；加 --delete 才真删）")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
