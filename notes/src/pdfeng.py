# -*- coding: utf-8 -*-
"""PDF 引擎（2026-09-30 辞玖：「我的意思就是搞一个 pdf 引擎，编辑器是其中一部分」）。

**分层**（引擎只认 bytes / 路径，**不认库**；库里那层——前像、事件、落库文件夹——在 server 里）：

  ① 只读模型   info / pages / text_layer / annots / outline / meta
  ② 渲染       render_page（任意缩放的 PNG）
  ③ 编辑原语   annotate（批注：高亮/下划线/删除线/便签/文本框/矩形/自由画）
               pages_op（旋转 / 删页 / 移动 / 提取）
               apply_text_ops / find_replace / insert_text（**擦字重画**：擦掉原字、原位重画新字）
               stamp（页码 / 水印）· merge_pdf（合并）· bake_annots（扁平化）· export_images
  ④ 导出       export_text（按页文字）/ export_images（每页 PNG）
  ⑤ 保存       save_bytes（默认**增量**保存，尽量保留原字节）

为什么要单独一层：PDF 的"编辑"不是 UI 的事 —— 改字要处理**字体子集**、批注要处理 **annot 对象与
外观流**、页面操作要处理 **xref 与父对象**。这些都在这里做掉，编辑器（前端）只管画和点。

坐标系约定（**重要**）：对外一律用 **PyMuPDF 坐标**（原点在**左上**、y 轴朝**下**、单位 point）；
文字层的 bbox、批注的 rect、search_for 的命中框都是这一套，原样进原样出，不做翻转；
前端把它换算成页面像素（scale = 渲染宽度 / 页宽），top 用 bbox[1]、left 用 bbox[0]。
"""
from __future__ import annotations

import io
import os
import re
import shutil
import tempfile
import time

ANNOT_KINDS = {
    "highlight": "高亮",
    "underline": "下划线",
    "strikeout": "删除线",
    "squiggly": "波浪线",
    "note": "便签",
    "text": "文本框",
    "rect": "矩形",
    "ink": "自由画",
}
# 颜色名 → RGB（0~1）
COLORS = {
    "yellow": (1.0, 0.85, 0.2),
    "green": (0.45, 0.85, 0.45),
    "blue": (0.45, 0.65, 1.0),
    "red": (1.0, 0.45, 0.45),
    "purple": (0.75, 0.55, 1.0),
}


def _fitz():
    import fitz                                     # 懒加载：没装也不至于 import 就炸
    return fitz


def open_doc(data=None, path=None):
    """打开一份 PDF：给 bytes 或给磁盘路径。返回 (doc, 错误串)。"""
    fitz = _fitz()
    try:
        if data is not None:
            return fitz.open(stream=data, filetype="pdf"), ""
        return fitz.open(path), ""
    except Exception as e:
        return None, "打不开这个 PDF：%s" % str(e)[:120]


def color_of(name):
    if isinstance(name, (list, tuple)) and len(name) == 3:
        return tuple(float(x) for x in name)
    return COLORS.get(str(name or "yellow").lower(), COLORS["yellow"])


# ── ① 只读模型 ────────────────────────────────────────────
def page_info(doc, n: int) -> dict:
    pg = doc.load_page(n - 1)
    txt = pg.get_text("text") or ""
    return {"n": n, "w": round(pg.rect.width, 1), "h": round(pg.rect.height, 1),
            "rotation": int(pg.rotation or 0), "chars": len(txt.strip())}


def info(doc) -> dict:
    """整篇的模型：页、元信息、目录、批注清单、是不是扫描件。"""
    pages = [page_info(doc, i + 1) for i in range(doc.page_count)]
    meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    try:
        outline = [[lv, t, p + 1] for lv, t, p in (doc.get_toc() or [])][:200]
    except Exception:
        outline = []
    return {"ok": True, "pages": pages, "count": doc.page_count,
            "meta": meta, "outline": outline,
            "scanned": bool(pages) and all(p["chars"] < 8 for p in pages),
            "annots": annots(doc)}


def text_layer(doc, n: int) -> dict:
    """第 n 页的**文字层**：行 + span（含 bbox/字号/字体）。

    前端拿它做：点选文字（点一下选一整行/一个 span）、复制、"摘录到库"、在命中处画东西。
    """
    if n < 1 or n > doc.page_count:
        return {"ok": False, "error": "页码 %d 越界（共 %d 页）" % (n, doc.page_count)}
    pg = doc.load_page(n - 1)
    out = []
    for b in (pg.get_text("dict") or {}).get("blocks") or []:
        if b.get("type") != 0:                       # 0=文字块，1=图片块
            continue
        for ln in b.get("lines") or []:
            spans = []
            for sp in ln.get("spans") or []:
                t = sp.get("text") or ""
                if not t.strip():
                    continue
                spans.append({"text": t, "bbox": [round(x, 1) for x in sp.get("bbox")],
                              "origin": [round(x, 1) for x in (sp.get("origin") or ())],
                              "size": round(float(sp.get("size") or 0), 1),
                              "color": int(sp.get("color") or 0),
                              "font": sp.get("font") or ""})
            if spans:
                out.append({"bbox": [round(x, 1) for x in ln.get("bbox")],
                            "spans": spans,
                            "text": "".join(s["text"] for s in spans)})
    return {"ok": True, "page": n, "lines": out, "count": len(out),
            "w": round(pg.rect.width, 1), "h": round(pg.rect.height, 1),
            "rotation": int(pg.rotation or 0)}


