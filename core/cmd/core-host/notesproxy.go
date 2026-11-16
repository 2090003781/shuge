//go:build windows

// 本文件：枢阁主进程侧的 ai-notes 反向代理（core 侧另有一份，约定一致）。
// 内核停着时笔记页面仍然可用。
//
//	GET /notes    → 302 /notes/
//	GET /notes/…  → 反代到 ai-notes（默认 http://127.0.0.1:8848，AINOTES_URL 可覆盖）
package main

import (
	"io"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"strings"
)

const notesMount = "/notes"

func notesTarget() string {
	if v := os.Getenv("AINOTES_URL"); v != "" {
		return v
	}
	return "http://127.0.0.1:8848"
}

// handleNotesMount GET /notes — 重定向到 /notes/
func (st *serveState) handleNotesMount(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, notesMount+"/", http.StatusFound)
}

// handleNotesReverse /notes/{path...} — 反向代理到 ai-notes
func (st *serveState) handleNotesReverse(w http.ResponseWriter, r *http.Request) {
	p := strings.TrimPrefix(r.URL.Path, notesMount)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	// 白名单：只放行 ai-notes 自己的入口
	if p != "/" && !strings.HasPrefix(p, "/api/") && !strings.HasPrefix(p, "/index.html") {
		http.Error(w, "ai-notes: not found "+p, http.StatusNotFound)
		return
	}

	target, err := url.Parse(notesTarget())
	if err != nil {
		http.Error(w, "ai-notes: bad target url: "+err.Error(), http.StatusInternalServerError)
		return
	}
	proxy := httputil.NewSingleHostReverseProxy(target)
	orig := proxy.Director
	proxy.Director = func(req *http.Request) {
		orig(req)
		req.URL.Path = p // /notes/api/tree → /api/tree
		req.URL.RawPath = ""
		req.Host = target.Host
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusBadGateway)
		io.WriteString(w, `{"ok":false,"error":"ai-notes 服务不可达（`+notesTarget()+`）：`+err.Error()+`",
"hint":"先启动 ai-notes：python src/server.py --port 8848"}`)
	}
	proxy.ServeHTTP(w, r)
}
