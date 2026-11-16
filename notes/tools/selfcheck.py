"""一键自检（只读）：服务 / 数据一致性 / 嵌入规格 / 引用图 / 备份 / 卫生 / 近 24h 变化。

用法：
  python tools/selfcheck.py            # 人看的报告
  python tools/selfcheck.py --json     # 机器可读（给定时任务/程序用）
  python tools/selfcheck.py --quiet    # 只输出有问题的项（退出码仍然反映好坏）
  python tools/selfcheck.py --suite    # 顺带跑一遍全量体检 tools/run_all.py（慢，约 8 分钟）

级别：❌ 必须处理；⚠ 值得看一眼；✅ 正常。有 ❌ 时退出码 1。
只读：不写库、不改文件（--suite 会跑测试，测试自己会自清）。
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "src"))
sys.path.insert(0, os.path.join(HERE, "tools"))

from db import Database, DEFAULT_DATA_DIR        # noqa: E402
from indexer import Indexer                      # noqa: E402
from embedinput import SPEC                      # noqa: E402
import refs_audit as RA                          # noqa: E402

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

SERVICES = [("host(core-host)", "http://127.0.0.1:8790/"),
            ("core", "http://127.0.0.1:8795/"),
            ("库(library)", "http://127.0.0.1:8848/api/health")]


IN_SERVER = os.environ.get("SELFCHECK_IN_SERVER") == "1"     # 由库进程自身触发时置为 1


def probe(url: str, timeout: float = 3.0):
    try:
        r = urllib.request.urlopen(url, timeout=timeout)
        return r.status, r.read(200)
    except Exception as e:
        return None, str(e)[:80]


def human(n) -> str:
    return "%.1f MB" % (n / 1048576.0) if n > 1048576 else "%d B" % n


def run(data_dir: str, suite: bool = False) -> dict:
    checks = []

    def add(cid, level, title, detail=""):
        checks.append({"id": cid, "level": level, "title": title, "detail": str(detail)})

    # ── 服务 ──────────────────────────────────────────────
    svc = {}
    for name, url in SERVICES:
        if IN_SERVER and "8848" in url:
            # 库是单线程的：它正等着这次子进程返回，探自己必然超时 —— 跳过并说明
            svc[name] = "self"
            add("svc:" + name, "ok", "服务 %s（自检由库进程触发）" % name,
                "跳过自探：单线程服务器会把自己堵住")
            continue
        st, body = probe(url)
        svc[name] = st
        if st:
            add("svc:" + name, "ok", "服务在线 %s" % name, "HTTP %s" % st)
        else:
            add("svc:" + name, "bad", "服务不可达 %s" % name, body)

    db = Database(data_dir)
    q = lambda s, *a: db.conn.execute(s, a).fetchone()[0]      # noqa: E731
    files, chunks = q("SELECT COUNT(*) FROM files"), q("SELECT COUNT(*) FROM chunks")
    vectors, edges = q("SELECT COUNT(*) FROM vectors"), q("SELECT COUNT(*) FROM edges WHERE kind='mention'")
    add("scale", "ok", "库规模", "文件 %d（其中未入检索 %d）/ 块 %d / 向量 %d / 引用边 %d / 目录 %d / 墓碑 %d / 事件 %d"
        % (files, q("SELECT COUNT(*) FROM files WHERE IFNULL(rag,'on')<>'on'"), chunks, vectors,
           edges, q("SELECT COUNT(*) FROM folders"), q("SELECT COUNT(*) FROM tombstones"),
           q("SELECT COUNT(*) FROM events")))

    # ── 一致性 ────────────────────────────────────────────
    v = Indexer(db).verify()
    bad = {k: v[k] for k in ("orphan_chunks", "orphan_edges", "orphan_vectors", "sensitive_mismatch",
                             "oversized_chunks", "missing_pool_files")
           if v.get(k)}
    if v["fts_integrity"] != "ok":
        bad["fts_integrity"] = v["fts_integrity"]
    add("verify", "ok" if not bad else "bad", "一致性自检（indexer.verify）",
        "ok" if not bad else json.dumps(bad, ensure_ascii=False))

    # ── 嵌入规格 / 缺口 ────────────────────────────────────
    tags = {r["model"]: r["n"] for r in db.conn.execute(
        "SELECT model, COUNT(*) n FROM vectors GROUP BY model")}
    base_model = (db.conn.execute("SELECT value FROM manifest WHERE key='embedding_model'"
                                  ).fetchone() or ["bge-m3"])[0] or "bge-m3"
    want_tag = "%s#%s" % (base_model, SPEC)
    pend = q("SELECT COUNT(*) FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
             " WHERE v.chunk_id IS NULL OR IFNULL(v.model,'')<>?", want_tag)
    lvl = "ok" if pend == 0 else ("warn" if pend < 200 else "bad")
    add("vectors", lvl, "嵌入规格与缺口",
        "期望 %s；库里 %s；待补 %d" % (want_tag, tags or "无", pend))
    missing = q("SELECT COUNT(*) FROM chunks c LEFT JOIN vectors v ON v.chunk_id=c.id"
                " WHERE v.chunk_id IS NULL")
    if missing:
        add("vectors:missing", "warn", "有块完全没有向量", "%d 块" % missing)

    # ── 引用图（复用巡检） ────────────────────────────────
    R = RA.scan(db, data_dir)
    need = sum(R["need_human"].values())
    add("refs:unresolved", "ok" if need == 0 else "warn", "需要人看的未解析引用",
        "AMBIGUOUS+MISSING %d 处（忽略名单挡掉 %d 处，日志噪声等已分类）" % (need, R["ignored"]))
    add("refs:bad_sections", "ok" if R["bad_sections"] == 0 else "warn", "引用的小节不存在",
        "%d 处 %s" % (R["bad_sections"], R["bad_sections_detail"] or ""))
    add("refs:oldwiki", "ok" if R["wiki_left"] == 0 else "warn", "还没转的 [[旧引用]]",
        "%d 处（库里确实没有这些目标，属正常）" % R["wiki_left"])
    add("refs:space", "ok" if not R["unquoted_space"] else "warn", "名字带空格但没加引号",
        "%d 篇（tools/convert_refs.py --fix-spaces 可自动补）" % len(R["unquoted_space"]))

    dup = q("SELECT COUNT(*) FROM (SELECT SUBSTR(name,1,LENGTH(name)-CASE WHEN LOWER(name)"
            " LIKE '%.md' THEN 3 ELSE 0 END) d FROM files GROUP BY d HAVING COUNT(*)>1)")
    dated = q("SELECT COUNT(*) FROM files WHERE IFNULL(doc_date,'')<>''")
    # 私密位置（2026-10-15）：你定的第①条是「位置决定私密」。
    # 这一项如实报"还有几篇私密篇不在私密文件夹里" —— 那是迁移进度，不是故障。
    # 为什么放自检：进度要**持续可见**，不靠人记；做完自然变 ok。
    try:
        import priv_folder as _pf
        _rep = _pf.location_report(db)
        if _rep["by_flag"]:
            add("priv:location", "warn", "私密篇不在私密文件夹里（迁移未完成）",
                "%d/%d 篇在私密文件夹；另 %d 篇仍靠 visibility 标记：%s"
                % (_rep["by_loc"], _rep["total"], _rep["by_flag"],
                   "、".join(r[0] for r in _rep["rows"][:3])))
        else:
            add("priv:location", "ok", "私密篇全在私密文件夹里（位置决定私密）",
                "%d/%d 篇" % (_rep["by_loc"], _rep["total"]))
    except Exception as _e:
        add("priv:location", "warn", "私密位置检查失败", str(_e)[:80])

    # 私密加密包（2026-10-15）：盘上那个 .privpack 与盘上那些独立密文文件的关系。
    # 拆两半是因为 selfcheck 是**子进程**，拿不到库进程内存里的 master：
    #   锁着  → 只报盘面（包多大、目录里有几个非包文件），并**说明它没解锁所以比不了内容**
    #   能解  → 逐篇比长度（密文 = 明文 + 28），报出只在一边的篇
    try:
        import priv_folder as _pf
        _lib = db.manifest().get("library_dir") or ""
        if not _lib:
            add("priv:pack", "ok", "私密加密包", "没配 library_dir，不适用")
        else:
            _sm = _pf.pack_disk_summary(_lib)
            if not _sm["has_pack"]:
                if _sm["disk_files"]:
                    add("priv:pack", "warn", "私密加密包不存在，但目录里有独立密文文件",
                        "目录 %s 下 %d 个文件；包还没建？" % (_sm["dir"], len(_sm["disk_files"])))
                else:
                    add("priv:pack", "ok", "私密加密包", "还没建包，目录也空着")
            else:
                _n = len(_sm["disk_files"])
                _det = ("包 %.1f KB ｜ 目录里另有 %d 个非包文件"
                        % (_sm["pack_bytes"] / 1024.0, _n))
                add("priv:pack", "ok", "私密加密包", _det)
    except Exception as _e:
        add("priv:pack", "warn", "私密加密包检查失败", str(_e)[:80])

    add("names", "ok" if dup == 0 else "warn", "重名（只能靠路径引用）",
        "%d 组；有日期的文件 %d/%d" % (dup, dated, files))

    # ── 备份 ──────────────────────────────────────────────
    snaps = sorted(glob.glob(os.path.join(data_dir, "snapshots", "*.json")))
    if not snaps:
        add("snapshot", "warn", "没有任何快照（tools/snapshot.py 打一份）")
    else:
        newest = max(snaps, key=os.path.getmtime)
        age_h = (time.time() - os.path.getmtime(newest)) / 3600.0
        add("snapshot", "ok" if age_h < 168 else "warn", "最近快照",
            "%s（%.1f 小时前，%s）；共 %d 份"
            % (os.path.basename(newest), age_h, human(os.path.getsize(newest)), len(snaps)))
    reps = sorted(glob.glob(os.path.join(data_dir, "reports", "*.json")))
    if reps:
        newest = max(reps, key=os.path.getmtime)
        add("audit-report", "ok", "最近巡检报告",
            "%s（%.1f 小时前）" % (os.path.basename(newest),
                                 (time.time() - os.path.getmtime(newest)) / 3600.0))

    # ── 卫生 ──────────────────────────────────────────────
    zz = [r["name"] for r in db.conn.execute("SELECT name FROM files WHERE name LIKE 'zz-%' LIMIT 5")]
    zzt = q("SELECT COUNT(*) FROM tombstones WHERE display LIKE 'zz-%'")
    if zz:                                   # 真污染：夹带测试文件
        add("hygiene:zz", "bad", "库里有测试残留（文件）", "%s" % zz)
    elif zzt:                                # 只剩墓碑：run_all 收尾会清，不算故障
        add("hygiene:zz", "warn", "库里有测试残留（只有墓碑）",
            "%d 条 zz- 墓碑（跑一次 tools/run_all.py 会清）" % zzt)
    else:
        add("hygiene:zz", "ok", "库里没有测试残留", "无 zz- 文件/墓碑")
    leftover = [os.path.basename(p) for p in glob.glob(os.path.join(HERE, "tools", "_*"))
                if os.path.basename(p) != "__pycache__"]
    add("hygiene:tmp", "ok" if not leftover else "warn", "tools 下的临时文件",
        "无" if not leftover else "、".join(leftover[:8]))
    smoke_dir = os.path.join(data_dir, "_smoke")
    if os.path.isdir(smoke_dir):
        sz = sum(os.path.getsize(os.path.join(r, f))
                 for r, _d, fs in os.walk(smoke_dir) for f in fs)
        add("hygiene:smoke", "ok", "冒烟沙箱", "%s（下次 smoke.py 自己清）" % human(sz))

    # ── 接口清单与实现是否一致（防"加了接口忘了登记"）────
    try:
        import api_spec as AS
        d = AS.diff()
        miss, extra = d["missing"], d["extra"]
        try:
            cur = json.loads(io.open(AS.OUT, encoding="utf-8").read())
            fresh = (cur == AS.openapi())
        except Exception:
            fresh = False
        detail = "实现 %d 个 / 登记 %d 个" % (len(d["implemented"]), len(d["documented"]))
        if miss:
            detail += "；实现但没登记：%s" % "、".join(miss[:3])
        if extra:
            detail += "；登记了但没实现：%s" % "、".join(extra[:3])
        if not fresh:
            detail += "；docs/openapi.json 不是最新（python tools/api_spec.py --write）"
        add("api_spec", "ok" if (not miss and not extra and fresh) else "warn",
            "接口清单与实现一致", detail)
    except Exception as ex:
        add("api_spec", "warn", "接口清单与实现一致", "对账失败：%s" % str(ex)[:70])

    # ── 文档转换引擎（编辑器导出/新建文档要用） ─────────────
    try:
        import docconvert
        e = docconvert.engines()
        miss = []
        if not e["pandoc"]:
            miss.append("pandoc（读入/写出都靠它）")
        if not e["wps"]:
            miss.append("WPS（出 PDF 靠它）")
        add("engines", "ok" if not miss else "warn", "文档转换引擎",
            "pandoc %s / WPS %s / 能写出 %s%s"
            % (e.get("pandoc_version") or "无", "可用" if e["wps"] else "不可用",
               "、".join(docconvert.WRITE_EXTS),
               ("；缺：" + "、".join(miss)) if miss else ""))
    except Exception as ex:
        add("engines", "warn", "文档转换引擎", "探测失败：%s" % str(ex)[:70])

    # ── 近 24 小时变化（信息） ─────────────────────────────
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - 86400))
    newf = q("SELECT COUNT(*) FROM files WHERE created_at>=?", since)
    modf = q("SELECT COUNT(*) FROM files WHERE mtime>=?", since)
    ev = q("SELECT COUNT(*) FROM events WHERE time>=?", since)
    add("activity", "ok", "近 24 小时", "新增 %d 篇 / 改动 %d 篇 / 事件 %d 条" % (newf, modf, ev))

    # ── 全量体检（可选） ───────────────────────────────────
    if suite:
        t0 = time.time()
        p = subprocess.run([sys.executable, os.path.join(HERE, "tools", "run_all.py")],
                           cwd=HERE, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=3600, creationflags=NO_WINDOW)
        tail = (p.stdout or "").strip().splitlines()[-1:] or [""]
        add("suite", "ok" if p.returncode == 0 else "bad", "全量体检 run_all.py",
            "%s（%.0f 秒）" % (tail[0][:80], time.time() - t0))

    db.close()
    bad_n = sum(1 for c in checks if c["level"] == "bad")
    warn_n = sum(1 for c in checks if c["level"] == "warn")
    return {"ok": bad_n == 0, "bad": bad_n, "warn": warn_n, "checks": checks,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")}


ICON = {"ok": "✅", "warn": "⚠️ ", "bad": "❌"}


def main() -> int:
    ap = argparse.ArgumentParser(description="一键自检（只读）")
    ap.add_argument("--data", default=None)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="只输出 ⚠/❌ 的项")
    ap.add_argument("--suite", action="store_true", help="顺带跑 run_all.py（慢）")
    a = ap.parse_args()
    R = run(a.data or DEFAULT_DATA_DIR, suite=a.suite)
    if a.json:
        print(json.dumps(R, ensure_ascii=False, indent=1))
        return 0 if R["ok"] else 1
    print("自检 %s" % R["created_at"])
    for c in R["checks"]:
        if a.quiet and c["level"] == "ok":
            continue
        line = "%s %-26s %s" % (ICON[c["level"]], c["title"], c["detail"])
        print(line.rstrip())
    print("—— %s：❌ %d 项 / ⚠️ %d 项 / 共 %d 项"
          % ("通过" if R["ok"] else "有问题", R["bad"], R["warn"], len(R["checks"])))
    return 0 if R["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
