# -*- coding: utf-8 -*-
"""端到端 · 第六批入口：批量设私密 + 包内篇重建派生层。

覆盖两件事：
  ① **批量**：连续对多篇 `set_visibility(→private)`（界面"多选后设为私密"就是它）→ 每篇都该进包
  ② **重建派生层**：`indexer.reindex_file` 对包内那篇 —— 它要经 `get_content` 读包才能切块

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
from indexer import Indexer

PASS, FAIL = [], []
PW = "测试密码-e2e-6"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def in_pack(lib_dir, d, master, name):
    items = pf.read_items(lib_dir, master, d.vault_priv(master))
    return pf.get_item_bytes(items, name)


print("=" * 78)
print("端到端 · 批量设私密 + 包内篇重建派生层")
print("=" * 78)
td = tempfile.mkdtemp(prefix="e2e-batch-")
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

    # ── ① 批量：三篇普通篇 → 连续设私密 ──────────────────────
    print("① 批量设私密（三篇，模拟界面多选）")
    bodies = {}
    ids = []
    for i in (1, 2, 3):
        nm = "批量%d.md" % i
        b = ("批量第 %d 篇的正文，带一点可检索内容。\n" % i).encode("utf-8")
        bodies[nm] = b
        ids.append((nm, d.create_file(pfid, nm, b, visibility="internal", master=master)))
    ok_all, bad = True, []
    for nm, fid in ids:
        try:
            d.set_visibility(fid, "private", master)
        except Exception as e:
            ok_all = False
            bad.append("%s(%s)" % (nm, type(e).__name__))
    check("三篇都转私密没抛异常", ok_all, ("失败: %s" % bad) if bad else "3/3")
    miss = [nm for nm, _ in ids if in_pack(lib_dir, d, master, nm) != bodies[nm]]
    check("**三篇都进了包且内容对**", not miss, ("缺/不符: %s" % miss) if miss else "3/3")
    read_bad = []
    for nm, fid in ids:
        try:
            if d.get_content(fid, master) != bodies[nm]:
                read_bad.append(nm)
        except Exception as e:
            read_bad.append("%s(%s)" % (nm, type(e).__name__))
    check("三篇都读得回来", not read_bad, ("读不到: %s" % read_bad) if read_bad else "3/3")
    print()

    # ── ② 重建派生层（要经包读正文）──────────────────────────
    print("② indexer.reindex_file 对包内那篇")
    nm0, fid0 = ids[0]
    ix = Indexer(d)
    ix.master = lambda: master        # 上层注入（Indexer 默认是 None → 会被当锁定态）
    try:
        r = ix.reindex_file(fid0)
        check("reindex 没抛异常", True, "chunks=%s" % r.get("chunks"))
        check("**切出了块**（说明它经包读到了正文）", (r.get("chunks") or 0) > 0,
              "chunks=%s" % r.get("chunks"))
    except Exception as e:
        check("reindex 没抛异常", False, "%s: %s" % (type(e).__name__, str(e)[:50]))
        check("**切出了块**", False, "reindex 失败")
    print()

    # ── ③ 锁定态：reindex 应报 locked、不切块 ────────────────
    print("③ 锁定态 reindex（应报 locked）")
    ix_locked = Indexer(d)
    ix_locked.master = lambda: None            # 模拟锁上
    try:
        r2 = ix_locked.reindex_file(fid0)
        check("锁定态报 locked", (r2.get("locked") is True) or (r2.get("skipped") == "locked"),
              str({k: r2.get(k) for k in ("chunks", "locked", "skipped")}))
    except Exception as e:
        check("锁定态报 locked", False, "%s: %s" % (type(e).__name__, str(e)[:50]))
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
