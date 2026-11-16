"""检索器 —— 向量索引与混合检索（FORMAT §3）。

两个职责：
  VectorIndexer：把 chunks 编码成向量写入 vectors 表，并把模型/维度写进 manifest
                 （manifest 是"索引是否过时"的唯一判据 → stale_check）
  HybridSearcher：双通道检索 —— FTS5(trigram) 关键词 + 向量语义 → RRF 融合排序

第一期不做：ANN 索引（数据量小时内存检索）、重排模型、图谱扩散（旧 RAG 工程的
BM25 权重/关系扩散可在 M4 之后迁移进来）。
"""
from __future__ import annotations

import os
import time

import numpy as np

from db import Database
from embedinput import SPEC as EMBED_SPEC, embed_text as embed_input, spec_model
from embedder import Embedder, get_embedder, pack_vec, unpack_vec


# ── 计时（diagnose）：只在 ?timing=1 时收集，平时每个阶段只多一次 None 判断 ─────
_TIMING = None          # None=不收集；列表=收集 [(stage, ms), ...]


def timing_on() -> list:
    global _TIMING
    _TIMING = []
    return _TIMING


def timing_off() -> list:
    global _TIMING
    out = _TIMING or []
    _TIMING = None
    return out


def _t() -> float:
    return time.perf_counter() if _TIMING is not None else 0.0


def tick(stage: str, t0: float) -> None:
    if _TIMING is not None and t0:
        _TIMING.append((stage, round((time.perf_counter() - t0) * 1000.0, 1)))


# 向量缓存世代号（2026-09-30）：后台补向量线程写完只把它 +1；
# 请求线程下次查时发现号变了，就自己重载矩阵 —— 跨线程只共享一个整数，安全。
VEC_EPOCH = 0
# 后台工人补过向量的"哪几篇"：请求线程取走后**只换这几篇**（别整表重载，见 2026-09-30 的 acc_cache）
VEC_DIRTY: set = set()
VEC_DIRTY_LOCK = __import__("threading").Lock()


def mark_dirty(file_ids) -> None:
    with VEC_DIRTY_LOCK:
        VEC_DIRTY.update(x for x in (file_ids or []) if x)


def drain_dirty() -> set:
    with VEC_DIRTY_LOCK:
        out = set(VEC_DIRTY)
        VEC_DIRTY.clear()
    return out


def bump_epoch() -> int:
    """后台线程补完向量后调它（见 server 的向量工人）。"""
    global VEC_EPOCH
    VEC_EPOCH += 1
    return VEC_EPOCH


