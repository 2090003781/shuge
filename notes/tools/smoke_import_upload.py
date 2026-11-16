# -*- coding: utf-8 -*-
"""浏览器选文件 → 按字节导入（POST /api/import/upload）的验收（辞玖 2026-09-28）。

不用磁盘路径：直接像页面那样把文件内容 base64 传上去，验四件事：
  ① 单文件能进库，内容对得上
  ② 带 rel_dir 的能保留目录层级
  ③ 同名再传一次 = 覆盖更新（updated），不是新建重复
  ④ 清场：删掉这两篇（连带自建的空文件夹），篇数复原
"""
import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

# 可选位置参数：指向别的服务（临时服务可加载**新代码**，验收改动必须能换地址）
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = 0
FAIL = []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    print("  %s %s%s" % ("✅" if cond else "❌", name, "" if cond else "  | " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "X-Writer": "tool:upload-acc"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def tree_paths():
    _, d = call("/api/tree")
    out = []

    def walk(n, pre):
        for f in n.get("files") or []:
            out.append((f["id"], pre + "/" + f["name"]))
        for c in n.get("children") or []:
            walk(c, pre + "/" + (c.get("name") or ""))
    walk(d["tree"], "")
    return out


def main():
    before = call("/api/health")[1]["stats"]["files"]
    names = ["zz-上传-甲.md", "zz-上传-乙.md"]
    made, folders = [], []

    def cleanup():
        for fid, path in tree_paths():
            if os.path.basename(path) in names:
                call("/api/files/" + fid, "DELETE")
        _, fs = call("/api/folders")
        for f in sorted((fs.get("folders") or []), key=lambda x: -len(x.get("path") or "")):
            if "zz-上传" in (f.get("path") or ""):
                call("/api/folders/" + f["id"], "DELETE")

    cleanup()
    try:
        print("① 单文件：按字节传上去就能进库")
        st, d = call("/api/import/upload", "POST", {"files": [
            {"name": names[0], "content_b64": b64("# 甲\n\n字节传上来的。\n")}], "rag": False})
        check("HTTP 200 且 ok", st == 200 and d.get("ok") is True, d)
        check("imported=1", int(d.get("imported") or 0) == 1, d.get("imported"))
        hits = [(i, p) for i, p in tree_paths() if os.path.basename(p) == names[0]]
        check("树里能找到它", bool(hits), [p for _i, p in tree_paths()][-5:])
        if hits:
            made.append(hits[0][0])
            _, one = call("/api/files/" + hits[0][0])
            check("正文一致", "字节传上来的" in (one.get("file") or {}).get("content", ""),
                  (one.get("file") or {}).get("content", "")[:40])

        print("② 带 rel_dir：目录层级保留")
        st, d2 = call("/api/import/upload", "POST", {"files": [
            {"name": names[1], "rel_dir": "zz-上传-子/更里一层",
             "content_b64": b64("# 乙\n\n在子目录里。\n")}], "rag": False, "under": "vault"})
        check("HTTP 200 且 ok", st == 200 and d2.get("ok") is True, d2)
        deep = [p for _i, p in tree_paths()
                if os.path.basename(p) == names[1] and "zz-上传-子" in p and "更里一层" in p]
        check("层级对：vault/zz-上传-子/更里一层/…", bool(deep), deep)
        _, fs = call("/api/folders")
        folders = [f["id"] for f in (fs.get("folders") or []) if "zz-上传" in (f.get("path") or "")]
        check("库里的文件夹也建出来了", bool(folders), folders)

        print("③ 同名再传 = 覆盖更新，不是新建重复")
        st, d3 = call("/api/import/upload", "POST", {"files": [
            {"name": names[0], "content_b64": b64("# 甲改\n\n第二版。\n")}], "rag": False})
        check("updated=1", int(d3.get("updated") or 0) == 1, d3)
        n_same = len([p for _i, p in tree_paths() if os.path.basename(p) == names[0]])
        check("同名只有一篇（没重复）", n_same == 1, n_same)
        hits2 = [(i, p) for i, p in tree_paths() if os.path.basename(p) == names[0]]
        if hits2:
            _, one2 = call("/api/files/" + hits2[0][0])
            check("内容换成了第二版", "第二版" in (one2.get("file") or {}).get("content", ""),
                  (one2.get("file") or {}).get("content", "")[:40])

        print("④ 落盘：导入件必须在库文件夹里（2026-09-30 体检修的那条）")
        _, h = call("/api/health")
        lib = (h.get("manifest") or {}).get("library_dir") or ""
        check("health 报了库文件夹", bool(lib), lib)
        if lib:
            for nm in names:
                hit = [p for _i, p in tree_paths() if os.path.basename(p) == nm]
                rel = (hit[0] if hit else "").lstrip("/")
                if rel.startswith("vault/"):
                    rel = rel[len("vault/"):]
                full = os.path.join(lib, rel.replace("/", os.sep)) if rel else ""
                check("库文件夹里有 %s" % nm, bool(full) and os.path.isfile(full), full)

        print("⑤ 边界：空 files / 缺 content_b64 要如实报错")
        st, d4 = call("/api/import/upload", "POST", {"files": []})
        check("空 files → 400 且带 error", st == 400 and bool(d4.get("error")), (st, d4))
        st, d5 = call("/api/import/upload", "POST", {"files": [{"name": "x.md"}]})
        check("缺内容 → failed=1 并写进 errors", st == 400 and int(d5.get("failed") or 0) == 1
              and bool(d5.get("errors")), (st, d5.get("errors")))
    finally:
        cleanup()
        import time
        time.sleep(0.3)
        after = call("/api/health")[1]["stats"]["files"]
        check("清场：篇数复原", after == before, "%s → %s" % (before, after))

    print("\n========  结论: %d 通过 / %d 失败" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
