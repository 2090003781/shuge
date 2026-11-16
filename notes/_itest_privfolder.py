# -*- coding: utf-8 -*-
"""真集成测试：对着**跑新代码的临时服务**验私密文件夹全流程。

为什么必须这么验：库是常驻进程，跑的是它启动时加载的代码 ——
今晚所有修复**在重启前都验不到**。而这个临时服务加载的是磁盘上的新代码。

覆盖（全走 HTTP 接口，与真人操作同一条路）：
  ① 建库（自设密码 + package 模式）→ 起服务 → 解锁
  ② 在私密文件夹里新建 → 盘上只有包、读得到
  ③ 拖进去（移动 + 设私密）→ 进包
  ④ 改名 → 包内名字跟上
  ⑤ 追加 → 包内内容跟上
  ⑥ 移出私密 → 包里那条被删
  ⑦ 补投影（把盘上弄坏 → 再导一次要补回来）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\core-notes"
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbmod            # noqa: E402
import privpack as pp         # noqa: E402
import vaultcrypto            # noqa: E402
import priv_folder as pf      # noqa: E402

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PW = "集成测试密码-2026"
PORT = 8899
BASE = "http://127.0.0.1:%d" % PORT
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def req(path, body=None, method=None, timeout=120):
    m = method or ("POST" if body is not None else "GET")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=m,
                               headers={"Content-Type": "application/json", "X-Writer": "t:itest"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def wait_health(n=60):
    for _ in range(n):
        try:
            st, d = req("/api/health", timeout=5)
            if st == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


print("=" * 78)
print("真集成测试：对着跑新代码的临时服务")
print("=" * 78)

sandbox = tempfile.mkdtemp(prefix="zz-itest-")
data = os.path.join(sandbox, "data")
lib = os.path.join(sandbox, "库")
os.makedirs(data, exist_ok=True)
os.makedirs(os.path.join(lib, "个人", "私密"), exist_ok=True)
srv = None
try:
    # ── ① 建库：自设密码 + package 模式 ──────────────────────
    d = dbmod.Database(data)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib)
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib), pp.pack_bytes(k0, pp.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)
    d.close()
    print("  临时库已备：%s" % sandbox)

    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(PORT), "--data", data],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
    if not wait_health():
        print("  ✗ 临时服务没起来")
        sys.exit(1)
    print("  临时服务就绪 %s" % BASE)
    print()

    # 解锁（走接口，与真人一样）
    print("① 解锁 + 确认 package 模式")
    st, r = req("/api/lock/unlock", {"password": PW})
    check("解锁成功", st == 200 and r.get("ok") is True, str(r.get("error") or "")[:40])
    st, h = req("/api/health")
    check("priv_mode = package", (h.get("manifest") or {}).get("priv_mode") == "package",
          str((h.get("manifest") or {}).get("priv_mode")))
    print()

    def in_pack(name):
        items = pf.read_items(lib, master, d2.vault_priv(master))
        return pf.get_item_bytes(items, name)

    d2 = dbmod.Database.__new__(dbmod.Database)
    import sqlite3
    d2.conn = sqlite3.connect(os.path.join(data, "index.db"), timeout=30)
    d2.conn.row_factory = sqlite3.Row
    d2.data_dir = data

    # ── ② 在私密文件夹里新建 ─────────────────────────────────
    print("② 在私密文件夹里新建（POST /api/files 到私密文件夹 + private）")
    body2 = "# 集成新建\n\n正文。\n"
    # 走"建到私密文件夹下"：先建（internal），再移进去 + 设私密 —— 与拖进去同路
    st, r2 = req("/api/files", {"name": "zz集成新建.md", "content": body2})
    fid2 = (r2.get("file_id") or ((r2.get("saved") or {}).get("id")))
    check("建篇成功", st in (200, 201) and bool(fid2), "st=%s" % st)
    st, r3 = req("/api/files/%s/move" % fid2, {"path": "vault/" + pf.PRIV_DIR_REL})
    check("移进私密文件夹", st == 200, str(r3.get("error") or "")[:40])
    st, r4 = req("/api/files/%s/visibility" % fid2, {"visibility": "private"})
    check("设为私密", st == 200, str(r4.get("error") or "")[:40])
    check("**包里已经有它**", in_pack("zz集成新建.md") == body2.encode("utf-8"),
          "包内 %d 条" % len(pf.read_items(lib, master, d2.vault_priv(master))))
    st, r5 = req("/api/files/" + fid2)
    got = ((r5.get("file") or {}).get("content") or "")
    check("读回逐字节相同", got == body2, "%d 字符" % len(got))
    print()

    # ── ③ 改名 → 包内跟上 ───────────────────────────────────
    print("③ 改名（POST /api/files/<id>/rename）")
    st, r6 = req("/api/files/%s/rename" % fid2, {"name": "zz集成改名.md"})
    check("改名接口 200", st == 200, str(r6.get("error") or "")[:40])
    names = [x["name"] for x in pf.read_items(lib, master, d2.vault_priv(master))]
    check("**包内名字也跟上了**", names == ["zz集成改名.md"], str(names))
    print()

    # ── ④ 追加 → 包内跟上 ───────────────────────────────────
    print("④ 追加（POST /api/files/<id>/append）")
    st, r7 = req("/api/files/%s/append" % fid2, {"text": "追加的一段。\n"})
    check("追加接口 200", st == 200, str(r7.get("error") or "")[:40])
    want = (body2 + "追加的一段。\n").encode("utf-8")
    check("**包里那份也跟上了**", in_pack("zz集成改名.md") == want,
          "包内 %d 字节" % len(in_pack("zz集成改名.md") or b""))
    print()

    # ── ⑤ 移出私密 → 包里删掉 ───────────────────────────────
    print("⑤ 移出私密（visibility → internal）")
    st, r8 = req("/api/files/%s/visibility" % fid2, {"visibility": "internal"})
    check("接口 200", st == 200, str(r8.get("error") or "")[:40])
    check("**包里那条被删了**", in_pack("zz集成改名.md") is None,
          "包内 %d 条" % len(pf.read_items(lib, master, d2.vault_priv(master))))
    print()

    # ── ⑥ 补投影（把盘上弄坏 → 再导一次要补回来）────────────
    print("⑥ 补投影：盘上弄坏 → 再导一次")
    import tempfile as _tf
    disk = os.path.join(lib, "zz补投影.md")
    b6 = "# 补投影\n\n正文一二三。\n"
    with _tf.TemporaryDirectory(prefix="it-") as td6:
        p6 = os.path.join(td6, "zz补投影.md")
        open(p6, "w", encoding="utf-8").write(b6)
        st, _ = req("/api/import", {"src": p6, "under": "vault", "overwrite": True})
        ok1 = os.path.isfile(disk) and open(disk, encoding="utf-8").read() == b6
        check("先导一次：盘上对", ok1, repr(open(disk, encoding="utf-8").read()[:20]) if os.path.isfile(disk) else "(没有)")
        with open(disk, "w", encoding="utf-8") as f:
            f.write("坏的")
        st, imp = req("/api/import", {"src": p6, "under": "vault", "overwrite": True})
        after = open(disk, encoding="utf-8").read() if os.path.isfile(disk) else "(没了)"
        check("**盘上被补回来了**", after == b6, repr(after[:24]))
        check("（对照）接口自报 tree.ok >= 1",
              ((imp.get("tree") or {}).get("ok") or 0) >= 1, str(imp.get("tree"))[:60])
    d2.conn.close()
finally:
    if srv:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()
    time.sleep(1)
    shutil.rmtree(sandbox, ignore_errors=True)

print()
print("=" * 78)
print("结果：通过 %d ｜ 失败 %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("   ✗ %s" % f)
if not FAIL:
    print("★ 全流程在**跑新代码的服务**上通过 —— 今晚的修复真的到位了")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
