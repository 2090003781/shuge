# -*- coding: utf-8 -*-
"""体检/修复：**被判成 asset、但内容其实是文本**的文件（2026-10-03 辞玖那篇就是）。

  python tools/repair_kind.py            # 干跑：只列出来
  python tools/repair_kind.py --apply    # 真修：改 kind + 让库重新解析那一篇

背景：`kind_for()` 原来只看扩展名 —— 没有扩展名的笔记落到兜底 `asset`
（UI 显示「📦 只存不解析：不进检索、不能编辑」），可正文明明是 Markdown。
2026-10-03 已修（扩展名给不出答案时按内容嗅探），但**已经在库里的行**不会自己变，
所以要跑一次这个。

判据与 `db._sniff_text` 同一套：非空、无 NUL、无非白控制字符、能 UTF-8 解码 → 算文本。

**两类分开处理（2026-10-03，辞玖说「除了进入向量检索这一条」）**：
  · 没有扩展名的 → 当笔记：改 kind，**rag 不动**
  · 扩展名不认识但内容是文本的（.jsonl/.meta 这类数据备份）→ 改 kind，**并把 rag 置 off**
    —— 可看可编辑，但要进检索得手动加（不然会悄悄改变检索结果）
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
LIB = "http://127.0.0.1:8848"


def call(path, body=None, method="GET", timeout=300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(LIB + path, data=data, method=method,
                               headers={"Content-Type": "application/json", "X-Writer": "tool:repair-kind"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真修（默认只干跑）")
    a = ap.parse_args()

    from db import Database, caps_for
    db = Database()
    rows = db.conn.execute(
        "SELECT id,name,ext,kind,size FROM files ORDER BY name").fetchall()
    todo = []
    for r in rows:
        if (r["kind"] or "") not in ("asset",):        # 只查"被判成只存放"的
            continue
        try:
            data = db.get_content(r["id"])
        except Exception:
            continue
        sniffed = db._sniff_text(data)
        if sniffed:
            # 没扩展名 = 本该是笔记（bug）；有扩展名但不认识 = 数据文件（能力扩展，别推进检索）
            is_note = not (r["ext"] or "").strip()
            todo.append((r["id"], r["name"], r["ext"], r["kind"], sniffed, r["size"], is_note))

    notes = [t for t in todo if t[6]]
    others = [t for t in todo if not t[6]]
    print("被判成 asset、但内容其实是文本的：%d 篇（没扩展名的 %d / 扩展名不认识的 %d）"
          % (len(todo), len(notes), len(others)))
    for fid, name, ext, kind, sniffed, size, is_note in todo:
        print("   %-46s ext=%-6r %s → %-8s rag=%s  %d 字节"
              % (name[:46], ext, kind, sniffed, "不动" if is_note else "off", size))
    if not todo:
        return 0
    if not a.apply:
        print("\n（干跑：加 --apply 才真改）")
        return 0

    fixed = 0
    for fid, name, ext, kind, sniffed, size, is_note in todo:
        if is_note:
            db.conn.execute("UPDATE files SET kind=? WHERE id=?", (sniffed, fid))
        else:
            # 数据文件：可看可编辑，但不自动进检索（辞玖 2026-10-03：「除了进入向量检索这一条」）
            db.conn.execute("UPDATE files SET kind=?, rag='off' WHERE id=?", (sniffed, fid))
        db.conn.commit()
        # 让库重新解析这一篇（indexer 按行里的 kind 走 → 会按 markdown 切块）
        _st, body = call("/api/files/" + fid)
        content = ((body.get("file") or {}).get("content")) if isinstance(body, dict) else None
        if content is None:
            print("   ⚠️ %s：读不回正文，只改了 kind（下次 reindex 会跟上）" % name[:40])
            fixed += 1
            continue
        st, r = call("/api/files/" + fid, {"content": content}, "PUT", timeout=900)
        print("   %s %s → %s（PUT %s）" % ("✅" if st == 200 else "❌", name[:40], sniffed, st))
        if st == 200:
            fixed += 1
    print("\n修了 %d / %d 篇" % (fixed, len(todo)))
    return 0 if fixed == len(todo) else 1


if __name__ == "__main__":
    sys.exit(main())
