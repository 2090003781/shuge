"""高风险操作的**前像**（辞玖 2026-09-24 定：备份只针对高风险操作 —— 删除、覆盖）。

三条"删/盖"路径才有前像：
  · `DELETE /api/files/<id>`（删除）
  · `PUT /api/files/<id>`（把正文整篇换掉）
  · 导入的覆盖分支（镜像/投递箱把磁盘那份盖到库内那份上）

不做：新建、只读、派生层重建、切可见性（那是加解密不是丢内容）—— 没有"前像"可言，
而"每次写都备份"会把磁盘和注意力一起吃掉（辞玖的判断，我同意）。

留在哪：`data/前像/<日期>/<时分秒>-<fileId 前 8 位>-<名字>`，索引 `data/前像/index.jsonl` 一行一条。
能做什么：列出来（可按 file_id 过滤）、按 id 或 file_id 恢复（文件行还在就换回正文；
已被删除就按记录重建那一行 + 放回原文件夹）。
"""
from __future__ import annotations

import io
import json
import os
import time
import uuid
from pathlib import Path

from db import Database, local_iso, sha256_bytes


# 每篇保留几个版本（辞玖 2026-10-15：顶多两到三个 → 取 3）。
# 想临时多留几版做对比，设环境变量 SHUGE_KEEP_VERSIONS 即可（不用改代码）。
_KEEP_VERSIONS = 0        # 0 = 不做版本管理（辞玖 2026-10-15 22:06：先搞定能用）
try:
    import os as _os
    _KEEP_VERSIONS = max(0, int(_os.environ.get("SHUGE_KEEP_VERSIONS") or _KEEP_VERSIONS))
except Exception:
    pass


def _dir(db: Database) -> Path:
    return db.data_dir / "前像"


def _index_path(db: Database) -> Path:
    return _dir(db) / "index.jsonl"


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    out = "".join(("_" if c in bad else c) for c in (name or "unnamed"))
    return out[:80]


def save(db: Database, file_id: str, reason: str, writer: str = "", master: bytes | None = None,
         extra: dict | None = None) -> dict:
    """给这次高风险操作留一份前像。失败不抛（别让它挡住正事），但如实返回原因。"""
    row = db.file_by_id(file_id)
    if row is None:
        return {"ok": False, "why": "no such file"}
    if (row["visibility"] or "") == "private":
        # 私密篇**不留前像**（辞玖 2026-09-28）。
        # 前像是解密后的**明文**落在 data/前像/ 的；私密篇的意义就是"磁盘上是密文"，
        # 给它留前像等于在库目录里再摊一份明文，上锁时也只是从清单里隐藏、文件本身照样读得到。
        # 代价（他已知情并选择）：私密篇删除/覆盖后**无法从回收站恢复**。
        return {"ok": False, "why": "private-skipped"}
    try:
        data = db.get_content(file_id, master)
    except Exception as e:                       # 私密且未解锁：留不了前像
        return {"ok": False, "why": "locked" if "Locked" in type(e).__name__ else str(e)[:60]}
    stamp = time.strftime("%Y%m%d")
    hhmmss = time.strftime("%H%M%S")
    d = _dir(db) / stamp
    d.mkdir(parents=True, exist_ok=True)
    img = d / ("%s-%s-%s" % (hhmmss, file_id[:8], _safe(row["name"])))
    if img.exists():                             # 同秒同文件：加个尾巴
        img = d / ("%s-%s-%s-%s" % (hhmmss, file_id[:8], uuid.uuid4().hex[:4], _safe(row["name"])))
    try:
        with open(img, "wb") as f:
            f.write(data)
    except Exception as e:
        return {"ok": False, "why": "写前像失败: %s" % str(e)[:60]}
    rec = {"id": uuid.uuid4().hex[:12], "at": local_iso(), "file_id": file_id, "name": row["name"],
           "path": db.display_path(file_id), "reason": reason, "writer": writer or "",
           "bytes": len(data), "sha256": sha256_bytes(data),
           "visibility": row["visibility"], "rag": row["rag"] or "on", "kind": row["kind"],
           "ext": row["ext"] or "",
           "image": str(img.relative_to(db.data_dir)).replace("\\", "/")}
    if extra:
        rec.update(extra)
    with io.open(_index_path(db), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    # 辞玖 2026-10-15：「做版本管理不做前像」—— 落完这一版，把同一篇更老的版本清掉，**每篇只留 3 版**。
    # 挂在这里 = 所有留前像的入口（writeback / pdf / docx / 删除）都自动按这个口径，
    # 不用各写一套（这个库以前就吃过"多个入口各写一套"的亏）。
    try:
        if _KEEP_VERSIONS > 0:                   # 0 = 不做版本管理（辞玖 2026-10-15 22:06）
            keep_versions(db, keep=_KEEP_VERSIONS)
    except Exception:
        pass                                     # 清理失败不影响这次留档本身
    return {"ok": True, **rec}


def index(db: Database, limit: int = 50, file_id: str | None = None) -> list:
    p = _index_path(db)
    if not p.is_file():
        return []
    out = []
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if file_id and rec.get("file_id") != file_id:
                continue
            out.append(rec)
    return out[-limit:][::-1]                     # 新的在前


def restore(db: Database, image_id: str = "", file_id: str = "", writer: str = "",
            master: bytes | None = None) -> dict:
    """按前像恢复：文件行还在 → 换回正文；已被删除 → 重建那一行并放回原文件夹。"""
    cands = index(db, limit=10000, file_id=file_id or None)
    if image_id:
        cands = [c for c in cands if c.get("id") == image_id]
    if not cands:
        return {"ok": False, "error": "没找到前像"}
    rec = cands[0]
    img = db.data_dir / rec["image"]
    if not img.is_file():
        return {"ok": False, "error": "前像文件不在了: %s" % rec["image"]}
    data = img.read_bytes()
    row = db.file_by_id(rec["file_id"])
    if row is not None:                                     # 行还在：换回正文
        db.put_content(rec["file_id"], data, master)
        return {"ok": True, "mode": "content", "file_id": rec["file_id"], "name": row["name"],
                "bytes": len(data), "from": rec["at"], "reason": rec.get("reason")}
    path = rec.get("path") or ""
    folder_path = path.rsplit("/", 1)[0] if "/" in path else ""
    parent = db.ensure_folder_path(folder_path)
    fid = db.create_file(parent, rec["name"], data, visibility="internal")
    if (rec.get("rag") or "on") != "on":
        db.set_rag(fid, False)
    return {"ok": True, "mode": "recreated", "file_id": fid, "name": rec["name"],
            "bytes": len(data), "from": rec["at"], "reason": rec.get("reason"),
            "path": folder_path}


def purge_records(db: Database, ids=None, file_id: str = "", names=None) -> dict:
    """**按条彻底删除**：把指定前像所属的那一篇的**所有版本**连文件带索引一起清掉。

    辞玖 2026-10-15：「回收站内的文件是包括其衍生出的版本等全部删除」——
    所以给 `ids`（清单里点中的那几条）时，会先把它们展开成 `file_id` 集合，
    再把**这些 file_id 的全部记录**清掉（含 overwrite 版本），不是只删点中的那一行。

    `names` 收没有 file_id 的老记录（用名字兜底匹配）。
    """
    p = _index_path(db)
    ids = set(x for x in (ids or []) if x)
    names = set(x for x in (names or []) if x)
    if not p.is_file():
        return {"ok": True, "removed": 0, "kept": 0, "files": 0}
    recs = []
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except Exception:
                continue

    # ① 先把点中的那几条展开成"整篇"：拿到它们的 file_id / name
    hit_fids, hit_names = set(), set()
    for rec in recs:
        if (rec.get("id") in ids) or (file_id and rec.get("file_id") == file_id):
            if rec.get("file_id"):
                hit_fids.add(rec["file_id"])
            if rec.get("name"):
                hit_names.add(rec["name"])
    hit_names |= names

    # ② 再把所有属于这些篇的记录都算进来（含衍生版本）
    keep, removed, gone_files = [], 0, set()
    for rec in recs:
        same = (rec.get("file_id") and rec["file_id"] in hit_fids) or (rec.get("name") in hit_names)
        if not same:
            keep.append(rec)
            continue
        img = db.data_dir / (rec.get("image") or "")
        try:
            if img.is_file():
                os.remove(img)
        except OSError:
            pass
        if rec.get("file_id"):
            gone_files.add(rec["file_id"])
        removed += 1

    with io.open(p, "w", encoding="utf-8") as f:
        for rec in keep:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "removed": removed, "kept": len(keep), "files": len(gone_files)}


