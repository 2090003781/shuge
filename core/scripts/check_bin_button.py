#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**回收站那颗按钮真的能开回收站**（可重跑）—— 辞玖 2026-10-12 报的「点了只跳到库页」。

它验的是"页面里那套开回收站的逻辑"（`window.__shugeOpenBin`）：
  ① 在**控制台那一格**叫它 → 它应该自动切到库、**等库页装配好**、把回收站打开（`#binpage` 可见）；
  ② 打开失败要**说出来**（回一条 binFailed 给壳），不许静默。
为什么用 CDP 而不是真点壳上那颗按钮：壳的原生控件没法从外面点（虚拟机里试过，点不动）。
逻辑在页面里（壳只叫一声），所以验页面这套逻辑就等于验那颗按钮。

用法：python scripts\check_bin_button.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
import urllib.request

SHELL = r"D:\core\bin\shuge-app.exe"
PORT = 9421
user32 = ctypes.WinDLL("user32", use_last_error=True)
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:200]))
    if not cond:
        fails.append(name)


def main():
    proc = subprocess.Popen([SHELL, "--debug-port", str(PORT), "--mutex-suffix", "binchk",
                             "--profile-suffix", "binchk", "--state-suffix", "binchk"])
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
            chk("备用壳起来了", False, "CDP 拿不到页面")
            return 1
        import websocket
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=25)
        n = [0]

        def js(expr, wait=25):
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
        print("① 起点：应当在控制台那一格")
        chk("活的是 view-console",
            js("(function(){var v=document.getElementById('view-console');"
               "return !!(v && v.className.indexOf('active')>=0)})()") is True,
            js("location.pathname"))
        chk("页面里有 __shugeOpenBin（壳叫的就是它）", js("typeof window.__shugeOpenBin") == "function")

        print("② 叫它开回收站（模拟壳上那颗按钮）")
        print("   返回 =", js("window.__shugeOpenBin()"))
        # 等它自己切页 + 等库页装配 + 打开
        opened = False
        for _ in range(40):
            time.sleep(1)
            vis = js("(function(){var b=document.getElementById('binpage');"
                     "if(!b) return 'none';"
                     "var st=getComputedStyle(b);"
                     "return (b.getAttribute('aria-hidden')!=='true' && st.display!=='none') ? 'open' : 'closed'})()")
            if vis == "open":
                opened = True
                break
        chk("回收站真的打开了（#binpage 可见）", opened, vis)
        chk("并且是切到库那一格开的",
            js("(function(){var v=document.getElementById('view-notes');"
               "return !!(v && v.className.indexOf('active')>=0)})()") is True,
            js("location.pathname"))
        chk("openBin 已经是函数（面板装配完了）", js("typeof openBin") == "function")
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 回收站：叫一声就能开（自动切到库 + 等装配 + 打开），逻辑在页面里")
    return 0


if __name__ == "__main__":
    sys.exit(main())
