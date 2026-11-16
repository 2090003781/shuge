"""导入器：把系统磁盘上的外部文件读进库（FORMAT §8「导入」）。

第一期（基础文档）：
  · 支持扩展名：.md .txt .log + 常见代码后缀
  · 按相对目录在库内建文件夹树
  · content_hash 去重：同内容已存在 → 跳过（返回 skipped）
  · 同名不同内容：默认跳过；overwrite=True 时按"谁新谁赢"覆盖（磁盘新→更新，库里新→kept）
  · 正文按**库规则**改写：`[[目标]]` → `@目标`（磁盘留原件，库内永远是库的引用语法）
  · 排除规则：隐藏目录/临时目录（.git / .trash / tmp / __pycache__ / node_modules）
  · 不改变外部文件（只读）

不做（第二期）：pdf/sheet/image/audio、增量监控、双向同步。
"""
from __future__ import annotations

import datetime
import os
from pathlib import Path

import refs_convert
import vaultcrypto
from db import Database, sha256_bytes
from textio import decode_text


def _is_doc_ext(ext: str) -> bool:
    """二进制/压缩容器的文档格式：必须原样入库（html 之类是文本，照旧做编码归一）"""
    try:
        import doctext
        return doctext.needs_parse(ext)
    except Exception:
        return ext in ("docx", "docm", "dotx", "odt", "ott", "rtf", "pdf", "epub")

TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".log",
    ".py", ".js", ".ts", ".go", ".java", ".c", ".cpp", ".h", ".rs", ".rb", ".php",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql", ".sh", ".ps1", ".css", ".html",
}
# 文档类：正文是二进制/压缩容器，入库时原样存、按需解析（解析器在 doctext.py）
DOC_EXTS = {".docx", ".docm", ".dotx", ".odt", ".ott", ".rtf", ".pdf", ".epub"}
# 导入口径 = 文本类 + 文档类。
# 2026-09-21 修：原来 iter_files 只认 TEXT_EXTS，于是 docx/pdf 这些**根本走不到导入这条路**上——
# §38-45 做的解析器在 watcher / API / 页面导入里都够不着。表格类（xlsx/csv）留给表格线。
# 表格原料：csv/tsv 本身也是"文件"（要留档），同时表格线会把它们解析成 SQL 表
TABLE_EXTS = {".csv", ".tsv", ".xlsx", ".xlsm"}
IMPORT_EXTS = TEXT_EXTS | DOC_EXTS | TABLE_EXTS
EXCLUDE_DIRS = {".git", ".trash", ".obsidian", "node_modules", "__pycache__", "tmp", ".smart-env", ".claudian"}


def iter_files(src_dir: str, limit: int = 0):
    """遍历可导入文件，返回 (绝对路径, 相对路径) 列表"""
    root = Path(src_dir)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.startswith("."):                     # 点文件是工具配置/缓存
                continue
            # 辞玖 2026-09-25：库按文件管理系统来 —— **什么文件都收**（能不能看/改/检索由 caps 划分）
            # 老的 IMPORT_EXTS 白名单留作"哪些默认进检索"的参考，不再拦文件。
            abs_p = Path(dirpath) / fn
            rel_p = abs_p.relative_to(root)
            out.append((abs_p, rel_p))
            if limit and len(out) >= limit:
                return out
    return out


def import_from_dir(db: Database, src_dir: str, limit: int = 0, events=None,
                    tag: str = "", tag_dirs: dict | None = None, under: str = "",
                    rag_off: bool = False, overwrite: bool = False,
                    visibility: str = "internal", master: bytes | None = None,
                    convert_refs: bool = True) -> dict:
    """目录递归导入。返回 {imported, skipped, failed, files}

    tag / tag_dirs：导入时打标签（如"顶层目录 日志 → 标签 logs"），
    检索侧据此默认排除归档类内容。
    rag_off=True：导入的文件一律不进检索库（只存放、要看再手动加入）——
    2026-09-21 辞玖定："不需要一进库就进 rag"。API / watcher 那条路默认 rag_off=True。
    """
    if not os.path.isdir(src_dir):
        return {"error": "目录不存在: %s" % src_dir, "imported": 0, "skipped": 0, "failed": 0, "files": []}
    return _import_files(db, src_dir, iter_files(src_dir, limit=limit), under, tag, tag_dirs,
                         events, rag_off, overwrite, visibility, master, convert_refs)


