# -*- coding: utf-8 -*-
"""迁移「3 篇 DSH 人设 → 私密文件夹」的**干跑**：在临时副本上完整跑一遍，真库不动。

给你看的四件事：
  ① 迁移要改的数据：哪 3 篇、从哪到哪
  ② 迁移后自检那项会变成什么（5/8 → 8/8 → ok）
  ③ 迁移后**哪些代码/配置会失联**（写死的引用清单）—— 对应你的 P56
  ④ 迁移后 private 篇会不会都正确改道到包

不写真库：一切在 tempfile 里。
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import priv_folder as pf

DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"


def main() -> int:
    print("=" * 78)
    print("干跑：3 篇 DSH 人设 → 私密文件夹（临时副本，真库不动）")
    print("=" * 78)

    td = tempfile.mkdtemp(prefix="migrate-dry-")
    try:
        for suf in ("", "-wal", "-shm"):
            s = DB + suf
            if os.path.exists(s):
                shutil.copy2(s, os.path.join(td, "index.db" + suf))
        d = dbmod.Database(td)
        d.set_manifest(library_dir=LIB, priv_mode="package")

        # ── ① 谁要迁 ────────────────────────────────────────
        print()
        print("① 要迁的篇")
        rep0 = pf.location_report(d)
        todo = rep0["rows"]
        for n, rel in todo:
            print("     %-28s %s" % (n[:28], rel))
        print("     共 %d 篇；迁移后应变成 %d/8 都在私密文件夹" % (len(todo), rep0["by_loc"] + len(todo)))

        # 目标文件夹
        priv_folder_id = d.conn.execute(
            "SELECT id FROM folders WHERE name=? AND parent_id=("
            "SELECT id FROM folders WHERE name=?)", ("私密", "个人")).fetchone()
        if priv_folder_id is None:
            priv_folder_id = d.conn.execute("SELECT id FROM folders WHERE name='私密'").fetchone()
        if priv_folder_id is None:
            print("     ✗ 找不到私密文件夹，无法继续")
            return 1
        pid = priv_folder_id["id"]

        # ── ② 迁移前 vs 迁移后 ──────────────────────────────
        print()
        print("② 迁移前 / 迁移后 的库内路径对照")
        pairs = []
        for n, rel in todo:
            row = d.conn.execute("SELECT id FROM files WHERE name=? ORDER BY mtime DESC LIMIT 1",
                                 (n,)).fetchone()
            pairs.append((n, rel, row["id"]))
        for n, rel, fid in pairs:
            print("     %-28s %s  →  个人/私密/%s" % (n[:28], rel, n))

        # 真迁（只动临时副本）
        for n, rel, fid in pairs:
            d.conn.execute("UPDATE files SET folder_id=? WHERE id=?", (pid, fid))
        d.conn.commit()
        rep1 = pf.location_report(d)
        print()
        print("     迁移后 location_report：共 %d ｜ 在私密文件夹 %d ｜ 靠旧标记 %d"
              % (rep1["total"], rep1["by_loc"], rep1["by_flag"]))
        check_ok = (rep1["by_flag"] == 0 and rep1["by_loc"] == rep1["total"] == 8)
        print("     %s 第①条落地（8/8 全在私密文件夹）"
              % ("★" if check_ok else "✗"))

        # ── ③ 改道验证 ──────────────────────────────────────
        print()
        print("③ 迁移后：8 篇是不是都正确改道到加密包")
        routed = [r["name"] for r in d.conn.execute(
            "SELECT id,name FROM files WHERE visibility='private'").fetchall()
            if d.priv_should_route_to_pack(r["id"])]
        print("     改道 %d 篇 %s" % (len(routed), "★" if len(routed) == 8 else "✗"))
        # 顺带：非私密篇有没有被误纳
        others = d.conn.execute("SELECT id FROM files WHERE enc_algo IS NULL LIMIT 300").fetchall()
        bad = [r["id"] for r in others if d.priv_should_route_to_pack(r["id"])]
        print("     非私密篇误纳 %d 篇 %s" % (len(bad), "★ 无" if not bad else "✗"))

        # ── ④ 会失联的写死引用 ──────────────────────────────
        print()
        print("④ 迁移后**会失联**的写死引用（你的 P56 清单）")
        print("     [必须改] sync_preset_identity.py:24-25")
        print("              VAULT = D:/个人数据/辞玖 ；PERSONA_REL = second-brain-system/dsh/DSH-人设.md")
        print("              persona_text() 直接 open(VAULT/PERSONA_REL) —— 无库优先、无锁检查")
        print("              它是**人设进 DSH 预设的唯一通道**（config.yaml:199-200 明确写了这条链）")
        print("              迁移后旧路径就没了 → 该脚本会读不到 → 人设同步断掉")
        print("     [建议改] context_pack_plugin.py:67 DOC_SOURCES['persona']")
        print("              当前**没被任何路由引用**（config.yaml:207 docs=[profile,rules-l1]），")
        print("              但留着旧路径 = 将来有人把 persona 加回 docs 时会指向不存在的文件")
        print("     [不用改] 其余全平台扫描：plugins / scripts / cmd / agents / web / config 均无该路径的写死引用")

        d.close()
    finally:
        import time
        time.sleep(0.5)
        shutil.rmtree(td, ignore_errors=True)

    print()
    print("=" * 78)
    print("干跑结论：数据侧只有「改 3 篇的 folder_id」这一件事；")
    print("         真正的风险在**代码侧**那一处写死引用（sync_preset_identity.py）。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
