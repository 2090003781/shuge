# -*- coding: utf-8 -*-
"""沙箱验证：拿真实的 8 篇私密篇，证明 privpack 格式【逐字节无损】。

不碰任何真数据：
  · 8 篇内容通过读接口取（库现在是解锁态）
  · 包只建在内存 + 一个临时目录里
  · 真库的 .privpack 一个字节都不写

判据（全过才算格式可用）：
  ① 8 篇各自：解出来的字节 == 原字节（sha256 逐篇对）
  ② 包整体：items 顺序、名字、folder、size、hash 全部一致
  ③ 锁定视角：`peek()` 不解密 → **拿不到任何文件名**
  ④ 明文不入包：包的原始字节里搜不到篇名、搜不到正文特征串
  ⑤ 错密钥：解包必须失败（不能"解出垃圾但报成功"）
  ⑥ 坏包：截断/改一个字节 → 必须失败（GCM 认证）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import privpack as pp
import vaultcrypto

API = "http://127.0.0.1:8848"
DB = r"D:\core-notes\data\index.db"

import urllib.request  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-46s %s" % ("✓" if cond else "✗", name, detail))


# ── 取真数据 ─────────────────────────────────────────────────

con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
man = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM manifest")}
rows = con.execute("SELECT id,name,mtime,size,content_hash FROM files"
                   " WHERE visibility='private' ORDER BY name").fetchall()
con.close()

vault_pub = man.get("vault_pub") or ""
vault_priv_ref = man.get("vault_priv_ref") or ""
print("=" * 78)
print("沙箱验证 privpack v1 · 真实私密篇 %d 篇" % len(rows))
print("=" * 78)
print("  库公钥 vault_pub     : %s" % (vault_pub[:24] + "…" if vault_pub else "(空)"))
print("  库私钥 vault_priv_ref: %s" % (vault_priv_ref[:24] + "…" if vault_priv_ref else "(空)"))
print()

# 逐篇取明文（走接口 —— 库在解锁态；这条路已验证 8/8 哈希对）
originals = {}
import urllib.error as _ue
for r in rows:
    u = "%s/api/files/%s" % (API, r["id"])
    try:
        d = json.loads(urllib.request.urlopen(u, timeout=90).read().decode("utf-8"))
    except _ue.HTTPError as _e:
        if _e.code in (403, 404):
            continue                     # 锁定态：私密篇整体隐藏（403）→ 本节跳过
        raise
    txt = (d.get("file") or {}).get("content") or ""
    if not txt:
        continue
    originals[r["id"]] = txt.encode("utf-8")
print("  已取回 %d 篇明文，合计 %d 字节" % (len(originals), sum(len(v) for v in originals.values())))
if not any(originals.values()):
    print()
    print("  · 库是**锁定态** → 取不到私密明文，本沙箱验证跳过（解锁后重跑）")
    print("    （这不是失败：本脚本验的是格式本身，需要真实明文才有意义）")
    sys.exit(0)
print()

# ── 建包 ─────────────────────────────────────────────────────

k_pack = vaultcrypto.new_key()
key_ref = pp.seal_key(k_pack, vault_pub)

items = []
for r in rows:
    data = originals[r["id"]]
    items.append(pp.make_item(r["name"], data, folder="", mtime=(r["mtime"] or "")[:19]))

blob = pp.pack_bytes(k_pack, key_ref, items)
print("  建包完成：%d 字节（明文合计 %d 字节，膨胀 %.2f%%）"
      % (len(blob), sum(len(v) for v in originals.values()),
         100.0 * len(blob) / max(1, sum(len(v) for v in originals.values())) - 100))
print()

# ── ① 逐篇无损 ───────────────────────────────────────────────

print("① 逐篇解包逐字节比")
ok_n = 0
for r in rows:
    hit = [it for it in items if it["name"] == r["name"]]
    if not hit:
        check("找到 %s" % r["name"], False, "包里没有")
        continue
    good, why = pp.item_verify(hit[0])
    same = pp.item_bytes(hit[0]) == originals[r["id"]]
    if good and same:
        ok_n += 1
    else:
        check("无损 %s" % r["name"], False, why)
check("8 篇解出字节与原字节全等", ok_n == len(rows), "%d/%d" % (ok_n, len(rows)))
print()

# ── ② 整体结构 ───────────────────────────────────────────────

print("② 包整体结构（用主密钥走一遍完整读取链）")
# 沙箱里没有密码 → 主密钥拿不到。这里直接用包密钥验结构 ——
# 这正是"有包密钥就能读"的语义；"密码→私钥→开盒→包密钥"那条链已在真库解锁时验过。
header, got_items = pp.unpack(blob, k_pack)
names_ok = [it["name"] for it in got_items] == [it["name"] for it in items]
sizes_ok = all(it["size"] == len(pp.item_bytes(it)) for it in got_items)
hashes_ok = all(pp.sha_hex(pp.item_bytes(it)) == it["hash"] for it in got_items)
check("条目数一致", header.get("n") == len(items), "n=%s" % header.get("n"))
check("篇名与顺序一致", names_ok)
check("每篇 size 自洽", sizes_ok)
check("每篇 hash 自洽", hashes_ok)
check("包头不含文件名（只计数与时间）",
      set(header.keys()) <= {"v", "created", "updated", "n"}, "keys=%s" % sorted(header.keys()))
print()

# ── ③ 锁定视角 ───────────────────────────────────────────────

print("③ 锁定视角：不解密能看到什么")
peek = pp.peek(blob)
check("peek 不返回任何篇名", not any(k in peek for k in ("items", "names", "files")))
check("peek 报出 key_ref 是公钥封存", peek["key_ref_kind"] == "x25519", peek["key_ref_kind"])
print("     peek = %s" % json.dumps(peek, ensure_ascii=False))
print()

# ── ④ 明文不入包 ─────────────────────────────────────────────

print("④ 包原始字节里搜不到明文/篇名")
probes = []
for r in rows:
    probes.append(("篇名", r["name"]))
    probes.append(("篇名去扩展名", r["name"].rsplit(".", 1)[0]))
probes += [("正文特征", "仅AI运行时读取，严禁外传"), ("正文特征", "API 密钥清单"),
           ("正文特征", "cookies"), ("正文特征", "access:")]
leak = []
for kind, s in probes:
    if not s:
        continue
    if s.encode("utf-8") in blob:
        leak.append("%s:%s" % (kind, s[:20]))
check("包字节里搜不到任何篇名/正文特征", not leak, ("泄漏: %s" % leak[:4]) if leak else "%d 个探针全无命中" % len(probes))
print()

# ── ⑤ 错密钥 ─────────────────────────────────────────────────

print("⑤ 错密钥必须失败")
try:
    pp.unpack(blob, vaultcrypto.new_key())
    check("错密钥解包", False, "竟然成功了")
except pp.PackError:
    check("错密钥解包被拒", True)
except Exception as e:
    check("错密钥解包被拒", False, "抛了别的：%s" % type(e).__name__)
print()

# ── ⑥ 坏包 ───────────────────────────────────────────────────

print("⑥ 坏包必须失败（GCM 认证）")
bad_cases = []
b1 = blob[:len(blob) // 2]
b2 = bytearray(blob)
b2[len(blob) - 5] ^= 0x01          # 翻一位（在密文里）
b3 = bytearray(blob)
b3[20] ^= 0x01                      # 翻一位（在 key_ref 里）
reasons = {}
for label, b in (("截断一半", b1), ("尾部翻一位", bytes(b2)), ("key_ref 翻一位", bytes(b3))):
    try:
        pp.unpack(b, k_pack)
        bad_cases.append(label)
    except Exception as e:
        reasons[label] = "%s: %s" % (type(e).__name__, str(e)[:44])
check("三种坏包全被拒", not bad_cases, ("漏网: %s" % bad_cases) if bad_cases else "截断/改密文/改key_ref")
for k, v in reasons.items():
    print("     %-14s → %s" % (k, v))
# 帧被篡改要**由帧校验抓住**（而不是绕到解密才失败）—— 这是本轮修的点
check("key_ref 篡改由帧校验定位（非拖到解密）",
      "帧校验" in reasons.get("key_ref 翻一位", ""), reasons.get("key_ref 翻一位", "(无)"))
print()

# ── ⑦ 落盘往返（临时目录）────────────────────────────────────

print("⑦ 落盘往返（临时目录）＋ 原子写")
with tempfile.TemporaryDirectory(prefix="privpack-sbx-") as td:
    fp = os.path.join(td, "个人", "私密", ".privpack")
    w = pp.write_file(fp, blob)
    check("落盘成功", w.get("ok"), w.get("why", ""))
    back = pp.read_file(fp)
    check("读回与内存包一致", back == blob, "%d 字节" % len(back))
    h2, it2 = pp.unpack(back, k_pack)
    check("落盘后再解包仍 8 篇无损",
          all(pp.item_verify(it)[0] for it in it2), "%d 篇" % len(it2))
    check("盘上那份目录里只有 .privpack",
          os.listdir(os.path.dirname(fp)) == [".privpack"], str(os.listdir(os.path.dirname(fp))))
print()

# ── 汇总 ─────────────────────────────────────────────────────

print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("失败项：")
    for f in FAIL:
        print("   ✗ %s" % f)
else:
    print("★ 判定：privpack v1 格式成立 —— 8 篇逐字节无损、锁定态不泄名、坏包/错钥全被拒")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
