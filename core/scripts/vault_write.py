"""vault 的安全写入口（辞玖 2026-09-24 问："定时任务不是 agent 完成的吗，那么为什么不能受 agent 侧管理？"）。

能 —— 它们本来就是 agent（DSH 定时任务会拉起会话）。缺的不是能力，是**口径**：现在的 prompt 教它们
"直接写文件"，于是整文件覆盖没有任何拦截。这个脚本就是把"写 vault 文件"收成一个**唯一的门**：

  python scripts/vault_write.py --writer task:日报 --mode merge  <相对路径> --text "..."
  python scripts/vault_write.py --writer task:周报 --mode append <相对路径> --from-file x.txt
  python scripts/vault_write.py --writer task:日报 --mode replace <相对路径> --text "..." --allow-shrink

三道闸（跟镜像/前像一个口径，辞玖定的"备份只针对高风险操作"）：
  · **先写前像**：旧内容复制到 `<vault>/.前像/<日期>/…`（点开头目录 → 导入/镜像/检索全都看不见）
  · **记账本**：`<vault>/.写入账本/<日期>.jsonl` 追加一行（时间/路径/写入者/模式/前后字节/前后哈希）
  · **缩短要显式**：新内容 < 旧内容的 70%（旧文件 ≥3 行或 ≥200 字节）→ 拒绝；确实要删加 --allow-shrink

模式：
  merge   读旧内容 → **只追加"旧文件里没有的整行"**（保底不丢；要改某一行得用 replace）
  append  纯追加（不读旧内容去重，适合日志）
  replace 整文件替换（高风险，前像 + 缩短闸都会生效）

原子替换（写临时文件再 rename），失败不留半截文件。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import time

DEFAULT_VAULT = r"D:\个人数据\辞玖"
SHRINK_GUARD = 0.7


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def read_text(path: str) -> str:
    if not os.path.isfile(path):
        return ""
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def merge_lines(old: str, new: str) -> tuple:
    """merge：保序追加"旧文件里没有的整行"（重复行不重复加）。返回 (结果, 追加行数)"""
    have = {ln.rstrip() for ln in old.splitlines()}
    add = []
    for ln in new.splitlines():
        key = ln.rstrip()
        if key and key not in have:
            add.append(ln)
            have.add(key)
    if not add:
        return old, 0
    out = old
    if out and not out.endswith("\n"):
        out += "\n"
    out += "\n".join(add) + "\n"
    return out, len(add)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="vault 相对路径（如 记忆/热缓存.md）；也接受绝对路径，但必须在 vault 内")
    ap.add_argument("--vault", default=os.environ.get("VAULT_PATH") or DEFAULT_VAULT)
    ap.add_argument("--writer", default="tool:vault_write", help="写入者标签（如 task:日报 / agent:dsh）")
    ap.add_argument("--mode", choices=("merge", "append", "replace"), default="merge")
    ap.add_argument("--text", default=None, help="要写的内容；不给则从 --from-file 或 stdin 读")
    ap.add_argument("--from-file", default=None)
    ap.add_argument("--allow-shrink", action="store_true", help="允许把文件写短（默认拒绝）")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    vault = os.path.abspath(a.vault)
    p = a.path if os.path.isabs(a.path) else os.path.join(vault, a.path.replace("/", os.sep))
    p = os.path.abspath(p)
    if not p.lower().startswith(vault.lower()):
        print(json.dumps({"ok": False, "error": "路径不在 vault 内：%s" % p}, ensure_ascii=False))
        return 2

    if a.text is not None:
        new = a.text
    elif a.from_file:
        new = read_text(a.from_file)
    else:
        new = sys.stdin.read()
    old = read_text(p)

    if a.mode == "merge":
        out, added = merge_lines(old, new)
    elif a.mode == "append":
        out = old + ("" if (not old or old.endswith("\n")) else "\n") + new
        added = len(new.splitlines())
    else:
        out, added = new, 0

    if out == old:
        print(json.dumps({"ok": True, "changed": False, "path": p, "bytes": len(old.encode("utf-8"))},
                         ensure_ascii=False))
        return 0
    old_bytes = len(old.encode("utf-8"))
    old_lines = len([ln for ln in old.splitlines() if ln.strip()])
    shrink = len(out.encode("utf-8")) < old_bytes * SHRINK_GUARD
    # 小文件也要管：旧文件有 3 行以上（或 200 字节以上）而新内容不足七成 → 拒绝
    if shrink and (old_lines >= 3 or old_bytes >= 200) and not a.allow_shrink:
        print(json.dumps({"ok": False, "refused": "shrink",
                          "error": "新内容比旧内容短 %.0f%%（%d → %d 字节）；这是「整文件覆盖」最常见的形态，"
                                   "已拒绝。确实要写短就加 --allow-shrink" %
                                   (100 * (1 - len(out.encode('utf-8')) / max(1, len(old.encode('utf-8')))),
                                    len(old.encode("utf-8")), len(out.encode("utf-8")))},
                         ensure_ascii=False))
        return 3

    stamp = time.strftime("%Y-%m-%d")
    hhmmss = time.strftime("%H%M%S")
    rel = os.path.relpath(p, vault).replace("\\", "/")
    img_rel = ""
    if old:
        img_dir = os.path.join(vault, ".前像", stamp)
        os.makedirs(img_dir, exist_ok=True)
        img = os.path.join(img_dir, "%s-%s" % (hhmmss, rel.replace("/", "__")))
        shutil.copy2(p, img)
        img_rel = os.path.relpath(img, vault).replace("\\", "/")
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "path": rel, "writer": a.writer,
           "mode": a.mode, "before_bytes": len(old.encode("utf-8")),
           "after_bytes": len(out.encode("utf-8")), "before_sha": sha(old), "after_sha": sha(out),
           "added_lines": added, "before_image": img_rel, "allow_shrink": bool(a.allow_shrink)}

    if a.dry_run:
        print(json.dumps({"ok": True, "dry_run": True, **rec}, ensure_ascii=False))
        return 0

    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".vaultwrite-tmp"
    with io.open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    os.replace(tmp, p)                                    # 原子替换：不会留半截文件
    led = os.path.join(vault, ".写入账本", stamp + ".jsonl")
    os.makedirs(os.path.dirname(led), exist_ok=True)
    with io.open(led, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(json.dumps({"ok": True, **rec}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
