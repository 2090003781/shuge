# -*- coding: utf-8 -*-
"""验修好的 append_through：三种情形分别落到哪个 mode。

判据（都用**真库 + 真流程**，不用替身）：
  ① disk_projection=1（现网配置）：put_content 已经落盘 → `mode=already`，不重复写
  ② 盘上那份被删掉      → 整份写一次（mode=write），不在坏基础上接
  ③ 盘上是"追加前"的旧那份 → **真按字节追加**（mode=append）

③ 是这条通路存在的意义，必须能走到 —— 否则 append_through 就是死的。
做法：临时关掉 disk_projection，手动把盘上那份改回追加前的长度，再调。
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import tree_write as tw

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


d = dbmod.Database(r"D:\core-notes\data")
lib = d.manifest().get("library_dir")
made = []


def new_note(nm, body):
    fid = d.create_file(d.root_folder_id(), nm, body.encode("utf-8"), visibility="internal")
    made.append(fid)
    rel = tw.rel_of(d, fid)
    return fid, os.path.join(lib, rel.replace("/", os.sep))


print("=" * 78)
print("验 append_through 的三种 mode")
print("=" * 78)

# ① 现网配置：put_content 已落盘 → already
print("① disk_projection=1：put_content 已写盘 → 不该重复写")
fid, dst = new_note("zz-验append-A.md", "# A\n\n第一段。\n")
d.append_content(fid, "追加段。\n".encode("utf-8"), None)
r = tw.append_through(d, fid, "追加段。\n".encode("utf-8"), None)
check("mode = already（不重复写）", r.get("mode") == "already",
      str({k: r.get(k) for k in ("mode", "appended", "bytes")}))
check("盘上内容正确（完整）",
      open(dst, encoding="utf-8").read() == "# A\n\n第一段。\n追加段。\n",
      repr(open(dst, encoding="utf-8").read()[:30]))
print()

# ② 盘上被删 → 整份写一次
print("② 盘上那份被删 → 整份写一次")
fid2, dst2 = new_note("zz-验append-B.md", "# B\n\n第一段。\n")
d.append_content(fid2, "追加段。\n".encode("utf-8"), None)
os.remove(dst2)
r2 = tw.append_through(d, fid2, "追加段。\n".encode("utf-8"), None)
check("磁盘那份被补回来了", os.path.isfile(dst2))
check("mode = write（整份）", r2.get("mode") in (None, "write") or r2.get("bytes"),
      str({k: r2.get(k) for k in ("mode", "bytes", "why")}))
check("补回来的内容完整",
      open(dst2, encoding="utf-8").read() == "# B\n\n第一段。\n追加段。\n")
print()

# ③ 真按字节追加（关掉投影，手动把盘上改成"追加前"那份）
print("③ 关掉投影 + 盘上是追加前那份 → 真按字节追加")
d.set_manifest(disk_projection="0")
fid3, dst3 = new_note("zz-验append-C.md", "# C\n\n第一段。\n")
# 此刻 put_content 不落盘，手动造出"盘上是追加前的旧那份"
with open(dst3, "wb") as f:
    f.write("# C\n\n第一段。\n".encode("utf-8"))
before = os.path.getsize(dst3)
d.put_content(fid3, "# C\n\n第一段。\n追加段。\n".encode("utf-8"), None)   # 库里已新，盘上还是旧
r3 = tw.append_through(d, fid3, "追加段。\n".encode("utf-8"), None)
check("mode = append（真按字节追加）", r3.get("mode") == "append",
      str({k: r3.get(k) for k in ("mode", "appended", "bytes")}))
check("盘上长度 = 追加前 + 增量",
      os.path.getsize(dst3) == before + len("追加段。\n".encode("utf-8")),
      "%d → %d" % (before, os.path.getsize(dst3)))
check("盘上内容 = 完整新内容",
      open(dst3, encoding="utf-8").read() == "# C\n\n第一段。\n追加段。\n",
      repr(open(dst3, encoding="utf-8").read()[:30]))
d.set_manifest(disk_projection="1")
print()

# 收摊
for fid in made:
    rel = tw.rel_of(d, fid)
    p = os.path.join(lib, rel.replace("/", os.sep))
    try:
        os.remove(p)
    except OSError:
        pass
    d.conn.execute("DELETE FROM files WHERE id=?", (fid,))
d.conn.commit()
print("  已清理 %d 篇测试篇" % len(made))
d.close()

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ append_through 三条路都对：already / write / append")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
