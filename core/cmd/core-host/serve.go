//go:build windows

// 本文件：枢阁主进程（宿主）的 HTTP 面 —— 前门 + 控制台 + 内核启停。
//
// 层级（辞玖 2026-09-12 定案，不许搞反）：
//
//	枢阁（笔记软件）      = 主进程 = 本程序（常驻、无窗口）
//	   ├─ 拥有：整个界面（控制台）、启停内核的能力、保活
//	   └─ 内核（core / 平台） = 附属进程，可被启停；提供笔记数据、总线、账本、插件、agent 桥
//
// 三个入口：
//
//	GET  /                   前门：内核在 → 透明反向代理到内核（完整笔记软件界面）
//	                              内核不在 → 出「外壳页」：内核状态 + 启动按钮 + 缓存笔记树 + 日志
//	GET  /console            主进程自己的控制台（内嵌到内核界面左栏，也可单独打开；?embed=1 精简）
//	GET  /notes/*            笔记视图：反代到 ai-notes（不依赖内核）
//	POST /api/core/{start,stop,restart}   启停内核
//
// 为什么内核不在时页面不能是"打不开"：用户打开的是**笔记软件**，不是内核。
package main

import (
	"context"
	"core/internal/singlelock"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const (
	servePortDefault = 8790 // 与下面候选表首选一致（8791 是历史遗留：那时内核还占着 8790）
	// monitorEvery 巡检间隔（2026-09-20 辞玖："要尽可能的快"：20s → 1s）。
	// 为什么 1 秒也伤不到库：这一拍在稳态下**只对库做一次 TCP 探端口**（不等 HTTP），
	// 库是单线程服务，既不会被探针挤占、也不会被刷出访问日志；
	// HTTP 健康查询仍只在状态接口（页面 3 秒一次，和以前一样）与"端口不通要确认"时发生。
	// 内核那一侧每拍一次 /healthz（并发进程，代价可忽略）。
	monitorEvery = 1 * time.Second
	cacheEvery   = 60 * time.Second
)

// 候选端口（按顺序试；都不可用就让系统随便给一个）
// 前门候选端口：**8790 = 枢阁的唯一地址**（内核已挪到 8795，不再和它抢）；
// 后面的备选只为端口被瞬时占用时还能起来（实际端口写 data/host.port）。
// 候选端口：**首选 8790**（枢阁唯一地址），后面是"被瞬时占用时还能起来"的备选。
// 2026-10-10 从 6 个扩到 30 个：原来是"6 个都占就退出"，那会让"点启动没反应"变成必然。
// 但**绝不退到随机端口**（2026-09-27 的教训：随机端口会把 host.port 改掉，探针再也找不到它）。
var servePortCandidates = func() []int {
	out := make([]int, 0, 30)
	for p := 8790; p <= 8799; p++ {
		out = append(out, p)
	}
	for p := 8890; p <= 8899; p++ {
		out = append(out, p)
	}
	for p := 18790; p <= 18799; p++ {
		out = append(out, p)
	}
	return out
}()

type serveState struct {
	coreDir     string
	notes       notesSpec // 主体：先保它，再保内核
	url         string    // 内核健康探针（.../healthz）
	launcher    string
	logDir      string
	startAt     time.Time
	port        int
	patrolEvery time.Duration

	hostLock  *singlelock.Lock // 启停台单实例锁（持有到进程退出）
	mu        sync.Mutex       // 保护 busy
	busy      bool
	busySince time.Time // busy 是什么时候置上的（看护用）

	proxy *httputil.ReverseProxy

	cacheMu sync.Mutex
	tree    json.RawMessage
	treeAt  time.Time

	tokenMu    sync.Mutex
	tokenCache string
	tokenAt    time.Time

	patrolMu    sync.Mutex
	patrolCache *patrolState

	theme *themeHub // 界面主题的实时广播（SSE，见 themehub.go）

	// 界面装配（2026-10-10）：主进程内再开一个**只发界面**的回环监听。
	// 为什么：界面是硬盘上的文件，本该"打开就有"，不该依赖唯一地址那条网关活着。
	// 端口写进 data/ui-assets.port；进程退了这个监听就没了（所以判据是"端口 + /healthz 都通"）。
	localLn   net.Listener
	localPort int
}

// busyStuckAfter busy 超过这么久还没放开 → 判定"人工动作卡死了"，自动复位。
//
// 为什么必须有（2026-09-20 审计发现）：monitor() 在 busy 时**整体跳过**，
// 而 setBusy(false) 只有 defer 一条路径 —— 一旦某个动作卡在无超时的外部调用里
// （如 taskkill 的 .Run()），busy 会永久为真 → **整套 1 秒巡检静默停摆**，页面还看不出来。
// 5 分钟远超任何合法动作（内部等待都是有界的：等锁 ≤30s、等健康 ≤20s）。
const busyStuckAfter = 5 * time.Minute

func (st *serveState) isBusy() bool {
	st.mu.Lock()
	if st.busy && !st.busySince.IsZero() && time.Since(st.busySince) > busyStuckAfter {
		st.busy = false
		st.busySince = time.Time{}
		st.mu.Unlock()
		logf(st.logDir, "看护：busy 已持续超过 %v 仍未放开 → 判定动作卡死，自动复位（巡检恢复）", busyStuckAfter)
		return false
	}
	defer st.mu.Unlock()
	return st.busy
}

// busyFor 给接口/页面看"静默多久了"
func (st *serveState) busyFor() float64 {
	st.mu.Lock()
	defer st.mu.Unlock()
	if !st.busy || st.busySince.IsZero() {
		return 0
	}
	return time.Since(st.busySince).Seconds()
}

func (st *serveState) setBusy(v bool) {
	st.mu.Lock()
	if v && !st.busy {
		st.busySince = time.Now()
	} else if !v {
		st.busySince = time.Time{}
	}
	st.busy = v
	st.mu.Unlock()
}

// coreBase 内核基地址（从健康探针 URL 推）
func (st *serveState) coreBase() string { return strings.TrimSuffix(st.url, "/healthz") }

// buildMux 主进程的全部路由（唯一地址那条监听、与"只发界面"的本机监听**共用同一套**）。
// 2026-10-10 抽出来：这样"界面从哪来"和"接口在哪"不再绑死。
func (st *serveState) buildMux() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"ok": true, "pid": os.Getpid(), "role": "core-host",
			"version": hostVersion, "uptime_s": int64(time.Since(st.startAt).Seconds()), "core_dir": st.coreDir})
	})
	mux.HandleFunc("/api/host/status", st.handleStatus)
	mux.HandleFunc("/api/host/logs", st.handleLogs)
	mux.HandleFunc("/api/host/tree", st.handleTree)
	mux.HandleFunc("/api/host/check", st.handleCheck)
	mux.HandleFunc("/api/host/patrol", st.handlePatrol)
	mux.HandleFunc("/api/notes/start", st.handleNotesAction("start"))
	mux.HandleFunc("/api/notes/stop", st.handleNotesAction("stop"))
	mux.HandleFunc("/api/notes/restart", st.handleNotesAction("restart"))
	mux.HandleFunc("/api/core/start", st.handleAction("start"))
	mux.HandleFunc("/api/core/stop", st.handleAction("stop"))
	mux.HandleFunc("/api/core/restart", st.handleAction("restart"))
	mux.HandleFunc("/console", st.handleConsole)
	// `/console/xxx` 也归控制台页：窗口版"记住上次那一格"会直接开 /console/settings，
	// 页面自己按路径切页签（见 page.go 的启动分支）。不挂这条，那个路径会被前门代理给内核 → 404，
	// 窗口里就是一片空白（2026-10-12 实测）。
	mux.HandleFunc("/console/", st.handleConsole)
	// 界面主题（浅白·护眼 / 深色）：**全站唯一一份**，存在 data/ui-theme.json。
	// 端口不同 = 不同源，localStorage 各存各的；主程序（8790）是唯一地址，所以真相放这儿，
	// 内核控制台（8795）与库（8848）跨端口来读（本机来源放行，见 isLocalOrigin）。
	mux.HandleFunc("/api/ui/theme", st.handleUITheme)
	// 主题的实时广播：页面连上这条 SSE，别人一改立刻收到（辞玖 2026-10-06「全实时」）
	mux.HandleFunc("/api/ui/theme/stream", st.handleUIThemeStream)
	// 控制台的日志区（主进程自己读文件，内核不在也能看）
	mux.HandleFunc("/api/host/logsources", st.handleLogSources)
	mux.HandleFunc("/api/host/log", st.handleLogTail)
	mux.HandleFunc("/api/host/log/timeline", st.handleLogTimeline)
	// 整体生命：关闭 / 打开枢阁；子项的「自动重连」开关
	mux.HandleFunc("/api/host/power/on", st.handlePower)
	mux.HandleFunc("/api/host/power/off", st.handlePower)
	mux.HandleFunc("/api/core/autoreconnect", st.handleAutoReconnect("core"))
	// 内核界面（原前门）：挂到 /core/
	mux.HandleFunc("/core", st.handleCoreMount)
	mux.HandleFunc("/core/", st.handleCoreUI)
	// 内核页顶部那条小控制面板的 JS：主进程直接从磁盘发（内核在不在都同一份）
	mux.HandleFunc("/kernelctl.js", st.handleKernelCtlJS)
	// 面板装载器（2026-10-10：库页/内核页不再是 iframe，取回来装配进面板）
	mux.HandleFunc("/page-panels.js", st.handlePanelJS)
	// 设置这一页（2026-10-11）：页面自己能做的（主题、看状态）两边都能用；
	// 只有窗口才有的（缩放/全屏/开发者工具/打开目录）问壳要桥，没桥就隐身。
	mux.HandleFunc("/settings", st.handleSettingsMount)
	mux.HandleFunc("/settings/", st.handleSettings)
	// 图标（内联 SVG）：省得每次开页面都留一条 /favicon.ico 404
	mux.HandleFunc("/favicon.ico", st.handleFavicon)
	// 壳写的小状态文件（桥通不通 / 缩放 / 壳版本 / 界面地址）：给设置页的「关于」读
	mux.HandleFunc("/shell-state.json", st.handleShellState)
	// 「这台机器上 / 关于枢阁」那几行事实（全从当下真实位置推，换台机器/换目录自动变）
	mux.HandleFunc("/api/host/about", st.handleAbout)
	// 笔记视图：见 notesproxy.go
	mux.HandleFunc("/notes", st.handleNotesMount)
	// 主题是**主进程**的状态，不是库的：窗口版里页面挂在主进程那条本机监听上，
	// 它按同源去问 /notes/api/ui/theme —— 别让反代把它当库的接口转下去（会 404）。
	// 见 cmd/core-host/themehub.go 与 notes 页的 shuge-theme 引导。
	mux.HandleFunc("/core/api/ui/theme", st.handleUITheme)
	mux.HandleFunc("/core/api/ui/theme/stream", st.handleUIThemeStream)
	mux.HandleFunc("/notes/api/ui/theme", st.handleUITheme)
	mux.HandleFunc("/notes/api/ui/theme/stream", st.handleUIThemeStream)
	mux.HandleFunc("/notes/", st.handleNotesReverse)
	mux.HandleFunc("/", st.handleFront)

	return mux
}

