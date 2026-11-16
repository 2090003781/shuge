# -*- coding: utf-8 -*-
"""验：包能不能**独立承载正文**（库内那份密文清掉之后还能不能读）。

为什么这件事重要：迁移之后"包是**读**的真相源"。如果读的时候还是绕回库内那份密文，
那包就只是个副本 —— 而 C 项（盘上/库里那些独立密文留不留）讨论的前提就是
"包能不能独立撑住"。这里把它验死。

判据（全程真代码 + 自设密码）：
  ① 包模式下写一篇 → 读得到
  ② **把库内 content 列清空**（模拟"清掉第二份密文"）→ 还能读得到
  ③ 清空后再写一次 → 仍然对（写路径不依赖库内那份）
  ④ 对照：legacy 模式下同样清空 → **读不到**（这证明②的通过不是巧合）
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
import privpack as pp
import vaultcrypto

PASS, FAIL = [], []
PW = "测试密码-独立承载"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def fresh(tmp, name, mode):
    data_dir = os.path.join(tmp, name + "-data")
    lib_dir = os.path.join(tmp, name + "-lib")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(lib_dir, "个人", "私密"), exist_ok=True)
    d = dbmod.Database(data_dir)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib_dir)
    if mode == "package":
        k0 = vaultcrypto.new_key()
        pp.write_file(pf.pack_abs(lib_dir),
                      pp.pack_bytes(k0, pp.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode=mode)
    return d, master, lib_dir


def clear_library_content(d, fid):
    """把库内那份正文清掉（模拟"库里不再留第二份密文"）。返回清之前它有多大。"""
    row = d.conn.execute("SELECT length(content) L FROM files WHERE id=?", (fid,)).fetchone()
    before = row["L"] or 0
    d.conn.execute("UPDATE files SET content=NULL, storage_ref=NULL WHERE id=?", (fid,))
    d.conn.commit()
    return before


print("=" * 78)
print("验：包能不能独立承载正文")
print("=" * 78)
tmp = tempfile.mkdtemp(prefix="solo-")
try:
    # ── package 模式 ────────────────────────────────────────
    print("【package 模式】")
    d, master, lib = fresh(tmp, "pkg", "package")
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)
    body = "包独立承载的正文。\n第二行。\n".encode("utf-8")
    fid = d.create_file(pfid, "独立.md", body, visibility="private", master=master)

    check("① 写得进、读得到", d.get_content(fid, master) == body)
    check("① 包里也有", pf.get_item_bytes(
        pf.read_items(lib, master, d.vault_priv(master)), "独立.md") == body)

    L = clear_library_content(d, fid)
    print("     已清掉库内那份（原 %d 字节）" % L)
    try:
        got = d.get_content(fid, master)
        check("② **清掉库内那份后仍读得到**", got == body,
              "%d 字节" % len(got))
    except Exception as e:
        check("② **清掉库内那份后仍读得到**", False,
              "%s: %s" % (type(e).__name__, str(e)[:50]))

    # ③ 清空后再写一次
    body2 = "清空后又改了一版。\n".encode("utf-8")
    try:
        d.put_content(fid, body2, master)
        got2 = d.get_content(fid, master)
        check("③ 清空后再写、再读都对", got2 == body2, "%d 字节" % len(got2))
        check("③ 包里也是新那版",
              pf.get_item_bytes(pf.read_items(lib, master, d.vault_priv(master)),
                                "独立.md") == body2)
    except Exception as e:
        check("③ 清空后再写、再读都对", False, "%s: %s" % (type(e).__name__, str(e)[:50]))
    d.close()
    print()

    # ── 对照：legacy 模式清空后应读不到 ─────────────────────
    print("【对照 legacy 模式】同样清空 → 应读不到")
    d2, m2, lib2 = fresh(tmp, "leg", "legacy")
    pfid2 = d2.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)
    b2 = "legacy 的正文。\n".encode("utf-8")
    fid2 = d2.create_file(pfid2, "对照.md", b2, visibility="private", master=m2)
    check("legacy 写得进、读得到", d2.get_content(fid2, m2) == b2)
    clear_library_content(d2, fid2)
    try:
        got3 = d2.get_content(fid2, m2)
        check("**legacy 清空后读不到**（对照组）", got3 != b2,
              "读到了 %d 字节（说明清空没生效？）" % len(got3))
    except Exception:
        check("**legacy 清空后读不到**（对照组）", True, "抛错 = 读不到，符合预期")
    d2.close()
finally:
    import time
    time.sleep(0.4)
    shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ 包能独立承载正文 —— 清掉库内那份密文之后照读照写（这正是 C 项讨论的前提）")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
