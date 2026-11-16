"""块树验收：PDF 进库后要留下 页/块/类型/层级/bbox/表格，并且
  · 页眉页脚只进块树、不进检索正文
  · 标题层级还原 → PDF 也有小节可引用
  · 表格变成 Markdown 表格（行列不散）
  · 命中结果带"第 N 页"这种位置（locator）
"""
import base64
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DBP = os.path.join(HERE, "data", "index.db")
MADE = []
OK = True
CJK = "china-s"


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
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:160] if extra else ""))


def sample_pdf() -> bytes:
    """两页样例：页眉 / 页脚 / 大标题 / 小节 / 正文 / 三列两行表格 / 双栏"""
    import fitz

    def head(pg, n):
        pg.insert_text((72, 40), "内部资料 · 请勿外传", fontsize=9, fontname=CJK)
        pg.insert_text((430, 40), "第 %d 页" % n, fontsize=9, fontname=CJK)
        pg.insert_text((72, 760), "本文档仅用于内部讨论。", fontsize=8, fontname=CJK)

    d = fitz.open()
    p1 = d.new_page(width=595, height=842)
    head(p1, 1)
    p1.insert_text((72, 100), "第三季度财务摘要", fontsize=20, fontname=CJK)
    p1.insert_text((72, 130), "一、总体情况", fontsize=13, fontname=CJK)
    p1.insert_textbox(fitz.Rect(72, 145, 520, 210),
                      "本季度收入同比增长百分之十八，主要来自企业客户续约与新增两条产品线。"
                      "成本端因算力采购增加而上升，但毛利率仍保持在合理区间。",
                      fontsize=11, fontname=CJK)
    p1.insert_text((72, 250), "二、明细表", fontsize=13, fontname=CJK)
    for i, (a, b, c) in enumerate([("模块", "状态", "负责人"), ("检索", "已上线", "甲"),
                                   ("向量", "待补", "乙")]):
        y = 285 + i * 20
        p1.insert_text((72, y), a, fontsize=11, fontname=CJK)
        p1.insert_text((200, y), b, fontsize=11, fontname=CJK)
        p1.insert_text((330, y), c, fontsize=11, fontname=CJK)
    p1.insert_text((72, 700), "注：数据未经审计。", fontsize=9, fontname=CJK)
    p2 = d.new_page(width=595, height=842)
    head(p2, 2)
    p2.insert_text((72, 100), "三、风险提示", fontsize=13, fontname=CJK)
    p2.insert_textbox(fitz.Rect(72, 120, 300, 260),
                      "供应链风险：关键器件交期仍有波动，需要提前锁量。", fontsize=11, fontname=CJK)
    p2.insert_textbox(fitz.Rect(320, 120, 540, 260),
                      "汇率风险：海外收入占比提升，汇兑损益影响变大。", fontsize=11, fontname=CJK)
    out = d.tobytes()
    d.close()
    return out


