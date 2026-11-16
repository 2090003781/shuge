// Package api 控制台 HTTP API：插件/状态/事件 + 静态前端 + Vault 资源。
// 纯 API 服务器：前端只调这里，不碰本地文件/进程（Web→桌面只换壳原则）。
package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"core/internal/bridge"
	"core/internal/config"
	"core/internal/eventbus"
	"core/internal/ledger"
	"core/internal/plugin"
	"core/internal/registry"
	"core/internal/supervisor"
	"core/internal/vault"
)

// EventLogSearcher 账本查询能力（由 internal/ledger 实现，main 注入）
type EventLogSearcher interface {
	Search(p ledger.SearchParams) ([]map[string]any, error)
}

// Server 控制台 API 服务器
type Server struct {
	bus           *eventbus.Bus
	reg           *registry.Registry
	sup           *supervisor.Supervisor
	vault         *vault.Vault
	bridge        *bridge.Bridge
	mgr           *PluginManager      // 插件运行时管理（会话级启停/删除）
	searcher      EventLogSearcher    // 分层账本查询（日志模块）
	pluginCalls   *pluginCallBroker   // external 插件调用通道（平台→插件→回结果）
	ctxPacks      *ContextPackManager // 上下文包管理（注入开关 + 直读包）
	led           *ledger.Ledger      // 平台账本（状态/校验/位点读取）
	logDir        string              // 统一日志目录（logsink 落盘处；/api/logs 读它）
	coreCtl       CoreController      // core 生命周期控制（控制台顶部启停按钮）
	probe         *ProbeScheduler     // 心跳点名调度（活性台账）
	pluginToken   string              // 插件面鉴权 token（main 注入；空 = 不校验，便于单机调试）
	pluginCfgPath string              // 插件清单路径（/api/plugins/reload 重读它；main 注入）

	authMu        sync.Mutex // 上报面鉴权统计
	agentAuthMode string     // soft（默认）/ strict / off
	authedTotal   uint64
	unauthTotal   uint64
	unauthByPath  map[string]uint64
	unauthLast    map[string]time.Time
	startedAt     time.Time // 本实例启动时刻（/healthz 报存活时长）

	recvMu        sync.Mutex
	recvBySource  map[string]uint64 // 入站事件计数（按来源）
	recvTotal     uint64
	lastRecvFlush time.Time
	log           *slog.Logger
	events        []eventItem // 环形缓冲：最近事件
	mu            sync.RWMutex
	webDir        string
}

type eventItem struct {
	Type   string `json:"type"`
	Source string `json:"source"`
	Time   string `json:"time"`
}

// New 创建 API 服务器并订阅事件流
func New(bus *eventbus.Bus, reg *registry.Registry, sup *supervisor.Supervisor,
	v *vault.Vault, webDir string, log *slog.Logger) *Server {
	if log == nil {
		log = slog.Default()
	}
	s := &Server{bus: bus, reg: reg, sup: sup, vault: v, log: log, webDir: webDir,
		pluginCalls: newPluginCallBroker(), startedAt: time.Now()}
	// 订阅事件 → 环形缓冲（最多 100 条）；展示层只收"涉及平台"的事件（scope 含 platform）
	ctx := context.Background()
	_, _ = bus.SubscribeNamed(ctx, "console.events", nil, func(ctx context.Context, ev plugin.Event) error {
		if !ev.TouchesPlatform() {
			return nil // agent 内部活动不上展示（进账本但不展示）
		}
		s.mu.Lock()
		s.events = append(s.events, eventItem{Type: ev.Type, Source: ev.Source,
			Time: ev.Time.Format("15:04:05")})
		if len(s.events) > 100 {
			s.events = s.events[len(s.events)-100:]
		}
		s.mu.Unlock()
		return nil
	})
	return s
}

