#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""把某个 agent 的提示词/接入单**原样取出来看一眼**（面板上点「生成并接入」拿到的就是这份）。

用法：
    python scripts/show_prompt.py coze            # 打印体检结果 + 章节大纲
    python scripts/show_prompt.py coze --full     # 连正文一起打印
    python scripts/show_prompt.py coze --out data/tmp/coze.md   # 存一份（默认也存 data/tmp/<id>-prompt.md）

体检项（都是**踩过坑**才加进来的）：
  · 有没有残留占位符 {{...}}
  · 有没有空的代码段（`` ）—— Coze 那种"我们不知道配置文件在哪"最容易出这个
  · 有没有 A1.5「格式 × 形状」对照表、第 0 步在不在
它走的是**内核里的那个插件**（面板同一条路），不是直接 import 脚本 —— 免得"我这儿好看、面板上不好看"。
"""
import argparse
import io
import json
import os
import sys
import urllib.request

CORE = os.environ.get("SB_CORE", "http://127.0.0.1:8795")
BT = chr(96)                     # 反引号：写死在源码里容易被 shell/编辑器搅，用 chr 取


def call(tool, args):
    req = urllib.request.Request(
        CORE + "/api/bridge/plugins/call",
        data=json.dumps({"plugin": "plugin.agentctl", "tool": tool, "args": args,
                         "timeoutMs": 60000}, ensure_ascii=False).encode("utf-8"),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("id")
    ap.add_argument("--full", action="store_true", help="把正文也打出来")
    ap.add_argument("--out", default="", help="存到哪（默认 data/tmp/<id>-prompt.md）")
    a = ap.parse_args()
    try:
        r = call("agents.prompt", {"id": a.id})
    except Exception as e:
        print("拿不到提示词：%s: %s（内核在跑吗？%s）" % (type(e).__name__, e, CORE))
        return 1
    d = r.get("data") or {}
    if not d.get("prompt"):
        print("插件没给回正文：%s" % json.dumps(r, ensure_ascii=False)[:400])
        return 1
    text = d["prompt"]
    out = a.out or os.path.join("data", "tmp", "%s-prompt.md" % a.id)
    if not os.path.isdir(os.path.dirname(out)):
        os.makedirs(os.path.dirname(out))
    io.open(out, "w", encoding="utf-8", newline="\n").write(text)

    fence = BT * 3
    nofence = text.replace(fence, "")
    ph = [x for x in __import__("re").findall(r"\{\{[^}]+\}\}", text)]
    print("agent：%s（%s）  类型：%s  长度：%d 字符" % (d.get("name"), d.get("id"), d.get("kind"), len(text)))
    print("存放：%s" % out)
    print("-" * 66)
    print("  %s 没有残留占位符 {{…}}" % ("✅" if not ph else "❌ %s" % ph[:4]))
    print("  %s 没有空的代码段（%s%s）" % ("✅" if BT * 2 not in nofence else "❌", BT, BT))
    print("  %s 有 A1.5「格式 × 形状」对照表" % ("✅" if "#### A1.5" in text else "❌"))
    print("  %s 有第 0 步自测" % ("✅" if ":: 0)" in text else "❌"))
    for key, label in (("### A1 ", "A1 描述文件模板"), ("### A2 ", "A2 四步命令"),
                       ("### A3 ", "A3 自检清单"), ("## 五、", "给主人的转发段"),
                       ("## 六、", "失败对照表 + report 模板")):
        print("  %s %s" % ("✅" if key in text else "❌", label))
    print("-" * 66)
    print("章节：")
    for ln in text.splitlines():
        if ln.startswith("#"):
            print("   " + ln.strip()[:90])
    if a.full:
        print("-" * 66)
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
