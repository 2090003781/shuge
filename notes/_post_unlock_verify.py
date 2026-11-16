# -*- coding: utf-8 -*-
"""解锁后第一件事：触发自愈 + 验包内 8 篇无损。

为什么现在能做：刚解锁（unlocked=true），`get_content` 拿得到主密钥，
所以读那 3 篇"库里在私密文件夹、但包里还没有"的篇会走自愈分支。

判据（每一步都不许跳）：
  ① 读前：包 46205 字节（5 篇）
  ② 逐篇读 8 篇（走接口 —— 与"打开一篇"同一条路）
  ③ 读后：包**变大**了（3 篇被自愈补进去）
  ④ 每篇读回的 sha256 == 库内 content_hash
  ⑤ 8 篇读回的**字节数** == 库内 size
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

API = "http://127.0.0.1:8848"
DB = r"D:\core-notes\data\index.db"
PACK = r"D:\枢阁\库\个人\私密\.privpack"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-48s %s" % ("✓" if cond else "✗", name, detail))


def get(p, t=120):
    with urllib.request.urlopen(API + p, timeout=t) as r:
        return json.loads(r.read().decode("utf-8"))


print("=" * 78)
print("解锁后：触发自愈 + 验包内 8 篇")
print("=" * 78)

size_before = os.path.getsize(PACK)
check("① 读前包大小（应为 46205 字节 = 5 篇）", size_before == 46205, "%d 字节" % size_before)
print()

con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
con.row_factory = sqlite3.Row
rows = list(con.execute("SELECT id,name,size,content_hash FROM files"
                        " WHERE visibility='private' ORDER BY name"))
con.close()

print("② 逐篇读（经接口，与打开一篇同一条路）")
bad_hash, bad_len, err = [], [], []
for r in rows:
    try:
        d = get("/api/files/" + r["id"])
        txt = ((d.get("file") or {}).get("content") or "")
        b = txt.encode("utf-8")
        got = hashlib.sha256(b).hexdigest()
        want = (r["content_hash"] or "").replace("sha256:", "")
        if got != want:
            bad_hash.append(r["name"])
        if len(b) != (r["size"] or 0):
            bad_len.append("%s(%d vs %s)" % (r["name"], len(b), r["size"]))
        print("     %-30s %6d 字节  哈希%s  长度%s"
              % (r["name"][:30], len(b), "✓" if got == want else "✗",
                 "✓" if len(b) == (r["size"] or 0) else "✗"))
    except urllib.error.HTTPError as e:
        err.append("%s(HTTP %d)" % (r["name"], e.code))
        print("     %-30s HTTP %d" % (r["name"][:30], e.code))
    except Exception as e:
        err.append("%s(%s)" % (r["name"], type(e).__name__))
        print("     %-30s %s" % (r["name"][:30], type(e).__name__))

check("② 8 篇都读得到（无错）", not err, ("错误: %s" % err) if err else "8/8")
check("④ 每篇哈希 == 库内 content_hash", not bad_hash,
      ("不符: %s" % bad_hash) if bad_hash else "8/8")
check("⑤ 每篇长度 == 库内 size", not bad_len,
      ("不符: %s" % bad_len) if bad_len else "8/8")
print()

size_after = os.path.getsize(PACK)
check("③ 读后包变大了（自愈生效）", size_after > size_before,
      "%d → %d 字节（+%d）" % (size_before, size_after, size_after - size_before))
print()

print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ 包内已有全部 8 篇、逐篇哈希对齐 —— 「删掉那 8 个独立密文」的前置条件成立")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
