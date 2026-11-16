"""投递箱 v1.1 库侧自测：同名文件"改了要不要跟上"的四种情形。

  python tools/smoke_import_overwrite.py

情形（全部只碰临时目录，不连服务、不动真库）：
  ① 内容没变        → 跳过（去重照旧）
  ② 同名内容变了、不传 overwrite → 跳过（v1 老行为，一字不变）
  ③ 同名内容变了、传 overwrite、磁盘更新 → 覆盖：file_id 不变、正文换新、size/hash 跟上、rag 标记留着
  ④ 同名内容变了、传 overwrite、磁盘更旧（库里编辑过）→ kept：一个字都不动
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "src")
from db import Database                       # noqa: E402
from importer import import_path              # noqa: E402

ok_all = True


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def row_of(db, name):
    return db.conn.execute("SELECT * FROM files WHERE name=?", (name,)).fetchone()


def body_of(db, name):
    return db.get_content(row_of(db, name)["id"]).decode("utf-8")


src = tempfile.mkdtemp(prefix="ai-notes-ow-src-")
data = tempfile.mkdtemp(prefix="ai-notes-ow-db-")
p = os.path.join(src, "稿件.md")

try:
    db = Database(data)

    print("① 首次导入")
    open(p, "wb").write("# 第一版\n\n原始正文".encode("utf-8"))
    r1 = import_path(db, p, under="vault", tag="inbox")
    check("imported=1", r1["imported"] == 1, r1["imported"])
    fid0 = row_of(db, "稿件.md")["id"]
    db.set_rag(fid0, False)                      # 模拟投递箱：只存放、不进检索
    check("updated/kept 计数默认是 0", r1["updated"] == 0 and r1["kept"] == 0, r1)

    print("② 内容没变再导一次 → 去重跳过")
    r2 = import_path(db, p, under="vault", overwrite=True)
    check("skipped=1 且没更新", r2["skipped"] == 1 and r2["updated"] == 0, r2)

    print("③ 同名改了内容、不带 overwrite → v1 老行为：跳过")
    time.sleep(0.02)
    open(p, "wb").write("# 第二版\n\n改过的正文（不带开关）".encode("utf-8"))
    r3 = import_path(db, p, under="vault")
    check("skipped=1", r3["skipped"] == 1, r3)
    check("库里还是第一版", "第一版" in body_of(db, "稿件.md"), body_of(db, "稿件.md")[:20])

    print("④ 同名改了内容、带 overwrite、磁盘更新 → 覆盖")
    r4 = import_path(db, p, under="vault", overwrite=True)
    check("updated=1", r4["updated"] == 1, r4)
    check("skipped 不混进来", r4["skipped"] == 0, r4["skipped"])
    row = row_of(db, "稿件.md")
    check("file_id 不变（标签/引用/位置都还在）", row["id"] == fid0, row["id"])
    check("正文换成第二版", "第二版" in body_of(db, "稿件.md"), body_of(db, "稿件.md")[:20])
    check("size 跟上", row["size"] == os.path.getsize(p), (row["size"], os.path.getsize(p)))
    check("rag 标记没被导入重置", row["rag"] == "off", row["rag"])
    check("updated_ids 报出来给路由重建派生层", r4["updated_ids"] == [fid0], r4["updated_ids"])

    print("⑤ 库内编辑过（库里那行更新）→ 让库赢")
    db.put_content(fid0, "库内写的第三版".encode("utf-8"))     # 库里改：files.mtime 刷新
    old_disk = time.time() - 3600
    os.utime(p, (old_disk, old_disk))                        # 磁盘这份退回一小时前
    r5 = import_path(db, p, under="vault", overwrite=True)
    check("kept=1、updated=0", r5["kept"] == 1 and r5["updated"] == 0, r5)
    check("库里正文一个字没被动", body_of(db, "稿件.md") == "库内写的第三版", body_of(db, "稿件.md"))
    check("kept_files 报出是哪个文件", r5["kept_files"] == ["稿件.md"], r5["kept_files"])

    print("⑥ 磁盘又改新了（比库新）→ 再覆盖，还是同一个 file_id")
    now = time.time()
    open(p, "wb").write("# 第四版\n\n磁盘又改了".encode("utf-8"))
    os.utime(p, (now + 5, now + 5))                          # 明确比库里的行新
    r6 = import_path(db, p, under="vault", overwrite=True)
    check("updated=1", r6["updated"] == 1, r6)
    check("file_id 依旧不变", row_of(db, "稿件.md")["id"] == fid0, row_of(db, "稿件.md")["id"])
    check("正文是第四版", "第四版" in body_of(db, "稿件.md"), body_of(db, "稿件.md")[:20])

    db.close()
finally:
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(data, ignore_errors=True)
    print("⑦ 临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
