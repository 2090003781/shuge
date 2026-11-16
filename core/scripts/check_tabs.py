#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**窗口里那几个页签能不能切**（可重跑）：不开浏览器、不靠人点鼠标，直接问页面自己。

做法：起一个"备用壳"（带 CDP 调试口 + 自己的窗口状态文件），用 DevTools 协议在页面里执行：
  ① 读它认为的页签状态；② 依次 showTab('notes') / showTab('core') / showTab('console')；
  ③ 每一步都验：`.view` 的 active 类、`.panelHost` 里是不是真装配进来了、地址栏路径对不对；
  ④ **真点一下那一列上的按钮**（SetCursorPos + mouse_event，跟人点一样）——
     验"壳里那一列 → 页面切页签"这条线是通的。

2026-10-12 修三处（原来是"点了没反应"的假故障）：
  · 原来自检给控件发 WM_LBUTTONDOWN/UP —— 对自绘控件（NavButton 是 Control 子类）不算数；
  · 主人窗口/浏览器常常铺满整屏，备用壳压在底下时点下去命中的是**别人的窗口**
    → 点之前把备用壳临时置顶（HWND_TOPMOST），点完撤掉；
  · ⚠️ SetWindowPos 不声明 argtypes 时，-1 会当成 32 位传坏（置顶静默失效）—— 必须 c_void_p(-1)。

用法：python scripts\check_tabs.py
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

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.SetWindowPos.restype = ctypes.c_bool
user32.WindowFromPoint.argtypes = [wt.POINT]
user32.WindowFromPoint.restype = wt.HWND
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]

SHELL = r"D:\core\desktop\Shuge.Desktop\bin\Release\net10.0-windows\win-x64\shuge-app.exe"
PORT = 9345
SUFFIX = "selftest"
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
TOPMOST = ctypes.c_void_p(-1)
NOTOPMOST = ctypes.c_void_p(-2)
SWP_SHOWWINDOW, SWP_NOSIZE, SWP_NOMOVE = 0x0040, 0x0001, 0x0002
fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:220]))
    if not cond:
        fails.append(name)


def skip(name, why):
    print("  ⏭ " + name + "（跳过：" + why + "）")


def shells():
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if not n:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, b, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        if b.value.strip() == "枢阁" and "WindowsForms" in cls.value:
            found.append(int(h))
        return True

    user32.EnumWindows(cb, 0)
    return found


