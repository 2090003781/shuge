//go:build windows

// 本文件：控制台里的**日志区**接口（主进程自己提供，因此内核不在也能看）。
//
// 为什么放主进程而不是继续用内核的 /api/logs：辞玖 2026-09-20 定了"控制台=主进程页"，
// 而控制台必须在内核停着时照样可用 —— 日志恰恰是内核挂了以后最要看的东西。
// 数据来源本来就是文件（平台是"输出中枢"：所有子进程输出落在 data/logs/<source>.log），
// 所以主进程直接读文件即可，不经过内核。
//
//	GET /api/host/logsources                 有哪些来源（data/logs/*.log + 库的输出）
//	GET /api/host/log?source=<x>&tail=300    某来源最近 N 行（统一格式原样）
//	GET /api/host/log?source=__events__…     平台事件（总线·账本）：经内核 /api/events/search，
//	                                         内核不在就明确回"不可用"，不装作没日志
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	logTailMaxBytes = 1 << 20 // 只读文件尾巴：core.log 能到几 MB，全读没必要
	logTailCached   = 1500    // 缓存里最多留这么多行；再多就直接读盘
	eventsSourceID  = "__events__"
)

// 日志尾巴缓存：按 (size, mtime) 认版本 —— 文件没变就不再读盘。
// 页面「全部（聚合）」每 5 秒要把十来个来源各读一遍，没这个缓存就是每 5 秒读十几 MB。
type tailCacheEntry struct {
	size  int64
	mtime time.Time
	lines []string
}

var (
	tailCacheMu sync.Mutex
	tailCache   = map[string]tailCacheEntry{}
)

// tailOf 读某文件尾巴（带缓存）；n = 需要的行数
func tailOf(path string, n int) ([]string, bool) {
	stt, err := os.Stat(path)
	if err != nil {
		return nil, false
	}
	if n <= logTailCached {
		tailCacheMu.Lock()
		if e, ok := tailCache[path]; ok && e.size == stt.Size() && e.mtime.Equal(stt.ModTime()) && len(e.lines) >= n {
			lines := e.lines[len(e.lines)-n:]
			tailCacheMu.Unlock()
			return lines, true
		}
		tailCacheMu.Unlock()
	}
	lines, ok := tailFile(path, logTailCached, logTailMaxBytes)
	if !ok {
		return nil, false
	}
	if n <= logTailCached {
		tailCacheMu.Lock()
		tailCache[path] = tailCacheEntry{size: stt.Size(), mtime: stt.ModTime(), lines: lines}
		tailCacheMu.Unlock()
	}
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return lines, true
}

type logSource struct {
	Source  string `json:"source"`
	Name    string `json:"name"`
	Bytes   int64  `json:"bytes"`
	Updated string `json:"updated"`
	Path    string `json:"-"`
}

// logSourceName 给来源起个中文名（面板上直接认得出是谁在说话）
func logSourceName(id string) string {
	switch id {
	case "host":
		return "主进程"
	case "core":
		return "内核"
	case "notes":
		return "库"
	case "notes-host":
		return "库·看护"
	}
	if strings.HasPrefix(id, "plugin.") {
		return "插件 " + strings.TrimPrefix(id, "plugin.")
	}
	return id
}

// collectLogSources 主进程侧的来源台账：data/logs/*.log + 库自己的输出日志
func (st *serveState) collectLogSources() []logSource {
	out := []logSource{}
	seen := map[string]bool{}
	add := func(id, path string) {
		if id == "" || seen[id] {
			return
		}
		stt, err := os.Stat(path)
		if err != nil || stt.IsDir() {
			return
		}
		seen[id] = true
		out = append(out, logSource{Source: id, Name: logSourceName(id), Bytes: stt.Size(),
			Updated: stt.ModTime().Format(time.RFC3339), Path: path})
	}

	if entries, err := os.ReadDir(st.logDir); err == nil {
		for _, e := range entries {
			n := e.Name()
			if e.IsDir() || !strings.HasSuffix(n, ".log") {
				continue
			}
			add(strings.TrimSuffix(n, ".log"), filepath.Join(st.logDir, n))
		}
	}
	// 库的输出（在库仓库里，不在平台日志目录）
	if st.notes.Dir != "" {
		add("notes", st.notes.outLog())
		// 库的**看护日志**：主进程保活库时写的是 <库目录>/data/logs/host.log（notes.go 里的 logDir），
		// 不在平台日志目录里 —— 不加进来，"库为什么反复被拉起"在控制台上永远看不到（2026-09-20 审计发现）。
		add("notes-host", filepath.Join(st.notes.Dir, "data", "logs", "host.log"))
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Bytes != out[j].Bytes {
			return out[i].Bytes > out[j].Bytes
		}
		return out[i].Source < out[j].Source
	})
	return out
}

// safeSourceID 白名单化来源名（防路径穿越）
func safeSourceID(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9',
			r == '.', r == '-', r == '_':
			b.WriteRune(r)
		}
	}
	return b.String()
}

