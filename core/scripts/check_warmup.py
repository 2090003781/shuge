#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""预热 + 展开状态持久化 **端到端自检**（2026-10-12 加的那两样，必须真点得动才算数）。

两趟：
  第一趟 A：起窗口 → 断言「人还在控制台时，库那格的树已经建好」（预热）
            切到库 → 断言 库那格可见、面板没报错 → 点开头一篇（编辑器里要真有字）
            展开一个**本来是折叠的**文件夹 → 断言它被记进了本地 → 关掉
  第二趟 B：再起一个窗口 → 断言上次那个文件夹**这次自己就是展开的**
            （必须挑"不含当前篇"的文件夹，否则是被"当前篇所在那条路"带开的，验不出东西）

用法：python scripts\check_warmup.py
"""
import json
import subprocess
import sys
import time
import urllib.request

SHELL = r"D:\core\bin\shuge-app.exe"
PORT = 9434
SUFFIX = "preld"          # 两趟共用一个 profile 后缀 -> localStorage 留着（这正是要验的东西）

FAILS = []


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


def want(cond, what, extra=""):
    print(("  [ok] " if cond else "  [FAIL] ") + what + (("  " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(what)


def wait_for(js, expr, seconds=30, step=0.1):
    for _ in range(int(seconds / step)):
        v = js(expr)
        if v:
            return v
        time.sleep(step)
    return None


def run(port, expect_open):
    """expect_open=None：第一趟（建立展开状态）；给了 id：第二趟（断言那个文件夹自己就是展开的）。"""
    proc = subprocess.Popen([SHELL, "--debug-port", str(port), "--mutex-suffix", SUFFIX,
                             "--profile-suffix", SUFFIX, "--state-suffix", SUFFIX])
    try:
        ws = connect(port)
        if not ws:
            print("CDP 拿不到页面")
            return None
        js = make_js(ws)
        # 1 预热：人还在控制台，树就该建好了
        rows = wait_for(js, "(function(){var t=document.getElementById('tree');"
                            "return t && t.children.length ? t.querySelectorAll('.row').length : 0})()", 40)
        tab = js("(function(){var v=document.getElementById('view-console');"
                 "return v && v.className.indexOf('active')>=0 ? 'console':'other'})()")
        want(bool(rows), "人还在控制台时，库那格的树已经建好（预热）", "行数=%s 当前格=%s" % (rows, tab))
        want(tab == "console", "预热没有把界面抢到库那一格（还停在控制台）")

        # 2 第二趟：先验「上次展开的还在不在」（要在切页之前看）
        if expect_open:
            shown = js("""(function(){
                var rows = document.querySelectorAll('#tree .row');
                for (var i = 0; i < rows.length; i++) {
                  if (rows[i].dataset.id === %s) {
                    var body = rows[i].nextElementSibling;
                    return body ? (body.style.display || '(展开)') : 'no-body';
                  }
                }
                return 'not-found';
            })()""" % json.dumps(expect_open))
            want(shown in ("", "(展开)"),
                 "上次展开的那个文件夹，这次自己就是展开的（不是被当前篇那条路带开的）",
                 "内联 display=%s" % shown)

        # 3 切到库：可见、能点开一篇
        js("showTab('notes'); 'ok'")
        vis = wait_for(js, "(function(){var v=document.getElementById('view-notes');"
                           "return v && getComputedStyle(v).display!=='none' ? 1:0})()", 10)
        want(bool(vis), "切到库：那一格真的显示出来了")
        err = js("(function(){var h=document.getElementById('panelNotes');"
                 "return h ? (h.getAttribute('data-error')||'') : 'no-panel'})()")
        want(err == "", "库那一格没有报错", err)
        opened = js("""(async function(){
            var rows = document.querySelectorAll('#tree .row');
            for (var i = 0; i < rows.length; i++) {
              var r = rows[i];
              if (r.dataset.isDir === '1') continue;
              r.click();
              for (var k = 0; k < 80; k++) {
                await new Promise(function(res){ setTimeout(res, 100); });
                var ed = document.getElementById('editor');
                if (ed && (ed.value || '').length > 0) return (ed.value || '').length;
              }
              return -1;
            }
            return -2;
        })()""", wait=120)
        want(isinstance(opened, int) and opened > 0, "点第一篇能打开（编辑器里真有字）", "字数=%s" % opened)

        # 4 第一趟：展开一个真折叠的文件夹，并确认写进了本地
        if not expect_open:
            toggled = js("""(function(){
                var rows = document.querySelectorAll('#tree .row');
                for (var i = 0; i < rows.length; i++) {
                  if (rows[i].dataset.isDir !== '1') continue;
                  if (!rows[i].dataset.id) continue;                  // 根目录不算
                  var body = rows[i].nextElementSibling;
                  if (body && body.style.display === 'none') { rows[i].click(); return rows[i].dataset.id; }
                }
                return '';
            })()""")
            time.sleep(1.2)               # 存盘是防抖的（400ms）
            kept = js("(function(){var s=localStorage.getItem('shuge.openDirs')||'[]';"
                      "try{return JSON.parse(s)}catch(e){return null}})()")
            want(bool(toggled), "展开了一个本来是折叠的文件夹", "id=%s" % toggled)
            want(isinstance(kept, list) and toggled in kept,
                 "展开状态写进了本地（下次能接着上次）", "记着 %s" % (kept,))
            ws.close()
            return {"toggled": toggled}
        ws.close()
        return {}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def main():
    print("第一趟：预热 + 点开一篇 + 展开一个文件夹")
    a = run(PORT, None)
    if a is None:
        print("第一趟没跑起来")
        return 1
    time.sleep(2)
    print("第二趟：重新起窗口，看上次展开的还在不在")
    b = run(PORT + 1, (a or {}).get("toggled"))
    if b is None:
        print("第二趟没跑起来")
        return 1
    print("")
    if FAILS:
        print("结果：%d 项没过 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("结果：全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
