"""只补向量（不重建块）：按当前嵌入规格（含日期前缀）把过时/缺失的块重算一遍。

用法：python tools/reembed.py                        # 干跑（默认）：只报待补多少块
      python tools/reembed.py --apply [--data …] [--batch 32]   # 真写

2026-09-30 体检加的门禁：原来无参数就直接补全库向量，没有任何 dry-run。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))

from db import Database, DEFAULT_DATA_DIR          # noqa: E402
from events import EventLog                        # noqa: E402
from retriever import VectorIndexer                # noqa: E402
from embedinput import spec_model                  # noqa: E402
from embedder import get_embedder                  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="按当前嵌入规格补向量")
    ap.add_argument("--data", default=None, help="库目录（默认 %s）" % DEFAULT_DATA_DIR)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--apply", action="store_true",
                    help="真写（默认只报「待补多少块」）。会加载 bge-m3，几十~几百秒，且吃 CPU/内存")
    a = ap.parse_args()

    db = Database(a.data or DEFAULT_DATA_DIR)
    tag = spec_model(get_embedder().model_name)
    n = db.conn.execute(
        "SELECT COUNT(*) FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
        " WHERE v.chunk_id IS NULL OR v.model<>?", (tag,)).fetchone()[0]
    print("规格 %s · 待补 %d / 共 %d 块" % (tag, n,
          db.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]))
    if not n:
        db.close()
        return 0
    if not a.apply:
        print("干跑：什么都没写。要真补加 --apply。"
              "（会加载 bge-m3；在真库上跑会跟服务抢 CPU，服务期间会变慢）")
        db.close()
        return 0
    log = EventLog(db)
    t0 = time.time()
    with log.action("index.vectors", reason="reembed", expect_within_ms=7200000):
        res = VectorIndexer(db).index(batch_size=a.batch, progress=True)
    print("结果 %s · 用时 %.1fs" % (res, time.time() - t0))
    left = db.conn.execute(
        "SELECT COUNT(*) FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
        " WHERE v.chunk_id IS NULL OR v.model<>?", (tag,)).fetchone()[0]
    print("剩余待补 %d" % left)
    db.close()
    return 0 if left == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
