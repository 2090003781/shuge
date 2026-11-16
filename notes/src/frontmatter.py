# -*- coding: utf-8 -*-
"""YAML \u524d\u7f6e\u5c5e\u6027\uff08Obsidian \u53eb"\u5c5e\u6027"）\u2014\u2014 \u8bfb\u3001\u6539\u3001\u5199\u56de\u3002

\u4e3a\u4ec0\u4e48\u8981\u5b83\uff1a\u8f9e\u7396\u7684 vault \u91cc\u6709 181 \u7bc7\u5e26\u524d\u7f6e\u5c5e\u6027\uff08`tags` 136\u3001`title` 51\u3001
`created/updated/status/type`\u2026\uff09\u3002\u5e93\u4e0d\u8ba4\u5b83\u4eec\u7684\u8bdd\uff0c\u8fdb\u6765\u7684\u7b14\u8bb0\u5c31\u4e22\u4e86\u6807\u7b7e\u3001\u6807\u9898\u53ea\u80fd\u7528\u6587\u4ef6\u540d \u2014\u2014
\u63a5\u4e0b Obsidian \u7684\u6d3b\u5c31\u7f3a\u4e00\u5757\u3002

\u8bbe\u8ba1\uff08\u53cd\u8fc7\u5ea6\u8bbe\u8ba1\uff09\uff1a
\u00b7 \u89e3\u6790\u7528 PyYAML\uff08\u5df2\u5b89\u88c5\uff09\uff1b\u53ea\u8ba4**\u6587\u4ef6\u5f00\u5934**\u7684\u90a3\u4e00\u5757 --- ... ---\uff1b
\u00b7 \u5199\u56de\u7528**\u884c\u7ea7\u66ff\u6362**\uff1a\u53ea\u52a8\u8981\u6539\u7684\u90a3\u51e0\u4e2a\u952e\uff0c\u5176\u4f59\u884c\uff08\u5305\u62ec\u6ce8\u91ca\u4e0e\u987a\u5e8f\uff09\u539f\u6837\u4fdd\u7559\uff1b
\u00b7 \u6ca1\u6709\u5757\u5c31\u5728\u6587\u4ef6\u5934\u63d2\u4e00\u4e2a\u3002
"""
from __future__ import annotations

import io
import re

FENCE = "---"
# \u5e93\u7ba1\u7684\u51e0\u4e2a\u952e\uff08\u5176\u4f59\u7684\u53ea\u5f53"\u5c5e\u6027"\u5c55\u793a\uff0c\u4e0d\u53c2\u4e0e\u903b\u8f91\uff09
K_TAGS = "tags"
K_TITLE = "title"
DATE_KEYS = ("date", "created", "updated", "updated_at", "created_at")
DATE_RE = re.compile(r"(20\d{2})[-_./]?(\d{2})[-_./]?(\d{2})")


# YAML 里"以这些字符开头的裸值"不是合法标量（@ 与 [ 正是辞玖笔记里最常见的两种写法）
_INDIC = set("@`%&*!|>'\"#,?[]{}")
_KEYS_RE = re.compile(r"^(\s*)([^\s:#][^:]*?)\s*:\s*(.*)$")


def _needs_quote(v: str) -> bool:
    if not v:
        return False
    if v[0] in _INDIC:
        return True
    if v[0] == "-" and (len(v) == 1 or v[1] == " "):
        return True
    if ": " in v or v.endswith(":"):
        return True
    return False


def _q(v: str) -> str:
    return '"%s"' % v.replace("\\", "\\\\").replace('"', '\\"')


