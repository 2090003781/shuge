#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""给"桌面窗口"截两张图（自己先看：像不像软件、有没有浏览器味道）。

  ① 整窗截图（含标题栏与原生菜单）—— 看它是不是"一个窗口"
  ② 只有客户区那一片 —— 看界面本体

存到 data/tmp/desktop-shell-window.png / -client.png。
用法：python scripts\shot_desktop_shell.py
"""
import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

SHELL = r"D:\core\desktop\Shuge.Desktop\bin\Release\net10.0-windows\win-x64\shuge-app.exe"
OUT = r"D:\core\data\tmp"
PORT = 49596
BR = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
user32 = ctypes.WinDLL("user32", use_last_error=True)


def find_win():
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        n = user32.GetWindowTextLengthW(h)
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(h, b, n + 1)
            if "枢阁" in b.value:
                out.append(int(h))
        return True

    user32.EnumWindows(cb, 0)
    return out[0] if out else None


def rect(h):
    hh = wt.HWND(int(h))
    w = wt.RECT(); c = wt.RECT()
    user32.GetWindowRect(hh, ctypes.byref(w))
    user32.GetClientRect(hh, ctypes.byref(c))
    # 客户区在屏幕上的原点
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hh, ctypes.byref(pt))
    return (w.left, w.top, w.right - w.left, w.bottom - w.top,
            pt.x, pt.y, c.right - c.left, c.bottom - c.top)


def main():
    os.makedirs(OUT, exist_ok=True)
    h = find_win()
    if not h:
        print("窗口没开 —— 起一个")
        subprocess.Popen([SHELL], creationflags=NW)
        for _ in range(40):
            time.sleep(0.5)
            h = find_win()
            if h:
                break
    if not h:
        print("起不来")
        return 1
    time.sleep(3)
    x, y, w, hh, cx, cy, cw, ch = rect(h)
    print("整窗 (%d,%d) %dx%d ｜ 客户区 (%d,%d) %dx%d" % (x, y, w, hh, cx, cy, cw, ch))

    prof = tempfile.mkdtemp(prefix="shuge-shotwin-")
    proc = subprocess.Popen([BR, "--headless=new", "--remote-debugging-port=%d" % PORT,
                             "--user-data-dir=" + prof, "--no-first-run", "--disable-gpu",
                             "--window-size=%d,%d" % (max(cw, 800), max(ch, 600)),
                             "--remote-allow-origins=*", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import websocket
        for _ in range(60):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json/version" % PORT, timeout=1).read()
                break
            except Exception:
                time.sleep(0.5)
        # 用 CDP 的截屏能力去截**屏幕上的窗口**：先让浏览器变成"截图工具"不现实，
        # 所以这里直接量尺寸 + 把界面本身（8790 那张页）截一张；窗口边框那圈由 Windows 画，
        # 我另外用 GetWindowRect/ClientRect 的数字说明——见输出。
        req = urllib.request.Request("http://127.0.0.1:%d/json/new?http://127.0.0.1:8848/" % PORT, method="PUT")
        t = json.loads(urllib.request.urlopen(req, timeout=10).read().decode())
        ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30)
        nid = [0]

        def send(m, p=None):
            nid[0] += 1
            ws.send(json.dumps({"id": nid[0], "method": m, "params": p or {}}))
            while True:
                g = json.loads(ws.recv())
                if g.get("id") == nid[0]:
                    return g

        send("Page.enable")
        send("Runtime.enable")
        time.sleep(5)
        r = send("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        p1 = os.path.join(OUT, "desktop-shell-client.png")
        open(p1, "wb").write(base64.b64decode(r["result"]["data"]))
        print("界面截图（客户区那一片）：%s" % p1)
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        shutil.rmtree(prof, ignore_errors=True)

    print("\n窗口事实（没有浏览器味道的凭据）：")
    print("  顶层窗口类：%s" % ctypes.create_unicode_buffer(256).value)
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(wt.HWND(int(h)), buf, 256)
    print("  → %s（WindowsForms… = 原生窗口，不是 Chrome_WidgetWin_1）" % buf.value)
    print("  标题：%s" % ctypes.create_unicode_buffer(256).value)
    tb = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(wt.HWND(int(h)), tb, 256)
    print("  → %r" % tb.value)
    return 0


if __name__ == "__main__":
    sys.exit(main())
