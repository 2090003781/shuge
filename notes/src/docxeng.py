# -*- coding: utf-8 -*-
"""DOCX 引擎（2026-10-01 辞玖：「然后就是 doc-docx格式的引擎和编辑器」）。

**分层**（跟 `pdfeng.py` 一样：引擎只认 bytes / 路径，**不认库**；库里那层——前像、重解析、
落库文件夹——留在 `server.py`）：

  ① 只读模型   info / blocks（段落 + 表格 + 图片，带样式与 run）/ outline
  ② 渲染       html（编辑器铺页面用；样式内联，尽量还原）
  ③ 编辑原语   set_text / set_run / set_para / insert_para / del_para / move_para
               set_cell / row_op / find_replace / insert_image / set_page
  ④ 导出       export_md（借 pandoc / 内置解析器）/ export_txt / export_html
  ⑤ 保存       save_bytes（**原地改 XML 再打包**：没碰到的段落、样式、页眉页脚、批注原样带过去）

**为什么不用"md 往返"**：现在的文档编辑器是 pandoc 往返（样式会简化、页眉页脚丢）。
这一层直接在 OOXML 上改，改一段文字只动那一段。

`.doc`（老二进制）**python-docx 读不了**，只能借 WPS/pandoc 转成 docx 再进来 —— 如实说。
"""
from __future__ import annotations

import io
import os
import re

# 段落的样式名（中英都要认：WPS 存的是「标题 1」，Word/pandoc 存的是 "Heading 1"）
HEAD_NAMES = {
    "heading 1": 1, "heading 2": 2, "heading 3": 3, "heading 4": 4, "heading 5": 5, "heading 6": 6,
    "标题 1": 1, "标题 2": 2, "标题 3": 3, "标题 4": 4, "标题 5": 5, "标题 6": 6,
    "标题1": 1, "标题2": 2, "标题3": 3, "标题4": 4, "标题5": 5, "标题6": 6,
    "title": 0, "标题": 0,
}
BULLET_STYLES = ("list bullet", "list paragraph", "项目符号", "项目符号 1", "无序列表", "bullet")
NUMBER_STYLES = ("list number", "编号", "编号 1", "有序列表", "number")
# 对齐：给前端/接口用的名字 ↔ python-docx 的枚举值
ALIGN_WD = {"left": 0, "center": 1, "right": 2, "justify": 3, "both": 3, "distribute": 4}
ALIGN_NAME = {0: "left", 1: "center", 2: "right", 3: "justify", 4: "distribute"}


def _docx():
    import docx
    return docx


def open_doc(data=None, path=None):
    """打开一篇 docx：返回 (Document, 错误串)。"""
    d = _docx()
    try:
        if data is not None:
            return d.Document(io.BytesIO(bytes(data))), ""
        return d.Document(path), ""
    except Exception as e:
        return None, "打不开这个 docx：%s" % str(e)[:120]


def probe(data) -> dict:
    """这份字节到底是不是能用的 docx（zip 完好 + document.xml 能解析 + 有多少段）。"""
    import zipfile
    if not data:
        return {"ok": False, "why": "内容是空的"}
    try:
        z = zipfile.ZipFile(io.BytesIO(bytes(data)))
        names = z.namelist()
        if "word/document.xml" not in names:
            return {"ok": False, "why": "不是 docx（zip 里没有 word/document.xml）"}
        doc, err = open_doc(data=data)
        if doc is None:
            return {"ok": False, "why": err}
        n = len(doc.paragraphs)
        t = len(doc.tables)
        return {"ok": True, "paras": n, "tables": t, "parts": len(names)}
    except Exception as e:
        return {"ok": False, "why": "不是 docx：%s" % str(e)[:100]}


# ── ① 只读模型 ────────────────────────────────────────────
def _pt(v) -> float:
    """python-docx 的长度 → pt；**没有就是 0**（pandoc 生成的 docx 常常不写 <w:pgSz>，值是 None）。"""
    try:
        return round(float(v.pt), 1) if v is not None else 0.0
    except Exception:
        return 0.0


