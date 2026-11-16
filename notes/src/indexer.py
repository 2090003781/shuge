"""索引器：把 files 的内容切块并落入 chunks / chunks_fts（第一期）。

职责（FORMAT §2 / §6）：
  reindex_file(file_id)  → 取内容 → 分块 → replace_chunks
  reindex_all()          → 遍历所有文件（可只做"内容变了"的，第一期全量）
  verify()               → 一致性自检（块数 / FTS 可查 / 无孤儿 / 敏感标记一致）
  stale_check()          → manifest 参数与当前配置比对，判断是否需要重建

不负责（后续期次）：向量嵌入（vectors）、语义边（edges）、事件发布。
"""
from __future__ import annotations

import hashlib
import json

import chunker
import doctext
from db import Database
from embedinput import spec_model


def doc_title(name: str) -> str:
    """文件名去扩展名当标题（第一期；将来可用 frontmatter/title 覆盖）"""
    return name.rsplit(".", 1)[0] if "." in name else name


class Indexer:
    def __init__(self, db: Database):
        self.db = db
        self.master = lambda: None        # 上层注入：当前会话主密钥
        self.events = None                # 上层注入：EventLog（可选）

    # ── 索引签名：内容 / 名字 / 日期 / 分块策略 / 嵌入规格 / 解析器 都没变 → 不用重建 ──
    @staticmethod
    def index_sig(row, man: dict) -> str:
        """索引签名（决定这篇要不要重建）

        为什么要它：全量重建 1162 篇实测 146 秒，而这期间**单线程的服务完全不能响应**
        （看门狗会以为它卡死）。把"没变的"跳掉，重建从两分半降到秒级；而一旦
        内容 / 文件名（块标题用它）/ 文档日期（进嵌入）/ 分块策略 / 嵌入规格 / 解析器版本
        有任何变化，签名就变，照旧老老实实重建。
        """
        parts = [str(row["content_hash"] or ""), str(row["name"] or ""),
                 str(row["doc_date"] or ""), str(row["kind"] or ""),
                 chunker.VERSION, str(man.get("embed_spec") or "")]
        if (row["kind"] or "") == "document":
            parts.append(doctext.VERSION)
        return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:20]

    # ── 单文件 ────────────────────────────────────────────────
    def reindex_file(self, file_id: str, force: bool = True) -> dict:
        row = self.db.conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            return {"error": "no such file: %s" % file_id}
        master = self.master()
        if row["visibility"] == "private" and master is None:
            # 锁定态下 private 不进检索。**没有块就别写"删了 0 块"这条空事件**：
            # 每重扫一遍就给每篇白写一条，还顺带把 events 表撑大（2026-10-03 抓到的）
            n = self._n_chunks(file_id)
            if n:
                self.db.delete_chunks(file_id)
                if self.events:
                    self.events.emit("chunk.removed",
                                     {"file_id": file_id, "chunks": n, "reason": "locked"})
            return {"file_id": file_id, "name": row["name"], "kind": row["kind"],
                    "chunks": 0, "locked": True,
                    # 带 skipped 标记：全量重建时它算"跳过"而不是"重建过"（否则每次全量都把这 5 篇
                    # 当变化的，台账也失真）—— 2026-09-22 有了真私密篇才暴露
                    "skipped": "locked"}
        if (row["rag"] or "on") != "on":
            # 不进检索库：只存不索引（不解析成文本、不切块、不嵌入）
            # 同上：没有块就不写空事件（重扫时这类篇是绝大多数）
            n = self._n_chunks(file_id)
            if n:
                self.db.delete_chunks(file_id)
                if self.events:
                    self.events.emit("chunk.removed", {"file_id": file_id, "chunks": n,
                                                       "reason": "rag-off"})
            return {"file_id": file_id, "name": row["name"], "kind": row["kind"], "chunks": 0,
                    "rag": "off", "skipped": "rag-off"}
        try:
            raw = self.db.get_content(file_id, master)
        except Exception as e:
            return {"file_id": file_id, "error": "读内容失败: %s" % e}
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("utf-8", "replace")

        man = self.db.manifest()
        sig = self.index_sig(row, man)
        if not force and (row["indexed_sig"] or "") == sig:
            return {"file_id": file_id, "name": row["name"], "kind": row["kind"],
                    "chunks": self._n_chunks(file_id), "skipped": "unchanged"}
        max_chars = int(man.get("chunk_max_chars") or 1500)
        overlap = int(man.get("chunk_overlap") or 150)
        kind = row["kind"]
        parse_error = ""
        if kind == "document":
            # 文档格式：先解析成 Markdown，再按 Markdown 分块（检索/预览与笔记同一条流水线）
            der = self.db.derived_for(file_id, raw)
            parse_error = der.get("error") or ""
            if der.get("text") is None:
                self.db.delete_chunks(file_id)
                if self.events:
                    self.events.emit("chunk.indexed", {
                        "file_id": file_id, "chunks": 0, "model": man.get("embedding_model") or "",
                        "dim": int(man.get("dim") or 0), "reason": "document-parse-failed",
                        "error": parse_error[:200]})
                return {"file_id": file_id, "name": row["name"], "kind": kind, "chunks": 0,
                        "parse_error": parse_error}
            content, kind = der["text"], "markdown"
        pieces = chunker.chunk_file(kind, content, doc_title(row["name"]),
                                    max_chars=max_chars, overlap=overlap)
        blocks = self.db.blocks_of(file_id)
        if blocks:
            # 块树在 → 每块按偏移落到块上（第几页 / 覆盖了哪些块），命中结果据此定位
            chunker.attach_locators(pieces, blocks)
        # seq 归一（分块器不保证连续）
        for i, p in enumerate(pieces):
            p["seq"] = i
            p["is_sensitive"] = 1 if (row["visibility"] == "private") else 0
        n = self.db.replace_chunks(file_id, pieces)
        self.db.conn.execute("UPDATE files SET indexed_sig=? WHERE id=?", (sig, file_id))
        self.db.conn.commit()
        if self.events:
            self.events.emit("chunk.indexed", {
                "file_id": file_id, "chunks": n,
                "model": man.get("embedding_model") or "", "dim": int(man.get("dim") or 0)})
        return {"file_id": file_id, "name": row["name"], "kind": row["kind"], "chunks": n}

    def _n_chunks(self, file_id: str) -> int:
        """这一篇现在有几个块（走 idx_chunks_file，微秒级）"""
        return self.db.conn.execute("SELECT COUNT(*) FROM chunks WHERE file_id=?",
                                    (file_id,)).fetchone()[0]

    # ── 全量 ──────────────────────────────────────────────────
    def reindex_all(self, only_changed: bool = True) -> dict:
        rows = self.db.conn.execute("SELECT id, name FROM files ORDER BY name").fetchall()
        done, total_chunks, errors, skipped = 0, 0, [], 0
        for r in rows:
            res = self.reindex_file(r["id"], force=not only_changed)
            if res.get("error"):
                errors.append({"name": r["name"], "error": res["error"]})
                continue
            if res.get("skipped"):          # 不进检索库的：跳过，不算错
                skipped += 1
                continue
            done += 1
            total_chunks += res.get("chunks", 0)
        return {"files": done, "chunks": total_chunks, "errors": errors, "skipped": skipped}

    # ── 自检（FORMAT §6）──────────────────────────────────────
    def verify(self) -> dict:
        q = lambda s, *a: self.db.conn.execute(s, a).fetchone()[0]
        out = {}

        out["chunks"] = q("SELECT COUNT(*) FROM chunks")
        out["files_with_chunks"] = q("SELECT COUNT(DISTINCT file_id) FROM chunks")

        # 无孤儿：chunks.file_id 必须存在
        out["orphan_chunks"] = q(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN files f ON f.id = c.file_id WHERE f.id IS NULL")

        # 敏感标记一致：私密文件的块必须 is_sensitive=1，反之亦然。
        # 判据用 priv_folder 的**统一判据**（位置 ∪ 旧标记），不是单看 visibility 列 ——
        # 位置接管之后只看列，这个不变量会在"位置说私密、列说不是"的篇上悄悄失效。
        try:
            import priv_folder as _pf
            # ⚠️ 必须是**并集**：位置 ∪ visibility 列。
            # 第一版只写了位置判据 → 那 3 篇「靠列标记私密、但位置不在私密文件夹」的
            # （vis=private / enc=aesgcm-v1 / is_sensitive=1）被误判成"非私密却有敏感块"，
            # 凑出 45 个**假**不一致。并集才对：它既认位置、也认旧标记。
            _loc = ("EXISTS (SELECT 1 FROM folders fo2 WHERE fo2.id = f.folder_id AND %s)"
                    % _pf.PRIVATE_SQL.replace("fo.", "fo2."))
            _cond = "((%s) OR (%s))" % (_loc, _pf.LEGACY_PRIVATE_SQL)
        except Exception:
            _cond = "f.visibility = 'private'"
        out["sensitive_mismatch"] = q(
            "SELECT COUNT(*) FROM chunks c JOIN files f ON f.id = c.file_id "
            "WHERE ((%s) AND c.is_sensitive = 0) OR ((NOT (%s)) AND c.is_sensitive = 1)"
            % (_cond, _cond))

        # 超长块：分块上限失守会让向量化卡死（16 万字的块进模型 = 一批几分钟）
        cap = int(self.db.manifest().get("chunk_max_chars") or 1500)
        out["oversized_chunks"] = q("SELECT COUNT(*) FROM chunks WHERE char_count > ?", cap)

        # 向量层（FORMAT §7：块数与向量数对齐；没建过向量就不算错）
        out["vectors"] = q("SELECT COUNT(*) FROM vectors")
        out["chunks_without_vectors"] = q(
            "SELECT COUNT(*) FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
            " WHERE v.chunk_id IS NULL")
        out["orphan_vectors"] = q(
            "SELECT COUNT(*) FROM vectors v LEFT JOIN chunks c ON c.id=v.chunk_id WHERE c.id IS NULL")
        # 规格不符的向量：模型换了、或嵌入规格升级（如加了日期前缀）之后还没重算的
        out["vectors_stale_spec"] = q(
            "SELECT COUNT(*) FROM vectors WHERE model <> ?",
            spec_model(self.db.manifest().get("embedding_model") or ""))

        # 池引用：storage=pool 的文件，池文件必须真的在（FORMAT §7 规定动作）
        miss_pool = 0
        for r in self.db.conn.execute("SELECT storage_ref FROM files WHERE storage='pool'"):
            ref = r["storage_ref"] or ""
            if not ref or not (self.db.data_dir / "pool" / ref).is_file():
                miss_pool += 1
        out["pool_files"] = q("SELECT COUNT(*) FROM files WHERE storage='pool'")
        out["missing_pool_files"] = miss_pool

        # 无孤儿边
        out["orphan_edges"] = self.db.orphan_edges()

        # FTS 完整性（外部内容表：用官方 integrity-check 命令）
        try:
            self.db.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
            out["fts_integrity"] = "ok"
        except Exception as e:
            out["fts_integrity"] = "FAIL: %s" % e

        # FTS 可查（抽查第一个块的第一个词）
        first = self.db.conn.execute(
            "SELECT text FROM chunks WHERE LENGTH(text) > 0 LIMIT 1").fetchone()
        if first:
            word = first["text"].strip().split()[0][:8] if first["text"].strip() else ""
            out["fts_probe_word"] = word
            out["fts_probe_hits"] = len(self.db.search_chunks(word, limit=5)) if word else 0
        out["ok"] = (out["vectors_stale_spec"] == 0
                     and out["orphan_chunks"] == 0 and out["orphan_edges"] == 0
                     and out["oversized_chunks"] == 0 and out["orphan_vectors"] == 0
                     and out["missing_pool_files"] == 0
                     and (out["vectors"] == 0 or out["chunks_without_vectors"] == 0)
                     and out["sensitive_mismatch"] == 0 and out["fts_integrity"] == "ok")
        return out

    # ── 过时判据（manifest）───────────────────────────────────
    def stale_check(self, current: dict) -> dict:
        """current: {embedding_model, dim, chunk_max_chars, chunk_overlap} —— 与 manifest 比对"""
        man = self.db.manifest()
        diff = {k: {"manifest": man.get(k, ""), "current": str(v)}
                for k, v in current.items() if str(man.get(k, "")) != str(v)}
        return {"stale": bool(diff), "diff": diff}
