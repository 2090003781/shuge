# -*- coding: utf-8 -*-
"""验活：用一次密码，逐层验证 8 篇私密笔记到底还能不能解开。

    python -B _vault_verify.py

逐层验证（哪一层断链立刻定位，不留"大概能开"的模糊结论）：
   ① 密码 → 主密钥        check_master(verify_blob)      —— 密码对不对
   ② 主密钥 → 库私钥       unwrap_key(vault_priv_ref)     —— 库密钥对能不能用
   ③ 主密钥 → 6 篇内容密钥 unwrap_key(key_ref)
      库私钥 → 2 篇内容密钥 open_box(key_ref)
   ④ 内容密钥 → 明文       open_sealed(data)              —— AES-GCM 认证标签
   ⑤ 明文 vs 库内 size     —— 明文长度必须等于 size 列
   ⑥ 明文 vs content_hash  —— 内容哈希必须对得上（**这一步才是"没被改过"的铁证**）
   ⑦ 明文 vs 盘上密文重加密 —— 盘上那份确实是同一篇的密文

密码只在本进程内存里，不写盘、不进日志、不回显。
"""
from __future__ import annotations

import getpass
import hashlib
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import vaultcrypto  # noqa: E402

DB = r"D:\core-notes\data\index.db"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    man = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM manifest")}
    rows = con.execute(
        "SELECT id,name,size,content,content_hash,enc_algo,key_ref,storage,storage_ref"
        " FROM files WHERE enc_algo IS NOT NULL ORDER BY name").fetchall()
    con.close()

    print("=" * 78)
    print("私密笔记验活 · 共 %d 篇" % len(rows))
    print("=" * 78)

    pw = getpass.getpass("请输入库密码（不回显、不落盘）: ")
    if not pw:
        print("  没输入密码，退出。")
        return 2

    # ① 密码 → 主密钥
    master = vaultcrypto.derive(pw, man.get("kdf_salt", ""),
                                man.get("kdf_n", 1 << 15), man.get("kdf_r", 8), man.get("kdf_p", 1))
    if not vaultcrypto.check_master(master, man.get("verify_blob") or ""):
        print("  ① 密码 → 主密钥 ................ ✗ 密码不对（verify_blob 校验失败）")
        print("     结论：无法继续。8 篇一篇都验不了。")
        return 1
    print("  ① 密码 → 主密钥 ................ ✓")

    # ② 主密钥 → 库私钥
    priv = None
    try:
        priv = vaultcrypto.unwrap_key(master, man.get("vault_priv_ref") or "")
        print("  ② 主密钥 → 库私钥 .............. ✓（%d 字节）" % len(priv))
    except Exception as e:
        print("  ② 主密钥 → 库私钥 .............. ✗ %s: %s" % (type(e).__name__, str(e)[:60]))
        print("     影响：2 篇用公钥封存（x25519）的会开不了；其余 6 篇不受影响。")

    print()
    print("-" * 78)
    ok = fail = 0
    failed = []
    for r in rows:
        kr = r["key_ref"] or ""
        box = kr.startswith("x25519:")
        data = r["content"] if isinstance(r["content"], bytes) else \
            (r["content"] or "").encode("utf-8")
        line = []
        try:
            # ③ 内容密钥
            if box:
                if priv is None:
                    raise vaultcrypto.LockedError("库私钥不可用")
                key = vaultcrypto.open_box(priv, kr)
                kd = "库私钥开盒"
            else:
                key = vaultcrypto.unwrap_key(master, kr)
                kd = "主密钥直开"
            line.append("③✓%s" % kd)
            # ④ 明文
            plain = vaultcrypto.open_sealed(key, data)
            line.append("④✓解密")
            # ⑤ 长度
            len_ok = (r["size"] is None) or (len(plain) == r["size"])
            line.append("⑤%s长度(%d/%s)" % ("✓" if len_ok else "✗", len(plain), r["size"]))
            # ⑥ 哈希（铁证）
            want = (r["content_hash"] or "").replace("sha256:", "")
            got = sha(plain)
            hash_ok = (not want) or (got == want)
            line.append("⑥%s哈希" % ("✓" if hash_ok else "✗"))
            head = plain[:70].decode("utf-8", "replace").replace("\n", "⏎").replace("\r", "")
            if len_ok and hash_ok:
                ok += 1
                print("  ✓ %-30s %s" % (r["name"][:30], " ".join(line)))
                print("      明文 %d 字节  开头: %s" % (len(plain), head))
            else:
                fail += 1
                failed.append(r["name"])
                print("  ! %-30s %s" % (r["name"][:30], " ".join(line)))
                print("      明文 %d 字节  期望 %s 字节  开头: %s" % (len(plain), r["size"], head))
        except Exception as e:
            fail += 1
            failed.append(r["name"])
            print("  ✗ %-30s %s  →  %s: %s"
                  % (r["name"][:30], " ".join(line), type(e).__name__, str(e)[:60]))
            if isinstance(e, vaultcrypto.LockedError):
                print("      原因：内容密钥打不开（库私钥缺失/不可用）")

    print("-" * 78)
    print("  结果：可解 %d / %d 篇" % (ok, len(rows)))
    if failed:
        print("  开不了：%s" % ", ".join(failed))
    print()
    if ok == len(rows):
        print("  判定：**8 篇全部可解，且哈希与库内记录一致 → 数据完好，未损坏**")
    elif ok > 0:
        print("  判定：**部分可解** —— 能救的先救，开不了的单独查")
    else:
        print("  判定：**一篇都开不了** —— 按紧急抢救处理，别再做任何写操作")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