def keep_versions(db: Database, keep: int = 3) -> dict:
    """每篇**只留最近 `keep` 版**（版本管理口径），更老的连文件带索引一起清。

    与 `prune(days)` 的区别：那个按**天数**，这个按**每篇的版本数**。
    辞玖 2026-10-15：「做版本管理不做前像」—— 所以默认口径是这个，不是那个。
    保留窗口大小写死 3：多留无益（真要看历史应该走 git / 版本库，而不是前像堆）。
    """
    p = _index_path(db)
    if not p.is_file():
        return {"ok": True, "removed": 0, "kept": 0}
    groups: dict = {}
    order = []
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            key = rec.get("file_id") or rec.get("name") or "?"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(rec)

    keep_recs, removed = [], 0
    for key in order:
        recs = groups[key]
        # 按时间倒序：新的在前，留前 keep 条
        # ⚠️ **删除留底不参与"留 3 版"的配额**（2026-10-15 修）：
        #    它是回收站恢复的对象，被版本挤掉就等于"删了还不给恢复"。
        #    编辑版本（overwrite 等）才按 keep 清。
        deletable = [r for r in recs if (r.get("reason") or "") != "delete"]
        protected = [r for r in recs if (r.get("reason") or "") == "delete"]
        deletable.sort(key=lambda r: r.get("at") or "", reverse=True)
        keep_now = deletable[:keep] + protected
        for rec in deletable[keep:]:
            img = db.data_dir / rec.get("image", "")
            try:
                if img.is_file():
                    os.remove(img)
            except OSError:
                pass
            removed += 1
        keep_recs.extend(keep_now)

    keep_recs.sort(key=lambda r: r.get("at") or "")
    with io.open(p, "w", encoding="utf-8") as f:
        for rec in keep_recs:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "removed": removed, "kept": len(keep_recs)}


def prune(db: Database, days: int = 30) -> dict:
    """删掉太老的前像（默认 30 天）。只删文件与索引行，不动别的。"""
    p = _index_path(db)
    if not p.is_file():
        return {"ok": True, "removed": 0, "kept": 0}
    cutoff = time.strftime("%Y%m%d", time.localtime(time.time() - days * 86400))
    keep, removed = [], 0
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if (rec.get("at") or "")[:10].replace("-", "") < cutoff:
                img = db.data_dir / rec.get("image", "")
                try:
                    if img.is_file():
                        os.remove(img)
                except OSError:
                    pass
                removed += 1
            else:
                keep.append(rec)
    with io.open(p, "w", encoding="utf-8") as f:
        for rec in keep:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "removed": removed, "kept": len(keep)}
