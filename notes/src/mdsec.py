# -*- coding: utf-8 -*-
"""按**小节**改 Markdown —— 写正文的第三种形态。

写正文现在有三种，各自对应一种"编辑意图"：
    · 整篇 set      `PUT /api/files/<id>`             —— 我就是要这一份新正文
    · 尾部 append   `POST .../append`                 —— 只会越长（日志转录）
    · **按节改**    `POST .../sections`（本件）        —— 改/加/删"某一节"，其余一个字不动

为什么必须有第三种：**小节在库里早就是一等概念**（`@篇#节` 引用、按节还原、反链按节、
`sections/relink` 修小节引用），可**写**只有整篇 —— 改一节得把全篇读回来、拼好、整篇写回去：
O(n) 传输、过缩短闸、还和并发写打架。按节改是"基础操作"，不是组合。

纯文本操作（不碰库），所以可以单独测：`split_sections` / `find_section` / `apply_section`。
"""
import re

from mentions import norm_section      # 复用库里"小节名怎么算同一个"的归一规则

_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")


def split_sections(text: str):
    """切 → (前言文本, [(name, level, start_line, end_line)])；行号区间是**左闭右开**。

    只认 ATX 标题（`## 名字`）—— 跟库里其它地方对小节的理解一致（块标题 + 层级路径）。
    """
    lines = (text or "").split("\n")
    marks = []
    for i, l in enumerate(lines):
        m = _HEAD.match(l)
        if m:
            marks.append((i, len(m.group(1)), m.group(2).strip()))
    if not marks:
        return text or "", []
    head = "\n".join(lines[:marks[0][0]])
    out = []
    for k, (i, lvl, name) in enumerate(marks):
        end = marks[k + 1][0] if k + 1 < len(marks) else len(lines)
        out.append((name, lvl, i, end))
    return head, out


def section_names(text: str) -> list:
    return [s[0] for s in split_sections(text)[1]]


def find_section(text: str, token: str):
    """按名找一节 → (name, level, start, end) 或 None（归一后**整串相等**优先，其次前缀/包含）"""
    _, secs = split_sections(text)
    tok = norm_section(token)
    if not tok:
        return None
    for name, lvl, s, e in secs:
        if norm_section(name) == tok:
            return (name, lvl, s, e)
    for name, lvl, s, e in secs:
        n = norm_section(name)
        if n.startswith(tok) or tok in n:
            return (name, lvl, s, e)
    return None


def apply_section(text: str, name: str, mode: str, body: str = "", new_name: str = "",
                  after: str = "", level: int = 0):
    """改一节 → (新文本, 做了什么, 错误串)。错误串非空就是 400。

    mode：
      replace —— 换掉这一节的正文（标题保留；给了 new_name 就顺手改名）
      append  —— 在这一节**末尾**接一段（不是文末，是节内末尾）
      delete  —— 删掉这一节（连标题）
      insert  —— 加一节：`after` 指哪节就插它后面（不指就放文末），`level` 不给就跟 after 同级
    """
    lines = (text or "").split("\n")
    if mode not in ("replace", "append", "delete", "insert"):
        return text, "", "mode 只能是 replace / append / delete / insert"
    hit = find_section(text, name) if mode != "insert" else None
    if mode != "insert" and hit is None:
        return text, "", "这篇里没有小节：%s（现有：%s）" % (name, "、".join(section_names(text))[:160])

    if mode == "replace":
        sname, lvl, s, e = hit
        title = "#" * lvl + " " + ((new_name or "").strip() or sname)
        blk = [title, ""] + (body.strip().split("\n") if body.strip() else []) + [""]
        lines[s:e] = blk
        return "\n".join(lines), "replace「%s」（%d→%d 行）" % (sname, e - s, len(blk)), ""

    if mode == "append":
        sname, lvl, s, e = hit
        end = e
        while end > s + 1 and not lines[end - 1].strip():
            end -= 1                        # 收掉节尾空行再往后接
        blk = ([""] + body.strip().split("\n")) if body.strip() else []
        lines[end:end] = blk
        return "\n".join(lines), "append「%s」（+%d 行）" % (sname, len(blk)), ""

    if mode == "delete":
        sname, lvl, s, e = hit
        del lines[s:e]
        return "\n".join(lines), "delete「%s」（-%d 行）" % (sname, e - s), ""

    # insert
    if not (name or "").strip():
        return text, "", "insert 要给 name（新小节叫什么）"
    pos, lvl = len(lines), int(level or 0)
    if after:
        h2 = find_section(text, after)
        if h2 is None:
            return text, "", "after 指的那一节不存在：%s" % after
        pos, lvl = h2[3], (lvl or h2[1])
    lvl = lvl or 2
    blk = ["", "#" * lvl + " " + name.strip(), ""] + (body.strip().split("\n") if body.strip() else [])
    lines[pos:pos] = blk
    return "\n".join(lines), "insert「%s」（+%d 行）" % (name, len(blk)), ""
