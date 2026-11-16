# -*- coding: utf-8 -*-
"""端到端 · 第二批入口：`set_visibility` / `append_content` / `rename_file`
在 package 模式下对**包里那篇**做了什么。

为什么专挑这三个：它们都要动私密篇的正文或路径，而包里那份是**读**的真相源 ——
谁没跟上，谁就会造成"包与库分叉"或"正文丢失"。

全程真代码 + 自设密码，零替身。
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
PW = "测试密码-e2e-2"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def new_lib(td, name="库"):
    """造一个 package 模式的临时库，返回 (db, master, lib_dir)"""
    data_dir = os.path.join(td, name + "-data")
    lib_dir = os.path.join(td, name)
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
    return d, master, lib_dir


def pack_names(d, master, lib_dir):
    items = pf.read_items(lib_dir, master, d.vault_priv(master))
    return sorted(x["name"] for x in items)


print("=" * 78)
print("端到端 · 第二批入口（set_visibility / append_content / rename_file）")
print("=" * 78)
td = tempfile.mkdtemp(prefix="e2e-b-")
try:
    # ── 准备：一篇在包里的私密篇 ──────────────────────────────
    d, master, lib_dir = new_lib(td)
    root = d.root_folder_id()
    body = "第一批正文内容\n".encode("utf-8")
    fid = d.create_file(root, "甲.md", body, visibility="private", master=master)
    d.move_in(fid, master=master)
    check("准备：已在包里的篇", pack_names(d, master, lib_dir) == ["甲.md"],
          str(pack_names(d, master, lib_dir)))
    check("准备：读得到", d.get_content(fid, master) == body)
    print()

    # ── ① set_visibility → internal（移出私密的正路）──────────
    print("① set_visibility(file,'internal') —— 移出私密")
    r = d.set_visibility(fid, "internal", master)
    after = pack_names(d, master, lib_dir)
    vis = d.file_by_id(fid)["visibility"]
    enc = d.file_by_id(fid)["enc_algo"]
    print("     返回：%s" % {k: r.get(k) for k in ("visibility", "decrypted", "bytes")})
    check("visibility 变成 internal", vis == "internal", vis)
    check("不再加密（enc_algo 清空）", not enc, repr(enc))
    check("**包里那条也被删了**（否则留个幽灵）", after == [], str(after))
    print()

    # ── ② append_content（追加）──────────────────────────────
    print("② append_content —— 追加一段")
    d2, m2, lib2 = new_lib(td, "库2")
    root2 = d2.root_folder_id()
    b2 = "开头一段\n".encode("utf-8")
    f2 = d2.create_file(root2, "乙.md", b2, visibility="private", master=m2)
    d2.move_in(f2, master=m2)
    before_n = len(d2.get_content(f2, m2))
    n = d2.append_content(f2, "追加的一段\n".encode("utf-8"), m2)
    got = d2.get_content(f2, m2)
    exp = b2 + "追加的一段\n".encode("utf-8")
    check("返回追加前的字节数", n == before_n, "%d" % n)
    check("**读回含追加内容**", got == exp, "%d 字节" % len(got))
    check("**包里那份也跟着更新了**",
          pf.get_item_bytes(pf.read_items(lib2, m2, d2.vault_priv(m2)), "乙.md") == exp,
          "包内 %d 字节" % len(pf.get_item_bytes(
              pf.read_items(lib2, m2, d2.vault_priv(m2)), "乙.md") or b""))
    print()

    # ── ③ rename_file（改名）─────────────────────────────────
    print("③ rename_file —— 改名")
    d3, m3, lib3 = new_lib(td, "库3")
    root3 = d3.root_folder_id()
    b3 = "要改名的正文\n".encode("utf-8")
    f3 = d3.create_file(root3, "旧名.md", b3, visibility="private", master=m3)
    d3.move_in(f3, master=m3)
    names_before = pack_names(d3, m3, lib3)
    try:
        d3.rename_file(f3, "新名.md", m3)
        renamed = True
        err = ""
    except Exception as e:
        renamed = False
        err = "%s: %s" % (type(e).__name__, str(e)[:50])
    names_after = pack_names(d3, m3, lib3)
    check("改名没抛异常", renamed, err)
    if renamed:
        check("库内名字变了", d3.file_by_id(f3)["name"] == "新名.md",
              d3.file_by_id(f3)["name"])
        print("     包内名字：%s → %s" % (names_before, names_after))
        check("**包内名字也跟上了**（否则读不到）", names_after == ["新名.md"], str(names_after))
    print()
    d.close(); d2.close(); d3.close()
finally:
    import time
    time.sleep(0.4)
    shutil.rmtree(td, ignore_errors=True)

print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
print("=" * 78)
sys.exit(0 if not FAIL else 1)
