// 本文件：Bridge 主类型 —— 中间平台主框架的装配与生命周期。
package bridge

import (
	"context"
	"log/slog"
	"sync"
	"time"

	"core/internal/eventbus"
	"core/internal/plugin"
)

// Bridge 中间平台（supervisor 主框架）。
//
// 职责（纯骨架）：
//  1. 管理接入的 agent（注册/查找/注入）
//  2. 管理挂载的能力模块（注册/生命周期）
//  3. 在 agent 与模块之间路由请求（骨架：send 直接转发给模块/发布事件）
//  4. 把笔记软件事件（note.saved 等）转发给关心的 agent（事件注入雏形）
type Bridge struct {
	bus *eventbus.Bus
	log *slog.Logger

	mu      sync.RWMutex
	agents  map[string]Agent           // 内核内 agent（当前无；外部插件背书的身份不在这里）
	plugins map[string]*ExternalPlugin // 外部插件进程（身份声明是它的字段）
	modules *moduleRegistry
	caller  PluginCaller // 平台 → 外部插件 的调用通道（api 层注入；插件背书的 agent 用它投递）

	started bool
}

// Options Bridge 创建选项
type Options struct {
	Bus    *eventbus.Bus
	Logger *slog.Logger
}

// New 创建 Bridge
func New(opts Options) *Bridge {
	log := opts.Logger
	if log == nil {
		log = slog.Default()
	}
	return &Bridge{
		bus:     opts.Bus,
		plugins: map[string]*ExternalPlugin{},
		log:     log,
		agents:  make(map[string]Agent),
		modules: newModuleRegistry(),
	}
}

// ---- agent 管理 ---- //

// RegisterAgent 注册 agent（重复 ID 报错）
func (b *Bridge) RegisterAgent(a Agent) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if _, exists := b.agents[a.ID()]; exists {
		return &AgentDuplicateError{ID: a.ID()}
	}
	b.agents[a.ID()] = a
	b.log.Info("bridge: agent registered", "id", a.ID())
	return nil
}

// Agents 全部 agent 信息
func (b *Bridge) Agents() []AgentInfo {
	b.mu.RLock()
	defer b.mu.RUnlock()
	// 只返回**内核内** agent：外部插件的身份声明是插件自己的字段（见 Plugins()），
	// 不再作为独立实体出现在列表里（2026-09-12 辞玖定：身份声明与通道放一起）。
	out := make([]AgentInfo, 0, len(b.agents))
	for _, a := range b.agents {
		out = append(out, AgentInfo{ID: a.ID(), Name: a.Name(), Capabilities: a.Capabilities()})
	}
	return out
}

// agent 数量（供状态接口）
func (b *Bridge) agentCount() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return len(b.agents)
}

// ---- 模块管理 ---- //

// RegisterModule 注册模块（重复 ID 报错）
func (b *Bridge) RegisterModule(m Module) error {
	if err := b.modules.register(m); err != nil {
		return err
	}
	b.log.Info("bridge: module registered", "id", m.ID())
	if b.bus != nil {
		_ = b.bus.Publish(context.Background(), plugin.NewEvent("platform.module.registered", "bridge",
			map[string]any{"id": m.ID(), "name": m.Name()}))
	}
	return nil
}

// Modules 全部模块信息
func (b *Bridge) Modules() []ModuleInfo {
	ms := b.modules.list()
	out := make([]ModuleInfo, 0, len(ms))
	for _, m := range ms {
		out = append(out, ModuleInfo{ID: m.ID(), Name: m.Name(), Capabilities: m.Capabilities()})
	}
	return out
}

// 模块数量（供状态接口）
func (b *Bridge) moduleCount() int { return len(b.modules.list()) }

// Start 启动全部模块（并启动已注册 agent）
func (b *Bridge) Start(ctx context.Context) error {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.started {
		return nil
	}
	env := &ModuleEnv{Bus: b.bus, Bridge: b}
	for _, m := range b.modules.list() {
		if err := m.Init(ctx, env); err != nil {
			b.log.Error("bridge: module init failed", "id", m.ID(), "err", err)
			continue
		}
		if err := m.Start(ctx); err != nil {
			b.log.Error("bridge: module start failed", "id", m.ID(), "err", err)
			continue
		}
		b.log.Info("bridge: module started", "id", m.ID())
		if b.bus != nil {
			_ = b.bus.Publish(context.Background(), plugin.NewEvent("platform.module.started", "bridge",
				map[string]any{"id": m.ID()}))
		}
	}
	for _, a := range b.agents {
		if err := a.Start(ctx); err != nil {
			b.log.Error("bridge: agent start failed", "id", a.ID(), "err", err)
		}
	}
	b.started = true
	return nil
}

