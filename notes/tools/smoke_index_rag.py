"""文档线第 1 步（库侧）自测：按磁盘路径索引 + 批量进/出检索。

  python tools/smoke_index_rag.py

验四件事：
  ① POST /api/import 能把磁盘上的目录/单个文件索引进库，**默认不进检索**（rag=off）
  ② 重复导入按 content_hash 去重（skipped）
  ③ POST /api/rag/batch 批量进 → 真的切块（chunks>0）；批量出 → 块清掉
  ④ GET /api/rag/list 能按 on/off 列出（带库内路径）
跑完把造出来的东西删干净，篇数回到开工前的数。
"""
import io
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request

LIB = os.environ.get("AINOTES_URL", "http://127.0.0.1:8848")
OK = True


def call(path, body=None, method="GET", timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(LIB + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def files_count():
    st, d = call("/api/health")
    return d.get("stats", {}).get("files")


before = files_count()
print("开工前库内篇数：", before)

tmp = tempfile.mkdtemp(prefix="zz-导入测试-")
os.makedirs(os.path.join(tmp, "子目录"), exist_ok=True)
io.open(os.path.join(tmp, "甲.md"), "w", encoding="utf-8").write("# 甲\n\n这是导入测试的第一篇。\n")
io.open(os.path.join(tmp, "子目录", "乙.md"), "w", encoding="utf-8").write("# 乙\n\n嵌套目录里的一篇。\n")
single = os.path.join(tempfile.gettempdir(), "zz-单文件导入.md")
io.open(single, "w", encoding="utf-8").write("# 单文件\n\n直接丢进来的一份。\n")

created_ids, folder_ids = [], []
try:
    print("① 目录导入（默认不进检索）")
    st, d = call("/api/import", {"src": tmp, "under": "zz-导入测试"}, "POST")
    check("接口在且导入成功", st == 200 and d.get("ok") is True and d.get("imported") == 2, d)
    check("默认 rag_off=True", d.get("rag_off") is True)
    check("篇数 +2", files_count() == before + 2, files_count())

    st, tree = call("/api/tree?depth=2")
    hit = json.dumps(tree, ensure_ascii=False)
    check("出现在库页面的对应文件夹里", "zz-导入测试" in hit and "甲.md" in hit, "")

    st, lst = call("/api/rag/list?on=0&limit=2000")
    names = {f["name"]: f for f in lst.get("files", [])}
    check("rag/list 能列出它们（off）", "甲.md" in names and "乙.md" in names,
          [f["path"] for f in lst.get("files", [])][:3])
    check("带库内路径", all("/" in (names[n]["path"] or "") or names[n]["dir"] == "zz-导入测试"
                            for n in ("甲.md", "乙.md") if n in names))
    created_ids = [names[n]["id"] for n in ("甲.md", "乙.md") if n in names]

    print("② 重复导入应被去重")
    st, d = call("/api/import", {"src": tmp, "under": "zz-导入测试"}, "POST")
    check("第二次导入 skipped=2", d.get("skipped") == 2 and d.get("imported") == 0, d)
    check("篇数没变", files_count() == before + 2, files_count())

    print("③ 单个文件导入（拖进来的最小单位）")
    st, d = call("/api/import", {"src": single, "under": "zz-导入测试"}, "POST")
    check("单文件导入成功", st == 200 and d.get("imported") == 1, d)
    st, lst = call("/api/rag/list?on=0&limit=2000")
    one = [f["id"] for f in lst.get("files", []) if f["name"] == "zz-单文件导入.md"]
    check("它在库里且 rag=off", len(one) == 1, one)
    created_ids += one

    print("④ 批量进检索 → 真的切块")
    st, d = call("/api/rag/batch", {"ids": created_ids, "on": True}, "POST")
    check("批量进：count=3 且 chunk>0", d.get("count") == 3 and d.get("chunks", 0) > 0, d)
    st, lst = call("/api/rag/list?on=1&limit=3000")
    inrag = {f["name"] for f in lst.get("files", [])}
    check("rag/list(on=1) 里能看到", {"甲.md", "乙.md", "zz-单文件导入.md"} <= inrag)

    print("⑤ 批量出检索 → 块清掉")
    st, d = call("/api/rag/batch", {"ids": created_ids, "on": False}, "POST")
    check("批量出：count=3", d.get("count") == 3, d)
    st, lst = call("/api/rag/list?on=0&limit=2000")
    off = {f["name"] for f in lst.get("files", [])}
    check("又回到 off 名单里", {"甲.md", "乙.md"} <= off)

    print("⑥ 按文件夹批量（folder_id + recursive）")
    st, folders = call("/api/folders")
    fid = next((f["id"] for f in folders.get("folders", []) if f.get("name") == "zz-导入测试"), None)
    check("找到测试文件夹", bool(fid), fid)
    if fid:
        folder_ids.append(fid)
        st, d = call("/api/rag/batch", {"folder_id": fid, "on": True}, "POST")
        check("按文件夹批量进（应覆盖 3 个文件）", d.get("count") == 3, d)
        call("/api/rag/batch", {"folder_id": fid, "on": False}, "POST")
finally:
    print("⑦ 清场")
    for i in created_ids:
        call("/api/files/" + i, method="DELETE")
    # 子文件夹也要删（不然父文件夹非空删不掉，库文件夹里就留残渣）：按路径深到浅全删
    st, fs = call("/api/folders")
    victims = [f for f in fs.get("folders", [])
               if "zz-导入测试" in (f.get("path") or "") or f.get("id") in folder_ids]
    for f in sorted(victims, key=lambda x: -len(x.get("path") or "")):
        call("/api/folders/" + f["id"], method="DELETE")
    for p in (single,):
        try:
            os.remove(p)
        except Exception:
            pass
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    time.sleep(0.5)
    after = files_count()
    check("篇数回到开工前", after == before, "before=%s after=%s" % (before, after))

print("=" * 60)
print("库侧导入/检索自测：", "全部通过" if OK else "有失败")
sys.exit(0 if OK else 1)
