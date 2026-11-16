"""验证：① 老库（v1 unicode61 FTS）能被 _migrate 自动升级为 trigram ② 检索双通道。

用法：python tools/verify_migrate.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "src"))

from db import Database  # noqa: E402
from indexer import Indexer  # noqa: E402


def make_legacy_db(path: str) -> None:
    """手工造一个"老库"：v1 schema，chunks_fts 用 unicode61"""
    conn = sqlite3.connect(path)
    conn.executescript("""
      CREATE TABLE chunks (id TEXT PRIMARY KEY, file_id TEXT, seq INT, kind TEXT, heading TEXT,
        title_path TEXT, text TEXT, char_count INT, content_hash TEXT, locator TEXT,
        is_sensitive INT DEFAULT 0);
      CREATE VIRTUAL TABLE chunks_fts USING fts5(text, heading, content='chunks', content_rowid='rowid');
      CREATE TABLE files (id TEXT PRIMARY KEY, folder_id TEXT, name TEXT, ext TEXT, kind TEXT,
        size INT, content_hash TEXT, mtime TEXT, created_at TEXT, visibility TEXT,
        storage TEXT, content BLOB, storage_ref TEXT);
      CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO files(id,folder_id,name,ext,kind,size,content_hash,visibility,storage,content)"
                 " VALUES('f1','r','老笔记.md','md','markdown',40,'h1','internal','inline',?)",
                 ("这是老库里的上下文内容，用于验证迁移。".encode("utf-8"),))
    conn.execute("INSERT INTO chunks(id,file_id,seq,kind,heading,title_path,text,char_count,is_sensitive)"
                 " VALUES('f1:0','f1',0,'text','测试','[\"测试\"]',?,20,0)",
                 ("这是老库里的上下文内容，用于验证迁移。",))
    rowid = conn.execute("SELECT rowid FROM chunks").fetchone()[0]
    conn.execute("INSERT INTO chunks_fts(rowid,text,heading) VALUES(?,?,?)", (rowid, "这是老库里的上下文内容，用于验证迁移。", "测试"))
    conn.execute("INSERT INTO manifest(key,value) VALUES('schema_version','1')")
    conn.commit()
    conn.close()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="ai-notes-migrate-")
    path = os.path.join(tmp, "index.db")
    make_legacy_db(path)
    print("[1] 造出老库（unicode61 FTS）:", path)
    c = sqlite3.connect(path)
    print("    迁移前 chunks_fts DDL:", (c.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone() or [""])[0][:70])
    c.close()

    print("[2] 用 Database 打开（应触发 _migrate）")
    db = Database(tmp)
    ddl = db.conn.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()["sql"]
    print("    迁移后 tokenize 含 trigram:", "trigram" in ddl)
    print("    schema_version:", db.manifest().get("schema_version"))

    print("[3] 老数据中文检索（trigram 迁移后应能命中）")
    for q in ["上下文", "迁移", "老库"]:
        hits = db.search_chunks(q, limit=3)
        print("    %-5s → %d 命中  %s" % (q, len(hits), (hits[0]["text"][:24] if hits else "")))

    print("[4] 短词兜底（2 字，走 LIKE 通道）")
    hits = db.search_chunks("验证", limit=3)
    print("    验证  → %d 命中" % len(hits))

    print("[5] 自检:", Indexer(db).verify())
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
