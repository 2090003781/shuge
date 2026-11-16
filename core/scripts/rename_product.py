#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""改名工具：把产品名/标识从「第二大脑 · secondbrain · ai-notes」搬到「枢阁 · core」。

2026-10-06 辞玖定的名字：中文 **枢阁**，英文/目录 **core**（"枢"＝core 的核心义，"阁"＝藏书阁）。

分两层做（这是本工具的 `--layer` 参数）：
  · `text`（默认，**文字层**，低风险）：只改"人看得见的名字"——
      `第二大脑` → `枢阁`；品牌图标 `🧠 第二大脑` → `🏛 枢阁`、`🧠 控制台` → `🏛 控制台`。
      注意**不碰**语义图标（例：`web/app.js` 里 `🧠 代理的身份` 表示"身份"那个图标，不是品牌）。
  · `ident`（**标识/路径层**，高风险，**默认只干跑**）：`secondbrain-core` → `core`、
      `secondbrain.exe` → `core-host.exe`、`SecondBrain`/`secondbrain` 标识符、写死的
      `D:\secondbrain-core` 路径、`ai-notes` → `core-notes`、计划任务 `secondbrain-core-watchdog`……
      这一层会动到**运行中的绑定**（四个 agent 的 MCP 条目、计划任务、快捷方式），所以：
      工具只负责改文件，**搬目录/改计划任务/重接 agent 由人按清单做**，见输出末尾的清单。

为什么要写成工具而不是一把 sed：
  · 仓库里有 **UTF-8 BOM 的 .ps1**（PowerShell 5.1 读无 BOM 的中文会按 GBK 解释 → 乱码），
    必须**原样保留编码与 BOM**，否则改完名字把脚本改成乱码；
  · 有读不出 UTF-8 的文件（二进制/GBK），必须**跳过并报出来**，不能写坏；
  · 改名必须**可核对**：干跑先给"哪个文件改几处"，改完再断言"旧名 0 处"。

用法：
    python scripts\rename_product.py                     # 文字层干跑（只报告）
    python scripts\rename_product.py --apply             # 文字层真改
    python scripts\rename_product.py --layer ident       # 标识层干跑（默认不给 --apply 就只报告）
    python scripts\rename_product.py --layer ident --apply
"""
import argparse
import io
import os
import sys

ROOTS = [r"D:\secondbrain-core", r"D:\ai-notes"]
SKIP_DIRS = {"data", ".git", "__pycache__", "bin", ".pytest_cache", ".mypy_cache"}
SKIP_EXT = {".exe", ".dll", ".pyc", ".zip", ".db", ".sqlite", ".png", ".jpg", ".ico",
            ".woff", ".woff2", ".ttf", ".pdf", ".docx"}
# ⚠️ **排除自己**：本文件里存着"旧名→新名"的映射规则，改它等于把映射表擦掉
#    （干跑时它自己命中 11 处就是这么来的）。改名工具必须永远能重跑。
SKIP_NAMES = {"rename_product.py"}

# 文字层：顺序有意义（先把 🧠 第二大脑 变成品牌图标，再单独换图标组合）
TEXT_RULES = [
    ("🧠 第二大脑", "🏛 枢阁"),
    ("🧠 控制台", "🏛 控制台"),
    ("第二大脑", "枢阁"),
]

# 标识/路径层：**长串在前、转义变体在前**（先 secondbrain-core 再 secondbrain，免得半路截断）
IDENT_RULES = [
    # ── 平台目录 / 模块 / 可执行件 / 计划任务 ──
    ("secondbrain-core-watchdog", "core-watchdog"),
    (r"D:\\\\secondbrain-core", r"D:\\\\core"),        # JS/JSON 里 4 个反斜杠那种
    (r"D:\\secondbrain-core", r"D:\\core"),            # 源码里 2 个反斜杠
    (r"D:\secondbrain-core", r"D:\core"),              # 普通 1 个反斜杠（Go 反引号串最常见）
    ("D:/secondbrain-core", "D:/core"),
    (r"cmd\\secondbrain", r"cmd\\core-host"),
    (r"cmd\secondbrain", r"cmd\core-host"),
    ("cmd/secondbrain", "cmd/core-host"),
    ("secondbrain-core", "core"),                      # go.mod 模块名 + 所有 import
    ("secondbrain.exe.bak", "core-host.exe.bak"),
    ("secondbrain.exe", "core-host.exe"),
    # ── 契约名（都在仓库内，两边一起改就一致）──
    ("SECONDBRAIN_", "COREHOST_"),
    ("secondbrain-host", "core-host"),                 # 锁名 / role 字符串
    ("secondbrain-locks", "core-locks"),
    ("secondbrain", "core-host"),                      # 兜底（注释/Get-Process 等）
    # ── 笔记程序目录（**只动路径**，保护清单见 PROTECT）──
    (r"D:\\\\ai-notes", r"D:\\\\core-notes"),
    (r"D:\\ai-notes", r"D:\\core-notes"),
    (r"D:\ai-notes", r"D:\core-notes"),
    ("D:/ai-notes", "D:/core-notes"),
    (r"C:\ai-notes", r"C:\core-notes"),
    # ⚠️ 别写宽松的 '"ai-notes/'：加密常量 b"ai-notes/vault-…" 前面也是引号，会被误伤
    #    （2026-10-06 真发生过，PROTECT 断言当场逮住）。只匹配打包脚本里那两条明确的相对路径。
    ('"ai-notes/src/', '"core-notes/src/'),
    ('"ai-notes/web/', '"core-notes/web/'),
    ('os.path.join(root, "ai-notes")', 'os.path.join(root, "core-notes")'),
    # ── 显示文案 ──
    ("ai-notes 库接口", "枢阁 库接口"),
]

# 保护清单：改完必须**原样还在**（数量一致）。这些都是"数据/契约"级的字符串，
# 改了会解不开已加密的库、或与既有导出件/测试不兼容。
PROTECT = [
    "ai-notes/vault-unlock-check/v1",   # 私密库解锁校验（vaultcrypto）
    "ai-notes/vault-box/v1",            # 私密库加密 BOX info
    ".ai-notes-export.json",            # 导出件的元数据文件名
    "ai-notes-export/1",                # 导出格式标识
    "ai-notes-ribbon",                  # 前端 localStorage 键
    'prefix="ai-notes-',                # 临时目录前缀（纯装饰，不动）
    '"ai-notes"',                       # 测试里的标签值（smoke_fm_parse 断言它）
    "AI_NOTES_DB",                      # 环境变量（外部契约，不动）
]


BOM = b"\xef\xbb\xbf"


def read_text(path):
    """原样读：返回 (文本, 编码, 有无 BOM)；读不出 UTF-8 就返回 (None, why, False)

    ⚠️ 2026-10-06 踩过：探测时若把 `utf-8-sig` 放前面并据此判"有 BOM"，**没有 BOM 的文件也会被判成有**
    （utf-8-sig 对两者都能解码），于是 write_text 给 85 个文件全补了 BOM —— go.mod 加了 BOM 直接编不过。
    正确做法：**只看头三个字节**。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception as e:
        return None, "读失败:%s" % e, False
    bom = raw.startswith(BOM)
    body = raw[3:] if bom else raw
    try:
        txt = body.decode("utf-8")
    except UnicodeDecodeError:
        return None, "不是 UTF-8（可能是 GBK/二进制）", bom
    if "\x00" in txt:                               # 含 NUL = 二进制，别碰
        return None, "看起来是二进制", bom
    return txt, "utf-8", bom


