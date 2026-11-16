"""ai-notes 数据库层 —— 实现 FORMAT-数据格式规范-v0.4 的库结构。

设计要点（协议对照）：
  · 数据库即唯一真相源（FORMAT §0.1）
  · 对用户呈现为"文件夹 + 文件"（folders / files）
  · 派生层同库：chunks / chunks_fts / vectors / edges（FORMAT §2/§3）
  · 内容存储抽象 storage: inline | pool（FORMAT §5）
  · 私密加锁：visibility + enc_algo/key_ref；chunks.is_sensitive（FORMAT §4）
  · manifest 记录 schema 版本与模型参数（判断"索引是否过时"的唯一依据）

本文件只做"库与表"，业务逻辑放 service 层；上层**不得**直接读写 content 列，
一律用 get_content / put_content（FORMAT §5.1）。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

import vaultcrypto
from embedinput import SPEC as EMBED_SPEC, embed_text
from mentions import resolve as resolve_mentions, section_hit, split_anchor

SCHEMA_VERSION = 11       # 11：files.frontmatter（YAML 前置属性）；10：events.writer；9：indexed_sig

# 私密文件夹在**库内**的路径（不带 vault 层）。从 priv_folder 派生，不另写一份字符串。
try:
    import priv_folder as _pf_top
    PRIV_DIR_PATH = _pf_top.PRIV_DIR_REL
    # 库根那一层（folder_path/folder_by_path 的口径）—— 两者别混（踩过）
    PRIV_DIR_PATH_ROOTED = "vault/" + _pf_top.PRIV_DIR_REL
except Exception:
    PRIV_DIR_PATH = "个人/私密"
    PRIV_DIR_PATH_ROOTED = "vault/个人/私密"
DEFAULT_DATA_DIR = r"D:\core-notes\data"

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS folders (
  id          TEXT PRIMARY KEY,
  parent_id   TEXT,
  name        TEXT NOT NULL,
  sort        INTEGER DEFAULT 0,
  created_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id);

CREATE TABLE IF NOT EXISTS files (
  id           TEXT PRIMARY KEY,
  folder_id    TEXT NOT NULL,
  name         TEXT NOT NULL,
  ext          TEXT,
  kind         TEXT NOT NULL,
  size         INTEGER DEFAULT 0,
  content_hash TEXT,
  mtime        TEXT,
  created_at   TEXT,
  visibility   TEXT NOT NULL DEFAULT 'internal',
  doc_date     TEXT,                       -- 笔记自己的日期（从路径/名解析；mtime 是导入时间，不能当日期）
  storage      TEXT NOT NULL DEFAULT 'inline',
  content      BLOB,
  storage_ref  TEXT,
  enc_algo     TEXT,
  key_ref      TEXT,
  indexed_sig  TEXT                        -- 索引签名：内容/名字/日期/策略/规格/解析器 任一变化就重建
);
CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_id);
CREATE INDEX IF NOT EXISTS idx_files_hash   ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_vis    ON files(visibility);

CREATE TABLE IF NOT EXISTS tags (id TEXT PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS file_tags (file_id TEXT, tag_id TEXT, PRIMARY KEY (file_id, tag_id));

CREATE TABLE IF NOT EXISTS edges (
  a_file_id  TEXT NOT NULL,
  b_file_id  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  anchor     TEXT,
  b_section  TEXT,                       -- 目标端小节（建立时匹配到的规范小节名；整篇引用为空）
  weight     REAL DEFAULT 1.0,
  model      TEXT,
  created_at TEXT,
  PRIMARY KEY (a_file_id, b_file_id, kind, anchor)
);
CREATE INDEX IF NOT EXISTS idx_edges_b ON edges(b_file_id);
-- idx_edges_bs 在 _migrate_edges_column 里建（老库要先 ALTER 加列，SCHEMA 里建会报 no such column）

-- 派生层：文档格式（docx/odt/rtf/pdf/epub/html…）解析出来的 Markdown 缓存
CREATE TABLE IF NOT EXISTS derived (
  file_id     TEXT PRIMARY KEY,
  format      TEXT,
  parser      TEXT,
  source_hash TEXT,                       -- 解析时的内容哈希（变了就重解析）
  text        TEXT,                       -- 解析出来的 Markdown
  meta        TEXT,                       -- JSON：页数/标题/作者/图片数…
  error       TEXT,
  created_at  TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
  id           TEXT PRIMARY KEY,
  file_id      TEXT NOT NULL,
  seq          INTEGER,
  kind         TEXT,
  heading      TEXT,
  title_path   TEXT,
  text         TEXT,
  char_count   INTEGER,
  content_hash TEXT,
  locator      TEXT,
  is_sensitive INTEGER DEFAULT 0,
  block_id     TEXT,
  char_start   INTEGER,
  char_end     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
-- 文档块树（2026-09-18）：页/块/类型/层级/bbox/表格 + 在派生文本里的字符偏移
CREATE TABLE IF NOT EXISTS blocks (
  id         TEXT PRIMARY KEY,
  file_id    TEXT NOT NULL,
  seq        INTEGER,
  page_no    INTEGER,
  type       TEXT,
  level      INTEGER,
  bbox       TEXT,
  char_start INTEGER,
  char_end   INTEGER,
  text_md    TEXT,
  table_json TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_blocks_file ON blocks(file_id, seq);
-- FTS 用 trigram 分词器：默认 unicode61 不切中文（整段中文=一个 token，搜不到）
-- trigram 可做中文子串检索；短词（<3 字符）由 search_chunks 用 LIKE 兜底
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, heading, content='chunks', content_rowid='rowid', tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS vectors (
  chunk_id TEXT PRIMARY KEY,
  model    TEXT,
  dim      INTEGER,
  vec      BLOB
);

CREATE TABLE IF NOT EXISTS manifest (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS tombstones (
  file_id    TEXT PRIMARY KEY,
  name       TEXT,
  display    TEXT,          -- 引用时用的短名（去 .md 后缀）
  folder_path TEXT,
  deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tomb_display ON tombstones(display);

CREATE TABLE IF NOT EXISTS events (
  gseq    INTEGER PRIMARY KEY AUTOINCREMENT,
  id      TEXT, v INTEGER, type TEXT, source TEXT, seq INTEGER,
  time    TEXT, scope TEXT, target TEXT, payload TEXT, ref TEXT,
  writer  TEXT                       -- 谁写的：user / agent:<名> / tool:<名> / task:<名> / auto:<UA>
);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_events_time ON events(time);
-- 取号走 `SELECT MAX(seq) WHERE source=?`：**source 不建索引就是每次全表扫**
-- （2026-10-03 实测：12.5 万行的事件表，一次 62.7ms —— 每条事件都白花这么多，库越长越慢）
CREATE INDEX IF NOT EXISTS idx_events_source_seq ON events(source, seq);
"""

MANIFEST_DEFAULTS = {
    "schema_version": str(SCHEMA_VERSION),
    "chunk_max_chars": "1500",
    "chunk_overlap": "150",
    "pool_threshold": str(1024 * 1024),
    "embedding_model": "",
    "dim": "",
}


def local_iso(t: float | None = None) -> str:
    """本地时间 ISO（带偏移）—— EVENT §6：不得用 UTC 裸时间"""
    ts = time.time() if t is None else t
    lt = time.localtime(ts)
    off = -(time.altzone if lt.tm_isdst else time.timezone)
    sign = "+" if off >= 0 else "-"
    off = abs(off)
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + "%s%02d:%02d" % (sign, off // 3600, (off % 3600) // 60)


class BadName(ValueError):
    """名字带了不该带的东西（路径分隔符 / 上级目录 / 绝对路径 / 控制字符）。"""


_BAD_NAME_CHARS = set('/\\:*?"<>|')


def check_name(name, what: str = "名字") -> str:
    """校验一个文件/文件夹名，返回掐过空白的名字；不合法就抛 BadName。

    为什么必须在这儿挡（2026-09-30 体检）：
    `tree_write` 把「文件夹路径 + 名字」**直接拼成磁盘路径**（`Path(library_dir)/rel`）。
    于是名字里只要出现 `/`、`\`、`..` 或盘符，接口就能写到库文件夹**外面** ——
    实测：`rename name="../x"` 能覆盖库文件夹外的文件；文件夹名叫 `..` 再 DELETE 会
    递归删掉父目录（真库上就是 `D:\枢阁` 整棵树）。
    所以入口挡一次（这里），落盘处再兜一次（`tree_write._inside`）。
    """
    s = str(name or "").strip()
    if not s:
        raise BadName("%s不能为空" % what)
    if s in (".", ".."):
        raise BadName("%s不能是 %s" % (what, s))
    bad = sorted({c for c in s if c in _BAD_NAME_CHARS or ord(c) < 32})
    if bad:
        raise BadName("%s里不能有这些字符：%s" % (what, " ".join(bad)))
    if s[-1] in ". ":
        raise BadName("%s不能以点或空格结尾" % what)
    return s


def rewrite_at_mentions(text: str, old: str, new: str) -> tuple:
    """把正文里的 @旧名 改成 @新名。

    只在"@ + 旧名 + 边界"处替换：@abc 不会吃掉 @abcd 的一部分（边界=空白/标点/行尾）。
    代码围栏里的不动（那是代码，不是提及）。
    """
    import re as _re
    # 后面必须紧跟"边界"（空白/标点/行尾）：@abc 不会动 @abcd 里的那截
    pat = _re.compile(r"@" + _re.escape(old) + r"(?![^\s@\[\]()（）,，。；;：:！!？?*`#>|/\\])")
    out, fence, n = [], False, 0
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        line, k = pat.subn("@" + new, line)
        n += k
        out.append(line)
    return "\n".join(out), n


def rewrite_at_anchor(text: str, old: str, new: str) -> tuple:
    """把正文里的 @旧锚点 换成 @新锚点（锚点＝字面量，可能是**路径**）。

    边界判定与 rewrite_at_mentions 一致：后面必须是空白/标点/`#`/行尾，
    所以 `@路径/名字#小节` 只换锚点、小节原样保留。
    """
    import re as _re
    pat = _re.compile(r"@" + _re.escape(old) + r"(?![^\s@\[\]()（）,，。；;：:！!？?*`#>|\\])")
    out, fence, n = [], False, 0
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        if fence:
            out.append(line)
            continue
        line, k = pat.subn("@" + new, line)
        n += k
        out.append(line)
    return "\n".join(out), n


def ref_display(name: str) -> str:
    """引用里可以省的扩展名：md/markdown/txt/log + 所有文档格式（pdf/docx/odt/rtf/html/epub…）

    2026-09-18：文档进库后，人写引用说的是"季度报告"，不是"@季度报告.pdf"。
    同名冲突（既有 报告.pdf 又有 报告.docx）会自动退化成"必须写路径"，不会乱指。
    """
    n = str(name or "")
    low = n.lower()
    exts = ["markdown", "md", "txt", "log"]
    try:
        import doctext
        exts += list(doctext.DOC_EXTS)
    except Exception:
        pass
    for e in sorted(set(exts), key=len, reverse=True):
        if low.endswith("." + e):
            return n[:-(len(e) + 1)]
    return n


def block_id_of(title_path, text: str) -> str:
    """稳定块 ID：内容寻址（标题层级路径 + 归一化正文）

    为什么要它（2026-09-18 辞玖定的方向）：块的身份必须是**内容**而不是位置。
    用 `file_id:seq` 当身份时，在中间插一段会让后面所有块的 seq 平移，
    明明没改的块也被判成"变了"（实测插一节 = 6 块白重算）；换成内容寻址后只重算真正变的那块，
    而且以后引用/定位（命中定位到具体块）可以认这个 ID，编辑别处不影响它。
    """
    tp = "/".join(str(x) for x in (title_path or []))
    norm = " ".join((text or "").split())
    return hashlib.sha1(("%s\n%s" % (tp, norm)).encode("utf-8")).hexdigest()[:16]


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


# 能力划分（辞玖 2026-09-25）：什么文件都能进，但"能不能打开 / 能不能编辑 / 能不能检索"分开说
IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "avif", "svg"}
KNOWN_TEXT_EXTS = {"md", "markdown", "txt", "text", "log", "py", "js", "ts", "go", "java", "c",
                   "cpp", "h", "rs", "rb", "php", "json", "yaml", "yml", "toml", "ini", "cfg",
                   "sql", "sh", "ps1", "css", "html", "csv", "tsv", "jsonl"}


def caps_for(kind: str, ext: str = "", name: str = "") -> dict:
    """这一篇支持什么：open=能看（预览/图片/解析）· edit=能改正文 · search=能进检索库"""
    e = (ext or "").lower().lstrip(".")
    k = kind or ""
    can_open = k in ("markdown", "text", "code", "document") or e in IMAGE_EXTS
    can_edit = k in ("markdown", "text", "code")
    can_search = k in ("markdown", "text", "code", "document")
    return {"open": bool(can_open), "edit": bool(can_edit), "search": bool(can_search),
            "store_only": not (can_open or can_edit or can_search)}


class ThreadDB:
    """**线程安全的库门面**（2026-10-01 并发化）。

    为什么要有它：sqlite 连接不能跨线程用，而本产品上层到处都在用 `db.xxx()`，
    还有几个**持有 db 的单例**（事件日志 / 索引器 / 检索器 / 文章还原）。
    与其给每个消费者改一遍，不如做个门面：**谁在哪个线程调，就用那个线程自己的连接**。

    · 第一条连接跑一遍建表/迁移，后面的线程直接复用已建好的库（init=False）
    · 写仍然串行（server 里的 WRITE_LOCK），不会有两个写事务打架
    · WAL + busy_timeout：多读单写，读不跟写互斥
    """

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR):
        self._dir = str(data_dir)
        self._tls = threading.local()
        self._lock = threading.Lock()          # 只护"新建连接/建表"，不护查询（查询各走各的连接）
        self._schema_ready = False
        # 启动阶段（还没有工人线程）就把主连接建好：建表/迁移**只跑这一次**，
        # 后面的线程 init=False 直接复用，不会两条线程同时建表。
        self._tls.db = self._connect(init=True)

    def _connect(self, init: bool) -> "Database":
        with self._lock:
            if init and not self._schema_ready:
                cur = Database(self._dir, init=True)
                self._schema_ready = True
            else:
                cur = Database(self._dir, init=False)
        return cur

    def _db(self) -> "Database":
        cur = getattr(self._tls, "db", None)
        if cur is None:
            cur = self._connect(init=not self._schema_ready)
            self._tls.db = cur
        return cur

    @property
    def conn(self):
        return self._db().conn

    @property
    def data_dir(self):
        return self._db().data_dir

    @property
    def path(self):
        return self._db().path

    def close(self) -> None:
        cur = getattr(self._tls, "db", None)
        if cur is not None:
            cur.close()
            self._tls.db = None

    def __getattr__(self, name):          # 其余方法/属性一律转发给本线程那条连接
        return getattr(self._db(), name)


