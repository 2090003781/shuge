// Package plugin 定义平台插件的核心抽象。
package plugin

import (
	"context"
)

// Type 插件类型：响应式工具 / 自动化 / 协议执行
type Type string

const (
	TypeTool  Type = "tool"  // ToolPlugin：agent/人 调一次一答
	TypeAuto  Type = "auto"  // AutoPlugin：触发器驱动，主动跑
	TypeGuard Type = "guard" // GuardPlugin：挂钩子，拦截/审计/注入
)

// Plugin 是所有插件的统一接口（骨架的地基）。
type Plugin interface {
	// ID 唯一标识，如 "rag.gateway"、"event.injector"
	ID() string
	// Name 显示名
	Name() string
	// Type 插件类型
	Type() Type
	// Capabilities 声明能力（供 Registry 查询/控制台展示）
	Capabilities() []string
	// Init 初始化（加载配置、连接资源）；失败则插件不被启动
	Init(ctx context.Context, env *Env) error
	// Start 启动（Auto 注册触发器 / Guard 挂钩子 / Tool 就绪）
	Start(ctx context.Context) error
	// Stop 优雅停止
	Stop(ctx context.Context) error
}

// Env 插件运行环境：插件经它访问平台能力（不直接碰底层）
type Env struct {
	// Bus 事件总线（AutoPlugin 注册 Trigger、GuardPlugin 挂 Hook 用）
	Bus EventPublisher
	// Registry 插件注册表（插件间调用用）
	Registry PluginLookup
	// Resources 数据资源访问（经统一鉴权）
	Resources ResourceAccess
	// Config 插件自身配置
	Config map[string]any
}

// EventPublisher 事件发布接口（EventBus 的最小面）
type EventPublisher interface {
	Publish(ctx context.Context, event Event) error
	Subscribe(ctx context.Context, types []string, h Handler) (func(), error)
	// SubscribeNamed 命名订阅（拓扑快照/可追溯：谁订阅了哪些事件）
	SubscribeNamed(ctx context.Context, name string, types []string, h Handler) (func(), error)
}

// PluginLookup 插件查找接口
type PluginLookup interface {
	Get(id string) Plugin
	List() []Plugin
}

// ResourceAccess 统一资源访问（Vault/RAG/SQL，未来扩展）
type ResourceAccess interface {
	// Resource 按名字获取资源客户端（如 "rag"、"vault"、"sql"）
	Resource(name string) (any, error)
}

// Handler 事件处理函数
type Handler func(ctx context.Context, event Event) error
