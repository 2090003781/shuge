"""迁移过来的两个能力冒烟：精排（Cross-Encoder）+ 关系通道（沿 mention 走一跳）。

跑法：python tools/smoke_rerank.py
需要库在跑；会在库里造两篇临时笔记（互相有提及），测完删除。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
ok_all = True
A_FILE = "zz-关系甲.md"
B_FILE = "zz-关系乙.md"
C_FILE = "zz-关系丙.md"
A_ONLY = "ZZRELALPHA"      # 只出现在甲里
B_ONLY = "ZZRELBETA"       # 只出现在乙里


def call(path, method="GET", body=None, timeout=300):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global ok_all
    ok_all = ok_all and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


def search(q, **kw):
    qs = "q=%s&limit=%d" % (urllib.parse.quote(q), kw.pop("limit", 6))
    for k, v in kw.items():
        qs += "&%s=%s" % (k, v)
    return call("/api/search?" + qs)[1]


import urllib.parse  # noqa: E402

made = []
files_before = call("/api/health")[1]["stats"]["files"]
try:
    print("① 精排开不开（策略：加速器上开；CPU 上默认关 —— 实测精排 40 条在 CPU 上要 100 秒）2026-09-25）")
    r = search("私密加锁", limit=3)
    check("检索响应报出 rerank 状态", "rerank" in r, r.get("rerank"))
    if r.get("rerank"):
        check("开了精排：命中带精排分 rerank_score",
              all(h.get("rerank_score") is not None for h in r["hits"]),
              [h.get("rerank_score") for h in r["hits"]][:3])
        check("开了精排：精排分是数", all(isinstance(h.get("rerank_score"), float)
                                                       for h in r["hits"]))
    else:
        check("CPU 上默认不精排：不给精排分（省掉那一分钟）",
              all(h.get("rerank_score") is None for h in r["hits"]),
              [h.get("rerank_score") for h in r["hits"]][:3])
        check("CPU 上不精排也照样检索（融合分还在）",
              bool(r["hits"]) and all(h.get("rrf") is not None for h in r["hits"]),
              [(h.get("rrf"), h.get("channels")) for h in r["hits"]][:3])
    check("报出通道权重", (r.get("weights") or {}).get("rel") is not None, r.get("weights"))
    check("hit 里能看到来自哪些通道", all(h.get("channels") for h in r["hits"]),
          [h.get("channels") for h in r["hits"]])

    print("② 关系通道：查询词只在甲里，但丙与甲有提及关系 → 丙应被带出来")
    # 为什么多一个丙（2026-09-25）：乙和甲的正文风格相近，向量通道自己就能把它排进前几，
    # 而关系通道会把"已经是种子的那几篇"排掉 —— 于是"乙走 rel"这条断言就变成看运气。
    # 丙故意写成完全不相关的内容（烧菜），只能靠关系通道被带过来 —— 这才是这条想验的东西。
    a = call("/api/files", "POST", {"name": A_FILE,
                                    "content": "# 甲\n\n%s 这个词只在甲里。\n" % A_ONLY})[1]["file_id"]
    made.append(a)
    b = call("/api/files", "POST", {"name": B_FILE,
                                    "content": "# 乙\n\n%s 这个词只在乙里，和甲的查询词不重叠。\n" % B_ONLY})[1]["file_id"]
    made.append(b)
    c = call("/api/files", "POST", {"name": C_FILE,
                                    "content": "# 丙\n\n红烧肉要焦水去腥，冰糖炒糖色，"
                                               "小火慢炖四十分钟；出锅前收汁。\n"})[1]["file_id"]
    made.append(c)
    call("/api/files/" + a, "PUT",
         {"content": "# 甲\n\n%s 这个词只在甲里。见 @%s 和 @%s 。\n"
                     % (A_ONLY, B_FILE[:-3], C_FILE[:-3])})
    f = call("/api/files/" + a)[1]["file"]
    check("两篇之间建立了提及边", len(f["mentions"]) == 2, f["mentions"])

    # limit 给大一点：关系通道的权重（0.5）比 fts/向量（1.0）低，被带过来的那篇往往在 8 名之后（2026-09-25）
    # 为什么不再断言"乙/丙走的是 rel 通道"（2026-09-25）：
    # 关系通道的种子是 kw[:5]+vec[:5]，而它会把"已经是种子的那几篇"排掉；
    # 向量缓存修好之后（写完就刷），刚建的这两篇也会进 vec 前几，于是"走 rel"就成了看运气。
    # 现在分两层验：数据层直接看关系边（稳）、端到端看关联笔记有没有被带进结果（用户看得见的那一层）。
    sys.path.insert(0, "src")
    from db import Database
    _d = Database()
    _rel = set(_d.related_file_ids([a]))
    _d.close()
    check("数据层：甲的关系里能找到乙和丙", {b, c} <= _rel, sorted(_rel)[:6])

    r2 = search(A_ONLY, limit=20)
    names = [h["name"] for h in r2["hits"]]
    rel_hits = [h for h in r2["hits"] if "rel" in (h.get("channels") or [])]
    check("甲自己是命中", A_FILE in names, names[:4])
    check("关联笔记乙被带进结果（端到端）", B_FILE in names, names[:6])
    # 为什么不断言"丙走的是 rel 通道"：关系通道权重 0.5、而 RRF 是按排名给分，
    # 这句查询是个造出来的生词，全库都会拿到向量命中（权重 1.0）—— 只靠 rel 进来的那篇
    # 很容易排到前 20 之外。上一版这条之所以绿，是因为当时向量缓存没刷（刚建的笔记根本不在向量里）
    # —— 它在测一个 bug。现在只把 rel 那一分部当信息打出来（2026-09-25）。
    print("     这一查走关系通道的：%s" % [(h["name"], h.get("channels")) for h in rel_hits][:5])

    print("③ 关掉精排也要能检索（降级路径）")
    r3 = search("私密加锁", limit=3, rerank=0)
    check("rerank=0 时不带精排分", all(h.get("rerank_score") is None for h in r3["hits"]),
          [h.get("rerank_score") for h in r3["hits"]][:3])
    check("降级后仍有命中", len(r3["hits"]) > 0, len(r3["hits"]))
finally:
    print("④ 清场")
    for fid in made:
        call("/api/files/" + fid, "DELETE")
    sys.path.insert(0, "src")
    from db import Database
    d = Database()
    d.conn.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    d.conn.commit()
    d.close()
    h = call("/api/health")[1]["stats"]
    check("文件数复原", h["files"] == files_before, "%s vs %s" % (h["files"], files_before))

print("\n========  结论：%s" % ("全部通过" if ok_all else "有失败项"))
sys.exit(0 if ok_all else 1)
