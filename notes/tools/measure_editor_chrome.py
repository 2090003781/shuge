#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""量库界面「正文上面那几层」各占多少像素（辞玖 2026-10-09：上层占太多，正文窗口看着好小）。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

BR = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT = 49591
SEL = [(".topbar", "顶栏"), ("#tabBar", "页签栏"), ("#meta", "文件信息行"), ("#ac", "操作行(预览/保存/新建…)"),
       ("#ribbon", "编辑工具条(整块)"), ("#ribbonHead", "  └ 工具条-页签行"), ("#ribbonBody", "  └ 工具条-按钮区"),
       ("#findbar", "查找栏"), ("#editor", "编辑器(正文)"), ("#right", "右区(容器)")]


def main():
    import websocket
    profa = tempfile.mkdtemp(prefix="shuge-measure-")
    proc = subprocess.Popen([BR, "--headless=new", "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + profa, "--no-first-run", "--disable-gpu",
                             "--window-size=1400,900", "--remote-allow-origins=*", "about:blank"],
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
            return {"__err": str(r.get("result", {}).get("exceptionDetails"))[:200]}

        send("Page.enable")
        send("Runtime.enable")
        time.sleep(6)
        print("视口高度：%s px" % ev("innerHeight"))
        # 打开第一篇笔记（让编辑区真的显示出来）
        ev("(async function(){try{var d=await (await fetch('/api/tree')).json();"
           "function pick(n){if(n&&n.files&&n.files.length)return n.files[0].id;"
           "for(var i=0;i<((n&&n.children)||[]).length;i++){var r=pick(n.children[i]);if(r)return r}return null}"
           "var id=pick(d.tree||d);if(id&&window.openFile){await openFile(id,null);}return id}catch(e){return 'ERR '+e}})()")
        time.sleep(5)
        sel = json.dumps([s for s, _ in SEL])
        rows = ev("""(function(sels){
          return sels.map(function(s){
            var e=document.querySelector(s);
            if(!e) return [s,'(没有)','',''];
            var r=e.getBoundingClientRect(), st=getComputedStyle(e);
            return [s, Math.round(r.height), Math.round(r.top), st.display + (st.overflowY==='auto'?' /scroll':'')];
          });
        })(%s)""" % sel)
        print("\n%-16s %-14s %8s %8s  %s" % ("选择器", "是什么", "高度px", "顶部px", "备注"))
        total = None
        for (s, label), row in zip(SEL, rows):
            print("%-16s %-14s %8s %8s  %s" % (row[0], label, row[1], row[2], row[3]))
            if s == "#editor" and isinstance(row[2], int):
                total = row[2]
        if total is not None:
            print("\n正文顶部位置 = %d px；可用视口 %s px；正文只剩 %.0f%%" %
                  (total, ev("innerHeight"), 100.0 * (ev("innerHeight") - total) / ev("innerHeight")))
        print("\n工具条折叠状态：%s" % ev("(function(){var e=document.querySelector('#ribbonBody');"
                                    "return e ? (e.offsetHeight===0 ? '已收起' : '展开中') : 'n/a'})()"))
        # 再量一次：收起工具条之后，正文能多高
        ev("ribbonToggle(false)")
        time.sleep(1)
        h1 = ev("Math.round(document.querySelector('#editor').getBoundingClientRect().height)")
        top1 = ev("Math.round(document.querySelector('#editor').getBoundingClientRect().top)")
        print("收起工具条后：正文高度 %s px（顶部 %s px）—— 比展开时多 %s px" %
              (h1, top1, int(h1) - int([r for (s2, _), r in zip(SEL, rows) if s2 == '#editor'][0][1])))
        ev("ribbonToggle(true)")
        time.sleep(0.5)
        print("记忆的状态键：%s" % ev("JSON.stringify(Object.keys(localStorage).filter(function(k){"
                                 "return /ribbon|find|editor|tab/i.test(k)}))"))
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
