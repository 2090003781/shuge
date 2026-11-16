"""控制台 agent 接口插件（plugin.dsh）的能力自测入口 —— 转调平台侧那一份。

为什么是转调：权限三档（只读/可写/相信）与 MCP 工具面**全都封装在平台侧**
（D:\\core\\plugins\\{lib/ops_mcp.py,dsh_plugin.py}，自测在
scripts/smoke_ops_mcp.py），笔记库里不该再存一份副本 —— 这里只负责把它拉进全量体检。
"""
import os
import subprocess
import sys

NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

SCRIPT = os.path.join("D:\\", "core", "scripts", "smoke_ops_mcp.py")

if not os.path.isfile(SCRIPT):
    print("⚠️ 没找到平台侧自测（%s）——平台没装或路径变了" % SCRIPT)
    sys.exit(0)

# 平台侧的 smoke_ops_mcp.py 从 2026-09-30 起要 --yes（它会真的调平台操作、可能改档位）。
# 这个包装本身就是"全量体检"的正规入口（run_all 调它），所以由它来**显式同意**。
HOST = "http://127.0.0.1:8790"
PLUGIN = "plugin.dsh"          # 平台自测会改它的档位 —— 跑完必须还回去


def level_of(plugin):
    """读现在的档位（读不到回空串 —— 空就别乱写）"""
    import json as _json
    import urllib.request as _u
    try:
        with _u.urlopen(HOST + "/api/permissions?plugin=" + plugin, timeout=10) as r:
            return (_json.loads(r.read().decode("utf-8")) or {}).get("level") or ""
    except Exception:
        return ""


def set_level(plugin, level):
    import json as _json
    import urllib.request as _u
    body = _json.dumps({"plugin": plugin, "level": level}).encode("utf-8")
    req = _u.Request(HOST + "/api/permissions/set", data=body, method="POST",
                     headers={"Content-Type": "application/json"})
    with _u.urlopen(req, timeout=10) as r:
        return _json.loads(r.read().decode("utf-8"))


# **父进程兜底**（2026-10-04 辞玖的 trust 被这个自测回退成 write 之后加的）：
# 子进程被超时杀掉 / 中途异常时它自己的 finally 跑不到，只有还活着的父进程能还这笔账。
_orig = level_of(PLUGIN)
code = 0
try:
    p = subprocess.run([sys.executable, SCRIPT, "--yes"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=900, creationflags=NO_WINDOW,
                       env={**os.environ, "COREHOST_ALLOW_DESTRUCTIVE": "1"})
    print(p.stdout or "", end="")
    if p.returncode:
        print(p.stderr or "", end="")
    code = p.returncode
except subprocess.TimeoutExpired as e:
    out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    print(out, end="")
    print("⏱ 平台自测超时被杀 —— 档位交给父进程兜底还原")
    code = 1
finally:
    _back = level_of(PLUGIN)
    if _orig and _back and _back != _orig:
        try:
            set_level(PLUGIN, _orig)
            print("↩︎ 档位被自测改成了 %s，已还原为 %s（父进程兜底）" % (_back, _orig))
        except Exception as e:
            print("⚠️ 档位还原失败（现在 %s，原为 %s）：%s" % (_back, _orig, e))
sys.exit(code)
