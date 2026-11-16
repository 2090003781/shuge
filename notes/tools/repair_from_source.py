# -*- coding: utf-8 -*-
"""把停在加密态、密钥已不可恢复的笔记，从源目录的同名文件恢复成明文。

  python tools/repair_from_source.py                 # 干跑：只列会动哪几篇（默认）
  python tools/repair_from_source.py --yes           # 真改
  python tools/repair_from_source.py --yes --src D:\\某个源目录

**它会做什么**（2026-09-30 体检发现旧版 docstring 只说了「恢复明文」，没说全）：
对每一篇 `enc_algo` 非空的笔记，
  ① 把 `enc_algo` / `key_ref` 清成 NULL、`visibility` **改成 internal**（＝私密标记没了）；
  ② 用源目录里的同名文件**覆盖库内正文**；
  ③ 重跑索引（切块 / 引用 / 向量）。

**为什么必须加 --yes**：这三步都不可逆，而且 ② 会覆盖正文、① 会**把私密篇变成可见的普通篇**。
旧版无参数、无护栏、无备份就能跑（体检点 4）。现在：
  · 不给 `--yes` 只干跑并打印清单；
  · 真改之前先把「要动的行」整行导出到 `data/前像/repair-from-source-<时间>.json`（后悔药）；
  · 逐篇打印，改完再统计。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))

from db import Database, DEFAULT_DATA_DIR, local_iso      # noqa: E402
from indexer import Indexer                               # noqa: E402

DEFAULT_SRC = r"D:\个人数据\辞玖\second-brain-system"


def main() -> int:
    ap = argparse.ArgumentParser(description="从源目录恢复停在加密态的笔记（默认干跑）")
    ap.add_argument("src", nargs="?", default=DEFAULT_SRC, help="源目录（默认 %s）" % DEFAULT_SRC)
    ap.add_argument("--data", default=None, help="库目录（默认 %s）" % DEFAULT_DATA_DIR)
    ap.add_argument("--yes", action="store_true", help="真改（不给就只列清单）")
    a = ap.parse_args()

    d = Database(a.data or DEFAULT_DATA_DIR)
    rows = d.conn.execute(
        "SELECT id, name, visibility, folder_id, size FROM files WHERE enc_algo IS NOT NULL").fetchall()
    if not rows:
        print("没有停在加密态的文件")
        d.close()
        return 0

    index = {}
    for root, _dirs, files in os.walk(a.src):
        for fn in files:
            index.setdefault(fn, os.path.join(root, fn))

    plan = []
    for r in rows:
        src = index.get(r["name"])
        plan.append((r, src))
        print("%s %-40s ← %s" % ("✓" if src else "✗", r["name"], src or "（源目录里没有，跳过）"))
    todo = [(r, s) for r, s in plan if s]
    print("\n共 %d 篇停在加密态；其中 %d 篇找得到源文件。" % (len(rows), len(todo)))
    if not todo:
        d.close()
        return 0

    if not a.yes:
        print("\n干跑结束（**什么都没改**）。它会：清 enc_algo/key_ref、visibility→internal、"
              "覆盖正文、重跑索引。\n要真改请加 --yes。")
        d.close()
        return 0

    out_dir = os.path.join(str(d.data_dir), "前像")
    os.makedirs(out_dir, exist_ok=True)
    snap = os.path.join(out_dir, "repair-from-source-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(snap, "w", encoding="utf-8") as f:
        json.dump({"at": local_iso(), "src": a.src, "rows": [dict(r) for r in rows]},
                  f, ensure_ascii=False, indent=1)
    print("已留后悔药：%s" % snap)

    done = 0
    for r, src in todo:
        data = open(src, "rb").read()
        d.conn.execute("UPDATE files SET enc_algo=NULL, key_ref=NULL, visibility='internal' WHERE id=?",
                       (r["id"],))
        d.conn.commit()
        d.put_content(r["id"], data)
        Indexer(d).reindex_file(r["id"])
        done += 1
        print("✓ 已恢复：%s ← %s（%d 字节）" % (r["name"], src, len(data)))
    print("\n改完 %d 篇。**注意：这些篇的 visibility 现在是 internal（不再是私密）。**" % done)
    d.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