// hostVersion 主进程版本（写在一处，界面与说明书都引用它）。
const hostVersion = "0.2.0"

// hostLockNameOf 本实例的单实例锁名。
//
// 默认 `core-host`（全机单实例，符合"用户机器上只应该有一个枢阁"）；
// 但同一台机器想并存**第二个安装**（开发/测试/隔离实例）时，用 `COREHOST_LOCK_NAME` 换一个即可。
// 2026-10-10 实测：不换的话临时实例一启动就"锁被占"退出，连端口文件都写不出来 ——
// 也就没法在不动现役实例的前提下做"库起不来"这类故障注入测试。
func hostLockNameOf() string {
	if v := strings.TrimSpace(os.Getenv("COREHOST_LOCK_NAME")); v != "" {
		return v
	}
	return hostLockName
}

func runServe(coreDir, url, launcher string, patrolEvery time.Duration, ns notesSpec) {
	st := &serveState{
		coreDir:     coreDir,
		notes:       ns,
		url:         url,
		launcher:    launcher,
		logDir:      filepath.Join(coreDir, "data", "logs"),
		startAt:     time.Now(),
		patrolEvery: patrolEvery,
		theme:       newThemeHub(),
	}
	hostCoreDir = coreDir // 包级：给包级函数（ensureNotes）写【起不来说明】用

	// 第一件事：抢“启停台”单实例锁。抢不到 = 已经有一个在跑 → 直接退，
	// 绝不去占候选端口（否则会堆出一串隐形启停台）2026-09-27
	if hlock, err := singlelock.Acquire(hostLockNameOf()); err != nil {
		logf(filepath.Join(coreDir, "data", "logs"), "启停台已在运行（host 单实例锁被占），本次退出")
		return
	} else {
		st.hostLock = hlock
	}

	st.proxy = newCoreProxy(st)
	st.loadCache()

	mux := st.buildMux()

	// 并存防护已经在函数开头用单实例锁做了；这里**不能再用 serveAlive**：
	// 它里面的锁判断会把“自己持有的锁”当成“别人在跑” → 刚抢到锁就自杀（2026-09-27 踩过）。

	srv := &http.Server{Handler: cors(mux)}
	ln, port, lerr := listenControl(coreDir)
	if lerr != nil {
		logf(st.logDir, "主进程监听失败：%v（下次计划任务会再拉起）", lerr)
		return
	}
	st.port = port
	writePortFile(coreDir, port)
	go func() {
		if err := srv.Serve(ln); err != nil && err != http.ErrServerClosed {
			logf(st.logDir, "主进程服务退出：%v", err)
		}
	}()
	logf(st.logDir, "枢阁主进程已启动：http://127.0.0.1:%d （常驻、无窗口；主程序页=/console（带「控制台/库/内核」页签），库=/notes/；每 %v 巡检内核）", port, monitorEvery)

	st.startLocalAssets() // 界面装配：另开一个只发界面的本机监听（见 localassets.go）
	st.warmNotesDiag()    // 后台预热依赖探针（状态接口不等它）

	go st.cacheLoop()
	go st.patrolLoop(st.patrolEvery)

	// 巡检：立刻一次，之后每分钟
	st.monitor()
	for range time.Tick(monitorEvery) {
		st.monitor()
	}
}

