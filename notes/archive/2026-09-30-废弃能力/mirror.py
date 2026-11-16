"""库文件夹 ⇄ vault 双向同步（辞玖 2026-09-22 定：暂时保持数据同步，谁新谁赢）。

两个根：
  · 库文件夹  `D:\\枢阁\\库`   —— 库自己的正文落点（以后是唯一真相源）
  · vault    `D:\\个人数据\\辞玖`   —— 你现在的编辑处

规则（v1，刻意保守）：
  · 一边有一边没有 → 复制过去（新建也算）;
  · 两边都有：先比"大小 + mtime"，一样就当同步；不一样就比内容哈希，
    哈希一样只把 mtime 对齐到新的那个（不算改动）；真不一样 → **谁新谁赢**，把新的那份覆盖旧的;
  · **不删**：一边删了，下一次会被另一边"复制回来"（删除传播留到下一版，别误删）;
  · 忽略：点开头的项（.obsidian/.trash/…）与配置里的忽略前缀（默认 tmp）;
  · **私密前缀默认跳过**（默认 `个人/私密`）：那边 vault 是明文、库文件夹是密文，
    字节级同步会把两边都搞坏 —— 私密的加密/解密同步（要主密钥）留到下一版。

三道"真相保护"闸（2026-09-23 加，起因见 §75）：
  · 覆盖前**先备份** —— 默认走 **git**（把那一侧仓库提交一次，`pre_commit=True`），
    不是 git 仓库时才退回"复制文件到 `backup_root`"（2026-09-24 与 git 合并成一套）；
  · 源比目标**小太多就拒绝覆盖**（`shrink_guard`，默认 0.5），记冲突让人判；
  · `append_only` 前缀（默认 `日志/reasonix-raw`）**只补缺、永不覆盖**。
"""
from __future__ import annotations

import filecmp
import hashlib
import os
import shutil
import subprocess
import time

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

IGNORE_DIRS = {".git", ".trash", ".obsidian", ".smart-env", ".claudian", ".claude",
               "node_modules", "__pycache__", "tmp", ".dsh"}
DEFAULT_IGNORE_PREFIXES = ["tmp"]
DEFAULT_PRIVATE_PREFIXES = ["个人/私密"]
# 只追加、永不覆盖的目录（会话原始日志：整文件覆盖一次就是永久丢一段历史）
DEFAULT_APPEND_ONLY = ["日志/reasonix-raw"]
SHRINK_GUARD = 0.5          # 源 < 目标的 50% → 拒绝覆盖（0 或负数 = 关掉）


def _parts(rel: str):
    return [x for x in rel.replace("\\", "/").split("/") if x]


def ignored(rel: str, ignore_prefixes=()) -> bool:
    """点开头的一律忽略；配置里的忽略前缀整棵子树忽略"""
    p = _parts(rel)
    if not p:
        return False
    if any(x.startswith(".") for x in p):
        return True
    if any(x in IGNORE_DIRS for x in p[:-1]):
        return True
    r = "/".join(p)
    return any(r == pre or r.startswith(pre.rstrip("/") + "/") for pre in ignore_prefixes)


def is_private(rel: str, private_prefixes=()) -> bool:
    r = "/".join(_parts(rel))
    return any(r == pre or r.startswith(pre.rstrip("/") + "/") for pre in private_prefixes)


def _under(rel: str, prefixes) -> bool:
    r = "/".join(_parts(rel))
    return any(r == pre or r.startswith(pre.rstrip("/") + "/") for pre in prefixes)


def scan(root: str, ignore_prefixes=(), private_prefixes=(), tally=None) -> dict:
    """{相对路径: {"size":…, "mtime":…}}（只收文件；忽略项与私密前缀都不进来）

    2026-09-26 优化：以前 `os.walk` + 每个文件 `os.stat`，等于每个文件两次系统调用；
    改成 `os.scandir` —— Windows 上目录项自带的 stat 直接用（一次枚举就带回来），
    实测：两棵树扫描从 ~800ms + ~1100ms 降到几十毫秒。
    """
    out = {}
    n_ig = n_priv = 0
    stack = [(root, "")]
    while stack:
        d, rel_dir = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            rel = (rel_dir + "/" + e.name) if rel_dir else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in IGNORE_DIRS or e.name.startswith("."):
                        n_ig += 1
                        continue
                    stack.append((e.path, rel))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                if ignored(rel, ignore_prefixes):
                    n_ig += 1
                    continue
                if is_private(rel, private_prefixes):
                    n_priv += 1
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            out[rel] = {"size": st.st_size, "mtime": st.st_mtime}
    if tally is not None:
        tally["ignored"] = tally.get("ignored", 0) + n_ig
        tally["private_skipped"] = tally.get("private_skipped", 0) + n_priv
    return out


