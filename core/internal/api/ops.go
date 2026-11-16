package api

// 软件操作的**执行侧**：agent 让平台做事，平台按档校验后代为执行（辞玖 2026-09-19：
// "所有操作封装在平台侧，不关 dsh 的事"）。
//
//	POST /api/ops/call {plugin, op, args}
//	  · op 取 OpCatalog 里的 id（如 brain.health）
//	  · 先比档位：op 的档位 > 该 agent 接口插件的档位 → 403（写明需要哪一档、当前哪一档）
//	  · 再代为调用库（AINOTES_URL，默认 127.0.0.1:8848）：路径参数从 args 填，GET 走 query、其余走 body
//
// 这样"哪些操作允许、由谁执行"全在平台，调用方（agent 侧）不需要自带工具集。

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

func findOp(id string) *OpEntry {
	for i := range OpCatalog {
		if OpCatalog[i].ID == id {
			return &OpCatalog[i]
		}
	}
	return nil
}

func levelOfPlugin(plugin string) string {
	lv := LoadPermLevels()[plugin]
	if _, ok := LevelOrder[lv]; ok {
		return lv
	}
	return DefaultLevel
}

var libClient = &http.Client{Timeout: 600 * time.Second}

// handleOpsCall POST /api/ops/call
func (s *Server) handleOpsCall(w http.ResponseWriter, r *http.Request) {
	var body struct {
		Plugin string         `json:"plugin"`
		Op     string         `json:"op"`
		Args   map[string]any `json:"args"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "bad json: " + err.Error()})
		return
	}
	body.Plugin = strings.TrimSpace(body.Plugin)
	if body.Plugin == "" {
		writeJSON(w, map[string]any{"ok": false, "error": "missing plugin"})
		return
	}
	// 调用方必须是**已登记的 agent 接口插件**（注册时自报了 agent_id）：
	// 否则任何一个本机进程都能自称某插件、按别人的档位调操作。
	if s.bridge == nil || s.bridge.PluginByID(body.Plugin) == nil ||
		s.bridge.PluginByID(body.Plugin).Info().AgentID == "" {
		writeJSON(w, map[string]any{"ok": false,
			"error": "不是已登记的 agent 接口插件：" + body.Plugin})
		return
	}
	entry := findOp(strings.TrimSpace(body.Op))
	if entry == nil {
		writeJSON(w, map[string]any{"ok": false, "error": "unknown op: " + body.Op})
		return
	}
	have := levelOfPlugin(body.Plugin)
	if LevelOrder[entry.Level] > LevelOrder[have] {
		writeJSON(w, map[string]any{
			"ok": false, "denied": true, "op": entry.ID,
			"need": entry.Level, "have": have,
			"error": fmt.Sprintf("「%s」需要「%s」档，%s 现在只放到「%s」档", entry.ID, entry.Level, body.Plugin, have),
		})
		return
	}

	// 组装对库的请求：路径参数从 args 里取，剩下的 GET 走 query、其余走 body
	method, path := "GET", entry.Op
	if i := strings.Index(entry.Op, " "); i > 0 {
		method, path = entry.Op[:i], strings.TrimSpace(entry.Op[i+1:])
	}
	args := body.Args
	if args == nil {
		args = map[string]any{}
	}
	used := map[string]bool{}
	for k, v := range args {
		token := "{" + k + "}"
		if strings.Contains(path, token) {
			path = strings.ReplaceAll(path, token, url.PathEscape(fmt.Sprint(v)))
			used[k] = true
		}
	}
	if strings.Contains(path, "{") {
		writeJSON(w, map[string]any{"ok": false, "error": "缺少路径参数：" + path})
		return
	}
	q := url.Values{}
	var payload map[string]any
	for k, v := range args {
		if used[k] {
			continue
		}
		if method == "GET" {
			q.Set(k, fmt.Sprint(v))
		} else {
			if payload == nil {
				payload = map[string]any{}
			}
			payload[k] = v
		}
	}
	target := notesTarget() + path
	if len(q) > 0 {
		target += "?" + q.Encode()
	}
	var rdr io.Reader
	if payload != nil {
		raw, _ := json.Marshal(payload)
		rdr = bytes.NewReader(raw)
	}
	req, err := http.NewRequest(method, target, rdr)
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	if payload != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	// 写入者标签（辞玖 2026-09-24 定）：agent 走接口调库，默认自报家门，
	// 库那边把它记进 events.writer —— "这篇是谁改的"就有据可查。
	req.Header.Set("X-Writer", "agent:"+strings.TrimPrefix(body.Plugin, "plugin."))
	resp, err := libClient.Do(req)
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "库不可达：" + err.Error()})
		return
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	var parsed any
	if json.Unmarshal(raw, &parsed) != nil {
		parsed = string(raw)
	}
	out := map[string]any{
		"ok": resp.StatusCode >= 200 && resp.StatusCode < 300,
		"op": entry.ID, "level": have, "op_level": entry.Level,
		"status": resp.StatusCode, "result": parsed,
	}
	if !out["ok"].(bool) {
		out["error"] = fmt.Sprintf("库返回 HTTP %d", resp.StatusCode)
	}
	writeJSON(w, out)
}

// handleOpsList GET /api/ops[?plugin=] —— 目录（可按插件标出放没放开）
func (s *Server) handleOpsList(w http.ResponseWriter, r *http.Request) {
	plugin := strings.TrimSpace(r.URL.Query().Get("plugin"))
	lv := levelOfPlugin(plugin)
	released := map[string]bool{}
	for _, id := range ReleasedOps(lv) {
		released[id] = true
	}
	ops := make([]map[string]any, 0, len(OpCatalog))
	for _, e := range OpCatalog {
		ops = append(ops, map[string]any{
			"id": e.ID, "level": e.Level, "op": e.Op, "label": e.Label,
			"released": plugin == "" || released[e.ID],
		})
	}
	writeJSON(w, map[string]any{"ok": true, "plugin": plugin, "level": lv, "ops": ops})
}
