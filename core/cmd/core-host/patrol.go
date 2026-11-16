//go:build windows

// 本文件：常驻巡检（patrol）——把"今天手工验过一遍"变成"系统每 5 分钟自证一次"。
//
// 为什么需要（辞玖 2026-09-12 定）：
//
//	自检 18 项能力早就有了，但它是**手动点按钮**的——没人会每 10 分钟点一次，
//	于是"平台是否完好"仍然只等于"我上次看的时候是好的"。常驻巡检把它变成连续证据：
//	  · 主进程（常驻、内核不在也能跑）每 N 分钟跑一遍同一份清单；
//	  · **只在跃迁时留痕**（正常→异常、异常→恢复、异常项变化），避免刷屏；
//	  · 跃迁写进平台账本（platform.patrol.*），所以"什么时候坏过、坏了什么、什么时候好的"可事后查；
//	  · 控制台顶部一条常驻横条显示最近一次结论，失败时亮红。
//
// 与看门狗的分工：看门狗只管"内核进程在不在"；巡检管"平台是否**完好**"（账本/锁/端口分层/前门/
// 通道/日志链路/鉴权…）。前者是保命，后者是体检。
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	patrolDefaultEvery = 5 * time.Minute
	patrolWarmup       = 20 * time.Second // 启动后等一下再跑第一遍（让内核/插件先起来）
	patrolKeepHistory  = 20               // 保留最近多少次跃迁
)

// patrolState 巡检状态（落盘：控制台可读、重启不丢）
type patrolState struct {
	LastAt         string             `json:"last_at"`
	LastKind       string             `json:"last_kind"` // manual | timer | startup
	EveryS         int                `json:"every_s"`
	OK             int                `json:"ok"`
	Known          int                `json:"known"`
	Warn           int                `json:"warn"`
	Fail           int                `json:"fail"`
	Skip           int                `json:"skip"`
	Summary        string             `json:"summary"`
	Problems       []string           `json:"problems"`        // 当前的警告/失败项（名字）
	ProblemDetail  []string           `json:"problem_detail"`  // 每项的简述
	SinceAt        string             `json:"since_at"`        // 当前这个"结论"从什么时候开始
	ConsecProblems int                `json:"consec_problems"` // 连续多少轮有问题（含手动）
	TotalRuns      int                `json:"total_runs"`
	Transitions    []patrolTransition `json:"transitions"` // 最近的跃迁
	// PendingPublish 投递失败的跃迁事件（内核当时不在）——保留原始时间，等平台回来补录
	// 为什么必须补：报警最有价值的时刻恰恰是"平台正坏着"，那会儿账本写不进去，丢了就白报。
	PendingPublish []map[string]any `json:"pending_publish,omitempty"`
}

type patrolTransition struct {
	At      string `json:"at"`
	Kind    string `json:"kind"` // ok | problem | recovered | changed
	Summary string `json:"summary"`
}

func (st *serveState) patrolPath() string {
	return filepath.Join(st.coreDir, "data", "logs", "patrol.state.json")
}

// loadPatrol 读巡检状态（没有就返回空状态）
func (st *serveState) loadPatrol() *patrolState {
	ps := &patrolState{}
	if b, err := os.ReadFile(st.patrolPath()); err == nil {
		_ = json.Unmarshal(b, ps)
	}
	return ps
}

func (st *serveState) savePatrol(ps *patrolState) {
	if b, err := json.MarshalIndent(ps, "", "  "); err == nil {
		_ = os.MkdirAll(filepath.Dir(st.patrolPath()), 0o755)
		_ = os.WriteFile(st.patrolPath(), append(b, '\n'), 0o644)
	}
}

