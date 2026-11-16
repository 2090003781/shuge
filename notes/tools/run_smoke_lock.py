"""跑私密加锁冒烟，但对着**临时库**跑（真库完全不碰）。

  python tools/run_smoke_lock.py

为什么要绕这一下：smoke_lock 要在目标库上临时设密码再还原。真库旁边站着投递箱 ——
库一解锁，它就会把 个人/私密 那几篇加密进库，用的却是**测试密码派生的主密钥**；
等 kdf 被还原，那几行永远解不开。所以给它一个自己的临时库（临时 data + 空库 + 随机端口），
跑完连库带目录一起删。
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(base, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


port = free_port()
data = tempfile.mkdtemp(prefix="ai-notes-lock-")
base = "http://127.0.0.1:%d" % port
srv = None
try:
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(port), "--data", data],
                           cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    if not wait_health(base):
        print("临时库没起来（port=%d）" % port)
        sys.exit(1)
    print("临时库已就绪：%s（data=%s）" % (base, data))
    # 收下来再打：子进程直接继承 stdout 时，它崩在管道里会一个字都看不到（2026-09-28）
    p = subprocess.run([sys.executable, os.path.join(HERE, "smoke_lock.py"), base, "--data", data],
                       cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=NO_WINDOW)
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    if out:
        print(out)
    sys.exit(p.returncode)
finally:
    if srv:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
    shutil.rmtree(data, ignore_errors=True)
    print("临时库与临时目录已清理（真库未被触碰）")
