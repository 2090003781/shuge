//go:build windows

// 本文件：**整体生命**（主进程自己的开关）+ 两个子项的「自动重连」开关。
//
// 辞玖 2026-09-20 定的模型：
//
//	主进程启停 = 我打开或关闭（桌面图标打开；页面上的「关闭枢阁」关闭）
//	主进程负责库与内核的**生命启停**：默认开启 + 自动重连；
//	手动关掉某一项 → 只表示"**不再自动重连**"（不杀正在跑的），要它回来就再打开。
//
// 落地用的还是既有那套"暂停标志"（data/core.paused），
// 新增一个主进程自己的 data/host.paused —— 计划任务（每 2 分钟）看到它就不再把整个软件拉起来。
// **库没有开关**（2026-09-20 辞玖："库不需要启停的说法，永远保持在线"）：只有在"关闭枢阁"时
// 才作为整体退出的一环被停掉。
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

func hostPausedPath(coreDir string) string { return filepath.Join(coreDir, "data", "host.paused") }

func hostPaused(coreDir string) bool { return fileExists(hostPausedPath(coreDir)) }

// setHostPaused 置 / 清「主进程被人工关闭」标志。清它 = 允许计划任务再把软件打开。
func setHostPaused(coreDir string, on bool) {
	p := hostPausedPath(coreDir)
	if on {
		_ = os.WriteFile(p, []byte(time.Now().Format(time.RFC3339)+"\n"), 0o644)
		return
	}
	_ = os.Remove(p)
}

// handlePower 整体生命：
//
//	POST /api/host/power/off  关闭枢阁（停库 → 停内核 → 置 host.paused → 主进程退出）
//	POST /api/host/power/on   打开（清 host.paused；真正的"打开"由桌面图标/--force-start 完成）
func (st *serveState) handlePower(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	on := r.URL.Path == "/api/host/power/on"
	if on {
		setHostPaused(st.coreDir, false)
		logf(st.logDir, "主进程：收到「打开枢阁」（已清 host.paused）")
		writeJSON(w, map[string]any{"ok": true, "host_paused": false})
		return
	}

	setHostPaused(st.coreDir, true) // 先落标志：免得计划任务在这一两秒里又拉起来
	logf(st.logDir, "主进程：收到「关闭枢阁」→ 停库、停内核，然后退出（之后不再自动打开）")
	writeJSON(w, map[string]any{"ok": true, "host_paused": true,
		"note": "正在关闭：先停库、再停内核，然后主进程退出；重新打开请用桌面图标「★启动枢阁」"})
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	go func() {
		notesHoldFor(10 * time.Minute) // 整体关闭：别让退出看护在这 1.5 秒里把库又拉起来
		killed := stopNotes(st.notes)
		st.stopCore()
		logf(st.logDir, "已关闭枢阁（停库杀 %d 个进程；已置 host.paused/core.paused）", killed)
		time.Sleep(1500 * time.Millisecond) // 让浏览器把回执读完
		os.Exit(0)
	}()
}

// handleAutoReconnect 内核的「自动重连」开关（库没有这一项：它永远在线）：
//
//	POST /api/core/autoreconnect {"on":true|false}
//
// on=true  → 清暂停标志 + 立刻确保它在跑（人工意图，走 force）
// on=false → 置暂停标志（**不杀**正在跑的内核：那只表示"挂了不再自动拉起"）
func (st *serveState) handleAutoReconnect(kind string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		var body struct {
			On *bool `json:"on"`
		}
		_ = decodeBody(r, &body)
		if body.On == nil {
			writeJSON(w, map[string]any{"ok": false, "err": "缺 on（true/false）"})
			return
		}
		on := *body.On
		switch kind {
		case "core":
			paused := filepath.Join(st.coreDir, "data", "core.paused")
			if on {
				_ = os.Remove(paused)
				_ = os.Remove(filepath.Join(st.coreDir, "data", "core.quarantine"))
				_ = os.Remove(filepath.Join(st.logDir, "host.starts"))
				logf(st.logDir, "内核：自动重连已打开（清 core.paused/熔断，立刻确保在跑）")
				checkAndStart(st.coreDir, st.url, "枢阁控制台（打开内核的自动重连）", true, st.notes)
			} else {
				_ = os.WriteFile(paused, []byte(time.Now().Format(time.RFC3339)+"\n"), 0o644)
				logf(st.logDir, "内核：自动重连已关（置 core.paused；正在跑的内核不动）")
			}
			writeJSON(w, map[string]any{"ok": true, "kind": "core", "autoreconnect": on})
		default:
			writeJSON(w, map[string]any{"ok": false, "err": fmt.Sprintf("未知对象 %s", kind)})
		}
	}
}

// decodeBody 读 JSON 请求体（读不动就当空体，由调用方按缺字段报错）。
func decodeBody(r *http.Request, v any) error {
	defer r.Body.Close()
	return json.NewDecoder(r.Body).Decode(v)
}
