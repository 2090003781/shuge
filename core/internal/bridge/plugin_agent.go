// 本文件：外部插件（external process）的注册与身份。
//
// 协议（2026-09-06 设计；2026-09-12 辞玖定"身份声明与通道放一起"）：
//
//	插件进程启动后 POST /api/bridge/plugins/register 自报家门：
//	  id / name / capabilities / produces / consumes / description
//	  + 可选【agent 声明】：agentId / agentName / agentCapabilities / agentDescription / deliverTool
//
// **为什么身份声明并进插件，而不是单独登记一个 agent 实体**（辞玖 2026-09-12 拍板）：
//
//	身份（"谁在说话"）与通道（"怎么投递"）本来就是一件事的两面——通道死了身份也接不到东西，
//	所以它们是**同一个插件**的属性，不是两个实体。合成一条：
//	  · 组件列表一行 = 一个插件（身份作为它的一个字段展示）；
//	  · 定向投递按 target 匹配：既能用插件 id（plugin.dsh），也能用身份 id（agent.dsh）；
//	  · 停用/删除这个插件 = 通道与身份一起下线，语义直白，不留悬空身份。
//	代价（如实记账）：将来若"同一身份要有第二种实现"或"一个通道服务多个身份"，需要加字段/加映射，
//	而不是加实体。当前没有这种需求，所以选简单的那条。
package bridge

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"core/internal/plugin"
)

// ExternalPlugin 外部插件进程（平台侧的"身份牌"）。
// Send：平台 → 插件的定向投递（经插件调用通道；插件收到后自己落盘/转发——inbox 是它的业务）。
type ExternalPlugin struct {
	log      *slog.Logger
	id       string
	name     string
	caps     []string
	produces []string // 声明：本插件会产生哪些类型的事件
	consumes []string // 声明：本插件订阅/消费哪些类型的事件
	desc     string   // 自报的中文说明（"我是干什么的"）

	// ── 可选：agent 声明（通道型插件才有）────────────────────
	// 声明"我在平台里代表哪个接入方"：平台据此把定向事件投给我，并在组件列表里显示这个身份。
	agentID    string
	agentName  string
	agentCaps  []string
	agentDesc  string
	deliverTol string

	caller    PluginCaller
	timeout   time.Duration
	delivered int
}

// PluginRegistration 一次插件注册（HTTP 请求体的映射）
type PluginRegistration struct {
	ID           string
	Name         string
	Capabilities []string
	Produces     []string
	Consumes     []string
	Description  string

	AgentID           string
	AgentName         string
	AgentCapabilities []string
	AgentDescription  string
	DeliverTool       string
}

// PluginInfo 插件信息（组件列表 / 详情用）
type PluginInfo struct {
	ID           string   `json:"id"`
	Name         string   `json:"name"`
	Capabilities []string `json:"capabilities"`
	Produces     []string `json:"produces,omitempty"`
	Consumes     []string `json:"consumes,omitempty"`
	Description  string   `json:"description,omitempty"`
	// 身份声明（通道型插件才有；空表示这个插件不代表任何接入方）
	AgentID           string   `json:"agent_id,omitempty"`
	AgentName         string   `json:"agent_name,omitempty"`
	AgentCapabilities []string `json:"agent_capabilities,omitempty"`
	AgentDescription  string   `json:"agent_description,omitempty"`
}

func (p *ExternalPlugin) ID() string             { return p.id }
func (p *ExternalPlugin) Name() string           { return p.name }
func (p *ExternalPlugin) Capabilities() []string { return p.caps }
func (p *ExternalPlugin) Produces() []string     { return p.produces }
func (p *ExternalPlugin) Consumes() []string     { return p.consumes }

// Description 插件自报的中文说明
func (p *ExternalPlugin) Description() string { return p.desc }

// Info 导出为对外的信息结构
func (p *ExternalPlugin) Info() PluginInfo {
	return PluginInfo{
		ID: p.id, Name: p.name, Capabilities: p.caps,
		Produces: p.produces, Consumes: p.consumes, Description: p.desc,
		AgentID: p.agentID, AgentName: p.agentName,
		AgentCapabilities: p.agentCaps, AgentDescription: p.agentDesc,
	}
}

// HasAgent 是否代表某个接入方身份
func (p *ExternalPlugin) HasAgent() bool { return p.agentID != "" }

