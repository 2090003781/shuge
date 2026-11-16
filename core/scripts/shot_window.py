#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**真窗口截图**：把枢阁那个桌面窗口（连标题栏一起）截下来，存成 PNG。

为什么不用浏览器截图：以前那支 shot_desktop_shell.py 是拿无头 Edge 去打开 8848 截**网页**，
出来的当然是网页的样子，看不到"窗口"本身。要看这列页签长什么样，必须用 Windows 自己的
PrintWindow 去截**屏幕上那个窗口**（WebView2 那片也能截到，PW_RENDERFULLCONTENT）。

用法：
  python tools\shot_window.py                  # 截整窗 → data\tmp\shell-window.png
  python tools\shot_window.py --client         # 只截客户区
  python tools\shot_window.py --out x.png
  python tools\shot_window.py --click 28,84    # 截图前先点一下（换页签看效果）
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# ⚠️ 自己必须先声明 PerMonitorV2（每屏 DPI 感知），否则 Windows 会把坐标**虚拟化**：
#    实测同一个窗口，不感知的进程量到 1088x720，感知的进程/枢阁自己量到 1360x900，
#    差 1.25 倍（本机 125%）—— 我为此查了半天"三个窗口键为什么画到窗口外面"（2026-10-11）。
try:
    ctypes.WinDLL("user32").SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except Exception:
        pass
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

SHELL = r"D:\core\desktop\Shuge.Desktop\bin\Release\net10.0-windows\win-x64\shuge-app.exe"
DEFAULT_OUT = r"D:\core\data\tmp\shell-window.png"


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD)]


class RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


def find_shell_window():
    """找那个**原生**壳窗口（类名 WindowsForms… 且标题就是 枢阁），别抓错浏览器。"""
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
        if b.value.strip() == "枢阁" and "WindowsForms" in cls.value and user32.IsWindowVisible(h):
            found.append(int(h))
        return True

    user32.EnumWindows(cb, 0)
    return found[0] if found else None


def window_rect(hwnd):
    r = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def quit_dialogs(title_hint="127.0.0.1"):
    """把界面弹出来的原生确认框按掉（Esc）。

    为什么需要：这支脚本会用真鼠标去点窗口里某个坐标（验页签），一旦点歪点到
    「关闭枢阁」那种按钮上，就会弹一个原生确认框把整个界面**卡住**（模态），
    后面所有点击、脚本都跟着废掉（2026-10-11 踩过：探针超时 3 分钟就是这个）。
    """
    n = 0
    for _ in range(4):
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def cb(h, _):
            t = user32.GetWindowTextLengthW(h)
            if t:
                b = ctypes.create_unicode_buffer(t + 1)
                user32.GetWindowTextW(h, b, t + 1)
                cls = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(h, cls, 256)
                if cls.value == "#32770" and title_hint in b.value:
                    found.append(int(h))
            return True

        user32.EnumWindows(cb, 0)
        if not found:
            break
        for h in found:
            user32.SetForegroundWindow(wt.HWND(h))
            time.sleep(0.2)
            user32.keybd_event(0x1B, 0, 0, 0)          # VK_ESCAPE
            user32.keybd_event(0x1B, 0, 2, 0)
            n += 1
            time.sleep(0.5)
    return n


def restore(hwnd):
    """最小化的窗口在 Windows 里"位置"是 (-25600,-25600) 的图标格 —— 先还原成正常窗口，
    否则截出来是 159x27 的一块空白（这支脚本第一版就踩了）。"""
    SW_RESTORE, SW_SHOWNORMAL = 9, 1
    if user32.IsIconic(wt.HWND(hwnd)) or not user32.IsWindowVisible(wt.HWND(hwnd)):
        user32.ShowWindow(wt.HWND(hwnd), SW_RESTORE)
        user32.ShowWindow(wt.HWND(hwnd), SW_SHOWNORMAL)
        time.sleep(1.2)
    user32.SetForegroundWindow(wt.HWND(hwnd))
    time.sleep(0.6)


def capture(hwnd, x, y, w, h, out):
    hdc = user32.GetWindowDC(wt.HWND(hwnd))
    mem = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mem, bmp)
    ok = user32.PrintWindow(wt.HWND(hwnd), mem, PW_RENDERFULLCONTENT)
    if not ok:
        gdi32.BitBlt(mem, 0, 0, w, h, hdc, x, y, SRCCOPY)

    bi = BITMAPINFO()
    bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.bmiHeader.biWidth = w
    bi.bmiHeader.biHeight = -h            # 负数 = 自上而下
    bi.bmiHeader.biPlanes = 1
    bi.bmiHeader.biBitCount = 32
    bi.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), DIB_RGB_COLORS)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem)
    user32.ReleaseDC(wt.HWND(hwnd), hdc)

    from PIL import Image
    img = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    img.save(out)
    return out, (w, h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--client", action="store_true", help="只截客户区（不含标题栏与边框）")
    ap.add_argument("--click", default="", help="截图前先点窗口里的坐标，如 28,84（相对客户区）")
    ap.add_argument("--wait", type=float, default=2.5, help="点完之后等几秒再截")
    a = ap.parse_args()

    hwnd = find_shell_window()
    if not hwnd:
        print("壳窗口没开 —— 我起一个")
        subprocess.Popen([SHELL], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(60):
            time.sleep(0.5)
            hwnd = find_shell_window()
            if hwnd:
                break
    if not hwnd:
        print("起不来（没找到 WindowsForms 类的【枢阁】窗口）")
        return 1

    restore(hwnd)

    if a.click:
        cx, cy = (int(v) for v in a.click.split(","))
        pt = wt.POINT(cx, cy)
        user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(pt))
        user32.SetForegroundWindow(wt.HWND(hwnd))
        time.sleep(0.3)
        user32.SetCursorPos(pt.x, pt.y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)   # 左键按下
        user32.mouse_event(0x0004, 0, 0, 0, 0)   # 左键抬起
        time.sleep(a.wait)

    x, y, w, h = window_rect(hwnd)
    if a.client:
        c = wt.RECT()
        user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(c))
        px = wt.POINT(0, 0)
        user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(px))
        x, y, w, h = px.x, px.y, c.right - c.left, c.bottom - c.top

    out, size = capture(hwnd, x, y, w, h, a.out)
    print("窗口 hwnd=%d 整窗=(%d,%d) %dx%d" % (hwnd, x, y, w, h))
    print("截图 → %s  %dx%d" % (out, size[0], size[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