# PyMuPDF 的批注类型名 → 我们的批注种类（写进去和读回来**必须同一套词**：
# 便签写的是 note，PyMuPDF 的类型名是 Text，早先读回来变成 "text"，前端和验收都跟着拧）
TYPE2KIND = {
    "text": "note",            # Text = 便签（小图标 + 弹出内容）
    "freetext": "text",        # FreeText = 直接画在页面上的文本框
    "square": "rect",          # Square = 矩形框
    "rect": "rect",
}


def _annot_row(a, page_no: int) -> dict:
    """一条批注 → 可 JSON 化的行。**页码由调用方给**：刚 add 的 annot 上 `a.page` 可能是 None。"""
    try:
        inf = a.info or {}
    except Exception:
        inf = {}
    t = getattr(a, "type", "")
    kind = t[1] if isinstance(t, (list, tuple)) and len(t) > 1 else str(t or "")
    low = (kind or "").lower()
    return {"id": str(a.xref), "page": int(page_no), "kind": TYPE2KIND.get(low, low), "type": kind,
            "rect": [round(x, 1) for x in a.rect],
            "content": inf.get("content") or "",
            "author": inf.get("title") or "",
            "at": inf.get("creationDate") or ""}


def annots(doc, page: int = 0) -> list:
    """已有批注清单（可按页过滤）"""
    out = []
    rng = [page - 1] if page else range(doc.page_count)
    for i in rng:
        if i < 0 or i >= doc.page_count:
            continue
        for a in (doc.load_page(i).annots() or []):
            try:
                out.append(_annot_row(a, i + 1))
            except Exception as e:
                out.append({"id": "", "page": i + 1, "kind": "?", "error": str(e)[:60]})
    return out


# ── ② 渲染 ────────────────────────────────────────────────
def render_page(doc, n: int, width: int = 1200) -> dict:
    fitz = _fitz()
    if n < 1 or n > doc.page_count:
        return {"error": "页码 %d 越界（共 %d 页）" % (n, doc.page_count)}
    pg = doc.load_page(n - 1)
    zoom = max(0.2, min(4.0, float(width) / max(1.0, pg.rect.width)))
    pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return {"png": pix.tobytes("png"), "w": pix.width, "h": pix.height,
            "zoom": round(zoom, 3), "page": n, "count": doc.page_count}


# ── ③ 编辑原语：批注 ──────────────────────────────────────
def add_annot(doc, page: int, kind: str, rects, text: str = "", color="yellow",
              author: str = "", size: float = 11.0) -> dict:
    """加一条批注。rects 是 PDF 坐标的矩形数组（高亮可以多条，一行一个）。

    - highlight/underline/strikeout/squiggly：走"文字标记"对象，标在给定矩形上
    - note：便签（小图标 + 弹出内容）
    - text：文本框（直接画在页面上，边看边读）
    - rect：矩形框
    - ink：自由画（rects 当作笔迹点串处理在 server 侧不必要，这里按矩形近似）
    """
    fitz = _fitz()
    kind = str(kind or "highlight").lower()
    if kind not in ANNOT_KINDS:
        return {"ok": False, "error": "不认识的批注类型：%s（可用：%s）"
                                      % (kind, "/".join(ANNOT_KINDS))}
    if page < 1 or page > doc.page_count:
        return {"ok": False, "error": "页码 %d 越界（共 %d 页）" % (page, doc.page_count)}
    rs = []
    for r in (rects or []):
        if isinstance(r, dict):
            r = [r.get("x0"), r.get("y0"), r.get("x1"), r.get("y1")]
        if not isinstance(r, (list, tuple)) or len(r) != 4:
            return {"ok": False, "error": "rects 要是 4 个数的矩形数组：[[x0,y0,x1,y1], …]"}
        rs.append(fitz.Rect(float(r[0]), float(r[1]), float(r[2]), float(r[3])))
    if not rs:
        return {"ok": False, "error": "rects 不能为空（要标在哪儿？）"}
    pg = doc.load_page(page - 1)
    col = color_of(color)
    made = []
    for r in rs:
        if kind == "highlight":
            a = pg.add_highlight_annot(r)
        elif kind == "underline":
            a = pg.add_underline_annot(r)
        elif kind == "strikeout":
            a = pg.add_strikeout_annot(r)
        elif kind == "squiggly":
            a = pg.add_squiggly_annot(r)
        elif kind == "note":
            a = pg.add_text_annot(fitz.Point(r.x0, r.y1), text or "批注")
        elif kind == "text":
            a = pg.add_freetext_annot(r, text or "", fontsize=size,
                                      fontname="china-s", text_color=(0.1, 0.1, 0.1))
        elif kind == "rect":
            a = pg.add_rect_annot(r)
        else:                                        # ink：用两条对角线当一笔（够用，真笔迹下一步）
            a = pg.add_ink_annot([[fitz.Point(r.x0, r.y0), fitz.Point(r.x1, r.y1)]])
        if col:
            # 高亮/下划线/删除线/波浪线只有"描边色"（填充色 PyMuPDF 会忽略并告警）
            a.set_colors(stroke=col)
            if kind == "rect":
                a.set_colors(stroke=col, fill=None)
        if text and kind != "text":
            a.set_info(content=text)
        if author:
            a.set_info(title=author)
        a.update()
        made.append(str(a.xref))
    return {"ok": True, "made": made, "count": len(made), "kind": kind, "page": page}


