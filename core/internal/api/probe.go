// 本文件：心跳点名（probe）——平台按周期"点名"每个 external 插件。
//
// 一次点名同时拿到三样东西（辞玖 2026-09-11 定：心跳与点名合并成一个功能，方向取 pull）：
//
//	· 活性：答没答
//	· 功能：点名即探功能（顺手做件真实小事，如 context.get）
//	· 状态快照：回执带 stats → 直接喂 /api/status 的活性快照
//
// 每个插件一条独立点名时钟（不是刚性格子）：
//
//	① 启动/拉起成功 → 1s 后立刻点（首次自检；Q1：启动即点，不等周期）
//	② 在岗 → 每 interval 点一次
//	③ 无应答 → 每 retryInterval 重试；连续 suspectAfter 次 → suspect（入账）→ 交 supervisor 重启
//	④ 回执 busy（正在干长活）→ 记在岗，且下次点名推迟一个 interval（不追着问）
//	⑤ 一次点名未回 → 不再发新的（去重）；超时算一次失败
//	⑥ absent（未启用/按需未拉起）→ 不点、不告警
//
// 入账规则：只有**状态跃迁**入账（suspect / recovered / dead），点名本身不入账（避免流量洪水）。
//
// 放在 api 包的原因：点名用的是本包的插件调用通道（pluginCallBroker：call/poll/result）。
package api

import (
	"context"
	"sync"
	"time"

	"core/internal/config"
	"core/internal/plugin"
)

const (
	firstProbeDelay        = 1000 * time.Millisecond // 启动后首次点名延迟（等 register/poll 就绪）
	probeTimeout           = 5 * time.Second         // 单次点名超时
	retryInterval          = 3 * time.Second         // 失败重试间隔（去抖）
	probeSuspectAfter      = 3                       // 连续失败达此数 → suspect + 拉起
	probeDeadAfter         = 6                       // 连续失败达此数 → dead
	defaultProbeIntervalMS = 15000                   // 默认点名周期（心跳周期）
	probeSweepEvery        = 1000 * time.Millisecond // 调度器扫描间隔
	defaultActionExpectMS  = 60000                   // 动作未声明 expect_within_ms 时的默认超时期
)

// ProbeTarget 点名目标（来自 config 的 external 插件清单）
type ProbeTarget struct {
	ID         string
	IntervalMS int // 0 → defaultProbeIntervalMS
}

// ProbeTargetFromConfig 由插件配置造点名目标 —— 启动装配与热加**共用同一份规则**，
// 免得 interval 的解析在两处各写一遍、哪天只改了一处。
func ProbeTargetFromConfig(pc config.PluginConfig) ProbeTarget {
	interval := 0
	if iv, ok := pc.Settings["heartbeat_interval_ms"].(int); ok {
		interval = iv
	}
	return ProbeTarget{ID: pc.ID, IntervalMS: interval}
}

// AddProbeTarget 运行时补一个点名目标（热加插件用；同 id 覆盖刷新）。
//
// ⚠️ 必须持 p.mu 写：sweep() 原本是**无锁遍历** entries 的（在它之前没人写这张表，所以侥幸没炸），
// 一加写入就是 "concurrent map iteration and map write" → 内核 fatal。本补丁把 sweep/probeOne/
// ProbeSummary 的读也一并加锁，读写同一条规矩。
func (s *Server) AddProbeTarget(t ProbeTarget) {
	if s.probe == nil || t.ID == "" {
		return
	}
	if t.IntervalMS <= 0 {
		t.IntervalMS = defaultProbeIntervalMS
	}
	s.probe.mu.Lock()
	defer s.probe.mu.Unlock()
	s.probe.entries[t.ID] = &probeEntry{target: t, state: "unknown",
		nextAt: time.Now().Add(firstProbeDelay)}
}

// ProbeState 台账一行（供 /api/plugins/probe 与 /api/status）
type ProbeState struct {
	Plugin      string         `json:"plugin"`
	State       string         `json:"state"` // absent|unknown|healthy|busy|suspect|restarting|dead
	IntervalMS  int            `json:"interval_ms"`
	LastProbeAt string         `json:"last_probe_at,omitempty"`
	LastOKAt    string         `json:"last_ok_at,omitempty"`
	ConsecFail  int            `json:"consec_fail"`
	LastError   string         `json:"last_error,omitempty"`
	Restarts    int            `json:"restarts"`
	Stats       map[string]any `json:"stats,omitempty"`
}

