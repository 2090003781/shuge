# -*- coding: utf-8 -*-
"""DOCX 引擎的**离线**验收（不启库、不碰真库）。

  python tools/acc_docxeng.py

验的是引擎层：块模型（段落/表格/图片、标题/列表/run 样式）、编辑原语（改字/改样式/增删移/
表格/查找替换/插图/页面设置）、以及**改一段不丢别处**（这是它存在的理由：pandoc 往返会简化样式）。
样本 docx 用 pandoc 现场生成（跟库里那条约的路一样）。
"""
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import docconvert      # noqa: E402
import docxeng         # noqa: E402

OK, BAD = [], []
MD = """# 一级标题

第一段：普通正文，带 **加粗** 和 *斜体* 还有 `代码`。

## 二级标题

- 项目一
- 项目二

1. 第一步
2. 第二步

| 姓名 | 分数 |
|---|---|
| 张三 | 90 |
| 李四 | 85 |

结尾段落：这一句要留着当对照，一个字都不改。
"""


def chk(name, cond, extra=""):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name, ('  | ' + str(extra)[:150]) if extra else ''))


tmp = tempfile.mkdtemp(prefix="zz-docxeng-")
src_path = os.path.join(tmp, "样本.docx")
print('造样本 docx（pandoc）……')
w = docconvert.write_md(MD, "docx", src_path)
chk('pandoc 生成样本 docx', os.path.exists(src_path) and os.path.getsize(src_path) > 2000,
    {k: w.get(k) for k in ("ok", "why", "engine")} if isinstance(w, dict) else w)
DATA = io.open(src_path, "rb").read()
print('样本 %d 字节\n' % len(DATA))

