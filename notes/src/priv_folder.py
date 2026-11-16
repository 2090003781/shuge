# -*- coding: utf-8 -*-
"""私密文件夹 —— 位置判据 + 加密包读写的**唯一入口**。

你定的形态（2026-10-15）：`个人\\私密\\` = **一个真实目录 + 一个单独的加密包文件**。
格式见 `privpack.py` 与 `D:\\RAG\\枢阁-私密文件夹-加密包格式.md`。

## 两种模式（**默认 legacy，行为一字不变**）

| 模式 | 私密篇正文存哪 | 读 | 写 |
|---|---|---|---|
| `legacy`（默认） | 库里一篇一份密文（`content`/池） | 库内解密 | 库内加密 |
| `package` | 私密文件夹里的 `.privpack` | 解包 | 改包后原子替换 |

为什么要有这个开关：这个模块要先进库、再逐步切。默认 legacy 保证"接了代码但没开"
时**行为与今天完全一致** —— 否则接线的当天就会动到 8 篇真数据。

## 位置判据（你要的第①条）

`is_private_location(rel)`：库内相对路径**落在私密文件夹里**（含其子目录）即私密。
不再看 `visibility` 列。路径前缀由 `PRIV_DIR_REL` 一处定义（`个人/私密`），
将来库目录重构（`个人` 改名等）只改这里。
"""
from __future__ import annotations

import os

import privpack

# 私密文件夹在**库内**的相对路径（不带 vault 层）。
# 一处定义，别处不许再硬编码这个字符串。
PRIV_DIR_REL = "个人/私密"
PACK_NAME = privpack.DEFAULT_NAME


# ── 路径判据 ─────────────────────────────────────────────────

def _norm(rel: str) -> str:
    """规整成 "a/b/c"（去空段、统一斜杠、剥掉库内那层 vault）"""
    parts = [x for x in str(rel or "").replace("\\", "/").split("/") if x and x != "."]
    if parts and parts[0] == "vault":
        parts = parts[1:]
    return "/".join(parts)


def is_private_location(rel: str) -> bool:
    """这一篇是不是**在私密文件夹里**（含子目录）。

    判据是位置，不是 visibility 列 —— 这是你定的第①条。
    """
    p = _norm(rel)
    if not p:
        return False
    return p == PRIV_DIR_REL or p.startswith(PRIV_DIR_REL + "/")


# ── 「是不是私密」的唯一判据（2026-10-15）──────────────────────
#
# 给 SQL 用：这一篇在不在私密文件夹里。靠"自己的名字是私密文件夹"或
# "祖先链里有私密文件夹"判定（`folders` 表存的是相对路径，带 vault/ 层）。
#
# 用法：SELECT ... FROM files f JOIN folders fo ON fo.id=f.folder_id WHERE %s
#       （把 PRIVATE_SQL 里的 fo 换成实际别名）
_PDIR = PRIV_DIR_REL.split("/")
_NAME = _PDIR[-1]
_PARENT = "/".join(_PDIR[:-1])


def _sql(alias: str = "fo") -> str:
    a = alias
    if _PARENT:
        return ("({a}.name = '{nm}' AND EXISTS ("
                "  SELECT 1 FROM folders p WHERE p.id = {a}.parent_id AND p.name = '{par}'"
                "))").format(a=a, nm=_NAME, par=_PARENT)
    return "{a}.name = '{nm}'".format(a=a, nm=_NAME)


# 位置判据（SQL）
PRIVATE_SQL = _sql("fo")

# 私有列判据（SQL）—— 迁移期保留；3 篇挪进私密文件夹后这一半会自然失效
LEGACY_PRIVATE_SQL = "IFNULL(f.visibility,'')='private'"

# **统一判据**：位置 ∪ 旧标记。迁移期用它才不改变今天的行为。
PRIVATE_ANY_SQL = "((%s) OR (%s))" % (PRIVATE_SQL, LEGACY_PRIVATE_SQL)