// Handler 返回 http.Handler（路由 + 静态文件）
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/plugins", s.handlePlugins)
	mux.HandleFunc("/api/plugins/detail", s.handlePluginDetail)
	mux.HandleFunc("/api/components", s.handleComponents)
	mux.HandleFunc("/api/component-detail", s.handleComponentDetail)
	if s.mgr != nil {
		mux.HandleFunc("/api/plugins/stop", s.handlePluginStop)
		mux.HandleFunc("/api/plugins/start", s.handlePluginStart)
		mux.HandleFunc("/api/plugins/reload", s.handlePluginReload) // 热加：重读清单，只起新条目
		mux.HandleFunc("/api/plugins/delete", s.handlePluginDelete)
		mux.HandleFunc("/api/plugins/trash", s.handlePluginTrashList)
		mux.HandleFunc("/api/plugins/trash/restore", s.handlePluginTrashRestore)
		mux.HandleFunc("/api/plugins/trash/purge", s.handlePluginTrashPurge)
	}
	mux.HandleFunc("/api/status", s.handleStatus)
	mux.HandleFunc("/healthz", s.handleHealthz)
	mux.HandleFunc("/api/core/status", s.handleCoreStatus)
	mux.HandleFunc("/api/core/control", s.handleCoreControl)
	mux.HandleFunc("/api/logs/sources", s.handleLogSources)
	mux.HandleFunc("/api/logs", s.handleLogs)
	mux.HandleFunc("/api/events", s.handleEvents)
	mux.HandleFunc("/api/events/stream", s.handleEventStream)
	mux.HandleFunc("/api/events/topology", s.handleTopology)
	mux.HandleFunc("/api/ledger/stats", s.handleLedgerStats)
	mux.HandleFunc("/api/ledger/verify", s.handleLedgerVerify)
	mux.HandleFunc("/api/ledger/read", s.handleLedgerRead)
	mux.HandleFunc("/api/ledger/detail", s.handleLedgerDetail)
	mux.HandleFunc("/api/plugins/probe", s.handleProbeStates)
	// 软件操作权限（三档：只读/可写/相信）——所有操作封装在平台侧
	mux.HandleFunc("/api/permissions", s.handlePermissions)
	mux.HandleFunc("/api/permissions/set", s.handlePermissionSet)
	// 软件操作的执行侧：平台按档校验后代为执行（agent 侧不需要自带工具集）
	mux.HandleFunc("/api/ops", s.handleOpsList)
	mux.HandleFunc("/api/ops/call", s.handleOpsCall)
	mux.HandleFunc("/api/context/routes", s.handleContextRoutes)
	mux.HandleFunc("/api/context/route-set", s.handleContextRouteSet)
	mux.HandleFunc("/api/context/pack", s.handleContextPack)
	mux.HandleFunc("/api/agent/session-start", s.handleAgentSessionStart)
	if s.searcher != nil {
		mux.HandleFunc("/api/events/search", s.handleEventSearch)
	}
	mux.HandleFunc("/api/publish", s.handlePublish)
	if s.bridge != nil {
		mux.HandleFunc("/api/bridge/agents", s.handleBridgeAgents)
		mux.HandleFunc("/api/bridge/modules", s.handleBridgeModules)
		mux.HandleFunc("/api/bridge/send", s.handleBridgeSend)
		mux.HandleFunc("/api/bridge/status", s.handleBridgeStatus)
		mux.HandleFunc("/api/bridge/plugins/register", s.handlePluginRegister)
		mux.HandleFunc("/api/bridge/plugins/call", s.handlePluginCall)
		mux.HandleFunc("/api/bridge/plugins/poll", s.handlePluginPoll)
		mux.HandleFunc("/api/bridge/plugins/result", s.handlePluginResult)
	}
	// 库页面（ai-notes）：与 Vault 无关，Vault 没初始化也要能用
	// 见 internal/api/notesproxy.go —— 目标默认 http://127.0.0.1:8848，可用 AINOTES_URL 覆盖
	mux.HandleFunc("/notes", s.handleNotesMount)
	mux.HandleFunc("/notes/", s.handleNotesReverse)
	if s.vault != nil {
		// 本机文件对话框（控制台选外部文件用；Windows 原生对话框）
		mux.HandleFunc("/api/fs/pick", s.handleFsPick)
		// 文件上传落 Vault（控制台"添加文件"的网页做法；参考网页版 AI 的交互）
		mux.HandleFunc("/api/vault/upload", s.handleVaultUpload)
		// 把机器上的绝对路径纳入库（复制进「附件/导入/」→ 包清单改存 Vault 相对路径）
		mux.HandleFunc("/api/vault/adopt", s.handleVaultAdopt)
		mux.HandleFunc("/api/vault/list", s.handleVaultList)
		mux.HandleFunc("/api/vault/read", s.handleVaultRead)
		mux.HandleFunc("/api/vault/download", s.handleVaultDownload)
		mux.HandleFunc("/api/vault/move", s.handleVaultMove)
		mux.HandleFunc("/api/vault/merge", s.handleVaultMerge)
		mux.HandleFunc("/api/vault/rename", s.handleVaultRename)
		mux.HandleFunc("/api/vault/new-dir", s.handleVaultNewDir)
		mux.HandleFunc("/api/vault/new-file", s.handleVaultNewFile)
		mux.HandleFunc("/api/vault/delete", s.handleVaultDelete)
		mux.HandleFunc("/api/vault/unfolder", s.handleVaultUnfolder)
		mux.HandleFunc("/api/vault/trash", s.handleTrashList)
		mux.HandleFunc("/api/vault/trash/restore", s.handleTrashRestore)
		mux.HandleFunc("/api/vault/trash/delete", s.handleTrashDelete)
		mux.HandleFunc("/api/vault/trash/empty", s.handleTrashEmpty)
	}
	mux.Handle("/", noCache(http.FileServer(http.Dir(s.webDir))))
	return mux
}

// noCache 开发期禁用静态缓存，保证前端改动刷新即生效
func noCache(h http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store, no-cache, must-revalidate")
		w.Header().Set("Pragma", "no-cache")
		h.ServeHTTP(w, r)
	})
}

// ---- vault handlers ----

// publishNote 边界钩子：Vault 写操作成功后发事件（捕获层——不靠调用方自觉发布）。
func (s *Server) publishNote(typ string, payload map[string]any) {
	if s.bus == nil {
		return
	}
	_ = s.bus.Publish(context.Background(), plugin.NewEvent(typ, "vault", payload))
}

func (s *Server) handleVaultList(w http.ResponseWriter, r *http.Request) {
	tree, err := s.vault.Tree()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, tree)
}

func (s *Server) handleVaultRead(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Query().Get("path")
	if path == "" {
		http.Error(w, "missing path", http.StatusBadRequest)
		return
	}
	res, err := s.vault.Read(path)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	writeJSON(w, res)
}

func (s *Server) handleVaultDownload(w http.ResponseWriter, r *http.Request) {
	path := r.URL.Query().Get("path")
	if path == "" {
		http.Error(w, "missing path", http.StatusBadRequest)
		return
	}
	abs, err := s.vault.AbsPath(path)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	name := filepath.Base(abs)
	w.Header().Set("Content-Disposition", "attachment; filename=\""+name+"\"")
	http.ServeFile(w, r, abs)
}

func (s *Server) handleVaultMove(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		From string `json:"from"`
		To   string `json:"to"` // 目标目录
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.From == "" || body.To == "" {
		http.Error(w, "from/to required", http.StatusBadRequest)
		return
	}
	newPath, err := s.vault.Move(body.From, body.To)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.moved", map[string]any{"from": body.From, "to": body.To, "path": newPath})
	writeJSON(w, map[string]string{"ok": "true", "newPath": newPath})
}

func (s *Server) handleVaultMerge(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		A string `json:"a"`
		B string `json:"b"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	newDir, err := s.vault.MergeFiles(body.A, body.B)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.merged", map[string]any{"a": body.A, "b": body.B, "path": newDir})
	writeJSON(w, map[string]string{"ok": "true", "newDir": newDir})
}

func (s *Server) handleVaultRename(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Path    string `json:"path"`
		NewName string `json:"newName"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	newPath, err := s.vault.Rename(body.Path, body.NewName)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.renamed", map[string]any{"path": body.Path, "newName": body.NewName, "newPath": newPath})
	writeJSON(w, map[string]string{"ok": "true", "newPath": newPath})
}

func (s *Server) handleVaultNewDir(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Parent string `json:"parent"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	newDir, err := s.vault.CreateDir(body.Parent, "新建文件夹")
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.dir-created", map[string]any{"parent": body.Parent, "path": newDir})
	writeJSON(w, map[string]string{"ok": "true", "newDir": newDir})
}

func (s *Server) handleVaultNewFile(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Dir  string `json:"dir"`
		Name string `json:"name"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	newFile, err := s.vault.NewFile(body.Dir, body.Name)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.created", map[string]any{"dir": body.Dir, "name": body.Name, "path": newFile})
	writeJSON(w, map[string]string{"ok": "true", "newFile": newFile})
}

