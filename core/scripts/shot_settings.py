#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""给"设置"那一页拍一张（连着标题栏与左边那一列一起）—— 自己先看，再给人看。

做法：给那一列上的第 4 格（设置）发一下按钮消息（不靠真鼠标、不抢前台），
等装配完再截客户区。

用法：python scripts\shot_settings.py [--out D:\core\data\tmp\shell-settings.png]
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot_window import capture, find_shell_window, quit_dialogs, restore  # noqa: E402

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
    org = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(org))
    kids = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        r = wt.RECT()
        user32.GetWindowRect(h, ctypes.byref(r))
        rx = r.left - org.x
        ww, hh = r.right - r.left, r.bottom - r.top
        if -4 <= rx <= 16 and 20 <= ww <= 90 and 20 <= hh <= 90:      # 那一列上的格子
            kids.append((int(h), r.top - org.y))
        return True

    user32.EnumChildWindows(wt.HWND(hwnd), cb, 0)
    kids.sort(key=lambda k: k[1])
    if len(kids) <= index:
        print("   ⚠️ 那一列上只有 %d 个格子" % len(kids))
        return False
    btn = kids[index][0]
    user32.SendMessageW(wt.HWND(btn), 0x0201, 1, 0)      # WM_LBUTTONDOWN
    time.sleep(0.15)
    user32.SendMessageW(wt.HWND(btn), 0x0202, 0, 0)      # WM_LBUTTONUP
    time.sleep(4.5)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=r"D:\core\data\tmp\shell-settings.png")
    a = ap.parse_args()

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
    quit_dialogs()
    time.sleep(4)
    click_nav(hwnd, 3)                     # 第 4 格 = 设置
    out, size = client_shot(hwnd, a.out)
    print("设置页 → %s  %dx%d" % (out, size[0], size[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
