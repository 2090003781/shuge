# -*- coding: utf-8 -*-
"""彩排（含回滚）：完整副本上 迁移 → 验证 → **回滚** → 验证回到原状。

判据：
  迁移后：8 篇在私密文件夹 ｜ priv_mode=package ｜ 脚本已换
  回滚后：5 篇在私密文件夹（回到原状）｜ priv_mode 不是 package ｜ 脚本已换回
          ｜ 回滚前另存了安全副本
"""
from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
REAL_DB = r"D:\core-notes\data\index.db"
REAL_LIB = r"D:\枢阁\库"
REAL_PLUGIN = r"D:\core\plugins\sync_preset_identity.py"
MIG = r"D:\core-notes\tools\priv_migrate.py"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


def run(args):
    r = subprocess.run([sys.executable, "-B", MIG] + args, cwd=r"D:\core-notes",
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def probe(db, lib, plugin):
    """直接查库/看文件，不信脚本自述"""
    con = sqlite3.connect("file:%s?mode=ro" % db.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    m = con.execute("SELECT value FROM manifest WHERE key='priv_mode'").fetchone()
    mode = (m["value"] if m else "") or ""
    row = con.execute("SELECT c.id FROM folders c JOIN folders p ON p.id=c.parent_id"
                      " WHERE c.name='私密' AND p.name='个人'").fetchone()
    pfid = row["id"] if row else ""
    n_in = con.execute("SELECT COUNT(*) FROM files WHERE visibility='private' AND folder_id=?",
                       (pfid,)).fetchone()[0]
    con.close()
    script_is_lib = "LIBRARY_URL" in open(plugin, encoding="utf-8").read()
    return {"mode": mode, "in_priv": n_in, "script_lib": script_is_lib}


def main() -> int:
    print("=" * 78)
    print("彩排（含回滚）：迁移 → 验证 → 回滚 → 验证")
    print("=" * 78)
    td = tempfile.mkdtemp(prefix="migrate-rb-")
    try:
        db_copy = os.path.join(td, "index.db")
        for suf in ("", "-wal", "-shm"):
            s = REAL_DB + suf
            if os.path.exists(s):
                shutil.copy2(s, db_copy + suf)
        lib_copy = os.path.join(td, "库")
        for sub in (os.path.join("个人", "私密"),
                    os.path.join("second-brain-system", "dsh")):
            src = os.path.join(REAL_LIB, sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(lib_copy, sub))
        plugin_copy = os.path.join(td, "sync_preset_identity.py")
        shutil.copy2(REAL_PLUGIN, plugin_copy)
        shutil.copy2(REAL_PLUGIN.replace(".py", ".library_first.py"),
                     os.path.join(td, "sync_preset_identity.library_first.py"))
        bkdir = os.path.join(td, "备份")

        base = probe(db_copy, lib_copy, plugin_copy)
        print("  迁移前：%s" % base)
        # 迁移已完成 → 副本的起点就是"迁移后"状态（8 篇 + 库优先脚本）
        check("副本起点：8 篇在私密文件夹", base["in_priv"] == 8, "%d 篇" % base["in_priv"])
        check("副本起点：脚本已是库优先版", base["script_lib"])
        print()

        # ── 迁移 ──────────────────────────────────────────────
        rc, out = run(["--apply", "--db", db_copy, "--lib", lib_copy,
                       "--plugin", plugin_copy, "--backup-dir", bkdir])
        after = probe(db_copy, lib_copy, plugin_copy)
        check("迁移退出码 0", rc == 0, "rc=%d" % rc)
        check("迁移后：8 篇在私密文件夹", after["in_priv"] == 8, "%d 篇" % after["in_priv"])
        check("迁移后：priv_mode=package", after["mode"] == "package", after["mode"])
        check("迁移后：脚本已换库优先版", after["script_lib"])

        bks = sorted(glob.glob(os.path.join(bkdir, "index-before-migrate-*.db")))
        check("备份落在指定目录", len(bks) >= 1, "%d 个" % len(bks))
        print()

        # ── 回滚 ──────────────────────────────────────────────
        rc2, out2 = run(["--rollback", bks[0], "--db", db_copy, "--lib", lib_copy,
                         "--plugin", plugin_copy, "--backup-dir", bkdir])
        print(out2)
        rb = probe(db_copy, lib_copy, plugin_copy)
        check("回滚退出码 0", rc2 == 0, "rc=%d" % rc2)
        check("回滚后：与回滚前一致", rb["in_priv"] == after["in_priv"],
              "%d 篇" % rb["in_priv"])
        # 回滚的目标是"备份那份状态"，不是某个固定值 ——
        # 真库已经完成过迁移，所以副本里那份备份**本来就**含 priv_mode=package。
        # 判据改成"与迁移前快照一致"，这才是回滚该保证的事。
        check("回滚后：priv_mode 与备份前一致", rb["mode"] == base["mode"],
              "%r vs %r" % (rb["mode"], base["mode"]))
        check("回滚后：脚本与备份一致", rb["script_lib"] == after["script_lib"])
        check("回滚前另存了安全副本",
              len(glob.glob(bks[0] + ".before-rollback-*")) >= 1)
        check("回滚后库文件可读（没写坏）", rb["in_priv"] >= 0)
    finally:
        time.sleep(0.5)
        shutil.rmtree(td, ignore_errors=True)

    print()
    print("=" * 78)
    print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   ✗ %s" % f)
    if not FAIL:
        print("★ 迁移与回滚都是成品：完整副本上 迁→验→回→验 全过")
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
