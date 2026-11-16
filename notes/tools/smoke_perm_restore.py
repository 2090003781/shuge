"""档位还原的回归：**跑自测不许把你的档位留下**。

背景（辞玖 2026-10-04 报的）：`plugin.dsh` 明明设在"相信"档，我每次跑全量体检
（`run_all` → `tools/smoke_brain_plugin.py` → 平台 `scripts/smoke_ops_mcp.py --yes`）
它就被回退成"可写"。因为那个自测**真的会改档位**（read→write→trust 走一遍再还原），
而**子进程被超时杀掉时它自己的 finally 跑不到** —— 只剩还活着的父进程能还这笔账。

这条回归专打那个失败模式：
  ① 记下当前档位（跑完必须一模一样）
  ② 造一个"把档位改成 write 然后装死"的假子进程 + 一份 timeout 很短的包装副本
  ③ 跑它 → 必须看到父进程把它还原回来
  ④ 无论如何（哪怕中途炸了）把自己动手改过的档位恢复原样

    python tools/smoke_perm_restore.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HOST = "http://127.0.0.1:8790"
PLUGIN = "plugin.dsh"
WRAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "smoke_brain_plugin.py")
OK, FAILED = True, []


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    if not cond:
        FAILED.append(name)
    print("  %s %s%s" % ("✅" if cond else "❌", name, (" | " + str(extra))[:170] if extra else ""))


def level():
    try:
        with urllib.request.urlopen(HOST + "/api/permissions?plugin=" + PLUGIN, timeout=15) as r:
            return (json.loads(r.read().decode("utf-8")) or {}).get("level") or ""
    except Exception:
        return ""


def set_level(lv):
    body = json.dumps({"plugin": PLUGIN, "level": lv}).encode("utf-8")
    req = urllib.request.Request(HOST + "/api/permissions/set", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return (json.loads(r.read().decode("utf-8")) or {}).get("level") or ""


orig = level()
print("① 记下现在的档位：%s" % (orig or "（读不到）"))
if not orig:
    print("   ⚠️ 平台没在跑或读不到档位 —— 跳过（不算失败）")
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="zz-permrestore-")
try:
    if not os.path.isfile(WRAP):
        print("   ⚠️ 没找到 %s —— 跳过" % WRAP)
        sys.exit(0)

    print("② 造一个「改成 write 然后装死」的假子进程 + 一份 timeout=6 的包装副本")
    fake = os.path.join(tmp, "zz_fake_child.py")
    io.open(fake, "w", encoding="utf-8", newline="\n").write('''# -*- coding: utf-8 -*-
import json, time, urllib.request
b = json.dumps({"plugin": "%s", "level": "write"}).encode()
r = urllib.request.Request("%s/api/permissions/set", data=b, method="POST",
                           headers={"Content-Type": "application/json"})
urllib.request.urlopen(r, timeout=15).read()
print("假子进程：档位已改成 write，装死中", flush=True)
time.sleep(30)
''' % (PLUGIN, HOST))
    copy = os.path.join(tmp, "zz_wrap_copy.py")
    src = io.open(WRAP, encoding="utf-8").read()
    import re as _re
    src = _re.sub(r'SCRIPT = .*', lambda m: 'SCRIPT = r"%s"' % fake, src, count=1)
    src = src.replace("timeout=900", "timeout=6")
    assert fake in src and "timeout=6" in src, "改写包装副本失败"
    io.open(copy, "w", encoding="utf-8", newline="\n").write(src)

    print("③ 先把它设回 %s，再跑那份会超时被杀的子进程" % orig)
    set_level(orig)
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, copy], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        out = "（外层也超时了）"
    print("   用时 %.1fs" % (time.time() - t0))
    check("看得到「父进程兜底」那句话", "父进程兜底" in out, out.strip().splitlines()[-1] if out.strip() else "")
    check("子进程确实把档位改成过 write（否则这条回归没打中）", "write" in out, out[:120])
    check("跑完档位还是 %s（没被留下）" % orig, level() == orig, "现在是 %s" % level())
finally:
    if level() != orig:                      # 无论如何别把主人的档位留下
        try:
            set_level(orig)
            print("   （收尾：把档位恢复成 %s）" % orig)
        except Exception as e:
            print("   ⚠️ 收尾恢复失败：%s" % e)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

print("=" * 70)
print("通过" if OK else "失败：%s" % "、".join(FAILED))
sys.exit(0 if OK else 1)
