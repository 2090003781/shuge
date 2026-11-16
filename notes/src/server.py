"""ai-notes 最小 API 服务 —— 只用标准库（http.server），零依赖。

职责：把库暴露成可被界面/脚本调用的 HTTP 接口（第一期最小面）：
    GET    /api/health                 存活与统计
    GET    /api/tree                   文件夹树（含文件，不含内容）
    GET    /api/files/<id>             读文件（含内容文本、@ 提及、反向链接）
    PUT    /api/files/<id>             保存内容（走 put_content + 重新索引 + 重建 @ 提及边）
    POST   /api/files                  新建文件 {folder_id, name, content, format}
                                        format 显式规定格式（md/txt/docx/pdf…）；名字没扩展名
                                        时默认 md；名字扩展名与 format 不一致 → 400
    DELETE /api/files/<id>             删除文件（级联清派生层）
    POST   /api/folders                新建文件夹 {parent_id, name}
    GET    /api/search?q=&limit=       混合检索（FTS + 向量 RRF）
    GET    /api/search?q=&mode=kw      仅关键词（不加载模型）
    POST   /api/batch                  批量：把一个操作施加到一组目标（白名单见 apidoc.BATCH_OPS）
    POST   /api/files/<id>/sections    按小节改正文 {mode: replace|append|insert|delete, name, text}
    POST   /api/files/<id>/reindex     单篇重建派生层 {force_parse: bool, with_vectors: bool}
    POST   /api/reindex                全量重建索引 {with_vectors: bool}
    GET    /api/lock                   锁状态 {configured, unlocked, expires_in}
    POST   /api/lock/set               首次设库密码 {password, confirm}
    POST   /api/lock/unlock            解锁 {password}（会话级；默认不自动上锁，只手动锁）
    POST   /api/lock/lock              立即上锁
    POST   /api/lock/change            换库密码（先解后封，旧密码失效）
    POST   /api/lock/clear             取消库密码（私密篇解密成明文）
    POST   /api/files/<id>/visibility  切可见性 {visibility: public|internal|private}
    POST   /api/files/<id>/append      正文尾部追加一段 {text}（日志转录用：只写新增那段）
    POST   /api/files/<id>/rename      改名 {name}
    POST   /api/files/<id>/move        移动 {folder_id} 或 {path: "vault/项目"}
    GET    /api/folders                文件夹扁平表（含路径，给"移动"用）
    GET    /api/content?path=&max=     按库内路径取正文（单条；给"从库取内容"用）
    POST   /api/content/batch          批量取正文 {paths:[], max: n}（给注入包组包用）
    POST   /api/folders/<id>/rename    文件夹改名 {name}
    DELETE /api/folders/<id>           删空文件夹
    （标签随 GET /api/files/<id> 一起返回，不单开读接口）
    POST   /api/files/<id>/tags               改标签 {tags:[], mode: add|remove|set}
    GET    /api/tags                          标签统计
    GET    /api/events?after=&limit=&types=      增量拉事件（位点续读）
    GET    /api/events/replay?from=&to=&dry_run= 按 gseq 区间重放
    GET    /api/files/<id>/raw               原字节（图片/附件直接用；按扩展名给 Content-Type）
    GET    /api/files/<id>/writers           这篇最近被谁改过（写入者标签，来源事件）
    GET    /api/before-images?file_id=&limit= 高风险操作（删除/覆盖）留下的前像清单（私密篇不留）
    POST   /api/before-images/restore        按前像恢复 {id} 或 {file_id}
    GET    /api/mirror                       库文件夹 / vault 的配置（**不是镜像管线**，见下）
    POST   /api/mirror/config                设置这几个路径 {library_dir, vault_dir, ignore, private}
    POST   /api/mirror/sync                  **已退役**：回 410（2026-09-30 归档，见 archive/2026-09-30-废弃能力/）

设计约定：
  · 检索器（向量）**懒加载**：不调 /api/search 就完全不加载模型
  · 写路径统一走 db（上层不直接碰 content 列）
  · 所有响应 JSON；异常返回 {ok:false, error}
  · 只监听 127.0.0.1（本机单人使用）
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
import urllib.error                                          # 批量入口要内部转发
import urllib.request
from urllib.parse import urlparse, parse_qs, quote as urlquote

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import apidoc                                                # 操作目录（唯一真源）  # noqa: E402
from db import Database, ThreadDB, DEFAULT_DATA_DIR, BadName  # noqa: E402
from mentions import parse_refs, split_anchor, resolve as resolve_mentions  # noqa: E402

import vaultcrypto  # noqa: E402
from events import EventLog  # noqa: E402


DEFAULT_EXCLUDE_TAGS = "logs"


def deleted_refs_in(text: str, file_id: str) -> list:
    """正文里 @到的名字当中，哪些是"已经被删掉"的（渲染时标不可用）"""
    try:
        refs = parse_refs(text)
    except Exception:
        return []
    if not refs:
        return []
    # 引用可能写的是路径（@目录/名字）→ 跟墓碑比要用**末段**
    names = {split_anchor(r["name"])[0].rsplit("/", 1)[-1] for r in refs}
    alive = {split_anchor(m["anchor"])[0].rsplit("/", 1)[-1] for m in STATE.db.mentions(file_id)}
    dead = STATE.db.tombstone_displays() & names
    return sorted(dead - alive)


def _writable_exts():
    """能"新建"出来的文档格式（pandoc 能写、pdf 走 WPS）"""
    import docconvert
    return docconvert.WRITE_EXTS


def _make_doc(md_text: str, ext: str) -> dict:
    """新建文档：一段文本 → 目标格式的真文件"""
    import docconvert
    return docconvert.make(md_text, ext)


# ── 新建文件的"格式"（辞玖 2026-10-03：**规定好确定格式**，不靠名字猜、更不靠内容嗅探）──
# 为什么要有它：原来只从名字里切扩展名 —— 名字不带后缀就落到兜底 asset（那篇
# 《手搓小夜模型-完整过程记录》就是这么被当成"二进制、不能看不能改"的）。
_CREATE_ALIAS = {"markdown": "md", "text": "txt", "yaml": "yml", "htm": "html"}


def create_formats() -> dict:
    """能"新建"出来的格式 → 规范扩展名。

    文本族 = 库自己那份 `KNOWN_TEXT_EXTS`（+jsonl），写进去的就是字节、库内可直接编辑；
    文档族 = `docconvert.WRITE_EXTS`（docx/odt/rtf/pdf/epub/html），用 pandoc/WPS 现场生成。
    两边都有 html 时**文档族优先**——跟旧行为一致（旧代码就是拿 WRITE_EXTS 现生成）。
    """
    import db as _db
    f = {e: e for e in _db.KNOWN_TEXT_EXTS}
    f["jsonl"] = "jsonl"
    f.update({e: e for e in _writable_exts()})
    for a, b in _CREATE_ALIAS.items():
        f[a] = b
    return f


def resolve_create_format(name: str, fmt: str):
    """定下这篇的格式 → (最终名字, 规范格式, 错误串)。错误串非空就是 400。

    规则（不猜、不回退）：
      1. 给了 format → 它说了算；名字带扩展名就必须一致，不一致报错让人自己改
      2. 没给 format → 名字有扩展名就按它；扩展名不在清单里 → 报错（别悄悄当文本）
      3. 名字没有扩展名 → 默认 md
         （例外：`..` / `.` / `trail.` 这种名字本身不合法的**不补后缀** —— 补了就等于替它
          把名字洗白，`check_name` 那道守卫会被绕过；原样交给 create_file 去拒）
    """
    f = create_formats()
    name = (name or "").strip() or "untitled"
    fmt = (fmt or "").strip().lower().lstrip(".")
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    named = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if fmt:
        if fmt not in f:
            return name, "", ("format 不认识：%s；能新建的是 %s" % (fmt, "/".join(sorted(f))))
        fmt = f[fmt]
        if named and f.get(named, named) != fmt:
            return name, "", ("名字的扩展名是 .%s，format 说的是 %s —— 改成一样，或者只留一个"
                              % (named, fmt))
        if not named and (base in (".", "..") or base.endswith(".")):
            return name, "", ""                  # 同上：别替它洗白
        return (name if named else name + "." + fmt), fmt, ""
    if named:
        if named in f:
            return name, f[named], ""
        return name, "", ("扩展名 .%s 不是能新建的格式；用 format 指定（%s），或换个扩展名"
                          % (named, "/".join(sorted(f))))
    # 名字本身就不合法（"." / ".." / 结尾是点）：**不补后缀** —— 补了就等于替它把名字洗白，
    # 名字守卫（check_name → BadName → 400）会被绕过。原样交给 create_file 去拒。
    if base in (".", "..") or base.endswith("."):
        return name, "", ""
    return name + ".md", "md", ""


def where_of(loc) -> str:
    """locator → 人看的位置串（"第 2 页 · 表格"）"""
    try:
        d = json.loads(loc) if isinstance(loc, str) else (loc or {})
    except Exception:
        d = {}
    if not d:
        return ""
    bits = []
    pages = [p for p in (d.get("pages") or ([d["page"]] if d.get("page") else [])) if p]
    if pages:
        bits.append("第 %s 页" % (str(pages[0]) if len(pages) == 1
                                 else "%s-%s" % (pages[0], pages[-1])))
    tmap = {"table": "表格", "heading": "小节标题", "paragraph": "正文", "figure": "图"}
    ts = [tmap.get(t, t) for t in (d.get("types") or []) if t]
    if ts:
        bits.append("/".join(ts[:2]))
    return " · ".join(bits)


def decorate_hits(hits: list) -> list:
    """命中结果带上位置：locator（结构化）+ where（人看的串）"""
    for h in (hits or []):
        loc = h.get("locator")
        try:
            d = json.loads(loc) if isinstance(loc, str) else (loc or None)
        except Exception:
            d = None
        h["locator"] = d or None
        h["where"] = where_of(d)
    return hits


def bad_sections_in(text: str) -> list:
    """引用里写了 @目标#小节，但目标篇里没有这一节 → 列出来（只提示，不改正文）"""
    out = []
    try:
        refs = parse_refs(text)
    except Exception:
        return out
    look = STATE.db.ref_resolver()
    for r in refs:
        if not r["section"]:
            continue
        fid = look(r["name"])
        if fid and not STATE.db.section_exists(fid, r["section"]):
            out.append({"anchor": r["anchor"], "name": r["name"], "section": r["section"]})
    return out


def write_through(fid: str, text: str, reason: str) -> bool:
    """把改过的正文走一遍"写库 + 落盘 + 重索引 + 补向量"（接口仍是唯一写入口）"""
    row = STATE.db.file_by_id(fid)
    if row is None:
        return False
    try:
        STATE.db.put_content(fid, text.encode("utf-8"), STATE.master())
        STATE.indexer.reindex_file(fid)
        embed_now([fid])
        tree_sync(fid)
        STATE.events.emit("note.updated", {"file_id": fid, "name": row["name"],
                                           "reason": reason, "changed_chars": 0})
    except Exception as e:
        # 不吞：写库/落盘失败要留痕，别让界面猜"为什么改了没生效"
        print("[write_through] %s 失败：%s: %s" % (reason, type(e).__name__, str(e)[:120]),
              file=sys.stderr)
        return False
    return True


def properties_index(key: str = "", hide_private: bool = False) -> dict:
    """按属性看全库：哪些键、每个键有哪些值、各多少篇。

    直接扫 files.frontmatter（JSON）—— 1270 行，几十毫秒的事，不值得建索引表。
    """
    import json as _json
    from collections import defaultdict
    counts = defaultdict(lambda: defaultdict(int))
    files_with = 0
    rows = STATE.db.conn.execute(
        "SELECT frontmatter FROM files WHERE IFNULL(frontmatter,'')<>''"
        + (" AND IFNULL(visibility,'')<>'private'" if hide_private else "")).fetchall()
    for r in rows:
        try:
            meta = _json.loads(r["frontmatter"])
        except Exception:
            continue
        if not isinstance(meta, dict):
            continue
        files_with += 1
        for k, v in meta.items():
            if k.startswith("_"):
                continue
            vals = v if isinstance(v, list) else [v]
            for one in vals:
                if one is None or one == "":
                    continue
                counts[k][str(one)] += 1
    out = []
    for k, vals in counts.items():
        if key and k != key:
            continue
        out.append({"key": k, "count": sum(vals.values()),
                    "values": [{"value": v, "count": c}
                               for v, c in sorted(vals.items(), key=lambda x: (-x[1], x[0]))]})
    out.sort(key=lambda x: (-x["count"], x["key"]))
    return {"files_with_properties": files_with, "keys": out}


def properties_find(key: str, value: str, limit: int = 50, offset: int = 0,
                    hide_private: bool = False) -> dict:
    """按属性找篇：key=status & value=待办 → 那几篇。value 空 = 只要有这个键就算。

    hide_private=True（上锁）：私密篇不参与（属性值里可能就写着秘密）2026-09-27。
    """
    import json as _json
    hits = []
    for r in STATE.db.conn.execute(
            "SELECT id, name, folder_id, doc_date, frontmatter FROM files"
            " WHERE IFNULL(frontmatter,'')<>''"
            + (" AND IFNULL(visibility,'')<>'private'" if hide_private else "")
            + " ORDER BY mtime DESC"):
        try:
            meta = _json.loads(r["frontmatter"])
        except Exception:
            continue
        if not isinstance(meta, dict) or key not in meta:
            continue
        v = meta.get(key)
        vals = [str(x) for x in (v if isinstance(v, list) else [v]) if x not in (None, "")]
        if value and value not in vals:
            continue
        hits.append({"id": r["id"], "name": r["name"], "doc_date": r["doc_date"] or "",
                     "path": STATE.db.display_path(r["id"]), "value": vals})
    total = len(hits)
    return {"key": key, "value": value, "total": total,
            "hits": hits[offset:offset + max(1, limit)]}


def sync_frontmatter_tags(fid: str, tags: list) -> bool:
    """库里改了标签 → 顺手写回文件头 YAML 的 `tags:`。

    为什么：辞玖要的是"前置属性和文件属性是一回事"——库里改了标签，
    磁盘上那份（以及 vault 镜像）的 YAML 也得跟着变，否则两边对不上。
    只改那一行，正文与其它键一字不动；走的还是 put_content + 落盘（接口是唯一写入口）。
    """
    row = STATE.db.file_by_id(fid)
    if row is None or (row["kind"] or "") not in ("markdown", "text", "code"):
        return False
    if is_locked(row):
        return False
    try:
        raw = STATE.db.get_content(fid, STATE.master())
        text = raw.decode("utf-8", "replace")
    except Exception:
        return False
    import frontmatter as _fm
    cur = _fm.norm_tags(_fm.parse(text).get("tags"))
    if cur == list(tags):
        return False
    new_text = _fm.upsert(text, {"tags": list(tags)})
    if new_text == text:
        return False
    return write_through(fid, new_text, "tags→frontmatter")


# ── 并发（2026-10-01 辞玖：「轻小请求先做」）────────────────
# 库原来是**单线程**（sqlite 连接不能跨线程 + 图省事），代价是"一个慢活把整库堵住"：
# 实测一个 4.1s 的转换导出跑着时，一个 /api/health 要等 **3506ms**。
# 现在：**读的并发、写的串行** —— 树/统计/取正文/看锁状态这类小请求不再排队，
# 而会改库、会落盘的请求（保存 / 转换 / 导入 / 维护）仍然一件件来（跟改造前一样安全）。
WRITE_LOCK = threading.RLock()
SEARCH_LOCK = threading.RLock()
# 锁序（防死锁）：写处理器若刷检索缓存，是"先 WRITE_LOCK 再 SEARCH_LOCK"；检索只拿 SEARCH_LOCK。
# 反过来拿就会死锁 —— 以后加锁请守这条。


def _search_guarded(fn, *a, **kw):
    """检索那条链加锁跑（它持有共享缓存）；调用点写法几乎不用改"""
    with SEARCH_LOCK:
        return fn(*a, **kw)


def refresh_searcher(file_ids=None) -> None:
    """写完正文 / 重算向量之后，把检索缓存跟上（2026-09-25）。

    传了 id 就只换这几篇；不传（批量导入、全量重建）才整表清空。
    """
    with SEARCH_LOCK:
        if not STATE._searcher:
            return
        if file_ids:
            STATE._searcher.refresh_files(file_ids)
        else:
            STATE._searcher.invalidate()