func (s *Server) handleVaultDelete(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Path string `json:"path"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	trashed, err := s.vault.Trash(body.Path)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.trashed", map[string]any{"path": body.Path, "trashedTo": trashed})
	writeJSON(w, map[string]string{"ok": "true", "trashedTo": trashed})
}

func (s *Server) handleVaultUnfolder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Path string `json:"path"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	moved, err := s.vault.Unfolder(body.Path)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.unfolded", map[string]any{"path": body.Path, "moved": moved})
	writeJSON(w, map[string]any{"ok": "true", "moved": moved})
}

// ---- handlers ----

func (s *Server) handlePlugins(w http.ResponseWriter, r *http.Request) {
	type pluginView struct {
		ID           string   `json:"id"`
		Name         string   `json:"name"`
		Type         string   `json:"type"`
		Capabilities []string `json:"capabilities"`
		Status       string   `json:"status"`
	}
	views := []pluginView{}
	// 有管理器：列出全部已知插件（含 stopped，可重启）；无管理器：仅 registry
	if s.mgr != nil {
		for _, p := range s.mgr.All() {
			views = append(views, pluginView{
				ID: p["id"].(string), Name: p["name"].(string), Type: p["type"].(string),
				Capabilities: toStringSlice(p["capabilities"]), Status: p["status"].(string),
			})
		}
		writeJSON(w, views)
		return
	}
	for _, p := range s.reg.List() {
		views = append(views, pluginView{
			ID: p.ID(), Name: p.Name(), Type: string(p.Type()),
			Capabilities: p.Capabilities(), Status: "running",
		})
	}
	writeJSON(w, views)
}

// toStringSlice 安全转换 any → []string（map[string]any 里存的是 []string）
func toStringSlice(v any) []string {
	if s, ok := v.([]string); ok {
		return s
	}
	return []string{}
}

// handleComponents 统一组件列表：registry 插件 + bridge 模块 + bridge agent，
// 统一格式 {kind, id, name, type, capabilities, status} —— 控制台据此"插上即自动出现"。
func (s *Server) handleComponents(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.buildComponents())
}

// buildComponents 构造统一组件列表（HTTP 与拓扑快照共用）
func (s *Server) buildComponents() []map[string]any {
	out := []map[string]any{}
	// 监督器实况（2026-09-12 修）：external 插件的状态不能只看"注册表里在不在"——
	// 插件进程崩溃且重启预算耗尽后，bridge/registry 里仍有它的身份，界面却报"运行中"（假绿灯，
	// 与错误库 #48「进程停摆却报健康」同一类）。这里用监督器的进程实况纠偏。
	supLive := map[string]bool{}
	if s.sup != nil {
		for _, st := range s.sup.Status() {
			id, _ := st["id"].(string)
			run, _ := st["running"].(bool)
			if id != "" {
				supLive[id] = run
			}
		}
	}
	// liveStatus 按监督器实况纠偏 external 插件状态。
	// 注意两种"假绿灯"（2026-09-12 检修实测）：
	//   ① 监督器还在管但进程已退出（等重启）——known && !running；
	//   ② 崩溃预算耗尽后监督器**已把该进程从表里删掉**（platform.process.gave-up）——
	//      此时 supLive 里根本没有它，而插件管理器的状态表还停在 running。
	//      这正是"进程早没了、界面还报运行中"的成因（与错误库 #48 同一类）。
	liveStatus := func(id, status string) string {
		if status != "running" {
			return status
		}
		if run, known := supLive[id]; known {
			if !run {
				return "absent（进程已退出，监督器仍在管）"
			}
			return status
		}
		return "absent（不在监督中：崩溃预算耗尽或未成功拉起）"
	}
	// 外部插件自报信息（含中文说明与"我代表哪个身份"）：id → PluginInfo
	bridgePlugins := map[string]bridge.PluginInfo{}
	if s.bridge != nil {
		for _, p := range s.bridge.Plugins() {
			bridgePlugins[p.ID] = p
		}
	}
	// 插件：有管理器用 mgr.All()（含 stopped），否则 registry
	if s.mgr != nil {
		for _, p := range s.mgr.All() {
			id, _ := p["id"].(string)
			name, _ := p["name"].(string)
			caps := toStringSlice(p["capabilities"])
			row := map[string]any{}
			// 若插件已在 bridge 注册（自报家门），合并其 name/capabilities/说明/身份声明
			if reg, ok := bridgePlugins[id]; ok {
				if reg.Name != "" && reg.Name != id {
					name = reg.Name
				}
				if len(reg.Capabilities) > 0 {
					caps = reg.Capabilities
				}
				row["desc"] = reg.Description
				if reg.AgentID != "" { // 身份声明就是插件的一个字段（不再单独一行）
					row["agent_id"] = reg.AgentID
					row["agent_name"] = reg.AgentName
					row["agent_description"] = reg.AgentDescription
				}
				delete(bridgePlugins, id)
			}
			st, _ := p["status"].(string)
			row["kind"], row["id"], row["name"] = "plugin", id, name
			row["type"], row["capabilities"], row["status"] = p["type"], caps, liveStatus(id, st)
			// agent 接口插件：列表里也带上当前权限档（控制台一眼看到）
			if reg := s.bridge.PluginByID(id); reg != nil && reg.Info().AgentID != "" {
				row["perm_level"] = levelOfPlugin(id)
			}
			out = append(out, row)
		}
	} else {
		for _, p := range s.reg.List() {
			out = append(out, map[string]any{
				"kind": "plugin", "id": p.ID(), "name": p.Name(),
				"type": string(p.Type()), "capabilities": p.Capabilities(), "status": "running",
			})
		}
	}
	// 已注册但不在管理器内的外部插件（补录）
	for id, p := range bridgePlugins {
		row := map[string]any{
			"kind": "plugin", "id": id, "name": p.Name,
			"type": "external", "capabilities": p.Capabilities, "status": liveStatus(id, "running"),
			"desc": p.Description,
		}
		if p.AgentID != "" {
			row["agent_id"], row["agent_name"], row["agent_description"] = p.AgentID, p.AgentName, p.AgentDescription
		}
		out = append(out, row)
	}
	if s.bridge != nil {
		for _, m := range s.bridge.Modules() {
			out = append(out, map[string]any{
				"kind": "module", "id": m.ID, "name": m.Name,
				"type": "module", "capabilities": m.Capabilities, "status": "running",
			})
		}
		for _, a := range s.bridge.Agents() {
			if bridge.IsExternalPluginID(a.ID) {
				continue // 外部插件已按 plugin 归类
			}
			row := map[string]any{
				"kind": "agent", "id": a.ID, "name": a.Name,
				"type": "agent", "capabilities": a.Capabilities, "status": "connected",
			}
			if a.BackedBy != "" {
				row["backed_by"] = a.BackedBy
			}
			if a.Description != "" {
				row["desc"] = a.Description
			}
			out = append(out, row)
		}
	}
	// 事件总线（系统设施，作为可查看组件出现在列表）
	out = append(out, map[string]any{
		"kind": "system", "id": "event-bus", "name": "事件总线",
		"type": "bus", "capabilities": []string{"publish", "subscribe"}, "status": "running",
	})
	return out
}

