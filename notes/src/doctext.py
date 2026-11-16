"""文档格式解析：把各种格式抽成 **Markdown**（唯一中间格式）。

为什么统一成 Markdown：
  · 分块器、FTS、向量、界面预览（md.js）都已经吃 Markdown → 文档一进来就跟笔记同一条流水线
  · 转换器（docx → md）就是"解析结果落盘"，不用再写一套
设计取舍：
  · docx / odt / epub / html / rtf 用**标准库**自己解（zipfile + ElementTree + HTMLParser + 手写 RTF 词法）
    —— 可控、无版本漂移；
  · pdf 真需要库：按 fitz(PyMuPDF) → pypdf → pdfminer 顺序试，都没有就如实报"缺解析库"；
  · 一律**不改原文件**：解析结果进派生缓存（`derived` 表），原字节永远留着。

对外主入口：`parse(ext, data) -> {format, text, meta, parser, error}`
"""
from __future__ import annotations

import html as _html
import io
import json
import os
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
ODF_T = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
ODF_OFFICE = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"
ODF_META = "{urn:oasis:names:tc:opendocument:xmlns:meta:1.0}"
ODF_DC = "{http://purl.org/dc/elements/1.1/}"

# 能解析的格式（/text、convert_doc 用）
VERSION = "p1"         # 解析器版本：**解析逻辑变了就 +1**（文档因此重新解析并重建索引）

DOC_EXTS = {"docx", "docm", "dotx", "odt", "ott", "rtf", "pdf", "epub", "html", "htm", "xhtml"}
# 必须解析后才能入库的（二进制 / 压缩容器）—— 这些 kind 判成 document；
# html 不在此列：它本身就是可读文本，直接入库更可检索（解析只按需用）
INDEX_DOC_EXTS = {"docx", "docm", "dotx", "odt", "ott", "rtf", "pdf", "epub"}
# 这些扩展名已被当纯文本处理（分块器直接吃原文），不必解析
PLAIN_EXTS = {"md", "markdown", "txt", "text", "log", "json", "yaml", "yml", "toml", "ini",
              "py", "js", "ts", "go", "rs", "java", "c", "cpp", "h", "css", "sh", "ps1",
              "bat", "sql", "xml", "csv", "tsv", "srt", "vtt"}


def is_document(ext: str) -> bool:
    """能不能解析"""
    return (ext or "").lower().lstrip(".") in DOC_EXTS


def needs_parse(ext: str) -> bool:
    """入库前**必须**解析（kind=document）：二进制或压缩容器"""
    return (ext or "").lower().lstrip(".") in INDEX_DOC_EXTS


# ── 小工具 ────────────────────────────────────────────────────
def _md_escape(s: str) -> str:
    return (s or "").replace("|", "\\|").strip()


def _collapse(s: str) -> str:
    return re.sub(r"[ \t]+", " ", s or "").strip()


