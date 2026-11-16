"""快照工具验收：打快照 → 改一篇 → --diff 报出来 → --restore 还原回来；顺带 --list。"""
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
NAME = "zz-快照-验收"


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def tool(*args):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "snapshot.py")] + list(args),
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return p.returncode, p.stdout, p.stderr


A = "# 快照验收\n\n第一版内容 ZZSNAPONE。\n"
B = "# 快照验收\n\n第二版内容 ZZSNAPTWO。\n"
files_before = call("/api/health")[1]["stats"]["files"]
fid, snap, made = None, None, []
try:
    print("① 造一篇临时笔记")
    fid = call("/api/files", "POST", {"name": NAME + ".md", "content": A})[1]["file_id"]
    made.append(fid)

    print("② 打快照")
    rc, out, err = tool()
    check("快照退出码 0", rc == 0, err[-200:])
    m = re.search(r"快照：([^\s（]+)", out)   # 别把中文括号后的字数统计吃进路径
    snap = m.group(1) if m else None
    check("拿到了快照文件", bool(snap) and os.path.isfile(snap), out.strip()[:90])
    if not (snap and os.path.isfile(snap)):
        raise SystemExit("快照文件没拿到，后面没法验")

    print("③ --list 能看到它")
    rc, out2, err2 = tool("--list")
    check("列表里有这份快照", snap and os.path.basename(snap) in out2, out2.strip().splitlines()[-1] if out2 else "")

    print("④ 改内容 → --diff 报出来")
    call("/api/files/" + fid, "PUT", {"content": B})
    rc, out3, err3 = tool("--diff", snap, "--json")
    d = json.loads(out3)
    check("diff 里报出这篇变了", any(NAME in str(x) for x in d["changed"]) or
          any(x == fid for x in d["added"]), d["changed"][:3])

    print("⑤ --restore（只还原这一篇）把内容带回来")
    rc, out4, err4 = tool("--restore", snap, "--only", fid)
    check("还原退出码 0", rc == 0, err4[-200:])
    got = call("/api/files/" + fid)[1]["file"]["content"]
    check("内容回到快照那一版", got == A, got[:60].replace("\n", "⏎"))
finally:
    print("⑥ 清场")
    for x in made:
        call("/api/files/" + x, "DELETE")
    if snap and os.path.isfile(snap):
        os.remove(snap)
        print("   （删掉验收用的快照）")
    import sqlite3
    c = sqlite3.connect(os.path.join(ROOT, "data", "index.db"))
    c.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    c.commit()
    c.close()
    after = call("/api/health")[1]["stats"]["files"]
    check("文件数复原", after == files_before, "%s → %s" % (files_before, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
