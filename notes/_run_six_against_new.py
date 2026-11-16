# -*- coding: utf-8 -*-
"""对着跑新代码的临时服务，跑 6 条与私密相关的库体检项。

为什么挑这 6 条：它们覆盖的正是我今晚改动最密集的地方 ——
  smoke_private_hide  → 锁定态隐藏（第②条 keep_priv_dir 的直接验收）
  smoke_lock          → 私密加锁/解锁全流程（第①⑥条与包路由）
  smoke_private_import→ 私密篇导入
  smoke_frontmatter   → 前置属性（写正文时顺带做的）
  smoke_properties    → 属性索引（改动碰过 hide_private）
  smoke_bin           → 二进制/池存储（_write_blob 那条路）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\core-notes"
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbmod
import priv_folder as pf
import privpack as pp
import vaultcrypto

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PW = "验收密码"
PORT = 8897
BASE = "http://127.0.0.1:%d" % PORT

TESTS = [
    ("tools/smoke_private_hide.py", "锁定态隐藏（第②条）"),
    ("tools/smoke_lock.py", "私密加锁/解锁全流程"),
    ("tools/smoke_private_import.py", "私密篇导入"),
    ("tools/smoke_frontmatter.py", "前置属性"),
    ("tools/smoke_properties.py", "属性索引"),
    ("tools/smoke_bin.py", "二进制/池存储"),
]


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


sandbox = tempfile.mkdtemp(prefix="zz-batch6-")
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
    print("临时服务就绪：%s" % BASE)
    print()

    results = {}
    for script, desc in TESTS:
        p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, script), BASE],
                           cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=900)
        out = p.stdout.decode("utf-8", "replace")
        # 提取"通过/失败"统计行
        tail = [l.strip() for l in out.splitlines()
                if ("通过" in l or "失败" in l or "结论" in l) and len(l.strip()) < 140]
        results[script] = (p.returncode, out, tail[-3:] if tail else [])
        print("  %s %-32s [rc=%d] %s" % ("✓" if p.returncode == 0 else "✗",
                                         os.path.basename(script), p.returncode,
                                         " ｜ ".join(results[script][2])[:90]))

    print()
    bad = [k for k, (rc, _, _) in results.items() if rc != 0]
    print("=" * 78)
    if bad:
        print("有 %d 条没过：" % len(bad))
        for k in bad:
            print()
            print("  ── %s 输出尾部 ──" % k)
            for line in results[k][1].splitlines()[-20:]:
                print("    " + line)
    else:
        print("★ 6 条全过（对着跑新代码的服务）—— 私密相关路径在接口层也验过了")
    print("=" * 78)
    sys.exit(1 if bad else 0)
finally:
    if srv:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()
    time.sleep(1)
    shutil.rmtree(sandbox, ignore_errors=True)
