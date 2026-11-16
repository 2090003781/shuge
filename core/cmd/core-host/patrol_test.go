//go:build windows

// 本文件：常驻巡检的回归测试（2026-09-12 补测试网）。
//
// 为什么单测它：巡检的价值全在"**只在跃迁时留痕**"这条纪律上——
//
//	· 结论没变却留痕 → 每 5 分钟一条账，账本被体检噪音淹掉
//	· 结论变了却不留痕 → 坏了没人知道
//
// 另外钉住两个今天踩过的坑：首次运行只建基线；待补录队列必须被继承（否则每轮被清空）。
package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func newTestState(t *testing.T) *serveState {
	t.Helper()
	dir := t.TempDir()
	return &serveState{
		coreDir: dir,
		logDir:  filepath.Join(dir, "data", "logs"),
		startAt: time.Now(),
	}
}

func report(items ...checkItem) *checkReport {
	rep := &checkReport{At: time.Now().Format(time.RFC3339)}
	for _, it := range items {
		rep.Items = append(rep.Items, it)
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
	return rep
}

func okItem() checkItem { return checkItem{ID: "a", Name: "端口分层", Status: "ok"} }
func warnItem() checkItem {
	return checkItem{ID: "b", Name: "账本", Status: "warn", Detail: "坏了"}
}
func knownItem() checkItem { return checkItem{ID: "c", Name: "历史损坏", Status: "known"} }

// ① 首次运行只建基线：不报跃迁（否则启动就制造一条假告警）
func TestPatrolFirstRunIsBaseline(t *testing.T) {
	st := newTestState(t)
	tr, payload := st.applyPatrol(report(okItem()), "timer")
	if tr != nil || payload != nil {
		t.Fatalf("首次运行不该产生跃迁：%+v", tr)
	}
	ps := st.loadPatrol()
	if ps.TotalRuns != 1 || ps.SinceAt == "" {
		t.Fatalf("首次运行应建立基线（TotalRuns=1 且有 since_at）：%+v", ps)
	}
}

// ② 结论不变 → 不产生跃迁（不刷屏）
func TestPatrolNoTransitionWhenSame(t *testing.T) {
	st := newTestState(t)
	st.applyPatrol(report(okItem()), "timer")
	for i := 0; i < 5; i++ {
		if tr, _ := st.applyPatrol(report(okItem()), "timer"); tr != nil {
			t.Fatalf("结论未变不该产生跃迁：%+v", tr)
		}
	}
	if ps := st.loadPatrol(); len(ps.Transitions) != 0 {
		t.Fatalf("不该有跃迁历史：%+v", ps.Transitions)
	}
}

// ③ 好 → 坏 → 好：problem 与 recovered 各一条
func TestPatrolProblemThenRecovered(t *testing.T) {
	st := newTestState(t)
	st.applyPatrol(report(okItem()), "timer") // 基线

	tr, payload := st.applyPatrol(report(warnItem()), "timer")
	if tr == nil || tr.Kind != "problem" {
		t.Fatalf("出现异常应报 problem：%+v", tr)
	}
	if payload["problems"] == nil {
		t.Fatalf("跃迁载荷应带异常项：%+v", payload)
	}
	if ps := st.loadPatrol(); ps.ConsecProblems != 1 {
		t.Fatalf("连续问题计数应为 1：%+v", ps)
	}

	tr2, _ := st.applyPatrol(report(okItem()), "timer")
	if tr2 == nil || tr2.Kind != "recovered" {
		t.Fatalf("恢复应报 recovered：%+v", tr2)
	}
	if ps := st.loadPatrol(); ps.ConsecProblems != 0 {
		t.Fatalf("恢复后连续计数归零：%+v", ps)
	}
}

// ④ known（已知且接受）不算异常：历史账本损坏不该让巡警永远亮黄
func TestPatrolKnownIsNotProblem(t *testing.T) {
	st := newTestState(t)
	st.applyPatrol(report(okItem(), knownItem()), "timer")
	if tr, _ := st.applyPatrol(report(okItem(), knownItem()), "timer"); tr != nil {
		t.Fatalf("known 不该被当成问题（否则永远测不出恢复）：%+v", tr)
	}
	ps := st.loadPatrol()
	if len(ps.Problems) != 0 {
		t.Fatalf("known 不该进入 problems：%+v", ps.Problems)
	}
	if ps.Known != 1 {
		t.Fatalf("known 应单独计数：%+v", ps)
	}
}

// ⑤ 待补录队列必须被继承（今天踩过的坑：每轮检查把没送出去的报警清空了）
func TestPatrolPendingQueueSurvivesRuns(t *testing.T) {
	st := newTestState(t)
	st.applyPatrol(report(okItem()), "timer")
	// 造一条"投递失败的报警"
	st.enqueuePatrol(map[string]any{"kind": "problem", "at": "2026-09-12T20:04:20+08:00", "summary": "测试"})
	if ps := st.loadPatrol(); len(ps.PendingPublish) != 1 {
		t.Fatalf("入队失败：%+v", ps.PendingPublish)
	}
	// 再跑几轮检查（结论变化触发保存）——队列必须还在
	st.applyPatrol(report(warnItem()), "timer")
	st.applyPatrol(report(okItem()), "timer")
	ps := st.loadPatrol()
	if len(ps.PendingPublish) != 1 {
		t.Fatalf("待补录队列被清空了（回归！）：%+v", ps.PendingPublish)
	}
	// 落盘也应在（重启不丢）
	raw, err := os.ReadFile(st.patrolPath())
	if err != nil {
		t.Fatal(err)
	}
	var onDisk patrolState
	if json.Unmarshal(raw, &onDisk) != nil || len(onDisk.PendingPublish) != 1 {
		t.Fatalf("待补录应落盘：%+v", onDisk.PendingPublish)
	}
}