type probeEntry struct {
	mu        sync.Mutex
	target    ProbeTarget
	state     string
	lastProbe time.Time
	lastOK    time.Time
	fails     int
	lastErr   string
	restarts  int
	stats     map[string]any
	nextAt    time.Time
	inFlight  bool
}

// ProbeScheduler 点名调度器（同时负责动作超时判定：有发起无结束 → 补终态）
type ProbeScheduler struct {
	srv     *Server
	mu      sync.RWMutex
	entries map[string]*probeEntry
	stop    chan struct{}

	actMu   sync.Mutex
	actions map[string]*actionWatch
}

// actionWatch 在途动作（发起已见、结束未见）
type actionWatch struct {
	id       string
	name     string
	started  time.Time
	expectMS int64
}

// StartProbe 启动点名调度（main 装配时调用；重复调用无副作用）
func (s *Server) StartProbe(targets []ProbeTarget) {
	if s.probe != nil || len(targets) == 0 {
		return
	}
	p := &ProbeScheduler{srv: s, entries: map[string]*probeEntry{}, stop: make(chan struct{}),
		actions: map[string]*actionWatch{}}
	now := time.Now()
	for _, t := range targets {
		if t.IntervalMS <= 0 {
			t.IntervalMS = defaultProbeIntervalMS
		}
		p.entries[t.ID] = &probeEntry{target: t, state: "unknown", nextAt: now.Add(firstProbeDelay)}
	}
	s.probe = p

	// 订阅进程事件：拉起成功后**立即点名确认**（对应"启动即点"那条路径）
	if s.bus != nil {
		_, _ = s.bus.SubscribeNamed(context.Background(), "probe.process-events",
			[]string{"platform.process.started", "platform.process.exited", "platform.process.gave-up",
				"platform.action.started", "platform.action.settled"},
			func(ctx context.Context, ev plugin.Event) error {
				if len(ev.Type) > 8 && ev.Type[:8] == "platform" {
					switch ev.Type {
					case "platform.action.started", "platform.action.settled":
						p.onActionEvent(ev)
						return nil
					}
				}
				p.onProcessEvent(ev)
				return nil
			})
	}
	go p.loop()
	s.log.Info("心跳点名已启动", "targets", len(targets), "interval_ms", defaultProbeIntervalMS)
}

// ProbeStates 台账快照
func (s *Server) ProbeStates() []ProbeState {
	if s.probe == nil {
		return []ProbeState{}
	}
	return s.probe.states()
}

// ProbeSummary 台账汇总（喂 /api/status）
func (s *Server) ProbeSummary() map[string]any {
	if s.probe == nil {
		return map[string]any{"enabled": false}
	}
	counts := map[string]int{}
	for _, st := range s.probe.states() {
		counts[st.State]++
	}
	s.probe.mu.RLock()
	targets := len(s.probe.entries)
	s.probe.mu.RUnlock()
	return map[string]any{
		"enabled": true, "targets": targets,
		"interval_ms": defaultProbeIntervalMS, "by_state": counts,
	}
}

// ---- 调度 ----

func (p *ProbeScheduler) loop() {
	t := time.NewTicker(probeSweepEvery)
	defer t.Stop()
	for {
		select {
		case <-p.stop:
			return
		case <-t.C:
			p.sweep()
		}
	}
}

// onActionEvent 维护在途动作表：发起入表、结束出表（无结束的由 sweepActions 补终态）
func (p *ProbeScheduler) onActionEvent(ev plugin.Event) {
	act, _ := ev.Payload["action"].(map[string]any)
	id, _ := act["id"].(string)
	if id == "" {
		return
	}
	p.actMu.Lock()
	defer p.actMu.Unlock()
	switch ev.Type {
	case "platform.action.started":
		expect := int64(defaultActionExpectMS)
		if v, ok := act["expect_within_ms"].(float64); ok && v > 0 {
			expect = int64(v)
		}
		name, _ := act["name"].(string)
		p.actions[id] = &actionWatch{id: id, name: name, started: time.Now(), expectMS: expect}
	case "platform.action.settled":
		delete(p.actions, id)
	}
}

