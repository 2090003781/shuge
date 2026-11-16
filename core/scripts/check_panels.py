#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**面板装配自检**（可重跑）：库页/内核页不再是 iframe，改成"取 HTML 装配进面板"（辞玖 2026-10-10 定 B）。

用真浏览器（headless Edge + CDP）验五件事：
  ① 主程序页里**没有 iframe**（`document.querySelectorAll('iframe').length === 0`）；
  ② 切「库」页签后，`#panelNotes` 里装配好了：有库界面的实质元素（`#tree` / `#editor` / 搜索框…），
     并且 `data-loaded="1"`；
  ③ 装配进来的库界面**真的能取数**：在面板里调一次库的接口（`/api/health`）拿到文件数；
  ④ 切「内核」页签同样装好（`#panelCore`）；
  ⑤ 路由走 history：切页签后 `location.pathname` 变成 `/console/notes`，而 `location.hash` 不再是 `#notes`。

用法：python scripts\check_panels.py
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
PORT = 49597
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:220]))
    if not cond:
        fails.append(name)


def assets_port():
    f = os.path.join(CORE, "data", "ui-assets.port")
    if not os.path.isfile(f):
        return 0
    try:
        return int(open(f).read().strip())
    except Exception:
        return 0


def main():
    p = assets_port()
    print("界面装配端口：%s" % p)
    if not p:
        print("❌ 没有 data/ui-assets.port —— 主进程没起或版本旧")
        return 1

    import websocket
    prof = tempfile.mkdtemp(prefix="shuge-panels-")
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
        url = "http://127.0.0.1:%d/console" % p
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
            if "value" in res:
                return res["value"]
            return {"__err": str(r.get("result", {}).get("exceptionDetails"))[:200]}

        send("Page.enable")
        send("Runtime.enable")
        time.sleep(6)
        print("① 主程序页里还有没有 iframe")
        n_if = ev("document.querySelectorAll('iframe').length")
        chk("没有 iframe", n_if == 0, n_if)
        chk("有面板容器 #panelNotes", ev("!!document.getElementById('panelNotes')"))
        chk("装载器已引入（window.loadPanel）", ev("typeof window.loadPanel"))

        print("② 切到「库」：面板要装配好")
        ev("showTab('notes')")
        ok = False
        for _ in range(40):
            time.sleep(0.5)
            if ev("(function(){var e=document.getElementById('panelNotes');"
                  "return e && e.getAttribute('data-loaded')==='1'})()") is True:
                ok = True
                break
        chk("panelNotes 装配完成（data-loaded=1）", ok, ev("(function(){var e=document.getElementById('panelNotes');"
                                                     "return e?e.outerHTML.slice(0,160):'没有'})()"))
        probe = ev("""(function(){
          var h=document.getElementById('panelNotes');
          var q=function(s){return !!h.querySelector(s)};
          return JSON.stringify({tree:q('#tree'), editor:q('#editor'), search:q('#searchBox,input'),
            styles:h.querySelectorAll('style').length, scripts:h.querySelectorAll('script').length,
            kids:h.children.length, text:(h.innerText||'').slice(0,60)});
        })()""")
        print("     面板里的东西：%s" % probe)
        d = json.loads(probe) if isinstance(probe, str) else {}
        chk("库界面的骨架装进来了（#tree 或 #editor 在）", d.get("tree") or d.get("editor"), d)
        chk("样式搬进来了（≥1 个 <style>）", (d.get("styles") or 0) >= 1, d.get("styles"))
        chk("脚本重建了（≥1 个 <script>）", (d.get("scripts") or 0) >= 1, d.get("scripts"))

        # 光「节点搬进来了」不算数：脚本可能**整段抛错没执行**（全局重名就是这种）。
        # 这里直接点它几个函数的名字——库页自己声明的（openBin / loadTree / api）。
        glob = ev("(function(){ return JSON.stringify({openBin: typeof openBin, loadTree: typeof loadTree, api: typeof api, renderDir: typeof renderDir}); })()")
        print("     库页那几个函数：%s" % glob)
        g = json.loads(glob) if isinstance(glob, str) else {}
        chk("库页的脚本真的跑起来了（openBin/loadTree/api 都在）",
            g.get("openBin") == "function" and g.get("loadTree") == "function" and g.get("api") == "function", g)
        chk("页面全局没被装配进来的页撞坏（showTab 仍是函数）", ev("typeof showTab") == "function")

        print("③ 装配进来的库界面真能取数")
        health = ev("""(async function(){
          try { var r = await fetch('/api/health'); var d = await r.json();
                return JSON.stringify({ok: d.ok, files: (d.stats||{}).files}); }
          catch (e) { return 'ERR ' + e; }
        })()""")
        print("     /api/health → %s" % health)
        try:
            hh = json.loads(health)
            chk("库接口在面板里可用（拿到文件数）", hh.get("ok") and (hh.get("files") or 0) > 0, health)
        except Exception:
            chk("库接口在面板里可用（拿到文件数）", False, health)

        print("④ 切到「内核」")
        ev("showTab('core')")
        ok2 = False
        for _ in range(40):
            time.sleep(0.5)
            if ev("(function(){var e=document.getElementById('panelCore');"
                  "return e && e.getAttribute('data-loaded')==='1'})()") is True:
                ok2 = True
                break
        chk("panelCore 装配完成", ok2, ev("(function(){var e=document.getElementById('panelCore');"
                                     "return e?e.getAttribute('data-error')||e.outerHTML.slice(0,120):'没有'})()"))

        print("⑤ 路由：history 而不是 #hash")
        loc = ev("JSON.stringify({path:location.pathname, hash:location.hash})")
        print("     %s" % loc)
        try:
            L = json.loads(loc)
            chk("路径里有页签（/console/core）", "/core" in (L.get("path") or ""), L)
            chk("hash 不再被用（不是 #core）", (L.get("hash") or "") != "#core", L)
        except Exception:
            chk("路由检查", False, loc)
        ev("showTab('console')")

        ws.close()
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
    print("✅ 面板装配：没有 iframe；库/内核页取回来装配成功、接口可用；路由走 history")
    return 0


if __name__ == "__main__":
    sys.exit(main())
