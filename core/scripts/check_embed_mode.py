#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""量并验「壳里没有网页顶栏」这一条（2026-10-10，B 方案第③块第二步）。

对照两种打开方式，量**顶栏高度**与**工作区可用高度**：
  · `/console`            → 有顶栏（网页版/浏览器里用）
  · `/console?embed=1`    → 顶栏藏起来（桌面壳里用；页签改用壳的原生菜单）

用法：python scripts\check_embed_mode.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

CORE = r"D:\core"
BR = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 49598
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:200]))
    if not cond:
        fails.append(name)


def assets_port():
    f = os.path.join(CORE, "data", "ui-assets.port")
    return int(open(f).read().strip()) if os.path.isfile(f) else 0


def main():
    p = assets_port()
    print("界面装配端口：%s" % p)
    if not p:
        print("❌ 没有 data/ui-assets.port")
        return 1
    import websocket
    prof = tempfile.mkdtemp(prefix="shuge-embed-")
    proc = subprocess.Popen([BR, "--headless=new", "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--no-first-run", "--disable-gpu",
                             "--window-size=1500,950", "--remote-allow-origins=*", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    nid = [0]
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)

        def open_page(url):
            req = urllib.request.Request("http://127.0.0.1:%d/json/new?%s" % (PORT, url), method="PUT")
            t = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
            ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30)

            def send(m, par=None):
                nid[0] += 1
                ws.send(json.dumps({"id": nid[0], "method": m, "params": par or {}}))
                while True:
                    g = json.loads(ws.recv())
                    if g.get("id") == nid[0]:
                        return g

            def ev(expr):
                r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                res = r.get("result", {}).get("result", {})
                return res.get("value", {"__err": str(r.get("result", {}).get("exceptionDetails"))[:160]})

            send("Page.enable")
            send("Runtime.enable")
            return ws, ev

        print("① 普通模式 /console")
        ws1, ev1 = open_page("http://127.0.0.1:%d/console" % p)
        time.sleep(5)
        d1 = ev1("""(function(){
          var t=document.querySelector('.tabs');
          var v=document.getElementById('view-console');
          var r=v.getBoundingClientRect();
          return JSON.stringify({tabs: t?Math.round(t.getBoundingClientRect().height):-1,
            embed: document.body.classList.contains('embed'),
            top: Math.round(r.top), avail: Math.round(innerHeight - r.top), vh: innerHeight});
        })()""")
        print("     %s" % d1)
        a = json.loads(d1)
        chk("普通模式有顶栏（高度 > 20px）", (a.get("tabs") or 0) > 20, d1)
        chk("普通模式没加 embed 类", a.get("embed") is False, d1)
        ws1.close()

        print("② 壳模式 /console?embed=1")
        ws2, ev2 = open_page("http://127.0.0.1:%d/console?embed=1" % p)
        time.sleep(5)
        d2 = ev2("""(function(){
          var t=document.querySelector('.tabs');
          var v=document.getElementById('view-console');
          var r=v.getBoundingClientRect();
          return JSON.stringify({tabs: t?Math.round(t.getBoundingClientRect().height):-1,
            tabsDisplay: t?getComputedStyle(t).display:'-',
            embed: document.body.classList.contains('embed'),
            top: Math.round(r.top), avail: Math.round(innerHeight - r.top), vh: innerHeight});
        })()""")
        print("     %s" % d2)
        b = json.loads(d2)
        chk("embed 模式下顶栏藏起来（display:none）", b.get("tabsDisplay") == "none", d2)
        chk("embed 模式加了 embed 类", b.get("embed") is True, d2)
        print("     内容起点：普通 top=%s → 壳 top=%s" % (a.get("top"), b.get("top")))
        print("     可用高度：普通 %s → 壳 %s px" % (a.get("avail"), b.get("avail")))
        gain = (b.get("avail") or 0) - (a.get("avail") or 0)
        chk("藏掉顶栏后可用高度变大（≥ 30px）", gain >= 30, gain)
        chk("壳模式下内容从视口顶部开始（top ≤ 8）", b.get("top") is not None and b.get("top") <= 8, b.get("top"))

        print("③ embed 模式下页签仍可切（菜单调 showTab 的路子）")
        ev2("showTab('notes')")
        ok = False
        for _ in range(40):
            time.sleep(0.5)
            if ev2("(function(){var e=document.getElementById('panelNotes');"
                   "return e && e.getAttribute('data-loaded')==='1'})()") is True:
                ok = True
                break
        chk("壳里切「库」照样装配（showTab 仍可用）", ok, "")
        ws2.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        time.sleep(1)
        shutil.rmtree(prof, ignore_errors=True)

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 壳模式：网页顶栏已藏、工作区变高、页签改走壳的原生菜单（普通模式不受影响）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