// handlePluginDetail 单插件详情：基本 + 进程监督状态（external 才有 process 段）
//
// 名称/能力/中文说明以**插件自报**为准（注册时报上来），平台兜底 —— 与 /api/component-detail 同一口径，
// 免得两个入口显示的插件身份不一致。
func (s *Server) handlePluginDetail(w http.ResponseWriter, r *http.Request) {
	id := r.URL.Query().Get("id")
	if id == "" {
		http.Error(w, "missing id", http.StatusBadRequest)
		return
	}
	// 优先管理器（含 stopped 的已知插件）；否则 registry
	if s.mgr != nil {
		for _, p := range s.mgr.All() {
			if p["id"] == id {
				nm, _ := p["name"].(string)
				caps := p["capabilities"]
				detail := map[string]any{
					"id": id, "name": nm, "type": p["type"],
					"capabilities": caps, "status": p["status"], "process": nil,
				}
				// 以插件自报为准（含中文说明与 agent 身份声明）
				if reg := s.bridge.PluginByID(id); reg != nil {
					info := reg.Info()
					if info.Name != "" && info.Name != id {
						nm = info.Name
					}
					if len(info.Capabilities) > 0 {
						caps = info.Capabilities
					}
					detail["name"], detail["capabilities"] = nm, caps
					detail["desc"] = info.Description
					if info.AgentID != "" {
						detail["agent_id"] = info.AgentID
						detail["agent_name"] = info.AgentName
						detail["agent_description"] = info.AgentDescription
						// agent 接口插件才有这道闸：把软件操作权限块一并给控制台详情页
						detail["permissions"] = s.permissionBlock(id)
					}
				}
				if s.sup != nil {
					for _, st := range s.sup.Status() {
						if st["id"] == id {
							proc := map[string]any{}
							for k, v := range st {
								if k != "id" {
									proc[k] = v
								}
							}
							detail["process"] = proc
							break
						}
					}
				}
				writeJSON(w, detail)
				return
			}
		}
		http.Error(w, "plugin not found: "+id, http.StatusNotFound)
		return
	}
	p := s.reg.Get(id)
	if p == nil {
		http.Error(w, "plugin not found: "+id, http.StatusNotFound)
		return
	}
	detail := map[string]any{
		"id":           p.ID(),
		"name":         p.Name(),
		"type":         string(p.Type()),
		"capabilities": p.Capabilities(),
		"status":       "running",
		"process":      nil,
	}
	if s.sup != nil {
		for _, st := range s.sup.Status() {
			if st["id"] == id {
				proc := map[string]any{}
				for k, v := range st {
					if k != "id" {
						proc[k] = v
					}
				}
				detail["process"] = proc
				if running, ok := st["running"].(bool); ok && !running {
					detail["status"] = "restarting"
				}
				break
			}
		}
	}
	writeJSON(w, detail)
}

func (s *Server) handleStatus(w http.ResponseWriter, r *http.Request) {
	procs := 0
	if s.sup != nil {
		procs = len(s.sup.Status())
	}
	out := map[string]any{
		"plugins":   len(s.reg.List()),
		"subs":      s.bus.SubscriberCount(),
		"processes": procs,
		"uptime":    time.Now().Format(time.RFC3339),
	}
	if s.led != nil {
		out["ledger"] = s.led.Stats()
	}
	out["probe"] = s.ProbeSummary()
	writeJSON(w, out)
}

func (s *Server) handleEvents(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	writeJSON(w, s.events)
}

// RegisterEventSearcher 注入分层账本查询器（main 装配日志模块时调用）
func (s *Server) RegisterEventSearcher(se EventLogSearcher) { s.searcher = se }

// componentMeta 各组件详情元数据（描述 + 路径/工具等专属信息）
func (s *Server) componentMeta(kind, id string) (string, map[string]any) {
	switch kind + "/" + id {
	case "module/module.event-log":
		dir := filepath.Join("data", "logs")
		if abs, err := filepath.Abs(dir); err == nil {
			dir = abs
		}
		return "订阅平台全部事件，写入分层账本（每条先记总账，再按归属分账）", map[string]any{
			"账本目录": dir,
			"账本结构": []string{
				"all.jsonl —— 总账（全部事件）",
				"system.jsonl —— 系统事件（note.*/plugin.* 等）",
				"agent/<agent>.jsonl —— 各 agent 活动",
			},
			"工具":   []string{"log.record（agent 主动记日志）", "log.query（查总账最近 N 条）"},
			"查询接口": "/api/events/search?level=all|system|agent&keyword=&from=&to=",
		}
	case "module/module.note-search":
		root := ""
		if s.vault != nil {
			root = s.vault.Root()
		}
		return "在 Vault 笔记库中按文件名/内容关键词搜索，返回命中路径与片段", map[string]any{
			"检索范围": root,
			"工具":   "note.search（参数 query/limit/scope）",
			"说明":   "跳过 .trash/.git/.obsidian 等系统目录",
		}
	case "agent/agent.reasonix":
		dir := filepath.Join("data", "inbox")
		if abs, err := filepath.Abs(dir); err == nil {
			dir = abs
		}
		return "Reasonix AI 接入适配器：平台事件经 Send 落 inbox，reasonix 侧可读", map[string]any{
			"能力":  []string{"chat", "reasoning", "tool-use"},
			"收件箱": dir,
			"说明":  "本地闭环验证通道（推送 → data/inbox/*.json）",
		}
	case "system/event-bus":
		return "平台事件总线：发布/订阅/类型过滤，每订阅者独立队列（慢者隔离、满则丢弃不阻塞）", map[string]any{
			"当前订阅者": s.bus.SubscriberCount(),
			"事件来源": []string{
				"agent 上报 / 工具调用（bridge 发布）",
				"Vault 写操作（边界钩子）",
				"插件启停（pluginmgr）",
				"控制台手动发布",
			},
			"查询": "/api/events/search（按层级/时间/关键词查账本）",
			"实时": "/api/events/stream（SSE）",
		}
	default:
		// 通用兜底
		switch kind {
		case "plugin":
			return "平台插件：由插件管理器统一启停/删除（会话级）", map[string]any{
				"管理": []string{"POST /api/plugins/stop", "POST /api/plugins/start", "POST /api/plugins/delete"},
			}
		case "module":
			return "平台功能模块：经 bridge 挂载，能力以工具形式供 agent 调用", map[string]any{}
		case "agent":
			return "接入平台的智能体：可发请求/工具调用，平台可定向注入事件", map[string]any{}
		}
		return "", map[string]any{}
	}
}

