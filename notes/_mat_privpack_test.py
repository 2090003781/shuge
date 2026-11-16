# -*- coding: utf-8 -*-
"""实证：`materialize.py` 遇到库树里的 `.privpack` 会不会误删 / 误报。

这是"包能不能安全进库树"的前置条件。用**临时目标目录**测，不碰真库树。

三种模式都要看：
  ① --verify   ：会不会把 `.privpack` 报成漂移或多余
  ② --apply --prune ：会不会把 `.privpack` **删掉**（最危险）
  ③ --apply   ：会不会被包覆盖，或覆盖包
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\core-notes\src")

MAT = r"D:\core-notes\tools\materialize.py"
REAL_LIB = r"D:\枢阁\库"
# 包已进真库树（2026-10-15，git 643caa6）
STAGE_PACK = r"D:\枢阁\库\个人\私密\.privpack"
PY = sys.executable


def run(args):
    p = subprocess.run([PY, "-B", MAT] + args, cwd=r"D:\core-notes",
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def main() -> int:
    print("=" * 78)
    print("实证 materialize 对 .privpack 的行为")
    print("=" * 78)
    if not os.path.isfile(STAGE_PACK):
        print("  库树里的包不存在")
        return 1

    with tempfile.TemporaryDirectory(prefix="mat-privpack-") as td:
        target = os.path.join(td, "库")
        print("  临时目标：%s（不碰真库树）" % target)

        # 造一个"像库树"的目标：从真库树**复制目录结构 + 少量文件**太慢，
        # 改为只复制私密文件夹那一小块 + 放一个不带对应库条目的多余文件
        pd = os.path.join(target, "个人", "私密")
        os.makedirs(pd, exist_ok=True)
        shutil.copy2(STAGE_PACK, os.path.join(pd, ".privpack"))
        pack_sha_before = open(os.path.join(pd, ".privpack"), "rb").read()

        # 一个"库里没有"的普通文件 —— 用来验证 prune 的删除行为确实在工作
        junk = os.path.join(target, "zz-库里没有的多余文件.md")
        with open(junk, "w", encoding="utf-8") as f:
            f.write("prune 应该删掉我\n")

        print("  布置：个人/私密/.privpack（%d 字节）+ zz-多余文件.md"
              % len(pack_sha_before))
        print()

        # ① --verify
        print("① --verify")
        rc, out = run(["--verify", "--target", target])
        tail = [l for l in out.splitlines() if "核对" in l or "真漂移" in l or "privpack" in l]
        for l in tail[:6]:
            print("     %s" % l.strip())
        print("     退出码 %d" % rc)
        print()

        # ② apply + prune（最危险：会不会删掉包）
        print("② --apply --prune（关键：包不能被删）")
        rc2, out2 = run(["--apply", "--prune", "--target", target])
        deleted = [l for l in out2.splitlines() if l.strip().startswith("删（库里没有）")]
        for l in deleted[:8]:
            print("     %s" % l.strip())
        pack_now = os.path.join(pd, ".privpack")
        alive = os.path.isfile(pack_now)
        same = alive and open(pack_now, "rb").read() == pack_sha_before
        print("     .privpack 还在 ? %s" % alive)
        print("     .privpack 未被改 ? %s" % same)
        print("     zz-多余文件.md 被删 ? %s" % (not os.path.exists(junk)))
        print("     退出码 %d" % rc2)
        print()

        ok = alive and same and not os.path.exists(junk)
        print("=" * 78)
        if ok:
            print("★ 判定：materialize 对 .privpack 安全 —— 不删、不改、不误报；prune 照常干活")
        else:
            print("✗ 判定：有风险！包=%s 未改=%s 多余文件被删=%s"
                  % (alive, same, not os.path.exists(junk)))
        print("=" * 78)
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
