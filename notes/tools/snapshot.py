"""备份常态化：给整个库做快照 / 列快照 / 看差异 / 回滚。

为什么（2026-09-15 两次教训）：改正文的批量工具一多，就必须有"随时能回到上一秒"的兜底。
之前的备份是临时机制（还被我自己的体检脚本误删过），这里做成常备工具。

用法：
  python tools/snapshot.py                      # 打一份快照（全库）
  python tools/snapshot.py --list               # 列快照
  python tools/snapshot.py --diff <snap.json>    # 和快照比，只报差异（不写库）
  python tools/snapshot.py --restore <snap.json> # 按快照还原（正文+日期+可见性+标签+引用边）
  python tools/snapshot.py --only 名字或id ...    # 只处理这些文件（配合上面几个动作）

快照内容：每篇的正文、名字、所在路径、doc_date、visibility、标签、content_hash
（不含向量/分块派生层 —— 那些能重建；重建由 --restore 时顺手做）。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))

from db import Database, DEFAULT_DATA_DIR        # noqa: E402
from indexer import Indexer                      # noqa: E402
from events import EventLog                      # noqa: E402
from mentions import resolve as resolve_mentions  # noqa: E402

SNAP_DIR = "snapshots"


def snap_dir(data_dir: str) -> str:
    d = os.path.join(data_dir, SNAP_DIR)
    os.makedirs(d, exist_ok=True)
    return d


def take(db: Database, data_dir: str, master=None) -> dict:
    files, skipped = [], []
    for r in db.conn.execute("SELECT id, name, folder_id, visibility, doc_date, size FROM files"
                             " ORDER BY name"):
        if r["visibility"] == "private" and master is None:
            skipped.append(r["name"])
            continue
        try:
            raw = db.get_content(r["id"], master)
        except Exception:
            skipped.append(r["name"])
            continue
        files.append({
            "file_id": r["id"], "name": r["name"],
            "folder_path": db.folder_path(r["folder_id"]) if r["folder_id"] else "",
            "visibility": r["visibility"], "doc_date": r["doc_date"] or "",
            "tags": sorted(db.tags_of(r["id"])),
            "content_hash": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "content": raw.decode("utf-8", "replace"),
        })
    snap = {"created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files,
            "count": len(files), "skipped_locked": skipped,
            "folders": [{"path": db.folder_path(f["id"]), "id": f["id"]}
                        for f in db.conn.execute("SELECT id FROM folders")]}
    path = os.path.join(snap_dir(data_dir), "snap-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    io.open(path, "w", encoding="utf-8", newline="").write(json.dumps(snap, ensure_ascii=False))
    return {"path": path, "count": len(files), "skipped_locked": len(skipped),
            "bytes": os.path.getsize(path)}


def load(path: str) -> dict:
    return json.load(io.open(path, encoding="utf-8"))


def diff(db: Database, snap: dict, master=None) -> dict:
    """快照 vs 现在：新增/删除/内容变了/名字变了"""
    cur = {}
    for r in db.conn.execute("SELECT id, name, visibility FROM files"):
        if r["visibility"] == "private" and master is None:
            continue
        try:
            raw = db.get_content(r["id"], master)
        except Exception:
            continue
        cur[r["id"]] = {"name": r["name"], "hash": "sha256:" + hashlib.sha256(raw).hexdigest()}
    old = {f["file_id"]: f for f in snap.get("files", [])}
    added = [i for i in cur if i not in old]
    removed = [i for i in old if i not in cur]
    changed, renamed = [], []
    for i in set(cur) & set(old):
        if cur[i]["hash"] != old[i]["content_hash"]:
            changed.append((old[i]["name"], cur[i]["name"]))
        elif cur[i]["name"] != old[i]["name"]:
            renamed.append((old[i]["name"], cur[i]["name"]))
    return {"added": added, "removed": [old[i]["name"] for i in removed],
            "changed": changed, "renamed": renamed}


def restore(db: Database, snap: dict, only=None, master=None) -> dict:
    """按快照还原：正文 + 日期 + 可见性 + 标签 + 引用边（按 file_id 对；文件没了就按路径+名字新建）"""
    idx = Indexer(db)
    log = EventLog(db)
    want = {f["file_id"]: f for f in snap.get("files", [])}
    if only:
        keep = set(only)
        want = {k: v for k, v in want.items()
                if k in keep or v["name"] in keep
                or (v["name"][:-3] if v["name"].lower().endswith(".md") else v["name"]) in keep}
    done, created, missing = 0, 0, []
    with log.action("snapshot.restore", reason="cli", expect_within_ms=3600000):
        for fid, rec in want.items():
            row = db.file_by_id(fid)
            if row is None:                       # 快照里的文件现在没了 → 按路径重建
                folder = db.ensure_folder_path(rec["folder_path"].split("vault/", 1)[-1]) \
                    if rec["folder_path"] else db.root_folder_id()
                fid = db.create_file(folder, rec["name"], rec["content"].encode("utf-8"),
                                     visibility=rec.get("visibility") or "internal")
                created += 1
                row = db.file_by_id(fid)
            try:
                db.put_content(fid, rec["content"].encode("utf-8"), master)
            except Exception as e:
                missing.append("%s: %s" % (rec["name"], str(e)[:40]))
                continue
            if rec.get("doc_date"):
                db.conn.execute("UPDATE files SET doc_date=? WHERE id=?", (rec["doc_date"], fid))
            if rec.get("visibility") and rec["visibility"] != row["visibility"]:
                try:
                    db.set_visibility(fid, rec["visibility"], master)
                except Exception:
                    pass
            for t in rec.get("tags") or []:
                db.add_tag(fid, t)
            idx.reindex_file(fid)
            pairs, _miss = resolve_mentions(rec["content"], db.ref_resolver(), {})
            db.set_mentions(fid, pairs)
            done += 1
        db.conn.commit()
    return {"restored": done, "created": created, "failed": missing}


def main() -> int:
    ap = argparse.ArgumentParser(description="库快照 / 差异 / 回滚")
    ap.add_argument("--data", default=None)
    ap.add_argument("--list", action="store_true", help="列快照")
    ap.add_argument("--diff", default="", help="和快照比（只读）")
    ap.add_argument("--restore", default="", help="按快照还原")
    ap.add_argument("--only", action="append", default=[], help="只处理这些文件（名字或 id）")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    data_dir = a.data or DEFAULT_DATA_DIR

    if a.list:
        d = snap_dir(data_dir)
        rows = []
        for n in sorted(os.listdir(d)):
            if not n.endswith(".json"):
                continue
            p = os.path.join(d, n)
            try:
                s = load(p)
                rows.append((n, s.get("count"), os.path.getsize(p), s.get("created_at")))
            except Exception:
                rows.append((n, "?", os.path.getsize(p), "坏文件"))
        for n, c, sz, t in rows:
            print("  %-34s %5s 篇  %6.1f MB  %s" % (n, c, sz / 1048576.0, t))
        print("共 %d 份（目录 %s）" % (len(rows), d))
        return 0

    db = Database(data_dir)
    if a.diff:
        r = diff(db, load(a.diff))
        if a.json:
            print(json.dumps(r, ensure_ascii=False, indent=1))
        else:
            print("相比 %s：内容变了 %d 篇、改名 %d 篇、新增 %d 篇、快照里有而现在没了 %d 篇"
                  % (os.path.basename(a.diff), len(r["changed"]), len(r["renamed"]),
                     len(r["added"]), len(r["removed"])))
            for a_, b_ in r["changed"][:10]:
                print("   内容变了：%s" % a_)
            for a_, b_ in r["renamed"][:10]:
                print("   改名：%s → %s" % (a_, b_))
        db.close()
        return 0

    if a.restore:
        r = restore(db, load(a.restore), only=a.only)
        print("还原 %d 篇（新建 %d，失败 %d）%s"
              % (r["restored"], r["created"], len(r["failed"]),
                 ("：" + "；".join(r["failed"][:3])) if r["failed"] else ""))
        db.close()
        return 0

    r = take(db, data_dir)
    print("快照：%s（%d 篇，%.1f MB）%s"
          % (r["path"], r["count"], r["bytes"] / 1048576.0,
             ("；跳过加锁 %d 篇" % r["skipped_locked"]) if r["skipped_locked"] else ""))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