// sweepActions 动作超时：有发起无结束且超过 expect_within_ms → 补一条终态并告警
// （进程级卡死有点名看得见；"业务动作卡住"此前不可见，这里补上）
func (p *ProbeScheduler) sweepActions() {
	now := time.Now()
	overdue := []*actionWatch{}
	p.actMu.Lock()
	for id, w := range p.actions {
		if now.Sub(w.started) > time.Duration(w.expectMS)*time.Millisecond {
			overdue = append(overdue, w)
			delete(p.actions, id) // 一次性：补终态后不再重复告警
		}
	}
	p.actMu.Unlock()
	for _, w := range overdue {
		p.emit("platform.action.timeout", map[string]any{
			"action":           map[string]any{"id": w.id, "name": w.name},
			"expect_within_ms": w.expectMS,
			"overdue_ms":       now.Sub(w.started).Milliseconds(),
		})
	}
}

// sweep 每轮扫描：到点的插件发起点名（resident 判定 + 去重）
func (p *ProbeScheduler) sweep() {
	p.sweepActions()
	resident := p.residentSet()
	now := time.Now()
	// 先取 id 快照，再一个个处理：热加会**写** entries，遍历必须在 p.mu 下（见 AddProbeTarget 的注释）
	p.mu.RLock()
	ids := make([]string, 0, len(p.entries))
	for id := range p.entries {
		ids = append(ids, id)
	}
	p.mu.RUnlock()
	for _, id := range ids {
		p.mu.RLock()
		e := p.entries[id]
		p.mu.RUnlock()
		if e == nil {
			continue
		}
		e.mu.Lock()
		isRes := resident[id]
		if !isRes {
			// absent：未启用/按需未拉起 —— 不点、不告警（低频与按需是正常状态）
			e.state = "absent"
			e.inFlight = false
			e.nextAt = now.Add(time.Duration(e.target.IntervalMS) * time.Millisecond)
			e.mu.Unlock()
			continue
		}
		due := now.After(e.nextAt)
		busy := e.inFlight
		if due && !busy {
			e.inFlight = true
			e.lastProbe = now
		}
		e.mu.Unlock()
		if due && !busy {
			go p.probeOne(id)
		}
	}
}

// residentSet 当前"应在岗"的插件集合（由 supervisor 的实际托管状态决定）
func (p *ProbeScheduler) residentSet() map[string]bool {
	out := map[string]bool{}
	if p.srv.sup == nil {
		return out
	}
	for _, st := range p.srv.sup.Status() {
		id, _ := st["id"].(string)
		stopped, _ := st["stopped"].(bool)
		if id != "" && !stopped {
			out[id] = true
		}
	}
	return out
}