def _body_items(doc):
    """按**文档顺序**产出 ("p", Paragraph) / ("tbl", Table)（跳过 sectPr）。"""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in doc.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            yield "p", Paragraph(child, doc)
        elif tag == "tbl":
            yield "tbl", Table(child, doc)


def _align_name(p) -> str:
    try:
        al = p.alignment
    except Exception:
        al = None
    if al is None:
        return ""
    try:
        return ALIGN_NAME.get(int(al), "")
    except Exception:
        return ""


def _list_of(p) -> dict:
    """这一段是不是列表项（bullet / number），以及级别。"""
    style = ((p.style.name if p.style is not None else "") or "").lower()
    lvl = 0
    try:
        pPr = p._p.pPr
        if pPr is not None and pPr.numPr is not None:
            if pPr.numPr.ilvl is not None and pPr.numPr.ilvl.val is not None:
                lvl = int(pPr.numPr.ilvl.val)
            kind = "number" if any(s in style for s in NUMBER_STYLES) else "bullet"
            return {"kind": kind, "level": lvl}
    except Exception:
        pass
    if any(s in style for s in BULLET_STYLES):
        return {"kind": "bullet", "level": 0}
    if any(s in style for s in NUMBER_STYLES):
        return {"kind": "number", "level": 0}
    return {}


def _run_row(r) -> dict:
    f = r.font
    size = 0.0
    try:
        if f.size is not None:
            size = round(float(f.size.pt), 1)
    except Exception:
        size = 0.0
    color = ""
    try:
        if f.color is not None and f.color.rgb is not None:
            color = str(f.color.rgb)
    except Exception:
        color = ""
    return {"t": r.text or "", "b": bool(r.bold), "i": bool(r.italic),
            "u": bool(r.underline) and r.underline is not False,
            "strike": bool(f.strike), "size": size, "color": color, "font": f.name or ""}


def para_row(p, i: int) -> dict:
    style = (p.style.name if p.style is not None else "") or ""
    return {"i": i, "kind": "p", "style": style, "align": _align_name(p),
            "heading": HEAD_NAMES.get(style.strip().lower(), 0),
            "text": p.text or "", "list": _list_of(p),
            "runs": [_run_row(r) for r in p.runs][:60],
            "blank": not (p.text or "").strip()}


def blocks(doc, max_cells: int = 400) -> list:
    """整篇的块模型（编辑器就照这个铺页面）。"""
    out = []
    for i, (kind, obj) in enumerate(_body_items(doc)):
        if kind == "p":
            out.append(para_row(obj, i))
        else:
            rows = []
            for r in obj.rows:
                rows.append([c.text or "" for c in r.cells][:40])
                if len(rows) >= max_cells:
                    break
            out.append({"i": i, "kind": "table", "rows": len(obj.rows),
                        "cols": len(obj.columns), "cells": rows})
    return out


def info(doc) -> dict:
    ps = list(_body_items(doc))
    heads = []
    for i, (kind, obj) in enumerate(ps):
        if kind != "p":
            continue
        h = HEAD_NAMES.get(((obj.style.name if obj.style is not None else "") or "").strip().lower(), 0)
        if h:
            heads.append({"i": i, "level": h, "text": obj.text or ""})
    imgs = 0
    try:
        imgs = len(doc.inline_shapes)
    except Exception:
        imgs = 0
    chars = 0
    for kind, obj in ps:
        if kind == "p":
            chars += len((obj.text or "").strip())
    sec = doc.sections[0] if doc.sections else None
    page = {}
    if sec is not None:
        w, h = _pt(sec.page_width), _pt(sec.page_height)
        page = {"w": w, "h": h, "size_set": bool(w and h),
                "orient": "landscape" if w > h else "portrait",
                "margin_l": _pt(sec.left_margin), "margin_r": _pt(sec.right_margin),
                "margin_t": _pt(sec.top_margin), "margin_b": _pt(sec.bottom_margin)}
    return {"ok": True, "paras": sum(1 for k, _ in ps if k == "p"),
            "tables": sum(1 for k, _ in ps if k == "tbl"), "blocks": len(ps),
            "images": imgs, "chars": chars, "headings": heads[:200], "page": page,
            "styles": sorted({(o.style.name if o.style is not None else "") or ""
                              for k, o in ps if k == "p"})[:60]}


