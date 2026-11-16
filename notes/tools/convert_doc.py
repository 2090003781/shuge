"""文档转换器（命令行）：列出/刷新/另存为 Markdown。

  python tools/convert_doc.py                 # 盘点：库里的文档格式、能不能解析
  python tools/convert_doc.py --refresh        # 重算派生缓存（解析器升级后用）
  python tools/convert_doc.py --to-md          # 每个文档另存为同目录下的 .md（原件不动）
  python tools/convert_doc.py --to pdf          # 每个文档另存为 pdf/docx/odt/rtf/html/epub
  python tools/convert_doc.py --engines         # 只看转换引擎（pandoc / WPS）在不在
  python tools/convert_doc.py --to-md --only 周报.docx --only 季度.odt

不改原文件：另存为是**新建** .md；派生缓存只是解析结果的缓存（原字节永远留着）。
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))

from db import Database, DEFAULT_DATA_DIR     # noqa: E402
from doctext import is_document, parse, supported   # noqa: E402
from indexer import Indexer                   # noqa: E402
from events import EventLog                   # noqa: E402
from mentions import resolve as resolve_mentions   # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="文档解析 / 转换")
    ap.add_argument("--data", default=None)
    ap.add_argument("--refresh", action="store_true", help="重算派生缓存")
    ap.add_argument("--to-md", action="store_true", help="另存为 Markdown（新建 .md）")
    ap.add_argument("--to", default="", help="另存为指定格式：docx/odt/rtf/html/epub/pdf")
    ap.add_argument("--engines", action="store_true", help="只看转换引擎可用性")
    ap.add_argument("--only", action="append", default=[], help="只处理这些文件（名字或 id）")
    a = ap.parse_args()
    if a.engines:
        import docconvert
        e = docconvert.engines()
        print("转换引擎：")
        print("  pandoc   %s%s" % (e["pandoc"] or "（没有）",
                                  ("　" + e["pandoc_version"]) if e.get("pandoc_version") else ""))
        print("  WPS COM  %s（出 PDF 用）" % ("可用" if e["wps"] else "不可用，存不了 PDF"))
        print("  内置解析 %s" % e["builtin"])
        print("  能写出   %s" % "、".join(docconvert.WRITE_EXTS))
        return 0
    db = Database(a.data or DEFAULT_DATA_DIR)
    only = {x.strip() for x in a.only}

    rows = [dict(r) for r in db.conn.execute(
        "SELECT id, name, ext, kind FROM files ORDER BY name")]
    docs = [r for r in rows if (r["kind"] == "document" or is_document(r["ext"]))]
    if only:
        docs = [r for r in docs if r["id"] in only or r["name"] in only
                or r["name"].rsplit(".", 1)[0] in only]
    if not docs:
        print("库里没有文档格式（docx/odt/rtf/pdf/epub/html…）。支持的格式与解析器：")
        for k, v in supported().items():
            print("   %-6s %s" % (k, v))
        db.close()
        return 0

    print("库里文档 %d 篇：%s" % (len(docs), "、".join(sorted({r["ext"] or "?" for r in docs}))))
    idx = Indexer(db)
    log = EventLog(db)
    made, made_ids, failed = [], [], []
    to = (a.to or "").lower().lstrip(".")
    with log.action("doc.convert", reason="cli", expect_within_ms=1800000):
        for r in docs:
            try:
                raw = db.get_content(r["id"])
            except Exception as e:
                failed.append((r["name"], "读内容失败: %s" % str(e)[:40]))
                continue
            if a.refresh or a.to_md or to:
                d = db.derived_for(r["id"], raw, refresh=True)
                idx.reindex_file(r["id"])
            else:
                d = db.derived_for(r["id"], raw)
            err = d.get("error") or ""
            text = d.get("text") or ""
            if err:
                failed.append((r["name"], err))
                print("  ❌ %-34s %s" % (r["name"][:34], err[:70]))
                continue
            print("  ✅ %-34s %-12s %6d 字%s"
                  % (r["name"][:34], d.get("parser") or "", len(text),
                     ("　→ " + d.get("meta", {}).get("title", "")[:20]) if d.get("meta") else ""))
            if a.to_md and text.strip():
                base = r["name"].rsplit(".", 1)[0]
                name = base + ".md"
                frow = db.file_by_id(r["id"])
                if db.find_file(frow["folder_id"], name):
                    print("     （已存在同名 %s，跳过）" % name)
                    continue
                head = "<!-- 由 %s 转换而来（%s） -->\n\n" % (r["name"], d.get("parser") or "?")
                nid = db.create_file(frow["folder_id"], name, (head + text).encode("utf-8"),
                                     visibility=frow["visibility"])
                idx.reindex_file(nid)
                pairs, _m = resolve_mentions((head + text), db.ref_resolver())
                db.set_mentions(nid, pairs)
                made.append(name)
                made_ids.append(nid)
                print("     → 新建 %s" % name)
            if to and text.strip():
                import docconvert
                g = docconvert.make(text, to)
                if g.get("error"):
                    failed.append((r["name"], g["error"]))
                    print("     ❌ 转 %s 失败：%s" % (to, g["error"][:70]))
                    continue
                v = g.get("verify") or {}
                base2 = r["name"].rsplit(".", 1)[0]
                name2 = "%s.%s" % (base2, to)
                frow = db.file_by_id(r["id"])
                if db.find_file(frow["folder_id"], name2):
                    name2 = "%s-转换.%s" % (base2, to)
                nid = db.create_file(frow["folder_id"], name2, g["data"],
                                     visibility=frow["visibility"])
                idx.reindex_file(nid)
                made.append(name2)
                made_ids.append(nid)
                print("     → 新建 %s（%s，%s；汉字 %s/%s）"
                      % (name2, g.get("engine") or "?",
                         "校验通过" if v.get("ok") else "校验没过：" + str(v.get("detail"))[:40],
                         v.get("han_out"), v.get("han_src")))

    if a.refresh or a.to_md or to:
        from retriever import VectorIndexer
        print("补向量：", VectorIndexer(db).index_files([r["id"] for r in docs] + made_ids))
    print("完成：处理 %d 篇、新建 %d 个文件、失败 %d 篇" % (len(docs), len(made), len(failed)))
    db.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
