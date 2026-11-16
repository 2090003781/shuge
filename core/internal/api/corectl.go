// 本文件：内核（core）生命周期控制 —— 前端「启动/停止内核」按钮的后端。
//
// 设计边界（重要，务必知道）：
//
//	· 控制台页面是 core **自己**提供的 → core 不在时页面也打不开。
//	  所以页面上只能做「停止 / 重启」；「启动」必须由 core 之外的实体来做
//	  （看门狗 exe / 桌面快捷方式「▶ 启动枢阁」）。
//	· 「停止」会置暂停标志 `data/core.paused`：看门狗尊重它，不会 2 分钟后又把 core 拉起来。
//	  （core 每次启动会删掉这个标志——在跑的时候它没有意义。）
//	· 「重启」由 core 分离拉起 `watchdog.exe --restart`（它等 core 消失后拉起新实例），
//	  因此重启不需要用户碰终端。
package api

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"core/internal/plugin"
	"core/internal/pluginauth"
)

// CoreController core 生命周期控制口（由 main 注入实现）
type CoreController interface {
	// Stop 优雅停机；restart=true 表示已安排看门狗接管拉起新实例
	Stop(restart bool)
}

// SetCoreController 注入生命周期控制（main 装配时调用）
func (s *Server) SetCoreController(c CoreController) { s.coreCtl = c }

// SetAgentAuthMode 设置 agent 上报面的鉴权模式（soft/strict/off，空=soft）
func (s *Server) SetAgentAuthMode(mode string) {
	switch mode {
	case "strict", "off":
		s.agentAuthMode = mode
	default:
		s.agentAuthMode = "soft"
	}
}

// AgentAuthStats 上报面鉴权统计（/healthz 暴露，自检据此判断"谁没带 token"）
func (s *Server) AgentAuthStats() map[string]any {
	s.authMu.Lock()
	defer s.authMu.Unlock()
	byPath := map[string]uint64{}
	_ = byPath
	for k, v := range s.unauthByPath {
		byPath[k] = v
	}
	return map[string]any{
		"agent_uplink":   s.agentAuthMode,
		"authed_total":   s.authedTotal,
		"unauth_total":   s.unauthTotal,
		"unauth_by_path": byPath,
	}
}

// guardAgentUplink agent 上报面守卫（/api/bridge/send、/api/agent/session-start）
//
// 为什么默认 soft：调用方是**平台外**的接入方（reasonix hook、DSH 插件），它们拿不到内核
// 注入给子进程的环境变量，只能自己去读 data/plugins/token.json。迁移期先"记账不拦"，
// 等账本里确认没有不带 token 的调用方了，再切 strict —— 否则会当场切断正在跑的接入链路。
func (s *Server) guardAgentUplink(w http.ResponseWriter, r *http.Request) bool {
	mode := s.agentAuthMode
	if mode == "" {
		mode = "soft"
	}
	if mode == "off" || pluginauth.Valid(r, s.pluginToken) {
		s.authMu.Lock()
		s.authedTotal++
		s.authMu.Unlock()
		return true
	}
	// 没带/带错 token：计数 + 留痕（节流：同一路径每分钟最多一条账本事件）
	s.authMu.Lock()
	if s.unauthByPath == nil {
		s.unauthByPath = map[string]uint64{}
		s.unauthLast = map[string]time.Time{}
	}
	s.unauthTotal++
	s.unauthByPath[r.URL.Path]++
	last := s.unauthLast[r.URL.Path]
	now := time.Now()
	record := now.Sub(last) > time.Minute
	if record {
		s.unauthLast[r.URL.Path] = now
	}
	s.authMu.Unlock()
	if record && s.bus != nil {
		_ = s.bus.Publish(r.Context(), plugin.NewEvent("platform.auth.missing", "api",
			map[string]any{"path": r.URL.Path, "mode": mode, "client": r.RemoteAddr,
				"method": r.Method, "ua": r.UserAgent(), "origin": r.Header.Get("Origin"),
				"hint": "调用方（DSH 插件 / reasonix hook）应读 data/plugins/token.json 并带 X-Plugin-Token"}))
	}
	if mode == "strict" {
		w.Header().Set("WWW-Authenticate", pluginauth.Header)
		http.Error(w, "agent 上报面需要鉴权：请求头 "+pluginauth.Header+"（见 data/plugins/token.json）", http.StatusUnauthorized)
		return false
	}
	return true // soft：放行但已记账
}

