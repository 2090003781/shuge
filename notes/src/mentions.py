"""@ 提及解析（FORMAT §6）：文本里的 @锚点 → file_id → edges(kind='mention')。

锚点只认行首或空白/标点之后（不认邮箱 a@b），跳过代码围栏，长度上限 120。
锚点可以是**路径**（引用以路径为基底）：`@知识库/01-编程通识/08-调试方法论`。
"""

import re

# 锚点 = 名字**或路径**，后面可以跟 `#小节`。
#   · 路径是引用的基底：`@知识库/01-编程通识/08-调试方法论`、`@日志/reasonix-raw/2026-07-08/会话`
#     （`/` 在字符类里；`vault/` 前缀可省、`.md` 可省；重名笔记只能靠路径区分）
#   · `#` 不在字符类里 → 锚点在 # 处结束，后面是小节
#   · 小节名里不要带空格（带空格的标题取头一个词，如 `P12`）
# 名字带空格时用引号包起来：`@"03-Python FastAPI"`、`@"知识库/02-后端开发/03-Python FastAPI"`
MENTION_RE = re.compile(
    r'(?<![\w@])@(?:"(?P<qname>[^"\n]{1,160})"'
    r'|(?P<name>[^\s@\[\]()（）,，。；;：:！!？?*`#>|\\]{1,120}))'
    r"(?:#(?P<sec>[^\s，。；;：:！!？?）)】\]}（(]{1,60}))?")


def ref_from_match(m) -> dict:
    """一个正则命中 → {name, section, anchor}（anchor＝库里存的字面量，引号形式带引号）"""
    if m.group("qname") is not None:
        name = m.group("qname").strip()
        head = '"%s"' % name
    else:
        name = (m.group("name") or "").strip()
        head = name
    sec = (m.group("sec") or "").strip()
    return {"name": name, "section": sec,
            "anchor": ("%s#%s" % (head, sec)) if sec else head}


def parse_refs(text: str) -> list:
    """按出现顺序返回去重后的引用：[{name, section, anchor}]

    anchor 是**库里存的字面文本**：`名字` 或 `名字#小节`（引用颗粒度到小节时）。
    """
    out, fence = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            continue
        if fence:
            continue
        for m in MENTION_RE.finditer(line):
            r = ref_from_match(m)
            if r["name"]:
                out.append(r)
    seen, uniq = set(), []
    for r in out:
        if r["anchor"] not in seen:
            seen.add(r["anchor"])
            uniq.append(r)
    return uniq


def parse_mentions(text: str) -> list:
    """按出现顺序返回去重后的锚点文字列表（不含 @；可能带 `#小节`）"""
    return [r["anchor"] for r in parse_refs(text)]


def norm_section(s: str) -> str:
    """小节名归一：去掉开头的 [标签]/【标签】/(标签)，再去掉空白与常见分隔符，转小写"""
    x = re.sub(r"^\s*[\[【(（][^\]】)）]{0,16}[\]】)）]\s*", "", str(s or ""))
    return re.sub(r"[\s\-_·:：、]+", "", x).lower()


def section_hit(names, token: str) -> str:
    """在 names 里找 token（归一后：整串相等 → 前缀）；返回原名，找不到返回空串

    只做前缀、不做"包含"：包含太宽，会把"小节被改名/挪走"误判成还在
    （`P3` 会匹配到 `四、已经不是 P3 了`）。归一的目的是容错空格与 `[标签]` 前缀。
    """
    t = norm_section(token)
    if len(t) < 2:
        return ""
    for nm in names:
        if norm_section(nm) == t:
            return nm
    for nm in names:
        if norm_section(nm).startswith(t):
            return nm
    return ""


def split_anchor(anchor: str) -> tuple:
    """`名字#小节` → (名字, 小节)；没有小节时第二项为空串"""
    a = anchor or ""
    if "#" in a:
        n, s = a.split("#", 1)
        return n.strip(), s.strip()
    return a.strip(), ""


# 嵌入用的提及占位：**不带名字**。
# 为什么（2026-09-14 辞玖提，想清楚了）：向量只该吃"正文语义"，不该吃"链接到哪一篇的名字"。
#   · 名字是元数据（而且随时会改）—— 把 20 字的长文件名塞进一块 300 字的正文里，是纯噪声；
#   · 更要命的是：名字一改，块文本就变 → 向量作废要重算。名字不进嵌入，改名就不再动向量；
#   · 保留一个中性占位而不是整段删掉：句子的骨架还在（"见 〇 那篇" 而不是 "见 那篇"）。
EMBED_MASK = "（引用）"


def mask_mentions(text: str, resolve=None) -> str:
    """把正文里的 @提及 换成中性占位（代码围栏里不动）——**只用于嵌入**。

    resolve 给了就**只掩码真的能解析成一篇笔记的引用**：日志里的 `@event`/`@tool`/`@status`
    这类伪标签不是引用，不该被抹掉（2026-09-15 辞玖定的顺序）。
    resolve=None 时保持老行为（全部掩码），兼容旧调用。

    注意：库里存的 chunk.text、界面显示、关键词检索都用**原文**，不受影响。
    """
    def sub(m):
        if resolve is not None:
            try:
                if not resolve(ref_from_match(m)["name"]):
                    return m.group(0)          # 不是真引用 → 原样留着
            except Exception:
                return m.group(0)
        return EMBED_MASK

    out, fence = [], False
    for line in (text or "").splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        out.append(line if fence else MENTION_RE.sub(sub, line))
    return "\n".join(out)


def resolve(text: str, index, anchor_index: dict | None = None) -> tuple:
    """返回 (pairs, unresolved)：pairs=[(b_file_id, anchor)]，unresolved=[锚点]

    index：`db.ref_resolver()`（推荐：认路径，也认唯一名字）或 {名字: file_id} 字典。
    anchor_index：本篇已有的边 {锚点: b_file_id}；被引用方改名后正文里还写着旧锚点
    （协议 §6：文本只是渲染），靠它把旧锚点解析回同一篇，不让边白白断掉。
    """
    def look(x):
        return index(x) if callable(index) else (index or {}).get(x)

    pairs, miss = [], []
    for r in parse_refs(text):
        a, nm = r["anchor"], r["name"]
        b = look(nm) or (anchor_index or {}).get(a) or (anchor_index or {}).get(nm)
        if b:
            pairs.append((b, a))
        else:
            miss.append(a)
    return pairs, miss
