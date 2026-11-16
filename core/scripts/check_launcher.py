#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""模拟辞玖那一按：把包解到别处，双击 `启动枢阁.exe`，看它到底有没有反应。

两种情形都验：
  A. 机器上**已经有**一个枢阁在跑（就是现在这台机器的常态）→ 应当照样打开控制台页面（不再"没反应"）；
  B. 本机没有枢阁在跑 → 应当把启停台拉起来、库与内核跟着起、再打开页面。

跑完把本机原本那个宿主放回去（用计划任务拉），并清掉临时目录。
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

TMP = os.path.join(os.environ.get("TEMP", r"C:\Windows\Temp"), "shuge-launch-test")
CF = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def run(cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("creationflags", CF)
    return subprocess.run(cmd, **kw)


def hosts():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq core-host.exe", "/FO", "CSV", "/NH"],
                         capture_output=True, text=True, creationflags=CF).stdout
    return [l.split(",")[1].strip('"') for l in out.splitlines() if "core-host.exe" in l]


def kill_hosts():
    for pid in hosts():
        run(["taskkill", "/PID", pid, "/F"])


def up(port=8790, tries=30):
    for _ in range(tries):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=3).read()
            return True
        except Exception:
            time.sleep(1)
    return False


def main():
    zip_path = max(glob.glob(os.path.join(os.path.expanduser("~"), "Desktop", "枢阁-程序包-*.zip")),
                   key=os.path.getmtime)
    print("包：%s" % os.path.basename(zip_path))
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP)
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(TMP)
    pkg = os.path.join(TMP, "枢阁-程序包")
    launcher = os.path.join(pkg, "启动枢阁.exe")
    print("启动器：%s（%d 字节）" % (launcher, os.path.getsize(launcher)))
    log = os.path.join(pkg, "core", "data", "logs", "host.log")

    fails = []
    try:
        # A：本机已有枢阁在跑（现状）→ 双击启动器，应当打开页面（宿主日志里会记一条"已在默认浏览器打开"）
        print("\nA. 本机已有枢阁在跑：双击启动器")
        assert hosts(), "前置：本机应当已经有一个宿主在跑"
        n0 = len(hosts())
        if os.path.isfile(log):
            os.remove(log)
        subprocess.Popen([launcher], cwd=os.path.dirname(launcher), creationflags=CF)
        time.sleep(6)
        tail = open(log, encoding="utf-8", errors="replace").read() if os.path.isfile(log) else ""
        opened = "已在默认浏览器打开枢阁" in tail
        print("   宿主日志里出现【已在默认浏览器打开枢阁】：%s" % ("✅" if opened else "❌"))
        print("   宿主数量 %d → %d（不该多出一个）" % (n0, len(hosts())))
        if not opened or len(hosts()) != n0:
            fails.append("A：已有实例时双击启动器没打开页面")

        # B：把本机宿主停掉 → 双击启动器，应当把启停台拉起来（→ 库 → 内核）并打开页面
        print("\nB. 本机没有枢阁在跑：双击启动器")
        kill_hosts()
        time.sleep(3)
        assert not hosts(), "前置：应当已经全停"
        if os.path.isfile(log):
            os.remove(log)
        subprocess.Popen([launcher], cwd=os.path.dirname(launcher), creationflags=CF)
        ok = up(8790, 40)
        print("   8790 起来了：%s" % ("✅" if ok else "❌"))
        if ok:
            h = json.loads(urllib.request.urlopen("http://127.0.0.1:8790/healthz", timeout=5).read().decode())
            print("   healthz: core_dir=%s pid=%s" % (h.get("core_dir"), h.get("pid")))
            tail = open(log, encoding="utf-8", errors="replace").read() if os.path.isfile(log) else ""
            print("   日志里有【已在默认浏览器打开枢阁】：%s" % ("✅" if "已在默认浏览器打开枢阁" in tail else "❌"))
            st = json.loads(urllib.request.urlopen("http://127.0.0.1:8790/api/host/status", timeout=15).read().decode())
            nd = str((st.get("notes") or {}).get("dir") or "")
            print("   库目录 = %s（健康 %s）" % (nd, (st.get("notes") or {}).get("healthy")))
        else:
            fails.append("B：没跑起来时双击启动器没把启停台拉起来")
    finally:
        print("\n清场：停掉临时那个，把本机原本的宿主拉回来")
        kill_hosts()
        time.sleep(2)
        run(["schtasks", "/Run", "/TN", "core-watchdog"])
        ok = up(8790, 60)
        print("本机宿主已回来：%s" % ("✅" if ok else "❌ 看 D:\\core\\data\\logs\\host.log"))
        shutil.rmtree(TMP, ignore_errors=True)

    if fails:
        print("\n❌ %s" % "；".join(fails))
        return 1
    print("\n✅ 两种情形都对：启动器有反应（已跑就开页面；没跑就把它拉起来并开页面）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