def import_path(db: Database, src: str, under: str = "", tag: str = "", events=None,
                rag_off: bool = True, overwrite: bool = False,
                visibility: str = "internal", master: bytes | None = None,
                convert_refs: bool = True) -> dict:
    """导入一个**文件或一个目录**（单文件是"拖进来""从磁盘索引进库"的最小入口）。

    单文件时：只导入这一个文件，落在 under 目录下（相对路径就是文件名本身）；
    目录时：等价于 import_from_dir。
    """
    p = Path(src)
    if p.is_file():
        if p.name.startswith("."):
            return {"error": "这个文件类型不在可导入清单里: %s" % p.name, "imported": 0,
                    "skipped": 0, "failed": 0, "files": [], "source": str(p)}
        return _import_files(db, str(p.parent), [(p, Path(p.name))], under, tag, None, events,
                             rag_off, overwrite, visibility, master, convert_refs)
    if p.is_dir():
        return import_from_dir(db, str(p), under=under, tag=tag, events=events, rag_off=rag_off,
                               overwrite=overwrite, visibility=visibility, master=master,
                               convert_refs=convert_refs)
    return {"error": "路径不存在: %s" % src, "imported": 0, "skipped": 0, "failed": 0, "files": [],
            "source": src}


def _lib_time(row) -> float:
    """库里那一行的写入时间（files.mtime，带偏移的本地 ISO）→ epoch；读不出来当 0（=很旧，可以被覆盖）"""
    try:
        return datetime.datetime.fromisoformat(str(row["mtime"])).timestamp()
    except Exception:
        return 0.0


