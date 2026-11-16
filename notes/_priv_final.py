# -*- coding: utf-8 -*-
"""终局校验：8 篇私密篇的读回内容 vs 库内 content_hash，逐字节核。"""
import hashlib
import json
import sqlite3
import sys
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
API = "http://127.0.0.1:8848"

con = sqlite3.connect("file:D:/core-notes/data/index.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT id,name,size,content_hash FROM files
                      WHERE visibility='private' ORDER BY name""").fetchall()
con.close()

print("=== 8 篇私密篇：读回字节 vs 库内 content_hash ===")
ok = 0
for r in rows:
    try:
        d = json.loads(urllib.request.urlopen(
            API + "/api/files/" + r["id"], timeout=60).read().decode("utf-8"))
        txt = (d.get("file") or {}).get("content") or ""
        b = txt.encode("utf-8")
        want = (r["content_hash"] or "").replace("sha256:", "")
        got = hashlib.sha256(b).hexdigest()
        h_ok, l_ok = (got == want), (len(b) == r["size"])
        if h_ok and l_ok:
            ok += 1
        print("  %s %-28s 读回 %5d 字节  哈希%s  长度%s"
              % ("✓" if (h_ok and l_ok) else "✗", r["name"][:28], len(b),
                 "✓" if h_ok else "✗", "✓" if l_ok else "✗"))
    except Exception as e:
        print("  ✗ %-28s 读取失败 %s" % (r["name"][:28], str(e)[:50]))

print()
print("  结果：%d / %d 篇读回内容与库内哈希逐字节一致" % (ok, len(rows)))
print()
if ok == len(rows):
    print("  ★ 终局判定：8 篇私密篇【完好、可解、内容零差异】—— 无截断、无损坏")
