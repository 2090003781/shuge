"""按篇召回（二次检索）—— 短篇整篇给全，长篇先在本篇内定目标、再取那一节。

口径（2026-09-15 辞玖定）：「短篇直接塞，长篇进行二次检索（先确定目标再确定具体内容）」。

为什么不能一律整篇：库里 p90 的一篇已经 1.6 万字，最大一篇 26.3 万字 / 694 块
（`tools/` 里量过）。一律整篇 = 8% 的查询往上下文里灌几万字。

两段的分工：
  篇内粗定位 → 命中块所在的小节（小节信息来自 chunker 的 title_path，本来就分好了）
  篇内精定位 → 精排给本篇的块打分（没有精排就退化成词命中数），挑出目标块
  取内容     → 以目标块为中心左右扩，同小节优先，直到字数预算用完（天然连续上下文）
"""
from __future__ import annotations

import json
import os
import re

from mentions import section_hit

ARTICLE_MAX = int(os.environ.get("AI_NOTES_ARTICLE_MAX_CHARS") or 10000)
SECTION_MAX = int(os.environ.get("AI_NOTES_SECTION_MAX_CHARS") or 3000)
PHASE2_MAX = int(os.environ.get("AI_NOTES_ARTICLE_PHASE2_CHUNKS") or 200)


