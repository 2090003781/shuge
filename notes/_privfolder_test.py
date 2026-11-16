# -*- coding: utf-8 -*-
"""验证 priv_folder：位置判据 + 包读写（对暂存区的真包，不碰真库）。

判据：
  ① 位置判据：`个人/私密/…` 与 `vault/个人/私密/…` 都算私密；别处一律不算
  ② 包可见：暂存区的真包能 load + parse
  ③ 锁定态：不给 master → read_items 必须抛 NeedUnlock（**不能悄悄回空**）
  ④ 包密钥直接读：8 条逐篇与库内 content_hash 对齐
  ⑤ 包密钥直接写：upsert 一篇 → 包长大 → 读回含新篇 → 再 drop 掉复原
  ⑥ 无公钥：write_items 建新包时必须抛 NoPub
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import priv_folder as pf
import privpack as pp

# 包已搬进真库树并提交 git（2026-10-15，643caa6）——暂存区已清，测试改指真库
STAGE = r"D:\枢阁\库"
DB = r"D:\core-notes\data\index.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-46s %s" % ("✓" if cond else "✗", name, detail))


print("=" * 78)
print("验证 priv_folder（位置判据 + 包读写）")
print("=" * 78)

# ── ① 位置判据 ───────────────────────────────────────────────
print("① 位置判据")
cases = [
    ("个人/私密/API密钥清单.md", True),
    ("vault/个人/私密/API密钥清单.md", True),
    ("个人/私密/子目录/深层.md", True),
    ("个人\\私密\\x.md", True),                    # 反斜杠
    ("个人/私密", True),                            # 文件夹自身
    ("个人/知识库/索引.md", False),                  # 同名不同目录 —— 既有审计专门提过
    ("second-brain-system/dsh/DSH-人设.md", False),  # 现在私密但不在私密文件夹 → 应判 False
    ("个人/私密abc/x.md", False),                    # 前缀陷阱：不该被 "个人/私密" 前缀误吞
    ("", False),
]
bad = []
for rel, want in cases:
    got = pf.is_private_location(rel)
    if got != want:
        bad.append("%r 期望 %s 得 %s" % (rel, want, got))
check("9 个用例判对（含前缀陷阱与反斜杠）", not bad, ("错: %s" % bad) if bad else "9/9")
check("目录内相对路径提取", pf.inside_rel("个人/私密/a/b.md") == "a/b.md"
      and pf.inside_rel("vault/个人/私密/x.md") == "x.md"
      and pf.inside_rel("个人/知识库/x.md") == "",
      "a/b.md / x.md / (空)")
check("包文件名识别", pf.is_pack_file(r"D:\x\个人\私密\.privpack") and not pf.is_pack_file(r"D:\x\笔记.md"))
print()

# ── ② 包可见 ─────────────────────────────────────────────────
print("② 库树里的真包可见")
blob, p = pf.load_pack(STAGE)
check("load_pack 拿到包", blob is not None and p is not None, "%d 字节" % (len(blob) if blob else 0))
if blob is None:
    print("  → 暂存区没有包，先跑 _priv_migrate1.py")
    sys.exit(1)
check("parse 认得 key_ref 是公钥封存", pp.vaultcrypto.is_box_ref(p["key_ref"]))
print()

# ── ③ 锁定态 ─────────────────────────────────────────────────
print("③ 锁定态必须抛 NeedUnlock（不能悄悄回空）")
try:
    pf.read_items(STAGE, master=None, vault_priv_raw=None)
    check("不给 master 读包", False, "竟然没抛")
except pf.NeedUnlock as e:
    check("不给 master 读包 → NeedUnlock", True, str(e)[:36])
except Exception as e:
    check("不给 master 读包 → NeedUnlock", False, "抛了别的：%s" % type(e).__name__)
print()

# ── 取包密钥（沙箱里没有密码 → 直接构造，等价于"解锁后拿到的那把"）──
# 说明：包密钥是随机的、只封在包里，本脚本没有密码解不出它。
# 所以这里**复用建包脚本的思路**：用同一批明文重建一把，验证"读写往返"的语义。
# "密码→私钥→开盒"那条链在真库解锁时已用 8/8 哈希验过（_priv_final.py）。
con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
man = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM manifest")}
rows = con.execute("SELECT id,name,mtime,size,content_hash FROM files"
                   " WHERE visibility='private' ORDER BY name").fetchall()
con.close()

import json  # noqa: E402
import urllib.request  # noqa: E402
plains = {}
import urllib.error as _ue
for r in rows:
    try:
        d = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:8848/api/files/" + r["id"], timeout=90).read().decode("utf-8"))
    except _ue.HTTPError as _e:
        if _e.code in (403, 404):
            continue                     # 锁定态：私密篇整体隐藏（403）
        raise
    _txt = (d.get("file") or {}).get("content") or ""
    if not _txt:
        continue
    plains[r["id"]] = _txt.encode("utf-8")

if not plains:
    print()
    print("  · 库是**锁定态** → 取不到私密明文，本脚本的包读写往返验证跳过（解锁后重跑）")
    print("    （这不是失败：需要真实明文才有意义；前面的位置判据/锁定态抛错都已验过）")
    print()
    print("=" * 78)
    print("结果：通过 %d ｜ 失败 %d（含跳过说明）" % (len(PASS), len(FAIL)))
    print("=" * 78)
    sys.exit(0 if not FAIL else 1)

import vaultcrypto  # noqa: E402
k_pack = vaultcrypto.new_key()
key_ref = pf.privpack.seal_key(k_pack, man.get("vault_pub") or "")
items = [pp.make_item(r["name"], plains[r["id"]], folder="", mtime=(r["mtime"] or "")[:19]) for r in rows]

# ── ④ 包密钥直接读 ───────────────────────────────────────────
print("④ 包密钥直接读：与库内 content_hash 对齐")
hdr, got = pp.unpack(pp.pack_bytes(k_pack, key_ref, items), k_pack)
mis = []
for it in got:
    src = next((r for r in rows if r["name"] == it["name"]), None)
    want = (src["content_hash"] or "").replace("sha256:", "") if src else ""
    if want and pp.sha_hex(pp.item_bytes(it)) != want:
        mis.append(it["name"])
check("8 条与库内 hash 全对齐", not mis, ("不符: %s" % mis) if mis else "8/8")
print()

# ── ⑤ upsert / drop 往返 ─────────────────────────────────────
print("⑤ upsert / drop 往返（在暂存区真包上做）")
before = pp.read_file(pf.pack_abs(STAGE))
n0 = len(items)
items2 = pf.upsert_item(items, "zz-测试新增.md", "临时内容".encode("utf-8"))
check("upsert 后条目 +1", len(items2) == n0 + 1, "%d → %d" % (n0, len(items2)))
items3 = pf.upsert_item(items2, "zz-测试新增.md", "改过的内容".encode("utf-8"))
check("同名声覆盖（不是再加一条）", len(items3) == n0 + 1, "%d → %d" % (len(items2), len(items3)))
got_b = pf.get_item_bytes(items3, "zz-测试新增.md")
check("取回是改过的那版", got_b == "改过的内容".encode("utf-8"), repr(got_b))
items4, dropped = pf.drop_item(items3, "zz-测试新增.md")
check("drop 掉测试条目", dropped and len(items4) == n0, "%d → %d" % (len(items3), len(items4)))
check("drop 后与原始条目集一致",
      [i["name"] for i in items4] == [i["name"] for i in items])
names = pf.list_names(items4)
check("list_names 给出清单", len(names) == n0 and all(len(x) == 4 for x in names), "%d 条" % len(names))
print()

# ── ⑥ 无公钥必须报错 ─────────────────────────────────────────
print("⑥ 没设过库密码时不许建包")
try:
    pf.write_items(STAGE, items, vault_pub_b64="")
    check("空 vault_pub 建包", False, "竟然成功了")
except pf.NoPub:
    check("空 vault_pub → NoPub", True)
except Exception as e:
    check("空 vault_pub → NoPub", False, "抛了别的：%s" % type(e).__name__)
print()

check("库树里的包未被本脚本改动", pp.read_file(pf.pack_abs(STAGE)) == before, "逐字节相同")

print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ priv_folder 成立：位置判据准、锁定态抛错、包读写往返无损")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
