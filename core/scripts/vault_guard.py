"""真相哨兵 + 快照 + 自动提交（辞玖 2026-09-23 报"整文件覆盖抹掉旧内容"之后加的看护）。

  python scripts/vault_guard.py            # 跑一轮：扫描 → 对比上次 → 快照 → 自动提交
  python scripts/vault_guard.py --dry-run  # 只看不写（不生成快照、不提交）
  python scripts/vault_guard.py --no-commit
  python scripts/vault_guard.py --root D:\个人数据\辞玖   # 换一个根（默认盯库文件夹）

2026-09-27 库正式启用后：**默认盯库文件夹** `D:\枢阁\库`（库的正文落点），
vault 退役成只读参考 —— 两个根的清单/快照分开存，互不干扰。

做四件事（按"出事时能不能捞回来"排）：
  ① **自动提交**：vault 与库文件夹的变化都进 git（一套系统；有提交就有 diff/checkout 可回滚）。
  ② **快照**：把 `记忆/` 与 `日志/` 打包到 `D:\\枢阁\\快照\\`（默认留 30 份）——
     git 之外再加一层"整目录、可离线翻"的底。
  ③ **哨兵**：和上次的清单对比，报**变小（>30%）/ 消失**的文件 —— 整文件覆盖最常见的形态就是这两个。
     发现异常退出码 1，日志里明写，别让它悄悄过去。
  ④ **报告**：写到 `快照/哨兵报告.md`（追加），给人和 agent 都看得见。

忽略：点开头目录、tmp/stats/.trash 等工具态（与 vault 的 .gitignore 一致）。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import zipfile

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

VAULT = r"D:\个人数据\辞玖"
LIBDIR = r"D:\枢阁\库"          # 库自己的正文树 = 2026-09-27 起的真相源
REPOS = [VAULT, LIBDIR]              # 一套系统：两个仓库，同一个看护
SNAP_DIR = r"D:\枢阁\快照"
SNAP_TARGETS = ["记忆", "日志"]
# 清单按"盯的哪个根"分开存：换了根不会拿旧清单比出新假警报
MANIFEST = os.path.join(SNAP_DIR, "manifest.json")          # vault 的（历史）
MANIFEST_LIB = os.path.join(SNAP_DIR, "manifest-lib.json")  # 库文件夹的
REPORT = os.path.join(SNAP_DIR, "哨兵报告.md")
KEEP = 30
IGNORE_DIRS = {".git", ".obsidian", ".smart-env", ".claudian", ".claude", ".trash",
               "node_modules", "__pycache__", "tmp", "stats"}
SHRINK = 0.7          # 缩到 70% 以下就算可疑
MIN_BYTES = 200       # 太小的文件不参与（噪声）


def scan(root: str) -> dict:
    """{相对路径: [大小, mtime]}（忽略工具态目录与 .gitignore 里的私密目录）"""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, root).replace("\\", "/")
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[rel] = [st.st_size, int(st.st_mtime)]
    return out


def git(root: str, *args) -> str:
    r = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return (r.stdout or "").strip() + (("\n" + r.stderr.strip()) if r.returncode else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    ap.add_argument("--no-commit", action="store_true", help="不自动提交 git")
    ap.add_argument("--root", default=LIBDIR, help="盯哪个根（默认库文件夹）")
    a = ap.parse_args()

    t0 = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    root = a.root
    is_lib = os.path.abspath(root).lower() == os.path.abspath(LIBDIR).lower()
    manifest = MANIFEST_LIB if is_lib else MANIFEST
    prefix = "lib" if is_lib else "vault"
    label = "库文件夹" if is_lib else "vault"
    cur = scan(root)
    prev = {}
    if os.path.isfile(manifest):
        try:
            prev = json.load(open(manifest, encoding="utf-8")).get("files") or {}
        except Exception:
            prev = {}

    # ── 哨兵 ────────────────────────────────────────────────
    shrank, gone = [], []
    for rel, (size, _mt) in prev.items():
        if rel not in cur:
            gone.append((rel, size))
            continue
        now = cur[rel][0]
        if size > MIN_BYTES and now < size * SHRINK:
            shrank.append((rel, size, now))
    shrank.sort(key=lambda x: x[2] - x[1])
    gone.sort(key=lambda x: -x[1])

    lines = ["## %s（盯 %s）" % (stamp, label),
             "- 扫描 %d 个文件；比上次：变小(>%.0f%%) %d 个、消失 %d 个"
             % (len(cur), (1 - SHRINK) * 100, len(shrank), len(gone))]
    for rel, before, now in shrank[:20]:
        lines.append("  - ⚠ 变小：%s  %d → %d 字节（-%d%%)"
                     % (rel, before, now, round(100 * (1 - now / max(1, before)))))
    for rel, size in gone[:20]:
        lines.append("  - ⚠ 消失：%s（原 %d 字节）" % (rel, size))
    text = "\n".join(lines)
    print(text)

    # ── 快照 + 清单 + 报告 ──────────────────────────────────
    if not a.dry_run:
        os.makedirs(SNAP_DIR, exist_ok=True)
        zp = os.path.join(SNAP_DIR, "%s-%s.zip" % (prefix, time.strftime("%Y%m%d-%H%M%S")))
        with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
            n = 0
            for t in SNAP_TARGETS:
                base = os.path.join(root, t)
                for dirpath, dirnames, filenames in os.walk(base):
                    dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS
                                   and not d.startswith(".")]
                    for fn in filenames:
                        p = os.path.join(dirpath, fn)
                        z.write(p, os.path.relpath(p, root))
                        n += 1
        print("快照：%s（%d 个文件，%.2f MB）"
              % (os.path.basename(zp), n, os.path.getsize(zp) / 1048576.0))
        with open(manifest, "w", encoding="utf-8") as f:
            json.dump({"at": stamp, "count": len(cur), "files": cur, "root": root}, f,
                      ensure_ascii=False)
        with open(REPORT, "a", encoding="utf-8") as f:
            f.write(text + "\n\n")
        # 只留最近 KEEP 份
        snaps = sorted(x for x in os.listdir(SNAP_DIR)
                       if x.startswith(prefix + "-") and x.endswith(".zip"))
        for old in snaps[:-KEEP]:
            os.remove(os.path.join(SNAP_DIR, old))
            print("清掉旧快照：%s" % old)

    # ── 自动提交（两个仓库都提；git 就是备份）──────────────
    if not a.no_commit and not a.dry_run:
        for repo in REPOS:
            if not os.path.isdir(os.path.join(repo, ".git")):
                print("git：%s 不是仓库，跳过" % repo)
                continue
            dirty = git(repo, "status", "--porcelain")
            if dirty:
                git(repo, "add", "-A")
                msg = "guard: 自动存档 %s（变化 %d 个文件）" % (stamp, len(dirty.splitlines()))
                git(repo, "-c", "user.name=哨兵", "-c", "user.email=guard@local",
                    "commit", "-q", "-m", msg)
                print("已自动提交 %s：%s" % (os.path.basename(repo), msg))
            else:
                print("git（%s）：没有变化" % os.path.basename(repo))

    print("用时 %.1f 秒；哨兵结论：%s"
          % (time.time() - t0, "有异常，见上" if (shrank or gone) else "正常"))
    return 1 if (shrank or gone) else 0


if __name__ == "__main__":
    sys.exit(main())
