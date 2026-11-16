#!/usr/bin/env python3
"""Reasonix 接入通道（独立进程插件）——原内核内适配器 internal/bridge/adapters/reasonix.go 的外移版。

与 DSH 通道同构（共用 plugins/lib/agent_channel.py），差别只在身份与 inbox 目录：
    · 身份：plugin.reasonix 背书 agent.reasonix（reasonix 经 hook_logger 上报事件，属于"没有插件机制"的接入方）
    · 投递：平台 tool=agent.deliver → 落 data/inbox/<时间>_<类型>.json（reasonix 侧读取，格式不变）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

from agent_channel import AgentChannel  # noqa: E402

CORE_DIR = os.environ.get("CORE_DIR", os.path.dirname(HERE))
INBOX_DIR = os.path.join(CORE_DIR, "data", "inbox")  # 注意：reasonix 用的是 inbox 根目录

CHANNEL = AgentChannel(
    plugin_id="plugin.reasonix",
    name="Reasonix 接入通道",
    agent_id="agent.reasonix",
    agent_name="Reasonix",
    inbox_dir=INBOX_DIR,
    record_dir=os.path.join(CORE_DIR, "data", "transcripts", "agent.reasonix"),
    capabilities=["agent.bridge", "chat", "reasoning", "tool-use"],
    agent_capabilities=["chat", "reasoning", "tool-use"],
    produces=["platform.inject.sent"],
    consumes=["agent.reasonix"],
    status_tool="reasonix.status",
    agent_description="Reasonix 会话在平台里的身份（谁在说话）：经 hook 上报事件、会话开始取上下文包注入。",
    description="Reasonix 接入通道：我既是通道也是身份——平台要推给 reasonix 的事件由我接住，"
                "落成 data/inbox/<时间>_<类型>.json（reasonix 没有插件机制，靠 hook 上报、靠读 inbox 接收）；"
                "同时我在平台里代表 agent.reasonix。",
)

if __name__ == "__main__":
    # --mcp：同一份通道插件对外再开一个 MCP 面（给对应 agent 当工具用），
    # 见 plugins/lib/ops_mcp.py（reasonix 侧 spawn）。
    if "--mcp" in sys.argv:
        from ops_mcp import run_mcp  # noqa: E402

        sys.exit(run_mcp("plugin.reasonix", [a for a in sys.argv[1:] if a != "--mcp"]))
    CHANNEL.run()