def _table(rows: list) -> str:
    """[[c,c],[c,c]] → Markdown 表（首行当表头）"""
    rows = [[_md_escape(c) for c in r] for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


# ── docx / docm（OOXML：zip + document.xml）────────────────────
_STYLE_H = re.compile(r"^(?:Heading|标题)\s*([1-6])$", re.I)


def parse_docx(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    main = "word/document.xml"
    if main not in names:
        raise ValueError("不是有效的 docx（缺 word/document.xml）")
    rels = {}
    if "word/_rels/document.xml.rels" in names:
        for rel in ET.fromstring(z.read("word/_rels/document.xml.rels")):
            rels[rel.get("Id")] = rel.get("Target") or ""
    root = ET.fromstring(z.read(main))
    body = root.find(W + "body")
    out, meta = [], {}
    if "docProps/core.xml" in names:
        core = ET.fromstring(z.read("docProps/core.xml"))
        for tag, key in (("title", "title"), ("creator", "author"), ("created", "created"),
                         ("modified", "modified")):
            el = core.find("{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
                           + tag) or core.find("{http://purl.org/dc/elements/1.1/}" + tag)
            if el is not None and (el.text or "").strip():
                meta[key] = el.text.strip()

    def run_text(r_el) -> str:
        buf = []
        for t in r_el.iter():
            if t.tag == W + "t":
                buf.append(t.text or "")
            elif t.tag == W + "tab":
                buf.append("\t")
            elif t.tag in (W + "br", W + "cr"):
                buf.append("\n")
        txt = "".join(buf)
        rpr = r_el.find(W + "rPr")
        if rpr is not None and txt.strip():
            if rpr.find(W + "b") is not None:
                txt = "**%s**" % txt
            elif rpr.find(W + "i") is not None:
                txt = "*%s*" % txt
            elif rpr.find(W + "u") is not None:
                txt = "<u>%s</u>" % txt
        return txt

    def para(p_el) -> str:
        ppr = p_el.find(W + "pPr")
        style = ""
        if ppr is not None:
            st = ppr.find(W + "pStyle")
            if st is not None:
                style = st.get(W + "val") or ""
        txt = "".join(child_text(c) for c in p_el if c.tag != W + "pPr")
        txt = _collapse(txt)
        m = _STYLE_H.match(style or "")
        if m:
            return "#" * int(m.group(1)) + " " + txt
        if (style or "").lower().startswith("title"):
            return "# " + txt
        low = (style or "").lower()
        if "list" in low or (ppr is not None and ppr.find(W + "numPr") is not None):
            return "- " + txt if txt else ""
        if "quote" in low:
            return "> " + txt if txt else ""
        return txt

    def child_text(el) -> str:
        if el.tag == W + "r":
            return run_text(el)
        if el.tag == W + "hyperlink":
            inner = "".join(child_text(c) for c in el if c.tag == W + "r")
            rid = el.get(R + "id")
            tgt = rels.get(rid or "", "")
            return "[%s](%s)" % (inner or tgt, tgt) if tgt else inner
        if el.tag == W + "ins":
            return "".join(child_text(c) for c in el)
        if el.tag == W + "smartTag":
            return "".join(child_text(c) for c in el)
        return ""

    for el in body:
        if el.tag == W + "p":
            out.append(para(el))
        elif el.tag == W + "tbl":
            rows = []
            for tr in el.findall(W + "tr"):
                cells = []
                for tc in tr.findall(W + "tc"):
                    cells.append(_collapse(" ".join(child_text(c)
                                                    for p in tc.findall(W + "p")
                                                    for c in p if c.tag != W + "pPr")))
                rows.append(cells)
            out.append(_table(rows))
    media = [n for n in names if n.startswith("word/media/")]
    if media:
        out.append("\n".join("![%s](%s)" % (os.path.basename(n), n) for n in media))
    meta["images"] = len(media)
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(x for x in out if x is not None)).strip()
    return {"text": text, "meta": meta, "parser": "docx(ooxml)"}


# ── odt / ott（ODF：zip + content.xml）─────────────────────────
def parse_odt(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    if "content.xml" not in z.namelist():
        raise ValueError("不是有效的 odt（缺 content.xml）")
    meta = {}
    if "meta.xml" in z.namelist():
        m = ET.fromstring(z.read("meta.xml"))
        for tag, key in ((ODF_DC + "title", "title"), (ODF_DC + "creator", "author"),
                         (ODF_META + "creation-date", "created")):
            el = m.find(".//" + tag)
            if el is not None and (el.text or "").strip():
                meta[key] = el.text.strip()
    root = ET.fromstring(z.read("content.xml"))
    out = []

    def inline(el) -> str:
        buf = []
        for node in el.iter():
            if node.tag == ODF_T + "s":                      # 连续空格
                buf.append(" " * int(node.get(ODF_T + "c") or 1))
            elif node.tag == ODF_T + "line-break":
                buf.append("\n")
            elif node.tag == ODF_T + "tab":
                buf.append("\t")
            elif node.tag == ODF_T + "a":
                pass
            elif node.tag == ODF_T + "span":
                pass
        # text / tail 顺序在 ElementTree 里拿不全，退化成取全部 text 段
        parts = []
        for node in el.iter():
            if node.text:
                parts.append(node.text)
            if node.tail and node.tag not in (ODF_T + "p", ODF_T + "h"):
                parts.append(node.tail)
        return _collapse("".join(parts) + "".join(buf))

    def walk(el):
        for node in el:
            if node.tag == ODF_T + "h":
                lvl = int(node.get(ODF_T + "outline-level") or 1)
                out.append("#" * min(lvl, 6) + " " + inline(node))
            elif node.tag == ODF_T + "p":
                style = node.get(ODF_T + "style-name") or ""
                txt = inline(node)
                if not txt:
                    continue
                if "list" in style.lower() or "List" in style:
                    out.append("- " + txt)
                else:
                    out.append(txt)
            elif node.tag == ODF_TABLE + "table":
                rows = []
                # 用 iter：pandoc 把表头放在 <table:table-header-rows> 里，直接子节点抓不到
                for tr in node.iter(ODF_TABLE + "table-row"):
                    cells = []
                    for tc in tr.findall(ODF_TABLE + "table-cell"):
                        cells.append(_collapse(" ".join(inline(p)
                                                        for p in tc.findall(ODF_T + "p"))))
                    rows.append(cells)
                out.append(_table(rows))
            else:
                walk(node)

    walk(root)
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(x for x in out if x)).strip()
    return {"text": text, "meta": meta, "parser": "odt(odf)"}


# ── rtf（手写词法：够用就好）──────────────────────────────────
_RTF_SKIP_DESTS = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "themedata",
                   "datastore", "latentstyles", "listtable", "listoverridetable", "rsidtbl",
                   "generator", "xmlnstbl", "filetbl", "revtbl", "mmathPr"}


