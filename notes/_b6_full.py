# -*- coding: utf-8 -*-
"""B6 全量：把库内正文交出去（content=NULL），让文件系统成为唯一真相源。

    python -B _b6_full.py --dry    # 干跑：逐篇判「清了会不会丢」+ 记录哈希（只读）
    python -B _b6_full.py --clear  # 真清：只清「盘上逐字节相同」的那些
    python -B _b6_full.py --verify # 清后：读/写/搜三方复验

设计原则（**只信证据，不信来源**）：
  清掉 content 的唯一前提是「盘上存在与库内逐字节相同的副本」。
  不满足 → 一律拒清，并记进 refuse.json 供人复核。宁可少清，不可错清。

分类：
  A 加密篇（enc_algo 非空）  → 密文只在 content + 盘上密文，**永久拒清**
                               （读盘路由已明确绕开：read_disk_content 见 enc_algo 即退库）
  D 盘上逐字节相同            → 安全可清
  B 盘上没有 / C 大小不符 / E 内容不符 → 拒清（先查清为什么）
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

DB = r"D:\core-notes\data\index.db"
API = "http://127.0.0.1:8848"
ROOT = r"D:\枢阁\库"
EVID = r"D:\core-notes\data\tmp\b6_full"
BACKUP = os.path.join(EVID, "content_backup.jsonl")
REFUSE = os.path.join(EVID, "refuse.json")
SUMMARY = os.path.join(EVID, "summary.json")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def api_get(path: str, t: int = 180):
    with urllib.request.urlopen(API + path, timeout=t) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def api_put(path: str, obj, t: int = 180):
    req = urllib.request.Request(API + path, data=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                                 method="PUT",
                                 headers={"Content-Type": "application/json", "X-Writer": "t:b6full"})
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def to_bytes(c) -> bytes:
    """库内 content → 原始字节。

    TEXT 列就是 UTF-8 文本 → 直接编回；BLOB 列 → 原样。
    比较与哈希只认**原始字节**，绝不做转码（转码会掩盖真漂移）。
    """
    if c is None:
        return b""
    if isinstance(c, bytes):
        return c
    return c.encode("utf-8")


def rel_of(r, fold) -> str:
    """库内相对路径（去掉真正那层名为 vault 的库根文件夹）"""
    parts, fid, seen = [], r["folder_id"], set()
    while fid and fid in fold and fid not in seen:
        seen.add(fid)
        p, n = fold[fid]
        parts.append(n)
        fid = p
    names = [n for n in reversed(parts) if n and n.strip()]
    if names and names[0] == "vault":
        names = names[1:]
    return "/".join(names + [r["name"]])


def disk_of(rel: str) -> str:
    return os.path.join(ROOT, *[x for x in rel.split("/") if x])


def classify():
    con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
    con.row_factory = sqlite3.Row
    fold = {r["id"]: (r["parent_id"], r["name"]) for r in con.execute("SELECT id,parent_id,name FROM folders")}
    rows = con.execute("SELECT id,name,folder_id,ext,size,content,enc_algo,storage,visibility,content_hash"
                       " FROM files WHERE content IS NOT NULL").fetchall()
    con.close()

    out = {"A": [], "D": [], "other": []}
    for r in rows:
        raw = to_bytes(r["content"])
        rel = rel_of(r, fold)
        rec = {"id": r["id"], "name": r["name"], "rel": rel, "ext": r["ext"], "size": r["size"],
               "visibility": r["visibility"], "storage": r["storage"], "enc_algo": r["enc_algo"],
               "sha": sha(raw), "nbytes": len(raw)}
        if r["enc_algo"]:
            rec["why"] = "encrypted"
            out["A"].append(rec)
            continue
        p = disk_of(rel)
        if not os.path.isfile(p):
            rec["why"] = "not-on-disk"
            out["other"].append(rec)
            continue
        with open(p, "rb") as f:
            db_bytes = f.read()
        if db_bytes == raw:
            out["D"].append(rec)
        elif len(db_bytes) != len(raw):
            rec["why"] = "size-mismatch disk=%d lib=%d" % (len(db_bytes), len(raw))
            out["other"].append(rec)
        else:
            rec["why"] = "content-mismatch"
            out["other"].append(rec)
    return out


def pre():
    os.makedirs(EVID, exist_ok=True)
    c = classify()
    print("=" * 78)
    print("B6 全量 · 干跑")
    print("=" * 78)
    print("  库内 content 非空      : %d 篇" % (len(c["A"]) + len(c["D"]) + len(c["other"])))
    print("  A 加密篇（永久拒清）    : %d 篇" % len(c["A"]))
    print("  D 盘上逐字节相同（可清）: %d 篇" % len(c["D"]))
    print("  其它（拒清，需人看）    : %d 篇" % len(c["other"]))
    if c["other"]:
        print()
        print("  ── 拒清明细 ──")
        for r in c["other"]:
            print("     %-42s %s" % (r["name"][:42], r["why"]))
    print()
    print("  ── 加密篇（必须永远留着 content）──")
    for r in c["A"]:
        print("     %-42s %d 字节（明文 %s 字节）" % (r["name"][:42], r["nbytes"], r["size"]))

    # 记录「清前状态」证据：每篇的哈希 + 完整正文备份
    with open(BACKUP, "w", encoding="utf-8") as f:
        for grp in ("D", "other", "A"):
            for r in c[grp]:
                con = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"), uri=True)
                row = con.execute("SELECT content FROM files WHERE id=?", (r["id"],)).fetchone()
                con.close()
                raw = to_bytes(row[0])
                # base64 存 byte-for-byte 原文：JSON 里塞字符串会碰坏非法 UTF-8 字节
                f.write(json.dumps({"id": r["id"], "name": r["name"], "rel": r["rel"], "group": grp,
                                    "why": r.get("why", ""), "nbytes": len(raw), "sha": sha(raw),
                                    "b64": base64.b64encode(raw).decode("ascii")},
                                   ensure_ascii=False) + "\n")
    json.dump(c, open(REFUSE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print()
    print("  证据已存：%s（每篇完整正文，可逐字节还原）" % BACKUP)
    return 0


def clear():
    c = classify()
    ids_d = [r["id"] for r in c["D"]]
    ids_a = [r["id"] for r in c["A"]]
    print("=" * 78)
    print("B6 全量 · 清库内正文")
    print("=" * 78)
    if not ids_d:
        print("  没有可清的（全被拒）。中止。")
        return 1

    con = sqlite3.connect(DB, timeout=60)
    before = con.execute("SELECT COUNT(*) FROM files WHERE content IS NOT NULL").fetchone()[0]
    # 只清白名单里的 id —— 白名单来自「盘上逐字节相同」的实测，不来自任何假设
    con.executemany("UPDATE files SET content=NULL WHERE id=?", [(i,) for i in ids_d])
    con.commit()
    after = con.execute("SELECT COUNT(*) FROM files WHERE content IS NOT NULL").fetchone()[0]
    # 反向确认：加密篇一篇都没被动
    still = con.execute("SELECT COUNT(*) FROM files WHERE enc_algo IS NOT NULL AND content IS NOT NULL").fetchone()[0]
    enc_total = con.execute("SELECT COUNT(*) FROM files WHERE enc_algo IS NOT NULL").fetchone()[0]
    con.close()

    print("  清前 content 非空 : %d" % before)
    print("  清后 content 非空 : %d（清了 %d）" % (after, before - after))
    print("  加密篇仍持 content: %d / %d  %s" % (still, enc_total, "✓" if still == enc_total else "✗ 有加密篇被清！"))
    print("  仍在库内的（拒清）: %d 篇" % after)
    json.dump({"before": before, "after": after, "cleared": len(ids_d),
               "encrypted_kept": still, "encrypted_total": enc_total,
               "refused": [r["name"] for r in c["other"]]},
              open(SUMMARY, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    ok = (still == enc_total) and (before - after == len(ids_d))
    print()
    print("  判定：%s" % ("通过" if ok else "不通过 —— 停下来查"))
    return 0 if ok else 1


def verify():
    """清后：抽 40 篇（含最长/最短/多级目录）复验读得到、内容一致。"""
    c = json.load(open(REFUSE, encoding="utf-8"))
    pool = c["D"]
    if not pool:
        print("  没有可验的样本"); return 1
    pool = sorted(pool, key=lambda r: r["nbytes"])
    picks = pool[:5] + pool[len(pool) // 2 - 2:len(pool) // 2 + 3] + pool[-5:]
    picks += [r for r in pool if r["rel"].count("/") >= 3][:8]
    seen, sample = set(), []
    for r in picks:
        if r["id"] not in seen:
            seen.add(r["id"]); sample.append(r)

    print("=" * 78)
    print("B6 全量 · 清后复验（抽 %d 篇）" % len(sample))
    print("=" * 78)
    bad = []
    for r in sample:
        try:
            it = (api_get("/api/content?max=100000000&path=" + urllib.parse.quote("vault/" + r["rel"]))
                  .get("item") or {})
            txt = it.get("content") or ""
            got = sha(txt.encode("utf-8"))
            if not it.get("found") or got != r["sha"]:
                bad.append((r, it.get("found"), got == r["sha"]))
        except Exception as e:
            bad.append((r, "ERR:%s" % str(e)[:40], False))
    print("  读回一致：%d/%d" % (len(sample) - len(bad), len(sample)))
    for r, f, same in bad:
        print("     ✗ %-46s found=%s 一致=%s" % (r["rel"][:46], f, same))

    # 写路径：挑一篇写入再还原
    if sample:
        r = sample[len(sample) // 2]
        p = "vault/" + r["rel"]
        old = (api_get("/api/content?max=100000000&path=" + urllib.parse.quote(p)).get("item") or {}).get("content") or ""
        api_put("/api/files/" + r["id"], {"content": old + "\n\n<!-- B6-全量复验 -->\n"})
        time.sleep(2)
        got = (api_get("/api/content?max=100000000&path=" + urllib.parse.quote(p)).get("item") or {}).get("content") or ""
        wrote = "B6-全量复验" in got
        diskp = disk_of(r["rel"])
        on_disk = os.path.isfile(diskp) and "B6-全量复验" in open(diskp, "rb").read().decode("utf-8", "replace")
        api_put("/api/files/" + r["id"], {"content": old})
        time.sleep(2)
        back = (api_get("/api/content?max=100000000&path=" + urllib.parse.quote(p)).get("item") or {}).get("content") or ""
        print("  写路径：读回含标记=%s  盘上也有=%s  还原一致=%s"
              % (wrote, on_disk, sha(back.encode("utf-8")) == r["sha"]))

    # 检索
    q = sample[0]["name"].rsplit(".", 1)[0][:6]
    try:
        res = api_get("/api/search?q=" + urllib.parse.quote(q))
        hits = res.get("results") or res.get("hits") or []
        print("  检索：搜 %r → %d 条命中" % (q, len(hits)))
    except Exception as e:
        print("  检索 ERR: %s" % e)

    # 加密篇现在读会怎样（应报「锁着」，而不是返回密文）
    enc = c["A"]
    if enc:
        try:
            it = (api_get("/api/content?max=1000000&path=" + urllib.parse.quote("vault/" + enc[0]["rel"]))
                  .get("item") or {})
            body = (it.get("content") or "")
            looks_binary = sum(1 for ch in body[:200] if ord(ch) > 0x2000 or (ord(ch) < 32 and ch not in "\r\n\t")) > 40
            print("  加密篇（未解锁）读回：found=%s 锁着报错=%s 像密文=%s"
                  % (it.get("found"), it.get("locked"), looks_binary))
        except Exception as e:
            print("  加密篇读回 ERR（正常应是「锁着」）：%s" % str(e)[:70])
    return 0 if not bad else 1


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "--dry"
    fn = {"--dry": pre, "--clear": clear, "--verify": verify}.get(mode)
    if fn is None:
        print("用法: --dry | --clear | --verify"); sys.exit(2)
    sys.exit(fn())
