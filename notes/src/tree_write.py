"""库内改动**写回文件树**（辞玖 2026-09-25："库中文件等内容的写入等是要通过接口实现，不能绕道"）。

意思是：**接口是唯一写入口** —— 谁要改库里的内容，调接口（`PUT/POST /api/files` 等）；
文件树（库文件夹 / vault）是**库自己维护的投影**，不是给人绕道写的。
要做到这一点，接口写完之后必须**由库自己把文件落到树里**，否则：
  · 库内改了、磁盘没变 → 镜像还会把磁盘那份当成"新的"覆盖回来（互相打架）；
  · 别人以为磁盘是真相，看到的却是旧的。

所以这个模块干两件事：
  `write_through(db, file_id)`  —— 把这一篇的当前内容写进库文件夹（私密篇写**密文**），mtime 置为当下；
  `remove_from_tree(path)`      —— 删/改名时把旧路径从树里撤掉（先留前像，见 beforeimage）。
库文件夹 → vault 那一步交给镜像（谁新谁赢），库自己不直接碰 vault。
"""
from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

from db import Database, local_iso
try:
    from db import sha256_bytes
except ImportError:                    # 兜底：库里没导出就自己算
    import hashlib as _hl
    def sha256_bytes(b):
        return 'sha256:' + _hl.sha256(b).hexdigest()
import vaultcrypto


def library_dir(db: Database) -> str:
    """库文件夹（manifest 里配的；没配就空串 = 不写盘）"""
    return (db.manifest() or {}).get("library_dir") or ""


def _inside(root: Path, dst: Path) -> bool:
    """dst 必须真的在 root 里面（防「名字里带 .. 或盘符」把落点甩出去）。"""
    try:
        r = os.path.abspath(str(root))
        d = os.path.abspath(str(dst))
        return os.path.commonpath([r, d]) == r
    except ValueError:                    # 不同盘符 → commonpath 抛错 = 一定在外面
        return False


def safe_rel(rel) -> str:
    """把库内相对路径规整一遍；只要出现退路（`..`）或绝对路径就返回空串（= 拒绝）。

    注意：`Path(root) / 'C:/x'`、`Path(root) / '/x'` 在 pathlib 里会**丢掉左边**，
    所以光靠 `_inside` 兜不够 —— 这里先按段检查。
    """
    s = str(rel or "").replace("\\", "/")
    if s.startswith("/") or (len(s) > 1 and s[1] == ":"):
        return ""
    out = []
    for x in s.split("/"):
        if x in ("", "."):
            continue
        if x == "..":
            return ""
        out.append(x)
    return "/".join(out)


def rel_of(db: Database, file_id: str) -> str:
    """这一篇在库文件夹里的相对路径（去掉库内那层 vault/ 包装，与 materialize/mirror 同口径）"""
    row = db.file_by_id(file_id)
    if row is None:
        return ""
    p = db.folder_path(row["folder_id"]) if row["folder_id"] else ""
    parts = [x for x in p.split("/") if x]
    if parts and parts[0] == "vault":
        parts = parts[1:]
    return safe_rel("/".join([x for x in ("/".join(parts), row["name"]) if x]))


