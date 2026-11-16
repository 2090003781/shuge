# agent 侧 MCP 配置片段（写进 agent 自己的配置文件；按 mcp.kind 选落点）
# ⚠️ command 一律用 **pythonw.exe**（不是 python.exe）：这个进程是 agent 自己 spawn 的，
#    我们给不了 CREATE_NO_WINDOW —— python.exe 会每次都闪一个黑框，pythonw 不会（管道照通）。
#
# ── kind: mcp-json（Claude Code 的 ~/.claude.json，以及任何吃 {"mcpServers": {...}} 的客户端）──
{
  "mcpServers": {
    "{{server_name}}": {
      "command": "{{command}}",
      "args": {{args_json}},
      "cwd": "{{cwd}}"
    }
  }
}

# ── kind: codex-toml（~/.codex/config.toml 追加一段）──
# [mcp_servers.{{server_name}}]
# command = "{{command_toml}}"
# args = {{args_toml}}
# cwd = "{{cwd_toml}}"

# ── kind: dsh-plugin（~/.dsh/profiles/web/cordis.patch.yml 追加一条）──
#    注意：DSH 的 MCP 宿主是 @deepseek-ai/dsh-mcp-client，它支持两种传输：
#      stdio             → command/args（本地程序）
#      streamable-http   → url/headers（服务端；将来内核侧一个进程同时当 MCP 服务端时用它）
# - id: mcp-{{id}}
#   name: '@deepseek-ai/dsh-mcp-client'
#   config:
#     serverName: {{server_name}}
#     transport: stdio
#     command: '{{command}}'
#     args: {{args_yaml}}
#     cwd: '{{cwd}}'
#     toolCallTimeoutMs: 180000
#     failOnStartupError: false
