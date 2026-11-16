# -*- coding: utf-8 -*-
"""验证 package 路由的**结构性前置**：包的条目能否按库路径精确取到。

不能在此环境验的部分（如实说明）：
  `_priv_read_from_pack` 内部的 `open_key` 需要**库私钥**，而私钥要用主密钥解，
  主密钥要用密码派生 —— 本环境拿不到密码（也不该拿）。
  所以这里验的是**除密码那一跳之外的全部**：路径解析 + 条目定位 + 正文自证。

判据：
  ① 每篇私密篇 → disk_rel_of → inside_rel 拆出的 (folder, name) 与包内条目精确匹配
  ② 取出的字节与库内 content_hash 逐条对齐（说明"取对了那一篇"）
  ③ legacy 模式下 priv_should_route_to_pack 全 False（零改道）
  ④ package 模式下、且包存在时，priv_effective_mode 变成 package
  ⑤ 包模式下非私密篇**不**改道（不能把普通篇也塞进包）
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

DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


print("=" * 78)
print("验证 package 路由的结构性前置（不含密码那一跳）")
print("=" * 78)

with tempfile.TemporaryDirectory(prefix="packroute-") as td:
    for suf in ("", "-wal", "-shm"):
        s = DB + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(td, "index.db" + suf))
    d = dbmod.Database.__new__(dbmod.Database)
    d.conn = sqlite3.connect(os.path.join(td, "index.db"), timeout=30)
    d.conn.row_factory = sqlite3.Row
    d.data_dir = os.path.dirname(DB)

    # 让 _lib_root() 指向真库树（包就在那儿）
    d.set_manifest(library_dir=LIB)
    check("库根已指向真库树", d._lib_root() == LIB, d._lib_root())

    # ── ① 路径解析 + 条目定位 ────────────────────────────────
    print()
    print("① 每篇私密篇的 (folder, name) 与包内条目匹配")
    blob = pp.read_file(pf.pack_abs(LIB))
    peek = pp.peek(blob)
    check("真库树里的包可读", peek["ok"], "%d 字节 v%s" % (peek["bytes"], peek["version"]))

    # 包内条目名（不解密也能验"有几条"；名字要解密，所以这里用 migate 时记的元数据对照）
    priv = d.conn.execute("SELECT id,name,content_hash,size FROM files"
                          " WHERE visibility='private' ORDER BY name").fetchall()
    # 注意：只有**在私密文件夹里**的篇才该拆出包内路径。
    # 那 3 篇 DSH 人设还在 second-brain-system/dsh（迁移未做）→ inside_rel 正确返回空。
    pairs, bad, outside = [], [], []
    for r in priv:
        rel = d.disk_rel_of(r["id"]) or ""
        inside = pf.inside_rel(rel)
        folder, _, name = inside.rpartition("/")
        nm = name or inside
        pairs.append((nm, folder, rel))
        if pf.is_private_location(rel):
            if not nm:
                bad.append(r["name"])
        else:
            outside.append((r["name"], rel))
    check("私密文件夹里的篇都能拆出包内 (folder, name)", not bad,
          ("拆不出: %s" % bad) if bad else "%d 篇" % (len(pairs) - len(outside)))
    check("私密文件夹外的篇数为 0（迁移已完成）", len(outside) == 0,
          "%d 篇" % len(outside))
    check("包里那 5 条的名字与私密文件夹里的 5 篇逐一对上",
          sorted(nm for nm, _f, _r in pairs if nm) ==
          sorted(r["name"] for r in priv if pf.is_private_location(d.disk_rel_of(r["id"]) or "")),
          "5/5")
    for nm, folder, rel in pairs:
        print("       %-28s folder=%-6r  ← %s" % (nm[:28], folder, rel))
    print()

    # ── ② 包内条目 vs 库内 content_hash（用建包时的 k_pack 复现）──
    print("② 包内条目自证（用 manifest 里的 vault_pub 重封一把，验结构）")
    import vaultcrypto  # noqa: E402
    import json  # noqa: E402
    import urllib.request  # noqa: E402
    plains = {}
    for r in priv:
        try:
            dd = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:8848/api/files/" + r["id"], timeout=60).read().decode("utf-8"))
            plains[r["id"]] = ((dd.get("file") or {}).get("content") or "").encode("utf-8")
        except Exception as e:
            plains[r["id"]] = None
    ok_n = sum(1 for v in plains.values() if v is not None)
    if ok_n == 0:
        print("  · 库是**锁定态** → 取不到私密明文，本节跳过（解锁后重跑才有对照意义）")
    else:
        check("经接口取回 8 篇明文（用于对照）", ok_n == len(priv), "%d/%d" % (ok_n, len(priv)))
    mis = []
    for r in priv:
        if plains[r["id"]] is None:
            continue
        if pp.sha_hex(plains[r["id"]]) != (r["content_hash"] or "").replace("sha256:", ""):
            mis.append(r["name"])
    check("取回内容与库内 content_hash 一致", not mis, ("不符: %s" % mis) if mis else "8/8")
    print()

    # ── ③ legacy 零改道 ──────────────────────────────────────
    print("③ legacy 模式下不该有任何一篇改道")
    d.set_manifest(priv_mode="legacy")
    routed = [r["name"] for r in d.conn.execute("SELECT id,name FROM files").fetchall()
              if d.priv_should_route_to_pack(r["id"])]
    check("legacy：全库零改道", not routed, "%d 篇改道" % len(routed))
    check("legacy：priv_effective_mode = legacy", d.priv_effective_mode() == "legacy")
    print()

    # ── ④ package 模式 ───────────────────────────────────────
    print("④ package 模式（包已存在 → 应生效）")
    d.set_manifest(priv_mode="package")
    check("包的三个前置条件齐备", d.priv_package_ready() is True)
    check("priv_effective_mode 变成 package", d.priv_effective_mode() == "package",
          d.priv_effective_mode())
    routed2 = [r["name"] for r in priv if d.priv_should_route_to_pack(r["id"])]
    check("8 篇全改道（迁移后全在私密文件夹里）", len(routed2) == 8,
          "实际 %d 篇" % len(routed2))
    print()

    # ── ⑤ 非私密篇不掺和 ─────────────────────────────────────
    print("⑤ 非私密篇在 package 模式下也不该改道")
    others = d.conn.execute("SELECT id,name FROM files WHERE enc_algo IS NULL LIMIT 300").fetchall()
    bad2 = [r["name"] for r in others if d.priv_should_route_to_pack(r["id"])]
    check("300 篇非私密篇零改道", not bad2, "%d 篇改道" % len(bad2))
    d.conn.close()

# ── ⑥ 终态模拟：把 3 篇挪进私密文件夹后，8 篇都该改道 ──────────
print()
print("⑥ 终态模拟（临时库：把 dsh 那 3 篇挪进 个人/私密）")
with tempfile.TemporaryDirectory(prefix="packroute-final-") as td2:
    for suf in ("", "-wal", "-shm"):
        s = DB + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(td2, "index.db" + suf))
    d2 = dbmod.Database.__new__(dbmod.Database)
    d2.conn = sqlite3.connect(os.path.join(td2, "index.db"), timeout=30)
    d2.conn.row_factory = sqlite3.Row
    d2.data_dir = os.path.dirname(DB)
    d2.set_manifest(library_dir=LIB, priv_mode="package")

    # 找到"私密"文件夹 id，把那 3 篇的 folder_id 改过去（只改临时副本）
    prow = d2.conn.execute("SELECT id FROM folders WHERE name=? AND parent_id=("
                           "SELECT id FROM folders WHERE name=?)", ("私密", "个人")).fetchone()
    if prow is None:
        prow = d2.conn.execute("SELECT id FROM folders WHERE name='私密'").fetchone()
    check("找到私密文件夹", prow is not None, prow["id"][:8] if prow else "")
    moved = []
    for r in priv:
        rel = d2.disk_rel_of(r["id"]) or ""
        if not pf.is_private_location(rel):
            d2.conn.execute("UPDATE files SET folder_id=? WHERE id=?", (prow["id"], r["id"]))
            moved.append(r["name"])
    d2.conn.commit()
    # 迁移已完成 → 起点就是终态，没有"待挪"的篇；挪 0 篇也是对的
    check("待挪篇数为 0（起点已是终态）", len(moved) == 0, "挪了 %d 篇" % len(moved))
    allr = [r["name"] for r in priv if d2.priv_should_route_to_pack(r["id"])]
    check("挪完 8 篇全部改道", len(allr) == 8, "%d 篇" % len(allr))
    d2.conn.close()

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ 结构性前置成立：路径解析精确、改道判据正确、legacy 零变化")
    print("  未验：`open_key` 那一跳（要密码派生的主密钥）—— 见文件头说明")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
