#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**启动顺序演练**：停光三件套 → 按指定方式拉起 → 断言"主程序 → 库 → 内核"的先后。

辞玖 2026-10-06：「打开顺序固定好，先主程序然后是库最后启动内核」。
判据用**进程创建时间**（psutil create_time），不靠日志措辞：
    t(主程序 core-host.exe) ≤ t(库 python src/server.py) ≤ t(内核 core.exe)
（允许相等：拉起是顺序发起，但同一秒内完成也正常。）

用法：
    python scripts\check_boot_order.py                 # 用看门狗那条路（计划任务/保活）
    python scripts\check_boot_order.py --by=icon       # 用桌面图标那条路（--open-web，会开浏览器）
    python scripts\check_boot_order.py --keep-running  # 演练完不关（默认也保持运行，停下时再拉起）
"""
import argparse
import os
import socket
import subprocess
import sys
import time

import psutil

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
HOST = r"D:\core\bin\core-host.exe"
WATCHDOG = os.path.join(CORE, "tools", "core_watchdog.ps1")
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def up(port):
    s = socket.socket()
    s.settimeout(0.5)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def procs():
    """{角色: (pid, 创建时间)}"""
    out = {}
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            nm = (p.info.get("name") or "").lower()
            cl = " ".join(p.info.get("cmdline") or [])
            if nm == "core-host.exe":
                out.setdefault("主程序", (p.info["pid"], p.info["create_time"]))
            elif nm == "core.exe":
                out.setdefault("内核", (p.info["pid"], p.info["create_time"]))
            elif nm in ("python.exe", "pythonw.exe") and "src/server.py" in cl:
                out.setdefault("库", (p.info["pid"], p.info["create_time"]))
        except Exception:
            pass
    return out


def stop_all():
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            nm = (p.info.get("name") or "").lower()
            cl = " ".join(p.info.get("cmdline") or [])
            if nm in ("core-host.exe", "core.exe") or (nm.startswith("python") and "src/server.py" in cl):
                p.kill()
        except Exception:
            pass
    for _ in range(120):
        time.sleep(0.5)
        if not procs() and not any(up(p) for p in (8790, 8795, 8848)):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--by", choices=["watchdog", "icon"], default="watchdog")
    a = ap.parse_args()

    print("① 停光三件套…")
    print("   停干净：%s" % stop_all())
    print("② 用「%s」那条路拉起" % ("看门狗（计划任务/保活）" if a.by == "watchdog" else "桌面图标 --open-web"))
    if a.by == "watchdog":
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", WATCHDOG],
                         cwd=CORE, creationflags=FLAGS)
    else:
        subprocess.Popen([HOST, "--open-web", "--by=启动顺序演练（图标路径）"], cwd=CORE,
                         creationflags=0x08000000 | 0x00000008, close_fds=True)
    print("③ 等三件套都起来（最多 120 秒）…")
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(0.5)
        if up(8790) and up(8795) and up(8848):
            break
    ps = procs()
    print("   起来的是：%s" % ("、".join(sorted(ps)) or "（一个都没起）"))
    for role in ("主程序", "库", "内核"):
        if role in ps:
            pid, ct = ps[role]
            print("   %-4s pid=%-7s 启动时刻 %s" % (role, pid, time.strftime("%H:%M:%S", time.localtime(ct))))
    ok = all(r in ps for r in ("主程序", "库", "内核"))
    if ok:
        t_host, t_notes, t_core = ps["主程序"][1], ps["库"][1], ps["内核"][1]
        ordered = t_host <= t_notes + 1 and t_notes <= t_core + 1     # 允许 1 秒容差（同一拍发起）
        print("④ 顺序断言：主程序(%.0f) → 库(%.0f) → 内核(%.0f)  %s"
              % (t_host, t_notes, t_core, "✅ 正确" if ordered else "❌ 顺序不对"))
    else:
        ordered = False
    ports = {p: up(p) for p in (8790, 8795, 8848)}
    print("⑤ 端口：8790=%s 8795=%s 8848=%s" % (ports[8790], ports[8795], ports[8848]))
    print("⑥ 库在日志里的顺序证据（host.log 里最近几行，按时间）")
    lg = os.path.join(CORE, "data", "logs", "host.log")
    if os.path.isfile(lg):
        lines = io_tail(lg, 12)
        for l in lines:
            print("   " + l[:160])
    return 0 if (ordered and all(ports.values())) else 1


def io_tail(path, n):
    import io
    with io.open(path, encoding="utf-8", errors="replace") as f:
        return [x.rstrip("\n") for x in f.readlines()[-n:]]


if __name__ == "__main__":
    sys.exit(main())
