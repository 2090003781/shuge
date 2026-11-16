# -*- coding: utf-8 -*-
"""验 db.tree(keep_priv_dir=...)：默认行为不变，开关开了才保留「私密」节点。

判据：
  ① 默认（keep_priv_dir=False）→ 与改动前逐字一样：锁定时「私密」节点**不在**
  ② keep_priv_dir=True → 锁定时「私密」节点**在**，且它 files=[] 、children=[]（内容全不可见）
  ③ keep_priv_dir=True 时，节点带了 keep 标记（可判读）
  ④ 解锁态两种参数都照常显示内容（开关只对锁定态有意义）
  ⑤ 其它"只装私密的空枝"照旧收掉（开关不能把不该露的也露出来）
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

import db as dbmod

DB = r"D:\core-notes\data\index.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


def find(node, pred, out=None, path=""):
    if out is None:
        out = []
    nm = node.get("name") or ""
    p = (path + "/" + nm) if nm else path
    if pred(node, p):
        out.append((node, p))
    for c in node.get("children") or []:
        find(c, pred, out, p)
    return out


def is_priv_folder(n, p):
    return (n.get("name") or "") == "私密"


def main() -> int:
    print("=" * 78)
    print("验 db.tree(keep_priv_dir=...)")
    print("=" * 78)
    td = tempfile.mkdtemp(prefix="tree-keep-")
    try:
        for suf in ("", "-wal", "-shm"):
            s = DB + suf
            if os.path.exists(s):
                shutil.copy2(s, os.path.join(td, "index.db" + suf))
        d = dbmod.Database.__new__(dbmod.Database)
        d.conn = sqlite3.connect(os.path.join(td, "index.db"), timeout=30)
        d.conn.row_factory = sqlite3.Row
        d.data_dir = os.path.dirname(DB)

        # ① 默认 = 老行为
        print("① 默认（keep_priv_dir=False）= 老行为不变")
        t_old = d.tree(hide_private=True)
        hits_old = find(t_old, is_priv_folder)
        check("锁定时「私密」节点不在（与改动前一致）", not hits_old,
              "找到 %d 个" % len(hits_old))
        check("hidden_private 仍是 8", t_old.get("hidden_private") == 8,
              t_old.get("hidden_private"))
        print()

        # ② 开开关
        print("② keep_priv_dir=True：锁定时保留节点、内容不可见")
        t_new = d.tree(hide_private=True, keep_priv_dir=True)
        hits_new = find(t_new, is_priv_folder)
        check("「私密」节点在", len(hits_new) == 1, "找到 %d 个" % len(hits_new))
        if hits_new:
            n, p = hits_new[0]
            check("路径是 个人/私密", p.endswith("/个人/私密"), p)
            check("节点里 files 为空（内容不可见）", (n.get("files") or []) == [],
                  "%d 个文件" % len(n.get("files") or []))
            check("节点里 children 为空", (n.get("children") or []) == [],
                  "%d 个子目录" % len(n.get("children") or []))
            check("带 keep 标记（可判读）", n.get("keep") is True)
        check("hidden_private 仍是 8", t_new.get("hidden_private") == 8,
              t_new.get("hidden_private"))
        print()

        # ④ 解锁态：两种参数都照常
        print("④ 解锁态：开关不影响（仍显示 5 个文件）")
        for flag in (False, True):
            t = d.tree(hide_private=False, keep_priv_dir=flag)
            hits = find(t, is_priv_folder)
            nf = len(hits[0][0].get("files") or []) if hits else -1
            check("keep_priv_dir=%-5s 解锁态仍见 8 个文件" % flag, nf == 8,
                  "文件 %d" % nf)
        print()

        # ⑤ 树的其它部分没被影响
        print("⑤ 其它空枝照旧收掉（开关不能放宽整个裁剪）")
        def count_nodes(t):
            return len(find(t, lambda n, p: True))
        a = count_nodes(d.tree(hide_private=True))
        b = count_nodes(d.tree(hide_private=True, keep_priv_dir=True))
        check("开开关只多出 1 个节点（就是私密文件夹）", b == a + 1,
              "%d → %d" % (a, b))
        d.conn.close()
    finally:
        time.sleep(0.4)
        shutil.rmtree(td, ignore_errors=True)

    print()
    print("=" * 78)
    print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("   ✗ %s" % f)
    if not FAIL:
        print("★ 成立：默认零变化；开了开关才保留「私密」节点且内容不可见")
    print("=" * 78)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