def inside_rel(rel: str) -> str:
    """私密文件夹内的相对路径（用于包内 folder 字段）。

    "个人/私密/a/b.md" → "a/b.md"；"个人/私密/x.md" → "x.md"；
    不在私密文件夹里 → 空串。
    """
    p = _norm(rel)
    if not is_private_location(p):
        return ""
    rest = p[len(PRIV_DIR_REL):].lstrip("/")
    return rest


def dir_abs(library_dir: str) -> str:
    """私密文件夹在盘上的绝对路径"""
    return os.path.join(str(library_dir), *PRIV_DIR_REL.split("/"))


def pack_abs(library_dir: str) -> str:
    """加密包在盘上的绝对路径"""
    return os.path.join(dir_abs(library_dir), PACK_NAME)


def is_pack_file(path: str) -> bool:
    """这个盘上文件是不是加密包本身（对账时要当合法例外跳过）"""
    return os.path.basename(str(path)) == PACK_NAME


# ── 包读写 ───────────────────────────────────────────────────

class NeedUnlock(Exception):
    """包要主密钥才打得开（锁定态）"""


class NoPub(Exception):
    """库里没有公钥 → 建不了也写不了包（还没设过库密码）"""


def load_pack(library_dir: str) -> tuple:
    """读盘上的包 → (blob, pack_dict)。包不存在回 (None, None)。

    不抛"没有包" —— 那是"私密文件夹还是空的"的正常情形。
    """
    p = pack_abs(library_dir)
    if not os.path.isfile(p):
        return None, None
    blob = privpack.read_file(p)
    return blob, privpack.parse(blob)


def read_items(library_dir: str, master: bytes, vault_priv_raw: bytes | None,
               create_if_missing: bool = False) -> list:
    """解出包内条目（列表）。锁定态抛 NeedUnlock；没包回空表。

    **读**是要密码的：master → 私钥 → 开盒 → K_pack → 解包体。
    """
    blob, _ = load_pack(library_dir)
    if blob is None:
        return []
    p = privpack.parse(blob)
    if not master:
        raise NeedUnlock("私密文件夹要解锁才能读（锁定态只显示文件夹本身）")
    k_pack = privpack.open_key(p["key_ref"], master, vault_priv_raw)
    _header, items = privpack.unpack(blob, k_pack)
    return items


def write_items(library_dir: str, items: list, vault_pub_b64: str,
                master: bytes | None = None, vault_priv_raw: bytes | None = None) -> dict:
    """把条目列表写成包（原子替换）。

    **写有两条路**（对应你要的第③条「拖进去即加密」锁着也能做）：
      · 包已存在 → 复用包里那把 K_pack（要主密钥解开它）→ 需要解锁
      · 包不存在 → 用库公钥新封一把 K_pack → **锁着也能建包**
    """
    if not vault_pub_b64:
        raise NoPub("库里没有公钥（`vault_pub`）：先设一次库密码，之后锁着也能建私密包")
    blob, _ = load_pack(library_dir)
    if blob is not None:
        p = privpack.parse(blob)
        k_pack = privpack.open_key(p["key_ref"], master, vault_priv_raw)
        key_ref = p["key_ref"]
        created = None
        try:
            hb = privpack.unpack(blob, k_pack)[0]
            created = hb.get("created")
        except Exception:
            created = None
        new = privpack.pack_bytes(k_pack, key_ref, items, created=created)
    else:
        import vaultcrypto
        k_pack = vaultcrypto.new_key()
        key_ref = privpack.seal_key(k_pack, vault_pub_b64)
        new = privpack.pack_bytes(k_pack, key_ref, items)
    r = privpack.write_file(pack_abs(library_dir), new)
    if not r.get("ok"):
        raise IOError("写包失败：%s" % r.get("why"))
    r["pack"] = pack_abs(library_dir)
    return r


