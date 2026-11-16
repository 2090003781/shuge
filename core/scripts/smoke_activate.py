#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""activate 自测（**会真的重启一次内核**，约 10 秒；只重启内核与插件，DSH 会话不受影响）。

验三件事：
  ① 静态：agentctl.py 里没有"`text=True` 却不给 encoding"的地方（中文控制台解码雷的根）
  ② 故障注入：把"等内核停干净"那步弄成抛异常 → **start 仍然必须跑到**（内核不能躺在半路）
  ③ 真重启一次：停 → 起 → 等就绪，返回 0，并且内核与插件都回来了

跑：python scripts\\smoke_activate.py
"""
import io
import os
import re
import sys

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CORE, "scripts"))
import agentctl as AC  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  ← " + str(extra)))
    if not cond:
        fails.append(name)


print("activate 自测（会真重启一次内核）")
print("-" * 56)

# ① 静态：别再出现 text=True 不带 encoding（**走 AST**，免得把注释里的例子也当代码扫）
import ast as _ast
src = io.open(os.path.join(CORE, "scripts", "agentctl.py"), encoding="utf-8").read()
tree = _ast.parse(src)
bad = []
for node in _ast.walk(tree):
    if not isinstance(node, _ast.Call):
        continue
    fname = ""
    if isinstance(node.func, _ast.Name):
        fname = node.func.id
    elif isinstance(node.func, _ast.Attribute):
        fname = node.func.attr
    if fname not in ("_run", "run", "Popen", "check_output"):
        continue
    kw = {k.arg for k in node.keywords if k.arg}
    if "text" in kw and "encoding" not in kw:
        bad.append("第 %d 行：%s(...) 有 text=True 却没给 encoding" % (node.lineno, fname))
check("没有『text=True 却不给 encoding』的调用（中文控制台解码雷）", not bad, bad)

# ② 故障注入：等停干净那步抛异常 → start 仍然要跑
print("\n② 故障注入：让『等内核停干净』抛异常，看 start 会不会被丢掉")
orig = AC._core_running


def boom():
    raise UnicodeDecodeError("gbk", b"\xd0\xc5\xcf\xa2", 0, 1, "模拟中文控制台解码崩")


AC._core_running = boom
try:
    rc = AC.cmd_activate(None, None)
finally:
    AC._core_running = orig
check("故障注入下 activate 仍然把内核拉回来了（返回 0）", rc == 0, "rc=%s" % rc)
check("内核确实在跑（/healthz）", AC._core_alive(), "内核没起来")

# ③ 真重启一次（正常路径）
print("\n③ 正常路径：再真重启一次")
rc2 = AC.cmd_activate(None, None)
check("正常路径也返回 0", rc2 == 0, "rc=%s" % rc2)
st, comp = AC.http("/api/components")
comps = {c.get("id"): c for c in (comp if isinstance(comp, list) else [])}
check("内核回来后有插件在跑（>=10 个）", len(comps) >= 10, "只有 %d 个" % len(comps))
for pid in ("plugin.log", "plugin.agentctl", "plugin.workbuddy"):
    c = comps.get(pid) or {}
    check("%s 在跑" % pid, c.get("status") == "running", c.get("status"))

print("-" * 56)
if fails:
    print("失败 %d 条：%s" % (len(fails), "、".join(fails)))
    sys.exit(1)
print("全部通过")
