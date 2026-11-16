#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""体检：**不许有任何黑窗口**（辞玖 2026-10-05 定的硬约束）。

黑框就三个来源，改哪一处都可能漏掉另一边，所以单列一个可重跑的体检：

  ① **agent 侧的命令**（MCP 服务端 / hook 客户端）—— 这些进程是**别人 spawn** 的
     （DSH 的 node、Claude Code 的 cli Code、codex），我们给不了 CREATE_NO_WINDOW，
     只能用 `pythonw.exe`（GUI 子系统，天生没控制台，stdio 管道照通）。
  ② **平台自己 spawn 的**（插件、库服务、重启脚本）—— 必须显式隐藏：
     Go 走 `hideWindow()/prepareCmd()`（DETACHED_PROCESS + HideWindow），
     Python 走 `CREATE_NO_WINDOW`，PowerShell 走 `-WindowStyle Hidden`。
  ③ **被拉起的二进制本身**——控制台子系统（3）的 exe 由 GUI 父进程拉起时会弹窗；
     平台要拉起的 core.exe / core-host.exe 必须是 GUI 子系统（2）。

跑：python scripts\\check_no_windows.py     （退出码 0 = 干净）
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentctl as AC   # noqa: E402

CORE = AC.CORE_DIR
fails, warns, oks = [], [], []


def read(p):
    try:
        return io.open(p, encoding="utf-8", errors="replace").read()
    except Exception:
        return ""


# ── ① agent 侧命令 ────────────────────────────────────────────
risks = AC.console_risks()
for r in risks:
    fails.append("agent 侧还在用会弹黑窗的 python：%s（%s）" % (r["id"], r["where"]))
if not risks:
    oks.append("agent 侧命令全是 pythonw（描述文件 + 现网配置）")

for name in ("mcp.tpl", "hook.tpl"):
    p = os.path.join(CORE, "templates", "agent", name)
    for i, line in enumerate(read(p).splitlines(), 1):
        if re.search(r'(?i)["\']?\s*(command|command_toml)\s*[:=]', line) and re.search(r'(?i)(?<!w)python\.exe', line):
            fails.append("模板 %s:%d 的示例命令还是 python.exe" % (name, i))
if not any("模板" in f for f in fails):
    oks.append("模板里的示例命令全是 pythonw")

# ── ② 平台自己 spawn 的地方 ───────────────────────────────────
HIDE_HINT = ("hidewindow", "preparecmd", "configurehidden", "createnowindow", "detachedprocess")

# 已知例外：隐藏保证在别处，**理由写死在这儿**（不偷偷放行）
ALLOW_SPAWN = {
    (r"internal\supervisor\supervisor.go", "exec.Command(c.Path, c.Args[1:]...)"):
        "cloneCmd 重建的 Cmd 会经 start() → prepareCmd(mp.Cmd) 补标志（Spawn 与崩溃重启两条路都覆盖）",
}
go_files = []
for root, dirs, files in os.walk(CORE):
    if any(x in root for x in (os.sep + "bin", os.sep + "data", os.sep + "archive", ".git")):
        continue
    for f in files:
        if f.endswith(".go"):
            go_files.append(os.path.join(root, f))
n_spawn = n_hidden = 0
for p in go_files:
    lines = read(p).splitlines()
    is_test = p.endswith("_test.go")
    for i, line in enumerate(lines):
        if "exec.Command(" not in line:
            continue
        n_spawn += 1
        rel = os.path.relpath(p, CORE)
        allow = next((k for k in ALLOW_SPAWN if k[0] == rel and k[1] in line), None)
        if allow:
            n_hidden += 1
            oks.append("已知例外：%s:%d（%s）" % (rel, i + 1, ALLOW_SPAWN[allow]))
            continue
        # 往后多看几行：插件是 exec.Command 之后才交给 sup.Spawn 的
        near = "\n".join(lines[max(0, i - 12): i + 26]).lower()
        wrapped = any(w in near for w in (".spawn(", "sup.spawn"))
        if any(h in near for h in HIDE_HINT) or wrapped:
            n_hidden += 1
        elif is_test:
            warns.append("测试里 spawn（只有命令行跑得到，不影响日常）：%s:%d"
                         % (os.path.relpath(p, CORE), i + 1))
        else:
            fails.append("Go 里 spawn 了但附近看不到隐藏保证：%s:%d  %s"
                         % (os.path.relpath(p, CORE), i + 1, line.strip()[:70]))
oks.append("Go 的 %d 处 spawn：%d 处有隐藏保证（标志或 Supervisor）" % (n_spawn, n_hidden))

# ── ③ 二进制子系统 ───────────────────────────────────────────
def subsystem(path):
    try:
        with open(path, "rb") as f:
            f.seek(0x3C)
            off = int.from_bytes(f.read(4), "little")
            f.seek(off + 0x5C)
            return int.from_bytes(f.read(2), "little")
    except Exception:
        return None


for exe, must in (("core.exe", 2), ("core-host.exe", 2)):
    p = os.path.join(CORE, "bin", exe)
    if not os.path.isfile(p):
        warns.append("%s 不在（没构建过？跳过）" % exe)
        continue
    sub = subsystem(p)
    if sub == must:
        oks.append("%s 是 GUI 子系统（不弹窗）" % exe)
    else:
        fails.append("%s 子系统=%s（应为 2=GUI，否则被拉起时会弹窗）" % (exe, sub))
for p in sorted(__import__("glob").glob(os.path.join(CORE, "bin", "*.exe"))):
    if os.path.basename(p) in ("core.exe", "core-host.exe"):
        continue
    if subsystem(p) == 3:
        warns.append("%s 是控制台子系统（只有测试/命令行会用就别管）" % os.path.basename(p))

# ── ④ 插件（Python）自己起进程的地方 ─────────────────────────
import glob as _glob
SPAWN_HINT = ("creationflags", "no_window", "createnowindow", "startupinfo", "sw_hide")
n_py = 0
for p in sorted(_glob.glob(os.path.join(CORE, "plugins", "*.py"))):
    lines = read(p).splitlines()
    for i, line in enumerate(lines):
        if ("subprocess.Popen(" in line) or ("subprocess.run(" in line):
            n_py += 1
            near = "\n".join(lines[i:i + 8]).lower()
            if not any(h in near for h in SPAWN_HINT):
                fails.append("插件里起进程没看到隐藏标志：%s:%d  %s"
                             % (os.path.relpath(p, CORE), i + 1, line.strip()[:60]))
oks.append("插件（Python）里 %d 处起进程，都带隐藏标志" % n_py if n_py else "插件（Python）里没有起进程的地方")

print("不弹黑窗体检")
print("-" * 56)
for x in oks:
    print("  ✅ " + x)
for x in warns:
    print("  ⚠️  " + x)
for x in fails:
    print("  ❌ " + x)
print("-" * 56)
if fails:
    print("失败 %d 条" % len(fails))
    sys.exit(1)
print("通过（%d 条提醒）" % len(warns))
