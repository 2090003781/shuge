"""分块器 —— 实现 FORMAT-数据格式规范-v0.4 §2 的 chunk 结构。

第一期只做基础文档（files.kind ∈ markdown | text | code）：
  markdown → 按标题层级切；chunk.kind = text（代码块单独成块 kind=code_block）
  text     → 按段落/长度切
  code     → 整文件或按顶层定义切

产出字段（与 chunks 表列一一对应）：
  {seq, kind, heading, title_path(list), text, char_count, content_hash, locator}
"""
from __future__ import annotations

import hashlib

VERSION = "c1"          # 分块策略版本：**改了切法就 +1**（索引签名因此变化 → 全量重建）
MAX_CHARS = 1500        # 单块上限（manifest.chunk_max_chars 的同名默认）
OVERLAP = 150           # 超长块二次切分时的重叠
CODE_MAX = 3000         # code 文件超过此长度按顶层定义切

_HEADING = None


def _re():
    global _HEADING
    if _HEADING is None:
        import re
        _HEADING = {
            "heading": re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M),
            "fence": re.compile(r"```.*?```", re.DOTALL),
            "code_top": re.compile(r"^(?:def|class|func|function|public|private|const|async)\s.*$", re.M),
        }
    return _HEADING


def _hash(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hard_split(para: str, max_chars: int) -> list[str]:
    """无空行的长文本：先按行，单行仍超长就硬切。保证每段 <= max_chars"""
    if len(para) <= max_chars:
        return [para]
    out, buf = [], ""
    for line in para.split("\n"):
        while len(line) > max_chars:
            head, line = line[:max_chars], line[max_chars:]
            if buf:
                out.append(buf)
                buf = ""
            out.append(head)
        if buf and len(buf) + len(line) + 1 > max_chars:
            out.append(buf)
            buf = line
        else:
            buf = (buf + "\n" + line) if buf else line
    if buf:
        out.append(buf)
    return out


def _split_long(text: str, max_chars: int, overlap: int) -> list[str]:
    """超长文本二次切分：按段落边界优先，段落自身超长则硬切；保证每块 <= max_chars"""
    if len(text) <= max_chars:
        return [text]
    parts, buf = [], ""
    for para in text.split("\n\n"):
        for piece in _hard_split(para, max_chars):
            if buf and len(buf) + len(piece) + 2 > max_chars:
                parts.append(buf)
                tail = buf[-overlap:] if overlap and len(buf) > overlap else ""
                if tail and len(tail) + len(piece) + 2 > max_chars:
                    tail = ""                      # 重叠会把这块顶过上限，宁可不重叠
                buf = (tail + "\n\n" + piece) if tail else piece
            else:
                buf = (buf + "\n\n" + piece) if buf else piece
    if buf:
        parts.append(buf)
    return parts


def _with_offsets(body: str, pieces: list, base: int) -> list:
    """给切出来的 pieces 找在 body 里的偏移（重叠块靠 cursor 往前走定位）"""
    out, cursor = [], 0
    for p in pieces:
        head = p[:24]
        i = body.find(head, cursor)
        if i < 0:
            i = body.find(head)
        if i < 0:
            i = cursor if cursor < len(body) else 0
        out.append((base + i, base + i + len(p)))
        cursor = i + 1
    return out


def attach_locators(chunks: list, blocks: list) -> int:
    """按字符偏移把 chunk 落到块树上 → locator（页码 / 类型 / bbox / 覆盖的块）

    有了它，"命中在第 2 页的表格里"这种事才说得清，也为以后"点命中跳原文"打底。
    """
    if not blocks:
        return 0
    bs = [b for b in blocks if (b.get("char_start") or -1) >= 0
          and (b.get("char_end") or -1) > (b.get("char_start") or -1)]
    n = 0
    for c in chunks:
        cs, ce = c.get("char_start"), c.get("char_end")
        if cs is None or ce is None:
            continue
        hit = [b for b in bs if b["char_end"] > cs and b["char_start"] < ce]
        if not hit:
            continue
        pages = sorted({b["page_no"] for b in hit if b.get("page_no")})
        types = []
        for b in hit:
            t = b.get("type") or ""
            if t and t not in types:
                types.append(t)
        c["locator"] = {"page": pages[0] if pages else None, "pages": pages, "types": types,
                        "blocks": [b.get("block_id") or b.get("seq") for b in hit][:12],
                        "bbox": hit[0].get("bbox")}
        n += 1
    return n


def chunk_markdown(content: str, doc_title: str, max_chars: int = MAX_CHARS,
                   overlap: int = OVERLAP) -> list[dict]:
    """按标题层级切；代码块内的 # 不算标题"""
    r = _re()
    # 用等长占位替换代码块，避免代码里的 # 被当标题
    masked = r["fence"].sub(lambda m: "\x00" * len(m.group()), content)
    matches = list(r["heading"].finditer(masked))

    sections: list[tuple[list[str], str, int]] = []   # (title_path, 原样切片, 切片起点)
    if not matches:
        sections.append(([doc_title], content, 0))
    else:
        if matches[0].start() > 0:
            head = content[:matches[0].start()]
            if head.strip():
                sections.append(([doc_title], head, 0))       # 前言
        stack: list[str] = []
        for i, m in enumerate(matches):
            level = len(m.group(1))
            title = m.group(2).strip()
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            stack = stack[:level - 1] + [title]
            sections.append((list(stack), content[start:end], start))

    out = []
    for path, raw, base in sections:
        body = raw.strip()
        if not body:
            continue
        off = base + (len(raw) - len(raw.lstrip()))
        pieces = _split_long(body, max_chars, overlap)
        for piece, (cs, ce) in zip(pieces, _with_offsets(body, pieces, off)):
            out.append({
                "kind": "text",
                "heading": path[-1],
                "title_path": path,
                "text": piece,
                "char_count": len(piece),
                "content_hash": _hash(piece),
                "locator": None,
                "char_start": cs,
                "char_end": ce,
            })
    return out


def chunk_text(content: str, doc_title: str, max_chars: int = MAX_CHARS,
               overlap: int = OVERLAP) -> list[dict]:
    out = []
    body = content.strip()
    off = len(content) - len(content.lstrip())
    pieces = _split_long(body, max_chars, overlap)
    for piece, (cs, ce) in zip(pieces, _with_offsets(body, pieces, off)):
        out.append({
            "kind": "text", "heading": doc_title, "title_path": [doc_title],
            "text": piece, "char_count": len(piece),
            "content_hash": _hash(piece), "locator": None,
            "char_start": cs, "char_end": ce,
        })
    return out


def chunk_code(content: str, doc_title: str, max_chars: int = MAX_CHARS,
               overlap: int = OVERLAP) -> list[dict]:
    """code：短文件整块；长文件按顶层定义切；仍超长则继续切（kind=code_block）"""
    pieces: list[tuple[str, str]] = []
    if len(content) <= CODE_MAX:
        pieces = [(doc_title, content)]
    else:
        r = _re()
        marks = [m.start() for m in r["code_top"].finditer(content)]
        if not marks:
            pieces = [(doc_title, content)]
        else:
            if marks[0] > 0:
                pieces.append((doc_title, content[:marks[0]]))
            for i, s in enumerate(marks):
                e = marks[i + 1] if i + 1 < len(marks) else len(content)
                first_line = content[s:e].split("\n", 1)[0][:80]
                pieces.append((first_line, content[s:e]))

    out, base = [], 0
    for heading, body in pieces:
        if body.strip():
            stripped = body.strip()
            off = base + (len(body) - len(body.lstrip()))
            parts = _split_long(stripped, max_chars, overlap)
            for piece, (cs, ce) in zip(parts, _with_offsets(stripped, parts, off)):
                out.append({
                    "kind": "code_block", "heading": heading, "title_path": [doc_title, heading],
                    "text": piece, "char_count": len(piece),
                    "content_hash": _hash(piece), "locator": None,
                    "char_start": cs, "char_end": ce,
                })
        base += len(body)
    return out


def chunk_file(kind: str, content: str, doc_title: str, max_chars: int = MAX_CHARS,
               overlap: int = OVERLAP) -> list[dict]:
    """按 files.kind 分派"""
    if kind == "markdown":
        return chunk_markdown(content, doc_title, max_chars, overlap)
    if kind == "code":
        return chunk_code(content, doc_title, max_chars, overlap)
    return chunk_text(content, doc_title, max_chars, overlap)