def del_annot(doc, xrefs) -> dict:
    """删批注（按 xref）。"""
    ids = [int(x) for x in (xrefs or []) if str(x).strip().isdigit()]
    if not ids:
        return {"ok": False, "error": "xrefs 要是批注 id 数组（GET /pdf 的 annots[].id）"}
    n = 0
    for i in ids:
        for pi in range(doc.page_count):
            pg = doc.load_page(pi)
            hit = [a for a in (pg.annots() or []) if a.xref == i]
            if hit:
                pg.delete_annot(hit[0])
                n += 1
                break
    return {"ok": True, "deleted": n, "asked": len(ids)}


# ── ③ 编辑原语：页面 ──────────────────────────────────────
def pages_op(doc, op: str, n: int = 0, to: int = 0, nums=None) -> dict:
    """页面操作：rotate（n 转 90 度）/ delete（删 n）/ move（n → to）/ extract（nums 提取成新的一篇）。"""
    op = str(op or "").lower()
    if op == "rotate":
        if n < 1 or n > doc.page_count:
            return {"ok": False, "error": "页码越界"}
        pg = doc.load_page(n - 1)
        pg.set_rotation((int(pg.rotation or 0) + 90) % 360)
        return {"ok": True, "op": op, "page": n, "rotation": pg.rotation}
    if op == "delete":
        if doc.page_count <= 1:
            return {"ok": False, "error": "只剩一页了，删了就没内容（要删整篇请用回收站）"}
        if n < 1 or n > doc.page_count:
            return {"ok": False, "error": "页码越界"}
        doc.delete_page(n - 1)
        return {"ok": True, "op": op, "deleted": n, "count": doc.page_count}
    if op == "move":
        if not (1 <= n <= doc.page_count and 1 <= to <= doc.page_count):
            return {"ok": False, "error": "页码越界"}
        doc.move_page(n - 1, to - 1)
        return {"ok": True, "op": op, "from": n, "to": to}
    if op == "extract":
        want = [int(x) for x in (nums or [])]
        if not want:
            return {"ok": False, "error": "extract 要给 nums，比如 [1,3,5]"}
        nd = _fitz().open()
        for i in want:
            if 1 <= i <= doc.page_count:
                nd.insert_pdf(doc, from_page=i - 1, to_page=i - 1)
        if nd.page_count == 0:
            return {"ok": False, "error": "提取出来是空的（页码都不在范围内？）"}
        data = nd.tobytes()
        nd.close()
        return {"ok": True, "op": op, "bytes": data, "pages": len(want)}
    return {"ok": False, "error": "不认识的页面操作：%s（可用 rotate/delete/move/extract）" % op}


# ── ④ 导出 ────────────────────────────────────────────────
def export_text(doc, sep: str = "\n\n---\n\n") -> str:
    parts = []
    for i in range(doc.page_count):
        t = (doc.load_page(i).get_text("text") or "").strip()
        parts.append("## 第 %d 页\n\n%s" % (i + 1, t) if t else "## 第 %d 页\n\n（这页没有文字层）" % (i + 1))
    return sep.join(parts)


# ── ⑤ 保存 ────────────────────────────────────────────────
def save_bytes(doc, incremental: bool = True) -> dict:
    """保存成 bytes。**默认增量**（保留原字节，改哪写哪）。

    增量有个前提：文件没被"重排"过（删页/移动页会让增量变得又大又容易踩 xref 坑）——
    所以这里：能增量就增量，不行就整篇重写；两条路都给回 `mode`，别让人猜。
    """
    try:
        if incremental and not getattr(doc, "is_repaired", False):
            return {"data": doc.tobytes(garbage=3, deflate=True, incremental=True), "mode": "incremental"}
    except Exception:
        pass
    return {"data": doc.tobytes(garbage=3, deflate=True), "mode": "full"}