class VectorIndexer:
    """把 chunks 编码进 vectors 表"""

    def __init__(self, db, embedder: Embedder | None = None):
        self.db = db
        self.emb = embedder or get_embedder()
        self.tag = spec_model(self.emb.model_name)     # 向量表 model 列（含嵌入规格）
        # 掩码要认"真引用"，索引与续用判据必须是同一套解析器
        self.resolve = self.db.ref_resolver()

    def pending_chunks(self, limit: int = 0) -> list:
        """还没向量化、或模型变了的块"""
        model = self.tag
        sql = ("SELECT c.id, c.file_id AS file_id, c.text, f.doc_date AS doc_date FROM chunks c "
               "JOIN files f ON f.id=c.file_id "
               "LEFT JOIN vectors v ON v.chunk_id = c.id "
               "WHERE v.chunk_id IS NULL OR v.model <> ?")
        if limit:
            sql += " LIMIT %d" % int(limit)
        return [dict(r) for r in self.db.conn.execute(sql, (model,))]

    def pending_of_files(self, file_ids: list) -> list:
        """这些文件里"缺向量/模型变了"的块"""
        ids = [x for x in (file_ids or []) if x]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            "SELECT c.id, c.file_id AS file_id, c.text, f.doc_date AS doc_date FROM chunks c"
            " JOIN files f ON f.id=c.file_id LEFT JOIN vectors v ON v.chunk_id = c.id"
            " WHERE c.file_id IN (%s) AND (v.chunk_id IS NULL OR v.model <> ?)" % ph,
            (*ids, self.tag)).fetchall()
        return [dict(r) for r in rows]

    def _put_vector(self, chunk_id: str, v) -> None:
        """写一条向量，但**只在块还在的时候**（2026-09-30：后台线程补向量时，
        那篇可能已经被删/被改写；不判就会留下"悬空向量" —— selfcheck 会报 orphan_vectors）。"""
        self.db.conn.execute(
            "INSERT OR REPLACE INTO vectors(chunk_id, model, dim, vec)"
            " SELECT ?, ?, ?, ? WHERE EXISTS(SELECT 1 FROM chunks WHERE id=?)",
            (chunk_id, self.tag, int(v.shape[0]), pack_vec(v), chunk_id))

    def index_files(self, file_ids: list, batch_size: int = 32) -> dict:
        """只补这几个文件的向量（内容刚被改写过时用，通常是几块而已）"""
        todo = self.pending_of_files(file_ids)
        if not todo:
            return {"indexed": 0, "files": len(file_ids or [])}
        t0 = time.time()
        for i in range(0, len(todo), batch_size):
            part = todo[i:i + batch_size]
            # 嵌入吃的是"去掉 @提及名字"的文本（见 mentions.mask_mentions）
            vecs = self.emb.encode(
                [embed_input(p["text"], p.get("doc_date"), self.resolve) for p in part],
                batch_size=batch_size)
            for p, v in zip(part, vecs):
                self._put_vector(p["id"], v)
        self.db.conn.commit()
        return {"indexed": len(todo), "files": len(file_ids or []), "model": self.tag,
                "seconds": round(time.time() - t0, 1)}

    def index(self, batch_size: int = 32, limit: int = 0, progress: bool = True) -> dict:
        todo = self.pending_chunks(limit=limit)
        if not todo:
            return {"indexed": 0, "batches": 0, "model": self.emb.model_name, "note": "无待处理块"}
        t0 = time.time()
        indexed, batches = 0, 0
        for i in range(0, len(todo), batch_size):
            part = todo[i:i + batch_size]
            vecs = self.emb.encode(
                [embed_input(p["text"], p.get("doc_date"), self.resolve) for p in part],
                batch_size=batch_size)
            for p, v in zip(part, vecs):
                self._put_vector(p["id"], v)
            indexed += len(part)
            batches += 1
            self.db.conn.commit()        # 分批提交：长任务别把写锁攥十几分钟
            if progress:
                print("  向量化 %d/%d" % (indexed, len(todo)), flush=True)
        self.db.set_manifest(embedding_model=self.emb.model_name, dim=str(DIM_OF(self.emb)))
        self.db.set_manifest(embed_spec=EMBED_SPEC)     # 规格变了要全量重算，得记下来
        return {"indexed": indexed, "batches": batches, "model": self.emb.model_name,
                "seconds": round(time.time() - t0, 1)}

    def stats(self) -> dict:
        q = lambda s, *a: self.db.conn.execute(s, a).fetchone()[0]
        return {
            "vectors": q("SELECT COUNT(*) FROM vectors"),
            "chunks": q("SELECT COUNT(*) FROM chunks"),
            "missing": q("SELECT COUNT(*) FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id "
                         "WHERE v.chunk_id IS NULL"),
            "models": [dict(r) for r in self.db.conn.execute(
                "SELECT model, COUNT(*) n FROM vectors GROUP BY model")],
        }


def DIM_OF(emb: Embedder) -> int:
    try:
        return int(emb.encode_one("维度探测").shape[0])
    except Exception:
        return 0