// probeOne 对单个插件点一次名
func (p *ProbeScheduler) probeOne(id string) {
	p.mu.RLock()
	e := p.entries[id]
	p.mu.RUnlock()
	if e == nil {
		return
	}
	callID, ch := p.srv.pluginCalls.enqueue(id, "plugin.ping",
		map[string]any{"ts": time.Now().Format(time.RFC3339)})

	var res PluginCallResult
	select {
	case res = <-ch:
	case <-time.After(probeTimeout):
		p.srv.pluginCalls.forget(callID)
		res = PluginCallResult{OK: false, Error: "点名超时（插件未在超时内回复，检查 poll 循环）"}
	}

	interval := time.Duration(e.target.IntervalMS) * time.Millisecond
	if res.OK {
		stats, _ := res.Data.(map[string]any)
		busy := false
		if stats != nil {
			if s, _ := stats["status"].(string); s == "busy" {
				busy = true
			}
		}
		e.mu.Lock()
		prev := e.state
		if busy {
			e.state = "busy"
		} else {
			e.state = "healthy"
		}
		e.fails = 0
		e.lastOK = time.Now()
		e.lastErr = ""
		e.stats = stats
		e.nextAt = time.Now().Add(interval)
		e.inFlight = false
		e.mu.Unlock()
		if prev == "suspect" || prev == "restarting" || prev == "dead" {
			p.emit("platform.plugin.recovered", map[string]any{"plugin": id, "from": prev})
		}
		return
	}

	// 失败：累加、按阈值跃迁
	e.mu.Lock()
	e.fails++
	fails := e.fails
	prev := e.state
	e.lastErr = res.Error
	switch {
	case fails >= probeDeadAfter:
		e.state = "dead"
	case fails >= probeSuspectAfter:
		e.state = "suspect"
	default:
		e.state = "unknown"
	}
	e.nextAt = time.Now().Add(retryInterval)
	e.inFlight = false
	e.mu.Unlock()

	if fails == probeSuspectAfter {
		p.emit("platform.plugin.suspect", map[string]any{
			"plugin": id, "fails": fails, "last_error": res.Error, "previous_state": prev,
		})
		p.restartPlugin(id) // 拉起即自愈；拉起来会发 process.started → 触发"启动即点名"
	}
	if fails == probeDeadAfter {
		p.emit("platform.plugin.dead", map[string]any{
			"plugin": id, "fails": fails, "last_error": res.Error,
		})
	}
}

// restartPlugin 交 supervisor 重启（点名判定"卡死"后的自愈动作）
func (p *ProbeScheduler) restartPlugin(id string) {
	if p.srv.sup == nil {
		return
	}
	if err := p.srv.sup.Restart(id); err != nil {
		p.emit("platform.process.restart-failed", map[string]any{"plugin": id, "err": err.Error(), "by": "probe"})
		return
	}
	e := p.entries[id]
	if e == nil {
		return
	}
	e.mu.Lock()
	e.state = "restarting"
	e.restarts++
	e.mu.Unlock()
}

// onProcessEvent 进程事件驱动点名时钟（启动即点 / 退出重置 / 放弃即判死）
func (p *ProbeScheduler) onProcessEvent(ev plugin.Event) {
	id, _ := ev.Payload["id"].(string)
	if id == "" {
		return
	}
	e := p.entries[id]
	if e == nil {
		return
	}
	switch ev.Type {
	case "platform.process.started":
		e.mu.Lock()
		if e.state != "healthy" {
			e.state = "restarting"
		}
		e.fails = 0
		e.nextAt = time.Now().Add(firstProbeDelay) // 启动即点（不等周期）
		e.mu.Unlock()
	case "platform.process.exited":
		e.mu.Lock()
		e.nextAt = time.Now().Add(retryInterval)
		e.mu.Unlock()
	case "platform.process.gave-up":
		e.mu.Lock()
		prev := e.state
		e.state = "dead"
		e.mu.Unlock()
		if prev != "dead" {
			p.emit("platform.plugin.dead", map[string]any{"plugin": id, "reason": "supervisor 放弃重启"})
		}
	}
}

func (p *ProbeScheduler) states() []ProbeState {
	p.mu.RLock()
	defer p.mu.RUnlock()
	out := make([]ProbeState, 0, len(p.entries))
	for id, e := range p.entries {
		e.mu.Lock()
		st := ProbeState{
			Plugin:     id,
			State:      e.state,
			IntervalMS: e.target.IntervalMS,
			ConsecFail: e.fails,
			LastError:  e.lastErr,
			Restarts:   e.restarts,
			Stats:      e.stats,
		}
		if !e.lastProbe.IsZero() {
			st.LastProbeAt = e.lastProbe.Format(time.RFC3339)
		}
		if !e.lastOK.IsZero() {
			st.LastOKAt = e.lastOK.Format(time.RFC3339)
		}
		e.mu.Unlock()
		out = append(out, st)
	}
	return out
}

func (p *ProbeScheduler) emit(evType string, payload map[string]any) {
	if p.srv.bus == nil {
		return
	}
	_ = p.srv.bus.Publish(context.Background(), plugin.NewEvent(evType, "probe", payload))
	if p.srv.log != nil {
		p.srv.log.Warn("probe: 状态跃迁", "event", evType, "plugin", payload["plugin"])
	}
}
