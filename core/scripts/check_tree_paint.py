#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""「树没变就不重画」自检（2026-10-12，对照 Obsidian：改一篇笔记，树是一动不动的）。

验四件（都用**真动作**，不假装内部状态）：
  ① 树没变时叫 loadTree()：DOM 一个都不碰（在第一行做记号，叫完记号还在）+ 很快
  ② 建一个临时文件夹 → loadTree()：**必须重画**，而且新文件夹真出现在树里（不能为了快就不更新）
  ③ 删掉那些临时文件夹 → loadTree()：树里没了
  ④ 重画之后滚动位置还在（先把文件夹展开到树真的溢出，否则这一格根本滚不动 = 量不到）

用法：python scripts\check_tree_paint.py
"""
import json
import subprocess
import sys
import time
import urllib.request

SHELL = r"D:\core\bin\shuge-app.exe"
PORT = 9446
SUFFIX = "incr"
TMPNAME = "自检临时夹-可删"

FAILS = []
SKIPS = []


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

    def js(expr, wait=300):
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


def skip(what):
    print("  [skip] " + what)
    SKIPS.append(what)


def wait_for(js, expr, seconds=40, step=0.1):
    for _ in range(int(seconds / step)):
        v = js(expr)
        if v:
            return v
        time.sleep(step)
    return None


def main():
    proc = subprocess.Popen([SHELL, "--debug-port", str(PORT), "--mutex-suffix", SUFFIX,
                             "--profile-suffix", SUFFIX, "--state-suffix", SUFFIX])
    try:
        ws = connect(PORT)
        if not ws:
            print("CDP 拿不到页面")
            return 1
        js = make_js(ws)
        rows = wait_for(js, "(function(){var t=document.getElementById('tree');"
                            "return t && t.children.length ? t.querySelectorAll('.row').length : 0})()", 40)
        if not rows:
            print("库那格没建出来，后面没法验")
            return 1
        js("showTab('notes'); 'ok'")
        time.sleep(1.5)

        # ① 先对一次表（万一这期间库里真变了，那一次重画是应该的），再验"没变就不画"
        r1 = js("""(async function(){
            await loadTree();
            var box = document.getElementById('tree');
            var first = box.querySelector('.row');
            first.__mark = 'KEEP-ME';
            var a = performance.now();
            await loadTree();
            var ms = Math.round(performance.now() - a);
            return {ms: ms, kept: box.querySelector('.row') === first,
                    rows: box.querySelectorAll('.row').length};
        })()""")
        if isinstance(r1, dict):
            want(r1.get("kept") is True, "树没变时 loadTree() 不重建 DOM（记号还在）", r1)
            want(r1.get("ms", 9999) < 150, "树没变时 loadTree() 很快", "%s ms" % r1.get("ms"))
        else:
            want(False, "第一次 loadTree() 没跑通", r1)

        # ② 建一个临时文件夹 → 必须重画，而且真出现在树里
        made = js("""(async function(){
            var r = await fetch('/api/folders', {method:'POST', headers:{'Content-Type':'application/json'},
                                                 body: JSON.stringify({name: %s})});
            return await r.json();
        })()""" % json.dumps(TMPNAME))
        if not (isinstance(made, dict) and made.get("folder_id")):
            want(False, "临时文件夹没建出来（后面跳过）", made)
        else:
            r2 = js("""(async function(){
                var box = document.getElementById('tree');
                var first = box.querySelector('.row');
                first.__mark = 'KEEP-ME';
                await loadTree();
                return {kept: box.querySelector('.row') === first,
                        inTree: box.innerHTML.indexOf(%s) >= 0};
            })()""" % json.dumps(TMPNAME))
            want(isinstance(r2, dict) and r2.get("kept") is False,
                 "树真变了时必须重画（记号被清掉）", r2)
            want(bool(isinstance(r2, dict) and r2.get("inTree")),
                 "新建的文件夹真出现在树里", TMPNAME)

            # ④ 滚动位置：先展开文件夹，直到树真的溢出（不然这一格滚不动，量不出东西）
            prep = js("""(async function(){
                var box = document.getElementById('tree');
                var rows = box.querySelectorAll('.row');
                var opened = 0;
                for (var i = 0; i < rows.length && opened < 12; i++) {
                  if (rows[i].dataset.isDir !== '1' || !rows[i].dataset.id) continue;
                  var body = rows[i].nextElementSibling;
                  if (body && body.style.display === 'none') { rows[i].click(); opened++; }
                  if (box.scrollHeight > box.clientHeight + 120) break;
                }
                return {opened: opened, ch: box.clientHeight, sh: box.scrollHeight};
            })()""")
            r4 = js("""(async function(){
                var box = document.getElementById('tree');
                box.scrollTop = 120;
                var before = box.scrollTop;
                await fetch('/api/folders', {method:'POST', headers:{'Content-Type':'application/json'},
                                             body: JSON.stringify({name: '自检临时夹2-可删'})});
                await loadTree();
                return {before: before, after: box.scrollTop, ch: box.clientHeight, sh: box.scrollHeight};
            })()""")
            if isinstance(r4, dict) and r4.get("before", 0) > 50:
                want(abs((r4.get("after") or 0) - r4["before"]) <= 4,
                     "重画之后滚动位置还在", r4)
            else:
                skip("树这一格滚不动（展开后仍然没溢出），滚动位置这一条没法量：%s / prep=%s"
                     % (r4, prep))

            # ③ 删掉临时文件夹（都删）
            gid = js("""(async function(){
                var d = await (await fetch('/api/folders')).json();
                var out = [];
                (d.folders || []).forEach(function(f){
                  if ((f.name || '').indexOf('自检临时夹') === 0) out.push(f.id);
                });
                return out;
            })()""")
            gone = 0
            for x in (gid or []):
                js("""(async function(){
                    await fetch('/api/folders/' + %s, {method:'DELETE'});
                    return 'ok';
                })()""" % json.dumps(x))
                gone += 1
            js("""(async function(){ await loadTree(); return 'ok'; })()""")
            has = js("(function(){var t=document.getElementById('tree');"
                     "return !!t && t.innerHTML.indexOf(%s) >= 0})()" % json.dumps(TMPNAME))
            left = js("""(async function(){
                var d = await (await fetch('/api/folders')).json();
                return (d.folders || []).filter(function(f){
                  return (f.name || '').indexOf('自检临时夹') === 0; }).length;
            })()""")
            want(gone >= 1 and left == 0, "临时文件夹都删掉了", "删了 %d 个，还剩 %s 个" % (gone, left))
            want(not has, "删掉之后树里没有了", has)
        ws.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print("")
    if FAILS:
        print("结果：%d 项没过 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("结果：全过%s" % ("（%d 项跳过）" % len(SKIPS) if SKIPS else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