def projection_stale(file_id: str) -> bool:
    """库文件夹里那份是不是缺了 / 大小明显对不上（**只看 stat，不读内容**）。

    为什么要有：导入遇到"内容没变"会 skip（连投影都不碰），而盘上那份可能是缺的/旧的 ——
    库里对、盘上旧，再导一百次也修不了。这里给那种情况一个便宜的体检。
    """
    try:
        import tree_write as _tw
        rel = _tw.rel_of(STATE.db, file_id)
        root = _tw.library_dir(STATE.db)
        if not rel or not root:
            return False
        p = os.path.join(root, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            return True
        row = STATE.db.file_by_id(file_id)
        return os.path.getsize(p) != int((row["size"] if row else 0) or 0)
    except Exception:
        return False


def tree_sync_after_import(imp: dict) -> dict:
    """导入/上传完，把**每一篇**落到库文件夹（2026-09-30 体检：这条路从来没落过盘）。

    为什么必须有：`tree_write` 是"库自己维护磁盘投影"的唯一出口 —— 新建、保存、改名、移动、
    恢复前像都调了 `tree_sync`，只有导入这两条路由漏了。后果：**每个导入件都缺在库文件夹里**，
    一直到有人手动跑 `materialize --apply`（真库实测：`简历.docx` 和一张 webp 就是这么缺的）。

    批量做、失败不抛：导入本身已经成功（内容已经进库），落盘失败只记进 `tree_errors`，不改判结果。
    """
    ids = list(imp.get("created_ids") or []) + list(imp.get("updated_ids") or []) \
        + list(imp.get("secured_ids") or [])
    # "内容没变"的那几篇：不无条件重写（省 I/O），只在盘上那份**缺了/大小不对**时补一次
    ids += [fid for fid in (imp.get("unchanged_ids") or []) if projection_stale(fid)]
    if not ids:
        return {"ok": 0, "failed": 0, "files": []}
    master = STATE.master()
    done, failed = [], []
    for fid in ids:
        try:
            r = tree_sync(fid, master)
        except Exception as e:
            r = {"ok": False, "why": "%s: %s" % (type(e).__name__, str(e)[:60])}
        if r.get("ok"):
            done.append(fid)
        else:
            failed.append(fid)
            imp.setdefault("tree_errors", []).append("%s: %s" % (str(fid)[:8], r.get("why")))
    return {"ok": len(done), "failed": len(failed), "files": done}


def refresh_after_import(imp: dict) -> None:
    """批量导入 / 镜像之后刷新检索缓存（2026-09-25）。

    以前一律整表清空：镜像每几十秒跑一次，每次都把 11.5k 条向量缓存打掉，
    下一次查就要重读 ~700ms（实测）。现在：**没改动就什么都不做**；改了就只换动过的那几篇。
    """
    if not STATE._searcher:
        return
    if not (imp.get("imported") or imp.get("updated") or imp.get("secured")):
        return
    ids = list(imp.get("created_ids") or []) + list(imp.get("updated_ids") or []) \
        + list(imp.get("secured_ids") or [])
    if ids:
        STATE._searcher.refresh_files(ids)
    else:
        STATE._searcher.invalidate()


def embed_files(file_ids: list) -> dict:
    """重算这几篇的向量（改名/移动/转换/小节重链用），顺手把检索缓存跟上"""
    from retriever import VectorIndexer
    vi = VectorIndexer(STATE.db, STATE.searcher.emb)
    out = vi.index_files(file_ids)
    refresh_searcher(file_ids)
    return out


# ── 向量工人：把"嵌入"这种重活从请求线程挪走（2026-09-30 辞玖：「把向量等重工作分开线程」）──
#   为什么必须挪：嵌入是**同步**的（热态 2~5 秒/块），一篇几十块的大笔记就能把单线程的库占住几分钟
#   —— 期间页面、agent、插件全部排队（真库实测：148KB/60 块 = 310 秒，health 最长 120 秒无响应）。
#   现在的口径：**pending ≤ VEC_SYNC_CAP 就当场嵌**（小改动依旧"存完立刻能搜"），
#   更多就交给这里的后台线程，一批 VEC_BATCH 块、批间歇一下，慢慢补齐；
#   `/api/health` 的 `pending_vectors` 会自己往下掉，`vector_worker` 里能看到它干到哪了。
VEC_SYNC_CAP = int(os.environ.get("AI_NOTES_VEC_SYNC_CAP") or 4)
# 批次小一点（2）：一次嵌入的"在飞窗口"短，请求来的时候最多等它一小会儿（默认 2 块 ≈ 10 秒）
VEC_BATCH = max(1, int(os.environ.get("AI_NOTES_VEC_BATCH") or 2))
VEC_IDLE = float(os.environ.get("AI_NOTES_VEC_IDLE") or 3.0)
VEC_PAUSE = float(os.environ.get("AI_NOTES_VEC_PAUSE") or 0.3)
VEC_ON = (os.environ.get("AI_NOTES_VEC_WORKER") or "1").lower() not in ("0", "false", "no", "off")
VEC_WAKE = threading.Event()
# 正在处理的请求数：**请求在跑时工人不起新批次**（库是单线程 + 嵌入吃 CPU，抢起来两边都慢）
REQ_BUSY = [0]
REQ_BUSY_LOCK = threading.Lock()
VEC_STAT = {"indexed": 0, "batches": 0, "errors": 0, "last_error": "", "last_at": "",
            "running": False, "started": ""}
VEC_THREAD = None


def _vector_worker() -> None:
    """后台补向量。**自己的 sqlite 连接**（sqlite 不跨线程共用），写完只 bump 世代号，不碰检索缓存。"""
    import retriever as _rt
    db = emb = None
    while True:
        try:
            VEC_WAKE.wait(VEC_IDLE)
            VEC_WAKE.clear()
            if not VEC_ON:
                continue
            if db is None:
                db = Database()                     # 本线程自己的连接
            if emb is None:
                from embedder import get_embedder
                emb = get_embedder()
            vi = _rt.VectorIndexer(db, emb)
            if not vi.pending_chunks(limit=1):
                VEC_STAT["running"] = False
                continue
            with REQ_BUSY_LOCK:
                if REQ_BUSY[0] > 0:                 # 有请求在跑 → 这一轮先让路
                    time.sleep(0.2)
                    continue
            todo = vi.pending_chunks(limit=VEC_BATCH)
            if not todo:
                VEC_STAT["running"] = False
                continue
            VEC_STAT["running"] = True
            if not VEC_STAT["started"]:
                VEC_STAT["started"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            r = vi.index(batch_size=VEC_BATCH, limit=VEC_BATCH, progress=False)
            got = int(r.get("indexed") or 0)
            VEC_STAT["indexed"] += got
            VEC_STAT["batches"] += 1
            VEC_STAT["last_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            # 告诉请求线程"这几篇补过了"：它下次查只换这几篇（别整表重载）
            _rt.mark_dirty({x.get("file_id") for x in todo})
            _rt.bump_epoch()                        # 兜底：万一脏集合被丢了也能自愈
            if got:
                VEC_WAKE.set()                      # 还有活儿：下一轮立刻接着干
            time.sleep(VEC_PAUSE)                   # 让一让请求线程（CPU 也要留点）
        except Exception as e:
            VEC_STAT["errors"] += 1
            VEC_STAT["last_error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
            time.sleep(2.0)


def start_vector_worker() -> dict:
    global VEC_THREAD
    if not VEC_ON:
        return {"ok": False, "why": "AI_NOTES_VEC_WORKER=0"}
    if VEC_THREAD and VEC_THREAD.is_alive():
        return {"ok": True, "already": True}
    VEC_THREAD = threading.Thread(target=_vector_worker, name="vec-worker", daemon=True)
    VEC_THREAD.start()
    return {"ok": True, "started": True}


def embed_now(file_ids: list, cap: int | None = None, force_sync: bool = False) -> dict:
    """保存/新建后补向量：**小块当场嵌，大块交给后台线程**（2026-09-30 改）。

    小改动（pending ≤ VEC_SYNC_CAP，默认 4 块）仍然当场嵌 —— 存完就能搜，老习惯不变；
    超过就唤醒后台线程，由它一批批补（`pending_vectors` 会自己往下掉）。
    `force_sync=True` 留给维护路径（那些地方本来就要"顺手补干净"）；
    块数超过 cap 又不许后台补时，保留老语义：**一块都不嵌**，只报 pending。
    """
    cap = VEC_SYNC_CAP if cap is None else cap
    try:
        from retriever import VectorIndexer
        vi = VectorIndexer(STATE.db, STATE.searcher.emb)
        todo = vi.pending_of_files(file_ids)
        if not todo:
            return {"indexed": 0}
        if force_sync and len(todo) > cap:
            return {"indexed": 0, "pending": len(todo),
                    "note": "块数超过 %d，留给全量 reindex" % cap}
        if len(todo) > cap:
            VEC_WAKE.set()
            # pending 与 queued 同义（界面那句"还有 N 块语义索引待补建"认的是 pending）
            return {"indexed": 0, "queued": len(todo), "pending": len(todo),
                    "files": len(file_ids or []),
                    "note": "超过 %d 块（%d 块）→ 交给后台补向量线程，pending 会自己降"
                            % (cap, len(todo))}
        out = vi.index_files(file_ids)
        refresh_searcher(file_ids)          # 新块立刻可检索（以前这里不刷，是个隐性缺口）
        return out
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}


# ── 接口 = 唯一写入口：写完之后把文件落到库文件夹（辞玖 2026-09-25）────────────
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
         ".webp": "image/webp", ".bmp": "image/bmp", ".ico": "image/x-icon", ".svg": "image/svg+xml",
         ".avif": "image/avif", ".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
         ".md": "text/markdown; charset=utf-8", ".json": "application/json",
         ".mp3": "audio/mpeg", ".wav": "audio/wav", ".mp4": "video/mp4", ".zip": "application/zip"}


class BadRequest(ValueError):
    """入参不合法 → 400（不是 500，也不许把 Python 异常原文回给客户端）。"""


def as_int(value, default: int, lo=None, hi=None, name: str = "参数") -> int:
    """把查询串/请求体里的值变成整数，并按 lo/hi 钳住。

    为什么要有它（2026-09-30 体检）：路由里到处是裸 `int((q.get(x) or ["0"])[0])`，
    于是 `?limit=abc` → **500 + 原样回显** `ValueError: invalid literal for int()`；
    负数又没人管 —— SQLite 把 `LIMIT -1` 当"不限量"，`/api/rag/list?limit=-1` 真库实测 322KB。
    口径：非法 → 400（BadRequest）；越界 → 钳到边界（不报错，但绝不放大）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        v = int(default)
    else:
        try:
            v = int(str(value).strip())
        except (TypeError, ValueError):
            raise BadRequest("%s 必须是整数，收到 %r" % (name, str(value)[:40]))
    if lo is not None and v < lo:
        v = lo
    if hi is not None and v > hi:
        v = hi
    return v


def db_caps(row) -> dict:
    """这一篇支持什么（open/edit/search）—— 界面据此决定能不能看/改/检索"""
    try:
        import db as _db
        return _db.caps_for(row["kind"], row["ext"], row["name"])
    except Exception:
        return {}


def _mime_of(name: str) -> str:
    import os.path as _p
    return _MIME.get(_p.splitext(name or "")[1].lower(), "application/octet-stream")


def tree_rel(file_id: str) -> str:
    try:
        import tree_write as _tw
        return _tw.rel_of(STATE.db, file_id)
    except Exception:
        return ""


def _has_kdf() -> bool:
    """设过库密码没有（决定提示语是"先设密码"还是"先解锁一次"）"""
    try:
        return STATE.db.vault_kdf() is not None
    except Exception:
        return False


def vault_usable() -> bool:
    """锁着能不能用**公钥**加密：必须「设过库密码」且「有库公钥」两样都在。

    只看公钥不够 —— 取消密码时若没把密钥对清掉，一份过期的公钥会让加密"成功"，
    而私钥已经不在了，那篇就**永远解不开**（2026-10-01 被 smoke_lock 抓到的真 bug）。
    """
    try:
        return (STATE.db.vault_kdf() is not None) and bool(STATE.db.vault_pub())
    except Exception:
        return False


def tree_sync(file_id: str, master=None) -> dict:
    """接口写完 → 库自己把这一篇写进库文件夹（私密写密文）。镜像再把库文件夹同步去 vault。"""
    try:
        import tree_write as _tw
        return _tw.write_through(STATE.db, file_id,
                                 master if master is not None else STATE.master())
    except Exception as e:
        return {"ok": False, "why": "%s: %s" % (type(e).__name__, str(e)[:60])}


def tree_append(file_id: str, data: bytes) -> dict:
    """接口追加完 → 盘上那份也**按字节追加**（不是整份重写）"""
    try:
        import tree_write as _tw
        return _tw.append_through(STATE.db, file_id, data, STATE.master())
    except Exception as e:
        return {"ok": False, "why": "%s: %s" % (type(e).__name__, str(e)[:60])}


def tree_drop(rel: str) -> dict:
    try:
        import tree_write as _tw
        return _tw.remove_from_tree(STATE.db, rel)
    except Exception as e:
        return {"ok": False, "why": str(e)[:60]}


def prune_dirs(rels) -> int:
    """改名/移动后把库文件夹里空掉的旧目录收掉"""
    try:
        import tree_write as _tw
        return _tw.prune_empty_dirs(STATE.db, rels)
    except Exception:
        return 0


def tree_drop_dir(rel_dir: str) -> dict:
    """删文件夹时把库文件夹里那个目录也收掉（游离文件先记账再删）"""
    try:
        import tree_write as _tw
        return _tw.drop_dir(STATE.db, rel_dir, keep_rels=library_rel_set())
    except Exception as e:
        return {"ok": False, "why": str(e)[:80]}


def import_upload(b: dict) -> dict:
    """浏览器选好文件后按字节传进来（不再需要磁盘路径）。

    为什么要有它：服务端弹系统选择框那条路要看"库进程能不能在桌面上开窗口"，
    不可靠；浏览器自己的选择框一定看得见，还能多选 / 整目录选。文件到这里先落临时目录，
    再走**同一条 importer.import_path**（refs 转换、去重、权限、不覆盖同名等规则完全一致）。
    """
    import base64
    import tempfile
    import importer as _imp
    files = b.get("files") or []
    if not isinstance(files, list) or not files:
        return {"ok": False, "error": "files 必填：[{name, content_b64, rel_dir?}]"}
    under0 = str(b.get("under") or "").strip().replace("\\", "/").strip("/")
    rag_off = not bool(b.get("rag"))
    private = bool(b.get("private"))
    if private and STATE.master() is None:
        return {"ok": False, "error": "locked", "locked": True,
                "hint": "库未解锁：私密内容不落库（先在页面点 🔒 输库密码）"}
    res = {"imported": 0, "updated": 0, "secured": 0, "kept": 0, "skipped": 0, "failed": 0,
           "unsupported": 0, "refs_converted": 0, "files": [], "updated_ids": [],
           "created_ids": [], "secured_ids": [], "errors": [], "bytes": 0}
    with tempfile.TemporaryDirectory(prefix="ai-notes-up-") as td:
        for i, f in enumerate(files[:500]):
            name = os.path.basename(str(f.get("name") or "").replace("\\", "/")).strip()
            if not name or not f.get("content_b64"):
                res["failed"] += 1
                res["errors"].append("%s：缺 name / content_b64" % (name or i))
                continue
            try:
                data = base64.b64decode(f["content_b64"])
            except Exception:
                res["failed"] += 1
                res["errors"].append("%s：content_b64 解不开" % name)
                continue
            res["bytes"] += len(data)
            rel = str(f.get("rel_dir") or "").replace("\\", "/").strip("/")
            sub = "/".join(x for x in (under0, rel) if x)
            d = os.path.join(td, "f%d" % i)
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, name)                  # import_path 拿文件名当库内名 → 必须用原名
            with open(p, "wb") as fh:
                fh.write(data)
            try:
                r = _imp.import_path(STATE.db, p, under=sub, events=STATE.events,
                                     rag_off=rag_off, overwrite=True, convert_refs=True,
                                     visibility="private" if private else "internal",
                                     master=STATE.master() if private else None)
            except Exception as e:
                res["failed"] += 1
                res["errors"].append("%s：%s" % (name, str(e)[:80]))
                continue
            for k in ("imported", "updated", "secured", "kept", "skipped", "failed",
                      "unsupported", "refs_converted"):
                res[k] += int(r.get(k) or 0)
            res["updated_ids"].extend(r.get("updated_ids") or [])
            res["created_ids"].extend(r.get("created_ids") or [])
            res["secured_ids"].extend(r.get("secured_ids") or [])
            if r.get("files"):
                res["files"].append(r["files"][0])
            if r.get("error"):
                res["errors"].append("%s：%s" % (name, str(r["error"])[:80]))
    reindex_after_import(res)
    refresh_after_import(res)
    res["files"] = res["files"][:20]
    res["errors"] = res["errors"][:10]
    return {"ok": not res["errors"], **res, "rag_off": rag_off}


def pick_dir() -> str:
    """临时结果文件放哪（data/pick/<token>.json）"""
    p = os.path.join(getattr(STATE.db, "data_dir", "data"), "pick")
    os.makedirs(p, exist_ok=True)
    return p


def pick_start(kind: str = "file", title: str = "") -> dict:
    """弹系统「选文件 / 选文件夹」框 —— 另起一次性进程弹，库这边只拿 token。

    为什么不在请求线程里弹：库是**单线程**服务，模态窗口没关之前整库都被堵住
    （搜索/保存全卡、探针会以为库不健康）。所以：起进程 → 立刻回 token → 界面轮询结果。
    """
    import secrets
    kind = "dir" if str(kind).lower() in ("dir", "folder", "directory") else "file"
    token = secrets.token_hex(8)
    d = pick_dir()
    # 顺手清理：超过一天的旧结果文件（不然 data/pick 会攒一堆）
    try:
        cut = time.time() - 86400
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            if fn.endswith(".json") and os.path.getmtime(fp) < cut:
                os.remove(fp)
    except OSError:
        pass
    out = os.path.join(d, token + ".json")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools", "pick_path.py")
    cmd = [sys.executable, os.path.abspath(script), "--kind", kind, "--out", out]
    if title:
        cmd += ["--title", str(title)[:80]]
    try:
        subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         close_fds=True, cwd=os.path.dirname(os.path.abspath(script)))
    except Exception as e:
        return {"ok": False, "error": "起不了选择框进程：%s: %s" % (type(e).__name__, str(e)[:80])}
    return {"ok": True, "kind": kind, "token": token, "state": "pending"}


def pick_result(token: str = "") -> dict:
    """轮询：结果文件还没出现 = 人还在选（pending）"""
    token = str(token or "").strip()
    p = os.path.join(pick_dir(), token + ".json") if token else ""
    if not token or not os.path.isfile(p):
        return {"ok": True, "state": "pending"}
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        return {"ok": True, "state": "pending"}      # 还没写完（原子写，正常情况下不会读到半个）
    d["token"] = token
    return {"ok": True, **d}


def is_locked(row) -> bool:
    """私密篇在没解锁时不许读/写正文。

    这段判断原来在 6 处各写一遍（其中 4 处连响应体都一模一样）—— 2026-09-27 收到这里。
    """
    return bool(row) and row["visibility"] == "private" and STATE.master() is None


def locked_reply(row):
    """锁着 → 给出该发的响应体；没锁 → None"""
    return {"ok": False, "error": "locked", "locked": True} if is_locked(row) else None


def _looks_like_plaintext(raw: bytes) -> bool:
    """这份字节像不像**明文正文**？（不是就可能是密文 —— 绝不能拿它当明文重新加密）

    2026-10-15：`private_rekey` 原来从 (vault_dir, library_dir) 两个根找同名文件，
    读到就当明文重封。而**库树里那份是密文**（实测可打印 ASCII 约 40%）——
    拿到它就等于密文套密文，那篇**永久解不开**（既有修复方案 A7 条）。

    判据故意保守：宁可把可疑的判成"不是明文"（→ 跳过 + 报出来），
    也不能拿可疑字节去覆盖一篇好好的私密篇。
    """
    if not raw:
        return False
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False                       # 连 UTF-8 都不是 → 密文/BLOB
    if "\x00" in text:
        return False
    ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t")
    # 正文里允许极少量控制字符；超过 1% 就不像人写的东西
    return (ctrl / max(1, len(text))) < 0.01


def private_rekey(src_dir: str = "") -> dict:
    """一次性维修：把"密钥丢了、读不出来"的私密篇，用磁盘上的**明文**重新加密一遍。

    背景（2026-09-27 事故）：测试脚本把库的密钥材料清了，已加密的正文再也解不开（AES-GCM 换密钥必然验签失败）。
    明文还在 vault（`mirror_private` 前缀是跳过同步的，那边一直是明文），所以能救回来。
    必须在**已解锁**状态下调用（要有当前主密钥重新加密）。
    """
    import os as _os
    master = STATE.master()
    if master is None:
        return {"ok": False, "error": "locked", "locked": True,
                "hint": "先设/输库密码解锁，再跑这个维修"}
    roots = []
    if src_dir:
        roots.append(src_dir)
    else:
        c = mirror_conf()
        roots.append(c.get("vault_dir") or "")
        roots.append(c.get("library_dir") or "")
    done, failed, skipped = [], [], []
    for row in STATE.db.conn.execute(
            "SELECT id, name, folder_id FROM files WHERE visibility='private'").fetchall():
        fid = row["id"]
        try:
            STATE.db.get_content(fid, master)          # 能解开就跳过
            skipped.append(row["name"])
            continue
        except Exception:
            pass
        rel = tree_rel(fid)                            # 例：个人/私密/API密钥清单.md
        text = None
        for root in roots:
            if not root:
                continue
            p = _os.path.join(root, rel.replace("/", _os.sep))
            if _os.path.isfile(p):
                try:
                    with open(p, "rb") as f:
                        cand = f.read()
                except OSError:
                    continue
                # ⚠️ 不是明文就**不能要**（2026-10-15）：库树里那份是密文，
                # 拿它 rekey = 密文套密文 = 这篇永久解不开（A7）。
                # 继续试下一个根；都不行就在下面归入 failed（跳过 + 报出来）。
                if not _looks_like_plaintext(cand):
                    continue
                text = cand
                break
        if text is None:
            failed.append({"name": row["name"],
                           "why": "磁盘上找不到**明文**（找过 %s；同名文件若是密文会跳过 —— "
                                  "拿密文重封会让这篇永久解不开）" % rel})
            continue
        try:
            # 用当前主密钥**重新加密**（不能走 put_content：旧 key_ref 换密码后解不开）
            STATE.db.rekey_private(fid, text, master)
            STATE.indexer.reindex_file(fid)
            embed_now([fid], cap=4096, force_sync=True)   # 维护路径：顺手补干净，不留 pending
            tree_sync(fid, master)
            STATE.events.emit("note.rekeyed", {"file_id": fid, "name": row["name"]})
            done.append({"name": row["name"], "bytes": len(text)})
        except Exception as e:
            failed.append({"name": row["name"], "why": "%s: %s" % (type(e).__name__, str(e)[:80])})
    return {"ok": True, "rekeyed": len(done), "done": done, "skipped": skipped, "failed": failed}


def hide_private_entries(entries):
    """上锁时：引用 / 反向链接里**不露私密篇的名字**，只留一个占位（2026-09-27）"""
    out = []
    for e in entries or []:
        e = dict(e)
        if is_locked(STATE.db.file_by_id(e.get("id") or "")):
            e["name"] = "（私密·未解锁）"
            e["hidden"] = True
            e["anchor"] = ""
            e["section"] = ""
            e.pop("section_heading", None)
        out.append(e)
    return out


def hide_private_groups(groups):
    out = []
    for g in groups or []:
        g = dict(g)
        g["refs"] = hide_private_entries(g.get("refs"))
        out.append(g)
    return out


def hidden_private_count() -> int:
    """上锁时被隐藏的私密篇数（解锁时返回 0）"""
    if STATE.master() is not None:
        return 0
    try:
        return int(STATE.db.conn.execute(
            "SELECT COUNT(*) c FROM files WHERE visibility='private'").fetchone()["c"])
    except Exception:
        return 0


def library_rel_set() -> set:
    """库里所有文件**应该**在库文件夹里的相对路径（与 tree_write.rel_of 同口径）"""
    out = set()
    for r in STATE.db.conn.execute("SELECT id FROM files"):
        rel = tree_rel(r["id"])
        if rel:
            out.add(rel)
    return out


def after_rewrite(ids, reason: str, extra_embed=None) -> dict:
    """"某几篇的正文在库里被改过"之后的统一收尾：
    重建索引 → **写回库文件夹** → 补向量 → 发事件。

    改名 / 移动 / 文件夹改名 / 小节修正 都走它。2026-09-27 审计：这四步原来在四处各写一遍，
    而且**都漏了"落盘"** —— 于是"引用方的正文被改写了，磁盘那份还是旧的"，镜像只比 mtime，
    永远补不上（§九十三 的 A）。这里既补上落盘，也把重复的四段收成一处。
    """
    ids = [x for x in dict.fromkeys(ids or []) if x]
    tree, errs = {}, []
    for rid in ids:
        try:
            STATE.indexer.reindex_file(rid)
        except Exception as e:
            errs.append("%s reindex: %s" % (rid[:8], type(e).__name__))
        tree[rid] = tree_sync(rid)
        row = STATE.db.file_by_id(rid)
        STATE.events.emit("note.updated", {"file_id": rid, "name": (row["name"] if row else ""),
                                           "reason": reason, "changed_chars": 0})
    vec_ids = [x for x in dict.fromkeys(list(ids) + list(extra_embed or [])) if x]
    vec = None
    if vec_ids:
        try:
            vec = embed_files(vec_ids)
        except Exception as e:
            vec = {"error": str(e)[:120]}
    out = {"files": len(ids), "tree": tree, "vectors": vec}
    if errs:
        out["errors"] = errs
    return out


# ── 库文件夹 ⇄ vault 镜像（辞玖 2026-09-22 定：暂时保持数据同步，谁新谁赢）──────
# ── PDF 引擎（src/pdfeng.py）在服务侧的入口 ───────────────────────────
#   引擎只认 bytes；前像 / 事件 / 落库文件夹这些"库的规矩"在这一层做（2026-09-30）。
def _pdf_open(db, fid, master):
    """打开这一篇的 PDF（引擎版）：返回 (doc, 错误串)"""
    import pdfeng
    row = db.file_by_id(fid)
    if row is None:
        return None, "no such file"
    if (row["ext"] or "").lower() != "pdf":
        return None, "不是 PDF（%s）" % (row["ext"] or row["kind"])
    try:
        data = db.get_content(fid, master)
    except Exception as e:
        return None, str(e)[:80]
    return pdfeng.open_doc(data=data)


def pdf_pages(db, fid, master) -> dict:
    """PDF 页清单（阅读器的页码条；也是 PageOp 的入口数据）"""
    doc, err = _pdf_open(db, fid, master)
    if doc is None:
        return {"ok": False, "error": err}
    try:
        import pdfeng
        return pdfeng.info(doc)
    finally:
        doc.close()


def pdf_page_png(db, fid, master, n: int, width: int = 1200) -> dict:
    """第 n 页 → PNG"""
    doc, err = _pdf_open(db, fid, master)
    if doc is None:
        return {"error": err}
    try:
        import pdfeng
        return pdfeng.render_page(doc, n, width)
    finally:
        doc.close()


def pdf_apply(db, fid, mutator, what: str = "pdf.edit") -> dict:
    """**PDF 引擎写回**：打开 → 改 → 存 → 落库（前像/重解析/落盘/事件）。

    改 PDF 是"动主人的文件"，所以规矩和 writeback 一样：先留前像、失败不落盘、改完重新解析。
    mutator(doc) 返回 dict；里面 ok=False 就整篇不动（改到一半失败也不写回去）。
    """
    import pdfeng
    row = db.file_by_id(fid)
    if row is None:
        return {"ok": False, "error": "no such file"}
    doc, err = _pdf_open(db, fid, STATE.master())
    if doc is None:
        return {"ok": False, "error": err}
    try:
        res = mutator(doc) or {}
        if res.get("ok") is False:
            return res
        out = pdfeng.save_bytes(doc, incremental=bool(res.get("incremental", True)))
        data = out.get("data") or b""
        if not data:
            return {"ok": False, "error": "存出来的 PDF 是空的，没写回去"}
        before = len(db.get_content(fid, STATE.master()))
        import beforeimage as _bi
        img = _bi.save(db, fid, "overwrite", writer=STATE.events.writer, master=STATE.master())
        db.put_content(fid, data, STATE.master())
        res.update({"ok": True, "bytes": len(data), "before_bytes": before, "mode": out.get("mode"),
                    "before_image": img.get("id") if img.get("ok") else None})
        if STATE.indexer:
            try:
                STATE.indexer.reindex_file(fid)
            except Exception as e:
                res["reindex_error"] = str(e)[:80]
        try:
            res["tree"] = tree_sync(fid, STATE.master())
        except Exception as e:
            res["tree_error"] = str(e)[:80]
        STATE.events.emit("pdf.edit", {"file_id": fid, "bytes": len(data), "what": what,
                                       "before_image": res.get("before_image")})
        return res
    finally:
        doc.close()


def pdf_find(db, fid, master, needle: str, cap: int = 200) -> dict:
    """PDF 全文查找：返回每一处命中的页码 + 矩形（PDF 坐标）+ 片段，供阅读器跳页高亮"""
    doc, err = _pdf_open(db, fid, master)
    if doc is None:
        return {"ok": False, "error": err}
    try:
        hits = []
        low = needle.lower()
        for i in range(doc.page_count):
            pg = doc.load_page(i)
            rects = pg.search_for(needle) or []
            if not rects and low != needle:
                rects = pg.search_for(low) or []
            if not rects:
                continue
            text = (pg.get_text("text") or "").replace("\n", " ")
            at = text.lower().find(low)
            snip = text[max(0, at - 40): at + len(needle) + 60] if at >= 0 else text[:100]
            for r in rects:
                hits.append({"page": i + 1, "rect": [round(r.x0, 1), round(r.y0, 1),
                                                     round(r.x1, 1), round(r.y1, 1)],
                             "snippet": snip.strip()})
                if len(hits) >= cap:
                    break
            if len(hits) >= cap:
                break
        return {"ok": True, "q": needle, "hits": hits, "count": len(hits),
                "pages": doc.page_count}
    finally:
        doc.close()


def docx_apply(db, fid, mutator, what: str = "docx.edit", allow_shrink: bool = False) -> dict:
    """**DOCX 引擎写回**：读 → 改 OOXML → 存 → 落库（前像/重解析/落库文件夹/事件）。

    规矩跟 pdf_apply / writeback 一样：改主人的文件，先留前像、失败不落盘、改完重新解析。
    多一道**缩短闸**：新正文比原文短一半以上、又没带 allow_shrink → 拒绝（防手滑清空）。
    """
    import docxeng
    row = db.file_by_id(fid)
    if row is None:
        return {"ok": False, "error": "no such file"}
    if (row["ext"] or "").lower() != "docx":
        return {"ok": False, "error": "这篇不是 docx（%s）；odt/rtf/html/epub 请走文本写回"
                                      % (row["ext"] or row["kind"])}
    raw = db.get_content(fid, STATE.master())
    doc, err = docxeng.open_doc(data=raw)
    if doc is None:
        return {"ok": False, "error": err}
    res = mutator(doc) or {}
    if res.get("ok") is False:
        return res
    out = docxeng.save_bytes(doc)
    if out.get("ok") is False:
        return out
    data = out.get("data") or b""
    if not data:
        return {"ok": False, "error": "存出来的 docx 是空的，没写回去"}
    try:
        old = (db.derived_for(fid, raw).get("text") or "")
    except Exception:
        old = ""
    ob, nb = len(old.strip()), int(docxeng.info(doc).get("chars") or 0)
    if (ob >= 400) and nb < ob * 0.5 and not allow_shrink:
        return {"ok": False, "shrink_refused": True, "before_chars": ob, "after_chars": nb,
                "error": "改完的正文比原来短了一半以上（%d → %d 字）；确认要这么改再加 allow_shrink=true"
                         % (ob, nb)}
    import beforeimage as _bi
    img = _bi.save(db, fid, "overwrite", writer=STATE.events.writer, master=STATE.master())
    db.put_content(fid, data, STATE.master())
    res.update({"ok": True, "file_id": fid, "bytes": len(data), "before_bytes": len(raw),
                "before_image": img.get("id") if img.get("ok") else None,
                "engine": "docxeng"})
    if STATE.indexer:
        try:
            STATE.indexer.reindex_file(fid)
        except Exception as e:
            res["reindex_error"] = str(e)[:80]
    try:
        res["tree"] = tree_sync(fid, STATE.master())
        refresh_searcher([fid])
    except Exception as e:
        res["tree_error"] = str(e)[:80]
    STATE.events.emit("docx.edit", {"file_id": fid, "bytes": len(data), "what": what,
                                    "before_image": res.get("before_image")})
    return res


def doc_writeback(db, fid, md_text: str, allow_shrink: bool = False) -> dict:
    """文档编辑器保存：把编辑器里的 Markdown **按原格式重新生成**，写回同一篇。

    · 先留前像（可回退）；· 缩短闸（新正文不足旧解析文本 50% 且没带 allow_shrink → 拒绝）；
    · 写完重解析 + 落库文件夹（tree_sync）。
    """
    import docconvert
    import tempfile
    row = db.file_by_id(fid)
    ext = (row["ext"] or "").lower()
    if ext not in ("docx", "odt", "rtf", "html", "epub", "pdf"):
        return {"ok": False, "error": "这种格式还不能写回（%s）" % (ext or "?")}
    raw = db.get_content(fid, STATE.master())
    try:
        old = (db.derived_for(fid, raw).get("text") or "")
    except Exception:
        old = ""
    ob, nb = len(old.strip()), len(md_text.strip())
    if (ob >= 400) and nb < ob * 0.5 and not allow_shrink:
        return {"ok": False, "shrink_refused": True, "before_chars": ob, "after_chars": nb,
                "error": "新正文比原文短了一半以上（%d → %d 字）；确认要这么改再加 allow_shrink=true"
                         % (ob, nb)}
    with tempfile.TemporaryDirectory(prefix="ai-notes-wb-") as td:
        out = os.path.join(td, "out." + ext)
        r = docconvert.to_pdf(md_text, out) if ext == "pdf" else docconvert.write_md(md_text, ext, out)
        if r.get("error"):
            return {"ok": False, "error": "生成 .%s 失败：%s" % (ext, r["error"])}
        try:
            check = docconvert.verify(out, ext, md_text)      # 结构/汉字数对账（与「转换导出」同一套）
        except Exception as e:
            check = {"error": str(e)[:80]}
        data = open(out, "rb").read()
    if not data:
        return {"ok": False, "error": "生成出来的文件是空的，没写回去"}
    import beforeimage as _bi
    img = _bi.save(db, fid, "overwrite", writer=STATE.events.writer, master=STATE.master())
    db.put_content(fid, data, STATE.master())
    res = {"ok": True, "file_id": fid, "ext": ext, "bytes": len(data),
           "before_image": img.get("id") if img.get("ok") else None,
           "verify": check,
           "engine": r.get("engine") or ("wps" if ext == "pdf" else "pandoc")}
    if STATE.indexer:
        try:
            STATE.indexer.reindex_file(fid)            # 重新解析（解析缓存按 source_hash 自然失效）
        except Exception as e:
            res["reindex_error"] = str(e)[:80]
    try:
        res["tree"] = tree_sync(fid, STATE.master())
        refresh_searcher([fid])
    except Exception as e:
        res["tree_error"] = str(e)[:80]
    STATE.events.emit("note.writeback", {"file_id": fid, "ext": ext, "bytes": len(data),
                                         "before_image": res.get("before_image")})
    return res


_MIRROR_LAST: dict = {}


def _priv_default_dir() -> str:
    """私密文件夹在库内的相对路径（唯一来源：priv_folder.PRIV_DIR_REL）"""
    try:
        import priv_folder as _pf
        return _pf.PRIV_DIR_REL
    except Exception:
        return "个人/私密"


def mirror_conf(db=None) -> dict:
    """镜像配置（存在 manifest 里）：库文件夹 / vault / 忽略前缀 / 私密前缀"""
    m = (db or STATE.db).manifest()
    return {"library_dir": m.get("library_dir") or "",
            "vault_dir": m.get("vault_dir") or "",
            # 镜像管线总开关：正式启用库之后不再维持它（辞玖 2026-09-27）；
            # 库这边是唯一权威 —— 关掉后投递箱那边的调用只会拿到"已关闭"，不会搬文件。
            "enabled": (m.get("mirror_enabled") or "1") not in ("0", "false", "off", "no"),
            "ignore": [x.strip() for x in (m.get("mirror_ignore") or "tmp").split(",") if x.strip()],
            # 私密前缀的默认值从 priv_folder 派生 —— 2026-10-15：清掉重复的路径字符串。
            # 实测这一项已经是**死配置**（mirror_enabled=False、/api/mirror/sync 回 410），
            # 唯一消费者是设置页那两个回显接口；留着只为不破坏配置往返。
            # 「私密文件夹在哪」全世界只该有一处定义 → priv_folder.PRIV_DIR_REL。
            "private": [x.strip() for x in
                        (m.get("mirror_private") or _priv_default_dir()).split(",")
                        if x.strip()],
            # 真相保护：只追加目录（永不覆盖）+ 覆盖前备份到哪
            "append_only": [x.strip() for x in
                            (m.get("mirror_append_only") or "日志/reasonix-raw,记忆/热缓存.md")
                            .split(",") if x.strip()],
            "backup_root": m.get("mirror_backup_root") or r"D:\枢阁\.镜像备份",
            "last": _MIRROR_LAST or None}


def _reveal_rows(master) -> dict:
    """解锁后"露出来的私密篇"：**package 模式下以加密包为准**。

    为什么不能只回 `file_rows("private")`：包是**读**的真相源 ——
    包里有、库里还没有对应行的条目（比如刚从外面塞进包的），只按库查就漏了。

    什么时候不生效：`priv_mode != package` 时**原样返回库那套**，
    行为与今天逐字节一致（这个函数本身不改变任何现有行为）。

    返回 {"rows": [...], "ambiguous": {...}, "unmatched": [...]}
    """
    try:
        rows = STATE.db.file_rows("private")
    except Exception:
        rows = []
    try:
        if STATE.db.priv_effective_mode() != "package":
            return {"rows": rows, "ambiguous": {}, "unmatched": []}
        import priv_folder as _pf
        r = _pf.pack_tree_rows(STATE.db, master, STATE.db.vault_priv(master))
        pack_rows = r.get("rows") or []
        # 合并：库那套为底，包内条目按 id 覆盖/补齐（包是真相源）
        by_id = {}
        for x in rows:
            by_id[x.get("id") or ("name:" + (x.get("name") or ""))] = x
        for x in pack_rows:
            k = x.get("id") or ("name:" + (x.get("name") or ""))
            by_id[k] = x
        return {"rows": list(by_id.values()),
                "ambiguous": r.get("ambiguous") or {},
                "unmatched": r.get("unmatched") or []}
    except Exception as e:
        # 包读不出来**不能把解锁搞失败**（解锁本身已成功）——
        # 但要留痕，别让它静默退化成"只回库那套"而没人知道
        try:
            print("[unlock] 读包失败，revealed 退回库那套：%s: %s"
                  % (type(e).__name__, str(e)[:60]), flush=True)
        except Exception:
            pass
        return {"rows": rows, "ambiguous": {}, "unmatched": ["<读包失败：%s>" % type(e).__name__]}


def reindex_after_import(res: dict) -> dict:
    """导入后把"覆盖更新 / 补了权限"过的那几篇的派生层补上。

    /api/import 与镜像同步共用这一段（2026-09-22 抽出来）——否则"正文换了、索引没换"这种
    错位会在两条路上各犯一次。单次最多跟进 200 篇，多的如实报 left 给全量 reindex 收尾。
    """
    created = list(dict.fromkeys(res.get("created_ids") or []))
    ids = list(dict.fromkeys(list(res.get("updated_ids") or []) + list(res.get("secured_ids") or [])))
    res.pop("updated_ids", None)
    res.pop("secured_ids", None)
    # 新导入的篇：导入器只写正文，**一个 @提及边都没有** → 这里补建
    # （2026-09-27 审计 D：不建边的话预览里点不动、反向链接也查不到）
    for fid in created[:200]:
        try:
            txt = STATE.db.get_content(fid,
                                       STATE.master()).decode("utf-8", "replace")
            pairs, _miss = resolve_mentions(txt, STATE.db.ref_resolver())
            STATE.db.set_mentions(fid, pairs)
        except Exception as e:
            res.setdefault("mention_errors", []).append("%s: %s" % (type(e).__name__, str(e)[:80]))
    if not ids:
        return res
    head, chunks, vec = ids[:200], 0, {"indexed": 0}
    for fid in head:
        try:
            chunks += int((STATE.indexer.reindex_file(fid) or {}).get("chunks") or 0)
        except Exception as e:
            res.setdefault("reindex_errors", []).append("%s: %s" % (type(e).__name__, str(e)[:80]))
            continue
        try:
            # 正文换过了 → @提及边也得重算（导入这条路原来不重算，会留"陈边"：
            # 预览里点不动、反向链接却还显示）2026-09-27 审计 D
            txt = STATE.db.get_content(fid, STATE.master()).decode("utf-8", "replace")
            pairs, _miss = resolve_mentions(txt, STATE.db.ref_resolver())
            STATE.db.set_mentions(fid, pairs)
        except Exception as e:
            res.setdefault("mention_errors", []).append("%s: %s" % (type(e).__name__, str(e)[:80]))
        r = embed_now([fid], cap=4096, force_sync=True)
        vec["indexed"] += int(r.get("indexed") or 0)
        if r.get("pending"):
            vec["pending"] = vec.get("pending", 0) + int(r["pending"])
        if r.get("error"):
            vec.setdefault("errors", []).append(r["error"])
    res["reindexed"] = {"files": len(head), "chunks": chunks, "vectors": vec,
                        "left": max(0, len(ids) - len(head))}
    return res


def content_by_path(path: str, max_chars: int = 0) -> dict:
    """按库内路径取正文：vault/项目/x.md → {found, name, chars, locked, content}

    老写法（Vault 相对路径，如 项目/x.md）也认：自动补 vault/ 前缀再找一次。
    取不到就 found=false（不抛），让调用方决定降级。
    """
    p = str(path or "").replace("\\", "/").strip("/")
    if not p:
        return {"path": path, "found": False, "error": "empty path"}
    for cand in (p, "vault/" + p):
        d, _, name = cand.rpartition("/")
        fid = STATE.db.folder_by_path(d) if d else STATE.db.root_folder_id()
        if not fid:
            continue
        row = STATE.db.find_file(fid, name)
        if row is None:
            continue
        master = STATE.master()
        locked = (row["visibility"] == "private" and master is None)
        item = {"path": cand, "found": True, "id": row["id"], "name": row["name"],
                "chars": row["size"], "locked": locked, "content": ""}
        if locked:
            return item
        try:
            text = STATE.db.get_content(row["id"], master).decode("utf-8", "replace")
        except Exception as e:
            item["found"] = False
            item["error"] = "%s: %s" % (type(e).__name__, e)
            return item
        item["chars"] = len(text)
        if max_chars and len(text) > max_chars:
            item["content"] = text[:max_chars]
            item["truncated"] = True
        else:
            item["content"] = text
        return item
    return {"path": path, "found": False, "error": "not in library"}


def changed_chars(old: str, new: str) -> int:
    """改动字符数的上界：去掉公共前后缀后剩下的长度"""
    n = min(len(old), len(new))
    i = 0
    while i < n and old[i] == new[i]:
        i += 1
    j = 0
    while j < n - i and old[len(old) - 1 - j] == new[len(new) - 1 - j]:
        j += 1
    return max(len(old), len(new)) - i - j


# ── 极短 TTL 缓存（2026-10-12）───────────────────────────────────────────────
# /api/tree（554KB/225ms）与 /api/health（~115ms）会被反复拉：控制台每 3 秒探一次状态、
# 切页签、库页刷新。这里给它们一个 2 秒的缓存，**任何写请求都立刻清掉** ——
# 刚写完立刻读也读得到新数据，只是重复读不再重算。
_CACHE = {}
_CACHE_TTL = 5.0


def _cache_get(key):
    hit = _CACHE.get(key)
    if not hit:
        return None
    at, val = hit
    if time.time() - at > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return val


def _cache_put(key, val):
    _CACHE[key] = (time.time(), val)
    return val


def _cache_clear():
    _CACHE.clear()


class State:
    """进程级状态：库 + 懒加载的索引器/检索器 + 会话主密钥"""

    def __init__(self, data_dir: str):
        # 并发之后：db 是**线程安全的门面**（谁在哪个线程调，就用那个线程的连接）
        self.db = ThreadDB(data_dir)
        self._indexer = None
        self._searcher = None
        self._lock = threading.Lock()
        self._lazy_lock = threading.Lock()      # 懒加载单例的创建锁（并发首次触碰会建两个）
        self.events = EventLog(self.db)
        self._master: bytes | None = None          # 只在内存里
        self._seen = 0.0
        # 会话级解锁**不自动过期**（辞玖 2026-09-28："900 秒太短，先做手动"）：
        # 只有自己点 🔒 才上锁。要恢复闲置超时：设 AI_NOTES_LOCK_TIMEOUT=<秒>。
        # 0 = 不自动上锁（master() 里那句 `and self.lock_timeout` 就是为它留的）。
        self.lock_timeout = int(os.environ.get("AI_NOTES_LOCK_TIMEOUT") or 0)

    def master(self) -> bytes | None:
        """当前会话主密钥（只有 lock_timeout>0 时，闲置超时才算已上锁）"""
        if self._master and self.lock_timeout and (time.time() - self._seen) > self.lock_timeout:
            self.lock()
        if self._master:
            self._seen = time.time()
        return self._master

    def unlock(self, master: bytes) -> None:
        self._master = master
        self._seen = time.time()

    def lock(self) -> None:
        self._master = None

    def lock_info(self) -> dict:
        # 超时了就**如实报"已上锁"**（辞玖 2026-09-28）：
        # 这里原来直接读 self._master，从不做超时判定 → 闲置超时后仍报 unlocked:true，
        # 而 expires_in 已经是 0（自相矛盾），页面按钮就一直在骗人。
        # 注意：这里只判定、**不续期** —— 否则谁轮询 /api/lock 都能把会话无限续命。
        if self._master and self.lock_timeout and (time.time() - self._seen) > self.lock_timeout:
            self.lock()
        left = 0
        if self._master:
            left = max(0, int(self.lock_timeout - (time.time() - self._seen)))
        conf = self.db.vault_kdf() is not None
        try:
            pub_ready = bool(self.db.vault_pub())     # 有公钥 = 锁着也能设私密（老库第一次解锁时补）
        except Exception:
            pub_ready = False
        return {"configured": conf,
                "pub_ready": pub_ready,
                "unlocked": self._master is not None,
                "expires_in": left,
                # 库里有几篇私密篇：界面用它提示"取消密码会影响几篇"（2026-09-29）
                "private_files": (len(self.db.private_files()) if conf else 0)}

    @property
    def indexer(self):
        with self._lazy_lock:
            if self._indexer is None:
                from indexer import Indexer
                self._indexer = Indexer(self.db)
                self._indexer.master = self.master
                self._indexer.events = self.events
        return self._indexer

    @property
    def searcher(self):
        with self._lazy_lock:
            if self._searcher is None:
                from retriever import HybridSearcher
                self._searcher = HybridSearcher(self.db)  # 内部 Embedder 也是懒加载
        return self._searcher


STATE: State | None = None


def get_expander():
    """惰性建 ArticleExpander（精排模型与检索共用同一个单例，不会重复加载）"""
    global _EXPANDER
    if _EXPANDER is None:
        import article
        _EXPANDER = article.ArticleExpander(STATE.db, master=STATE.master)
    return _EXPANDER


# 只认 POST 的三条（原来都是 GET，但都会写东西/装定时器）——2026-09-30 体检
#   为什么单列出来而不是"GET 里也留个桩"：api_spec 是从源码里抠"实现了哪些 GET"的，
#   留桩会让它以为这三条还有 GET 路由。
POST_ONLY = {
    "/api/debug/stack-dump": ("抓栈会写日志 + 装定时器", '{"after":60,"repeat":1}'),
    "/api/maintenance/private-rekey": ("重新加密是写操作", '{"src":""}'),
    "/api/maintenance/frontmatter-backfill": ("会重解析并写回全库文件头", '{"limit":100} 或 {"all":true}'),
}

_EXPANDER = None


class Handler(BaseHTTPRequestHandler):
    server_version = "core-notes/0.1"

    # ── 工具 ──────────────────────────────────────────────────
    def parse_request(self):
        """写完请求行就决定：这是"读"还是"写"。

        写（POST/PUT/DELETE）**拿住全局写锁**，一直握到这个请求处理完 —— 于是写与写之间
        仍然是一件件来（跟单线程时代一样安全），而读（GET）完全不排队。
        """
        ok = super().parse_request()
        # `/api/batch` 是**调度者**：它自己不碰库，只把活派给单条路由（那些请求各自拿锁）。
        # 它要是也拿锁，就会"拿着锁等自己派出去的请求拿锁" → 死锁（2026-10-04 冒烟卡死抓到）。
        if ok and self.command in ("POST", "PUT", "DELETE") and self._batcher_path() != "/api/batch":
            WRITE_LOCK.acquire()
            self._write_held = True
        return ok

    def _batcher_path(self):
        try:
            return urlparse(self.path).path
        except Exception:
            return ""

    def handle_one_request(self):
        """每个请求前后给向量工人打招呼（见 REQ_BUSY）：重活别和请求抢 CPU。"""
        with REQ_BUSY_LOCK:
            REQ_BUSY[0] += 1
        try:
            return super().handle_one_request()
        finally:
            if getattr(self, "_write_held", False):     # 兜底：异常路径也别把写锁漏在手里
                self._write_held = False
                try:
                    WRITE_LOCK.release()
                except RuntimeError:
                    pass
            with REQ_BUSY_LOCK:
                REQ_BUSY[0] -= 1

    def _cors(self):
        """本机来源允许跨源（本机单人应用）"""
        origin = self.headers.get("Origin") or ""
        if origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Vary", "Origin")

    def _send(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        """请求体（**带缓存**：判写入者时要先看一眼 body，路由再读不能读到空）"""
        if getattr(self, "_body_cache", None) is not None:
            return self._body_cache
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        try:
            d = json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            d = {}
        self._body_cache = d
        return d

    def _mark_writer(self) -> str:
        """这次请求是谁发的？（辞玖 2026-09-24 定：标签跟**操作**捆绑，agent 走接口默认留）

        判定顺序：X-Writer 头 → body.writer → 页面（Origin 是本库自己）算 user → 其余算 auto:<UA>。
        插件/脚本带上 X-Writer 就能自报家门（投递箱=inbox、镜像=mirror、脚本=tool:xxx）。
        """
        w = (self.headers.get("X-Writer") or "").strip()
        if not w:
            w = str((self._body() or {}).get("writer") or "").strip()
        if not w:
            org = self.headers.get("Origin") or ""
            w = "user" if ":8848" in org else ("auto:" + ((self.headers.get("User-Agent")
                                                           or "unknown").split(" ")[0][:40]))
        try:
            STATE.events.writer = w
        except Exception:
            pass
        return w

    def _serve_ui(self):
        """托管界面（web/index.html）—— 产品的展示前端"""
        import os.path as _p
        p = _p.join(_p.dirname(_p.abspath(__file__)), "..", "web", "index.html")
        try:
            body = open(p, "rb").read()
        except Exception as e:
            return self._send({"ok": False, "error": "界面文件缺失: %s" % e}, 500)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if os.environ.get("AI_NOTES_API_QUIET") != "1":
            sys.stderr.write("[api] %s\n" % (fmt % args))

    # ── 路由 ──────────────────────────────────────────────────
    def do_GET(self):
        self._mark_writer()
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in POST_ONLY:                  # 这三条只认 POST（GET 会写东西）→ 405 + 怎么调
                why, body = POST_ONLY[u.path]
                return self._send({"ok": False, "error": "这个接口只认 POST（%s）" % why,
                                   "hint": "POST %s %s" % (u.path, body)}, 405)
            if u.path in ("/", "/index.html"):
                return self._serve_ui()
            if u.path == "/api/pick":
                return self._send(pick_result((q.get("token") or [""])[0]))
            if u.path == "/api/health":
                _hit = _cache_get("health")
                if _hit is not None:
                    return self._send(_hit)
                # 语义索引缺口要露出来：锁/解锁、改内容之后可能要补建 vectors
                from embedder import get_embedder
                from embedinput import spec_model
                pend = STATE.db.conn.execute(
                    "SELECT COUNT(*) c FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
                    " WHERE v.chunk_id IS NULL OR v.model<>?",
                    (spec_model(get_embedder().model_name),)).fetchone()["c"]
                # manifest 要摘掉密钥材料再给：kdf_salt + verify_blob + kdf 参数
                # 足够拿去**离线猜库密码**（2026-09-30 体检）。口径与 export.py 一致。
                man = {k: v for k, v in (STATE.db.manifest() or {}).items()
                       if not k.startswith("kdf_") and k != "verify_blob"}
                return self._send(_cache_put("health", {
                    "ok": True, "stats": STATE.db.stats(),
                    "pending_vectors": pend,
                    "hidden_private": hidden_private_count(),
                    "vector_worker": dict(VEC_STAT, pending=pend, on=VEC_ON,
                                          sync_cap=VEC_SYNC_CAP, batch=VEC_BATCH),
                    "manifest": man}))
            if u.path == "/api/convert/engines":
                # 转换页面要显示"能用哪些引擎"（pandoc 版本 / WPS 可用否 / 能写出哪些格式）
                import docconvert
                import doctext
                e = docconvert.engines()
                return self._send({"ok": True, "engines": {
                    "pandoc": e.get("pandoc") or "",
                    "pandoc_version": e.get("pandoc_version") or "",
                    "wps": bool(e.get("wps")),
                    "write_exts": list(docconvert.WRITE_EXTS),
                    "read_exts": sorted(doctext.supported().keys())}})
            if u.path == "/api/tree":
                _locked = STATE.master() is None
                _ck = ("tree", _locked)
                _hit = _cache_get(_ck)
                if _hit is None:
                    _hit = _cache_put(_ck, {"ok": True,
                                            "tree": STATE.db.tree(hide_private=_locked),
                                            "hidden_private": hidden_private_count()})
                return self._send(_hit)
            if u.path.startswith("/api/files/") and u.path.endswith("/text") \
                    and not u.path.endswith("/pdf/text"):     # PDF 文字层走引擎那条（2026-09-30）
                fid = u.path[len("/api/files/"):-len("/text")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                refresh = (q.get("refresh") or [""])[0] not in ("", "0", "false")
                raw = STATE.db.get_content(fid, STATE.master())
                d = STATE.db.derived_for(fid, raw, refresh=refresh)
                return self._send({"ok": True, "format": d.get("format"),
                                   "parser": d.get("parser"), "error": d.get("error") or "",
                                   "chars": len(d.get("text") or ""), "meta": d.get("meta") or {},
                                   "cached": bool(d.get("cached")), "text": d.get("text") or ""})
            if u.path.startswith("/api/files/") and u.path.endswith("/backlinks"):
                fid = u.path[len("/api/files/"):-len("/backlinks")]
                if STATE.db.file_by_id(fid) is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                sec = (q.get("section") or [""])[0]
                return self._send({"ok": True, "section": sec,
                                   "backlinks": STATE.db.backlinks(fid, section=sec),
                                   "groups": STATE.db.backlink_groups(fid)})
            if u.path.startswith("/api/files/") and u.path.endswith("/article"):
                fid = u.path[len("/api/files/"):-len("/article")]
                if STATE.db.file_by_id(fid) is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                focus = (q.get("focus") or [""])[0]
                tail = focus.rsplit(":", 1)[-1]
                seqs = [int(tail)] if tail.isdigit() else []
                art = get_expander().one(
                    fid, query=(q.get("q") or [""])[0], focus_seqs=seqs,
                    include_sensitive=STATE.master() is not None,
                    max_chars=as_int((q.get("max") or ["0"])[0], 0, lo=0, name="max"),
                    section_max=as_int((q.get("section_max") or ["0"])[0], 0, lo=0, name="section_max"),
                    section=(q.get("section") or [""])[0])
                import article as _art
                return self._send({"ok": True, "article": art,
                                   "article_max": _art.ARTICLE_MAX,
                                   "section_max": _art.SECTION_MAX})
            if u.path.startswith("/api/files/") and u.path.endswith("/blocks"):
                fid = u.path[len("/api/files/"):-len("/blocks")]
                if STATE.db.file_by_id(fid) is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                blocks = STATE.db.blocks_of(fid)
                pg = as_int((q.get("page") or ["0"])[0], 0, lo=0, name="page")
                if pg:
                    blocks = [b for b in blocks if b.get("page_no") == pg]
                bt = (q.get("type") or [""])[0]
                if bt:
                    blocks = [b for b in blocks if b.get("type") == bt]
                return self._send({"ok": True, "stats": STATE.db.block_stats(fid),
                                   "count": len(blocks), "blocks": blocks})
            if u.path.startswith("/api/files/") and u.path.endswith("/pages"):
                # PDF 页清单：阅读器的页码/总页数（2026-09-30「按格式切编辑器」）
                fid = u.path[len("/api/files/"):-len("/pages")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                out = pdf_pages(STATE.db, fid, STATE.master())
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/page"):
                # PDF 单页 → PNG（阅读器逐页渲染）
                fid = u.path[len("/api/files/"):-len("/page")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                n = as_int((q.get("n") or ["1"])[0], 1, lo=1, hi=100000, name="n")
                w = as_int((q.get("w") or ["1200"])[0], 1200, lo=200, hi=4000, name="w")
                r = pdf_page_png(STATE.db, fid, STATE.master(), n, w)
                if r.get("error"):
                    return self._send({"ok": False, "error": r["error"]}, 400)
                raw = r["png"]
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return

            if u.path.startswith("/api/files/") and u.path.endswith("/find"):
                # PDF 全文查找：命中在哪一页、哪几个矩形（阅读器跳页 + 高亮）
                fid = u.path[len("/api/files/"):-len("/find")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                needle = (q.get("q") or [""])[0].strip()
                if not needle:
                    return self._send({"ok": False, "error": "q required"}, 400)
                out = pdf_find(STATE.db, fid, STATE.master(), needle)
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/text"):
                # 第 n 页的**文字层**（行 + span + bbox）：编辑器靠它"点选文字"
                fid = u.path[len("/api/files/"):-len("/pdf/text")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                n = as_int((q.get("n") or ["1"])[0], 1, lo=1, hi=100000, name="n")
                doc, err = _pdf_open(STATE.db, fid, STATE.master())
                if doc is None:
                    return self._send({"ok": False, "error": err}, 400)
                try:
                    import pdfeng
                    r = pdfeng.text_layer(doc, n)
                    return self._send(r, 200 if r.get("ok", True) else 400)
                finally:
                    doc.close()

            if u.path.startswith("/api/files/") and u.path.endswith("/docx"):
                # **DOCX 引擎读模型**：块（段落/表格/图片 + 样式与 run）+ 篇信息，编辑器照它铺页面
                fid = u.path[len("/api/files/"):-len("/docx")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                if (row["ext"] or "").lower() != "docx":
                    return self._send({"ok": False, "error": "这篇不是 docx（%s）" % (row["ext"] or row["kind"])}, 400)
                import docxeng
                data = STATE.db.get_content(fid, STATE.master())
                doc, err = docxeng.open_doc(data=data)
                if doc is None:
                    return self._send({"ok": False, "error": err}, 400)
                inf = docxeng.info(doc)
                return self._send({"ok": True, "info": inf, "blocks": docxeng.blocks(doc),
                                   "html": docxeng.html(doc), "bytes": len(data)})

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf"):
                # PDF 引擎的总入口：页 / 元信息 / 目录 / **批注清单** / 是不是扫描件
                fid = u.path[len("/api/files/"):-len("/pdf")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                doc, err = _pdf_open(STATE.db, fid, STATE.master())
                if doc is None:
                    return self._send({"ok": False, "error": err}, 400)
                try:
                    import pdfeng
                    return self._send(pdfeng.info(doc))
                finally:
                    doc.close()

            if u.path.startswith("/api/files/") and u.path.endswith("/raw"):
                fid = u.path[len("/api/files/"):-len("/raw")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                try:
                    raw = STATE.db.get_content(fid, STATE.master())
                except Exception as e:
                    return self._send({"ok": False, "error": str(e)[:80]}, 500)
                ctype = _mime_of(row["name"])
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)
                return
            if u.path.startswith("/api/files/") and u.path.endswith("/writers"):
                fid = u.path[len("/api/files/"):-len("/writers")]
                if STATE.db.file_by_id(fid) is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                return self._send({"ok": True, "file_id": fid,
                                   "writers": STATE.events.writers_of(fid, limit=20)})
            if u.path.startswith("/api/files/"):
                fid = u.path.rsplit("/", 1)[-1]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    # 上锁时私密篇**整体不可见**（名字也不给）—— 与 /text、/raw 一个口径
                    return self._send(_lk, 403)
                master = STATE.master()
                locked = False
                data = b"" if locked else STATE.db.get_content(fid, master)
                content_text = data.decode("utf-8", "replace")
                _fm_raw = STATE.db.frontmatter_of(fid) or {}
                _fm = {k: v for k, v in _fm_raw.items()
                       if not k.startswith("_")}          # 内部记账键（_tags_applied）不对外
                _stem = row["name"].rsplit(".", 1)[0] if "." in row["name"] else row["name"]
                return self._send({"ok": True, "file": {
                    "id": row["id"], "name": row["name"], "kind": row["kind"],
                    "size": row["size"], "visibility": row["visibility"],
                    "mtime": row["mtime"], "doc_date": row["doc_date"], "locked": locked,
                    "created_at": row["created_at"],        # 入库时间（树里不再标，是属性）
                    "caps": db_caps(row),
                    "rag": (row["rag"] or "on"),
                    "block_stats": STATE.db.block_stats(fid),
                    "path": STATE.db.display_path(fid),
                    "derived": (lambda d: {"format": d["format"], "parser": d["parser"],
                                           "chars": len(d["text"] or ""), "error": d["error"],
                                           "meta": d["meta"]} if d else None)(
                        STATE.db.derived_of(fid)),
                    # 双链：mentions = 我引用了谁（带对方当前名，改名后引用处自动跟着变）
                    #       backlinks = 谁引用了我
                    # 引用 / 反向链接里若指向"锁着的私密篇"：名字换成占位（2026-09-27）
                    "mentions": hide_private_entries(STATE.db.mentions(fid)),
                    "backlinks": hide_private_entries(STATE.db.backlinks(fid)),
                    "backlink_groups": hide_private_groups(STATE.db.backlink_groups(fid)),
                    "tags": STATE.db.tags_of(fid),
                    "frontmatter": _fm,
                    "frontmatter_error": str(_fm_raw.get("_fm_error") or ""),
                    "title": str(_fm.get("title") or "").strip() or _stem,
                    "deleted_refs": deleted_refs_in(content_text, fid),
                    "content": content_text}})
            if u.path == "/api/events":
                after = as_int((q.get("after") or ["0"])[0], 0, lo=0, name="after")
                limit = as_int((q.get("limit") or ["100"])[0], 100, lo=1, hi=1000, name="limit")
                types = [t for t in (q.get("types") or [""])[0].split(",") if t]
                writer = (q.get("writer") or [""])[0]          # 按写入者过滤（写入者标签）
                evs = STATE.events.read(after, limit, types, writer=writer or None)
                if STATE.master() is None:        # 上锁：事件里也不露私密篇名（事件流是给 agent 看的）
                    for e in evs:
                        p = e.get("payload") or {}
                        if p.get("file_id") and is_locked(STATE.db.file_by_id(p["file_id"])):
                            p["name"] = "（私密·未解锁）"
                            p["hidden"] = True
                return self._send({"ok": True, "last_gseq": STATE.events.last_gseq(),
                                   "events": evs})
            if u.path == "/api/events/replay":
                frm = as_int((q.get("from") or ["0"])[0], 0, lo=0, name="from")
                to = as_int((q.get("to") or ["0"])[0], 0, lo=0, name="to") or STATE.events.last_gseq()
                types = [t for t in (q.get("types") or ["note.*"])[0].split(",") if t]
                dry = (q.get("dry_run") or ["1"])[0] not in ("0", "false")
                evs = STATE.events.read(frm, 100000, types, upto=to)
                files = sorted({e["payload"].get("file_id") for e in evs
                                if e["type"].startswith("note.") and e["payload"].get("file_id")})
                done = []
                if not dry:
                    with STATE.events.action("events.replay", reason="manual"):
                        for fid in files:
                            if STATE.db.file_by_id(fid) is None:
                                continue        # 已删除的文件重放不出来
                            STATE.indexer.reindex_file(fid)
                            done.append(fid)
                return self._send({"ok": True, "dry_run": dry, "events": len(evs),
                                   "files": files, "reindexed": done})
            if u.path == "/api/mirror":
                return self._send({"ok": True, **mirror_conf()})
            if u.path == "/api/before-images":
                import beforeimage as _bi
                fid = (q.get("file_id") or [""])[0]
                imgs = _bi.index(STATE.db, limit=as_int((q.get("limit") or ["50"])[0], 50,
                                                         lo=1, hi=2000, name="limit"),
                                 file_id=fid or None)
                if STATE.master() is None:        # 上锁：私密篇的前像不列（名字在记录里）
                    imgs = [i for i in imgs
                            if not is_locked(STATE.db.file_by_id(i.get("file_id") or ""))]
                return self._send({"ok": True, "images": imgs})
            if u.path == "/api/lock":
                return self._send({"ok": True, **STATE.lock_info()})
            if u.path == "/api/search":
                query = (q.get("q") or [""])[0]
                limit = as_int((q.get("limit") or ["8"])[0], 8, lo=1, hi=200, name="limit")
                mode = (q.get("mode") or ["hybrid"])[0]
                if not query:
                    return self._send({"ok": False, "error": "q required"}, 400)
                sens = STATE.master() is not None          # 解锁后才检索 private
                # 归档类内容（默认 logs 标签）不参与日常检索，scope=all 才纳入
                scope = (q.get("scope") or ["default"])[0]
                excl = [] if scope == "all" else [
                    t for t in (q.get("exclude") or [DEFAULT_EXCLUDE_TAGS])[0].split(",") if t]
                since = (q.get("since") or [""])[0].strip() or None
                until = (q.get("until") or [""])[0].strip() or None
                if mode == "kw":
                    hits = decorate_hits(_search_guarded(
                        STATE.db.search_chunks, query, limit=limit, include_sensitive=sens,
                        exclude_tags=excl, since=since, until=until))
                    return self._send({"ok": True, "mode": "kw", "hits": hits,
                                       "excluded_tags": excl, "since": since, "until": until})
                rr = (q.get("rerank") or [""])[0]
                rerank = None if rr == "" else (rr not in ("0", "false", "no"))
                ts = (q.get("time") or [""])[0]
                # 诊断用：?timing=1 把检索各段耗时随响应带回来（不带就一次 None 判断）
                import retriever as _rt
                want_timing = (q.get("timing") or [""])[0] in ("1", "true", "yes")
                if want_timing:
                    _rt.timing_on()
                t0 = time.perf_counter()
                hits = decorate_hits(_search_guarded(
                    STATE.searcher.search,
                    query, limit=limit, include_sensitive=sens, with_detail=True,
                    exclude_tags=excl, rerank=rerank, since=since, until=until,
                    time_sort=None if ts == "" else (ts not in ("0", "false", "no"))))
                t_dec = round((time.perf_counter() - t0) * 1000.0, 1)
                tim = _rt.timing_off() if want_timing else None
                # 按篇召回：默认给片段；expand=article 才还原成"整篇 / 本节"（一篇一条）
                expand = (q.get("expand") or [""])[0]
                arts = None
                if expand == "article":
                    arts = _search_guarded(
                        get_expander().expand, hits, query=query, include_sensitive=sens,
                        n=as_int((q.get("n") or ["3"])[0], 3, lo=0, hi=50, name="n"))
                    hits = arts
                se = STATE.searcher
                return self._send({"ok": True, "mode": "hybrid", "hits": hits,
                                   "expand": expand or None, "articles": len(arts or []),
                                   "timing": ([["装饰 hits", t_dec]] + tim) if tim is not None else None,
                                   "excluded_tags": excl, "since": since, "until": until,
                                   "rerank": bool(se.rerank_used),
                                   "degraded": se.degraded,
                                   "weights": {"kw": se.W_KW, "vec": se.W_VEC, "rel": se.W_REL}})
            if u.path == "/api/content":
                path = (q.get("path") or [""])[0]
                mx = as_int((q.get("max") or ["0"])[0], 0, lo=0, name="max")
                return self._send({"ok": True, "item": content_by_path(path, mx)})
            if u.path == "/api/folders":
                out = []
                for f in STATE.db.conn.execute("SELECT id,name,parent_id FROM folders ORDER BY name"):
                    out.append({"id": f["id"], "name": f["name"],
                                "path": STATE.db.folder_path(f["id"]),
                                "children": STATE.db.folder_children_count(f["id"])})
                return self._send({"ok": True, "folders": out})
            if u.path == "/api/properties":
                return self._send({"ok": True, **properties_index(
                    (q.get("key") or [""])[0], hide_private=STATE.master() is None)})
            if u.path == "/api/properties/find":
                key = (q.get("key") or [""])[0].strip()
                if not key:
                    return self._send({"ok": False, "error": "key required"}, 400)
                return self._send({"ok": True, **properties_find(
                    key, (q.get("value") or [""])[0].strip(),
                    limit=as_int((q.get("limit") or ["50"])[0], 50, lo=1, hi=2000, name="limit"),
                    offset=as_int((q.get("offset") or ["0"])[0], 0, lo=0, name="offset"),
                    hide_private=STATE.master() is None)})
            if u.path == "/api/maintenance/selfcheck":
                # 跑一遍 tools/selfcheck.py（子进程 + 不显示窗口），把 JSON 原样交回界面。
                # 只读工具，最长等 5 分钟（默认路径约 2 秒）。
                try:
                    import subprocess
                    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                    tool = os.path.join(root, "tools", "selfcheck.py")
                    if not os.path.isfile(tool):
                        return self._send({"ok": False, "error": "找不到 tools/selfcheck.py"}, 500)
                    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    env = dict(os.environ)
                    env["SELFCHECK_IN_SERVER"] = "1"      # 让工具跳过"探自己"（单线程会死锁）
                    p = subprocess.run([sys.executable, tool, "--json"], cwd=root, env=env,
                                       capture_output=True, text=True, encoding="utf-8",
                                       errors="replace", timeout=300, creationflags=flags)
                    if p.returncode not in (0, 1):        # 0=健康 1=有 ❌，两者都是正常输出
                        return self._send({"ok": False, "error": (p.stderr or "")[-200:]}, 500)
                    # 包一层：外层 ok = "请求成功"，里层是自检结论（它也带自己的 ok/bad/warn）
                    return self._send({"ok": True, "result": json.loads(p.stdout)})
                except Exception as e:
                    return self._send({"ok": False,
                                       "error": "%s: %s" % (type(e).__name__, str(e)[:120])}, 500)
            if u.path == "/api/tags":
                return self._send({"ok": True, "tags": STATE.db.tag_stats()})
            if u.path == "/api/tables":
                import datatables as _dt
                return self._send({"ok": True, "tables": _dt.list_tables(STATE.db)})
            if u.path == "/api/table_rows":
                import datatables as _dt
                q = parse_qs(u.query)
                g = lambda k, d="": (q.get(k) or [d])[0]
                res = _dt.rows(STATE.db, g("name"), limit=g("limit", "50"), offset=g("offset", "0"),
                               sort=g("sort"), desc=g("desc") in ("1", "true"), q=g("q"))
                if "error" in res:
                    return self._send({"ok": False, **res}, 404)
                return self._send({"ok": True, **res})
            if u.path == "/api/rag/list":
                q = parse_qs(u.query)
                only = (q.get("on") or [""])[0]
                on = None if only == "" else (only == "1" or only.lower() == "true")
                limit = as_int((q.get("limit") or ["1000"])[0], 1000, lo=1, hi=5000, name="limit")
                locked = STATE.master() is None
                rows = STATE.db.list_rag(on=on, limit=limit, hide_private=locked)
                total = STATE.db.conn.execute(
                    "SELECT COUNT(*) FROM files WHERE IFNULL(rag,'on')='on'"
                    + (" AND IFNULL(visibility,'')<>'private'" if locked else "")).fetchone()[0]
                return self._send({"ok": True, "count": len(rows), "rag_on_total": total,
                                   "hidden_private": hidden_private_count(), "files": rows})
            return self._send({"ok": False, "error": "not found: %s" % u.path}, 404)
        except (BadName, BadRequest) as e:       # 入参不合法 → 400 + 人话
            return self._send({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_OPTIONS(self):
        """CORS 预检"""
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PUT(self):
        _cache_clear()          # 写操作：缓存立刻失效
        self._mark_writer()
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/files/"):
                fid = u.path.rsplit("/", 1)[-1]
                if STATE.db.file_by_id(fid) is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                body = self._body()
                text = body.get("content")
                if text is None:
                    return self._send({"ok": False, "error": "content required"}, 400)
                if STATE.db.file_by_id(fid)["kind"] == "document":
                    return self._send({"ok": False, "error": "文档格式不能当文本写回（会把原文件写坏）；"
                                       "先「📝 另存为 Markdown」再改"}, 400)
                _lk = locked_reply(STATE.db.file_by_id(fid))
                if _lk is not None:
                    return self._send(_lk, 403)
                old = STATE.db.get_content(fid, STATE.master()).decode("utf-8", "replace")
                # 缩短闸（辞玖 2026-09-25："写入要经过接口，不能绕道" → 闸就放在接口这一层，
                # 页面 / agent / 脚本谁都绕不过）：新内容不足旧内容 70% 且旧文件有实质内容 → 拒绝
                _ob = len(old.encode("utf-8"))
                _ol = len([x for x in old.splitlines() if x.strip()])
                _nb = len(text.encode("utf-8"))
                if (_ob >= 200 or _ol >= 3) and _nb < _ob * 0.7 and not body.get("allow_shrink"):
                    return self._send({"ok": False,
                                       "error": "内容会缩短 %d%%（%d → %d 字节）；确认要删这么多"
                                                "再加 allow_shrink=true 重发"
                                       % (round(100 * (1 - _nb / max(1, _ob))), _ob, _nb),
                                       "shrink_refused": True, "before_bytes": _ob,
                                       "after_bytes": _nb}, 400)
                bi = None
                if old != text:                       # 高风险：整篇覆盖 → 先留前像（可恢复）
                    import beforeimage as _bi
                    bi = _bi.save(STATE.db, fid, "overwrite",
                                  writer=STATE.events.writer, master=STATE.master())
                STATE.db.put_content(fid, text.encode("utf-8"), STATE.master())
                res = STATE.indexer.reindex_file(fid)
                if bi:
                    res["before_image"] = bi
                res["tree"] = tree_sync(fid)          # 接口是唯一写入口 → 落库文件夹
                if _nb != _ob:                        # 体量变化回给页面（并进"已保存"那句）
                    res["size_change"] = {"before_bytes": _ob, "after_bytes": _nb}
                # @ 提及：从文本解析锚点 → 按名字索引解析成 file_id → 整体替换该文件的 mention 边
                anchors = {m["anchor"]: m["id"] for m in STATE.db.mentions(fid)}
                pairs, miss = resolve_mentions(text, STATE.db.ref_resolver(), anchors)
                res["mentions"] = STATE.db.set_mentions(fid, pairs)
                res["vectors"] = embed_now([fid])    # 语义索引跟上，不留缺口
                res["bad_sections"] = bad_sections_in(text)   # 指向不存在的小节 → 提示
                try:
                    # 本篇小节有变动 → 刷新"别人引我的哪一节"（前缀仍匹配就跟随新名）
                    res["sections"] = STATE.db.refresh_sections(fid)
                except Exception as e:
                    res["sections_error"] = "%s: %s" % (type(e).__name__, str(e)[:100])
                if miss:
                    res["unresolved"] = miss[:8]     # 解析不到的锚点（重名/名字改动）如实报出来
                row = STATE.db.file_by_id(fid)
                STATE.events.emit("note.updated", {
                    "file_id": fid, "name": row["name"], "size": row["size"],
                    "content_hash": row["content_hash"], "changed_chars": changed_chars(old, text)})
                return self._send({"ok": True, "saved": res})
            return self._send({"ok": False, "error": "not found"}, 404)
        except (BadName, BadRequest) as e:       # 入参不合法 → 400 + 人话
            return self._send({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        _cache_clear()          # 写操作：缓存立刻失效
        self._mark_writer()
        u = urlparse(self.path)
        try:
            if u.path == "/api/pick":
                b = self._body()
                if b.get("dry_run"):
                    # 验收用：不弹窗，直接把协议走一遍（免得自动化测试在桌面上弹一堆窗口）
                    return self._send({"ok": True, "state": "ok", "dry_run": True,
                                       "kind": b.get("kind") or "file",
                                       "paths": [b.get("fake") or ""], "token": "dry-run"})
                return self._send(pick_start(b.get("kind") or "file", b.get("title") or ""))
            if u.path == "/api/content/batch":
                b = self._body()
                mx = as_int(b.get("max"), 0, lo=0, name="max")
                _p = b.get("paths")
                if _p is None:
                    _p = []
                if not isinstance(_p, list):
                    # 传字符串会被"逐字符"当成路径（2026-09-30 体检）→ 直接 400
                    raise BadRequest("paths 必须是数组（[\"库内路径\", …]），收到 %s"
                                     % type(_p).__name__)
                paths = [str(x) for x in _p]
                return self._send({"ok": True, "items": {p: content_by_path(p, mx) for p in paths}})
            if u.path == "/api/lock/reset":
                # 重设库密码（忘了密码 / 想换一个）：**清掉密钥材料**再重新设。
                # 危险：已经加密的私密篇会读不出来 —— 之后可以用
                # POST /api/maintenance/private-rekey 从磁盘明文按新密钥重新加密捞回来。
                if not self._body().get("confirm"):
                    return self._send({"ok": False,
                                       "error": "重设会清掉密钥材料，要重设请带 confirm=true"}, 400)
                if STATE.master() is None:
                    # 库上锁时不许重设 —— 否则"锁"就形同虚设（谁点一下都能把密码换掉）。
                    # 忘了密码的正路是命令行维护工具：tools/lock_force_reset.py（要有磁盘权限）。
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "hint": "先解锁才能重设；忘密码请用 tools/lock_force_reset.py"}, 403)
                STATE.db.set_manifest(kdf_algo="", kdf_salt="", verify_blob="")
                STATE.lock()                       # 顺手把内存里的主密钥也丢掉
                STATE.events.emit("lock.reset", {"writer": STATE.events.writer})
                return self._send({"ok": True, **STATE.lock_info()})
            if u.path == "/api/lock/set":
                if STATE.db.vault_kdf() is not None:
                    return self._send({"ok": False, "error": "已经设过库密码"}, 400)
                b = self._body()
                pw = b.get("password") or ""
                if len(pw) < 4:
                    return self._send({"ok": False, "error": "密码至少 4 位"}, 400)
                if b.get("confirm") is not None and b.get("confirm") != pw:
                    return self._send({"ok": False, "error": "两次输入不一致"}, 400)
                salt = vaultcrypto.new_salt()
                master = vaultcrypto.derive(pw, salt, vaultcrypto.KDF_N, vaultcrypto.KDF_R, vaultcrypto.KDF_P)
                STATE.db.set_vault_password(master, salt)
                STATE.unlock(master)
                return self._send({"ok": True, **STATE.lock_info()})
            if u.path == "/api/lock/unlock":
                kdf = STATE.db.vault_kdf()
                if kdf is None:
                    return self._send({"ok": False, "error": "还没设过库密码"}, 400)
                pw = self._body().get("password") or ""
                master = vaultcrypto.derive(pw, kdf["salt"], kdf["n"], kdf["r"], kdf["p"])
                if not vaultcrypto.check_master(master, STATE.db.manifest().get("verify_blob") or ""):
                    return self._send({"ok": False, "error": "密码不对"}, 403)
                STATE.unlock(master)
                made_pub = False
                if not STATE.db.vault_pub():
                    # 老库（今天之前设的密码）没有库密钥对 → 趁手里有主密钥，补一对。
                    # 这样"锁着也能设为私密"对老库同样开箱可用，主人不用做任何事。
                    try:
                        pub, priv = vaultcrypto.new_box_keypair()
                        STATE.db.set_manifest(vault_pub=pub,
                                              vault_priv_ref=vaultcrypto.wrap_key(master, priv))
                        made_pub = True
                    except Exception:
                        made_pub = False
                n = 0
                for f in STATE.db.private_files():          # 解锁后补建 private 派生层
                    STATE.indexer.reindex_file(f["id"])
                    n += 1
                # 露出来的私密篇直接给前端：它能"插进现有的树"，不用把整树重拉一遍。
                # package 模式下以**加密包**为准（包是读的真相源）；否则与今天一字不差。
                _rev = _reveal_rows(master)
                out = {"ok": True, "reindexed": n, "pub_ready_made": made_pub,
                       "revealed": _rev["rows"]}
                if _rev["ambiguous"]:
                    out["revealed_ambiguous"] = _rev["ambiguous"]
                if _rev["unmatched"]:
                    out["revealed_unmatched"] = _rev["unmatched"]
                out.update(STATE.lock_info())
                return self._send(out)
            if u.path == "/api/lock/lock":
                STATE.lock()
                n = STATE.db.purge_sensitive_chunks()
                return self._send({"ok": True, "purged": n, **STATE.lock_info()})
            if u.path == "/api/lock/change":
                # 换密码 = **先解后封**（辞玖 2026-09-29）：用旧主密钥把每篇私密篇的明文取出来，
                # 换 KDF，再用新主密钥重新包裹。绝不能像 reset 那样先清密钥材料 —— 那会把旧密文废掉。
                if STATE.db.vault_kdf() is None:
                    return self._send({"ok": False, "error": "还没设过库密码"}, 400)
                old_master = STATE.master()
                if old_master is None:
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "hint": "先解锁（搜索框里输当前密码）才能换密码"}, 403)
                b = self._body()
                pw = str(b.get("password") or "")
                if len(pw) < 4:
                    return self._send({"ok": False, "error": "新密码至少 4 位"}, 400)
                if str(b.get("confirm") or "") != pw:
                    return self._send({"ok": False, "error": "两次输入不一致"}, 400)
                plains, failed = {}, []
                for f in STATE.db.private_files():
                    try:
                        plains[f["id"]] = STATE.db.get_content(f["id"], old_master)
                    except Exception as e:
                        failed.append({"name": f["name"], "why": "%s: %s" % (type(e).__name__, str(e)[:50])})
                if failed:              # 有解不开的先别动密码材料（动了就更救不回来）
                    return self._send({"ok": False, "error": "有 %d 篇私密篇解不开，先别换密码"
                                       % len(failed), "failed": failed[:5]}, 400)
                old_man = {k: (STATE.db.manifest() or {}).get(k, "")
                           for k in ("kdf_algo", "kdf_salt", "verify_blob", "kdf_n", "kdf_r", "kdf_p",
                                     "vault_pub", "vault_priv_ref")}
                salt = vaultcrypto.new_salt()
                new_master = vaultcrypto.derive(pw, salt, vaultcrypto.KDF_N,
                                                vaultcrypto.KDF_R, vaultcrypto.KDF_P)
                STATE.db.set_vault_password(new_master, salt)
                done = []
                try:
                    for fid, data in plains.items():
                        STATE.db.rekey_private(fid, data, new_master)
                        done.append(fid)
                except Exception as e:
                    for fid in done:                 # 回滚：用旧主密钥把已重封的包回去
                        try:
                            STATE.db.rekey_private(fid, plains[fid], old_master)
                        except Exception:
                            pass
                    STATE.db.set_manifest(**old_man)
                    return self._send({"ok": False, "error": "换密码失败，已回滚：%s"
                                       % str(e)[:80]}, 500)
                STATE.unlock(new_master)
                for fid in done:                     # 私密篇的派生层跟着刷一遍
                    try:
                        STATE.indexer.reindex_file(fid)
                    except Exception:
                        pass
                STATE.events.emit("lock.change", {"reencrypted": len(done),
                                                  "writer": STATE.events.writer})
                return self._send({"ok": True, "reencrypted": len(done), **STATE.lock_info()})
            if u.path == "/api/lock/clear":
                # 取消密码（辞玖 2026-09-29）：把私密篇解密回明文、改成"内部"，然后清密钥材料。
                # 有解不开的就**不清** —— 清了就永远读不出来（先让人去解决那几篇）。
                if STATE.db.vault_kdf() is None:
                    return self._send({"ok": False, "error": "本来就没设库密码"}, 400)
                if not self._body().get("confirm"):
                    return self._send({"ok": False,
                                       "error": "取消密码会把私密篇解密成明文（不再隐藏），"
                                                "要取消请带 confirm=true"}, 400)
                master = STATE.master()
                if master is None:
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "hint": "先解锁（搜索框里输当前密码）才能取消密码"}, 403)
                n, failed = 0, []
                for f in STATE.db.private_files():
                    try:
                        STATE.db.set_visibility(f["id"], "internal", master)   # 解密 + 不再是私密
                        n += 1
                    except Exception as e:
                        failed.append({"name": f["name"], "why": "%s: %s" % (type(e).__name__, str(e)[:50])})
                if failed:
                    return self._send({"ok": False, "error": "有 %d 篇解不开，没有清密码（清了就永远读不出来）"
                                       % len(failed), "decrypted": n, "failed": failed[:5]}, 400)
                STATE.db.set_manifest(kdf_algo="", kdf_salt="", verify_blob="",
                                      # **库密钥对也要清**：留着它 = 一把「私钥已经丢了」的锁，
                                      # 再拿它加密出来的篇会永远解不开（2026-10-01 smoke_lock 抓到）
                                      vault_pub="", vault_priv_ref="")
                STATE.lock()
                STATE.events.emit("lock.clear", {"decrypted": n, "writer": STATE.events.writer})
                return self._send({"ok": True, "decrypted": n, **STATE.lock_info()})
            if u.path.startswith("/api/files/") and u.path.endswith("/append"):
                fid = u.path[len("/api/files/"):-len("/append")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                if row["kind"] not in ("markdown", "text", "code"):
                    return self._send({"ok": False, "error":
                                       "这一篇不是文本（kind=%s），不能追加正文" % row["kind"]}, 400)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                text = b.get("text")
                if not isinstance(text, str) or not text.strip():
                    return self._send({"ok": False, "error":
                                       "缺 text（要追加的正文；只给空白也算空）"}, 400)
                data = text.encode("utf-8")
                before = STATE.db.append_content(fid, data, STATE.master())
                res = STATE.indexer.reindex_file(fid)
                res["tree"] = tree_append(fid, data)          # 盘上按字节追加
                full = STATE.db.get_content(fid, STATE.master()).decode("utf-8", "replace")
                anchors = {m["anchor"]: m["id"] for m in STATE.db.mentions(fid)}
                pairs, miss = resolve_mentions(full, STATE.db.ref_resolver(), anchors)
                res["mentions"] = STATE.db.set_mentions(fid, pairs)
                if miss:
                    res["unresolved"] = miss[:8]
                res["vectors"] = embed_now([fid])
                row2 = STATE.db.file_by_id(fid)
                STATE.events.emit("note.appended", {
                    "file_id": fid, "name": row2["name"], "appended": len(data),
                    "size": row2["size"]})
                return self._send({"ok": True, "appended": len(data), "before_bytes": before,
                                   "size": row2["size"], "saved": res})
            if u.path.startswith("/api/files/") and u.path.endswith("/properties"):
                fid = u.path[len("/api/files/"):-len("/properties")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                if (row["kind"] or "") not in ("markdown", "text", "code"):
                    return self._send({"ok": False, "error": "这类文件没有属性"}, 400)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                sets = {str(k): v for k, v in (b.get("set") or {}).items() if str(k)}
                dels = [str(k) for k in (b.get("del") or []) if str(k)]
                if not sets and not dels:
                    return self._send({"ok": False, "error": "没有要改的属性"}, 400)
                raw = STATE.db.get_content(fid, STATE.master()).decode("utf-8", "replace")
                import frontmatter as _fm
                text = raw
                if sets:
                    text = _fm.upsert(text, sets)
                for k in dels:
                    text = _fm.drop(text, k)
                if text == raw:
                    return self._send({"ok": True, "changed": False, "frontmatter":
                                       STATE.db.frontmatter_of(fid)})
                okw = write_through(fid, text, "properties→frontmatter")
                if not okw:
                    return self._send({"ok": False, "changed": False,
                                       "error": "属性没能写进库（写库/落盘/补向量任一失败，详见库日志）"}, 500)
                return self._send({"ok": True, "changed": True,
                                   "frontmatter": STATE.db.frontmatter_of(fid),
                                   "tags": STATE.db.tags_of(fid)})
            if u.path.startswith("/api/files/") and u.path.endswith("/tags"):
                fid = u.path[len("/api/files/"):-len("/tags")]
                row_t = STATE.db.file_by_id(fid)
                if row_t is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row_t)      # 和改属性一样：锁着不许改（文件头 YAML 也写不进去）
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                names = [str(t).strip() for t in (b.get("tags") or []) if str(t).strip()]
                mode = b.get("mode") or "add"
                if mode == "set":
                    for t in STATE.db.tags_of(fid):
                        STATE.db.remove_tag(fid, t)
                    mode = "add"
                for t in names:
                    STATE.db.add_tag(fid, t) if mode == "add" else STATE.db.remove_tag(fid, t)
                tags_now = STATE.db.tags_of(fid)
                synced = sync_frontmatter_tags(fid, tags_now)      # 写回文件头的 YAML tags
                return self._send({"ok": True, "tags": tags_now, "frontmatter_synced": synced})
            if u.path.startswith("/api/files/") and u.path.endswith("/visibility"):
                fid = u.path[len("/api/files/"):-len("/visibility")]
                cur = STATE.db.file_by_id(fid)
                if cur is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                was = cur["visibility"]
                vis = self._body().get("visibility") or ""
                # 「设为私密」= 加密，**锁着也能做**（用库公钥封内容密钥，辞玖 2026-10-01 定）。
                # 只有一种情况做不了：库里压根没设过密码 —— 那没有钥匙能保护它，得先去设一个。
                if vis == "private" and was != "private" and (cur["enc_algo"] or "") != vaultcrypto.ALGO \
                        and STATE.master() is None and not vault_usable():
                    return self._send({"ok": False, "need_setup": not _has_kdf(),
                                       "pub_ready": False, "locked": False,
                                       "error": ("还没设过库密码：先在「右键文件树 → 设置密码…」设一个，再来设为私密"
                                                 if not _has_kdf() else
                                                 "库密钥还没生成：先在搜索框输一次密码解锁（会自动补上），"
                                                 "之后锁着也能设为私密")},
                                      403)
                # 「移出私密」= 解密 → **必须解锁**（要私钥）
                if vis != "private" and was == "private" and STATE.master() is None:
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "hint": "移出私密要把内容解密出来，先解锁（搜索框输库密码）"}, 403)
                try:
                    res = STATE.db.set_visibility(fid, vis, STATE.master())
                except vaultcrypto.LockedError:
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "need_setup": not STATE.db.vault_pub(),
                                       "hint": "还没设过库密码（先去设一个）"}, 403)
                if vis == "private" and STATE.master() is None:
                    # 锁着转的私密：库里没人能重新解析它 → **当场把派生层清干净**
                    # （否则"私密"只是改了个标记，正文还在块表/向量里、还搜得到）
                    STATE.db.purge_sensitive_chunks(fid)
                    try:
                        refresh_searcher([fid])
                    except Exception:
                        pass
                else:
                    # 解锁态切可见性只影响标记：派生层留着（chunk 标 is_sensitive），
                    # 上锁时由 purge_sensitive_chunks 统一清——否则每次切私密都会白丢该文件的向量
                    try:
                        STATE.indexer.reindex_file(fid)
                    except Exception as e:
                        res["reindex_error"] = str(e)[:80]
                res["tree"] = tree_sync(fid)            # 转私密 → 树里换成密文；转回来 → 明文
                STATE.events.emit("note.visibility.changed", {"file_id": fid, "from": was, "to": vis,
                                                              "locked": STATE.master() is None})
                return self._send({"ok": True, "result": res, **STATE.lock_info()})
            if u.path == "/api/visibility/batch":
                # 多选后批量切私密（辞玖 2026-10-01：「多选后操作还应该包括设为私密、解锁状态下的移出私密」）
                b = self._body()
                vis = str(b.get("visibility") or "")
                if vis not in ("private", "internal"):
                    raise BadRequest("visibility 只能是 private 或 internal")
                ids = [str(x) for x in (b.get("ids") or [])]
                if b.get("folder_id"):
                    ids += STATE.db.folder_file_ids(str(b["folder_id"]),
                                                    recursive=bool(b.get("recursive", True)))
                seen, uniq = set(), []
                for i in ids:
                    if i and i not in seen:
                        seen.add(i)
                        uniq.append(i)
                if not uniq:
                    return self._send({"ok": False, "error": "缺 ids 或 folder_id"}, 400)
                if vis == "private" and STATE.master() is None and not vault_usable():
                    return self._send({"ok": False, "need_setup": not _has_kdf(),
                                       "pub_ready": False, "locked": False,
                                       "error": ("还没设过库密码：先在「右键文件树 → 设置密码…」设一个，"
                                                 "再来设为私密" if not _has_kdf() else
                                                 "库密钥还没生成：先在搜索框输一次密码解锁（会自动补上），"
                                                 "之后锁着也能批量设为私密")}, 403)
                if vis != "private" and STATE.master() is None:
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "hint": "移出私密要把内容解密出来，先解锁（搜索框输库密码）"}, 403)
                changed, skipped, failed = 0, 0, []
                for fid in uniq:
                    row = STATE.db.file_by_id(fid)
                    if row is None:
                        continue
                    if is_locked(row):
                        skipped += 1                      # 锁着看不见的私密篇：跳过（不报错）
                        continue
                    try:
                        STATE.db.set_visibility(fid, vis, STATE.master())
                        if vis == "private" and STATE.master() is None:
                            STATE.db.purge_sensitive_chunks(fid)
                        else:
                            try:
                                STATE.indexer.reindex_file(fid)
                            except Exception:
                                pass
                        tree_sync(fid)
                        changed += 1
                    except Exception as e:
                        failed.append({"id": fid, "name": row["name"], "why": str(e)[:60]})
                if vis == "private":
                    try:
                        refresh_searcher(uniq)
                    except Exception:
                        pass
                STATE.events.emit("note.visibility.batch",
                                  {"visibility": vis, "count": changed, "skipped": skipped,
                                   "failed": len(failed), "locked": STATE.master() is None})
                return self._send({"ok": True, "visibility": vis, "count": changed,
                                   "skipped": skipped, "failed": failed[:5],
                                   **STATE.lock_info()})

            if u.path.startswith("/api/files/") and u.path.endswith("/rag"):
                fid = u.path[len("/api/files/"):-len("/rag")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                on = bool(b.get("on", True))
                rag = STATE.db.set_rag(fid, on)
                res = STATE.indexer.reindex_file(fid)      # on → 解析+切块；off → 清块清向量
                vec = embed_now([fid]) if on else {}
                if not on:
                    refresh_searcher([fid])    # 移出检索：把这篇的块从缓存里拿掉
                STATE.events.emit("note.rag", {"file_id": fid, "name": row["name"], "rag": rag,
                                               "chunks": res.get("chunks", 0)})
                return self._send({"ok": True, "rag": rag, "chunks": res.get("chunks", 0),
                                   "vectors": vec, "parse_error": res.get("parse_error", "")})
            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/annot"):
                # 批注：一次调用里可以"加几条 + 删几条"，最后一起写回（编辑器攒够一次保存）
                fid = u.path[len("/api/files/"):-len("/pdf/annot")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                adds = b.get("add") or []
                dels = b.get("del") or []
                if not isinstance(adds, list) or not isinstance(dels, list):
                    raise BadRequest("add / del 都必须是数组")
                if not adds and not dels:
                    raise BadRequest("add 或 del 至少给一个（没东西改就不用写回）")

                def _mut(doc):
                    import pdfeng
                    made, gone = [], 0
                    for a in adds:
                        if not isinstance(a, dict):
                            return {"ok": False, "error": "add 里每一项要是对象"}
                        r = pdfeng.add_annot(doc, int(a.get("page") or 1), a.get("kind") or "highlight",
                                             a.get("rects"), text=str(a.get("text") or ""),
                                             color=a.get("color") or "yellow",
                                             author=str(a.get("author") or STATE.events.writer))
                        if r.get("ok") is False:
                            return r
                        made += r.get("made") or []
                    if dels:
                        r = pdfeng.del_annot(doc, dels)
                        gone = int(r.get("deleted") or 0)
                    return {"ok": True, "added": made, "deleted": gone,
                            "annots": pdfeng.annots(doc)}

                out = pdf_apply(STATE.db, fid, _mut, what="annot")
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/pages"):
                # 页面操作：rotate / delete / move / extract（extract 产出新文件，不写回原篇）
                fid = u.path[len("/api/files/"):-len("/pdf/pages")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import pdfeng
                b = self._body()
                op = str(b.get("op") or "").lower()
                if op == "extract":
                    doc, err = _pdf_open(STATE.db, fid, STATE.master())
                    if doc is None:
                        return self._send({"ok": False, "error": err}, 400)
                    try:
                        r = pdfeng.pages_op(doc, "extract", nums=b.get("nums") or [])
                    finally:
                        doc.close()
                    if r.get("ok") is False:
                        return self._send(r, 400)
                    base = (row["name"] or "x.pdf").rsplit(".", 1)[0]
                    nn = str(b.get("name") or ("%s-提取.pdf" % base)).strip()
                    if STATE.db.find_file(row["folder_id"], nn):
                        nn = "%s-提取2.pdf" % base
                    nid = STATE.db.create_file(row["folder_id"], nn, r["bytes"])
                    STATE.indexer.reindex_file(nid)
                    STATE.events.emit("pdf.extract", {"from": fid, "file_id": nid, "pages": r.get("pages")})
                    return self._send({"ok": True, "op": "extract", "file_id": nid, "name": nn,
                                       "pages": r.get("pages"), "tree": tree_sync(nid)})

                def _mut(doc):
                    return pdfeng.pages_op(doc, op, n=int(b.get("n") or 0),
                                           to=int(b.get("to") or 0), nums=b.get("nums") or [])

                out = pdf_apply(STATE.db, fid, _mut, what="pages:" + op)
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/text-edit"):
                # **擦字重画**：编辑器攒下的一批 {page,rect,text,size?,color?,font?,align?} 一次写回
                fid = u.path[len("/api/files/"):-len("/pdf/text-edit")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import pdfeng
                b = self._body()
                ops = b.get("ops") or []
                if not isinstance(ops, list) or not ops:
                    raise BadRequest("ops 要是非空数组（每项 {page,rect,text,...}）")

                out = pdf_apply(STATE.db, fid,
                                lambda doc: pdfeng.apply_text_ops(doc, ops), what="text-edit")
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/replace"):
                # 全篇查找替换：先空跑一遍数命中（不动文件），有命中才走 pdf_apply
                fid = u.path[len("/api/files/"):-len("/pdf/replace")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import pdfeng
                b = self._body()
                q = str(b.get("q") or "")
                rep = str(b.get("rep") if b.get("rep") is not None else "")
                pages = b.get("pages") or []
                if not q:
                    raise BadRequest("q 不能空（要替换什么？）")
                doc, err = _pdf_open(STATE.db, fid, STATE.master())
                if doc is None:
                    return self._send({"ok": False, "error": err}, 400)
                try:
                    probe = pdfeng.find_replace(doc, q, rep, pages=pages)
                finally:
                    doc.close()
                if not probe.get("count"):
                    return self._send({"ok": True, "count": 0, "q": q, "rep": rep,
                                       "why": "没找到「%s」" % q})

                out = pdf_apply(STATE.db, fid,
                                lambda doc: pdfeng.find_replace(doc, q, rep, pages=pages),
                                what="replace")
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/stamp"):
                # 印记：页码（kind=pageno，写进内容、文字层干净）或水印（kind=watermark，斜的、图）
                fid = u.path[len("/api/files/"):-len("/pdf/stamp")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import pdfeng
                b = self._body()
                kind = str(b.get("kind") or "").lower()
                if kind not in ("pageno", "watermark"):
                    raise BadRequest("kind 只能是 pageno（页码）或 watermark（水印）")
                if kind == "pageno":
                    def _mut(doc):
                        return pdfeng.stamp_page_numbers(doc, start=int(b.get("start") or 1),
                                                         fmt=str(b.get("fmt") or "第 %d 页 / 共 %d 页"),
                                                         size=float(b.get("size") or 9),
                                                         where=str(b.get("where") or "bottom-center"),
                                                         color=b.get("color") or (0.35, 0.35, 0.35),
                                                         margin=float(b.get("margin") or 30),
                                                         pages=b.get("pages") or [])
                else:
                    def _mut(doc):
                        return pdfeng.stamp_watermark(doc, str(b.get("text") or ""),
                                                      size=float(b.get("size") or 46),
                                                      opacity=float(b.get("opacity") or 0.12),
                                                      angle=float(b.get("angle") or 45),
                                                      color=b.get("color") or (0.5, 0.5, 0.5),
                                                      pages=b.get("pages") or [])

                out = pdf_apply(STATE.db, fid, _mut, what="stamp:" + kind)
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/merge"):
                # 合并：把别的篇并进来（where=end/start/页码下标）；不产出新文件，直接写回这一篇
                fid = u.path[len("/api/files/"):-len("/pdf/merge")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import pdfeng
                b = self._body()
                ids = b.get("ids") or ([b.get("id")] if b.get("id") else [])
                if not isinstance(ids, list) or not ids:
                    raise BadRequest("ids 要是数组（要并进来的那几篇）")
                if len(ids) > 10:
                    raise BadRequest("一次最多并 10 篇")
                blobs, names = [], []
                for oid in ids:
                    orow = STATE.db.file_by_id(oid)
                    if orow is None:
                        return self._send({"ok": False, "error": "要并的那篇不存在：%s" % oid}, 404)
                    _lk2 = locked_reply(orow)
                    if _lk2 is not None:
                        return self._send(_lk2, 403)
                    if (orow["ext"] or "").lower() != "pdf":
                        return self._send({"ok": False,
                                           "error": "「%s」不是 PDF，不能并" % orow["name"]}, 400)
                    blobs.append(STATE.db.get_content(oid, STATE.master()))
                    names.append(orow["name"])

                def _mut(doc):
                    total, pages = 0, None
                    for blob in blobs:
                        r = pdfeng.merge_pdf(doc, blob, where=b.get("where") or "end")
                        if r.get("ok") is False:
                            return r
                        total += int(r.get("added") or 0)
                        pages = r.get("pages")
                    return {"ok": True, "op": "merge", "added": total, "pages": pages,
                            "from": names}

                out = pdf_apply(STATE.db, fid, _mut, what="merge")
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/flatten"):
                # 扁平化：把批注烤进页面内容（如实说：这页会变成图，不再是文字）
                fid = u.path[len("/api/files/"):-len("/pdf/flatten")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import pdfeng
                b = self._body()
                out = pdf_apply(STATE.db, fid,
                                lambda doc: pdfeng.bake_annots(doc, dpi=int(b.get("dpi") or 150),
                                                               pages=b.get("pages") or []),
                                what="flatten")
                return self._send(out, 200 if out.get("ok") else 400)

            if u.path.startswith("/api/files/") and u.path.endswith("/pdf/export-images"):
                # 导出图片：**产出新文件**（每页一张 PNG），跟 extract 一个规矩，不写回原篇
                fid = u.path[len("/api/files/"):-len("/pdf/export-images")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import pdfeng
                b = self._body()
                doc, err = _pdf_open(STATE.db, fid, STATE.master())
                if doc is None:
                    return self._send({"ok": False, "error": err}, 400)
                try:
                    r = pdfeng.export_images(doc, dpi=int(b.get("dpi") or 110),
                                             pages=b.get("nums") or b.get("pages") or [])
                finally:
                    doc.close()
                if r.get("ok") is False:
                    return self._send(r, 400)
                base = (row["name"] or "x.pdf").rsplit(".", 1)[0]
                made = []
                for im in r["images"]:
                    nn = "%s-p%03d.png" % (base, im["page"])
                    if STATE.db.find_file(row["folder_id"], nn):
                        nn = "%s-p%03d-2.png" % (base, im["page"])
                    nid = STATE.db.create_file(row["folder_id"], nn, im["data"])
                    STATE.indexer.reindex_file(nid)
                    made.append({"file_id": nid, "name": nn, "page": im["page"],
                                 "w": im["w"], "h": im["h"]})
                if made:
                    STATE.events.emit("pdf.export_images",
                                      {"file_id": fid, "count": len(made),
                                       "files": [m["file_id"] for m in made]})
                return self._send({"ok": True, "op": "images", "count": len(made), "files": made,
                                   "dpi": r.get("dpi"), "tree": tree_sync(made[0]["file_id"]) if made else None})

            if u.path.startswith("/api/files/") and u.path.endswith("/docx/edit"):
                # **DOCX 引擎写回**：一批 {op:text|run|para|insert|del|move|cell|row|replace|image|page}
                fid = u.path[len("/api/files/"):-len("/docx/edit")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import docxeng
                b = self._body()
                ops = b.get("ops") or []
                if not isinstance(ops, list) or not ops:
                    raise BadRequest("ops 要是非空数组（每项 {op:...}）")
                out = docx_apply(STATE.db, fid, lambda doc: docxeng.apply_ops(doc, ops),
                                 what="edit", allow_shrink=bool(b.get("allow_shrink")))
                return self._send(out, 200 if out.get("ok") else (403 if out.get("locked") else 400))

            if u.path.startswith("/api/files/") and u.path.endswith("/docx/export"):
                # 导出成**新文件**：md / txt / html（引擎抽）/ pdf（WPS 直接吃 docx，样式不简化）
                fid = u.path[len("/api/files/"):-len("/docx/export")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                import docxeng
                import docconvert
                import tempfile
                b = self._body()
                to = str(b.get("to") or "md").lower().lstrip(".")
                if to not in ("md", "markdown", "txt", "html", "pdf"):
                    raise BadRequest("to 只能是 md / txt / html / pdf")
                if to == "markdown":
                    to = "md"
                data = STATE.db.get_content(fid, STATE.master())
                base = (row["name"] or "x.docx").rsplit(".", 1)[0]
                engine = "docxeng"
                if to == "pdf":
                    with tempfile.TemporaryDirectory(prefix="ai-notes-dx-") as td:
                        src = os.path.join(td, "in.docx")
                        dst = os.path.join(td, "out.pdf")
                        open(src, "wb").write(data)
                        r = docconvert.docx_to_pdf(src, dst)
                        if r.get("error"):
                            return self._send({"ok": False, "error": r["error"]}, 400)
                        out_data = open(dst, "rb").read()
                        engine = r.get("engine") or "WPS COM"
                elif to == "txt":
                    r = docxeng.export_txt(data)
                    if r.get("ok") is False:
                        return self._send(r, 400)
                    out_data = (r.get("text") or "").encode("utf-8")
                elif to == "html":
                    doc, err = docxeng.open_doc(data=data)
                    if doc is None:
                        return self._send({"ok": False, "error": err}, 400)
                    out_data = ("<!doctype html><meta charset=\"utf-8\">\n" + docxeng.html(doc)).encode("utf-8")
                else:
                    r = docxeng.export_md(data)
                    if not r.get("md"):
                        return self._send({"ok": False, "error": r.get("error") or "导不出 Markdown"}, 400)
                    out_data = (r.get("md") or "").encode("utf-8")
                    engine = r.get("engine") or "pandoc"
                nn = "%s.%s" % (base, to)
                if STATE.db.find_file(row["folder_id"], nn):
                    nn = "%s-导出.%s" % (base, to)
                nid = STATE.db.create_file(row["folder_id"], nn, out_data)
                STATE.indexer.reindex_file(nid)
                STATE.events.emit("docx.export", {"file_id": fid, "to": to, "new": nid})
                return self._send({"ok": True, "to": to, "file_id": nid, "name": nn,
                                   "bytes": len(out_data), "engine": engine, "tree": tree_sync(nid)})

            if u.path.startswith("/api/files/") and u.path.endswith("/writeback"):
                # 「文档编辑器」保存：编辑器里改的是解析出来的 Markdown，
                # 保存时按**原格式**重新生成真文件，写回同一篇（2026-09-30）。
                # 为什么单独一条路由、而不是让 PUT 认：PUT 是"文本写回"，对二进制文档会把原文件写坏
                # （PUT 里那条显式拒绝就是为这个）。这条只处理 kind=document，且**先留前像**。
                fid = u.path[len("/api/files/"):-len("/writeback")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                if row["kind"] != "document":
                    return self._send({"ok": False,
                                       "error": "这篇不是文档格式（%s）；文本格式请走 PUT 保存" % row["kind"]}, 400)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                text = b.get("content")
                if not isinstance(text, str):
                    return self._send({"ok": False, "error": "content 必须是字符串"}, 400)
                out = doc_writeback(STATE.db, fid, text, allow_shrink=bool(b.get("allow_shrink")))
                return self._send(out, 200 if out.get("ok") else (403 if out.get("locked") else 400))

            if u.path.startswith("/api/files/") and u.path.endswith("/convert"):
                fid = u.path[len("/api/files/"):-len("/convert")]
                src = STATE.db.file_by_id(fid)
                if src is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                if src["visibility"] == "private" and STATE.master() is None:
                    return self._send({"ok": False, "error": "locked", "locked": True}, 403)
                import docconvert
                import tempfile
                b = self._body()
                to = (b.get("to") or "md").lower().lstrip(".")
                if to == "markdown":
                    to = "md"
                if to not in ("md", "docx", "odt", "rtf", "html", "epub", "pdf"):
                    return self._send({"ok": False,
                                       "error": "不支持转成 %s（可用 md/docx/odt/rtf/html/epub/pdf）"
                                                % to}, 400)
                raw = STATE.db.get_content(fid, STATE.master())
                # 源文本：文档格式 → 解析出来的 Markdown；文本格式 → 原文
                if src["kind"] == "document":
                    d = STATE.db.derived_for(fid, raw)
                    md_text = d.get("text") or ""
                    parser = d.get("parser") or ""
                    meta = d.get("meta") or {}
                else:
                    md_text = raw.decode("utf-8", "replace")
                    parser, meta = "原文", {}
                if not md_text.strip():
                    return self._send({"ok": False, "error": "这个文件没有可转出的正文"}, 400)
                base = src["name"].rsplit(".", 1)[0]
                new_name = (b.get("name") or ("%s.%s" % (base, to))).strip()
                if not new_name.lower().endswith("." + to):
                    new_name += "." + to
                if STATE.db.find_file(src["folder_id"], new_name):
                    new_name = "%s-转换.%s" % (base, to)      # 不覆盖同名
                with tempfile.TemporaryDirectory(prefix="ai-notes-conv-") as td:
                    out = os.path.join(td, "out." + to)
                    if to == "pdf":
                        r = docconvert.to_pdf(md_text, out)
                    else:
                        r = docconvert.write_md(md_text, to, out)
                    if r.get("error"):
                        return self._send({"ok": False, "error": r["error"]}, 500)
                    check = docconvert.verify(out, to, md_text)
                    data = open(out, "rb").read()
                if to == "md":
                    data = ("<!-- 由 %s 转换而来（%s） -->\n\n"
                            % (src["name"], parser or "?")).encode("utf-8") + data
                _priv = src["visibility"] == "private"
                nid = STATE.db.create_file(src["folder_id"], new_name, data,
                                           visibility=src["visibility"],
                                           master=STATE.master() if _priv else None)
                STATE.indexer.reindex_file(nid)
                tree = tree_sync(nid)          # 产物也要落盘（以前只进库：库文件夹/vault 里都没有）
                texts = STATE.db.get_content(nid, STATE.master()).decode("utf-8", "replace")
                pairs, _miss = resolve_mentions(texts, STATE.db.ref_resolver())
                STATE.db.set_mentions(nid, pairs)
                row2 = STATE.db.file_by_id(nid)
                STATE.events.emit("note.created", {
                    "file_id": nid, "folder_id": row2["folder_id"], "name": row2["name"],
                    "kind": row2["kind"], "size": row2["size"], "reason": "converted",
                    "from": src["name"], "to": to, "engine": r.get("engine")})
                return self._send({"ok": True, "file_id": nid, "name": row2["name"], "to": to,
                                   "tree": tree,
                                   "chars": docconvert.char_count(md_text),
                                   "han": docconvert.han_count(md_text),
                                   "engine": r.get("engine"), "parser": parser, "meta": meta,
                                   "verify": check}, 201)
            if u.path.startswith("/api/files/") and u.path.endswith("/sections/relink"):
                fid = u.path[len("/api/files/"):-len("/sections/relink")]
                if STATE.db.file_by_id(fid) is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                b = self._body()
                try:
                    r = STATE.db.relink_section_refs(fid, b.get("old") or "", b.get("new") or "",
                                                     STATE.master())
                except ValueError as e:
                    return self._send({"ok": False, "error": str(e)}, 400)
                rew = after_rewrite(list(r.get("rewritten", {})), "section-relinked")
                STATE.events.emit("refs.section.relinked", {
                    "file_id": fid, "old_section": b.get("old"), "new_section": b.get("new"),
                    "files": r.get("files"), "moved": r.get("moved")})
                return self._send({"ok": True, "moved": r.get("moved", 0),
                                   "files": r.get("files", 0),
                                   "vectors": rew.get("vectors"),
                                   "tree": rew.get("tree")})
            if u.path.startswith("/api/files/") and u.path.endswith("/rename"):
                fid = u.path[len("/api/files/"):-len("/rename")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                name = (self._body().get("name") or "").strip()
                if not name:
                    return self._send({"ok": False, "error": "name required"}, 400)
                old = row["name"]
                res = {}
                if name != old:
                    old_date = row["doc_date"] or ""
                    _old_rel = tree_rel(fid)
                    # ⚠️ 传 master：package 模式下私密篇改名，包内那条的名字也要跟着改，
                    #    否则库里叫新名、包里还叫旧名 → 读不到（改名即失联）。
                    res = STATE.db.rename_file(fid, name, STATE.master())
                    STATE.indexer.reindex_file(fid)
                    res["tree"] = tree_sync(fid)             # 先写新
                    if _old_rel:
                        tree_drop(_old_rel)                  # 再撤旧（写新失败时旧那份还在，不会两头空）
                    # 引用处正文被改了：重建索引 + **落盘** + 补向量（统一收尾，见 after_rewrite）
                    date_changed = (STATE.db.file_by_id(fid)["doc_date"] or "") != old_date
                    res["rewrite"] = after_rewrite(list(res.get("rewritten") or {}),
                                                   "mention-renamed",
                                                   extra_embed=[fid] if date_changed else None)
                    STATE.events.emit("note.renamed", {
                        "file_id": fid, "old_name": old, "new_name": name,
                        "refs_rewritten": sum(res.get("rewritten", {}).values()),
                        "refs_files": len(res.get("rewritten", {})),
                        "refs_skipped_locked": len(res.get("skipped_locked", []))})
                return self._send({"ok": True, "file": {"id": fid, "name": name},
                                   "bad_sections": bad_sections_in(
                                       STATE.db.get_content(fid, STATE.master()).decode("utf-8", "replace")),
                                   "refs": {"rewritten": res.get("rewritten", {}),
                                            "skipped_locked": res.get("skipped_locked", []),
                                            "tree": res.get("tree"),
                                            "rewrite": res.get("rewrite")}})
            if u.path.startswith("/api/files/") and u.path.endswith("/move"):
                fid = u.path[len("/api/files/"):-len("/move")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                b = self._body()
                target = b.get("folder_id") or STATE.db.folder_by_path(b.get("path") or "")
                if not target or STATE.db.folder_by_id(target) is None:
                    return self._send({"ok": False, "error": "目标文件夹不存在"}, 400)
                frm = row["folder_id"]
                old_date = row["doc_date"] or ""
                _old_rel = tree_rel(fid)
                moved = STATE.db.move_file(fid, target)      # 会顺手改写"路径式引用"
                moved_tree = tree_sync(fid)                  # 先写新位置
                if _old_rel:
                    tree_drop(_old_rel)                      # 再撤旧位置
                new_date = (STATE.db.file_by_id(fid)["doc_date"] or "")
                rids = [rid for rid in (moved or {}).get("rewritten", {}) if rid != fid]
                date_changed = new_date != old_date
                if date_changed:                             # 日期进了嵌入文本 → 本篇向量要重算
                    STATE.indexer.reindex_file(fid)
                rew = after_rewrite(rids, "path-ref-moved",
                                    extra_embed=[fid] if date_changed else None)
                STATE.events.emit("note.moved", {"file_id": fid, "from_folder_id": frm,
                                                 "to_folder_id": target,
                                                 "doc_date": new_date or None})
                return self._send({"ok": True, "folder_id": target, "doc_date": new_date,
                                   "vectors": rew.get("vectors"), "refs_rewritten": len(rids),
                                   "tree": {"moved": moved_tree, "rewrite": rew.get("tree")},
                                   "path": STATE.db.folder_path(target),
                                   "ref_path": STATE.db.display_path(fid)})
            if u.path.startswith("/api/folders/") and u.path.endswith("/rename"):
                fid = u.path[len("/api/folders/"):-len("/rename")]
                if STATE.db.folder_by_id(fid) is None:
                    return self._send({"ok": False, "error": "no such folder"}, 404)
                name = (self._body().get("name") or "").strip()
                if not name:
                    return self._send({"ok": False, "error": "name required"}, 400)
                old = STATE.db.folder_by_id(fid)["name"]
                sub_ids = STATE.db.subtree_file_ids(fid)
                old_rels = [tree_rel(x) for x in sub_ids]
                STATE.db.rename_folder(fid, name)
                # 库文件夹里那棵目录也要跟着改名（以前只改库：磁盘上还是旧目录名，
                # 而"接口是唯一写入口"要求库和磁盘是同一份）2026-09-27 审计 A
                for x in sub_ids:
                    tree_sync(x)
                for rel in old_rels:
                    if rel:
                        tree_drop(rel)
                pruned = prune_dirs([r for r in old_rels if r])
                # 路径变了 → 引用了这棵子树里任何文件的"路径式引用"都要跟着改
                changed = STATE.db.refresh_doc_dates(fid)
                refs = STATE.db.refresh_path_refs(sub_ids)
                rids = [rid for rid in (refs or {}).get("rewritten", {})]
                rew = after_rewrite(sorted(set(changed) | set(rids)), "path-ref-folder-renamed")
                STATE.events.emit("folder.renamed", {"folder_id": fid, "old_name": old,
                                                     "new_name": name,
                                                     "dates_changed": len(changed)})
                return self._send({"ok": True, "path": STATE.db.folder_path(fid),
                                   "dates_changed": len(changed),
                                   "vectors": rew.get("vectors"),
                                   "refs_rewritten": len(rids),
                                   "tree": {"files": len(sub_ids), "pruned": pruned}})
            if u.path == "/api/files":
                b = self._body()
                gen = None
                _fmt = ""
                # content_b64：二进制/文档上传（docx、pdf…）；content 仍是文本路径
                if b.get("content_b64") is not None:
                    if str(b.get("format") or "").strip():
                        return self._send({"ok": False, "error":
                                           "content_b64 是成品字节，format 再用不上：二选一"}, 400)
                    import base64
                    try:
                        data = base64.b64decode(b["content_b64"])
                    except Exception as e:
                        return self._send({"ok": False, "error": "content_b64 解不开：%s"
                                           % str(e)[:80]}, 400)
                    _name = (b.get("name") or "").strip() or "untitled.md"
                else:
                    # 格式**显式规定**（2026-10-03）：名字给了扩展名也行，但两边必须一致
                    _name, _fmt, _err = resolve_create_format(b.get("name") or "",
                                                              b.get("format") or "")
                    if _err:
                        return self._send({"ok": False, "error": _err,
                                           "formats": sorted(create_formats())}, 400)
                    data = (b.get("content") or "").encode("utf-8")
                    # 文档族（Word/ODT/RTF/PDF/EPUB/HTML）→ 用 pandoc/WPS 现场生成真文档
                    if _fmt in _writable_exts():
                        g = _make_doc(data.decode("utf-8", "replace"), _fmt)
                        if g.get("error"):
                            return self._send({"ok": False, "error": "生成 .%s 失败：%s"
                                               % (_fmt, g["error"])}, 400)
                        data = g["data"]
                        gen = {"to": _fmt, "engine": g.get("engine"), "verify": g.get("verify")}
                fid = STATE.db.create_file(b.get("folder_id") or STATE.db.root_folder_id(),
                                           _name, data)
                STATE.indexer.reindex_file(fid)
                # 与 PUT 一致：新建的文件也可能带 @ 提及，边跟着文本走
                body_text = b.get("content") or ""
                pairs, _miss = resolve_mentions(body_text, STATE.db.ref_resolver())
                STATE.db.set_mentions(fid, pairs)
                _tree = tree_sync(fid)                # 新建也由库自己落盘
                row = STATE.db.file_by_id(fid)
                STATE.db.clear_tombstone(
                    row["name"][:-3] if row["name"].lower().endswith(".md") else row["name"])
                vres = embed_now([fid])
                STATE.events.emit("note.created", {
                    "file_id": fid, "folder_id": row["folder_id"], "name": row["name"],
                    "kind": row["kind"], "size": row["size"]})
                return self._send({"ok": True, "file_id": fid, "name": row["name"],
                                   "format": _fmt, "vectors": vres,
                                   "generated": gen,
                                   "bad_sections": bad_sections_in(body_text)}, 201)
            if u.path.startswith("/api/files/") and u.path.endswith("/sections"):
                # **按小节改**（写正文的第三种形态）：只动那一节，别的一个字不碰。
                # 为什么要有：小节是库里的一等概念（@篇#节 / 按节还原 / 反链按节 / relink），
                # 但写只有整篇 —— 改一节得整篇读回、拼好、整篇写回（O(n) + 过缩短闸 + 抢并发写）。
                fid = u.path[len("/api/files/"):-len("/sections")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                if row["kind"] not in ("markdown", "text", "code"):
                    return self._send({"ok": False, "error":
                                       "这一篇不是文本（kind=%s），不能按小节改" % row["kind"]}, 400)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                import mdsec as _ms
                mode = str(b.get("mode") or "replace").strip()
                old_text = STATE.db.get_content(fid, STATE.master()).decode("utf-8", "replace")
                new_text, what, err = _ms.apply_section(
                    old_text, str(b.get("name") or ""), mode,
                    body=str(b.get("text") or ""), new_name=str(b.get("new_name") or ""),
                    after=str(b.get("after") or ""),
                    level=as_int(b.get("level") if b.get("level") is not None else 0, 0,
                                 lo=0, hi=6, name="level"))
                if err:
                    return self._send({"ok": False, "error": err,
                                       "sections": _ms.section_names(old_text)}, 400)
                if new_text == old_text:
                    return self._send({"ok": True, "changed": False, "what": what,
                                       "sections": _ms.section_names(old_text)})
                try:
                    import beforeimage as _bi
                    _bi.save(STATE.db, fid, "section-write",
                             writer=STATE.events.writer, master=STATE.master())
                except Exception:
                    pass
                STATE.db.put_content(fid, new_text.encode("utf-8"), STATE.master())
                res = STATE.indexer.reindex_file(fid)
                res["tree"] = tree_sync(fid)
                anchors = {m["anchor"]: m["id"] for m in STATE.db.mentions(fid)}
                pairs, miss = resolve_mentions(new_text, STATE.db.ref_resolver(), anchors)
                res["mentions"] = STATE.db.set_mentions(fid, pairs)
                if miss:
                    res["unresolved"] = miss[:8]
                res["vectors"] = embed_now([fid])
                res["sections"] = STATE.db.refresh_sections(fid)
                res["bad_sections"] = bad_sections_in(new_text)
                row2 = STATE.db.file_by_id(fid)
                STATE.events.emit("note.updated", {"file_id": fid, "name": row2["name"],
                                                   "size": row2["size"], "mode": mode,
                                                   "section": str(b.get("name") or ""),
                                                   "changed_chars": changed_chars(old_text, new_text)})
                return self._send({"ok": True, "changed": True, "mode": mode, "what": what,
                                   "size": row2["size"], "saved": res})
            if u.path.startswith("/api/files/") and u.path.endswith("/reindex"):
                # **单篇重建派生层**：不改正文，只把这一篇的块/向量/派生层重算。
                # 为什么要它：全库 reindex 太重；靠 PUT 重建会改 mtime、留前像、发"改过了"的事件 —— 语义不对。
                fid = u.path[len("/api/files/"):-len("/reindex")]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                _lk = locked_reply(row)
                if _lk is not None:
                    return self._send(_lk, 403)
                b = self._body()
                if bool(b.get("force_parse")):
                    STATE.db.drop_derived(fid)          # 文档格式：把解析结果丢掉重解析
                res = STATE.indexer.reindex_file(fid, force=True)
                refresh_searcher([fid])
                if bool(b.get("with_vectors")):
                    res["vectors"] = embed_now([fid])
                STATE.events.emit("note.reindexed", {"file_id": fid, "name": row["name"],
                                                     "chunks": res.get("chunks"),
                                                     "force_parse": bool(b.get("force_parse"))})
                return self._send({"ok": True, "reindex": res})
            if u.path == "/api/batch":
                # **统一批量入口**：把一个操作施加到一组目标上。
                # 白名单在 apidoc.BATCH_OPS（唯一真源）；执行是**内部转发到单条路由** ——
                # 所以新增操作零成本可批量，且权限/前像/落盘投影/事件都跟单条一模一样。
                b = self._body()
                op = str(b.get("op") or "").strip()
                spec = apidoc.BATCH_OPS.get(op)
                if not spec:
                    return self._send({"ok": False, "error":
                                       "不认识的批量操作：%s；能批量的有 %s"
                                       % (op or "(空)", "、".join(sorted(apidoc.BATCH_OPS)))}, 400)
                targets = b.get("ids") or b.get("paths") or []
                if not isinstance(targets, list) or not targets:
                    return self._send({"ok": False, "error":
                                       "缺 ids（file 类目标）或 paths（path 类目标）"}, 400)
                args = b.get("args") if isinstance(b.get("args"), dict) else {}
                on_error = str(b.get("on_error") or "continue")
                dry = bool(b.get("dry_run"))
                port = self.server.server_address[1]
                done, failed, results = 0, [], []
                for t in targets[:500]:
                    tid = str(t)
                    url = "http://127.0.0.1:%d%s" % (
                        port, spec["route"].replace("{id}", urlquote(tid)))
                    if dry:
                        results.append({"target": tid, "would": "%s %s" % (spec["method"], url),
                                        "args": args})
                        continue
                    try:
                        data = (json.dumps(args, ensure_ascii=False).encode("utf-8")
                                if spec["method"] != "GET" else None)
                        rq = urllib.request.Request(
                            url, data=data, method=spec["method"],
                            headers={"Content-Type": "application/json", "X-Writer": "batch"})
                        with urllib.request.urlopen(rq, timeout=300) as r:
                            j = json.loads(r.read().decode("utf-8", "replace") or "{}")
                        done += 1
                        results.append({"target": tid, "ok": True,
                                        "result": {k: j.get(k) for k in list(j)[:6]}})
                    except urllib.error.HTTPError as e:
                        failed.append({"target": tid, "status": e.code,
                                       "error": e.read().decode("utf-8", "replace")[:200]})
                        if on_error == "stop":
                            break
                    except Exception as e:
                        failed.append({"target": tid,
                                       "error": "%s: %s" % (type(e).__name__, str(e)[:120])})
                        if on_error == "stop":
                            break
                with WRITE_LOCK:                                  # 本入口唯一的库内写动作
                    STATE.events.emit("batch.done", {"op": op, "count": len(targets),
                                                     "done": done, "failed": len(failed),
                                                     "dry_run": dry})
                return self._send({"ok": not failed, "op": op, "count": len(targets),
                                   "done": done, "failed": failed, "dry_run": dry,
                                   "on_error": on_error, "results": results[:50]})
            if u.path == "/api/tables":
                b = self._body()
                src = (b.get("src") or "").strip()
                if not src:
                    return self._send({"ok": False,
                                       "error": "缺 src（磁盘上的 csv/tsv/xlsx 路径）"}, 400)
                import datatables as _dt
                import importer as _imp
                # 原文件留档：照旧当"文件"进库（默认不进检索）—— 表只是它的派生视图，随时能重建
                keep = _imp.import_path(STATE.db, src, under=(b.get("under") or ""),
                                        events=STATE.events, rag_off=True)
                ext = os.path.splitext(src)[1].lower()
                if ext in (".xlsx", ".xlsm"):        # 一个工作簿 → 每个 sheet 一张表
                    res = _dt.create_from_xlsx(STATE.db, src, title=(b.get("title") or ""),
                                               replace=bool(b.get("replace")),
                                               only_sheet=(b.get("sheet") or ""))
                    if "error" in res:
                        return self._send({"ok": False, "table": res, "file": keep}, 400)
                    for t in res.get("tables", []):
                        STATE.events.emit("table.created", {"name": t["name"], "rows": t["rows"],
                                                            "source": src, "sheet": t["sheet"]})
                    return self._send({"ok": True, "file": keep, "tables": res["tables"],
                                       "count": res["count"], "sheets": res["sheets"]})
                res = _dt.create_from_csv(STATE.db, src, title=(b.get("title") or ""),
                                          replace=bool(b.get("replace")),
                                          delimiter=(b.get("delimiter") or None))
                if "error" in res:
                    return self._send({"ok": False, "table": res, "file": keep}, 400)
                STATE.events.emit("table.created", {"name": res["name"], "rows": res["rows"],
                                                    "source": src, "replaced": res["replaced"]})
                return self._send({"ok": True, "file": keep, "table": res})
            if u.path == "/api/table_drop":
                import datatables as _dt
                b = self._body()
                res = _dt.drop_table(STATE.db, (b.get("name") or "").strip())
                if "error" in res:
                    return self._send({"ok": False, **res}, 404)
                return self._send({"ok": True, **res})
            if u.path == "/api/import/upload":
                # 浏览器选好文件 → 按字节传进来（不需要磁盘路径）
                r = import_upload(self._body())
                if r.get("ok"):
                    r["tree"] = tree_sync_after_import(r)     # 每一篇都要落到库文件夹（2026-09-30）
                return self._send(r, 200 if r.get("ok") else (403 if r.get("locked") else 400))
            if u.path == "/api/import":
                b = self._body()
                src = (b.get("src") or "").strip()
                if not src:
                    return self._send({"ok": False, "error": "缺 src（磁盘路径：文件或目录）"}, 400)
                import importer as _imp
                private = bool(b.get("private"))        # 私密进库：照进，但按 private 上权限
                if private and STATE.master() is None:
                    # 没解锁=没有主密钥=没法加密。宁可不导，也不留一份明文假私密
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "hint": "库未解锁：私密内容不落库（先在页面点 🔒 输入库密码）"}, 403)
                res = _imp.import_path(STATE.db, src, under=(b.get("under") or ""),
                                       tag=(b.get("tag") or ""), events=STATE.events,
                                       rag_off=not bool(b.get("rag")),   # 默认不进检索（辞玖 2026-09-21）
                                       overwrite=bool(b.get("overwrite")),
                                       visibility="private" if private else "internal",
                                       master=STATE.master() if private else None)
                if private:
                    res["private"] = int(res.get("imported") or 0) + int(res.get("updated") or 0)
                # 覆盖更新过的文件：正文换了 → 派生层跟上（见 reindex_after_import）
                reindex_after_import(res)
                refresh_after_import(res)
                res["tree"] = tree_sync_after_import(res)     # 每一篇都要落到库文件夹（2026-09-30）
                return self._send({"ok": "error" not in res, **res}, 200 if "error" not in res else 400)
            if u.path == "/api/before-images/restore":
                import beforeimage as _bi
                b = self._body()
                r = _bi.restore(STATE.db, image_id=str(b.get("id") or ""),
                                file_id=str(b.get("file_id") or ""),
                                writer=STATE.events.writer, master=STATE.master())
                if not r.get("ok"):
                    return self._send(r, 404)
                fid_r = r["file_id"]
                STATE.events.emit("note.restored", {"file_id": fid_r, "from": r.get("from"),
                                                    "mode": r.get("mode"), "reason": r.get("reason")})
                # 恢复也是"库内正文变了"：落盘 + 重建 @提及边 + 补向量
                # （以前只重索引：磁盘还是"删掉/覆盖后"的样子，语义检索也搜不到这一篇）2026-09-27 审计 K
                try:
                    texts = STATE.db.get_content(fid_r, STATE.master()).decode("utf-8", "replace")
                    pairs, _miss = resolve_mentions(texts, STATE.db.ref_resolver())
                    STATE.db.set_mentions(fid_r, pairs)
                except Exception as e:
                    r["mentions_error"] = "%s: %s" % (type(e).__name__, str(e)[:80])
                if STATE.indexer:
                    try:
                        STATE.indexer.reindex_file(fid_r)
                    except Exception:
                        pass
                r["tree"] = tree_sync(fid_r)
                embed_now([fid_r])
                if STATE._searcher:
                    STATE._searcher.invalidate()
                return self._send(r)
            if u.path == "/api/before-images/purge":
                # 按条彻底删除（辞玖 2026-10-15 22:06）：
                # 点中的那几条会被展开成"整篇"，
                # 连它**衍生的各个版本**一起清掉。
                import beforeimage as _bi
                b = self._body()
                _ids = b.get("ids") or []
                if isinstance(_ids, str):
                    _ids = [_ids]
                if b.get("id"):
                    _ids = list(_ids) + [b["id"]]
                r = _bi.purge_records(STATE.db, ids=_ids, file_id=str(b.get("file_id") or ""),
                                      names=b.get("names") or [])
                STATE.events.emit("bin.purge", {"removed": r.get("removed"), "files": r.get("files")})
                return self._send(r)
            if u.path == "/api/before-images/prune":
                import beforeimage as _bi
                # 前像是明文落在 data/前像/ 的：锁着的时候不许清，免得悄悄删掉你看不见的那些。
                # 注意条件是"**设过密码**且当前锁着"—— 没设过密码的库一切都可见，
                # 用 master() is None 判会把它永久 403（2026-09-28 自查抓到的）。
                if STATE.db.vault_kdf() is not None and STATE.master() is None:
                    return self._send({"ok": False, "error": "locked", "locked": True,
                                       "hint": "先解锁再清理（前像里有私密篇的明文副本）"}, 403)
                b = self._body()
                # 别写 `or 30`：days=0 是假值，会被悄悄当成 30（2026-09-28 自查抓到的）
                _raw = b.get("days")
                days = 30 if _raw in (None, "") else int(_raw)
                if days < 1:
                    return self._send({"ok": False, "error": "days 至少 1"}, 400)
                r = _bi.prune(STATE.db, days=days)
                STATE.events.emit("bin.prune", {"days": days, "removed": r.get("removed"),
                                                "kept": r.get("kept")})
                return self._send(r)
            if u.path == "/api/debug/stack-dump":
                # 卡死抓栈（本地诊断）：body {"after":60,"repeat":1} → 60 秒后把所有线程的栈打到日志。
                # 为什么要这个：2026-09-25 实测遇到"进程在、端口在监听、CPU 跑满、但不接新连接"
                # —— 那种时候任何接口都叫不应，只能提前把定时器装好。
                import faulthandler
                b = self._body()
                after = as_int(b.get("after"), 60, lo=0, hi=86400, name="after")
                rep = str(b.get("repeat", 1)) not in ("0", "false", "no")
                if after <= 0:
                    faulthandler.dump_traceback()
                    return self._send({"ok": True, "dumped": "now"})
                faulthandler.cancel_dump_traceback_later()
                faulthandler.dump_traceback_later(after, repeat=rep)
                return self._send({"ok": True, "after": after, "repeat": rep})
            if u.path == "/api/maintenance/frontmatter-backfill":
                # 把库里**已有**笔记的文件头 YAML 属性补解析一遍（解析只在写入时发生）2026-09-27
                # 范围必须显式：不给就是 100 篇；全库要 {"all": true}（2026-09-30 体检）。
                b = self._body()
                lim = 1000000 if b.get("all") else as_int(b.get("limit"), 100, lo=1,
                                                          hi=1000000, name="limit")
                ids = [r["id"] for r in STATE.db.conn.execute(
                    "SELECT id FROM files WHERE kind IN ('markdown','text','code') LIMIT ?", (lim,))]
                done = failed = 0
                for fid in ids:
                    try:
                        raw = STATE.db.get_content(fid, STATE.master())
                        STATE.db._apply_frontmatter(fid, raw)
                        done += 1
                    except Exception:
                        failed += 1
                return self._send({"ok": True, "scanned": len(ids), "done": done, "failed": failed})
            if u.path == "/api/maintenance/private-rekey":
                # 一次性维修：密钥丢了之后，用磁盘明文把私密篇重新加密（必须已解锁）
                b = self._body()
                r = private_rekey(str(b.get("src") or ""))
                return self._send(r, 200 if r.get("ok") else 403)
            if u.path == "/api/mirror/config":
                b = self._body()
                kv = {}
                for k, mk in (("library_dir", "library_dir"), ("vault_dir", "vault_dir")):
                    if b.get(k) is not None:
                        kv[mk] = str(b[k]).strip()
                for k, mk in (("ignore", "mirror_ignore"), ("private", "mirror_private"),
                              ("append_only", "mirror_append_only"),
                              ("backup_root", "mirror_backup_root")):
                    if b.get(k) is not None:
                        v = b[k] if isinstance(b[k], list) else str(b[k]).split(",")
                        kv[mk] = ",".join(str(x).strip() for x in v if str(x).strip())
                if b.get("enabled") is not None:            # 镜像管线总开关
                    kv["mirror_enabled"] = ("1" if b["enabled"] not in (False, 0, "0", "false", "off")
                                            else "0")
                if kv:
                    STATE.db.set_manifest(**kv)
                return self._send({"ok": True, **mirror_conf()})
            if u.path == "/api/mirror/sync":
                # 2026-09-30 归档：库是唯一真相源（09-27 起），双向镜像管线退役。
                # 留这条路由是为了"叫得应"：回 410 + 指路，比 404 清楚。
                # 实现与自测都挪到了 archive/2026-09-30-废弃能力/（README 里有取回步骤）。
                return self._send({"ok": False, "retired": True,
                                   "error": "镜像同步管线已退役（2026-09-30 归档）",
                                   "hint": "库内写入现在只落库文件夹；要恢复见 "
                                           "archive/2026-09-30-废弃能力/README.md"}, 410)
            if u.path == "/api/rag/batch":
                b = self._body()
                on = bool(b.get("on", True))
                ids = [str(x) for x in (b.get("ids") or [])]
                if b.get("folder_id"):
                    ids += STATE.db.folder_file_ids(str(b["folder_id"]),
                                                    recursive=bool(b.get("recursive", True)))
                seen, uniq = set(), []
                for i in ids:
                    if i not in seen:
                        seen.add(i)
                        uniq.append(i)
                if not uniq:
                    return self._send({"ok": False, "error": "缺 ids 或 folder_id"}, 400)
                changed, locked, chunks = 0, 0, 0
                for fid in uniq:
                    row = STATE.db.file_by_id(fid)
                    if row is None:
                        continue
                    if is_locked(row):
                        locked += 1
                        continue
                    STATE.db.set_rag(fid, on)
                    res = STATE.indexer.reindex_file(fid)   # on → 解析+切块；off → 清块清向量
                    chunks += res.get("chunks", 0)
                    changed += 1
                vec = embed_now(uniq) if on else {}
                if not on:
                    refresh_searcher(uniq)
                STATE.events.emit("note.rag.batch", {"count": changed, "on": on, "locked": locked})
                return self._send({"ok": True, "on": on, "count": changed, "locked_skipped": locked,
                                   "chunks": chunks, "vectors": vec})
            if u.path == "/api/folders":
                b = self._body()
                parent = b.get("parent_id") or STATE.db.folder_by_path(b.get("path") or "") \
                    or STATE.db.root_folder_id()
                fid = STATE.db.create_folder(parent, (b.get("name") or "新建文件夹").strip())
                STATE.events.emit("folder.created", {"folder_id": fid, "parent_id": parent,
                                                     "name": STATE.db.folder_by_id(fid)["name"]})
                return self._send({"ok": True, "folder_id": fid}, 201)
            if u.path == "/api/reindex":
                b = self._body()
                # 默认只重建"变过的"（索引签名不同）；真全量重建要显式 {"full": true}
                only_changed = not bool(b.get("full"))
                with STATE.events.action("index.rebuild", reason=(b.get("reason") or "manual"),
                                         expect_within_ms=600000):
                    res = STATE.indexer.reindex_all(only_changed=only_changed)
                vec = {}
                if b.get("with_vectors"):
                    from retriever import VectorIndexer
                    vec = VectorIndexer(STATE.db).index(progress=False)
                    if STATE._searcher:
                        STATE._searcher.invalidate()
                return self._send({"ok": True, "reindex": res, "only_changed": only_changed,
                                   "vectors": vec})
            return self._send({"ok": False, "error": "not found"}, 404)
        except (BadName, BadRequest) as e:       # 名字/入参不合法 → 400 + 人话（不是 500）
            return self._send({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_DELETE(self):
        _cache_clear()          # 写操作：缓存立刻失效
        self._mark_writer()
        u = urlparse(self.path)
        try:
            if u.path.startswith("/api/folders/"):
                fid = u.path.rsplit("/", 1)[-1]
                row = STATE.db.folder_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such folder"}, 404)
                rel_dir = STATE.db.folder_path(fid)
                try:
                    STATE.db.delete_folder(fid)
                except ValueError as e:
                    return self._send({"ok": False, "error": str(e)}, 400)
                # 库文件夹里那个目录（可能还留着库里不认识的游离文件）也收掉：
                # 不收的话镜像会一轮轮把它们往 vault 复制，而库里根本没有它们（2026-09-27 审计 E）
                dropped = tree_drop_dir(rel_dir)
                STATE.events.emit("folder.deleted", {"folder_id": fid, "name": row["name"]})
                return self._send({"ok": True, "deleted": fid, "tree": dropped})
            if u.path.startswith("/api/files/"):
                fid = u.path.rsplit("/", 1)[-1]
                row = STATE.db.file_by_id(fid)
                if row is None:
                    return self._send({"ok": False, "error": "no such file"}, 404)
                name = row["name"]
                import beforeimage as _bi               # 高风险：删除 → 先留前像（可撤销）
                img = _bi.save(STATE.db, fid, "delete", writer=STATE.events.writer,
                               master=STATE.master())
                _rel = tree_rel(fid)
                STATE.db.delete_file(fid)
                if _rel:
                    tree_drop(_rel)                     # 树里那份也跟着撤（别留孤儿）
                STATE.events.emit("note.deleted", {"file_id": fid, "name": name,
                                                   "before_image": img.get("id") if img.get("ok") else None})
                return self._send({"ok": True, "deleted": fid, "before_image": img})
            return self._send({"ok": False, "error": "not found"}, 404)
        except (BadName, BadRequest) as e:       # 入参不合法 → 400 + 人话
            return self._send({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            return self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)


def port_answering(host: str, port: int, timeout: float = 0.8) -> bool:
    """端口上已经有人在答吗？（TCP 连一下，不发 HTTP 请求）。

    用途：库是单线程服务，**忙的时候从外面看像挂了**；
    宿主侧的保活探针一旦超时就会再拉一个实例，
    而那一个会先把 sqlite 和 3GB 模型全开一遍（还会 database is locked）。
    所以起重活之前先问一句：有人在答就别做重活。（2026-09-25）
    """
    sk = socket.socket()
    sk.settimeout(timeout)
    try:
        sk.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            sk.close()
        except Exception:
            pass


def serve(host: str = "127.0.0.1", port: int = 8848, data_dir: str | None = None):
    """**读并发、写串行**（2026-10-01 辞玖：「轻小请求先做」）。

    为什么改：单线程下"一个慢活把整库堵住"——实测 4.1s 的转换导出跑着时，
    一个 /api/health 要等 3506ms；界面上表现为"点哪都不动"。
    怎么做的（都是上面那句注释当年自己写下的正规做法，不是硬开线程）：
      · 每个线程一条自己的 sqlite 连接（`ThreadDB` 门面，上层代码一行没改）
      · 写请求拿全局 `WRITE_LOCK` → 写与写仍然串行（前像 → 写入 → 重解析 → 落盘 是一条龙）
      · 检索链拿 `SEARCH_LOCK`（共享缓存），锁序永远是 先写锁再检索锁
      · 工人数有上限（8），超过排队；每请求的写入者标签改成线程内（否则并发会串味）
    """
    global STATE
    # 卡死诊断（选配）：AI_NOTES_DUMP_STACK=180 → 每 180 秒把所有线程的栈打到 stderr。
    # 库是单线程的，"卡住了在哪" 只能靠这个看（2026-09-25 实测遇到过一次：
    # 进程还在、端口还在监听，但不接新连接、CPU 一直跑满）。
    _dump = os.environ.get("AI_NOTES_DUMP_STACK")
    if _dump and _dump.isdigit():
        import faulthandler
        faulthandler.dump_traceback_later(int(_dump), repeat=True)
    # 先问一句端口：已经有人在答（库在跑，只是可能很忙）就直接退出，
    # 别去开 sqlite / 加载模型（那一套起来要 3GB，还会把库锁住）。
    if port_answering(host, port):
        sys.stderr.write("端口 %d 已经在答（库已在跑），本次不起第二个实例\n" % port)
        sys.exit(2)
    STATE = State(data_dir or DEFAULT_DATA_DIR)
    STATE.db.purge_sensitive_chunks()          # 冷启动一律锁定态

    # 端口独占：Python 的 HTTPServer 默认 allow_reuse_address=1，在 Windows 上
    # 允许多个进程绑同一端口（谁最后绑谁收连接）—— 于是"探针不通就再拉一个"会静默堆出
    # 好几个实例同时监听 8848（2026-09-14 实测堆到 4 个）。关掉它，第二个实例直接报
    # 端口占用退出，宿主那边立刻能看清"已经在跑"。
    class _OneServer(ThreadingHTTPServer):
        """**有界线程池**：读并发、写串行（写锁在 Handler 里）。

        有界很重要：ThreadingHTTPServer 默认**一个连接一条线程**（无上限）——
        本机单用户用不上，还会让每线程一条 sqlite 连接的数量失控。超过就排队，不拒。
        """
        max_workers = 8
        allow_reuse_address = False
        # backlog（2026-09-20 压测发现）：Python 的 TCPServer 默认 request_queue_size = 5 ——
        # 第 6 个及以后的并发连接会被系统**直接拒接**（WinError 10061），
        # 端到端表现是"库不可达"（实测：直连 8 路开始零星失败、16 路失败 3/16、32 路失败 23/32）。
        # 库仍然是**单线程串行处理**（上面那条理由不变），这里只是把"等待队列"开大：
        # 并发来了就排队，而不是被拒。128 远超本机实际并发（页面 3 秒一轮 + agent 偶发调用）。
        request_queue_size = 128
        daemon_threads = True

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._slots = threading.BoundedSemaphore(self.max_workers)

        def process_request(self, request, client_address):
            self._slots.acquire()                      # 满 8 个就等（不是拒）
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._slots.release()

    # 启动预热：把嵌入模型与精排模型先加载好。
    # 只加载模型、不碰 sqlite —— 库是单线程服务，连接不能跨线程用；
    # 预热只做"把权重读进显存"，首次检索就不用干等这一两分钟。
    def _warm():
        # 顺序加载（实测结论，别改）：嵌入 16.2s + 精排 11.6s ≈ 28 秒。
        # 2026-09-25 试过"两个模型并行加载"：反而要 **56.3 秒**（两份权重同时压 CPU/磁盘，互相抢）—— 已回退。
        def warm_embed():
            try:
                from embedder import get_embedder
                e = get_embedder()
                e._ensure()                   # 必须与检索用的是同一实例，否则预热白做
                e.encode_one("预热")           # 顺手做一次真推理：把首次调用的开销也提前付掉
                if not e.ready:
                    sys.stderr.write("[warm] 预热完了但模型不在？（应该不会发生）\n")
            except Exception as e:
                sys.stderr.write("[warm] 嵌入模型预热失败（不影响检索，首次会现加载）：%s\n" % str(e)[:120])

        def warm_rerank():
            try:
                from reranker import get_reranker
                rr = get_reranker()
                if rr.available:
                    rr._ensure()
                    # 只预热一条（不要用真实批量！）：2026-09-25 试过用 40 条预热，
                    # 结果它跟真查询在 CPU 上抢着算，第一查反而拖到 247 秒。
                    # 第一个真正精排的查询要现付约 7 秒（一次性），这个代价可以接受。
                    rr.score("预热", ["预热文本"])
            except Exception as e:
                sys.stderr.write("[warm] 精排预热失败（不影响检索，首次会现加载）：%s\n" % str(e)[:120])

        warm_embed()
        sys.stderr.write("[warm] 嵌入模型就绪（精排后台加载中）\n")
        # 精排模型放**后台**：它不挡任何请求（_rerank 遇到没就绪就跳过这一查）
        threading.Thread(target=warm_rerank, daemon=True).start()

    _se = STATE.searcher          # 先建好：它会拿到共享 embedder/reranker，再启预热线程就不会抢着各建一个
    threading.Thread(target=_warm, daemon=True).start()

    try:
        srv = _OneServer((host, port), Handler)
    except OSError as e:
        sys.stderr.write("端口 %d 已被占用（库已在跑？）：%s\n" % (port, e))
        sys.exit(2)                      # 非 0：让拉起方一眼看出"这次没起来"
    # 向量缓存先读进内存：**必须在主线程**（sqlite 连接不跨线程）。
    # 此时端口已 bind（宿主探针看到"在"）、还没开始收请求 —— 第一查就不用现读 11.5k 条（2026-09-25）
    try:
        STATE.searcher._load_cache()
    except Exception as e:
        sys.stderr.write("[warm] 向量缓存预热失败（不影响检索）：%s\n" % str(e)[:120])
    start_vector_worker()          # 后台补向量线程（重活不占请求线程）2026-09-30
    print("ai-notes API → http://%s:%d  (data=%s)" % (host, port, STATE.db.path))
    print("  GET /api/health | /api/tree | /api/files/<id> | /api/search?q=")
    srv.serve_forever()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("AI_NOTES_PORT", 8848)))
    ap.add_argument("--data", default=None)
    a = ap.parse_args()
    serve(port=a.port, data_dir=a.data)
