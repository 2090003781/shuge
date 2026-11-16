#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""给"窗口版"拍两张能看的图（自己先看：像不像现代软件）。

  ① shell-console.png —— 控制台（左边那一列第一个页签亮着）
  ② shell-notes.png   —— 点一下第二个页签（库），看页面是不是整块切过去

做法：把窗口挪成屏幕装得下的大小（否则 PrintWindow 会被裁），截完把尺寸还回去。
用法：python scripts\shot_rail_tabs.py [--out D:\core\data\tmp]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot_window import capture, find_shell_window, restore  # noqa: E402

SHELL = r"D:\core\desktop\Shuge.Desktop\bin\Release\net10.0-windows\win-x64\shuge-app.exe"
user32 = ctypes.WinDLL("user32", use_last_error=True)
NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def client_shot(hwnd, out):
    c = wt.RECT()
    user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(c))
    p = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(p))
    return capture(hwnd, p.x, p.y, c.right - c.left, c.bottom - c.top, out)


def click_nav(hwnd, index):
    """点那一列上的第 index 个页签（0 起）。

    走 SendMessage 直接给那个按钮发 WM_LBUTTONDOWN/UP：
      · 不依赖"窗口必须在前台"（拿真鼠标点的时候，前台不是它就会点到别处，
        看着像"页签点了没反应"）；
      · 走的还是按钮自己的 WndProc —— 跟人点那一下是同一段代码。
    """
    org = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(org))
    kids = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        r = wt.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        rx, ry = r.left - org.x, r.top - org.y
        ww, hh = r.right - r.left, r.bottom - r.top
        if 0 <= rx <= 12 and 20 <= ww <= 60 and 20 <= hh <= 60:      # 那一列上的按钮
            kids.append((int(h), ry))
        return True

    user32.EnumChildWindows(wt.HWND(hwnd), cb, 0)
    kids.sort(key=lambda k: k[1])
    if len(kids) <= index:
        print("   ⚠️ 那一列上只有 %d 个按钮，点不到第 %d 个" % (len(kids), index + 1))
        return False
    btn = kids[index][0]
    user32.SendMessageW(wt.HWND(btn), 0x0201, 1, 0)      # WM_LBUTTONDOWN
    time.sleep(0.15)
    user32.SendMessageW(wt.HWND(btn), 0x0202, 0, 0)      # WM_LBUTTONUP
    time.sleep(3.5)
    return True


def click_client(hwnd, x, y):
    """真点一下窗口里的某个坐标。

    ⚠️ 得先确认**前台窗口就是它**，否则鼠标点下去落在别的窗口上（自检脚本第一版：
    点了没反应，看着像"页签坏了"，其实是窗口没在前台）。
    """
    user32.ShowWindow(wt.HWND(hwnd), 9)
    user32.SetForegroundWindow(wt.HWND(hwnd))
    time.sleep(0.6)
    fg = user32.GetForegroundWindow()
    if fg != hwnd:
        user32.BringWindowToTop(wt.HWND(hwnd))
        user32.SetForegroundWindow(wt.HWND(hwnd))
        time.sleep(0.6)
    p = wt.POINT(x, y)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(p))
    user32.SetCursorPos(p.x, p.y)
    time.sleep(0.5)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.12)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(3.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"D:\core\data\tmp")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    hwnd = find_shell_window()
    if not hwnd:
        print("壳没开 —— 我起一个")
        subprocess.Popen([SHELL], creationflags=NW)
        for _ in range(60):
            time.sleep(0.5)
            hwnd = find_shell_window()
            if hwnd:
                break
    if not hwnd:
        print("起不来")
        return 1
    restore(hwnd)
    time.sleep(6)

    # 整窗挪成屏幕装得下的尺寸：PrintWindow 只能截到桌面上有的那部分
    scr = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(scr))
    old = (scr.left, scr.top, scr.right - scr.left, scr.bottom - scr.top)
    scr_w = user32.GetSystemMetrics(0)
    scr_h = user32.GetSystemMetrics(1)
    w = min(old[2], scr_w - 24)
    h = min(old[3], scr_h - 60)
    user32.MoveWindow(wt.HWND(hwnd), 12, 12, w, h, True)
    time.sleep(2.5)

    w1, s1 = client_shot(hwnd, os.path.join(a.out, "shell-console.png"))
    print("① 控制台：%s  %dx%d" % (w1, s1[0], s1[1]))

    # 第二个页签（库）——那一列的第 2 格
    click_nav(hwnd, 1)
    w2, s2 = client_shot(hwnd, os.path.join(a.out, "shell-notes.png"))
    print("② 点了第 2 格（库）：%s  %dx%d" % (w2, s2[0], s2[1]))

    # 把光标放到安全角落（别停在按钮上，免得下次截到 hover）
    user32.SetCursorPos(4, 4)

    # 还回原来的大小/位置（别把主人的窗口改了）
    user32.MoveWindow(wt.HWND(hwnd), old[0], old[1], old[2], old[3], True)
    print("窗口尺寸已还回 %dx%d" % (old[2], old[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
