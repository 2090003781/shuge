# -*- coding: utf-8 -*-
"""钉死一条判断：那两个测试失败 = "修复未加载"，不是新回归。

做法：**用新代码在进程内复刻那条测试的判据**，看它通过不通过。
  · 若通过 → 说明失败确实是"运行中的库是旧代码"造成的
  · 若仍失败 → 说明我的修复不够，得继续查

复刻 acc_write_through ⑧b：
  ① 建一篇 → 盘上有、内容对
  ② 把盘上写成"坏的"（模拟写入被占用）
  ③ 调 `tree_sync_after_import` 那条路（= tree_sync）→ 期望盘上被补回来
"""
from __future__ import annotations

import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod
import server
import tree_write as tw

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


d = dbmod.Database(r"D:\core-notes\data")
lib = d.manifest().get("library_dir")


class S:
    pass


server.STATE = S()
server.STATE.db = d
server.STATE.master = lambda: None          # 非私密篇不需要主密钥

body = "# 补投影\n\n正文一二三。\n"
fid = d.create_file(d.root_folder_id(), "zz-钉死-补投影.md", body.encode("utf-8"),
                    visibility="internal")
rel = tw.rel_of(d, fid)
dst = os.path.join(lib, rel.replace("/", os.sep))

print("=" * 78)
print("钉死：补投影那条用新代码能不能过")
print("=" * 78)
check("① 建完盘上内容对", os.path.isfile(dst) and open(dst, encoding="utf-8").read() == body,
      repr(open(dst, encoding="utf-8").read()[:20]) if os.path.isfile(dst) else "(盘上没有)")

with open(dst, "w", encoding="utf-8") as f:
    f.write("坏的")
check("② 已经把盘上弄坏", open(dst, encoding="utf-8").read() == "坏的")

# ③ 走"补投影"那条路（与 tree_sync_after_import 里对 unchanged 篇做的一样）
r = server.tree_sync(fid, None)
after = open(dst, encoding="utf-8").read() if os.path.isfile(dst) else "(没了)"
check("③ 补投影返回 ok", (r or {}).get("ok") is True, str(r)[:60])
check("③ **盘上被补回来了**（这是这条测试要的）", after == body, repr(after[:30]))

# 收摊
try:
    os.remove(dst)
except OSError:
    pass
d.conn.execute("DELETE FROM files WHERE id=?", (fid,))
d.conn.commit()
d.close()

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
if not FAIL:
    print("★ 新代码能补投影 → 那两个失败确实是「运行中的库是旧代码」")
else:
    print("✗ 新代码也补不回来 → 我的修复不够，得继续查")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
