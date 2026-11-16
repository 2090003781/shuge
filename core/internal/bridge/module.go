// 本文件：功能模块（Module）抽象 —— 所有平台能力以模块形式挂到 Bridge 上。
// 典型模块（后续实现）：知识索引（热缓存）、RAG 网关、规则监督、日志记录、上下文注入器。
package bridge

import (
	"context"

	"core/internal/eventbus"
)

// ModuleEnv 模块运行环境：经它访问平台能力（Bus 事件发布/订阅、Bridge 本体）。
type ModuleEnv struct {
	// Bus 事件总线（模块可订阅 note.saved 等笔记事件，做增量触发）
	Bus *eventbus.Bus
	// Bridge 平台本体（可查 agent / 注入事件）
	Bridge *Bridge
}

// Module 平台功能模块接口（骨架）。
// 约定：模块只做"一件事"，经 Bridge 暴露能力，互不直接耦合。
type Module interface {
	// ID 唯一标识，如 "module.knowledge-index" / "module.rag-gateway"
	ID() string
	// Name 显示名
	Name() string
	// Capabilities 声明能力（agent 工具路由依据），如 "knowledge.search"
	Capabilities() []string
	// Init 初始化（加载配置/资源）；失败则模块不启动
	Init(ctx context.Context, env *ModuleEnv) error
	// Start 启动（注册触发器/就绪）
	Start(ctx context.Context) error
	// Stop 优雅停止
	Stop(ctx context.Context) error
}

// ToolModule 可选接口：模块实现它即可处理 agent 的工具调用（KindToolCall 路由到能力名）。
type ToolModule interface {
	Module
	// HandleTool 处理一次工具调用。call.Tool 必须是本模块 Capabilities 之一。
	HandleTool(ctx context.Context, call ToolCall) (ToolResult, error)
}

// ModuleInfo 模块注册信息（控制台展示用）。
type ModuleInfo struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	Capabilities []string `json:"capabilities"`
}

// ---- 骨架内置模块注册表 ---- //

// registry 模块注册表（Bridge 内部用）。
type moduleRegistry struct {
	order   []string
	modules map[string]Module
}

func newModuleRegistry() *moduleRegistry {
	return &moduleRegistry{modules: make(map[string]Module)}
}

func (r *moduleRegistry) register(m Module) error {
	if _, exists := r.modules[m.ID()]; exists {
		return &ModuleDuplicateError{ID: m.ID()}
	}
	r.modules[m.ID()] = m
	r.order = append(r.order, m.ID())
	return nil
}

func (r *moduleRegistry) get(id string) Module { return r.modules[id] }

func (r *moduleRegistry) list() []Module {
	out := make([]Module, 0, len(r.order))
	for _, id := range r.order {
		if m, ok := r.modules[id]; ok {
			out = append(out, m)
		}
	}
	return out
}

// ModuleDuplicateError 模块重复注册错误
type ModuleDuplicateError struct{ ID string }

func (e *ModuleDuplicateError) Error() string { return "module already registered: " + e.ID }
