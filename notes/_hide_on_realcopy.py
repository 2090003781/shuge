# -*- coding: utf-8 -*-
"""在**真库副本**上跑 smoke_private_hide（第②条的直接验收）。

上一轮它在空临时服务上跑失败，原因是：
  · 测试从 `D:\\core-notes\\data\\index.db`（**真库文件**）取私密篇 id
  · 而临时服务用的是空的临时库 → 那个 id 在空库里不存在 → 404
**所以那次失败是环境不匹配，不是我的 bug** —— 但"判定"要变成"验证"。

做法：把真库的 index.db **拷进临时目录**，让临时服务 `--data` 指向那份副本 ——
这样测试读的 id 与服务持有的库**是同一份**。

⚠️ 副本是**已加锁状态**（unlocked=false，我没有主人的密码），而这条测试本来就是
   "按当前锁状态断言应该成立的那一侧" —— 正好落在**锁定态**那一侧：
   私密篇不该在 tree / rag_list / 属性索引 / 前像 / 事件里出现，按 id 取也该 403。
   而这正是我改了 `tree(keep_priv_dir)` 之后最该验的东西。
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
PORT = 8896
BASE = "http://127.0.0.1:%d" % PORT


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


sandbox = tempfile.mkdtemp(prefix="zz-realcopy-")
data = os.path.join(sandbox, "data")
os.makedirs(data, exist_ok=True)
srv = None
try:
    # 拷真库（含 wal/shm）到临时目录
    real = os.path.join(ROOT, "data", "index.db")
    for suf in ("", "-wal", "-shm"):
        s = real + suf
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(data, "index.db" + suf))
    print("  已拷真库副本到 %s（%.1f MB）" % (data,
          os.path.getsize(os.path.join(data, "index.db")) / 1048576.0))

    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(PORT), "--data", data],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
    if not up():
        print("  ✗ 临时服务没起来")
        sys.exit(1)
    print("  临时服务就绪：%s（用的是真库副本）" % BASE)

    with urllib.request.urlopen(BASE + "/api/lock", timeout=30) as r:
        print("  副本锁状态：%s" % r.read().decode("utf-8")[:120])
    print()

    print("=" * 78)
    print("跑 smoke_private_hide.py（指向真库副本上的新代码服务）")
    print("=" * 78)
    p = subprocess.run([sys.executable, "-B",
                        os.path.join(ROOT, "tools", "smoke_private_hide.py"), BASE],
                       cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
    out = p.stdout.decode("utf-8", "replace")
    for line in out.splitlines():
        print("  " + line.rstrip())
    print()
    print("=" * 78)
    if p.returncode == 0:
        print("★ 过了 —— 第②条（锁定态隐藏）在**真数据**上被验到")
        print("  而且这同时也是 keep_priv_dir 改动「默认不生效」的实证")
    else:
        print("✗ 没过 —— 要逐条看是不是我的改动引起的")
    print("=" * 78)
    sys.exit(p.returncode)
finally:
    if srv:
        srv.terminate()
        try:
            srv.wait(timeout=15)
        except Exception:
            srv.kill()
    time.sleep(1)
    shutil.rmtree(sandbox, ignore_errors=True)
