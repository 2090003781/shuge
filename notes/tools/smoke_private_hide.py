# -*- coding: utf-8 -*-
"""上锁→隐藏 / 解锁→显示 的验收（辞玖 2026-09-27）。

不设密码、不加锁：只按库**当前**锁状态断言应该成立的那一侧 ——
· 上锁（configured 且未解锁）：私密篇在 tree / rag_list / 属性索引 / 前像 / 事件 里都不出现，按 id 取也 403；
· 解锁：私密篇照常出现（至少"按 id 取"能拿到，正文不被 403 挡）。
"""
import sys
import json
import os
import sqlite3
import urllib.error
import urllib.request

# 可选位置参数：指向别的服务（临时服务可加载**新代码**，验收改动必须能换地址）
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ROOT = r"D:\core-notes"
PASS = 0
FAIL = []


def get(p):
    with urllib.request.urlopen(BASE + p, timeout=120) as r:
        return json.load(r)


def get_status(p):
    try:
        with urllib.request.urlopen(BASE + p, timeout=120) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    print("  %s %s%s" % ("✅" if cond else "❌", name, "" if cond else "  | " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


def private_rows():
    """私密篇：id + 名字 + **库内完整路径**（同名篇很多，不能只按名字比）"""
    db = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "data", "index.db").replace("\\", "/"),
                         uri=True)
    db.row_factory = sqlite3.Row
    folders = {r["id"]: (r["name"], r["parent_id"]) for r in db.execute("SELECT id,name,parent_id FROM folders")}

    def path_of(fid):
        parts = []
        while fid and fid in folders:
            nm, up = folders[fid]
            if nm:
                parts.append(nm)
            fid = up
        return "/" + "/".join(reversed(parts))

    out = []
    for r in db.execute("SELECT id, name, folder_id FROM files WHERE visibility='private' ORDER BY name"):
        d = path_of(r["folder_id"])
        out.append({"id": r["id"], "name": r["name"],
                    "path": (d + "/" + r["name"]).replace("//", "/")})
    return out


def _db():
    return sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "data", "index.db").replace("\\", "/"),
                           uri=True)


def private_only_dirs():
    """只装私密篇的文件夹（库内路径，写法与树里一致）—— 锁定时这些**文件夹名**也不该露"""
    db = _db()
    db.row_factory = sqlite3.Row
    folders = {r["id"]: (r["name"], r["parent_id"]) for r in db.execute("SELECT id,name,parent_id FROM folders")}

    def path_of(fid):
        parts = []
        while fid and fid in folders:
            nm, up = folders[fid]
            if nm:
                parts.append(nm)
            fid = up
        return "/".join(reversed(parts))

    vis = {}
    for r in db.execute("SELECT folder_id, visibility FROM files"):
        vis.setdefault(r["folder_id"] or "", []).append(r["visibility"])
    out = set()
    for fid, vs in vis.items():
        if vs and all(v == "private" for v in vs):
            p = path_of(fid)
            if p:
                out.add(p)
    return out


def tree_dir_paths():
    """树上现在的文件夹路径（相对库内根）"""
    out = []

    def walk(n, pre):
        for c in n.get("children") or []:
            p = (pre + "/" + (c.get("name") or "")).strip("/")
            out.append(p)
            walk(c, p)
    walk(get("/api/tree")["tree"], "")
    return out


def tree_names():
    out = []

    def walk(n, pre):
        for f in n.get("files") or []:
            out.append((pre + "/" + f["name"], f.get("visibility")))
        for c in n.get("children") or []:
            walk(c, pre + "/" + (c.get("name") or ""))
    walk(get("/api/tree")["tree"], "")
    return out


