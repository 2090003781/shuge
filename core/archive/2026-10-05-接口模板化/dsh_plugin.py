#!/usr/bin/env python3
"""DSH 接入通道（独立进程插件）——原内核内适配器 internal/bridge/adapters/dsh.go 的外移版。

逻辑全在共用件 plugins/lib/agent_channel.py（通道型插件同构），本文件只填 DSH 的参数。
    · 身份：plugin.dsh 背书 agent.dsh → 组件列表/拓扑/Inject(target=agent.dsh) 对外不变
    · 投递：平台 tool=agent.deliver → 落 data/inbox/dsh/<时间>_<类型>.json（DSH 侧照旧取件，格式不变）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "lib"))

from agent_channel import AgentChannel  # noqa: E402

CORE_DIR = os.environ.get("CORE_DIR", os.path.dirname(HERE))
INBOX_DIR = os.path.join(CORE_DIR, "data", "inbox", "dsh")

CHANNEL = AgentChannel(
    plugin_id="plugin.dsh",
    name="DSH 接入通道",
    agent_id="agent.dsh",
    agent_name="DSH（DeepSeek Harness）",
    inbox_dir=INBOX_DIR,
    record_dir=os.path.join(CORE_DIR, "data", "transcripts", "agent.dsh"),
    capabilities=["agent.bridge", "chat", "reasoning", "tool-use", "transcript", "second-brain"],
    agent_capabilities=["chat", "reasoning", "tool-use", "transcript", "second-brain"],
    produces=["platform.inject.sent"],
    consumes=["agent.dsh"],
    status_tool="dsh.status",
    agent_description="DSH 会话在平台里的身份（谁在说话）：会话开始取上下文包注入、过程事件与工具调用进总线与账本。",
    description="DSH（DeepSeek Harness）接入通道：我既是通道也是身份——平台要推给 DSH 的事件由我接住，"
                "落成 data/inbox/dsh/<时间>_<类型>.json，DSH 侧插件在下一轮对话前取件注入；"
                "同时我在平台里代表 agent.dsh（定向事件按这个身份投给我，会话与事件也记在它名下）。",
)

if __name__ == "__main__":
    # 两种面同一个文件：
    #   （默认）通道面 —— 平台 spawn 我，接定向投递、落 inbox、看护父进程；
    #   --mcp     MCP 面 —— **DSH 侧** spawn 我，把我这个 agent 被释放的软件操作
    #             当工具发给模型（见 plugins/lib/ops_mcp.py）。两者进程互不相干。
    if "--mcp" in sys.argv:
        from ops_mcp import run_mcp  # noqa: E402

        sys.exit(run_mcp("plugin.dsh", [a for a in sys.argv[1:] if a != "--mcp"]))
    CHANNEL.run()
