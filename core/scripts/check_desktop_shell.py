#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**桌面窗口自检**（可重跑）：验"本地软件窗口"这个壳能不能用、够不够"像软件"。

辞玖 2026-10-09：「把 web 换成本地软件形式」+「参考 obsidian，我不要那种软件里面接浏览器的」。

用 Windows 自己的窗口 API（user32）去看那个窗口，而不是拿"进程还在"糊弄：
  ① 进程活着、有主窗口、标题 = 枢阁、顶层窗口类是 WinForms（不是浏览器窗口类）；
  ② 界面真在画：有 WebView2 渲染面（Chrome_RenderWidgetHostHWND）与 msedgewebview2 进程；
  ③ 没有浏览器 UI：地址栏 / 工具栏 / 后退键 / 自动补全下拉，一个都不许有；
  ④ 尺寸记忆：改**客户区**大小 → 关 → 再开，客户区尺寸一致（±20px，含 DPI 取整）；
  ⑤ 单实例：再点一次不开第二个窗口（进程数 1、窗口句柄不变）；
  ⑥ 启动器语义（`--launcher`）：宿主不在时双击壳，它能把宿主拉起来。

用法：
  python scripts\check_desktop_shell.py
  python scripts\check_desktop_shell.py --launcher    # 会临时重启宿主（计划任务会拉回）
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import time
import urllib.request

SHELL_EXE = r"D:\core\desktop\Shuge.Desktop\bin\Release\net10.0-windows\win-x64\shuge-app.exe"
TITLE = "枢阁"
STATE = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Shuge", "desktop-shell.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
user32 = ctypes.WinDLL("user32", use_last_error=True)
fails = []

BROWSER_UI = ("Omnibox", "Autocomplete", "Toolbar", "Bookmark", "BackButton", "ReloadButton")


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:200]))
    if not cond:
        fails.append(name)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, creationflags=NO_WINDOW)


def find_windows(title=TITLE):
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(hwnd, _):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if title in buf.value:
                out.append((int(hwnd), buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return out


def client_size(hwnd):
    """客户区大小（不含标题栏/边框）—— 这才是"窗口里那一片"的真实尺寸"""
    h = wt.HWND(int(hwnd))
    win = wt.RECT(); cli = wt.RECT()
    user32.GetWindowRect(h, ctypes.byref(win))
    user32.GetClientRect(h, ctypes.byref(cli))
    return (win.left, win.top, cli.right - cli.left, cli.bottom - cli.top)


def win_class(hwnd):
    h = wt.HWND(int(hwnd))
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, buf, 256)
    return buf.value


def child_classes(hwnd):
    classes = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, buf, 256)
        classes.append(buf.value)
        return True

    user32.EnumChildWindows(wt.HWND(int(hwnd)), cb, 0)
    return classes


def tasks(name):
    r = run(["tasklist", "/FI", "IMAGENAME eq " + name, "/FO", "CSV", "/NH"])
    return [l for l in (r.stdout or "").splitlines() if name.lower() in l.lower()]


def host_up(port=8790):
    try:
        urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port, timeout=4).read()
        return True
    except Exception:
        return False


def start_shell(tries=40):
    subprocess.Popen([SHELL_EXE], creationflags=NO_WINDOW)
    for _ in range(tries):
        time.sleep(0.5)
        if find_windows():
            return True
    return bool(find_windows())