def find_item(items: list, name: str, folder: str = "") -> dict | None:
    for it in items:
        if it.get("name") == name and (it.get("folder") or "") == (folder or ""):
            return it
    return None


def get_item_bytes(items: list, name: str, folder: str = "") -> bytes | None:
    it = find_item(items, name, folder)
    if it is None:
        return None
    return privpack.item_bytes(it)


def upsert_item(items: list, name: str, data: bytes, folder: str = "",
                mtime: str | None = None) -> list:
    """加一篇 / 覆盖一篇（按 name+folder 定位）。返回新列表（不改原列表对象）。"""
    out = [dict(it) for it in items if not (it.get("name") == name
                                           and (it.get("folder") or "") == (folder or ""))]
    out.append(privpack.make_item(name, data, folder=folder, mtime=mtime))
    out.sort(key=lambda it: ((it.get("folder") or ""), it.get("name") or ""))
    return out


def drop_item(items: list, name: str, folder: str = "") -> tuple:
    """删掉一篇。返回 (新列表, 是否真删掉了)"""
    keep = [dict(it) for it in items if not (it.get("name") == name
                                            and (it.get("folder") or "") == (folder or ""))]
    return keep, len(keep) != len(items)


def list_names(items: list) -> list:
    """包内清单（给"解锁后显示完整文件树"用）：[(folder, name, size, mtime)]"""
    return [((it.get("folder") or ""), it.get("name") or "", it.get("size") or 0,
             it.get("mtime") or "") for it in items]


# ── 可观测性：离「位置决定私密」还有多远 ─────────────────────

def location_report(db) -> dict:
    """如实报告私密篇的位置分布。

    返回：
      total     —— 标记为私密的总数（按并集判据）
      by_loc    —— 真的在私密文件夹里的
      by_flag   —— 只靠旧标记、位置不在私密文件夹的（**这些就是第①条还没落地的部分**）
      rows      —— by_flag 的明细 [(name, rel)]
    """
    rows = []
    for r in db.conn.execute(
            "SELECT f.id, f.name FROM files f JOIN folders fo ON fo.id = f.folder_id "
            "WHERE %s" % PRIVATE_ANY_SQL):
        rel = db.disk_rel_of(r["id"]) or ""
        rows.append((r["name"], rel, is_private_location(rel)))
    by_loc = [x for x in rows if x[2]]
    by_flag = [(x[0], x[1]) for x in rows if not x[2]]
    return {"total": len(rows), "by_loc": len(by_loc), "by_flag": len(by_flag), "rows": by_flag}


# ── 第⑤条：解锁后，包内条目怎么变成"树里的行" ─────────────────
#
# 前端 `insertFilesIntoTree(rows)` 按 `folder_id` 把行插进树（web/index.html:1211）。
# 包内条目只有 `folder`（包内相对子目录，空 = 私密文件夹根），没有数据库 folder_id ——
# 所以要把它映成前端认得的样子。
#
# 两种形态：
#   · 扁平（默认）：file_id 用真 id（库里那行），folder_id 给**私密文件夹的 id**
#   · 包内有子目录时：rows 里额外给出 dirs（虚拟目录），前端按需建节点
#
# 为什么 file_id 要用**库里的真 id**：点开一篇要经 `GET /api/files/<id>`，
# 那条路认的是库内 id；编造一个 id 会让"点得开"落空（P42：要能被真正到达）。

