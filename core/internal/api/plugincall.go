// 本文件：external 插件调用通道（协议第 3 步：平台 → 插件 → 回结果）。
//
// 设计（长轮询，纯标准库友好）：
//
//	平台调用：POST /api/bridge/plugins/call {plugin, tool, args, timeoutMs}
//	          → 投递到该插件的待办队列 → 阻塞等待结果（或超时）
//	插件取活：GET  /api/bridge/plugins/poll?plugin=<id>&wait=<sec>   （长轮询，返回待办调用列表）
//	插件回结果：POST /api/bridge/plugins/result {callId, ok, data, error}
//	          → 平台唤醒等待方，把结果返回给调用者
//
// 为什么轮询而非 SSE：external 插件可能用任何语言写，长轮询只需一个 HTTP GET 即可实现，
// 无流式解析负担；wait 参数让它接近实时。
package api

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// PluginCall 一次平台 → 插件的调用
type PluginCall struct {
	CallID    string         `json:"callId"`
	Plugin    string         `json:"plugin"`
	Tool      string         `json:"tool"`
	Args      map[string]any `json:"args,omitempty"`
	CreatedAt string         `json:"createdAt"`
}

// PluginCallResult 插件回传的结果
type PluginCallResult struct {
	OK    bool   `json:"ok"`
	Data  any    `json:"data,omitempty"`
	Error string `json:"error,omitempty"`
}

// pluginCallBroker 调用中转（待办队列 + 等待唤醒）
type pluginCallBroker struct {
	mu      sync.Mutex
	queue   map[string][]*PluginCall         // plugin id → 待办调用
	waiters map[string]chan PluginCallResult // callID → 结果通道
	seq     int64
}

func newPluginCallBroker() *pluginCallBroker {
	return &pluginCallBroker{
		queue:   map[string][]*PluginCall{},
		waiters: map[string]chan PluginCallResult{},
	}
}

// enqueue 投递调用并返回 callID + 结果通道
func (b *pluginCallBroker) enqueue(pluginID, tool string, args map[string]any) (string, chan PluginCallResult) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.seq++
	callID := fmt.Sprintf("call-%d-%d", time.Now().UnixMilli(), b.seq)
	call := &PluginCall{
		CallID:    callID,
		Plugin:    pluginID,
		Tool:      tool,
		Args:      args,
		CreatedAt: time.Now().Format(time.RFC3339),
	}
	b.queue[pluginID] = append(b.queue[pluginID], call)
	ch := make(chan PluginCallResult, 1)
	b.waiters[callID] = ch
	return callID, ch
}

// take 取走某插件的待办调用（长轮询用）
func (b *pluginCallBroker) take(pluginID string) []*PluginCall {
	b.mu.Lock()
	defer b.mu.Unlock()
	calls := b.queue[pluginID]
	if len(calls) == 0 {
		return nil
	}
	delete(b.queue, pluginID)
	return calls
}

// deliver 插件回结果，唤醒等待方
func (b *pluginCallBroker) deliver(callID string, res PluginCallResult) bool {
	b.mu.Lock()
	ch, ok := b.waiters[callID]
	if ok {
		delete(b.waiters, callID)
	}
	b.mu.Unlock()
	if !ok {
		return false
	}
	select {
	case ch <- res:
	default:
	}
	close(ch)
	return true
}

// forget 调用超时后清理
func (b *pluginCallBroker) forget(callID string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	delete(b.waiters, callID)
}