// monitor 巡检内核：不健康就（在允许的条件下）拉起。逻辑与计划任务共用 checkAndStart。
func (st *serveState) monitor() {
	if st.isBusy() {
		return // 正在执行人工启停动作，别插手
	}
	// 顺序是刻意的：**先库（主体）后内核（附属）**。
	// 内核停着库照样读写检索；库停了笔记就没了 —— 先保谁不用犹豫。
	ensureNotes(st.notes, st.launcher, false)
	checkAndStart(st.coreDir, st.url, st.launcher, false, st.notes) // 顺序：主程序 → 库 → 内核
}

// handleUITheme GET/POST /api/ui/theme —— 界面主题（"light" 浅白·护眼 / "dark" 深色）。
// 「一处切换、所有页面统一」（辞玖 2026-10-06）：各页面把它当唯一真相，localStorage 只作瞬时缓存。
func (st *serveState) handleUITheme(w http.ResponseWriter, r *http.Request) {
	path := filepath.Join(st.coreDir, "data", "ui-theme.json")
	if r.Method == http.MethodGet {
		writeJSON(w, map[string]any{"ok": true, "theme": st.themeNow()})
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "GET/POST only", http.StatusMethodNotAllowed)
		return
	}
	var d struct {
		Theme string `json:"theme"`
	}
	if err := json.NewDecoder(r.Body).Decode(&d); err != nil {
		http.Error(w, "bad json: "+err.Error(), http.StatusBadRequest)
		return
	}
	t := strings.ToLower(strings.TrimSpace(d.Theme))
	if t != "light" && t != "dark" {
		http.Error(w, "theme 只能是 light / dark", http.StatusBadRequest)
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		http.Error(w, "建目录失败: "+err.Error(), http.StatusInternalServerError)
		return
	}
	if err := os.WriteFile(path, []byte(`{"theme":"`+t+`"}`+"\n"), 0o644); err != nil {
		http.Error(w, "写不了: "+err.Error(), http.StatusInternalServerError)
		return
	}
	if st.theme != nil {
		st.theme.broadcast(t) // 推给所有正开着的页面（不用等它们切回来）
	}
	writeJSON(w, map[string]any{"ok": true, "theme": t, "pushed": st.themeClients()})
}

