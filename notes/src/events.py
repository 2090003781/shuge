"""事件协议落地（EVENT v0.2）：发布、落库、增量读取、动作配对。

gseq 由 events 表的自增主键分配（全局单调），(source, seq) 是来源内序号；
payload 超 4KB 走 ref 外置到 data/events/blobs/。
"""
from __future__ import annotations

import json
import os
import threading
import time

from db import Database, local_iso, sha256_bytes

V = 1
PAYLOAD_LIMIT = 4096


class EventLog:
    def __init__(self, db: Database, source: str = "app"):
        self.db = db
        self.source = source
        self.blob_dir = db.data_dir / "events" / "blobs"
        # 当前写入者（辞玖 2026-09-24 定：标签跟**操作**捆绑）。
        # 请求进来时由服务端按 X-Writer 头 / body.writer / 请求来源解析一次，写在这里；
        # 后台动作（镜像、导入）显式传 writer=。
        # **2026-10-01 改成线程内**：服务并发之后，全局属性会让两个请求互相串味（审计记错人）。
        self._tls = threading.local()
        self._writer_default = "user"
        self._emit_lock = threading.Lock()      # "取号 MAX(seq)+1 → 插入"要原子，否则并发撞号

    @property
    def writer(self) -> str:
        return getattr(self._tls, "writer", None) or self._writer_default

    @writer.setter
    def writer(self, w) -> None:
        self._tls.writer = w or self._writer_default

    # ── 发布 ────────────────────────────────────────────────
    def emit(self, type_: str, payload: dict | None = None, ref: dict | None = None,
             scope=None, target=None, source=None, writer: str | None = None) -> dict:
        src = source or self.source
        who = writer or self.writer or "user"
        payload = payload or {}
        body = json.dumps(payload, ensure_ascii=False)
        if ref is None and len(body.encode("utf-8")) > PAYLOAD_LIMIT:
            ref = self._put_blob(body)
            payload = {"_ref": True, "keys": sorted(payload.keys()), "bytes": len(body.encode("utf-8"))}
            body = json.dumps(payload, ensure_ascii=False)
        with self._emit_lock:                   # 取号 + 插入要一起完成（并发会撞号）
            seq = self._next_seq(src)
            ev = {"id": "evt-%s-%d" % (src, seq), "v": V, "type": type_, "source": src, "seq": seq,
                  "time": local_iso(), "scope": scope or ["platform"], "target": target}
            ev["writer"] = who
            cur = self.db.conn.execute(
                "INSERT INTO events(id,v,type,source,seq,time,scope,target,payload,ref,writer)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ev["id"], V, type_, src, seq, ev["time"], json.dumps(ev["scope"]),
                 target, body, json.dumps(ref, ensure_ascii=False) if ref else None, who))
            self.db.conn.commit()
            ev["gseq"] = cur.lastrowid
        ev["payload"] = payload
        ev["ref"] = ref
        return ev

    def _next_seq(self, src: str) -> int:
        r = self.db.conn.execute("SELECT MAX(seq) m FROM events WHERE source=?", (src,)).fetchone()
        return int(r["m"] or 0) + 1

    def _put_blob(self, text: str) -> dict:
        data = text.encode("utf-8")
        h = sha256_bytes(data)
        hexpart = h.split(":", 1)[-1]
        p = self.blob_dir / hexpart[:2] / hexpart
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file():
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        rel = str(p.relative_to(self.db.data_dir)).replace("\\", "/")
        return {"kind": "file", "path": rel, "bytes": len(data), "sha256": h}

    # ── 读取 ────────────────────────────────────────────────
    def read(self, after: int = 0, limit: int = 100, types: list | None = None,
             upto: int = 0, writer: str | None = None) -> list:
        sql = "SELECT * FROM events WHERE gseq>?"
        args: list = [after]
        if upto:
            sql += " AND gseq<=?"
            args.append(upto)
        if writer:
            sql += " AND IFNULL(writer,'') LIKE ?"
            args.append(writer.replace("*", "%"))
        if types:
            pats = [t.replace("*", "%") for t in types]
            sql += " AND (" + " OR ".join("type LIKE ?" for _ in pats) + ")"
            args.extend(pats)
        sql += " ORDER BY gseq LIMIT ?"
        args.append(limit)
        return [self._row(r) for r in self.db.conn.execute(sql, args)]

    @staticmethod
    def _row(r) -> dict:
        return {"id": r["id"], "v": r["v"], "type": r["type"], "source": r["source"],
                "seq": r["seq"], "gseq": r["gseq"], "time": r["time"],
                "writer": (r["writer"] if "writer" in r.keys() else None),
                "scope": json.loads(r["scope"] or "[]"), "target": r["target"],
                "payload": json.loads(r["payload"] or "{}"),
                "ref": json.loads(r["ref"]) if r["ref"] else None}

    def last_gseq(self) -> int:
        r = self.db.conn.execute("SELECT MAX(gseq) m FROM events").fetchone()
        return int(r["m"] or 0)

    def stats(self) -> dict:
        by = {r["type"]: r["c"] for r in self.db.conn.execute(
            "SELECT type, COUNT(*) c FROM events GROUP BY type ORDER BY c DESC")}
        writers = {(r["writer"] or "(未标)"): r["c"] for r in self.db.conn.execute(
            "SELECT writer, COUNT(*) c FROM events GROUP BY writer ORDER BY c DESC LIMIT 20")}
        return {"total": self.last_gseq(), "by_type": by, "by_writer": writers}

    def writers_of(self, file_id: str, limit: int = 20) -> list:
        """这篇最近被谁改过（按 payload 里出现的 file_id 过滤，倒序）"""
        out = []
        for r in self.db.conn.execute(
                "SELECT gseq,time,type,writer,payload FROM events"
                " WHERE type LIKE 'note.%' ORDER BY gseq DESC LIMIT 800"):
            if file_id in (r["payload"] or ""):
                p = json.loads(r["payload"] or "{}")
                out.append({"time": r["time"], "type": r["type"], "writer": r["writer"] or "(未标)",
                            "at": p.get("source_root") or p.get("name") or "",
                            "changed_chars": p.get("changed_chars")})
                if len(out) >= limit:
                    break
        return out

    # ── 动作配对（EVENT §4）──────────────────────────────────
    def action(self, name: str, reason: str = "manual", expect_within_ms: int = 0):
        return _Action(self, name, reason, expect_within_ms)


class _Action:
    """with log.action("index.rebuild") as act: … → 自动发 started / settled"""

    def __init__(self, log: EventLog, name: str, reason: str, expect_within_ms: int):
        self.log = log
        self.spec = {"id": "act-%s" % os.urandom(6).hex(), "name": name}
        if expect_within_ms:
            self.spec["expect_within_ms"] = expect_within_ms
        self.reason = reason
        self.started = 0.0
        self.status = "ok"
        self.code = ""
        self.ref = None

    def __enter__(self) -> "_Action":
        self.started = time.time()
        self.log.emit("platform.action.started", {"action": self.spec, "reason": self.reason})
        return self

    def fail(self, code: str = "error") -> None:
        self.status, self.code = "error", code

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.status, self.code = "error", type(exc).__name__
        payload = {"action": self.spec,
                   "outcome": {"status": self.status, "code": self.code,
                               "duration_ms": int((time.time() - self.started) * 1000)}}
        if self.ref:
            payload["ref"] = self.ref
        self.log.emit("platform.action.settled", payload, ref=self.ref)
        return False
