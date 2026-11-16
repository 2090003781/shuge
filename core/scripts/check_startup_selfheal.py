#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**启动自愈自检**（可重跑）：库起不来时，主进程要"当场说清"，并且能自己修的那部分要自己修。

辞玖 2026-10-10：「起不来服务应该是代码的问题，不要这个页面，确保永远能够启动。」

两种真实故障各验一次（每次起一个临时主进程，用独立 -dir，因此锁/端口都互不冲突）：

  ① **库不在这台机器上**（COREHOST_NOTES_DIR 指到一个空目录）
     → 应当写出 `data/notes-start-failed.txt`，里面写明"启动脚本不在位"；
  ② **解释器是假的**（COREHOST_NOTES_PYTHON 指到一个不存在的 exe）
     → **自愈**：换成能用的 python，库照样起得来；状态接口里 `python` 不再是那个假路径。

用法：python scripts\check_startup_selfheal.py
"""
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

CORE = r"D:\core"
HOST = os.path.join(CORE, "bin", "core-host.exe")
NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:200]))
    if not cond:
        fails.append(name)


def free_ports(n):
    """挑几个空闲端口（避开 8790/8795/8848，免得跟现役那套打架）"""
    out, socks = [], []
    while len(out) < n:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        if p in (8790, 8795, 8848):
            s.close()
            continue
        socks.append(s)
        out.append(p)
    for s in socks:
        s.close()
    return out


def run_case(name, env_extra, expect_ok):
    """起一个临时主进程，返回 (ret, 端口, 状态json, 说明文件内容)"""
    tmp = tempfile.mkdtemp(prefix="shuge-selfheal-")
    cdir = os.path.join(tmp, "core")
    os.makedirs(os.path.join(cdir, "bin"), exist_ok=True)
    os.makedirs(os.path.join(cdir, "data"), exist_ok=True)
    console, notes = free_ports(2)
    env = dict(os.environ)
    env.update(env_extra)
    env["COREHOST_NOTES_PORT"] = str(notes)
    # 2026-10-10：单实例锁默认是全机唯一 → 临时实例要用独立锁名才能起来（COREHOST_LOCK_NAME）
    env["COREHOST_LOCK_NAME"] = "core-host-test-%d" % console
    proc = subprocess.Popen([HOST, "-serve", "-dir", cdir, "-notes-port", str(notes)],
                            cwd=cdir, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", creationflags=NW)
    status = None
    port_file = os.path.join(cdir, "data", "host.port")
    for _ in range(60):
        time.sleep(0.5)
        if os.path.isfile(port_file):
            break
    real = None
    try:
        real = int(open(port_file).read().strip())
    except Exception:
        pass
    if real:
        for _ in range(30):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/host/status" % real, timeout=30) as r:
                    status = json.loads(r.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(1)
    fail_file = os.path.join(cdir, "data", "notes-start-failed.txt")
    ftext = open(fail_file, encoding="utf-8", errors="replace").read() if os.path.isfile(fail_file) else ""
    proc.kill()
    try:
        proc.wait(timeout=10)
    except Exception:
        pass
    time.sleep(1)
    shutil.rmtree(tmp, ignore_errors=True)
    return real, status, ftext


def main():
    print("① 库不在这台机器上（COREHOST_NOTES_DIR 指向空目录）")
    empty = tempfile.mkdtemp(prefix="shuge-empty-notes-")
    try:
        real, status, ftext = run_case("missing", {"COREHOST_NOTES_DIR": empty}, expect_ok=False)
        chk("临时主进程起来了（独立 -dir，没打架）", bool(real), real)
        chk("写出了 notes-start-failed.txt", bool(ftext), ftext[:120])
        if ftext:
            print("     说明文件前 6 行：")
            for line in ftext.splitlines()[:6]:
                print("       " + line)
            chk("说明里点明「启动脚本不在位」", "启动脚本不在位" in ftext, ftext[:100])
            chk("说明里给出了库目录/解释器", "库目录" in ftext and "解释器" in ftext, "")
        if status:
            diag = (status.get("notes") or {}).get("diag") or {}
            chk("状态接口带上 diag", bool(diag), json.dumps(diag, ensure_ascii=False)[:140])
            chk("diag 里 script_ok=false", diag.get("script_ok") is False, diag)
    finally:
        shutil.rmtree(empty, ignore_errors=True)

    print("② 解释器是假的（COREHOST_NOTES_PYTHON 指向不存在的 exe）→ 要自愈")
    real, status, ftext = run_case("badpython",
                                   {"COREHOST_NOTES_PYTHON": r"C:\nonexistent\python.exe"}, expect_ok=True)
    chk("临时主进程起来了", bool(real), real)
    if status:
        diag = (status.get("notes") or {}).get("diag") or {}
        print("     diag：%s" % json.dumps(diag, ensure_ascii=False)[:200])
        chk("解释器已自愈（不再是那个假路径）",
            bool(diag.get("python")) and "nonexistent" not in str(diag.get("python")), diag.get("python"))
        chk("自愈后 python_ok=true", diag.get("python_ok") is True, diag)
        notes = status.get("notes") or {}
        chk("库起得来（healthy=true）", notes.get("healthy") is True, notes)
    else:
        chk("拿到状态（自愈后）", False, "状态接口没答")

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 启动自愈：库缺失时当场说清；解释器不对时自己换掉；临时实例互不打架")
    return 0


if __name__ == "__main__":
    sys.exit(main())
