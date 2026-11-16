# -*- coding: utf-8 -*-
"""在**真库副本**上跑另外两条（smoke_properties / smoke_frontmatter）。

同 `_hide_on_realcopy.py` 的道理：这两条从 `D:\\core-notes\\data\\index.db`
取数据，而上一轮临时服务用的是空库 → 环境不匹配。

smoke_lock 不在这里跑：它要**自己设密码**，而真库副本已经有密码（KDF 参数不同）
→ 必然"密码不对"。那条只能重启后在真库上跑。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")
ROOT = r"D:\core-notes"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
PORT = 8895
BASE = "http://127.0.0.1:%d" % PORT

TESTS = ["tools/smoke_properties.py", "tools/smoke_frontmatter.py",
         "tools/smoke_private_import.py", "tools/smoke_private_hide.py"]


def up(n=90):
    for _ in range(n):
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


sandbox = tempfile.mkdtemp(prefix="zz-realcopy2-")
data = os.path.join(sandbox, "data")
os.makedirs(data, exist_ok=True)
srv = None
try:
    real = os.path.join(ROOT, "data", "index.db")
    for suf in ("", "-wal", "-shm"):
        s = real + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(data, "index.db" + suf))
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(PORT), "--data", data],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
    if not up():
        print("✗ 临时服务没起来")
        sys.exit(1)
    print("临时服务就绪（真库副本）：%s" % BASE)
    print()

    results = {}
    for script in TESTS:
        p = subprocess.run([sys.executable, "-B", os.path.join(ROOT, script), BASE],
                           cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=900)
        out = p.stdout.decode("utf-8", "replace")
        tail = [l.strip() for l in out.splitlines() if "结论" in l or "失败" in l]
        results[script] = (p.returncode, out)
        print("  %s %-34s [rc=%d] %s" % ("✓" if p.returncode == 0 else "✗",
                                         os.path.basename(script), p.returncode,
                                         tail[-1][:70] if tail else ""))

    bad = [k for k, (rc, _) in results.items() if rc != 0]
    print()
    print("=" * 78)
    if bad:
        for k in bad:
            print("  ── %s 尾部 ──" % k)
            for line in results[k][1].splitlines()[-15:]:
                print("    " + line)
    else:
        print("★ 全部在真数据上通过")
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