// handleComponentDetail 单组件详情元数据（列表行"详情→"页数据源）
func (s *Server) handleComponentDetail(w http.ResponseWriter, r *http.Request) {
	kind := r.URL.Query().Get("kind")
	id := r.URL.Query().Get("id")
	if kind == "" || id == "" {
		http.Error(w, "kind/id required", http.StatusBadRequest)
		return
	}
	// 基础信息
	var base map[string]any
	if s.mgr != nil && kind == "plugin" {
		for _, p := range s.mgr.All() {
			if p["id"] == id {
				base = p
				break
			}
		}
	} else if s.bridge != nil {
		if kind == "module" {
			for _, m := range s.bridge.Modules() {
				if m.ID == id {
					base = map[string]any{"id": m.ID, "name": m.Name, "type": "module", "capabilities": m.Capabilities, "status": "running"}
				}
			}
		} else if kind == "agent" {
			for _, a := range s.bridge.Agents() {
				if a.ID == id {
					base = map[string]any{"id": a.ID, "name": a.Name, "type": "agent", "capabilities": a.Capabilities, "status": "connected"}
					// 身份 ↔ 实现：告诉前端"这个身份由哪个插件提供"（互相可点）
					if a.BackedBy != "" {
						base["backed_by"] = a.BackedBy
						base["meta"] = map[string]any{"实现插件": a.BackedBy}
					}
					if a.Description != "" {
						base["desc"] = a.Description
					}
				}
			}
		}
	}
	if base == nil && kind == "system" && id == "event-bus" {
		base = map[string]any{"id": "event-bus", "name": "事件总线", "type": "bus", "status": "running"}
	}
	if base == nil {
		http.Error(w, "component not found: "+kind+"/"+id, http.StatusNotFound)
		return
	}
	base["kind"] = kind // 前端详情页需按 kind 定制/判断
	// 外部插件"自报家门"的信息优先（插件最清楚自己叫什么、干什么）：
	// 名称/能力/中文说明都在注册时报上来，平台只做兜底，不硬编码
	if kind == "plugin" && s.bridge != nil {
		if reg := s.bridge.PluginByID(id); reg != nil {
			info := reg.Info()
			if info.Name != "" && info.Name != id {
				base["name"] = info.Name
			}
			if len(info.Capabilities) > 0 {
				base["capabilities"] = info.Capabilities
			}
			if info.Description != "" {
				base["desc"] = info.Description
			}
			// 身份声明：同一个插件实体上的一个字段（2026-09-12 辞玖定"身份申明加通道放一起"）
			if info.AgentID != "" {
				base["agent_id"] = info.AgentID
				base["agent_name"] = info.AgentName
				base["agent_description"] = info.AgentDescription
			}
		}
	}
	desc, meta := s.componentMeta(kind, id)
	if _, has := base["desc"]; !has {
		base["desc"] = desc // 插件没自报说明时才用兜底文案
	}
	base["meta"] = meta
	// 插件附加进程信息
	if kind == "plugin" && s.sup != nil {
		for _, st := range s.sup.Status() {
			if st["id"] == id {
				proc := map[string]any{}
				for k, v := range st {
					if k != "id" {
						proc[k] = v
					}
				}
				base["process"] = proc
				break
			}
		}
	}
	writeJSON(w, base)
}

// handleEventSearch GET /api/events/search —— 按层级查账本
// 参数：level=all|system|agent, agent=<id>, type=前缀, source=, from=, to= (RFC3339), limit=
func (s *Server) handleEventSearch(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit := 50
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	p := ledger.SearchParams{
		Level:   q.Get("level"),
		Agent:   q.Get("agent"),
		Type:    q.Get("type"),
		Source:  q.Get("source"),
		Keyword: q.Get("keyword"),
		From:    q.Get("from"),
		To:      q.Get("to"),
		Limit:   limit,
	}
	rows, err := s.searcher.Search(p)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	writeJSON(w, rows)
}