// Send 平台 → 插件（插件再决定怎么交给真正的接入方，例如落 inbox 文件）
func (p *ExternalPlugin) Send(ctx context.Context, ev plugin.Event) error {
	if p.caller == nil {
		return fmt.Errorf("插件 %s：调用通道未装配（bridge.SetPluginCaller）", p.id)
	}
	tool := p.deliverTol
	if tool == "" {
		tool = "agent.deliver"
	}
	args := map[string]any{"agent": p.agentID, "tool": tool, "event": envelope(ev)}
	ctx, cancel := context.WithTimeout(ctx, p.timeout)
	defer cancel()
	res, err := p.caller.CallPlugin(ctx, p.id, tool, args, p.timeout)
	if err != nil {
		return fmt.Errorf("投递给插件 %s 失败: %w", p.id, err)
	}
	if ok, has := res["ok"].(bool); has && !ok {
		return fmt.Errorf("插件 %s 拒绝投递: %v", p.id, res["error"])
	}
	p.delivered++
	if p.log != nil {
		p.log.Info("external plugin: event delivered",
			"plugin", p.id, "agent", p.agentID, "type", ev.Type, "delivered", p.delivered)
	}
	return nil
}

// Delivered 已投递事件数（状态可见性）
func (p *ExternalPlugin) Delivered() int { return p.delivered }

// envelope 事件 → 投递载荷（字段与内核版适配器落盘格式一致：type/source/target/time/payload）
func envelope(ev plugin.Event) map[string]any {
	t := ev.Time
	if t.IsZero() {
		t = time.Now()
	}
	return map[string]any{
		"type": ev.Type, "source": ev.Source, "target": ev.Target,
		"time": t.Format(time.RFC3339), "payload": ev.Payload,
	}
}

// RegisterPlugin 注册/更新一个外部插件（幂等 upsert）。返回 true 表示新增，false 表示更新。
func (b *Bridge) RegisterPlugin(reg PluginRegistration) (bool, error) {
	id := strings.TrimSpace(reg.ID)
	if id == "" {
		return false, fmt.Errorf("plugin id required")
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.plugins == nil {
		b.plugins = map[string]*ExternalPlugin{}
	}
	_, exists := b.plugins[id]
	ep := &ExternalPlugin{
		log: b.log, id: id, name: reg.Name, caps: reg.Capabilities,
		produces: reg.Produces, consumes: reg.Consumes, desc: reg.Description,
		agentID: reg.AgentID, agentName: reg.AgentName, agentCaps: reg.AgentCapabilities,
		agentDesc: reg.AgentDescription, deliverTol: reg.DeliverTool,
		caller: b.caller, timeout: 8 * time.Second,
	}
	if ep.name == "" {
		ep.name = id
	}
	if ep.agentName == "" {
		ep.agentName = ep.agentID
	}
	if ep.deliverTol == "" {
		ep.deliverTol = "agent.deliver"
	}
	b.plugins[id] = ep
	verb := "registered"
	if exists {
		verb = "re-registered"
	}
	b.log.Info("bridge: plugin "+verb, "id", id, "caps", reg.Capabilities,
		"agent", ep.agentID, "produces", reg.Produces, "consumes", reg.Consumes)
	return !exists, nil
}

// Plugins 全部外部插件信息（组件列表用）
func (b *Bridge) Plugins() []PluginInfo {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make([]PluginInfo, 0, len(b.plugins))
	for _, p := range b.plugins {
		out = append(out, p.Info())
	}
	return out
}

// PluginByID 取某个插件（不存在返回 nil）
func (b *Bridge) PluginByID(id string) *ExternalPlugin {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.plugins[id]
}

// PluginByAgent 按 agent 身份找插件（定向投递用：target 既可以是 plugin.x 也可以是 agent.x）
func (b *Bridge) PluginByAgent(agentID string) *ExternalPlugin {
	b.mu.RLock()
	defer b.mu.RUnlock()
	for _, p := range b.plugins {
		if p.agentID != "" && p.agentID == agentID {
			return p
		}
	}
	return nil
}

// PluginDeclarations 已注册外部插件的拓扑声明（id → {produces, consumes}）
func (b *Bridge) PluginDeclarations() map[string]map[string][]string {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := map[string]map[string][]string{}
	for id, p := range b.plugins {
		out[id] = map[string][]string{"produces": p.produces, "consumes": p.consumes}
	}
	return out
}

// IsExternalPluginID 判断 id 是否为外部插件（约定前缀 plugin.）
func IsExternalPluginID(id string) bool { return strings.HasPrefix(id, "plugin.") }

// PluginCaller 平台 → 外部插件 的调用通道（api 层实现，main 装配时注入）。
// 定向投递（Inject）与工具路由（能力 → 插件）都走它：带收件人、超时、回执。
type PluginCaller interface {
	CallPlugin(ctx context.Context, pluginID, tool string, args map[string]any, timeout time.Duration) (map[string]any, error)
}

// SetPluginCaller 注入插件调用通道（把 caller 灌给已注册的插件，之后注册的自动带上）
func (b *Bridge) SetPluginCaller(c PluginCaller) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.caller = c
	for _, p := range b.plugins {
		p.caller = c
	}
}

func (b *Bridge) pluginCaller() PluginCaller {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return b.caller
}
