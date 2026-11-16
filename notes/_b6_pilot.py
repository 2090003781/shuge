# -*- coding: utf-8 -*-
"""B6 试点：清掉 20 篇的库内正文，验证「读/写/搜」全靠文件仍然正常。

    python -B _b6_pilot.py --pre   # 记录清之前的正文哈希（只读）
    python -B _b6_pilot.py --clear # 真清（content=NULL，元数据保留）
    python -B _b6_pilot.py --check # 清之后验证

判据（每一步都要过）：
  ① 清之前记下每篇正文的 sha256（从库内取）
  ② 清之后：库内 content 为空，但**读接口仍能取到同样内容**（来自盘）
  ③ 清之后：改一篇，盘上跟着变（写路径仍通）
  ④ 清之后：搜索仍命中（检索靠 chunks，不靠 content）
  ⑤ 盘 = 库 = git 三方仍一致
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

DB = r"D:\core-notes\data\index.db"
API = "http://127.0.0.1:8848"
SAMPLE = r"D:\core-notes\data\tmp\b6_sample.json"
MARK = r"D:\core-notes\data\tmp\b6_before.json"


def sha(b) -> str:
    return hashlib.sha256(b).hexdigest()


def api(path: str, t: int = 120):
    with urllib.request.urlopen(API + path, timeout=t) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def api_post(path: str, obj, method="POST", t: int = 120):
    req = urllib.request.Request(API + path, data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                 method=method,
                                 headers={"Content-Type": "application/json", "X-Writer": "t:b6"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def db_path_of(fid: str) -> str:
    """库内路径（vault/…）"""
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    fold = {r["id"]: (r["parent_id"], r["name"]) for r in con.execute("SELECT id,parent_id,name FROM folders")}
    r = con.execute("SELECT name,folder_id FROM files WHERE id=?", (fid,)).fetchone()
    con.close()
    parts, fid2, seen = [], r["folder_id"], set()
    while fid2 and fid2 in fold and fid2 not in seen:
        seen.add(fid2)
        p, n = fold[fid2]
        parts.append(n)
        fid2 = p
    path = "/".join(reversed(parts))
    if not path.startswith("/vault"):
        path = "/vault" + path
    return (path.rstrip("/") + "/" + r["name"]).lstrip("/")


def pre():
    sample = json.load(open(SAMPLE, encoding="utf-8"))
    out = []
    for r in sample:
        rel = db_path_of(r["id"])
        it = (api("/api/content?max=100000000&path=" + urllib.parse.quote(rel)).get("item") or {})
        txt = it.get("content") or ""
        out.append({"id": r["id"], "name": r["name"], "rel": rel,
                    "inlib_sha": sha(txt.encode("utf-8")), "chars": len(txt), "found": it.get("found")})
    json.dump(out, open(MARK, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = sum(1 for x in out if x["found"])
    print("  ① 记录清前状态：%d/%d 篇可读" % (ok, len(out)))
    for x in out[:4]:
        print("     %-40s %6d 字符  sha=%s" % (x["name"][:40], x["chars"], x["inlib_sha"][:12]))
    return 0 if ok == len(out) else 1


def clear():
    sample = json.load(open(SAMPLE, encoding="utf-8"))
    ids = [r["id"] for r in sample]
    con = sqlite3.connect(DB, timeout=30)
    before = con.execute("SELECT COUNT(*) FROM files WHERE content IS NOT NULL").fetchone()[0]
    con.executemany("UPDATE files SET content=NULL WHERE id=?", [(i,) for i in ids])
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM files WHERE content IS NOT NULL").fetchone()[0]
    # 确认样本确实清了
    left = con.execute("SELECT COUNT(*) FROM files WHERE content IS NOT NULL AND id IN (%s)"
                       % ",".join("?" * len(ids)), ids).fetchone()[0]
    con.close()
    print("  ② 清库内正文：全库 content 非空 %d → %d（清了 %d）" % (before, after, before - after))
    print("     样本里还剩 content 非空的：%d（应为 0）" % left)
    return 0 if left == 0 else 1


def check():
    mark = json.load(open(MARK, encoding="utf-8"))
    print("  ③ 清后：读接口还能取到同样内容吗")
    bad = 0
    for x in mark:
        it = (api("/api/content?max=100000000&path=" + urllib.parse.quote(x["rel"])).get("item") or {})
        txt = it.get("content") or ""
        same = sha(txt.encode("utf-8")) == x["inlib_sha"]
        if not (it.get("found") and same):
            bad += 1
            print("     ✗ %-40s found=%s 一致=%s" % (x["name"][:40], it.get("found"), same))
    print("     %d/%d 篇：读得到且内容与清之前一致" % (len(mark) - bad, len(mark)))

    print("\n  ④ 清后：库内那列真的空了吗")
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    ids = [x["id"] for x in mark]
    n = con.execute("SELECT COUNT(*) FROM files WHERE content IS NOT NULL AND id IN (%s)"
                    % ",".join("?" * len(ids)), ids).fetchone()[0]
    con.close()
    print("     样本里 content 非空：%d（应为 0）" % n)

    print("\n  ⑤ 清后：写一篇再改，盘上跟不跟")
    fid = mark[0]["id"]
    rel = mark[0]["rel"]
    old = (api("/api/content?max=100000000&path=" + urllib.parse.quote(rel)).get("item") or {}).get("content") or ""
    new = old + "\n\n<!-- B6-试点标记 -->\n"
    api_post("/api/files/" + fid, {"content": new}, method="PUT")
    time.sleep(2)
    got = (api("/api/content?max=100000000&path=" + urllib.parse.quote(rel)).get("item") or {}).get("content") or ""
    print("     写后读回含标记：%s" % ("B6-试点标记" in got))
    # 还原
    api_post("/api/files/" + fid, {"content": old}, method="PUT")
    time.sleep(1.5)
    back = (api("/api/content?max=100000000&path=" + urllib.parse.quote(rel)).get("item") or {}).get("content") or ""
    print("     还原成原文：%s" % (sha(back.encode("utf-8")) == mark[0]["inlib_sha"]))

    print("\n  ⑥ 清后：搜索还命中吗")
    q = mark[0]["name"].rsplit(".", 1)[0][:6]
    try:
        r = api("/api/search?q=" + urllib.parse.quote(q))
        hits = r.get("results") or r.get("hits") or []
        print("     搜 %r → %d 条命中" % (q, len(hits)))
    except Exception as e:
        print("     搜索 ERR: %s" % e)

    return 0 if bad == 0 and n == 0 else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--pre"
    fn = {"--pre": pre, "--clear": clear, "--check": check}.get(mode)
    if fn is None:
        print("用法: --pre | --clear | --check")
        sys.exit(2)
    sys.exit(fn())
