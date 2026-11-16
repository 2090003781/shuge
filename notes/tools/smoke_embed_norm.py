"""验证"改名不再动向量"（以及"正文真变了仍然要重算"这条底线）。

做法：
  ① 建目标 A + 引用方 B（B 正文里 @A）→ 给 B 的块嵌入向量
  ② 记下 B 那一块的向量字节
  ③ 改名 A（会改 B 的正文）→ 向量的**字节必须一模一样**，且没有新嵌入
  ④ 反例：改 B 的正文（不是提及名）→ 向量必须被重算（否则就是过度保留）
自建自清。
"""
import hashlib
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True
A0, A1 = "zz-向量甲.md", "zz-向量甲改名.md"
B = "zz-向量乙.md"
D0 = A0[:-3]


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


sys.path.insert(0, "src")
from db import Database                     # noqa: E402
from retriever import VectorIndexer         # noqa: E402
from mentions import mask_mentions          # noqa: E402
from embedinput import embed_text, SPEC     # noqa: E402

print("ⓐ 掩码只吃「真引用」（伪标签 @event/@tool 不该被抹掉）")
_db = Database()
_look = _db.ref_resolver()
_real = ""
for _r in _db.conn.execute("SELECT name FROM files"):
    _nm = _r["name"] or ""
    _nm = _nm[:-3] if _nm.lower().endswith(".md") else _nm
    if _nm and _look(_nm):
        _real = _nm
        break
check("库里能找到一个可解析的名字", bool(_real), _real)
check("真引用被掩码", "（引用）" in mask_mentions("见 @%s 那篇" % _real, _look))
check("伪标签不被掩码（带解析器）",
      "@event" in mask_mentions("12:53 | @event:SessionStart", _look),
      mask_mentions("12:53 | @event:SessionStart", _look))
check("不带解析器时保持老行为（全掩码）",
      "（引用）" in mask_mentions("12:53 | @event:SessionStart"))
check("嵌入文本也只掩真引用",
      "@event" in embed_text("| @event:Start", None, _look)
      and "（引用）" in embed_text("见 @%s" % _real, None, _look))
check("规格已升到 d2", SPEC == "d2", SPEC)
_db.close()

made = []
files_before = call("/api/health")[1]["stats"]["files"]


def vec_of(db, fid):
    """该文件第一块的向量字节哈希"""
    r = db.conn.execute(
        "SELECT v.vec FROM vectors v JOIN chunks c ON c.id=v.chunk_id"
        " WHERE c.file_id=? ORDER BY c.seq LIMIT 1", (fid,)).fetchone()
    return hashlib.sha256(r["vec"]).hexdigest()[:16] if r else None


try:
    print("① 建两篇 + 给引用方嵌入")
    a = call("/api/files", "POST", {"name": A0, "content": "# 甲\n\n被引用的那篇。\n"})[1]["file_id"]
    made.append(a)
    b = call("/api/files", "POST", {"name": B, "content": "# 乙\n\n见 @%s 那篇，正文就这样。\n" % D0})[1]["file_id"]
    made.append(b)
    db = Database()
    vi = VectorIndexer(db)
    print("   嵌入：", vi.index_files([a, b]))
    v_before = vec_of(db, b)
    check("引用方有了向量", bool(v_before), v_before)
    pend_before = call("/api/health")[1]["pending_vectors"]

    print("② 纯函数：只有提及名字不同 → 嵌入文本应完全相同")
    t1 = mask_mentions("见 @甲笔记 那篇")
    t2 = mask_mentions("见 @甲笔记改了个名字 那篇")
    check("不同提及名 → 同一段嵌入文本", t1 == t2, (t1, t2))

    print("③ 改名（会改 B 的正文）→ 向量必须原封不动")
    st, r = call("/api/files/%s/rename" % a, "POST", {"name": A1})
    vv = (r.get("refs") or {}).get("vectors") or {}
    check("改名接口 200", st == 200, r.get("file"))
    check("没有为改名重算向量（indexed=0）", vv.get("indexed", 0) == 0, vv)
    body = call("/api/files/" + b)[1]["file"]["content"]
    check("正文确实被改了（@新名）", ("@%s" % A1[:-3]) in body, body[:60])
    v_after = vec_of(db, b)
    check("那一块的向量字节完全没变", v_before == v_after, "%s → %s" % (v_before, v_after))
    check("库没有新增向量缺口",
          call("/api/health")[1]["pending_vectors"] <= pend_before,
          "%s vs %s" % (call("/api/health")[1]["pending_vectors"], pend_before))

    print("④ 反例：改正文本身（不是提及名）→ 必须重算（保存时就地补）")
    st, sv = call("/api/files/" + b, "PUT", {"content": body + "\n又加了一句别的话。\n"})
    vsaved = ((sv.get("saved") or {}).get("vectors") or {})
    check("保存时就地补了向量（不再先留缺口）", vsaved.get("indexed", 0) >= 1, vsaved)
    miss = VectorIndexer(Database()).pending_of_files([b])
    check("补完没有缺口", len(miss) == 0, len(miss))
    check("补算后向量换了新的", vec_of(Database(), b) != v_after, "已更新")
finally:
    print("⑤ 清场")
    for fid in made:
        call("/api/files/" + fid, "DELETE")
    d = Database()
    d.conn.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    d.conn.commit()
    d.close()
    h = call("/api/health")[1]["stats"]
    check("文件数复原", h["files"] == files_before, "%s vs %s" % (h["files"], files_before))

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
