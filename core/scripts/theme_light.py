#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""给枢阁加一个**浅白（护眼浅黄）**主题 —— 三个界面共用一套调色板。

辞玖 2026-10-06：「给枢阁加一个浅白（带一点护眼浅黄）的主题」。

做法（为什么这么绕）：
  · 三个界面的风格本来都是 Tokyo Night 深色（`#16161e/#1a1b26/#c0caf5…`），
    但**只有库界面**是变量驱动的（`:root{--bg…}`），内核控制台与主进程页面全是**硬编码 hex**。
  · 直接堆一堆 `!important` 覆盖是治标：以后改一处颜色还得再摸一遍。
    所以先把最常见的 19 个颜色**抽成 CSS 变量**（`#1a1b26` → `var(--panel)` …），
    再只写**一个** `html[data-theme="light"]{…}` 覆盖块 —— 整站跟着变。
  · 抽变量时**跳过变量定义区**（`:root{}` 与浅色块），否则 `--bg:#16161e` 会被替换成
    `--bg:var(--bg)` 自引用。

开关：两个界面头部各一个「☀️ 浅白·护眼 / 🌙 深色」按钮，选择记在 `localStorage['shuge-theme']`，
也认 `?theme=light`（方便分享链接）。默认仍是深色（不动原来习惯）。

用法：
    python scripts\theme_light.py --check          # 只看现状（不动文件）
    python scripts\theme_light.py --apply          # 抽变量 + 加浅色块 + 装切换按钮
    python scripts\theme_light.py --apply --dry    # 打印将要做什么，不落盘