// ---- 前门 ----

func (st *serveState) handleFront(w http.ResponseWriter, r *http.Request) {
	// 辞玖 2026-09-20：**控制台 = 主进程页**，永远在这一页上（内核在不在都一样）。
	// 内核界面挪到 /core/，组件/插件/权限/账本那些归内核自己。
	if r.URL.Path == "/" {
		st.writePage(w)
		return
	}
	// 判据是"内核能不能答话"，**不是"200 才代理"**：
	// 内核降级时（如账本打不开）它照样在服务，只是 /healthz 自报 503；
	// 以前把它当"不在"，前门会对所有 /api/* 回 503 —— 插件注册/领活全断，
	// 一个小毛病升级成"整个平台 API 不可用"（演练 B 挖出）。
	if st.kernelAnswering() {
		st.proxy.ServeHTTP(w, r)
		return
	}
	if strings.HasPrefix(r.URL.Path, "/api/") {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusServiceUnavailable)
		_ = json.NewEncoder(w).Encode(map[string]any{
			"ok": false, "err": "内核（core）未运行", "front": "host",
			"hint": "打开 http://127.0.0.1:" + fmt.Sprint(st.port) + "/ 点「启动内核」，或访问 /console",
		})
		return
	}
	st.writePage(w)
}

// handleNotesAction 库（主体）的启停：与内核那套对称，但没有熔断/隔离那些附属语义
func (st *serveState) handleNotesAction(action string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSON(w, map[string]any{"ok": false, "err": "只支持 POST"})
			return
		}
		st.setBusy(true)
		defer st.setBusy(false)
		switch action {
		case "stop":
			notesHoldFor(30 * time.Second) // 维护窗口：30 秒后自动拉回（库永远在线）
			n := stopNotes(st.notes)
			logf(st.logDir, "维护：停库（杀掉 %d 个进程，30 秒后自动拉回）", n)
			writeJSON(w, map[string]any{"ok": true, "action": "stop", "killed": n,
				"note": "维护窗口 30 秒：到点自动拉回（库永远在线）"})
		case "start":
			notesHoldClear()
			ensureNotesNow(st.notes, st.launcher)
			writeJSON(w, map[string]any{"ok": true, "action": "start", "status": notesStatus(st.notes)})
		case "restart":
			notesHoldFor(3 * time.Second) // 先挡一下退出看护，免得它和这次重启抢着拉
			stopNotes(st.notes)
			time.Sleep(1 * time.Second)
			ensureNotesNow(st.notes, st.launcher)
			notesHoldClear()
			writeJSON(w, map[string]any{"ok": true, "action": "restart", "status": notesStatus(st.notes)})
		default:
			writeJSON(w, map[string]any{"ok": false, "err": "未知动作 " + action})
		}
	}
}

func (st *serveState) handleConsole(w http.ResponseWriter, r *http.Request) { st.writePage(w) }