class Database:
    """库门面：连接、建表、内容存取、manifest"""

    def __init__(self, data_dir: str = DEFAULT_DATA_DIR, init: bool = True):
        """init=False：只开连接、不跑建表/迁移（给"每线程一条连接"用：库已经建好了）"""
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "index.db"
        self.conn = sqlite3.connect(str(self.path), timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=15000")   # 服务与命令行同时用时等一等，别直接报锁
        if init:
            self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self._migrate()
        self._repair_orphan_folders()
        self._add_doc_date_column()
        self._add_rag_column()
        self._migrate_events_writer()          # 写入者标签（events.writer）
        self._add_frontmatter_column()         # YAML 前置属性（Obsidian 叫"属性"）
        self._add_block_id_column()
        self._add_chunk_offsets_column()
        self._add_indexed_sig_column()
        self._migrate_edges_pk()
        self._migrate_edges_column()

    def _migrate(self) -> None:
        """旧库迁移：v1 的 chunks_fts 是 unicode61（中文搜不到）→ 重建为 trigram

        派生表可重建，所以直接 DROP + 重建 + 回填，不需备份。
        """
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='chunks_fts'").fetchone()
        sql = (row["sql"] or "") if row else ""
        if "trigram" in sql:
            self._sync_schema_version()
            return
        self.conn.execute("DROP TABLE IF EXISTS chunks_fts")
        self.conn.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5("
            "text, heading, content='chunks', content_rowid='rowid', tokenize='trigram')")
        self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        self.conn.commit()
        self._sync_schema_version()
        print("[db] 已把 chunks_fts 迁移为 trigram 并回填")

    def _add_frontmatter_column(self) -> int:
        """老库补 frontmatter 列（存解析出来的 YAML 前置属性，JSON）"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(files)")]
        if "frontmatter" in cols:
            return 0
        self.conn.execute("ALTER TABLE files ADD COLUMN frontmatter TEXT DEFAULT ''")
        self.conn.commit()
        return 1

    def set_frontmatter(self, file_id: str, meta_json: str) -> None:
        self.conn.execute("UPDATE files SET frontmatter=? WHERE id=?", (meta_json or "", file_id))
        self.conn.commit()

    def frontmatter_of(self, file_id: str) -> dict:
        row = self.conn.execute("SELECT frontmatter FROM files WHERE id=?", (file_id,)).fetchone()
        if not row or not row["frontmatter"]:
            return {}
        try:
            import json as _json
            v = _json.loads(row["frontmatter"])
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def _apply_frontmatter(self, file_id: str, data: bytes) -> None:
        """内容写完后：解析 YAML 前置属性 → 存 meta / 合并标签 / 补日期。

        只对文本类文件做（图片/exe 不碰）；失败不能挡住保存。
        """
        row = self.conn.execute("SELECT kind FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None or (row["kind"] or "") not in ("markdown", "text", "code"):
            return
        try:
            text = data.decode("utf-8", "replace")
        except Exception:
            return
        try:
            from frontmatter import apply as _fm_apply
            _fm_apply(self, file_id, text)
        except Exception as e:
            import sys as _sys
            _sys.stderr.write("[frontmatter] %s: %s\n" % (type(e).__name__, str(e)[:140]))

    def _add_doc_date_column(self) -> int:
        """老库补 doc_date 列，并按"路径 + 文件名"回填（幂等）"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(files)")]
        if "doc_date" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN doc_date TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_files_date ON files(doc_date)")
        # 2026-10-12：/api/health 里有两条"按表达式过滤"的计数（rag_off / stale_sig），
        # 以前是全表扫（实测 88.7ms + 44ms，库 1394 篇时）——加表达式索引后各自降到 ~1ms。
        # ⚠️ 这一步跑在**迁移**里：老库这时候可能还没有 rag / indexed_sig 列（别的迁移步骤后加），
        #    所以必须先看列在不在 —— 不然老库（和所有"起临时库"的冒烟）会直接
        #    `no such column: rag` 崩掉（2026-10-12 体检 56/71 就是这么来的）。
        cols_now = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)")}
        if "rag" in cols_now:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_files_rag_x ON files(IFNULL(rag,'on'))")
        if "indexed_sig" in cols_now:
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_files_sig_x ON files(IFNULL(indexed_sig,''))")
        self.conn.commit()
        n = 0
        for r in self.conn.execute(
                "SELECT id, folder_id, name FROM files WHERE IFNULL(doc_date,'')=''").fetchall():
            d = self.derive_doc_date(r["folder_id"], r["name"])
            if d:
                self.conn.execute("UPDATE files SET doc_date=? WHERE id=?", (d, r["id"]))
                n += 1
        if n:
            self.conn.commit()
        return n

    def _add_rag_column(self) -> int:
        """老库补 rag 列（是否进检索库）：存量全部回填 'on'，不改变既有行为"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(files)")]
        if "rag" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN rag TEXT DEFAULT 'on'")
        n = self.conn.execute(
            "UPDATE files SET rag='on' WHERE IFNULL(rag,'')=''").rowcount
        self.conn.commit()
        return n

    def _add_block_id_column(self) -> int:
        """老库补 chunks.block_id（内容寻址的稳定块 ID）并回填（幂等）"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(chunks)")]
        if "block_id" not in cols:
            self.conn.execute("ALTER TABLE chunks ADD COLUMN block_id TEXT")
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_block ON chunks(block_id)")
        self.conn.commit()
        n = 0
        for r in self.conn.execute(
                "SELECT id, title_path, text FROM chunks WHERE IFNULL(block_id,'')=''").fetchall():
            try:
                tp = json.loads(r["title_path"] or "[]")
            except Exception:
                tp = []
            self.conn.execute("UPDATE chunks SET block_id=? WHERE id=?",
                              (block_id_of(tp, r["text"] or ""), r["id"]))
            n += 1
        if n:
            self.conn.commit()
        return n

    def _add_chunk_offsets_column(self) -> int:
        """老库补 chunks.char_start/char_end（在派生文本里的偏移，块树定位用）"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(chunks)")]
        added = 0
        for col in ("char_start", "char_end"):
            if col not in cols:
                self.conn.execute("ALTER TABLE chunks ADD COLUMN %s INTEGER" % col)
                added += 1
        self.conn.commit()
        return added

    def _add_indexed_sig_column(self) -> int:
        """老库补 files.indexed_sig：空值 = "还没按新策略建过"，下次重建就补上"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(files)")]
        if "indexed_sig" not in cols:
            self.conn.execute("ALTER TABLE files ADD COLUMN indexed_sig TEXT")
            self.conn.commit()
            return 1
        return 0

    def _migrate_events_writer(self) -> int:
        """events 补 writer 列（写入者标签；2026-09-24 加）"""
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(events)")]
        added = 0
        if "writer" not in cols:
            self.conn.execute("ALTER TABLE events ADD COLUMN writer TEXT")
            added = 1
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_events_writer ON events(writer)")
        self.conn.commit()
        return added

    def _migrate_edges_column(self) -> int:
        """edges 加 b_section 并回填（幂等）

        为什么要它：引用只有"源端字面量"时，反查"谁引了我的 P12"只能把这篇的所有反向链接
        捞出来再做字符串归一对齐。把目标端小节**存成键的一部分**，反查才是精确命中，
        小节改名也才有跟随的依据。
        """
        cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(edges)")]
        if "b_section" not in cols:
            self.conn.execute("ALTER TABLE edges ADD COLUMN b_section TEXT")
            self.conn.commit()
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_bs ON edges(b_file_id, b_section)")
        self.conn.commit()
        # 回填：锚点里带 # 的边，按目标篇的小节名归一匹配一次
        rows = self.conn.execute(
            "SELECT rowid, b_file_id, anchor FROM edges"
            " WHERE kind='mention' AND IFNULL(b_section,'')='' AND anchor LIKE '%#%'").fetchall()
        cache, n = {}, 0
        for r in rows:
            _, tok = split_anchor(r["anchor"])
            if not tok:
                continue
            if r["b_file_id"] not in cache:
                cache[r["b_file_id"]] = self.section_names(r["b_file_id"])
            hit = section_hit(cache[r["b_file_id"]], tok)
            if hit:
                self.conn.execute("UPDATE edges SET b_section=? WHERE rowid=?", (hit, r["rowid"]))
                n += 1
        if n:
            self.conn.commit()
            print("[db] 回填 edges.b_section：%d 条" % n)
        return n

    def _migrate_edges_pk(self) -> int:
        """把 edges 主键从 (a,b,kind) 升到 (a,b,kind,anchor)（幂等）

        为什么：老主键下一"篇笔记对同一个目标只能有一条边"，于是
        `@X#P1` 与 `@X#P3` 会互相覆盖（小节级引用直接丢一条）。
        """
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='edges'").fetchone()
        sql = (row["sql"] or "") if row else ""
        if "PRIMARY KEY (a_file_id, b_file_id, kind, anchor)" in sql.replace("\n", " "):
            return 0
        n = self.conn.execute("SELECT COUNT(*) c FROM edges").fetchone()["c"]
        self.conn.executescript(
            "ALTER TABLE edges RENAME TO edges_old;"
            "CREATE TABLE edges ("
            " a_file_id TEXT NOT NULL, b_file_id TEXT NOT NULL, kind TEXT NOT NULL,"
            " anchor TEXT, weight REAL DEFAULT 1.0, model TEXT, created_at TEXT,"
            " PRIMARY KEY (a_file_id, b_file_id, kind, anchor));"
            "INSERT OR REPLACE INTO edges(a_file_id,b_file_id,kind,anchor,weight,model,created_at)"
            " SELECT a_file_id,b_file_id,kind,anchor,weight,model,created_at FROM edges_old;"
            "DROP TABLE edges_old;"
            "CREATE INDEX IF NOT EXISTS idx_edges_b ON edges(b_file_id);")
        self.conn.commit()
        self._sync_schema_version()
        print("[db] edges 主键升级为 (a,b,kind,anchor)，搬了 %d 条边" % n)
        return n

    def _repair_orphan_folders(self) -> None:
        """顶层文件夹若 parent 为空，挂回根目录（早期导入留下的游离目录）"""
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM folders WHERE IFNULL(parent_id,'')='' AND name<>''").fetchone()["c"]
        if not n:
            return                                     # 没得修就别动写锁
        rid = self.root_folder_id()
        self.conn.execute(
            "UPDATE folders SET parent_id=? WHERE IFNULL(parent_id,'')='' AND name<>''", (rid,))
        self.conn.commit()
        print("[db] 已把 %d 个游离的顶层文件夹挂回根目录" % n)

    def _sync_schema_version(self) -> None:
        """把 manifest.schema_version 对齐到当前 SCHEMA_VERSION（迁移后必须更新）"""
        cur = self.conn.execute("SELECT value FROM manifest WHERE key='schema_version'").fetchone()
        if cur is None or str(cur["value"]) != str(SCHEMA_VERSION):
            self.conn.execute("INSERT OR REPLACE INTO manifest(key,value) VALUES('schema_version',?)",
                              (str(SCHEMA_VERSION),))
            self.conn.commit()
        # 缺的默认键要补（老库在新版本加了键时不能视而不见，
        # 否则 pool_threshold 这类"默认值"实际是空的）
        for k, v in MANIFEST_DEFAULTS.items():
            self.conn.execute("INSERT OR IGNORE INTO manifest(key,value) VALUES(?,?)", (k, str(v)))
        self.conn.commit()

    def manifest(self) -> dict:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key,value FROM manifest")}

    def set_manifest(self, **kv) -> None:
        for k, v in kv.items():
            self.conn.execute("INSERT OR REPLACE INTO manifest(key,value) VALUES(?,?)", (k, str(v)))
        self.conn.commit()

    # ── 内容存取（唯一入口；FORMAT §5.1）──────────────────────
    def get_content(self, file_id: str, master: bytes | None = None) -> bytes:
        """取正文。

        倒转（2026-10-14）：**文件系统是唯一真相源** —— 所以先看盘上那份。
        路由规则（见 read_disk_content 的说明）：
          · 非私密 + 库树里有这一篇 + manifest 开了 disk_first → 读盘
          · 私密 / 不在库树里 / 没配库树 / 读盘失败 → 退回库内那份（旧行为）
        加密文件缺 master 时抛 LockedError
        """
        # 私密文件夹模式（2026-10-15）：私密篇的正文可能在**加密包**里。
        # 位置判据来自 priv_folder（你定的第①条：位置决定私密）。
        # ⚠️ 这一判必须在读盘**之前** —— 否则会先读到盘上那份独立密文文件（那是旧形态）。
        if self.priv_effective_mode() == "package" and self.priv_should_route_to_pack(file_id):
            if not master:
                raise vaultcrypto.LockedError(file_id)      # 锁定态：包里那份解不开
            return self._priv_read_from_pack(file_id, master)
        if self._disk_first():
            data, why = self.read_disk_content(file_id)
            if data is not None:
                return data
            # 私密走库内解密（要 master）；其余"盘上没有"的退回旧路，别把「读不到」当成空
        r = self.conn.execute(
            "SELECT storage, content, storage_ref, enc_algo, key_ref FROM files WHERE id=?",
            (file_id,)).fetchone()
        if r is None:
            raise KeyError("no such file: %s" % file_id)
        if r["storage"] == "pool":
            p = self.data_dir / "pool" / (r["storage_ref"] or "")
            raw = p.read_bytes() if p.is_file() else b""
        else:
            raw = r["content"] or b""
        if r["enc_algo"]:
            if not master:
                raise vaultcrypto.LockedError(file_id)
            key = vaultcrypto.open_key_ref(master, r["key_ref"], self.vault_priv(master))
            return vaultcrypto.open_sealed(key, raw)
        return raw

    def put_content(self, file_id: str, data: bytes, master: bytes | None = None) -> None:
        """写内容，超过阈值走池；加密文件按 K_f 加密后写入。

        content_hash / size 记明文，池文件名按密文算。
        """
        row = self.conn.execute("SELECT enc_algo, key_ref FROM files WHERE id=?", (file_id,)).fetchone()
        h = sha256_bytes(data)
        if row and row["enc_algo"]:
            # 私密文件夹模式：正文写进加密包（库里只留 size/hash/mtime 这些索引数据）。
            # 写包要主密钥 —— 包密钥是用库公钥封的，得用私钥开盒才拿得到。
            if self.priv_effective_mode() == "package" and self.priv_should_route_to_pack(file_id, row):
                self._priv_write_to_pack(file_id, data, master)
                self._apply_frontmatter(file_id, data)
                return
            if not master:
                raise vaultcrypto.LockedError(file_id)
            key = vaultcrypto.open_key_ref(master, row["key_ref"], self.vault_priv(master))
            self._write_blob(file_id, vaultcrypto.seal(key, data), h, len(data),
                             enc_algo=row["enc_algo"], key_ref=row["key_ref"])
        else:
            self._write_blob(file_id, data, h, len(data), enc_algo=None, key_ref=None)
        # YAML 前置属性（Obsidian "属性"）：写完正文顺手把 tags/title/date 带进库
        self._apply_frontmatter(file_id, data)
        # 倒转（2026-10-14）：文件系统是唯一真相源 → 正文落盘。
        # **双写期**（库内那份先留着）—— 出错能立刻退回旧行为；
        # 等验证透了再走 B6 清库内那份。
        if self._disk_projection():
            self._project_to_disk(file_id, data)

    def append_content(self, file_id: str, data: bytes, master: bytes | None = None) -> int:
        """**在正文尾部追加**（不是整篇替换）。返回追加前的字节数。

        给谁用：日志转录那种"只会越长"的篇（一会话一篇、一天一份）。
        为什么不做字节级追加：正文是"一篇一个 blob"（内联或池，**池名就是内容哈希**），
        按字节追加会让池名与内容对不上。所以这儿读旧+接新，仍走 `put_content` 那条路
        （哈希/size/mtime/前置属性一致由它保证）；省下来的是**上游**的渲染与整篇传输。
        """
        old = self.get_content(file_id, master)
        self.put_content(file_id, old + data, master)
        return len(old)

    # ── 加密包读写（package 模式专用）2026-10-15 ─────────────────
    def _priv_write_to_pack_safe(self, file_id: str, data: bytes,
                                 master: bytes | None) -> bool:
        """写包，但**失败不抛**（调用方的库内写入已经成功）。

        取舍与 `_project_to_disk` 一致：主体动作已完成，不能因为"顺带写包"失败
        就把整次操作判失败；但**必须留痕**，别让它静默半成功。
        """
        try:
            self._priv_write_to_pack(file_id, data, master)
            return True
        except Exception as e:
            try:
                print("[priv_pack] 写包失败（库内已写入）：%s: %s"
                      % (type(e).__name__, str(e)[:60]), flush=True)
            except Exception:
                pass
            return False

    def _pack_drop_current(self, file_id: str, master: bytes | None,
                           note: str = "") -> bool:
        """把这一篇从加密包里删掉（用**当前** disk_rel 算它的包内位置）。

        什么时候用：移出私密、改名（改名用 _pack_rename_current 而不是这个）。
        锁着 / 没包 / 不在包里 → 返回 False 并**不改任何东西**（不静默乱动）。
        """
        if not master or self.priv_effective_mode() != "package":
            return False
        lib = self._lib_root()
        if not lib:
            return False
        import priv_folder as _pf
        inside = _pf.inside_rel(self.disk_rel_of(file_id) or "")
        if not inside:
            return False
        folder, _, name = inside.rpartition("/")
        name = name or inside
        try:
            priv = self.vault_priv(master)
            items = _pf.read_items(lib, master, priv)
            if _pf.get_item_bytes(items, name, folder) is None:
                return False                      # 本来就不在包里 → 没什么可删
            items2, dropped = _pf.drop_item(items, name, folder)
            if not dropped:
                return False
            _pf.write_items(lib, items2, self.vault_pub(), master=master, vault_priv_raw=priv)
            return True
        except Exception as e:
            try:
                print("[priv_pack] 从包里删 %s 失败：%s: %s"
                      % (name, type(e).__name__, str(e)[:60]), flush=True)
            except Exception:
                pass
            return False

    def _pack_rename_current(self, file_id: str, new_name: str, master: bytes | None) -> bool:
        """改名后把包内条目改成新名字。

        ⚠️ **必须在库内名字改掉之前调用**（用旧名定位包内那条）。
        原因与 move_in 那条同一个：改完之后 disk_rel_of 就是新名，
        用新名去包里找旧条目 → 找不到 → 这条永远留在包里（改名即失联）。
        """
        if not master or self.priv_effective_mode() != "package":
            return False
        lib = self._lib_root()
        if not lib:
            return False
        import priv_folder as _pf
        inside = _pf.inside_rel(self.disk_rel_of(file_id) or "")
        if not inside:
            return False
        folder, _, old_name = inside.rpartition("/")
        old_name = old_name or inside
        if old_name == new_name:
            return False
        try:
            priv = self.vault_priv(master)
            items = _pf.read_items(lib, master, priv)
            data = _pf.get_item_bytes(items, old_name, folder)
            if data is None:
                return False                      # 不在包里 → 不用改
            items = _pf.drop_item(items, old_name, folder)[0]
            items = _pf.upsert_item(items, new_name, data, folder=folder)
            _pf.write_items(lib, items, self.vault_pub(), master=master, vault_priv_raw=priv)
            return True
        except Exception as e:
            try:
                print("[priv_pack] 包内改名 %s → %s 失败：%s: %s"
                      % (old_name, new_name, type(e).__name__, str(e)[:60]), flush=True)
            except Exception:
                pass
            return False

    def _priv_pack_ctx(self, master: bytes | None):
        """(库根, 库私钥) —— 包读写都要这两样"""
        return self._lib_root(), (self.vault_priv(master) if master else None)

    def _priv_read_from_pack(self, file_id: str, master: bytes) -> bytes:
        """从加密包取这一篇的正文。

        **读不出来时明确抛错，不回空**（P39：「没有」与「读不出来」是两件事）——
        回空会让上层把一篇私密笔记当成空文件，那是静默数据事故。
        """
        row = self.conn.execute("SELECT name FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise KeyError("no such file: %s" % file_id)
        lib, priv = self._priv_pack_ctx(master)
        if not lib:
            raise IOError("package 模式但没配 library_dir，包在哪都不知道")
        import priv_folder as _pf
        inside = _pf.inside_rel(self.disk_rel_of(file_id))
        folder, _, name = inside.rpartition("/")
        items = _pf.read_items(lib, master, priv)
        b = _pf.get_item_bytes(items, name or inside, folder)
        if b is None:
            # **自愈**（2026-10-15）：包里没有这一篇，但库内那份密文还在 ——
            # 把它解出来、**顺手补进包**，而不是直接报错。
            #
            # 为什么需要：迁移把"在私密文件夹里"的篇路由到包，而写包要主密钥（要解锁）。
            # 于是"迁移"与"解锁"之间有个先后依赖。有了自愈，**锁定态就能跑迁移**，
            # 解锁后第一次读到谁就把它补进包 —— 不需要迁移脚本持有密码。
            try:
                nb = self._priv_legacy_read(file_id, master)
            except Exception as e:
                raise IOError("包里没有这一篇，库内那份也读不出来（%s）：%s: %s"
                              % (inside or file_id, type(e).__name__, str(e)[:50]))
            try:
                items = _pf.upsert_item(items, name or inside, nb, folder=folder)
                _pf.write_items(lib, items, self.vault_pub(), master=master, vault_priv_raw=priv)
                try:
                    print("[priv_pack] 自愈：把 %s 补进了加密包" % (inside or file_id),
                          flush=True)
                except Exception:
                    pass
            except Exception as e:
                try:
                    print("[priv_pack] 自愈写包失败（本次仍返回正确内容）：%s"
                          % str(e)[:60], flush=True)
                except Exception:
                    pass
            return nb
        return b

    def _priv_legacy_read(self, file_id: str, master: bytes) -> bytes:
        """从**库内那份密文**解出正文（不走盘、不走包路由）。

        用途：package 模式下包里还没有这一篇时的兜底读取（自愈用）。
        与 `get_content` 的区别：它**不会**再回到包路由，避免递归。
        """
        r = self.conn.execute(
            "SELECT storage, content, storage_ref, enc_algo, key_ref FROM files WHERE id=?",
            (file_id,)).fetchone()
        if r is None:
            raise KeyError("no such file: %s" % file_id)
        if r["storage"] == "pool":
            p = self.data_dir / "pool" / (r["storage_ref"] or "")
            raw = p.read_bytes() if p.is_file() else b""
        else:
            raw = r["content"] or b""
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not r["enc_algo"]:
            return raw                      # 本来就没加密（不该发生，但别炸）
        if not master:
            raise vaultcrypto.LockedError(file_id)
        key = vaultcrypto.open_key_ref(master, r["key_ref"], self.vault_priv(master))
        return vaultcrypto.open_sealed(key, raw)

    def _priv_write_to_pack(self, file_id: str, data: bytes, master: bytes | None) -> None:
        """把这一篇写进加密包（库里那行同时按 legacy 写一份密文，见方法内的说明）。

        包是**读**的真相源；库内那份密文暂时保留，因为：
          · `tree_write` 落盘投影要从 content 列取密文
          · 「库里/盘上那些独立密文留不留」是 C 项，等你定
        """
        if not master:
            raise vaultcrypto.LockedError(file_id)     # 写包要主密钥开盒，锁着写不了
        row = self.conn.execute("SELECT name FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise KeyError("no such file: %s" % file_id)
        lib, priv = self._priv_pack_ctx(master)
        if not lib:
            raise IOError("package 模式但没配 library_dir")
        import priv_folder as _pf
        inside = _pf.inside_rel(self.disk_rel_of(file_id))
        folder, _, name = inside.rpartition("/")
        name = name or inside
        items = _pf.read_items(lib, master, priv)
        items = _pf.upsert_item(items, name, data, folder=folder)
        _pf.write_items(lib, items, self.vault_pub(), master=master, vault_priv_raw=priv)
        # 库里那行仍按 legacy 写一份密文（**不清 content**）。
        # 为什么：`tree_write._sealed_bytes()` 靠 content 列取密文去落盘；
        # 清了它，倒转那步投影就会报「落盘失败」。
        # 「库里不再留第二份密文」属于 C 项（旧密文文件留不留）—— 那要你定，不在这儿顺手做。
        row2 = self.conn.execute("SELECT enc_algo, key_ref FROM files WHERE id=?",
                                 (file_id,)).fetchone()
        if row2 and row2["enc_algo"]:
            key = vaultcrypto.open_key_ref(master, row2["key_ref"], priv)
            self._write_blob(file_id, vaultcrypto.seal(key, data), sha256_bytes(data), len(data),
                             enc_algo=row2["enc_algo"], key_ref=row2["key_ref"])

    def _write_blob(self, file_id: str, stored: bytes, plain_hash: str, plain_len: int,
                    enc_algo: str | None, key_ref: str | None, visibility: str | None = None) -> None:
        """落盘：内联或池"""
        thr = int(self.manifest().get("pool_threshold") or 0)
        pool_hash = sha256_bytes(stored) if enc_algo else plain_hash
        now = local_iso()
        if thr and len(stored) > thr:
            ref = self._pool_put(pool_hash, stored)
            self.conn.execute(
                "UPDATE files SET storage='pool', storage_ref=?, content=NULL, content_hash=?, size=?,"
                " mtime=?, enc_algo=?, key_ref=? WHERE id=?",
                (ref, plain_hash, plain_len, now, enc_algo, key_ref, file_id))
        else:
            self.conn.execute(
                "UPDATE files SET storage='inline', storage_ref=NULL, content=?, content_hash=?, size=?,"
                " mtime=?, enc_algo=?, key_ref=? WHERE id=?",
                (stored, plain_hash, plain_len, now, enc_algo, key_ref, file_id))
        if visibility:
            self.conn.execute("UPDATE files SET visibility=? WHERE id=?", (visibility, file_id))
        self.conn.commit()

    def set_visibility(self, file_id: str, visibility: str, master: bytes | None = None) -> dict:
        """切可见性；转 private 加密内容，转回解密"""
        row = self.conn.execute(
            "SELECT id, visibility, enc_algo, key_ref FROM files WHERE id=?", (file_id,)).fetchone()
        if row is None:
            raise KeyError("no such file: %s" % file_id)
        if visibility not in ("public", "internal", "private"):
            raise ValueError("bad visibility: %s" % visibility)
        plain = self.get_content(file_id, master)
        # 移出私密（→ internal/public）时，包内那条要**跟着删**（2026-10-15 修）。
        # 原来只管库这边（解密 + 清 enc_algo），包里那条留着 → 幽灵：明明不是私密了，
        # 包里还有它的正文；而且下次谁把它算进"包里有什么"，就会多出一篇。
        # 判据要在 set_visibility 之前取（之后 enc_algo 会被清掉，路由判据就不成立了）。
        _was_in_pack = (visibility != "private"
                        and self.priv_effective_mode() == "package"
                        and self.priv_should_route_to_pack(file_id, row))
        if visibility == "private":
            if row["enc_algo"] == vaultcrypto.ALGO:
                self.conn.execute("UPDATE files SET visibility='private' WHERE id=?", (file_id,))
                self.conn.commit()
                return {"file_id": file_id, "visibility": "private", "already": True}
            key = vaultcrypto.new_key()
            if master:                                   # 解锁态：老写法（主密钥对称包裹）
                key_ref = vaultcrypto.wrap_key(master, key)
                via = "master"
            else:                                        # **锁着也能设为私密**：用库公钥封（2026-10-01）
                pub = self.vault_pub()
                if not pub or self.vault_kdf() is None:
                    # 没设过密码（或密码材料已被清）→ 没有能保护它的钥匙，得先设一个
                    raise vaultcrypto.LockedError(file_id)
                key_ref = vaultcrypto.wrap_box(key, pub)
                via = "pubkey"
            self._write_blob(file_id, vaultcrypto.seal(key, plain), sha256_bytes(plain), len(plain),
                             enc_algo=vaultcrypto.ALGO, key_ref=key_ref,
                             visibility="private")
            out = {"file_id": file_id, "visibility": "private", "encrypted": True,
                   "via": via, "bytes": len(plain)}
            # ⚠️ package 模式：正文还要进包（2026-10-15 修，第 6 处同形状的漏）。
            #    这一支也走 `_write_blob`（直写），不经过包路由 —— 与 create_file 同一个坑。
            #    这正是"拖进私密文件夹"的**第二步**（先移动、再设为私密），
            #    所以它一漏，UI 上把一篇拖进私密文件夹后就再也读不出来。
            #    判据此刻已经成立（行已更新为 private + 位置已在私密文件夹）——直写之后再补写包即可。
            if self.priv_effective_mode() == "package" and self.priv_should_route_to_pack(file_id):
                if master:
                    out["to_pack"] = self._priv_write_to_pack_safe(file_id, plain, master)
                else:
                    # 锁着：不是"写包失败"，是"还没到能写的那一步"。
                    # 别打误导性的失败日志（调用方 move_in 会给出更准的说明）。
                    out["to_pack"] = False
                    out["pack_note"] = "库锁着，未写包"
            return out
        self._write_blob(file_id, plain, sha256_bytes(plain), len(plain),
                         enc_algo=None, key_ref=None, visibility=visibility)
        out = {"file_id": file_id, "visibility": visibility, "decrypted": True,
               "bytes": len(plain)}
        if _was_in_pack:
            out["pack_dropped"] = self._pack_drop_current(file_id, master)
        return out

    def rekey_private(self, file_id: str, data: bytes, master: bytes) -> dict:
        """把一篇私密篇**按当前主密钥重新加密**（换过密码、旧密文解不开时用）。

        为什么不能直接用 put_content：那条路要先 `unwrap_key(master, 旧 key_ref)` ——
        换了密码之后旧 key_ref 解不开，会 BadPassword。这里照 set_visibility 的写法：
        **新生成一把内容密钥**、seal、再用当前主密钥 wrap。
        """
        if not master:
            raise vaultcrypto.LockedError(file_id)
        key = vaultcrypto.new_key()
        self._write_blob(file_id, vaultcrypto.seal(key, data), sha256_bytes(data), len(data),
                         enc_algo=vaultcrypto.ALGO, key_ref=vaultcrypto.wrap_key(master, key),
                         visibility="private")
        self._apply_frontmatter(file_id, data)
        return {"file_id": file_id, "bytes": len(data)}

    def private_files(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, name FROM files WHERE visibility='private' ORDER BY name")]

    def file_rows(self, visibility: str = "") -> list:
        """树的文件行（跟 tree() 里同一种形状）——解锁时把露出来的私密篇直接还给前端，
        省得它为了这几篇把整棵树（几百 KB）重拉一遍。"""
        sql = ("SELECT id,folder_id,name,ext,kind,size,visibility,mtime,doc_date,rag FROM files")
        args = ()
        if visibility:
            sql += " WHERE IFNULL(visibility,'')=?"
            args = (visibility,)
        return [dict(r) for r in self.conn.execute(sql, args)]

    def purge_sensitive_chunks(self, file_id: str = "") -> int:
        """清掉 private 文件的派生层（含文档解析缓存 —— 那是明文，不能留在库里）。

        给了 file_id 就只清那一篇（锁着设为私密时用：那一刻库里没人能重解析，只能先清干净）。
        """
        if file_id:
            rows = self.conn.execute("SELECT id FROM files WHERE id=?", (file_id,)).fetchall()
        else:
            rows = self.conn.execute("SELECT id FROM files WHERE visibility='private'").fetchall()
        n = 0
        for r in rows:
            self.delete_chunks(r["id"])
            self.drop_derived(r["id"])
            n += 1
        return n

    def _pool_put(self, content_hash: str, data: bytes) -> str:
        """池写入：<前两位>/<hash>；临时文件 + 原子 rename（FORMAT §5.3）"""
        hexpart = content_hash.split(":", 1)[-1]
        rel = "%s/%s" % (hexpart[:2], hexpart)
        p = self.data_dir / "pool" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file():
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
        return rel

    # ── 文件夹 ────────────────────────────────────────────────
    def vault_kdf(self) -> dict | None:
        man = self.manifest()
        if not man.get("kdf_salt"):
            return None
        return {"algo": man.get("kdf_algo") or vaultcrypto.KDF_ALGO,
                "salt": man["kdf_salt"],
                "n": int(man.get("kdf_n") or vaultcrypto.KDF_N),
                "r": int(man.get("kdf_r") or vaultcrypto.KDF_R),
                "p": int(man.get("kdf_p") or vaultcrypto.KDF_P)}

    def set_vault_password(self, master: bytes, salt: str | None = None,
                           n: int | None = None, r: int | None = None,
                           p: int | None = None) -> dict:
        """首次设密码 / 换密码：写 KDF 参数与验证块，并**确保有一对库密钥**。

        库密钥 = X25519 对：公钥明文（只能加密 → 锁着也能设为私密），私钥用主密钥包着（要密码才能解）。
        换密码时重新生成一对：换密码那条路会先用旧主密钥把每篇私密篇解出来、再用新主密钥对称重封，
        旧的公钥封存引用不会留下，所以换一对是安全的（旧的一对那时已经没人用了）。

        `salt` 与 `n/r/p` 都可以显式给（**必须与你派生 master 时用的那套一致**）；
        不给则用模块常量。
        """
        salt = salt or vaultcrypto.new_salt()
        # n/r/p 也接受自定义（2026-10-15）：原来只开 salt、把 n/r/p 钉死成模块常量 ——
        # 调用方若用别的 n 派生 master，写进去的参数就对不上，重派生出的主密钥开不了库。
        # 默认仍取模块常量 → **现有行为一字不变**。
        n = int(n) if n else vaultcrypto.KDF_N
        r = int(r) if r else vaultcrypto.KDF_R
        p = int(p) if p else vaultcrypto.KDF_P
        pub, priv = vaultcrypto.new_box_keypair()
        self.set_manifest(kdf_algo=vaultcrypto.KDF_ALGO, kdf_salt=salt,
                          kdf_n=n, kdf_r=r, kdf_p=p,
                          verify_blob=vaultcrypto.verify_blob(master),
                          vault_pub=pub, vault_priv_ref=vaultcrypto.wrap_key(master, priv))
        return self.vault_kdf()

    def vault_pub(self) -> str:
        """库公钥（明文，只能加密）—— 有它就能「锁着也设为私密」"""
        return self.manifest().get("vault_pub") or ""

    def vault_priv(self, master: bytes):
        """库私钥（要用主密钥解出来）；没解锁就回 None"""
        if not master:
            return None
        ref = self.manifest().get("vault_priv_ref") or ""
        if not ref:
            return None
        try:
            return vaultcrypto.unwrap_key(master, ref)
        except Exception:
            return None

    # ── 私密文件夹模式（2026-10-15）────────────────────────────
    # 你定的形态：`个人\私密\` = 一个真实目录 + 一个单独的加密包 `.privpack`。
    #
    # **默认 legacy = 与今天逐字节一致的行为** —— 这一段先进库、但不改任何行为。
    # 为什么要有开关：接线当天就动 8 篇真数据太危险；先让代码就位，
    # 再在"两种模式读同一篇结果相同"验证过之后，才把开关翻到 package。
    #
    # legacy：私密正文 = 库里一篇一份密文（files.content / 池）  ← 今天
    # package：私密正文 = 私密文件夹里的 .privpack（整包加密）
    def priv_mode(self) -> str:
        """私密存储模式：`legacy`（默认）｜`package`。manifest 里 `priv_mode` 覆盖。"""
        try:
            v = str((self.manifest() or {}).get("priv_mode") or "").strip().lower()
        except Exception:
            return "legacy"
        return v if v in ("legacy", "package") else "legacy"

    def priv_package_ready(self) -> bool:
        """package 模式能不能启用（缺一不可）。

        三个条件缺一个都会让 8 篇**解不开**，所以宁可退回 legacy：
          · manifest 里有 `vault_pub`（建包/封包密钥要用）
          · 配了 `library_dir`（包要落在那儿）
          · 盘上真有那个包文件
        """
        if not self.vault_pub():
            return False
        if not self._lib_root():
            return False
        try:
            import priv_folder as _pf
            return bool(_pf.load_pack(self._lib_root())[0])
        except Exception:
            return False

    def priv_effective_mode(self) -> str:
        """实际生效的模式：请求了 package 但条件不够 → 落到 legacy 并留痕（不静默变行为）。"""
        want = self.priv_mode()
        if want != "package":
            return "legacy"
        if self.priv_package_ready():
            return "package"
        self.note_priv_fallback("请求了 package 但条件不足（无公钥/无库树/无包文件）→ 退回 legacy")
        return "legacy"

    def note_priv_fallback(self, why: str) -> None:
        """模式回退留痕（与 _note_tree_error 同一套路：不抛，但要能查到）"""
        try:
            globals().setdefault("_PRIV_FALLBACKS", []).append(
                {"at": local_iso(), "why": why})
            print("[priv_mode] %s" % why, flush=True)
        except Exception:
            pass

    def priv_fallbacks(self) -> list:
        """本次进程里模式回退的记录（自检用）"""
        return list(globals().get("_PRIV_FALLBACKS") or [])

    def priv_should_route_to_pack(self, file_id: str, row=None) -> bool:
        """这一篇该不该走加密包。

        判据（**全部满足才走**）：
          · 实际模式是 package
          · 这一篇是私密篇（`enc_algo` 非空）—— 加密封装在库里，包只换"正文存哪"
          · 它在**私密文件夹里**（位置判据）—— 你定的第①条
        """
        if self.priv_effective_mode() != "package":
            return False
        try:
            r = row if row is not None else self.conn.execute(
                "SELECT enc_algo, name, folder_id FROM files WHERE id=?", (file_id,)).fetchone()
            if r is None or not r["enc_algo"]:
                return False
            rel = self.disk_rel_of(file_id)
            if not rel:
                return False
            import priv_folder as _pf
            return _pf.is_private_location(rel)
        except Exception:
            return False

    def root_folder_id(self) -> str:
        row = self.conn.execute("SELECT id FROM folders WHERE IFNULL(parent_id,'')='' AND name=''").fetchone()
        if row:
            return row["id"]
        fid = str(uuid.uuid4())
        self.conn.execute("INSERT INTO folders(id,parent_id,name,sort,created_at) VALUES(?,NULL,'',0,?)",
                          (fid, local_iso()))
        self.conn.commit()
        return fid

    def ensure_folder_path(self, rel_dir: str) -> str:
        """按 "a/b/c" 逐级确保文件夹存在（从根目录挂起），返回最深层的 folder_id"""
        parent = self.root_folder_id()
        for part in [p for p in str(rel_dir).replace("\\", "/").split("/") if p]:
            row = self.conn.execute(
                "SELECT id FROM folders WHERE name=? AND IFNULL(parent_id,'')=IFNULL(?,'')",
                (part, parent)).fetchone()
            if row:
                parent = row["id"]
            else:
                fid = str(uuid.uuid4())
                self.conn.execute("INSERT INTO folders(id,parent_id,name,sort,created_at) VALUES(?,?,?,?,?)",
                                  (fid, parent, part, 0, local_iso()))
                parent = fid
        self.conn.commit()
        return parent

    # ── 文件 ──────────────────────────────────────────────────
    def find_file(self, folder_id: str, name: str):
        return self.conn.execute("SELECT * FROM files WHERE folder_id=? AND name=?", (folder_id, name)).fetchone()

    @staticmethod
    def parse_doc_date(*parts: str) -> str:
        """从路径片段里认日期：2026-08-12 / 2026_08_12 / 2026.08.12 / 20260812；
        也认按层拆开的目录（日报/2026/09/14.md）。只认 2000~2099，免得把版本号当日期。
        """
        import re as _re
        for p in parts:
            for m in _re.finditer(r"(20\d{2})[-_./]?(\d{2})[-_./]?(\d{2})", str(p or "")):
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 1 <= mo <= 12 and 1 <= d <= 31:
                    return "%04d-%02d-%02d" % (y, mo, d)
        nums = []
        for p in parts:
            for seg in str(p or "").split("/"):
                if seg.isdigit() and len(seg) in (4, 2):
                    nums.append(seg)
        if len(nums) >= 3 and len(nums[0]) == 4:
            y, mo, d = nums[0], int(nums[1]), int(nums[2])
            if 1 <= mo <= 12 and 1 <= d <= 31:
                return "%s-%02d-%02d" % (y, mo, d)
        return ""

    def derive_doc_date(self, folder_id: str, name: str) -> str:
        return self.parse_doc_date(self.folder_path(folder_id) if folder_id else "", name)

    def refresh_doc_dates(self, folder_id: str) -> list:
        """文件夹改名/移动后，重算其下所有文件的 doc_date；返回日期变了的文件 id"""
        ids = [r["id"] for r in self.conn.execute(
            "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL "
            "SELECT f.id FROM folders f JOIN sub ON f.parent_id=sub.id) "
            "SELECT id FROM sub", (folder_id,))]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        changed = []
        for r in self.conn.execute(
                "SELECT id, folder_id, name, IFNULL(doc_date,'') AS d FROM files"
                " WHERE folder_id IN (%s)" % ph, ids).fetchall():
            nd = self.derive_doc_date(r["folder_id"], r["name"])
            if nd != r["d"]:
                self.conn.execute("UPDATE files SET doc_date=? WHERE id=?", (nd, r["id"]))
                changed.append(r["id"])
        if changed:
            self.conn.commit()
        return changed

    def kind_for(self, name: str, ext: str, data: bytes | None = None) -> str:
        """判"该怎么对待这个文件"：**先看扩展名，扩展名给不出答案就看内容**。

        markdown / text / code / document（要解析的文档）/ image（图片）/ asset（其它二进制）。
        辞玖 2026-09-25：库按文件管理系统来，**什么文件都能进**；能不能看/改/检索由 caps_for 划分。

        **补（2026-10-03）**：原来只按扩展名判 —— 于是**没有扩展名的笔记**（库内新建时可以不起名后缀，
        辞玖那篇《手搓小夜模型-完整过程记录》就是）落到兜底 `asset` → 被判成"二进制、不能看不能改"，
        正文却明明是 Markdown。同类的 `rag_default()` 早就在按内容嗅探，这里跟上：
        **扩展名缺失或不在清单里 → 嗅探内容**；能当文本就当文本，否则仍是 asset。
        （辞玖同日定：**不做任何自动格式转换**，转换保持手动。）
        """
        e = (ext or "").lower().lstrip(".")
        if e in IMAGE_EXTS:
            return "image"
        try:
            import doctext
            if doctext.needs_parse(e):        # 只有"必须解析"的才算 document
                return "document"
        except Exception:
            pass
        if e in KNOWN_TEXT_EXTS:
            return self._kind_plain(name, ext)
        if e in ("json", "yaml", "yml", "toml", "ini", "cfg", "sql", "sh", "ps1", "css", "html"):
            return "code"
        sniffed = self._sniff_text(data)      # 扩展名给不出答案 → 看内容
        if sniffed:
            return sniffed
        return "asset"                        # 未知二进制：只存放（不能看/改/检索）

    @staticmethod
    def _sniff_text(data: bytes | None) -> str:
        """像文本就回 markdown / text，不像就回 ""（空）。

        判据保守（宁可判成二进制，也别把乱码塞进检索）：头 8KB **没有 NUL** 且能按 UTF-8 解码。
        像 Markdown 的（有 ATX 标题 / 围栏代码块 / 列表项）算 markdown，其余算 text。
        """
        b = (data or b"")[:8192]
        if not b or b"\x00" in b:
            return ""
        try:
            t = b.decode("utf-8")
        except UnicodeDecodeError:
            return ""
        # 光"没 NUL"不够：zip 的头 `PK\x03\x04` 既没 NUL 又能 UTF-8 解码（自测抓到的假文本）。
        # 再加一条：**不许有控制字符**（制表/换行/回车除外）——宁可判成二进制，也别把二进制当文本。
        if any(ord(c) < 32 and c not in "\t\n\r" for c in t):
            return ""
        import re as _re
        if _re.search(r"^#{1,6} \S", t, _re.M) or "```" in t or _re.search(r"^\s*[-*+] \S", t, _re.M):
            return "markdown"
        return "text"

    def _kind_plain(self, name: str, ext: str) -> str:
        e = (ext or "").lower()
        if e in ("md", "markdown"):
            return "markdown"
        if e in ("txt", "text", "log"):
            return "text"
        return "code"

    def rag_of(self, file_id: str) -> str:
        r = self.conn.execute("SELECT IFNULL(rag,'on') FROM files WHERE id=?", (file_id,)).fetchone()
        return r[0] if r else ""

    def set_rag_many(self, ids: list, on: bool) -> int:
        """批量进/出检索库（只改标记；切块与向量由 indexer 跟着做）"""
        v = "on" if on else "off"
        n = 0
        for fid in ids:
            n += self.conn.execute("UPDATE files SET rag=? WHERE id=? AND IFNULL(rag,'')<>?",
                                   (v, fid, v)).rowcount
        self.conn.commit()
        return n

    def list_rag(self, on: bool | None = None, limit: int = 1000,
                 hide_private: bool = False) -> list:
        """列出在/不在检索库的文件（带库内路径，给界面与 agent 挑选用）。

        hide_private=True（上锁）：私密篇不列 —— 名字本身也是秘密（2026-09-27）。
        """
        sql = "SELECT id, folder_id, name, IFNULL(rag,'on') AS rag, kind, size FROM files"
        args = []
        conds = []
        if on is not None:
            conds.append("IFNULL(rag,'on')=?")
            args.append("on" if on else "off")
        if hide_private:
            conds.append("IFNULL(visibility,'')<>'private'")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY name LIMIT ?"
        args.append(int(limit))
        cache = {}
        out = []
        for r in self.conn.execute(sql, args):
            fid = r["folder_id"]
            if fid not in cache:
                cache[fid] = self.folder_path(fid) or ""
            d = cache[fid]
            out.append({"id": r["id"], "name": r["name"], "dir": d,
                        "path": (d + "/" + r["name"]).lstrip("/"),
                        "rag": r["rag"], "kind": r["kind"], "size": r["size"]})
        return out

    def folder_file_ids(self, folder_id: str, recursive: bool = True) -> list:
        """某文件夹下的文件 id（recursive=True 含子文件夹）"""
        if recursive:
            sql = ("WITH RECURSIVE t(id) AS (SELECT ? UNION ALL "
                   "SELECT f.id FROM folders f JOIN t ON f.parent_id=t.id) "
                   "SELECT id FROM files WHERE folder_id IN (SELECT id FROM t)")
            return [r[0] for r in self.conn.execute(sql, (folder_id,))]
        return [r[0] for r in self.conn.execute("SELECT id FROM files WHERE folder_id=?", (folder_id,))]

    @staticmethod
    def rag_default(ext: str, data: bytes) -> str:
        """默认进不进检索库：文档格式与二进制**默认不进**（要看/编辑就手动加入）

        理由（2026-09-18 辞玖定）：这类文件进来先只当"文件"存放，解析器/编辑器随取随用；
        不手动加入就永不解析成文本、不切块、不嵌入 —— 顺带把"没有解析器的二进制被硬解成乱码
        混进检索"这条路彻底堵死。
        """
        try:
            import doctext
            if doctext.needs_parse(ext):
                return "off"
        except Exception:
            pass
        return "off" if b"\x00" in (data or b"")[:8192] else "on"

    def set_rag(self, file_id: str, on: bool) -> str:
        v = "on" if on else "off"
        self.conn.execute("UPDATE files SET rag=? WHERE id=?", (v, file_id))
        self.conn.commit()
        return v

    def create_file(self, folder_id: str, name: str, data: bytes, kind: str | None = None,
                    ext: str | None = None, visibility: str = "internal",
                    master: bytes | None = None) -> str:
        """建文件：先写元数据行，再落内容。

        注意：内容一律经 put_content / _write_blob（FORMAT 5.1）——否则池阈值形同虚设。
        （初版直接写 content 列，冒烟测试第 6 步抓到 storage 恒为 inline）

        visibility='private' 必须同时给 master：**边建边加密**。
        为什么不复用"先明文入库再 set_visibility"：那一瞬明文会落进 index.db 和 WAL，
        密码/密钥这种内容不能这么走（2026-09-22 加）。
        """
        name = check_name(name, "文件名")
        if visibility == "private" and not master:
            # 先判后写：没有主密钥就别建行（否则会留下一行"私密标记 + 明文内容"的半成品）
            raise vaultcrypto.LockedError("<new:%s>" % name)
        fid = str(uuid.uuid4())
        if ext is None:
            ext = (name.rsplit(".", 1)[1].lower() if "." in name else "")
        if kind is None:
            kind = self.kind_for(name, ext, data)      # 扩展名给不出答案时按内容嗅探（2026-10-03）
        now = local_iso()
        self.conn.execute(
            "INSERT INTO files(id,folder_id,name,ext,kind,size,content_hash,mtime,created_at,"
            "visibility,storage,doc_date,rag) VALUES(?,?,?,?,?,?,?,?,?,?,'inline',?,?)",
            (fid, folder_id, name, ext, kind, len(data), sha256_bytes(data), now, now, visibility,
             self.derive_doc_date(folder_id, name), self.rag_default(ext, data)))
        self.conn.commit()
        if visibility == "private":
            key = vaultcrypto.new_key()                 # 每个文件一把内容密钥，用库主密钥包起来
            self._write_blob(fid, vaultcrypto.seal(key, data), sha256_bytes(data), len(data),
                             enc_algo=vaultcrypto.ALGO, key_ref=vaultcrypto.wrap_key(master, key))
            self._apply_frontmatter(fid, data)      # 私密笔记也读前置属性（标签是元数据，不是正文）
            # ⚠️ package 模式：正文还要进包（2026-10-15 修）。
            #    这一支走的是 `_write_blob`（**直写**，不走 put_content），所以不走包路由 ——
            #    后果：在私密文件夹里新建一篇，正文落在包**外**，而读的时候（package 模式）
            #    只找包 → "包里没有这一篇"。这是最自然的用法，却坏得彻底。
            #    这里不能再"先建行再 move_in"（那会绕一圈），直接在原地补一次写包。
            if self.priv_effective_mode() == "package" and self.priv_should_route_to_pack(fid):
                if not master:
                    # 与上面那条同理：没有主密钥就不能留下"私密标记 + 正文不在包里"的半成品
                    raise vaultcrypto.LockedError("<new:%s>" % name)
                self._priv_write_to_pack(fid, data, master)
            return fid
        self.put_content(fid, data)     # 唯一内容入口（inline / pool 由它决定）
        return fid

    # ── 树 / 读 / 写 / 建（API 层用）────────────────────────────
    def folder_by_id(self, folder_id: str):
        return self.conn.execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()

    def file_by_id(self, file_id: str):
        return self.conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()

    def list_folders(self, parent_id=None) -> list:
        if parent_id is None:
            rows = self.conn.execute("SELECT * FROM folders WHERE IFNULL(parent_id,'')='' ORDER BY name")
        else:
            rows = self.conn.execute("SELECT * FROM folders WHERE parent_id=? ORDER BY name", (parent_id,))
        return [dict(r) for r in rows]

    def list_files(self, folder_id: str) -> list:
        rows = self.conn.execute(
            "SELECT id,name,ext,kind,size,visibility,storage,mtime,doc_date,rag"
            " FROM files WHERE folder_id=? ORDER BY name",
            (folder_id,))
        return [dict(r) for r in rows]

    def tree(self, hide_private: bool = False, keep_priv_dir: bool = False) -> dict:
        """文件夹树（含每层文件；不返回 content，避免大树一次拉爆）。

        hide_private=True（库上锁 / 没解锁）：私密篇**连名字都不给**（辞玖 2026-09-27：
        "上锁直接隐藏上锁文件，解锁状态显示"）；隐藏了几篇用 hidden_private 如实报出来。

        keep_priv_dir=True（2026-10-15，私密文件夹形态）：锁定态**保留「私密」文件夹节点本身**，
        只把里面的内容收掉。理由是私密文件夹是"加密容器"——容器本身要看得见（否则用户
        根本不知道往哪拖文件），而它装了什么一律不给。默认 False = 保持原有行为不变。
        """
        folders = [dict(r) for r in self.conn.execute("SELECT * FROM folders")]
        files = [dict(r) for r in self.conn.execute(
            "SELECT id,folder_id,name,ext,kind,size,visibility,mtime,doc_date,rag"
            " FROM files")]
        hidden = 0
        vis_sub = {}                           # 锁定时：folder_id → 这一枝（含子孙）里可见/私密文件数
        priv_sub = {}
        if hide_private:
            keep, privs = [], []
            for f in files:
                if (f.get("visibility") or "") == "private":
                    hidden += 1
                    privs.append(f)
                else:
                    keep.append(f)
            files = keep
            kids_of = {}
            for fo in folders:
                kids_of.setdefault(fo["parent_id"] or "", []).append(fo["id"])

            def subtree(fid, src, memo):
                if fid in memo:
                    return memo[fid]
                n = src.get(fid, 0)
                for k in kids_of.get(fid, []):
                    n += subtree(k, src, memo)
                memo[fid] = n
                return n

            src_vis, src_priv = {}, {}
            for f in files:
                src_vis[f["folder_id"] or ""] = src_vis.get(f["folder_id"] or "", 0) + 1
            for f in privs:
                src_priv[f["folder_id"] or ""] = src_priv.get(f["folder_id"] or "", 0) + 1
            memo_v, memo_p = {}, {}
            for fo in folders:
                vis_sub[fo["id"]] = subtree(fo["id"], src_vis, memo_v)
                priv_sub[fo["id"]] = subtree(fo["id"], src_priv, memo_p)
        for f in files:                        # 引用时用的短名（扩展名可省），界面直接用它
            f["disp"] = ref_display(f["name"])
            f["caps"] = caps_for(f.get("kind"), f.get("ext"), f.get("name"))
        by_parent = {}
        for f in folders:
            by_parent.setdefault(f["parent_id"] or "", []).append(f)
        files_in = {}
        for f in files:
            files_in.setdefault(f["folder_id"], []).append(f)

        def build(fid, name):
            return {
                "id": fid, "name": name, "type": "dir",
                "children": [build(c["id"], c["name"]) for c in sorted(by_parent.get(fid, []), key=lambda x: x["name"])],
                "files": sorted(files_in.get(fid, []), key=lambda x: x["name"]),
            }

        roots = self.list_folders()
        if len(roots) == 1 and roots[0]["name"] == "":
            out = build(roots[0]["id"], "")           # 单根：直接展开
        else:
            out = {"id": "", "name": "", "type": "dir",
                   "children": [build(r["id"], r["name"]) for r in roots],
                   "files": sorted(files_in.get("", []), key=lambda x: x["name"])}
        if hidden:
            # 辞玖 2026-09-29："锁定状态的文件和文件夹不显示；空内容（未锁定）照常显示"。
            # 规则：这一枝里**可见文件数为 0**、而它本来装着私密内容 → 整枝收掉（连文件夹名都不给）。
            # 关键：判断依据是"可见**文件**数"，不是"渲染完还剩没剩子节点" ——
            # 否则 私密/空草稿 这种（私密篇 + 一个空子文件夹）会把「私密」这名字露出来。
            # 本来就空的文件夹（可见 0 / 私密 0）照常显示。
            # 私密文件夹的 id（位置判据：个人/私密）—— keep_priv_dir 时这一枝要留住
            _priv_ids = set()
            if keep_priv_dir:
                try:
                    import priv_folder as _pf
                    _pidir = _pf.PRIV_DIR_REL.split("/")
                    if len(_pidir) >= 2:
                        for _r in self.conn.execute(
                                "SELECT c.id FROM folders c JOIN folders p ON p.id=c.parent_id"
                                " WHERE c.name=? AND p.name=?", (_pidir[-1], _pidir[-2])):
                            _priv_ids.add(_r["id"])
                except Exception:
                    _priv_ids = set()

            def prune(n):
                n["children"] = [c for c in (prune(x) for x in (n.get("children") or []))
                                 if c is not None]
                if vis_sub.get(n["id"], -1) == 0 and priv_sub.get(n["id"], 0) > 0:
                    if n["id"] in _priv_ids:
                        # 私密文件夹本身：留住节点，但里面已经空了（子节点被上面那行收掉、文件本就被剔）
                        n["files"] = []
                        n["keep"] = True          # 给界面/测试一个可判读的标记
                        return n
                    return None
                return n
            pruned = prune(out)
            if pruned is None:                 # 整棵树只剩私密（连根都空了）→ 给个空的
                pruned = {"id": out.get("id", ""), "name": out.get("name", ""), "type": "dir",
                          "children": [], "files": []}
            out = pruned
        if hidden:
            out["hidden_private"] = hidden
        return out

    def create_folder(self, parent_id: str, name: str) -> str:
        name = check_name(name, "文件夹名")
        fid = str(uuid.uuid4())
        self.conn.execute("INSERT INTO folders(id,parent_id,name,sort,created_at) VALUES(?,?,?,?,?)",
                          (fid, parent_id, name, 0, local_iso()))
        self.conn.commit()
        return fid

    def folder_path(self, folder_id: str) -> str:
        """根目录到该文件夹的路径，如 vault/项目（根返回 ""）"""
        parts = []
        cur = folder_id
        for _ in range(64):                      # 防环
            row = self.conn.execute("SELECT id,name,parent_id FROM folders WHERE id=?", (cur,)).fetchone()
            if row is None:
                break
            if row["name"]:
                parts.append(row["name"])
            cur = row["parent_id"]
            if not cur:
                break
        return "/".join(reversed(parts))

    def folder_by_path(self, path: str) -> str | None:
        """把 vault/项目 这样的路径解析成 folder_id（找不到返回 None）"""
        cur = self.root_folder_id()
        for part in [x for x in str(path or "").replace("\\", "/").split("/") if x]:
            row = self.conn.execute(
                "SELECT id FROM folders WHERE parent_id=? AND name=?", (cur, part)).fetchone()
            if row is None:
                return None
            cur = row["id"]
        return cur

    def folder_children_count(self, folder_id: str) -> int:
        a = self.conn.execute("SELECT COUNT(*) c FROM folders WHERE parent_id=?", (folder_id,)).fetchone()["c"]
        b = self.conn.execute("SELECT COUNT(*) c FROM files WHERE folder_id=?", (folder_id,)).fetchone()["c"]
        return int(a) + int(b)

    def rename_folder(self, folder_id: str, new_name: str) -> None:
        new_name = check_name(new_name, "文件夹名")
        self.conn.execute("UPDATE folders SET name=? WHERE id=?", (new_name, folder_id))
        self.conn.commit()

    def delete_folder(self, folder_id: str) -> None:
        """只删空文件夹：里面还有东西就让上层报错（避免一次误操作连带删掉一堆笔记）"""
        if folder_id == self.root_folder_id():
            raise ValueError("根目录不能删")
        if self.folder_children_count(folder_id):
            raise ValueError("文件夹非空（先移走或删掉里面的内容）")
        self.conn.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        self.conn.commit()

    def move_folder(self, folder_id: str, new_parent_id: str, new_name: str | None = None,
                    master=None) -> dict:
        """搬一个文件夹（连带整棵子树），并把**引用处的路径式锚点**改成新路径。

        与 `move_file` 同一套路数：`folder_path()` 是按 parent_id 现算的，所以改一行
        `parent_id` 整棵子树的库内路径当场就变了 —— 磁盘那份由调用方跟着搬
        （`tools/migrate_tree.py` 负责）。

        返回 {old_rel,new_rel,refs}；`old_rel` 为空 = 拒绝执行（原因在 `why`）。
        """
        src = self.folder_by_id(folder_id)
        if src is None:
            return {"ok": False, "why": "no-folder", "old_rel": ""}
        if folder_id == self.root_folder_id() or not (src["name"] or ""):
            return {"ok": False, "why": "root", "old_rel": ""}
        tgt = self.folder_by_id(new_parent_id)
        if tgt is None:
            return {"ok": False, "why": "no-target", "old_rel": ""}
        # 防「搬进自己的子孙里」：从新父往上爬，撞见自己就拒绝
        cur, guard = new_parent_id, 0
        while cur and guard < 200:
            if cur == folder_id:
                return {"ok": False, "why": "into-self", "old_rel": ""}
            p = self.folder_by_id(cur)
            cur = p["parent_id"] if p else None
            guard += 1
        old_rel = self.folder_path(folder_id) or ""      # 必须在改 parent_id **之前**取
        if not old_rel:
            return {"ok": False, "why": "no-rel", "old_rel": ""}
        nid = [x for x in self.folder_file_ids(folder_id)]
        new_name = (new_name or src["name"])
        self.conn.execute("UPDATE folders SET parent_id=?, name=? WHERE id=?",
                          (new_parent_id, new_name, folder_id))
        self.conn.commit()
        refs = self.refresh_path_refs(nid, master=master) if nid else {}
        return {"ok": True, "old_rel": old_rel,
                "new_rel": self.folder_path(folder_id) or "", "refs": refs, "files": len(nid)}

    def display_path(self, file_id) -> str:
        """用户视角的引用路径：去 `vault/` 前缀与 `.md`（`知识库/01-编程通识/08-调试方法论`）"""
        row = file_id if isinstance(file_id, dict) else self.file_by_id(file_id)
        if row is None:
            return ""
        p = self.folder_path(row["folder_id"]) if row["folder_id"] else ""
        p = p.split("vault/", 1)[-1] if "vault/" in p else p
        nm = row["name"] or ""
        disp = ref_display(nm)
        return (p.strip("/") + "/" + disp).strip("/")

    def ref_resolver(self):
        """`@锚点` → file_id 的解析器：**路径优先**，其次唯一显示名（重名一律不解析）

        对每条路径建"整条命中"和"所有后缀命中（唯一的才算）"两张表，于是
        `@日志/reasonix-raw/2026-07-08/会话` 与 `@2026-07-08/会话` 都能指到同一篇；
        `@会话` 这种重名（库里 16 篇）继续解析不出来 —— 这正是要路径的原因。
        """
        by_name, by_path, by_suffix = {}, {}, {}
        dup_name, dup_path, dup_suffix = set(), set(), set()
        cache = {}

        def folder_path_cached(fid):
            if fid not in cache:
                cache[fid] = self.folder_path(fid) if fid else ""
            return cache[fid]

        for r in self.conn.execute("SELECT id, name, folder_id FROM files"):
            nm = r["name"] or ""
            disp = ref_display(nm)
            if disp:
                if disp in by_name:
                    dup_name.add(disp)
                else:
                    by_name[disp] = r["id"]
            p = folder_path_cached(r["folder_id"])
            p = p.split("vault/", 1)[-1] if "vault/" in p else p
            path = (p.strip("/") + "/" + disp).strip("/")
            if path:
                if path in by_path:
                    dup_path.add(path)
                else:
                    by_path[path] = r["id"]
                seg = path.split("/")
                for i in range(len(seg)):
                    suf = "/".join(seg[i:])
                    if suf in by_suffix:
                        dup_suffix.add(suf)
                    else:
                        by_suffix[suf] = r["id"]
        for k in dup_name:
            by_name.pop(k, None)
        for k in dup_path:
            by_path.pop(k, None)
        for k in dup_suffix:
            by_suffix.pop(k, None)

        def look(token):
            t = ref_display((token or "").strip().strip("/"))
            if not t:
                return None
            if "/" in t:
                return by_path.get(t) or by_suffix.get(t)
            return by_name.get(t) or by_path.get(t) or by_suffix.get(t)

        return look

    def refresh_sections(self, file_id: str) -> dict:
        """目标篇内容改过之后刷新指向它的边的 b_section

        - 小节还在（token 归一后仍前缀匹配）→ 跟随成新的规范名（`P12 决策边界` 改名成
          `P12 决策边界（新）` 时，引用端一个字都不用改）
        - 真没了 → b_section 清空，算 dangling（如实报出来，不硬凑）
        """
        names = self.section_names(file_id)
        rows = self.conn.execute(
            "SELECT rowid, anchor, IFNULL(b_section,'') AS bs FROM edges"
            " WHERE b_file_id=? AND kind='mention' AND anchor LIKE '%#%'", (file_id,)).fetchall()
        upd = dang = 0
        for r in rows:
            _, tok = split_anchor(r["anchor"])
            hit = section_hit(names, tok) if tok else ""
            if hit != r["bs"]:
                self.conn.execute("UPDATE edges SET b_section=? WHERE rowid=?", (hit or None, r["rowid"]))
                upd += 1
                if not hit:
                    dang += 1
        if upd:
            self.conn.commit()
        return {"sections": len(names), "updated": upd, "dangling": dang}

    def relink_section_refs(self, file_id: str, old: str, new: str, master=None) -> dict:
        """把小节引用整体改写：`@本篇#old` → `@本篇#new`（像文件名改名那样跟随）

        用于"小节换了个不相干的名字"这种无法自动判断的情况 —— 由人点名，机器改。
        """
        old_t, new_t = (old or "").strip(), (new or "").strip()
        if not old_t or not new_t:
            raise ValueError("old/new 都要给")
        names = self.section_names(file_id)
        if not section_hit(names, new_t):
            raise ValueError("目标篇里没有这一节：%s" % new_t)
        rows = self.conn.execute(
            "SELECT DISTINCT a_file_id FROM edges WHERE b_file_id=? AND kind='mention'"
            " AND anchor LIKE ?", (file_id, "%#" + old_t)).fetchall()
        rewritten = {}
        for r in rows:
            rid = r["a_file_id"]
            f = self.file_by_id(rid)
            if f is None:
                continue
            if f["visibility"] == "private" and master is None:
                continue
            text = self.get_content(rid, master).decode("utf-8", "replace")
            new_text, n = text, 0
            for e in self.conn.execute(
                    "SELECT anchor FROM edges WHERE b_file_id=? AND kind='mention'"
                    " AND a_file_id=? AND anchor LIKE ?", (file_id, rid, "%#" + old_t)).fetchall():
                a_old = e["anchor"]
                base = a_old.rsplit("#", 1)[0]
                a_new = "%s#%s" % (base, new_t)
                new_text, k = rewrite_at_anchor(new_text, a_old, a_new)
                n += k
            if n:
                self.put_content(rid, new_text.encode("utf-8"), master)
                pairs, _miss = resolve_mentions(new_text, self.ref_resolver(), {})
                self.set_mentions(rid, pairs)
                rewritten[rid] = n
        return {"rewritten": rewritten, "files": len(rewritten),
                "moved": sum(rewritten.values())}

    def subtree_file_ids(self, folder_id: str) -> list:
        """该文件夹（含子目录）下的所有文件 id"""
        ids = [r["id"] for r in self.conn.execute(
            "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL "
            "SELECT f.id FROM folders f JOIN sub ON f.parent_id=sub.id) SELECT id FROM sub",
            (folder_id,))]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        return [r["id"] for r in self.conn.execute(
            "SELECT id FROM files WHERE folder_id IN (%s)" % ph, ids)]

    def refresh_path_refs(self, file_ids: list, master=None) -> dict:
        """改名/移动/所属文件夹改名之后：把引用处的**路径式**锚点改成新路径。

        依据是 edges（谁引用了它、锚点字面量是什么）→ 不需要在改动前先快照。
        名字式锚点（不含 `/`）不受路径变化影响，仍由原来那套"@旧名→@新名"处理。
        """
        ids = [x for x in (file_ids or []) if x]
        if not ids:
            return {"rewritten": {}, "skipped_locked": []}
        want = {fid: self.display_path(fid) for fid in ids}
        ph = ",".join("?" * len(ids))
        rows = self.conn.execute(
            "SELECT a_file_id, b_file_id, anchor FROM edges"
            " WHERE kind='mention' AND b_file_id IN (%s)" % ph, ids).fetchall()
        by_ref = {}
        for e in rows:
            a = e["anchor"] or ""
            if "/" not in a:
                continue                      # 名字式引用与路径无关
            by_ref.setdefault(e["a_file_id"], []).append((e["b_file_id"], a))
        rewritten, skipped = {}, []
        for rid, items in by_ref.items():
            row = self.file_by_id(rid)
            if row is None:
                continue
            if row["visibility"] == "private" and master is None:
                skipped.append(rid)
                continue
            try:
                text = self.get_content(rid, master).decode("utf-8", "replace")
            except Exception:
                skipped.append(rid)
                continue
            new, n = text, 0
            for b_id, anchor in items:
                base, _, sec = anchor.partition("#")
                target = want.get(b_id) or ""
                if not target or base == target:
                    continue
                new, k = rewrite_at_anchor(new, anchor, target + (("#" + sec) if sec else ""))
                n += k
            if n:
                self.put_content(rid, new.encode("utf-8"), master)
                pairs, _miss = resolve_mentions(new, self.ref_resolver(), {})
                self.set_mentions(rid, pairs)
                rewritten[rid] = n
        return {"rewritten": rewritten, "skipped_locked": skipped}

    def move_file(self, file_id: str, folder_id: str) -> dict:
        """移动文件；返回"引用处被改写的文件"（路径式引用要跟着走）"""
        row = self.file_by_id(file_id)
        self.conn.execute("UPDATE files SET folder_id=?, mtime=?, doc_date=? WHERE id=?",
                          (folder_id, local_iso(),
                           self.derive_doc_date(folder_id, row["name"] if row else ""), file_id))
        self.conn.commit()
        return self.refresh_path_refs([file_id])

    def move_in(self, file_id: str, master: bytes | None = None,
                drop_disk: bool = False) -> dict:
        """把一篇**移进私密文件夹**（第③条：拖进去即进加密包）。

        做三件事：
          ① 改 folder_id 到私密文件夹（复用 move_file：它顺手改写路径式引用）
          ② 若还不是私密篇 → 加密（复用 set_visibility：边转边加密，语义与既有"设为私密"一致）
          ③ package 模式下 → 正文写进加密包

        ⚠️ **判据要在改 folder_id 之前取**：`move_file()` 之后 `disk_rel_of()` 立刻就是
           私密文件夹路径，"改完再判该不该写包"会永远为真。这点与设计文档里
           "路由判据用 enc_algo/visibility，不是盘上有没有文件"是同一条教训。

        返回 {"file_id","from","to","encrypted","to_pack","note"}
        """
        row = self.file_by_id(file_id)
        if row is None:
            raise KeyError("no such file: %s" % file_id)

        # 私密文件夹（位置判据：个人/私密）
        #
        # ⚠️ `folder_by_path` 要的是**带库根层**的路径（`folder_path()` 返回 `vault/个人/私密`），
        #    而 `PRIV_DIR_PATH` 是不带那层的。实测传不带层的会返回空 →
        #    接着 `UPDATE folder_id=NULL` → `IntegrityError: NOT NULL`（踩过）。
        target = self.folder_by_path(PRIV_DIR_PATH_ROOTED)
        if not target:
            target = self.ensure_folder_path(PRIV_DIR_PATH_ROOTED)
        if not target:
            # 解析不到就**明确失败**，绝不带着空目标往下走（那会写坏 folder_id）
            raise IOError("找不到也建不出私密文件夹：%s" % PRIV_DIR_PATH_ROOTED)

        # ── 判据：**改之前**取 ─────────────────────────────────
        was_private = bool(row["enc_algo"])
        # ⚠️ 这里**只看模式**，不看 was_private（2026-10-15 修）。
        #    原来写成「(not was_private) and 模式==package」→ 已经加密的私密篇**永远不写包**。
        #    后果远不止新建的篇：库里已有那 8 篇私密篇同样进不了包 —— 而"把现有私密篇
        #    收进包"正是本功能的主场景。_priv_write_to_pack 本身是幂等的（按 name+folder
        #    覆盖），所以每篇私密篇在 package 模式下都可以安全地写一次。
        want_pack = self.priv_effective_mode() == "package"

        # ⚠️ **正文必须在改 folder_id 之前读出来**（2026-10-15 修）。
        #    改完之后这一篇就算"私密文件夹里的"了 → get_content 走包路由去找它 →
        #    而包里还没有 → 「包里没有这一篇」。**先有鸡还是先有蛋**：永远写不进去。
        #    （上面那条注释说的是"路由判据"要在改之前取；正文同理，但更容易漏。）
        pre_data = None
        if want_pack:
            try:
                pre_data = self.get_content(file_id, master)
            except Exception as e:
                note_pre = "读正文失败（%s: %s）" % (type(e).__name__, str(e)[:40])
                pre_data = None
            else:
                note_pre = ""

        frm = row["folder_id"]
        moved = self.move_file(file_id, target)

        note = ""
        # ② 还不是私密篇 → 加密（set_visibility 内部就是"边转边加密"）
        #    注意：set_visibility(→private) **自己会把正文写进包**（2026-10-15 起），
        #    所以这一支不需要下面再写一次 —— 否则同一篇整包重写两遍。
        sv = None
        if not was_private:
            sv = self.set_visibility(file_id, "private", master)
            note = "已加密为私密篇"
            if sv.get("to_pack"):
                note += "；正文已写进加密包"
        else:
            note = "本来就是私密篇，只挪位置"

        # ③ 写进包（用改完之后重新取的正文——此时已经能读到）
        to_pack = bool(sv and sv.get("to_pack"))
        if want_pack and not to_pack:
            # 走到这里 = 本篇不是"刚由 set_visibility 转私密"那条路（本来就是私密篇）
            if not master:
                note += "；但库锁着，**没写进包**（解锁后再移一次，或先解锁）"
            elif pre_data is None:
                note += "；没写进包：%s" % (note_pre or "取正文失败")
            else:
                try:
                    self._priv_write_to_pack(file_id, pre_data, master)
                    to_pack = True
                    note += "；正文已写进加密包"
                except Exception as e:
                    note += "；写包失败：%s: %s" % (type(e).__name__, str(e)[:50])

        # 盘上那份独立密文文件留不留 → **你的 C 项，未定**。默认保留。
        if not drop_disk:
            note += "（盘上那份独立密文文件按默认保留 —— 留不留是 C 项）"

        return {"file_id": file_id, "from": frm, "to": target,
                "encrypted": not was_private, "to_pack": to_pack,
                "refs": (moved or {}).get("rewritten", {}), "note": note}

    def rename_file(self, file_id: str, new_name: str, master: bytes | None = None) -> dict:
        """改名，并把**引用处的正文**里的 @旧名 一起改成 @新名。

        返回 {old, new, rewritten:{file_id: 处数}, skipped_locked:[file_id], cleared_tombstones:n}
        """
        new_name = check_name(new_name, "文件名")
        old = self.file_by_id(file_id)
        ext = new_name.rsplit(".", 1)[1].lower() if "." in new_name else ""
        old_disp = self._display(old["name"]) if old else ""
        new_disp = self._display(new_name)
        # 改名可能把扩展名去掉/换掉 → kind 得重判；扩展名给不出答案时按**内容**嗅探
        # （读回正文失败不算错：那就退回只按扩展名判，别让改名因为嗅探而失败）
        try:
            _kind = self.kind_for(new_name, ext, self.get_content(file_id))
        except Exception:
            _kind = self.kind_for(new_name, ext)
        # ⚠️ 包内改名必须**在库内名字改掉之前**（旧名才能定位到包里那条）——
        #    否则新名在包里找不到，这条就永远留成旧名，改名即失联（2026-10-15 修）。
        _pack_renamed = self._pack_rename_current(file_id, new_name, master)
        self.conn.execute(
            "UPDATE files SET name=?, ext=?, kind=?, mtime=?, doc_date=? WHERE id=?",
            (new_name, ext, _kind, local_iso(),
             self.derive_doc_date(old["folder_id"] if old else "", new_name), file_id))
        self.conn.commit()
        if _pack_renamed:
            try:
                print("[priv_pack] 包内已改名 → %s" % new_name, flush=True)
            except Exception:
                pass
        # 新名字又出现了 → 撤掉同名墓碑（别把活的当成已删除）
        cleared = self.clear_tombstone(new_disp) if new_disp else 0

        rewritten, skipped = {}, []
        if old_disp and old_disp != new_disp:
            for r in self.referrers(file_id):
                n = self.rewrite_mentions(r["id"], old_disp, new_disp)
                if n < 0:
                    skipped.append(r["id"])
                elif n:
                    rewritten[r["id"]] = n
                    # 正文已改成新名 → 按**新正文**重新解析该引用方的全部提及。
                    # （不能盲写 anchor=新名：引用方可能同时用路径式引用同一篇，那样会把路径锚点覆盖掉）
                    try:
                        txt = self.get_content(r["id"]).decode("utf-8", "replace")
                        # 旧边当兜底：引用方可能还写着"旧路径"式引用，先让它继续指向同一篇，
                        # 紧接着 refresh_path_refs 会把路径改成新的
                        old_edges = {m["anchor"]: m["id"] for m in self.mentions(r["id"])}
                        pairs, _miss = resolve_mentions(txt, self.ref_resolver(), old_edges)
                        self.set_mentions(r["id"], pairs)
                    except Exception:
                        pass
            self.conn.commit()
        # 路径式引用（@目录/旧名）也要跟着改名走
        pr = self.refresh_path_refs([file_id])
        for k, v in pr["rewritten"].items():
            rewritten[k] = rewritten.get(k, 0) + v
        skipped += [x for x in pr["skipped_locked"] if x not in skipped]
        return {"old": old_disp, "new": new_disp, "rewritten": rewritten,
                "skipped_locked": skipped, "cleared_tombstones": cleared}

    def delete_file(self, file_id: str) -> None:
        row = self.file_by_id(file_id)
        if row is not None:
            # 先立碑：删完就没地方查名字了。有了它，别人正文里的 @它 才能显示"已删除"
            self.add_tombstone(file_id, row["name"], row["folder_id"])
        self.delete_chunks(file_id)                   # 先清派生层（含 FTS）
        # 边一并清除
        self.conn.execute("DELETE FROM edges WHERE a_file_id=? OR b_file_id=?", (file_id, file_id))
        self.conn.execute("DELETE FROM derived WHERE file_id=?", (file_id,))
        self.conn.execute("DELETE FROM blocks WHERE file_id=?", (file_id,))
        self.conn.execute("DELETE FROM files WHERE id=?", (file_id,))
        self.conn.commit()

    # ── 双链：@ 提及式（FORMAT §6：关系是数据，文本只是它的渲染）──────
    def set_mentions(self, a_file_id: str, pairs: list) -> int:
        """替换 a 的全部 mention 边：[(b_file_id, anchor)]，返回写入条数"""
        self.conn.execute("DELETE FROM edges WHERE a_file_id=? AND kind='mention'", (a_file_id,))
        now = local_iso()
        seen, n = set(), 0
        cache = {}
        for b_id, anchor in pairs:
            if not b_id or b_id == a_file_id or (b_id, anchor) in seen:
                continue
            seen.add((b_id, anchor))
            _, tok = split_anchor(anchor)
            if tok:
                if b_id not in cache:
                    cache[b_id] = self.section_names(b_id)
                bs = section_hit(cache[b_id], tok)
            else:
                bs = ""
            self.conn.execute(
                "INSERT OR REPLACE INTO edges(a_file_id,b_file_id,kind,anchor,b_section,"
                "weight,created_at) VALUES(?,?,'mention',?,?,1.0,?)",
                (a_file_id, b_id, anchor, bs, now))
            n += 1
        self.conn.commit()
        return n

    def mentions(self, a_file_id: str) -> list:
        """我引用了谁（带对方当前名；带小节时给出那一节还在不在）"""
        rows = self.conn.execute(
            "SELECT e.b_file_id AS id, e.anchor AS anchor, COALESCE(f.name,'') AS name,"
            " COALESCE(f.kind,'') AS kind"
            " FROM edges e LEFT JOIN files f ON f.id=e.b_file_id"
            " WHERE e.a_file_id=? AND e.kind='mention' ORDER BY f.name", (a_file_id,)).fetchall()
        out, cache = [], {}
        for r in rows:
            d = dict(r)
            name, sec = split_anchor(d["anchor"])
            d["section"] = sec
            if sec:
                if d["id"] not in cache:
                    cache[d["id"]] = self.section_names(d["id"])
                hit = section_hit(cache[d["id"]], sec)
                d["section_heading"] = hit
                d["section_ok"] = bool(hit)
            else:
                d["section_heading"] = ""
                d["section_ok"] = True
            out.append(d)
        return out

    def backlinks(self, b_file_id: str, section: str = "") -> list:
        """谁引用了我（带"引的是我的哪一节"）

        section 给了就按**目标端小节**精确过滤（`b_section`）：这是"谁引用了我的 P12"的正解 ——
        不用把我这篇的所有反向链接捞出来再字符串对齐。
        """
        names = self.section_names(b_file_id)
        sql = ("SELECT e.a_file_id AS id, e.anchor AS anchor, e.b_section AS b_section,"
               " COALESCE(f.name,'') AS name"
               " FROM edges e LEFT JOIN files f ON f.id=e.a_file_id"
               " WHERE e.b_file_id=? AND e.kind='mention'")
        args = [b_file_id]
        if (section or "").strip():
            hit = section_hit(names, section)
            sql += " AND e.b_section=?"
            args.append(hit or "\u0000")          # 匹配不到就查不到（不硬凑）
        sql += " ORDER BY f.name"
        out = []
        for r in self.conn.execute(sql, tuple(args)).fetchall():
            d = dict(r)
            _, tok = split_anchor(d["anchor"])
            d["section"] = tok
            d["section_heading"] = d.pop("b_section") or ""
            d["section_ok"] = True if not tok else bool(d["section_heading"])
            out.append(d)
        return out

    def backlink_groups(self, b_file_id: str) -> list:
        """把反向链接**按"被引的是我的哪一节"分组**（本文小节顺序在前，整篇引用排最后）

        [{section, heading, ok, count, refs:[{id,name}]}]
        """
        names = self.section_names(b_file_id)
        order = {n: i for i, n in enumerate(names)}
        groups = {}
        for b in self.backlinks(b_file_id):
            # 分组的键用**规范小节名**（写 `#P12` 与写 `#P12 决策边界` 的会合到一组）；
            # 目标篇已经没有的那一节，只好退化成按写的 token 分组
            key = b["section_heading"] or ("?" + b["section"] if b["section"] else "")
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"section": b["section"], "heading": b["section_heading"],
                                   "ok": b["section_ok"], "refs": []}
            g["refs"].append({"id": b["id"], "name": b["name"]})
        out = list(groups.values())
        for g in out:
            g["count"] = len(g["refs"])
        out.sort(key=lambda g: (1, 0) if not g["section"] else
                 (0, order.get(g["heading"], 10 ** 6)))
        return out

    def name_index(self) -> dict:
        """display name（去 .md 后缀）→ file_id；重名的跳过"""
        idx, dup = {}, set()
        for r in self.conn.execute("SELECT id, name FROM files"):
            nm = r["name"] or ""
            d = nm[:-3] if nm.lower().endswith(".md") else nm
            if not d:
                continue
            if d in idx:
                dup.add(d)
            else:
                idx[d] = r["id"]
        for d in dup:
            idx.pop(d, None)
        return idx

    # ── 墓碑：删掉的文件留个记号，引用处才能标"已删除" ────────
    @staticmethod
    def _display(name: str) -> str:
        return ref_display(name)

    def add_tombstone(self, file_id: str, name: str, folder_id: str = "") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO tombstones(file_id,name,display,folder_path,deleted_at)"
            " VALUES(?,?,?,?,?)",
            (file_id, name, self._display(name), self.folder_path(folder_id) if folder_id else "",
             local_iso()))
        self.conn.commit()

    def tombstone_displays(self) -> set:
        return {r["display"] for r in self.conn.execute("SELECT display FROM tombstones")}

    def tombstones(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM tombstones ORDER BY deleted_at DESC")]

    def clear_tombstone(self, display: str) -> int:
        """同名文件又被建出来 → 撤掉墓碑（否则新文件会被当成已删除）"""
        cur = self.conn.execute("DELETE FROM tombstones WHERE display=?", (display,))
        self.conn.commit()
        return cur.rowcount

    # ── 改名时把引用处的正文一起改 ───────────────────────────
    def referrers(self, b_file_id: str) -> list:
        """引用过这篇的文件：[{id, name, anchor}]"""
        return [dict(r) for r in self.conn.execute(
            "SELECT DISTINCT e.a_file_id AS id, COALESCE(f.name,'') AS name, e.anchor AS anchor"
            " FROM edges e LEFT JOIN files f ON f.id=e.a_file_id"
            " WHERE e.b_file_id=? AND e.kind='mention'", (b_file_id,))]

    def rewrite_mentions(self, file_id: str, old_display: str, new_display: str,
                         master=None) -> int:
        """把某篇正文里的 @旧名 改成 @新名；返回改了几处（0 = 正文没变）

        加密且未解锁的文件跳过（返回 -1），由调用方如实报出来。
        """
        if not old_display or old_display == new_display:
            return 0
        row = self.file_by_id(file_id)
        if row is None:
            return 0
        if row["visibility"] == "private" and master is None:
            return -1
        try:
            text = self.get_content(file_id, master).decode("utf-8", "replace")
        except Exception:
            return -1
        out, n = rewrite_at_mentions(text, old_display, new_display)
        if not n:
            return 0
        self.put_content(file_id, out.encode("utf-8"), master)
        return n

    # ── 标签（用于"归档类内容默认不参与检索"）────────────────
    def add_tag(self, file_id: str, name: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO tags(id,name) VALUES(?,?)", (name, name))
        self.conn.execute("INSERT OR IGNORE INTO file_tags(file_id,tag_id) VALUES(?,?)", (file_id, name))
        self.conn.commit()

    def remove_tag(self, file_id: str, name: str) -> None:
        self.conn.execute("DELETE FROM file_tags WHERE file_id=? AND tag_id=?", (file_id, name))
        self.conn.commit()

    def tags_of(self, file_id: str) -> list:
        return [r["name"] for r in self.conn.execute(
            "SELECT t.name FROM file_tags ft JOIN tags t ON t.id=ft.tag_id"
            " WHERE ft.file_id=? ORDER BY t.name", (file_id,))]

    def files_with_tag(self, name: str) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT f.id, f.name FROM file_tags ft JOIN files f ON f.id=ft.file_id"
            " WHERE ft.tag_id=? ORDER BY f.name", (name,))]

    def tag_stats(self) -> dict:
        return {r["name"]: r["c"] for r in self.conn.execute(
            "SELECT t.name, COUNT(*) c FROM file_tags ft JOIN tags t ON t.id=ft.tag_id"
            " GROUP BY t.name ORDER BY c DESC")}

    @staticmethod
    def _tag_where(exclude_tags, alias: str = "c") -> tuple:
        """排除带指定标签的文件；返回 (SQL 片段, 参数)"""
        if not exclude_tags:
            return "", []
        ph = ",".join("?" * len(exclude_tags))
        return ("AND NOT EXISTS (SELECT 1 FROM file_tags ft JOIN tags t ON t.id=ft.tag_id"
                " WHERE ft.file_id=%s.file_id AND t.name IN (%s)) " % (alias, ph),
                list(exclude_tags))

    @staticmethod
    def _date_where(since=None, until=None, alias: str = "f") -> tuple:
        """按笔记日期收窄；返回 (SQL 片段, 参数)。

        日期是从路径/文件名解析出来的（files.doc_date）。没有日期的笔记（约 3 成）
        在带日期条件时不出现 —— 判断不了，就不硬塞进某个时间段。
        """
        sql, args = "", []
        if since:
            sql += "AND IFNULL(%s.doc_date,'') >= ? " % alias
            args.append(str(since))
        if until:
            sql += "AND IFNULL(%s.doc_date,'') <= ? " % alias
            args.append(str(until))
        return sql, args

    # ── 关系通道：沿 mention 边走一跳（旧 RAG 的"图谱扩展"，这里用库里的边）──
    def related_file_ids(self, seed_file_ids: list) -> list:
        """和这些文件有 mention 关系（任一方向）的其它文件 id"""
        seeds = {x for x in (seed_file_ids or []) if x}
        if not seeds:
            return []
        ph = ",".join("?" * len(seeds))
        rows = self.conn.execute(
            "SELECT b_file_id AS id FROM edges WHERE kind='mention' AND a_file_id IN (%s)"
            " UNION"
            " SELECT a_file_id AS id FROM edges WHERE kind='mention' AND b_file_id IN (%s)" % (ph, ph),
            (*seeds, *seeds)).fetchall()
        return sorted({r["id"] for r in rows} - seeds)

    def related_chunks(self, seed_file_ids: list, terms: list, limit: int = 10,
                       include_sensitive: bool = False, exclude_tags=None,
                       since=None, until=None) -> list:
        """关联笔记里挑"最贴查询"的块：每篇只出一条（命中查询词最多的那块）

        关系是数据（FORMAT §6），这里不是猜的，是查 edges 走一跳。
        """
        rel = self.related_file_ids(seed_file_ids)
        if not rel:
            return []
        dsql, dargs = self._date_where(since, until)
        out = []
        for fid in rel:
            rows = self.conn.execute(
                "SELECT c.id AS chunk_id, c.file_id, f.name AS name, c.heading, c.text,"
                " c.seq AS seq, c.title_path AS title_path, c.locator AS locator,"
                " f.doc_date AS doc_date,"
                " (SELECT COUNT(*) FROM chunks c2 WHERE c2.file_id=c.file_id) AS total,"
                " c.is_sensitive, (SELECT GROUP_CONCAT(t.name) FROM file_tags ft"
                "  JOIN tags t ON t.id=ft.tag_id WHERE ft.file_id=c.file_id) AS tags"
                " FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.file_id=?"
                + dsql + " ORDER BY c.seq LIMIT 40", (fid, *dargs)).fetchall()
            first, best, best_hits = None, None, -1
            for r in rows:
                if r["is_sensitive"] and not include_sensitive:
                    continue
                if exclude_tags and (set((r["tags"] or "").split(",")) & set(exclude_tags)):
                    continue
                if first is None:
                    first = dict(r)
                hits = sum(1 for t in (terms or []) if t in (r["text"] or ""))
                if hits > best_hits:
                    best, best_hits = dict(r), hits
            # 命中词为零也给第一条：关系通道的价值正是"查询词不在关联笔记里"时把人带过去
            pick = best or first
            if pick:
                pick["via"] = "related"
                pick["rel_hits"] = max(best_hits, 0)
                out.append(pick)
        out.sort(key=lambda x: -sum(1 for t in (terms or []) if t in (x.get("text") or "")))
        return out[:limit]

    def orphan_edges(self) -> int:
        """两端不存在的边有几条"""
        r = self.conn.execute(
            "SELECT COUNT(*) AS n FROM edges e"
            " WHERE NOT EXISTS(SELECT 1 FROM files f WHERE f.id=e.a_file_id)"
            "    OR NOT EXISTS(SELECT 1 FROM files f WHERE f.id=e.b_file_id)").fetchone()
        return int(r["n"])

    # ── 分块与全文检索（FORMAT 2 / EVENT 无涉；纯库操作）─────────
    def delete_chunks(self, file_id: str) -> None:
        """删该文件的块：先按 rowid 同步 FTS（外部内容表必须显式 'delete'），再删主表。

        细节：FTS5 content= 外部表时，'delete' 命令要喂**原内容表的列值**，
        不是 index 里的值 —— 所以先查出 rowid 再按 rowid 删。
        """
        # 块被清掉 → 索引签名必须一起失效：否则下次"增量重建"看到签名没变会跳过，
        # 而块其实已经没了（上锁 purge、移出检索库都会走到这里）。
        self.conn.execute("UPDATE files SET indexed_sig=NULL WHERE id=?", (file_id,))
        rows = self.conn.execute(
            "SELECT id AS cid, rowid AS rid, text, heading FROM chunks WHERE file_id=?", (file_id,)).fetchall()
        for r in rows:
            self.conn.execute(
                "INSERT INTO chunks_fts(chunks_fts, rowid, text, heading) VALUES('delete',?,?,?)",
                (r["rid"], r["text"], r["heading"]))
            self.conn.execute("DELETE FROM vectors WHERE chunk_id=?", (r["cid"],))
            self.conn.execute("DELETE FROM chunks WHERE rowid=?", (r["rid"],))
        self.conn.commit()

    def replace_chunks(self, file_id: str, chunks: list) -> int:
        """替换该文件的全部块（先删后插，含 FTS 同步）。

        内容没变的块把旧向量接回去：否则每次重建索引都会把语义索引整个抹掉。
        """
        # 续用的判据是"**嵌入文本**没变"（= 正文去掉 @提及名字后的文本），
        # 不是"正文没变"：@提及 改名会让正文变、但嵌入文本不变 → 向量照旧可用（2026-09-14 辞玖定）
        frow = self.file_by_id(file_id)
        ddate = frow["doc_date"] if frow is not None else None
        # 续用判据用**语法级掩码**（所有 @引用 都当占位），不用解析级：
        # 解析级掩码会随"被引用方改名/新增"而变 —— 改名那一瞬间引用方正文里的旧名字不再解析得到，
        # 于是一边掩码一边不掩码，哈希必然不等，本该续用的向量被判成待重算（§25 要的正是反过来）。
        # 续用**按内容配对**，不按位置（seq）：
        # 按位置配对时，中间插一段会让后面所有块的 seq 平移 → 全部被判成"变了"白重算
        # （2026-09-18 实测：6 块变 1 块；改成内容配对后只重算真正变的那块）。
        # 同一个哈希可能对应多个块（重复段落），所以按内容挂一个栈，逐个取用。
        carry = {}
        for r in self.conn.execute(
                "SELECT v.chunk_id AS cid, v.model AS model, v.dim AS dim, v.vec AS vec,"
                " c.text AS text FROM vectors v JOIN chunks c ON c.id=v.chunk_id"
                " WHERE c.file_id=?", (file_id,)):
            d = dict(r)
            eh = sha256_bytes(embed_text(d.pop("text") or "", ddate).encode("utf-8"))
            carry.setdefault(eh, []).append(d)
        self.delete_chunks(file_id)
        n, reused = 0, 0
        for c in chunks:
            tp = c.get("title_path") or []
            text = c.get("text") or ""
            heading = c.get("heading") or ""
            cid = "%s:%d" % (file_id, c.get("seq", n))
            chash = c.get("content_hash") or ""
            bid = c.get("block_id") or block_id_of(tp, text)
            cur = self.conn.execute(
                "INSERT INTO chunks(id,file_id,seq,kind,heading,title_path,text,char_count,"
                "content_hash,locator,is_sensitive,block_id,char_start,char_end)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, file_id, c.get("seq", n),
                 c.get("kind") or "text", heading, json.dumps(tp, ensure_ascii=False),
                 text, c.get("char_count") or len(text), chash,
                 json.dumps(c.get("locator"), ensure_ascii=False) if c.get("locator") else None,
                 1 if c.get("is_sensitive") else 0, bid,
                 c.get("char_start"), c.get("char_end")))
            rid = cur.lastrowid
            self.conn.execute("INSERT INTO chunks_fts(rowid, text, heading) VALUES(?,?,?)",
                              (rid, text, heading))
            bucket = carry.get(sha256_bytes(embed_text(text, ddate).encode("utf-8"))) or []
            old = bucket.pop() if bucket else None
            if old and str(old["model"]).split("#")[-1] == EMBED_SPEC:
                self.conn.execute(
                    "INSERT OR REPLACE INTO vectors(chunk_id,model,dim,vec) VALUES(?,?,?,?)",
                    (cid, old["model"], old["dim"], old["vec"]))
                reused += 1
            n += 1
        self.conn.commit()
        self.last_reused = reused
        return n

    def section_names(self, file_id: str) -> list:
        """这篇里"可以被引用的小节名"：各块的标题 + 层级路径各级（去重保序，跳过文档标题那段）"""
        out, seen = [], set()

        def push(x):
            x = str(x or "").strip()
            if x and x not in seen:
                seen.add(x)
                out.append(x)

        for r in self.conn.execute("SELECT heading, title_path FROM chunks WHERE file_id=?"
                                   " ORDER BY seq", (file_id,)):
            push(r["heading"])
            try:
                tp = json.loads(r["title_path"] or "[]")
            except Exception:
                tp = []
            for x in (tp[1:] or []):
                push(x)
        return out

    def section_match(self, file_id: str, token: str) -> str:
        """token 匹配到的小节名（归一后 整串/前缀/包含）；没有就返回空串"""
        if not (token or "").strip():
            return ""
        return section_hit(self.section_names(file_id), token)

    def section_exists(self, file_id: str, token: str) -> bool:
        """这篇里有没有这一节（标题/层级路径前缀匹配）

        用来在保存时提醒"@目标#小节 里的这一节不存在"，也用来标"小节级死链"。
        """
        if not (token or "").strip():
            return True
        return bool(self.section_match(file_id, token))

    # ── 派生文本（文档解析缓存）────────────────────────────────
    def derived_of(self, file_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM derived WHERE file_id=?", (file_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        try:
            d["meta"] = json.loads(d["meta"] or "{}")
        except Exception:
            d["meta"] = {}
        return d

    def put_derived(self, file_id: str, res: dict, source_hash: str) -> dict:
        self.conn.execute(
            "INSERT OR REPLACE INTO derived(file_id,format,parser,source_hash,text,meta,error,"
            "created_at) VALUES(?,?,?,?,?,?,?,?)",
            (file_id, res.get("format") or "", res.get("parser") or "", source_hash,
             res.get("text"), json.dumps(res.get("meta") or {}, ensure_ascii=False),
             res.get("error") or "", local_iso()))
        self.conn.commit()
        return res

    def drop_derived(self, file_id: str) -> int:
        self.conn.execute("DELETE FROM blocks WHERE file_id=?", (file_id,))   # 块树同属派生缓存
        cur = self.conn.execute("DELETE FROM derived WHERE file_id=?", (file_id,))
        self.conn.commit()
        return cur.rowcount

    # ── 块树（文档结构：页/块/类型/bbox/表格）──────────────────
    def put_blocks(self, file_id: str, blocks: list) -> int:
        """整篇替换块树（解析产出的结构）；传空列表就清空"""
        self.conn.execute("DELETE FROM blocks WHERE file_id=?", (file_id,))
        now = local_iso()
        n = 0
        for b in (blocks or []):
            text = b.get("text_md")
            bid = b.get("block_id") or block_id_of(
                [b.get("type") or "", "p%s" % (b.get("page_no") or "")], text or "")
            self.conn.execute(
                "INSERT OR REPLACE INTO blocks(id,file_id,seq,page_no,type,level,bbox,char_start,"
                "char_end,text_md,table_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("%s:%s" % (file_id, b.get("seq", n)), file_id, b.get("seq", n), b.get("page_no"),
                 b.get("type") or "", b.get("level"),
                 json.dumps(b.get("bbox"), ensure_ascii=False) if b.get("bbox") else None,
                 b.get("char_start"), b.get("char_end"), text,
                 json.dumps(b.get("table_json"), ensure_ascii=False) if b.get("table_json") else None,
                 now))
            n += 1
        self.conn.commit()
        return n

    def blocks_of(self, file_id: str) -> list:
        out = []
        for r in self.conn.execute(
                "SELECT seq,page_no,type,level,bbox,char_start,char_end,text_md,table_json"
                " FROM blocks WHERE file_id=? ORDER BY seq", (file_id,)):
            d = dict(r)
            d["block_id"] = block_id_of([d["type"] or "", "p%s" % (d["page_no"] or "")],
                                        d["text_md"] or "")
            for k in ("bbox", "table_json"):
                try:
                    d[k] = json.loads(d[k]) if d[k] else None
                except Exception:
                    d[k] = None
            out.append(d)
        return out

    def block_stats(self, file_id: str) -> dict:
        def q(sql):
            return self.conn.execute(sql, (file_id,)).fetchone()[0]
        return {"blocks": q("SELECT COUNT(*) FROM blocks WHERE file_id=?"),
                "pages": q("SELECT COUNT(DISTINCT page_no) FROM blocks WHERE file_id=?"
                           " AND page_no IS NOT NULL"),
                "tables": q("SELECT COUNT(*) FROM blocks WHERE file_id=? AND type='table'"),
                "headings": q("SELECT COUNT(*) FROM blocks WHERE file_id=? AND type='heading'"),
                "dropped": q("SELECT COUNT(*) FROM blocks WHERE file_id=?"
                             " AND type IN ('header','footer')")}

    def derived_for(self, file_id: str, raw: bytes | None = None, refresh: bool = False) -> dict:
        """取（必要时重算）文档的派生文本；按内容哈希失效。

        raw=None 时只用缓存（不重算）；给了 raw 且哈希变了（或 refresh）就重新解析。
        """
        import doctext
        row = self.derived_of(file_id)
        chash = sha256_bytes(raw) if raw is not None else None
        if row is not None and not refresh and (chash is None or row.get("source_hash") == chash):
            row["cached"] = True
            return row
        if raw is None:
            return row or {"format": "", "parser": "", "text": None, "meta": {}, "error": "",
                           "cached": False}
        f = self.file_by_id(file_id)
        ext = (f["ext"] if f is not None else "") or ""
        # 文本优先用成熟引擎（pandoc）读；内置解析器既兜底又提供 meta（页数/标题）
        meta_res = doctext.parse(ext, raw)
        try:
            import docconvert
            got = docconvert.read_to_md(ext, raw)
        except Exception as e:
            got = {"text": "", "engine": "", "error": "%s: %s" % (type(e).__name__, str(e)[:100])}
        text = got.get("text") or meta_res.get("text")
        res = {"format": ext, "text": text,
               "parser": got.get("engine") or meta_res.get("parser") or "",
               "error": "" if text else (got.get("error") or meta_res.get("error") or ""),
               "meta": meta_res.get("meta") or {}}
        self.put_derived(file_id, res, chash)
        # 块树只在"留下的正文与结构解析同源"时才存：pandoc 的正文与内置结构解析不是同一份，
        # 偏移会错位 —— 那种情况宁可不存，也不要留一份对不上的坐标。
        blocks = meta_res.get("blocks") or []
        same_src = (text or "") == (meta_res.get("text") or "")
        # 没块树、库里也没有 → 什么都不用做（否则每解析一篇都白写一次 DELETE+commit）
        if blocks or self.conn.execute("SELECT 1 FROM blocks WHERE file_id=? LIMIT 1",
                                       (file_id,)).fetchone():
            self.put_blocks(file_id, blocks if same_src else [])
        res["cached"] = False
        res["source_hash"] = chash
        return res

    def article_chunks(self, file_id: str, include_sensitive: bool = False) -> list:
        """整篇的块（按 seq 序）—— 按篇召回时用来定目标、取上下文"""
        sens = "" if include_sensitive else "AND c.is_sensitive = 0 "
        rows = self.conn.execute(
            "SELECT c.id, c.seq, c.kind, c.heading, c.title_path, c.text, c.char_count"
            " FROM chunks c WHERE c.file_id=? " + sens + "ORDER BY c.seq", (file_id,)).fetchall()
        return [dict(r) for r in rows]

    # ── 查询切词（中文无空格，整句必须切）────────────────────
    @staticmethod
    def _terms(q: str, max_terms: int = 12) -> list:
        """把自然语言查询切成检索词。

        - 英文/数字：整词（正则）
        - 中文：连续中文片段（长度 2..8，优先长的），去重保序
        关键词通道只需"命中任一词"，所以切碎是好事（句子里的词通常分散在文档各处）。
        """
        import re as _re
        s = str(q or "")
        parts = []
        seen = set()

        def push(x):
            x = x.strip()
            if len(x) >= 2 and x not in seen:
                seen.add(x)
                parts.append(x)

        for w in _re.findall(r"[A-Za-z][A-Za-z0-9_+\-]{1,}", s):
            push(w)
        for seg in _re.findall(r"[\u4e00-\u9fff]+", s):
            if len(seg) <= 8:
                push(seg)
            else:
                for n in (4, 3, 2):
                    for i in range(0, len(seg) - n + 1, n):
                        push(seg[i:i + n])
                    if len(parts) >= max_terms:
                        break
        return parts[:max_terms]

    # SQLite FTS5 的 MATCH 不支持参数绑定 —— 必须内联到 SQL 里
    @staticmethod
    def _fts_query(q: str) -> str:
        """把用户输入转成安全的 FTS5 查询：按空白切词，每个词加双引号（防操作符注入）"""
        terms = [t for t in str(q or "").replace('"', " ").split() if t.strip()]
        if not terms:
            return '""'
        return " ".join('"%s"' % t for t in terms)

    def search_chunks(self, q: str, limit: int = 10, include_sensitive: bool = False,
                      exclude_tags=None, since=None, until=None) -> list:
        """全文检索（混合）：切词后多词 OR，按**命中词数**再按 bm25 排序。

        返回 [{chunk_id, file_id, name, heading, text, hits, rank, is_sensitive}]
        - MATCH 通道：trigram，词须 ≥3 字符
        - LIKE 通道：覆盖短词（2 字中文）与 MATCH 未命中
        """
        terms = self._terms(q)
        if not terms:
            return []
        sens = "" if include_sensitive else "AND c.is_sensitive = 0 "
        tag_sql, tag_args = self._tag_where(exclude_tags)
        date_sql, date_args = self._date_where(since, until)
        tag_args = list(tag_args) + list(date_args)
        tag_sel = ("(SELECT GROUP_CONCAT(t.name) FROM file_tags ft JOIN tags t ON t.id=ft.tag_id"
                   " WHERE ft.file_id=c.file_id) AS tags, ")
        long_terms = [x for x in terms if len(x) >= 3]
        short_terms = [x for x in terms if len(x) < 3]

        def _collect(rows):
            out = {}
            for r in rows:
                d = dict(r)
                out[d["chunk_id"]] = d
            return out

        found = {}

        # ① MATCH（trigram）：多词 OR
        if long_terms:
            sql = ("SELECT c.id AS chunk_id, c.file_id, f.name AS name, c.heading, c.text,"
                   " c.seq AS seq, c.title_path AS title_path, c.locator AS locator,"
                   " c.is_sensitive, f.doc_date AS doc_date,"
                   " (SELECT COUNT(*) FROM chunks c2 WHERE c2.file_id=c.file_id) AS total, "
                   + tag_sel + " bm25(chunks_fts) AS rank "
                   "FROM chunks_fts JOIN chunks c ON c.rowid = chunks_fts.rowid "
                   "JOIN files f ON f.id = c.file_id WHERE chunks_fts MATCH ? "
                   + sens + tag_sql + date_sql + "ORDER BY rank LIMIT ?")
            fq = " OR ".join('"%s"' % x for x in long_terms)
            try:
                found.update(_collect(self.conn.execute(sql, (fq, *(tag_args + [int(limit) * 4])))))
            except Exception:
                pass

        # ② LIKE：短词 + （MATCH 已命中不足时的长词兜底）
        like_terms = short_terms + (long_terms if len(found) < limit else [])
        if like_terms:
            ors = " OR ".join(["c.text LIKE ?"] * len(like_terms))
            sql = ("SELECT c.id AS chunk_id, c.file_id, f.name AS name, c.heading, c.text,"
                   " c.seq AS seq, c.title_path AS title_path, c.locator AS locator,"
                   " c.is_sensitive, f.doc_date AS doc_date,"
                   " (SELECT COUNT(*) FROM chunks c2 WHERE c2.file_id=c.file_id) AS total, "
                   + tag_sel + " 0.0 AS rank "
                   "FROM chunks c JOIN files f ON f.id = c.file_id "
                   "WHERE (" + ors + ") " + sens + tag_sql + date_sql + "LIMIT ?")
            args = ["%" + x + "%" for x in like_terms] + tag_args + [int(limit) * 4]
            try:
                for k, v in _collect(self.conn.execute(sql, args)).items():
                    found.setdefault(k, v)
            except Exception:
                pass

        # ③ 打分：命中词数优先，其次 bm25（越小越相关）
        out = []
        for d in found.values():
            text = d.get("text") or ""
            hits = sum(1 for x in terms if x in text)
            d["hits"] = hits
            out.append(d)
        out = [d for d in out if d["hits"] > 0]
        out.sort(key=lambda d: (-d["hits"], d.get("rank") or 0.0))
        return out[:int(limit)]

    def backfill_fts(self) -> int:
        """重建 FTS 索引（挂在 chunks 外部内容表上）—— 自检/修复用"""
        self.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        self.conn.commit()
        return self.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]

    def stats(self) -> dict:
        q = lambda s: self.conn.execute(s).fetchone()[0]
        db_bytes = self.path.stat().st_size if self.path.exists() else 0
        wal_bytes = 0
        for suffix in ("-wal", "-shm"):
            p = self.path.with_name(self.path.name + suffix)
            if p.exists():
                wal_bytes += p.stat().st_size
        return {
            "folders": q("SELECT COUNT(*) FROM folders"),
            "files": q("SELECT COUNT(*) FROM files"),
            "chunks": q("SELECT COUNT(*) FROM chunks"),
            "blocks": q("SELECT COUNT(*) FROM blocks"),
            "rag_off": q("SELECT COUNT(*) FROM files WHERE IFNULL(rag,'on')<>'on'"),
            "stale_sig": q("SELECT COUNT(*) FROM files WHERE IFNULL(indexed_sig,'')=''"),
            "bytes": q("SELECT IFNULL(SUM(size),0) FROM files"),
            "db_bytes": db_bytes,
            "wal_bytes": wal_bytes,
            "disk_bytes": db_bytes + wal_bytes,
        }

    # ── 倒转：内容真源＝库树文件 ────────────────────────────────
    # 2026-10-14 定案（辞玖）：**文件系统是唯一真相源，数据库降为可重建的索引**。
    # 做法分三步，每步可单独开关、可回退：
    #   ① disk_first=True  → 读优先读盘（本段）
    #   ② disk_projection=True → 写落盘（库内不再留正文）
    # 例外（**必须走库**，不能读盘）：
    #   · 私密篇：盘上是密文，要主密钥解密 + 解锁语义
    #   · 不在库树里的（模板/文档等）：盘上没有映射
    #   · 没配 library_dir：退回库内读（宁可旧行为，也不要读空）
    def _disk_projection(self) -> bool:
        """写是否落盘（倒转开关）。manifest 里 disk_projection=1 才开 —— 默认关 = 现有行为不变。

        与 _disk_first 分开：读先改、写后改，两步各自能单独回退。
        """
        try:
            return str((self.manifest() or {}).get("disk_projection") or "") == "1"
        except Exception:
            return False

    def _project_to_disk(self, file_id: str, data: bytes | None = None) -> None:
        """把这一篇投影到库树（写盘）—— 倒转：文件系统是唯一真相源。

        **data 必须传**（2026-10-14 实测的 bug）：不传的话 write_through 会自己去
        `db.get_content()` 取内容 —— 而倒转开了 disk_first 之后那会**从盘上读旧内容**，
        于是"把旧内容又写回盘上"，新内容永远落不了盘（静默数据错）。

        复用 tree_write.write_through —— 它是库自己维护磁盘投影的**唯一出口**，
        已有原子替换（tmp + os.replace）、转义保护（_inside）、被占用重试 3 次、失败不静默。
        不另造一套写盘逻辑（两套必然漂移）。

        ⚠️ 失败不吞（P39/P54：「写不进去」不许当成功）：写盘没成功时**打印出来**，
           但**不抛** —— 双写期库内那份仍然写成功了，抛出去会让整次保存失败。
        """
        if not self._lib_root():
            return                                  # 没配库树：write_through 自己会 no-op
        try:
            import tree_write as _tw
            r = _tw.write_through(self, file_id, None, data)
            if not r.get("ok"):
                self._note_tree_error(file_id, r.get("why") or "unknown")
        except Exception as e:
            self._note_tree_error(file_id, "%s: %s" % (type(e).__name__, str(e)[:80]))
    def _note_tree_error(self, file_id: str, why: str) -> None:
        """落盘失败留痕（供自检/排障查；不抛）"""
        try:
            rec = {"at": local_iso(), "file_id": file_id, "why": why}
            globals().setdefault("_TREE_ERRORS", []).append(rec)
            print("[disk_projection] 落盘失败：%s: %s" % (str(file_id)[:8], why), flush=True)
        except Exception:
            pass

    def disk_projection_errors(self) -> list:
        """本次进程里落盘失败的记录（自检用）"""
        return list(globals().get("_TREE_ERRORS") or [])

    def _disk_first(self) -> bool:
        """读是否优先读盘（倒转开关）。manifest 里 disk_first=1 才开 —— 默认关 = 现有行为不变。"""
        try:
            return str((self.manifest() or {}).get("disk_first") or "") == "1"
        except Exception:
            return False

    def _lib_root(self) -> str:
        try:
            return (self.manifest() or {}).get("library_dir") or ""
        except Exception:
            return ""

    def _disk_path(self, rel: str) -> str:
        """库内相对路径 → 盘上绝对路径。口径与 tree_write.rel_of 一致：去掉库内那层 vault/ 包装。"""
        if not rel:
            return ""
        parts = [x for x in str(rel).replace("\\", "/").split("/") if x]
        if parts and parts[0] == "vault":
            parts = parts[1:]
        if not parts:
            return ""
        root = self._lib_root()
        if not root:
            return ""
        p = os.path.abspath(os.path.join(root, *parts))
        # 落点必须真在库树里（防「名字里带 .. 或盘符」把读甩出去）——与 tree_write._inside 同口径
        try:
            if os.path.commonpath([os.path.abspath(root), p]) != os.path.abspath(root):
                return ""
        except ValueError:
            return ""
        return p

    def read_disk_content(self, file_id: str) -> tuple[bytes | None, str]:
        """按「内容真源＝文件」的判据从盘上读。返回 (bytes|None, 原因)。

        不适用（返回 None）的情形见上面注释里的"例外"。
        """
        try:
            row = self.conn.execute(
                "SELECT storage, content, storage_ref, enc_algo, key_ref, name, folder_id, visibility"
                " FROM files WHERE id=?", (file_id,)).fetchone()
        except Exception:
            return None, "no-row"
        if row is None:
            return None, "no-such-file"
        if row["enc_algo"]:
            return None, "private"                    # 私密：必须走库（解密 + 解锁语义）
        rel = self.disk_rel_of(file_id)
        if not rel:
            return None, "no-disk-path"
        p = self._disk_path(rel)
        if not p or not os.path.isfile(p):
            return None, "not-on-disk"
        try:
            return Path(p).read_bytes(), "disk"
        except Exception as e:
            return None, "read-failed:%s" % type(e).__name__

    def disk_rel_of(self, file_id: str) -> str:
        """这一篇在库文件夹里的相对路径（去掉库内那层 vault/ 包装）。

        口径必须与 `tree_write.rel_of` **完全一致** —— 否则读盘会读错文件。
        （tree_write 是库自己维护磁盘投影的唯一出口，它用的就是这套算法。）
        """
        try:
            row = self.conn.execute("SELECT name, folder_id FROM files WHERE id=?", (file_id,)).fetchone()
        except Exception:
            return ""
        if row is None:
            return ""
        p = ""
        try:
            if row["folder_id"]:
                p = self.folder_path(row["folder_id"]) or ""
        except Exception:
            p = ""
        parts = [x for x in p.replace("\\", "/").split("/") if x]
        if parts and parts[0] == "vault":
            parts = parts[1:]
        name = str(row["name"] or "")
        if not name or name in (".", "..") or any(c in name for c in '/\\:*?"<>|'):
            return ""
        return "/".join(parts + [name])

    def close(self) -> None:
        self.conn.close()
