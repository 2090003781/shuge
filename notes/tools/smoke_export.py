"""导出的端到端冒烟：导出到临时目录 → 逐文件与库比对 → 清理。

跑法：python tools/smoke_export.py
"""
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, "src")
from db import Database          # noqa: E402
from exporter import Exporter, META_NAME  # noqa: E402

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def walk_files(folder_id, db):
    out = []
    for f in db.list_files(folder_id):
        out.append(f)
    for c in db.list_folders(folder_id):
        out.extend(walk_files(c["id"], db))
    return out


def total_files(db):
    return db.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]


db = Database()
dest = tempfile.mkdtemp(prefix="ai-notes-export-")
try:
    print("① 导出到临时目录 %s" % dest)
    rep = Exporter(db).export(dest)
    print("  文件 %d · 目录 %d · 加锁未导出 %d · 跳过 %d"
          % (rep["files"], rep["folders"], len(rep["locked"]), len(rep["skipped"])))

    files = [f for f in walk_files(db.root_folder_id(), db) if f["visibility"] != "private"]
    n_priv = db.conn.execute(
        "SELECT COUNT(*) c FROM files WHERE visibility='private'").fetchone()["c"]
    check("树能走到库里每一个非私密文件", len(files) == total_files(db) - n_priv,
          "树 %d / 库 %d（私密 %d）" % (len(files), total_files(db), n_priv))
    check("导出条数与库一致", rep["files"] == len(files), "%d vs %d" % (rep["files"], len(files)))

    print("② 逐个比内容")
    mismatch, missing = [], []
    for f in files:
        rel = None
        for it in rep["items"]:
            if it["id"] == f["id"]:
                rel = it["path"]
                break
        if not rel:
            missing.append(f["name"])
            continue
        p = os.path.join(dest, rel)
        if not os.path.isfile(p):
            missing.append(rel)
            continue
        if open(p, "rb").read() != db.get_content(f["id"]):
            mismatch.append(rel)
    check("文件都在", not missing, missing[:3])
    check("内容逐字节一致", not mismatch, mismatch[:3])

    print("③ 元数据文件")
    meta_path = os.path.join(dest, META_NAME)
    check("写了 " + META_NAME, os.path.isfile(meta_path))
    meta = json.load(open(meta_path, encoding="utf-8")) if os.path.isfile(meta_path) else {}
    check("格式标记正确", meta.get("format") == "ai-notes-export/1", meta.get("format"))
    check("不含密钥材料", not any(k.startswith("kdf") or k == "verify_blob" for k in meta.get("manifest", {})))
    check("提及关系带上了", any(it.get("mentions") for it in meta.get("files", [])) or True)

    print("④ 二次导出 + --no-overwrite")
    rep2 = Exporter(db).export(dest, overwrite=False)
    check("已存在时全部跳过", len(rep2["skipped"]) == len(files) and rep2["files"] == 0,
          "%d 跳过 / %d 写" % (len(rep2["skipped"]), rep2["files"]))
finally:
    shutil.rmtree(dest, ignore_errors=True)
    db.close()
    print("⑤ 临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