def parse_rtf(data: bytes) -> dict:
    r"""RTF → 文本。手写词法：够用就好（\par/\tab/\b/\i/\'xx/\uN?），
    并**整组跳过** fonttbl/colortbl/pict 等目标（第一版只跳了一个词，把字体表漏进正文了）。
    """
    s = data.decode("latin-1", "replace")
    out, stack = [], []
    depth, skip_at = 0, None
    bold = italic = False
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch == "{":
            depth += 1
            stack.append((bold, italic))
            i += 1
            continue
        if ch == "}":
            if skip_at is not None and depth <= skip_at:
                skip_at = None
            depth = max(0, depth - 1)
            if stack:
                bold, italic = stack.pop()
            i += 1
            continue
        if ch == "\\":
            m = re.match(r"\\([a-zA-Z]+)(-?\d+)? ?", s[i:])
            if m:
                word, num = m.group(1), m.group(2)
                i += m.end()
                if word == "*" or word in _RTF_SKIP_DESTS:      # 整组跳过
                    skip_at = depth
                    continue
                if skip_at is not None:
                    continue
                if word in ("par", "line", "sect"):
                    out.append("\n")
                elif word == "tab":
                    out.append("\t")
                elif word == "b":
                    bold = (num != "0")
                elif word == "i":
                    italic = (num != "0")
                elif word == "u" and num:
                    cp = int(num)
                    out.append(chr(cp if cp > 0 else cp + 65536))
                    if i < n and s[i] == "?":                   # 替身字符
                        i += 1
                elif word in ("pard", "plain"):
                    bold = italic = False
                continue
            m = re.match(r"\\'([0-9a-fA-F]{2})", s[i:])
            if m:
                if skip_at is None:
                    out.append(bytes([int(m.group(1), 16)]).decode("cp1252", "replace"))
                i += m.end()
                continue
            if i + 1 < n and s[i + 1] in "\\{}":               # 转义的 \ { }
                if skip_at is None:
                    out.append(s[i + 1])
                i += 2
                continue
            i += 1
            continue
        if skip_at is None:
            out.append(ch)
        i += 1

    txt = "".join(out).replace("\r", "")
    txt = re.sub(r"[ \t]+\n", "\n", txt)
    txt = re.sub(r"\n{3,}", "\n\n", txt)
    return {"text": txt.strip(), "meta": {}, "parser": "rtf"}


