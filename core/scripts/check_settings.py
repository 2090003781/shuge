#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**设置页自检**（可重跑）：验"设置这一页"真的在、真的能用、桥真的通。

辞玖 2026-10-11：「1 做（设置页）」。

① 网页侧（不依赖窗口）：`/settings/` 200、是它那一页；`/favicon.ico` 是一条 SVG（不再 404）；
   切到设置那一格能装配进来（CDP 问页面：面板有孩子、主题按钮在、事实行读到了东西）。
② 窗口侧（真开一个备用壳）：壳那道桥在不在（`chrome.webview.hostObjects.sync.shuge`）；
   真叫它一下（放大 → 幂等可量、打开 data 目录 → 不报错），再验"页面上那颗按钮点下去也走同一条路"。

用法：python scripts\check_settings.py
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot_window import find_shell_window  # noqa: E402

# ⚠️ 用**发布版单文件**（bin\shuge-app.exe）：desktop\…\win-x64\shuge-app.exe 是 apphost，
#    它把"平台目录"算成 …\win-x64，于是壳写的 data/shell-state.json 与设置页读的不是同一份
#    （2026-10-11 踩：自检一直显示"读不到"）。bin 那份的上一层才是平台根 D:\core。
SHELL = r"D:\core\bin\shuge-app.exe"
HOST = "http://127.0.0.1:8790"
PORT = 9371
user32 = ctypes.WinDLL("user32", use_last_error=True)
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:220]))
    if not cond:
        fails.append(name)


def get(url):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-store"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")


SHELL_PLATFORM = os.path.dirname(os.path.dirname(SHELL))   # <平台>\bin\shuge-app.exe → <平台>


def bridge_state():
    """壳每次动作都把"桥通不通 + 缩放"写进 <平台>\data\shell-state.json；这就是"真的生效了"的凭据。

    ⚠️ 读的是**备用壳自己的平台目录**（bin 的上一层），不是 D:\core\data ——
    两个壳各有各的平台根（2026-10-11 踩：读错地方，一直显示"读不到"）。
    """
    # 备用壳写的是 shell-state-<后缀>.json（免得读到主人那个窗口的）
    try:
        with open(os.path.join(SHELL_PLATFORM, "data", "shell-state-setchk.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        pass
    try:
        with open(os.path.join(SHELL_PLATFORM, "data", "shell-state.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"bridge": "读不到: %s" % e}


def num(v, default=0.0):
    """COM 那头回来的数可能是字符串（晚绑定），统一转 float。"""
    try:
        return float(v)
    except Exception:
        return default


class Cdp:
    def __init__(self, ws):
        self.ws, self.n = ws, 0

    def call(self, method, params=None, timeout=25):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params or {}}))
        self.ws.settimeout(timeout)
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                r = m.get("result", {})
                if "exceptionDetails" in r:
                    return "ERR " + str(r["exceptionDetails"].get("text"))
                return r.get("result", {}).get("value")

    def js(self, expr, timeout=25):
        return self.call("Runtime.evaluate",
                         {"expression": expr, "returnByValue": True, "awaitPromise": True}, timeout)


def main():
    print("① 网页侧：这一页在不在")
    st, ct, body = get(HOST + "/settings/")
    chk("/settings/ 200", st == 200, st)
    chk("是 HTML 页", "text/html" in ct, ct)
    chk("有主题那一组（浅白·护眼 / 深色）", "浅白·护眼" in body and "深色" in body)
    chk("有窗口那一组（缩放 / 全屏）", "缩放" in body and "全屏" in body)
    st2, ct2, body2 = get(HOST + "/favicon.ico")
    chk("/favicon.ico 200", st2 == 200, st2)
    chk("图标是 SVG", "svg" in ct2 and "<svg" in body2, ct2)

    print("② 窗口侧：起一个带调试口的备用壳，验设置那一格与那道桥")
    before = find_shell_window()
    proc = subprocess.Popen([SHELL, "--debug-port", str(PORT),
                             "--mutex-suffix", "setchk", "--profile-suffix", "setchk"])
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
            chk("备用壳起来了", False, "CDP 拿不到 /console")
            return 1

        import websocket
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=25)
        c = Cdp(ws)
        c.call("Runtime.enable")
        time.sleep(6)

        chk("页面里看得见那道桥（WebView2 消息通道）", c.js(
            "!!(window.chrome && chrome.webview && typeof chrome.webview.postMessage === 'function')") is True)
        chk("壳记的桥状态是 ok（data/shell-state.json）", bridge_state().get("bridge") == "ok",
            bridge_state())

        print("   切到设置那一格…")
        c.js("showTab('settings'); 'ok'")
        time.sleep(6)
        chk("活的是 view-settings", c.js(
            "(function(){var v=document.getElementById('view-settings');"
            "return v && v.className.indexOf('active')>=0 ? 'view-settings' : v.className})()") == "view-settings")
        chk("设置面板装配进来了", (c.js(
            "(function(){var h=document.getElementById('panelSettings');"
            "return h ? h.children.length : -1})()") or 0) > 0)
        chk("主题那两颗按钮在", c.js("document.querySelectorAll('#setTheme button[data-t]').length") == 2)
        chk("窗口那一组显示出来了（有桥才显示）", c.js(
            "(function(){var e=document.getElementById('setShellOnly');"
            "return e && e.style.display !== 'none'})()") is True)
        chk("事实行读到了版本与路径", c.js(
            "(function(){var t=(document.getElementById('setFacts')||{}).innerText||'';"
            "return t.indexOf('版本')>=0 && t.indexOf('平台目录')>=0})()") is True,
            c.js("(document.getElementById('setFacts')||{}).innerText"))
        chk("「关于枢阁」读到了版本 / 路径 / 窗口", c.js(
            "(function(){var t=(document.getElementById('setAbout')||{}).innerText||'';"
            "return t.indexOf('主进程')>=0 && t.indexOf('库目录')>=0 && t.indexOf('窗口版')>=0})()") is True,
            c.js("(document.getElementById('setAbout')||{}).innerText"))

        print("   真叫一下那道桥（放大 → 壳那边的缩放值真的变）")
        c.js("chrome.webview.postMessage(JSON.stringify({shuge:'zoomReset'}))")
        time.sleep(0.8)
        z0 = num(bridge_state().get("zoom"))
        c.js("chrome.webview.postMessage(JSON.stringify({shuge:'zoomIn'}))")
        time.sleep(0.9)
        z1 = num(bridge_state().get("zoom"))
        chk("桥能让窗口真的放大（%.2f → %.2f）" % (z0, z1), z1 > z0, bridge_state())
        c.js("chrome.webview.postMessage(JSON.stringify({shuge:'zoomReset'}))")
        time.sleep(0.5)

        print("   点页面上那颗「放大」按钮（跟人点一样）")
        c.js("""(function(){
            var b = [].slice.call(document.querySelectorAll('#setShellOnly button'))
                     .filter(function(x){return x.getAttribute('data-act')==='zoomIn'})[0];
            if (!b) return 'no button';
            b.click(); return 'clicked';
        })()""")
        time.sleep(0.9)
        z2 = num(bridge_state().get("zoom"))
        chk("页面上的按钮走的是同一条路（%.2f）" % z2, z2 > 1.0, bridge_state())
        c.js("chrome.webview.postMessage(JSON.stringify({shuge:'zoomReset'}))")
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
    print("✅ 设置页：网页侧能开、窗口里能装、桥通（按钮 → 壳的动作真的生效）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
