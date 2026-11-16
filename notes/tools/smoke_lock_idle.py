# -*- coding: utf-8 -*-
"""会话超时上锁：默认**不自动上锁**，设了 AI_NOTES_LOCK_TIMEOUT 才开（辞玖 2026-09-28）。

为什么要它：这段逻辑只有 4 行（server.py 的 master()/lock_info()），但以前**一个测试都没有**。
而它出错的后果是"隐私以为锁着、其实一直开着"，或者反过来"你正写着突然被锁、存不上去"。
两种都不能靠肉眼看出来，所以两侧都断言：

· 默认（不设环境变量）：解锁后 expires_in 必须是 0，放着不动也不许自己上锁；
· 设了 AI_NOTES_LOCK_TIMEOUT=3：到点必须真的回锁。

全程对着**临时库**跑（临时 data + 随机端口），真库完全不碰。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PW = "smoke-idle-9527"
PASS = 0
FAIL = []


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    print("  %s %s%s" % ("✅" if cond else "❌", name, "" if cond else "  | " + str(extra)[:200]))
    if not cond:
        FAIL.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(base, timeout=90):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def call(base, path, method="GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def with_lib(env_timeout, fn):
    """起一个临时库，跑 fn(base)，无论成败都收摊"""
    port = free_port()
    data = tempfile.mkdtemp(prefix="ai-notes-idle-")
    base = "http://127.0.0.1:%d" % port
    env = dict(os.environ)
    env.pop("AI_NOTES_LOCK_TIMEOUT", None)          # 先清干净，免得外面环境串味
    if env_timeout is not None:
        env["AI_NOTES_LOCK_TIMEOUT"] = str(env_timeout)
    srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "src", "server.py"),
                            "--port", str(port), "--data", data],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
    try:
        if not wait_health(base):
            print("临时库没起来（port=%d）" % port)
            return
        fn(base)
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=10)
        except Exception:
            srv.kill()
        shutil.rmtree(data, ignore_errors=True)


def unlock(base):
    st, r = call(base, "/api/lock/set", "POST", {"password": PW, "confirm": PW})
    return st, r


def case_default(base):
    print("① 默认：不设 AI_NOTES_LOCK_TIMEOUT → 不许自动上锁")
    st, r = unlock(base)
    check("设密码后已解锁", st == 200 and r.get("unlocked") is True, r)
    check("expires_in = 0（0 就是「不自动上锁」）", r.get("expires_in") == 0, r.get("expires_in"))
    time.sleep(5)                                    # 故意晾着，比旧默认的 900 秒当然等不到，
    st, lk = call(base, "/api/lock")                 # 但只要有人把它写回小值，这一步就会抓到
    check("晾 5 秒后仍然解锁着", lk.get("unlocked") is True, lk)
    call(base, "/api/tree")                          # 发个真请求，确认不是"标志位对但实际锁了"
    st, lk2 = call(base, "/api/lock")
    check("发过请求后也仍然解锁着", lk2.get("unlocked") is True, lk2)
    check("库规模读得到（真在服务）", call(base, "/api/health")[1]["stats"]["files"] == 0)
    st, r = call(base, "/api/lock/lock", "POST", {})
    check("手动点锁 → 立刻上锁", st == 200 and r.get("unlocked") is False, r)
    st, lk3 = call(base, "/api/lock")
    check("上锁后状态如实", lk3.get("configured") is True and lk3.get("unlocked") is False, lk3)


def case_timeout(base):
    print("② 设了 AI_NOTES_LOCK_TIMEOUT=3 → 到点必须真回锁")
    st, r = unlock(base)
    check("设密码后已解锁", st == 200 and r.get("unlocked") is True, r)
    left = r.get("expires_in")
    check("expires_in 报出了剩余秒数（1~3）", isinstance(left, int) and 1 <= left <= 3, left)
    st, lk = call(base, "/api/lock")
    check("刚解锁时还锁着吗 → 没锁", lk.get("unlocked") is True, lk)
    time.sleep(4)
    st, lk = call(base, "/api/lock")
    check("闲置超过 3 秒 → 自动回锁", lk.get("unlocked") is False, lk)
    check("回锁后 expires_in 归 0", lk.get("expires_in") == 0, lk)
    check("密码还在（只是锁了，没被清）", lk.get("configured") is True, lk)


with_lib(None, case_default)
print()
with_lib(3, case_timeout)

print("\n========  结论: %d 通过 / %d 失败" % (PASS, len(FAIL)))
raise SystemExit(1 if FAIL else 0)