func (st *serveState) writePage(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write([]byte(hostPageHTML))
}

// ---- 主进程自己的接口（注意：都在 /api/host/ 下，避免和内核的 /api/* 冲突）----

func (st *serveState) handleStatus(w http.ResponseWriter, r *http.Request) {
	// 内核状态走 2 秒采样缓存：页面 3 秒一轮，缓存吃掉重复问询（内核单价约 1.8ms）
	cs, coreAt := coreStatusProbe.get(st.probeCoreStatus)
	st.cacheMu.Lock()
	treeAt := st.treeAt
	hasTree := len(st.tree) > 0
	st.cacheMu.Unlock()

	notesSt := notesStatus(st.notes) // 主体：库（永远在线，没有启停/自动重连这些说法）
	// 库"为什么不在"一屏看清（2026-10-10）：解释器 / 脚本 / 端口被谁占 / 缺哪些包 / 起不来说明
	if diag := st.notesDiag(); len(diag) > 0 {
		notesSt["diag"] = diag
	}
	hostURL := fmt.Sprintf("http://127.0.0.1:%d", st.port)
	body := map[string]any{
		"app":                "枢阁",
		"version":            hostVersion,
		"core_dir":           st.coreDir,
		"role":               "主进程（笔记软件）",
		"notes":              notesSt, // 主体：库
		"core":               cs,
		"core_checked_at":    coreAt.Format(time.RFC3339),
		"core_label":         "内核（core）",
		"host_paused":        hostPaused(st.coreDir),
		"core_paused":        fileExists(filepath.Join(st.coreDir, "data", "core.paused")),
		"core_autoreconnect": !fileExists(filepath.Join(st.coreDir, "data", "core.paused")),
		"urls": map[string]string{
			"console": hostURL + "/console",
			"core":    hostURL + coreMount + "/",
			"notes":   hostURL + notesMount + "/",
		},
		"paused":        fileExists(filepath.Join(st.coreDir, "data", "core.paused")),
		"checked_ttl_s": map[string]int{"notes": int(notesHealthTTL.Seconds()), "core": int(coreStatusTTL.Seconds())},
		"quarantine":    fileExists(filepath.Join(st.coreDir, "data", "core.quarantine")),
		"lock_busy":     lockBusy(),
		"host_state":    strings.TrimSpace(readFileStr(filepath.Join(st.logDir, "host.state"))),
		"owner":         readJSONFile(filepath.Join(st.coreDir, "data", "owner.json")),
		"host_pid":      os.Getpid(),
		"port":          st.port,
		"uptime_s":      int64(time.Since(st.startAt).Seconds()),
		"busy":          st.isBusy(),
		"busy_s":        int(st.busyFor()),
		"tree_at":       treeAt.Format(time.RFC3339),
		"tree_ready":    hasTree,
		"console":       fmt.Sprintf("http://127.0.0.1:%d/console", st.port),
	}
	writeJSON(w, body)
}

func (st *serveState) handleTree(w http.ResponseWriter, r *http.Request) {
	st.cacheMu.Lock()
	defer st.cacheMu.Unlock()
	if len(st.tree) == 0 {
		writeJSON(w, map[string]any{"tree": nil, "at": "", "err": "还没有缓存（内核从未在线过）"})
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write([]byte(`{"at":"` + st.treeAt.Format(time.RFC3339) + `","tree":`))
	_, _ = w.Write(st.tree)
	_, _ = w.Write([]byte(`}`))
}

func (st *serveState) handleAction(action string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		if r.Method != http.MethodPost {
			http.Error(w, "POST only", http.StatusMethodNotAllowed)
			return
		}
		if st.isBusy() {
			writeJSON(w, map[string]any{"ok": false, "err": "已有动作在执行中"})
			return
		}
		st.setBusy(true)
		writeJSON(w, map[string]any{"ok": true, "action": action, "note": "已受理，正在执行"})
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		go func() {
			defer st.setBusy(false)
			switch action {
			case "start":
				logf(st.logDir, "主进程：收到「启动内核」")
				os.Remove(filepath.Join(st.coreDir, "data", "core.quarantine"))
				os.Remove(filepath.Join(st.logDir, "host.starts"))
				checkAndStart(st.coreDir, st.url, "枢阁控制台（页面按钮）", true, st.notes)
			case "stop":
				logf(st.logDir, "主进程：收到「停止内核」")
				st.stopCore()
			case "restart":
				logf(st.logDir, "主进程：收到「重启内核」")
				st.stopCore()
				for i := 0; i < 60 && lockBusy(); i++ { // 等旧实例释放单实例锁，否则会双写账本
					time.Sleep(500 * time.Millisecond)
				}
				os.Remove(filepath.Join(st.coreDir, "data", "core.quarantine"))
				os.Remove(filepath.Join(st.logDir, "host.starts"))
				checkAndStart(st.coreDir, st.url, "枢阁控制台（页面按钮·重启）", true, st.notes)
			}
		}()
	}
}