// applyPatrol 用一次检查结果更新巡检状态；**只在跃迁时**返回需要留痕的事件
//
// 判据（避免噪音）：
//
//	· 结论未变 → 什么都不记（只更新 last_at/计数）
//	· 结论变了（好→坏 / 坏→好 / 坏项集合变了）→ 记一条跃迁 + 进账本
func (st *serveState) applyPatrol(rep *checkReport, kind string) (transition *patrolTransition, payload map[string]any) {
	st.patrolMu.Lock()
	defer st.patrolMu.Unlock()

	prev := st.loadPatrol()
	cur := &patrolState{
		LastAt: rep.At, LastKind: kind, EveryS: prev.EveryS,
		OK: rep.OK, Known: rep.Known, Warn: rep.Warn, Fail: rep.Fail, Skip: rep.Skip,
		Summary: rep.Summary, Transitions: prev.Transitions,
		PendingPublish: prev.PendingPublish, // 必须继承：否则每轮检查都会把"没送出去的报警"清空（本人踩过）
		TotalRuns:      prev.TotalRuns + 1,
		SinceAt:        prev.SinceAt,
	}
	for _, it := range rep.Items {
		if it.Status == "warn" || it.Status == "fail" {
			cur.Problems = append(cur.Problems, it.Name)
			cur.ProblemDetail = append(cur.ProblemDetail, it.Name+"："+truncate(it.Detail, 120))
		}
	}
	sort.Strings(cur.Problems)
	prevKey := strings.Join(prev.Problems, "|")
	curKey := strings.Join(cur.Problems, "|")

	if prevKey == curKey { // 结论没变：只续期
		cur.SinceAt = prev.SinceAt
		if cur.SinceAt == "" {
			cur.SinceAt = rep.At
		}
		cur.ConsecProblems = prev.ConsecProblems
		st.savePatrol(cur)
		st.patrolCache = cur
		return nil, nil
	}

	// 第一次运行只建立基线：没有"上一份结论"可比，报跃迁只会制造噪音
	if prev.TotalRuns == 0 {
		cur.SinceAt = rep.At
		cur.ConsecProblems = 0
		st.savePatrol(cur)
		st.patrolCache = cur
		return nil, nil
	}

	// 跃迁
	t := &patrolTransition{At: rep.At}
	switch {
	case prevKey == "" && curKey != "":
		t.Kind = "problem"
		t.Summary = fmt.Sprintf("平台体检出现异常：%s（%s）", strings.Join(cur.Problems, "、"), rep.Summary)
		cur.ConsecProblems = 1
	case prevKey != "" && curKey == "":
		t.Kind = "recovered"
		t.Summary = fmt.Sprintf("平台体检恢复：%s", rep.Summary)
		cur.ConsecProblems = 0
	default:
		t.Kind = "changed"
		t.Summary = fmt.Sprintf("平台体检异常项变化：%s → %s", prevKey, curKey)
		cur.ConsecProblems = prev.ConsecProblems + 1
	}
	cur.SinceAt = rep.At
	cur.Transitions = append([]patrolTransition{*t}, prev.Transitions...)
	if len(cur.Transitions) > patrolKeepHistory {
		cur.Transitions = cur.Transitions[:patrolKeepHistory]
	}
	st.savePatrol(cur)
	st.patrolCache = cur

	payload = map[string]any{
		"kind": t.Kind, "at": t.At, "summary": t.Summary,
		"ok": rep.OK, "known": rep.Known, "warn": rep.Warn, "fail": rep.Fail, "skip": rep.Skip,
		"problems": cur.Problems, "problem_detail": cur.ProblemDetail,
		"every_s": cur.EveryS, "consec_problems": cur.ConsecProblems,
	}
	return t, payload
}

// patrolLoop 常驻巡检循环（主进程里跑；内核不在也能跑——那时检查项自己会 skip）
func (st *serveState) patrolLoop(every time.Duration) {
	if every <= 0 {
		logf(st.logDir, "常驻巡检：已关闭（--patrol=0）")
		return
	}
	st.patrolMu.Lock()
	ps := st.loadPatrol()
	ps.EveryS = int(every.Seconds())
	st.savePatrol(ps)
	st.patrolMu.Unlock()

	time.Sleep(patrolWarmup)
	logf(st.logDir, "常驻巡检启动：每 %v 跑一遍自检（只在跃迁时留痕 + 进账本）", every)
	for {
		st.flushPendingPatrol() // 先补录上次没送出去的（平台可能刚回来）
		rep := st.runChecks()
		t, payload := st.applyPatrol(rep, "timer")
		if t != nil {
			logf(st.logDir, "巡检跃迁[%s]：%s", t.Kind, t.Summary)
			if err := st.publishPatrol(payload); err != nil {
				logf(st.logDir, "巡检跃迁事件投递失败（入队，等下次补录）：%v", err)
				st.enqueuePatrol(payload)
			}
		}
		time.Sleep(every)
	}
}

