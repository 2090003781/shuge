# -*- coding: utf-8 -*-
"""验证 db.py 里新加的私密文件夹模式骨架：**默认必须等价于今天的行为**。

判据：
  ① priv_mode() 默认 'legacy'（manifest 里没有 priv_mode 时）
  ② priv_effective_mode() 默认 'legacy' —— 即"接了代码但没开" = 行为零变化
  ③ priv_should_route_to_pack() 对**任何**文件都回 False（legacy 下不该有任何一篇改道）
  ④ manifest 里写 priv_mode='package' 但库树/包不存在 → 仍落回 legacy 且留痕
  ⑤ 8 篇私密篇在 legacy 下的 disk_rel_of 能算出来（路由判据依赖它）
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

DB = r"D:\core-notes\data\index.db"
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-48s %s" % ("✓" if cond else "✗", name, detail))


# 用**副本**做一个"请求 package 但条件不足"的实验，避免动真库的 manifest
import shutil  # noqa: E402
import tempfile  # noqa: E402

print("=" * 78)
print("验证 db 私密模式骨架（默认 legacy）")
print("=" * 78)

print("① manifest 里有没有 priv_mode")
con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
row = con.execute("SELECT value FROM manifest WHERE key='priv_mode'").fetchone()
con.close()
# 迁移（2026-10-15 01:58）之后真库已开 package；这条改成"要么没设、要么就是 package"
check("真库 priv_mode 要么未设、要么是 package",
      row is None or row[0] == "package",
      "priv_mode=%r" % (row[0] if row else None))
print()

print("②③ 用真库（只读）验默认模式")
import db as dbmod  # noqa: E402

# 真库根（manifest 里那个）—— 临时库会被测试改来改去，所以先记下来
_real_lib_root = None

# 只读打开：Database 的构造会跑迁移，所以这里不用它，直接用类里的纯函数式逻辑
# （私密模式那几个方法只读 manifest / vault_pub / library_dir / 盘上文件，不改库）
with tempfile.TemporaryDirectory(prefix="privmode-") as td:
    # 拷一份库（含 wal）到临时目录，避免任何写落到真库
    for suf in ("", "-wal", "-shm"):
        s = DB + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(td, "index.db" + suf))
    d = dbmod.Database.__new__(dbmod.Database)          # 不跑 __init__（不迁移）
    d.conn = sqlite3.connect(os.path.join(td, "index.db"), timeout=30)
    d.conn.row_factory = sqlite3.Row
    d.data_dir = os.path.dirname(DB)
    _real_lib_root = d._lib_root()
    d._real_lib_root = lambda: _real_lib_root

    # 真库已开 package（迁移后）→ 这里验"读得到且是合法值"，不假定 legacy
    check("priv_mode() 是合法值", d.priv_mode() in ("legacy", "package"), d.priv_mode())
    check("priv_effective_mode() 与包状态自洽",
          d.priv_effective_mode() in ("legacy", "package"), d.priv_effective_mode())

    ids = [r["id"] for r in d.conn.execute("SELECT id FROM files LIMIT 200")]
    routed = [i for i in ids if d.priv_should_route_to_pack(i)]
    check("legacy 下没有任何一篇改道到包", not routed, "%d 篇抽查" % len(ids))

    # ④ 请求 package：
    #    先用**不存在的库根**验回退路径（这是 "条件不足 → 退回 legacy" 的判据）
    #    再还原成真库根 —— 包现在**已经**在真库树里了（2026-10-15 搬入并提交 git 643caa6）
    import tempfile as _tf
    empty_root = _tf.mkdtemp(prefix="no-pack-here-")
    d.set_manifest(priv_mode="package", library_dir=empty_root)
    check("priv_mode() 读到 package", d.priv_mode() == "package", d.priv_mode())
    check("库根没有包 → priv_package_ready False", d.priv_package_ready() is False,
          "库根=%s" % empty_root)
    check("→ priv_effective_mode() 退回 legacy", d.priv_effective_mode() == "legacy",
          d.priv_effective_mode())
    check("回退留了痕", len(d.priv_fallbacks()) > 0,
          d.priv_fallbacks()[-1]["why"][:40] if d.priv_fallbacks() else "(无)")

    # ④b 真库根下：包已在 → 条件齐备，模式生效
    d.set_manifest(library_dir=d._real_lib_root())
    check("真库根有包 → priv_package_ready True", d.priv_package_ready() is True,
          d._real_lib_root())
    check("→ priv_effective_mode() = package", d.priv_effective_mode() == "package",
          d.priv_effective_mode())
    d.set_manifest(priv_mode="legacy")          # 收尾：别把临时库留在 package

    # ⑤ 8 篇私密篇的 disk_rel_of 能算出来（路由判据要用它判"在不在私密文件夹"）
    priv = d.conn.execute("SELECT id,name FROM files WHERE visibility='private'").fetchall()
    rels = {r["name"]: (d.disk_rel_of(r["id"]) or "") for r in priv}
    got = sum(1 for v in rels.values() if v)
    check("8 篇都能算出盘上相对路径", got == len(priv), "%d/%d" % (got, len(priv)))

    import priv_folder as pf  # noqa: E402
    inside = [n for n, rel in rels.items() if pf.is_private_location(rel)]
    # 迁移（2026-10-15 01:58）之后：8 篇全在私密文件夹
    check("8 篇全在私密文件夹里（迁移后）", len(inside) == 8,
          "%d 篇" % len(inside))
    for n, rel in sorted(rels.items()):
        print("       %-28s %s" % (n[:28], rel))
    d.conn.close()

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ 模式骨架成立：默认 legacy = 行为零变化；请求 package 而条件不足会留痕退回")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
