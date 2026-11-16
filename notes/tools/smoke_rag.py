"""RAG 链路验收：向量索引 + 双通道融合检索。

用法：
    python tools/smoke_rag.py            # 快速：只向量化 8 个块，验证通路
    python tools/smoke_rag.py --full     # 全量向量化（198 块，GPU 上约几十秒）
    python tools/smoke_rag.py --kwonly   # 只测关键词通道（不加载模型）
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from db import Database  # noqa: E402
from embedder import Embedder  # noqa: E402
from retriever import HybridSearcher, VectorIndexer  # noqa: E402

QUERIES = [
    "中文全文检索为什么搜不到",
    "事件协议的版本迁移怎么规定",
    "私密文件怎么加锁",
]


def main() -> int:
    full = "--full" in sys.argv
    kwonly = "--kwonly" in sys.argv
    data_dir = os.path.abspath(os.path.join(HERE, "..", "data"))
    db = Database(data_dir)
    print("[1] 库:", db.path)

    if kwonly:
        print("[2] 仅关键词通道")
        hyb = HybridSearcher(db, embedder=Embedder())
        for q in QUERIES:
            hits = db.search_chunks(q, limit=3)
            print("   %-18s → %d 命中" % (q[:18], len(hits)))
            for h in hits[:2]:
                print("      · %s  %s" % (h["name"][:22], h["text"][:40].replace("\n", " ")))
        db.close()
        return 0

    emb = Embedder()
    print("[2] 嵌入模型: %s | 设备: %s | 目录存在: %s" % (emb.model_path, emb.device, emb.available))

    vi = VectorIndexer(db, emb)
    print("[3] 向量化前:", vi.stats())
    limit = 0 if full else 8
    res = vi.index(limit=limit, progress=True)
    print("    结果:", res)
    print("    之后:", vi.stats())

    hyb = HybridSearcher(db, emb)
    print("[4] 融合检索（FTS + 向量 RRF）:")
    for q in QUERIES:
        hits = hyb.search(q, limit=3, with_detail=True)
        print("   ── %s（%d 命中）" % (q, len(hits)))
        print(hyb.format_hits(hits))

    print("[5] manifest（过时判据）:", {k: v for k, v in db.manifest().items()
                                        if k in ("embedding_model", "dim", "schema_version", "chunk_max_chars")})
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
