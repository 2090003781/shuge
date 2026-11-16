import json, urllib.request, urllib.parse, sys, hashlib
sys.stdout.reconfigure(encoding="utf-8")
API = "http://127.0.0.1:8848"
import sqlite3
con = sqlite3.connect("file:D:/core-notes/data/index.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
fold = {r["id"]: (r["parent_id"], r["name"]) for r in con.execute("SELECT id,parent_id,name FROM folders")}
def rel_of(r):
    parts, fid, seen = [], r["folder_id"], set()
    while fid and fid in fold and fid not in seen:
        seen.add(fid); p, n = fold[fid]; parts.append(n); fid = p
    names = [n for n in reversed(parts) if n and n.strip()]
    return "/".join(names + [r["name"]])
rows = con.execute("SELECT id,name,folder_id,size,content_hash FROM files WHERE enc_algo IS NOT NULL ORDER BY name").fetchall()
con.close()

print("=== 解锁后逐篇验证（只报长度/哈希，不外显正文）===")
ok = 0
for r in rows:
    rel = rel_of(r)
    try:
        u = API + "/api/content?max=100000000&path=" + urllib.parse.quote(rel)
        d = json.loads(urllib.request.urlopen(u, timeout=60).read().decode("utf-8"))
        it = d.get("item") or {}
        txt = it.get("content") or ""
        b = txt.encode("utf-8")
        want = (r["content_hash"] or "").replace("sha256:", "")
        got = hashlib.sha256(b).hexdigest()
        h_ok = (got == want)
        l_ok = (len(b) == r["size"])
        # 判断是不是明文：可打印/中文占比
        sample = txt[:300]
        printable = sum(1 for ch in sample if ch.isprintable() or ch in "\r\n\t") / max(1, len(sample))
        is_text = printable > 0.9
        if it.get("found") and h_ok and l_ok:
            ok += 1
            print("  ✓ %-28s %6d 字节  哈希✓ 长度✓ 是明文=%s" % (r["name"][:28], len(b), is_text))
        else:
            print("  ✗ %-28s found=%s 哈希%s 长度%s 是明文=%s"
                  % (r["name"][:28], it.get("found"), "✓" if h_ok else "✗", "✓" if l_ok else "✗", is_text))
    except Exception as e:
        print("  ✗ %-28s 读取失败 %s: %s" % (r["name"][:28], type(e).__name__, str(e)[:50]))
print()
print("  可读且与库内哈希一致：%d / %d 篇" % (ok, len(rows)))
if ok == len(rows):
    print("  ★ 判定：8 篇私密笔记全部完好、可解、明文正确 —— 数据没有损坏")