def outline(doc) -> list:
    return info(doc).get("headings") or []


# ── ② 渲染 ────────────────────────────────────────────────
def _css_of_run(r: dict) -> str:
    bits = []
    if r.get("b"):
        bits.append("font-weight:700")
    if r.get("i"):
        bits.append("font-style:italic")
    if r.get("u"):
        bits.append("text-decoration:underline")
    if r.get("strike"):
        bits.append("text-decoration:line-through")
    if r.get("size"):
        bits.append("font-size:%.1fpt" % r["size"])
    if r.get("color"):
        bits.append("color:#%s" % r["color"].lstrip("#"))
    if r.get("font"):
        bits.append("font-family:'%s'" % r["font"].replace("'", ""))
    return ";".join(bits)


def _esc(t: str) -> str:
    return (str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def html(doc) -> str:
    """整篇 → HTML（编辑器/预览用）：样式内联，标题成 h1~h6，连续列表合成 ul/ol。"""
    out, open_list = [], None
    for kind, obj in _body_items(doc):
        if kind == "tbl":
            if open_list:
                out.append("</%s>" % open_list)
                open_list = None
            rows = []
            for r in obj.rows:
                tds = "".join("<td>%s</td>" % _esc(c.text) for c in r.cells)
                rows.append("<tr>%s</tr>" % tds)
            out.append('<table class="dx-table" border="1" cellspacing="0" cellpadding="4">%s</table>'
                       % "".join(rows))
            continue
        p = obj
        lst = _list_of(p)
        tag = "p"
        h = HEAD_NAMES.get(((p.style.name if p.style is not None else "") or "").strip().lower(), 0)
        if h:
            tag = "h%d" % min(6, max(1, h))
        if lst:
            want = "ul" if lst.get("kind") == "bullet" else "ol"
            if open_list != want:
                if open_list:
                    out.append("</%s>" % open_list)
                out.append("<%s>" % want)
                open_list = want
            tag = "li"
        elif open_list:
            out.append("</%s>" % open_list)
            open_list = None
        body = "".join(('<span style="%s">%s</span>' % (_css_of_run(r), _esc(r.get("t"))))
                       if _css_of_run(r) else _esc(r.get("t")) for r in (para_row(p, 0).get("runs") or []))
        if not body:
            body = _esc(p.text or "")
        al = _align_name(p)
        style = ' style="text-align:%s"' % al if al else ""
        out.append("<%s%s>%s</%s>" % (tag, style, body, tag))
    if open_list:
        out.append("</%s>" % open_list)
    return "\n".join(out)


# ── ③ 编辑原语 ────────────────────────────────────────────
def _block_at(doc, i: int):
    for k, (kind, obj) in enumerate(_body_items(doc)):
        if k == i:
            return kind, obj
    return None, None


def _style_name(doc, want: str) -> str:
    """把「标题1 / heading1 / H2 / 正文」翻成文档里真有的样式名。"""
    d = _docx()
    w = str(want or "").strip()
    low = w.lower().replace(" ", "")
    if low in ("normal", "body", "正文", "p", "paragraph", "正文文本"):
        return "Normal" if "Normal" in [s.name for s in doc.styles] else (doc.styles[0].name or "Normal")
    m = re.match(r"^(?:heading|标题|h)([1-6])$", low)
    if m:
        lvl = m.group(1)
        for cand in ("Heading %s" % lvl, "标题 %s" % lvl, "标题%s" % lvl):
            if cand in [s.name for s in doc.styles]:
                return cand
        return "Heading %s" % lvl
    if low in ("title", "标题"):
        for cand in ("Title", "标题"):
            if cand in [s.name for s in doc.styles]:
                return cand
    if low in ("bullet", "项目符号", "ul"):
        for cand in ("List Bullet", "项目符号", "List Paragraph"):
            if cand in [s.name for s in doc.styles]:
                return cand
    if low in ("number", "编号", "ol"):
        for cand in ("List Number", "编号", "List Paragraph"):
            if cand in [s.name for s in doc.styles]:
                return cand
    for s in doc.styles:
        if s.name == w:
            return w
    return ""


def set_text(doc, i: int, text: str) -> dict:
    """改这一段的文字：**保留第一个 run 的样式**，其余 run 的样式丢掉（跟 Word 里整段重打一样）。"""
    kind, p = _block_at(doc, i)
    if kind != "p":
        return {"ok": False, "error": "第 %d 块不是段落（是 %s）" % (i, kind or "空")}
    text = str(text if text is not None else "")
    runs = list(p.runs)
    if not runs:
        p.add_run(text)
        return {"ok": True, "made": {"i": i, "runs": 1, "kept_style": False}}
    runs[0].text = text
    for r in runs[1:]:
        r._element.getparent().remove(r._element)
    return {"ok": True, "made": {"i": i, "runs": 1, "kept_style": True,
                                 "was": (p.text or "")[:0] or ""}}


def set_run(doc, i: int, j: int, props: dict) -> dict:
    """改第 i 段第 j 个 run 的样式：b/i/u/strike/size/color/font。size<=0 或空串 = 清掉。"""
    kind, p = _block_at(doc, i)
    if kind != "p":
        return {"ok": False, "error": "第 %d 块不是段落" % i}
    runs = list(p.runs)
    if j < 0 or j >= len(runs):
        return {"ok": False, "error": "第 %d 段只有 %d 个 run" % (i, len(runs))}
    r = runs[j]
    props = props or {}
    try:
        if "b" in props:
            r.bold = bool(props["b"])
        if "i" in props:
            r.italic = bool(props["i"])
        if "u" in props:
            r.underline = bool(props["u"])
        if "strike" in props:
            r.font.strike = bool(props["strike"])
        if "size" in props:
            sz = float(props["size"] or 0)
            r.font.size = _docx().shared.Pt(sz) if sz > 0 else None
        if "color" in props:
            c = str(props["color"] or "").lstrip("#")
            if len(c) == 6:
                r.font.color.rgb = _docx().shared.RGBColor.from_string(c.upper())
            else:
                r.font.color.rgb = None
        if "font" in props and props["font"]:
            r.font.name = str(props["font"])
    except Exception as e:
        return {"ok": False, "error": "改 run 样式失败：%s" % str(e)[:100]}
    return {"ok": True, "made": {"i": i, "j": j}}


def _list_numid(doc, kind: str):
    """文档里已有的同类列表编号定义（拿它的 numId 用，这样**不用动段落样式**）。"""
    for k, obj in _body_items(doc):
        if k != "p":
            continue
        lst = _list_of(obj)
        if not lst or lst.get("kind") != kind:
            continue
        try:
            pPr = obj._p.pPr
            if pPr is not None and pPr.numPr is not None and pPr.numPr.numId is not None:
                return int(pPr.numPr.numId.val), int((pPr.numPr.ilvl.val if pPr.numPr.ilvl is not None else 0) or 0)
        except Exception:
            continue
    return None, 0


def set_para(doc, i: int, props: dict) -> dict:
    """改段落属性：style（标题/正文/列表）/ align / list（bullet|number|none）。"""
    kind, p = _block_at(doc, i)
    if kind != "p":
        return {"ok": False, "error": "第 %d 块不是段落" % i}
    props = props or {}
    made = {}
    if props.get("style"):
        name = _style_name(doc, props["style"])
        if not name:
            return {"ok": False, "error": "这份文档里没有样式「%s」（可用：%s）"
                                          % (props["style"], "、".join(s.name for s in doc.styles)[:120])}
        p.style = doc.styles[name]
        made["style"] = name
    if "align" in props:
        p.alignment = ALIGN_WD.get(str(props["align"] or "").lower())
        made["align"] = props["align"]
    if "list" in props:
        want = str(props["list"] or "").lower()
        if want in ("", "none", "off"):
            try:
                pPr = p._p.get_or_add_pPr()
                if pPr.numPr is not None:
                    pPr.remove(pPr.numPr)
            except Exception:
                pass
            made["list"] = "none"
        else:
            kind = "bullet" if want.startswith("b") else "number"
            numid, lvl = _list_numid(doc, kind)
            if numid:
                # 用已有的编号定义（w:numPr）：**不动段落样式**，所以"标题 2 + 列表"能共存
                from docx.oxml.ns import qn
                pPr = p._p.get_or_add_pPr()
                old = pPr.numPr
                if old is not None:
                    pPr.remove(old)
                numPr = pPr.makeelement(qn("w:numPr"), {})
                numPr.append(numPr.makeelement(qn("w:ilvl"), {qn("w:val"): str(lvl)}))
                numPr.append(numPr.makeelement(qn("w:numId"), {qn("w:val"): str(numid)}))
                pPr.append(numPr)
                made["list"] = want
                made["list_via"] = "numPr"
            else:
                name = _style_name(doc, kind)
                if name:
                    p.style = doc.styles[name]
                    made["list"] = want
                    made["list_via"] = "style"      # 退回换样式：这一段原来的样式会被换掉，回执里说明
    return {"ok": True, "made": made}


def insert_para(doc, i: int, text: str, style: str = "") -> dict:
    """在第 i 块**之前**插一段；i 超过末尾就加到最后。"""
    from docx.text.paragraph import Paragraph
    items = list(_body_items(doc))
    new = None
    if 0 <= i < len(items):
        kind, obj = items[i]
        new = obj.insert_paragraph_before(str(text or ""))
    else:
        new = doc.add_paragraph(str(text or ""))
    if style:
        name = _style_name(doc, style)
        if name:
            new.style = doc.styles[name]
    return {"ok": True, "made": {"at": i, "text": str(text or "")[:40]}}


def del_para(doc, i: int) -> dict:
    """删掉第 i 块（段落或表格）。"""
    kind, obj = _block_at(doc, i)
    if kind is None:
        return {"ok": False, "error": "第 %d 块不存在" % i}
    el = obj._element if kind == "p" else obj._element
    el.getparent().remove(el)
    return {"ok": True, "made": {"i": i, "kind": kind}}


def move_para(doc, i: int, to: int) -> dict:
    """把第 i 块挪走，**挪完它就在第 to 位**（to 超过末尾 = 挪到最后）。"""
    items = [o for _, o in _body_items(doc)]
    if i < 0 or i >= len(items):
        return {"ok": False, "error": "第 %d 块不存在" % i}
    el = items[i]._element
    el.getparent().remove(el)
    items2 = [o for _, o in _body_items(doc)]
    if to >= len(items2):
        items2[-1]._element.addnext(el) if items2 else doc.element.body.append(el)
    else:
        items2[max(0, to)]._element.addprevious(el)
    return {"ok": True, "made": {"i": i, "to": to}}


def set_cell(doc, i: int, r: int, c: int, text: str) -> dict:
    """改表格单元格的文字（保留单元格里第一个 run 的样式）。"""
    kind, t = _block_at(doc, i)
    if kind != "tbl":
        return {"ok": False, "error": "第 %d 块不是表格" % i}
    try:
        cell = t.rows[r].cells[c]
    except Exception:
        return {"ok": False, "error": "表格没有第 %d 行第 %d 列" % (r, c)}
    ps = cell.paragraphs
    if not ps:
        return {"ok": False, "error": "单元格是空的"}
    return set_text.__wrapped__(doc, i, text) if False else _set_para_text(ps[0], text)


def _set_para_text(p, text: str) -> dict:
    runs = list(p.runs)
    if not runs:
        p.add_run(str(text or ""))
        return {"ok": True}
    runs[0].text = str(text if text is not None else "")
    for r in runs[1:]:
        r._element.getparent().remove(r._element)
    return {"ok": True}


def row_op(doc, i: int, action: str = "insert", at: int = 0) -> dict:
    """表格加/删一行（python-docx 只能加在末尾，这里按 at 挪一下位置）。"""
    kind, t = _block_at(doc, i)
    if kind != "tbl":
        return {"ok": False, "error": "第 %d 块不是表格" % i}
    try:
        if action == "insert":
            row = t.add_row()
            if 0 <= at < len(t.rows) - 1:
                t.rows[at]._element.addprevious(row._element)
        elif action == "del":
            if len(t.rows) <= 1:
                return {"ok": False, "error": "只剩一行了，删了表就空了"}
            if at < 0 or at >= len(t.rows):
                return {"ok": False, "error": "表格没有第 %d 行" % at}
            el = t.rows[at]._element
            el.getparent().remove(el)
        else:
            return {"ok": False, "error": "action 只能是 insert 或 del"}
    except Exception as e:
        return {"ok": False, "error": "改表格失败：%s" % str(e)[:100]}
    return {"ok": True, "made": {"i": i, "action": action, "at": at}}


def find_replace(doc, q: str, rep: str, cap: int = 2000) -> dict:
    """全篇查找替换（段落 + 表格单元格）。命中的**段落**整段重写（保留第一个 run 的样式）。"""
    q = str(q or "")
    if not q:
        return {"ok": False, "error": "q 不能空（要找什么？）"}
    rep = str(rep if rep is not None else "")
    hit = []

    def do(p, where, idx):
        t = p.text or ""
        if q not in t:
            return
        new = t.replace(q, rep)
        _set_para_text(p, new)
        hit.append({"where": where, "i": idx, "before": t[:60], "after": new[:60]})
        return len(hit) >= cap

    for i, (kind, obj) in enumerate(_body_items(doc)):
        if len(hit) >= cap:
            break
        if kind == "p":
            do(obj, "p", i)
        else:
            for ri, row in enumerate(obj.rows):
                for ci, cell in enumerate(row.cells):
                    for p in cell.paragraphs:
                        if do(p, "cell", "%d/%d/%d" % (i, ri, ci)):
                            break
    return {"ok": True, "count": len(hit), "hits": hit[:60],
            "why": "" if hit else "没找到「%s」" % q}


def insert_image(doc, i: int, blob, width_pt: float = 0) -> dict:
    """在第 i 块后面插一张图（blob 是图片字节）。"""
    d = _docx()
    if not blob:
        return {"ok": False, "error": "图片是空的"}
    try:
        items = list(_body_items(doc))
        if 0 <= i < len(items):
            kind, obj = items[i]
            p = obj.insert_paragraph_before("")
            run = p.add_run()
            run.add_picture(io.BytesIO(bytes(blob)),
                            width=d.shared.Pt(float(width_pt)) if width_pt else None)
        else:
            run = doc.add_paragraph().add_run()
            run.add_picture(io.BytesIO(bytes(blob)),
                            width=d.shared.Pt(float(width_pt)) if width_pt else None)
        return {"ok": True, "made": {"at": i, "bytes": len(blob)}}
    except Exception as e:
        return {"ok": False, "error": "插图失败：%s" % str(e)[:120]}


def set_page(doc, props: dict) -> dict:
    """页面设置：orientation（portrait/landscape）/ margin_* （pt）。"""
    from docx.enum.section import WD_ORIENT
    props = props or {}
    made = {}
    for sec in doc.sections:
        try:
            if (sec.page_width is None or sec.page_height is None) and (
                    props.get("orientation") or props.get("margin_l") or props.get("margin_r")
                    or props.get("margin_t") or props.get("margin_b")):
                sec.page_width, sec.page_height = _docx().shared.Pt(595.3), _docx().shared.Pt(841.9)
            if props.get("orientation"):
                want = str(props["orientation"]).lower()
                w, h = sec.page_width, sec.page_height
                if want.startswith("land") and (w or 0) < (h or 0):
                    sec.orientation = WD_ORIENT.LANDSCAPE
                    sec.page_width, sec.page_height = h, w
                elif want.startswith("port") and (w or 0) > (h or 0):
                    sec.orientation = WD_ORIENT.PORTRAIT
                    sec.page_width, sec.page_height = h, w
                made["orientation"] = want
            for k, attr in (("margin_l", "left_margin"), ("margin_r", "right_margin"),
                            ("margin_t", "top_margin"), ("margin_b", "bottom_margin")):
                if props.get(k) is not None:
                    setattr(sec, attr, _docx().shared.Pt(float(props[k])))
                    made[k] = float(props[k])
        except Exception as e:
            return {"ok": False, "error": "页面设置失败：%s" % str(e)[:100]}
    return {"ok": True, "made": made}


def apply_ops(doc, ops) -> dict:
    """一批编辑操作：**先改后动结构**（增删段落会让后面的序号全变）。"""
    if not isinstance(ops, list) or not ops:
        return {"ok": False, "error": "ops 要是非空数组"}
    soft, hard, warn = [], [], []
    for op in ops:
        if not isinstance(op, dict):
            return {"ok": False, "error": "ops 里每一项要是对象"}
        k = str(op.get("op") or "").lower()
        if k in ("text", "run", "para", "cell", "replace"):
            soft.append(op)
        elif k in ("insert", "del", "move", "row", "image"):
            hard.append(op)
        else:
            return {"ok": False, "error": "不认识的 op：%s（可用 text/run/para/insert/del/move/cell/row/"
                                          "replace/image）" % k}
    made = []
    for op in soft:
        k = op["op"].lower()
        if k == "text":
            r = set_text(doc, int(op.get("i") or 0), op.get("text"))
        elif k == "run":
            r = set_run(doc, int(op.get("i") or 0), int(op.get("j") or 0), op.get("props") or {})
        elif k == "para":
            r = set_para(doc, int(op.get("i") or 0), op.get("props") or {})
        elif k == "cell":
            r = set_cell(doc, int(op.get("i") or 0), int(op.get("r") or 0), int(op.get("c") or 0),
                         op.get("text"))
        else:
            r = find_replace(doc, op.get("q"), op.get("rep"))
        if r.get("ok") is False:
            return r
        made.append({k: r.get("made") or r.get("count")})
    # 结构性操作：按序号从大到小做（先动后面，前面的序号才不会串）
    for op in sorted(hard, key=lambda o: -int(o.get("i") or 0)):
        k = op["op"].lower()
        if k == "insert":
            r = insert_para(doc, int(op.get("i") or 0), op.get("text"), op.get("style") or "")
        elif k == "del":
            r = del_para(doc, int(op.get("i") or 0))
        elif k == "move":
            r = move_para(doc, int(op.get("i") or 0), int(op.get("to") or 0))
        elif k == "row":
            r = row_op(doc, int(op.get("i") or 0), op.get("action") or "insert", int(op.get("at") or 0))
        else:
            import base64
            blob = op.get("b64") or b""
            if isinstance(blob, str):
                try:
                    blob = base64.b64decode(blob)
                except Exception:
                    return {"ok": False, "error": "图片的 b64 解不开"}
            r = insert_image(doc, int(op.get("i") or 0), blob, float(op.get("width") or 0))
        if r.get("ok") is False:
            return r
        made.append({k: r.get("made")})
    return {"ok": True, "changed": len(made), "made": made, "warnings": warn}


# ── ⑤ 保存 / ④ 导出 ───────────────────────────────────────
def save_bytes(doc) -> dict:
    """存回 docx 字节（原地改 XML 再打包）。"""
    try:
        buf = io.BytesIO()
        doc.save(buf)
        data = buf.getvalue()
        p = probe(data)
        if not p.get("ok"):
            return {"ok": False, "error": "存出来的 docx 不合法：%s" % p.get("why")}
        return {"ok": True, "data": data, "bytes": len(data), "paras": p.get("paras")}
    except Exception as e:
        return {"ok": False, "error": "保存失败：%s" % str(e)[:120]}


def export_md(data, ext: str = "docx") -> dict:
    """导出成 Markdown（借 docconvert：pandoc → 内置解析器）。"""
    import docconvert
    try:
        r = docconvert.read_to_md(ext, bytes(data))
        if not isinstance(r, dict):
            return {"ok": True, "md": str(r), "engine": "?"}
        return {"ok": bool(r.get("text")), "md": r.get("text") or "",
                "engine": r.get("engine") or "", "error": r.get("error") or ""}
    except Exception as e:
        return {"ok": False, "error": "导出 md 失败：%s" % str(e)[:120]}


def export_txt(data, ext: str = "docx") -> dict:
    """纯文本（用引擎自己的块模型，不依赖 pandoc）。"""
    doc, err = open_doc(data=data)
    if doc is None:
        return {"ok": False, "error": err}
    lines = []
    for b in blocks(doc):
        if b["kind"] == "p":
            lines.append(b.get("text") or "")
        else:
            for row in b.get("cells") or []:
                lines.append("\t".join(row))
    return {"ok": True, "text": "\n".join(lines)}
