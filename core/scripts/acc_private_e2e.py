"""私密进库端到端（打真接口）：起一个临时库 → 设密码 → 走 /api/import private → 验权限。

  python scripts/acc_private_e2e.py

为什么要它：投递箱依赖库的三条 HTTP 契约 ——
  ① 库没解锁时 private 导入必须 403（不许降级成明文）；
  ② 解锁后同样一次调用能进库，且 visibility=private；
  ③ 上锁后读不到正文、解锁后读得到。
真库现在还没设密码（密码只能辞玖自己设），所以先用临时库把契约钉死；
等真库设了密码，同一条路自动生效（投递箱那侧已在真环境验证过会转发 private / 处理 403）。
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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 平台仓根
LIB_SRC = r"D:\core-notes"
PORT = 8851
LIB = "http://127.0.0.1:%d" % PORT
OK = True
SECRET = "sk-E2E-PRIVATE-3f9a 这句不该被搜到"


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:160] if extra else ""))


def call(path, body=None, method="GET", timeout=120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(LIB + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:200]}


def find_file(name):
    """在 /api/tree 里按名字找（返回 (node, 路径)）"""
    _, d = call("/api/tree")
    out = []

    def walk(n, path):
        path = path + [n.get("name", "")]
        for f in n.get("files", []):
            if f["name"] == name:
                out.append((f, "/".join([p for p in path if p])))
        for c in n.get("children", []):
            walk(c, path)

    walk(d["tree"], [])
    return out[0] if out else (None, None)


data_dir = tempfile.mkdtemp(prefix="ai-notes-e2e-")
vault = tempfile.mkdtemp(prefix="e2e-vault-")
priv_dir = os.path.join(vault, "个人", "私密")
os.makedirs(priv_dir)
secret_file = os.path.join(priv_dir, "密钥.md")
with open(secret_file, "w", encoding="utf-8") as f:
    f.write("# 密钥\n\n" + SECRET + "\n")
srv = None

try:
    print("① 起临时库（%s，data=%s）" % (LIB, data_dir))
    srv = subprocess.Popen([sys.executable, os.path.join(LIB_SRC, "src", "server.py"),
                            "--port", str(PORT), "--data", data_dir],
                           cwd=LIB_SRC, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    up = False
    for _ in range(60):
        try:
            st, _d = call("/api/health", timeout=5)
            up = st == 200
            if up:
                break
        except Exception:
            pass
        time.sleep(1)
    check("临时库起来了", up)
    if not up:
        raise SystemExit(1)

    print("② 还没设密码：private 导入必须被挡（403），且库里不留东西")
    st, d = call("/api/import", {"src": secret_file, "under": "vault/个人/私密",
                                 "rag": False, "private": True}, method="POST")
    check("403 locked", st == 403 and d.get("locked") is True, d)
    node, _p = find_file("密钥.md")
    check("库里没有这篇（没降级成明文）", node is None)

    print("③ 设库密码 → 同一次调用就能进库，且是 private")
    st, d = call("/api/lock/set", {"password": "e2e-pw-9527", "confirm": "e2e-pw-9527"},
                 method="POST")
    check("密码已设且已解锁", st == 200 and d.get("unlocked") is True, d)
    st, d = call("/api/import", {"src": secret_file, "under": "vault/个人/私密",
                                 "rag": False, "private": True}, method="POST")
    check("imported=1 且 private=1", st == 200 and d.get("imported") == 1 and d.get("private") == 1, d)
    node, where = find_file("密钥.md")
    check("落在 vault/个人/私密", where == "vault/个人/私密", where)
    check("visibility=private", node and node.get("visibility") == "private",
          node and node.get("visibility"))

    print("④ 解锁态：正文读得到")
    st, d = call("/api/files/" + node["id"])
    check("locked=false 且正文含密钥内容",
          d["file"]["locked"] is False and SECRET in d["file"]["content"], d["file"]["locked"])

    print("⑤ 上锁：正文读不到（且不给报错，只给 locked 标记）")
    st, d = call("/api/lock/lock", {}, method="POST")
    check("已上锁", st == 200 and d.get("unlocked") is False, d)
    st, d = call("/api/files/" + node["id"])
    check("locked=true 且正文为空",
          d["file"]["locked"] is True and d["file"]["content"] == "", d["file"].get("locked"))
    st, d = call("/api/search?q=" + urllib.parse.quote("sk-E2E-PRIVATE"))
    hits = (d.get("hits") or [])
    check("锁定态搜不到", not any(h.get("file_id") == node["id"] for h in hits), len(hits))
finally:
    if srv:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
    shutil.rmtree(data_dir, ignore_errors=True)
    shutil.rmtree(vault, ignore_errors=True)
    print("⑥ 临时库进程与临时目录已清理")

print("\n========  结论：%s" % ("全部通过" if OK else "有失败项"))
sys.exit(0 if OK else 1)
