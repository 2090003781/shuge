# -*- coding: utf-8 -*-
"""库文件树搬迁（10-15 敲定版）。

    D:\\枢阁\\库\\日志\\      → D:\\枢阁\\库\\记录\\日志\\
    D:\\枢阁\\库\\日报\\      → D:\\枢阁\\库\\记录\\日报\\
    D:\\枢阁\\库\\周报\\      → D:\\枢阁\\库\\记录\\周报\\
    D:\\枢阁\\库\\记忆\\      → D:\\枢阁\\库\\系统\\记忆\\
    D:\\枢阁\\库\\系统设计\\   → D:\\枢阁\\库\\第二大脑项目\\系统设计\\
    D:\\枢阁\\库\\second-brain-system\\ → D:\\枢阁\\库\\第二大脑项目\\second-brain-system\\

库内路径口径带一层 `vault/`；磁盘口径去掉它。DB 与磁盘必须一起搬：这个库是
disk_first=1，`folder_path` 一改，落盘投影的相对路径当场就变了 —— 只改 DB 不搬盘
= 树里看着对、盘上是空壳（下次 materialize 会当成「盘上缺文件」）。

用法：
    python tools/migrate_tree.py                 # 预演（不动任何东西）
    python tools/migrate_tree.py --apply         # 真搬
    python tools/migrate_tree.py --apply --db <另一个 index.db> --lib <另一个库目录>
    python tools/migrate_tree.py --rollback <备份的 index.db>
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

# (源, 目标父, 新名字) —— 库内相对路径口径，**不带** vault/
MOVES = [
    ("日志", "记录", "日志"),
    ("日报", "记录", "日报"),
    ("周报", "记录", "周报"),
    ("记忆", "系统", "记忆"),
    ("系统设计", "第二大脑项目", "系统设计"),
    ("second-brain-system", "第二大脑项目", "second-brain-system"),
    ("项目/第二大脑", "第二大脑项目", "第二大脑"),
    # 对齐文档 §九-12「砍法」：reasonix-raw 移进 Archive（677 篇 / 11.7MB，树里最占地方的一坨）
    ("记录/日志/reasonix-raw", "Archive", "reasonix-raw"),
]


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def db_backup(db_path: str, backup_dir: str) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(backup_dir, "index-before-tree-%s.db" % stamp)
    src = sqlite3.connect(db_path, timeout=30)
    out = sqlite3.connect(dst)
    try:
        src.backup(out)
    finally:
        out.close()
        src.close()
    return dst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真搬（默认只预演）")
    ap.add_argument("--db", default=os.path.join(ROOT, "data", "index.db"))
    ap.add_argument("--lib", default=r"D:\枢阁\库")
    ap.add_argument("--backup-dir", default=r"D:\枢阁\镜像备份")
    ap.add_argument("--rollback", default="", help="从某个备份 DB 反向搬回")
    a = ap.parse_args()

    import db as dbmod
    import tree_write

    data_dir = os.path.dirname(os.path.abspath(a.db))   # Database 收的是目录，它自己拼 index.db
    d = dbmod.Database(data_dir)
    lib_root = a.lib
    manifest_lib = (d.manifest() or {}).get("library_dir") or ""
    print("DB      :", a.db)
    print("库根    :", lib_root, "（manifest=%s）" % manifest_lib)
    if manifest_lib and os.path.normcase(os.path.abspath(manifest_lib)) != os.path.normcase(os.path.abspath(lib_root)):
        print("!! 传入的 --lib 与 manifest 不一致，拒绝继续")
        return 2

    root_id = d.root_folder_id()
    print("根节点  :", root_id)
    print()

    # ---- 逐个检查源/目标 ----
    plan = []
    for src_rel, dst_parent_rel, new_name in MOVES:
        src_id = d.folder_by_path("vault/" + src_rel)
        if not src_id:
            print("[跳过] %-22s 库内找不到" % src_rel)
            continue
        src_disk = os.path.join(lib_root, *src_rel.split("/"))
        if not os.path.isdir(src_disk):
            print("[跳过] %-22s 盘上找不到 %s" % (src_rel, src_disk))
            continue
        dst_disk = os.path.join(lib_root, *dst_parent_rel.split("/"), new_name)
        plan.append((src_rel, dst_parent_rel, new_name, src_id, src_disk, dst_disk))
        print("[待搬] %-22s → %-24s （%d 篇）"
              % (src_rel, dst_parent_rel + "/" + new_name, len(d.folder_file_ids(src_id))))

    if not plan:
        print("没有可搬的，结束")
        return 0
    if not a.apply and not a.rollback:
        print()
        print("== 预演结束（加 --apply 才真搬）==")
        return 0

    # ---- 备份 ----
    bak = db_backup(a.db, a.backup_dir)
    print()
    print("DB 备份 :", bak)
    print()

    os.makedirs(lib_root, exist_ok=True)
    done = []                                   # [(src_disk, dst_disk)] —— 出事按逆序搬回去
    try:
        for src_rel, dst_parent_rel, new_name, src_id, src_disk, dst_disk in plan:
            parent_id = d.ensure_folder_path("vault/" + dst_parent_rel)
            if os.path.exists(dst_disk):
                print("[错误] 目标已存在，停手：", dst_disk)
                raise SystemExit(3)
            # 先搬盘、再改库：磁盘是落盘投影的真源（disk_first=1），盘上错了库还能重建
            os.makedirs(os.path.dirname(dst_disk), exist_ok=True)   # ensure_folder_path 只建库里的节点，不建盘上目录
            os.rename(src_disk, dst_disk)
            done.append((src_disk, dst_disk))
            r = d.move_folder(src_id, parent_id, new_name)
            if not r.get("ok"):
                print("[错误] move_folder 拒绝：", r)
                raise SystemExit(3)
            rw = (r.get("refs") or {}).get("rewritten") or {}
            sk = (r.get("refs") or {}).get("skipped_locked") or []
            print("[已搬] %-22s → %-34s folder=%s 文件=%d 引用改写=%d 跳过=%d"
                  % (src_rel, dst_parent_rel + "/" + new_name, src_id[:8],
                     r.get("files") or 0, len(rw), len(sk)))
            for fid in list(rw)[:5]:
                print("       引用改写:", fid[:8], "x", rw[fid])
    except BaseException as e:
        print("出事，把盘上已搬的搬回去：", type(e).__name__, e)
        for s, t2 in reversed(done):
            if os.path.isdir(t2) and not os.path.exists(s):
                os.rename(t2, s)
        raise

    print()
    print("== 完事，自检 ==")
    for src_rel, dst_parent_rel, new_name, src_id, src_disk, dst_disk in plan:
        new_rel = "vault/" + dst_parent_rel + "/" + new_name
        ok_db = d.folder_by_path(new_rel) == src_id
        n_expect = len(d.folder_file_ids(src_id))
        n_disk = sum(len(fs) for _, _, fs in os.walk(dst_disk))
        print("  %-30s DB=%s 盘上文件=%d 库内=%d"
              % (dst_parent_rel + "/" + new_name, "ok" if ok_db else "不一致", n_disk, n_expect))
    return 0


if __name__ == "__main__":
    sys.exit(main())
