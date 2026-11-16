#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 agent 接口插件 —— **读 `agents/<id>.yaml`，不为每个 agent 写代码**。

一个文件两种跑法（同一个 plugin id，两条腿）：

    python plugins/agent_plugin.py --agent dsh          # ① 内核侧常驻接口（内核 Supervisor 保活）
    python plugins/agent_plugin.py --agent dsh --mcp    # ② agent 侧 MCP 服务端（agent 自己 spawn）

为什么要两个进程：MCP 的 stdio **必须由客户端 spawn**（内核拉起的进程不是 agent 的子进程），
而且生命周期不同（接口要"agent 不在时也接住事件"，MCP 跟会话走）。
但**身份、档位、工具面、日志入口都归同一个 plugin id** —— 两处都从同一份描述文件来，不各写一遍。

唯一"每个 agent 写一遍"的东西是描述文件（见 `templates/agent/README.md`）：
识别特征、接口参数（inbox/record/能力/状态工具）、MCP 与 hook 怎么接、日志源是什么。
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CORE_DIR = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, "lib"))

AGENTS_DIR = os.path.join(CORE_DIR, "agents")


def load_desc(agent: str) -> dict:
    """读 `agents/<id>.yaml`；没有就报清楚（别让它退化成"默认值"）"""
    import yaml
    p = os.path.join(AGENTS_DIR, agent + ".yaml")
    if not os.path.isfile(p):
        raise SystemExit("没有这个 agent 的描述文件：%s（见 templates/agent/README.md）" % p)
    with open(p, encoding="utf-8") as f:
        d = yaml.safe_load(f) or {}
    for k in ("agent_id", "plugin_id"):
        if not d.get(k):
            raise SystemExit("描述文件缺 %s：%s" % (k, p))
    return d


def build_channel(desc: dict):
    """按描述文件装配内核侧接口（AgentChannel 是共用件，不复制）"""
    from agent_channel import AgentChannel
    itf = desc.get("interface") or {}
    inbox = itf.get("inbox") or ("data/inbox/" + desc["id"])
    record = itf.get("record") or ("data/transcripts/" + desc["agent_id"])
    return AgentChannel(
        plugin_id=desc["plugin_id"],
        name=desc.get("name") or desc["id"],
        agent_id=desc["agent_id"],
        agent_name=desc.get("name") or desc["id"],
        inbox_dir=os.path.join(CORE_DIR, inbox.replace("/", os.sep)),
        record_dir=os.path.join(CORE_DIR, record.replace("/", os.sep)),
        capabilities=list(itf.get("capabilities") or ["agent.bridge"]),
        agent_capabilities=list(itf.get("agent_capabilities") or itf.get("capabilities")
                                or ["agent.bridge"]),
        status_tool=itf.get("status_tool") or (desc["id"] + ".status"),
        description=itf.get("description") or "",
        agent_description=itf.get("description") or "",
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="描述文件名（agents/<agent>.yaml）")
    ap.add_argument("--mcp", action="store_true", help="当 agent 侧 MCP 服务端（stdio）")
    ap.add_argument("--id", default="", help="覆盖 plugin_id（默认取描述文件）")
    a = ap.parse_args(argv)

    desc = load_desc(a.agent)
    if a.id:
        desc["plugin_id"] = a.id

    if a.mcp:                                  # ② agent 侧：MCP 服务端
        from ops_mcp import run_mcp
        rest = [x for x in (argv if argv is not None else sys.argv[1:])
                if x not in ("--mcp", "--agent", a.agent)]
        return run_mcp(desc["plugin_id"], rest)

    build_channel(desc).run(["--id", desc["plugin_id"]])   # ① 内核侧：常驻接口
    return 0


if __name__ == "__main__":
    sys.exit(main())