def probe(path_or_bytes) -> dict:
    """体检用：把一份 PDF 的关键事实吐出来（页/字/字体/批注/是否扫描件）。"""
    data = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else None
    path = None if data is not None else str(path_or_bytes)
    doc, err = open_doc(data=data, path=path)
    if doc is None:
        return {"ok": False, "error": err}
    try:
        d = info(doc)
        d["fonts"] = sorted({f[3] for i in range(doc.page_count)
                             for f in doc.load_page(i).get_fonts(full=True)})
        d["subset_fonts"] = [f for f in d["fonts"] if "+" in f]
        d["bytes"] = len(data) if data is not None else os.path.getsize(path)
        d["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        return d
    finally:
        doc.close()


# ── ③b 擦字重画：字体子集 + 按行重绘 ───────────────────────
# 为什么这么麻烦：PDF 里嵌的字体是**子集**（JYVKAE+DengXian），拿它画不出新字；换内置 CJK 字体又
# **不嵌字体**（实测 /FontFile 都没有），MuPDF 自己能画、WPS/Edge 打开是空白。所以：按原字体名找到
# 本机字体文件 → 用 fontTools 只留"这次要画的那些字" → 用这个小子集去画（16MB 的 Deng.ttf → 15KB）。
FONT_FILES = {
    "dengxianlight": "Dengl.ttf", "dengxianbold": "Dengb.ttf", "dengxian": "Deng.ttf",
    "simsun": "simsun.ttc", "nsimsun": "simsun.ttc", "songti": "simsun.ttc", "stsong": "simsun.ttc",
    "simhei": "simhei.ttf", "heiti": "simhei.ttf", "stheiti": "simhei.ttf",
    "microsoftyaheiui": "msyh.ttc", "microsoftyahei": "msyh.ttc", "yahei": "msyh.ttc",
    "simkai": "simkai.ttf", "kaiti": "simkai.ttf", "stkaiti": "simkai.ttf",
    "simfang": "simfang.ttf", "fangsong": "simfang.ttf", "stfangsong": "simfang.ttf",
    "timesnewromanpsmt": "times.ttf", "timesnewroman": "times.ttf", "times": "times.ttf",
    "arial": "arial.ttf", "arialmt": "arial.ttf", "helvetica": "arial.ttf",
    "calibri": "calibri.ttf", "cambria": "cambria.ttc", "consolas": "consola.ttf",
    "georgia": "georgia.ttf", "verdana": "verdana.ttf", "tahoma": "tahoma.ttf",
}
FONT_DIR = os.path.join(os.environ.get("WINDIR") or r"C:\Windows", "Fonts")
# 兜底顺序：找的字体画不出某个字时按这个顺序换（雅黑覆盖最全）
FALLBACK_FONT_FILES = ["msyh.ttc", "Deng.ttf", "simsun.ttc", "simhei.ttf"]

# 内置（不嵌字体）的退路：fontTools 不在时用，画得出来但别的阅读器可能空白 —— 回执里会说明
BUILTIN_CJK = "china-s"


def clean_font_name(name: str) -> str:
    """PDF 里的字体名 → 干净名字：去掉子集前缀（JYVKAE+）与逗号后缀。"""
    n = re.sub(r"^[A-Z]{6}\+", "", str(name or ""))
    return n.split(",")[0].strip()


def font_file_for(name: str) -> str:
    """PDF 里的字体名 → 本机字体文件路径（找不到回空串）。"""
    key = re.sub(r"[^a-z]", "", clean_font_name(name).lower())
    if not key:
        return ""
    for k in sorted(FONT_FILES, key=len, reverse=True):     # 长名优先：dengxianlight 先于 dengxian
        if key.startswith(k) or k in key:
            p = os.path.join(FONT_DIR, FONT_FILES[k])
            if os.path.exists(p):
                return p
    return ""


def _font_obj(path: str):
    fitz = _fitz()
    try:
        return fitz.Font(fontfile=path) if path else fitz.Font(fontname=BUILTIN_CJK)
    except Exception:
        return None


def _covers(path: str, text: str) -> bool:
    f = _font_obj(path)
    if f is None:
        return False
    return all(f.has_glyph(ord(c)) for c in set(text) if not c.isspace())


def pick_font(orig_name: str, text: str) -> str:
    """挑一个"画得出这段字"的字体文件：先按原字体名找，画不全再按兜底顺序换。"""
    first = font_file_for(orig_name)
    if first and _covers(first, text):
        return first
    for fn in FALLBACK_FONT_FILES:
        p = os.path.join(FONT_DIR, fn)
        if os.path.exists(p) and _covers(p, text):
            return p
    return first or ""


def _subset_file(src: str, chars, out: str) -> bool:
    """把 src 里 chars 用到的字形抽出来写成 out（fontTools）。"""
    import logging
    from fontTools import subset as _sub
    _log = logging.getLogger("fontTools.subset")
    _lvl = _log.level
    _log.setLevel(logging.ERROR)                     # fontTools 子集化会往 stderr 吐一堆 NOT subset
    opts = _sub.Options()
    opts.layout_features = ["*"]
    opts.name_IDs = ["*"]
    opts.notdef_outline = True
    opts.drop_tables += ["meta"]                     # meta 表子集化会告警，直接丢
    opts.font_number = 0                             # .ttc 是字体集合，取第 0 个（雅黑/宋体都在 0）
    font = _sub.load_font(src, opts)
    keep = _sub.Subsetter(options=opts)
    keep.populate(text="".join(sorted(chars)))
    keep.subset(font)
    try:
        _sub.save_font(font, out, opts)
    finally:
        font.close()
        _log.setLevel(_lvl)
    return os.path.getsize(out) > 0


class FontSubset:
    """一次编辑里的字体子集：同一种字体只做了一个小文件（一篇 PDF 只嵌很小一份）。"""

    def __init__(self, workdir: str = ""):
        self.dir = workdir or tempfile.mkdtemp(prefix="zz-pdffont-")
        self.mine = not workdir
        self.chars = {}          # 字体文件 → set(字)
        self.made = {}           # 字体文件 → 子集文件
        self.failed = []         # 抽不出子集的（回执里说明，走内置字体兜底）

    def add(self, path: str, text: str):
        if path:
            self.chars.setdefault(path, set()).update(text or "")
        return self

    def build(self):
        for i, (path, chars) in enumerate(sorted(self.chars.items())):
            out = os.path.join(self.dir, "sub%d%s" % (i, os.path.splitext(path)[1] or ".ttf"))
            try:
                if _subset_file(path, chars, out):
                    self.made[path] = out
            except Exception as e:
                self.failed.append({"font": os.path.basename(path), "why": str(e)[:80]})
        return self

    def file(self, path: str) -> str:
        return self.made.get(path) or ""

    def tag(self, path: str) -> str:
        """这个字体在页面资源里的**名字**。

        必须"同文件同字集 → 同名、否则不同名"：PyMuPDF 的 insert_text 是按**名字**复用页面字体资源的，
        名字撞了就会拿上一个子集去画新字 —— 新字形的 id 不在那个子集里，文字层提取出来是 \x00
        （看着就跟"没画上"一样，2026-10-01 实测踩过）。
        """
        import hashlib
        chars = "".join(sorted(self.chars.get(path) or []))
        h = hashlib.md5((os.path.basename(path or "builtin") + "|" + chars).encode("utf-8")).hexdigest()
        return "PF" + h[:8]

    def close(self):
        if self.mine:
            shutil.rmtree(self.dir, ignore_errors=True)


def _color_tuple(v):
    """颜色：'#rrggbb' / [r,g,b] / 0~1 浮点三元组 / PDF 整数色 → (r,g,b) 0~1。"""
    fitz = _fitz()
    if v is None:
        return None
    if isinstance(v, str):
        t = v.strip().lstrip("#")
        if re.fullmatch(r"[0-9a-fA-F]{6}", t):
            return (int(t[0:2], 16) / 255.0, int(t[2:4], 16) / 255.0, int(t[4:6], 16) / 255.0)
        if t.lower() in COLORS:
            return COLORS[t.lower()]
        return None
    if isinstance(v, (list, tuple)) and len(v) == 3:
        try:
            vals = [float(x) for x in v]
        except Exception:
            return None
        if max(vals) > 1.0:
            vals = [x / 255.0 for x in vals]
        return tuple(vals)
    if isinstance(v, int):
        try:
            return fitz.sRGB_to_pdf(v)
        except Exception:
            return None
    return None


def _rect_of(v):
    fitz = _fitz()
    if isinstance(v, dict):
        v = [v.get("x0"), v.get("y0"), v.get("x1"), v.get("y1")]
    if not isinstance(v, (list, tuple)) or len(v) != 4 or any(x is None for x in v):
        return None
    try:
        r = fitz.Rect(float(v[0]), float(v[1]), float(v[2]), float(v[3]))
    except Exception:
        return None
    return r if r.width > 0 and r.height > 0 else None


def page_lines(pg) -> list:
    """一页的文字行（行 + span，带 origin/size/font/color）——重画时靠它找基线与原字体。"""
    out = []
    for b in (pg.get_text("dict") or {}).get("blocks") or []:
        if b.get("type") != 0:
            continue
        for ln in b.get("lines") or []:
            spans = []
            for sp in ln.get("spans") or []:
                if not (sp.get("text") or "").strip():
                    continue
                spans.append({"text": sp.get("text") or "",
                              "bbox": [round(x, 1) for x in sp.get("bbox")],
                              "origin": list(sp.get("origin") or ()),
                              "size": round(float(sp.get("size") or 0), 1),
                              "color": int(sp.get("color") or 0),
                              "font": sp.get("font") or ""})
            if spans:
                out.append({"bbox": [round(x, 1) for x in ln.get("bbox")], "spans": spans,
                            "text": "".join(x["text"] for x in spans)})
    return out


def _line_at(pg, rect):
    """rect 落在哪一行上（按重叠面积取最大的一行）。"""
    best, best_a = None, 0.0
    for ln in page_lines(pg):
        r = _fitz().Rect(ln["bbox"])
        a = abs((r & rect).get_area()) if (r & rect) else 0.0
        if a > best_a:
            best, best_a = ln, a
    return best


def apply_text_ops(doc, ops, expand: float = 1.2) -> dict:
    """**擦字重画**：一批 {page, rect, text, size?, color?, font?, align?, shrink?}。

    每一步都是"擦掉 rect 里的原字（保留底纹与图）→ 在原来的基线上按原字体原字号重画新字"。
    新字比原位宽时按可用宽度缩字号（shrink=False 就不缩，回执里标 overflow）。
    一次调用里所有新字只嵌**一个**字体子集。
    """
    fitz = _fitz()
    plan = []
    for op in (ops or []):
        if not isinstance(op, dict):
            return {"ok": False, "error": "ops 里每一项要是对象"}
        n = int(op.get("page") or 1)
        if n < 1 or n > doc.page_count:
            return {"ok": False, "error": "页码 %d 越界（共 %d 页）" % (n, doc.page_count)}
        rect = _rect_of(op.get("rect") or op.get("bbox"))
        if rect is None:
            return {"ok": False, "error": "rect 要 4 个数 [x0,y0,x1,y1]（要擦哪儿、画哪儿）"}
        pg = doc.load_page(n - 1)
        sp = None
        ln = _line_at(pg, rect)
        if ln:
            for x in ln["spans"]:
                r = fitz.Rect(x["bbox"])
                if (r & rect) and abs((r & rect).get_area()) > 0:
                    sp = x
                    break
            sp = sp or (ln["spans"][0] if ln["spans"] else None)
        text = str(op.get("text") if op.get("text") is not None else "")
        size = float(op.get("size") or (sp or {}).get("size") or max(6.0, rect.height * 0.78))
        org = (sp or {}).get("origin") or []
        origin = (float(org[0]), float(org[1])) if len(org) == 2 else (rect.x0, rect.y1 - rect.height * 0.22)
        color = _color_tuple(op.get("color")) or _color_tuple((sp or {}).get("color")) or (0, 0, 0)
        plan.append({"n": n, "rect": rect, "text": text, "size": size, "origin": origin,
                     "color": color, "font": str(op.get("font") or (sp or {}).get("font") or ""),
                     "align": str(op.get("align") or "left").lower(),
                     "shrink": op.get("shrink", True),
                     "was": (sp or {}).get("text") or (ln or {}).get("text") or ""})
    if not plan:
        return {"ok": False, "error": "ops 是空的（没东西改）"}

    sub = FontSubset()
    try:
        for it in plan:
            if it["text"]:
                it["ffile"] = pick_font(it["font"], it["text"])
                sub.add(it["ffile"], it["text"])
        sub.build()

        # ① 擦：所有框一起加，再逐页 apply（顺序不能反，apply 会重写内容流）
        touched = []
        for it in plan:
            pg = doc.load_page(it["n"] - 1)
            if it["n"] not in touched:
                touched.append(it["n"])
            pg.add_redact_annot(it["rect"] + (-expand, -expand, expand, expand), fill=None)
        for n in touched:
            doc.load_page(n - 1).apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                                  graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                                                  text=fitz.PDF_REDACT_TEXT_REMOVE)

        # ② 画：新字比原位宽就缩字号（缩到 55% 为止，再宽就标 overflow）
        made, warns = [], []
        for it in plan:
            if not it["text"]:
                made.append({"page": it["n"], "text": "", "removed_only": True})
                continue
            pg = doc.load_page(it["n"] - 1)
            fp = sub.file(it["ffile"])
            f = _font_obj(fp) or _font_obj("")
            need = f.text_length(it["text"], it["size"]) if f else 0.0
            avail = max(6.0, it["rect"].width * 1.02)
            size = it["size"]
            if need > avail and it["shrink"]:
                size = max(it["size"] * 0.55, it["size"] * avail / need)
                need = f.text_length(it["text"], size) if f else need
            x = it["origin"][0]
            if it["align"] == "center":
                x = it["rect"].x0 + (it["rect"].width - need) / 2.0
            elif it["align"] == "right":
                x = it["rect"].x1 - need
            over = need > avail + 0.5
            if over:
                warns.append({"page": it["n"], "why": "新字比原位宽（%d→%d pt），已按可放宽度缩字号"
                                                     % (round(it["rect"].width), round(need))})
            kw = {"fontname": sub.tag(it["ffile"]), "fontsize": round(size, 2), "color": it["color"]}
            if fp:
                kw["fontfile"] = fp
            else:
                kw["fontname"] = BUILTIN_CJK                   # 兜底：不嵌字体（回执里会说明）
            pg.insert_text((x, it["origin"][1]), it["text"], **kw)
            made.append({"page": it["n"], "text": it["text"], "size": round(size, 2),
                         "was": it["was"], "overflow": over,
                         "embedded": bool(fp), "font": os.path.basename(it["ffile"]) if it["ffile"] else ""})
        out = {"ok": True, "changed": len([m for m in made if m.get("text")]),
               "removed": len([m for m in made if m.get("removed_only")]), "made": made,
               "fonts": {os.path.basename(k): os.path.getsize(v) for k, v in sub.made.items()},
               "font_failed": sub.failed, "warnings": warns}
        if sub.failed:
            out["note"] = "有字体抽不出子集，那几行用了内置字体（别的阅读器可能看不见那几个字）"
        return out
    finally:
        sub.close()


