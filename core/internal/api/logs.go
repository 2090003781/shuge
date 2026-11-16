// 本文件：统一日志视图接口——把 logsink 收进来的各来源日志，按同一格式读出来。
//
// 定位（辞玖 2026-09-11）：平台不做"控制台宿主"，而做"输出中枢"。
// 子进程没有控制台（免得弹窗/打架），输出全部落 data/logs/<source>.log，
// 这里提供两个只读接口给控制台/网页用：
//
//	GET /api/logs/sources                     有哪些来源、各多大、最后写入时间
//	GET /api/logs?source=<x>&tail=200         某来源最近 N 行（原始统一格式行）
package api

import (
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

// SetLogDir 注入日志根目录（main 装配时调用）
func (s *Server) SetLogDir(dir string) { s.logDir = dir }

// handleLogSources GET /api/logs/sources —— 日志来源台账
func (s *Server) handleLogSources(w http.ResponseWriter, r *http.Request) {
	dir := s.logDir
	if dir == "" {
		dir = filepath.Join("data", "logs")
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		writeJSON(w, []any{})
		return
	}
	type item struct {
		Source  string `json:"source"`
		Bytes   int64  `json:"bytes"`
		Updated string `json:"updated"`
		File    string `json:"file"`
	}
	out := []item{}
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".log") {
			continue
		}
		st, err := e.Info()
		if err != nil {
			continue
		}
		name := strings.TrimSuffix(e.Name(), ".log")
		if i := strings.Index(name, "."); i > 0 { // plugin.xxx.log → xxx 作为来源名更易读
			// 保留完整名，前端自行裁剪
		}
		out = append(out, item{Source: name, Bytes: st.Size(),
			Updated: st.ModTime().Format("2006-01-02T15:04:05Z07:00"), File: e.Name()})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Updated > out[j].Updated })
	writeJSON(w, out)
}

// handleLogs GET /api/logs?source=<x>&tail=N —— 某来源最近 N 行（只读，不改格式）
func (s *Server) handleLogs(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	source := q.Get("source")
	if source == "" {
		http.Error(w, "source required（用 /api/logs/sources 查有哪些）", http.StatusBadRequest)
		return
	}
	tail := 200
	if v := q.Get("tail"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 5000 {
			tail = n
		}
	}
	dir := s.logDir
	if dir == "" {
		dir = filepath.Join("data", "logs")
	}
	path := filepath.Join(dir, safeLogName(source)+".log")
	data, err := os.ReadFile(path)
	if err != nil {
		writeJSON(w, map[string]any{"source": source, "found": false, "lines": []string{}})
		return
	}
	lines := strings.Split(strings.TrimRight(string(data), "\n"), "\n")
	if len(lines) > tail {
		lines = lines[len(lines)-tail:]
	}
	writeJSON(w, map[string]any{"source": source, "found": true, "count": len(lines), "lines": lines})
}

// safeLogName 白名单化来源名（防路径穿越）
func safeLogName(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9',
			r == '.', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	if b.Len() == 0 {
		return "unknown"
	}
	return b.String()
}
