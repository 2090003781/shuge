#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""接入手工单自测：渲染出来的那份单子必须**能直接用**。

缘起（2026-10-05 reasonix 报的第 ① 条硬 bug）：手工单 A1 里那句
    config: "C:\\Users\\<用户目录>\\AppData\\Roaming\\reasonix\\config.toml"
YAML 双引号里 `\U` 是**非法转义** → 照抄的人 yaml.safe_load 立刻抛
`unknown escape character` → 白忙一场。同类原因还踩过：我自己写的一次性补丁脚本 docstring 里也有 `\U`。

所以这里钉住四条：
  ① 单子里那段 A1 YAML **必须能 yaml.safe_load 解析**（每个"接不了"的 agent 都验）
  ② 交出去的值**一律正斜杠**（Windows 路径不许原样进双引号）
  ③ 检测路径分三行：目录→paths、文件→files、可执行→exes（别把 .py 文件塞进 paths）
  ④ 有 hook 证据时 source.events 预填 hook；非 mcp-json 的 kind 不许出现"建空壳 {mcpServers:{}}"

跑：python scripts\smoke_handoff.py     退出码 0 = 单子能直接用
"""
import json
import os
import re
import sys

CORE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(CORE, "scripts"))
import agentctl as AC  # noqa: E402

try:
    import yaml
except Exception as e:
    print("没有 yaml 模块：%s" % e)
    sys.exit(1)

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <- " + str(extra)))
    if not cond:
        fails.append(name)


def probe_row(aid):
    """照着 find_row 里"候选表命中"那条的形状造一行（不看描述文件 —— 手工单就是给没描述文件的 agent 的）"""
    pr = next(x for x in AC.load_probes() if x.get("id") == aid)
    return {"id": aid, "name": pr.get("name") or aid, "has_desc": False, "detected": True,
            "evidence": AC.detect_markers(pr.get("markers") or []), "status": None,
            "mcp_kind": pr.get("mcp") or "none", "hook_kind": "none",
            "probe": {"config_hint": pr.get("config_hint") or "", "note": pr.get("note") or "",
                      "config_file": pr.get("config_file") or ""}}


print("接入手工单自测（渲染 → 解析 → 抽查）")
print("-" * 60)
cases = []
for aid in ("coze", "codebuddy", "reasonix"):
    try:
        cases.append((aid, probe_row(aid)))
    except StopIteration:
        check("候选表里有 %s" % aid, False, "没有这条")
try:
    u = AC.find_row("unknown:.mcp.json") or AC.find_row("unknown:mcp.json")
    if u:
        cases.append((u["id"], u))
except Exception as e:
    check("模糊发现那条能取到", False, e)

for aid, row in cases:
    text = AC.make_onboard_prompt(row)
    m = re.search(r"```yaml\r?\n(.*?)\r?\n```", text, re.S)
    if not m:
        check("%s：单子里有 A1 的 yaml 块" % aid, False, text[:120])
        continue
    body = m.group(1)
    try:
        d = yaml.safe_load(body)
        check("%s：A1 的 YAML 能解析（①）" % aid, isinstance(d, dict), d)
    except Exception as e:
        check("%s：A1 的 YAML 能解析（①）" % aid, False, "%s\n%s" % (e, body[:300]))
        continue
    # ② 只看**我们注入的值**（mcp.config + detect 三项）：不许有反斜杠。
    #    command: 里是本机 pythonw 的真路径（来自 sys.executable），反斜杠是**合法** YAML 转义，不算问题。
    inj = [str((d.get("mcp") or {}).get("config") or "")]
    det0 = d.get("detect") or {}
    for k in ("paths", "files", "exes"):
        inj += [str(x) for x in (det0.get(k) or [])]
    bad = [x for x in inj if "\\" in x]
    check("%s：我们注入的值都是正斜杠（②）" % aid, not bad, bad[:2])
    # ③ 检测路径分三类
    det = d.get("detect") or {}
    check("%s：paths 里没有文件（③）" % aid,
          all(not str(p).lower().endswith((".py", ".json", ".toml", ".yaml", ".yml"))
              for p in (det.get("paths") or [])), det.get("paths"))
    # ④ hook 证据 → events 预填 hook；非 mcp-json 不出现"建空壳"
    ev = [str(x) for x in (row.get("evidence") or [])]
    has_hook = any(("hook" in s.lower() or "settings.json" in s.lower()) for s in ev)
    if has_hook:
        check("%s：有 hook 证据 → source.events=hook（④）" % aid,
              (d.get("source") or {}).get("events") == "hook", (d.get("source") or {}))
    # ⑤ 单子里**不许出现空的代码段**（`` 这种：路径是空的，读起来像 bug）
    #    ⑥ 每份单子都要带 A1.5 的「格式 × 形状」对照表（写错形状会冲掉别人的服务）
    _nofence = text.replace("```", "")          # 围栏 ``` 里含 `` —— 先剥掉再查
    check("%s：没有空路径的代码段（``）" % aid, "``" not in _nofence,
          [l.strip()[:100] for l in _nofence.splitlines() if "``" in l][:2])
    # 信箱 2026-10-06 拆掉：单子里不许再叫对方往我们盘上写报告（在别人电脑上写不回来）
    check("%s：不叫对方往我们盘上写报告" % aid,
          "data/onboard" not in text and "data\\onboard" not in text, "")
    check("%s：失败时叫它停下来（不是硬接）" % aid, "别硬接" in text, "")
    check("%s：带了 A1.5 格式×形状对照表" % aid,
          "| 格式 | 表名 / 顶层键 | 谁是这样 |" in text and "mcp_servers" in text, "")
    # 表格行**各占一行**（踩过：`\n` 被写成字面量，6 行挤成一行，渲染出来就是一坨）
    check("%s：对照表每行各占一行（没有字面量 \\n）" % aid,
          "\\n" not in text and any(l.startswith("| `toml` | `mcp_servers` |")
                                     for l in text.splitlines()), "")
    kind = (d.get("mcp") or {}).get("kind") or ""
    if kind and kind != "mcp-json":
        step0 = next((l for l in text.splitlines() if l.strip().startswith(":: 0)")), "")
        check("%s：kind=%s 时第 0 步不提「建空壳 mcpServers」（④）" % (aid, kind),
              ('{"mcpServers"' not in step0) and ("别去建空壳" in step0 or "官方 CLI" in step0), step0[:120])
        check("%s：kind=%s 时指路官方 CLI（⑥）" % (aid, kind), "官方 CLI" in step0, step0[:120])

print("-" * 60)
if fails:
    print("失败 %d 条：%s" % (len(fails), "、".join(fails)))
    sys.exit(1)
print("全部通过（%d 份单子）" % len(cases))
