// Package bridge 中间平台主框架（supervisor 主框架）：连接 agent 与笔记软件（core-host）。
//
// 设计原则（对应 2026-09-03/09-05 讨论）：
//  1. agent 以"固定格式输出"接入：一条 AgentRequest 可以是工具调用、事件上报、查询或日志
//  2. 平台向 agent 反向注入事件/上下文（EventEnvelope = plugin.Event 复用）
//  3. 所有功能以 Module/插件形式挂载（热缓存知识索引 / RAG 网关 / 规则监督 / 日志……）
//  4. 纯骨架期：只定义类型与接口 + 最小路由，不实现具体能力
package bridge

import (
	"time"

	"core/internal/plugin"
)

// RequestKind agent 请求类型：agent 发来的固定格式消息分类。
type RequestKind string

const (
	KindToolCall RequestKind = "tool-call" // agent 调用平台工具
	KindEvent    RequestKind = "event"     // agent 上报事件（如 note.saved / log）
	KindQuery    RequestKind = "query"     // agent 查询平台能力/知识
	KindLog      RequestKind = "log"       // agent 记录日志
)

// ToolCall agent 发起的工具调用。
type ToolCall struct {
	Tool string         `json:"tool"` // 工具名，如 "knowledge.search" / "vault.read"
	Args map[string]any `json:"args,omitempty"`
}

// AgentRequest 一条从 agent 进入平台的消息（agent → bridge）。
type AgentRequest struct {
	ID      string         `json:"id"`
	AgentID string         `json:"agentId"` // 来源 agent
	Kind    RequestKind    `json:"kind"`
	Call    *ToolCall      `json:"call,omitempty"`  // Kind=KindToolCall
	Event   *plugin.Event  `json:"event,omitempty"` // Kind=KindEvent
	Query   string         `json:"query,omitempty"` // Kind=KindQuery
	Payload map[string]any `json:"payload,omitempty"`
	Time    time.Time      `json:"time"`
}

// ToolResult 工具调用结果。
type ToolResult struct {
	Ok    bool   `json:"ok"`
	Data  any    `json:"data,omitempty"`
	Error string `json:"error,omitempty"`
}

// AgentResponse 平台返回给 agent 的消息（bridge → agent）。
type AgentResponse struct {
	ID     string         `json:"id"` // 对应请求 ID
	Kind   RequestKind    `json:"kind"`
	Result *ToolResult    `json:"result,omitempty"`
	Events []plugin.Event `json:"events,omitempty"` // 附带注入给 agent 的事件
	Error  string         `json:"error,omitempty"`
}

// NewAgentRequest 便捷构造。
func NewAgentRequest(agentID string, kind RequestKind) AgentRequest {
	return AgentRequest{
		ID:      "req." + agentID + "." + time.Now().Format("20060102T150405.000000000"),
		AgentID: agentID,
		Kind:    kind,
		Time:    time.Now(),
	}
}
