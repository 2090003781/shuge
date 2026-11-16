#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""量并截「编辑栏 = 页签 + 查找 + 按钮 + 最下沿把手」这一版（辞玖 2026-10-09 22:11 三条）。

验四件事：
  ① 查找行属于编辑栏（在 #ribbon 里面，且在按钮区上面）；
  ② 把手是编辑栏的**最后一个**元素（最边缘）；
  ③ 收起后把手**还在**（能点它展开），上面有字；
  ④ 正文高度：展开 / 收起各多少。

用法：python tools/shot_ribbon_grip.py
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BR = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 49595
OUT = r"D:\core-notes\data\tmp"


def main():
    import websocket
    os.makedirs(OUT, exist_ok=True)
    profa = tempfile.mkdtemp(prefix="shuge-grip3-")
    proc = subprocess.Popen([BR, "--headless=new", "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + profa, "--no-first-run", "--disable-gpu",
                             "--window-size=1500,900", "--remote-allow-origins=*", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    nid = [0]
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        req = urllib.request.Request("http://127.0.0.1:%d/json/new?http://127.0.0.1:8848/" % PORT, method="PUT")
        t = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30)

        def send(m, p=None):
            nid[0] += 1
            ws.send(json.dumps({"id": nid[0], "method": m, "params": p or {}}))
            while True:
                g = json.loads(ws.recv())
                if g.get("id") == nid[0]:
                    return g

        def ev(expr):
            r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
            res = r.get("result", {}).get("result", {})
            if "value" in res:
                return res["value"]
            return {"__err": str(r.get("result", {}).get("exceptionDetails"))[:160]}

        send("Page.enable")
        send("Runtime.enable")
        time.sleep(6)
        ev("(async function(){try{var d=await (await fetch('/api/tree')).json();"
           "function pick(n){if(n&&n.files&&n.files.length)return n.files[0].id;"
           "for(var i=0;i<((n&&n.children)||[]).length;i++){var r=pick(n.children[i]);if(r)return r}return null}"
           "var id=pick(d.tree||d);if(id&&window.openFile){await openFile(id,null);}return id}catch(e){return 'ERR'}})()")
        time.sleep(4)

        def shot(name, clip):
            r = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False, "clip": clip})
            p = os.path.join(OUT, name)
            open(p, "wb").write(base64.b64decode(r["result"]["data"]))
            print("截图：%s" % p)

        def state(tag):
            d = json.loads(ev("""(function(){
              var rb=document.getElementById('ribbon'), rb2=rb.getBoundingClientRect();
              var kids=[].slice.call(rb.children).map(function(e){return e.id||e.className||e.tagName});
              var fb=document.getElementById('findbar').getBoundingClientRect();
              var bd=document.getElementById('ribbonBody').getBoundingClientRect();
              var g=document.getElementById('ribbonBtn').getBoundingClientRect();
              var ed=document.getElementById('editor').getBoundingClientRect();
              return JSON.stringify({kids:kids, ribbonH:Math.round(rb2.height),
                findTop:Math.round(fb.top), findShown:getComputedStyle(document.getElementById('findbar')).display,
                bodyTop:Math.round(bd.top), gripTop:Math.round(g.top), gripH:Math.round(g.height),
                gripText:document.getElementById('ribbonBtn').textContent,
                editorH:Math.round(ed.height), editorTop:Math.round(ed.top)});
            })()"""))
            print("\n[%s] 编辑栏子元素顺序：%s" % (tag, d["kids"]))
            print("      编辑栏高 %d | 查找行 display=%s (top %d) | 按钮区 top %d | 把手 top %d 高 %d"
                  % (d["ribbonH"], d["findShown"], d["findTop"], d["bodyTop"], d["gripTop"], d["gripH"]))
            print("      正文 %d px (顶部 %d) | 把手文字=%r" % (d["editorH"], d["editorTop"], d["gripText"]))
            return d

        ev("findToggle()")
        time.sleep(1)
        d1 = state("展开 + 查找行打开")
        print("      ① 查找行在编辑栏里：%s" % ("是" if "findbar" in d1["kids"] else "否"))
        print("      ② 把手是最后一个：%s" % ("是" if d1["kids"][-1] == "ribbonBtn" else "否"))
        print("      ② 查找行在按钮区上面：%s" % ("是" if d1["findTop"] <= d1["bodyTop"] else "否"))
        shot("ribbon-grip-open.png", {"x": 260, "y": 60, "width": 1200, "height": 340, "scale": 1})

        ev("ribbonToggle(false)")
        time.sleep(1)
        d2 = state("收起（查找行应一起收）")
        print("      ③ 收起后把手还在：%s (高 %d, 文字 %r)"
              % ("是" if d2["gripH"] > 0 else "否", d2["gripH"], d2["gripText"]))
        print("      ③ 收起后查找行也藏起来：%s" % ("是" if d2["findShown"] == "none" else "否"))
        shot("ribbon-grip-closed.png", {"x": 260, "y": 60, "width": 1200, "height": 340, "scale": 1})

        ev("ribbonToggle(true)")
        time.sleep(1)
        d3 = state("再展开（查找行应回来）")
        print("      查找行回来了：%s" % ("是" if d3["findShown"] != "none" else "否"))
        ev("findToggle()")
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        time.sleep(1)
        shutil.rmtree(profa, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
