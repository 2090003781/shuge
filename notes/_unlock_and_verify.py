# -*- coding: utf-8 -*-
"""解锁库 + 立刻验 8 篇私密笔记（一步到位）。

    python -B _unlock_and_verify.py

走的是**库自己的进程内**解锁语义（与 POST /api/lock/unlock 同一条路）：
  ① 密码 → 主密钥（scrypt，约 0.1 秒），校验 verify_blob
  ② 主密钥 → 库私钥（2 篇 x25519 封存的靠它）
  ③ 逐篇解出明文，核对长度(size) 与哈希(content_hash)
  ④ 解锁后**顺带重索引私密派生层**（与接口行为一致，检索才认得这几篇）

密码：本进程内存里用完即弃，不写盘、不进日志、不回显。
"""
from __future__ import annotations

import getpass
import hashlib
import os
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
        "SELECT id,name,size,content,content_hash,key_ref FROM files"
        " WHERE enc_algo IS NOT NULL ORDER BY name").fetchall()
    con.close()
    if not rows:
        print("  库里没有加密篇，不用解锁。")
        return 0

    print("=" * 78)
    print("解锁库 + 验证 %d 篇私密笔记" % len(rows))
    print("=" * 78)

    # 密码来源：优先环境变量 SHUGE_PW（方便一行命令跑），否则交互提示。
    # 环境变量只在本次进程可见，跑完由调用方清掉。
    pw = os.environ.get("SHUGE_PW") or ""
    if pw:
        print("  （密码取自环境变量 SHUGE_PW）")
    else:
        pw = getpass.getpass("请输入库密码: ")
    if not pw:
        print("  没输入，退出。")
        return 2

    # ① 密码 → 主密钥
    master = vaultcrypto.derive(pw, man.get("kdf_salt", ""), man.get("kdf_n", 1 << 15),
                                man.get("kdf_r", 8), man.get("kdf_p", 1))
    if not vaultcrypto.check_master(master, man.get("verify_blob") or ""):
        print("  ① ✗ 密码不对")
        return 1
    print("  ① 密码 → 主密钥 .............. ✓")

    # ② 主密钥 → 库私钥
    priv = None
    try:
        priv = vaultcrypto.unwrap_key(master, man.get("vault_priv_ref") or "")
        print("  ② 主密钥 → 库私钥 ............ ✓（%d 字节）" % len(priv))
    except Exception as e:
        print("  ② 主密钥 → 库私钥 ............ ✗ %s" % type(e).__name__)

    print()
    print("-" * 78)
    ok, bad = 0, []
    plains = {}
    for r in rows:
        kr = r["key_ref"] or ""
        data = r["content"] if isinstance(r["content"], bytes) else (r["content"] or "").encode("utf-8")
        try:
            key = vaultcrypto.open_box(priv, kr) if kr.startswith("x25519:") \
                else vaultcrypto.unwrap_key(master, kr)
            plain = vaultcrypto.open_sealed(key, data)
            want = (r["content_hash"] or "").replace("sha256:", "")
            h_ok = (not want) or sha(plain) == want
            l_ok = (r["size"] is None) or len(plain) == r["size"]
            if h_ok and l_ok:
                ok += 1
                plains[r["id"]] = plain
                head = plain[:60].decode("utf-8", "replace").replace("\n", "⏎").replace("\r", "")
                print("  ✓ %-28s %6d 字节  哈希✓  %s" % (r["name"][:28], len(plain), head))
            else:
                bad.append(r["name"])
                print("  ! %-28s %6d 字节  哈希%s 长度%s"
                      % (r["name"][:28], len(plain), "✓" if h_ok else "✗", "✓" if l_ok else "✗"))
        except Exception as e:
            bad.append(r["name"])
            print("  ✗ %-28s 开不了：%s: %s" % (r["name"][:28], type(e).__name__, str(e)[:50]))

    print("-" * 78)
    print("  可解 %d / %d 篇" % (ok, len(rows)))
    if bad:
        print("  开不了：%s" % ", ".join(bad))
    print()

    if ok != len(rows):
        print("  判定：**不是全绿** —— 先把能救的救出来，别急着重索引。")
        return 1
    print("  判定：**8 篇全部可解，哈希与库内记录一致 → 数据完好、未被篡改**")

    # ③ 解锁并重索引（让检索认得这几篇）；这一步要库进程配合，失败不算数据问题
    print()
    print("  ③ 解锁库并重索引私密派生层 ……")
    try:

        import json
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:8848/api/lock/unlock",
                                     data=json.dumps({"password": pw}).encode("utf-8"),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.loads(r.read().decode("utf-8"))
        print("     库已解锁：unlocked=%s  重索引 %s 篇  库密钥对就绪=%s"
              % (res.get("unlocked"), res.get("reindexed"), res.get("pub_ready")))
    except Exception as e:
        print("     ✗ 接口解锁失败（数据本身没问题，只是库没进解锁态）：%s" % str(e)[:80])

    print()
    print("  下一步：私密篇现在能读了。明文要不要留底、"
          "盘上那份怎么处理，等你定架构。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
