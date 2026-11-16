# -*- coding: utf-8 -*-
"""移出私密：把加密包里的一篇**解密落盘**成普通笔记，再从包里删掉（你定的第⑥条）。

设计（2026-10-15 你定）：**只写当前版**，加密包里那份删掉。

## 为什么是这个顺序

    先写盘（成功） → 再删包里那条

反过来先删包里那条，写盘一失败就**永久丢内容**。所以本工具把顺序写死，
并且写盘用「临时文件 + 原子替换」—— 半截文件不算成功。

## 为什么做成独立工具，不做成接口

「移出私密」到底算不算一次"移动"（要动 `folder_id`、要改引用路径、要触发检索重算），
还是只算"把正文吐出来"——**这两种语义都说得通，我没定**。
所以在定之前不碰 `db.py` / `server.py` 的接口面，只给一个能跑的工具：
它只做三件事（解密 → 落盘 → 从包里删），不碰库的文件夹树与引用。

用法：
    python -B tools/priv_out.py --list                    # 看包里有什么（要密码）
    python -B tools/priv_out.py --name "索引.md" --to D:\\某个目录 [--keep]
    --keep = 只落盘、不从包里删（先看效果）
"""
from __future__ import annotations

import argparse
import getpass
import io
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import priv_folder as pf  # noqa: E402
import privpack as pp  # noqa: E402
import vaultcrypto  # noqa: E402

DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"


def master_from_password(pw: str):
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    man = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM manifest")}
    con.close()
    m = vaultcrypto.derive(pw, man.get("kdf_salt", ""), man.get("kdf_n", 1 << 15),
                           man.get("kdf_r", 8), man.get("kdf_p", 1))
    if not vaultcrypto.check_master(m, man.get("verify_blob") or ""):
        raise SystemExit("密码不对")
    return m


def unique_path(directory: str, name: str) -> str:
    """落盘不留歧义：同名就加 .1 .2 …（绝不用「覆盖」来"成功"）"""
    dst = os.path.join(directory, name)
    if not os.path.exists(dst):
        return dst
    base, ext = os.path.splitext(name)
    i = 1
    while True:
        cand = os.path.join(directory, "%s.%d%s" % (base, i, ext))
        if not os.path.exists(cand):
            return cand
        i += 1


def write_atomic(path: str, data: bytes, retries: int = 3) -> dict:
    tmp = path + ".po-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    last = None
    for i in range(retries):
        try:
            os.replace(tmp, path)
            return {"ok": True, "path": path, "bytes": len(data)}
        except OSError as e:
            last = e
            time.sleep(0.15 * (i + 1))
    try:
        os.remove(tmp)
    except OSError:
        pass
    return {"ok": False, "why": "%s: %s" % (type(last).__name__, str(last)[:60])}