// SetPluginToken 注入插件面鉴权 token（内核启动时读/建 data/plugins/token.json）
func (s *Server) SetPluginToken(t string) { s.pluginToken = t }

// requirePluginToken 插件面端点统一校验：不通过就回 401 并返回 false
//
// 只管插件面（register / poll / result）。/api/bridge/send（agent 上报面）暂不强制：
// 那一侧是平台外的接入方（reasonix hook、DSH 插件），它们拿不到内核注入的环境变量，
// 贸然强制会打断正在跑的接入链路（见 internal/pluginauth 包注释）。
func (s *Server) requirePluginToken(w http.ResponseWriter, r *http.Request) bool {
	if pluginauth.Valid(r, s.pluginToken) {
		return true
	}
	w.Header().Set("WWW-Authenticate", pluginauth.Header)
	http.Error(w, "插件面需要鉴权：请求头 "+pluginauth.Header+"（内核 spawn 插件时经环境变量 PLUGIN_TOKEN 注入）", http.StatusUnauthorized)
	return false
}

// PausedFlagPath core 暂停标志（看门狗据此不自动拉起）
func PausedFlagPath() string { return filepath.Join("data", "core.paused") }

// handleCoreStatus GET /api/core/status —— 顶部按钮的状态源
func (s *Server) handleCoreStatus(w http.ResponseWriter, r *http.Request) {
	_, statErr := os.Stat(PausedFlagPath())
	body := map[string]any{
		"running":  true,
		"pid":      os.Getpid(),
		"uptime_s": int64(time.Since(s.startedAt).Seconds()),
		"paused":   statErr == nil,
		"probe":    s.ProbeSummary(),
	}
	// 主进程（枢阁本体，常驻）的控制台地址：端口写在 data/host.port（旧名 serve.port 兼容）
	// 内核是它的附属进程 —— 前端拿这个地址做启停与左侧面板
	for _, f := range []string{"host.port", "serve.port"} {
		b, err := os.ReadFile(filepath.Join("data", f))
		if err != nil {
			continue
		}
		var p int
		if _, err := fmt.Sscanf(strings.TrimSpace(string(b)), "%d", &p); err == nil && p > 0 {
			body["control_url"] = fmt.Sprintf("http://127.0.0.1:%d/", p)
			break
		}
	}
	// owner 登记：这次 core 是谁拉起来的（看门狗/桌面图标/控制台/手动）
	if b, err := os.ReadFile(filepath.Join("data", "owner.json")); err == nil {
		var o map[string]any
		if json.Unmarshal(b, &o) == nil {
			body["owner"] = o
			body["launched_by"] = o["launched_by"]
		}
	}
	// 熔断隔离标记（存在 = 下次不会自动拉起，需要人工用桌面图标恢复）
	if _, err := os.Stat(filepath.Join("data", "core.quarantine")); err == nil {
		body["quarantine"] = true
	}
	if s.led != nil {
		st := s.led.Stats()
		body["ledger"] = map[string]any{"file": st.File, "last_seq": st.LastSeq, "degraded": st.Degraded}
	}
	if s.sup != nil {
		body["processes"] = len(s.sup.Status())
	}
	writeJSON(w, body)
}

// handleCoreControl POST /api/core/control {action:"stop"|"restart"}
//
// 先回响应再停机（异步），这样浏览器能收到"已受理"而不是连接中断。
func (s *Server) handleCoreControl(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Action string `json:"action"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.Action != "stop" && body.Action != "restart" {
		http.Error(w, "action must be stop|restart", http.StatusBadRequest)
		return
	}
	if s.coreCtl == nil {
		http.Error(w, "core controller not wired", http.StatusServiceUnavailable)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "action": body.Action,
		"note": "已受理，正在停机；重启由看门狗接管，约 5 秒后回来"})
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	restart := body.Action == "restart"
	go func() {
		time.Sleep(300 * time.Millisecond) // 让响应先回到浏览器
		s.coreCtl.Stop(restart)
	}()
}