# ── html / xhtml ──────────────────────────────────────────────
class _MdHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.skip = 0
        self.pre = 0
        self.list_stack = []
        self.cell = None
        self.rows = []
        self.row = None
        self.link_stack = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self.out.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "p":
            self.out.append("\n\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "hr":
            self.out.append("\n\n---\n\n")
        elif tag in ("ul", "ol"):
            self.list_stack.append(tag == "ol")
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n" + ("1. " if (self.list_stack and self.list_stack[-1]) else "- "))
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n\n```\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "blockquote":
            self.out.append("\n\n> ")
        elif tag == "table":
            self.rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []
        elif tag == "img":
            self.out.append("![%s](%s)" % (a.get("alt", ""), a.get("src", "")))
        elif tag == "a" and a.get("href"):
            self.link_stack.append({"href": a["href"], "start": len(self.out)})

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "div", "section"):
            self.out.append("\n")
        elif tag in ("ul", "ol"):
            if self.list_stack:
                self.list_stack.pop()
            self.out.append("\n")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n\n")
        elif tag == "code" and not self.pre:
            self.out.append("`")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag in ("td", "th"):
            if self.row is not None and self.cell is not None:
                self.row.append(_collapse("".join(self.cell)))
            self.cell = None
        elif tag == "tr":
            if self.row:
                self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.out.append("\n\n" + _table(self.rows) + "\n\n")
            self.rows = []
        elif tag == "a" and self.link_stack:
            lk = self.link_stack.pop()
            inner = "".join(self.out[lk["start"]:]).strip()
            del self.out[lk["start"]:]
            self.out.append("[%s](%s)" % (inner, lk["href"]))

    def handle_data(self, data):
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
            return
        # 内联文本：只把空白折叠成一个空格，**不要自己加空格**（否则 `**加粗 **`）
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def parse_html(data: bytes) -> dict:
    raw = data.decode("utf-8", "replace")
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S)
    if m:
        title = _html.unescape(_collapse(m.group(1)))
    p = _MdHTML()
    p.feed(raw)
    txt = re.sub(r"[ \t]+\n", "\n", "".join(p.out))
    txt = re.sub(r"\n{3,}", "\n\n", txt).strip()
    return {"text": txt, "meta": {"title": title} if title else {}, "parser": "html"}


# ── epub（zip + opf spine）────────────────────────────────────
def parse_epub(data: bytes) -> dict:
    z = zipfile.ZipFile(io.BytesIO(data))
    try:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        opf_path = container.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
                                  ).get("full-path")
    except Exception:
        raise ValueError("不是有效的 epub（缺 container.xml）")
    opf = ET.fromstring(z.read(opf_path))
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    meta = {}
    for tag, key in (("title", "title"), ("creator", "author")):
        el = opf.find(".//opf:metadata/{%s}%s" % (ns["opf"], tag)) or \
            opf.find(".//{http://purl.org/dc/elements/1.1/}%s" % tag)
        if el is not None and (el.text or "").strip():
            meta[key] = el.text.strip()
    base = os.path.dirname(opf_path)
    items = {i.get("id"): i.get("href") for i in opf.findall(".//opf:manifest/opf:item", ns)}
    chunks = []
    for ref in opf.findall(".//opf:spine/opf:itemref", ns):
        href = items.get(ref.get("idref"))
        if not href:
            continue
        path = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
        if path not in z.namelist():
            continue
        try:
            d = parse_html(z.read(path))
            if d["text"]:
                chunks.append(d["text"])
        except Exception:
            continue
    return {"text": "\n\n---\n\n".join(chunks), "meta": meta,
            "parser": "epub", "meta_extra": {"chapters": len(chunks)}}