"""
import argparse
import io
import os
import re
import sys

NOTES = r"D:\core-notes\web\index.html"
CONSOLE = r"D:\core\web\index.html"
CONSOLE_JS = [r"D:\core\web\%s" % f for f in
              ("app.js", "agents.js", "logs.js", "corectl.js", "kernelctl.js", "md.js")]
HOST_GO = [r"D:\core\cmd\core-host\page.go", r"D:\core\cmd\core-host\kernelpage.go"]

# 深色 → 变量名（长串在前无所谓，都是 6 位 hex 互不包含；但同一色值只映射一次）
MAP = [
    ("#16161e", "--bg"), ("#1a1b26", "--panel"), ("#1f2335", "--panel2"),
    ("#24283b", "--panel3"), ("#20233a", "--bar"), ("#3b4261", "--line"),
    ("#2f3549", "--line2"), ("#c0caf5", "--fg"), ("#9aa5ce", "--fg2"),
    ("#565f89", "--dim"), ("#7aa2f7", "--blue"), ("#9ece6a", "--green"),
    ("#f7768e", "--red"), ("#e0af68", "--amber"), ("#bb9af7", "--purple"),
    ("#2d3350", "--sel"), ("#232433", "--hover"), ("#20222f", "--hit"),
    ("#101018", "--edge"),
    # ── 第二批（按第一轮之后的实际残留补：块背景 + 次要文字）──
    ("#1c1d2b", "--bg2"), ("#2a2e42", "--bg3"), ("#22304d", "--sel2"), ("#3d4670", "--sel3"),
    ("#2a3a5c", "--info-bg"), ("#1f3d2b", "--ok-bg"), ("#233024", "--ok-bg2"),
    ("#4d3a1f", "--warn-bg"), ("#4a3b1f", "--warn-bg2"), ("#2a271d", "--warn-bg3"),
    ("#3a2a30", "--bad-bg"), ("#3d4a2a", "--ok-bg3"), ("#4b6a2f", "--ok-bg4"),
    ("#8b93b8", "--dim2"), ("#a9b1d6", "--fg3"), ("#7dcfff", "--cyan"), ("#89b4fa", "--blue2"),
    ("#f7a8b8", "--pink"), ("#d99", "--red2"), ("#e8a", "--magenta"), ("#ffd479", "--amber2"),
    ("#6b4a2f", "--brown"), ("#6b5a2f", "--brown2"), ("#2f6b43", "--green2"),
    ("#e6ffd8", "--ok-fg"),
    # ── 第三批（辞玖截图里"读不出字的深色方块"顺藤摸出来的）──
    ("#2b3f66", "--primary"), ("#3d5a8a", "--primary-line"),
    ("#262a45", "--hover2"), ("#e0c07a", "--warn-fg2"), ("#3a3320", "--warn-bg4"),
    ("#2a2f45", "--line3"), ("#15161f", "--bg4"), ("#1f2233", "--bg5"),
]

# 有变量但**没有对应深色字面量**的（如"按钮上的字"），单独给深色值
EXTRA_DARK = {"on-primary": "#c0caf5"}

# 一次性规则修订（幂等：已经是新样子就跳过）：(相对路径关键词, 旧, 新)
RULE_FIXES = [
    ("button.primary { background:var(--primary); border-color:var(--primary-line); }",
     "button.primary { background:var(--primary); border-color:var(--primary-line); }",
     "button.primary { background:var(--primary); border-color:var(--primary-line); color:var(--on-primary); }"),
]
DARK = {v.lstrip("-"): k for k, v in MAP}
DARK.update(EXTRA_DARK)          # 没有深色字面量的变量（如 --on-primary）
LIGHT = {
    "bg": "#FBF6E7",      # ← 护眼浅黄就是这一块底
    "panel": "#FFFCF3",
    "panel2": "#FEF8EA",
    "panel3": "#F5EDD9",
    "bar": "#F7F0DE",
    "line": "#E2D8BC",
    "line2": "#EDE6D2",
    "fg": "#33302A",
    "fg2": "#4C473C",
    "dim": "#6E6656",      # 4.34:1 不过，调深（--check 逮到的）
    "blue": "#2F6DB0",
    "green": "#37703E",
    "red": "#A8463F",
    "amber": "#8A6110",
    "purple": "#6A4FA3",
    "sel": "#EFE3C2",
    "hover": "#F6EEDA",
    "hit": "#F8F2E2",
    "edge": "#F3EAD3",
    # 第二批：块背景压浅、次要文字压深（都是"浅底上看得清、又不像纯黑白那样冲"）
    "bg2": "#F4EBD6", "bg3": "#F1E7CF", "sel2": "#DCE7F5", "sel3": "#C9D8EE",
    "info-bg": "#DCE8F7", "ok-bg": "#DDEBD9", "ok-bg2": "#DDEBD9", "ok-bg3": "#E2EFD9",
    "ok-bg4": "#D5E8CE", "warn-bg": "#F6E7C6", "warn-bg2": "#F6E7C6", "warn-bg3": "#F7EAD0",
    "bad-bg": "#F5DEDC", "dim2": "#6E6656", "fg3": "#3E3A31", "cyan": "#1F6E8C",
    "blue2": "#2F6DB0", "pink": "#A8463F", "red2": "#A8463F", "magenta": "#8A3F7A",
    "amber2": "#8A6110", "brown": "#7A5A20", "brown2": "#7A5A20", "green2": "#37703E",
    "ok-fg": "#2F5A33",
    # 第三批：主按钮在浅色下要"白字蓝底"（原样式没写文字色，浅色继承到深字 = 看不见）
    # 辞玖 2026-10-06：「浅色模式下保存按钮改成浅蓝」——浅蓝底 + 深蓝字，
    # 描边留住对比（浅蓝底压在米色页面上只有 ~1.2:1，不描边就看不出按钮的边界）
    "primary": "#D7E5F8", "primary-line": "#5E8CBE", "on-primary": "#1F4E79",
    "hover2": "#F1E7CF", "warn-fg2": "#7A5A20", "warn-bg4": "#F6E7C6",
    "line3": "#E2D8BC", "bg4": "#F2E8D2", "bg5": "#F1E7CF",
}
MARK = "/* shuge-theme */"
HERE = os.path.dirname(os.path.abspath(__file__))
BOOT_JS = os.path.join(HERE, "theme_boot.js")      # 主题引导的**真源码**（可单测/可 lint）
LIGHT_BLOCK = MARK + """
html[data-theme="light"] {
%s}
"""
TOGGLE_BTN = ('<button id="btnTheme" class="theme-btn"'
              ' title="主题：浅白·护眼（带一点暖黄，看着不刺眼）/ 深色；三个界面一起变，选择会记住"'
              ' onclick="__shugeTheme.toggle()"><span class="ti">☀️</span><span class="tt">浅白·护眼</span></button>')

# 主题按钮的样子（三页统一）。辞玖 2026-10-06 16:38：「这个按钮有点太突兀了，
# 换成稍微明显但是和整体不突兀的样式」——平面、细描边、次要文字色、圆角胶囊；
# 悬停只提一档（不加白晕，免得又抢视线），点下去缩一下，键盘 focus 有描边。
THEME_BTN_CSS = MARK + """
button.theme-btn {
  display: inline-flex; align-items: center; gap: 6px;
  background: var(--panel3); color: var(--dim2);
  border: 1px solid var(--line2); border-radius: 999px;
  padding: 4px 12px; font-size: 12px; line-height: 1.5; cursor: pointer;
  white-space: nowrap;
}
button.theme-btn .ti { font-size: 13px; line-height: 1; opacity: .95; }
button.theme-btn .tt { color: var(--dim2); }
button.theme-btn:hover:not(:disabled) {
  color: var(--fg2); border-color: var(--line); background-image: none; box-shadow: none;
}
button.theme-btn:active:not(:disabled) { transform: translateY(1px) scale(.98); animation: none; }
button.theme-btn:focus-visible { outline: 2px solid var(--primary-line); outline-offset: 1px; }
@media (prefers-reduced-motion: reduce) { button.theme-btn:active:not(:disabled) { transform: none; } }
"""
_BOOT = io.open(BOOT_JS, encoding="utf-8").read().rstrip("\n")
# ⚠️ 硬断言（2026-10-06 事故）：这段脚本要内联进 page.go / kernelpage.go 的**反引号原始字符串**，
#    脚本里一旦出现反引号，Go 字符串就被提前结束 → 宿主编不出来（而 python/node 检查全绿，很难查）。
#    `%` 也挡掉：Go 侧若过 Sprintf，`%` 会被当格式动词。
assert "`" not in _BOOT, "theme_boot.js 里不能有反引号（会截断 Go 原始字符串）"
assert "%" not in _BOOT, "theme_boot.js 里不能有 %（Go Sprintf 会当格式动词）"
THEME_JS = "\n<script>%s\n%s</script>\n" % (MARK, _BOOT)


def read(p):
    with open(p, "rb") as f:
        raw = f.read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return (raw[3:] if bom else raw).decode("utf-8"), bom


def write(p, txt, bom):
    data = txt.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    with open(p, "wb") as f:
        f.write(data)


def def_regions(txt):
    """变量定义区（`:root{}`、浅色块）的区间 —— 抽变量时要跳过它们"""
    spans = []
    for pat in (r":root\s*\{[^}]*\}", r'html\[data-theme="light"\]\s*\{[^}]*\}'):
        spans += [m.span() for m in re.finditer(pat, txt, re.S)]
    return spans


def in_spans(i, spans):
    return any(a <= i < b for a, b in spans)


def ensure_root(txt):
    """确保 :root 里 19 个变量都在（缺的补上）；返回 (新文本, 补了几个)"""
    m = re.search(r":root\s*\{([^}]*)\}", txt, re.S)
    missing = [k for k in DARK if not re.search(r"--%s\s*:" % re.escape(k), m.group(1) if m else "")]
    if not missing:
        return txt, 0
    add = "".join(" --%s:%s;" % (k, DARK[k]) for k in missing)
    if m:
        return txt[:m.end(1)] + add + txt[m.end(1):], len(missing)
    # 没有 :root 就新建一个（放在第一个 <style> 后面）
    i = txt.find("<style>")
    assert i > 0, "找不到 <style>"
    j = i + len("<style>")
    return txt[:j] + "\n:root {%s }\n" % add + txt[j:], len(missing)


def subst(txt):
    """把硬编码色换成 var(...)（跳过变量定义区）—— 一次正则搞定，re.I 认大写写法"""
    spans = def_regions(txt)
    look = {h.lower(): v for h, v in MAP}
    pat = re.compile("|".join(re.escape(h) for h, _ in MAP), re.I)

    def rep(m):
        if in_spans(m.start(), spans):
            return m.group(0)
        return "var(%s)" % look[m.group(0).lower()]

    new, n = pat.subn(rep, txt)
    return new, n


def add_light_block(txt):
    """浅色块是**生成物**：每次都按当前 DARK/LIGHT 重新生成并整块替换（幂等）。

    ⚠️ 2026-10-06 踩过：原来写成"看到标记就原样返回"，于是第二批/第三批变量只进了 `:root`，
    浅色块里没有 → 那些变量在浅色下回落到深色值（`--primary` 还是深蓝、主按钮还是深色块）。
    被自检里新加的「浅色块写全了」当场逮住。
    """
    block = LIGHT_BLOCK % "".join("  --%s: %s;\n" % (k, LIGHT[k]) for k in DARK)
    pat = re.compile(r'%s\s*html\[data-theme="light"\]\s*\{[^}]*\}' % re.escape(MARK))
    if pat.search(txt):
        new = pat.sub(lambda m: block.rstrip("\n"), txt, count=1)
        return new, (new != txt)
    i = txt.find("</style>")
    assert i > 0, "找不到 </style>"
    return txt[:i] + block + txt[i:], True


def add_theme_css(txt):
    """按钮样式也是**生成物**：已存在就整块替换（否则改了样式，老页面不会更新）。"""
    pat = re.compile(r'%s\s*button\.theme-btn\s*\{[^}]*\}'
                     r'(?:\s*button\.theme-btn[^{]*\{[^}]*\})*'
                     r'(?:\s*@media[^{]*\{[^{}]*\})?' % re.escape(MARK), re.S)
    if pat.search(txt):
        new = pat.sub(lambda m: THEME_BTN_CSS.rstrip('\n'), txt, count=1)
        return new, (new != txt)
    i = txt.find('</style>')
    assert i > 0, '找不到 </style>'
    return txt[:i] + THEME_BTN_CSS + txt[i:], True


def add_js(txt):
    """主题脚本也是**生成物**：已存在就整段替换（否则改了脚本，老页面不会更新）。"""
    pat = re.compile(r"<script>%s.*?</script>\s*" % re.escape(MARK), re.S)
    if pat.search(txt):
        new = pat.sub(lambda m: THEME_JS, txt, count=1)
        return new, (new != txt)
    i = txt.find("</body>")
    txt2 = (txt + THEME_JS) if i < 0 else (txt[:i] + THEME_JS + txt[i:])
    return txt2, True


def insert_toggle(txt):
    """把切换按钮插到品牌名后面（找不到品牌名就退到 </header> / </body> 前）。

    ⚠️ 2026-10-06 踩过：原来写成「看到 id=btnTheme 就原样返回」，于是改了按钮的类名/文案，
    老页面**永远不会更新**（改了样式看不到变化，白折腾）。现在按钮是**生成物**：
    已有那个按钮就整段替换成当前 TOGGLE_BTN。
    """
    pat = re.compile(r'<button id="btnTheme".*?</button>', re.S)
    if pat.search(txt):
        new = pat.sub(lambda m: TOGGLE_BTN, txt, count=1)
        return new, (new != txt)
    m = re.search(r'<span class="brand"[^>]*>.*?</span>', txt, re.S)
    if m:
        return txt[:m.end()] + "\n  " + TOGGLE_BTN + txt[m.end():], True
    for anchor in ("</header>", "</body>"):
        i = txt.find(anchor)
        if i > 0:
            return txt[:i] + "  " + TOGGLE_BTN + "\n" + txt[i:], True
    return txt, False


def process(path, toggle=True, css=True, label=""):
    """css=False：只把这个文件里的硬编码色换成 var(...)（.js 那种只有内联样式的文件）"""
    txt, bom = read(path)
    before = len(re.findall(r"#[0-9a-fA-F]{6}", txt))
    if css:
        txt, nroot = ensure_root(txt)
    else:
        nroot = 0
    txt, nvar = subst(txt)
    fixed = 0
    for _kw, old, new in RULE_FIXES:            # 规则修订（幂等）
        if old in txt and new not in txt:
            txt = txt.replace(old, new, 1)
            fixed += 1
    if css:
        txt, blk = add_light_block(txt)
        txt, btncss = add_theme_css(txt)
        txt, js = add_js(txt)
    else:
        blk = js = btncss = False
    btn = False
    if toggle:
        txt, btn = insert_toggle(txt)
    print("  %-52s 硬编码 %d → %d 处；补变量 %d；换 var %d；规则修订 %d；浅色块 %s；按钮样式 %s；脚本 %s；按钮 %s"
          % (label or os.path.basename(path), before,
             len(re.findall(r"#[0-9a-fA-F]{6}", txt)), nroot, nvar, fixed,
             "加" if blk else "已存在", "加" if btncss else "已存在", "加" if js else "已存在", "加" if btn else "已有/无"))
    write(path, txt, bom)
    return txt


def contrast(c1, c2):
    def lum(h):
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    a, b = lum(c1), lum(c2)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def check():
    ok = True
    print("① 对比度（WCAG：正文 ≥4.5，次要文字 ≥4.5，强调色 ≥3.0）")
    pairs = [("fg", "bg", 4.5), ("fg", "panel", 4.5), ("fg2", "bg", 4.5), ("dim", "bg", 4.5),
             ("dim", "panel", 4.5), ("blue", "bg", 3.0), ("green", "bg", 3.0), ("red", "bg", 3.0),
             ("amber", "bg", 3.0), ("purple", "bg", 3.0), ("fg", "sel", 4.5), ("fg", "hover", 4.5),
             # 主按钮（浅色下是浅蓝底）—— 文字要够清、描边要够看得见
             ("on-primary", "primary", 4.5), ("primary-line", "bg", 3.0)]
    for a, b, need in pairs:
        r = contrast(LIGHT[a], LIGHT[b])
        good = r >= need
        ok &= good
        print("   %s %-6s on %-6s %5.2f:1（要求 ≥%.1f）%s"
              % ("✅" if good else "❌", a, b, r, need, "" if good else " ← 需要调深"))
    print("② 三个界面各自的主题要素")
    for path, label, need_btn in ((NOTES, "库界面", True), (CONSOLE, "内核控制台", True),
                                  (HOST_GO[0], "主进程页面", True)):
        t = io.open(path, encoding="utf-8").read()
        has = {'浅色块': MARK + '\nhtml[data-theme="light"]' in t,
               '切换脚本': 'shuge-theme' in t and '__shugeTheme' in t,
               '按钮': 'id="btnTheme"' in t,
               '%d 个变量' % len(DARK): all(("--%s" % k) in t for k in DARK),
               '浅色块写全了': all(re.search(r'html\[data-theme="light"\][^}]*--%s\s*:' % re.escape(k), t, re.S)
                               for k in DARK)}
        if need_btn is False:
            has['按钮'] = True
        good = all(has.values())
        ok &= good
        print("   %s %-10s %s" % ("✅" if good else "❌", label,
                                  "、".join("%s%s" % (k, "✓" if v else "✗") for k, v in has.items())))
    print("③ 变量闭环（引用了但没定义的变量 = 会掉色）")
    for path, label in ((NOTES, "库界面"), (CONSOLE, "内核控制台")):
        t = io.open(path, encoding="utf-8").read()
        used = set(re.findall(r"var\((--[\w-]+)\)", t))
        defined = set(re.findall(r"(--[\w-]+)\s*:", t))
        miss = sorted(used - defined)
        ok &= not miss
        print("   %s %-10s 用到 %d 个变量，未定义 %s" % ("✅" if not miss else "❌", label, len(used),
                                                       miss or "无"))
    print("④ 实地取三个界面发出来的页面（跑着的才是真的）")
    import urllib.request
    for url, label in (("http://127.0.0.1:8848/", "库（8848）"),
                       ("http://127.0.0.1:8795/", "内核控制台（8795）"),
                       ("http://127.0.0.1:8790/console", "主进程控制台（8790）")):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                t = r.read().decode("utf-8", "replace")
            got = ("shuge-theme" in t, 'data-theme' in t, 'id="btnTheme"' in t)
            good = all(got)
            ok &= good
            print("   %s %-20s 主题脚本%s 属性%s 按钮%s%s"
                  % ("✅" if good else "❌", label, *["✓" if x else "✗" for x in got],
                     "" if good else "（可能这个界面没起，或还没重启）"))
        except Exception as e:
            print("   ⚠️ %-20s 取不到：%s（界面没起？）" % (label, type(e).__name__))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    if a.check or not a.apply:
        rc = check()
        if not a.apply:
            print("\n（只看现状；要落地加 --apply）")
        return rc
    if a.dry:
        print("干跑：以下文件会被处理（不落盘）")
    print("① 抽变量 + 加浅色块 + 装切换")
    if not a.dry:
        process(NOTES, label="库界面")
        process(CONSOLE, label="内核控制台")
        for p in CONSOLE_JS:
            if os.path.isfile(p):
                process(p, toggle=False, css=False)   # JS：只换 var，不插按钮/:root/浅色块
        for p in HOST_GO:
            if os.path.isfile(p):
                process(p)
    print("② 复查")
    return check()


if __name__ == "__main__":
    sys.exit(main())