def _import_files(db: Database, source: str, pairs, under: str, tag: str,
                  tag_dirs: dict | None, events, rag_off: bool,
                  overwrite: bool = False, visibility: str = "internal",
                  master: bytes | None = None, convert_refs: bool = True) -> dict:
    """导入循环本体（import_from_dir 与 import_path 共用）。"""
    imported, skipped, failed, tagged, reencoded, names = 0, 0, 0, 0, 0, []
    refs_converted = 0                    # 这次入库顺手按库规则转掉的 `[[目标]]` 处数
    updated, kept, updated_ids, kept_names = 0, 0, [], []
    created_ids = []                       # 新建进来的那几篇（检索缓存要增量跟上）
    # 内容没变、所以"没动手"的那几篇：**盘上投影可能是缺的/旧的**，调用方要按需补一次
    # （不补的话：库里对、盘上旧，而且再导一百次也修不了 —— 每次都走 skip。2026-10-04 栽过）
    unchanged_ids = []
    secured, secured_ids = 0, []          # secured = 库里已有这篇，只把权限补成私密（内容不动）
    private_ready = 0                     # 这次没动手，但"库里那份已经是私密"已经成立
    src_dir = source
    for abs_p, rel_p in pairs:
        name = rel_p.name
        rel_dir = str(rel_p.parent).replace("\\", "/")
        if rel_dir == ".":
            rel_dir = ""
        try:
            data = abs_p.read_bytes()
        except Exception:
            failed += 1
            continue
        # 二进制/文档格式**原样入库**：decode_text 会把 docx/pdf 这种毁掉
        ext = (abs_p.suffix or "").lstrip(".").lower()
        binary = (b"\x00" in data[:8192]) or _is_doc_ext(ext)
        if binary:
            enc = "binary"
            reencoded += 0                     # 没动过内容，不算"转码"
        else:
            text, enc = decode_text(data)      # 文本类统一成 UTF-8
            if enc != "utf-8":                 # 带 BOM / 其它编码都算被规范化过
                reencoded += 1
            if convert_refs:
                # 库规则：`[[目标]]` → `@目标`。**必须在算 content_hash 之前**，
                # 否则"库里存的"和"比 hash 用的"不是同一份文本，去重和覆盖判断全会错位。
                text, _ref_stats = refs_convert.convert_for_db(db, text)
                refs_converted += int(_ref_stats.get("converted") or 0)
            data = text.encode("utf-8")
        h = sha256_bytes(data)
        rel_name = "%s/%s" % (rel_dir, name) if rel_dir else name
        folder_id = db.ensure_folder_path("/".join(x for x in (under.strip("/"), rel_dir) if x))
        old = db.find_file(folder_id, name)          # 同名同目录：有它就得先处理它
        if old is None:
            dup = db.conn.execute("SELECT id FROM files WHERE content_hash=?", (h,)).fetchone()
            if dup:
                skipped += 1
                continue
        else:
            same = (old["content_hash"] == h)
            # 库里这行还没上私密权限（明文 internal 之类）→ 这次必须补上，不能因为"内容没变"跳过
            need_priv = visibility == "private" and not (old["visibility"] == "private"
                                                         and old["enc_algo"])
            if same and not need_priv:
                if visibility == "private":
                    private_ready += 1               # 已经是私密且内容一样 → 没事可做，但目标已达成
                unchanged_ids.append(old["id"])       # 内容没变：让调用方按需补一次盘上投影
                skipped += 1                         # 一模一样、权限也对 → 老行为
                continue
            if not overwrite and not need_priv:
                skipped += 1                         # 内容不同、没开覆盖、也不是补权限 → v1 老行为
                continue
            try:
                try:
                    disk_t = abs_p.stat().st_mtime
                except Exception:
                    disk_t = 0.0
                if overwrite and not same and disk_t > _lib_time(old):
                    # 高风险（覆盖库内那一份）→ 先留前像，可恢复（辞玖 2026-09-24 定）
                    try:
                        import beforeimage as _bi
                        _bi.save(db, old["id"], "import-overwrite",
                                 writer=getattr(events, "writer", "") or "mirror", master=master)
                    except Exception:
                        pass                                   # 留不了前像也别挡住导入
                    # 必须带 master：这行可能已经是密文（私密文件），没主密钥连写都写不了
                    db.put_content(old["id"], data, master)   # file_id 不变：标签/引用/位置都留着
                    updated += 1
                    updated_ids.append(old["id"])
                    names.append(rel_name)
                elif not same:
                    kept += 1                        # 库里那份更新（或没开覆盖）→ 让库赢，别冲掉人的字
                    kept_names.append(rel_name)
                    if visibility == "private" and not need_priv:
                        private_ready += 1
                if need_priv:
                    db.set_visibility(old["id"], "private", master)   # 只补权限：内容一个字不动
                    secured += 1
                    secured_ids.append(old["id"])
                    if same:
                        names.append(rel_name)
            except vaultcrypto.LockedError:
                raise                                # 库没解锁：如实往上抛，不当成"这个文件坏了"
            except Exception:
                failed += 1
            continue
        try:
            fid = db.create_file(folder_id, name, data, visibility=visibility, master=master)
            created_ids.append(fid)
            if rag_off:
                db.set_rag(fid, False)      # 只存放：不解析、不切块、不嵌入（要看再手动加入）
            imported += 1
            names.append("%s/%s" % (rel_dir, name) if rel_dir else name)
            t = (tag_dirs or {}).get(rel_dir.split("/")[0]) or tag
            if t:
                db.add_tag(fid, t)
                tagged += 1
        except vaultcrypto.LockedError:
            raise                                        # 没解锁就不能建私密：不许降级成明文，也不许静默失败
        except Exception:
            failed += 1

    result = {"imported": imported, "skipped": skipped, "failed": failed, "tagged": tagged,
              "reencoded": reencoded, "under": under, "files": names[:10], "source": src_dir,
              "rag_off": rag_off, "updated": updated, "kept": kept, "secured": secured,
              "private_ready": private_ready, "refs_converted": refs_converted,
              "updated_ids": updated_ids, "secured_ids": secured_ids, "created_ids": created_ids,
              "unchanged_ids": unchanged_ids,
              "kept_files": kept_names[:10]}
    if events:
        events.emit("note.imported", {"count": imported, "source_root": src_dir,
                                      "skipped": skipped, "failed": failed})
    return result
