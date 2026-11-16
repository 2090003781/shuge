"""镜像同步端到端验收（真目录、真接口）：两个方向 + 清理。

  python tools/acc_mirror.py

四个场景：
  ① 库文件夹新建 → 同步 → 出现在 vault + 进库
  ② vault 新建    → 同步 → 出现在库文件夹 + 进库
  ③ vault 改内容  → 同步 → 库文件夹跟着变 + 库内正文更新
  ④ 清理：两侧删文件 + 库里删行 + 确认篇数回到开工前
"""
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

LIB = "http://127.0.0.1:8848"
LIBCFG = "http://127.0.0.1:8848/api/mirror"
LIBDIR = r"D:\枢阁\库"
VAULT = r"D:\个人数据\辞玖"
SUB = "_验收_镜像"
NAME_A = "甲-库建的.md"
NAME_B = "乙-vault建的.md"
OK = True


def call(path, body=None, method="GET", timeout=900):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(LIB + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:150] if extra else ""))


def sync():
    st, d = call("/api/mirror/sync", {"apply": True}, "POST")
    assert st == 200, d
    return d


def files_count():
    return call("/api/health")[1]["stats"]["files"]


def find(name):
    st, d = call("/api/tree")
    hits = []

    def walk(n, path):
        path = path + [n.get("name", "")]
        for f in n.get("files", []):
            if f["name"] == name:
                hits.append((f["id"], "/".join([p for p in path if p])))
        for c in n.get("children", []):
            walk(c, path)

    walk(d["tree"], [])
    return hits


def body(fid):
    return call("/api/files/" + fid)[1]["file"]["content"]


# 2026-09-27 库正式启用后镜像默认关闭：这两个脚本专测镜像，关着就跳过
# （不自己临时打开 —— 那一开一跑会把库里的待同步内容顺手推到 vault，正是启用后不该发生的事）
if call("/api/mirror")[1].get("enabled") is not True:
    print("镜像管线已关闭（库已正式启用，vault 只读参考）→ 跳过本验收")
    print("\n========  结论：全部通过（跳过）")
    sys.exit(0)

before = files_count()
print("开工前篇数：", before)
pa = os.path.join(LIBDIR, SUB, NAME_A)
pb = os.path.join(VAULT, SUB, NAME_B)
try:
    print("① 库文件夹新建 → 同步")
    os.makedirs(os.path.dirname(pa), exist_ok=True)
    with open(pa, "wb") as f:
        f.write(f"# 库建的\n\n这是库文件夹里新建的（{time.time():.0f}）。\n".encode("utf-8"))
    sync()
    vb = os.path.join(VAULT, SUB, NAME_A)
    check("进了 vault", os.path.isfile(vb), vb)
    hits = find(NAME_A)
    check("也进了库", bool(hits), hits[:1])
    check("库内正文对得上", hits and "库文件夹里新建的" in body(hits[0][0]))

    print("② vault 新建 → 同步")
    os.makedirs(os.path.dirname(pb), exist_ok=True)
    with open(pb, "wb") as f:
        f.write("# vault 建的\n\n这是 vault 里新建的。\n".encode("utf-8"))
    sync()
    check("进了库文件夹", os.path.isfile(os.path.join(LIBDIR, SUB, NAME_B)))
    hits2 = find(NAME_B)
    check("也进了库", bool(hits2), hits2[:1])

    print("③ vault 改内容 → 同步")
    with open(pb, "wb") as f:
        f.write("# vault 建的\n\n改过了：vault 更新版。\n".encode("utf-8"))
    time.sleep(1.2)                     # 让 mtime 明确变新
    with open(pb, "ab") as f:
        f.write(b"\n")
    r = sync()
    check("库文件夹跟着变", "vault 更新版" in open(os.path.join(LIBDIR, SUB, NAME_B),
                                            encoding="utf-8").read())
    hits3 = find(NAME_B)
    check("库内正文跟着更新", hits3 and "vault 更新版" in body(hits3[0][0]),
          r["import"])
    check("同步报告无错误", not r["mirror"]["errors"] and not (r.get("import") or {}).get("failed"),
          r["mirror"]["errors"][:2])
finally:
    print("④ 清理")
    for name in (NAME_A, NAME_B):
        for fid, _w in find(name):
            call("/api/files/" + fid, method="DELETE")
    for p in (os.path.join(LIBDIR, SUB), os.path.join(VAULT, SUB)):
        shutil.rmtree(p, ignore_errors=True)
    sync()                              # 让两边都回到"没有这些文件"的状态
    # 镜像把目录导进库时会建文件夹行：自己弄出来的要自己删（否则库里留一排同名的空文件夹）
    for _f in (call("/api/folders")[1].get("folders") or []):
        if "_验收_镜像" in (_f.get("path") or ""):
            _st, _d = call("/api/folders/" + _f["id"], "DELETE")
            print("   清场：文件夹 %s → HTTP %s %s"
                  % (_f.get("path"), _st, "" if _st == 200 else _d.get("error")))
    time.sleep(0.5)
    print("   两侧目录已删；篇数 %d（开工前 %d）→ %s"
          % (files_count(), before, "对上了" if files_count() == before else "对不上"))

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
