#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**多轮模拟检测**：把自测与识别/验收连跑 N 轮，看有没有"跑一遍绿、跑三遍就红"的东西。

辞玖 2026-10-06：「模拟检测几遍」。为什么不能只跑一遍：
  · 有几条自测是**有状态**的（改 config.yaml、建临时 agent、起假插件）—— 第一遍绿不代表可重跑；
  · 识别/验收依赖运行中的平台（组件表、档位、MCP 面），"偶尔绿"和"稳定绿"是两回事；
  · 今晚这类坑真出现过：热加载自测每跑一次会在内核注册表留一行幽灵、自测里的假 agent 会被热加载真拉起来。
所以这个脚本做三件事：**跑 N 轮 → 逐轮逐项记结果 → 把"轮次之间不一致"的项单独点出来**。

用法：
    python scripts\soak_check.py                    # 3 轮：7 套自测 + 假机器模拟 + 面板 + 检测/验收
    python scripts\soak_check.py --rounds 5
    python scripts\soak_check.py --with-hotreload   # 额外把热加载自测也纳入（⚠️ 每轮留一行注册表幽灵，需重启内核清）
    python scripts\soak_check.py --quick            # 只跑检测/验收（不跑自测）
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
PY = sys.executable
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SMOKES = ["smoke_mcphosts", "smoke_handoff", "smoke_prompt_speed", "smoke_tier",
          "smoke_agent_guard", "check_no_windows", "check_ui_effects"]
GREEN_HINT = ("全部通过", "通过（", "通过")


def run(argv, timeout=1800):
    r = subprocess.run(argv, cwd=CORE, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", creationflags=FLAGS, timeout=timeout)
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode, out


def last_meaningful(out):
    for ln in reversed(out.strip().split("\n")):
        s = ln.strip()
        if s and ("通过" in s or "失败" in s or "不过" in s):
            return s[:80]
    return (out.strip().split("\n")[-1][:80] if out.strip() else "")


def http_json(path, timeout=20):
    with urllib.request.urlopen("http://127.0.0.1:8795" + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


GHOST_EXPECTED = False          # 由 --with-hotreload 置位：热加载自测每轮必留一行幽灵


def detection_round():
    """识别 + 验收（一轮）→ {项: (ok, 摘要)}"""
    res = {}
    rc, out = run([PY, os.path.join(HERE, "agentctl.py"), "scan"], timeout=600)
    agents = out.count("目录 ") + out.count("文件 ") + out.count("可执行 ")
    res["scan 列得出候选/已知"] = (rc == 0 and "agent" in out.lower(), "exit=%s" % rc)
    rc, out = run([PY, os.path.join(HERE, "agentctl.py"), "list"], timeout=600)
    rows = [l for l in out.split("\n") if l.strip() and ("✅" in l or "·" in l)]
    res["list 五行都在"] = (rc == 0 and len(rows) >= 5, "行数=%d" % len(rows))
    for aid in ("dsh", "codex", "workbuddy", "reasonix", "claude-code"):
        rc, out = run([PY, os.path.join(HERE, "agentctl.py"), "verify", aid], timeout=600)
        ok = (rc == 0 and "全部通过" in out)
        res["verify %s" % aid] = (ok, last_meaningful(out))
    try:
        h = http_json("/healthz")
        c = http_json("/api/components")
        ids = [x.get("id") for x in c] if isinstance(c, list) else []
        res["内核在跑且组件齐"] = (bool(h.get("ok")) and len(ids) >= 13,
                                  "components=%d probes=%s" % (len(ids), (h.get("probe") or {}).get("by_state")))
        ghosts = [i for i in ids if "hotreload" in i]
        if GHOST_EXPECTED:
            # 热加载自测的预期后果（注册表条目只能靠重启内核清）—— 标成 ⚠️ 而不是 ❌，
            # 否则每次带 --with-hotreload 都"必红"，看多了就会把真问题也忽略掉。
            res["组件里的测试幽灵（预期）"] = (True, "预期 %d 行：%s（重启内核即清）"
                                        % (len(ghosts), ghosts or "无"))
        else:
            res["组件里没有测试幽灵"] = (not ghosts, ghosts or "干净")
    except Exception as e:
        res["内核在跑且组件齐"] = (False, "%s: %s" % (type(e).__name__, e))
        res["组件里没有测试幽灵"] = (False, "拿不到组件表")
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--with-hotreload", action="store_true")
    ap.add_argument("--quick", action="store_true", help="只跑检测/验收")
    a = ap.parse_args()

    global GHOST_EXPECTED
    suites = [] if a.quick else list(SMOKES) + ["sim_foreign"]
    if a.with_hotreload:
        suites.append("smoke_hotreload")
        GHOST_EXPECTED = True
        print("⚠️ 带了 --with-hotreload：每轮会在内核注册表留一行 hotreload 幽灵，"
              "跑完请重启内核清掉（cold restart 演练会顺手清）")
    suites.append("panel_selftest(js)")

    matrix = {}          # 项 → [每轮结果]
    t0 = time.time()
    for rnd in range(1, a.rounds + 1):
        print("═" * 74)
        print("第 %d / %d 轮" % (rnd, a.rounds))
        for s in suites:
            t = time.time()
            if s.startswith("panel_selftest"):
                rc, out = run(["node", os.path.join(CORE, "web", "_agent_panel_selftest.js")], timeout=600)
            else:
                rc, out = run([PY, os.path.join(HERE, s + ".py")], timeout=2400)
            ok = (rc == 0)
            tail = last_meaningful(out)
            matrix.setdefault(s, []).append((ok, tail, time.time() - t))
            print("   %s %-22s %5.1fs  %s" % ("✅" if ok else "❌", s, time.time() - t, tail))
        for k, (ok, tail) in detection_round().items():
            matrix.setdefault("检测·" + k, []).append((ok, tail, 0.0))
            print("   %s %-22s        %s" % ("✅" if ok else "❌", "检测·" + k, tail))

    print("═" * 74)
    print("汇总（%d 轮，用时 %.0f 秒）" % (a.rounds, time.time() - t0))
    flaky, red = [], []
    for k, rows in matrix.items():
        oks = [x[0] for x in rows]
        if all(oks):
            print("   ✅ %-26s %d/%d 全绿" % (k, sum(oks), len(oks)))
        elif any(oks):
            flaky.append(k)
            print("   ⚠️ %-26s %d/%d —— **轮次之间不一致**：%s"
                  % (k, sum(oks), len(oks), [x[1] for x in rows if not x[0]]))
        else:
            red.append(k)
            print("   ❌ %-26s %d/%d 全红：%s" % (k, sum(oks), len(oks), rows[0][1]))
    print("═" * 74)
    if red:
        print("结论：有 %d 项**每轮都失败**：%s" % (len(red), "、".join(red)))
        return 1
    if flaky:
        print("结论：有 %d 项**时好时坏**（这才是要多跑几遍的原因）：%s" % (len(flaky), "、".join(flaky)))
        return 2
    print("结论：全部项 %d 轮全绿（可重跑、不依赖残留状态）" % a.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