// stopCore 优雅停机：先请内核自己退（它能刷完账本、收掉插件），叫不动就强杀。
func (st *serveState) stopCore() {
	paused := filepath.Join(st.coreDir, "data", "core.paused")
	_ = os.WriteFile(paused, []byte(time.Now().Format(time.RFC3339)+"\n"), 0o644) // 先置暂停：防止巡检又拉起来
	client := &http.Client{Timeout: 5 * time.Second}
	req, _ := http.NewRequest(http.MethodPost, st.coreBase()+"/api/core/control",
		strings.NewReader(`{"action":"stop"}`))
	req.Header.Set("Content-Type", "application/json")
	graceful := false
	if resp, err := client.Do(req); err == nil {
		resp.Body.Close()
		graceful = true
	}
	for i := 0; i < 24; i++ { // 最多等 12s
		if !lockBusy() {
			logf(st.logDir, "主进程：内核已优雅停止")
			return
		}
		time.Sleep(500 * time.Millisecond)
	}
	if graceful {
		logf(st.logDir, "主进程：内核未在 12s 内退出，改为强杀")
	}
	killCore()
	for i := 0; i < 20 && lockBusy(); i++ {
		time.Sleep(500 * time.Millisecond)
	}
}

func (st *serveState) handleLogs(w http.ResponseWriter, r *http.Request) {
	n := 40
	if v := r.URL.Query().Get("tail"); v != "" {
		fmt.Sscanf(v, "%d", &n)
	}
	if n < 1 || n > 500 {
		n = 40
	}
	writeJSON(w, map[string]any{
		"host": firstNonEmpty(filepath.Join(st.logDir, "host.log"), filepath.Join(st.logDir, "watchdog.log")),
		"core": tailLines(filepath.Join(st.logDir, "core.log"), n),
	})
}

// firstNonEmpty 新文件优先，兼容旧文件名（改名前的 watchdog.log）
func firstNonEmpty(primary, legacy string) []string {
	if out := tailLines(primary, 60); len(out) > 0 {
		return out
	}
	return tailLines(legacy, 60)
}

// ---- 缓存：内核在线时定期抓一份笔记树，内核不在时外壳页还能列出来（只读）----

func (st *serveState) cachePath() string {
	return filepath.Join(st.coreDir, "data", "host-cache.json")
}

func (st *serveState) loadCache() {
	b, err := os.ReadFile(st.cachePath())
	if err != nil {
		return
	}
	var wrap struct {
		At   time.Time       `json:"at"`
		Tree json.RawMessage `json:"tree"`
	}
	if json.Unmarshal(b, &wrap) != nil || len(wrap.Tree) == 0 {
		return
	}
	st.tree, st.treeAt = wrap.Tree, wrap.At
}

func (st *serveState) cacheLoop() {
	for {
		if healthy(st.url) {
			client := &http.Client{Timeout: 10 * time.Second}
			if resp, err := client.Get(st.coreBase() + "/api/vault/list"); err == nil {
				b, _ := readAllLimited(resp.Body, 8<<20)
				resp.Body.Close()
				var probe any
				if json.Unmarshal(b, &probe) == nil {
					st.cacheMu.Lock()
					st.tree, st.treeAt = json.RawMessage(b), time.Now()
					out, _ := json.Marshal(map[string]any{"at": st.treeAt, "tree": st.tree})
					st.cacheMu.Unlock()
					_ = os.WriteFile(st.cachePath(), out, 0o644)
				}
			}
		}
		time.Sleep(cacheEvery)
	}
}

// ---- 反向代理 ----

