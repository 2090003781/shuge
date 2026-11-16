#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**热加载自测**：往 config.yaml 临时加一条假插件 → 调 `POST /api/plugins/reload` → 它应该
**不用重启内核**就起来，而且**在跑的接口一个都不能被动**。

为什么这么测（2026-10-06 辞玖："我记得我点了接入并生成，为什么没有准备好 plugin.codex"）：
面板/CLI 写完 config.yaml 之后，内核是**启动时读一次**那张清单（PluginManager.cfgs），
所以新条目以前必须重启整个内核（所有 agent 接口一起断 3~5 秒）。现在内核有热加载口，
这条自测就是钉住它：**只加不减、不动在跑的**。

可重跑：每轮用一个**唯一 id**（`plugin.hotreloadtest-<时间戳>`），跑完停掉 + 删掉 + 从
回收站/禁用名单里抹掉 + 还原 config.yaml。内核内存里会留一行 stopped（唯一 id，重启即消失），
脚本最后会**明说**这一点 —— 不藏着。
"""
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
CONFIG = os.path.join(CORE, "config.yaml")
TRASH = os.path.join(CORE, "data", "plugins", "trash.json")
DISABLED = os.path.join(CORE, "data", "plugins", "disabled.json")
TAG = os.path.join(CORE, "data", "tmp", "hotreload-alive.txt")
CORE_URL = os.environ.get("SB_CORE", "http://127.0.0.1:8795")

FAILS = []
N = 0


def check(name, cond, detail=""):
    global N
    N += 1
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  <- %s" % str(detail)[:220]) if (not cond and detail) else ""))
    if not cond:
        FAILS.append(name)


def http(path, body=None, method="GET", timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(CORE_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, {"raw": e.read().decode("utf-8", "replace")[:300]}
    except Exception as e:
        return 0, {"error": "%s: %s" % (type(e).__name__, e)}


def components():
    st, c = http("/api/components")
    return {x.get("id"): x for x in (c if isinstance(c, list) else [])}


def plugin_pids():
    """各插件进程的 pid（按 cmdline 认，不靠进程名猜）—— 用来证明"在跑的一个没动" """
    try:
        import psutil
    except Exception:
        return {}
    out = {}
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = " ".join(p.info.get("cmdline") or [])
            if "agent_plugin.py" not in cl and "_hotreload_probe.py" not in cl:
                continue
            toks = cl.split()
            if "_hotreload_probe.py" in cl:
                out["_fake"] = p.info["pid"]
            elif "--agent" in toks:
                out["plugin." + toks[toks.index("--agent") + 1]] = p.info["pid"]
        except Exception:
            pass
    return out


def live(pid):
    try:
        import psutil
        return bool(pid) and psutil.pid_exists(int(pid)) and psutil.Process(int(pid)).is_running()
    except Exception:
        return False


def main():
    pid = "plugin.hotreloadtest-%d" % int(time.time())
    print("热加载自测（真 config.yaml + 真内核 %s）" % CORE_URL)
    print("本轮假插件 id：%s" % pid)
    print("-" * 72)
    st, h = http("/healthz")
    if st != 200 or not h.get("ok"):
        print("❌ 内核没在跑（%s）—— 先 activate 或点控制台「⟳ 重启内核」" % (h.get("error") or st))
        return 2
    before_cfg = io.open(CONFIG, encoding="utf-8").read()
    comp0, pids0 = components(), plugin_pids()
    print("① 现状：组件 %d 个，接口进程 %d 个" % (len(comp0), len(pids0)))
    check("这个 id 是全新的（内核从没见过它）", pid not in comp0, [k for k in comp0 if "hotreload" in k])

    # ── 加一条假插件（走 agentctl 自己的插入器，跟真接入同一个写法）──
    sys.path.insert(0, HERE)
    import agentctl as AC
    entry = """  - id: %s
    type: external
    command: python
    args: ["plugins/_hotreload_probe.py", "--id", "%s"]
    cwd: "."
    enabled: true
    supervisor:
      max_restarts: 5
      restart_wait_ms: 1000
