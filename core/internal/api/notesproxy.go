// 本文件：把 ai-notes（库 + 检索 + 界面）挂到枢阁 core 的一个入口下。
//
// 为什么需要它（2026-09-13 实测）：
//
//	浏览器把「不同端口」也算不同源 —— 从 DSH 页面(3080) 或枢阁控制台(8790)
//	去请求 ai-notes(8848) 会被 CORS 拦；而直接导航到 8848 在用户环境里也打不开。
//	结论：不要再让用户记第二个端口。由 core 做**同源反向代理**，界面与 API 都在 8790。
//
// 设计：
//
//	· 挂在 /notes/* 下（子路径），ai-notes 前端用相对前缀拼 API，天然适配
//	· 只放行 ai-notes 自己的路由（/、/api/*）；其他路径 404，避免变成一个开放代理
//	· 目标地址可用环境变量 AINOTES_URL 覆盖（默认 http://127.0.0.1:8848）
//	· 目标不可用时给出明确提示（而不是 502 白屏）
package api

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

// handleNotesMount GET /notes — 无尾斜杠时重定向到 /notes/
func (s *Server) handleNotesMount(w http.ResponseWriter, r *http.Request) {
	http.Redirect(w, r, notesMount+"/", http.StatusFound)
}

// handleNotesReverse GET/POST/PUT/DELETE /notes/{path...} — 反向代理到 ai-notes
func (s *Server) handleNotesReverse(w http.ResponseWriter, r *http.Request) {
	p := strings.TrimPrefix(r.URL.Path, notesMount)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	// 白名单：只放行 ai-notes 自己的入口，别把 core 变成开放代理
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
	// 去掉挂载前缀：/notes/api/tree → /api/tree
	orig := proxy.Director
	proxy.Director = func(req *http.Request) {
		orig(req)
		req.URL.Path = p
		req.URL.RawPath = ""
		req.Host = target.Host
		// 控制台代用户访问库：标成 console（用户经控制台的操作，跟 agent 区分开）
		req.Header.Set("X-Writer", "console")
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusBadGateway)
		io.WriteString(w, `{"ok":false,"error":"ai-notes 服务不可达（`+notesTarget()+`）：`+err.Error()+`",
"hint":"先启动 ai-notes：python src/server.py --port 8848"}`)
	}
	proxy.ServeHTTP(w, r)
}
