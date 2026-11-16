//go:build windows

// 本文件：平台自检（🩺）——把"平台是否完好"变成一次可重复的检查。
//
// 为什么需要：2026-09-12 这一天里，单实例锁、账本并发写、熔断、端口分层、前门代理、owner 登记…
// 每一条都是**手工**验过一遍才知道好坏；改动一多、或者半夜崩一次，就没有统一入口回答
// "现在到底哪里不对"。这里把那些手工步骤固化下来，内核在不在都能跑（内核不在时用文件兜底）。
//
// 判据原则：
//
//	· 只信**证据**（谁在应答、pid 是谁、账本里数出来多少），不做"应该没问题"的推断；
//	· 内核不在 ≠ 平台坏了（人工停止是正常状态）→ 该 skip 就 skip，别报假红灯；
//	· 损坏要看**新增**：历史缺口记基线，只有超过基线才判失败。
package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"core/internal/pluginauth"
)

type checkItem struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Status string `json:"status"` // ok | warn | fail | skip
	Detail string `json:"detail"`
	Hint   string `json:"hint,omitempty"`
}

type checkReport struct {
	At      string      `json:"at"`
	Items   []checkItem `json:"items"`
	OK      int         `json:"ok"`
	Known   int         `json:"known"` // 已知且接受的（如历史账本损坏）——不算异常，不让常驻告警永远亮黄
	Warn    int         `json:"warn"`
	Fail    int         `json:"fail"`
	Skip    int         `json:"skip"`
	Summary string      `json:"summary"`
}

// damageBaseline 已知的历史损坏（不回填，只记基线）；超过基线才算"新增"
type damageBaseline struct {
	Gaps     int    `json:"gaps"`
	Disorder int    `json:"disorder"`
	BadLines int    `json:"bad_lines"`
	Note     string `json:"note,omitempty"`
}

func (st *serveState) handleCheck(w http.ResponseWriter, r *http.Request) {
	rep := st.runChecks()
	st.applyPatrol(rep, "manual") // 手动跑也更新巡检状态（控制台显示的"上次体检"永远是最新那次）
	writeJSON(w, rep)
}

// runChecks 跑一遍全部检查项（手动自检与常驻巡检共用同一份清单，避免两套判据漂移）
func (st *serveState) runChecks() *checkReport {
	rep := &checkReport{At: time.Now().Format(time.RFC3339)}
	add := func(it checkItem) { rep.Items = append(rep.Items, it) }

	kern := st.kernelHealth() // 内核 /healthz 解析结果
	add(st.checkHostPort())
	add(st.checkSingleHost())
	add(st.checkUIAssets())
	add(st.checkPortLayering(kern))
	add(st.checkProxy(kern))
	add(st.checkLock(kern))
	add(st.checkFlags())
	add(st.checkLedger(kern))
	add(st.checkProbe(kern))
	add(st.checkOwner(kern))
	add(st.checkCache(kern))
	add(st.checkKernelHealth(kern))
	add(st.checkLogs())
	add(st.checkPluginChannel(kern))
	for _, it := range st.checkChannels(kern) {
		add(it)
	}
	add(st.checkLogPipeline(kern))
	add(st.checkToolRouting(kern))
	add(st.checkPluginAuth(kern))
	add(st.checkAgentUplinkAuth(kern))

	for _, it := range rep.Items {
		switch it.Status {
		case "ok":
			rep.OK++
		case "known":
			rep.Known++
		case "warn":
			rep.Warn++
		case "fail":
			rep.Fail++
		default:
			rep.Skip++
		}
	}
	rep.Summary = fmt.Sprintf("自检 %d 项：%d 通过 / %d 已知 / %d 警告 / %d 失败 / %d 跳过",
		len(rep.Items), rep.OK, rep.Known, rep.Warn, rep.Fail, rep.Skip)
	return rep
}

// ---- 内核探针结果 ----

type kernelInfo struct {
	Online   bool
	PID      int
	UptimeS  int64
	Degraded bool
	LastSeq  uint64
	Healthy  bool           // /healthz 自报 ok（false = 内核在跑但降级，如账本打不开）
	Reasons  []string       // 自报不健康的原因（ledger not enabled 等）
	Auth     map[string]any // /healthz 的 auth 段（上报面鉴权统计）
	Raw      map[string]any
}

func (st *serveState) kernelHealth() kernelInfo {
	ki := kernelInfo{}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(st.url)
	if err != nil {
		return ki
	}
	defer resp.Body.Close()
	var v struct {
		Auth    map[string]any `json:"auth"`
		OK      bool           `json:"ok"`
		PID     int            `json:"pid"`
		Uptime  int64          `json:"uptime_s"`
		Reasons []string       `json:"reasons"`
		Ledger  struct {
			LastSeq  uint64 `json:"last_seq"`
			Degraded bool   `json:"degraded"`
		} `json:"ledger"`
	}
	// 关键：**能答上话就算在线**。ok=false 是"内核在跑但降级"（如账本打不开、/healthz 503），
	// 不是"没运行"——以前把两者混为一谈，导致降级时账本项走离线路径、锁项误报"有残留进程"（演练 B 挖出）。
	if json.NewDecoder(resp.Body).Decode(&v) != nil {
		return ki
	}
	ki.Online, ki.PID, ki.UptimeS, ki.Degraded, ki.LastSeq = true, v.PID, v.Uptime, v.Ledger.Degraded, v.Ledger.LastSeq
	ki.Healthy, ki.Reasons, ki.Auth = v.OK, v.Reasons, v.Auth
	return ki
}

