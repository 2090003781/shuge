# -*- coding: utf-8 -*-
"""对着**跑新代码的临时服务**跑那两条原本失败的体检项。

为什么：库是常驻进程，跑的是启动时加载的代码。要验"新代码修好了没有"，
必须让测试连到一个加载了新代码的服务上。

做法：备一个临时库（含密码与目录配置）→ 起 server.py（新代码）→
把 smoke_append / acc_write_through 的 BASE 指向它 → 跑。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import json

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\core-notes"
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbmod
import privpack as pp
import priv_folder as pf
import vaultcrypto

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PW = "验收密码"
PORT = 8898
BASE = "http://127.0.0.1:%d" % PORT


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


sandbox = tempfile.mkdtemp(prefix="zz-verify-")
data = os.path.join(sandbox, "data")
lib = os.path.join(sandbox, "库")
os.makedirs(data, exist_ok=True)
os.makedirs(os.path.join(lib, "个人", "私密"), exist_ok=True)
srv = None
try:
    d = dbmod.Database(data)
    salt = vaultcrypto.new_salt()
    master = vaultcrypto.derive(PW, salt, 1 << 12, 8, 1)
    d.set_vault_password(master, salt, n=1 << 12, r=8, p=1)
    d.set_manifest(library_dir=lib)
    k0 = vaultcrypto.new_key()
    pp.write_file(pf.pack_abs(lib), pp.pack_bytes(k0, pp.seal_key(k0, d.vault_pub()), []))
    d.set_manifest(priv_mode="package")
    d.close()

    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(PORT), "--data", data],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
    if not up():
        print("临时服务没起来")
        sys.exit(1)
    # 解锁（让私密相关路径可用）
    body = json.dumps({"password": PW}).encode("utf-8")
    rq = urllib.request.Request(BASE + "/api/lock/unlock", data=body, method="POST",
                                headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(rq, timeout=60) as r:
        print("解锁：%s" % r.read().decode("utf-8")[:80])
    print("临时服务就绪：%s（库文件夹=%s）" % (BASE, lib))
    print()

    results = {}
    for script in ("tools/smoke_append.py", "tools/acc_write_through.py"):
        print("=" * 78)
        print("跑 %s（指向新代码服务）" % script)
        print("=" * 78)
        p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, script), BASE],
                           cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = p.stdout.decode("utf-8", "replace")
        for line in out.splitlines():
            if ("❌" in line or "✅" in line and ("盘上" in line or "补回来" in line)
                    or "失败" in line or "结论" in line or "通过" in line):
                print("  " + line.strip())
        results[script] = (p.returncode, out)
        print("  退出码 %d" % p.returncode)
        print()

    print("=" * 78)
    ok = all(rc == 0 for rc, _ in results.values())
    for k, (rc, _) in results.items():
        print("  %s  %s" % ("✓" if rc == 0 else "✗", k))
    print()
    if ok:
        print("★ 两条都过了 —— 之前的失败确实是「常驻库是旧代码」，新代码已修好")
    else:
        print("✗ 仍有失败 —— 说明修复不彻底，要继续查")
        for k, (rc, out) in results.items():
            if rc != 0:
                print()
                print("  ── %s 的输出尾部 ──" % k)
                for line in out.splitlines()[-25:]:
                    print("    " + line)
    print("=" * 78)
    sys.exit(0 if ok else 1)
finally:
    if srv:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()
    time.sleep(1)
    shutil.rmtree(sandbox, ignore_errors=True)
