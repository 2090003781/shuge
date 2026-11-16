#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**真浏览器里验主题统一 / 实时 / 不振荡**（headless Edge/Chrome + CDP，可重跑）。

辞玖 2026-10-06：「①所有页面主题统一 ②只页签能切换主题，其他地方没变」→「两个主题自己频繁变化」→「全实时」。

背景（决定了这个脚本为什么这么写）：
  · 三个界面是**三个不同的源**（8790 / 8795 / 8848 端口不同）：实测这个浏览器里
    BroadcastChannel 跨标签**不投递**，localStorage 也各存各的 → **跨端口只能走服务端**；
  · 所以实时靠主程序的一条 SSE：`GET /api/ui/theme/stream`，谁改主题立刻推给所有页面。

验七件事：
  ① 三个标签都打开后，三个 `<html data-theme>` 同一个值；
  ② **实时**：在【库】里点一下 → 0.6 秒内 **另外两个标签也变了**（不切标签、不刷新）；
  ③ 反向再来一次（主程序页点 → 库与内核跟着变）；
  ④ 服务端值始终等于界面上看到的（唯一真相）；
  ⑤ 新开一个标签：一进来就是当前主题；
  ⑥ 谁都不点静置 N 秒 → 三个文档 + 服务端都不许动（**振荡回归**）；
  ⑦ 每个文档 `<html data-theme>` 的写入次数有界（互推会暴涨到几百上千）。

用法：`python scripts\check_theme_browser.py [静置秒数]`
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

API = "http://127.0.0.1:8790/api/ui/theme"
TABS = [("http://127.0.0.1:8790/console", "主程序页(8790)"),
        ("http://127.0.0.1:8848/", "库(8848)"),
        ("http://127.0.0.1:8795/", "内核控制台(8795)")]
CAND = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"]
RT_BUDGET = 0.8   # 实时预算：点了之后多久内必须全变（秒）
fails = []


def check(name, cond, extra=""):
    print("  %s %s%s" % ("✅" if cond else "❌", name, ("  ← %s" % str(extra)[:200]) if (not cond and extra) else ""))
    if not cond:
        fails.append(name)


def find_browser():
    for p in CAND:
        if os.path.isfile(p):
            return p
    return shutil.which("msedge") or shutil.which("chrome")


def api_get():
    with urllib.request.urlopen(API, timeout=10) as r:
        return json.loads(r.read().decode("utf-8")).get("theme")


