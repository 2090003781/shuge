# -*- coding: utf-8 -*-
"""端到端 · 第四批入口：`set_visibility(→ private)`，且该篇**已在私密文件夹里**。

这是"拖进私密文件夹"的真实两步：
    ① 移动（folder_id 进私密文件夹）→ ② 设为私密（转加密）
第 ② 步走 `set_visibility`，而它内部用的是 `_write_blob`（**直写**）——
与 `create_file` 同一个形状：可能整个绕开包路由。

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
PW = "测试密码-e2e-4"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


print("=" * 78)
print("端到端 · set_visibility(→ private)，篇已在私密文件夹里")
print("=" * 78)
td = tempfile.mkdtemp(prefix="e2e-d-")
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
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)

    # ── 准备：先在私密文件夹里建一篇**普通**篇 ────────────────
    print("准备：私密文件夹里先放一篇普通篇（internal）")
    body = "先普通、后转私密的正文\n".encode("utf-8")
    fid = d.create_file(pfid, "待转私密.md", body, visibility="internal", master=master)
    rel = d.disk_rel_of(fid)
    check("位置在私密文件夹", pf.is_private_location(rel or ""), rel or "")
    check("还是普通篇（未加密）", not d.file_by_id(fid)["enc_algo"])
    check("此刻路由判据为 False（还不是私密篇）",
          d.priv_should_route_to_pack(fid) is False)
    print()

    # ── ① 设为私密 ──────────────────────────────────────────
    print("① set_visibility(→ private)")
    r = d.set_visibility(fid, "private", master)
    print("     返回：%s" % {k: r.get(k) for k in ("visibility", "encrypted", "via", "bytes")})
    check("已加密", bool(d.file_by_id(fid)["enc_algo"]),
          d.file_by_id(fid)["enc_algo"])
    check("路由判据现在该为 True", d.priv_should_route_to_pack(fid) is True)
    in_pack = pf.read_items(lib_dir, master, d.vault_priv(master))
    check("**包里已经有它**（否则读不到）",
          pf.get_item_bytes(in_pack, "待转私密.md") == body,
          "包内 %d 条" % len(in_pack))
    print()

    # ── ② 读回 ──────────────────────────────────────────────
    print("② 立刻读回")
    try:
        got = d.get_content(fid, master)
        check("读回逐字节相同", got == body, "%d 字节" % (len(got) if got else 0))
    except Exception as e:
        check("读回逐字节相同", False, "%s: %s" % (type(e).__name__, str(e)[:50]))
    print()

    # ── ③ 再转回普通（走上一轮修的那条路）────────────────────
    print("③ 再转回 internal（pack_dropped 应为 True）")
    r2 = d.set_visibility(fid, "internal", master)
    after = pf.read_items(lib_dir, master, d.vault_priv(master))
    check("包里那条被删了", pf.get_item_bytes(after, "待转私密.md") is None,
          "包内 %d 条" % len(after))
    check("返回里报了 pack_dropped", r2.get("pack_dropped") is True,
          str(r2.get("pack_dropped")))
    check("转回后读得到（走库内那份）", d.get_content(fid, master) == body)
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
