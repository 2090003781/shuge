# -*- coding: utf-8 -*-
"""端到端 · 第五批入口：从磁盘**导入**私密篇（`importer`）。

导入路径用的是我刚修的两个入口：
  · 新建        → `create_file(visibility='private')`   （L218）
  · 补私密权限  → `set_visibility(→private)`            （L207）
  · 内容覆盖    → `put_content(...)`                     （L197）

三个场景都要在 package 模式下验"包里跟没跟上"。

全程真代码 + 自设密码。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import importer
import priv_folder as pf
import privpack as pp
import vaultcrypto

PASS, FAIL = [], []
PW = "测试密码-e2e-5"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def in_pack(lib_dir, d, master, name):
    items = pf.read_items(lib_dir, master, d.vault_priv(master))
    return pf.get_item_bytes(items, name)


print("=" * 78)
print("端到端 · 导入私密篇")
print("=" * 78)
td = tempfile.mkdtemp(prefix="e2e-imp-")
try:
    data_dir = os.path.join(td, "data")
    lib_dir = os.path.join(td, "库")
    src_dir = os.path.join(td, "外来")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(lib_dir, "个人", "私密"), exist_ok=True)
    os.makedirs(src_dir, exist_ok=True)
    d = dbmod.Database(data_dir)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib_dir)
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib_dir),
                  pp.pack_bytes(k0, pf.privpack.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)

    body1 = "导入进来的私密正文 v1\n".encode("utf-8")
    f1 = os.path.join(src_dir, "导入的.md")
    open(f1, "wb").write(body1)

    # ── ① 新建导入（private）到私密文件夹 ───────────────────
    print("① 新建导入：private → 私密文件夹")
    UNDER = "vault/" + pf.PRIV_DIR_REL          # under 是**路径**（相对库根）
    res = importer.import_path(d, f1, under=UNDER, rag_off=False, overwrite=False,
                               visibility="private", master=master, convert_refs=False)
    check("导入成功（imported>=1）", (res.get("imported") or 0) >= 1, str(res.get("imported")))
    row = d.conn.execute("SELECT id,name FROM files WHERE name='导入的.md'").fetchone()
    check("库里有这一篇", row is not None)
    if row:
        fid = row["id"]
        check("**包里已经有它**", in_pack(lib_dir, d, master, "导入的.md") == body1)
        check("读回逐字节相同", d.get_content(fid, master) == body1)

        # ── ③ 覆盖导入（内容变了）→ put_content 那条路 ────────
        print()
        print("③ 覆盖导入：内容改了 + overwrite=True")
        body2 = "导入进来的私密正文 v2（改过）\n".encode("utf-8")
        open(f1, "wb").write(body2)
        res3 = importer.import_path(d, f1, under=UNDER, rag_off=False, overwrite=True,
                                    visibility="private", master=master, convert_refs=False)
        print("     updated=%s kept=%s changed=%s"
              % (res3.get("updated"), res3.get("kept"), res3.get("changed")))
        got = d.get_content(fid, master)
        check("**库读到的是 v2**", got == body2, "%d 字节" % len(got))
        check("**包里那份也是 v2**", in_pack(lib_dir, d, master, "导入的.md") == body2,
              "包内 %d 字节" % len(in_pack(lib_dir, d, master, "导入的.md") or b""))

    # ── ② 已有普通篇 → 导入时补私密权限 ──────────────────────
    print()
    print("② 补权限导入：库里已有（internal）→ 导入时 visibility=private")
    body3 = "先以普通篇入库的正文\n".encode("utf-8")
    f3 = os.path.join(src_dir, "补权限的.md")
    open(f3, "wb").write(body3)
    r_a = importer.import_path(d, f3, under=UNDER, rag_off=False, overwrite=False,
                               visibility="internal", master=master, convert_refs=False)
    row3 = d.conn.execute("SELECT id FROM files WHERE name='补权限的.md'").fetchone()
    check("先作为 internal 导入成功", row3 is not None)
    if row3:
        f3id = row3["id"]
        check("此刻不是私密", not d.file_by_id(f3id)["enc_algo"])
        # 再导入一次，这次要 private → 走 set_visibility 补权限
        r_b = importer.import_path(d, f3, under=UNDER, rag_off=False, overwrite=False,
                                   visibility="private", master=master, convert_refs=False)
        print("     secured=%s" % r_b.get("secured"))
        check("已加密", bool(d.file_by_id(f3id)["enc_algo"]),
              d.file_by_id(f3id)["enc_algo"])
        check("**包里已经有它**（否则读不到）",
              in_pack(lib_dir, d, master, "补权限的.md") == body3)
        check("读回逐字节相同", d.get_content(f3id, master) == body3)
    d.close()
finally:
    import time
    time.sleep(0.4)
    shutil.rmtree(td, ignore_errors=True)

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
print("=" * 78)
sys.exit(0 if not FAIL else 1)
