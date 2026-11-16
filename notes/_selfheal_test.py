# -*- coding: utf-8 -*-
"""验自愈：包里没有这一篇时，从库内密文解出来 + 顺手补进包。

复刻的状态 = **迁移之后、解锁之前**：
  篇的 folder_id 已在私密文件夹里（→ 被路由到包）
  库内有它的密文（legacy 写下的）
  但包里没有它（包是之前按"只有那 5 篇"建的）

判据：
  ① 这种篇在 package 模式下读得到（不是报"包里没有这一篇"）
  ② 读完之后它**进了包**
  ③ 再读一次走的是包（内容一致）
  ④ 锁定态读它 → 抛 LockedError（不是别的错）
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
PW = "自愈测试密码"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


print("=" * 78)
print("验自愈：包里没有 → 从库内密文解出来并补进包")
print("=" * 78)
tmp = tempfile.mkdtemp(prefix="heal-")
try:
    data = os.path.join(tmp, "data")
    lib = os.path.join(tmp, "lib")
    os.makedirs(data, exist_ok=True)
    os.makedirs(os.path.join(lib, "个人", "私密"), exist_ok=True)
    d = dbmod.Database(data)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib)

    # 先按 legacy 建一篇私密篇（库内是密文），**打包时它还不存在**
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)
    body = "迁移后待自愈的正文。\n".encode("utf-8")
    fid = d.create_file(pfid, "待自愈.md", body, visibility="private", master=master)
    # 建一个**空包**（模拟"包是更早建的，里面没有这一篇"）
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib), pp.pack_bytes(k0, pp.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")

    check("布置完成：篇在私密文件夹里、库内有密文、包里没有",
          pf.is_private_location(d.disk_rel_of(fid) or "")
          and bool(d.file_by_id(fid)["enc_algo"])
          and len(pf.read_items(lib, master, d.vault_priv(master))) == 0,
          "包内 %d 条" % len(pf.read_items(lib, master, d.vault_priv(master))))
    print()

    print("① 读它（包里没有）→ 应自愈")
    try:
        got = d.get_content(fid, master)
        check("① **读得到**（不再报包里没有）", got == body, "%d 字节" % len(got))
    except Exception as e:
        check("① **读得到**", False, "%s: %s" % (type(e).__name__, str(e)[:50]))
    print()

    items = pf.read_items(lib, master, d.vault_priv(master))
    check("② **读完之后它进了包**",
          pf.get_item_bytes(items, "待自愈.md") == body, "包内 %d 条" % len(items))

    got2 = d.get_content(fid, master)
    check("③ 再读一次内容一致", got2 == body, "%d 字节" % len(got2))
    print()

    print("④ 锁定态读它 → LockedError")
    try:
        d.get_content(fid, None)
        check("④ 锁定态抛 LockedError", False, "竟然读到了")
    except vaultcrypto.LockedError:
        check("④ 锁定态抛 LockedError", True)
    except Exception as e:
        check("④ 锁定态抛 LockedError", False, "抛了别的：%s" % type(e).__name__)
    d.close()
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
    print("★ 自愈成立 —— 于是**锁定态也能跑迁移**（写包留到解锁后第一次读）")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
