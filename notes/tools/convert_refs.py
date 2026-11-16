"""把正文里的旧引用格式 `[[目标]]` 转成 @引用（FORMAT §6 的双链语法）。

口径（2026-09-15 辞玖定）：**只转"目标能唯一对上现有笔记"的**。
  · `[[目标]]`            → `@目标显示名`（921 处）
  · `[[目标|别名]]`        → 只在"别名 == 目标名"时转（8 处）；别名是别的文字时留着（@ 语法承载不了别名）
  · `[[目标#小节]]`        → 不动（@ 语法没有"指向哪一节"，转了会丢信息）
  · 对不上 / 有歧义        → 不动（转了要么是死链、要么连错篇）
代码围栏与行内代码里的 `[[...]]` 一律不动。

@ 解析要求：`@` 前一个字符不能是字母数字（`(?<![\w@])`），锚点遇到空白/标点才结束。
所以紧贴中文时转换要补一个空格（这类很少，报告里会单独计数）。

用法：
  python tools/convert_refs.py                    # 干跑（默认，不写库）
  python tools/convert_refs.py --apply            # 真转（先自动备份）
  python tools/convert_refs.py --only <文件id/名>  # 只处理指定文件（冒烟测试用）
  python tools/convert_refs.py --json             # 干跑结果以 JSON 输出
  python tools/convert_refs.py --restore <备份>    # 回滚
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from collections import Counter

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))

from db import Database, DEFAULT_DATA_DIR          # noqa: E402
from events import EventLog                        # noqa: E402
from indexer import Indexer                        # noqa: E402
from mentions import resolve as resolve_mentions   # noqa: E402
# CODE 也要：本文件的"补引号"和 tools/refs_audit.py 都按这个切代码段
from refs_convert import (CODE, STOP, WIKI, build_resolver, convert_segment,   # noqa: E402
                          convert_text, sec_token)

# 转换规则本体搬去了 src/refs_convert.py（导入路径也要用同一套），下面直接引用
def reverse_mentions(text: str, anchors) -> str:
    """@锚点 → [[锚点]]（best-effort 反代；@ 前后转换时补的空格不撤）"""
    out = text or ""
    for a in sorted(set(anchors or []), key=len, reverse=True):
        if not a:
            continue
        pat = re.compile(r"(?<![\w@])@%s(?![\w])" % re.escape(a))
        out = pat.sub("[[%s]]" % a, out)
    return out


SRC_ROOTS = [os.environ.get("AI_NOTES_SRC") or r"D:\个人数据\辞玖\second-brain-system",
             r"D:\个人数据\辞玖"]


def rebuild_backup(db: Database, resolve, display, out_path: str) -> dict:
    """按 mention 边找出"被转换过的篇"，尽量用源文件还原转换前原文"""
    ids = [r[0] for r in db.conn.execute(
        "SELECT DISTINCT a_file_id FROM edges WHERE kind='mention'")]
    detail = {"skip_miss": Counter(), "skip_ambiguous": Counter(),
              "skip_alias": Counter(), "skip_anchor": Counter()}
    recs, exact = [], 0
    for fid in ids:
        row = db.file_by_id(fid)
        if row is None:
            continue
        cur = db.get_content(fid).decode("utf-8", "replace")
        path = db.folder_path(row["folder_id"])
        rel = path.split("vault/", 1)[-1] if "vault/" in path else path
        rel = rel.strip("/")
        src = None
        for base in SRC_ROOTS:
            cand = os.path.join(base, *([p for p in rel.split("/") if p] + [row["name"]]))
            if os.path.isfile(cand):
                src = cand
                break
        before, is_exact = None, False
        if src:
            raw = io.open(src, encoding="utf-8", errors="replace").read()
            converted, _st = convert_text(raw, resolve, display, detail)
            if converted == cur:
                before, is_exact = raw, True
        if before is None:
            anchors = [m["anchor"] for m in db.mentions(fid)]
            before = reverse_mentions(cur, anchors)
        recs.append({"file_id": fid, "name": row["name"], "content": before, "exact": is_exact})
        exact += 1 if is_exact else 0
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    io.open(out_path, "w", encoding="utf-8", newline="").write(json.dumps(
        {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "rebuilt": True,
         "note": "重建的转换前备份：exact=true 来自源文件，false 是 @→[[]] 反代（近似）",
         "files": recs}, ensure_ascii=False))
    return {"files": len(recs), "exact": exact, "approx": len(recs) - exact, "path": out_path}


SPACE_FIX = re.compile(
    r"(?<![\w@])@([^\s@\[\]()（）,，。；;：:！!？?*`#>|\\]{1,40})"
    r"((?:\s+[^\s@\[\]()（）,，。；;：:！!？?*`#>|\\]{1,40}){1,3})")


def fix_spaces_text(text: str, look, stats) -> str:
    """给"名字带空格却没加引号"的引用补上引号：`@03-Python FastAPI` → `@"03-Python FastAPI"`。

    只在"拼起来确实是一篇笔记的名字/路径"时才改（否则原样不动）。
    """
    out, fence = [], False
    for line in (text or "").splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        parts, codes = CODE.split(line), CODE.findall(line)
        buf = []
        for k, part in enumerate(parts):
            buf.append(_fix_seg(part, look, stats))
            if k < len(codes):
                buf.append(codes[k])
        out.append("".join(buf))
    return "".join(out)


def _fix_seg(seg: str, look, stats) -> str:
    def sub(m):
        head, tail = m.group(1), m.group(2)
        words = tail.split()
        for n in range(len(words), 0, -1):
            cand = head + " " + " ".join(words[:n])
            if look(cand):
                stats["fixed"] += 1
                rest = words[n:]                       # 没被吃进名字的词要原样留下
                return '@"%s"%s' % (cand, (" " + " ".join(rest)) if rest else "")
        return m.group(0)
    return SPACE_FIX.sub(sub, seg)


def load_text(db, fid):
    try:
        return db.get_content(fid).decode("utf-8", "replace")
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="把 [[旧引用]] 转成 @引用")
    ap.add_argument("--data", default=None)
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只干跑）")
    ap.add_argument("--only", action="append", default=[], help="只处理这些文件（名字或 id，可重复）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个文件（0=不限）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    ap.add_argument("--restore", default="", help="从备份 JSON 回滚")
    ap.add_argument("--rebuild-backup", default="",
                    help="重建一份可回滚的备份（源文件精确还原 + @→[[]] 反代兜底）")
    ap.add_argument("--fix-spaces", action="store_true",
                    help="给名字带空格的引用补引号（@03-Python FastAPI → @\"03-Python FastAPI\"）")
    a = ap.parse_args()

    db = Database(a.data or DEFAULT_DATA_DIR)

    if a.restore:
        data = json.load(io.open(a.restore, encoding="utf-8"))
        n = 0
        for rec in data.get("files", []):
            if db.file_by_id(rec["file_id"]) is None:
                continue
            db.put_content(rec["file_id"], rec["content"].encode("utf-8"))
            Indexer(db).reindex_file(rec["file_id"])
            txt = rec["content"]
            pairs, _miss = resolve_mentions(txt, db.ref_resolver(), {})
            db.set_mentions(rec["file_id"], pairs)
            n += 1
        print("已回滚 %d 篇（内容与引用边都还原）" % n)
        db.close()
        return 0

    resolve, display = build_resolver(db)
    if a.rebuild_backup:
        r = rebuild_backup(db, resolve, display, a.rebuild_backup)
        print("重建备份：%s（精确 %d 篇 / 近似 %d 篇）" % (r["path"], r["exact"], r["approx"]))
        db.close()
        return 0
    detail = {"skip_miss": Counter(), "skip_ambiguous": Counter(),
              "skip_alias": Counter(), "skip_anchor": Counter()}
    todo = []
    rows = db.conn.execute("SELECT id, name, ext, visibility FROM files ORDER BY name").fetchall()
    only = set(a.only)
    for r in rows:
        if only and r["id"] not in only and r["name"] not in only \
                and (r["name"][:-3] if r["name"].lower().endswith(".md") else r["name"]) not in only:
            continue
        if (r["ext"] or "").lower() not in ("md", "markdown"):
            continue
        if r["visibility"] == "private":
            detail.setdefault("skip_locked", Counter())[r["name"]] += 1
            continue
        text = load_text(db, r["id"])
        if text is None:
            continue
        if a.fix_spaces:
            st = Counter()
            new = fix_spaces_text(text, db.ref_resolver(), st)
            if st["fixed"]:
                st["converted"] = st["fixed"]
                todo.append((r["id"], r["name"], text, new, st))
            if a.limit and len(todo) >= a.limit:
                break
            continue
        if "[[" not in text:
            continue
        new, stats = convert_text(text, resolve, display, detail)
        if stats["converted"]:
            todo.append((r["id"], r["name"], text, new, stats))
        if a.limit and len(todo) >= a.limit:
            break

    total = sum(s["converted"] for *_x, s in todo)
    summary = {
        "files": len(todo),
        "converted": total,
        "spaced_head": sum(s["spaced_head"] for *_x, s in todo),
        "spaced_tail": sum(s["spaced_tail"] for *_x, s in todo),
        "with_section": sum(s.get("with_section", 0) for *_x, s in todo),
        "skip_miss": sum(s["skip_miss"] for *_x, s in todo),
        "skip_ambiguous": sum(s["skip_ambiguous"] for *_x, s in todo),
        "skip_alias": sum(s["skip_alias"] for *_x, s in todo),
        "skip_anchor": sum(s["skip_anchor"] for *_x, s in todo),
        "locked_files_skipped": len(detail.get("skip_locked", {})),
        "applied": bool(a.apply),
        "per_file": [{"file_id": fid, "name": nm, "converted": s["converted"],
                      "spaced": s["spaced_head"] + s["spaced_tail"]}
                     for fid, nm, _o, _n, s in todo],
    }

    if a.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    else:
        print("要处理 %d 篇、%s %d 处%s"
              % (len(todo), "补引号" if a.fix_spaces else "转换", total,
                 "" if a.fix_spaces else "（其中 %d 处带小节 #）" % summary["with_section"]))
        for x in summary["per_file"][:15]:
            print("  %-46s 转 %-4d 补空格 %d" % (x["name"][:46], x["converted"], x["spaced"]))
        if len(todo) > 15:
            print("  …（还有 %d 篇）" % (len(todo) - 15))
        print("保留未转：对不上 %d、有歧义 %d、别名不同 %d、带 #小节 %d；私密跳过 %d 篇"
              % (summary["skip_miss"], summary["skip_ambiguous"], summary["skip_alias"],
                 summary["skip_anchor"], summary["locked_files_skipped"]))
        for key in ("skip_miss", "skip_ambiguous"):
            top = detail[key].most_common(5)
            if top:
                print("  %s top：%s" % (key, "、".join("%s×%d" % t for t in top)))

    if not a.apply:
        if a.json:
            sys.stderr.write("（干跑，没有写库；加 --apply 才真转）\n")
        else:
            print("\n（干跑，没有写库；加 --apply 才真转）")
        db.close()
        return 0

    # ── 真转：先备份，再逐个写 ───────────────────────────────
    bdir = os.path.join(a.data or DEFAULT_DATA_DIR, "backups")
    os.makedirs(bdir, exist_ok=True)
    bpath = os.path.join(bdir, "refs-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    io.open(bpath, "w", encoding="utf-8", newline="").write(json.dumps(
        {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": [
            {"file_id": fid, "name": nm, "content": old} for fid, nm, old, _n, _s in todo]},
        ensure_ascii=False))
    print("\n备份：%s（%d 篇原文）" % (bpath, len(todo)))

    idxr, log = Indexer(db), EventLog(db)
    changed, edges = [], 0
    with log.action("refs.convert", reason="cli:[[->@", expect_within_ms=1800000):
        for fid, nm, old, new, _s in todo:
            db.put_content(fid, new.encode("utf-8"))
            idxr.reindex_file(fid)
            anchors = {m["anchor"]: m["id"] for m in db.mentions(fid)}
            pairs, miss = resolve_mentions(new, db.ref_resolver(), anchors)
            edges += db.set_mentions(fid, pairs)
            changed.append(fid)
            log.emit("note.updated", {"file_id": fid, "name": nm,
                                      "changed_chars": len(new) - len(old),
                                      "reason": "refs-converted"})
    from retriever import VectorIndexer
    vres = VectorIndexer(db).index_files(changed)
    print("已写 %d 篇，重建引用边 %d 条，补向量 %s" % (len(changed), edges, vres))
    print("回滚：python tools/convert_refs.py --restore %s" % bpath)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