def main():
    do_launcher = "--launcher" in sys.argv
    print("① 壳在跑吗")
    if not tasks("shuge-app.exe"):
        print("   没在跑 —— 我起一个")
        start_shell()
    chk("壳进程在（shuge-app.exe）", len(tasks("shuge-app.exe")) >= 1, tasks("shuge-app.exe"))

    wins = find_windows()
    chk("有标题含【枢阁】的顶层窗口", len(wins) >= 1, wins)
    if not wins:
        print("（没有窗口，后面没法量）")
        return 1
    hwnd, title = wins[0]
    x, y, w, h = client_size(hwnd)
    print("   窗口：hwnd=%s 标题=%r 客户区=%dx%d" % (hwnd, title, w, h))
    print("   顶层窗口类：%s" % win_class(hwnd))
    chk("顶层窗口是 WinForms 窗口（不是浏览器窗口类）", "WindowsForms" in win_class(hwnd), win_class(hwnd))

    print("② 界面真的在渲染吗")
    chk("有 msedgewebview2.exe（Edge 内核在渲染界面）", len(tasks("msedgewebview2.exe")) >= 1,
        len(tasks("msedgewebview2.exe")))
    kids = child_classes(hwnd)
    print("   子控件类名（前 8）：%s" % kids[:8])
    chk("有 WebView2 渲染面（界面真在画）", any("RenderWidgetHost" in k for k in kids), kids[:8])

    print("③ 有没有浏览器 UI（地址栏/工具栏/后退键/自动补全）")
    ui = [k for k in kids if any(t in k for t in BROWSER_UI)]
    chk("一个浏览器 UI 控件都没有", not ui, ui)
    chk("窗口标题就是【枢阁】（不是 xxx - Microsoft Edge）", title.strip() == TITLE, title)

    print("④ 尺寸记忆（改客户区大小 → 关 → 再开）")
    target_w, target_h = 1180, 760
    # MoveWindow 收的是**整窗**大小 → 按当前差值补上边框/标题栏
    wx, wy, cw0, ch0 = client_size(hwnd)
    win = wt.RECT(); user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(win))
    dw = (win.right - win.left) - cw0
    dh = (win.bottom - win.top) - ch0
    user32.MoveWindow(wt.HWND(hwnd), win.left, win.top, target_w + dw, target_h + dh, True)
    time.sleep(1.5)
    run(["taskkill", "/IM", "shuge-app.exe", "/F"])
    time.sleep(2)
    start_shell()
    wins2 = find_windows()
    chk("重开后又有窗口", len(wins2) >= 1, wins2)
    if wins2:
        x2, y2, w2, h2 = client_size(wins2[0][0])
        print("   重开后客户区 = %dx%d（期望约 %dx%d）" % (w2, h2, target_w, target_h))
        chk("客户区尺寸被记住（±20px）", abs(w2 - target_w) <= 20 and abs(h2 - target_h) <= 20, "%dx%d" % (w2, h2))
    chk("状态文件写下来了", os.path.isfile(STATE), STATE)
    if os.path.isfile(STATE):
        print("   状态：%s" % open(STATE, encoding="utf-8").read().replace("\n", " ")[:160])

    print("⑤ 单实例：再点一次")
    before = find_windows()[0][0] if find_windows() else None
    subprocess.Popen([SHELL_EXE], creationflags=NO_WINDOW)
    time.sleep(4)
    chk("进程数仍是 1", len(tasks("shuge-app.exe")) == 1, len(tasks("shuge-app.exe")))
    after = find_windows()[0][0] if find_windows() else None
    chk("窗口还是原来那个（没开第二个）", before == after, "%s vs %s" % (before, after))

    if do_launcher:
        print("⑥ 启动器语义：宿主不在时，壳要能把它拉起来")
        chk("前置：宿主在跑", host_up(), "8790")
        run(["taskkill", "/IM", "core-host.exe", "/F"])
        time.sleep(4)
        chk("前置：宿主已停", not host_up(), "8790")
        run(["taskkill", "/IM", "shuge-app.exe", "/F"])
        time.sleep(2)
        start_shell()
        up = False
        for _ in range(40):
            time.sleep(1)
            if host_up():
                up = True
                break
        chk("壳把宿主拉起来了（8790 通）", up, "等 40 秒")
        if up:
            hh = json.loads(urllib.request.urlopen("http://127.0.0.1:8790/healthz", timeout=5).read().decode())
            print("   宿主：pid=%s core_dir=%s" % (hh.get("pid"), hh.get("core_dir")))
            st = json.loads(urllib.request.urlopen("http://127.0.0.1:8790/api/host/status", timeout=15).read().decode())
            notes = st.get("notes") or {}
            print("   库：%s（健康 %s）" % (notes.get("dir"), notes.get("healthy")))

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 桌面窗口可用：真窗口 + 界面在渲染 + 没有浏览器 UI + 尺寸记忆 + 单实例")
    return 0


if __name__ == "__main__":
    sys.exit(main())
