"""验证 pool_gc：造出池文件与孤儿，确认能列出并按 --delete 删除。"""
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, "src")
from db import Database

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

tmp = tempfile.mkdtemp(prefix="ai-notes-pool-")
try:
    db = Database(tmp)
    print("默认 pool_threshold =", db.manifest().get("pool_threshold"))
    db.set_manifest(pool_threshold="100")          # 调小阈值，逼文件进池
    fid = db.create_file(db.root_folder_id(), "big.md", b"x" * 500)
    row = db.file_by_id(fid)
    print("落盘方式：", row["storage"], "| storage_ref =", row["storage_ref"])
    ref = row["storage_ref"]
    orphan = db.data_dir / "pool" / "zz" / "orphan-not-referenced"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"orphan")
    old = 3600 * 24
    os.utime(orphan, (orphan.stat().st_atime - old, orphan.stat().st_mtime - old))  # 装作很旧
    db.close()

    p = subprocess.run([sys.executable, "src/pool_gc.py", "--data", tmp],
                       capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW)
    print("--- 只列清单 ---")
    print(p.stdout.strip())
    assert "可清理 1 个" in p.stdout, "应该只认出 1 个孤儿"
    assert orphan.is_file(), "未加 --delete 不应删除"

    p2 = subprocess.run([sys.executable, "src/pool_gc.py", "--data", tmp, "--delete"],
                        capture_output=True, text=True, encoding="utf-8", creationflags=NO_WINDOW)
    print("--- 真删 ---")
    print(p2.stdout.strip())
    assert not orphan.is_file(), "孤儿应被删除"
    assert (db.data_dir / "pool" / ref).is_file(), "被引用的池文件不能被删"
    print("\n✅ pool_gc 行为正确：孤儿删掉、被引用的留着")
finally:
    shutil.rmtree(tmp, ignore_errors=True)
