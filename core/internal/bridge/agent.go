// 本文件：agent 抽象与通道骨架。
package bridge

import (
	"context"

	"core/internal/plugin"
)

// Agent 接入平台的 agent 抽象（骨架）。
// 实现可由外部系统提供：Reasonix 会话 / QQ 机器人 / 群聊 agent / MCP 客户端等。
type Agent interface {
	// ID 唯一标识，如 "agent.reasonix"、"agent.qq-bot"
	ID() string
	// Name 显示名
	Name() string
	// Capabilities 声明能力（供控制台展示/路由匹配）
	Capabilities() []string
	// Send 平台 → agent 的推送通道（事件注入/上下文推送）
	Send(ctx context.Context, ev plugin.Event) error
	// Start/Stop 生命周期（骨架：连接建立/释放）
	Start(ctx context.Context) error
	Stop(ctx context.Context) error
}

// AgentInfo agent 注册信息（控制台展示用）。
type AgentInfo struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	Capabilities []string `json:"capabilities"`
	// BackedBy 由哪个外部插件提供（"身份 ↔ 实现"里的实现；内核内 agent 为空）
	BackedBy string `json:"backed_by,omitempty"`
	// Description 该身份是干什么的（由背书插件自报；内核内 agent 可由 componentMeta 兜底）
	Description string `json:"description,omitempty"`
}
