# -*- coding: utf-8 -*-
"""红队式自查：锁定态下，那 8 篇私密笔记能从哪些路读到？

只做**只读探测**，不改任何东西。目的：把"锁到底挡住了什么"变成一张清单，
而不是靠读代码猜。

探测面：
  ① /api/content?path=…        （正文）
  ② /api/files/<id>            （正文 + 元数据）
  ③ /api/files/<id>/raw        （原始字节）
  ④ /api/tree                  （名字）
  ⑤ /api/folders               （文件夹名 + 子项数）
  ⑥ /api/tags                  （标签）
  ⑦ /api/rag/list              （在不在检索库）
  ⑧ /api/files/<id>/article    （按篇还原）
  ⑨ /api/files/<id>/blocks     （块树）
  ⑩ /api/files/<id>/backlinks  （引用关系）
  ⑪ /api/search?q=<篇名>        （检索命中）
  ⑫ 盘上直接读（不经库）         （对照：盘上那份是密文吗）
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

API = "http://127.0.0.1:8848"
DB = r"D:\core-notes\data\index.db"
LIB = r"D:\枢阁\库"


def get(path, timeout=30):
    try:
        with urllib.request.urlopen(API + path, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:200]
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, str(e)[:80])


def main() -> int:
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT id,name,size,content_hash FROM files"
                       " WHERE visibility='private' ORDER BY name").fetchall()
    con.close()

    print("=" * 78)
    print("锁定态自查：8 篇私密笔记能从哪些路读到")
    print("=" * 78)
    st, body = get("/api/lock")
    try:
        lk = json.loads(body)
        print("  当前锁状态：configured=%s unlocked=%s private_files=%s"
              % (lk.get("configured"), lk.get("unlocked"), lk.get("private_files")))
        locked = not lk.get("unlocked")
    except Exception:
        print("  /api/lock 解析失败：%s" % body[:80])
        return 1
    if not locked:
        print()
        print("  ⚠️ 库现在是**解锁态** —— 这份探测只有在锁定时才有判别力。")
        print("     下表仍会列出各接口的返回形态，但『露不露』的结论对锁定态才成立。")
    print()

    # 路径版本（锁定时 /api/content 会回 locked）
    rel = "vault/个人/私密/API密钥清单.md"
    fid = rows[0]["id"] if rows else ""
    name = rows[0]["name"] if rows else ""
    stem = name.rsplit(".", 1)[0] if name else ""

    probes = [
        ("① 正文（按路径）", "/api/content?max=100000&path=" + urllib.parse.quote(rel)),
        ("② 正文+元数据（按 id）", "/api/files/" + fid),
        ("③ 原始字节", "/api/files/%s/raw" % fid),
        ("④ 文件树", "/api/tree"),
        ("⑤ 文件夹表", "/api/folders"),
        ("⑥ 标签统计", "/api/tags"),
        ("⑦ 检索库清单", "/api/rag/list"),
        ("⑧ 按篇还原", "/api/files/%s/article" % fid),
        ("⑨ 块树", "/api/files/%s/blocks" % fid),
        ("⑩ 引用关系", "/api/files/%s/backlinks" % fid),
        ("⑪ 检索命中", "/api/search?q=" + urllib.parse.quote(stem)),
        ("⑫ 属性索引", "/api/properties?key=tags"),
    ]

    print("  %-22s %-6s %s" % ("接口", "HTTP", "判定"))
    print("  " + "-" * 74)
    for label, path in probes:
        st, body = get(path)
        leak = []
        # 判据：出现篇名/去扩展名/正文特征 → 算泄漏
        for probe in (name, stem, "仅AI运行时读取", "API密钥清单"):
            if probe and probe in body:
                leak.append(probe)
        verdict = "**露**（含 %s）" % "、".join(leak[:2]) if leak else "不露"
        print("  %-22s %-6s %s" % (label, st, verdict))

    # ⑬ 盘上直接读（不经库）
    print()
    print("  ⑬ 盘上直接读（不经库）—— 对照项")
    p = os.path.join(LIB, "个人", "私密", name) if name else ""
    if p and os.path.isfile(p):
        b = open(p, "rb").read()
        pr = sum(1 for i in range(min(500, len(b))) if 32 <= b[i] < 127)
        printable = pr / max(1, min(500, len(b)))
        print("     %s：%d 字节，可打印 ASCII %.0f%% → %s"
              % (name, len(b), printable * 100,
                 "**明文**" if printable > 0.7 else "密文（不是明文，好）"))
    else:
        print("     盘上没找到：%s" % p)

    print()
    print("=" * 78)
    print("结论判据：锁定态下若上面任何一行报『露』，就是锁的缺口。")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
