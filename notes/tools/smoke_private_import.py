"""私密进库自测：私密文件**照进库**，但内容加密、锁定态读不到也搜不到。

  python tools/smoke_private_import.py

辞玖 2026-09-22："私密进库，但是设置权限" —— 这个脚本验的就是"权限"那部分：
  ① 私密导入 → visibility=private、内容用库主密钥封起来（**密文落盘，明文不进库**）
  ② 拿主密钥读得出来；没有主密钥 → LockedError
  ③ 没主密钥还想导私密 → 直接 LockedError（服务端据此回 403，绝不降级成明文）
  ④ 覆盖更新：磁盘改了内容，私密的还是私密、还是密文、内容真的换了
  ⑤ 派生层：私密进检索库后块标 is_sensitive；锁定态（purge）块被清干净，解锁前搜不到
全程用临时库 + 临时目录，跑完删干净。
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, "src")
from db import Database                       # noqa: E402
from importer import import_path              # noqa: E402
from indexer import Indexer                   # noqa: E402
import vaultcrypto                            # noqa: E402

OK = True


def _raises(fn):
    """跑一下就抛：期望 LockedError（库没解锁）"""
    try:
        fn()
        return False
    except vaultcrypto.LockedError:
        return True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


src = tempfile.mkdtemp(prefix="ai-notes-priv-src-")
data = tempfile.mkdtemp(prefix="ai-notes-priv-db-")
SECRET = "密钥 sk-PRIVATE-9f2c8a1b 这句话不该出现在明文里"
secret_md = os.path.join(src, "密钥清单.md")
plain_md = os.path.join(src, "普通.md")

try:
    db = Database(data)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive("test-pw-1234", salt, vaultcrypto.KDF_N, vaultcrypto.KDF_R,
                                vaultcrypto.KDF_P)
    db.set_vault_password(master, salt)

    print("① 私密导入（带主密钥）")
    with open(secret_md, "w", encoding="utf-8") as f:
        f.write("# 私密\n\n" + SECRET + "\n")
    r1 = import_path(db, secret_md, under="vault/个人/私密", visibility="private", master=master)
    check("imported=1", r1["imported"] == 1, r1)
    row = db.conn.execute("SELECT * FROM files WHERE name='密钥清单.md'").fetchone()
    fid = row["id"]
    check("visibility=private", row["visibility"] == "private", row["visibility"])
    check("内容已加密（enc_algo=%s）" % vaultcrypto.ALGO, row["enc_algo"] == vaultcrypto.ALGO,
          row["enc_algo"])
    check("有 key_ref（内容密钥被主密钥包起来）", bool(row["key_ref"]), bool(row["key_ref"]))
    check("落库的是密文：明文不在 content 里", SECRET.encode("utf-8") not in (row["content"] or b""),
          len(row["content"] or b""))
    check("size/hash 记的是明文口径", row["size"] == os.path.getsize(secret_md),
          (row["size"], os.path.getsize(secret_md)))

    print("② 有主密钥读得出，没主密钥读不出")
    check("带主密钥 → 原文一字不差",
          db.get_content(fid, master).decode("utf-8").find(SECRET) >= 0)
    try:
        db.get_content(fid)
        check("不带主密钥 → 应当 LockedError", False, "居然读出来了")
    except vaultcrypto.LockedError:
        check("不带主密钥 → LockedError", True)

    print("③ 没主密钥还想导私密 → 拒绝（不许降级成明文）")
    other = os.path.join(src, "另一份密钥.md")
    with open(other, "w", encoding="utf-8") as f:
        f.write("另一份密钥 sk-2\n")
    try:
        import_path(db, other, under="vault/个人/私密", visibility="private")
        check("应当 LockedError", False, "居然导进去了")
    except vaultcrypto.LockedError:
        check("LockedError，且没有留下明文行",
              db.conn.execute("SELECT COUNT(*) c FROM files WHERE name='另一份密钥.md'")
              .fetchone()["c"] == 0)

    print("④ 普通文件不受影响（internal 明文照旧）")
    with open(plain_md, "w", encoding="utf-8") as f:
        f.write("# 普通\n\n普通正文\n")
    r4 = import_path(db, plain_md, under="vault/笔记")
    prow = db.conn.execute("SELECT * FROM files WHERE name='普通.md'").fetchone()
    check("imported=1 且 visibility=internal",
          r4["imported"] == 1 and prow["visibility"] == "internal", prow["visibility"])
    check("普通文件没有加密", prow["enc_algo"] in (None, ""), prow["enc_algo"])

    print("⑤ 覆盖更新：私密还是私密、还是密文、内容真换了")
    with open(secret_md, "w", encoding="utf-8") as f:
        f.write("# 私密\n\n" + SECRET + " + 第二版新增 token sk-NEW-777\n")
    r5 = import_path(db, secret_md, under="vault/个人/私密", visibility="private", master=master,
                     overwrite=True)
    row5 = db.conn.execute("SELECT * FROM files WHERE name='密钥清单.md'").fetchone()
    check("updated=1 且 file_id 不变", r5["updated"] == 1 and row5["id"] == fid, r5.get("updated"))
    check("还是 private + 密文", row5["visibility"] == "private"
          and row5["enc_algo"] == vaultcrypto.ALGO, (row5["visibility"], row5["enc_algo"]))
    check("新内容读得到（解锁态）", "sk-NEW-777" in db.get_content(fid, master).decode("utf-8"))
    check("新明文同样不在库里", "sk-NEW-777".encode("utf-8") not in (row5["content"] or b""))

    print("⑥ 库里早就有这一篇（明文 internal）+ 内容没变 → 必须补权限，而不是被去重跳过")
    legacy = os.path.join(src, "老副本.md")
    body = "# 老副本\n\n早就导进来的那份密钥 sk-LEGACY-42\n"
    with open(legacy, "wb") as f:                    # 二进制写：免得 Windows 把 \n 变成 \r\n
        f.write(body.encode("utf-8"))
    import_path(db, legacy, under="vault/个人/私密")            # 先当普通文件导进来
    lrow = db.conn.execute("SELECT * FROM files WHERE name='老副本.md'").fetchone()
    check("先导成 internal 明文（模拟历史遗留）",
          lrow["visibility"] == "internal" and not lrow["enc_algo"], lrow["visibility"])
    n_before = db.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
    r6 = import_path(db, legacy, under="vault/个人/私密", visibility="private", master=master,
                     overwrite=True)
    lrow2 = db.conn.execute("SELECT * FROM files WHERE name='老副本.md'").fetchone()
    check("secured=1", r6["secured"] == 1, r6.get("secured"))
    check("没有新增一行", db.conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"] == n_before)
    check("file_id 不变且已加密", lrow2["id"] == lrow["id"] and lrow2["enc_algo"] == vaultcrypto.ALGO,
          (lrow2["id"] == lrow["id"], lrow2["enc_algo"]))
    check("visibility=private", lrow2["visibility"] == "private", lrow2["visibility"])
    check("内容一字没动（还是原文）", db.get_content(lrow2["id"], master).decode("utf-8") == body)
    check("secured_ids 报出来给路由重建派生层", r6["secured_ids"] == [lrow["id"]], r6["secured_ids"])
    # 真正要拒的是"内容变了要写加密行、却没有主密钥"；内容没变时本来就该无事发生（skipped）
    time.sleep(0.02)
    with open(legacy, "wb") as f:
        f.write((body + "又改了一行 sk-LEGACY-99\n").encode("utf-8"))
    check("没主密钥时改写密文 → LockedError",
          _raises(lambda: import_path(db, legacy, under="vault/个人/私密", visibility="private",
                                      overwrite=True)))
    check("失败后库里那份没被写坏",
          "sk-LEGACY-99" not in db.get_content(lrow2["id"], master).decode("utf-8"))

    with open(legacy, "wb") as f:                    # 把磁盘这份写回原文（上一段改过它）
        f.write(body.encode("utf-8"))
    r6b = import_path(db, legacy, under="vault/个人/私密", visibility="private", master=master,
                      overwrite=True)
    check("已私密 + 内容没变 → skipped 且 private_ready=1",
          r6b["skipped"] == 1 and r6b["private_ready"] == 1 and r6b["secured"] == 0,
          {k: r6b[k] for k in ("skipped", "private_ready", "secured")})

    print("⑦ 派生层：私密进检索 → 块标敏感；上锁（purge）→ 块清干净")
    db.set_rag(fid, True)
    ix = Indexer(db)
    ix.master = lambda: master                    # 解锁态
    res = ix.reindex_file(fid)
    n_sens = db.conn.execute(
        "SELECT COUNT(*) c FROM chunks WHERE file_id=? AND is_sensitive=1", (fid,)).fetchone()["c"]
    check("切了块（%s 块）" % res.get("chunks"), (res.get("chunks") or 0) > 0, res)
    check("块全部标成 is_sensitive", n_sens == (res.get("chunks") or 0), (n_sens, res.get("chunks")))
    hit_unlocked = db.search_chunks("sk-PRIVATE", limit=5, include_sensitive=True)
    hit_locked = db.search_chunks("sk-PRIVATE", limit=5, include_sensitive=False)
    check("解锁态搜得到", any(h.get("file_id") == fid for h in hit_unlocked), len(hit_unlocked))
    check("锁定态搜不到", not any(h.get("file_id") == fid for h in hit_locked), len(hit_locked))
    purged = db.purge_sensitive_chunks()
    left = db.conn.execute("SELECT COUNT(*) c FROM chunks WHERE file_id=?", (fid,)).fetchone()["c"]
    check("purge 之后块为 0（%d 篇被清）" % purged, left == 0, left)
    ix.master = lambda: None                       # 锁定态重建 → 也不许留块
    ix.reindex_file(fid)
    left2 = db.conn.execute("SELECT COUNT(*) c FROM chunks WHERE file_id=?", (fid,)).fetchone()["c"]
    check("锁定态重建不产生块", left2 == 0, left2)
    check("但正文还在（只是读需要主密钥）", db.get_content(fid, master).decode("utf-8").find(SECRET) >= 0)

    db.close()
finally:
    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(data, ignore_errors=True)
    print("⑧ 临时库与临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