# ── pdf（要库：fitz → pypdf → pdfminer）───────────────────────
# ── PDF：带结构的解析（页 / 块 / bbox / 字号 / 表格 / 页眉页脚）──────
# 为什么要有这一层（2026-09-18 辞玖定"先做块树"）：
#   旧实现 fitz 逐页 get_text("text") 再 "\n\n" 拼平 —— 页边界、页眉页脚、标题层级、表格行列、
#   坐标全部丢失（实测：2 页 PDF 变成 1 块扁平文本，页眉页脚还排在最前面污染检索）。
#   块树把这五样都留下来：type/页码/bbox/字号 → 用来定位（locator）、过滤（页眉页脚不进检索）、
#   还原标题层级（让 PDF 也有小节可引用）与表格（行列不散）。
PDF_HEAD_BAND = 0.10        # 页高上 10% 之内算页眉带
PDF_FOOT_BAND = 0.88        # 页高下 10% 之外算页脚带
PDF_TABLE_GAP = 12.0        # 同一行内两段的水平间距超过它 → 认为分属不同单元格
PDF_TABLE_MIN_ROWS = 2
PDF_HEADING_RATIO = 1.18    # 字号 ≥ 正文中位数 × 这个倍数 → 标题候选
PDF_HEADING_MAXLEN = 42
PDF_ROW_TOL = 3.5           # 行中心 y 相差这么点以内算"同一行"


def _num_level(text: str) -> int:
    """按中文/数字编号猜层级："一、"→2；"1.1"→3；"第一章"→1"""
    t = (text or "").strip()
    m = re.match(r"^第[一二三四五六七八九十百]+[章节篇]", t)
    if m:
        return 1
    m = re.match(r"^([一二三四五六七八九十]+)[、.．)）]", t)
    if m:
        return 2
    m = re.match(r"^(\d+(?:\.\d+)*)[、.．)）\s]", t)
    if m:
        return min(3, 1 + m.group(1).count(".") + 1)
    return 0


def _pdf_lines(page) -> list:
    """一页按 fitz 给的阅读顺序取行：文本、字号、bbox、分段（表格用）、所属块、序号

    保留 fitz 的顺序（ord）而不是自己按 y 重排：双栏页面靠它才不会把两栏交错。
    """
    out = []
    d = page.get_text("dict")
    ordn = 0
    for bi, blk in enumerate(d.get("blocks", [])):
        if blk.get("type") != 0:
            out.append({"image": True, "blk": bi, "ord": ordn,
                        "bbox": [round(float(v), 1) for v in (blk.get("bbox") or [])]})
            ordn += 1
            continue
        for ln in blk.get("lines", []):
            spans = [x for x in ln.get("spans", []) if (x.get("text") or "").strip()]
            if not spans:
                continue
            text = "".join(x["text"] for x in spans).strip()
            if not text:
                continue
            sizes = [float(x.get("size") or 0) for x in spans]
            out.append({
                "text": text,
                "size": max(sizes) if sizes else 0.0,
                "blk": bi,
                "ord": ordn,
                "bbox": [round(float(v), 1) for v in ln["bbox"]],
                "segs": [[round(float(x["bbox"][0]), 1), round(float(x["bbox"][2]), 1),
                          (x.get("text") or "").strip()]
                         for x in spans if (x.get("text") or "").strip()],
            })
            ordn += 1
    return out


def _join_lines(texts: list) -> str:
    """把同一个段落里的多行接起来：中文之间不插空格，西文之间插一个空格"""
    out = ""
    for t in texts:
        if not out:
            out = t
            continue
        cjk_prev = "\u4e00" <= out[-1] <= "\u9fff" or out[-1] in "，。；：、！？）》”’"
        cjk_next = ("\u4e00" <= t[0] <= "\u9fff") or t[0] in "（《“‘"
        out += ("" if (cjk_prev or cjk_next) else " ") + t
    return out


def _pdf_rows(lines: list) -> list:
    """把行按 y 归成"行"（表格判定用）：每行是若干条 line；保持 fitz 顺序"""
    rows, cur, cy = [], [], None
    for ln in lines:
        if ln.get("image"):
            continue
        y = (ln["bbox"][1] + ln["bbox"][3]) / 2.0
        if cy is None or abs(y - cy) <= PDF_ROW_TOL:
            cur.append(ln)
            cy = y if cy is None else (cy + y) / 2.0
        else:
            rows.append(cur)
            cur, cy = [ln], y
    if cur:
        rows.append(cur)
    return rows