// Stop 优雅停止全部模块与 agent
func (b *Bridge) Stop(ctx context.Context) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if !b.started {
		return
	}
	ms := b.modules.list()
	for i := len(ms) - 1; i >= 0; i-- {
		_ = ms[i].Stop(ctx)
	}
	for _, a := range b.agents {
		_ = a.Stop(ctx)
	}
	b.started = false
}

// ---- 消息路由（骨架） ---- //

// Handle 处理一条 agent 请求（骨架期最小语义）：
//   - KindEvent：agent 上报事件 → 注入转发给已注册 agent（验证 Send 通道/事件注入雏形）
//   - 其他 kind：未接入模块路由，返回 not implemented
func (b *Bridge) Handle(ctx context.Context, req AgentRequest) (AgentResponse, error) {
	b.log.Info("bridge: handle request",
		"id", req.ID, "agent", req.AgentID, "kind", req.Kind)

	switch req.Kind {
	case KindEvent:
		ev := req.Event
		if ev == nil {
			ev = &plugin.Event{Type: "bridge.event", Source: req.AgentID, Time: req.Time}
		}
		if ev.Source == "" {
			ev.Source = req.AgentID
		}
		if ev.Time.IsZero() {
			ev.Time = time.Now()
		}
		// 影响面标记：agent 上报的业务事件同时涉及 agent 与平台（双标记，展示层可见）
		if len(ev.Scope) == 0 {
			ev.Scope = []string{plugin.ScopeAgent, plugin.ScopePlatform}
		}
		// 1) 广播到平台事件总线：控制台事件流 / 日志模块等订阅者均能收到
		_ = b.bus.Publish(ctx, *ev)
		// 2) 仅当事件带 Target 时定向注入（避免高频事件污染所有 agent 的 inbox；
		//    需要通知某 agent 时由其产出方设置 event.target）
		if ev.Target != "" {
			_ = b.Inject(ctx, ev.Target, *ev)
		}
		return AgentResponse{ID: req.ID, Kind: req.Kind}, nil
	case KindToolCall:
		resp, _ := b.handleToolCall(ctx, req)
		// 捕获层：每次工具调用都发事件（agent 活动事件化，日志/账本可记录）
		b.publishToolCall(ctx, req, resp)
		return resp, nil
	default:
		return AgentResponse{
			ID:    req.ID,
			Kind:  req.Kind,
			Error: "not implemented: bridge 骨架期未接入模块路由",
		}, nil
	}
}

// handleToolCall 路由工具调用到声明该能力的模块（实现了 ToolModule 者）。
func (b *Bridge) handleToolCall(ctx context.Context, req AgentRequest) (AgentResponse, error) {
	if req.Call == nil {
		return AgentResponse{ID: req.ID, Kind: req.Kind,
			Error: "tool-call missing call"}, nil
	}
	tool := req.Call.Tool
	for _, m := range b.modules.list() {
		if !hasCapability(m.Capabilities(), tool) {
			continue
		}
		tm, ok := m.(ToolModule)
		if !ok {
			b.log.Warn("bridge: module declares capability but not ToolModule",
				"module", m.ID(), "tool", tool)
			continue
		}
		res, err := tm.HandleTool(ctx, *req.Call)
		if err != nil {
			b.log.Error("bridge: tool call failed", "tool", tool, "err", err)
			return AgentResponse{ID: req.ID, Kind: req.Kind, Result: &ToolResult{
				Ok: false, Error: err.Error(),
			}}, nil
		}
		return AgentResponse{ID: req.ID, Kind: req.Kind, Result: &res}, nil
	}
	// 内核里没有模块认领这个工具 → 看**外部插件**谁声明了它（能力名 == 工具名）
	//
	// 这是"内核内能力外移"缺的最后一块协议（2026-09-12 检修步 5）：
	// 工具调用原本只在内核模块里找（module.note-search），搬成插件后必须能按能力路由过去，
	// 否则插件再健康也接不到活。走的是同一条插件调用通道（带超时与回执）。
	if pluginID, ok := b.externalPluginFor(tool); ok {
		if b.pluginCaller() == nil {
			return AgentResponse{ID: req.ID, Kind: req.Kind, Result: &ToolResult{
				Ok: false, Error: "插件调用通道未装配，无法路由工具 " + tool,
			}}, nil
		}
		args := map[string]any{}
		if req.Call.Args != nil {
			args = req.Call.Args
		}
		ctx2, cancel := context.WithTimeout(ctx, 20*time.Second)
		defer cancel()
		res, err := b.pluginCaller().CallPlugin(ctx2, pluginID, tool, args, 20*time.Second)
		if err != nil {
			b.log.Error("bridge: plugin tool call failed", "plugin", pluginID, "tool", tool, "err", err)
			return AgentResponse{ID: req.ID, Kind: req.Kind, Result: &ToolResult{
				Ok: false, Error: err.Error(),
			}}, nil
		}
		ok, _ := res["ok"].(bool)
		if !ok {
			msg, _ := res["error"].(string)
			if msg == "" {
				msg = "插件返回 ok=false"
			}
			return AgentResponse{ID: req.ID, Kind: req.Kind, Result: &ToolResult{Ok: false, Error: msg}}, nil
		}
		b.log.Info("bridge: tool routed to plugin", "plugin", pluginID, "tool", tool)
		return AgentResponse{ID: req.ID, Kind: req.Kind, Result: &ToolResult{Ok: true, Data: res["data"]}}, nil
	}
	return AgentResponse{ID: req.ID, Kind: req.Kind, Result: &ToolResult{
		Ok:    false,
		Error: "no module/plugin provides tool: " + tool,
	}}, nil
}

