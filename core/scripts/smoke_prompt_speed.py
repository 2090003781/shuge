#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提示词速度自测：钉住"点一下不该等好几秒"。

背景（2026-10-05 辞玖报"reasonix 的接入手工单点击出现了明显延迟"）：
  点击候选 agent 的「接入手工单」原来会去调**全量 scan_all()** → 实测 4.9 秒；
  拆开是 `_status_of` 每个 agent 各打两次 HTTP（1768ms）+ 满盘 glob 的模糊发现（885ms）+ `claude mcp list` 起一次 CLI（1500ms）。
  修完：只查目标那一个（find_row）、模糊发现缓存 60s、claude 名单缓存 30s、scan_all 里 comp/perm 只取一次。

跑：python scripts\\smoke_prompt_speed.py      退出码 0 = 达标
"""
import os
import sys
import time

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CORE, "scripts"))
import agentctl as AC  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  ← " + str(extra)))
    if not cond:
        fails.append(name)


def ms(fn):
    t0 = time.time()
    v = fn()
    return (time.time() - t0) * 1000, v


print("提示词速度自测")
print("-" * 56)
# 预热（第一次本来就要建缓存：claude 名单、模糊发现）
AC.scan_all()

d, r = ms(lambda: AC.find_row("coze"))
check("find_row(候选) < 300ms（只查一个，不跑全量）", d < 300, "%.0f ms" % d)
check("find_row 回来的是候选行（有证据、没描述文件）",
      bool(r) and r.get("has_desc") is False and bool(r.get("evidence")), r and r.get("id"))

d, _ = ms(lambda: AC.claude_mcp_names())
check("claude 名单第二次命中缓存（<50ms，原来要起一次 CLI ~1.5s）", d < 50, "%.0f ms" % d)

d, rows = ms(lambda: AC.scan_all())
check("scan_all（暖）< 1500ms（原来是 4.4 秒）", d < 1500, "%.0f ms" % d)
check("scan_all 行数正常（>=8）", len(rows) >= 8, len(rows))

d, _ = ms(lambda: AC.make_onboard_prompt(AC.find_row("coze")))
check("生成一份手工单 < 400ms", d < 400, "%.0f ms" % d)

print("-" * 56)
if fails:
    print("失败 %d 条：%s" % (len(fails), "、".join(fails)))
    sys.exit(1)
print("全部通过")
