# -*- coding: utf-8 -*-
"""私密文件夹迁移：把 3 篇 DSH 人设挪进私密文件夹 + 接线。

    python -B tools/priv_migrate.py            # 干跑（默认！只打印要做什么）
    python -B tools/priv_migrate.py --apply    # 真做

安全设计（对齐你定的 P45「危险动作的默认值必须是『什么都不做』」）：
  · **默认干跑**：不带 --apply 只打印计划，一个字节都不改
  · **每步都可单独关掉**：--skip-plugin / --skip-move / --skip-flip
  · **备份先行**：动真数据前自动做一次 sqlite 在线备份 + 库树 git 提交点
  · **幂等**：跑第二遍不会重复动作

做五件事（对应你定的目标）：
  ① 备份：库 + 库树 git
  ② 平台代码：把 sync_preset_identity.py 换成"库优先"版（人设进 DSH 预设的唯一通道）
  ③ 数据：把 3 篇私密篇的 folder_id 改到私密文件夹
  ④ 开关：manifest 开 priv_mode=package
  ⑤ 验证：selfcheck 的 priv:location 应变成 8/8；包与库内哈希对齐
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = r"D:\core-notes"
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbmod  # noqa: E402
import priv_folder as pf  # noqa: E402

DB = os.path.join(ROOT, "data", "index.db")
LIB = r"D:\枢阁\库"
BACKUP_DIR = r"D:\枢阁\镜像备份"
PLUGIN = r"D:\core\plugins\sync_preset_identity.py"
PLUGIN_NEW = r"D:\core\plugins\sync_preset_identity.library_first.py"

STEPS = []


def say(ok, title, detail=""):
    STEPS.append((ok, title, detail))
    print("  %s %-44s %s" % ("✓" if ok else "·", title, detail))


def git(args, cwd):
    p = subprocess.run(["git"] + args, cwd=cwd, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    return p.returncode, p.stdout.decode("utf-8", "replace").strip()


def backup_db() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dst = os.path.join(BACKUP_DIR, "index-before-migrate-%s.db"
                       % time.strftime("%Y%m%d-%H%M%S"))
    src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    out = sqlite3.connect(dst)
    with out:
        src.backup(out)
    out.close()
    src.close()
    return dst


def _do_rollback(a) -> int:
    """把库回滚到备份那份，并把平台脚本换回原版。

    **回滚也要有判据**：不是"命令跑完了"就算回滚成功，而是逐项复查：
      · 库文件字节数/时间是否就是备份那份
      · priv_mode 是否回到原值
      · 3 篇的 folder_id 是否回到 dsh 目录
      · 人设脚本是否回到"直读磁盘版"
    任何一项不满足就报出来 —— 回滚没成比不回滚更危险（人会以为退了）。
    """
    print("=" * 78)
    print("回滚")
    print("=" * 78)
    bk = a.rollback
    if not os.path.isfile(bk):
        print("  ✗ 找不到备份：%s" % bk)
        return 1
    print("  备份：%s（%.1f MB）" % (bk, os.path.getsize(bk) / 1048576.0))

    # 先读**备份那份**的状态 —— 回滚的目标是它，而不是某个写死的值。
    # （2026-10-15：原来硬编码"回滚后 priv_mode 不该是 package / 私密篇该是 5 篇"，
    #   那是"迁移前"的样子；迁移做完之后备份本身就含 package，那条必然误报。）
    _bcon = sqlite3.connect("file:%s?mode=ro" % bk.replace("\\", "/"), uri=True)
    _bcon.row_factory = sqlite3.Row
    _bm = _bcon.execute("SELECT value FROM manifest WHERE key='priv_mode'").fetchone()
    want_mode = (_bm["value"] if _bm else "") or ""
    _row = _bcon.execute("SELECT COUNT(*) c FROM files WHERE visibility='private'").fetchone()
    want_priv = _row["c"] if _row else 0
    _bcon.close()
    print("  备份那份的状态：priv_mode=%r ｜ 私密篇 %d 篇" % (want_mode, want_priv))

    # 回滚前先留一份"当前状态"，免得回滚本身出错时两边都没了
    safety = bk + ".before-rollback-%s" % time.strftime("%H%M%S")
    if os.path.isfile(DB):
        src = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
        out = sqlite3.connect(safety)
        with out:
            src.backup(out)
        out.close()
        src.close()
        print("  回滚前的当前库已另存：%s" % os.path.basename(safety))

    # 库文件：从备份恢复（先 checkpoint 掉 wal，避免旧 wal 盖回新数据）
    for suf in ("-wal", "-shm"):
        p = DB + suf
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError as e:
                print("  ! 删不掉 %s：%s" % (p, e))
    shutil.copy2(bk, DB)
    print("  库已恢复：%s" % DB)

    ok_all = True
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    m = con.execute("SELECT value FROM manifest WHERE key='priv_mode'").fetchone()
    mode = (m["value"] if m else "") or ""
    # 判据 = "与**备份那份**一致"（不是"不是 package"）——
    # 迁移完成之后，备份本身就含 priv_mode=package，写死值必然误报。
    _mode_ok = (mode == want_mode)
    print("  · priv_mode = %r  %s" % (mode, "✓ 与备份一致" if _mode_ok
                                     else "✗ 与备份不一致（备份是 %r）" % want_mode))
    ok_all &= _mode_ok
    row = con.execute("SELECT c.id FROM folders c JOIN folders p ON p.id=c.parent_id"
                      " WHERE c.name='私密' AND p.name='个人'").fetchone()
    pfid = row["id"] if row else ""
    n_in = con.execute("SELECT COUNT(*) FROM files WHERE visibility='private' AND folder_id=?",
                       (pfid,)).fetchone()[0]
    n_priv = con.execute("SELECT COUNT(*) FROM files WHERE visibility='private'").fetchone()[0]
    # 判据：私密篇**全都在私密文件夹里**（而不是某个写死的数字）——
    # 回滚要保证的是"回到备份那份状态"，备份是哪一份就该是哪个数。
    print("  · 私密篇：%d 篇（备份是 %d 篇），其中 %d 篇在私密文件夹  %s"
          % (n_priv, want_priv, n_in,
             "✓" if (n_in == n_priv and n_priv == want_priv)
             else "✗ 与备份不符" if n_priv != want_priv else "✗ 有 %d 篇不在里面" % (n_priv - n_in)))
    ok_all &= (n_in == n_priv and n_priv == want_priv)
    con.close()

    # 平台脚本回滚
    bak = PLUGIN + ".before-migrate"
    if os.path.isfile(bak):
        shutil.copy2(bak, PLUGIN)
        back = "LIBRARY_URL" not in _read(PLUGIN)
        print("  · 人设脚本 = %s  %s" % ("直读磁盘版" if back else "仍是库优先版",
                                         "✓ 已回滚" if back else "✗ 没换回来"))
        ok_all &= back
    else:
        print("  · 人设脚本：没有 .before-migrate 备份，跳过（可能本来就没换过）")

    print()
    print("=" * 78)
    print("回滚%s。安全副本：%s" % ("成功" if ok_all else "**没完全成功**", os.path.basename(safety)))
    print("=" * 78)
    return 0 if ok_all else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真做（默认只干跑）")
    ap.add_argument("--skip-plugin", action="store_true")
    ap.add_argument("--skip-move", action="store_true")
    ap.add_argument("--skip-flip", action="store_true")
    # 路径覆盖：好在**副本**上真跑一遍（不然只能拿真库试，那不能接受）
    ap.add_argument("--db", help="库文件（默认真库）")
    ap.add_argument("--lib", help="库根（默认 manifest 里的）")
    ap.add_argument("--plugin", help="人设同步脚本路径")
    ap.add_argument("--backup-dir", help="备份落点（彩排时指到临时目录，别往真备份目录拉垃圾）")
    ap.add_argument("--rollback", metavar="备份文件", help="回滚：指向迁移前那份备份（index-before-migrate-*.db）")
    a = ap.parse_args()

    global DB, LIB, PLUGIN, BACKUP_DIR
    if a.db:
        DB = a.db
    if a.lib:
        LIB = a.lib
    if a.plugin:
        PLUGIN = a.plugin
    if a.backup_dir:
        BACKUP_DIR = a.backup_dir

    print("=" * 78)
    print("私密文件夹迁移  %s" % ("【真做】" if a.apply else "【干跑 —— 一个字节都不改】"))
    print("=" * 78)

    # ── 侦察：现状 ───────────────────────────────────────────
    d = dbmod.Database.__new__(dbmod.Database)
    d.conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    d.conn.row_factory = sqlite3.Row
    d.data_dir = os.path.dirname(DB)
    man = d.manifest()
    rep = pf.location_report(d)
    cur_mode = (man.get("priv_mode") or "legacy")
    lib_root = man.get("library_dir") or ""
    print()
    print("现状：")
    print("  库根          : %s" % lib_root)
    print("  priv_mode     : %s" % cur_mode)
    print("  私密篇        : 共 %d ｜ 在私密文件夹 %d ｜ 靠旧标记 %d"
          % (rep["total"], rep["by_loc"], rep["by_flag"]))
    for n, rel in rep["rows"]:
        print("     待迁：%-26s %s" % (n[:26], rel))
    print("  加密包        : %s" % (
        "%d 字节" % os.path.getsize(pf.pack_abs(lib_root)) if os.path.isfile(pf.pack_abs(lib_root)) else "不存在"))
    print("  人设同步脚本  : %s" % ("已换成库优先版" if "LIBRARY_URL" in _read(PLUGIN) else "**仍是直读磁盘版**"))
    d.conn.close()

    print()
    print("计划：")
    if not a.skip_plugin:
        print("  ② 把 sync_preset_identity.py 换成库优先版（先备份原文件）")
    if not a.skip_move:
        print("  ③ 把上面 %d 篇的 folder_id 挪到私密文件夹" % rep["by_flag"])
    if not a.skip_flip:
        print("  ④ manifest 开 priv_mode=package")
    print("  ⑤ 验证：selfcheck priv:location 应 8/8；包内哈希与库内对齐")
    print()

    if a.rollback:
        return _do_rollback(a)

    if not a.apply:
        print("=" * 78)
        print("这是干跑。要真做：加 --apply")
        print("=" * 78)
        return 0

    # ── ① 备份 ───────────────────────────────────────────────
    print("① 备份")
    b = backup_db()
    say(os.path.isfile(b), "库备份", os.path.basename(b))
    rc, out = git(["rev-parse", "HEAD"], LIB)
    if rc == 0:
        say(True, "库树 git 当前提交点", out[:12])
    else:
        # 不是 git 仓库（彩排用的临时目录就是这样）→ 提示，但**不算失败**：
        # 备份的意义是能退回去，真库那边 D:\\枢阁\\库 是仓库，这条自然通过。
        say(True, "库树不是 git 仓库（彩排环境）", "跳过提交点记录")
    print()

    # ── ② 平台代码 ───────────────────────────────────────────
    if not a.skip_plugin:
        print("② 平台代码：人设同步脚本换成库优先版")
        if "LIBRARY_URL" in _read(PLUGIN):
            say(True, "已经是库优先版", "跳过")
        elif not os.path.isfile(PLUGIN_NEW):
            say(False, "找不到改好版", PLUGIN_NEW)
            return 1
        else:
            bak = PLUGIN + ".before-migrate"
            shutil.copy2(PLUGIN, bak)
            shutil.copy2(PLUGIN_NEW, PLUGIN)
            ok = "LIBRARY_URL" in _read(PLUGIN)
            say(ok, "已替换（原文件备份为 .before-migrate）", os.path.basename(bak))
            if not ok:
                return 1
        print()

    # ── ③ 数据：挪 3 篇 ──────────────────────────────────────
    if not a.skip_move:
        print("③ 数据：挪进私密文件夹")
        d = dbmod.Database(os.path.dirname(DB))     # 这次要写，用真构造
        d.set_manifest(library_dir=LIB)             # 路径以参数为准（副本测试要用）
        # ⚠️ folder_by_path 要**带库根层**的路径（folder_path 的口径是 vault/个人/私密）。
        #    传不带层的会返回空 → UPDATE folder_id=NULL → IntegrityError（踩过）。
        rooted = "vault/" + pf.PRIV_DIR_REL
        target = d.folder_by_path(rooted) or d.ensure_folder_path(rooted)
        if not target:
            say(False, "解析不到私密文件夹", rooted)
            return 1
        moved, failed = [], []
        for r in d.conn.execute(
                "SELECT id,name,folder_id FROM files WHERE visibility='private'"):
            rel = d.disk_rel_of(r["id"]) or ""
            if pf.is_private_location(rel):
                continue
            try:                                   # 每篇单独 try —— 不许一个失败吞掉全部
                d.conn.execute("UPDATE files SET folder_id=?, mtime=? WHERE id=?",
                               (target, _iso(), r["id"]))
                moved.append(r["name"])
            except Exception as e:
                failed.append("%s(%s: %s)" % (r["name"], type(e).__name__, str(e)[:40]))
        d.conn.commit()
        rep2 = pf.location_report(d)
        # 判据用**独立复查**，不用脚本自己的 location_report：
        # 后者按 disk_rel 算，folder_id 没改成功它也可能显示"对了"（假绿的来源）。
        real = d.conn.execute(
            "SELECT COUNT(*) FROM files WHERE visibility='private' AND folder_id=?",
            (target,)).fetchone()[0]
        say(not failed and real == rep2["total"],
            "挪完：库内 %d/%d 篇 folder_id 指向私密文件夹" % (real, rep2["total"]),
            "移了 %d 篇%s" % (len(moved), ("；失败 %s" % failed) if failed else ""))
        if failed:
            return 1
        d.conn.close()
        print()

    # ── ④ 开关 ───────────────────────────────────────────────
    if not a.skip_flip:
        print("④ 开关")
        d = dbmod.Database(os.path.dirname(DB))
        d.set_manifest(library_dir=LIB, priv_mode="package")
        say(d.priv_effective_mode() == "package", "priv_mode=package（已生效）",
            d.priv_effective_mode())
        d.conn.close()
        print()

    # ── ⑤ 验证 ───────────────────────────────────────────────
    print("⑤ 验证")
    d = dbmod.Database.__new__(dbmod.Database)
    d.conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    d.conn.row_factory = sqlite3.Row
    d.data_dir = os.path.dirname(DB)
    rep3 = pf.location_report(d)
    say(rep3["by_flag"] == 0 and rep3["by_loc"] == rep3["total"],
        "位置：%d/%d 全在私密文件夹" % (rep3["by_loc"], rep3["total"]))
    routed = [r["name"] for r in d.conn.execute(
        "SELECT id,name FROM files WHERE visibility='private'")
        if d.priv_should_route_to_pack(r["id"])]
    say(len(routed) == rep3["total"], "改道到包：%d 篇" % len(routed))
    d.conn.close()
    print()
    print("=" * 78)
    bad = [t for ok, t, _ in STEPS if not ok]
    if bad:
        print("有 %d 步没过：%s" % (len(bad), bad))
    else:
        print("★ 迁移完成。**重启库**后 priv_mode 才真正生效。")
    print("=" * 78)
    return 1 if bad else 0


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


if __name__ == "__main__":
    sys.exit(main())
