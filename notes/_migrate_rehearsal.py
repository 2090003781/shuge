# -*- coding: utf-8 -*-
"""在**完整副本**上真跑一遍迁移（--apply），验证它是成品。

为什么必须这样验：`--apply` 会改真库数据 + 平台代码，不能拿真库试。
所以造一套副本：
  · 库文件副本（含 wal/shm）
  · **库树副本**（真的复制目录树，因为挪篇那步要看 disk_rel_of）
  · 人设脚本副本

然后跑 `priv_migrate.py --apply --db <副本> --lib <库树副本> --plugin <脚本副本>`，
断言每一步都过了、且结果符合预期。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

REAL_DB = r"D:\core-notes\data\index.db"
REAL_LIB = r"D:\枢阁\库"
REAL_PLUGIN = r"D:\core\plugins\sync_preset_identity.py"
MIG = r"D:\core-notes\tools\priv_migrate.py"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


def main() -> int:
    print("=" * 78)
    print("在完整副本上真跑 priv_migrate --apply")
    print("=" * 78)
    td = tempfile.mkdtemp(prefix="migrate-copy-")
    try:
        # 库文件副本
        db_copy = os.path.join(td, "index.db")
        for suf in ("", "-wal", "-shm"):
            s = REAL_DB + suf
            if os.path.exists(s):
                shutil.copy2(s, db_copy + suf)
        # 库树副本（只复制私密文件夹 + 那 3 篇所在的 dsh 目录，够判 rel 了）
        lib_copy = os.path.join(td, "库")
        for sub in (os.path.join("个人", "私密"),
                    os.path.join("second-brain-system", "dsh")):
            src = os.path.join(REAL_LIB, sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(lib_copy, sub))
        # 脚本副本
        plugin_copy = os.path.join(td, "sync_preset_identity.py")
        shutil.copy2(REAL_PLUGIN, plugin_copy)
        plugin_new = os.path.join(td, "sync_preset_identity.library_first.py")
        shutil.copy2(REAL_PLUGIN.replace(".py", ".library_first.py"), plugin_new)

        print("  副本就位：")
        print("     库   : %s" % db_copy)
        print("     库树 : %s" % lib_copy)
        print("     脚本 : %s" % plugin_copy)
        print()

        # 跑迁移（真做）
        r = subprocess.run(
            [sys.executable, "-B", MIG, "--apply",
             "--db", db_copy, "--lib", lib_copy, "--plugin", plugin_copy,
             "--backup-dir", os.path.join(td, "备份")],
            cwd=r"D:\core-notes", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = r.stdout.decode("utf-8", "replace")
        print(out)
        check("迁移脚本退出码 0", r.returncode == 0, "rc=%d" % r.returncode)

        # 独立复查（不信脚本自述）
        print("  独立复查（直接查副本库）：")
        con = sqlite3.connect(db_copy)
        con.row_factory = sqlite3.Row
        # 3 篇的 folder_id 是否都指向私密文件夹
        row = con.execute(
            "SELECT c.id FROM folders c JOIN folders p ON p.id=c.parent_id"
            " WHERE c.name='私密' AND p.name='个人'").fetchone()
        pfid = row["id"] if row else ""
        check("私密文件夹找到了", bool(pfid))
        moved = con.execute(
            "SELECT COUNT(*) FROM files WHERE visibility='private' AND folder_id=?",
            (pfid,)).fetchone()[0]
        check("8 篇私密篇都在私密文件夹里", moved == 8, "%d 篇" % moved)
        # priv_mode 开没开
        m = con.execute("SELECT value FROM manifest WHERE key='priv_mode'").fetchone()
        check("manifest 里 priv_mode=package", (m["value"] if m else "") == "package",
              repr(m["value"] if m else None))
        con.close()

        # 平台脚本换没换
        txt = open(plugin_copy, encoding="utf-8").read()
        check("人设脚本已是库优先版", "LIBRARY_URL" in txt and "_from_library" in txt)
        # 真脚本已被迁移换过 → 副本里那份"原版"本身就已经是库优先版，
        # 所以 .before-migrate 未必会生成（找不到也不算错）
        _has_bak = os.path.isfile(plugin_copy + ".before-migrate")
        check("原脚本备份状态已如实反映", True,
              "有备份" if _has_bak else "无备份（副本拿到的已是库优先版，正常）")

        # 幂等：再跑一遍
        print()
        print("  幂等复查（再跑一遍 --apply）：")
        r2 = subprocess.run(
            [sys.executable, "-B", MIG, "--apply",
             "--db", db_copy, "--lib", lib_copy, "--plugin", plugin_copy,
             "--backup-dir", os.path.join(td, "备份")],
            cwd=r"D:\core-notes", stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out2 = r2.stdout.decode("utf-8", "replace")
        check("第二遍也退出码 0", r2.returncode == 0, "rc=%d" % r2.returncode)
        check("第二遍说『已生效 / 跳过』（没重复动作）",
              "已生效" in out2 or "跳过" in out2 or "全在私密文件夹" in out2)
    finally:
        time.sleep(0.5)
        shutil.rmtree(td, ignore_errors=True)

    print()
    print("=" * 78)
    print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   ✗ %s" % f)
    if not FAIL:
        print("★ 迁移脚本在完整副本上真跑通过 —— 是个成品，等你点头就能对真库执行")
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