class HybridSearcher:
    """三通道检索：FTS(trigram) + 向量余弦 + 关系（沿 mention 边一跳）→ 加权 RRF → 精排"""

    RRF_K = 60
    # 通道权重（对应旧 RAG 的 bm25/semantic/relation_weight，按 rank-RRF 的口径调过）
    W_KW = float(os.environ.get("AI_NOTES_W_KW") or 1.0)
    W_VEC = float(os.environ.get("AI_NOTES_W_VEC") or 1.0)
    W_REL = float(os.environ.get("AI_NOTES_W_REL") or 0.5)
    # 精排：alpha 越大越信精排；只对融合分前 RERANK_MAX 个做
    RERANK_ALPHA = float(os.environ.get("AI_NOTES_RERANK_ALPHA") or 0.7)
    RERANK_MAX = int(os.environ.get("AI_NOTES_RERANK_MAX") or 40)
    # 同级时间优先：分数落在本档最高分的 TIME_BAND 内算"同级"，同档内按笔记日期倒序
    TIME_SORT = os.environ.get("AI_NOTES_TIME_SORT", "1") not in ("0", "false", "no")
    TIME_BAND = float(os.environ.get("AI_NOTES_TIME_BAND") or 0.05)

    def __init__(self, db, embedder: Embedder | None = None):
        self.db = db
        self.emb = embedder or get_embedder()
        self.degraded = None        # 上一查是否降级（模型还没就绪）
        self.rerank_used = False    # 上一查到底跑没跑精排
        # 向量缓存（2026-09-26 改成"常驻矩阵"），四份行对齐：
        #   self._mat   —— (N, dim) float32，第 k 行就是 self._ids[k] 的向量
        #   self._ids   —— 行号 → chunk_id
        #   self._files —— 行号 → file_id（增量刷新靠它定位）
        #   self._rows  —— chunk_id → 行号
        # 以前是"dict of 小数组 + 每次查询 np.stack 一份 45 MB"：现在矩阵常驻，
        # 一次查询省 36~49 ms，内存峰值也降一半（不再每次复制一整份）。
        self._ids = None
        self._mat = None
        self._files = None
        self._rows = None
        # 精排开关：显式环境变量优先；否则"模型在就用"
        env = os.environ.get("AI_NOTES_RERANK")
        if env is None:
            # 精排默认只在**加速器上**开（2026-09-25 实测）：
            # bge-reranker-v2-m3 在这台机器的 CPU 上是 **每条约 2.5 秒**（各 600 字），
            # 精排 40 条 = **100 秒**；库是单线程的，这一查把整个库堵死（外面看就是"卡死"\/拒连）。
            # 显卡上（空闲）只要 1.5~2.9 秒，所以：加速器就精排，CPU 就不精排（检索保持 0.3~0.5 秒）。
            # 想在 CPU 上强行开：AI_NOTES_RERANK=1（自己知道代价）。
            try:
                from reranker import get_reranker
                rr = get_reranker()
                self.rerank_on = rr.available and (self.emb.device != "cpu")
            except Exception:
                self.rerank_on = False
        else:
            self.rerank_on = env not in ("0", "false", "no")
        self._epoch = VEC_EPOCH          # 缓存对应的向量世代（2026-09-30）

    # ── 向量缓存（第一期：全量入内存）────────────────────────
    def _load_cache(self, model: str | None = None):
        """把全库向量读成**一块常驻矩阵**（首次查/预热时做一次）

        2026-09-30：加了世代号 —— 后台补向量线程 bump 之后，这里会重载一次，
        于是"后台补的向量"也能被检索到（否则内存里那份矩阵一直是旧的）。
        """
        dirty = drain_dirty()
        if self._mat is not None and dirty:
            self.refresh_files(dirty)          # 后台补过的：**只换这几篇**，不整表重载
            return self._mat
        if self._mat is not None and self._epoch == VEC_EPOCH:
            return self._mat
        model = model or spec_model(self.emb.model_name)
        rows = self.db.conn.execute(
            "SELECT v.chunk_id AS chunk_id, v.vec AS vec, c.file_id AS file_id"
            " FROM vectors v JOIN chunks c ON c.id=v.chunk_id WHERE v.model=?", (model,)).fetchall()
        n = len(rows)
        ids, files = [], []
        mat = None
        for i, r in enumerate(rows):
            v = unpack_vec(r["vec"])
            if mat is None:
                mat = np.empty((n, int(v.shape[0])), dtype=np.float32)
            mat[i] = v
            ids.append(r["chunk_id"])
            files.append(r["file_id"])
        if mat is None:
            mat = np.zeros((0, 0), dtype=np.float32)
        self._mat, self._ids, self._files = mat, ids, files
        self._rows = {cid: i for i, cid in enumerate(ids)}
        self._epoch = VEC_EPOCH
        return self._mat

    def invalidate(self) -> None:
        self._ids = self._mat = self._files = self._rows = None
        self._epoch = VEC_EPOCH

    def refresh_files(self, file_ids) -> int:
        """写完只换受影响的这几篇（2026-09-25）。

        以前要么整表 invalidate（下一次查要重读 11.5k 条向量，实测 1.8~3.5 秒），
        要么干脆不刷（PUT 就是这样：新块的向量没进缓存 → 向量通道看不见它）。
        返回换掉了多少块。
        """
        if self._mat is None:
            return 0                      # 还没建过缓存，下次查自然会建
        ids = {x for x in (file_ids or []) if x}
        if not ids:
            self.invalidate()
            return 0
        keep = [i for i, f in enumerate(self._files) if f not in ids]
        dropped = len(self._files) - len(keep)
        model = spec_model(self.emb.model_name)
        ph = ",".join("?" * len(ids))
        rows = self.db.conn.execute(
            "SELECT v.chunk_id AS chunk_id, v.vec AS vec, c.file_id AS file_id"
            " FROM vectors v JOIN chunks c ON c.id=v.chunk_id"
            " WHERE v.model=? AND c.file_id IN (%s)" % ph, [model] + sorted(ids)).fetchall()
        vecs = [unpack_vec(r["vec"]) for r in rows]
        dim = int(self._mat.shape[1])
        if dim == 0 and vecs:
            dim = int(vecs[0].shape[0])
        new = np.zeros((len(keep) + len(vecs), dim), dtype=np.float32)
        for k, i in enumerate(keep):
            new[k] = self._mat[i]
        ids_new = [self._ids[i] for i in keep]
        files_new = [self._files[i] for i in keep]
        for k, r in enumerate(rows):
            new[len(keep) + k] = vecs[k]
            ids_new.append(r["chunk_id"])
            files_new.append(r["file_id"])
        self._mat, self._ids, self._files = new, ids_new, files_new
        self._rows = {cid: i for i, cid in enumerate(ids_new)}
        self._epoch = VEC_EPOCH
        return len(vecs) + dropped

    # ── 向量通道 ──────────────────────────────────────────────
    def search_vector(self, query: str, limit: int = 20, include_sensitive: bool = False,
                      exclude_tags=None, since=None, until=None) -> list:
        t = _t()
        mat = self._load_cache()
        tick("  向量·载入缓存", t)
        if mat is None or not self._ids:
            return []
        t = _t()
        qv = self.emb.encode_one(query)
        tick("  向量·查询向量化", t)
        t = _t()
        ids = self._ids                    # 矩阵常驻，这里只是取引用（原来要 stack 一份 45 MB）
        tick("  向量·矩阵组装", t)
        t = _t()
        sims = mat @ qv
        order = np.argsort(-sims)[: max(limit * 3, limit)]
        tick("  向量·比对+排序", t)
        t = _t()
        out = []
        for idx in order:
            cid = ids[int(idx)]
            row = self.db.conn.execute(
                "SELECT c.id AS chunk_id, c.file_id, f.name AS name, c.heading, c.text,"
                " c.seq AS seq, c.title_path AS title_path, c.locator AS locator,"
                " f.doc_date AS doc_date,"
                " (SELECT COUNT(*) FROM chunks c2 WHERE c2.file_id=c.file_id) AS total,"
                " c.is_sensitive,"
                " (SELECT GROUP_CONCAT(t.name) FROM file_tags ft JOIN tags t ON t.id=ft.tag_id"
                "  WHERE ft.file_id=c.file_id) AS tags "
                "FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id=?", (cid,)).fetchone()
            if row is None:
                continue
            if row["is_sensitive"] and not include_sensitive:
                continue
            dd = row["doc_date"] or ""
            if (since and dd < str(since)) or (until and (not dd or dd > str(until))):
                continue
            if exclude_tags:
                hit_tags = set((row["tags"] or "").split(","))
                if hit_tags & set(exclude_tags):
                    continue
            d = dict(row)
            d["score"] = float(sims[int(idx)])
            out.append(d)
            if len(out) >= limit:
                break
        tick("  向量·命中取详情", t)
        return out

    # ── 融合检索 ──────────────────────────────────────────────
    def _time_tiebreak(self, hits: list) -> list:
        """同级（分数落在本档最高分 TIME_BAND 内）按笔记日期倒序；无日期的排在同档后面。

        实测依据：查「这周五 做了什么」这类宽泛查询，top-10 里有 6 条落在最高分 5% 以内
        且日期是乱的（09-07/08-31/08-17/08-03/08-10）；而精确查询只有 1 条落在 5% 内。
        也就是说这个重排只在该起作用的地方起作用。
        无日期的内容（库里 39% 的块）不能因为"没日期"被系统性沉底 → 同档内保持原相对次序。
        """
        key = "final" if any(h.get("final") is not None for h in hits) else "rrf"
        vals = [float(h.get(key) or 0.0) for h in hits]
        out, i, n = [], 0, len(hits)
        while i < n:
            gtop = vals[i]
            j = i + 1
            while j < n and vals[j] >= gtop * (1.0 - self.TIME_BAND):
                j += 1
            band = hits[i:j]
            dated = [h for h in band if (h.get("doc_date") or "")]
            undated = [h for h in band if not (h.get("doc_date") or "")]
            dated.sort(key=lambda h: h["doc_date"], reverse=True)
            out.extend(dated)
            out.extend(undated)
            i = j
        return out

    def search(self, query: str, limit: int = 8, include_sensitive: bool = False,
               with_detail: bool = False, exclude_tags=None, rerank=None,
               since=None, until=None, time_sort=None) -> list:
        """三通道加权 RRF（关键词 / 向量 / 关系）→ 可选 Cross-Encoder 精排。

        迁移自旧 RAG 工程的两阶段检索（D:\RAG）：粗召回量给够 → 融合分截断 → 只精排前 N。
        rerank=None 时按 self.rerank_on（默认：模型在就用）。
        """
        self.degraded = None
        self.rerank_used = False
        t_all = _t()
        t = _t()
        kw = self.db.search_chunks(query, limit=limit * 3, include_sensitive=include_sensitive,
                                   exclude_tags=exclude_tags, since=since, until=until)
        tick("fts 通道", t)
        t = _t()
        vec = []
        if not self.emb.ready:
            # 冷启动头几秒：嵌入模型还在加载（这台机器要十几秒）。
            # 别让这一查卡在等模型上：先给关键词+关系的结果，模型就绪后自然带语义（2026-09-25）
            self.degraded = "vector_not_ready"
        else:
            try:
                vec = self.search_vector(query, limit=limit * 3, include_sensitive=include_sensitive,
                                         exclude_tags=exclude_tags, since=since, until=until)
            except Exception as e:
                if with_detail:
                    print("[search] 向量通道不可用（%s），本次只用关键词" % str(e)[:60])
        tick("向量通道（含上面四段）", t)

        # 关系通道：拿粗召回里靠前的几篇当种子，沿 mention 边走一跳（旧 RAG 的 relation_weight）
        t = _t()
        rel = []
        try:
            seeds = [h["file_id"] for h in kw[:5]] + [h["file_id"] for h in vec[:5]]
            rel = self.db.related_chunks(seeds, Database._terms(query), limit=limit * 2,
                                         include_sensitive=include_sensitive,
                                         exclude_tags=exclude_tags, since=since, until=until)
        except Exception as e:
            if with_detail:
                print("[search] 关系通道不可用（%s）" % str(e)[:60])
        tick("关系通道", t)

        def add(channel, hits, weight, tag):
            for rank, h in enumerate(hits, start=1):
                cid = h["chunk_id"]
                scores[cid] = scores.get(cid, 0.0) + weight * (1.0 / (self.RRF_K + rank))
                if cid in store:
                    if tag not in store[cid]["channels"]:
                        store[cid]["channels"].append(tag)
                    if h.get("score") is not None:
                        store[cid]["vec_score"] = h.get("score")
                else:
                    h["channels"] = [tag]
                    store[cid] = h

        scores, store = {}, {}
        add("fts", kw, self.W_KW, "fts")
        add("vec", vec, self.W_VEC, "vec")
        add("rel", rel, self.W_REL, "rel")

        cands = sorted(store.values(), key=lambda x: -scores[x["chunk_id"]])
        for h in cands:
            h["rrf"] = round(scores[h["chunk_id"]], 6)

        t = _t()
        use_rr = self.rerank_on if rerank is None else bool(rerank)
        if use_rr and len(cands) > 1:
            cands = self._rerank(query, cands, with_detail=with_detail)
        tick("精排", t)
        t = _t()
        use_ts = self.TIME_SORT if time_sort is None else bool(time_sort)
        if use_ts and len(cands) > 1:
            cands = self._time_tiebreak(cands)
        tick("同级时间优先", t)
        out = cands[:limit]
        for h in out:
            h["rrf"] = round(scores.get(h["chunk_id"], h.get("rrf", 0.0)), 6)
        tick("search 合计", t_all)
        return out

    # ── 精排：融合分粗排截断 → Cross-Encoder 打分 → 与融合分混合 ──
    def _rerank(self, query, cands, with_detail=False):
        try:
            from reranker import get_reranker
            rr = get_reranker()
            if not rr.available:
                return cands
            if not rr.ready:
                # 冷启动期间不阻塞：这一查按融合分给结果，模型好了下一查自然就有精排（2026-09-25）
                self.degraded = self.degraded or "rerank_not_ready"
                return cands
            head = cands[: self.RERANK_MAX]          # 提速关键：只精排前 N
            tail = cands[self.RERANK_MAX:]
            sc = rr.score(query, [c.get("text") or "" for c in head])
            lo, hi = min(sc), max(sc)
            span = (hi - lo) or 1.0
            rrf_max = max((c.get("rrf") or 0.0) for c in head) or 1.0
            for c, s in zip(head, sc):
                c["rerank_score"] = round(s, 4)
                norm_rr = (s - lo) / span
                norm_fuse = (c.get("rrf") or 0.0) / rrf_max
                c["final"] = self.RERANK_ALPHA * norm_rr + (1 - self.RERANK_ALPHA) * norm_fuse
            self.rerank_used = True
            head.sort(key=lambda c: -c["final"])
            for c in tail:
                c["final"] = 0.0
            return head + tail
        except Exception as e:
            if with_detail:
                print("[search] 精排不可用（%s），本次用融合分" % str(e)[:70])
            return cands

    def format_hits(self, hits: list) -> str:
        lines = []
        for i, h in enumerate(hits, 1):
            rr = (" rr=%.3f" % h["rerank_score"]) if h.get("rerank_score") is not None else ""
            loc = ("%s " % h["doc_date"]) if h.get("doc_date") else ""
            seg = ("第%d/%d段 " % (int(h["seq"]) + 1, int(h["total"]))) if h.get("total") else ""
            lines.append("%d. [%s] %s%s%s（%s）rrf=%.4f%s %s" % (
                i, h["name"][:26], loc, seg, (h["heading"] or "")[:20],
                "+".join(h.get("channels", [])), h.get("rrf", 0), rr,
                ("sim=%.3f" % h["vec_score"]) if h.get("vec_score") is not None else ""))
            lines.append("     " + h["text"][:90].replace("\n", " "))
        return "\n".join(lines)
