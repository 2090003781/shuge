"""实测：一份 PDF 进库后到底被拆成什么（临时库，不碰真库）。

顺带证明：fitz 本来就能给出 页码/bbox/字号/块顺序，是现在的解析把它们丢掉了。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, r"D:\core-notes\src")
import fitz                                      # noqa: E402
from db import Database                          # noqa: E402
from indexer import Indexer                      # noqa: E402

TD = tempfile.mkdtemp(prefix="pdf-trace-")
PDF = os.path.join(TD, "zz-测试.pdf")
CJK = "china-s"


def page_head(pg, n):
    pg.insert_text((72, 40), "内部资料 · 请勿外传", fontsize=9, fontname=CJK)
    pg.insert_text((430, 40), "第 %d 页" % n, fontsize=9, fontname=CJK)
    pg.insert_text((72, 760), "本文档仅用于内部讨论。", fontsize=8, fontname=CJK)


def build():
    d = fitz.open()
    p1 = d.new_page(width=595, height=842)        # A4
    page_head(p1, 1)
    p1.insert_text((72, 100), "第三季度财务摘要", fontsize=20, fontname=CJK)   # 大字号=标题
    p1.insert_text((72, 130), "一、总体情况", fontsize=13, fontname=CJK)
    p1.insert_textbox(fitz.Rect(72, 145, 520, 210),
                      "本季度收入同比增长百分之十八，主要来自企业客户续约与新增两条产品线。"
                      "成本端因算力采购增加而上升，但毛利率仍保持在合理区间。",
                      fontsize=11, fontname=CJK)
    p1.insert_text((72, 250), "二、明细表", fontsize=13, fontname=CJK)
    rows = [("模块", "状态", "负责人"), ("检索", "已上线", "甲"), ("向量", "待补", "乙")]
    for i, (a, b, c) in enumerate(rows):            # 用绝对坐标画一张"表"
        y = 285 + i * 20
        p1.insert_text((72, y), a, fontsize=11, fontname=CJK)
        p1.insert_text((200, y), b, fontsize=11, fontname=CJK)
        p1.insert_text((330, y), c, fontsize=11, fontname=CJK)
    p1.insert_text((72, 700), "注：数据未经审计。", fontsize=9, fontname=CJK)

    p2 = d.new_page(width=595, height=842)
    page_head(p2, 2)
    p2.insert_text((72, 100), "三、风险提示", fontsize=13, fontname=CJK)
    p2.insert_textbox(fitz.Rect(72, 120, 300, 260),      # 左栏
                      "供应链风险：关键器件交期仍有波动，需要提前锁量。"
                      "合规风险：数据出境的相关评估要在下季度完成。",
                      fontsize=11, fontname=CJK)
    p2.insert_textbox(fitz.Rect(320, 120, 540, 260),     # 右栏
                      "汇率风险：海外收入占比提升，汇兑损益影响变大。"
                      "人才风险：核心岗位补充速度慢于预期。",
                      fontsize=11, fontname=CJK)
    d.save(PDF)
    d.close()


build()
raw = open(PDF, "rb").read()
db = Database(TD)
fid = db.create_file(db.root_folder_id(), "zz-测试.pdf", raw)
row = db.conn.execute("SELECT ext,kind,size,storage FROM files WHERE id=?", (fid,)).fetchone()

print("① 进库")
print("   ext=%s  kind=%s  size=%d B  storage=%s（原字节原样存，未动）"
      % (row["ext"], row["kind"], row["size"], row["storage"]))

print("\n② 解析（docconvert.read_to_md → pdf 跳过 pandoc → doctext.parse_pdf → fitz）")
der = db.derived_for(fid, raw)
print("   format=%s  parser=%s  meta=%s" % (der["format"], der["parser"], der["meta"]))
print("   解析出的文本（原样，前 700 字）：")
for ln in (der["text"] or "").splitlines():
    if ln.strip():
        print("     | " + ln[:76])

print("\n③ 切块（chunker，kind 被改成 markdown 后按标题切）")
res = Indexer(db).reindex_file(fid)
print("   块数=%s" % res.get("chunks"))
for r in db.conn.execute("SELECT seq,heading,title_path,char_count,locator,text FROM chunks"
                         " WHERE file_id=? ORDER BY seq", (fid,)):
    print("   #%d heading=%r title_path=%s chars=%d locator=%r"
          % (r["seq"], r["heading"], r["title_path"], r["char_count"], r["locator"]))
    print("      正文开头：%s" % (r["text"] or "").replace("\n", " ")[:60])

print("\n④ 关键词索引看到的东西（FTS trigram）")
for r in db.conn.execute("SELECT c.seq, c.heading, substr(c.text,1,40) t FROM chunks_fts"
                         " JOIN chunks c ON c.rowid=chunks_fts.rowid WHERE c.file_id=?", (fid,)):
    print("   #%d heading=%r %r" % (r["seq"], r["heading"], r["t"]))

print("\n⑤ 嵌入输入（embedinput：日期前缀 + 提及遮罩后进 bge-m3）")
try:
    import embedinput
    one = db.conn.execute("SELECT text FROM chunks WHERE file_id=? ORDER BY seq LIMIT 1",
                          (fid,)).fetchone()["text"]
    print("   %r" % embedinput.embed_text(one, "", lambda n, s=None: None)[:180])
except Exception as e:
    print("   （跳过：%s）" % str(e)[:80])

print("\n⑥ 同一份 PDF：fitz 其实能给到的结构（现在的解析全丢了）")
d = fitz.open(PDF)
for pno in range(d.page_count):
    pg = d[pno]
    dic = pg.get_text("dict")
    print("   第 %d 页 %.0fx%.0f pt，%d 个块：" % (pno + 1, pg.rect.width, pg.rect.height,
                                                len(dic["blocks"])))
    for bi, b in enumerate(dic["blocks"][:4]):
        if b.get("type") != 0:
            print("      block%d = 图像 bbox=%s" % (bi, [round(x) for x in b["bbox"]]))
            continue
        line = b["lines"][0]["spans"][0]
        print("      block%d bbox=%s 字号=%.0f 首行=%r"
              % (bi, [round(x) for x in b["bbox"]], line["size"], line["text"][:26]))
d.close()

print("\n⑦ 结论（现在这份 PDF 在库里是什么样）")
n_chunks = db.conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id=?", (fid,)).fetchone()[0]
print("   页数=%s；块数=%d；有标题层级的块=%d；带页码/坐标的块=%d"
      % (der["meta"].get("pages"), n_chunks,
         db.conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id=? AND COALESCE(heading,'')<>''",
                         (fid,)).fetchone()[0],
         db.conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id=? AND locator IS NOT NULL",
                         (fid,)).fetchone()[0]))
db.close()
shutil.rmtree(TD, ignore_errors=True)
print("\n（临时库与临时 PDF 已删，真库未动）")
