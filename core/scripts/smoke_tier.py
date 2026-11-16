#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""档位登记自测：拿假 agent 走一遍 `add --apply`，断言档位**显式**写进 data/plugins/permissions.json。

为什么要它：内核 `/api/permissions` 会把**默认档**也报成"有档位"，所以"看起来登记了"不等于真登记 ——
2026-10-05 就漏过（workbuddy 接完有效档 write，但文件里根本没有它）。
config.yaml 与 permissions.json 前后做字节快照，跑完还原。

跑：python scripts\\smoke_tier.py
"""
import io
import json
import os
import shutil
import subprocess
import sys

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CORE, "scripts"))
import agentctl as AC  # noqa: E402

AID = "zz-tier"
DESC = os.path.join(CORE, "agents", AID + ".yaml")
SNAP = os.path.join(CORE, "data", "tmp", "tier-snap")
os.makedirs(SNAP, exist_ok=True)
fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  ← " + str(extra)))
    if not cond:
        fails.append(name)


def snap(p, n):
    if os.path.isfile(p):
        shutil.copy2(p, os.path.join(SNAP, n))


print("档位登记自测（假 agent：%s）" % AID)
print("-" * 56)
check("一开始文件里没有它（对照）", AC._explicit_tier("plugin." + AID) == "",
      AC._explicit_tier("plugin." + AID))

snap(AC.CONFIG, "config.yaml")
snap(AC.PERM_FILE, "permissions.json")
io.open(DESC, "w", encoding="utf-8", newline="\n").write('''id: %s
name: zz 档位登记自测
agent_id: agent.%s
plugin_id: plugin.%s
detect: {paths: [], files: [], exes: []}
interface:
  tier: read                       # ← 故意给 read：验"按描述文件里的档位登记"
  status_tool: %s.status
  inbox: data/inbox/%s
  record: data/transcripts/agent.%s
  capabilities: [agent.bridge]
  agent_capabilities: []
  description: "zz 档位登记自测"
mcp: {kind: none}
hook: {kind: none}
source: {events: none, transcript: none, adapter: "", path: ""}
''' % (AID, AID, AID, AID, AID, AID))
try:
    # AGENTCTL_NO_RELOAD：**自测不该动真内核的插件表** —— add --apply 默认会自动热加载，
    # 假 agent 没有真插件，热加载会把 agent_plugin.py --agent zz-tier 真拉起来并留幽灵。
    _env = dict(os.environ, AGENTCTL_NO_RELOAD="1")
    r = subprocess.run([sys.executable, os.path.join(CORE, "scripts", "agentctl.py"),
                        "add", AID, "--apply"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=CORE, timeout=300, env=_env)
    print("── add --apply → exit=%s" % r.returncode)
    for ln in ((r.stdout or "") + (r.stderr or "")).strip().splitlines():
        print("   " + ln)
    check("档位按描述文件的 interface.tier 显式登记了（read）",
          AC._explicit_tier("plugin." + AID) == "read", AC._explicit_tier("plugin." + AID))
    try:
        raw = json.load(io.open(AC.PERM_FILE, encoding="utf-8"))
        check("permissions.json 里能找到它", ("plugin." + AID) in raw, list(raw))
    except Exception as e:
        check("permissions.json 能解析", False, e)
    # 再跑一次：已显式登记的**不该被动**
    r2 = subprocess.run([sys.executable, os.path.join(CORE, "scripts", "agentctl.py"),
                         "add", AID, "--apply"], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", cwd=CORE, timeout=300, env=_env)
    check("重跑 add 不覆盖已登记的档位（幂等）",
          AC._explicit_tier("plugin." + AID) == "read" and "保持不动" in (r2.stdout or ""),
          AC._explicit_tier("plugin." + AID))
    # trust 的那位不许被碰（回归：以前有把 dsh 档位写回 write 的事故）
    check("别人已登记的档位没被碰到（dsh 还是 trust）",
          AC._explicit_tier("plugin.dsh") in ("trust", ""), AC._explicit_tier("plugin.dsh"))
finally:
    shutil.copy2(os.path.join(SNAP, "config.yaml"), AC.CONFIG)
    if os.path.isfile(os.path.join(SNAP, "permissions.json")):
        shutil.copy2(os.path.join(SNAP, "permissions.json"), AC.PERM_FILE)
    try:
        os.remove(DESC)
    except Exception:
        pass
    print("已还原（config.yaml / permissions.json 回快照，假描述文件删掉）")

print("-" * 56)
if fails:
    print("失败 %d 条：%s" % (len(fails), "、".join(fails)))
    sys.exit(1)
print("全部通过")
