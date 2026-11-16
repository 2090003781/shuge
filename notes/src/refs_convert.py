"""旧引用格式 `[[目标]]` → 库的 `@引用`（FORMAT §6）。

为什么搬进 src/：这件事从 2026-09-22 起**每次导入都要做** —— 辞玖定："vault 留原件 `[[ ]]`，
进库按库规则转成 `@`"。原先它只是 `tools/convert_refs.py` 里的一次性批转（2026-09-15 那次），
现在导入路径也要用同一套规则，所以规则本体放这里，工具保留"全库批转 + 备份/回滚"的外壳。

规则（2026-09-15 定，原样搬过来，一处不改）：
  · `[[目标]]`            → `@目标显示名`（`@` 前是字母数字时补空格）
  · `[[目标#小节]]`        → `@目标#小节token`（token 取小节名第一个词）
  · `[[目标|别名]]`        → 仅当 别名 == 目标名，或别名就是小节 token / 以它开头 → 转；否则原样
  · 目标对不上 / 有歧义 / 别名承载不了 → **原样不动**（宁可不转，也不写一个解析不回去的引用）
  · 代码围栏与行内代码里的 `[[...]]` 一律不动

解析口径与服务端 `db.ref_resolver()` 完全一致：**路径优先**，其次唯一显示名。
"""
from __future__ import annotations

import re
from collections import Counter

WIKI = re.compile(r"\[\[([^\]\n|]{1,120})(?:\|([^\]\n]{0,120}))?\]\]")
CODE = re.compile(r"`[^`\n]*`")
# 锚点的合法终止符（与 mentions.MENTION_RE 的字符类一致）
STOP = set(" \t\r\n@[]()（）,，。；;：:！!？?*`#>|/\\")


def sec_token(s: str) -> str:
    """小节名取一个词：`P12 决策前置 — …` → `P12`（@ 语法里 # 后面只能有一个词）"""
    return re.split(r"[\s，。；：,;:！!？?）)】\]}|（(]+", (s or "").strip())[0].strip()


def build_resolver(db):
    """造 (resolve, display) 两个函数：resolve 把 `[[目标]]` 里的目标解析成 file_id，display 生成 @ 写法。

    路径是引用的基底：`系统设计/Bot系统-v2/README` 能指到具体那篇，
    而 `README`（3 篇重名）本来解析不出来 —— 这类只能靠路径。
    """
    look = db.ref_resolver()                  # 服务端同一套：整条路径 → 唯一后缀 → 唯一名字
    counts = {}
    for r in db.conn.execute("SELECT name FROM files"):
        nm = r["name"] or ""
        d = nm[:-3] if nm.lower().endswith(".md") else nm
        if d:
            counts[d] = counts.get(d, 0) + 1

    def display(fid):
        """插进正文的写法：末段重名 → 整条路径；名字/路径带空格 → 引号包起来。
        （两种都是为了让转出来的 @引用 一定解析得回去）
        """
        p = db.display_path(fid)
        out = p if counts.get(p.split("/")[-1], 0) > 1 else p.split("/")[-1]
        return '"%s"' % out if " " in out else out

    def resolve(t):
        t = (t or "").strip().strip("\\/")
        if not t or "#" in t:
            return None, "anchor"
        fid = look(t)
        if fid:
            return fid, "ok"
        # 区分"库里没这篇"和"重名解析不了"
        leaf = re.split(r"[\\/]", t)[-1]
        if leaf.lower().endswith(".md"):
            leaf = leaf[:-3]
        if counts.get(leaf) or counts.get(t):
            return None, "ambiguous"
        return None, "miss"

    return resolve, display


def convert_segment(seg: str, resolve, display, stats: Counter, detail: dict):
    """只处理一段非代码文本"""

    def sub(m):
        tgt, alias = m.group(1), m.group(2)
        name_part, _, sec_part = tgt.partition("#")
        token = sec_token(sec_part)
        fid, why = resolve(name_part)
        if why == "anchor":
            stats["skip_anchor"] += 1
            return m.group(0)
        if why != "ok":
            stats["skip_" + why] += 1
            detail["skip_" + why][tgt.strip()[:60]] += 1
            return m.group(0)
        name = display(fid)
        anchor = ("%s#%s" % (name, token)) if token else name
        if alias is not None:
            a = alias.strip()
            if a.lower().endswith(".md"):
                a = a[:-3]
            # 别名要么就是目标名，要么就是小节 token（`[[目标#P12|P12]]`），
            # 要么以小节 token 开头（`|P9 持久化与操作纪律` 只是那一节的标签）——否则 @ 语法承载不了
            if a not in (name, token, anchor) and not (token and a.startswith(token)):
                stats["skip_alias"] += 1
                detail["skip_alias"][(tgt.strip()[:40], alias.strip()[:30])] += 1
                return m.group(0)
        # 边界：@ 前不能是 \w/@；锚点后面必须是终止符
        i, j = m.start(), m.end()
        before = seg[i - 1] if i > 0 else "\n"
        after = seg[j] if j < len(seg) else "\n"
        head = " " if re.match(r"[\w@]", before or "") else ""
        tail = " " if (after and after not in STOP) else ""
        if head:
            stats["spaced_head"] += 1
        if tail:
            stats["spaced_tail"] += 1
        if token:
            stats["with_section"] += 1
        stats["converted"] += 1
        return "%s@%s%s" % (head, anchor, tail)

    return WIKI.sub(sub, seg)


def convert_text(text: str, resolve, display, detail: dict) -> tuple:
    """整篇转换：逐行处理，跳过 ``` 围栏与行内代码，返回 (新正文, 计数 Counter)"""
    stats = Counter()
    out, fence = [], False
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        parts, codes = CODE.split(line), CODE.findall(line)
        buf = []
        for k, part in enumerate(parts):
            buf.append(convert_segment(part, resolve, display, stats, detail))
            if k < len(codes):
                buf.append(codes[k])
        out.append("".join(buf))
    return "".join(out), stats


_CACHE: dict = {}


def converter_for(db):
    """按库缓存 (resolve, display)。一次导入跑一批文件时复用，别每篇重建（1164 个名字的表）。"""
    key = id(db)
    got = _CACHE.get(key)
    if got is None:
        got = build_resolver(db)
        _CACHE[key] = got
    return got


def convert_for_db(db, text: str):
    """便捷入口：把一篇正文按库规则转好，返回 (新正文, 计数)。没有 `[[` 直接原样返回，不做无用功。"""
    if "[[" not in (text or ""):
        return text, Counter()
    resolve, display = converter_for(db)
    detail = {"skip_miss": Counter(), "skip_ambiguous": Counter(), "skip_alias": Counter()}
    new, stats = convert_text(text, resolve, display, detail)
    return new, stats
