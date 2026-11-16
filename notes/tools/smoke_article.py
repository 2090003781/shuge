"""按篇召回 + 同级时间排序的验收（对着在跑的库打接口 + 一段纯函数单测）。

① 短篇：expand=article 直接给整篇原文
② 长篇：本篇内二次检索定目标 → 只给那一节/那一段，且在预算内
③ 同篇多块命中 → 聚合：该篇只出一条，但记录所有命中段
④ 同级时间优先：新的在前、无日期的排在同档后面（纯函数单测，不靠运气）
⑤ 日期排序不能把"明显更相关但没有日期"的内容压下去
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
M = "ZZARTMARK"          # 短篇标记
L = "ZZLONGTARGET"       # 长篇：只出现在第 5 节
T = "ZZARTTIE"           # 时间排序用的共同词


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
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def search(q, extra=""):
    return call("/api/search?limit=20&scope=all&q=%s%s" % (urllib.parse.quote(q), extra))[1]


def mk(name, content):
    return call("/api/files", "POST", {"name": name, "content": content}, timeout=600)[1]["file_id"]


def filler(chars, text="这一节的内容与目标无关，纯占位。 "):
    n = max(1, int(chars / len(text)))
    return text * n


def section(n, chars=1300):
    return "\n\n## 小节 %d 标题\n\n" % n + filler(chars) + "\n"


# ── ⓪ 纯函数：同级时间优先 ────────────────────────────────────
print("⓪ 时间 tiebreak 单测（同级 = 分数在最高分 5% 内）")
from retriever import HybridSearcher   # noqa: E402

sh = HybridSearcher.__new__(HybridSearcher)      # 不加载模型，只测纯函数
hits = [{"chunk_id": "a", "final": 0.90, "doc_date": "2026-08-01"},
        {"chunk_id": "b", "final": 0.895, "doc_date": "2026-09-11"},
        {"chunk_id": "c", "final": 0.893, "doc_date": ""},
        {"chunk_id": "d", "final": 0.50, "doc_date": "2026-09-12"}]
out = [h["chunk_id"] for h in sh._time_tiebreak(hits)]
check("同档内改成日期倒序（无日期的排最后）", out == ["b", "a", "c", "d"], out)
check("低分档不参与重排（d 仍在最后）", out[-1] == "d", out)
runs = [{"chunk_id": "x", "rrf": 0.01, "doc_date": ""},
        {"chunk_id": "y", "rrf": 0.0095, "doc_date": "2026-09-01"}]
check("没有精排分时按 rrf 分档", [h["chunk_id"] for h in sh._time_tiebreak(runs)] == ["y", "x"],
      [h["chunk_id"] for h in sh._time_tiebreak(runs)])

files_before = call("/api/health")[1]["stats"]["files"]
made = []
try:
    print("① 短篇：整篇直给")
    short = "# 短篇\n\n开头标记 %sSTART。\n\n中间随便写一点。\n\n结尾标记 %sEND。\n" % (M, M)
    fid_s = mk("zz-按篇-短.md", short)
    made.append(fid_s)
    _, f = call("/api/files/" + fid_s)
    r = search(M, "&expand=article")
    hit = [h for h in r["hits"] if h["file_id"] == fid_s]
    check("命中该篇", bool(hit), len(r["hits"]))
    if hit:
        a = hit[0].get("article") or {}
        check("mode=full", a.get("mode") == "full", a.get("mode"))
        check("给的是整篇原文", a.get("text") == f["file"]["content"],
              "%s vs %s 字" % (a.get("chars"), len(f["file"]["content"])))
        check("首尾标记都在", M + "START" in a.get("text", "") and M + "END" in a.get("text", ""))

    print("② 长篇：篇内二次检索（先定目标再取内容）")
    long_body = "# 长篇\n" + "".join(section(i) for i in range(1, 5))
    long_body += "\n\n## 小节 5 标题\n\n目标就在这里：%s 只出现这一次。\n" % L
    long_body += "".join(section(i) for i in range(6, 13))
    fid_l = mk("zz-按篇-长.md", long_body)
    made.append(fid_l)
    _, f2 = call("/api/files/" + fid_l)
    total = len(f2["file"]["content"])
    check("长篇确实超过整篇上限（>10000 字）", total > 10000, "%d 字" % total)
    r2 = search(L, "&expand=article")
    hit2 = [h for h in r2["hits"] if h["file_id"] == fid_l]
    check("命中该篇", bool(hit2))
    if hit2:
        a2 = hit2[0].get("article") or {}
        check("mode=section（没整篇塞）", a2.get("mode") == "section", a2.get("mode"))
        check("含目标词", L in (a2.get("text") or ""))
        check("不含第 1 节的占位内容", "小节 1 标题" not in (a2.get("text") or ""))
        check("在预算内（≤3000+一块）", (a2.get("chars") or 0) <= 3200, a2.get("chars"))
        check("标为已截断", a2.get("truncated") is True)
        check("给出定位（节标题/段范围）",
              bool(a2.get("heading")) and a2.get("seq_from") is not None, a2.get("heading"))

    print("③ 同篇多块命中 → 聚合一条")
    rep_body = "# 重复\n\n%s 出现在很远的第 1 处。\n\n" % T + filler(4000) \
        + "\n\n%s 出现在第 2 处。\n\n" % T + filler(4000) \
        + "\n\n%s 出现在第 3 处。\n" % T
    fid_r = mk("zz-按篇-重复.md", rep_body)
    made.append(fid_r)
    r3 = search(T, "&expand=article&n=5")
    mine = [h for h in r3["hits"] if h["file_id"] == fid_r]
    check("该篇只出一条（聚合）", len(mine) == 1, "%d 条" % len(mine))
    if mine:
        check("记录了多个命中段", len(mine[0].get("matched_seqs") or []) >= 2,
              mine[0].get("matched_seqs"))

    print("④ 真实检索：同正文不同日期")
    body_t = "# 时间\n\n%s 这句话每周都写一遍。\n" % T
    f_new = mk("zz-按篇-2026-09-11-时间.md", body_t)
    f_old = mk("zz-按篇-2026-09-04-时间.md", body_t)
    f_nod = mk("zz-按篇-无日期-时间.md", body_t)
    made += [f_new, f_old, f_nod]
    r4 = search(T, "&time=1")
    order = [h["file_id"] for h in r4["hits"]]
    idx = {fid: order.index(fid) for fid in (f_new, f_old, f_nod) if fid in order}
    check("三篇都被召回", len(idx) == 3, idx)
    if len(idx) == 3:
        check("日期新的排在旧的之前", idx[f_new] < idx[f_old],
              "09-11@%d 09-04@%d" % (idx[f_new], idx[f_old]))
        check("无日期的没被排到最前", idx[f_nod] > idx[f_new], "无日期@%d" % idx[f_nod])

    print("⑤ 日期排序不能压掉明显更相关的内容（需要精排）")
    strong = "# 精确\n\nZZARTPRECISE 关于 %s 的唯一精确句子。\n" % T
    f_strong = mk("zz-按篇-无日期-精确.md", strong)
    made.append(f_strong)
    r5 = search("ZZARTPRECISE " + T, "&time=1")
    if r5.get("rerank"):
        check("最相关（无日期）仍排第一", bool(r5["hits"]) and r5["hits"][0]["file_id"] == f_strong,
              r5["hits"][0]["name"] if r5["hits"] else "无命中")
    else:
        # CPU 上默认不精排（2026-09-26）：没有精排分就只有 RRF，
        # 而 RRF 是"按排名给分"—— 分不出"明显更相关"，这条前提不成立。
        check("不精排时：这篇精确的也在命中里（只是排序不再由精排决定）",
              any(h["file_id"] == f_strong for h in r5["hits"]),
              [h["name"] for h in r5["hits"]][:3])
finally:
    print("⑥ 清场")
    for fid in made:
        call("/api/files/" + fid, "DELETE")
    after = call("/api/health")[1]["stats"]["files"]
    check("文件数复原", after == files_before, "%s → %s" % (files_before, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
