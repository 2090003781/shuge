# -*- coding: utf-8 -*-
"""补迁移的缺口：盘上那 3 篇还留在原目录，要搬到私密文件夹。

**先搬文件，再让投影去核对** ——
  · 如果投影能修，它会自己写对（但那样会写出密文到库内说的地方）
  · 如果搬对了，投影一看"盘 == 库"就什么都不做

顺序不能反：先让投影写，会在新位置留下密文文件；而旧位置那个也得删掉。

安全：每个文件先记 sha256，搬完复核；旧目录空了顺手收掉。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")

LIB = r"D:\枢阁\库"
OLD_DIR = os.path.join(LIB, "second-brain-system", "dsh")
NEW_DIR = os.path.join(LIB, "个人", "私密")
NAMES = ["DSH-人设-变更账本.md", "DSH-人设.md", "DSH提示词整合方案.md"]

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-50s %s" % ("✓" if cond else "✗", name, detail))


print("=" * 78)
print("补缺口：把盘上那 3 篇搬到私密文件夹")
print("=" * 78)
os.makedirs(NEW_DIR, exist_ok=True)

for nm in NAMES:
    src = os.path.join(OLD_DIR, nm)
    dst = os.path.join(NEW_DIR, nm)
    if not os.path.isfile(src):
        check("源文件在：%s" % nm, False, "找不到 %s" % src)
        continue
    raw = open(src, "rb").read()
    sha_before = hashlib.sha256(raw).hexdigest()
    if os.path.exists(dst):
        check("目标已存在（跳过搬）：%s" % nm, True, "%d 字节" % os.path.getsize(dst))
        # 若目标已存在且内容一致 → 只删源
        if open(dst, "rb").read() == raw:
            os.remove(src)
            print("       内容一致，已删源文件")
        continue
    shutil.move(src, dst)
    ok = os.path.isfile(dst) and hashlib.sha256(open(dst, "rb").read()).hexdigest() == sha_before
    check("搬到私密文件夹且逐字节相同：%s" % nm, ok,
          "%d 字节" % (os.path.getsize(dst) if os.path.isfile(dst) else -1))

print()
left = os.listdir(OLD_DIR) if os.path.isdir(OLD_DIR) else []
print("  原目录剩下的：%s" % (left or "（空）"))
if not left and os.path.isdir(OLD_DIR):
    try:
        os.rmdir(OLD_DIR)
        print("  空目录已收掉：%s" % OLD_DIR)
    except OSError as e:
        print("  目录没收回：%s" % e)

print()
print("  私密文件夹现在：")
for f in sorted(os.listdir(NEW_DIR)):
    print("     %-32s %s" % (f, os.path.getsize(os.path.join(NEW_DIR, f))))

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
print("=" * 78)
sys.exit(0 if not FAIL else 1)
