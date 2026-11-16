// 本文件：上下文包管理（注入链路平台侧）。
//
// 设计（辞玖 2026-09-10 定稿）：
//
//	· 插件（plugin.context-pack）只负责"按配置准备包"——写 data/context/<agent>.md（定时/触发刷新）
//	· 平台只负责"取包 + 开关判定"——agent 接口直读文件（零转接）
//	· 可用性 = 启停开关（不是进程活着）：插件级开关（pluginmgr）+ agent 级开关（本文件）
//	· 禁用 = 不注入（语义干净，不用旧包）
//
// 数据：
//
//	config.yaml: agent.session_start.routes（默认路由：agent → docs + enabled）
//	data/context/routes.json（运行时开关覆盖，控制台可改）
//	data/context/<agent>.md（注入包，由插件生成）
package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"core/internal/plugin"
)

// ContextRoute 一条注入路由（agent → 注入内容）
type ContextRoute struct {
	Agent   string   `json:"agent"`
	Enabled bool     `json:"enabled"`
	Docs    []string `json:"docs,omitempty"`
}

// ContextPackManager 上下文包管理（取包 + 开关）
type ContextPackManager struct {
	mu       sync.RWMutex
	dir      string                   // 包目录（相对 core 运行目录，如 data/context）
	routes   map[string]*ContextRoute // agent → 路由（含运行时开关覆盖）
	override map[string]bool          // 运行时开关覆盖（routes.json）
}

// NewContextPackManager 创建包管理器。defaults 来自 config 的默认路由。
func NewContextPackManager(dir string, defaults []ContextRoute) *ContextPackManager {
	if dir == "" {
		dir = filepath.Join("data", "context")
	}
	m := &ContextPackManager{
		dir:      dir,
		routes:   map[string]*ContextRoute{},
		override: map[string]bool{},
	}
	for i := range defaults {
		r := defaults[i]
		m.routes[r.Agent] = &ContextRoute{Agent: r.Agent, Enabled: r.Enabled, Docs: r.Docs}
	}
	m.loadOverrides() // 运行时开关覆盖优先
	return m
}

func (m *ContextPackManager) routesFile() string {
	return filepath.Join(m.dir, "routes.json")
}

// loadOverrides 读运行时开关覆盖
func (m *ContextPackManager) loadOverrides() {
	m.mu.Lock()
	defer m.mu.Unlock()
	data, err := os.ReadFile(m.routesFile())
	if err != nil {
		return
	}
	_ = json.Unmarshal(data, &m.override)
	for agent, enabled := range m.override {
		if r, ok := m.routes[agent]; ok {
			r.Enabled = enabled
		} else {
			// 覆盖里有但 config 没有：补一条（兼容控制台新加）
			m.routes[agent] = &ContextRoute{Agent: agent, Enabled: enabled}
		}
	}
}

// saveOverrides 写运行时开关覆盖（供插件与重启后继续生效）
func (m *ContextPackManager) saveOverrides() error {
	if err := os.MkdirAll(m.dir, 0o755); err != nil {
		return err
	}
	data, err := json.MarshalIndent(m.override, "", "  ")
	if err != nil {
		return err
	}
	return os.WriteFile(m.routesFile(), data, 0o644)
}

// packPath 某 agent 的包文件路径
func (m *ContextPackManager) packPath(agent string) string {
	return filepath.Join(m.dir, safeAgentName(agent)+".md")
}

// safeAgentName 防路径穿越（agent id → 安全文件名）
func safeAgentName(agent string) string {
	var b strings.Builder
	for _, r := range agent {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9') || r == '.' || r == '-' || r == '_' {
			b.WriteRune(r)
		} else {
			b.WriteRune('_')
		}
	}
	if b.Len() == 0 {
		return "unknown"
	}
	return b.String()
}

// Routes 路由列表（含包状态，供控制台展示）
func (m *ContextPackManager) Routes() []map[string]any {
	m.mu.RLock()
	defer m.mu.RUnlock()
	out := []map[string]any{}
	for _, r := range m.routes {
		info := map[string]any{
			"agent": r.Agent, "enabled": r.Enabled, "docs": r.Docs,
			"packPath": filepath.ToSlash(m.packPath(r.Agent)),
		}
		if st, err := os.Stat(m.packPath(r.Agent)); err == nil {
			info["packBytes"] = st.Size()
			info["packUpdated"] = st.ModTime().Format(time.RFC3339)
		} else {
			info["packBytes"] = 0
		}
		out = append(out, info)
	}
	return out
}

// SetEnabled 设置某 agent 的注入开关（运行时覆盖，持久化）
func (m *ContextPackManager) SetEnabled(agent string, enabled bool) error {
	m.mu.Lock()
	r, ok := m.routes[agent]
	if !ok {
		r = &ContextRoute{Agent: agent, Enabled: enabled}
		m.routes[agent] = r
	}
	r.Enabled = enabled
	m.override[agent] = enabled
	err := m.saveOverrides()
	m.mu.Unlock()
	return err
}

