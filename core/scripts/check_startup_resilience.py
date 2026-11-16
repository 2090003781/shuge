#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**启动可靠性自检**（可重跑）：候选端口被占满时，主进程必须"说清楚是谁占的"，而不是静默退出。

辞玖 2026-10-10：「起不来服务应该是代码的问题，不要这个页面，确保永远能够启动」。

为什么要在测试里**先停掉计划任务**：`core-watchdog` 每 2 分钟会把主进程拉回来 ——
不暂停它，"全被占"这个假设就不成立（测试会自己把宿主拉回来）。测完把任务恢复并立刻触发一次。

验三件事：
  ① 候选端口表够宽（从源码读，≥ 30 个）；
  ② 极端情况：候选端口全占上 → 主进程很快退出，且写出 `data/host-start-failed.txt`，
     逐个点名"端口 / pid / 进程名"（"点启动没反应"的对立面）；
  ③ 清场：恢复计划任务并把主进程拉回来（确认 8790 应答）。

用法：python scripts\check_startup_resilience.py
"""
import io
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

CORE = r"D:\core"
HOST = os.path.join(CORE, "bin", "core-host.exe")
TASK = "core-watchdog"
NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:200]))
    if not cond:
        fails.append(name)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NW)


def candidates_from_source():
    s = io.open(os.path.join(CORE, "cmd", "core-host", "serve.go"), encoding="utf-8").read()
    out = []
    for a, b in re.findall(r"for p := (\d+); p <= (\d+); p\+\+", s):
        out += list(range(int(a), int(b) + 1))
    return out


def host_up(port=8790, t=4):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=t).read()
        return True
    except Exception:
        return False


def kill_hosts():
    r = run(["tasklist", "/FI", "IMAGENAME eq core-host.exe", "/FO", "CSV", "/NH"])
    for line in (r.stdout or "").splitlines():
        if "core-host.exe" in line:
            run(["taskkill", "/PID", line.split(",")[1].strip('"'), "/F"])


def main():
    cands = candidates_from_source()
    print("① 候选端口表（从源码里读）")
    print("   %d 个：%s%s" % (len(cands), cands[:12], " …" if len(cands) > 12 else ""))
    chk("候选端口 ≥ 30 个", len(cands) >= 30, len(cands))

    print("② 极端情况：候选端口全被占")
    was_disabled = "Disabled" in run(["schtasks", "/Query", "/TN", TASK, "/FO", "LIST"]).stdout
    run(["schtasks", "/Change", "/TN", TASK, "/DISABLE"])
    print("   已暂停计划任务 %s（否则它 2 分钟内会把主进程拉回来，测试就不成立）" % TASK)
    kill_hosts()
    for _ in range(12):
        time.sleep(1)
        if not host_up():
            break
    chk("前置：主进程已停、8790 空出来", not host_up(), "8790 仍有应答")

    socks = []
    for p in cands:
        try:
            sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sk.bind(("127.0.0.1", p))
            sk.listen(1)
            socks.append(sk)
        except OSError:
            pass
    occupied = sorted({sk.getsockname()[1] for sk in socks})
    chk("占住了候选端口（≥ 25）", len(socks) >= 25, len(socks))

    tmp = tempfile.mkdtemp(prefix="shuge-startup-")
    cdir = os.path.join(tmp, "core")
    os.makedirs(os.path.join(cdir, "bin"), exist_ok=True)
    os.makedirs(os.path.join(cdir, "data"), exist_ok=True)
    try:
        t0 = time.time()
        proc = subprocess.Popen([HOST, "-serve", "-dir", cdir], cwd=cdir,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", creationflags=NW)
        out = ""
        exited = True
        try:
            out, _ = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            exited = False
        dt = time.time() - t0
        print("   主进程退出：%s（%.1f 秒，退出码 %s）" % ("是" if exited else "否", dt, proc.returncode))
        chk("很快退出（≤ 20 秒，不卡住）", exited and dt <= 20, "%.1f 秒" % dt)

        f = os.path.join(cdir, "data", "host-start-failed.txt")
        ok_file = os.path.isfile(f)
        chk("写出 data/host-start-failed.txt", ok_file, f)
        if ok_file:
            txt = io.open(f, encoding="utf-8", errors="replace").read()
            print("   失败说明（前 6 行）：")
            for line in txt.splitlines()[:6]:
                print("     " + line)
            port_lines = [l for l in txt.splitlines() if "被占" in l]
            chk("逐个点名被占端口（≥ 10 条）", len(port_lines) >= 10, len(port_lines))
            chk("带上了 pid", any("pid=" in l for l in port_lines), port_lines[:2])
            chk("带上了进程名", any("（" in l and "）" in l for l in port_lines), port_lines[:2])
            chk("给了处理办法", ("任务管理器" in txt or "找到 pid" in txt), txt[-100:])
    finally:
        for sk in socks:
            try:
                sk.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)

    print("③ 清场：恢复计划任务并把主进程拉回来")
    run(["schtasks", "/Change", "/TN", TASK, "/ENABLE"])
    if was_disabled:
        print("   （原本就是停用状态；本脚本按【要能跑】处理，已启用）")
    run(["schtasks", "/Run", "/TN", TASK])
    back = False
    for _ in range(45):
        time.sleep(1)
        if host_up():
            back = True
            break
    chk("主进程已恢复（8790 应答）", back, "等 45 秒")

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 启动可靠性：候选端口 30 个；全被占时快速退出并写清原因；清场后主进程已恢复")
    return 0


if __name__ == "__main__":
    sys.exit(main())