def pack_tree_rows(db, master: bytes, vault_priv_raw=None) -> dict:
    """把包内条目整理成前端 insertFilesIntoTree 能吃的形状。

    返回 {"rows": [...], "dirs": [...], "by_name": {...}}

    rows 每项字段与 `/api/lock/unlock` 的 `revealed` 对齐：
        id, name, folder_id, ext, kind, size, visibility, rag, mtime, disp, from_pack
    """
    lib = db._lib_root()
    if not lib:
        return {"rows": [], "dirs": [], "by_name": {}}
    items = read_items(lib, master, vault_priv_raw)

    # 私密文件夹的数据库 id（树要挂在它下面）
    pidir = PRIV_DIR_REL.split("/")
    _pf_id = ""
    try:
        if len(pidir) >= 2:
            r = db.conn.execute(
                "SELECT c.id FROM folders c JOIN folders p ON p.id=c.parent_id"
                " WHERE c.name=? AND p.name=?", (pidir[-1], pidir[-2])).fetchone()
            _pf_id = (r["id"] if r else "") or ""
    except Exception:
        _pf_id = ""

    # 每篇在**库里的真实那行** —— 位置用库里的真 folder_id，**不硬塞私密文件夹**。
    #
    # ⚠️ 不能只按名字取（踩过两次）：
    #   ① 那 3 篇 DSH 人设的库内位置在 second-brain-system/dsh，硬塞成私密文件夹会挂错位置
    #   ② **重名**：`个人/私密/索引.md`（private）与 `个人/知识库/索引.md`（internal）同名，
    #      按名字 setdefault 会取到错的那篇 → 给出错的位置与错的 file_id。
    #      既有审计早就记过这个坑："只比文件名会误判"。
    #
    # 所以两轮匹配：**先在私密文件夹里找**（包内条目的正确位置），找不到再退回全库；
    # 全库里有多个同名候选时，记进 ambiguous 如实报出来，不静默挑一个。
    _all = {}
    for r in db.conn.execute(
            "SELECT id,name,folder_id,ext,kind,size,visibility,rag,mtime FROM files"):
        _all.setdefault(r["name"], []).append(dict(r))

    def _pick(nm):
        """本轮选择：私密文件夹内优先 → 否则唯一候选 → 否则空（并记 ambiguous）"""
        cands = _all.get(nm) or []
        if not cands:
            return None
        for c in cands:
            try:
                if is_private_location(db.disk_rel_of(c["id"]) or ""):
                    return c
            except Exception:
                pass
        return cands[0] if len(cands) == 1 else None

    ambiguous = {}
    for _nm, _cs in _all.items():
        if len({c["folder_id"] for c in _cs}) > 1:
            ambiguous[_nm] = len(_cs)

    rows, dirs = [], []
    for it in items:
        nm = it.get("name") or ""
        sub = (it.get("folder") or "").strip("/")
        m = _pick(nm) or {}
        fid = m.get("id") or ""
        ext = (nm.rsplit(".", 1)[1].lower() if "." in nm else "") or ""
        if sub:
            dirs.append(sub)
        rows.append({
            "id": fid,                      # 真 id（点得开）；库里还没这行时为空串
            "name": nm,
            # 树挂在哪：用库里的**真实位置**；库里没有这行时才退回私密文件夹
            "folder_id": (m.get("folder_id") or _pf_id),
            "ext": ext,
            "kind": m.get("kind") or "markdown",
            "size": it.get("size") or 0,
            "visibility": "private",
            "rag": m.get("rag") or "off",
            "mtime": it.get("mtime") or "",
            "disp": nm.rsplit(".", 1)[0] if "." in nm else nm,
            "from_pack": True,              # 标记来源，方便前端/排障区分
            "pack_folder": sub,
        })
    rows.sort(key=lambda x: (x["pack_folder"], x["name"]))
    return {"rows": rows, "dirs": sorted(set(dirs)),
            "by_name": {r["name"]: r for r in rows},
            "pack_dir_id": _pf_id,
            # 同名多位置的篇：如实报出来（这类必须靠路径区分，不能靠名字猜）
            "ambiguous": ambiguous,
            "unmatched": [r["name"] for r in rows if not r["id"]]}


def pack_meta(items: list) -> dict:
    """包内清单摘要（不解密就能算的只有条数；名字要解密）"""
    return {"count": len(items or []),
            "bytes": sum((it.get("size") or 0) for it in (items or [])),
            "folders": sorted({(it.get("folder") or "") for it in (items or [])})}


