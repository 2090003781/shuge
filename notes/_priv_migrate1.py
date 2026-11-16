# -*- coding: utf-8 -*-
"""迁移第 1 步：建真包到暂存区，并逐项验证。**真数据一个字节都不动。**

    python -B _priv_migrate1.py

为什么先落暂存区（`data\\tmp\\privpack-staging`）而不是直接进库树：
  真库树是 `materialize --verify` 的对账对象 —— 突然多一个 `.privpack`，
  在"把包当合法例外"那条改完之前会被报成漂移。所以先在这儿把包验死，再搬进去。

验证链（每一环都要过）：
  ① 建包      —— 8 篇明文（经读接口取，库在解锁态） → .privpack
  ② 落盘读回  —— 盘上那份与内存包逐字节相同
  ③ 解包无损  —— 8 条逐篇：解出字节 == 取回的明文（sha256 对）
  ④ 对照库    —— 解出内容的 sha256 == 库里 content_hash（**这是"与库一致"的铁证**）
  ⑤ 锁定视角  —— peek() 拿不到任何篇名
  ⑥ 不泄明文  —— 包原始字节里搜不到篇名/正文特征
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import priv_folder as pf
import privpack as pp
import vaultcrypto

API = "http://127.0.0.1:8848"
DB = r"D:\core-notes\data\index.db"
STAGE = r"D:\core-notes\data\tmp\privpack-staging"
# 落点交给 priv_folder 算 —— 它按"库根 + 个人/私密 + .privpack"拼，
# 所以暂存目录要**镜像真库布局**（staging 本身当库根）。自己拼过一版，拼错了一次。
PACK = pf.pack_abs(STAGE)

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-44s %s" % ("✓" if cond else "✗", name, detail))


def main() -> int:
    os.makedirs(STAGE, exist_ok=True)

    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    man = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM manifest")}
    rows = con.execute("SELECT id,name,mtime,size,content_hash FROM files"
                       " WHERE visibility='private' ORDER BY name").fetchall()
    con.close()

    vault_pub = man.get("vault_pub") or ""
    print("=" * 78)
    print("迁移① 建真包（暂存区）· %d 篇私密篇" % len(rows))
    print("=" * 78)
    print("  暂存路径：%s" % PACK)
    print("  真库树  ：D:\\枢阁\\库\\个人\\私密\\  ← **本次不动**")
    print()

    if not vault_pub:
        print("  ✗ 库里没有 vault_pub，建不了包（先设一次库密码）")
        return 1
    if len(rows) != 8:
        print("  ! 注意：私密篇不是 8 篇，而是 %d 篇 —— 按实际数量继续" % len(rows))

    # ── ① 取明文 + 建包 ──────────────────────────────────────
    print("① 取明文（经读接口）并建包")
    items, plains, bad_read = [], {}, []
    for r in rows:
        try:
            d = json.loads(urllib.request.urlopen(
                "%s/api/files/%s" % (API, r["id"]), timeout=90).read().decode("utf-8"))
            txt = (d.get("file") or {}).get("content") or ""
            b = txt.encode("utf-8")
            plains[r["id"]] = b
            items.append(pp.make_item(r["name"], b, folder="", mtime=(r["mtime"] or "")[:19]))
        except Exception as e:
            bad_read.append("%s(%s)" % (r["name"], str(e)[:30]))
    check("8 篇明文全部取到", not bad_read, ("失败: %s" % bad_read) if bad_read else "%d 篇" % len(items))
    if bad_read:
        return 1

    k_pack = vaultcrypto.new_key()
    key_ref = pp.seal_key(k_pack, vault_pub)
    blob = pp.pack_bytes(k_pack, key_ref, items)
    total = sum(len(v) for v in plains.values())
    check("建包成功", len(blob) > 0,
          "%d 字节（明文 %d，膨胀 %.2f%%）" % (len(blob), total, 100.0 * len(blob) / total - 100))
    print()

    # ── ② 落盘 + 读回 ────────────────────────────────────────
    print("② 落盘并读回")
    w = pp.write_file(PACK, blob)
    check("原子落盘成功", w.get("ok"), w.get("why", "") or w.get("path", ""))
    if not w.get("ok"):
        return 1
    back = pp.read_file(PACK)
    check("盘上那份与内存包逐字节相同", back == blob, "%d 字节" % len(back))
    check("包所在目录里只有 .privpack",
      os.listdir(os.path.dirname(PACK)) == [pp.DEFAULT_NAME],
      str(os.listdir(os.path.dirname(PACK))))
    print()

    # ── ③ 解包无损 ───────────────────────────────────────────
    print("③ 解包（用包密钥）逐篇比")
    header, got = pp.unpack(back, k_pack)
    check("条目数一致", header.get("n") == len(items), "n=%s" % header.get("n"))
    bad_bytes = []
    for it in got:
        ok, why = pp.item_verify(it)
        src = next((r for r in rows if r["name"] == it["name"]), None)
        if not ok:
            bad_bytes.append("%s(%s)" % (it["name"], why))
        elif src is None or pp.item_bytes(it) != plains[src["id"]]:
            bad_bytes.append("%s(与取回的明文不等)" % it["name"])
    check("8 篇解出字节 == 取回的明文", not bad_bytes, ("不符: %s" % bad_bytes) if bad_bytes else "8/8")
    print()

    # ── ④ 对照库内 content_hash（铁证）───────────────────────
    print("④ 对照库里 content_hash")
    mismatch = []
    for it in got:
        src = next((r for r in rows if r["name"] == it["name"]), None)
        if src is None:
            mismatch.append("%s(库里找不到)" % it["name"])
            continue
        want = (src["content_hash"] or "").replace("sha256:", "")
        got_h = pp.sha_hex(pp.item_bytes(it))
        if want and got_h != want:
            mismatch.append("%s(哈希不符)" % it["name"])
        if src["size"] != it["size"]:
            mismatch.append("%s(长度 %s vs %s)" % (it["name"], src["size"], it["size"]))
    check("包内 8 条与库内 size/hash 全一致", not mismatch,
          ("不一致: %s" % mismatch) if mismatch else "8/8 逐条对齐")
    print()

    # ── ⑤ 锁定视角 ───────────────────────────────────────────
    print("⑤ 锁定视角（不解密能看到什么）")
    peek = pp.peek(back)
    check("peek 不回篇名", not any(k in peek for k in ("items", "names", "files")))
    check("peek 报公钥封存", peek["key_ref_kind"] == "x25519", peek["key_ref_kind"])
    print("     %s" % json.dumps(peek, ensure_ascii=False))
    print()

    # ── ⑥ 不泄明文 ───────────────────────────────────────────
    print("⑥ 包原始字节里搜明文")
    probes = []
    for r in rows:
        probes += [r["name"], r["name"].rsplit(".", 1)[0]]
    probes += ["仅AI运行时读取，严禁外传", "API 密钥清单", "cookies", "access:", "APP_SECRET"]
    leak = [s for s in probes if s and s.encode("utf-8") in back]
    check("搜不到任何篇名/正文特征", not leak,
          ("泄漏: %s" % leak[:4]) if leak else "%d 个探针 0 命中" % len(probes))
    print()

    # ── 记一份元数据（给下一步用）────────────────────────────
    meta = {"built_at": header.get("updated"), "n": len(items), "pack_bytes": len(back),
            "plain_bytes": total, "items": [{"name": it["name"], "size": it["size"],
                                             "hash": it["hash"], "mtime": it["mtime"]} for it in got]}
    with open(os.path.join(STAGE, "pack-meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    print("  元数据已存：%s" % os.path.join(STAGE, "pack-meta.json"))
    print()
    print("=" * 78)
    print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("   ✗ %s" % f)
        print("  → 不通过。真数据仍未动，可直接重跑。")
        return 1
    print("★ 第 1 步成立：真包已建在暂存区，8 篇与库内哈希逐条对齐")
    print("  真库树 D:\\枢阁\\库\\个人\\私密\\ 本次**未改动**")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