def _table_cells(row: list):
    """一行里的多个"格子"：按 x 分段、间距够大才算分列；不够就是一整段"""
    if len(row) == 1 and len(row[0].get("segs") or []) >= 2:
        segs = sorted(row[0]["segs"], key=lambda x: x[0])
    else:
        segs = sorted([[ln["bbox"][0], ln["bbox"][2], ln["text"]] for ln in row],
                      key=lambda x: x[0])
    if len(segs) < 2:
        return None
    gaps = [segs[i + 1][0] - segs[i][1] for i in range(len(segs) - 1)]
    if max(gaps) < PDF_TABLE_GAP:
        return None
    return [x[2] for x in segs]


def _md_table(cells: list) -> str:
    def line(cs):
        return "| " + " | ".join((c or " ").replace("|", "\\|").replace("\n", " ") for c in cs) + " |"
    head = cells[0]
    out = [line(head), "|" + "|".join(["---"] * len(head)) + "|"]
    for r in cells[1:]:
        out.append(line((list(r) + [""] * len(head))[:len(head)]))
    return "\n".join(out)


def _pdf_structure(data: bytes) -> dict:
    """PDF → (Markdown, 块树)。块树含页码/bbox/类型/层级；页眉页脚**只进块树、不进 Markdown**。"""
    import fitz
    doc = fitz.open(stream=data, filetype="pdf")
    pages, all_sizes = [], []
    for pno in range(doc.page_count):
        pg = doc[pno]
        lines = _pdf_lines(pg)
        pages.append({"no": pno + 1, "rect": pg.rect, "lines": lines})
        all_sizes += [ln["size"] for ln in lines if not ln.get("image") and ln["size"] > 0]
    n_pages = doc.page_count
    meta = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
    doc.close()

    body_size = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 0.0
    tiers = sorted({round(sz, 1) for sz in all_sizes
                    if body_size and sz >= body_size * PDF_HEADING_RATIO}, reverse=True)[:3]
    # 同一串字在多数页的同一相对位置反复出现 → 页眉/页脚（配合上下带使用）
    seen = {}
    for pg in pages:
        h = pg["rect"].height or 1
        for ln in pg["lines"]:
            if ln.get("image"):
                continue
            yc = (ln["bbox"][1] + ln["bbox"][3]) / 2.0
            seen.setdefault((ln["text"], round((yc - pg["rect"].y0) / h, 1)), set()).add(pg["no"])
    repeated = {k for k, v in seen.items() if len(v) >= max(2, n_pages - 1)}

    def hf_kind(pg, ln):
        h = pg["rect"].height or 1
        yc = (ln["bbox"][1] + ln["bbox"][3]) / 2.0
        rel = (yc - pg["rect"].y0) / h
        top, foot = rel <= PDF_HEAD_BAND, rel >= PDF_FOOT_BAND
        if not (top or foot) and (ln["text"], round(rel, 1)) in repeated \
                and (rel <= 0.20 or rel >= 0.80):
            top, foot = rel <= 0.20, rel > 0.20
        return "header" if top else ("footer" if foot else "")

    emit = []          # (page_no, ord, kind, payload)
    stats = {"header": 0, "footer": 0, "heading": 0, "table": 0, "paragraph": 0, "figure": 0}
    for pg in pages:
        lines = [ln for ln in pg["lines"] if not ln.get("image")]
        for ln in pg["lines"]:
            if ln.get("image"):
                stats["figure"] += 1
                emit.append((pg["no"], ln["ord"], "figure", {"bbox": ln["bbox"]}))
        rows = _pdf_rows(pg["lines"])
        row_cells = [_table_cells(r) for r in rows]
        used, ti = set(), 0
        while ti < len(rows):
            if row_cells[ti] is None:
                ti += 1
                continue
            tj = ti
            while tj + 1 < len(rows) and row_cells[tj + 1] is not None \
                    and len(row_cells[tj + 1]) == len(row_cells[ti]):
                tj += 1
            if tj - ti + 1 >= PDF_TABLE_MIN_ROWS:
                cells = [row_cells[k] for k in range(ti, tj + 1)]
                xs = [ln["bbox"] for k in range(ti, tj + 1) for ln in rows[k]]
                bbox = [round(min(b[0] for b in xs), 1), round(min(b[1] for b in xs), 1),
                        round(max(b[2] for b in xs), 1), round(max(b[3] for b in xs), 1)]
                first = min(ln["ord"] for k in range(ti, tj + 1) for ln in rows[k])
                for k in range(ti, tj + 1):
                    for ln in rows[k]:
                        used.add(id(ln))
                stats["table"] += 1
                emit.append((pg["no"], first, "table",
                             {"cells": cells, "bbox": bbox}))
            ti = tj + 1
        # 其余行：页眉页脚 → 标题 → 段落（同一 fitz 块内连续的行并成一个段落）
        pending = []

        def flush():
            if not pending:
                return
            text = _join_lines([x["text"] for x in pending])
            bbox = [min(x["bbox"][0] for x in pending), min(x["bbox"][1] for x in pending),
                    max(x["bbox"][2] for x in pending), max(x["bbox"][3] for x in pending)]
            stats["paragraph"] += 1
            emit.append((pg["no"], min(x["ord"] for x in pending), "paragraph",
                         {"text": text, "bbox": [round(v, 1) for v in bbox]}))
            pending.clear()

        for ln in lines:
            if id(ln) in used:
                continue
            kind = hf_kind(pg, ln)
            if kind:
                flush()
                stats[kind] += 1
                emit.append((pg["no"], ln["ord"], kind, {"text": ln["text"], "bbox": ln["bbox"]}))
                continue
            t = ln["text"]
            lvl = 0
            if body_size and ln["size"] >= body_size * PDF_HEADING_RATIO \
                    and len(t) <= PDF_HEADING_MAXLEN:
                sz = round(ln["size"], 1)
                lvl = (tiers.index(sz) + 1) if sz in tiers else min(3, len(tiers) + 1)
                lvl = max(1, min(3, lvl))
            nlvl = _num_level(t)
            if nlvl and len(t) <= PDF_HEADING_MAXLEN:
                lvl = max(lvl, nlvl) if lvl else nlvl
            if lvl:
                flush()
                stats["heading"] += 1
                emit.append((pg["no"], ln["ord"], "heading", {"text": t, "level": lvl,
                                                              "bbox": ln["bbox"]}))
            else:
                if pending and pending[-1].get("blk") != ln.get("blk"):
                    flush()
                pending.append(ln)
        flush()

    emit.sort(key=lambda x: (x[0], x[1]))
    md_parts, blocks, pos, seq = [], [], 0, 0
    for page_no, _ordn, kind, p in emit:
        if kind == "header" or kind == "footer":
            blocks.append({"seq": seq, "page_no": page_no, "type": kind, "level": None,
                           "bbox": p["bbox"], "char_start": -1, "char_end": -1,
                           "text_md": p.get("text") or "", "table_json": None})
            seq += 1
            continue
        if kind == "heading":
            text_md = "%s %s" % ("#" * p["level"], p["text"])
            level = p["level"]
        elif kind == "table":
            text_md = _md_table(p["cells"])
            level = None
        elif kind == "figure":
            blocks.append({"seq": seq, "page_no": page_no, "type": "figure", "level": None,
                           "bbox": p["bbox"], "char_start": -1, "char_end": -1,
                           "text_md": "", "table_json": None})
            seq += 1
            continue
        else:
            text_md = p["text"]
            level = None
        start = pos
        md_parts.append(text_md)
        pos += len(text_md) + 2
        blocks.append({"seq": seq, "page_no": page_no, "type": kind, "level": level,
                       "bbox": p["bbox"], "char_start": start, "char_end": start + len(text_md),
                       "text_md": text_md,
                       "table_json": ({"columns": p["cells"][0], "rows": p["cells"][1:]}
                                      if kind == "table" else None)})
        seq += 1

    md = "\n\n".join(md_parts).strip()
    meta_out = {"pages": n_pages, "blocks": len(blocks), "tables": stats["table"],
                "headings": stats["heading"], "figures": stats["figure"],
                "dropped_header_footer": stats["header"] + stats["footer"],
                "body_font": body_size, **meta}
    return {"text": md, "blocks": blocks, "meta": meta_out}


