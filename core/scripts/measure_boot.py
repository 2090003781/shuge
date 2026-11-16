#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**枢阁启动到底花几秒、之后还有没有在偷偷干活**（对照 Obsidian：初始化几秒，之后什么都不再发生）。

量五段：
  ① 从进程起来到页面出现（CDP 目标可见）
  ② 从进程起来到控制台**数据填上**（那才是「能用了」）
  ③ **预热**：人还停在控制台，库那一格的树是什么时候建好的（这就是「启动即初始化完」）
  ④ 静默期：清空 resource timing，安静等 15 秒，看还有哪些请求在周期性发生
  ⑤ 切到「库」的**真实**耗时：页面内挂 MutationObserver，记「树被看见」的那一刻
     （以前用 CDP 每 250ms 轮询一次，量出来的是「我什么时候发现的」，不是真实耗时）

用法：python scripts\measure_boot.py
"""
import json
import subprocess
import sys
import time
import urllib.request

SHELL = r"D:\core\bin\shuge-app.exe"
PORT = 9432
QUIET = 15.0

OBSERVE = """(function(){
  var box = document.getElementById('tree');
  if (!box) return 'no-tree';
  window.__t0 = performance.now();
  window.__seen = box.children.length ? -1 : 0;      // -1 = 切之前树就已经在了
  try {
    new MutationObserver(function(){
      if (!window.__seen && box.children.length) window.__seen = performance.now() - window.__t0;
    }).observe(box, { childList: true });
  } catch (e) { return 'ERR ' + e; }
  return 'ok';
})()"""


def connect(port, timeout=60):
    for _ in range(int(timeout * 10)):
        time.sleep(0.1)
        try:
            lst = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/json/list" % port, timeout=2).read().decode())
            ps = [x for x in lst if x.get("type") == "page" and "/console" in x.get("url", "")]
            if ps:
                import websocket
                return websocket.create_connection(ps[0]["webSocketDebuggerUrl"], timeout=30)
        except Exception:
            pass
    return None


def make_js(ws):
    n = [0]

    def js(expr, wait=60):
        n[0] += 1
        ws.send(json.dumps({"id": n[0], "method": "Runtime.evaluate",
                            "params": {"expression": expr, "returnByValue": True,
                                       "awaitPromise": True}}))
        ws.settimeout(wait)
        while True:
            g = json.loads(ws.recv())
            if g.get("id") == n[0]:
                r = g.get("result", {})
                if "exceptionDetails" in r:
                    return "ERR " + str(r["exceptionDetails"].get("text"))
                return r.get("result", {}).get("value")
    return js


def quiet_window(js, title, seconds=QUIET):
    js("performance.clearResourceTimings(); 'ok'")
    time.sleep(seconds)
    res = js("""(function(){
      return JSON.stringify(performance.getEntriesByType('resource')
        .filter(function(e){ return e.initiatorType !== 'link' && e.initiatorType !== 'script'; })
        .map(function(e){ return {u: e.name.replace(location.origin,'').split('?')[0]}; }));
    })()""")
    try:
        rows = json.loads(res)
    except Exception:
        print("   ", res)
        return
    counts = {}
    for r in rows:
        counts[r["u"]] = counts.get(r["u"], 0) + 1
    print("   %s：%.0f 秒里共 %d 笔请求" % (title, seconds, len(rows)))
    for u, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print("      %-34s x%d" % (u[:34], c))
    if not rows:
        print("      （一笔都没有 —— 这就是「后续没有这些东西」）")


def main():
    t0 = time.time()
    proc = subprocess.Popen([SHELL, "--debug-port", str(PORT), "--mutex-suffix", "startm",
                             "--profile-suffix", "startm", "--state-suffix", "startm"])
    try:
        ws = connect(PORT)
        if not ws:
            print("CDP 拿不到页面")
            return 1
        t_page = time.time() - t0
        js = make_js(ws)
        t_ready = None
        t_warm = None
        for _ in range(1200):                     # 最多 60 秒
            v = js("(function(){var p=document.getElementById('hostPill');"
                   "var s=document.getElementById('state');var t=document.getElementById('tree');"
                   "return [(p&&p.textContent&&s&&s.textContent)?1:0, t?t.children.length:0];})()")
            if isinstance(v, list):
                if v[0] == 1 and t_ready is None:
                    t_ready = time.time() - t0
                if v[1] and t_warm is None:
                    t_warm = time.time() - t0
                if t_ready is not None and t_warm is not None:
                    break
            time.sleep(0.05)
        nav = js("""(function(){var n=performance.getEntriesByType('navigation')[0]||{};
          return JSON.stringify({rs: Math.round(n.responseStart||0),
                                 dcl: Math.round(n.domContentLoadedEventEnd||0),
                                 load: Math.round(n.loadEventEnd||0)});})()""")
        print("(1) 进程起来 -> 页面出现        : %.1f 秒" % t_page)
        print("(2) 进程起来 -> 控制台数据填上  : %s" % (("%.1f 秒" % t_ready) if t_ready else "（没填上）"))
        print("(3) 进程起来 -> 库那格的树建好  : %s  <- 人还在控制台，这就是「启动即初始化完」"
              % (("%.1f 秒" % t_warm) if t_warm else "（没建好）"))
        try:
            d = json.loads(nav)
            print("    页面自己的时序（导航起算）: responseStart %s ms / DCL %s ms / load %s ms"
                  % (d["rs"], d["dcl"], d["load"]))
        except Exception:
            print("   ", nav)

        print("(4) 静默期：谁还在周期性发请求")
        quiet_window(js, "控制台那一格（可见）")

        print("(5) 切到「库」的真实耗时")
        print("    挂观察器：", js(OBSERVE))
        t_call = js("(function(){var a=performance.now(); showTab('notes');"
                    "return Math.round(performance.now()-a);})()")
        seen = None
        for _ in range(240):
            v = js("window.__seen")
            if isinstance(v, (int, float)) and v != 0:
                seen = v
                break
            time.sleep(0.05)
        box = js("(function(){var t=document.getElementById('tree');"
                 "var v=document.querySelector('#view-notes');"
                 "return JSON.stringify({rows:t?t.querySelectorAll('.row').length:-1,"
                 "vis: v? getComputedStyle(v).display : '?'});})()")
        if seen == -1:
            print("    showTab() 同步花了 %s ms；树**在切之前就已经建好**（预热生效 = 零等待）" % t_call)
        elif seen:
            print("    showTab() 同步花了 %s ms；切完到树出现 %.1f ms" % (t_call, seen))
        else:
            print("    showTab() 同步花了 %s ms；12 秒内没看到树" % t_call)
        print("    树里的行数 / 库那格显示状态：", box)
        quiet_window(js, "库那一格（可见）")
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
