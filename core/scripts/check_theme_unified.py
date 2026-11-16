#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**主题统一自检**：一条命令验"所有页面主题统一"这件事有没有真的成立。

辞玖 2026-10-06：「①所有页面主题统一 ②图片中的问题是只页签能切换主题，其他地方没变」。

五道检查（都可重跑）：
  ① 脚本卫生：`scripts/theme_boot.js` **不许有反引号 / %**（它要内联进 Go 的反引号原始字符串，
     出过事故：反引号把 Go 字符串截断，宿主编不出来，而 python/node 检查全绿）+ `node --check` 通过；
  ② **单测**：`node web/_theme_selftest.js` —— 桩 DOM 验"点击切换后每个 iframe 的 <html> 都跟着变"
     /"收到 message 只应用不回推（防循环）"/"被嵌时跟随父页面并隐藏自己的按钮"
     /**"两个页面互推不会无限循环"（2026-10-06 振荡事故的回归用例）**；
  ③ **页面标记**：三个界面（磁盘上的源文件 **和** 跑着的页面）都要有那五样：
     服务端接口、iframe 直写、postMessage、跟随父页面、被嵌隐藏；
  ④ **服务端唯一真相**：`GET/POST /api/ui/theme` 往返 + 非法值被拒（测完把原值写回）；
  ⑤ 汇总。

用法：`python scripts\check_theme_unified.py`
"""
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
API = "http://127.0.0.1:8790/api/ui/theme"
BOOT_JS = os.path.join(HERE, "theme_boot.js")
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

PAGES = [(os.path.join(CORE, "web", "index.html"), "内核控制台(源)"),
         (os.path.join(CORE, "cmd", "core-host", "page.go"), "主程序页(源)"),
         (os.path.join(CORE, "cmd", "core-host", "kernelpage.go"), "内核页(源)"),
         (os.path.join(os.path.dirname(CORE), "core-notes", "web", "index.html"), "库(源)")]
LIVE = [("http://127.0.0.1:8790/console", "主程序页(跑着)"),
        ("http://127.0.0.1:8795/", "内核控制台(跑着)"),
        ("http://127.0.0.1:8848/", "库(跑着)")]
MARKS = [("/api/ui/theme", "服务端接口"), ("contentDocument", "iframe 直写"),
         ("postMessage", "postMessage"), ("parentTheme", "跟随父页面"), ("hideIfEmbedded", "被嵌隐藏"),
         ("EventSource", "实时通道(SSE)"), ("theme-btn", "主题按钮样式类")]

fails = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  ← %s" % str(extra)[:160]) if (not cond and extra) else ""))
    if not cond:
        fails.append(name)


def http(method="GET", theme=None):
    data = json.dumps({"theme": theme}).encode("utf-8") if theme is not None else None
    req = urllib.request.Request(API, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8", "replace")[:120]}


def main():
    print("① 脚本卫生（要内联进 Go 原始字符串，反引号 / %% 都不许有）")
    js = io.open(BOOT_JS, encoding="utf-8").read()
    check("theme_boot.js 没有反引号", "`" not in js, "有反引号会把 Go 字符串截断（出过事故）")
    check("theme_boot.js 没有 %", "%" not in js, "Go 侧过 Sprintf 会当格式动词")
    r = subprocess.run(["node", "--check", BOOT_JS], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    check("node --check 通过", r.returncode == 0, (r.stderr or "")[:160])

    print("② 单测（点击 → iframe 跟随 / message 不回推 / 双页互推不振荡 / 同源多标签 / **SSE 实时**）")
    # ②-1 静态防线：push 只许在 apply 的"用户动作"分支里（2026-10-06 就是它跑出去才无限互推的）
    # 去注释后全文件只许有一处 push(t)；且它必须落在 apply 的 if (!fromElsewhere) { 里
    nose = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    nose = re.sub(r"//[^\n]*", "", nose)
    n_all = len(re.findall(r"\bpush\(t\);", nose))          # 只看"调用"（function push(t) 不算）
    i_apply = nose.find("function apply(t, fromElsewhere)")
    seg = nose[i_apply:i_apply + 500] if i_apply >= 0 else ""
    in_branch = ("if (!fromElsewhere) {" in seg
                 and seg.index("push(t);") > seg.index("if (!fromElsewhere) {")) if "push(t);" in seg else False
    check("push(t) 全文件只有一处（就是用户点的那一下）", n_all == 1, "去注释后出现 %d 处" % n_all)
    check("这一处落在 apply 的用户动作分支里（收到推送不回推）", in_branch,
          "不在 if (!fromElsewhere) 里 = 又会出现两页互推自闪")
    r = subprocess.run(["node", os.path.join(CORE, "web", "_theme_selftest.js")],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=FLAGS)
    out = (r.stdout or "") + (r.stderr or "")
    npass = out.count("PASS ")
    check("单测全过（%d 项）" % npass, r.returncode == 0 and npass >= 30, out.strip().split("\n")[-1])
    for must in ["⑥ 一次点击的消息会停下来", "⑥ 两个页面收敛到同一个主题", "⑥ 同值推送被短路",
                 "⑦ 收到广播的一方", "⑧ 流推来 light", "⑧ 同值推送被短路"]:
        check("单测跑到并过了：%s" % must, ("PASS " + must) in out, "单测里少了这条（防振荡用例被删掉）")

    print("③ 页面标记（源文件 + 跑着的页面都要有）")
    for p, label in PAGES:
        if not os.path.isfile(p):
            check("%s 存在" % label, False, p)
            continue
        t = io.open(p, encoding="utf-8").read()
        miss = [n for k, n in MARKS if k not in t]
        check("%s：五样齐" % label, not miss, "缺：%s" % miss)
    for url, label in LIVE:
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                t = resp.read().decode("utf-8", "replace")
            miss = [n for k, n in MARKS if k not in t]
            check("%s：五样齐" % label, not miss, "缺：%s" % miss)
        except Exception as e:
            check("%s：取得到" % label, False, "%s: %s" % (type(e).__name__, e))

    print("④ 服务端唯一真相（GET/POST 往返 + 非法值被拒）")
    st, d = http()
    if st != 200:
        check("GET /api/ui/theme", False, "%s %s（主程序没起？）" % (st, d))
    else:
        orig = d.get("theme")
        check("GET 回 theme", orig in ("light", "dark"), d)
        st2, d2 = http("POST", "light")
        st3, d3 = http()
        check("POST light → GET light", st2 == 200 and d3.get("theme") == "light", (d2, d3))
        st4, _ = http("POST", "rainbow")
        check("非法值被拒（HTTP 400）", st4 == 400, st4)
        http("POST", orig)
        st5, d5 = http()
        check("测完还原成原值（%s）" % orig, d5.get("theme") == orig, d5)

    print("⑤ 汇总")
    if fails:
        print("   ❌ %d 项不过：%s" % (len(fails), "、".join(fails[:6])))
        return 1
    print("   ✅ 主题统一：一处切换，主程序页 / 库 / 内核控制台（含同页 iframe）都跟着变")
    print("   ✅ 且不会自己来回变：只有用户点的那一下才往外推，收到推送的一方到此为止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
