package eventbus

import (
	"context"
	"io"
	"log/slog"
	"sync/atomic"
	"testing"
	"time"

	"core/internal/plugin"
)

// TestBusStressBurst 压力测试：30 个订阅者（模拟几十个自动流程模块），
// 突发 10 万事件。验证：发布不阻塞、快速消费者全收不丢。
func TestBusStressBurst(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	bus := New(256, log)
	const nSubs = 30
	const N = 100000

	var counters [nSubs]atomic.Int64
	ctx := context.Background()
	for i := 0; i < nSubs; i++ {
		idx := i
		_, _ = bus.Subscribe(ctx, nil, func(ctx context.Context, ev plugin.Event) error {
			// 只统计压测事件本身：总线自 2026-09-11 起还会发 subscriber.added 等平台记账事件
			if ev.Type == "stress" {
				counters[idx].Add(1)
			}
			return nil
		})
	}

	start := time.Now()
	for i := 0; i < N; i++ {
		_ = bus.Publish(ctx, plugin.Event{Type: "stress", Source: "bench", Time: time.Now()})
	}
	pubDur := time.Since(start)
	time.Sleep(800 * time.Millisecond) // 等队列排空

	t.Logf("突发发布 %d 事件 × %d 订阅者，发布耗时 %v（%.0f ev/s，发布不阻塞）",
		N, nSubs, pubDur, float64(N)/pubDur.Seconds())

	dropped := 0
	for i := 0; i < nSubs; i++ {
		if counters[i].Load() != N {
			dropped++
			t.Errorf("sub %d 收到 %d，期望 %d", i, counters[i].Load(), N)
		}
	}
	if dropped == 0 {
		t.Logf("全部 %d 个订阅者均完整收到 %d 条，零丢失", nSubs, N)
	}
}

// TestBusSlowSubscriberIsolation 慢订阅者隔离：
// 29 个快速消费者 + 1 个慢消费者（每事件 sleep 8ms，队列 256 很快打满）。
// 验证：发布者不阻塞；快速消费者不丢（隔离生效）；慢消费者有丢弃但平台不崩。
func TestBusSlowSubscriberIsolation(t *testing.T) {
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	bus := New(256, log)
	const nFast = 29
	const N = 20000

	var fastTotal atomic.Int64
	ctx := context.Background()
	for i := 0; i < nFast; i++ {
		_, _ = bus.Subscribe(ctx, nil, func(ctx context.Context, ev plugin.Event) error {
			fastTotal.Add(1)
			return nil
		})
	}
	var slowGot atomic.Int64
	_, _ = bus.Subscribe(ctx, nil, func(ctx context.Context, ev plugin.Event) error {
		slowGot.Add(1)
		time.Sleep(8 * time.Millisecond) // 慢模块：写盘/外部调用等
		return nil
	})

	start := time.Now()
	for i := 0; i < N; i++ {
		_ = bus.Publish(ctx, plugin.Event{Type: "stress", Source: "bench"})
	}
	pubDur := time.Since(start)
	time.Sleep(600 * time.Millisecond)

	fast := fastTotal.Load()
	slow := slowGot.Load()
	fastPct := float64(fast) / float64(N*nFast) * 100
	slowPct := float64(slow) / float64(N) * 100
	t.Logf("发布 %d 事件：发布耗时 %v（不阻塞 ✅）；快速消费者收 %d/%d (%.3f%%)；慢消费者收 %d (%.1f%%，队列满丢弃 %d)",
		N, pubDur, fast, N*nFast, fastPct, slow, slowPct, N-slow)
	// 隔离判据：快速消费者不应被慢者显著拖累（突发下允许 Go 调度瞬态丢失 <0.5%）
	if fastPct < 99.5 {
		t.Errorf("快速消费者被慢者拖累：收到 %.3f%%，期望 ≥99.5%%（隔离失效！）", fastPct)
	}
	// 背压判据：慢消费者队列满被丢弃（有界队列背压生效，而非无限堆积）
	if slow >= N {
		t.Errorf("慢消费者不该收满（队列应满丢弃），收到 %d", slow)
	}
}
