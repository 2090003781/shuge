# 内核侧 agent 接口插件条目（渲染进 config.yaml 的 plugins: 列表）
# 变量：{{id}} {{agent_id}} {{plugin_id}} {{name}} {{capabilities_csv}}
  # agent 接口：{{name}}（{{agent_id}}）—— 由 scripts/agentctl.py 按 agents/{{id}}.yaml 生成
  #   身份/通道/日志入口都在它身上；能力名一个 agent 一个（内核按能力名路由）
  - id: {{plugin_id}}
    type: external
    command: python
    args: ["plugins/agent_plugin.py", "--agent", "{{id}}"]
    cwd: "."
    enabled: true
    supervisor:
      max_restarts: 5
      restart_wait_ms: 1000
