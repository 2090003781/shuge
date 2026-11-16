"""验收现场清理：库里删掉 201 个测试文件 + 测试文件夹，磁盘也删掉，最后核对篇数回到 1164。

只动 vault/_验收_事件驱动 这一处，别的一概不碰；每删一批打印进度，结尾自证。
"""

import os as _os, sys as _sys          # 门禁用（2026-09-30 体检）

if "--yes" not in _sys.argv and _os.environ.get("COREHOST_ALLOW_DESTRUCTIVE") != "1":
    print(__doc__ or "")
    print("\n【门禁】%s 会真的动到**运行中的枢阁 / 库文件夹**（这个脚本忽略参数，--help 也照跑）。"
          % _os.path.basename(__file__))
    print("       确认要跑：python %s --yes（或设 COREHOST_ALLOW_DESTRUCTIVE=1）"
          % _os.path.basename(__file__))
    _sys.exit(2)
import json
import os
import shutil
import urllib.request

LIB = "http://127.0.0.1:8848"
FOLDER = "ef45034e-01d1-4cc3-aaa6-43322b288c88"
DISK = r"D:\个人数据\辞玖\_验收_事件驱动"
BASE = 1164


def req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.load(resp)


def find_folder():
    d = json.load(urllib.request.urlopen(LIB + "/api/tree", timeout=120))
    hits = []

    def walk(n, path):
        path = path + [n.get("name", "")]
        if n["id"] == FOLDER:
            hits.append((n, "/".join([p for p in path if p])))
        for c in n.get("children", []):
            walk(c, path)

    walk(d["tree"], [])
    return hits[0] if hits else (None, None)


node, where = find_folder()
if node is None:
    print("库中已无该测试文件夹")
else:
    files = node.get("files", [])
    print("待删：%s 下 %d 个文件" % (where, len(files)))
    for i, f in enumerate(files, 1):
        req("DELETE", "%s/api/files/%s" % (LIB, f["id"]))
        if i % 50 == 0:
            print("  已删 %d/%d" % (i, len(files)))
    print("  文件删完，删文件夹")
    try:
        print("  folder drop:", req("DELETE", "%s/api/folders/%s" % (LIB, FOLDER)))
    except Exception as e:
        print("  folder drop 失败：", e)

if os.path.isdir(DISK):
    shutil.rmtree(DISK)
    print("磁盘目录已删：", DISK)

left, where2 = find_folder()
print("复查：树里还有该文件夹吗 →", "有" if left else "没有")
print("复查：磁盘还有吗 →", "有" if os.path.isdir(DISK) else "没有")
h = json.load(urllib.request.urlopen(LIB + "/api/health", timeout=120))
print("篇数：%d（应为 %d）→ %s" % (h["stats"]["files"], BASE,
                                 "对上了" if h["stats"]["files"] == BASE else "对不上！"))