// externalPluginFor 找一个声明了该能力（能力名 == 工具名）的外部插件
func (b *Bridge) externalPluginFor(tool string) (string, bool) {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for id, p := range b.plugins {
		if hasCapability(p.caps, tool) {
			return id, true
		}
	}
	return "", false
}

func hasCapability(caps []string, tool string) bool {
	for _, c := range caps {
		if c == tool {
			return true
		}
	}
	return false
}

// publishToolCall 把一次工具调用发布为 agent.tool-call 事件（捕获层：让 agent 活动进总线可记录）。
func (b *Bridge) publishToolCall(ctx context.Context, req AgentRequest, resp AgentResponse) {
	if req.Call == nil {
		return
	}
	payload := map[string]any{
		"agent":     req.AgentID,
		"tool":      req.Call.Tool,
		"args":      req.Call.Args,
		"requestId": req.ID,
	}
	if resp.Result != nil {
		payload["ok"] = resp.Result.Ok
		if resp.Result.Error != "" {
			payload["error"] = resp.Result.Error
		}
		if resp.Result.Data != nil {
			payload["hasResult"] = true
		}
	}
	_ = b.bus.Publish(ctx, plugin.NewEventScoped("agent.tool-call", "bridge", payload,
		plugin.ScopeAgent, plugin.ScopePlatform)) // 双标记：agent 活动 + 涉及平台（调平台能力）
}

// Inject 事件注入雏形：把事件推送给指定 agent（Target 为空 = 全部）。
// 骨架期仅记录；后续按 agent 能力订阅过滤。
func (b *Bridge) Inject(ctx context.Context, target string, ev plugin.Event) error {
	b.mu.RLock()
	defer b.mu.RUnlock()
	delivered := []string{}
	failed := []string{}
	// 内核内 agent：按 id 匹配
	for id, a := range b.agents {
		if target != "" && target != id {
			continue
		}
		if err := a.Send(ctx, ev); err != nil {
			b.log.Error("bridge: inject failed", "agent", id, "err", err)
			failed = append(failed, id)
			continue
		}
		delivered = append(delivered, id)
	}
	// 外部插件：target 既可以是插件 id（plugin.dsh），也可以是它声明的身份（agent.dsh）
	for pid, p := range b.plugins {
		if target != "" && target != pid && target != p.agentID {
			continue
		}
		if err := p.Send(ctx, ev); err != nil {
			b.log.Error("bridge: inject failed", "plugin", pid, "err", err)
			failed = append(failed, pid)
			continue
		}
		delivered = append(delivered, pid)
	}
	// 推送失败留痕：inject.failed（可追溯"推给谁失败了、为什么"）
	if len(failed) > 0 && b.bus != nil {
		_ = b.bus.Publish(ctx, plugin.NewEventScoped("platform.inject.failed", "bridge", map[string]any{
			"target": target, "failed": failed, "eventType": ev.Type,
		}, plugin.ScopePlatform))
	}
	// 推送行为留痕：定向推送必须进总线可查（谁/何时/把什么/推给了谁）
	if len(delivered) > 0 && b.bus != nil {
		_ = b.bus.Publish(ctx, plugin.NewEvent("platform.inject.sent", "bridge", map[string]any{
			"target":    target,
			"delivered": delivered,
			"eventType": ev.Type,
		}))
	}
	b.log.Info("bridge: event injected", "type", ev.Type, "target", target, "delivered", len(delivered))
	return nil
}

// ---- 错误 ---- //

// AgentDuplicateError agent 重复注册错误
type AgentDuplicateError struct{ ID string }

func (e *AgentDuplicateError) Error() string { return "agent already registered: " + e.ID }