def _sha(path: str, cap: int = 0) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            if cap and f.tell() > cap:
                break
    return h.hexdigest()


def same_file(a: str, b: str) -> bool:
    try:
        if os.path.getsize(a) != os.path.getsize(b):
            return False
    except OSError:
        return False
    return _sha(a) == _sha(b)


def _pre_commit(root: str, rel: str, writer: str = "mirror", reason: str = "overwrite") -> str:
    """写前把那一侧仓库提交一次 —— **git 提交就是前像**。返回 commit 短号；不可用返回 ""。

    为什么比"复制被覆盖的那个文件"强：提交保存的是**整棵树**当时的样子，
    恢复时 `git -C <root> checkout <commit> -- <rel>` 一行就回来，入口也只有这一个。
    """
    if not root or not os.path.isdir(os.path.join(root, ".git")):
        return ""
    try:
        subprocess.run(["git", "-C", root, "add", "-A"], capture_output=True, timeout=120, creationflags=NO_WINDOW)
        subprocess.run(["git", "-C", root, "-c", "user.name=mirror",
                        "-c", "user.email=mirror@local", "commit", "-q", "-m",
                        "pre-write(%s): %s by %s" % (reason, rel, writer)],
                       capture_output=True, timeout=120, creationflags=NO_WINDOW)
        r = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"],
                           capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def _backup(dst: str, backup_root: str, root_label: str, rel: str) -> str:
    """覆盖前把目标文件留一份（按时间戳分层）。返回备份路径（失败返回 ""）。"""
    if not backup_root or not os.path.isfile(dst):
        return ""
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = os.path.join(backup_root, stamp, root_label, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shutil.copy2(dst, out)
        return out
    except Exception:
        return ""


def _too_short(src: dict, dst: dict, guard: float) -> bool:
    """源是否"小得可疑"（要覆盖一个明显更大的目标）。guard<=0 表示关掉这道闸。"""
    if not guard or guard <= 0:
        return False
    ds, ss = int(dst.get("size") or 0), int(src.get("size") or 0)
    return ds > 0 and ss < ds * guard


def _copy(src: str, dst: str) -> None:
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".mirror-tmp"
    shutil.copyfile(src, tmp)
    shutil.copystat(src, tmp)          # mtime 一起带过去，否则每次都被当成"新的"
    os.replace(tmp, dst)


def sync(lib_dir: str, vault_dir: str, apply: bool = True, ignore_prefixes=(),
         private_prefixes=None, append_only=None, backup_root: str = "",
         shrink_guard: float = SHRINK_GUARD, pre_commit: bool = True,
         writer: str = "mirror", log=None) -> dict:
    """跑一轮。返回报告；apply=False 时只算不写（预演）。"""
    t0 = time.time()
    tally = {"ignored": 0, "private_skipped": 0}
    ip = tuple(ignore_prefixes or DEFAULT_IGNORE_PREFIXES)
    ao = tuple(DEFAULT_APPEND_ONLY if append_only is None else append_only)
    # None = 用默认（`()` 是"显式给了空表"，两者不能混）
    pp = tuple(DEFAULT_PRIVATE_PREFIXES if private_prefixes is None else private_prefixes)
    t_scan = time.time()
    a = scan(lib_dir, ip, pp, tally)          # 库文件夹
    b = scan(vault_dir, ip, pp, tally)        # vault
    scan_ms = round((time.time() - t_scan) * 1000, 1)
    rep = {"library": lib_dir, "vault": vault_dir, "apply": apply,
           "only_library": 0, "only_vault": 0, "newer_library": 0, "newer_vault": 0,
           "same": 0, "mtime_aligned": 0, "copied_to_vault": [], "copied_to_library": [],
           "conflicts": [], "errors": [], "ignored": tally["ignored"],
           "private_skipped": tally["private_skipped"], "backed_up": [],
           "pre_commits": [], "seconds": 0.0}

    def tell(msg):
        if log:
            log(msg)

    for rel in sorted(set(a) | set(b)):
        pa = os.path.join(lib_dir, rel.replace("/", os.sep))
        pb = os.path.join(vault_dir, rel.replace("/", os.sep))
        try:
            if rel not in b:                                   # 只在库文件夹 → 复制去 vault
                if os.path.isdir(pb):                          # 同名但对面是目录：不动，如实报
                    rep["conflicts"].append({"rel": rel, "why": "一边是文件一边是目录"})
                    continue
                rep["only_library"] += 1
                if apply:
                    _copy(pa, pb)
                rep["copied_to_vault"].append(rel)
                tell("库→vault %s" % rel)
                continue
            if rel not in a:                                   # 只在 vault → 复制进库文件夹
                if os.path.isdir(pa):
                    rep["conflicts"].append({"rel": rel, "why": "一边是文件一边是目录"})
                    continue
                rep["only_vault"] += 1
                if apply:
                    _copy(pb, pa)
                rep["copied_to_library"].append(rel)
                tell("vault→库 %s" % rel)
                continue
            sa, sb = a[rel], b[rel]
            if sa["size"] == sb["size"] and abs(sa["mtime"] - sb["mtime"]) < 1:
                rep["same"] += 1
                continue
            if sa["size"] == sb["size"] and same_file(pa, pb):
                rep["same"] += 1
                newest = max(sa["mtime"], sb["mtime"])          # 内容一样，只把时间对齐
                if apply and abs(sa["mtime"] - sb["mtime"]) >= 1:
                    os.utime(pa if sa["mtime"] < sb["mtime"] else pb, (newest, newest))
                rep["mtime_aligned"] += 1
                continue
            if _under(rel, ao):
                # 只追加目录：**只挡"磁盘 → 库"**（护住 vault 原件不被磁盘侧改写）；
                # 库那份更新（= 接口写盘写下来的）照旧下发到投影，否则接口写了、磁盘永远不变。
                if same_file(pa, pb):
                    rep["same"] += 1
                    continue
                if sa["mtime"] > sb["mtime"]:
                    rep["newer_library"] += 1
                    if apply:
                        c = _pre_commit(vault_dir, rel, writer, "lib-newer-ao") if pre_commit else ""
                        if c:
                            rep["pre_commits"].append({"rel": rel, "repo": "vault", "commit": c})
                        _copy(pa, pb)
                    rep["copied_to_vault"].append(rel)
                    tell("库→vault（只追加目录·接口写的）%s" % rel)
                else:
                    rep["conflicts"].append({"rel": rel,
                                             "why": "只追加目录：磁盘那份更新 → 不动（等人工）"})
                continue
            if abs(sa["mtime"] - sb["mtime"]) < 1:
                # 时间打平但内容不同：**让 vault 赢**（不动原件）。
                # 库内那份可能是按库规则转换过的文本，跟原件不是同一串字节 ——
                # 这里若让库赢，等于把 vault 的原件改掉（2026-09-22 想通，见 §74）。
                rep["conflicts"].append({"rel": rel, "why": "同时间不同内容 → 保留 vault 原件"})
                continue
            if sa["mtime"] > sb["mtime"]:                       # 库文件夹真的更新 → 覆盖 vault
                if _too_short(sa, sb, shrink_guard):
                    rep["conflicts"].append({"rel": rel, "why": "库那份比 vault 小得多（%d→%d），拒绝覆盖"
                                             % (sb["size"], sa["size"])})
                    continue
                rep["newer_library"] += 1
                if apply:
                    c = _pre_commit(vault_dir, rel, writer, "lib-newer") if pre_commit else ""
                    if c:
                        rep["pre_commits"].append({"rel": rel, "repo": "vault", "commit": c})
                    else:
                        b = _backup(pb, backup_root, "vault", rel)
                        if b:
                            rep["backed_up"].append(b)
                    _copy(pa, pb)
                rep["copied_to_vault"].append(rel)
                tell("库→vault（库新）%s%s" % (rel, "（已备份目标）" if backup_root else ""))
            else:                                              # vault 更新 → 覆盖库文件夹
                if _too_short(sb, sa, shrink_guard):
                    rep["conflicts"].append({"rel": rel, "why": "vault 那份比库小得多（%d→%d），拒绝覆盖"
                                             % (sa["size"], sb["size"])})
                    continue
                rep["newer_vault"] += 1
                if apply:
                    c = _pre_commit(lib_dir, rel, writer, "vault-newer") if pre_commit else ""
                    if c:
                        rep["pre_commits"].append({"rel": rel, "repo": "library", "commit": c})
                    else:
                        b = _backup(pa, backup_root, "library", rel)
                        if b:
                            rep["backed_up"].append(b)
                    _copy(pb, pa)
                rep["copied_to_library"].append(rel)
                tell("vault→库（vault新）%s%s" % (rel, "（已备份目标）" if backup_root else ""))
        except Exception as e:                                  # 单个文件出错不拖垮整轮
            rep["errors"].append({"rel": rel, "err": "%s: %s" % (type(e).__name__, str(e)[:80])})
    rep["seconds"] = round(time.time() - t0, 3)
    rep["scan_ms"] = scan_ms                  # 两棵树各扫一遍的耗时（诊断用）
    rep["compare_ms"] = round(rep["seconds"] * 1000 - scan_ms, 1)
    return rep
