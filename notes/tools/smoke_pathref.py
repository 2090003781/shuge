"""路径式引用的验收：`@目录/名字`（重名笔记只能这么引）。

覆盖：重名靠路径区分 / 唯一后缀也能定位 / 改名跟着改 / 移动跟着改 / 文件夹改名跟着改 /
小节 `#P1` 在路径改写时保留 / 删掉后被引用方标不可用 / 嵌入掩码把整条路径吃掉。
"""
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from mentions import mask_mentions, parse_refs        # noqa: E402

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8848").rstrip("/")
OK = True
NAME = "zz-路径-重名"


def call(path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
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
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra)) if extra else ""))


def disp_path(p):
    p = str(p or "")
    return p.split("vault/", 1)[-1] if "vault/" in p else p


def text_of(fid):
    return call("/api/files/" + fid)[1]["file"]["content"]


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def tool(*args):
    p = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "convert_refs.py")] + list(args),
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    return p.returncode, p.stdout, p.stderr


print("⓪ 解析（纯函数）")
r = parse_refs("见 @知识库/01-编程通识/08-调试方法论#P2 和 @zz-重名")
check("路径+小节一起解析", r[0]["name"] == "知识库/01-编程通识/08-调试方法论"
      and r[0]["section"] == "P2", r[0])
check("路径式引用的锚点是路径", r[0]["anchor"] == "知识库/01-编程通识/08-调试方法论#P2", r[0]["anchor"])
m = mask_mentions("见 @知识库/01-编程通识/08-调试方法论#P2 那条。")
check("嵌入掩码把整条路径吃掉", "知识库" not in m and "P2" not in m and "（引用）" in m, m)

def _zz_count():
    """只数测试自己的东西（`zz-` 前缀）。

    全库 files 数**不再是测试独占**的：日志转录插件（plugin.transcript）在同一个库里
    持续追加，随时可能新建一个会话笔记 → 拿全库计数比"复原"必然偶发翻红（2026-10-04）。
    """
    n = 0
    def walk(node):
        nonlocal n
        for f in (node.get("files") or []):
            if (f.get("name") or "").startswith("zz-"):
                n += 1
        for c in (node.get("children") or []):
            walk(c)
    walk(call("/api/tree")[1].get("tree") or {})
    return n