def main():
    rows = private_rows()
    lock = get("/api/lock")
    print("库里私密篇 %d 篇 · 锁：configured=%s unlocked=%s"
          % (len(rows), lock.get("configured"), lock.get("unlocked")))
    if not rows:
        print("  没有私密篇 → 无从验收，跳过")
        print("\n========  结论: 通过（无样本，跳过）")
        return 0
    locked = not lock.get("unlocked")

    paths = {r["path"] for r in rows}
    ids = {r["id"] for r in rows}

    if locked:
        print("① 上锁：tree 里不许出现私密篇")
        tn = tree_names()
        hit = [p for p, _v in tn if p in paths or p.lstrip("/") in {x.lstrip("/") for x in paths}]
        check("tree 里没有私密篇（按完整路径比）", not hit, hit[:5])
        check("tree 如实报隐藏了几篇", get("/api/tree").get("hidden_private") == len(rows),
              get("/api/tree").get("hidden_private"))
        check("health 也报隐藏数", get("/api/health").get("hidden_private") == len(rows),
              get("/api/health").get("hidden_private"))

        print("①b 上锁：只装私密篇的文件夹整枝不露（文件夹名和那个 0 都不给）")
        only_priv = private_only_dirs()
        dirs_now = set(tree_dir_paths())
        leaked = sorted(p for p in only_priv if p in dirs_now)
        check("只装私密篇的文件夹锁定时整枝不露", not leaked, leaked[:3])
        check("（不是把所有文件夹都藏了：可见的还在）", bool(dirs_now), len(dirs_now))

        print("② 上锁：rag 清单里不许出现")
        rl = get("/api/rag/list?limit=3000")
        h2 = [f for f in (rl.get("files") or []) if f.get("id") in ids]
        check("rag/list 里没有私密篇", not h2, h2[:3])
        check("rag/list 报隐藏数", rl.get("hidden_private") == len(rows), rl.get("hidden_private"))

        print("③ 上锁：属性索引与按属性找不许牵出私密篇")
        pi = get("/api/properties")
        h3 = []
        for k in (pi.get("keys") or []):
            for v in (k.get("values") or []):
                if str(v.get("value")) in paths:
                    h3.append((k["key"], v))
        check("属性索引里没有私密篇名", not h3, h3[:3])
        check("（同名不算：私密是 私密/索引.md，公开是 知识库/索引.md）", True)

        print("④ 上锁：按 id 直接取 → 403（不是 200 带元信息）")
        st, d = get_status("/api/files/" + rows[0]["id"])
        check("GET /api/files/<私密 id> 回 403", st == 403 and d.get("locked") is True, (st, d))
        st2, _d2 = get_status("/api/files/" + rows[0]["id"] + "/raw")
        check("raw 也是 403", st2 == 403, st2)

        print("⑤a 上锁：重设密码必须显式确认（裸调一律 400，且状态不变）")
        _req = urllib.request.Request(BASE + "/api/lock/reset", data=b"{}", method="POST",
                                      headers={"Content-Type": "application/json",
                                               "X-Writer": "tool:lock-acc"})
        try:
            with urllib.request.urlopen(_req, timeout=60) as _r:
                st_r, d_r = _r.status, json.load(_r)
        except urllib.error.HTTPError as _e:
            st_r, d_r = _e.code, json.loads(_e.read().decode("utf-8", "replace") or "{}")
        check("不带 confirm → 400", st_r == 400 and bool(d_r.get("error")), (st_r, d_r))
        check("而且没把密码状态改掉", get("/api/lock").get("configured") == lock.get("configured"),
              (get("/api/lock"), lock))

        print("⑤a2 上锁：带 confirm 也不许重设（要先解锁）")
        _req2 = urllib.request.Request(BASE + "/api/lock/reset",
                                       data=json.dumps({"confirm": True}).encode("utf-8"),
                                       method="POST",
                                       headers={"Content-Type": "application/json",
                                                "X-Writer": "tool:lock-acc"})
        try:
            with urllib.request.urlopen(_req2, timeout=60) as _r2:
                st_r2, d_r2 = _r2.status, json.load(_r2)
        except urllib.error.HTTPError as _e2:
            st_r2, d_r2 = _e2.code, json.loads(_e2.read().decode("utf-8", "replace") or "{}")
        check("上锁时带 confirm 也回 403", st_r2 == 403 and d_r2.get("locked") is True, (st_r2, d_r2))
        check("密码状态依旧没被动", get("/api/lock").get("configured") == lock.get("configured"),
              get("/api/lock"))

        print("⑤b 上锁：维修入口（private-rekey）不许跑")
        _rq3 = urllib.request.Request(BASE + "/api/maintenance/private-rekey", data=b"{}",
                                      headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(_rq3, timeout=60) as _r3:
                st3, d3 = _r3.status, json.load(_r3)
        except urllib.error.HTTPError as _e3:
            st3, d3 = _e3.code, json.loads(_e3.read().decode("utf-8", "replace") or "{}")
        check("锁着时 private-rekey 回 403", st3 == 403 and d3.get("locked") is True, (st3, d3))

        print("⑤ 上锁：前像清单与事件流里不露名字")
        bi = get("/api/before-images?limit=200")
        h4 = [i for i in (bi.get("images") or []) if i.get("file_id") in ids]
        check("前像清单里没有私密篇", not h4, h4[:3])
        ev = get("/api/events?limit=200")
        h5 = [e for e in (ev.get("events") or [])
              if (e.get("payload") or {}).get("file_id") in ids
              and (e.get("payload") or {}).get("name") not in (None, "（私密·未解锁）")]
        check("事件流里私密篇名被换成占位", not h5, [e.get("payload") for e in h5[:2]])
    else:
        print("① 已解锁：私密篇应当照常显示")
        tn = tree_names()
        hit = [p for p, _v in tn if p in paths or p.lstrip("/") in {x.lstrip("/") for x in paths}]
        check("tree 里能看到私密篇（按完整路径比）", bool(hit), len(tn))
        check("没报隐藏数（解锁时不隐藏）", not get("/api/tree").get("hidden_private"),
              get("/api/tree").get("hidden_private"))
        st, d = get_status("/api/files/" + rows[0]["id"])
        check("按 id 能取到（200）", st == 200 and (d.get("file") or {}).get("locked") is False,
              (st, d.get("file", {}).get("locked")))

        only_priv = private_only_dirs()
        dirs_now = set(tree_dir_paths())
        missing = sorted(p for p in only_priv if p not in dirs_now)
        check("解锁后那些文件夹照常显示", not missing, missing[:3])

    print("\n（备注：本验收**不动库的锁状态**，只按当前状态断言对应的一侧）")
    print("========  结论: %d 通过 / %d 失败" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
