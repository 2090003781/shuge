// Package eventbus 平台事件总线：发布/订阅/路由。
//
// 设计（2026-09-11 改造）：
//
//	· 先写后播：Publish 先把事件交给账本（Appender），再扇出到订阅者。
//	  账本 = 不可丢的真相源；扇出 = best-effort 的通知。
//	· 无界扇出 + 每订阅者独立缓冲队列 + 慢订阅者隔离（不阻塞发布者）
//	· 丢弃留痕：队列满不再只写日志（日志可能进 NUL），而是聚合成
//	  `platform.bus.dropped` 事件进账本——"丢了多少、丢给谁、什么类型"必有账
//	· panic 隔离：单个订阅者 panic 不再带走整个 core，记 `platform.bus.handler.panic`
//
// 依赖 plugin 包的类型定义（plugin 为纯接口，无反向依赖 → 无循环）。
package eventbus

import (
	"context"
	"fmt"
	"log/slog"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"core/internal/plugin"
)

const (
	dropSummaryEvery   = 5 * time.Second // 丢弃聚合上报窗口
	handlerNoticeEvery = time.Second     // 订阅者出错上报节流
	eventErrMaxLen     = 500             // 错误信息截断
)

// Appender 账本写入前置接口（由 internal/ledger 实现）。
// 放在这里是为了让总线"先写后播"，同时不反向依赖 ledger 包。
type Appender interface {
	Append(ev plugin.Event)
}

// subscriber 单个订阅者：独立队列 + 独立消费 goroutine
type subscriber struct {
	name    string // 订阅者名字（拓扑快照/可追溯用；"" = 匿名）
	types   map[string]struct{}
	queue   chan plugin.Event
	handler plugin.Handler
	ctx     context.Context
	cancel  context.CancelFunc
	bus     *Bus

	// 丢弃计数放在订阅者身上 + 原子操作：让"队列满"这条热路径不碰锁（2026-09-11 压测实证）
	dropped     atomic.Int64
	lastDropped atomic.Value // string：最近一次被丢弃的事件类型
}

// Bus 事件总线
type Bus struct {
	mu       sync.RWMutex
	subs     map[*subscriber]struct{}
	queueLen int
	log      *slog.Logger

	appender Appender // 账本前置（nil = 不记账）

	noticeMu             sync.Mutex
	pendingDrops         atomic.Int64 // 待上报的丢弃总数（热路径只做原子加）
	lastDropNotice       time.Time
	lastHandlerNotice    time.Time
	suppressedHandlerErr uint64
}

// New 创建总线。queueLen 为每订阅者缓冲长度（慢订阅者不阻塞发布者的关键）。
func New(queueLen int, log *slog.Logger) *Bus {
	if queueLen <= 0 {
		queueLen = 256
	}
	if log == nil {
		log = slog.Default()
	}
	return &Bus{
		subs:     make(map[*subscriber]struct{}),
		queueLen: queueLen,
		log:      log,
	}
}

// SetAppender 注入账本（先写后播的唯一入口；main 装配时调用）
func (b *Bus) SetAppender(a Appender) {
	b.mu.Lock()
	b.appender = a
	b.mu.Unlock()
}

// Publish 发布事件：① 先交账本（关键事件同步落盘）② 非阻塞扇出到匹配订阅者。
// 订阅者队列满（消费过慢）→ 丢弃并**计数**，由 flushDrops 聚合成事件进账本。
func (b *Bus) Publish(ctx context.Context, event plugin.Event) error {
	// ① 先写后播：不持锁调用，避免记账耗时阻塞其他发布者
	b.mu.RLock()
	app := b.appender
	b.mu.RUnlock()
	if app != nil {
		app.Append(event)
	}

	// ② 扇出（best-effort）
	b.mu.RLock()
	for s := range b.subs {
		if !s.matches(event.Type) {
			continue
		}
		select {
		case s.queue <- event:
		default:
			b.noteDrop(s, event.Type)
		}
	}
	b.mu.RUnlock()

	// ③ 惰性聚合上报丢弃（无需额外 goroutine；有流量就有信号）
	b.flushDrops()
	return nil
}

// Subscribe 订阅（匿名）。返回取消函数。
func (b *Bus) Subscribe(ctx context.Context, types []string, h plugin.Handler) (func(), error) {
	return b.SubscribeNamed(ctx, "", types, h)
}

// SubscribeNamed 命名订阅：name 用于拓扑快照/可追溯（"谁订阅了哪些事件"）。
// types 为空表示订阅全部事件。
func (b *Bus) SubscribeNamed(ctx context.Context, name string, types []string, h plugin.Handler) (func(), error) {
	s := &subscriber{
		name:    name,
		types:   make(map[string]struct{}),
		queue:   make(chan plugin.Event, b.queueLen),
		handler: h,
		bus:     b,
	}
	for _, t := range types {
		s.types[t] = struct{}{}
	}
	s.ctx, s.cancel = context.WithCancel(ctx)

	b.mu.Lock()
	b.subs[s] = struct{}{}
	b.mu.Unlock()

	go s.run()
	if name != "" {
		b.Publish(context.Background(), plugin.NewEvent("platform.bus.subscriber.added", "eventbus",
			map[string]any{"name": name, "types": types, "queue_len": b.queueLen}))
	}
	return func() {
		s.cancel()
		b.mu.Lock()
		delete(b.subs, s)
		b.mu.Unlock()
		if name != "" {
			b.Publish(context.Background(), plugin.NewEvent("platform.bus.subscriber.removed", "eventbus",
				map[string]any{"name": name}))
		}
	}, nil
}