def parse_pdf(data: bytes) -> dict:
    err = []
    try:                                   # PyMuPDF：最稳，还能给页数 + 结构（块树）
        r = _pdf_structure(data)
        return {"text": r["text"], "meta": r["meta"], "parser": "pdf(fitz+structure)",
                "blocks": r["blocks"]}
    except Exception as e:
        err.append("fitz: %s" % str(e)[:60])
    try:
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "") for p in rd.pages]
        meta = {}
        try:
            if rd.metadata:
                meta = {k: str(v) for k, v in dict(rd.metadata).items() if v}
        except Exception:
            pass
        return {"text": "\n\n".join(p.strip() for p in pages if p.strip()),
                "meta": {"pages": len(rd.pages), **meta}, "parser": "pdf(pypdf)", "blocks": []}
    except Exception as e:
        err.append("pypdf: %s" % str(e)[:60])
    try:
        from pdfminer.high_level import extract_text
        txt = extract_text(io.BytesIO(data))
        return {"text": (txt or "").strip(), "meta": {}, "parser": "pdf(pdfminer)", "blocks": []}
    except Exception as e:
        err.append("pdfminer: %s" % str(e)[:60])
    raise ValueError("PDF 解析失败（本机缺可用解析库）：" + "；".join(err))


PARSERS = {
    "docx": parse_docx, "docm": parse_docx, "dotx": parse_docx,
    "odt": parse_odt, "ott": parse_odt,
    "rtf": parse_rtf,
    "html": parse_html, "htm": parse_html, "xhtml": parse_html,
    "epub": parse_epub,
    "pdf": parse_pdf,
}


