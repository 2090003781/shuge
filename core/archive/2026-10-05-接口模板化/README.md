# 2026-10-05 · agent 接口模板化（老插件文件退役）

辞玖："dsh 的部分按照模板改，统一格式不搞例外。"

## 为什么退役

原来**每个 agent 一个插件文件**（`plugins/dsh_plugin.py` / `plugins/reasonix_plugin.py`），
两份都在干同一件事：装配 `AgentChannel`（身份/通道/日志入口/探针）。差异只有几个常量
（agent_id、inbox、能力、状态工具名）—— 这正是"例外会漂移"的温床。

现在改成**一处描述、多处生成**：

    agents/<id>.yaml          ← 每个 agent 只写这一份（识别特征 + 接口参数 + MCP/hook 怎么接 + 日志源）
    plugins/agent_plugin.py   ← 通用内核侧接口（读描述文件装配 AgentChannel）
                                同一个文件 `--mcp` 时兼当 agent 侧 MCP 服务端
    scripts/agentctl.py       ← scan / plan / add --apply / verify / activate

## 退役的是哪两份

- `dsh_plugin.py`      → 等价物：`plugins/agent_plugin.py --agent dsh`（描述：`agents/dsh.yaml`）
- `reasonix_plugin.py` → 等价物：`plugins/agent_plugin.py --agent reasonix`（描述：`agents/reasonix.yaml`）

**没退役的共用件**：`plugins/lib/agent_channel.py`（通道本体）、`plugins/lib/ops_mcp.py`（MCP 面）。

## 要还原怎么办

把这两份拷回 `plugins/`，再把 `config.yaml` 里对应条目的 `args` 换回
`["plugins/dsh_plugin.py", "--id", "plugin.dsh"]` 即可（2026-10-05 之前就是这么跑的）。
