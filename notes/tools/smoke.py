"""验收脚本：导入 → 分块索引 → 全文搜索 → 一致性自检。

用法：
    python tools/smoke.py                 # 默认：导入 second-brain-system 下 12 个文件
    python tools/smoke.py <目录> [数量]
    python tools/smoke.py --keep          # 保留已有库（默认清空重建，保证结果可复现）
"""
from __future__ import annotations

import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from db import Database  # noqa: E402
from importer import import_from_dir  # noqa: E402
from indexer import Indexer  # noqa: E402

DEFAULT_SRC = r"D:\个人数据\辞玖\second-brain-system"
PROBES = ["上下文", "插件", "事件", "索引", "原则"]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    keep = "--keep" in sys.argv
    src = args[0] if args else DEFAULT_SRC
    limit = int(args[1]) if len(args) > 1 else 12

    # 沙箱目录：**绝不能**指向真库（../data）。
    # 2026-09-15 教训：这里原来用的是 ../data，run_all 每次都把真库目录清一遍 ——
    # 真库 index.db 被服务进程占着删不掉（Windows 共享冲突），但备份/池等同目录文件当场丢；
    # 而且它会把源目录里的新文件导进真库。
    data_dir = os.path.abspath(os.path.join(HERE, "..", "data", "_smoke"))
    probe = os.path.join(data_dir, "index.db")
    if os.path.isfile(probe) and os.path.getsize(probe) > 50 * 1024 * 1024:
        print("!! %s 看起来像真库（index.db %d B），拒绝清空" % (data_dir, os.path.getsize(probe)))
        return 2
    if not keep and os.path.isdir(data_dir):
        shutil.rmtree(data_dir, ignore_errors=True)
        print("[0] 清空沙箱旧库（可复现）")

    db = Database(data_dir)
    idx = Indexer(db)
    print("[1] 库:", db.path)

    res = import_from_dir(db, src, limit=limit)
    print("[2] 导入: imported=%d skipped=%d failed=%d" % (res["imported"], res["skipped"], res["failed"]))
    for n in res.get("files", [])[:5]:
        print("     +", n)

    r = idx.reindex_all()
    print("[3] 索引: files=%d chunks=%d errors=%d" % (r["files"], r["chunks"], len(r["errors"])))
    for e in r["errors"][:3]:
        print("     !", e)

    print("[4] 分块类型分布:")
    for row in db.conn.execute("SELECT kind, COUNT(*) c, AVG(char_count) avg_len FROM chunks GROUP BY kind"):
        print("     %-12s %4d 块  平均 %6.0f 字符" % (row["kind"], row["c"], row["avg_len"] or 0))

    print("[5] 前 3 个块:")
    for row in db.conn.execute(
            "SELECT f.name, c.seq, c.kind, c.heading, substr(c.text,1,46) t "
            "FROM chunks c JOIN files f ON f.id=c.file_id ORDER BY f.name, c.seq LIMIT 3"):
        print("     [%s #%d %s] %s | %s" % (row["name"][:20], row["seq"], row["kind"],
                                            (row["heading"] or "")[:16], row["t"].replace("\n", " ")))

    print("[6] 全文搜索:")
    for q in PROBES:
        hits = db.search_chunks(q, limit=3)
        print("     %-6s → %d 命中" % (q, len(hits)))
        for h in hits[:2]:
            print("        · %s（%s）%s" % (h["name"][:24], (h["heading"] or "")[:14],
                                            h["text"][:40].replace("\n", " ")))

    print("[7] 自检:", idx.verify())
    print("[8] 统计:", db.stats())
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