// handleTopology GET /api/events/topology —— 事件拓扑快照（实时组装）
// 节点：组件（插件/模块/agent/系统）+ 订阅者
// 边：produces_declared / produces_observed / consumes_declared / subscribes / pushes
func (s *Server) handleTopology(w http.ResponseWriter, r *http.Request) {
	type node struct {
		ID         string         `json:"id"`
		Kind       string         `json:"kind"`
		Name       string         `json:"name,omitempty"`
		Status     string         `json:"status,omitempty"`
		Declared   map[string]any `json:"declared,omitempty"`   // produces/consumes 声明
		Observed   map[string]any `json:"observed,omitempty"`   // 从账本反推的产生关系
		Subscribes []string       `json:"subscribes,omitempty"` // 订阅的事件类型（空=全部）
	}
	type edge struct {
		From string `json:"from"`
		To   string `json:"to"`
		Rel  string `json:"rel"`
	}
	nodes := []node{}
	edges := []edge{}

	// 1) 组件节点（插件/模块/agent/系统）
	for _, c := range s.buildComponents() {
		id, _ := c["id"].(string)
		kind, _ := c["kind"].(string)
		if kind == "system" {
			continue // 总线自身不作为节点
		}
		name, _ := c["name"].(string)
		status, _ := c["status"].(string)
		nodes = append(nodes, node{ID: id, Kind: kind, Name: name, Status: status})
	}

	// 2) 声明关系（外部插件注册时声明，覆盖观测值）
	if s.bridge != nil {
		for id, d := range s.bridge.PluginDeclarations() {
			for i := range nodes {
				if nodes[i].ID == id {
					nodes[i].Declared = map[string]any{}
					if len(d["produces"]) > 0 {
						nodes[i].Declared["produces"] = d["produces"]
						for _, t := range d["produces"] {
							edges = append(edges, edge{From: id, To: "event:" + t, Rel: "produces_declared"})
						}
					}
					if len(d["consumes"]) > 0 {
						nodes[i].Declared["consumes"] = d["consumes"]
						for _, t := range d["consumes"] {
							edges = append(edges, edge{From: id, To: "event:" + t, Rel: "consumes_declared"})
						}
					}
				}
			}
		}
	}

	// 3) 订阅关系（eventbus 命名订阅者）
	subs := s.bus.Subscribers()
	for _, sub := range subs {
		name := sub.Name
		if name == "" {
			name = "(匿名订阅者)"
		}
		nodes = append(nodes, node{ID: name, Kind: "subscriber", Subscribes: sub.Types})
		target := "event:*"
		if len(sub.Types) > 0 {
			for _, t := range sub.Types {
				edges = append(edges, edge{From: name, To: "event:" + t, Rel: "subscribes"})
			}
			continue
		}
		edges = append(edges, edge{From: name, To: target, Rel: "subscribes"})
	}

	// 4) 观测关系（从账本反推：谁产生过什么事件）
	if s.searcher != nil {
		if rows, err := s.searcher.Search(ledger.SearchParams{Level: "all", Limit: 500}); err == nil {
			seen := map[string]map[string]bool{}
			for _, e := range rows {
				src, _ := e["source"].(string)
				typ, _ := e["type"].(string)
				if src == "" || typ == "" {
					continue
				}
				if seen[src] == nil {
					seen[src] = map[string]bool{}
				}
				seen[src][typ] = true
			}
			for src, types := range seen {
				list := []string{}
				for t := range types {
					list = append(list, t)
				}
				for i := range nodes {
					if nodes[i].ID == src {
						if nodes[i].Observed == nil {
							nodes[i].Observed = map[string]any{}
						}
						nodes[i].Observed["produces"] = list
					}
				}
				for _, t := range list {
					edges = append(edges, edge{From: src, To: "event:" + t, Rel: "produces_observed"})
				}
			}
		}
	}

	writeJSON(w, map[string]any{
		"generatedAt": time.Now().Format(time.RFC3339),
		"nodes":       nodes,
		"edges":       edges,
	})
}

// handleEventStream SSE：总线事件实时推送到浏览器（事件驱动刷新，替代轮询）。
// 前端据此在插件/模块/agent 增删时自动刷新列表。
func (s *Server) handleEventStream(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming unsupported", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("Connection", "keep-alive")

	ctx := r.Context()
	unsub, err := s.bus.SubscribeNamed(ctx, "console.sse", nil, func(ctx context.Context, ev plugin.Event) error {
		if !ev.TouchesPlatform() {
			return nil // 展示层只推"涉及平台"的事件（agent 内部活动不上展示）
		}
		msg, merr := json.Marshal(map[string]any{
			"type": ev.Type, "source": ev.Source,
			"time": ev.Time.Format("15:04:05"), "payload": ev.Payload,
		})
		if merr != nil {
			return merr
		}
		if _, werr := fmt.Fprintf(w, "data: %s\n\n", msg); werr != nil {
			return werr
		}
		fl.Flush()
		return nil
	})
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer unsub()

	// 心跳注释帧：防代理/空闲断连
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			if _, err := fmt.Fprint(w, ": ping\n\n"); err != nil {
				return
			}
			fl.Flush()
		}
	}
}

// handlePublish 从前端发布事件（控制台测试/手动触发用）
func (s *Server) handlePublish(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Type    string         `json:"type"`
		Source  string         `json:"source"`
		Payload map[string]any `json:"payload"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.Type == "" {
		body.Type = "console.manual"
	}
	if body.Source == "" {
		body.Source = "console"
	}
	_ = s.bus.Publish(r.Context(), plugin.NewEvent(body.Type, body.Source, body.Payload))
	writeJSON(w, map[string]any{"ok": true})
}

// ---- helpers ----

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	_ = json.NewEncoder(w).Encode(v)
}

// ---- trash handlers ----

func (s *Server) handleTrashList(w http.ResponseWriter, r *http.Request) {
	items, err := s.vault.TrashList()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, items)
}

func (s *Server) handleTrashRestore(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Path string `json:"path"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	restored, err := s.vault.TrashRestore(body.Path)
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.restored", map[string]any{"path": body.Path, "restoredTo": restored})
	writeJSON(w, map[string]string{"ok": "true", "restoredTo": restored})
}

func (s *Server) handleTrashDelete(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Path string `json:"path"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json", http.StatusBadRequest)
		return
	}
	if err := s.vault.TrashPurge(body.Path); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	s.publishNote("note.purged", map[string]any{"path": body.Path})
	writeJSON(w, map[string]string{"ok": "true"})
}

func (s *Server) handleTrashEmpty(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if err := s.vault.TrashEmpty(); err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	s.publishNote("trash.emptied", map[string]any{})
	writeJSON(w, map[string]string{"ok": "true"})
}

// ---- bridge (中间平台主框架) handlers ---- //

// RegisterBridge 挂载中间平台到控制台 API（骨架）
func (s *Server) RegisterBridge(b *bridge.Bridge) { s.bridge = b }

// SetPluginManager 注入插件运行时管理器（启停/删除）
func (s *Server) SetPluginManager(m *PluginManager) { s.mgr = m }

// SetContextPacks 注入上下文包管理器（main 装配）
func (s *Server) SetContextPacks(m *ContextPackManager) { s.ctxPacks = m }

// handlePluginAction 通用：解析 id 并执行 mgr 动作
func (s *Server) handlePluginAction(w http.ResponseWriter, r *http.Request, act func(string) error) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		ID string `json:"id"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.ID == "" {
		http.Error(w, "missing id", http.StatusBadRequest)
		return
	}
	if err := act(body.ID); err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	writeJSON(w, map[string]any{"ok": true, "id": body.ID, "status": s.mgr.State(body.ID)})
}

func (s *Server) handlePluginStop(w http.ResponseWriter, r *http.Request) {
	s.handlePluginAction(w, r, s.mgr.Stop)
}

// SetPluginConfigPath 注入插件清单路径（热加插件时要重读它）
func (s *Server) SetPluginConfigPath(p string) { s.pluginCfgPath = p }