def find_replace(doc, q: str, rep: str, pages=None, cap: int = 400, color=None,
                 expand: float = 1.2) -> dict:
    """**全篇查找替换**：命中的那**一整行**擦掉重画（只擦命中那几个字会把后面的字挤位）。"""
    q = str(q or "")
    if not q:
        return {"ok": False, "error": "要替换什么？q 不能空"}
    rng = [int(x) for x in (pages or [])] or list(range(1, doc.page_count + 1))
    ops, hits = [], []
    low = q.lower()
    for n in rng:
        if n < 1 or n > doc.page_count:
            continue
        pg = doc.load_page(n - 1)
        rects = pg.search_for(q) or (pg.search_for(low) if low != q else []) or []
        seen = set()
        for r in rects:
            ln = _line_at(pg, r)
            if not ln:
                continue
            key = (n, tuple(ln["bbox"]))
            if key in seen:
                continue
            seen.add(key)
            new = (ln["text"] or "").replace(q, rep)
            if low != q:
                new = re.sub(re.escape(q), rep, ln["text"] or "", flags=re.I)
            if new == ln["text"]:
                continue
            sp = ln["spans"][0] if ln["spans"] else {}
            ops.append({"page": n, "rect": ln["bbox"], "text": new, "size": sp.get("size"),
                        "color": color if color is not None else sp.get("color"),
                        "font": sp.get("font"), "align": "left"})
            hits.append({"page": n, "before": ln["text"], "after": new})
            if len(ops) >= cap:
                break
        if len(ops) >= cap:
            break
    if not ops:
        return {"ok": True, "count": 0, "hits": [], "why": "没找到「%s」" % q}
    r = apply_text_ops(doc, ops, expand=expand)
    r.update({"op": "replace", "q": q, "rep": rep, "count": len(ops), "hits": hits[:50]})
    return r


