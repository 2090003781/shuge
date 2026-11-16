// Package logforward 内核侧日志转发器：**只做搬运与兜底**，日志的格式化/分级/轮转/落盘归插件。
//
// 为什么采集端必须留在内核（2026-09-12 检修 · 日志记录插件化）：
//
//	Windows 上子进程的 stdout/stderr 只能由**父进程**持有（控制台只能继承/自建/ConPTY）。
//	所以"谁采集"没有选择：supervisor 是插件的父进程，管道只能在它手里。
//	能外移的是"**记什么、怎么记**"——统一行格式、级别推断、轮转、落盘、查询，
//	这些现在由独立插件 plugin.log 承担；内核只剩：
//	  ① 把行原样转发过去（平台内部调用通道，不经过网络栈）
//	  ② 插件不可达时**本地兜底直写**（日志绝不因为插件挂了而丢）
//
// 设计取舍（为什么不用"把管道交给插件"）：
//
//	那要求插件先于被观察者启动，且插件一崩全部日志断流——比转发更脆。
//	转发 + 兜底是"独立进程"与"不丢日志"之间的平衡点：插件握着记录权，内核握着兜底权。
package logforward

import (
	"context"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"core/internal/logging"
)

// Forwarder 平台 → 插件 的调用（由 api 层的 CallPlugin 实现，main 装配时注入）
type Forwarder func(ctx context.Context, pluginID, tool string, args map[string]any, timeout time.Duration) (map[string]any, error)

const (
	DefaultPluginID = "plugin.log"
	AppendTool      = "log.append"

	flushEvery   = 250 * time.Millisecond
	flushMax     = 200  // 单次最多转多少行
	queueMax     = 4000 // 队列上限（满了丢最旧并计数：宁可丢日志也不阻塞子进程）
	callTimeout  = 5 * time.Second
	cooldownDown = 30 * time.Second // 插件不可达后的冷却：期间直接走兜底，不再每批都等超时
)

// Sink 单个来源的日志转发器（实现 io.Writer，交给 exec.Cmd 的 Stdout/Stderr）
type Sink struct {
	source   string
	fallback *logging.RotatingWriter
	pluginID string
	fwd      Forwarder

	mu        sync.Mutex
	queue     []string
	dropped   int64
	closed    bool
	retryAt   time.Time
	fallbackN int64
	fwdN      int64
	wake      chan struct{}
	done      chan struct{}
}

// New 创建转发器。fwd 为 nil 时（未装配）直接走兜底直写。
func New(dir, source string, maxBytes int64, keep int, fwd Forwarder, pluginID string) *Sink {
	if pluginID == "" {
		pluginID = DefaultPluginID
	}
	s := &Sink{
		source:   source,
		fallback: logging.NewRotatingWriter(filepath.Join(dir, safeName(source)+".log"), maxBytes, keep),
		pluginID: pluginID,
		fwd:      fwd,
		wake:     make(chan struct{}, 1),
		done:     make(chan struct{}),
	}
	go s.loop()
	return s
}

// Write 实现 io.Writer：只做"拆行 + 入队"，绝不阻塞子进程
func (s *Sink) Write(p []byte) (int, error) {
	if len(p) == 0 {
		return 0, nil
	}
	lines := splitLines(string(p))
	s.mu.Lock()
	if s.closed {
		s.mu.Unlock()
		return len(p), nil
	}
	s.queue = append(s.queue, lines...)
	if len(s.queue) > queueMax {
		over := len(s.queue) - queueMax
		s.queue = s.queue[over:]
		s.dropped += int64(over)
	}
	s.mu.Unlock()
	select {
	case s.wake <- struct{}{}:
	default:
	}
	return len(p), nil
}

// Close 冲干净队列并关闭兜底写入器
func (s *Sink) Close() error {
	s.mu.Lock()
	s.closed = true
	s.mu.Unlock()
	select {
	case s.wake <- struct{}{}:
	default:
	}
	select {
	case <-s.done:
	case <-time.After(3 * time.Second):
	}
	return s.fallback.Close()
}

// Stats 转发/兜底/丢弃计数（状态可见性）
func (s *Sink) Stats() map[string]any {
	s.mu.Lock()
	defer s.mu.Unlock()
	return map[string]any{
		"forwarded": s.fwdN, "fallback": s.fallbackN, "dropped": s.dropped,
		"pending": len(s.queue), "cooldown_ms": maxInt64(0, time.Until(s.retryAt).Milliseconds()),
	}
}