def move_out(lib: str, master: bytes, priv_raw, name: str, to_dir: str,
             folder: str = "", keep: bool = False, vault_pub: str = "") -> dict:
    """核心动作：解密 → 落盘 → （可选）从包里删。返回结构化结果。

    **顺序是硬保证**：落盘（含读回自证）不成功 → 直接返回，**一个字节都不动包**。
    抽成函数是为了让测试能真跑到"落盘失败"那条路，而不是靠复刻顺序来自证。

    返回 {"ok": bool, "why": str, "path": str, "bytes": int, "pack_rewritten": bool,
          "left": int}
    """
    items = pf.read_items(lib, master, priv_raw)
    data = pf.get_item_bytes(items, name, folder)
    if data is None:
        return {"ok": False, "why": "包里没有这一篇：%r（folder=%r）" % (name, folder),
                "path": "", "bytes": 0, "pack_rewritten": False, "left": len(items)}

    # ① 先落盘
    try:
        os.makedirs(to_dir, exist_ok=True)
    except OSError as e:
        return {"ok": False, "why": "落盘目录不可用：%s" % str(e)[:60],
                "path": "", "bytes": 0, "pack_rewritten": False, "left": len(items)}
    dst = unique_path(to_dir, name)
    w = write_atomic(dst, data)
    if not w.get("ok"):
        return {"ok": False, "why": "落盘失败（包里那份不动）：%s" % w.get("why"),
                "path": "", "bytes": 0, "pack_rewritten": False, "left": len(items)}
    try:
        back = open(dst, "rb").read()
    except OSError as e:
        return {"ok": False, "why": "落盘后读不回：%s（包里那份不动）" % str(e)[:50],
                "path": dst, "bytes": 0, "pack_rewritten": False, "left": len(items)}
    if back != data:
        return {"ok": False, "why": "落盘后读回不一致（包里那份不动）",
                "path": dst, "bytes": 0, "pack_rewritten": False, "left": len(items)}

    if keep:
        return {"ok": True, "why": "只落盘（--keep）", "path": dst, "bytes": len(back),
                "pack_rewritten": False, "left": len(items)}

    # ② 再删包里那条
    items2, dropped = pf.drop_item(items, name, folder)
    if not dropped:
        return {"ok": False, "why": "包里没删掉（不该发生）", "path": dst, "bytes": len(back),
                "pack_rewritten": False, "left": len(items)}
    r = pf.write_items(lib, items2, vault_pub or _manifest_key("vault_pub"),
                       master=master, vault_priv_raw=priv_raw)
    # ③ 复查
    items3 = pf.read_items(lib, master, priv_raw)
    gone = pf.get_item_bytes(items3, name, folder) is None
    return {"ok": bool(gone), "why": "" if gone else "复查没过：包里那条还在",
            "path": dst, "bytes": len(back), "pack_rewritten": True, "left": len(items3)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出包内清单（要密码）")
    ap.add_argument("--name", help="要移出的篇名（包内名字）")
    ap.add_argument("--folder", default="", help="包内子目录（默认根）")
    ap.add_argument("--to", help="落盘目录（默认库树的 个人/私密 同级外面：D:\\枢阁\\库\\个人）")
    ap.add_argument("--keep", action="store_true", help="只落盘，不从包里删（先看效果）")
    a = ap.parse_args()

    print("=" * 78)
    print("移出私密（设计第⑥条：只写当前版，包里那份删掉）")
    print("=" * 78)
    print("  包：%s" % pf.pack_abs(LIB))
    blob, _ = pf.load_pack(LIB)
    if blob is None:
        print("  ✗ 库里没有加密包")
        return 1

    pw = os.environ.get("SHUGE_PW") or getpass.getpass("库密码: ")
    master = master_from_password(pw)
    priv = vaultcrypto.unwrap_key(master, _manifest_key("vault_priv_ref"))
    items = pf.read_items(LIB, master, priv)
    print("  包内 %d 篇" % len(items))
    print()

    if a.list or not a.name:
        print("  %-34s %8s  %s" % ("包内名字", "字节", "mtime"))
        print("  " + "-" * 62)
        for it in sorted(items, key=lambda x: ((x.get("folder") or ""), x.get("name") or "")):
            sub = ("%s/" % it["folder"]) if it.get("folder") else ""
            print("  %-34s %8d  %s" % ((sub + it["name"])[:34], it.get("size") or 0,
                                       (it.get("mtime") or "")[:19]))
        if not a.name:
            print()
            print("  加 --name 指定要移出的那一篇。")
            return 0

    name, folder = a.name, a.folder
    outdir = a.to or os.path.join(LIB, '个人')
    r = move_out(LIB, master, priv, name, outdir, folder=folder, keep=a.keep)
    if not r.get('ok'):
        print('  X %s' % r.get('why'))
        if r.get('path'):
            print('     （已落盘的那份在：%s）' % r['path'])
        return 1
    print('  落盘：%s（%d 字节，读回逐字节一致）' % (r['path'], r['bytes']))
    if not r.get('pack_rewritten'):
        print()
        print('  --keep：只落盘。包里那份没删（要真移出，去掉 --keep 重跑）')
        return 0
    print('  包里已删该条，剩 %d 篇（整包原子替换）' % r['left'])
    print()
    print('  ★ 移出完成：%s' % r['path'])
    print('     注意：本工具只做「解密落盘 + 包里删条」，')
    print('     没有动库的 folder_id / 引用路径 / 检索 —— 那部分语义还没定。')
    return 0

    # ② 再删包里那条（整包原子替换）
    items2, dropped = pf.drop_item(items, name, folder)
    if not dropped:
        print("  ✗ 包里没删掉（不该发生），包里那份仍在")
        return 1
    r = pf.write_items(LIB, items2, _manifest_key("vault_pub"), master=master, vault_priv_raw=priv)
    print("  ② 包里已删该条，剩 %d 篇；包已原子替换（%d 字节）"
          % (len(items2), r.get("bytes") or 0))

    # ③ 复查：重读包，确认那条真没了、别的还在
    items3 = pf.read_items(LIB, master, priv)
    gone = pf.get_item_bytes(items3, name, folder) is None
    same = sorted(x["name"] for x in items3) == sorted(x["name"] for x in items if x["name"] != name)
    print("  ③ 复查：该条已不在包里=%s ｜ 其余篇完好=%s" % (gone, same))
    if not (gone and same):
        print("  ✗ 复查没过 —— 落盘那份在，包状态异常，请人工看")
        return 1
    print()
    print("  ★ 移出完成：%s" % dst)
    print("     注意：本工具**只**做「解密落盘 + 包里删条」，")
    print("     没有动库的 folder_id / 引用路径 / 检索 —— 那部分语义还没定。")
    return 0


def _manifest_key(k: str) -> str:
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    r = con.execute("SELECT value FROM manifest WHERE key=?", (k,)).fetchone()
    con.close()
    return (r[0] if r else "") or ""


if __name__ == "__main__":
    sys.exit(main())