// handlePluginReload POST /api/plugins/reload —— 重读 config.yaml，把**新加**的插件热启起来。
//
// 语义（正面写清楚，免得被误用）：**只加不减、不动在跑的**。
//
//	· 新 id → 并进快照 + Spawn（走的是跟 Stop/Start 同一条 supervisor 通道，环境变量也同一套）
//	· 已在快照里 → 原样跳过（不重启、不打断）
//	· 回收站 / 永久禁用 → 跳过（跟启动时同一套过滤）
//	· config.yaml 里被删掉的 → **不碰**（要停走 /api/plugins/stop，要删走 /api/plugins/delete）
func (s *Server) handlePluginReload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if s.mgr == nil {
		http.Error(w, "插件管理器未装配", http.StatusServiceUnavailable)
		return
	}
	path := s.pluginCfgPath
	if path == "" {
		path = "config.yaml"
	}
	cfg, err := config.Load(path)
	if err != nil {
		http.Error(w, "读插件清单失败: "+err.Error(), http.StatusBadRequest)
		return
	}
	res := s.mgr.Reload(cfg.EnabledPlugins())
	// 热加进来的也要进心跳点名（启动时已有的都点过了，别重复加）
	if added, ok := res["added"].([]string); ok && len(added) > 0 {
		fresh := map[string]bool{}
		for _, id := range added {
			fresh[id] = true
		}
		for _, pc := range cfg.EnabledPlugins() {
			if fresh[pc.ID] {
				s.AddProbeTarget(ProbeTargetFromConfig(pc))
			}
		}
	}
	writeJSON(w, map[string]any{"ok": true, "config": path, "result": res})
}

func (s *Server) handlePluginStart(w http.ResponseWriter, r *http.Request) {
	s.handlePluginAction(w, r, s.mgr.Start)
}
func (s *Server) handlePluginDelete(w http.ResponseWriter, r *http.Request) {
	s.handlePluginAction(w, r, s.mgr.Delete)
}

// handlePluginTrashIDs 解析批量 id（body {ids:[...]} 或 {id:...}）
func (s *Server) handlePluginTrashIDs(w http.ResponseWriter, r *http.Request) ([]string, bool) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return nil, false
	}
	var body struct {
		ID  string   `json:"id"`
		IDs []string `json:"ids"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return nil, false
	}
	ids := body.IDs
	if body.ID != "" {
		ids = append(ids, body.ID)
	}
	if len(ids) == 0 {
		http.Error(w, "id/ids required", http.StatusBadRequest)
		return nil, false
	}
	return ids, true
}

// handlePluginTrashList GET /api/plugins/trash —— 插件回收站列表
func (s *Server) handlePluginTrashList(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.mgr.TrashList())
}

// handlePluginTrashRestore POST /api/plugins/trash/restore —— 恢复（单/批量）
func (s *Server) handlePluginTrashRestore(w http.ResponseWriter, r *http.Request) {
	ids, ok := s.handlePluginTrashIDs(w, r)
	if !ok {
		return
	}
	done, _ := s.mgr.RestoreBatch(ids)
	writeJSON(w, map[string]any{"ok": true, "restored": done})
}

// handlePluginTrashPurge POST /api/plugins/trash/purge —— 永久删除（单/批量）
func (s *Server) handlePluginTrashPurge(w http.ResponseWriter, r *http.Request) {
	ids, ok := s.handlePluginTrashIDs(w, r)
	if !ok {
		return
	}
	done, _ := s.mgr.PurgeBatch(ids)
	writeJSON(w, map[string]any{"ok": true, "purged": done})
}

// handlePluginRegister POST /api/bridge/plugins/register —— 外部插件自报家门
// body: {id, name, capabilities:[]}；幂等（插件重启后重新注册会更新）
func (s *Server) handlePluginRegister(w http.ResponseWriter, r *http.Request) {
	if !s.requirePluginToken(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		ID           string   `json:"id"`
		Name         string   `json:"name"`
		Capabilities []string `json:"capabilities"`
		Produces     []string `json:"produces"`
		Consumes     []string `json:"consumes"`
		// 可选：通道型插件可以声明"我背书哪个 agent"（如 DSH 通道声明 agent.dsh）。
		// 平台据此登记一个插件背书的 agent：组件列表/拓扑照旧可见，Inject 也能真的投到插件进程。
		AgentID     string   `json:"agentId"`
		AgentName   string   `json:"agentName"`
		AgentCaps   []string `json:"agentCapabilities"`
		AgentDesc   string   `json:"agentDescription"`
		DeliverTool string   `json:"deliverTool"`
		// 自报中文说明（"我是干什么的"）：控制台插件详情页直接展示，平台不硬编码
		Description string `json:"description"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	// 身份声明（agentId 等）与插件是**同一条注册**：不进第二个实体（2026-09-12 辞玖定）
	isNew, err := s.bridge.RegisterPlugin(bridge.PluginRegistration{
		ID: body.ID, Name: body.Name, Capabilities: body.Capabilities,
		Produces: body.Produces, Consumes: body.Consumes, Description: body.Description,
		AgentID: body.AgentID, AgentName: body.AgentName,
		AgentCapabilities: body.AgentCaps, AgentDescription: body.AgentDesc,
		DeliverTool: body.DeliverTool,
	})
	if err != nil {
		http.Error(w, err.Error(), http.StatusBadRequest)
		return
	}
	// 通知前端刷新组件列表（SSE）
	if s.bus != nil {
		action := "registered"
		if !isNew {
			action = "re-registered"
		}
		_ = s.bus.Publish(r.Context(), plugin.NewEvent("platform.plugin."+action, "plugin:"+body.ID,
			map[string]any{"id": body.ID, "component": "plugin", "capabilities": body.Capabilities}))
	}
	writeJSON(w, map[string]any{"ok": true, "id": body.ID, "isNew": isNew})
}

// handleBridgeAgents GET /api/bridge/agents —— 已接入的 agent 列表
func (s *Server) handleBridgeAgents(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.bridge.Agents())
}

// handleBridgeModules GET /api/bridge/modules —— 已挂载的功能模块列表
func (s *Server) handleBridgeModules(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, s.bridge.Modules())
}

// handleBridgeStatus GET /api/bridge/status —— 主框架状态
func (s *Server) handleBridgeStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{
		"agents":  len(s.bridge.Agents()),
		"modules": len(s.bridge.Modules()),
	})
}