def flat(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def _pos_of_flat(content: str, k: int) -> int:
    """第 k 个非空白字符在原文里的下标（块文本与原文之间只差空白，所以能对回去）"""
    seen = -1
    for i, ch in enumerate(content):
        if not ch.isspace():
            seen += 1
            if seen == k:
                return i
    return len(content)


def locate(content: str, text: str):
    """把拼出来的文本对回原文区间 [start, end)；对不上返回 None（宁可退回拼接文本）"""
    f = flat(text)
    if not f or not content:
        return None
    at = flat(content).find(f[:60])
    if at < 0:
        return None
    start = _pos_of_flat(content, at)
    i, k = start, 0
    while i < len(content) and k < len(f):
        ch = content[i]
        if not ch.isspace():
            if ch != f[k]:
                return None
            k += 1
        i += 1
    return (start, i)


def merge_chunks(chunks: list, overlap_hint: int = 400) -> str:
    """把连续的块接成一整段：块之间本来有 overlap，接缝处去重"""
    out = ""
    for c in chunks:
        t = c.get("text") or ""
        if not out:
            out = t
            continue
        cap = min(overlap_hint, len(out), len(t))
        cut = 0
        for n in range(cap, 1, -1):
            if out.endswith(t[:n]):
                cut = n
                break
        out += t[cut:]
    return out


def title_path_of(c: dict) -> tuple:
    tp = c.get("title_path")
    if isinstance(tp, str):
        try:
            tp = json.loads(tp)
        except Exception:
            tp = [tp]
    return tuple(tp or ())


def _size(c: dict) -> int:
    return int(c.get("char_count") or len(c.get("text") or ""))


class ArticleExpander:
    """把命中块还原成"一篇/一节"的上下文"""

    def __init__(self, db, master=None, reranker=None):
        self.db = db
        self.master = master or (lambda: None)
        self._rr = reranker

    # ── 篇内二次检索：给本篇的块打分，定目标 ──────────────────
    def _scores(self, query: str, chunks: list) -> dict:
        if not query:
            return {}
        if self._rr is None:
            try:
                from reranker import get_reranker
                self._rr = get_reranker()
            except Exception:
                self._rr = False
        if self._rr and getattr(self._rr, "available", False):
            try:
                return {i: float(s) for i, s in
                        enumerate(self._rr.score(query, [(c.get("text") or "") for c in chunks]))}
            except Exception:
                pass
        terms = self.db._terms(query)           # 精排不可用 → 词命中数也能定目标
        return {i: float(sum(1 for t in terms if t in (c.get("text") or "")))
                for i, c in enumerate(chunks)}

    def _pick_target(self, chunks: list, query: str, focus_seqs):
        """目标块：先看**字面命中**，再看精排。

        2026-09-26 改：以前不管什么查询都先问精排模型。两个毛病：
        ① 查询词是生僻词/代号时，“语义分”反而挑错块（实测 smoke_article ②）；
        ② CPU 上精排一条要 2.5 秒，14k 字的长篇就是几十秒白等。
        现在：含询询词的块只有一个 → 直接定它（不问模型）；
        有多个 → 只在这些块里挑最高分；一个字面命中都没有（语义查询）→ 才走原来那套。
        """
        focus = [int(s) for s in (focus_seqs or []) if s is not None]
        if focus:
            cand = [i for i, c in enumerate(chunks) if int(c["seq"]) in focus]
            if len(cand) == 1:
                return cand[0], {}
            if cand:
                # 命中可能落在这一篇的好几块上（长篇尤其如此）：
                # 2026-09-26 修：以前取"文档顺序最靠前的那个命中块"—— 于是长篇总是取到第 1 节。
                # 现在先看**谁真含查询词**，都不含就取命中排名最高的那个。
                terms0 = self.db._terms(query)
                hh = [sum(1 for t in terms0 if t in (chunks[i].get("text") or "")) for i in cand]
                b = max(range(len(cand)), key=lambda k: (hh[k], -focus.index(int(chunks[cand[k]]["seq"]))))
                return cand[b], {}
        terms = self.db._terms(query)
        hits = [sum(1 for t in terms if t in (c.get("text") or "")) for c in chunks]
        lit = [i for i, h in enumerate(hits) if h > 0]
        if len(lit) == 1:                       # 只有一块真含查询词 → 就是它
            return lit[0], {}
        if lit:
            pool = sorted(lit, key=lambda i: -hits[i])[:PHASE2_MAX]   # 含词的块太多就先按词命中数截断
        else:
            pool = list(range(min(len(chunks), PHASE2_MAX)))          # 语义查询：原来那套（粗筛后精排）
        sc = self._scores(query, [chunks[i] for i in pool])
        if not sc:
            return (max(lit, key=lambda i: hits[i]) if lit else 0), {}
        best = max(sc, key=lambda k: sc[k])     # sc 的键是 pool 里的下标
        return pool[best], {pool[k]: v for k, v in sc.items()}

    def _grow(self, chunks: list, ti: int, scores: dict, budget: int) -> list:
        """以目标块为中心向两侧扩：同小节优先、同小节里分高的优先，到预算为止"""
        same = title_path_of(chunks[ti])
        total = _size(chunks[ti])
        lo = hi = ti
        chosen = {ti}
        while True:
            cands = [i for i in (lo - 1, hi + 1) if 0 <= i < len(chunks)]
            if not cands:
                break
            cands.sort(key=lambda i: (0 if title_path_of(chunks[i]) == same else 1,
                                      -(scores.get(i) or 0.0), abs(i - ti)))
            pick = cands[0]
            if total + _size(chunks[pick]) > budget:
                break
            chosen.add(pick)
            total += _size(chunks[pick])
            lo, hi = min(lo, pick), max(hi, pick)
        return [chunks[i] for i in sorted(chosen)]

    def _section_window(self, chunks: list, token: str, budget: int) -> list:
        """按小节名找目标块，再在小节内扩到预算

        匹配用 mentions.section_hit（归一后 整串/前缀/包含）：人写 `@X#AI模型`，
        标题可能是 `AI 模型 API Key`；写 `#看图优先本地模型`，标题可能是 `[视觉] 看图优先本地模型`。
        """
        if not (token or "").strip():
            return []
        heads = [str(c.get("heading") or "") for c in chunks]
        want = section_hit(heads, token)
        hit = [i for i, h in enumerate(heads) if h == want] if want else []
        if not hit:      # 标题没匹配上，再试层级路径（跳过第一段 = 文档标题）
            names = []
            for c in chunks:
                names.extend(str(x) for x in title_path_of(c)[1:])
            want2 = section_hit(names, token)
            if want2:
                hit = [i for i, c in enumerate(chunks)
                       if any(str(x) == want2 for x in title_path_of(c)[1:])]
        if not hit:
            return []
        ti = hit[0]
        same = title_path_of(chunks[ti])
        total, lo, hi = _size(chunks[ti]), ti, ti
        chosen = {ti}
        while True:
            cands = [i for i in (lo - 1, hi + 1)
                     if 0 <= i < len(chunks) and title_path_of(chunks[i]) == same]
            if not cands:
                break
            pick = cands[0]
            if total + _size(chunks[pick]) > budget:
                break
            chosen.add(pick)
            total += _size(chunks[pick])
            lo, hi = min(lo, pick), max(hi, pick)
        return [chunks[i] for i in sorted(chosen)]

    # ── 单篇 ──────────────────────────────────────────────────
    def one(self, file_id: str, query: str = "", focus_seqs=None,
            include_sensitive: bool = False, max_chars: int = 0,
            section_max: int = 0, section: str = "") -> dict:
        row = self.db.file_by_id(file_id)
        if row is None:
            return {"error": "no such file"}
        chunks = self.db.article_chunks(file_id, include_sensitive=include_sensitive)
        if not chunks:
            return {"file_id": file_id, "name": row["name"], "doc_date": row["doc_date"],
                    "mode": "empty", "chars": 0, "total_chars": 0, "text": ""}
        try:
            content = self.db.get_content(file_id, self.master()).decode("utf-8", "replace")
        except Exception:
            content = ""
        total = len(content) or sum(_size(c) for c in chunks)
        base = {"file_id": file_id, "name": row["name"], "doc_date": row["doc_date"],
                "chunks": len(chunks), "total_chars": total,
                "matched_seqs": sorted(int(s) for s in (focus_seqs or []) if s is not None)}

        # ⓪ 指定了小节：只给那一节（引用颗粒度到小节时的取内容方式）
        if section:
            win = self._section_window(chunks, section, int(section_max or SECTION_MAX))
            if win:
                text = merge_chunks(win)
                loc = locate(content, text) if content else None
                if loc:
                    text = content[loc[0]:loc[1]]
                hit = next((c for c in win
                            if (c.get("heading") or "").strip().lower()
                            .startswith(section.strip().lower())), win[0])
                return dict(base, mode="section", section=section, chars=len(text),
                            truncated=len(text) < total, heading=hit["heading"],
                            title_path=list(title_path_of(hit)),
                            seq_from=int(win[0]["seq"]), seq_to=int(win[-1]["seq"]),
                            text=text)

        # ① 短篇：整篇直接给（原文，不拼块 —— 拼接会带上 overlap 的重复）
        if content and total <= int(max_chars or ARTICLE_MAX):
            return dict(base, mode="full", chars=len(content), truncated=False,
                        seq_from=0, seq_to=len(chunks) - 1, heading="", text=content)

        # ② 长篇：二次检索
        ti, scores = self._pick_target(chunks, query, focus_seqs)
        window = self._grow(chunks, ti, scores, int(section_max or SECTION_MAX))
        text = merge_chunks(window)
        loc = locate(content, text) if content else None
        if loc:
            text = content[loc[0]:loc[1]]
        best = chunks[ti]
        return dict(base, mode="section", chars=len(text), truncated=len(text) < total,
                    heading=best["heading"], title_path=list(title_path_of(best)),
                    seq_from=int(window[0]["seq"]), seq_to=int(window[-1]["seq"]),
                    text=text)

    # ── 一批命中：按篇聚合（一篇一条，避免同一篇整篇塞多次）──
    def expand(self, hits: list, query: str = "", include_sensitive: bool = False,
               n: int = 3) -> list:
        order, matched = [], {}
        for h in hits:
            fid = h.get("file_id")
            if not fid:
                continue
            if fid not in matched:
                matched[fid] = {"anchor": h, "seqs": []}
                order.append(fid)
            if h.get("seq") is not None:
                matched[fid]["seqs"].append(h["seq"])
        out = []
        for fid in order[:max(1, int(n))]:
            m = matched[fid]
            item = dict(m["anchor"])
            item["matched_seqs"] = sorted(int(x) for x in m["seqs"] if x is not None)
            item["article"] = self.one(fid, query=query, focus_seqs=m["seqs"],
                                       include_sensitive=include_sensitive)
            out.append(item)
        return out