def parse(ext: str, data: bytes) -> dict:
    """把文档字节解析成 Markdown；纯文本类原样返回（kind=None）"""
    e = (ext or "").lower().lstrip(".")
    if e in PLAIN_EXTS:
        return {"format": e, "text": None, "meta": {}, "parser": "", "error": ""}
    fn = PARSERS.get(e)
    if fn is None:
        return {"format": e, "text": None, "meta": {}, "parser": "",
                "error": "暂不支持解析该格式：%s" % (e or "(无扩展名)")}
    try:
        r = fn(data)
        r.setdefault("blocks", [])
        extra = r.pop("meta_extra", {}) or {}
        r["meta"] = {**(r.get("meta") or {}), **extra}
        return {"format": e, "error": "", **r}
    except Exception as ex:
        return {"format": e, "text": None, "meta": {}, "parser": "",
                "error": "%s: %s: %s" % (e, type(ex).__name__, str(ex)[:140])}


def supported() -> dict:
    """哪些格式能解（给界面/自检用）"""
    out = {}
    for e in sorted(DOC_EXTS):
        if e == "pdf":
            have = []
            for name in ("fitz", "pypdf", "pdfminer"):
                try:
                    __import__(name)
                    have.append(name)
                except Exception:
                    pass
            out[e] = "、".join(have) or "缺解析库"
        else:
            out[e] = "内置"
    return out


if __name__ == "__main__":       # 手测：python src/doctext.py <文件>
    import sys
    for p in sys.argv[1:]:
        ext = p.rsplit(".", 1)[-1].lower() if "." in p else ""
        r = parse(ext, open(p, "rb").read())
        print("=== %s（%s｜%s）%s" % (p, r["format"], r["parser"], r["error"] or ""))
        print((r["text"] or "")[:600])