def api_post(t):
    req = urllib.request.Request(API, data=json.dumps({"theme": t}).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    hold = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    orig = api_get()
    br = find_browser()
    if not br:
        print("❌ 没找到 Edge/Chrome，这台机器上没法跑真浏览器验证")
        return 2
    import websocket

    prof = tempfile.mkdtemp(prefix="shuge-theme-chk-")
    port = 49578
    proc = subprocess.Popen([br, "--headless=new", "--remote-debugging-port=%d" % port,
                             "--user-data-dir=" + prof, "--no-first-run", "--no-default-browser-check",
                             "--disable-gpu", "--window-size=1400,900",
                             "--remote-allow-origins=*", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    nid = [0]
    try:
        ver = None
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=1) as r:
                    ver = json.loads(r.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.5)
        if not ver:
            print("❌ 浏览器没起来")
            return 1
        print("  浏览器：%s（服务端起始主题 %s）" % (ver.get("Browser"), orig))

        def open_tab(url):
            req = urllib.request.Request("http://127.0.0.1:%d/json/new?%s" % (port, url), method="PUT")
            with urllib.request.urlopen(req, timeout=10) as r:
                t = json.loads(r.read().decode("utf-8"))
            ws = websocket.create_connection(t["webSocketDebuggerUrl"], timeout=30)

            def send(method, params=None):
                nid[0] += 1
                ws.send(json.dumps({"id": nid[0], "method": method, "params": params or {}}))
                while True:
                    got = json.loads(ws.recv())
                    if got.get("id") == nid[0]:
                        return got

            def ev(expr):
                r = send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                res = r.get("result", {}).get("result", {})
                if "value" in res:
                    return res["value"]
                return {"__err": str(r.get("result", {}).get("exceptionDetails") or r)[:160]}

            send("Page.enable")
            send("Runtime.enable")
            return {"ws": ws, "send": send, "ev": ev, "url": url}

        def wait_all(tabs, want, budget):
            """在 budget 秒内轮询，直到所有标签都是 want；返回 (是否全中, 实际值, 用时)"""
            t0 = time.time()
            last = None
            while time.time() - t0 < budget:
                last = {t["label"]: T(t) for t in tabs}
                if set(last.values()) == {want}:
                    return True, last, time.time() - t0
                time.sleep(0.08)
            return False, last, time.time() - t0

        tabs = []
        for url, label in TABS:
            t = open_tab(url)
            t["label"] = label
            tabs.append(t)
        for t in tabs:
            for _ in range(60):
                if t["ev"]("document.readyState") == "complete":
                    break
                time.sleep(0.5)
        time.sleep(3)
        for t in tabs:
            t["ev"]("(function(){var h=document.documentElement;if(h.__n===undefined){h.__n=0;"
                    "new MutationObserver(function(){h.__n++}).observe(h,{attributes:true,attributeFilter:['data-theme']});}"
                    "return 1})()")
        T = lambda t: t["ev"]("document.documentElement.getAttribute('data-theme')")
        C = lambda t: t["ev"]("(document.documentElement.__n||0)")   # 新标签可能还没装上计数器 → 当 0
        STREAM = lambda t: t["ev"]("(function(){try{return typeof EventSource}catch(e){return 'x'}})()")

        print("① 三个标签（三个源）的主题必须一致")
        vals = {t["label"]: T(t) for t in tabs}
        print("     %s" % json.dumps(vals, ensure_ascii=False))
        check("三个界面同一个主题", len(set(vals.values())) == 1 and list(vals.values())[0] in ("dark", "light"), vals)
        cur = list(vals.values())[0]
        check("页面里有 EventSource（实时通道可用）", all(STREAM(t) == "function" for t in tabs),
              {t["label"]: STREAM(t) for t in tabs})

        print("② 实时：在【库】里点一下 → %.1f 秒内另外两个标签也要变（不切标签、不刷新）" % RT_BUDGET)
        want = "light" if cur == "dark" else "dark"
        tabs[1]["ev"]("document.getElementById('btnTheme').click()")
        ok, got, dt = wait_all(tabs, want, RT_BUDGET)
        check("点了 %s → %s：三页 %.2f 秒内全变" % (cur, want, dt), ok, got)

        print("③ 反向再来一次：在【主程序页】点 → 库与内核控制台跟着变")
        want2 = "light" if want == "dark" else "dark"
        tabs[0]["ev"]("document.getElementById('btnTheme').click()")
        ok, got, dt = wait_all(tabs, want2, RT_BUDGET)
        check("点了 %s → %s：三页 %.2f 秒内全变" % (want, want2, dt), ok, got)

        print("④ 服务端唯一真相 = 界面上看到的")
        push = api_post(want2)
        check("服务端读到 %s" % want2, api_get() == want2, api_get())
        check("POST 回执里有推送人数（说明真有页面连着实时通道）", int(push.get("pushed", 0)) >= 3, push)

        print("⑤ 新开一个标签：一进来就是 %s" % want2)
        t4 = open_tab("http://127.0.0.1:8848/")
        t4["label"] = "新标签(8848)"
        for _ in range(60):
            if t4["ev"]("document.readyState") == "complete":
                break
            time.sleep(0.5)
        time.sleep(2)
        check("新标签一进来就是 %s" % want2, T(t4) == want2, T(t4))
        tabs.append(t4)
        for t in tabs:
            t["ev"]("(function(){var h=document.documentElement;if(h.__n===undefined){h.__n=0;"
                    "new MutationObserver(function(){h.__n++}).observe(h,{attributes:true,attributeFilter:['data-theme']});}"
                    "}return 1})()")

        print("⑥ 谁都不点，静置 %.0f 秒：值不许自己变（振荡回归）" % hold)
        snap0 = {t["label"]: T(t) for t in tabs}
        api0 = api_get()
        flips = []
        t0 = time.time()
        while time.time() - t0 < hold:
            time.sleep(1.0)
            snap = {t["label"]: T(t) for t in tabs}
            if snap != snap0:
                flips.append((round(time.time() - t0, 1), snap))
                snap0 = snap
        check("静置 %.0f 秒里四个文档的主题一次没变" % hold, not flips, flips[:3])
        check("静置期间服务端值也没被动过", api_get() == api0, "%s → %s" % (api0, api_get()))

        print("⑦ 每个文档的 data-theme 写入次数（全程只点了 2 次；互推会暴涨到几百上千）")
        for t in tabs:
            n = C(t)
            check("%s：写入 %s 次（≤ 15 才正常）" % (t["label"], n), isinstance(n, int) and n <= 15, n)

        for t in tabs:
            t["ws"].close()
        api_post(orig)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        time.sleep(1)
        shutil.rmtree(prof, ignore_errors=True)

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ 真浏览器里验过：三个界面主题统一、点一下全都实时跟着变、不点不会自己变")
    return 0


if __name__ == "__main__":
    sys.exit(main())