def insert_text(doc, page: int, x: float, y: float, text: str, size: float = 12.0,
                font: str = "", color=(0, 0, 0)) -> dict:
    """在页面任意位置**直接写进内容**（不是批注）。x,y 是基线左端（PDF 坐标：原点左上、y 朝下）。"""
    if page < 1 or page > doc.page_count:
        return {"ok": False, "error": "页码 %d 越界（共 %d 页）" % (page, doc.page_count)}
    text = str(text or "")
    if not text:
        return {"ok": False, "error": "text 不能空"}
    sub = FontSubset()
    try:
        ff = pick_font(font, text)
        sub.add(ff, text).build()
        fp = sub.file(ff)
        pg = doc.load_page(page - 1)
        kw = {"fontname": sub.tag(ff), "fontsize": float(size),
              "color": _color_tuple(color) or (0, 0, 0)}
        if fp:
            kw["fontfile"] = fp
        else:
            kw["fontname"] = BUILTIN_CJK
        pg.insert_text((float(x), float(y)), text, **kw)
        return {"ok": True, "page": page, "text": text, "embedded": bool(fp),
                "font_size": os.path.getsize(fp) if fp else 0, "font_failed": sub.failed}
    finally:
        sub.close()


def stamp_page_numbers(doc, start: int = 1, fmt: str = "第 %d 页 / 共 %d 页", size: float = 9.0,
                       where: str = "bottom-center", color=(0.35, 0.35, 0.35), margin: float = 30.0,
                       pages=None) -> dict:
    """给每页盖页码（**写进内容**，不是批注）。where：bottom-center / bottom-right / top-center。"""
    fitz = _fitz()
    total = doc.page_count
    rng = [int(x) for x in (pages or [])] or list(range(1, total + 1))
    texts = [str(fmt or "%d") % (start + n - 1, total) if "%d" in str(fmt) else str(fmt) for n in rng]
    sub = FontSubset()
    try:
        ff = pick_font("", "".join(texts) + "0123456789")
        sub.add(ff, "".join(texts)).build()
        fp = sub.file(ff)
        f = _font_obj(fp)
        made = 0
        kw = {"fontname": sub.tag(ff), "fontsize": float(size),
              "color": _color_tuple(color) or (0.35, 0.35, 0.35)}
        if fp:
            kw["fontfile"] = fp
        else:
            kw["fontname"] = BUILTIN_CJK
        for n, t in zip(rng, texts):
            if n < 1 or n > total:
                continue
            pg = doc.load_page(n - 1)
            w = f.text_length(t, size) if f else len(t) * size * 0.6
            r = pg.rect
            if where == "top-center":
                pos = (r.width / 2.0 - w / 2.0, margin + size)
            elif where == "bottom-right":
                pos = (r.width - margin - w, r.height - margin)
            else:
                pos = (r.width / 2.0 - w / 2.0, r.height - margin)
            pg.insert_text(pos, t, **kw)          # 不用 TextWriter：它是逐字写的，文字层会散成一格一格
            made += 1
        return {"ok": True, "op": "pageno", "stamped": made, "total": total,
                "embedded": bool(fp), "font_failed": sub.failed}
    finally:
        sub.close()


