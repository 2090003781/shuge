#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检：控制台那几处「手上感觉」还在不在 —— 按钮悬停浅白 / 点击白晕 / 识别转齿轮。

辞玖 2026-10-05：「按钮悬停加一个浅白特效」「还有点击特效，然后在识别过程中加一个旋转齿轮动画」。

做法（全 CSS，不引第三方）：
  · 悬停：`background-image` 叠一层半透明白 —— **不动按钮自己的底色**（红的还是红的、蓝的还是蓝的），
    所以这里顺带盯着"有没有别的规则也设 background-image"（有就会把这层白顶掉）。
  · 点击：`button:active` 缩一下 + 一圈白晕扩散（`@keyframes btn-hit`）。
  · 齿轮：`.spin` + `@keyframes ui-spin`，识别/接入进行中由 agents.js 挂上；`prefers-reduced-motion` 下不转。

跑：python scripts\\check_ui_effects.py     退出码 0 = 干净
"""
import io
import os
import re
import sys
import urllib.request

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(CORE, "web", "index.html")
AGJS = os.path.join(CORE, "web", "agents.js")
URL = os.environ.get("CONSOLE_URL", "http://127.0.0.1:8795/")
fails, oks, warns = [], [], []


def read(p):
    try:
        return io.open(p, encoding="utf-8").read()
    except Exception as e:
        print("读不到 %s：%s" % (p, e))
        sys.exit(1)


raw, js = read(HTML), read(AGJS)

RULES = [
    # ① 悬停浅白
    (raw, r"button\s*\{[^}]*transition", "按钮有过渡（不然是硬跳）"),
    (raw, r"button:hover:not\(:disabled\)\s*\{[^}]*linear-gradient\(rgba\(255,\s*255,\s*255", "悬停叠一层浅白"),
    (raw, r"button:hover:not\(:disabled\)\s*\{[^}]*inset 0 0 0 1px rgba\(255,\s*255,\s*255", "悬停有内圈淡白边"),
    (raw, r"button:hover:not\(:disabled\)\s*\{[^}]*box-shadow[^}]*rgba\(255,\s*255,\s*255", "悬停有柔光"),
    # ② 点击白晕
    (raw, r"@keyframes\s+btn-hit", "点击白晕关键帧（btn-hit）"),
    (raw, r"button:active:not\(:disabled\)\s*\{[^}]*animation:\s*btn-hit", "按下时触发白晕"),
    (raw, r"button:active:not\(:disabled\)\s*\{[^}]*scale\(\.9", "按下时缩一下"),
    (raw, r"button:disabled", "禁用态变淡 + 禁手"),
    # ③ 转圈齿轮
    (raw, r"@keyframes\s+ui-spin", "齿轮关键帧（ui-spin）"),
    (raw, r"\.spin\s*\{[^}]*animation:\s*ui-spin", ".spin 会转"),
    (raw, r"@media\s*\(prefers-reduced-motion[^)]*\)\s*\{[\s\S]*?\.spin\s*\{[^}]*animation:\s*none", "动了系统「减少动态效果」就不转"),
    (js, r"class=\\?\"spin\\?\"", "agents.js 里挂上了 .spin"),
    (js, r"识别中", "agents.js 识别中有文案"),
    (js, r"_acBusy", "识别/接入时按钮也进忙态"),
]
for text, pat, name in RULES:
    if re.search(pat, text, re.S):
        oks.append(name)
    else:
        fails.append("缺：" + name)

# 悬停那层白用 background-image 叠的：别人也设就会顶掉。
# 先去掉注释，只看"属性位置"的 background-image（transition: background-image 不算）。
# 两处**故意**的例外，不算"顶掉"：
#   · :root 里定义变量（--x: linear-gradient(...)）—— 不是直接给元素设；
#   · 主题按钮 `button.theme-btn:hover` 里显式 `background-image: none` —— 它就是要**关掉**那层白
#     （辞玖 2026-10-06 16:38「按钮太突兀」→ 主题按钮故意不吃悬停白晕）。
css = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)
compet = []
for m in re.finditer(r"(?:^|[;{])\s*background-image\s*:", css):
    seg = css[m.end(): m.end() + 240]
    val = seg.strip().splitlines()[0][:70]
    if "rgba(255,255,255" in seg:
        continue
    if val.strip().lower().startswith("none"):
        continue
    # 往前找 200 字符，判断这条规则是不是 :root（变量定义）
    before = css[max(0, m.start() - 200): m.start()]
    if ":root" in before.split("}")[-1]:
        continue
    compet.append(val)
if compet:
    fails.append("还有别的规则设 background-image，会把浅白顶掉：\n      " + "\n      ".join(compet))
else:
    oks.append("没有别的规则设 background-image（浅白不会被顶掉）")

# 跑着的控制台发的是不是同一份（磁盘直发 + noCache，刷新即生效）
try:
    live = urllib.request.urlopen(URL, timeout=10).read().decode("utf-8", "replace")
    if "button:hover:not(:disabled)" in live and "ui-spin" in live:
        oks.append("跑着的控制台（%s）已经在发这些样式，刷新就能看到" % URL)
    else:
        warns.append("控制台（%s）发的还没有这些样式（页面没刷新？或服务读的是别处 web 目录）" % URL)
except Exception as e:
    warns.append("取不到控制台 %s：%s" % (URL, e))

print("控制台手感体检（悬停 / 点击 / 齿轮）")
print("-" * 56)
for x in oks:
    print("  ✅ " + x)
for x in warns:
    print("  ⚠️  " + x)
for x in fails:
    print("  ❌ " + x)
print("-" * 56)
if fails:
    print("失败 %d 条" % len(fails))
    sys.exit(1)
print("通过（%d 条提醒）" % len(warns))