// CallPlugin 平台**内部**调用插件（复用同一条长轮询通道）。
//
// 给谁用：插件背书的 agent（bridge.ExternalAgent）做定向投递——DSH 通道这类"通道型插件"，
// 平台需要把事件直接交给某个插件进程，而不是广播给所有人。
// 语义：入队 → 等回执（带超时）→ 超时则清理 waiter 并报错（调用方据此记 platform.inject.failed）。
func (s *Server) CallPlugin(ctx context.Context, pluginID, tool string, args map[string]any, timeout time.Duration) (map[string]any, error) {
	if s.pluginCalls == nil {
		return nil, fmt.Errorf("插件调用通道未初始化")
	}
	if timeout <= 0 {
		timeout = 15 * time.Second
	}
	callID, ch := s.pluginCalls.enqueue(pluginID, tool, args)
	select {
	case res := <-ch:
		return map[string]any{"ok": res.OK, "data": res.Data, "error": res.Error, "callId": callID}, nil
	case <-time.After(timeout):
		s.pluginCalls.forget(callID)
		return nil, fmt.Errorf("插件 %s 在 %v 内没有回执（看它的 poll 循环是否活着）", pluginID, timeout)
	case <-ctx.Done():
		s.pluginCalls.forget(callID)
		return nil, ctx.Err()
	}
}

// ---- HTTP handlers（挂在 Server 上）----

// handlePluginCall POST /api/bridge/plugins/call —— 平台（或 agent）调用插件工具
func (s *Server) handlePluginCall(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		Plugin    string         `json:"plugin"`
		Tool      string         `json:"tool"`
		Args      map[string]any `json:"args"`
		TimeoutMs int            `json:"timeoutMs"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.Plugin == "" || body.Tool == "" {
		http.Error(w, "plugin/tool required", http.StatusBadRequest)
		return
	}
	if body.TimeoutMs <= 0 {
		body.TimeoutMs = 15000
	}
	callID, ch := s.pluginCalls.enqueue(body.Plugin, body.Tool, body.Args)
	select {
	case res := <-ch:
		writeJSON(w, map[string]any{"ok": res.OK, "callId": callID,
			"data": res.Data, "error": res.Error})
	case <-time.After(time.Duration(body.TimeoutMs) * time.Millisecond):
		s.pluginCalls.forget(callID)
		writeJSON(w, map[string]any{"ok": false, "callId": callID,
			"error": "plugin call timeout (插件未在超时内回复，检查其 poll 循环)"})
	}
}

// handlePluginPoll GET /api/bridge/plugins/poll?plugin=x&wait=20 —— 插件长轮询取活
func (s *Server) handlePluginPoll(w http.ResponseWriter, r *http.Request) {
	if !s.requirePluginToken(w, r) {
		return
	}
	pluginID := r.URL.Query().Get("plugin")
	if pluginID == "" {
		http.Error(w, "plugin required", http.StatusBadRequest)
		return
	}
	waitSec := 20
	if v := r.URL.Query().Get("wait"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 && n <= 60 {
			waitSec = n
		}
	}
	deadline := time.Now().Add(time.Duration(waitSec) * time.Second)
	for {
		if calls := s.pluginCalls.take(pluginID); len(calls) > 0 {
			writeJSON(w, map[string]any{"calls": calls})
			return
		}
		if time.Now().After(deadline) {
			// 长轮询结束：返回空（插件立即再轮询）
			writeJSON(w, map[string]any{"calls": []PluginCall{}})
			return
		}
		select {
		case <-r.Context().Done():
			return
		case <-time.After(300 * time.Millisecond):
		}
	}
}

// handlePluginResult POST /api/bridge/plugins/result —— 插件回传执行结果
func (s *Server) handlePluginResult(w http.ResponseWriter, r *http.Request) {
	if !s.requirePluginToken(w, r) {
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var body struct {
		CallID string `json:"callId"`
		OK     bool   `json:"ok"`
		Data   any    `json:"data"`
		Error  string `json:"error"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	if body.CallID == "" {
		http.Error(w, "callId required", http.StatusBadRequest)
		return
	}
	ok := s.pluginCalls.deliver(body.CallID, PluginCallResult{OK: body.OK, Data: body.Data, Error: body.Error})
	writeJSON(w, map[string]any{"ok": ok})
}
