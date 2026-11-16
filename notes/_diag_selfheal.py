# -*- coding: utf-8 -*-
"""自愈到底有没有发生？—— 包 mtime + 单篇复读 + 包内容实查。

三个独立判据：
  A. 包的 mtime：如果自愈写过，mtime 会是刚才
  B. 读一篇 DSH 人设 → mtime 变不变、大小变不变
  C. 直接查包：用**库自己**（能拿到 master）列出包内条目名
     —— 通过内存里那份 master 做不到（master 不在我这），
        所以改用一个可判读的间接判据：**把库里那篇的 content 清空**，
        再读 —— 如果读得到，说明内容确实在包里（而不是库内解出来的）。
        但那样会动真库，**不做**。
        改用：读包文件头解析出条目数？（不在头部）→ 用 file_id 间接判：
        临时把该篇的 disk_rel 指向别处？也不动真库。

结论：A + B 已经足够判读；C 不做（会动真库）。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

API = "http://127.0.0.1:8848"
PACK = r"D:\枢阁\库\个人\私密\.privpack"


def get(p, t=120):
    with urllib.request.urlopen(API + p, timeout=t) as r:
        return json.loads(r.read().decode("utf-8"))


def pack_state():
    st = os.stat(PACK)
    return st.st_size, st.st_mtime


def fmt(t):
    return time.strftime("%H:%M:%S", time.localtime(t))


print("=" * 78)
print("自愈有没有发生")
print("=" * 78)
sz0, mt0 = pack_state()
print("  A) 包现状：%d 字节  mtime=%s" % (sz0, fmt(mt0)))
print()

# 找一篇"库里在私密文件夹、但包里可能还没有"的（那 3 篇 DSH）
import sqlite3
con = sqlite3.connect("file:D:/core-notes/data/index.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
row = con.execute("SELECT id,name FROM files WHERE name LIKE 'DSH-人设.md%' LIMIT 1").fetchone()
con.close()
print("  B) 读一篇 DSH 人设：%s（id %s）" % (row["name"], row["id"][:8]))
before = pack_state()
d = get("/api/files/" + row["id"])
txt = ((d.get("file") or {}).get("content") or "")
time.sleep(1.5)
after = pack_state()
print("     读回 %d 字符" % len(txt))
print("     包：%d → %d 字节（%+d）  mtime %s → %s"
      % (before[0], after[0], after[0] - before[0], fmt(before[1]), fmt(after[1])))
print()

print("  C) 再读一次同一篇（幂等性）：")
before2 = pack_state()
d2 = get("/api/files/" + row["id"])
time.sleep(1.5)
after2 = pack_state()
print("     包：%d → %d 字节（%+d）  mtime %s → %s"
      % (before2[0], after2[0], after2[0] - before2[0], fmt(before2[1]), fmt(after2[1])))
print()

print("=" * 78)
if after[0] > before[0]:
    print("★ 自愈**发生了**（读的时候包长大了）")
elif after[1] != before[1]:
    print("★ 包**被重写过**（mtime 变了）但大小没变 —— 可能是同内容覆盖写")
else:
    print("✗ 包**完全没被动过** —— 自愈没触发，或触发了但写失败（静默）")
    print("   那读回的内容是从库内那份密文解出来的（即走了 legacy 兜底读取）")
print("=" * 78)
