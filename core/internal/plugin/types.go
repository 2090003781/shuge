// Package plugin 事件与触发器定义。
package plugin

import (
	"time"
)

// Event 一切触发的信息载体。
type Event struct {
	ID      string         `json:"id"`      // 事件唯一 ID
	Type    string         `json:"type"`    // 如 note.saved / task.checked / agent.request / injection.due
	Source  string         `json:"source"`  // 来源（插件 ID / channel / 系统）
	Payload map[string]any `json:"payload"` // 载荷
	Time    time.Time      `json:"time"`    // 发生时间
	Target  string         `json:"target"`  // 可选：定向给某插件/agent
	// Scope 影响面标记（2026-09-10 辞玖定）：
	//   ["agent"]              agent 内部活动（不涉平台，展示层不显示）
	//   ["platform"]           平台自身事件（展示层显示）
	//   ["agent","platform"]    agent 事件但涉及平台（双标记，展示层显示）
	// 展示层规则：只展示 scope 含 "platform" 的事件。
	Scope []string `json:"scope,omitempty"`
}

// ScopeAgent / ScopePlatform 影响面常量
const (
	ScopeAgent    = "agent"
	ScopePlatform = "platform"
)

// NewEvent 便捷构造（默认 scope = [platform]，平台自身事件）
func NewEvent(typ, source string, payload map[string]any) Event {
	return Event{
		ID:      source + "." + typ + "." + time.Now().Format("20060102T150405.000000000"),
		Type:    typ,
		Source:  source,
		Payload: payload,
		Time:    time.Now(),
		Scope:   []string{ScopePlatform},
	}
}

// NewEventScoped 构造带影响面标记的事件
func NewEventScoped(typ, source string, payload map[string]any, scope ...string) Event {
	e := NewEvent(typ, source, payload)
	if len(scope) > 0 {
		e.Scope = scope
	}
	return e
}

// TouchesPlatform 事件是否涉及平台（展示层过滤依据）
func (e Event) TouchesPlatform() bool {
	if len(e.Scope) == 0 {
		return true // 未标记视为平台事件（兼容旧事件）
	}
	for _, s := range e.Scope {
		if s == ScopePlatform {
			return true
		}
	}
	return false
}

// Trigger 自动化触发条件（AutoPlugin 用）。
// 四类：schedule(定时) / state(状态变化) / event(事件) / condition(规则)
type Trigger struct {
	ID         string   `json:"id"`
	Plugin     string   `json:"plugin"`                // 所属插件
	Kind       string   `json:"kind"`                  // schedule|state|event|condition
	EventTypes []string `json:"event_types,omitempty"` // event 类：订阅的事件类型
	Cron       string   `json:"cron,omitempty"`        // schedule 类：cron 表达式
	StatePath  string   `json:"state_path,omitempty"`  // state 类：监视的状态路径
	Cond       string   `json:"cond,omitempty"`        // condition 类：条件描述/表达式
	Action     []Action `json:"action"`                // 触发后执行的行为链
	Enabled    bool     `json:"enabled"`
}

// Action 行为链的一步：记录/提醒/调用工具/更新状态/发布事件
type Action struct {
	Op      string         `json:"op"`               // record|notify|tool|state|publish
	Target  string         `json:"target,omitempty"` // 操作目标
	Payload map[string]any `json:"payload,omitempty"`
}

// HookPoint 协议强制经过点（GuardPlugin 挂载位置）。
type HookPoint string

const (
	HookOutputBefore HookPoint = "output.before"  // agent 输出前
	HookDecision     HookPoint = "decision.point" // 决策点
	HookTaskDone     HookPoint = "task.done"      // 任务完成时
	HookToolBefore   HookPoint = "tool.before"    // 工具调用前
)

// HookResult 协议执行结果。
type HookResult struct {
	Allow    bool   // true=放行 false=拦截
	Reason   string // 拦截/放行原因
	Inject   string // 需要注入给 agent 的修正内容
	Severity int    // 0=通过 1=警告 2=拦截
}
