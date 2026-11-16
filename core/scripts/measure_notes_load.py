#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**库页加载慢在哪**：量四个数 —— ①切过去到树出来多久；②那几笔请求各自多大/多久；
③有没有压缩；④树有多少行（DOM 规模）。

用法：python scripts\measure_notes_load.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

SHELL = r"D:\core\bin\shuge-app.exe"
PORT = 9431


def main():
    proc = subprocess.Popen([SHELL, "--debug-port", str(PORT), "--mutex-suffix", "loadm",
                             "--profile-suffix", "loadm", "--state-suffix", "loadm"])
    try:
        page = None
        for _ in range(60):
            time.sleep(1)
            try:
                lst = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT, timeout=2).read().decode())
                ps = [x for x in lst if x.get("type") == "page" and "/console" in x.get("url", "")]
                if ps:
                    page = ps[0]
                    break
            except Exception:
                pass
        if not page:
            print("CDP 拿不到页面")
            return 1
        import websocket
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=30)
        n = [0]

        def js(expr, wait=40):
            n[0] += 1
            ws.send(json.dumps({"id": n[0], "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True, "awaitPromise": True}}))
            ws.settimeout(wait)
            while True:
                g = json.loads(ws.recv())
                if g.get("id") == n[0]:
                    r = g.get("result", {})
                    if "exceptionDetails" in r:
                        return "ERR " + str(r["exceptionDetails"].get("text"))
                    return r.get("result", {}).get("value")

        time.sleep(7)
        print("① 切到「库」，量到树出现为止")
        js("window.__t0 = performance.now(); showTab('notes'); 'ok'")
        el = None
        for _ in range(60):
            time.sleep(0.25)
            v = js("(function(){var t=document.getElementById('tree');"
                   "return (t && t.children.length>0) ? (performance.now()-window.__t0) : 0})()")
            if isinstance(v, (int, float)) and v > 0:
                el = v
                break
        print("   从切页到树渲染出来：%s ms" % (round(el, 1) if el else "（60×250ms 还没出来）"))

        print("② 那几笔请求（浏览器自己记的）")
        res = js("""(function(){
          return JSON.stringify(performance.getEntriesByType('resource')
            .filter(function(e){ return e.name.indexOf('/notes/') >= 0 || e.name.indexOf('/api/') >= 0; })
            .map(function(e){ return {u: e.name.replace(location.origin,'').split('?')[0],
                                      ms: Math.round(e.duration), kb: Math.round((e.transferSize||0)/1024),
                                      dec: Math.round((e.decodedBodySize||0)/1024)}; })
            .slice(-14));
        })()""")
        try:
            for r in json.loads(res):
                print("   %-26s %6s ms  传输 %5s KB  解压后 %5s KB" % (r["u"][:26], r["ms"], r["kb"], r["dec"]))
        except Exception:
            print("   ", res)

        print("①-2 再切走、切回来（热）")
        js("showTab('console'); 'ok'")
        time.sleep(2.5)
        js("window.__t1 = performance.now(); showTab('notes'); 'ok'")
        el2 = None
        for _ in range(60):
            time.sleep(0.25)
            v = js("(function(){var t=document.getElementById('tree');"
                   "return (t && t.children.length>0) ? (performance.now()-window.__t1) : 0})()")
            if isinstance(v, (int, float)) and v > 0:
                el2 = v
                break
        print("   第二次切换（热）：%s ms" % (round(el2, 1) if el2 else "（没量到）"))

        print("③ 树/编辑器规模")
        print("   树里的行数        :", js("document.querySelectorAll('#tree *').length"))
        print("   面板子节点        :", js("(function(){var h=document.getElementById('panelNotes');return h?h.children.length:-1})()"))
        print("   面板里 style 段数 :", js("document.querySelectorAll('#panelNotes style').length"))
        print("   面板里 script 段数:", js("document.querySelectorAll('#panelNotes script').length"))

        print("④ 直接问接口（对照：网络那头花了多久、多大）")
        base = js("location.origin")
        for path in ("/notes/", "/notes/api/tree", "/notes/api/health"):
            t0 = time.time()
            try:
                req = urllib.request.Request(base + path, headers={"Accept-Encoding": "gzip, deflate"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read()
                    enc = r.headers.get("Content-Encoding") or "（没有压缩）"
                    print("   %-20s %6.0f ms  %7.1f KB  Content-Encoding=%s" %
                          (path, (time.time() - t0) * 1000, len(body) / 1024, enc))
            except Exception as e:
                print("   %-20s 失败 %s" % (path, e))
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