func (s *Sink) loop() {
	t := time.NewTicker(flushEvery)
	defer t.Stop()
	defer close(s.done)
	for {
		select {
		case <-s.wake:
		case <-t.C:
		}
		s.flush()
		s.mu.Lock()
		closed := s.closed
		s.mu.Unlock()
		if closed {
			s.flush() // 退出前再冲一次
			return
		}
	}
}

func (s *Sink) flush() {
	s.mu.Lock()
	if len(s.queue) == 0 {
		s.mu.Unlock()
		return
	}
	n := len(s.queue)
	if n > flushMax {
		n = flushMax
	}
	batch := s.queue[:n]
	s.queue = s.queue[n:]
	inCooldown := time.Now().Before(s.retryAt)
	fwd, pluginID := s.fwd, s.pluginID
	s.mu.Unlock()

	// 兜底条件：没装配通道 / 插件刚失败过（冷却期）/ 这个来源就是日志插件自己（避免自己转发给自己成环）
	// 兜底条件：没装配通道 / 插件刚失败过（冷却期）→ 标注原因直写；
	// 例外是这个来源就是日志插件自己：它自己的输出按设计直写（不转发给自己，避免成环）。
	if s.source == pluginID {
		s.writeFallback(batch, "")
		return
	}
	if fwd == nil {
		s.writeFallback(batch, "转发通道未装配")
		return
	}
	if inCooldown {
		s.writeFallback(batch, "日志插件冷却中")
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), callTimeout)
	defer cancel()
	args := map[string]any{"source": s.source, "lines": batch}
	if _, err := fwd(ctx, pluginID, AppendTool, args, callTimeout); err != nil {
		s.mu.Lock()
		s.retryAt = time.Now().Add(cooldownDown)
		s.mu.Unlock()
		s.writeFallback(batch, err.Error())
		return
	}
	s.mu.Lock()
	s.fwdN += int64(len(batch))
	s.mu.Unlock()
}

// writeFallback 兜底直写：格式统一，行内标注「兜底写入」原因，便于事后分辨
func (s *Sink) writeFallback(lines []string, reason string) {
	ts := time.Now().Format(time.RFC3339)
	var b strings.Builder
	for _, l := range lines {
		b.WriteString(ts)
		b.WriteString(" | ")
		b.WriteString(levelOf(l))
		b.WriteString(" | ")
		b.WriteString(s.source)
		b.WriteString(" | ")
		if reason != "" {
			b.WriteString("[兜底写入] ")
		}
		b.WriteString(l)
		b.WriteByte('\n')
	}
	if _, err := s.fallback.Write([]byte(b.String())); err == nil {
		s.mu.Lock()
		s.fallbackN += int64(len(lines))
		s.mu.Unlock()
	}
}

// levelOf 级别粗判（插件侧也有一份；兜底时用它，保证格式不因兜底而变）
func levelOf(line string) string {
	l := strings.ToLower(line)
	switch {
	case strings.Contains(l, "traceback") || strings.Contains(l, "error") ||
		strings.Contains(l, "exception") || strings.Contains(l, "failed") ||
		strings.Contains(line, "失败") || strings.Contains(line, "错误") || strings.Contains(line, "异常"):
		return "ERROR"
	case strings.Contains(l, "warn") || strings.Contains(line, "警告") || strings.Contains(line, "降级"):
		return "WARN "
	default:
		return "INFO "
	}
}

func splitLines(text string) []string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.TrimRight(text, "\n")
	if text == "" {
		return nil
	}
	out := strings.Split(text, "\n")
	res := make([]string, 0, len(out))
	for _, l := range out {
		if strings.TrimSpace(l) != "" {
			res = append(res, l)
		}
	}
	return res
}

func safeName(s string) string {
	var b strings.Builder
	for _, r := range s {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '.', r == '-', r == '_':
			b.WriteRune(r)
		default:
			b.WriteRune('_')
		}
	}
	if b.Len() == 0 {
		return "unknown"
	}
	return b.String()
}

func maxInt64(a, b int64) int64 {
	if a > b {
		return a
	}
	return b
}
