# -*- coding: utf-8 -*-
"""端到端（修正版）：自设密码跑通整条链。

修正上一版三处**脚手架错误**（不是产品 bug）：
  ① 我用 `new_salt()` 派生 master，而 `set_vault_password` 内部**又生成一个 salt**
     → verify_blob 对不上。正确做法：**自己生成 salt 并传进去**，两处共用同一个。
  ② `priv_package_ready()` 要求包**已存在**，而本测试第 ② 步才建包
     → 先手动建一个空包，再验"模式生效"。
  ③ `create_file(visibility='private')` 已经加密了，所以 move_in 报"本来就是私密篇"是对的
     → 断言改成"移进私密文件夹 + 正文进包"。
"""
from __future__ import annotations

import importlib.util
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

spec = importlib.util.spec_from_file_location("priv_out", r"D:\core-notes\tools\priv_out.py")
po = importlib.util.module_from_spec(spec)
spec.loader.exec_module(po)

PASS, FAIL = [], []
PW = "测试用密码-2026"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


print("=" * 78)
print("端到端（修正版）：自设密码 → 建库 → 建包 → 拖进 → 上锁 → 解锁读 → 移出")
print("=" * 78)

td = tempfile.mkdtemp(prefix="e2e2-")
try:
    data_dir = os.path.join(td, "data")
    lib_dir = os.path.join(td, "库")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(lib_dir, "个人", "私密"), exist_ok=True)

    # ── ① 建库 + 设密码（**salt 自己生成并共用**）──────────────
    print("① 建库 + 设密码")
    d = dbmod.Database(data_dir)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)      # 测试用低 N，快
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)    # ← 同一套参数
    check("有 vault_pub", bool(d.vault_pub()), (d.vault_pub() or "")[:18] + "…")
    check("有 vault_priv_ref", bool(d.manifest().get("vault_priv_ref")))
    # 关键：用**存下来的**参数重新派生，必须能过 verify_blob
    man = d.manifest()
    m2 = vaultcrypto.derive(PW, man["kdf_salt"], int(man["kdf_n"]),
                            int(man["kdf_r"]), int(man["kdf_p"]))
    check("密码派生的主密钥能过 verify_blob",
          vaultcrypto.check_master(m2, man.get("verify_blob") or ""))
    check("两次派生的主密钥相同", m2 == master)
    print()

    # ── ② 先建一个空包，再开 package 模式 ────────────────────
    print("② 建空包 → 开 package 模式")
    d.set_manifest(library_dir=lib_dir)
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib_dir),
                  pp.pack_bytes(k0, pf.privpack.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")
    check("priv_package_ready True", d.priv_package_ready() is True)
    check("priv_effective_mode = package", d.priv_effective_mode() == "package",
          d.priv_effective_mode())
    print()

    # ── ③ 建一篇 → 拖进私密文件夹 ────────────────────────────
    print("③ 建一篇 → move_in")
    root = d.root_folder_id()
    body = "这是一篇要被拖进私密文件夹的测试正文。\n第二行。\n".encode("utf-8")
    fid = d.create_file(root, "端到端测试.md", body, visibility="private", master=master)
    check("建篇成功", bool(fid), fid[:8])
    check("建篇时不在私密文件夹", not pf.is_private_location(d.disk_rel_of(fid) or ""),
          d.disk_rel_of(fid) or "")

    r = d.move_in(fid, master=master)
    check("移进私密文件夹了", pf.is_private_location(d.disk_rel_of(fid) or ""),
          d.disk_rel_of(fid) or "")
    check("**正文写进了包**（to_pack=True）", r.get("to_pack") is True, r.get("note", "")[:48])
    items = pf.read_items(lib_dir, master, d.vault_priv(master))
    check("包里有这一篇", pf.get_item_bytes(items, "端到端测试.md") == body,
          "%d 条" % len(items))
    print()

    # ── ④ 锁上 → 读必须失败 ──────────────────────────────────
    print("④ 锁上（master=None）")
    try:
        d.get_content(fid, None)
        check("锁定态读私密篇抛 LockedError", False, "竟然读到了")
    except vaultcrypto.LockedError:
        check("锁定态读私密篇抛 LockedError", True)
    except Exception as e:
        check("锁定态读私密篇抛 LockedError", False, "抛了别的：%s" % type(e).__name__)
    print()

    # ── ⑤ 解锁 → 读回逐字节相同（**走包**）────────────────────
    print("⑤ 解锁 → 从包里读回")
    got = d.get_content(fid, m2)
    check("**读回正文与写入逐字节相同**", got == body, "%d 字节" % (len(got) if got else 0))
    check("路由确实走了包", d.priv_should_route_to_pack(fid) is True)
    priv = d.vault_priv(m2)
    check("库私钥解得开", bool(priv), "%d 字节" % (len(priv) if priv else 0))
    print()

    # ── ⑥ 盘上不泄明文 ───────────────────────────────────────
    print("⑥ 盘上不泄明文")
    pack = open(pf.pack_abs(lib_dir), "rb").read()
    check("包字节里搜不到正文特征",
          "要被拖进私密文件夹的测试正文".encode("utf-8") not in pack,
          "包 %d 字节" % len(pack))
    print()

    # ── ⑦ 移出私密 ───────────────────────────────────────────
    print("⑦ 移出私密（priv_out.move_out）")
    outdir = os.path.join(td, "移出落点")
    rr = po.move_out(lib_dir, master=m2, priv_raw=priv, name="端到端测试.md",
                     to_dir=outdir, folder="", keep=False, vault_pub=d.vault_pub())
    check("移出成功", rr.get("ok") is True, rr.get("why", ""))
    dst = rr.get("path") or ""
    check("落盘文件在", bool(dst) and os.path.isfile(dst), os.path.basename(dst))
    if dst and os.path.isfile(dst):
        check("落盘内容与原文逐字节相同", open(dst, "rb").read() == body)
    check("包里那条已删", rr.get("left") == 0, "剩 %d 条" % (rr.get("left") or 0))
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
if not FAIL:
    print("★ 端到端成立：**密码那一跳也是真跑的** —— 建库/建包/拖进/上锁/解锁/移出 全通")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