// handleBridgeSend POST /api/bridge/send —— agent 消息入口（骨架桩）
func (s *Server) handleBridgeSend(w http.ResponseWriter, r *http.Request) {
	if !s.guardAgentUplink(w, r) { // agent 上报面鉴权（soft：记账放行 / strict：401）
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req bridge.AgentRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}
	s.noteRecv(req.AgentID) // 入站可见性：按来源计数，窗口汇总进账本
	resp, err := s.bridge.Handle(r.Context(), req)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, resp)
}

// ---- 平台账本接口（2026-09-11 改造）----

// recvSummaryWindow 入站汇总窗口
const recvSummaryWindow = 60 * time.Second

// SetLedger 注入平台账本（main 装配时调用）
func (s *Server) SetLedger(l *ledger.Ledger) { s.led = l }

// handleLedgerStats GET /api/ledger/stats —— 账本运行状态
func (s *Server) handleLedgerStats(w http.ResponseWriter, r *http.Request) {
	if s.led == nil {
		writeJSON(w, map[string]any{"enabled": false})
		return
	}
	writeJSON(w, s.led.Stats())
}

// handleLedgerVerify GET /api/ledger/verify —— 校验账本（缺口/坏行/边界）
func (s *Server) handleLedgerVerify(w http.ResponseWriter, r *http.Request) {
	// 账本没启用就**明说没启用**，不要去扫 data/ledger 报"2551 行、seq 正常"——
	// 那是假绿灯：内核打不开账本目录时一条都记不进去，而校验接口还在念旧账（演练 B 挖出）。
	if s.led == nil {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusServiceUnavailable)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"enabled": false,
			"error":   "账本未启用：内核没能打开账本目录，事件不会入账",
		})
		return
	}
	rep, err := ledger.Verify(s.led.Dir())
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, rep)
}

// handleLedgerRead GET /api/ledger/read?after=<seq>&limit= —— 位点读取（派生视图追赶消费用）
func (s *Server) handleLedgerRead(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	var after uint64
	if v := q.Get("after"); v != "" {
		if n, err := strconv.ParseUint(v, 10, 64); err == nil {
			after = n
		}
	}
	limit := 200
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	dir := "data/ledger"
	if s.led != nil {
		dir = s.led.Dir()
	}
	rows, err := ledger.ReadAfter(dir, after, limit)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]any{"after": after, "count": len(rows), "entries": rows})
}

// noteRecv 入站事件计数（按来源），每窗口惰性汇总成一条账本事件——
// 这是"某来源整段时间零到达"的照妖镜（对应 2026-09-11 的 4h26m 记账黑洞）。
func (s *Server) noteRecv(source string) {
	if s.led == nil {
		return
	}
	if source == "" {
		source = "(unknown)"
	}
	s.recvMu.Lock()
	if s.recvBySource == nil {
		s.recvBySource = map[string]uint64{}
	}
	s.recvBySource[source]++
	s.recvTotal++
	if time.Since(s.lastRecvFlush) < recvSummaryWindow {
		s.recvMu.Unlock()
		return
	}
	by := s.recvBySource
	total := s.recvTotal
	s.recvBySource = map[string]uint64{}
	s.recvTotal = 0
	s.lastRecvFlush = time.Now()
	s.recvMu.Unlock()

	_ = s.bus.Publish(context.Background(), plugin.NewEvent("platform.bridge.recv-summary", "bridge",
		map[string]any{"window_s": int(recvSummaryWindow.Seconds()), "by_source": by, "total": total}))
}

// handleLedgerDetail GET /api/ledger/detail?action=<id>&owner=&limit= —— 两级查询第二步：
// 先从总账按 action.id 找 ref，再按 ref.owner 去分账读回详情（分账缺失不算错）
func (s *Server) handleLedgerDetail(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	actionID := q.Get("action")
	if actionID == "" {
		http.Error(w, "action required", http.StatusBadRequest)
		return
	}
	dir := "data/ledger"
	if s.led != nil {
		dir = s.led.Dir()
	}
	limit := 200
	if v := q.Get("limit"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			limit = n
		}
	}
	ref, act, out := ledger.FindRef(dir, actionID)
	owner := q.Get("owner")
	if owner == "" && ref != nil {
		owner = ref.Owner
	}
	if owner == "" {
		writeJSON(w, map[string]any{"action": actionID, "found": false,
			"note": "总账里没有该动作的状态行（可能早于账本上线，或该动作未上报）"})
		return
	}
	rows, err := ledger.ReadDetail(dir, owner, actionID, limit)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, map[string]any{
		"action": actionID, "found": true, "owner": owner,
		"ref": ref, "action_desc": act, "outcome": out,
		"detail_count": len(rows), "detail": rows,
	})
}

// handleProbeStates GET /api/plugins/probe —— 心跳点名台账（活性快照）
func (s *Server) handleProbeStates(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{"summary": s.ProbeSummary(), "plugins": s.ProbeStates()})
}

// handleHealthz GET /healthz —— 给 L0 外部锚点（看门狗/计划任务）用的健康探针。
// 判据（只看"平台还能不能正常记账"，不看业务量）：
//
//	· 账本可用（未打开 = 不健康）
//	· 账本未处于 degraded（写盘失败后会置起，成功落盘自动清除）
//
// 返回 200 健康 / 503 不健康，body 带细节供排障。
func (s *Server) handleHealthz(w http.ResponseWriter, r *http.Request) {
	ok := true
	reasons := []string{}
	body := map[string]any{
		"ok":        ok,
		"pid":       os.Getpid(),
		"uptime_s":  int64(time.Since(s.startedAt).Seconds()),
		"processes": 0,
		"auth":      s.AgentAuthStats(), // 上报面鉴权：模式 + 有无带 token 的调用
	}
	if s.sup != nil {
		body["processes"] = len(s.sup.Status())
	}
	if s.led == nil {
		ok = false
		reasons = append(reasons, "ledger not enabled")
	} else {
		st := s.led.Stats()
		body["ledger"] = map[string]any{
			"file": st.File, "last_seq": st.LastSeq, "last_write_at": st.LastWriteAt,
			"write_errors": st.WriteErrors, "degraded": st.Degraded,
			"pending": st.Pending, "critical_backlog": st.CriticalBacklog,
		}
		if st.Degraded {
			ok = false
			reasons = append(reasons, "ledger degraded (写盘失败未恢复)")
		}
	}
	body["probe"] = s.ProbeSummary()
	body["ok"] = ok
	body["reasons"] = reasons
	if !ok {
		w.WriteHeader(http.StatusServiceUnavailable)
	}
	writeJSON(w, body)
}
