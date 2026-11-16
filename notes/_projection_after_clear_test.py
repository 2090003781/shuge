# -*- coding: utf-8 -*-
"""补验：库内那份密文清空后，落盘投影还工不工作。

`tree_write._sealed_bytes()` 原本靠 `content` 列取密文去落盘。
package 模式下如果库内那份被清了（C 项选了"清"），`_sealed_bytes()` 会返回空
→ `write_through` 退回"要 master 解密再写"那条路 —— 能不能走通？
走不通的话，"清库内那份"就会把落盘投影一起弄坏。

判据：
  ① 清空前：落盘投影 ok（写的是密文）
  ② **清空后：落盘投影仍 ok**
  ③ 清空后：盘上那份是密文（不是明文！）
  ④ 清空后：盘上那份能解开、内容与原字节相同
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import priv_folder as pf
import privpack as pp
import tree_write as tw
import vaultcrypto

PASS, FAIL = [], []
PW = "测试密码-投影"


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def looks_plaintext(b: bytes) -> bool:
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if "\x00" in t:
        return False
    ctrl = sum(1 for ch in t if ord(ch) < 32 and ch not in "\r\n\t")
    return (ctrl / max(1, len(t))) < 0.01


print("=" * 78)
print("验：库内密文清空后，落盘投影还工作吗")
print("=" * 78)
tmp = tempfile.mkdtemp(prefix="proj-")
try:
    data_dir = os.path.join(tmp, "data")
    lib_dir = os.path.join(tmp, "lib")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(lib_dir, "个人", "私密"), exist_ok=True)
    d = dbmod.Database(data_dir)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib_dir)
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib_dir), pp.pack_bytes(k0, pp.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)

    body = "投影用的私密正文。\n".encode("utf-8")
    fid = d.create_file(pfid, "投影.md", body, visibility="private", master=master)
    rel = tw.rel_of(d, fid)
    dst = os.path.join(lib_dir, rel.replace("/", os.sep))

    # ⚠️ create_file **不做落盘投影**（`_project_to_disk` 只在 put_content 里），
    #    所以这里要显式投一次，才能验"清空前盘上是什么"。
    w0 = tw.write_through(d, fid, master)
    L = d.conn.execute("SELECT length(content) L FROM files WHERE id=?", (fid,)).fetchone()["L"]
    print("  库内那份密文 %s 字节；落盘投影 %s" % (L, w0.get("ok")))
    check("① 清空前：盘上是密文（不是明文）",
          os.path.isfile(dst) and not looks_plaintext(open(dst, "rb").read()),
          "%d 字节，像明文=%s" % (os.path.getsize(dst) if os.path.isfile(dst) else -1,
                                 looks_plaintext(open(dst, "rb").read()) if os.path.isfile(dst) else "-"))
    print()

    # 清掉库内那份密文
    d.conn.execute("UPDATE files SET content=NULL, storage_ref=NULL WHERE id=?", (fid,))
    d.conn.commit()
    print("  已清掉库内那份密文")
    print()

    # ② 落盘投影还能不能做
    r = tw.write_through(d, fid, master)
    check("② **清空后落盘投影仍 ok**", r.get("ok") is True, str(r)[:70])
    disk = open(dst, "rb").read() if os.path.isfile(dst) else b""
    check("③ 盘上那份仍是**密文**（没被写成明文）", bool(disk) and not looks_plaintext(disk),
          "%d 字节，像明文=%s" % (len(disk), looks_plaintext(disk) if disk else "-"))
    # ④ 盘上那份能解开
    ok4 = False
    try:
        row = d.conn.execute("SELECT key_ref FROM files WHERE id=?", (fid,)).fetchone()
        key = vaultcrypto.open_key_ref(master, row["key_ref"], d.vault_priv(master))
        ok4 = (vaultcrypto.open_sealed(key, disk) == body)
    except Exception as e:
        print("     解开失败：%s: %s" % (type(e).__name__, str(e)[:50]))
    check("④ 盘上那份解得开、与原字节相同", ok4)
    # 读路径仍好
    check("⑤ 清空后读路径仍好", d.get_content(fid, master) == body)
    # ⑥ 顺手把库内那份补回了密文（省得每次现封）
    L2 = d.conn.execute("SELECT length(content) L FROM files WHERE id=?", (fid,)).fetchone()["L"]
    check("⑥ 库内那份被补回密文", (L2 or 0) > 0, "%s 字节" % L2)
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
    print("★ 清掉库内那份密文之后，落盘投影照常、盘上仍是密文 —— C 项选「清」这条路可行")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