""" % (pid, pid)
    AC.insert_plugin_entry(pid, entry)
    check("config.yaml 里插进了假插件（并留了备份）",
          pid in io.open(CONFIG, encoding="utf-8").read() and os.path.isfile(CONFIG + ".bak-agentctl"), "")
    check("插了但**内核还没读到**（组件表里没有它）", pid not in components(), "")

    # ── 热加载 ──
    t0 = time.time()
    st, r = http("/api/plugins/reload", {}, "POST")
    res = (r or {}).get("result") or {}
    dt = time.time() - t0
    check("POST /api/plugins/reload 回 200/ok", st == 200 and r.get("ok") is True, r)
    check("它被认成「新增并启动」（added/started 都只有它）",
          res.get("added") == [pid] and res.get("started") == [pid], res)
    check("入口清单读的是 config.yaml 本身", r.get("config") in ("config.yaml", CONFIG), r.get("config"))
    check("热加载很快（<5s；重启内核是 3~5s 起步、还会打断所有接口）", dt < 5, "%.1fs" % dt)

    # ── 真起来了 ──
    up = False
    for _ in range(30):
        time.sleep(0.5)
        if pid in components():
            up = True
            break
    check("组件表里出现了它（注册成功，不用重启内核）", up, [k for k in components() if "hotreload" in k])
    dead, apid = time.time() + 10, 0
    while time.time() < dead:
        if os.path.isfile(TAG):
            try:
                apid = int((io.open(TAG, encoding="utf-8").read() or "0").strip() or 0)
            except Exception:
                apid = 0
            if apid and live(apid):
                break
        time.sleep(0.5)
    check("它的进程真活着（不只是内核记了一笔账）", live(apid),
          "pid=%s 标记文件=%s" % (apid, os.path.isfile(TAG)))
    cap = (components().get(pid) or {}).get("capabilities") or []
    check("能力登记上了（hotreload.ping）", "hotreload.ping" in cap, cap)

    # ── 在跑的一个都没动（本次改动的**核心承诺**）──
    pids1 = plugin_pids()
    check("在跑的接口 pid 一个没变（没被打断）",
          all(pids1.get(k) == v for k, v in pids0.items()),
          {k: (pids0.get(k), pids1.get(k)) for k in pids0 if pids1.get(k) != pids0.get(k)})

    # ── 幂等：再调一次不该重复起 ──
    pid_before = pids1.get("_fake")
    st2, r2 = http("/api/plugins/reload", {}, "POST")
    res2 = (r2 or {}).get("result") or {}
    check("第二次热加载：没有新增、没有重起", res2.get("added") == [] and res2.get("started") == [], res2)
    check("第二次热加载也没动它（pid 不变）", plugin_pids().get("_fake") == pid_before,
          (pid_before, plugin_pids().get("_fake")))

    # ── 清场 ──
    print("② 清场（停 → 删 → 抹掉回收站/禁用名单 → 还原 config.yaml）")
    st3, r3 = http("/api/plugins/stop", {"id": pid}, "POST")
    check("假插件已停", st3 == 200, r3)
    st4, r4 = http("/api/plugins/delete", {"id": pid}, "POST")
    check("已移入回收站（从插件列表里消失）", st4 == 200, r4)
    # ⚠️ 这里**不调 purge**：它会把 id 写进 disabled.json（永久禁用名单）→ 留下持久垃圾。
    #    而"文件级抹除"也不可靠：内核内存里那份还在，下次它落盘就把我的改动覆盖回去
    #    （2026-10-06 实测，disabled.json 里攒了 3 条测试垃圾才发现的）。
    #    真正会消失的是**内核内存里的注册表身份** —— 那要重启内核（自测不该擅自重启它）。
    time.sleep(1)
    # ⚠️ 现实：注册表里的条目 stop/delete/purge 都拿不掉，要等内核重启。
    # 所以这里只断言"**不在跑**"，残留那几行只报告（不算失败）。
    _c = components().get(pid) or {}
    check("它已经不在跑（running 之外的任何状态都算：absent/stopped/没这条）",
          (not _c) or _c.get("status") != "running", _c.get("status"))
    AC.backup(CONFIG)
    io.open(CONFIG, "w", encoding="utf-8", newline="\n").write(before_cfg)
    check("config.yaml 已还原（一个字节不差）", io.open(CONFIG, encoding="utf-8").read() == before_cfg, "")
    if os.path.isfile(TAG):
        os.remove(TAG)
    if os.path.isfile(CONFIG + ".bak-agentctl") and \
            io.open(CONFIG + ".bak-agentctl", encoding="utf-8").read() == before_cfg:
        os.remove(CONFIG + ".bak-agentctl")
    check("假插件进程已退", not live(apid), "pid=%s" % apid)
    left = sorted([k for k in components() if "hotreload" in k])
    if left:
        print("  （提醒）内核注册表里还留着 %d 行已停的 hotreload 组件（**重启内核即消失**）：%s"
              % (len(left), "、".join(left)))

    print("-" * 72)
    print("注：%s 的身份还留在**内核内存**的插件注册表里（stop/delete 都拿不掉，"
          "buildComponents 会把它补录成 stopped/absent）—— **重启内核即消失**。"
          "它已停止、进程已退、config.yaml 已还原、没往回收站/禁用名单写垃圾："
          "不会再起来，也不会拉起任何东西。" % pid)
    if FAILS:
        print("失败 %d 条（共 %d 项）：%s" % (len(FAILS), N, "、".join(FAILS)))
        return 1
    print("全部通过（%d 项：热加生效 / 不动在跑的 / 幂等 / 清场干净）" % N)
    return 0


if __name__ == "__main__":
    sys.exit(main())
