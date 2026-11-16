#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""**agent 接入不写死路径**的验收（辞玖 2026-10-06 18:08：「coze等agent的接口等等不要写死路径，
应该使用识别后的路径占位」）。

说明一处**故意保留**：渲染出来的单子里出现 `C:\Python311\pythonw.exe` 是**对的** ——
    那是本机 `sys.executable` 推出来的真值（这机器 python 就装在 C:\Python311）。
    "不写死"指的是**模板/描述文件里**不许有它，而不是"不许出现本机的真路径"。

验四件事（都可重跑）：
  ① `fill_paths()`：`{{pyw}}` / `{{core}}` 按本机识别结果填（嵌套结构也要走到）；
  ② `agents/*.yaml` 里没有写死的绝对路径；
  ③ `handoff.tpl` 里没有写死路径，且它用到的占位符渲染器都给；
  ④ 真渲染一份手工单（Coze：不知道它配置在哪 → 不许编）：不许残留 `{{...}}`、
     不许出现**别的机器**的路径（`D:/core` 这种），给出的路径必须是本机真值。

用法：`python scripts\check_agent_paths.py`
"""
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import agentctl as A  # noqa: E402

fails = []


def chk(name, cond, extra=""):
    print(("  ✅ " if cond else "  ❌ ") + name + ("" if cond else "  ← %s" % str(extra)[:200]))
    if not cond:
        fails.append(name)


def main():
    print("① fill_paths：占位符 → 本机识别结果")
    d = {"command": "{{pyw}}", "args": ["{{core}}/plugins/agent_plugin.py", "--agent", "x"],
         "cwd": "{{core}}", "hosts": [{"config": "{{core}}/x.json", "n": 1}]}
    f = A.fill_paths(d)
    chk("command 变成本机 pythonw（不弹黑窗那个）",
        f["command"].lower().endswith("pythonw.exe") and "{{" not in f["command"], f["command"])
    chk("args 里的 {{core}} 填成本机平台路径且文件在", "{{" not in f["args"][0] and os.path.isfile(f["args"][0]), f["args"][0])
    chk("cwd 填成平台目录且目录在", os.path.isdir(f["cwd"]), f["cwd"])
    chk("嵌套结构也走到（hosts[].config）", "{{" not in f["hosts"][0]["config"], f["hosts"][0])
    chk("填的是本机真值（就是本仓根）", f["cwd"] == A.CORE_DIR, f["cwd"])

    print("② agents/*.yaml 不许写死绝对路径")
    bad = []
    for p in sorted(glob.glob(os.path.join(CORE, "agents", "*.yaml"))):
        t = io.open(p, encoding="utf-8").read()
        for m in re.finditer(r"(C:\\Python|C:/Python|D:/core|D:\\core|C:\\Users)", t):
            bad.append("%s :: %s" % (os.path.basename(p), t[max(0, m.start() - 30):m.end() + 30].replace("\n", " ")))
    chk("描述文件都不写死路径", not bad, bad[:3])

    print("③ handoff.tpl 不许写死路径，占位符必须都有人填")
    tpl = io.open(os.path.join(CORE, "templates", "agent", "handoff.tpl"), encoding="utf-8").read()
    flat = tpl.replace("\\\\", "\\")
    chk("模板里没有别人的 python 路径 / 平台路径",
        "Python311" not in flat and "D:\\core" not in flat and "D:/core" not in flat,
        [l for l in flat.splitlines() if "Python311" in l or ("core" in l and "D:" in l)][:2])
    need = set(re.findall(r"\{\{([a-z_]+)\}\}", tpl))
    src = io.open(os.path.join(HERE, "agentctl.py"), encoding="utf-8").read()
    provided = set(re.findall(r'm\["([a-z_]+)"\]', src)) | {"mcp_kind"}   # mcp_kind 由描述文件带进来
    chk("模板用到的占位符渲染器都提供", not (need - provided), sorted(need - provided))

    print("④ 真渲染一份手工单（Coze：我们不知道它配置在哪 → 不许编）")
    desc = {"id": "coze", "name": "Coze（扣子）", "agent_id": "agent.coze", "plugin_id": "plugin.coze",
            "detect": {"paths": ["C:/Users/<用户目录>/AppData/Roaming/Coze"]},
            "interface": {"status_tool": "coze.status", "inbox": "data/inbox/coze",
                          "record": "data/transcripts/agent.coze",
                          "capabilities": ["chat"], "agent_capabilities": ["chat"]},
            "mcp": {"kind": "mcp-json", "server_name": "brain", "command": "{{pyw}}",
                    "args": ["{{core}}/plugins/agent_plugin.py", "--agent", "coze", "--mcp"], "cwd": "{{core}}"},
            "hook": {"kind": "none"}}
    out = A.make_onboard_prompt(A.onboard_ctx(desc))
    chk("渲染出来了（不是报错文本）", out.startswith("# 接入手工单"), out[:80])
    chk("没有残留 {{...}}", "{{" not in out, [l for l in out.splitlines() if "{{" in l][:2])
    chk("平台路径只有一处来源（本机识别结果）",
        A.CORE_DIR in out or A.CORE_DIR.replace("\\", "/") in out,
        [l for l in out.splitlines() if "core" in l and "D:" in l][:2])
    chk("给出的是本机 pythonw（sys.executable 推的）", A.pyw() in out, A.pyw())
    chk("给出的是本机平台目录", A.CORE_DIR.replace("\\", "/") in out, A.CORE_DIR)
    chk("不知道它配置在哪 → 走兜底那句", "你（或主人）知道的那个 MCP 配置文件路径" in out,
        [l for l in out.splitlines() if "config" in l.lower()][:2])

    print("")
    if fails:
        print("❌ %d 项不过：%s" % (len(fails), "、".join(fails)))
        return 1
    print("✅ agent 接入这条线不再写死路径：描述文件与模板用占位符，写配置/发手工单按本机识别结果填")
    return 0


if __name__ == "__main__":
    sys.exit(main())