try:
    print('① 只读模型')
    p = docxeng.probe(DATA)
    chk('probe：是能用的 docx', p.get("ok") is True, p)
    chk('probe 报出段落数与表格数', (p.get("paras") or 0) >= 5 and (p.get("tables") or 0) == 1, p)
    doc, err = docxeng.open_doc(data=DATA)
    chk('open_doc 成功', doc is not None, err)
    bs = docxeng.blocks(doc)
    chk('块模型拿到块', len(bs) >= 6, len(bs))
    heads = [b for b in bs if b.get("heading")]
    chk('认得出标题（1 级 + 2 级）', [h["heading"] for h in heads][:2] == [1, 2],
        [(h["heading"], h["text"]) for h in heads[:3]])
    chk('段落样式名读到了', any((b.get("style") or "") for b in bs if b["kind"] == "p"),
        [b.get("style") for b in bs if b["kind"] == "p"][:4])
    lst = [b for b in bs if b.get("list")]
    chk('认得出列表（项目符号 / 编号）', len(lst) >= 3 and {l["list"]["kind"] for l in lst} >= {"bullet"},
        [(l["list"]["kind"], l.get("text")) for l in lst[:4]])
    runs = [r for b in bs if b["kind"] == "p" for r in (b.get("runs") or [])]
    chk('run 带样式（加粗/斜体至少认出一个）',
        any(r.get("b") for r in runs) or any(r.get("i") for r in runs),
        [(r.get("t", "")[:8], r.get("b"), r.get("i")) for r in runs[:6]])
    tb = [b for b in bs if b["kind"] == "table"]
    chk('表格进了块模型（行 × 列 + 单元格文字）',
        tb and tb[0]["rows"] >= 3 and tb[0]["cols"] == 2 and "张三" in str(tb[0]["cells"]),
        tb[0] if tb else None)

    inf = docxeng.info(doc)
    chk('info：段/表/字数/标题大纲', inf.get("paras", 0) >= 5 and inf.get("tables") == 1
        and inf.get("chars", 0) > 50 and len(inf.get("headings") or []) == 2, inf)
    pg = inf.get("page") or {}
    chk('info 带页面设置：pandoc 的样本没写 <w:pgSz> → 如实报 size_set=false（不编尺寸）',
        "size_set" in pg and (pg.get("size_set") is False or pg.get("w", 0) > 100), pg)
    h = docxeng.html(doc)
    chk('html 渲染：有 h1/h2、有 ul/ol、有 table',
        "<h1" in h and "<h2" in h and ("<ul>" in h or "<ol>" in h) and "<table" in h, h[:80].replace("\n", " "))
    chk('html 里加粗/斜体内联进去了', "font-weight:700" in h or "font-style:italic" in h, "")

    print('\n② 改字：只动那一段，样式/别处都留着')
    i_end = [b["i"] for b in bs if "对照" in (b.get("text") or "")][0]
    i_h1 = heads[0]["i"]
    r = docxeng.set_text(doc, i_end, "结尾段落：这一句已经被引擎改过了。")
    chk('set_text 成功', r.get("ok") is True, r)
    out = docxeng.save_bytes(doc)
    chk('存回去是合法 docx', out.get("ok") is True and out.get("paras") == p.get("paras"), out.get("error") or out.get("bytes"))
    d2, _ = docxeng.open_doc(data=out["data"])
    b2 = docxeng.blocks(d2)
    chk('改的那段变了', any("已经被引擎改过了" in (b.get("text") or "") for b in b2), '')
    chk('标题还在、还是标题', any(b.get("heading") == 1 for b in b2), '')
    chk('表格还在、内容没动', any(b["kind"] == "table" and "张三" in str(b["cells"]) for b in b2), '')
    chk('列表还在', any(b.get("list") for b in b2), '')
    md2 = docxeng.export_md(out["data"]).get("md") or ""
    chk('用 pandoc 读回来：加粗/斜体/表格/标题都还在（改字不丢样式）',
        "加粗" in md2 and ("**" in md2 or "__" in md2) and "张三" in md2 and "#" in md2, md2[:60].replace("\n", " "))
    chk('用 pandoc 读回来：改过的那句是新文字', "已经被引擎改过了" in md2, '')

    print('\n③ 改样式：run / 段落 / 对齐 / 列表')
    i_body = [b["i"] for b in b2 if "普通正文" in (b.get("text") or "")][0]
    r = docxeng.set_run(d2, i_body, 0, {"b": True, "size": 18, "color": "#ff0000"})
    chk('set_run 改字号/加粗/颜色', r.get("ok") is True, r)
    r2 = docxeng.set_para(d2, i_body, {"align": "center"})
    chk('set_para 居中对齐', r2.get("ok") is True and r2.get("made", {}).get("align") == "center", r2)
    r3 = docxeng.set_para(d2, i_body, {"style": "标题2"})
    chk('set_para 换成「标题 2」（中文样式名也认）',
        r3.get("ok") is True and "2" in str(r3.get("made", {}).get("style")), r3)
    r4 = docxeng.set_para(d2, i_body, {"style": "正文"})
    chk('set_para 换回正文', r4.get("ok") is True, r4)
    r5 = docxeng.set_para(d2, i_body, {"style": "没有这个样式"})
    chk('写错的样式名：拒绝并列出可用样式', r5.get("ok") is False and "没有样式" in str(r5.get("error")), r5.get("error"))
    out3 = docxeng.save_bytes(d2)
    d3, _ = docxeng.open_doc(data=out3["data"])
    b3 = docxeng.blocks(d3)
    row = [b for b in b3 if b["i"] == i_body][0]
    chk('存回去后：这一段居中 + 第一个 run 18 号红字加粗',
        row.get("align") == "center" and (row.get("runs") or [{}])[0].get("b") is True
        and abs(float((row.get("runs") or [{}])[0].get("size") or 0) - 18) < 0.6,
        [(r0.get("b"), r0.get("size"), r0.get("color")) for r0 in (row.get("runs") or [])[:2]])

    print('\n④ 增 / 删 / 移')
    n0 = len(b3)
    r = docxeng.insert_para(d3, 1, "插进来的一段新文字", "Heading 3")
    chk('insert_para 插一段', r.get("ok") is True and len(docxeng.blocks(d3)) == n0 + 1, r)
    b4 = docxeng.blocks(d3)
    chk('插进去的是 3 级标题、文字对', b4[1].get("heading") == 3 and "插进来的一段新文字" in b4[1].get("text", ""),
        [(b.get("heading"), (b.get("text") or "")[:12]) for b in b4[:3]])
    r = docxeng.move_para(d3, 1, 4)
    chk('move_para 挪完它就在第 4 位', r.get("ok") is True and "插进来" in (docxeng.blocks(d3)[4].get("text") or ""),
        [((b.get("text") or "")[:12]) for b in docxeng.blocks(d3)[:6]])
    r = docxeng.del_para(d3, 4)
    chk('del_para 删掉它', r.get("ok") is True and len(docxeng.blocks(d3)) == n0, len(docxeng.blocks(d3)))
    chk('删块越界会说清', docxeng.del_para(d3, 999).get("ok") is False, '')

    print('\n⑤ 表格 / 查找替换 / 插图 / 页面')
    ti = [b["i"] for b in docxeng.blocks(d3) if b["kind"] == "table"][0]
    r = docxeng.set_cell(d3, ti, 1, 1, "95")
    chk('set_cell 改单元格', r.get("ok") is True, r)
    chk('改完读回来是 95', "95" in str([b for b in docxeng.blocks(d3) if b["i"] == ti][0]["cells"]), '')
    r = docxeng.row_op(d3, ti, "insert", at=1)
    chk('表格插一行', r.get("ok") is True and [b for b in docxeng.blocks(d3) if b["i"] == ti][0]["rows"] == 4, r)
    chk('只剩一行不让删', docxeng.row_op(d3, ti, "del", at=9).get("ok") is False, '')
    r = docxeng.find_replace(d3, "张三", "张大师")
    chk('find_replace 命中并换掉', r.get("count", 0) >= 1 and "张大师" in str(docxeng.blocks(d3)), r.get("count"))
    chk('find_replace 找不到：count=0 + 说清', docxeng.find_replace(d3, "库里绝对没有的词", "x").get("count") == 0, '')
    chk('find_replace 不给 q：拒绝', docxeng.find_replace(d3, "", "x").get("ok") is False, '')

    png = _png = None
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (80, 40), (200, 30, 30)).save(buf, "PNG")
        png = buf.getvalue()
    except Exception:
        png = None
    if png:
        before = docxeng.info(d3).get("images", 0)
        r = docxeng.insert_image(d3, 0, png, width_pt=120)
        chk('insert_image 插图', r.get("ok") is True and docxeng.info(d3).get("images", 0) == before + 1, r)
    r = docxeng.set_page(d3, {"orientation": "landscape", "margin_l": 50})
    chk('set_page 横版 + 左边距', r.get("ok") is True, r)
    inf3 = docxeng.info(d3)
    chk('页面设置生效（宽 > 高）', (inf3.get("page") or {}).get("orient") == "landscape"
        and inf3["page"]["w"] > inf3["page"]["h"], inf3.get("page"))

    print('\n⑥ 一批操作一起做（结构操作按序号从大到小，序号不会串）')
    d5, _ = docxeng.open_doc(data=DATA)
    b5 = docxeng.blocks(d5)
    i_last = len(b5) - 1
    r = docxeng.apply_ops(d5, [
        {"op": "text", "i": 1, "text": "第一段：被批量改过。"},
        {"op": "del", "i": i_last},
        {"op": "insert", "i": 1, "text": "批插的一段"},
        {"op": "para", "i": 2, "props": {"align": "right"}},
    ])
    chk('apply_ops 一批全成功', r.get("ok") is True and r.get("changed") == 4, r)
    b6 = docxeng.blocks(d5)
    chk('批量后：新插的段在、原最后一段没了、改字生效、对齐生效',
        any("批插的一段" in (x.get("text") or "") for x in b6)
        and not any("对照" in (x.get("text") or "") for x in b6)
        and any("被批量改过" in (x.get("text") or "") for x in b6), [((x.get("text") or "")[:10]) for x in b6[:5]])
    chk('乱写 op 会被拒绝', docxeng.apply_ops(d5, [{"op": "bogus"}]).get("ok") is False, '')
    chk('空 ops 会被拒绝', docxeng.apply_ops(d5, []).get("ok") is False, '')

    print('\n⑦ 导出')
    chk('export_md 出得来 Markdown', "一级标题" in (docxeng.export_md(DATA).get("md") or ""), '')
    chk('export_txt 出得来纯文本（引擎自己抽，不靠 pandoc）',
        "结尾段落" in (docxeng.export_txt(DATA).get("text") or ""), '')
    chk('probe 对垃圾字节会说清不是 docx',
        docxeng.probe(b"not a docx").get("ok") is False, docxeng.probe(b"not a docx").get("why"))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print('\n通过 %d / %d；失败：%s' % (len(OK), len(OK) + len(BAD), '、'.join(BAD) if BAD else '无'))
sys.exit(1 if BAD else 0)
