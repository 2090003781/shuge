# -*- coding: utf-8 -*-
"""验 db.move_in()：把一篇移进私密文件夹。

判据：
  ① 移完 folder_id 在私密文件夹里（location_report 的 by_flag 减少）
  ② 还不是私密篇 → 被加密（enc_algo 非空、visibility='private'）
  ③ legacy 模式下 **不**写包（to_pack=False）
  ④ package 模式下、有 master → 写包（to_pack=True）
  ⑤ 顺序陷阱：判据必须在改 folder_id **之前**取 ——
     用一个"本来不在私密文件夹"的普通篇验证它确实被判为需要处理
  ⑥ 已经是私密篇再移一次 → 幂等（不重复加密）
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import priv_folder as pf
import vaultcrypto as _vc

DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def fresh(mode="legacy"):
    td = tempfile.mkdtemp(prefix="movein-")
    for suf in ("", "-wal", "-shm"):
        s = DB + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(td, "index.db" + suf))
    d = dbmod.Database(td)
    d.set_manifest(library_dir=LIB, priv_mode=mode)
    return d, td


print("=" * 78)
print("验 db.move_in()")
print("=" * 78)

# ── ① legacy 模式：移一个普通篇进私密文件夹 ──────────────────
print("① legacy 模式：普通篇 → 私密文件夹（应加密、但不写包）")
d, td = fresh("legacy")
try:
    # 挑一个不在私密文件夹里的普通篇
    row = d.conn.execute(
        "SELECT id,name,folder_id FROM files WHERE IFNULL(enc_algo,'')='' "
        "AND kind='markdown' AND IFNULL(content,'')<>'' LIMIT 1").fetchone()
    fid, name = row["id"], row["name"]
    rel0 = d.disk_rel_of(fid)
    check("挑到一篇普通篇", bool(rel0) and not pf.is_private_location(rel0), rel0[:52])
    rep0 = pf.location_report(d)

    r = d.move_in(fid, master=None)
    rel1 = d.disk_rel_of(fid)
    check("移完在私密文件夹里", pf.is_private_location(rel1), rel1[:52])
    check("已加密（enc_algo 非空）", bool(d.file_by_id(fid)["enc_algo"]),
          d.file_by_id(fid)["enc_algo"])
    check("visibility=private", (d.file_by_id(fid)["visibility"] or "") == "private")
    check("legacy 下不写包（to_pack=False）", r.get("to_pack") is False, r.get("note", "")[:40])
    rep1 = pf.location_report(d)
    # 注意：by_flag 是"**只**靠旧标记、位置不在私密文件夹"的篇数。
    # 移一篇**新**篇进来不会减少它（那 3 篇 DSH 人设没被碰）；该涨的是 by_loc，
    # 同时 total +1（多了一篇私密篇）。
    check("location_report：在私密文件夹的多了 1 篇",
          rep1["by_loc"] == rep0["by_loc"] + 1,
          "%d → %d" % (rep0["by_loc"], rep1["by_loc"]))
    check("靠旧标记的没变（那 3 篇没被碰）", rep1["by_flag"] == rep0["by_flag"],
          "%d → %d" % (rep0["by_flag"], rep1["by_flag"]))
    check("总数 +1（多了一篇私密篇）", rep1["total"] == rep0["total"] + 1,
          "%d → %d" % (rep0["total"], rep1["total"]))
    # ⑥ 再移一次 → 幂等
    r2 = d.move_in(fid, master=None)
    check("再移一次：不重复加密（encrypted=False）", r2.get("encrypted") is False)
    check("再移一次：仍在私密文件夹", pf.is_private_location(d.disk_rel_of(fid)))
finally:
    d.close(); time.sleep(0.3); shutil.rmtree(td, ignore_errors=True)
print()

# ── ② package 模式：有 master → 写包 ─────────────────────────
print("② package 模式：有 master → 写进加密包")
d, td = fresh("package")
try:
    row = d.conn.execute(
        "SELECT id,name FROM files WHERE IFNULL(enc_algo,'')='' AND kind='markdown' "
        "AND IFNULL(content,'')<>'' LIMIT 1").fetchone()
    fid = row["id"]
    # 本环境没有密码派生不出真 master —— 只用"是否走到写包"这一支来验，
    # 把 _priv_write_to_pack 换成记录调用的替身（其余全真）
    calls = []
    real = d._priv_write_to_pack
    d._priv_write_to_pack = lambda f, data, m: calls.append((f, len(data)))
    real_get = d.get_content
    d.get_content = lambda f, m=None: "假正文".encode("utf-8")
    try:
        r = d.move_in(fid, master=_vc.new_key())
    finally:
        d._priv_write_to_pack = real
        d.get_content = real_get
    check("to_pack=True", r.get("to_pack") is True, r.get("note", "")[:44])
    # 次数应为 1：set_visibility(-> private) 自己会写包，move_in 不再重复写。
    # 关键是"写包真发生过、且指向那一篇"。
    check("_priv_write_to_pack 真被调用（恰好 1 次）", len(calls) == 1,
          "调用 %d 次" % len(calls))
    check("写包指向的就是那一篇", all(c[0] == fid for c in calls) and bool(calls),
          "%d 次都指向同一篇" % len(calls))
finally:
    d.close(); time.sleep(0.3); shutil.rmtree(td, ignore_errors=True)
print()

# ── ③ package 模式但锁着 → 不写包，且说明清楚 ────────────────
print("③ package 模式但锁着（master=None）→ 不写包，说明清楚")
d, td = fresh("package")
try:
    row = d.conn.execute(
        "SELECT id,name FROM files WHERE IFNULL(enc_algo,'')='' AND kind='markdown' "
        "AND IFNULL(content,'')<>'' LIMIT 1").fetchone()
    r = d.move_in(row["id"], master=None)
    check("to_pack=False", r.get("to_pack") is False)
    check("note 里说明了原因", "锁着" in (r.get("note") or ""), (r.get("note") or "")[:56])
    check("但位置已经移进私密文件夹",
          pf.is_private_location(d.disk_rel_of(row["id"])))
finally:
    d.close(); time.sleep(0.3); shutil.rmtree(td, ignore_errors=True)
print()

print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ move_in 成立：移位置 + 加密 + （package 模式下）写包，且幂等")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