func newCoreProxy(st *serveState) *httputil.ReverseProxy {
	u, err := url.Parse(st.coreBase())
	if err != nil {
		u, _ = url.Parse("http://127.0.0.1:8795")
	}
	p := httputil.NewSingleHostReverseProxy(u)
	p.FlushInterval = -1 // SSE（/api/events/stream）立即刷出，不要攒
	p.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		// 内核重启/停机时，正在长轮询（如 /api/bridge/plugins/poll）或已断开的请求会被取消——
		// 这是**正常现象**，不是故障：调用方本来就会重试。记成 ERROR 只会让日志变噪音（2026-09-12 实测）。
		if errors.Is(err, context.Canceled) || errors.Is(err, net.ErrClosed) ||
			strings.Contains(err.Error(), "context canceled") {
			return
		}
		// 内核重启/停机时被取消的请求、以及"内核已经不在了"这种失败，都不记错误：
		// 前者调用方本来就会重试，后者是"内核掉了"的症状（由巡检跃迁记录），
		// 每个在途请求写一行只会把日志淹掉。只有"内核在线却转发失败"才是主进程自己的问题。
		if errors.Is(err, context.Canceled) || errors.Is(err, net.ErrClosed) ||
			strings.Contains(err.Error(), "context canceled") || !healthy(st.url) {
			return
		}
		logf(st.logDir, "反代内核失败（内核在线却转发失败）：%v %v", r.URL.Path, err)
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"ok":false,"err":"内核未运行或中途退出"}`))
	}
	return p
}

// ---- 端口与监听 ----

// listenControl 按候选端口列表依次尝试绑定。
//
// 为什么要列表：Windows 上 Go 的 net.Listen 会对监听套接字强制 SO_EXCLUSIVEADDRUSE，
// 一旦该端口正被别处占用（本机动态端口范围从 1024 起，端口可能被某个**对外连接**当临时源端口，
// 2026-09-12 实测），bind 直接返回 WSAEACCES，且无法用 SO_REUSEADDR 化解（两者互斥）。
// 所以：换端口，并把实际端口写进文件；最后兜底 :0（系统随便给）。
func listenControl(coreDir string) (net.Listener, int, error) {
	for _, port := range servePortCandidates {
		if lt, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port)); err == nil {
			return lt, port, nil
		}
	}
	// 不再退到 :0（随机端口）：启停台必须在**已知端口**上，
	// 随机端口会把 host.port 改掉，探针就再也找不到它（2026-09-27）。
	// 全被占时把"谁占着"写清楚（2026-10-10）：只报"全被占"等于什么都没说，用户只能看着没反应。
	detail := portOccupancyReport()
	_ = os.MkdirAll(filepath.Join(coreDir, "data"), 0o755)
	report := fmt.Sprintf(`主进程起不来：候选端口全被占（共 %d 个候选）

谁占着：
%s
处理：把占用这些端口的程序关掉（多半是上一份没退干净的主进程），再双击启动器。
     或者：netstat -ano | findstr :8790 找到 pid，任务管理器里结束它。