def _wm_png(text: str, size: float, opacity: float, angle: float, color, dpi: int = 160):
    """把水印画成一张带透明的 PNG（斜的）：PIL 画字 → rotate → PNG 字节。"""
    from PIL import Image, ImageDraw, ImageFont
    px = max(8, int(round(float(size) * dpi / 72.0)))
    ff = pick_font("", text) or os.path.join(FONT_DIR, "msyh.ttc")
    if ff.lower().endswith(".ttc"):
        ff = ff + ""                                   # PIL 认 .ttc 的第 0 个字体
    font = ImageFont.truetype(ff, px)
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    box = probe.textbbox((0, 0), text, font=font)
    w, h = max(2, box[2] - box[0] + 8), max(2, box[3] - box[1] + 8)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    r, g, b = _color_tuple(color) or (0.5, 0.5, 0.5)
    a = int(max(0.0, min(1.0, float(opacity))) * 255)
    ImageDraw.Draw(img).text((4 - box[0], 4 - box[1]), text, font=font,
                             fill=(int(r * 255), int(g * 255), int(b * 255), a))
    if angle:
        img = img.rotate(float(angle), expand=True, resample=Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), img.width, img.height, dpi


def stamp_watermark(doc, text: str, size: float = 46.0, opacity: float = 0.12, angle: float = 45.0,
                    color=(0.5, 0.5, 0.5), pages=None) -> dict:
    """斜水印盖在页面中间。

    **为什么是图不是字**：PyMuPDF 的 TextWriter 一给旋转矩阵就把整段字丢掉（实测 θ=45/90 文字层
    与画面都是空的），所以水印走 PIL 画字 + 旋转 + 透明 PNG，再贴到页心 —— 看起来是斜的、
    半透明、任何阅读器都正常，**但它是图**：搜不到、选不中、要去掉只能重做一版。回执里会写明。
    """
    fitz = _fitz()
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "watermark 的文字不能空"}
    total = doc.page_count
    rng = [int(x) for x in (pages or [])] or list(range(1, total + 1))
    try:
        png, iw, ih, dpi = _wm_png(text, size, opacity, angle, color)
    except Exception as e:
        return {"ok": False, "error": "画水印失败：%s" % str(e)[:120]}
    sc = 72.0 / float(dpi)
    made = 0
    for n in rng:
        if n < 1 or n > total:
            continue
        pg = doc.load_page(n - 1)
        W, H = iw * sc, ih * sc
        cx, cy = pg.rect.width / 2.0, pg.rect.height / 2.0
        pg.insert_image(fitz.Rect(cx - W / 2.0, cy - H / 2.0, cx + W / 2.0, cy + H / 2.0),
                        stream=png, overlay=True)
        made += 1
    return {"ok": True, "op": "watermark", "stamped": made, "text": text, "angle": angle,
            "as_image": True, "png_bytes": len(png),
            "note": "水印是**图**（PyMuPDF 旋转文字会丢字）：搜不到、选不中，要去掉只能重做一版"}


