# -*- coding: utf-8 -*-
"""修 2026-10-15 18:5x 那次批量改锚点留下的**级联重写**，然后用单遍正则重做。

事故原因（留作教训）：
    规则里 `项目/第二大脑 → 第二大脑项目/第二大脑`，替换文本**自己又包含旧串**。
    逐条 `str.replace` 一遍过完，第二遍（乃至同一条规则的后续扫描）把刚写进去的
    `第二大脑项目/第二大脑` 里的 `项目/第二大脑` **又替了一遍** →
    变成 `第二大脑第二大脑项目/第二大脑/待办清单.md`（样本：系统/任务-日报.md）。

修法：
    ① 先把 `第二大脑项目/第二大脑` 还原成 `项目/第二大脑`（一步到位，与旧串不重叠）；
    ② 再用**单遍正则**重做：alternation 里长的在前，re.sub 从左到右只扫一遍，
       替换出来的文本**不会被再扫**（这正是 str.replace 链做不到的）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:8848"
FROZEN_PREFIX = ("vault/记录/日志", "vault/记录/日报", "vault/记录/周报")

# ① 还原用的：把被级联的产物收回旧串
UNDO = [("第二大脑项目/第二大脑", "项目/第二大脑")]

# ② 单遍重做：长的在前（正则 alternation 按顺序匹配）
CANON = [
    ("项目/第二大脑/", "第二大脑项目/第二大脑/"),
    ("项目/第二大脑", "第二大脑项目/第二大脑"),
    ("second-brain-system/", "第二大脑项目/second-brain-system/"),
    ("系统设计/功能更新/", "第二大脑项目/系统设计/功能更新/"),
    ("系统设计/Bot系统-v2/", "第二大脑项目/系统设计/Bot系统-v2/"),
]
PAT = re.compile("|".join(re.escape(k) for k, _ in CANON))
MAP = dict(CANON)


def api(path, body=None, method="GET"):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def path_of(folders, fid):
    parts, cur = [], fid
    for _ in range(60):
        f = folders.get(cur)
        if not f:
            break
        if f["name"]:
            parts.append(f["name"])
        cur = f["parent_id"]
        if not cur:
            break
    return "/".join(reversed(parts))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--undo-only", action="store_true", help="只还原，不重做")
    ap.add_argument("--db", default=r"D:\core-notes\data\index.db")
    a = ap.parse_args()

    c = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(a.db).replace("\\", "/"), uri=True)
    c.row_factory = sqlite3.Row
    folders = {r["id"]: dict(r) for r in c.execute("SELECT id,parent_id,name FROM folders")}

    todo = []
    for r in c.execute("SELECT id,name,folder_id FROM files"):
        name = r["name"] or ""
        if not name.endswith((".md", ".txt")):
            continue
        rel = path_of(folders, r["folder_id"]) if r["folder_id"] else ""
        if any(rel.startswith(f) for f in FROZEN_PREFIX):
            continue
        try:
            f = (api("/api/files/%s?mode=full" % r["id"]).get("file") or {})
        except Exception:
            continue
        txt = f.get("content") or f.get("text") or ""
        if not isinstance(txt, str) or not txt:
            continue

        fixed = txt
        undo_n = 0
        for old, new in UNDO:
            if old in fixed:
                undo_n += fixed.count(old)
                fixed = fixed.replace(old, new)
        redo_n = 0
        if not a.undo_only:
            fixed, redo_n = PAT.subn(lambda m: MAP[m.group(0)], fixed)
        if fixed != txt:
            todo.append((r["id"], rel + "/" + name, undo_n, redo_n, fixed))

    print("要修 %d 篇（还原 %d 处 / 重做 %d 处）："
          % (len(todo), sum(x[2] for x in todo), sum(x[3] for x in todo)))
    for _, p, u, rd, _ in todo:
        print("  %-72s 还原 %-3d 重做 %d" % (p, u, rd))
    if not a.apply:
        print()
        print("== 预演（加 --apply 才真改）==")
        return 0

    ok = bad = 0
    for fid, p, u, rd, txt in todo:
        try:
            r = api("/api/files/%s" % fid, {"content": txt}, method="PUT")
            if r.get("ok"):
                ok += 1
            else:
                bad += 1
                print("  [失败]", p, r)
        except Exception as e:
            bad += 1
            print("  [异常]", p, e)
    print()
    print("== 完事：成功 %d / 失败 %d ==" % (ok, bad))
    return 0


if __name__ == "__main__":
    sys.exit(main())