`, len(servePortCandidates), detail)
	_ = os.WriteFile(filepath.Join(coreDir, "data", "host-start-failed.txt"), []byte(report), 0o644)
	return nil, 0, fmt.Errorf("候选端口全被占（%d 个候选），谁占着见 data/host-start-failed.txt：\n%s",
		len(servePortCandidates), detail)
}

// portOccupancyReport 把"候选端口上是谁"查清楚（netstat 一次 + tasklist 一次，内存里查）。
//
// ⚠️ 2026-10-10：第一版**每个端口各起一次** netstat/tasklist —— 30 个端口把启动拖到 25 秒以上，
//
//	用户看到的是"点了没反应"。改成各调一次。
func portOccupancyReport() string {
	owners := listenOwners() // port -> pid
	names := processNames()  // pid  -> 进程名
	var b strings.Builder
	for _, p := range servePortCandidates {
		pid, ok := owners[p]
		if !ok {
			continue
		}
		name := names[pid]
		if name == "" {
			name = "?"
		}
		fmt.Fprintf(&b, "  端口 %d 被占：pid=%d（%s）\n", p, pid, name)
	}
	if b.Len() == 0 {
		return "  （复查时端口又空了：可能刚才只是瞬时占用，再点一次启动器即可）\n"
	}
	return b.String()
}

// listenOwners 一次 netstat 拿"哪些端口被谁监听"（port -> pid）。
func listenOwners() map[int]int {
	out := map[int]int{}
	if runtime.GOOS != "windows" {
		return out
	}
	cmd := exec.Command("netstat", "-ano", "-p", "tcp")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	b, err := cmd.Output()
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		if !strings.Contains(line, "LISTENING") {
			continue
		}
		f := strings.Fields(line)
		if len(f) < 5 {
			continue
		}
		hostport := f[1]
		i := strings.LastIndex(hostport, ":")
		if i < 0 {
			continue
		}
		port, err1 := strconv.Atoi(hostport[i+1:])
		pid, err2 := strconv.Atoi(f[len(f)-1])
		if err1 == nil && err2 == nil {
			out[port] = pid
		}
	}
	return out
}

// processNames 一次 tasklist 拿"pid -> 进程名"。
func processNames() map[int]string {
	out := map[int]string{}
	if runtime.GOOS != "windows" {
		return out
	}
	cmd := exec.Command("tasklist", "/FO", "CSV", "/NH")
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	b, err := cmd.Output()
	if err != nil {
		return out
	}
	for _, line := range strings.Split(string(b), "\n") {
		line = strings.TrimSpace(line)
		if !strings.HasPrefix(line, "\"") {
			continue
		}
		// tasklist /FO CSV /NH 的形态： "名字","pid","会话","序号","内存"
		f := strings.Split(line, "\",\"")
		if len(f) < 3 {
			continue
		}
		name := strings.Trim(f[0], "\"")
		pidStr := strings.Trim(f[1], "\"")
		if pid, err := strconv.Atoi(pidStr); err == nil {
			out[pid] = name
		}
	}
	return out
}

// PortFilePath 主进程实际端口（内核据此告诉前端控制台地址；端口可能不是 8791）
func PortFilePath(coreDir string) string { return filepath.Join(coreDir, "data", "host.port") }

func writePortFile(coreDir string, port int) {
	_ = os.MkdirAll(filepath.Join(coreDir, "data"), 0o755)
	_ = os.WriteFile(PortFilePath(coreDir), []byte(fmt.Sprintf("%d\n", port)), 0o644)
	// 兼容旧文件名（内核侧读的是 host.port，这里顺手留一份 serve.port）
	_ = os.WriteFile(filepath.Join(coreDir, "data", "serve.port"), []byte(fmt.Sprintf("%d\n", port)), 0o644)
}

// ReadPort 读主进程端口；文件不在时回落到默认端口
func ReadPort(coreDir string) int {
	for _, p := range []string{PortFilePath(coreDir), filepath.Join(coreDir, "data", "serve.port")} {
		b, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		var v int
		if _, err := fmt.Sscanf(strings.TrimSpace(string(b)), "%d", &v); err == nil && v > 0 {
			return v
		}
	}
	return servePortDefault
}

// ---- 小工具 ----

// cors 只管「本机自己的页面」：没 Origin（curl / 插件 / 同源）放行；Origin 是本机
// （127.0.0.1 / localhost / [::1]）就回显该来源；**别的来源既不给 CORS 头、写请求还直接 403**。
//
// 为什么不能只把 `ACAO: *` 删掉（2026-09-30 体检）：CORS 只挡「读响应」，不挡「发请求」——
// 一个恶意网页照样能 POST /api/notes/stop 把库停掉（简单跨源 POST 不触发预检）。
func cors(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		origin := r.Header.Get("Origin")
		if origin != "" && !isLocalOrigin(origin) {
			if r.Method == http.MethodOptions {
				w.WriteHeader(http.StatusNoContent) // 跨源预检：不给 ACAO，浏览器自己会拒
				return
			}
			if r.Method != http.MethodGet && r.Method != http.MethodHead {
				w.Header().Set("Content-Type", "application/json; charset=utf-8")
				w.WriteHeader(http.StatusForbidden)
				_, _ = w.Write([]byte(`{"ok":false,"err":"拒绝跨源写请求：来源不是本机"}`))
				return
			}
			next.ServeHTTP(w, r) // 读请求照旧，但不给 ACAO：别的网页读不到内容
			return
		}
		if origin != "" {
			w.Header().Set("Access-Control-Allow-Origin", origin)
			w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
			w.Header().Set("Access-Control-Allow-Headers", "Content-Type")
			w.Header().Set("Vary", "Origin")
		}
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// isLocalOrigin 只认本机来源（端口不限）
func isLocalOrigin(origin string) bool {
	u, err := url.Parse(origin)
	if err != nil {
		return false
	}
	switch strings.ToLower(u.Hostname()) {
	case "127.0.0.1", "localhost", "::1":
		return true
	}
	return false
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	_ = json.NewEncoder(w).Encode(v)
}

// coreBaseURL 内核内部基地址（仅主进程使用；对外只有一个地址 = 前门）
func coreBaseURL() string { return "http://127.0.0.1:8795" }

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }

func readFileStr(p string) string {
	b, err := os.ReadFile(p)
	if err != nil {
		return ""
	}
	return string(b)
}

func readJSONFile(p string) any {
	b, err := os.ReadFile(p)
	if err != nil {
		return nil
	}
	var v any
	if json.Unmarshal(b, &v) != nil {
		return nil
	}
	return v
}

// tailLines 取文件末尾 n 行（只读末尾 256KB，日志再大也不卡）
func tailLines(path string, n int) []string {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil
	}
	const maxRead = 256 << 10
	off := int64(0)
	if st.Size() > maxRead {
		off = st.Size() - maxRead
	}
	buf := make([]byte, st.Size()-off)
	if _, err := f.ReadAt(buf, off); err != nil && len(buf) == 0 {
		return nil
	}
	lines := strings.Split(strings.ReplaceAll(string(buf), "\r\n", "\n"), "\n")
	out := []string{}
	for _, l := range lines {
		if strings.TrimSpace(l) != "" {
			out = append(out, l)
		}
	}
	if len(out) > n {
		out = out[len(out)-n:]
	}
	return out
}

// readAllLimited 读但不超过 max（防止一个异常大的响应把内存吃光）
func readAllLimited(rc io.Reader, max int64) ([]byte, error) {
	return io.ReadAll(io.LimitReader(rc, max))
}

// handlePatrol GET /api/host/patrol —— 常驻巡检快照（控制台横条用）
func (st *serveState) handlePatrol(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, st.patrolSnapshot())
}

// kernelAnswering 内核**能不能答话**（任何 HTTP 响应都算，包括 503 降级自报）。
// 与 healthy() 的分工：healthy 用于"该不该拉起/重启"（200 才算真健康），
// Answering 用于"要不要把请求转给它"（活着转过去，好坏由内核自己说）。
func (st *serveState) kernelAnswering() bool {
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(st.url)
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body)
	return true
}
