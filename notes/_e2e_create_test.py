# -*- coding: utf-8 -*-
"""端到端 · 第三批入口：直接在私密文件夹里**新建**一篇私密笔记。

`create_file(visibility='private')` 走的是 `_write_blob`（直写），不是 `put_content`
—— 如果它没处理包，那么"在私密文件夹里新建一篇"就会正文落在包外，
而读的时候（package 模式）去找包 → 找不到。

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
import priv_folder as pf
import privpack as pp
import vaultcrypto

PASS, FAIL = [], []
PW = "测试密码-e2e-3"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


print("=" * 78)
print("端到端 · 直接在私密文件夹里新建")
print("=" * 78)
td = tempfile.mkdtemp(prefix="e2e-c-")
try:
    data_dir = os.path.join(td, "data")
    lib_dir = os.path.join(td, "库")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(lib_dir, "个人", "私密"), exist_ok=True)
    d = dbmod.Database(data_dir)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib_dir)
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib_dir),
                  pp.pack_bytes(k0, pf.privpack.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")

    # 私密文件夹的 id
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)   # 空库里要先建
    check("拿到私密文件夹 id", bool(pfid), (pfid or "")[:8])
    print()

    # ── ① 直接在私密文件夹里新建（private）──────────────────
    print("① 直接在私密文件夹里新建 private 篇")
    body = "直接在私密文件夹里新建的正文\n".encode("utf-8")
    fid = d.create_file(pfid, "新建的.md", body, visibility="private", master=master)
    rel = d.disk_rel_of(fid)
    check("位置就在私密文件夹", pf.is_private_location(rel or ""), rel or "")
    check("路由判据认为该走包", d.priv_should_route_to_pack(fid) is True)
    in_pack = pf.read_items(lib_dir, master, d.vault_priv(master))
    check("**包里已经有它**（否则读不到）",
          pf.get_item_bytes(in_pack, "新建的.md") == body,
          "包内 %d 条" % len(in_pack))
    print()

    # ── ② 立刻读回 ──────────────────────────────────────────
    print("② 立刻读回")
    try:
        got = d.get_content(fid, master)
        check("读回逐字节相同", got == body, "%d 字节" % (len(got) if got else 0))
    except Exception as e:
        check("读回逐字节相同", False, "%s: %s" % (type(e).__name__, str(e)[:50]))
    print()

    # ── ③ 锁上后读必须失败 ──────────────────────────────────
    print("③ 锁上后读")
    try:
        d.get_content(fid, None)
        check("锁定态抛 LockedError", False, "竟然读到了")
    except vaultcrypto.LockedError:
        check("锁定态抛 LockedError", True)
    except Exception as e:
        check("锁定态抛 LockedError", False, "抛了别的：%s" % type(e).__name__)
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