def write_text(path, txt, enc, bom):
    data = txt.encode(enc)
    if bom and not data.startswith(b"\xef\xbb\xbf"):
        data = b"\xef\xbb\xbf" + data
    with open(path, "wb") as f:
        f.write(data)


def walk():
    for root in ROOTS:
        if not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in SKIP_DIRS]
            for f in fns:
                if os.path.splitext(f)[1].lower() in SKIP_EXT or ".bak" in f:
                    continue
                if f in SKIP_NAMES:
                    continue
                yield os.path.join(dp, f)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--layer", choices=["text", "ident"], default="text")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    rules = TEXT_RULES if a.layer == "text" else IDENT_RULES
    probe = TEXT_RULES[2][0] if a.layer == "text" else "secondbrain-core"

    hits, skipped, total = [], [], 0
    for p in walk():
        txt, enc, bom = read_text(p)
        if txt is None:
            with open(p, "rb") as f:
                if probe.encode("utf-8") in f.read():
                    skipped.append((p, enc))
            continue
        n = sum(txt.count(k) for k, _ in rules)
        if n:
            hits.append((p, n, txt, enc, bom))
            total += n
    rel = lambda p: os.path.relpath(p, os.path.dirname(ROOTS[0]) if p.startswith(ROOTS[0]) else
                                    os.path.dirname(ROOTS[1]) if p.startswith(ROOTS[1]) else p)
    print("层=%s%s  命中 %d 个文件 / %d 处" % (a.layer, "" if a.apply else "（干跑）", len(hits), total))
    if a.layer == "ident":
        per = {k: 0 for k, _ in rules}
        for _p, _n, t, _e, _b in hits:
            for k, _v in rules:
                per[k] += t.count(k)
        print("   各规则命中：")
        for k, _v in rules:
            if per[k]:
                print("      %-34s %d" % (k.replace("\\", "\\"), per[k]))
    for p, n, _t, _e, _b in sorted(hits, key=lambda x: -x[1])[:30]:
        print("   %-64s %d 处" % (rel(p), n))
    if len(hits) > 30:
        print("   …… 还有 %d 个文件" % (len(hits) - 30))
    if skipped:
        print("   ⚠️ 跳过了 %d 个含旧名但读不出 UTF-8 的文件（没动它们）：" % len(skipped))
        for p, why in skipped[:8]:
            print("      %s（%s）" % (rel(p), why))
    if not a.apply:
        print("\n（干跑，什么都没写。要真改加 --apply）")
        return 0

    changed = 0
    for p, _n, txt, enc, bom in hits:
        new = txt
        for k, v in rules:
            new = new.replace(k, v)
        if new != txt:
            write_text(p, new, enc, bom)
            changed += 1
    print("\n已改 %d 个文件。" % changed)

    # 改完自己验一遍：同层旧名应当 0 处
    left = []
    for p in walk():
        txt, _enc, _bom = read_text(p)
        if txt is None:
            continue
        n = sum(txt.count(k) for k, _ in rules)
        if n:
            left.append((rel(p), n))
    if left:
        print("❌ 还有旧名残留 %d 个文件：%s" % (len(left), left[:8]))
        return 1
    if a.layer == "ident":
        bad = []
        for lit in PROTECT:
            n0 = sum(t.count(lit) for _p, _n, t, _e, _b in hits)
            n1 = 0
            for p in walk():
                t, _e, _b = read_text(p)
                if t:
                    n1 += t.count(lit)
            if n1 < n0:
                bad.append("%s（改前 %d → 改后 %d）" % (lit, n0, n1))
        if bad:
            print("❌ 保护清单被动了：%s" % "；".join(bad))
            return 1
        print("✅ 保护清单原样（加密常量/导出格式/前端键/测试标签/环境变量）")
    print("✅ 同层旧名已清零（%s）" % "、".join(k for k, _ in rules))
    return 0


if __name__ == "__main__":
    sys.exit(main())
