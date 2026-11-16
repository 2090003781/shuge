package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// themeHub —— 主题的**实时广播**（辞玖 2026-10-06：「全实时」）。
//
// 为什么要有它（先把事实摆清）：
//   - 三个界面是三个不同的源（8790 / 8795 / 8848 端口不同）：localStorage 各存各的，
//     实测 BroadcastChannel 跨端口**不投递**、storage 事件也到不了；
//   - 所以跨端口只有一条路：**服务端**。以前靠"页面重新可见时拉一次"（切回标签才同步），
//     辞玖要的是"点一下就全都变" → 这里加一条 SSE（Server-Sent Events）长连接：
//     POST 主题的那一下，主程序把它推给所有正开着的页面，谁也不用等。
//
// 为什么用 SSE 而不是 WebSocket：
//   - 单向广播就够（页面 → 服务端走普通 POST），SSE 是纯 HTTP：不用握手库、不受代代理影响、
//     浏览器自带断线重连（EventSource 自己会重试），出问题一眼能在 curl 里看见。
//     少一层机制，少一处会坏的地方。
type themeHub struct {
	mu   sync.Mutex
	subs map[chan string]struct{}
}

func newThemeHub() *themeHub {
	return &themeHub{subs: map[chan string]struct{}{}}
}

func (h *themeHub) add() chan string {
	ch := make(chan string, 8) // 缓冲 8：读慢的页面不能拖住写的人
	h.mu.Lock()
	h.subs[ch] = struct{}{}
	h.mu.Unlock()
	return ch
}

func (h *themeHub) drop(ch chan string) {
	h.mu.Lock()
	if _, ok := h.subs[ch]; ok {
		delete(h.subs, ch)
		close(ch)
	}
	h.mu.Unlock()
}

// broadcast 推给所有连着的页面（非阻塞：满了就跳过这个页面，它下一拍会自己拉一次）
func (h *themeHub) broadcast(theme string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for ch := range h.subs {
		select {
		case ch <- theme:
		default:
		}
	}
}

func (h *themeHub) count() int {
	h.mu.Lock()
	defer h.mu.Unlock()
	return len(h.subs)
}

// themeNow 当前主题（唯一真相：data/ui-theme.json；读不到当深色）
func (st *serveState) themeNow() string {
	if b, err := os.ReadFile(filepath.Join(st.coreDir, "data", "ui-theme.json")); err == nil {
		var d struct {
			Theme string `json:"theme"`
		}
		if json.Unmarshal(b, &d) == nil && (d.Theme == "light" || d.Theme == "dark") {
			return d.Theme
		}
	}
	return "dark"
}

// themeClients 现在有几个页面连着实时通道（给自检看，不看也不影响功能）
func (st *serveState) themeClients() int {
	if st.theme == nil {
		return 0
	}
	return st.theme.count()
}

// handleUIThemeStream GET /api/ui/theme/stream —— SSE：主题一变就推给所有页面。
func (st *serveState) handleUIThemeStream(w http.ResponseWriter, r *http.Request) {
	fl, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "这个连接不支持流式推送", http.StatusInternalServerError)
		return
	}
	origin := r.Header.Get("Origin")
	if origin != "" {
		if !isLocalOrigin(origin) {
			http.Error(w, "只允许本机来源", http.StatusForbidden)
			return
		}
		w.Header().Set("Access-Control-Allow-Origin", origin)
		w.Header().Set("Vary", "Origin")
	}
	h := w.Header()
	h.Set("Content-Type", "text/event-stream; charset=utf-8")
	h.Set("Cache-Control", "no-cache, no-store")
	h.Set("Connection", "keep-alive")
	h.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	send := func(theme, ev string) bool {
		if ev != "" {
			fmt.Fprintf(w, "event: %s\n", ev)
		}
		b, _ := json.Marshal(map[string]any{"theme": theme, "at": time.Now().Format(time.RFC3339)})
		if _, err := fmt.Fprintf(w, "data: %s\n\n", b); err != nil {
			return false
		}
		fl.Flush()
		return true
	}
	if !send(st.themeNow(), "hello") { // 一连上先给当前值：新开的页面立刻对齐
		return
	}
	ch := st.theme.add()
	defer st.theme.drop(ch)
	beat := time.NewTicker(20 * time.Second) // 心跳：让中间的代理/浏览器知道这条连接还活着
	defer beat.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case theme, ok := <-ch:
			if !ok {
				return
			}
			if !send(theme, "theme") {
				return
			}
		case <-beat.C:
			if _, err := fmt.Fprint(w, ": ping\n\n"); err != nil {
				return
			}
			fl.Flush()
		}
	}
}