def merge_pdf(doc, blob, where="end") -> dict:
    """把另一篇 PDF 并进来（where：end / start / 页码下标）。**不写回原篇**由调用方决定。"""
    fitz = _fitz()
    if not blob:
        return {"ok": False, "error": "要合并的那篇没内容"}
    try:
        other = fitz.open(stream=bytes(blob), filetype="pdf")
    except Exception as e:
        return {"ok": False, "error": "打不开要合并的 PDF：%s" % str(e)[:80]}
    try:
        before = doc.page_count
        if where in ("end", None, ""):
            at = -1
        elif where == "start":
            at = 0
        else:
            at = max(0, min(before, int(where)))
        doc.insert_pdf(other, start_at=at)
        return {"ok": True, "op": "merge", "added": other.page_count,
                "pages": doc.page_count, "at": at, "before": before}
    except Exception as e:
        return {"ok": False, "error": "合并失败：%s" % str(e)[:120]}
    finally:
        other.close()


def export_images(doc, dpi: int = 110, pages=None, cap: int = 30, fmt: str = "png") -> dict:
    """每页导出成图片（默认 PNG，110 dpi）。一次最多 cap 页，免得一口气几百张。"""
    asked = [int(x) for x in (pages or [])] or list(range(1, doc.page_count + 1))
    if len(asked) > cap:                       # 按"要了几页"判，不是按过滤后剩几页判
        return {"ok": False, "error": "一次最多导出 %d 页（这次要 %d 页；用 pages 指定要哪几页）"
                                      % (cap, len(asked))}
    rng = [n for n in asked if 1 <= n <= doc.page_count]
    out = []
    for n in rng:
        pg = doc.load_page(n - 1)
        pix = pg.get_pixmap(dpi=max(36, min(300, int(dpi))), alpha=False)
        out.append({"page": n, "data": pix.tobytes(fmt), "w": pix.width, "h": pix.height,
                    "ext": fmt})
    return {"ok": True, "op": "images", "count": len(out), "images": out, "dpi": int(dpi)}


def bake_annots(doc, dpi: int = 150, pages=None) -> dict:
    """把批注**烤进页面内容**（扁平化）。

    如实说：PyMuPDF 没有"把批注转成矢量内容"的接口，这里的做法是**把整页连同批注渲成图再贴回去**
    —— 看起来一模一样、别的软件也改不动了，但这页**不再是文字**（等于变扫描件）。
    """
    fitz = _fitz()
    rng = [int(x) for x in (pages or [])] or list(range(1, doc.page_count + 1))
    made = 0
    for n in rng:
        if n < 1 or n > doc.page_count:
            continue
        pg = doc.load_page(n - 1)
        if not list(pg.annots() or []):        # annots() 是生成器，直接判真永远为真
            continue
        pix = pg.get_pixmap(dpi=max(72, min(300, int(dpi))), alpha=False)
        for a in list(pg.annots() or []):
            pg.delete_annot(a)
        pg.clean_contents()
        pg.add_redact_annot(pg.rect, fill=(1, 1, 1))
        pg.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                            graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                            text=fitz.PDF_REDACT_TEXT_REMOVE)
        pg.insert_image(pg.rect, pixmap=pix)
        made += 1
    return {"ok": True, "op": "flatten", "pages": made, "dpi": int(dpi),
            "note": "已烤进页面（这 %d 页不再是文字，等于扫描件）" % made}
