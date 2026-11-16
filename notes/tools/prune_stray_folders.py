# -*- coding: utf-8 -*-
"""清掉库树里挂在 vault/ 外面（或已空）的杂枝，让顶层回到敲定的六分区。

只处理明确的垃圾：
  · 根级 `个人`（只剩一篇测试导入的简历.docx）
  · 根级 `产品化` / `产品化/新文件夹`（空壳）
  · 根级 `项目笔记`、`dsh`、`vault/dsh`、`vault/产品化`（空壳）
  · 根级散落的测试残片（shuge-pick-check.md / zz-unlock-diag.json，盘上早没了）

默认预演；`--apply` 真删。删文件走 db.delete_file（立墓碑，盘上那份由 deletions 账本负责）。
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

DROP_ROOT_FOLDERS = ["个人", "产品化", "产品化/新文件夹", "项目笔记", "dsh",
                     "vault/dsh", "vault/产品化"]
DROP_ROOT_FILES = ["shuge-pick-check.md", "zz-unlock-diag.json"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "index.db"))
    ap.add_argument("--lib", default=r"D:\枢阁\库")
    a = ap.parse_args()

    import db as dbmod
    d = dbmod.Database(os.path.dirname(os.path.abspath(a.db)))
    root_id = d.root_folder_id()

    def path(fid):
        parts, cur = [], fid
        for _ in range(40):
            r = d.folder_by_id(cur)
            if not r:
                break
            if r["name"]:
                parts.append(r["name"])
            cur = r["parent_id"]
            if not cur:
                break
        return "/".join(reversed(parts))

    print("== 要删的文件 ==")
    doomed_files = []
    for r in d.conn.execute("SELECT id,name,folder_id FROM files"):
        p = path(r["folder_id"]) if r["folder_id"] else ""
        if p in DROP_ROOT_FOLDERS or (not p and r["name"] in DROP_ROOT_FILES):
            doomed_files.append((r["id"], r["name"], p))
            print("   %-26s %s" % (p or "(根)", r["name"]))
    # 根级 个人 里那篇简历：盘上还在不在
    print()
    print("== 要删的空节点 ==")
    # ⚠️ 按**精确库内路径**取 id：`folder_by_path('个人')` 与 `folder_by_path('vault/个人')`
    #    是两个不同节点（前者是那次测试导入留下的根级壳）——用 "vault/x" 或 "x" 硬拼会误伤真的 vault/个人。
    doomed_folders = []
    exact = {}
    for r in d.conn.execute("SELECT id FROM folders"):
        exact[path(r["id"])] = r["id"]
    for p in DROP_ROOT_FOLDERS:
        fid = exact.get(p)
        if not fid:
            print("   %-26s （库里没有，跳过）" % p)
            continue
        n = d.folder_children_count(fid)
        sub = len([x for x in d.folder_file_ids(fid)])
        print("   %-26s 直接子项=%d 子树文件=%d" % (p, n, sub))
        doomed_folders.append((p, fid, sub))

    if not a.apply:
        print()
        print("== 预演结束（加 --apply 才真删）==")
        return 0

    for fid, name, p in doomed_files:
        d.delete_file(fid)
        print("[已删文件]", p, name)
    # 从最深的先删
    for p, fid, sub in sorted(doomed_folders, key=lambda x: -x[0].count("/")):
        for f in list(d.folder_file_ids(fid)):
            d.delete_file(f)
        # 先删子节点
        kids = [r["id"] for r in d.list_folders(fid)]
        for k in kids:
            try:
                d.delete_folder(k)
            except Exception as e:
                print("  子节点删不掉：", e)
        try:
            d.delete_folder(fid)
            print("[已删节点]", p)
        except Exception as e:
            print("[留手]", p, e)
        # 盘上那一份
        disk = os.path.join(a.lib, *[x for x in p.split("/") if x != "vault"])
        if os.path.isdir(disk) and not os.listdir(disk):
            try:
                shutil.rmtree(disk, ignore_errors=True)
                print("       盘上空目录已清:", disk)
            except Exception as e:
                print("       盘上目录没清掉:", e)
    d.conn.commit()
    print()
    print("== 完事 ==")
    for r in d.conn.execute("SELECT id,name FROM folders"):
        pp = path(r["id"])
        if pp.count("/") == 0 or (pp.startswith("vault/") and pp.count("/") == 1):
            print("   %s" % pp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