// publishPatrol 把跃迁写进平台账本（经 agent 上报面；主进程带 token）
//
// 走 /api/bridge/send 而不是直接写账本：账本归内核，主进程只是"另一个上报方"——
// 这样即便内核在重启，事件也只是投递失败（下次巡检继续），不会破坏"谁写账本"的边界。
func (st *serveState) publishPatrol(payload map[string]any) error {
	kind, _ := payload["kind"].(string)
	at, _ := payload["at"].(string)
	if at == "" {
		at = time.Now().Format(time.RFC3339)
	}
	body := map[string]any{
		"id":      fmt.Sprintf("patrol-%d", time.Now().UnixMilli()),
		"agentId": "patrol",
		"kind":    "event",
		"event": map[string]any{
			"type": "platform.patrol." + kind, "source": "patrol", "time": at, "payload": payload,
		},
	}
	_, err := st.postJSON("/api/bridge/send", body, 10*time.Second)
	return err
}

// enqueuePatrol 投递失败的事件入队（持久化：主进程重启也不丢）
func (st *serveState) enqueuePatrol(payload map[string]any) {
	st.patrolMu.Lock()
	defer st.patrolMu.Unlock()
	ps := st.loadPatrol()
	ps.PendingPublish = append(ps.PendingPublish, payload)
	if len(ps.PendingPublish) > 50 { // 防无限堆积
		ps.PendingPublish = ps.PendingPublish[len(ps.PendingPublish)-50:]
	}
	st.savePatrol(ps)
	st.patrolCache = ps
}

// flushPendingPatrol 把之前没送出去的跃迁补录进账本（成功后清队）
func (st *serveState) flushPendingPatrol() {
	st.patrolMu.Lock()
	ps := st.loadPatrol()
	pending := ps.PendingPublish
	st.patrolMu.Unlock()
	if len(pending) == 0 {
		return
	}
	left := []map[string]any{}
	for _, pl := range pending {
		if err := st.publishPatrol(pl); err != nil {
			left = append(left, pl) // 还是不在：继续留着
			continue
		}
		logf(st.logDir, "巡检跃迁补录成功（原始时间 %v）", pl["at"])
	}
	st.patrolMu.Lock()
	ps2 := st.loadPatrol()
	ps2.PendingPublish = left
	st.savePatrol(ps2)
	st.patrolCache = ps2
	st.patrolMu.Unlock()
}

// patrolSnapshot 给控制台的巡检快照
func (st *serveState) patrolSnapshot() map[string]any {
	st.patrolMu.Lock()
	defer st.patrolMu.Unlock()
	ps := st.patrolCache
	if ps == nil {
		ps = st.loadPatrol()
		st.patrolCache = ps
	}
	verdict := "unknown"
	switch {
	case ps.TotalRuns == 0:
		verdict = "unknown"
	case ps.Fail > 0:
		verdict = "fail"
	case ps.Warn > 0:
		verdict = "warn" // 只算 warn/fail：known（已知且接受）不拉警报
	default:
		verdict = "ok"
	}
	return map[string]any{
		"verdict": verdict, "last_at": ps.LastAt, "last_kind": ps.LastKind,
		"every_s": ps.EveryS, "total_runs": ps.TotalRuns,
		"ok": ps.OK, "known": ps.Known, "warn": ps.Warn, "fail": ps.Fail, "skip": ps.Skip,
		"summary": ps.Summary, "problems": ps.Problems, "problem_detail": ps.ProblemDetail,
		"since_at": ps.SinceAt, "consec_problems": ps.ConsecProblems,
		"transitions": ps.Transitions, "pending_publish": len(ps.PendingPublish),
	}
}