// tailFile 读文件最后 maxBytes 字节，返回末尾 n 行
func tailFile(path string, n int, maxBytes int64) ([]string, bool) {
	f, err := os.Open(path)
	if err != nil {
		return nil, false
	}
	defer f.Close()
	stt, err := f.Stat()
	if err != nil {
		return nil, false
	}
	start := int64(0)
	if stt.Size() > maxBytes {
		start = stt.Size() - maxBytes
	}
	if _, err := f.Seek(start, 0); err != nil {
		return nil, false
	}
	buf := make([]byte, stt.Size()-start)
	if _, err := f.Read(buf); err != nil && len(buf) > 0 {
		// 读到部分也算（文件正在被写时常见）
	}
	lines := strings.Split(strings.TrimRight(string(buf), "\n"), "\n")
	if start > 0 && len(lines) > 0 {
		lines = lines[1:] // 首行可能被截断
	}
	if len(lines) > n {
		lines = lines[len(lines)-n:]
	}
	return lines, true
}

func (st *serveState) handleLogSources(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, map[string]any{
		"ok":        true,
		"kernel_up": st.kernelAnswering(),
		"sources":   st.collectLogSources(),
	})
}

// handleLogTimeline GET /api/host/log/timeline?tail=N —— 全部来源合并成一条时间线（**一笔请求**）
//
// 为什么要有它（2026-10-12）：界面上的"全部（聚合时间线）"原来是在浏览器里对每个来源各发一笔
// /api/host/log，库里 30 多个来源 = 每 5 秒 30 多笔请求。合并这件事在服务端做一次就够，
// 排序口径与前端原来那段完全一致：各来源先取 max(40, tail/来源数) 行，再按时间戳倒序取前 tail 行。
func (st *serveState) handleLogTimeline(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	tail := 500
	if v := q.Get("tail"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 5000 {
			tail = n
		}
	}
	srcs := st.collectLogSources()
	if len(srcs) == 0 {
		writeJSON(w, map[string]any{"ok": true, "count": 0, "sources": 0, "lines": []string{}})
		return
	}
	per := tail / len(srcs)
	if per < 40 {
		per = 40
	}
	type tsLine struct {
		ts   string
		line string
	}
	all := make([]tsLine, 0, per*len(srcs))
	for _, s := range srcs {
		lines, ok := tailOf(s.Path, per)
		if !ok {
			continue
		}
		for _, ln := range lines {
			ts := ln
			if i := strings.Index(ln, "|"); i >= 0 {
				ts = strings.TrimSpace(ln[:i])
			}
			all = append(all, tsLine{ts: ts, line: ln})
		}
	}
	// 时间戳倒序（新的在上）—— 与前端 localeCompare 的口径一致
	sort.SliceStable(all, func(i, j int) bool { return all[i].ts > all[j].ts })
	if len(all) > tail {
		all = all[:tail]
	}
	out := make([]string, 0, len(all))
	for _, x := range all {
		out = append(out, x.line)
	}
	writeJSON(w, map[string]any{"ok": true, "count": len(out), "sources": len(srcs), "lines": out})
}

func (st *serveState) handleLogTail(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	source := q.Get("source")
	if source == "" {
		writeJSON(w, map[string]any{"ok": false, "err": "缺 source（先看 /api/host/logsources）"})
		return
	}
	tail := 300
	if v := q.Get("tail"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 5000 {
			tail = n
		}
	}

	// 平台事件（总线·账本）：只有内核在的时候才有
	if source == eventsSourceID {
		if !st.kernelAnswering() {
			// 界面在内核不在时根本不会列这个来源；真被问到就安静地回空，不吐"不可用"这种话。
			writeJSON(w, map[string]any{"ok": true, "source": source, "found": false, "lines": []string{}})
			return
		}
		lines, err := st.eventLines(tail)
		if err != nil {
			writeJSON(w, map[string]any{"ok": false, "source": source, "lines": []string{},
				"err": "读平台事件失败：" + err.Error()})
			return
		}
		writeJSON(w, map[string]any{"ok": true, "source": source, "found": true,
			"count": len(lines), "lines": lines})
		return
	}

	id := safeSourceID(source)
	if id == "" {
		writeJSON(w, map[string]any{"ok": false, "err": "来源名不合法"})
		return
	}
	var path string
	for _, s := range st.collectLogSources() {
		if s.Source == id {
			path = s.Path
			break
		}
	}
	if path == "" {
		writeJSON(w, map[string]any{"ok": false, "source": id, "found": false, "lines": []string{},
			"err": "没有这个来源：" + id})
		return
	}
	lines, ok := tailOf(path, tail)
	writeJSON(w, map[string]any{"ok": true, "source": id, "found": ok, "count": len(lines), "lines": lines})
}

// eventLines 把内核账本查询结果压成统一格式行（与 logs.js 同款：ts | type | source | payload）
func (st *serveState) eventLines(limit int) ([]string, error) {
	url := fmt.Sprintf("%s/api/events/search?level=all&limit=%d", st.coreBase(), limit)
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	var rows []struct {
		TS      string          `json:"ts"`
		Type    string          `json:"type"`
		Source  string          `json:"source"`
		Payload json.RawMessage `json:"payload"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&rows); err != nil {
		return nil, err
	}
	out := make([]string, 0, len(rows))
	for _, r := range rows {
		p := strings.TrimSpace(string(r.Payload))
		if len(p) > 400 {
			p = p[:400] + "…"
		}
		out = append(out, strings.Join([]string{r.TS, r.Type, r.Source, p}, " | "))
	}
	return out, nil
}
