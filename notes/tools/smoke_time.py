"""日期感知的冒烟：doc_date 从路径/名解析、进嵌入文本、检索能按日期收窄、命中带篇内上下文。

对应辞玖问的两件事：
  ① 同一篇里的片段是否保留"同篇 + 上下段"关系 → 命中带 file_id/seq/total/doc_date；
  ② "这周五 vs 上周五" 会不会混 → 正文一模一样的笔记，日期不同 → 向量不同；
     检索可用 since/until 收窄到某段时间。

用法：python tools/smoke_time.py [base_url]
"""
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
DB = os.environ.get("AI_NOTES_DB", r"D:\core-notes\data\index.db")
OK = True


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def ro():
    return sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)


def vec_of(conn, file_id):
    """该文件所有块的向量（按 seq 序）"""
    rows = conn.execute(
        "SELECT v.vec, v.model, c.seq FROM chunks c JOIN vectors v ON v.chunk_id=c.id"
        " WHERE c.file_id=? ORDER BY c.seq", (file_id,)).fetchall()
    out = []
    for b, model, seq in rows:
        import numpy as np
        out.append((np.frombuffer(b, dtype="float32"), model, seq))
    return out


BODY = ("# 同名不同日\n\nZZTIMEBODY 这句话每周五都写一遍，用来验证日期能不能把它们分开。\n"
        "第二行也是同样的内容，纯粹凑长度。\n")


def search(term, extra=""):
    return call("/api/search?q=%s&limit=20%s" % (urllib.parse.quote(term), extra))[1]


def main():
    global OK
    files_before = call("/api/health")[1]["stats"]["files"]
    st, folder = call("/api/folders", "POST", {"name": "zz-时间冒烟", "path": ""})
    folder_id = folder["folder_id"]
    print("① 造两篇正文完全相同、日期不同的笔记（2026-09-11 / 2026-09-04）")
    ids = {}
    for d in ("2026-09-11", "2026-09-04"):
        st, made = call("/api/files", "POST",
                        {"folder_id": folder_id, "name": "zz-%s-同句.md" % d, "content": BODY})
        ids[d] = made["file_id"]
        check("建 %s 成功" % d, st == 201, made)

    print("② 日期从文件名解析出来")
    for d, fid in ids.items():
        _, f = call("/api/files/" + fid)
        check("%s 的 doc_date" % d, f["file"].get("doc_date") == d, f["file"].get("doc_date"))

    print("③ 正文一样、日期不同 → 向量必须不同（日期进了嵌入文本）")
    conn = ro()
    try:
        va = vec_of(conn, ids["2026-09-11"])
        vb = vec_of(conn, ids["2026-09-04"])
        check("两边都有向量", bool(va) and bool(vb), "%d / %d 块" % (len(va), len(vb)))
        if va and vb:
            import numpy as np
            cos = float(np.dot(va[0][0], vb[0][0]) /
                        (np.linalg.norm(va[0][0]) * np.linalg.norm(vb[0][0]) + 1e-9))
            check("同句不同日 余弦 < 0.999（不是同一个向量）", cos < 0.999, "cos=%.4f" % cos)
            # 规格标签从库里读，别硬编码（掩码语义一变，规格就升一版）
            tag = conn.execute("SELECT model FROM vectors GROUP BY model"
                               " ORDER BY COUNT(*) DESC LIMIT 1").fetchone()[0]
            check("向量记着当前嵌入规格（%s）" % tag, va[0][1] == tag, va[0][1])
    finally:
        conn.close()

    print("④ 检索能按日期收窄")
    r = search("ZZTIMEBODY")
    got = {h["file_id"] for h in r["hits"]}
    check("不带区间：两篇都出现", ids["2026-09-11"] in got and ids["2026-09-04"] in got,
          "%d 命中" % len(r["hits"]))
    r1 = search("ZZTIMEBODY", "&since=2026-09-10")
    got1 = [h["file_id"] for h in r1["hits"]]
    check("since=2026-09-10：只剩 09-11", ids["2026-09-11"] in got1 and ids["2026-09-04"] not in got1,
          got1)
    r2 = search("ZZTIMEBODY", "&until=2026-09-05")
    got2 = [h["file_id"] for h in r2["hits"]]
    check("until=2026-09-05：只剩 09-04", ids["2026-09-04"] in got2 and ids["2026-09-11"] not in got2,
          got2)
    check("响应回显区间", r1.get("since") == "2026-09-10" and r2.get("until") == "2026-09-05",
          "%s / %s" % (r1.get("since"), r2.get("until")))

    print("⑤ 命中带篇内上下文（同一篇的关系）")
    h = [x for x in r["hits"] if x["file_id"] == ids["2026-09-11"]][0]
    check("带 doc_date", h.get("doc_date") == "2026-09-11", h.get("doc_date"))
    check("带 seq / total", isinstance(h.get("seq"), int) and isinstance(h.get("total"), int),
          "seq=%s total=%s" % (h.get("seq"), h.get("total")))
    check("total 与该篇块数一致", h.get("total") == len(va), "%s vs %s" % (h.get("total"), len(va)))

    print("⑥ 改名把日期改掉 → doc_date 与向量都跟着重算")
    st, rn = call("/api/files/%s/rename" % ids["2026-09-04"], "POST",
                  {"name": "zz-2026-08-28-同句.md"})
    check("改名成功", st == 200, rn)
    _, f = call("/api/files/" + ids["2026-09-04"])
    check("doc_date 变成 2026-08-28", f["file"].get("doc_date") == "2026-08-28",
          f["file"].get("doc_date"))
    conn = ro()
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
            " WHERE c.file_id=? AND (v.chunk_id IS NULL OR v.model<>"
            "  (SELECT model FROM vectors GROUP BY model ORDER BY COUNT(*) DESC LIMIT 1))",
            (ids["2026-09-04"],)).fetchone()[0]
    finally:
        conn.close()
    check("改完没有留向量缺口", n == 0, "缺口 %d" % n)
    r3 = search("ZZTIMEBODY", "&until=2026-09-01")
    check("按新日期搜得到", ids["2026-09-04"] in [x["file_id"] for x in r3["hits"]])

    print("⑦ 挪进带日期的文件夹 → 日期也跟着走")
    st, sub = call("/api/folders", "POST", {"name": "2026-07-31", "parent_id": folder_id})
    st, mv = call("/api/files/%s/move" % ids["2026-09-11"], "POST", {"folder_id": sub["folder_id"]})
    check("移动成功", st == 200 and mv.get("doc_date") == "2026-07-31", mv)
    _, f = call("/api/files/" + ids["2026-09-11"])
    check("doc_date 随路径变成 2026-07-31", f["file"].get("doc_date") == "2026-07-31",
          f["file"].get("doc_date"))

    print("⑧ 清理")
    for fid in ids.values():
        call("/api/files/" + fid, "DELETE")
    for fid in (sub["folder_id"], folder_id):
        call("/api/folders/" + fid, "DELETE")
    after = call("/api/health")[1]["stats"]["files"]
    check("临时文件已删净", after == files_before, "%s → %s" % (files_before, after))

    print("\n%s" % ("全部通过" if OK else "有失败项"))
    return 0 if OK else 1


if __name__ == "__main__":
    sys.exit(main())
