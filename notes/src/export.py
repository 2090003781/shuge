"""导出命令行：python src/export.py --out <目录> [--no-overwrite] [--no-meta] [--locks]

private 文件默认跳过；要连私密内容一起导出，加 --locks（会提示输入库密码）。
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import vaultcrypto                      # noqa: E402
from db import Database, DEFAULT_DATA_DIR  # noqa: E402
from exporter import Exporter           # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把库导出到磁盘")
    ap.add_argument("--out", required=True, help="目标目录（不存在则创建）")
    ap.add_argument("--data", default=None, help="库目录（默认 %s）" % DEFAULT_DATA_DIR)
    ap.add_argument("--no-overwrite", action="store_true", help="已存在同名文件时跳过")
    ap.add_argument("--no-meta", action="store_true", help="不写 .ai-notes-export.json")
    ap.add_argument("--locks", action="store_true", help="导出 private 文件（会提示输入库密码）")
    a = ap.parse_args()

    db = Database(a.data or DEFAULT_DATA_DIR)
    master = None
    if a.locks:
        kdf = db.vault_kdf()
        if kdf is None:
            print("库里还没设过密码，没有可导出的私密内容")
        else:
            pw = getpass.getpass("库密码：")
            cand = vaultcrypto.derive(pw, kdf["salt"], kdf["n"], kdf["r"], kdf["p"])
            if not vaultcrypto.check_master(cand, db.manifest().get("verify_blob") or ""):
                print("密码不对")
                return 2
            master = cand
    elif db.private_files():
        print("注意：库里有 %d 篇私密文件，本次不导出它们（要导出请加 --locks）" % len(db.private_files()))

    from events import EventLog
    rep = Exporter(db, master, EventLog(db)).export(
        a.out, overwrite=not a.no_overwrite, with_meta=not a.no_meta)
    print("导出到 %s" % rep["dest"])
    print("  文件 %d 个 · 目录 %d 个" % (rep["files"], rep["folders"]))
    if rep["skipped"]:
        print("  跳过（已存在）%d 个" % len(rep["skipped"]))
    if rep["locked"]:
        print("  未导出（加锁）%d 个：%s" % (len(rep["locked"]), "、".join(rep["locked"][:5])))
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
