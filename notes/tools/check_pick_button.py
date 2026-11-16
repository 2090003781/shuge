#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""验收「🖥 从系统选择框选」这条链路（真浏览器 + CDP，不点系统窗口用 dry_run 代替）：

  ① 库界面的导入弹窗里**有这颗按钮**；
  ② `pickFromSystem` 函数在（页面里可调用）；
  ③ 链路两段各自可用：
     · `POST /api/pick`（dry_run）→ 回 token（协议通）；
     · `POST /api/import {src}` → 真按路径导入一篇 → 回执里有 `created_ids` → 删掉它（清场）。

用法：python scripts\check_pick_button.py
"""
import io
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
PORT = 49599
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:200]))
    if not cond:
        fails.append(name)


def api(path, body=None, method="POST"):
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:8848" + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    import websocket
    prof = tempfile.mkdtemp(prefix="shuge-pickbtn-")
    proc = subprocess.Popen([BR, "--headless=new", "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--no-first-run", "--disable-gpu",
                             "--window-size=1500,950", "--remote-allow-origins=*", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    nid = [0]
    tmpf = None
    created = None
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
            return res.get("value", {"__err": str(r.get("result", {}).get("exceptionDetails"))[:200]})

        send("Page.enable")
        send("Runtime.enable")
        time.sleep(6)

        print("① 导入弹窗里有这颗按钮吗")
        print("     打开导入弹窗：%s" % ev("(function(){try{openImport();return 'ok'}catch(e){return 'ERR '+e}})()"))
        time.sleep(1)
        got = ev("(function(){var b=document.querySelector('#importModal button[onclick*=pickFromSystem]');"
                 "return b? b.textContent.trim() : null})()")
        chk("导入弹窗里有「从系统选择框选」按钮", bool(got), got)
        chk("pickFromSystem 是函数", ev("typeof pickFromSystem") == "function", ev("typeof pickFromSystem"))

        print("② 链路 A 段：POST /api/pick（dry_run，不弹窗）")
        tok = api("/api/pick", {"kind": "file", "title": "验收", "dry_run": True, "fake": "D:/core/README.md"}).get("token")
        chk("拿得到 token", bool(tok), tok)
        with urllib.request.urlopen("http://127.0.0.1:8848/api/pick?token=" + str(tok), timeout=20) as r:
            st = json.loads(r.read().decode("utf-8")).get("state")
        chk("轮询回 pending（dry_run 的正常态）", st in ("pending", "ok"), st)

        print("③ 链路 B 段：POST /api/import 按路径导入 → 再删掉")
        tmpf = os.path.join(tempfile.gettempdir(), "shuge-pickbtn-check.md")
        io.open(tmpf, "w", encoding="utf-8").write("# 选择框按钮验收\n\n自检写的，验完删。\n")
        imp = api("/api/import", {"src": tmpf, "under": "", "rag": False})
        ids = imp.get("created_ids") or []
        chk("导入成功且有 created_ids", imp.get("ok") and len(ids) >= 1, json.dumps(imp, ensure_ascii=False)[:160])
        created = ids[0] if ids else None
        print("     导入进来的 id = %s，names = %s" % (created, imp.get("files")))
    finally:
        if created:
            try:
                d = json.loads(urllib.request.urlopen(urllib.request.Request(
                    "http://127.0.0.1:8848/api/files/" + str(created), method="DELETE"), timeout=30)
                    .read().decode("utf-8"))
                print("     清场：删除导入的那篇 → ok=%s" % d.get("ok"))
            except Exception as e:
                print("     清场失败：%s" % e)
        if tmpf and os.path.isfile(tmpf):
            os.remove(tmpf)
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
    print("✅ 系统选择框那条路：按钮在、函数在、两段接口都通（导入→清场干净）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