BEFORE = call("/api/health")[1]["stats"]["files"]
try:
    print("① 传一份两页 PDF → 打开就能看（默认不进检索）")
    st, up = call("/api/files", "POST", {
        "name": "zz-块树-样例.pdf",
        "content_b64": base64.b64encode(sample_pdf()).decode("ascii")})
    check("上传成功", st == 201 and up.get("file_id"), up.get("error"))
    fid = up["file_id"]
    MADE.append(fid)
    check("默认不进检索库", call("/api/files/" + fid)[1]["file"]["rag"] == "off")

    t = call("/api/files/%s/text" % fid)[1]
    txt = t.get("text") or ""
    check("正文有标题层级（PDF 也有 # / ##）",
          "# 第三季度财务摘要" in txt and "## 一、总体情况" in txt and "## 二、明细表" in txt,
          txt[:90].replace("\n", " / "))
    check("页眉不进正文（'内部资料' 不在）", "内部资料" not in txt)
    check("页脚不进正文（'仅用于内部讨论' 不在）", "仅用于内部讨论" not in txt)
    check("表格变成 Markdown 表格", "| 模块 | 状态 | 负责人 |" in txt and "| 检索 | 已上线 | 甲 |" in txt,
          [l for l in txt.splitlines() if l.startswith("|")][:2])
    check("双栏两段都在", "供应链风险" in txt and "汇率风险" in txt)

    print("② 加入检索库 → 块树")
    st, rg = call("/api/files/%s/rag" % fid, "POST", {"on": True})
    check("加入检索库", rg.get("rag") == "on" and rg.get("chunks", 0) > 0, rg)
    st, bl = call("/api/files/%s/blocks" % fid)
    stats = bl.get("stats") or {}
    check("块树有内容", st == 200 and bl.get("count", 0) > 5, stats)
    check("页数 = 2", stats.get("pages") == 2, stats)
    check("识别出表格", stats.get("tables") == 1, stats)
    check("识别出标题", stats.get("headings", 0) >= 3, stats)
    check("页眉页脚被单独标出（并从检索正文里剔除）", stats.get("dropped", 0) >= 4, stats)
    check("接口返回了块数组",
          isinstance(bl.get("blocks"), list) and bl.get("count", 0) == len(bl["blocks"]),
          str(bl)[:120])
    types = sorted({b["type"] for b in (bl.get("blocks") or [])})
    check("块类型齐全（heading/paragraph/table/header/footer）",
          {"heading", "paragraph", "table", "header", "footer"} <= set(types), types)
    tb = [b for b in bl["blocks"] if b["type"] == "table"][0]
    check("表格块带行列数据（table_json）",
          (tb.get("table_json") or {}).get("columns") == ["模块", "状态", "负责人"],
          tb.get("table_json"))
    check("块带页码与坐标", all(b.get("page_no") and b.get("bbox") for b in bl["blocks"]
                                if b["type"] != "table") and tb.get("bbox"), tb.get("bbox"))
    hdr = [b for b in bl["blocks"] if b["type"] == "header"][0]
    check("页眉块不占正文偏移（char_start < 0）", hdr["char_start"] < 0, hdr["char_start"])
    st, bl2 = call("/api/files/%s/blocks?page=2" % fid)
    check("能按页取块（第 2 页）", bl2["count"] > 0
          and all(b["page_no"] == 2 for b in bl2["blocks"]), bl2["count"])

    print("③ 命中结果带位置（locator → 第 N 页）")
    s = call("/api/search?q=%s&limit=10" % urllib.parse.quote("供应链风险"))[1]
    hit = next((h for h in s["hits"] if h["file_id"] == fid), None)
    check("搜得到这篇", hit is not None, "%d 命中" % len(s["hits"]))
    if hit:
        check("命中的是第 2 页", (hit.get("locator") or {}).get("page") == 2, hit.get("locator"))
        check("给人看的位置串也对", "第 2 页" in (hit.get("where") or ""), hit.get("where"))

    print("④ PDF 也有小节可引用")
    body = "# 引用方\n\n见 @zz-块树-样例#一、总体情况 那一节。\n\n还有 @zz-块树-样例#不存在的节。\n"
    st, made = call("/api/files", "POST", {"name": "zz-块树-引用.md", "content": body})
    MADE.append(made["file_id"])
    bad = (made.get("bad_sections") or [])
    check("存在的节解析得上（只有'不存在的节'被报出来）",
          len(bad) == 1 and "不存在的节" in (bad[0].get("section") or str(bad[0])), bad)
    f = call("/api/files/" + made["file_id"])[1]["file"]
    m = f["mentions"][0] if f["mentions"] else {}
    check("引用边指向 PDF 且带小节",
          m.get("name") == "zz-块树-样例.pdf" and (m.get("section") or ""), m)
finally:
    print("⑤ 清场")
    for x in MADE:
        call("/api/files/" + x, "DELETE")
    c = sqlite3.connect(DBP)
    n = c.execute("DELETE FROM blocks WHERE file_id NOT IN (SELECT id FROM files)").rowcount
    c.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    c.commit()
    c.close()
    if n:
        print("   （顺手清了 %d 条孤儿块）" % n)
    after = call("/api/health")[1]["stats"]["files"]
    check("文件数复原", after == BEFORE, "%s → %s" % (BEFORE, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