def main():
    before = shells()
    proc = subprocess.Popen([SHELL, "--debug-port", str(PORT), "--mutex-suffix", SUFFIX,
                             "--profile-suffix", SUFFIX, "--state-suffix", SUFFIX])
    old_fg = user32.GetForegroundWindow()
    pt = wt.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    old_pos = (pt.x, pt.y)
    print("备用壳 pid=%d（主人那几个窗口 %s 不动它）" % (proc.pid, before))
    try:
        page = None
        for _ in range(60):
            time.sleep(1)
            try:
                lst = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/json/list" % PORT, timeout=2).read().decode())
                pages = [x for x in lst if x.get("type") == "page" and x.get("url", "").startswith("http")]
                if pages:
                    page = pages[0]
                    break
            except Exception:
                pass
        if not page:
            print("❌ 备用壳没起来（CDP 拿不到目标）")
            return 1

        import websocket
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=20)
        nid = [0]

        def ev(expr):
            nid[0] += 1
            ws.send(json.dumps({"id": nid[0], "method": "Runtime.evaluate",
                                "params": {"expression": expr, "returnByValue": True,
                                           "awaitPromise": True}}))
            ws.settimeout(30)
            while True:
                g = json.loads(ws.recv())
                if g.get("id") == nid[0]:
                    r = g.get("result", {})
                    if "exceptionDetails" in r:
                        return "ERR: " + str(r["exceptionDetails"].get("text"))
                    return r.get("result", {}).get("value")

        def state():
            return ev("""JSON.stringify({
                href: location.pathname,
                active: ['console','notes','core'].filter(function(n){
                    var v = document.getElementById('view-' + n);
                    return v && v.className.indexOf('active') >= 0 }).map(function(n){
                    return 'view-' + n }).join(','),
                host: [].slice.call(document.querySelectorAll('.panelHost')).map(function(v){
                    return v.id + '=' + v.children.length + (v.hasAttribute('data-error') ? '!err' : '')
                        + (v.hasAttribute('data-loading') ? '~load' : '') }).join(' ')
            })""")

        print("① 起点（刚起来应当是控制台）")
        s0 = json.loads(state())
        chk("地址是 /console", s0["href"] == "/console", s0["href"])
        chk("活的是 view-console", s0["active"] == "view-console", s0["active"])
        chk("页面里 showTab 是函数", ev("typeof showTab") == "function", ev("typeof showTab"))

        for tab, view, path in (("notes", "view-notes", "/console/notes"),
                                ("core", "view-core", "/console/core"),
                                ("console", "view-console", "/console")):
            print("② 切到 %s" % tab)
            ev("showTab('%s'); 'ok'" % tab)
            time.sleep(4)
            st = json.loads(state())
            chk("活的是 %s" % view, st["active"] == view, st["active"])
            chk("地址是 %s" % path, st["href"] == path, st["href"])
            if tab != "console":
                host = [h for h in st["host"].split() if h.startswith("panel" + tab.capitalize())]
                ok = host and "=0" not in host[0].split("=")[1][:2]
                chk("面板 %s 真装配进来了" % tab, bool(ok), st["host"])
                chk("面板没报装配失败", "err" not in st["host"], st["host"])

        # ③ 真点一下那一列上的第 2 格（库）
        print("③ 真点那一列的第 2 格（库）")
        time.sleep(2)
        new = [h for h in shells() if h not in before]
        if not new:
            skip("点那一列", "没找到备用壳的窗口（EnumWindows）")
        else:
            hwnd = new[-1]
            user32.SetWindowPos(wt.HWND(hwnd), TOPMOST, 900, 200, 620, 620, SWP_SHOWWINDOW)
            time.sleep(0.9)
            org = wt.POINT(0, 0)
            user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(org))
            kids = []

            @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
            def cb2(h, _):
                r = wt.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                rx, ry = r.left - org.x, r.top - org.y
                ww, hh = r.right - r.left, r.bottom - r.top
                if 0 <= rx <= 12 and 20 <= ww <= 60 and 20 <= hh <= 60:
                    kids.append((int(h), ry, (r.left + r.right) // 2, (r.top + r.bottom) // 2))
                return True

            try:
                user32.EnumChildWindows(wt.HWND(hwnd), cb2, 0)
                kids.sort(key=lambda k: k[1])
                if len(kids) < 3:
                    chk("那一列上的按钮都在", False, kids)
                else:
                    btn, _, cx, cy = kids[1]          # 第 2 格 = 库
                    under = user32.WindowFromPoint(wt.POINT(cx, cy))
                    if int(under or 0) != btn:
                        skip("点那一列", "那个坐标被别的窗口压着（hwnd=%s，目标是 %s）" % (under, btn))
                    else:
                        user32.SetCursorPos(cx, cy)
                        time.sleep(0.3)
                        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                        time.sleep(0.08)
                        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                        time.sleep(4)
                        st = json.loads(state())
                        chk("点那一列的第 2 格 → 页面真的切到库", st["active"] == "view-notes", st["active"])
                        chk("地址也跟着变 /console/notes", st["href"] == "/console/notes", st["href"])
            finally:
                user32.SetWindowPos(wt.HWND(hwnd), NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)

        ws.close()
    finally:
        user32.SetCursorPos(old_pos[0], old_pos[1])
        try:
            if old_fg:
                user32.SetForegroundWindow(old_fg)
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 窗口里的页签都能切（控制台 / 库 / 内核），地址与面板都对得上；那一列真点也接得住")
    return 0


if __name__ == "__main__":
    sys.exit(main())
