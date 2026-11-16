"""增量更新验收：稳定块 ID（内容寻址）+ 向量只重算真正变的块。

背景（2026-09-18）：续用旧向量原来按位置（`file_id:seq`）配对，
**在中间插一段会让后面所有块白重算**。改成按内容配对后应当只重算真正变的那块。
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBP = os.path.join(HERE, "data", "index.db")
OK = True


def call(path, method="GET", body=None, timeout=300):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def idmap(fid):
    """库里这篇的块：{heading: block_id}"""
    c = sqlite3.connect(DBP)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT heading, block_id FROM chunks WHERE file_id=?", (fid,)).fetchall()
    c.close()
    return {r["heading"]: (r["block_id"] or "") for r in rows}


def ids_only(fid):
    c = sqlite3.connect(DBP)
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT block_id FROM chunks WHERE file_id=?", (fid,)).fetchall()
    c.close()
    return [r["block_id"] or "" for r in rows]


def sec(i):
    return "## 小节 %d\n第 %d 段的内容，各自不同，便于按内容配对。\n\n" % (i, i)


def note(secs):
    return "# 增量测试\n\n" + "".join(sec(i) for i in secs)


def put(fid, text):
    st, r = call("/api/files/%s" % fid, "PUT", {"content": text})
    return (r.get("saved", {}).get("vectors") or {}).get("indexed")


def wait_vectors(timeout=600):
    """等到后台补向量线程把 pending 清空（2026-09-30：>4 块的保存改成异步补了，
    不等它清完，"只重算 1 块"这类断言就会被"上一篇还没补完的块"污染）。"""
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < timeout:
        h = call("/api/health")[1]
        vw = h.get("vector_worker") or {}
        if not (h.get("pending_vectors") or 0) and not vw.get("running"):
            return True
        _t.sleep(2)
    return False


BEFORE = call("/api/health")[1]["stats"]["files"]
print("① 建一篇 5 小节的笔记")
st, made = call("/api/files", "POST", {"name": "zz-增量-测试.md", "content": note(range(5))})
fid = made.get("file_id")
check("建好了", st == 201 and fid, made.get("error"))
ids = ids_only(fid)
check("块都有 block_id（内容寻址）", ids and all(ids), ids)
check("同一篇里 block_id 不重复", len(set(ids)) == len(ids), len(ids))
m0 = idmap(fid)

print("①b 等后台把这篇的向量补完（异步之后这一步必须等）")
check("后台补完（pending 归零）", wait_vectors(), call("/api/health")[1].get("pending_vectors"))

print("② 改一小节 → 只重算那 1 块")
cur = note(range(5)).replace("第 3 段的内容", "第 3 段的内容（改过）", 1)
idx = put(fid, cur)
check("重算向量数 = 1", idx == 1, "indexed=%s" % idx)
m1 = idmap(fid)
gone = [k for k in ("小节 0", "小节 1", "小节 2", "小节 4") if m1.get(k) != m0.get(k)]
check("没改的小节 block_id 原样不动", not gone, gone)

print("③ 中间插一小节 → 仍只重算那 1 块（旧实现这一步是 6 块）")
cur2 = cur.replace("## 小节 2\n", "## 新插的小节\n这是插进来的新内容。\n\n## 小节 2\n", 1)
idx2 = put(fid, cur2)
check("重算向量数 = 1", idx2 == 1, "indexed=%s" % idx2)
m2 = idmap(fid)
moved = [k for k in ("小节 0", "小节 1", "小节 2", "小节 3", "小节 4")
         if m2.get(k) != m1.get(k)]
check("插入后：原有小节的 block_id 全部没变", not moved, moved)
check("新插入的小节有自己的 block_id", bool(m2.get("新插的小节")), m2.get("新插的小节"))

print("④ 删一小节 → 其余块一块都不重算")
cur3 = cur2.replace("## 小节 4\n第 4 段的内容，各自不同，便于按内容配对。\n\n", "", 1)
idx3 = put(fid, cur3)
check("重算向量数 = 0", idx3 == 0, "indexed=%s" % idx3)
m3 = idmap(fid)
check("小节 4 的块没了", "小节 4" not in m3)
stay = [k for k in ("小节 0", "小节 1", "小节 2", "小节 3") if m3.get(k) != m2.get(k)]
check("其余块 block_id 照旧", not stay, stay)

print("⑤ 内容寻址：同样的内容 → 同样的 block_id")
st, made2 = call("/api/files", "POST", {"name": "zz-增量-副本.md", "content": cur3})
fid2 = made2.get("file_id")
m4 = idmap(fid2)
check("另一篇里同样的小节，block_id 相同",
      bool(m4.get("小节 1")) and m4.get("小节 1") == m3.get("小节 1"),
      "%s vs %s" % (m4.get("小节 1"), m3.get("小节 1")))

print("⑥ 重建：没变就跳过（秒级），变了只重建改过的那几篇")
import time as _t
_gseq0 = call("/api/events?limit=1")[1]["last_gseq"]     # 位点：重扫前后比一比事件
_t0 = _t.time()
st, r1 = call("/api/reindex", "POST", {"reason": "smoke-incremental"})
secs = _t.time() - _t0
rr = r1.get("reindex") or {}
check("什么都没变 → 全跳过", rr.get("files") == 0 and rr.get("skipped", 0) > 0, rr)
# 2026-09-30：闸门 30 → 45 秒。全量重建 ~150 秒，45 秒仍然把"增量/全量"分得很开；
# 放宽是因为**后台补向量线程**可能正好在跑一批（≤2 块 ≈ 10 秒），它不该让这条断言翻红。
check("而且很快（< 45 秒；全量重建要 150 秒上下）", secs < 45, "%.1f 秒" % secs)
# 2026-10-03：这条曾经 48 秒。根因不是扫描（本地 0.2s），是**重扫时给每一篇 rag=off / 锁着的
# 私密篇各写一条空的 `chunk.removed {chunks:0}`**，而 events 表 12.5 万行又没给 source 建索引
# ——每次取号 `SELECT MAX(seq) WHERE source=?` 全表扫 62.7ms。两处都修了（indexer 不再写空事件；
# events(source,seq) 建索引，实测 62.7ms → 0.0ms，整通 48.3s → 1.7s）。这条守着"别再冒空事件"。
_ev = call("/api/events?after=%d&types=chunk.removed&limit=200" % _gseq0)[1]["events"]
check("重扫没冒空的 chunk.removed（那几百篇本来就没块）", len(_ev) == 0, len(_ev))
check("接口标了这是增量重建", r1.get("only_changed") is True, r1.get("only_changed"))
st, made3 = call("/api/files", "POST", {"name": "zz-增量-重建.md", "content": note([0, 1])})
fid3 = made3["file_id"]
call("/api/files/%s" % fid3, "PUT", {"content": note([0, 1, 2])})
st, r2 = call("/api/reindex", "POST", {"reason": "smoke-incremental-2"})
rr2 = r2.get("reindex") or {}
check("只重建变过的那几篇（其余全跳过）",
      rr2.get("files", 0) <= 2 and rr2.get("skipped", 0) >= 1000, rr2)
call("/api/files/" + fid3, "DELETE")

print("⑦ 清场")
for x in (fid, fid2):
    if x:
        call("/api/files/" + x, "DELETE")
c = sqlite3.connect(DBP)
c.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
c.commit()
c.close()
after = call("/api/health")[1]["stats"]["files"]
check("文件数复原", after == BEFORE, "%s → %s" % (BEFORE, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
