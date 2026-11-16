#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""生成枢阁的窗口图标（shuge.ico）：护眼底 + 一个「枢」字。

辞玖 2026-10-11：「还有这个图标是能改的吗」—— 能。原来窗口用的是系统默认图标，
现在出一份跟界面同色的：浅黄底（#F7F0DE）+ 深蓝「枢」（#1F4E79）。

产物：<平台>\bin\shuge.ico（多尺寸：16/24/32/48/64/128/256）——可执行文件与窗口都用它。
重新生成：python scripts\make_icon.py
"""
import os

from PIL import Image, ImageDraw, ImageFont

OUT = r"D:\core\bin\shuge.ico"
SIZES = [256, 128, 64, 48, 32, 24, 16]
BG = (247, 240, 222)      # --bar（护眼浅黄）
LINE = (226, 216, 188)    # --line
INK = (31, 78, 121)       # --on-primary 深蓝
FONT_CANDIDATES = [r"C:\Windows\Fonts\msyhbd.ttc", r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]


def font(size):
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            try:
                return ImageFont.truetype(f, size)
            except Exception:
                continue
    return ImageFont.load_default()


def one(size):
    # 4 倍超采样再缩：小尺寸下的笔画才干净
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    r = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=BG, outline=LINE, width=max(2, s // 42))
    f = font(int(s * 0.62))
    box = d.textbbox((0, 0), "枢", font=f)
    d.text(((s - (box[2] - box[0])) / 2 - box[0], (s - (box[3] - box[1])) / 2 - box[1]),
           "枢", font=f, fill=INK)
    return im.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    imgs = [one(n) for n in SIZES]
    imgs[0].save(OUT, format="ICO", sizes=[(n, n) for n in SIZES])
    print("图标 → %s  %d 字节  尺寸 %s" % (OUT, os.path.getsize(OUT), SIZES))
    # ② 同时写一份到桌面壳工程里：`<ApplicationIcon>` 要嵌进 exe（文件/任务栏图标）
    proj_ico = r"D:\core\desktop\Shuge.Desktop\shuge.ico"
    os.makedirs(os.path.dirname(proj_ico), exist_ok=True)
    imgs[0].save(proj_ico, format="ICO", sizes=[(n, n) for n in SIZES])
    print("图标（工程内）→ %s" % proj_ico)

    # 顺手存一张 PNG 便于肉眼看（自检/截图用）
    png = os.path.join(r"D:\core\data\tmp", "shuge-icon.png")
    os.makedirs(os.path.dirname(png), exist_ok=True)
    imgs[0].save(png)
    print("预览 → %s" % png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