def _unq(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _rescue_block(lines) -> list:
    """补救：把"以 YAML 指示符开头"的裸值就地加引号（只动值，不动键、顺序、注释）"""
    out = []
    for ln in lines:
        m = re.match(r"^(\s*-\s+)(.*)$", ln)
        if m and _needs_quote(m.group(2).strip()):
            out.append(m.group(1) + _q(m.group(2).strip()))
            continue
        m = _KEYS_RE.match(ln)
        if m:
            v = m.group(3).strip()
            if v.startswith("[["):
                # `[[目标]]` 是库自己的引用写法，不是 YAML 行内列表 → 整块当标量
                out.append("%s%s: %s" % (m.group(1), m.group(2), _q(v)))
                continue
            if v.startswith("[") and v.endswith("]"):
                parts = [p.strip() for p in v[1:-1].split(",")]
                out.append("%s%s: [%s]" % (m.group(1), m.group(2),
                                           ", ".join(_q(p) if _needs_quote(p) else p for p in parts)))
                continue
            if _needs_quote(v):
                out.append("%s%s: %s" % (m.group(1), m.group(2), _q(v)))
                continue
        out.append(ln)
    return out


def _line_parse(lines) -> dict:
    """最后兜底：只认最朴素的 `键: 值` 与块列表 —— 宁可解得不全，也别把属性整块丢了"""
    meta, cur = {}, None
    for ln in lines:
        if not ln.strip() or ln.lstrip().startswith("#"):
            continue
        m = re.match(r"^([^\s:#][^:]*?)\s*:\s*(.*)$", ln)
        if m:
            cur = m.group(1).strip()
            v = m.group(2).strip()
            meta[cur] = _unq(v) if v else []
            continue
        m = re.match(r"^\s*-\s+(.*)$", ln)
        if m and cur:
            if not isinstance(meta.get(cur), list):
                meta[cur] = []
            meta[cur].append(_unq(m.group(1).strip()))
    return meta


def _load(block_text: str):
    """返回 (meta, level, err)：level = ok / rescued / lines"""
    import yaml
    err = ""
    try:
        m = yaml.safe_load(block_text) or {}
        if isinstance(m, dict):
            return m, "ok", ""
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, (str(e).splitlines() or [""])[0])
    try:
        m = yaml.safe_load("\n".join(_rescue_block(block_text.split("\n")))) or {}
        if isinstance(m, dict) and m:
            return m, "rescued", ""
    except Exception as e:
        err = "%s: %s" % (type(e).__name__, (str(e).splitlines() or [""])[0])
    m = _line_parse(block_text.split("\n"))
    if m:
        return m, "lines", (err or "YAML 解析失败（已按行兜底）")
    return {}, "lines", (err or "YAML 解析失败")


def split_ex(text: str):
    """返回 (meta, block_lines, body_offset, err)；err 只在"连补救都读不通"时非空"""
    if not text:
        return {}, [], 0, ""
    t = text.lstrip("\ufeff")
    if not t.startswith(FENCE):
        return {}, [], 0, ""
    lines = t.split("\n")
    if lines[0].strip() != FENCE:
        return {}, [], 0, ""
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            end = i
            break
    if end is None:
        return {}, [], 0, ""
    block = lines[1:end]
    meta, _level, err = _load("\n".join(block))
    body_offset = len("\n".join(lines[:end + 1])) + 1
    return meta, block, body_offset, err


def split(text: str):
    """返回 (meta, block_lines, body_offset)；没有前置块就 ({}, [], 0)"""
    meta, block, off, _ = split_ex(text)
    return meta, block, off


def parse(text: str) -> dict:
    return split(text)[0]


def norm_tags(v) -> list:
    """`tags: a, b` / `tags: [a, b]` / `tags: #a #b` / \u5757\u5217\u8868 \u2192 ['a','b']"""
    out = []
    if v is None:
        return out
    items = v if isinstance(v, (list, tuple)) else re.split(r"[,，\s]+", str(v))
    for x in items:
        s = str(x).strip().lstrip("#").strip()
        if s and s not in out:
            out.append(s)
    return out


def date_of(meta: dict) -> str:
    for k in DATE_KEYS:
        v = meta.get(k)
        if not v:
            continue
        m = DATE_RE.search(str(v))
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return "%04d-%02d-%02d" % (y, mo, d)
    return ""


def _fmt(v) -> str:
    """\u503c \u2192 \u4e00\u884c YAML\uff08\u53ea\u4f1a\u4ea7\u751f\u6807\u91cf\u4e0e\u884c\u5185\u5217\u8868\uff09"""
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_fmt(x) for x in v) + "]"
    if v is None:
        return '""'
    s = str(v)
    if s == "" or re.search(r"[:#\[\]{}&*!|>'\"%@`]", s) or s != s.strip():
        return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')
    return s


def upsert(text: str, updates: dict) -> str:
    """\u628a updates \u5199\u8fdb\u524d\u7f6e\u5757\uff08\u6ca1\u6709\u5c31\u65b0\u5efa\uff09\uff1b\u5176\u4f59\u5185\u5bb9\u4e00\u5b57\u4e0d\u52a8"""
    todo = {k: v for k, v in (updates or {}).items() if k}
    if not todo:
        return text
    meta, block, off = split(text)
    body = text[off:] if off else text
    if not block and not off:
        head = [FENCE] + ["%s: %s" % (k, _fmt(v)) for k, v in todo.items()] + [FENCE, ""]
        return "\n".join(head) + "\n" + body
    lines = list(block)
    for k, v in todo.items():
        hit = None
        for i, ln in enumerate(lines):
            if re.match(r"^\s*%s\s*:" % re.escape(k), ln):
                hit = i
                break
        if hit is None:
            lines.append("%s: %s" % (k, _fmt(v)))
            continue
        lines[hit] = "%s: %s" % (k, _fmt(v))
        # \u539f\u6765\u662f\u5757\u5217\u8868\uff08\u4e0b\u9762\u8ddf\u7740 "- xxx"\uff09\u5c31\u628a\u90a3\u4e9b\u884c\u5220\u6389
        j = hit + 1
        while j < len(lines) and re.match(r"^\s*-\s", lines[j]):
            lines.pop(j)
    return "\n".join([FENCE] + lines + [FENCE]) + "\n" + body


