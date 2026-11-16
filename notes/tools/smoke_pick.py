# -*- coding: utf-8 -*-
"""导入"从系统选择框选路径"的验收（辞玖 2026-09-28）。**全程不弹窗**。

验的是协议与接线，不验"窗口长什么样"（那个要人点）：
  ① 帮手进程 --self-test 能写出结果 JSON（ok 与 canceled 两种）
  ② POST /api/pick {dry_run} 立刻回 ok（不弹窗，只走协议）
  ③ GET /api/pick?token=<没见过的> → pending（人还在选的那种状态）
  ④ 页面里真的有「📂 选文件…」「📁 选目录…」两颗按钮 + pickPath() 接线
"""
import json
import os
import subprocess
import sys
import tempfile
import uuid
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8848"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FAIL = []
PASS = 0


def check(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    print("  %s %s%s" % ("✅" if cond else "❌", name, "" if cond else "  | " + str(extra)[:220]))
    if not cond:
        FAIL.append(name)


def call(path, method="GET", body=None):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          "X-Writer": "tool:pick-acc"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def main():
    helper = os.path.join(HERE, "pick_path.py")
    tmp = tempfile.mkdtemp(prefix="pick-acc-")

    print("① 帮手进程：--self-test 写结果（不弹窗）")
    out = os.path.join(tmp, "a.json")
    r = subprocess.run([sys.executable, helper, "--self-test", "--out", out,
                        "--fake", r"D:\示例\某文.docx"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    d = json.load(open(out, encoding="utf-8")) if os.path.isfile(out) else {}
    check("退出码 0", r.returncode == 0, r.stderr[-200:])
    check("state=ok 且带回路径", d.get("state") == "ok" and d.get("paths") == [r"D:\示例\某文.docx"], d)

    out2 = os.path.join(tmp, "b.json")
    subprocess.run([sys.executable, helper, "--self-test", "--out", out2],
                   capture_output=True, text=True, encoding="utf-8", errors="replace")
    d2 = json.load(open(out2, encoding="utf-8")) if os.path.isfile(out2) else {}
    check("没给 --fake → canceled（等价于人把窗口关了）", d2.get("state") == "canceled" and d2.get("paths") == [], d2)

    print("② POST /api/pick：dry_run 不发窗口、按协议回执")
    st, d3 = call("/api/pick", "POST", {"kind": "dir", "dry_run": True, "fake": r"D:\示例目录"})
    check("HTTP 200", st == 200, st)
    check("state=ok + dry_run 标记", d3.get("state") == "ok" and d3.get("dry_run") is True, d3)
    check("kind 按请求回显 dir", d3.get("kind") == "dir", d3.get("kind"))
    st, d4 = call("/api/pick", "POST", {"kind": "file", "dry_run": True})
    check("kind=file 也认", d4.get("kind") == "file", d4.get("kind"))

    print("③ GET /api/pick：轮询语义")
    st, d5 = call("/api/pick?token=" + uuid.uuid4().hex)
    check("没见过的 token → pending", st == 200 and d5.get("state") == "pending", d5)
    st, d6 = call("/api/pick")
    check("没有 token → pending（不报错）", st == 200 and d6.get("state") == "pending", d6)

    print("④ 页面：导入面板现在用浏览器自己的选择框（服务端弹窗那两颗按钮已下线）")
    with urllib.request.urlopen(BASE + "/", timeout=60) as r:
        html = r.read().decode("utf-8", "replace")
    check("按钮叫「从磁盘导入」", "impPick()" in html and "从磁盘导入" in html)
    check("「选目录」已去掉", "选目录" not in html and "webkitdirectory" not in html)
    check("选文件输入框还在（multiple）", 'id="impFiles"' in html and "multiple" in html)
    check("手写路径框已删掉", 'id="impSrc"' not in html)
    check("目标位置显示成一行文字（不用手打）", 'id="impUnderText"' in html)
    check("来源 + 位置在**同一个弹窗**里（2026-09-29）",
          'id="importModal"' in html and 'id="destTree"' in html and 'id="importPanel"' not in html)
    check("点导入就直接打开弹窗（并同步叫系统选择框）",
          "function openImport(" in html and "openImport()" in html and "if (pick !== false) impPick();" in html)
    check("选位置接线在（点中即选定）", "function destRender(" in html and "function destPick(" in html)
    check("上传接口接线在", "api/import/upload" in html)
    check("面板上有「已选清单」", 'id="impList"' in html and "还没选文件" in html)
    check("面板上有错误/状态行", 'id="impInfo"' in html)

    print("\n（本验收不弹任何窗口；真窗口要人点一下才能验）")
    print("========  结论: %d 通过 / %d 失败" % (PASS, len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
