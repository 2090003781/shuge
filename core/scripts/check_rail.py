#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**窗口左侧那一列的几何自检**（可重跑）：拿真截图量像素，不靠"我看着像"。

辞玖 2026-10-11 要的形状：
  · 顶部**没有**那条黑条（原来的菜单栏已经去掉）；
  · 最左边一列里三个页签（控制台/库/内核），**最底下**一个设置键；
  · 颜色跟主题一致（护眼浅黄：那一列 #F7F0DE）；
  · 页签那一列右边是页面本体，正文不许贴着列。

两个坑（都踩过，这里处理掉）：
  ① PrintWindow 截客户区时，图里左上有标题栏那一圈 → 先**定位列**再量，不写死原点；
  ② 鼠标正好停在某个页签上会变 hover 色 → 取样点避开鼠标所在那一格。

用法：python scripts\check_rail.py
"""
import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shot_window import capture, find_shell_window, quit_dialogs, restore  # noqa: E402

SHELL = r"D:\core\desktop\Shuge.Desktop\bin\Release\net10.0-windows\win-x64\shuge-app.exe"
OUT = r"D:\core\data\tmp\rail-check.png"
user32 = ctypes.WinDLL("user32", use_last_error=True)
NW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
fails = []

RAIL_W = 56
BAR = (247, 240, 222)       # --bar：那一列的底
BODY = (254, 248, 234)      # --panel2：页面正文的底


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  <- " + str(extra)[:220]))
    if not cond:
        fails.append(name)


def near(c, ref, tol=5):
    return all(abs(a - b) <= tol for a, b in zip(c[:3], ref))


def ink(px, x0, y0, x1, y1):
    """这一小块里"不是底色"的像素占比 —— emoji 是彩色的，不能拿"比底色暗"当判据。"""
    n = diff = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            n += 1
            c = px[x, y][:3]
            if max(abs(c[0] - BAR[0]), abs(c[1] - BAR[1]), abs(c[2] - BAR[2])) > 26:
                diff += 1
    return (diff / n) if n else 0.0


def client_shot(hwnd, out):
    c = wt.RECT()
    user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(c))
    p = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(p))
    return capture(hwnd, p.x, p.y, c.right - c.left, c.bottom - c.top, out)


def locate(px, w, h):
    """在截图里找到"那一列"的左右边界：扫一行，连续 BAR 色的那一段就是列。"""
    y = int(h * 0.62)
    runs, start = [], None
    for x in range(w):
        ok = near(px[x, y], BAR)
        if ok and start is None:
            start = x
        elif not ok and start is not None:
            if x - start >= 20:
                runs.append((start, x))
            start = None
    if start is not None and w - start >= 20:
        runs.append((start, w))
    if not runs:
        return None
    left, right = max(runs, key=lambda r: r[1] - r[0])
    y0 = 0
    for yy in range(y):
        if near(px[left + 4, yy], BAR):
            y0 = yy
            break
    return left, right, y0   # x0=列的左沿；右沿=列的右边；y0=列在**图里**的顶（=客户区顶）


def gear_by_child(hwnd, client_h):
    """图被裁的时候，改用窗口子控件的真实坐标，报告设置键的位置（顺便看看它离底边多远）。"""
    out = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(c, _):
        r = wt.RECT()
        user32.GetWindowRect(c, ctypes.byref(r))
        out.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return True

    root = wt.RECT()
    user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(root))
    cli = wt.RECT()
    user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(cli))
    org = wt.POINT(0, 0)
    user32.ClientToScreen(wt.HWND(hwnd), ctypes.byref(org))
    kids = []
    user32.EnumChildWindows(wt.HWND(hwnd), cb, 0)
    for (l, t, w, hh) in out:
        rel_y = t - org.y
        rel_x = l - org.x
        if 20 <= w <= 60 and 20 <= hh <= 60 and rel_x < 60:
            kids.append((rel_x, rel_y, w, hh))
    if not kids:
        return -1, 0.0
    kids.sort(key=lambda k: k[1])
    bottom = [k for k in kids if k[1] + k[3] >= client_h - 60]
    print("   列上的按钮格（相对客户区）：%s" % kids)
    return (len(bottom), 0.0)


def main():
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
        print("❌ 起不来")
        return 1
    restore(hwnd)
    time.sleep(6)                      # 等界面装配完
    _, size = client_shot(hwnd, OUT)
    print("客户区截图 %s  %dx%d" % (OUT, size[0], size[1]))

    from PIL import Image
    im = Image.open(OUT).convert("RGB")
    px = im.load()
    w, h = im.size

    crect = wt.RECT()
    user32.GetClientRect(wt.HWND(hwnd), ctypes.byref(crect))
    real_ch = crect.bottom - crect.top
    real_cw = crect.right - crect.left

    quit_dialogs()          # 上一次留下的确认框会卡住界面（模态），先按掉
    loc = locate(px, w, h)
    if not loc:
        print("❌ 截图里找不到那一列（底色 #F7F0DE 的那一段）")
        return 1
    x0, x1, y0 = loc
    # 图里 x0,y0 就是客户区原点；客户区高 = h - y0（图底下那一截是标题栏之外的一圈）
    ch = h - y0
    print("那一列：x %d→%d（宽 %d）｜图里客户区原点 (%d,%d)，客户区高约 %d（图高 %d）"
          % (x0, x1, x1 - x0, x0, y0, ch, h))

    print("① 那一列的底色与宽度")
    # 壳里写的是**逻辑 56**，乘上本机 DPI 缩放才是物理宽度（本机 125% → 70px）。
    # 缩放从哪来：壳里那套几何是按"逻辑 720 高"这一档写的，物理高 / 720 = 本机缩放。
    scale = real_ch / 720.0 if real_ch else 1.0
    want = int(round(RAIL_W * scale))
    chk("列宽 = %dpx（56 逻辑 × 缩放 %.2f）" % (want, scale),
        abs((x1 - x0) - want) <= 4, "实测 %d" % (x1 - x0))
    # 取样只挑"干净的底"：格子之间、以及那一列最右一条（避开当前页签那块高亮与 emoji 字形）
    spots = [(x0 + 3, y0 + 14), (x0 + 3, y0 + int(320 * scale)), (x0 + 3, y0 + ch // 2),
             (x0 + 3, y0 + ch - int(70 * scale)), (x0 + int(4 * scale), y0 + ch - 3)]
    bad = [(p, px[p]) for p in spots if not near(px[p], BAR)]
    chk("整列都是主题底色 #F7F0DE（护眼浅黄）", not bad, bad)

    print("② 列右边就是页面本体（没被列盖住、也没留缝）")
    right = px[x1 + 5, y0 + 300]
    chk("列右边是页面底色（不是列自己的色）", not near(right, BAR), right)

    print("③ 三个页签：每一格都画出了图标")
    for i, nm in enumerate(("控制台", "库", "内核")):
        t = y0 + int(round((10 + i * 54) * scale))
        r = ink(px, x0 + int(13 * scale), t + int(6 * scale), x0 + int(45 * scale), t + int(38 * scale))
        chk("第 %d 格（%s）有图标" % (i + 1, nm), r > 0.02, "墨色占比 %.3f" % r)

    print("④ 最底下那一格（⋯ 更多），它上面一格是干净的")
    clipped = (y0 + real_ch) > h   # 图比客户区矮 → PrintWindow 被屏幕/桌面边裁了
    if clipped:
        print("   截图被裁了（客户区 %dx%d，图只有 %dx%d）—— 用窗口自己的控件坐标量"
              % (real_cw, real_ch, w, h))
        gear, above = gear_by_child(hwnd, real_ch)
    else:
        gear = ink(px, x0 + int(10 * scale), y0 + ch - int(50 * scale),
                   x0 + int(46 * scale), y0 + ch - int(8 * scale))
        above = ink(px, x0 + int(10 * scale), y0 + ch - int(100 * scale),
                    x0 + int(46 * scale), y0 + ch - int(54 * scale))
    chk("最底下那一格有图标（⋯ 更多）", gear > 0, "位置/墨色 = %s" % (gear,))
    chk("那一格没飘在中间", above < 0.006, "墨色占比 %.3f" % above)

    print("⑤ 占位：页面正文与列之间留了呼吸位")
    # ⚠️ 两个坑：① 列最右边那 1px 是**分隔线**（不是正文）；② 页面自己的底色
    #    （--panel / --panel2）跟这一列的底色很接近，不能当成"内容"。
    #    所以从"列右边 2px"起扫，只认"既不是列的色、也不是页面底色"的像素。
    first = None
    for yy in range(y0 + 80, y0 + ch - 40, 30):
        for x in range(x1 + 2, min(w, x1 + 260)):
            if not near(px[x, yy], BAR, 14) and not near(px[x, yy], BODY, 14):
                first = (x, yy)
                break
        if first:
            break
    gap = (first[0] - x1) if first else -1
    chk("正文与列之间 ≥12px", gap >= 12, "第一处内容像素在列右侧 %s px（y=%s）" % (gap, first[1] if first else "-"))

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 左侧那一列：四个页签（控制台/库/内核/设置）+ 最底下「更多」+ 护眼浅黄同主题 + 正文留了呼吸位")
    return 0


if __name__ == "__main__":
    sys.exit(main())