files_before = _zz_count()
made_files, made_folders = [], []
try:
    print("① 两个文件夹里各放一篇同名笔记")
    st, fa = call("/api/folders", "POST", {"name": "zz-路径A", "path": ""})
    st, fb = call("/api/folders", "POST", {"name": "zz-路径B", "path": ""})
    made_folders += [fa["folder_id"], fb["folder_id"]]
    pa, pb = "zz-路径A", "zz-路径B"          # 都建在根下，显示路径就是目录名
    a = call("/api/files", "POST", {"folder_id": fa["folder_id"], "name": NAME + ".md",
                                    "content": "# A 篇\n\n## P1 甲节\n\n这是 A 的正文 ZZPATHA。\n"})[1]["file_id"]
    b = call("/api/files", "POST", {"folder_id": fb["folder_id"], "name": NAME + ".md",
                                    "content": "# B 篇\n\n这是 B 的正文 ZZPATHB。\n"})[1]["file_id"]
    made_files += [a, b]
    check("两篇同名笔记都在", bool(a) and bool(b))
    check("A 的引用路径", pa.endswith("zz-路径A"), pa)

    print("② 光写名字（重名）解析不了；写路径就能指到具体那篇")
    st, made = call("/api/files", "POST", {"name": "zz-路径-引用方.md",
                                           "content": "# 引用方\n\n先空着。\n"})
    ref = made["file_id"]
    made_files.append(ref)
    body = ("# 引用方\n\n"
            "只写名字：见 @%s 那篇。\n\n"
            "写全路径：见 @%s/%s 那篇。\n\n"
            "写后缀：见 @%s/%s 那篇。\n"
            % (NAME, pa, NAME, os.path.basename(pa), NAME))
    st, res = call("/api/files/" + ref, "PUT", {"content": body})
    check("保存成功", st == 200 and res.get("ok"), res.get("error"))
    check("重名那条进了 unresolved", NAME in (res.get("saved", {}).get("unresolved") or []),
          res.get("saved", {}).get("unresolved"))
    men = {m2["anchor"]: m2["id"] for m2 in (call("/api/files/" + ref)[1]["file"].get("mentions") or [])}
    check("全路径那条连到了 A", men.get("%s/%s" % (pa, NAME)) == a, men)
    check("唯一后缀那条也连到了 A", men.get("%s/%s" % (os.path.basename(pa), NAME)) == a, men)

    print("③ 改名：路径式引用跟着改")
    st, rn = call("/api/files/%s/rename" % a, "POST", {"name": NAME + "V2.md"})
    t = text_of(ref)
    check("引用处路径改成新名", ("@%s/%sV2" % (pa, NAME)) in t, [l for l in t.splitlines() if "V2" in l])
    check("后缀式那条也改了", ("@%s/%sV2" % (os.path.basename(pa), NAME)) in t)

    print("④ 移动：路径式引用跟着改（小节要保留）")
    st, fc = call("/api/folders", "POST", {"name": "zz-路径C", "path": ""})
    made_folders.append(fc["folder_id"])
    pc = "zz-路径C"
    # 这里是“把引用方换成带小节的一行”（故意缩短）：短缩闸对脚本要明说
    call("/api/files/" + ref, "PUT",
         {"content": "# 引用方\n\n带小节：见 @%s/%sV2#P1 那一节。\n" % (pa, NAME),
          "allow_shrink": True})
    st, mv = call("/api/files/%s/move" % a, "POST", {"folder_id": fc["folder_id"]})
    check("移动接口返回改了引用", st == 200 and mv.get("refs_rewritten", 0) >= 1, mv)
    t = text_of(ref)
    check("引用处变成新路径且小节还在",
          ("@%s/%sV2#P1" % (pc, NAME)) in t, [l for l in t.splitlines() if "#P1" in l])

    print("⑤ 文件夹改名：子树里的路径式引用跟着改")
    st, fr = call("/api/folders/%s/rename" % fc["folder_id"], "POST", {"name": "zz-路径D"})
    pd = disp_path(fr.get("path") or "") or "zz-路径D"
    t = text_of(ref)
    check("引用处路径换成新目录名",
          ("@%s/%sV2#P1" % (pd, NAME)) in t, [l for l in t.splitlines() if "#P1" in l])

    print("⑥ 按路径取那一篇/那一节")
    fid = None
    men = {m2["anchor"]: m2["id"] for m2 in (call("/api/files/" + ref)[1]["file"].get("mentions") or [])}
    fid = men.get("%s/%sV2#P1" % (pd, NAME))
    check("路径式引用连的仍是同一篇", fid == a, men)
    if fid:
        art = call("/api/files/%s/article?section=P1" % fid)[1]["article"]
        check("能取到那一节", "ZZPATHA" in (art.get("text") or ""), art.get("mode"))

    print("⑦ 名字带空格的引用：引号形式")
    sp = call("/api/files", "POST", {"name": "zz-路径 带空格.md",
                                     "content": "# 带空格的名字\n\n正文 ZZSPACE。\n"})[1]["file_id"]
    made_files.append(sp)
    r2 = call("/api/files", "POST", {"name": "zz-路径-引用方2.md",
                                     "content": "# 引用方2\n\n先空着。\n"})[1]["file_id"]
    made_files.append(r2)
    st, res2 = call("/api/files/" + r2, "PUT",
                    {"content": "# 引用方2\n\n不带引号：见 @zz-路径 带空格 那篇。\n"})
    check("不带引号只认到第一个词（解析不到）",
          "zz-路径" in (res2.get("saved", {}).get("unresolved") or []),
          res2.get("saved", {}).get("unresolved"))
    st, res3 = call("/api/files/" + r2, "PUT",
                    {"content": '# 引用方2\n\n带引号：见 @"zz-路径 带空格" 那篇。\n'})
    men2 = {m2["anchor"]: m2["id"] for m2 in (call("/api/files/" + r2)[1]["file"].get("mentions") or [])}
    check("引号形式能连上", men2.get('"zz-路径 带空格"') == sp, men2)

    print("⑦b 工具补引号：@名字 带空格 → @\"名字 带空格\"")
    call("/api/files/" + r2, "PUT", {"content": "# 引用方2\n\n不带引号：见 @zz-路径 带空格 那篇。\n"})
    rc, out, err = tool("--fix-spaces", "--only", r2, "--apply")
    check("补引号退出码 0", rc == 0, err[-200:])
    t2 = text_of(r2)
    check("正文补成了引号形式", '@"zz-路径 带空格"' in t2, [l for l in t2.splitlines() if "带空格" in l])
    check("名字后面的词没被吃掉", "那篇。" in t2, [l for l in t2.splitlines() if "带空格" in l])
    men3 = {m2["anchor"]: m2["id"] for m2 in (call("/api/files/" + r2)[1]["file"].get("mentions") or [])}
    check("补完连上了", men3.get('"zz-路径 带空格"') == sp, men3)

    print("⑧ 删除后被引用方标不可用（按名字部分）")
    call("/api/files/" + a, "DELETE")
    made_files.remove(a)
    dead = call("/api/files/" + ref)[1]["file"].get("deleted_refs") or []
    check("报出被删的名字", (NAME + "V2") in dead, dead)
finally:
    print("⑨ 清场")
    for fid in made_files:
        call("/api/files/" + fid, "DELETE")
    for fid in made_folders:
        call("/api/folders/" + fid, "DELETE")
    import sqlite3
    c = sqlite3.connect(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "data", "index.db"))
    c.execute("DELETE FROM tombstones WHERE display LIKE 'zz-%'")
    c.commit()
    c.close()
    after = _zz_count()
    check("测试自己的文件数复原（只数 zz-）", after == files_before, "%s → %s" % (files_before, after))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