// Pack 取包（注入用）：返回内容与状态。
// status: "ok"（有包）| "empty"（启用但包未生成）| "disabled"（开关关闭，不注入）| "not-configured"（无此 agent 路由）
func (m *ContextPackManager) Pack(agent string) (content string, status string, err error) {
	m.mu.RLock()
	r, ok := m.routes[agent]
	if !ok {
		m.mu.RUnlock()
		return "", "not-configured", nil
	}
	isEnabled := r.Enabled
	m.mu.RUnlock()
	if !isEnabled {
		return "", "disabled", nil
	}
	data, rerr := os.ReadFile(m.packPath(agent))
	if rerr != nil {
		if os.IsNotExist(rerr) {
			return "", "empty", nil // 启用但包还没生成
		}
		return "", "error", rerr
	}
	return string(data), "ok", nil
}

// ---- HTTP handlers ----

// handleContextRoutes GET /api/context/routes —— 注入路由列表（控制台开关面板数据源）
func (s *Server) handleContextRoutes(w http.ResponseWriter, r *http.Request) {
	if s.ctxPacks == nil {
		writeJSON(w, []any{})
		return
	}
	writeJSON(w, s.ctxPacks.Routes())
}

// handleContextRouteSet POST /api/context/routes —— 设置某 agent 注入开关
func (s *Server) handleContextRouteSet(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if s.ctxPacks == nil {
		http.Error(w, "context packs not enabled", http.StatusServiceUnavailable)
		return
	}
	var body struct {
		Agent   string `json:"agent"`
		Enabled bool   `json:"enabled"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.Agent == "" {
		http.Error(w, "agent required", http.StatusBadRequest)
		return
	}
	if err := s.ctxPacks.SetEnabled(body.Agent, body.Enabled); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	// 留痕：开关变更事件
	if s.bus != nil {
		_ = s.bus.Publish(r.Context(), plugin.NewEvent("platform.config.route-changed", "console",
			map[string]any{"agent": body.Agent, "enabled": body.Enabled}))
	}
	writeJSON(w, map[string]any{"ok": true, "agent": body.Agent, "enabled": body.Enabled})
}

// handleAgentSessionStart POST /api/agent/session-start —— agent 会话开始（agent 接口的注入执行点）
//
// 流程（辞玖 2026-09-10 定稿）：查开关 → 直读包（零转接）→ 记 agent.session.started（含注入结果）→ 返回
// body: {agent, sessionId?}
// 响应: {injected, reason, content, bytes}   reason: ok|disabled|not-configured|empty
func (s *Server) handleAgentSessionStart(w http.ResponseWriter, r *http.Request) {
	if !s.guardAgentUplink(w, r) { // 会话开始同属"上报面"：soft 记账 / strict 401
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Agent     string `json:"agent"`
		SessionID string `json:"sessionId"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.Agent == "" {
		http.Error(w, "agent required", http.StatusBadRequest)
		return
	}

	injected, reason, content := false, "not-configured", ""
	if s.ctxPacks != nil {
		c, status, err := s.ctxPacks.Pack(body.Agent)
		reason = status
		if err != nil {
			reason = "error"
		} else if status == "ok" {
			injected, content = true, c
		}
	}

	// 会话留痕（合并注入结果，一个动作一条账）：agent.session.started
	if s.bus != nil {
		_ = s.bus.Publish(r.Context(), plugin.NewEventScoped(
			"platform.agent.session.started", "agent-interface",
			map[string]any{
				"agent": body.Agent, "sessionId": body.SessionID,
				"injected": injected, "reason": reason, "bytes": len(content),
			},
			plugin.ScopeAgent, plugin.ScopePlatform))
	}

	writeJSON(w, map[string]any{
		"agent": body.Agent, "injected": injected, "reason": reason,
		"bytes": len(content), "content": content,
	})
}

// 返回：{enabled, content, bytes}；enabled=false 表示开关关闭（调用方应不注入）
func (s *Server) handleContextPack(w http.ResponseWriter, r *http.Request) {
	agent := r.URL.Query().Get("agent")
	if agent == "" {
		http.Error(w, "agent required", http.StatusBadRequest)
		return
	}
	if s.ctxPacks == nil {
		writeJSON(w, map[string]any{"enabled": false, "content": "", "bytes": 0,
			"note": "context packs not enabled"})
		return
	}
	content, status, err := s.ctxPacks.Pack(agent)
	if err != nil {
		http.Error(w, fmt.Sprintf("read pack: %v", err), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]any{
		"agent": agent, "enabled": status == "ok", "status": status,
		"content": content, "bytes": len(content),
	})
}
