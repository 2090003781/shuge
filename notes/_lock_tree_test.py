# -*- coding: utf-8 -*-
"""解锁前后，树里那 8 篇私密篇到底出不出来？—— 用自设密码的临时库实测。

背景：你反馈"成功解锁后搜索框没清空"。我怀疑 `searchGo()` 在
`tryUnlockBySearch()` 里中断了（`insertFilesIntoTree` 抛异常之类），
导致它后面的 `loadTree()` 与清框都没跑到。

这条测试验的是**服务端那一半**：解锁前/后 `/api/tree` 与
`/api/lock/unlock` 的 `revealed` 字段分别是什么。
（客户端那一半要看你的图才能定。）
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

import db as dbmod
import priv_folder as pf
import privpack as pp
import vaultcrypto

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PW = "实测密码-2026"
PORT = 8894
BASE = "http://127.0.0.1:%d" % PORT
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-52s %s" % ("✓" if cond else "✗", name, detail))


def req(path, body=None, method=None, timeout=120):
    m = method or ("POST" if body is not None else "GET")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=m,
                               headers={"Content-Type": "application/json", "X-Writer": "t:locktest"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def up(n=60):
    for _ in range(n):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def tree_priv_names(t):
    """树里出现的私密篇名（按路径比）"""
    out = []

    def walk(n, path=""):
        nm = n.get("name") or ""
        p = (path + "/" + nm) if nm else path
        for f in (n.get("files") or []):
            out.append((p + "/" + (f.get("name") or ""), f.get("name")))
        for c in (n.get("children") or []):
            walk(c, p)
    walk(t)
    return out


print("=" * 78)
print("解锁前/后：树与 revealed 字段")
print("=" * 78)
sandbox = tempfile.mkdtemp(prefix="zz-locktree-")
data = os.path.join(sandbox, "data")
lib = os.path.join(sandbox, "库")
os.makedirs(data, exist_ok=True)
os.makedirs(os.path.join(lib, "个人", "私密"), exist_ok=True)
srv = None
try:
    # 备库：自设密码 + package 模式 + 建 2 篇私密篇（放在私密文件夹里）
    d = dbmod.Database(data)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib)
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib), pp.pack_bytes(k0, pp.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")
    pfid = d.ensure_folder_path("vault/" + pf.PRIV_DIR_REL)
    d.create_file(pfid, "私密甲.md", "甲的正文\n".encode("utf-8"), visibility="private", master=master)
    d.create_file(pfid, "私密乙.md", "乙的正文\n".encode("utf-8"), visibility="private", master=master)
    d.close()

    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(PORT), "--data", data],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
    if not up():
        print("  临时服务没起来"); sys.exit(1)
    print("  临时服务就绪：%s" % BASE)
    print()

    # ── 锁定态 ────────────────────────────────────────────
    print("【锁定态】")
    st, t = req("/api/tree")
    locked_names = [n for _, n in tree_priv_names(t)]
    check("树里没有私密篇", not any("私密甲" in (n or "") for n in locked_names),
          "树里文件数 %d" % len(locked_names))
    check("hidden_private = 2", t.get("hidden_private") == 2, str(t.get("hidden_private")))
    print()

    # ── 解锁 ──────────────────────────────────────────────
    print("【解锁】")
    st, r = req("/api/lock/unlock", {"password": PW})
    check("解锁 200", st == 200 and r.get("ok") is True, str(r.get("error") or "")[:40])
    rev = r.get("revealed") or []
    check("**revealed 里有 2 篇**（前端靠它插树）", len(rev) == 2,
          "revealed %d 条" % len(rev))
    if rev:
        names = sorted(x.get("name") for x in rev)
        check("revealed 的篇名正确", names == ["私密甲.md", "私密乙.md"], str(names))
        ids_ok = all(x.get("id") for x in rev)
        check("revealed 里每行都带 id（插树要用）", ids_ok,
              "缺 id 的 %d 条" % sum(1 for x in rev if not x.get("id")))
        print("     revealed[0] = %s" % json.dumps(
            {k: v for k, v in rev[0].items() if k in ("id", "name", "folder_id", "from_pack")},
            ensure_ascii=False))
    print()

    # ── 解锁后 ────────────────────────────────────────────
    print("【解锁后】")
    st, t2 = req("/api/tree")
    names2 = [n for _, n in tree_priv_names(t2)]
    check("**树里出现了这 2 篇**", any("私密甲" in (n or "") for n in names2)
          and any("私密乙" in (n or "") for n in names2),
          "树里文件数 %d" % len(names2))
    check("hidden_private 变 0", (t2.get("hidden_private") or 0) == 0,
          str(t2.get("hidden_private")))
    print()

    # ── 幂等：再解锁一次 ──────────────────────────────────
    print("【再解锁一次（已解锁）】")
    st, r2 = req("/api/lock/unlock", {"password": PW})
    check("再解锁也 200", st == 200, "HTTP %d" % st)
    check("revealed 仍有 2 篇", len(r2.get("revealed") or []) == 2,
          "%d 条" % len(r2.get("revealed") or []))
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
    print("★ 服务端那一半是好的：解锁→revealed 有 2 篇→树里出现 2 篇")
    print("  → 若你那边没出现，问题就在**客户端**（插入树/刷树那两步）")
print("=" * 78)
sys.exit(0 if not FAIL else 1)