func (st *serveState) getJSON(path string, out any) error {
	client := &http.Client{Timeout: 8 * time.Second}
	resp, err := client.Get(st.coreBase() + path)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// ---- 各检查项 ----

// ① 主进程与唯一地址：8790 必须由**本进程**应答（否则说明有别的程序占着这个地址）
func (st *serveState) checkHostPort() checkItem {
	it := checkItem{ID: "front_owner", Name: "唯一地址 8790 归属"}
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/api/host/status", st.port))
	if err != nil {
		it.Status = "fail"
		it.Detail = fmt.Sprintf("8790 无应答：%v", err)
		it.Hint = "主进程可能正在退出；看 data\\logs\\host.log"
		return it
	}
	defer resp.Body.Close()
	var v struct {
		HostPID int `json:"host_pid"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&v)
	switch {
	case v.HostPID == os.Getpid():
		it.Status = "ok"
		it.Detail = fmt.Sprintf("8790 → 枢阁主进程（pid %d，已运行 %s）", os.Getpid(), shortDur(time.Since(st.startAt)))
	case v.HostPID == 0:
		it.Status = "fail"
		it.Detail = "8790 上应答的不是主进程（像是别的程序/旧内核占着）"
		it.Hint = "查 Get-NetTCPConnection -LocalPort 8790 的持有进程"
	default:
		it.Status = "warn"
		it.Detail = fmt.Sprintf("8790 由另一个主进程实例应答（pid %d，本进程 %d）", v.HostPID, os.Getpid())
		it.Hint = "同时跑了两个主进程；正常只会有一个"
	}
	return it
}

// ② 主进程单实例：候选端口上应答的"主进程"必须只有本进程一个
// （两个主进程 = 两个前门 + 两个巡检者，和"两个内核"是同一类事故，只是后果轻一些）
func (st *serveState) checkSingleHost() checkItem {
	it := checkItem{ID: "single_host", Name: "主进程单实例"}
	client := &http.Client{Timeout: 1200 * time.Millisecond}
	found := map[int]int{} // pid -> port
	for _, port := range servePortCandidates {
		resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/api/host/status", port))
		if err != nil {
			continue
		}
		var v struct {
			HostPID int `json:"host_pid"`
		}
		_ = json.NewDecoder(resp.Body).Decode(&v)
		resp.Body.Close()
		if v.HostPID > 0 {
			found[v.HostPID] = port
		}
	}
	if len(found) <= 1 {
		it.Status = "ok"
		it.Detail = fmt.Sprintf("只有本进程在提供主进程服务（pid %d，端口 %d）", os.Getpid(), st.port)
		return it
	}
	ports := []string{}
	for pid, port := range found {
		ports = append(ports, fmt.Sprintf("pid %d@%d", pid, port))
	}
	it.Status = "warn"
	it.Detail = "同时有多个主进程在跑：" + strings.Join(ports, "、")
	it.Hint = "多主进程会让前门地址漂移（看门狗/计划任务只会保活其中一个）；留下一个、杀掉其余"
	return it
}

// ③ 端口分层：8790=主进程、8795=内核；两边都不是对方
// checkUIAssets 界面装配（2026-10-10）：本机那条"只发界面"的监听在不在。
//
// 管的是"界面从哪来"这件事 —— 界面是硬盘上的文件，本该打开就有，
// 不该依赖唯一地址那条网关活着（网关没起 / 端口被占 / 被别的程序劫持都不该影响它）。
func (st *serveState) checkUIAssets() checkItem {
	it := checkItem{ID: "ui_assets", Name: "界面装配（本机监听）"}
	if p := localAssetsAlive(st.coreDir); p > 0 {
		it.Status = "ok"
		it.Detail = fmt.Sprintf("界面可从 http://127.0.0.1:%d/console 直接装配（不经过唯一地址）", p)
	} else {
		it.Status = "warn"
		it.Detail = "本机界面监听不在（data/ui-assets.port 缺失或已失效）"
		it.Hint = "重启主进程；若反复出现，看 data/logs/host.log 里“界面监听”那几行"
	}
	return it
}

func (st *serveState) checkPortLayering(ki kernelInfo) checkItem {
	it := checkItem{ID: "layering", Name: "端口分层（8790 软件 / 8795 内核）"}
	lo := strings.TrimSuffix(st.coreBase(), "/")
	client := &http.Client{Timeout: 3 * time.Second}
	resp, err := client.Get(lo + "/healthz")
	if err != nil {
		if ki.Online {
			it.Status = "fail"
			it.Detail = fmt.Sprintf("内核健康探针说在线，但直连 %s 不通：%v", lo, err)
		} else {
			it.Status = "skip"
			it.Detail = "内核未运行，内部端口自然不通（正常）"
		}
		return it
	}
	defer resp.Body.Close()
	var v struct {
		PID  int    `json:"pid"`
		Role string `json:"role"`
	}
	_ = json.NewDecoder(resp.Body).Decode(&v)
	if v.Role != "" { // 8795 上跑着主进程 = 端口被反着占了
		it.Status = "fail"
		it.Detail = fmt.Sprintf("%s 上应答的是主进程（role=%s）—— 端口分层反了", lo, v.Role)
		it.Hint = "内核应监听 8795、主进程监听 8790；检查 CORE_PORT / 启动参数"
		return it
	}
	if ki.Online && v.PID != ki.PID {
		it.Status = "warn"
		it.Detail = fmt.Sprintf("8795 的 pid=%d 与 /healthz 报的 pid=%d 不一致", v.PID, ki.PID)
		return it
	}
	it.Status = "ok"
	it.Detail = fmt.Sprintf("8795 → 内核（pid %d）；8790 → 主进程", v.PID)
	return it
}

// ③ 前门代理：经 8790 能不能拿到内核的接口（外部调用方全靠这条）
func (st *serveState) checkProxy(ki kernelInfo) checkItem {
	it := checkItem{ID: "proxy", Name: "前门反向代理"}
	if !ki.Online {
		it.Status = "skip"
		it.Detail = "内核未运行 → 前门出的是外壳页（这是设计行为，不算故障）"
		return it
	}
	var v struct {
		Plugins int `json:"plugins"`
		Subs    int `json:"subs"`
	}
	client := &http.Client{Timeout: 5 * time.Second}
	url := fmt.Sprintf("http://127.0.0.1:%d/api/status", st.port)
	resp, err := client.Get(url)
	if err != nil {
		it.Status = "fail"
		it.Detail = fmt.Sprintf("经前门取内核接口失败：%v", err)
		it.Hint = "看 host.log 里有没有「反代内核失败」"
		return it
	}
	defer resp.Body.Close()
	if json.NewDecoder(resp.Body).Decode(&v) != nil {
		it.Status = "fail"
		it.Detail = "经前门返回的不是内核接口的 JSON"
		return it
	}
	it.Status = "ok"
	it.Detail = fmt.Sprintf("8790/api/status 正常代理到内核（插件 %d / 订阅者 %d）", v.Plugins, v.Subs)
	return it
}

// ④ 单实例锁：内核在线就该被持有；内核不在就不该被持有（被持有 = 有残留进程）
func (st *serveState) checkLock(ki kernelInfo) checkItem {
	it := checkItem{ID: "lock", Name: "单实例锁（防双写账本）"}
	busy := lockBusy()
	switch {
	case ki.Online && busy:
		it.Status = "ok"
		it.Detail = "内核持有 Local\\core（符合预期）"
	case !ki.Online && !busy:
		it.Status = "ok"
		it.Detail = "内核未运行，锁空闲（再启动不会撞车）"
	case ki.Online && !busy:
		it.Status = "warn"
		it.Detail = "内核在线却没持有单实例锁（锁可能失效）"
		it.Hint = "这种状态下起第二个内核会双写账本，先查单实例锁实现"
	default:
		it.Status = "warn"
		it.Detail = "内核未运行但锁仍被持有：有残留进程没退干净"
		it.Hint = "查 tasklist | findstr core.exe，必要时强杀后再启动"
	}
	return it
}

// ⑤ 人工停止 / 熔断标记
func (st *serveState) checkFlags() checkItem {
	it := checkItem{ID: "flags", Name: "暂停 / 熔断标记"}
	paused := fileExists(filepath.Join(st.coreDir, "data", "core.paused"))
	quar := fileExists(filepath.Join(st.coreDir, "data", "core.quarantine"))
	switch {
	case quar:
		it.Status = "fail"
		it.Detail = "已被熔断隔离（自动拉起已停手）"
		it.Hint = "看内核日志定位崩溃原因 → 点「启动内核」恢复"
	case paused:
		it.Status = "warn"
		it.Detail = "已置暂停标志：巡检不会自动拉起（人工停止的正常状态）"
		it.Hint = "点「启动内核」即可重新拉起"
	default:
		it.Status = "ok"
		it.Detail = "无暂停、无熔断"
	}
	return it
}

// ⑥ 账本：缺口/重复以**新增**为准（历史损坏记基线）
func (st *serveState) checkLedger(ki kernelInfo) checkItem {
	it := checkItem{ID: "ledger", Name: "账本（唯一真相源）"}
	base := st.loadBaseline()
	if !ki.Online {
		files, lastSeq, lines := scanLedgerOffline(filepath.Join(st.coreDir, "data", "ledger"))
		it.Status = "warn"
		it.Detail = fmt.Sprintf("内核未运行，只能离线抽查：账本文件 %d 个、最新 seq %d、总行数 %d（无法校验连续性）", files, lastSeq, lines)
		it.Hint = "启动内核后自检会自动做完整校验（seq 连续性 + 重复写检测）"
		return it
	}
	var rep struct {
		Lines    int    `json:"lines"`
		FirstSeq uint64 `json:"first_seq"`
		LastSeq  uint64 `json:"last_seq"`
		Gaps     []struct {
			AfterSeq  uint64 `json:"after_seq"`
			BeforeSeq uint64 `json:"before_seq"`
			Missing   uint64 `json:"missing"`
		} `json:"gaps"`
		Disorder []struct {
			File string `json:"file"`
			Line int    `json:"line"`
			Seq  uint64 `json:"seq"`
		} `json:"disorder"`
		Summary string `json:"summary"`
	}
	if err := st.getJSON("/api/ledger/verify", &rep); err != nil {
		it.Status = "fail"
		it.Detail = fmt.Sprintf("账本不可用（事件不会入账）：%v", err)
		it.Hint = "看内核日志「账本打开失败」与 data\\ledger 目录；修好后点「重启内核」"
		return it
	}
	ng, nd := len(rep.Gaps), len(rep.Disorder)
	switch {
	case ng > base.Gaps || nd > base.Disorder:
		it.Status = "fail"
		it.Detail = fmt.Sprintf("**新增损坏**：缺口 %d 处（基线 %d）、重复/回退 %d 处（基线 %d）",
			ng, base.Gaps, nd, base.Disorder)
		it.Hint = "新增损坏通常意味着两个写者同时写账本 —— 查 host.log/core.log 的启动记录与单实例锁"
	case ng == 0 && nd == 0:
		it.Status = "ok"
		it.Detail = fmt.Sprintf("seq %d→%d 连续、无重复写（共 %d 条）", rep.FirstSeq, rep.LastSeq, rep.Lines)
	default:
		// 历史损坏**未变**：这是"已知且接受"的状态（辞玖决策：不回填），
		// 所以判 known 而不是 warn —— 否则常驻巡检会永远亮黄，"恢复"永远测不出来。
		it.Status = "known"
		it.Detail = fmt.Sprintf("历史损坏未变（已记基线，不算异常）：缺口 %d 处、重复 %d 处（seq %d→%d，共 %d 条）",
			ng, nd, rep.FirstSeq, rep.LastSeq, rep.Lines)
		it.Hint = "历史损坏故意不回填；基线在 data/ledger/damage-baseline.json，新增损坏会立刻判失败"
	}
	return it
}

// ⑦ 探针（内核自带的心跳/探活）
func (st *serveState) checkProbe(ki kernelInfo) checkItem {
	it := checkItem{ID: "probe", Name: "插件探针"}
	if !ki.Online {
		it.Status = "skip"
		it.Detail = "内核未运行，没有探针"
		return it
	}
	var v struct {
		Summary struct {
			Targets int            `json:"targets"`
			Enabled bool           `json:"enabled"`
			ByState map[string]int `json:"by_state"`
		} `json:"summary"`
		Plugins []struct {
			Plugin      string `json:"plugin"`
			State       string `json:"state"`
			ConsecFail  int    `json:"consec_fail"`
			Restarts    int    `json:"restarts"`
			LastProbeAt string `json:"last_probe_at"`
		} `json:"plugins"`
	}
	if err := st.getJSON("/api/plugins/probe", &v); err != nil {
		it.Status = "fail"
		it.Detail = fmt.Sprintf("探针接口不可用：%v", err)
		return it
	}
	bad := []string{}
	for _, p := range v.Plugins {
		if p.State != "healthy" {
			bad = append(bad, fmt.Sprintf("%s=%s(连续失败%d)", p.Plugin, p.State, p.ConsecFail))
		}
	}
	if len(bad) > 0 {
		it.Status = "warn"
		it.Detail = "非健康插件：" + strings.Join(bad, "、")
		it.Hint = "探针失败达到阈值会自动重启插件；持续失败看内核日志"
		return it
	}
	it.Status = "ok"
	it.Detail = fmt.Sprintf("探针启用=%v，目标 %d 个，全部 healthy", v.Summary.Enabled, v.Summary.Targets)
	return it
}

// ⑧ owner 登记：账本/文件里记的 pid 必须就是现在跑着的内核
func (st *serveState) checkOwner(ki kernelInfo) checkItem {
	it := checkItem{ID: "owner", Name: "owner 登记（谁拉起的）"}
	raw := readJSONFile(filepath.Join(st.coreDir, "data", "owner.json"))
	m, _ := raw.(map[string]any)
	if m == nil {
		it.Status = "warn"
		it.Detail = "data\\owner.json 不存在"
		return it
	}
	pid := 0
	if f, ok := m["pid"].(float64); ok {
		pid = int(f)
	}
	by, _ := m["launched_by"].(string)
	if !ki.Online {
		it.Status = "skip"
		it.Detail = fmt.Sprintf("内核未运行；登记的是 pid %d（启动者：%s）", pid, by)
		return it
	}
	if pid != ki.PID {
		it.Status = "warn"
		it.Detail = fmt.Sprintf("登记 pid=%d，实际运行 pid=%d（登记文件过期）", pid, ki.PID)
		return it
	}
	it.Status = "ok"
	it.Detail = fmt.Sprintf("pid %d，启动者：%s", pid, by)
	return it
}

// ⑨ 缓存新鲜度（外壳页靠它列笔记）
func (st *serveState) checkCache(ki kernelInfo) checkItem {
	it := checkItem{ID: "cache", Name: "笔记缓存（外壳页用）"}
	st.cacheMu.Lock()
	at := st.treeAt
	ready := len(st.tree) > 0
	st.cacheMu.Unlock()
	if !ready {
		if ki.Online {
			it.Status = "warn"
			it.Detail = "内核在线但还没有缓存（刚启动或抓取失败）"
		} else {
			it.Status = "skip"
			it.Detail = "还没有缓存（内核从未在本机在线过）"
		}
		return it
	}
	age := time.Since(at)
	if ki.Online && age > 5*time.Minute {
		it.Status = "warn"
		it.Detail = fmt.Sprintf("缓存已 %s 未更新（内核在线，应该每 60s 更新一次）", shortDur(age))
		it.Hint = "看 host.log；缓存只影响外壳页的只读列表"
		return it
	}
	it.Status = "ok"
	it.Detail = fmt.Sprintf("缓存于 %s（%s 前）", at.Format("15:04:05"), shortDur(age))
	return it
}

// ⑩ 最近 5 分钟有没有 ERROR 级日志
func (st *serveState) checkLogs() checkItem {
	it := checkItem{ID: "logs", Name: "最近日志错误（5 分钟内）"}
	re := regexp.MustCompile(`^time=(\S+)\s+level=ERROR\s+msg=(.*)$`)
	cut := time.Now().Add(-5 * time.Minute)
	newest, count, where := "", 0, ""
	for _, f := range []struct{ path, label string }{
		{filepath.Join(st.logDir, "core.log"), "内核"},
		{filepath.Join(st.logDir, "host.log"), "主进程"},
	} {
		for _, line := range tailLines(f.path, 300) {
			m := re.FindStringSubmatch(strings.TrimSpace(line))
			if m == nil {
				continue
			}
			ts, err := time.Parse(time.RFC3339Nano, m[1])
			if err != nil || ts.Before(cut) {
				continue
			}
			count++
			newest = strings.TrimSpace(m[2])
			where = f.label
		}
	}
	if count == 0 {
		it.Status = "ok"
		it.Detail = "近 5 分钟无 ERROR 日志"
		return it
	}
	it.Status = "warn"
	it.Detail = fmt.Sprintf("%d 条 ERROR（最近：%s %s）", count, where, truncate(newest, 160))
	it.Hint = "日志页选「内核」/「主进程」看上下文"
	return it
}

// ---- 基线 ----

func (st *serveState) baselinePath() string {
	return filepath.Join(st.coreDir, "data", "ledger", "damage-baseline.json")
}

// loadBaseline 读已知损坏基线；没有就按"现在这一刻"写一份（显式接受既有损坏，而不是假装没坏）
func (st *serveState) loadBaseline() damageBaseline {
	b := damageBaseline{Note: "历史损坏（2026-09-12 并发写事故），故意不回填"}
	p := st.baselinePath()
	if raw, err := os.ReadFile(p); err == nil {
		var v damageBaseline
		if json.Unmarshal(raw, &v) == nil {
			return v
		}
	}
	// 首次：用当前校验结果当基线
	var rep struct {
		Gaps     []any `json:"gaps"`
		Disorder []any `json:"disorder"`
		BadLines []any `json:"bad_lines"`
	}
	if err := st.getJSON("/api/ledger/verify", &rep); err == nil {
		b.Gaps, b.Disorder, b.BadLines = len(rep.Gaps), len(rep.Disorder), len(rep.BadLines)
		if out, err := json.MarshalIndent(&b, "", "  "); err == nil {
			_ = os.MkdirAll(filepath.Dir(p), 0o755)
			_ = os.WriteFile(p, append(out, '\n'), 0o644)
		}
	}
	return b
}

// scanLedgerOffline 内核不在时对账本做最小统计（文件数 / 最新 seq / 行数）
func scanLedgerOffline(dir string) (files int, lastSeq uint64, lines int) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, 0, 0
	}
	names := []string{}
	for _, e := range entries {
		if !e.IsDir() && strings.HasPrefix(e.Name(), "ledger-") && strings.HasSuffix(e.Name(), ".jsonl") {
			names = append(names, e.Name())
		}
	}
	files = len(names)
	for _, n := range names {
		path := filepath.Join(dir, n)
		lines += countLines(path) // 真数（流式，不整文件读进内存）
		for _, line := range tailLines(path, 200) {
			var v struct {
				Seq uint64 `json:"seq"`
			}
			if json.Unmarshal([]byte(line), &v) == nil && v.Seq > lastSeq {
				lastSeq = v.Seq
			}
		}
	}
	return files, lastSeq, lines
}

// countLines 流式数行（256KB 一块，64MB 账本也只要几十毫秒）
func countLines(path string) int {
	f, err := os.Open(path)
	if err != nil {
		return 0
	}
	defer f.Close()
	buf := make([]byte, 256<<10)
	n := 0
	for {
		r, err := f.Read(buf)
		for _, b := range buf[:r] {
			if b == '\n' {
				n++
			}
		}
		if err != nil {
			break
		}
	}
	return n
}

// ---- 小工具 ----

func shortDur(d time.Duration) string {
	s := int(d.Seconds())
	if s < 0 {
		s = 0
	}
	switch {
	case s < 60:
		return strconv.Itoa(s) + " 秒"
	case s < 3600:
		return fmt.Sprintf("%d 分 %d 秒", s/60, s%60)
	default:
		return fmt.Sprintf("%d 小时 %d 分", s/3600, (s%3600)/60)
	}
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n]) + "…"
}

// ⑪ 插件调用通道：平台 → 插件的定向投递面（DSH 这类通道型插件全靠它）
//
// 判据：逐个 external 插件调 plugin.ping（探针应答器本来就会答这个工具），谁答不上来谁就有问题。
// 为什么不注入"测试事件"来验证：那会往 DSH 的 inbox 里塞假事件、污染真实会话——检查必须无副作用。
func (st *serveState) checkPluginChannel(ki kernelInfo) checkItem {
	it := checkItem{ID: "plugin_channel", Name: "插件调用通道（定向投递）"}
	if !ki.Online {
		it.Status = "skip"
		it.Detail = "内核未运行，插件也不在"
		return it
	}
	plugins, err := st.externalPlugins()
	if err != nil {
		it.Status = "fail"
		it.Detail = "取组件列表失败：" + err.Error()
		return it
	}
	if len(plugins) == 0 {
		it.Status = "skip"
		it.Detail = "没有 external 插件在跑"
		return it
	}
	dead, alive := []string{}, []string{}
	for _, id := range plugins {
		var res struct {
			OK    bool   `json:"ok"`
			Error string `json:"error"`
		}
		// 注意：平台在这条通道上即使"插件没回执"也返回 HTTP 200 + {"ok":false,"error":"timeout…"}，
		// 所以判据必须是响应体里的 ok，而不是 HTTP 状态码——曾经因此把死插件判成"应答正常"。
		err := st.postJSONTo("/api/bridge/plugins/call",
			map[string]any{"plugin": id, "tool": "plugin.ping", "timeoutMs": 3000}, 8*time.Second, &res)
		if err != nil || !res.OK {
			dead = append(dead, id)
		} else {
			alive = append(alive, id)
		}
	}
	switch {
	case len(dead) == 0:
		it.Status = "ok"
		it.Detail = fmt.Sprintf("%d 个插件全部应答定向调用（%s）", len(alive), strings.Join(alive, "、"))
	case len(alive) > 0:
		it.Status = "warn"
		it.Detail = fmt.Sprintf("无应答：%s；正常：%s", strings.Join(dead, "、"), strings.Join(alive, "、"))
		it.Hint = "无应答通常是该插件的 poll 循环卡住或进程不在；看它的日志页"
	default:
		it.Status = "fail"
		it.Detail = "所有插件都无应答：" + strings.Join(dead, "、")
		it.Hint = "调用通道或插件进程整体异常；先看内核日志与插件日志"
	}
	return it
}

// ⑫ 通道不变式：每个"通道型 agent"（如 agent.dsh / agent.reasonix）都必须由对应插件进程背书
//
// 「内核内能力外移」后的关键不变式——身份与背书进程必须成对出现，且**答得上话**：
//
//	· 有 agent 没插件 = 身份是空的，投递必然失败
//	· 有插件没 agent = 注册没建起 agent 面，Inject 找不到目标
//	· 都在册但插件不回执 = 身份还在、进程已死（假绿灯，见错误库 #54）
//
// 无副作用：只调插件自己的 status 工具读计数，不往 inbox 塞东西（那会污染真实接入方）。
func (st *serveState) checkChannels(ki kernelInfo) []checkItem {
	type chanPair struct{ agent, plugin, statusTool, label string }
	pairs := []chanPair{
		{"agent.dsh", "plugin.dsh", "dsh.status", "DSH"},
		{"agent.reasonix", "plugin.reasonix", "reasonix.status", "Reasonix"},
	}
	out := []checkItem{}
	if !ki.Online {
		return []checkItem{{ID: "channels", Name: "接入通道", Status: "skip", Detail: "内核未运行"}}
	}
	plugins, err := st.externalPlugins()
	if err != nil {
		return []checkItem{{ID: "channels", Name: "接入通道", Status: "fail", Detail: "取组件列表失败：" + err.Error()}}
	}
	isPlugin := map[string]bool{}
	for _, id := range plugins {
		isPlugin[id] = true
	}
	for _, c := range pairs {
		it := checkItem{ID: "chan_" + c.plugin, Name: c.label + " 通道（" + c.agent + " ⇄ " + c.plugin + "）"}
		// 身份现在是插件的一个字段（2026-09-12 辞玖定"身份申明加通道放一起"），
		// 所以判据 = 插件在册 **且** 它声明的身份正是这个 agent。
		declared, hasPlugin := st.agentDeclaredBy(c.plugin, c.agent), isPlugin[c.plugin]
		if !hasPlugin && !declared {
			it.Status = "skip"
			it.Detail = "本机未配置该通道"
			out = append(out, it)
			continue
		}
		if !hasPlugin {
			it.Status = "fail"
			it.Detail = c.agent + " 由 " + c.plugin + " 声明，但该插件不在册 → 平台→" + c.label + " 的推送必然失败"
			it.Hint = "查 config.yaml 里 " + c.plugin + " 是否 enabled；或看内核日志「plugin registered id=" + c.plugin + "」"
			out = append(out, it)
			continue
		}
		if !declared {
			it.Status = "fail"
			it.Detail = c.plugin + " 在册，但它没有声明身份 " + c.agent + "（注册里 agentId 丢了？）"
			it.Hint = "看插件注册日志；身份声明与通道是同一条注册"
			out = append(out, it)
			continue
		}
		var res struct {
			OK   bool `json:"ok"`
			Data struct {
				Agent      string `json:"agent"`
				Delivered  int    `json:"delivered"`
				InboxFiles int    `json:"inbox_files"`
			} `json:"data"`
		}
		err := st.postJSONTo("/api/bridge/plugins/call",
			map[string]any{"plugin": c.plugin, "tool": c.statusTool, "timeoutMs": 3000},
			8*time.Second, &res)
		if err == nil && !res.OK {
			err = fmt.Errorf("插件未回执（身份在册但进程可能已退出）")
		}
		if err != nil {
			it.Status = "fail"
			it.Detail = "身份成对，但插件不答 " + c.statusTool + "：" + err.Error()
			it.Hint = "看 data\\logs\\" + c.plugin + ".log；组件列表里它应是 running"
			out = append(out, it)
			continue
		}
		it.Status = "ok"
		it.Detail = fmt.Sprintf("%s 由 %s 背书；已投递 %d 次，inbox 现有 %d 个文件",
			c.agent, c.plugin, res.Data.Delivered, res.Data.InboxFiles)
		out = append(out, it)
	}
	return out
}

// externalPlugins 当前在跑的 external 插件 id 列表
func (st *serveState) externalPlugins() ([]string, error) {
	var comps []struct {
		Kind string `json:"kind"`
		ID   string `json:"id"`
		Type string `json:"type"`
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/api/components", st.port))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if err := json.NewDecoder(resp.Body).Decode(&comps); err != nil {
		return nil, err
	}
	out := []string{}
	for _, c := range comps {
		if c.Kind == "plugin" && c.Type == "external" {
			out = append(out, c.ID)
		}
	}
	return out, nil
}

// hasComponent 组件列表里是否有该 id
func (st *serveState) hasComponent(id string) bool {
	var comps []struct {
		ID string `json:"id"`
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/api/components", st.port))
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if json.NewDecoder(resp.Body).Decode(&comps) != nil {
		return false
	}
	for _, c := range comps {
		if c.ID == id {
			return true
		}
	}
	return false
}

// postJSON 向本机前门 POST 一个 JSON 并丢弃响应体
func (st *serveState) postJSON(path string, body map[string]any, timeout time.Duration) ([]byte, error) {
	var discard any
	return nil, st.postJSONTo(path, body, timeout, &discard)
}

// postJSONTo 向本机前门 POST 一个 JSON 并解析到 out
func (st *serveState) postJSONTo(path string, body map[string]any, timeout time.Duration, out any) error {
	b, _ := json.Marshal(body)
	client := &http.Client{Timeout: timeout}
	req, err := http.NewRequest(http.MethodPost, fmt.Sprintf("http://127.0.0.1:%d%s", st.port, path),
		strings.NewReader(string(b)))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	// 主进程自己调内核也带上 token（strict 下不带会被 401；主机与内核同机，读得到 token 文件）
	if tok := st.pluginToken(); tok != "" {
		req.Header.Set("X-Plugin-Token", tok)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return json.NewDecoder(resp.Body).Decode(out)
}

// ⑬ 日志链路：插件日志必须由 plugin.log 记录；内核兜底只应是"降级可用"，不该是常态
//
// 判据三层：
//
//	· plugin.log 答得上 log.stats（答不上 = 现在所有插件日志都在走内核兜底）
//	· 插件侧 errors/rejects 为 0（它自己记账有没有写失败）
//	· 最近日志里不该出现 [兜底写入]（出现 = 那段时间日志插件不可达）
func (st *serveState) checkLogPipeline(ki kernelInfo) checkItem {
	it := checkItem{ID: "log_pipeline", Name: "日志链路（插件记录 / 内核兜底）"}
	if !ki.Online {
		it.Status = "skip"
		it.Detail = "内核未运行"
		return it
	}
	var out struct {
		OK   bool `json:"ok"`
		Data struct {
			Batches   int `json:"batches"`
			Lines     int `json:"lines"`
			Rotations int `json:"rotations"`
			Errors    int `json:"errors"`
			Rejects   int `json:"rejects"`
		} `json:"data"`
	}
	err := st.postJSONTo("/api/bridge/plugins/call",
		map[string]any{"plugin": "plugin.log", "tool": "log.stats", "timeoutMs": 3000},
		8*time.Second, &out)
	if err != nil || !out.OK {
		it.Status = "warn"
		it.Detail = "plugin.log 没有应答 → 当前插件日志走内核兜底直写（不丢，但记录权没在插件手里）"
		it.Hint = "看 data\\logs\\plugin.log.log；控制台组件列表里它应是 running"
		return it
	}
	// 最近 5 分钟内是否出现过兜底写入（只看近期：历史兜底行会永久留在文件里，
	// 若不过滤时间，这条检查会永远告警——检查必须只反映"现在"。）
	fallback, newest := 0, ""
	cut := time.Now().Add(-5 * time.Minute)
	for _, f := range []string{"plugin.dsh.log", "plugin.context-pack.log", "plugin.log.log"} {
		for _, line := range tailLines(filepath.Join(st.logDir, f), 80) {
			if !strings.Contains(line, "[兜底写入]") {
				continue
			}
			tsPart := line
			if i := strings.Index(line, " | "); i > 0 {
				tsPart = line[:i]
			}
			ts, err := time.Parse(time.RFC3339, strings.TrimSpace(tsPart))
			if err != nil || ts.Before(cut) {
				continue // 解析不出来或太旧：不算"现在在兜底"
			}
			fallback++
			newest = line
		}
	}
	switch {
	case out.Data.Errors > 0 || out.Data.Rejects > 0:
		it.Status = "warn"
		it.Detail = fmt.Sprintf("插件记录中有错误：errors=%d rejects=%d（已记 %d 行）",
			out.Data.Errors, out.Data.Rejects, out.Data.Lines)
		it.Hint = "看 plugin.log 的分账 detail\\plugin.log\\*.jsonl 里的 error/rotate 详情"
	case fallback > 0:
		it.Status = "warn"
		it.Detail = fmt.Sprintf("近 5 分钟有 %d 行 [兜底写入]：这段时间 plugin.log 不可达（内核兜住了，没丢）；最近一条：%s", fallback, truncate(strings.TrimSpace(newest), 120))
		it.Hint = "偶发属正常（插件重启窗口）；持续出现说明日志插件反复挂"
	default:
		it.Status = "ok"
		it.Detail = fmt.Sprintf("日志由 plugin.log 记录：%d 批 / %d 行 / 轮转 %d 次，无错误",
			out.Data.Batches, out.Data.Lines, out.Data.Rotations)
	}
	return it
}

// ⑭ 工具路由：agent 的工具调用能不能按"能力名 == 工具名"转给插件（内核内模块外移后的命脉）
//
// 判据：真的走一次工具调用（note.search），看回执 ok 且 data 是数组——
// 这条路径断了的话，插件再健康也接不到活，而且**界面/探针全都看不出来**（探针只验插件活着）。
func (st *serveState) checkToolRouting(ki kernelInfo) checkItem {
	it := checkItem{ID: "tool_routing", Name: "工具路由（能力 → 插件）"}
	if !ki.Online {
		it.Status = "skip"
		it.Detail = "内核未运行"
		return it
	}
	plugins, err := st.externalPlugins()
	if err != nil {
		it.Status = "fail"
		it.Detail = "取组件列表失败：" + err.Error()
		return it
	}
	hasSearch := false
	for _, id := range plugins {
		if id == "plugin.note-search" {
			hasSearch = true
		}
	}
	if !hasSearch {
		it.Status = "skip"
		it.Detail = "本机没有声明 note.search 能力的插件"
		return it
	}
	body := map[string]any{
		"id": "selfcheck-tool-route", "agentId": "selfcheck", "kind": "tool-call",
		"call": map[string]any{"tool": "note.search", "args": map[string]any{"query": "枢阁", "limit": 1}},
	}
	var out struct {
		Result struct {
			OK    bool   `json:"ok"`
			Data  []any  `json:"data"`
			Error string `json:"error"`
		} `json:"result"`
	}
	if err := st.postJSONTo("/api/bridge/send", body, 25*time.Second, &out); err != nil {
		it.Status = "fail"
		it.Detail = "工具调用请求失败：" + err.Error()
		return it
	}
	if !out.Result.OK {
		it.Status = "fail"
		it.Detail = "工具未被插件处理：" + out.Result.Error
		it.Hint = "看内核日志「tool routed to plugin」与 data\\logs\\plugin.note-search.log"
		return it
	}
	it.Status = "ok"
	it.Detail = fmt.Sprintf("note.search 经能力路由到插件并返回 %d 条（内核内模块已清零）", len(out.Result.Data))
	return it
}

// ⑮ 插件面鉴权：插件端点必须拒绝"没有 token 的冒充者"，而带 token 的要能过
//
// 为什么要真发两次请求：鉴权最容易写成"看着有代码、其实没生效"（例如 token 为空时
// Valid() 直接放行，或校验挂错了端点）。所以这里**两个方向都验**：
// 无 token 期望 401、带 token 期望 200；token 从 data/plugins/token.json 读（主进程与内核同机）。
func (st *serveState) checkPluginAuth(ki kernelInfo) checkItem {
	it := checkItem{ID: "plugin_auth", Name: "插件面鉴权（token）"}
	if !ki.Online {
		it.Status = "skip"
		it.Detail = "内核未运行"
		return it
	}
	var doc struct {
		Token string `json:"token"`
	}
	b, err := os.ReadFile(filepath.Join(st.coreDir, "data", "plugins", "token.json"))
	if err != nil || json.Unmarshal(b, &doc) != nil || doc.Token == "" {
		it.Status = "warn"
		it.Detail = "没有 token 文件（data/plugins/token.json）→ 插件面未启用鉴权"
		it.Hint = "内核启动时会自动生成；若长期缺失，检查 data 目录写权限"
		return it
	}
	url := fmt.Sprintf("http://127.0.0.1:%d/api/bridge/plugins/poll?plugin=selfcheck&wait=0", st.port)
	client := &http.Client{Timeout: 6 * time.Second}
	// ① 无 token：必须被拒
	resp, err := client.Get(url)
	if err != nil {
		it.Status = "fail"
		it.Detail = "探测插件面失败：" + err.Error()
		return it
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		it.Status = "fail"
		it.Detail = fmt.Sprintf("无 token 的插件面请求竟然返回 HTTP %d（应为 401）→ 鉴权没生效", resp.StatusCode)
		it.Hint = "本机任何进程都能冒充插件注册/领活/回伪造结果，属平台完整性缺口"
		return it
	}
	// ② 带 token：必须放行
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	req.Header.Set(pluginauth.Header, doc.Token)
	resp2, err := client.Do(req)
	if err != nil {
		it.Status = "fail"
		it.Detail = "带 token 的探测失败：" + err.Error()
		return it
	}
	defer resp2.Body.Close()
	if resp2.StatusCode != http.StatusOK {
		it.Status = "fail"
		it.Detail = fmt.Sprintf("带正确 token 也被拒（HTTP %d）→ 插件会全部注册失败", resp2.StatusCode)
		return it
	}
	it.Status = "ok"
	it.Detail = fmt.Sprintf("无 token → 401、带 token → 200（token 文件存在，长度 %d）", len(doc.Token))
	return it
}

// ⑯ agent 上报面鉴权：谁在往平台报事件、带没带 token
//
// 判据取自内核 /healthz 的 auth 段（模式 + 带/不带 token 的调用计数）：
//
//	· strict 且无未鉴权调用 → 正常（闭环）
//	· soft  且收到未鉴权调用 → 警告（迁移期正常，但说明还有调用方没带 token）
//	· soft  且零未鉴权调用 → 提示可以转 strict（两端都已带 token）
func (st *serveState) checkAgentUplinkAuth(ki kernelInfo) checkItem {
	it := checkItem{ID: "agent_uplink_auth", Name: "agent 上报面鉴权"}
	if !ki.Online {
		it.Status = "skip"
		it.Detail = "内核未运行"
		return it
	}
	if ki.Auth == nil {
		it.Status = "warn"
		it.Detail = "内核 /healthz 没有 auth 段（版本旧？）"
		return it
	}
	mode, _ := ki.Auth["agent_uplink"].(string)
	authed := jsonNum(ki.Auth["authed_total"])
	unauth := jsonNum(ki.Auth["unauth_total"])
	paths := map[string]uint64{}
	if m, ok := ki.Auth["unauth_by_path"].(map[string]any); ok {
		for k, v := range m {
			paths[k] = jsonNum(v)
		}
	}
	recent, lastDetail := st.recentAuthMissing(5 * time.Minute)
	switch {
	case mode == "off":
		it.Status = "warn"
		it.Detail = "鉴权关闭（off）：本机任何进程都能伪造 agent 上报"
		it.Hint = "改 config.yaml 的 security.agent_uplink_auth 为 soft/strict 后重启内核"
	case mode == "strict" && recent == 0:
		it.Status = "ok"
		it.Detail = fmt.Sprintf("strict 模式：近 5 分钟无未鉴权调用（带 token 累计 %d 次；累计未鉴权 %d 次均已过期）", authed, unauth)
	case mode == "strict":
		it.Status = "warn"
		it.Detail = fmt.Sprintf("strict 模式：近 5 分钟有 %d 次未鉴权调用被拒（累计带 token %d）；最近：%s", recent, authed, lastDetail)
		it.Hint = "有调用方没带 token（DSH 插件 / reasonix hook / 自检探针）；看 data\\logs 里对应那方的日志"
	case recent > 0:
		it.Status = "warn"
		it.Detail = fmt.Sprintf("%s 模式：近 5 分钟 %d 次未鉴权（已放行但记账），带 token %d 次；来源 %v；最近：%s",
			mode, recent, authed, paths, lastDetail)
		it.Hint = "调用方（DSH 插件 / reasonix hook）应读 data/plugins/token.json 带 X-Plugin-Token；都带上后可切 strict"
	default:
		it.Status = "ok"
		it.Detail = fmt.Sprintf("%s 模式：全部调用都带 token（%d 次）→ 可切 strict 强制", mode, authed)
	}
	return it
}

// jsonNum JSON 数字 → uint64（healthz 解出来是 float64）
func jsonNum(v any) uint64 {
	switch n := v.(type) {
	case float64:
		return uint64(n)
	case int:
		return uint64(n)
	case uint64:
		return n
	}
	return 0
}

// pluginToken 读内核生成的插件面 token（主进程与内核同机；读不到返回空）
func (st *serveState) pluginToken() string {
	st.tokenMu.Lock()
	defer st.tokenMu.Unlock()
	if time.Since(st.tokenAt) < 30*time.Second {
		return st.tokenCache
	}
	st.tokenAt = time.Now()
	st.tokenCache = ""
	var doc struct {
		Token string `json:"token"`
	}
	if b, err := os.ReadFile(filepath.Join(st.coreDir, "data", "plugins", "token.json")); err == nil {
		if json.Unmarshal(b, &doc) == nil {
			st.tokenCache = doc.Token
		}
	}
	return st.tokenCache
}

// recentAuthMissing 最近 window 内的未鉴权调用（从账本尾部扫，避免"累计计数永久告警"）
func (st *serveState) recentAuthMissing(window time.Duration) (int, string) {
	files, _ := filepath.Glob(filepath.Join(st.coreDir, "data", "ledger", "ledger-*.jsonl"))
	if len(files) == 0 {
		return 0, ""
	}
	sort.Strings(files)
	cut := time.Now().Add(-window)
	n, last := 0, ""
	for _, line := range tailLines(files[len(files)-1], 800) {
		if !strings.Contains(line, "platform.auth.missing") {
			continue
		}
		var o struct {
			TS      string `json:"ts"`
			Payload struct {
				Path string `json:"path"`
				UA   string `json:"ua"`
				Mode string `json:"mode"`
			} `json:"payload"`
		}
		if json.Unmarshal([]byte(line), &o) != nil {
			continue
		}
		ts, err := time.Parse(time.RFC3339, o.TS)
		if err != nil || ts.Before(cut) {
			continue
		}
		n++
		last = fmt.Sprintf("%s path=%s ua=%s", ts.Format("15:04:05"), o.Payload.Path, o.Payload.UA)
	}
	return n, last
}

// agentDeclaredBy 该插件是否声明了指定 agent 身份（身份是插件字段，见 bridge.PluginInfo）
func (st *serveState) agentDeclaredBy(pluginID, agentID string) bool {
	var comps []struct {
		ID      string `json:"id"`
		AgentID string `json:"agent_id"`
	}
	client := &http.Client{Timeout: 5 * time.Second}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/api/components", st.port))
	if err != nil {
		return false
	}
	defer resp.Body.Close()
	if json.NewDecoder(resp.Body).Decode(&comps) != nil {
		return false
	}
	for _, x := range comps {
		if x.ID == pluginID && x.AgentID == agentID {
			return true
		}
	}
	return false
}

// ⑰ 内核自报健康：/healthz 的 ok 与 reasons（"在跑但降级"必须被看见）
//
// 为什么单独一项：其它检查各自只看自己关心的面；这一项专门回答"内核自己怎么说"。
// 演练 B（把账本目录指到不存在的盘）就是靠它把 reasons=[ledger not enabled] 摆出来的。
func (st *serveState) checkKernelHealth(ki kernelInfo) checkItem {
	it := checkItem{ID: "kernel_health", Name: "内核自报健康（/healthz）"}
	if !ki.Online {
		it.Status = "fail"
		it.Detail = "内核 /healthz 无应答（进程不在或端口不通）"
		it.Hint = "控制台点「启动内核」；看 data\\logs\\core.log"
		return it
	}
	if ki.Healthy {
		it.Status = "ok"
		it.Detail = fmt.Sprintf("ok=true（pid %d，已运行 %v）", ki.PID, shortDur(time.Duration(ki.UptimeS)*time.Second))
		return it
	}
	it.Status = "fail"
	it.Detail = "内核在跑但自报不健康：reasons=" + strings.Join(ki.Reasons, "、")
	it.Hint = "按 reasons 逐条查（如 ledger not enabled = 账本没打开，事件将不入账）"
	return it
}