def drop(text: str, key: str) -> str:
    """从前置块里删掉一个键（其它行与正文不动）"""
    meta, block, off = split(text)
    if not block or key not in (meta or {}):
        return text
    out, i, dropping = [], 0, False
    for ln in block:
        if re.match(r"^\s*%s\s*:" % re.escape(key), ln):
            dropping = True
            continue
        if dropping and re.match(r"^\s*-\s", ln):
            continue
        dropping = False
        out.append(ln)
    if not [x for x in out if x.strip()]:
        return text[off:] if off else text          # 空块就整块拿掉
    return "\n".join([FENCE] + out + [FENCE]) + "\n" + (text[off:] if off else text)


def jsonable(meta: dict) -> dict:
    """变成能进 JSON 的形式。

    2026-09-27 踩过：PyYAML 会把 `created: 2026-03-04` 解成 **datetime.date**，
    直接 json.dumps 会 TypeError（而当时还被 except 吞了，前置属性全丢）。
    """
    def one(v):
        if isinstance(v, (str, int, float, bool)) or v is None:
            return v
        if isinstance(v, (list, tuple)):
            return [one(x) for x in v]
        if isinstance(v, dict):
            return {str(k): one(x) for k, x in v.items()}
        return str(v)                      # date / datetime / 其它 → ISO 文本
    return {str(k): one(v) for k, v in (meta or {}).items()}


def apply(db, file_id: str, text: str, master=None) -> dict:
    """内容写完之"后调用：存 meta、合并标签、补 doc_date。返回摘要。"""
    import json as _json
    meta, block, _off, fm_err = split_ex(text)
    if block and not meta:
        # 整块 YAML 解析不出来：**绝不清空已有属性、也不撤标签**
        # （2026-09-27 审计：库里 7 篇的属性就是这么被清成空的）
        try:
            old = dict(db.frontmatter_of(file_id) or {})
        except Exception:
            old = {}
        if fm_err:
            old["_fm_error"] = fm_err
        try:
            db.set_frontmatter(file_id, _json.dumps(old, ensure_ascii=False) if old else "")
            cur_tags = list(db.tags_of(file_id) or [])
        except Exception:
            cur_tags = []
        return {"keys": sorted(k for k in old if not k.startswith("_")), "tags": cur_tags,
                "tags_added": 0, "tags_removed": 0, "date": "", "parse_error": fm_err}
    tags = norm_tags(meta.get(K_TAGS)) if meta else []
    date = date_of(meta) if meta else ""
    # 先读"上一次由前置属性带进来的标签"，**然后才覆盖** meta：
    # （踩过：先写后读，读到的是刚写进去的新值 → 永远不会撤标签）2026-09-27）
    try:
        prev = list((db.frontmatter_of(file_id) or {}).get("_tags_applied") or [])
    except Exception:
        prev = []
    if meta:
        meta = jsonable(dict(meta))
        meta["_tags_applied"] = tags
        if fm_err:
            meta["_fm_error"] = fm_err        # 只做到"行级兜底"才留痕，界面据此提示
    db.set_frontmatter(file_id, _json.dumps(meta, ensure_ascii=False) if meta else "")
    added = removed = 0
    try:
        lib = set(db.tags_of(file_id))
    except Exception:
        lib = set()
    # YAML 里不再有、上次又是前置属性带进来的 → 从库标签里撤掉
    for t in prev:
        if t not in tags and t in lib:
            try:
                db.remove_tag(file_id, t)
                removed += 1
            except Exception:
                pass
    for t in tags:
        try:
            before = set(db.tags_of(file_id))
            db.add_tag(file_id, t)
            if t not in before:
                added += 1
        except Exception:
            pass
    if date:
        row = db.file_by_id(file_id)
        if row is not None and not (row["doc_date"] or ""):
            # doc_date 会进嵌入文本（embedinput.embed_text(text, doc_date)）→ 得让索引作废，
            # 否则"没变就跳过"的增量重建会一直跳过它（sig 与真实内容对不上）2026-09-27
            db.conn.execute("UPDATE files SET doc_date=?, indexed_sig='' WHERE id=?", (date, file_id))
            db.conn.commit()
    return {"keys": sorted(k for k in (meta or {}).keys() if not k.startswith("_")),
            "tags": tags, "tags_added": added, "tags_removed": removed, "date": date,
            "parse_error": fm_err}
