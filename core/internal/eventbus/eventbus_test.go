// 本文件：总线回归测试——把 2026-09-11 晚上暴露的两个问题钉成用例。
//
// 被钉住的：
//
//	① 订阅者队列满 → 事件被丢弃，但**必须留痕**（platform.bus.dropped 事件），不能只剩一行日志
//	② 订阅者 panic → **不许带走 core**（panic 隔离），且必须留下 platform.bus.handler.panic
package eventbus

import (
	"context"
	"io"
	"log/slog"
	"sync"
	"testing"
	"time"

	"core/internal/plugin"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

type collector struct {
	mu   sync.Mutex
	seen []string
}

func (c *collector) add(t string) {
	c.mu.Lock()
	c.seen = append(c.seen, t)
	c.mu.Unlock()
}

func (c *collector) has(t string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, s := range c.seen {
		if s == t {
			return true
		}
	}
	return false
}

// TestDropIsRecorded 队列满丢弃必须事件化（今晚"静默丢账"的回归）
func TestDropIsRecorded(t *testing.T) {
	bus := New(4, quietLogger()) // 每订阅者队列只有 4 格
	col := &collector{}
	ctx := context.Background()

	// 收集器：只订阅总线自己的丢弃事件，处理极快
	if _, err := bus.SubscribeNamed(ctx, "collector", []string{"platform.bus.dropped"},
		func(_ context.Context, ev plugin.Event) error {
			col.add(ev.Type)
			return nil
		}); err != nil {
		t.Fatalf("subscribe collector: %v", err)
	}

	// 慢订阅者：永远不消费（把队列堵死）
	block := make(chan struct{})
	defer close(block)
	if _, err := bus.SubscribeNamed(ctx, "slow", nil, func(_ context.Context, _ plugin.Event) error {
		<-block
		return nil
	}); err != nil {
		t.Fatalf("subscribe slow: %v", err)
	}

	for i := 0; i < 60; i++ {
		_ = bus.Publish(ctx, plugin.NewEvent("test.event", "t", nil))
		time.Sleep(time.Millisecond) // 给收集器留出消费时间（它只有 4 格队列）
	}
	time.Sleep(200 * time.Millisecond)

	if !col.has("platform.bus.dropped") {
		t.Fatal("队列满丢弃必须发布 platform.bus.dropped（否则就是静默丢账）")
	}
}

// TestPanicIsolated 订阅者 panic 不许带走总线
func TestPanicIsolated(t *testing.T) {
	bus := New(16, quietLogger())
	col := &collector{}
	ctx := context.Background()

	if _, err := bus.SubscribeNamed(ctx, "collector", []string{"platform.bus.handler.panic"},
		func(_ context.Context, ev plugin.Event) error {
			col.add(ev.Type)
			return nil
		}); err != nil {
		t.Fatalf("subscribe collector: %v", err)
	}
	if _, err := bus.SubscribeNamed(ctx, "panicker", []string{"boom"},
		func(_ context.Context, _ plugin.Event) error { panic("boom") }); err != nil {
		t.Fatalf("subscribe panicker: %v", err)
	}

	got := make(chan string, 8)
	if _, err := bus.SubscribeNamed(ctx, "healthy", []string{"ok"},
		func(_ context.Context, ev plugin.Event) error {
			got <- ev.Type
			return nil
		}); err != nil {
		t.Fatalf("subscribe healthy: %v", err)
	}

	_ = bus.Publish(ctx, plugin.NewEvent("boom", "t", nil))
	time.Sleep(150 * time.Millisecond)
	_ = bus.Publish(ctx, plugin.NewEvent("ok", "t", nil)) // 总线在 panic 之后仍须可用

	select {
	case <-got:
	case <-time.After(2 * time.Second):
		t.Fatal("panic 之后总线不可用了（隔离失败）")
	}
	if !col.has("platform.bus.handler.panic") {
		t.Fatal("panic 必须留下 platform.bus.handler.panic 事件")
	}
}

// TestAppenderSeesEventBeforeFanout 先写后播：账本必须在扇出之前拿到事件
func TestAppenderSeesEventBeforeFanout(t *testing.T) {
	bus := New(16, quietLogger())
	app := &recordingAppender{}
	bus.SetAppender(app)

	fanout := make(chan struct{}, 1)
	if _, err := bus.SubscribeNamed(context.Background(), "sub", []string{"x"},
		func(_ context.Context, _ plugin.Event) error {
			if !app.sawType("x") {
				t.Error("扇出时账本还没拿到事件（先写后播被破坏）")
			}
			fanout <- struct{}{}
			return nil
		}); err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	_ = bus.Publish(context.Background(), plugin.NewEvent("x", "t", nil))
	select {
	case <-fanout:
	case <-time.After(2 * time.Second):
		t.Fatal("订阅者没收到事件")
	}
	if !app.sawType("x") {
		t.Fatal("账本应已拿到 x 事件")
	}
}

type recordingAppender struct {
	mu    sync.Mutex
	types []string
}

func (a *recordingAppender) Append(ev plugin.Event) {
	a.mu.Lock()
	a.types = append(a.types, ev.Type)
	a.mu.Unlock()
}

// sawType 账本是否已经拿到某类型事件（注意：SubscribeNamed 自己会发 subscriber.added，
// 所以这里按类型判断而不是按条数）
func (a *recordingAppender) sawType(t string) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	for _, s := range a.types {
		if s == t {
			return true
		}
	}
	return false
}
