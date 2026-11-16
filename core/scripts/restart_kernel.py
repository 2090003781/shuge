#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**正经重启内核**：停 → 等它真退干净 → 起 → 等就绪。比 `agentctl activate` 更有耐心。

为什么单独一个（2026-10-06 实测）：
  · 内核**优雅停机要 60~90 秒**（插件一个个收：06:37:57 收到停止 → 06:39:30 才"平台已停止"）；
  · `agentctl activate` 只等 30 秒就开始 start，会撞上启停台的"已有动作在执行中"，白试几轮；
  · 启停台那边 start 被挡时**不会自己重试**，人在面板上点一下会以为"重启了"，其实内核没动
    （今天就这样骗过一次：核心进程还是 10 分钟前那个，pid 都没变）。
所以：等到进程真消失；start 被"动作在执行中"挡了就地重试；最后以 /healthz 为准报结论。

用法：
    python scripts\restart_kernel.py            # 停+起，打印前后 pid
    python scripts\restart_kernel.py --status   # 只看现在状态，不动
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

HOST = "http://127.0.0.1:8790"      # 启停台（core-host.exe）
CORE = "http://127.0.0.1:8795"      # 内核


def post(base, path, body=None, timeout=60):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, "%s: %s" % (type(e).__name__, e)


def get(base, path, timeout=10):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return {}


def core_pids():
    try:
        import psutil
    except Exception:
        return None
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info.get("name") or "").lower() == "core.exe":
                out.append(p.info["pid"])
        except Exception:
            pass
    return out


def status(tag=""):
    h = get(CORE, "/healthz", timeout=5)
    pids = core_pids()
    line = "内核 %s：%s" % (tag, ("pid=%s uptime=%ss 进程=%s" % (h.get("pid"), h.get("uptime_s"),
                                                              h.get("processes"))) if h else "❌ 不可达")
    if pids is not None:
        line += "  进程表里的 core.exe：%s" % (pids or "无")
    print("   " + line)
    return h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="只看状态，不动")
    ap.add_argument("--wait-stop", type=int, default=240, help="等停干净的上限秒数（默认 240）")
    ap.add_argument("--wait-up", type=int, default=180, help="等就绪的上限秒数（默认 180）")
    a = ap.parse_args()

    before = status("现在")
    if a.status:
        return 0 if before else 1

    print("① 停（走启停台 /api/core/stop；优雅停机实测 60~90 秒）")
    st, txt = post(HOST, "/api/core/stop")
    print("   回执 %s %s" % (st, txt.strip()[:120]))
    old = before.get("pid")
    t0 = time.time()
    while time.time() - t0 < a.wait_stop:
        time.sleep(0.5)
        pids = core_pids()
        if pids is not None and not pids:
            print("   已退干净（等了 %.0f 秒）" % (time.time() - t0))
            break
        if pids is None and not get(CORE, "/healthz", timeout=3):
            print("   端口已关（等了 %.0f 秒；没 psutil 就按端口判）" % (time.time() - t0))
            break
    else:
        print("   ⚠️ %d 秒还没退干净（core.exe=%s）—— 仍然往下走，start 会自己兜" % (a.wait_stop, core_pids()))

    print("② 起（被\"已有动作在执行中\"挡就地重试，每 15 秒一轮）")
    ok = False
    for k in range(10):
        st, txt = post(HOST, "/api/core/start")
        print("   第 %d 轮 → %s %s" % (k + 1, st, txt.strip()[:120]))
        if '"ok":true' in txt or st == 200 and "true" in txt:
            ok = True
            break
        time.sleep(15)
    if not ok:
        print("   ❌ start 十轮都没被受理 —— 看 data/logs/host.log")

    print("③ 等就绪（最多 %d 秒）" % a.wait_up)
    t0 = time.time()
    while time.time() - t0 < a.wait_up:
        time.sleep(0.5)
        h = get(CORE, "/healthz", timeout=4)
        if h:
            print("   ✅ 内核回来了：pid=%s uptime=%ss 进程=%s（等了 %.0f 秒）"
                  % (h.get("pid"), h.get("uptime_s"), h.get("processes"), time.time() - t0))
            if old and h.get("pid") == old:
                print("   ⚠️ pid 跟重启前一样（%s）—— 这次重启**没落到内核上**，别以为清了" % old)
            return 0
    print("   ❌ %d 秒未就绪 —— 看 data/logs/core.log" % a.wait_up)
    return 1


if __name__ == "__main__":
    sys.exit(main())
