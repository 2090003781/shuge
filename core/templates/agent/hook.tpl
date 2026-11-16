# agent 侧 hook 配置片段（写进 agent 自己的配置；命令一律指到通用客户端）
#
# ⚠️ 命令一律用 **pythonw.exe**：这些命令是 agent 自己 spawn 的，我们给不了 CREATE_NO_WINDOW，
#    用 python.exe 就是每来一次工具调用闪一个黑框。pythonw 是 GUI 子系统，stdin/stdout 管道照通。
# ── kind: claude-settings（~/.claude/settings.json 的 hooks；Claude Code 用）──
#     把命令换成通用客户端（不要每个 agent 复制一份 hook 脚本）：
{
  "hooks": {
    "PostToolUse": [
      {
        "hooks": [
          {
            "command": "{{pyw_slash}} {{core}}/plugins/hook_client.py --agent {{id}}",
            "statusMessage": "📝",
            "timeout": 10,
            "type": "command"
          }
        ]
      }
    ]
  }
}

# ── kind: reasonix-toml（reasonix 没有插件机制，靠 hook 上报）──
#     它的 hook 现由 ~/.reasonix/logs/hook_logger.py 承担；模板化之后这个文件同样只做
#     "读 stdin 一条 JSON → 交给 agent 接口"，逻辑与 hook_client.py 一致。
