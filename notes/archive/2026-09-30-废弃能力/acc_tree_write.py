"""验收：接口是唯一写入口 —— 接口写完，库自己把文件落到库文件夹，镜像再推到 vault。

  python tools/acc_tree_write.py

四步：① 新建 → 库文件夹里立刻有；② 改正文 → 库文件夹跟着变；③ 镜像一轮 → vault 也有；
④ 删除 → 两侧都没有。跑完清干净。
"""
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

LIB = "http://127.0.0.1:8848"
LIBDIR = r"D:\枢阁\库"
VAULT = r"D:\个人数据\辞玖"
SUB = "_验收_接口写盘"
NAME = "接口写盘.md"
OK = True


def call(path, body=None, method="GET", headers=None, timeout=600):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(LIB + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:160] if extra else ""))


def read(p):
    return open(p, "rb").read().decode("utf-8", "replace") if os.path.isfile(p) else None


if call("/api/mirror")[1].get("enabled") is not True:
    print("镜像管线已关闭（库已正式启用，vault 只读参考）→ 跳过本验收")
    print("\n========  结论：全部通过（跳过）")
    sys.exit(0)

root = None
st, d = call("/api/tree")
tree = d.get("tree") or {}
root = tree.get("id")
vault_id = next((c["id"] for c in (tree.get("children") or []) if c.get("name") == "vault"), root)
# 目标文件夹：已经有一个同名的就复用（重名的留着就是残渣），没有再建
_st, _fs = call("/api/folders")
_exist = next((f for f in (_fs.get("folders") or []) if (f.get("path") or "").endswith("/" + SUB)), None)
if _exist:
    sub_id = _exist["id"]
    print("   （复用已存在的文件夹 %s）" % SUB)
else:
    st, folder = call("/api/folders", {"parent_id": vault_id, "name": SUB}, "POST")
    sub_id = folder.get("folder_id") or vault_id
fid = None
p_lib = os.path.join(LIBDIR, SUB, NAME)
p_vault = os.path.join(VAULT, SUB, NAME)
try:
    print("① 走接口新建")
    st, d = call("/api/files", {"folder_id": sub_id, "name": NAME, "content": "# 一版\n\n接口建的。\n"},
                 "POST", {"X-Writer": "tool:tree-test"})
    fid = d.get("file_id")
    check("建成功", st in (200, 201) and bool(fid), d.get("error") or st)

    time.sleep(0.3)
    check("库文件夹里立刻有这份", read(p_lib) is not None, p_lib)
    check("内容就是刚写的", (read(p_lib) or "").startswith("# 一版"), (read(p_lib) or "")[:20])

    print("② 走接口改正文")
    st, d = call("/api/files/%s" % fid, {"content": "# 二版\n\n接口改的。\n"}, "PUT",
                 {"X-Writer": "tool:tree-test"})
    check("改成功", st == 200 and d.get("ok") is not False, d.get("error"))
    time.sleep(0.3)
    check("库文件夹跟着变", "二版" in (read(p_lib) or ""), (read(p_lib) or "")[:20])

    print("③ 镜像一轮 → vault 也有")
    st, d = call("/api/mirror/sync", {"apply": True, "import": False}, "POST")
    time.sleep(0.3)
    check("vault 里出现了", read(p_vault) is not None, p_vault)
    check("vault 内容与库文件夹一致", read(p_vault) == read(p_lib))

    print("④ 走接口删除 → 两侧都撤")
    st, d = call("/api/files/%s" % fid, None, "DELETE", {"X-Writer": "tool:tree-test"})
    check("删成功", st == 200 and d.get("deleted") == fid, d.get("error"))
    fid = None
    call("/api/mirror/sync", {"apply": True, "import": False}, "POST")
    time.sleep(0.3)
    check("库文件夹里没了", not os.path.isfile(p_lib))
    check("vault 里也没了", not os.path.isfile(p_vault))
finally:
    if fid:
        call("/api/files/%s" % fid, None, "DELETE", {"X-Writer": "tool:tree-test"})
    for p in (os.path.join(LIBDIR, SUB), os.path.join(VAULT, SUB)):
        shutil.rmtree(p, ignore_errors=True)
    call("/api/mirror/sync", {"apply": True, "import": False}, "POST")
    # 自己建的文件夹也要删掉（以前不删 → 每跑一次留一个重名文件夹，库界面里一排删不掉的）
    if sub_id and sub_id != vault_id:
        _st, _d = call("/api/folders/" + sub_id, "DELETE")
        print("   清场：文件夹 %s → HTTP %s %s" % (SUB, _st, "" if _st == 200 else _d.get("error")))
    print("⑤ 收尾：测试目录已清")

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