def write_through(db: Database, file_id: str, master: bytes | None = None,
                  data: bytes | None = None) -> dict:
    """把这一篇写进库文件夹。返回 {ok, path?, bytes?, why?}；没配库文件夹就 no-op。"""
    root = library_dir(db)
    if not root:
        return {"ok": False, "why": "no library_dir"}
    rel = rel_of(db, file_id)
    if not rel:
        return {"ok": False, "why": "no such file"}
    row = db.file_by_id(file_id)
    # ⚠️ 倒转（2026-10-14）修的一个真 bug：**内容必须用调用方手上那份**。
    #    原来这里一律 `db.get_content(...)`——而倒转开了 disk_first 之后，
    #    get_content 会**从盘上读**，于是"把盘上旧内容又写回盘上"：
    #    实测 put_content(v2) 之后盘上仍是 v1，**新内容永远落不了盘**（静默数据错）。
    #    所以 put_content 那条路把刚写好的 bytes 直接传进来（data 非空即用）。
    if row["enc_algo"]:                      # 私密：树里放的是**密文**（锁才有意义）
        data = _sealed_bytes(db, file_id)    # 密文就在库里，**不需要主密钥**（锁着设为私密也要落盘）
        if not data:
            # 库里没有密文了（例如"库内那份被清掉"）→ 退回解密那条路，
            # 然后用刚生成的密文重新封一份，**盘上永远只放密文**。
            #
            # ⚠️ 2026-10-15 修：原来这里直接 `data = db.get_content(file_id, master)`
            #    拿到的**是明文**，紧接着就写到盘上 —— 等于把私密正文明文落盘，
            #    直接违背"盘上永无明文"。实测：清掉库内那份密文后调 write_through，
            #    盘上写出 28 字节明文（而回读解密 InvalidTag）。
            if not master:
                return {"ok": False, "why": "locked"}
            plain = db.get_content(file_id, master)
            key = vaultcrypto.new_key()
            data = vaultcrypto.seal(key, plain)
            # 顺手把库内那份也补回去（省得每次都现封一次；补不上不算失败）
            try:
                db._write_blob(file_id, data, sha256_bytes(plain), len(plain),
                               enc_algo=row["enc_algo"],
                               key_ref=vaultcrypto.wrap_key(master, key))
            except Exception:
                pass
    elif data is None:
        # ⚠️ 不传 data 时**不能**走 db.get_content()（2026-10-15 修）。
        #    disk_first=1 之后 get_content **优先读盘** —— 于是"把盘上那份读回来、
        #    再原样写回去"，**报 ok 但什么都没修**。
        #    实测（tools/acc_write_through.py ⑧b）：盘上被弄坏成 "坏的"，调补投影
        #    → 返回 {'ok': True, 'bytes': 6}（写的就是那 6 字节"坏的"），盘上依旧坏。
        #
        #    正确做法：**从库里那份拿**（密文走 _sealed_bytes，明文读 content/池），
        #    不经过任何"优先读盘"的路径。
        data = _library_bytes(db, file_id)
    dst = Path(root) / rel.replace("/", os.sep)
    if not _inside(Path(root), dst):                 # 兜底：绝不在库文件夹外面落盘
        return {"ok": False, "why": "escape", "rel": rel}
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(dst.suffix + ".wt-tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    # Windows：目标文件正被人打开（读/改）时 os.replace 会撞共享冲突 —— 重试两次再认输。
    # 认输也不能静默：调用方会把 why 记进 tree_errors（2026-10-04：撞过一次就没再写，盘上一直是旧的）
    last = None
    for attempt in range(3):
        try:
            os.replace(tmp, dst)
            last = None
            break
        except OSError as e:
            last = e
            if attempt < 2:
                time.sleep(0.15 * (attempt + 1))
    if last is not None:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return {"ok": False, "why": "落盘被占用(%s): %s" % (type(last).__name__, str(last)[:60]),
                "rel": rel}
    return {"ok": True, "path": str(dst), "bytes": len(data), "rel": rel}


def append_through(db: Database, file_id: str, data: bytes, master: bytes | None = None) -> dict:
    """把一段字节**追加**到库文件夹里那一份的尾部（日志转录用）。

    什么时候退回整份重写（`write_through`）：
      · 私密篇（树里是密文，追加会写坏）
      · 盘上那份不在，或长度与"库里追加前的长度"对不上（说明已有漂移 —— 别在坏基础上接着追加）
    """
    root = library_dir(db)
    if not root:
        return {"ok": False, "why": "no library_dir"}
    rel = rel_of(db, file_id)
    if not rel:
        return {"ok": False, "why": "no such file"}
    row = db.file_by_id(file_id)
    if row["enc_algo"]:
        return write_through(db, file_id, master)
    dst = Path(root) / rel.replace("/", os.sep)
    if not _inside(Path(root), dst):
        return {"ok": False, "why": "escape", "rel": rel}
    try:
        disk_len = dst.stat().st_size
    except OSError:
        disk_len = -1
    # ⚠️ 判据原先写的是 `disk_len != len(db.get_content(...)) - len(data)` —— **自指**：
    #    `get_content` 优先读盘（disk_first=1），读回来的就是盘上那份，
    #    即右边 = disk_len + len(data) ≠ disk_len → **永远成立** → 追加永远退回整份重写。
    #    （2026-10-15 由 `tools/smoke_append.py` 抓到：「盘上是追加模式」那条一直失败。）
    #
    #    正确判据要拿"**库里追加后的**长度"（`row["size"]` 记的就是明文长度）比：
    #      盘 == 追加后的长度 → 这次追加**已经落过了**（put_content 的落盘投影先做过）
    #                          → 什么都不用做
    #      盘 == 追加前的长度 → 正是"按字节追加"该处理的正常情形 → 继续往下追加
    #      其它               → 缺失/漂移 → 整份写一次（别在坏基础上接）
    if row is not None and row["size"]:
        if disk_len == int(row["size"]):
            return {"ok": True, "path": str(dst), "appended": 0, "rel": rel,
                    "mode": "already", "note": "盘上已是追加后的那一份，无需再写"}
        if disk_len != int(row["size"]) - len(data):
            return write_through(db, file_id, master)  # 缺失/漂移 → 整份写一次
    elif disk_len != len(db.get_content(file_id, master)) - len(data):
        return write_through(db, file_id, master)
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(dst, "ab") as f:
            f.write(data)
    except OSError as e:
        return {"ok": False, "why": "追加被占用(%s): %s" % (type(e).__name__, str(e)[:60]), "rel": rel}
    return {"ok": True, "path": str(dst), "appended": len(data), "rel": rel, "mode": "append"}


def _library_bytes(db: Database, file_id: str) -> bytes:
    """库里那份的**原始字节**（明文走 content/池；密文走 _sealed_bytes）。

    存在的理由：`write_through` 在调用方没给 data 时，**必须从库里拿** ——
    不能走 `db.get_content()`，因为倒转开了 `disk_first` 之后它会优先读盘，
    那就变成"把盘上那份读回来再写回去"，报 ok 却什么都没改（2026-10-15 实测）。
    """
    row = db.file_by_id(file_id)
    if row is None:
        return b""
    if row["enc_algo"]:
        return _sealed_bytes(db, file_id)
    # ⚠️ **绝不能调 db.get_content()** —— 它优先读盘（disk_first=1），
    #    那就又把盘上那份读回来了（我在这个函数的第一版里正是这么写的，白改一轮）。
    #    直接读列/池：这是"库里那份"的原始字节，与盘无关。
    r = db.conn.execute(
        "SELECT storage, content, storage_ref FROM files WHERE id=?", (file_id,)).fetchone()
    if r is None:
        return b""
    if r["storage"] == "pool":
        p = Path(db.data_dir) / "pool" / (r["storage_ref"] or "")
        return p.read_bytes() if p.is_file() else b""
    raw = r["content"]
    if raw is None:
        return b""
    if isinstance(raw, bytes):
        return raw
    return raw.encode("utf-8")


def _sealed_bytes(db: Database, file_id: str) -> bytes:
    """加密行在库里的原样字节（inline 取 content 列 / pool 取池文件）"""
    r = db.conn.execute("SELECT storage, content, storage_ref FROM files WHERE id=?",
                        (file_id,)).fetchone()
    if r is None:
        return b""
    if r["storage"] == "pool":
        p = Path(db.data_dir) / "pool" / (r["storage_ref"] or "")
        return p.read_bytes() if p.is_file() else b""
    return r["content"] or b""


def deletions_path(db: Database):
    return Path(db.data_dir) / "deletions.jsonl"


def note_deletion(db: Database, rel: str, writer: str = "") -> None:
    """把"接口删了这篇"记进删除账本（镜像据此把 vault 那份也撤掉；可审计）"""
    try:
        with io.open(deletions_path(db), "a", encoding="utf-8") as f:
            f.write(json.dumps({"rel": rel, "at": local_iso(), "writer": writer,
                                "applied": False}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def pending_deletions(db: Database) -> list:
    p = deletions_path(db)
    if not p.is_file():
        return []
    out = []
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if not rec.get("applied"):
                out.append(rec)
    return out


def mark_deletions_applied(db: Database, rels) -> None:
    p = deletions_path(db)
    if not p.is_file():
        return
    done = set(rels)
    keep = []
    with io.open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("rel") in done:
                rec["applied"] = True
                rec["applied_at"] = local_iso()
            keep.append(rec)
    with io.open(p, "w", encoding="utf-8") as f:
        for rec in keep:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _prune_up(rootp: Path, d: Path) -> int:
    """从 d 往上收掉空目录（一直收到 rootp 为止），返回收掉几个"""
    n = 0
    while d != rootp and rootp in d.parents:
        try:
            if any(d.iterdir()):
                break
            d.rmdir()
            n += 1
        except OSError:
            break
        d = d.parent
    return n


def prune_empty_dirs(db: Database, rels) -> int:
    """这批文件所在的目录链里，空掉的那些收掉（改名/移动后别在库文件夹里留一串空壳目录）"""
    root = library_dir(db)
    if not root:
        return 0
    rootp = Path(root)
    n, seen = 0, set()
    for rel in rels or []:
        if not rel:
            continue
        d = (rootp / str(rel).replace("/", os.sep)).parent
        if d in seen:
            continue
        seen.add(d)
        n += _prune_up(rootp, d)
    return n


def drop_dir(db: Database, rel_dir: str, keep_rels=(), writer: str = "tree") -> dict:
    """删掉库文件夹里的一个目录（连里面库里不认识的游离文件一起）。

    为什么需要它：`DELETE /api/folders/<id>` 只删库里的行，磁盘上那个目录要是不收，
    里面的游离文件会被镜像一轮轮往 vault 复制 —— 而库里根本没有它们（2026-09-27 审计 E）。
    `keep_rels` 里的相对路径**不动**（那是库自己的投影，删了就是丢内容）。
    """
    root = library_dir(db)
    if not root:
        return {"ok": False, "why": "no library_dir"}
    rel_dir = safe_rel(rel_dir)                      # 含 `..` / 绝对路径 → 空串 → 拒绝
    parts = [x for x in rel_dir.split("/") if x]
    if parts and parts[0] == "vault":
        parts = parts[1:]
    if not parts:
        return {"ok": False, "why": "no rel"}
    rootp = Path(root)
    dst = rootp.joinpath(*parts)
    if not _inside(rootp, dst) or dst == rootp:      # 兜底：不许删库文件夹自己、更不许删它外面
        return {"ok": False, "why": "escape", "rel": rel_dir}
    keep = set(keep_rels or ())
    removed, kept = [], []
    if dst.is_dir():
        for p in sorted(dst.rglob("*")):
            if not p.is_file():
                continue
            rel = "/".join(parts + list(p.relative_to(dst).parts))
            if rel in keep:
                kept.append(rel)
                continue
            try:
                p.unlink()
                note_deletion(db, rel, writer)
                removed.append(rel)
            except OSError:
                kept.append(rel)
    pruned = _prune_up(rootp, dst)
    return {"ok": True, "removed": removed, "kept": kept, "pruned": pruned}


def remove_from_tree(db: Database, rel_or_file_id: str, by_id: bool = False,
                     writer: str = "") -> dict:
    """把一篇从库文件夹里撤掉（删文件/改名/移动后的旧位置）。"""
    root = library_dir(db)
    if not root:
        return {"ok": False, "why": "no library_dir"}
    rel = rel_of(db, rel_or_file_id) if by_id else rel_or_file_id
    if not rel:
        return {"ok": False, "why": "no rel"}
    dst = Path(root) / rel.replace("/", os.sep)
    if not _inside(Path(root), dst):
        return {"ok": False, "why": "escape"}
    note_deletion(db, rel, writer)                  # 记一笔：vault 那份也要撤（镜像下一轮做）
    if dst.is_file():
        try:
            os.remove(dst)
        except OSError as e:
            return {"ok": False, "why": str(e)[:60]}
        return {"ok": True, "removed": str(dst)}
    return {"ok": True, "removed": None}