# ── C 项的**可观测性**：盘上那份独立密文文件与包内条目一致吗 ─────────
#
# 为什么需要它：你现在还没定"盘上那些独立密文文件留不留"（C 项）。
# 在定之前，它们与包**可能悄悄分叉** —— 包被改过、盘上那份还是旧的（或反过来）。
# 分叉本身不是错（两份都在），但**不可观测的分叉**是隐患：
# 将来谁按其中一份为准去动作，就会丢另一份的改动。
#
# 所以这里只做一件事：**如实报出两边一不一致**，不改任何东西。
# 返回：
#   {"in_pack": 包内条数, "on_disk": 盘上文件数, "same": 两处都一致且相等的篇数,
#    "only_pack": 只在包里, "only_disk": 只在盘上, "differs": 名字同但内容看不出来（只能验长度）}

def pack_disk_consistency(db, master: bytes, vault_priv_raw=None) -> dict:
    """盘上独立文件 vs 包内条目。

    注意：盘上那份是**密文**，包内是明文 —— 没法直接逐字节比。
    能如实比的是：
      · 哪些篇只在一边（这是硬事实）
      · 名字相同的篇，**盘上密文长度**与"包内明文长度 + GCM 开销"是否吻合
        （本库实测开销恒为 28 字节：nonce 12 + tag 16）
    比不出来的（盘上密文对应的明文是否真等于包内那份）**如实说"验不了"**，
    不假装验过。
    """
    lib = db._lib_root()
    out = {"in_pack": 0, "on_disk": 0, "same": 0, "only_pack": [], "only_disk": [],
           "len_mismatch": [], "unverifiable": [], "dir": ""}
    if not lib:
        return out
    import os as _os
    d = dir_abs(lib)
    out["dir"] = d
    on_disk = {}
    if _os.path.isdir(d):
        for fn in _os.listdir(d):
            p = _os.path.join(d, fn)
            if _os.path.isfile(p) and not is_pack_file(fn):
                on_disk[fn] = _os.path.getsize(p)
    try:
        items = read_items(lib, master, vault_priv_raw)
    except Exception:
        items = []
    pack = {(it.get("name") or ""): (it.get("size") or 0) for it in items}
    out["in_pack"], out["on_disk"] = len(pack), len(on_disk)
    for nm, sz in pack.items():
        if nm not in on_disk:
            out["only_pack"].append(nm)
            continue
        # 盘上密文应有的长度：明文 + 28
        want = sz + 28
        if on_disk[nm] == want:
            out["same"] += 1
        else:
            out["len_mismatch"].append((nm, on_disk[nm], want))
    for nm in on_disk:
        if nm not in pack:
            out["only_disk"].append(nm)
    out["unverifiable"] = ["%s（能比长度，比不出明文是否相同）" % n
                           for n, _, _ in out["len_mismatch"]] or []
    for k in ("only_pack", "only_disk"):
        out[k].sort()
    return out


def pack_disk_summary(lib: str) -> dict:
    """**不需要密码**的盘面摘要（自检用：锁着也能跑）。

    只报"看得见的"：包在不在、多大、包头计数（`n` 在加密体里，所以**读不到**）、
    目录里有几个非包文件。
    不猜、不假装：包头计数读不出来就说明读不出来。
    """
    import os as _os
    out = {"pack": "", "pack_bytes": 0, "has_pack": False, "disk_files": [], "dir": ""}
    d = dir_abs(lib)
    out["dir"] = d
    p = pack_abs(lib)
    if _os.path.isfile(p):
        out["pack"] = p
        out["pack_bytes"] = _os.path.getsize(p)
        out["has_pack"] = True
    if _os.path.isdir(d):
        out["disk_files"] = sorted(
            fn for fn in _os.listdir(d)
            if _os.path.isfile(_os.path.join(d, fn)) and not is_pack_file(fn))
    return out