// SubscriberInfo 订阅者信息（拓扑快照用）
type SubscriberInfo struct {
	Name  string   `json:"name"`  // 订阅者名（"" = 匿名）
	Types []string `json:"types"` // 订阅的事件类型（空 = 全部）
}

// Subscribers 列出全部订阅者（含名字与订阅类型），供事件拓扑快照查询
func (b *Bus) Subscribers() []SubscriberInfo {
	b.mu.RLock()
	defer b.mu.RUnlock()
	out := make([]SubscriberInfo, 0, len(b.subs))
	for s := range b.subs {
		types := make([]string, 0, len(s.types))
		for t := range s.types {
			types = append(types, t)
		}
		out = append(out, SubscriberInfo{Name: s.name, Types: types})
	}
	return out
}

// SubscriberCount 订阅者数量（监控用）
func (b *Bus) SubscriberCount() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return len(b.subs)
}

func (s *subscriber) matches(typ string) bool {
	if len(s.types) == 0 {
		return true
	}
	_, ok := s.types[typ]
	return ok
}

func (s *subscriber) run() {
	for {
		select {
		case <-s.ctx.Done():
			return
		case ev := <-s.queue:
			if err := s.invoke(ev); err != nil {
				s.bus.log.Error("eventbus: handler error", "sub", s.name, "type", ev.Type, "err", err)
				s.bus.noteHandlerError(s.name, ev.Type, err)
			}
		}
	}
}

// invoke 调用订阅者 handler，并兜住 panic——
// 单个订阅者崩掉不许带走整个 core（故障隔离从"错误隔离"补齐到"panic 隔离"）。
func (s *subscriber) invoke(ev plugin.Event) (err error) {
	defer func() {
		if r := recover(); r != nil {
			s.bus.log.Error("eventbus: handler panic (已隔离)", "sub", s.name, "type", ev.Type, "panic", r)
			s.bus.noteHandlerPanic(s.name, ev.Type, r)
			err = fmt.Errorf("panic: %v", r)
		}
	}()
	return s.handler(s.ctx, ev)
}

// ---- 失败留痕 ----

// noteDrop 记录一次丢弃（热路径：只做原子操作，不碰锁）
func (b *Bus) noteDrop(s *subscriber, typ string) {
	s.dropped.Add(1)
	s.lastDropped.Store(typ)
	b.pendingDrops.Add(1)
}

// flushDrops 把累计的丢弃聚合成 `platform.bus.dropped` 事件（节流 5s）。
// 无丢弃时零开销：一次原子读直接返回——发布路径不该为"没发生的事"付锁钱。
func (b *Bus) flushDrops() {
	if b.pendingDrops.Load() == 0 {
		return
	}
	b.noticeMu.Lock()
	if time.Since(b.lastDropNotice) < dropSummaryEvery {
		b.noticeMu.Unlock()
		return
	}
	b.lastDropNotice = time.Now()
	b.noticeMu.Unlock()

	type dropItem struct {
		name  string
		typ   string
		count int64
	}
	items := []dropItem{}
	var sum int64
	b.mu.RLock()
	for s := range b.subs {
		if n := s.dropped.Swap(0); n > 0 {
			typ, _ := s.lastDropped.Load().(string)
			items = append(items, dropItem{name: s.name, typ: typ, count: n})
			sum += n
		}
	}
	b.mu.RUnlock()
	if sum > 0 {
		b.pendingDrops.Add(-sum)
	}
	for _, it := range items {
		b.Publish(context.Background(), plugin.NewEvent("platform.bus.dropped", "eventbus",
			map[string]any{"subscriber": it.name, "type": it.typ, "count": it.count}))
	}
}

// noteHandlerError 订阅者出错：上报（节流 1s），避免高频错误淹没账本
func (b *Bus) noteHandlerError(sub, typ string, err error) {
	b.emitHandlerNotice(sub, typ, map[string]any{"err": clip(err.Error(), eventErrMaxLen)}, "platform.bus.handler.error")
}

// noteHandlerPanic 订阅者 panic：上报（节流 1s）
func (b *Bus) noteHandlerPanic(sub, typ string, r any) {
	b.emitHandlerNotice(sub, typ, map[string]any{"panic": clip(fmt.Sprint(r), eventErrMaxLen)}, "platform.bus.handler.panic")
}

func (b *Bus) emitHandlerNotice(sub, typ string, extra map[string]any, evType string) {
	// 防自激：总线自身事件出错不再产生新事件
	if strings.HasPrefix(typ, "platform.bus.") {
		return
	}
	b.noticeMu.Lock()
	now := time.Now()
	if now.Sub(b.lastHandlerNotice) < handlerNoticeEvery {
		b.suppressedHandlerErr++
		b.noticeMu.Unlock()
		return
	}
	suppressed := b.suppressedHandlerErr
	b.suppressedHandlerErr = 0
	b.lastHandlerNotice = now
	b.noticeMu.Unlock()

	payload := map[string]any{"subscriber": sub, "type": typ}
	for k, v := range extra {
		payload[k] = v
	}
	if suppressed > 0 {
		payload["suppressed_since_last"] = suppressed
	}
	b.Publish(context.Background(), plugin.NewEvent(evType, "eventbus", payload))
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "…"
}
