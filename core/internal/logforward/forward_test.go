// 本文件：日志转发的回归测试（2026-09-12 补测试网）。
//
// 钉住的契约（今天全靠手工验过，都是"错了会静默丢日志"的路径）：
//  1. 正常：行被转给日志插件（内核不自己写）
//  2. 插件不可达：**本地兜底直写**，且行内标注 [兜底写入]（日志绝不丢）
//  3. 日志插件自己的输出：直接写，不转发给自己（否则成环）
//  4. 通道没装配（内核刚启动那会儿）：同样走兜底并标原因
//  5. 队列满了：丢最旧并计数，绝不阻塞子进程
package logforward

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// fakeFwd 记录转发过来的批次；可设成失败（模拟日志插件不可达）
type fakeFwd struct {
	mu      sync.Mutex
	batches [][]string
	sources []string
	fail    bool
}

func (f *fakeFwd) call(_ context.Context, _ string, _ string, args map[string]any, _ time.Duration) (map[string]any, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	lines, _ := args["lines"].([]string)
	src, _ := args["source"].(string)
	f.batches = append(f.batches, lines)
	f.sources = append(f.sources, src)
	if f.fail {
		return nil, errors.New("plugin down")
	}
	return map[string]any{"ok": true}, nil
}

func (f *fakeFwd) lines() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	n := 0
	for _, b := range f.batches {
		n += len(b)
	}
	return n
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(b)
}

// ① 正常转发：行交给插件，内核不落盘
func TestForwardToPlugin(t *testing.T) {
	dir := t.TempDir()
	fc := &fakeFwd{}
	s := New(dir, "plugin.demo", 1<<20, 3, fc.call, "plugin.log")

	if _, err := s.Write([]byte("第一行\n第二行\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool { return fc.lines() >= 2 }, 3*time.Second)

	if got := readFile(t, filepath.Join(dir, "plugin.demo.log")); got != "" {
		t.Fatalf("转发成功时内核不该自己落盘，却写入了：%q", got)
	}
	if fc.sources[0] != "plugin.demo" {
		t.Fatalf("转发应带来源：%v", fc.sources)
	}
	_ = s.Close()
}

// ② 插件不可达 → 本地兜底直写，并标注原因（日志绝不丢）
func TestFallbackWhenPluginDown(t *testing.T) {
	dir := t.TempDir()
	fc := &fakeFwd{fail: true}
	s := New(dir, "plugin.demo", 1<<20, 3, fc.call, "plugin.log")

	if _, err := s.Write([]byte("崩了也要记下来\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool {
		return strings.Contains(readFile(t, filepath.Join(dir, "plugin.demo.log")), "崩了也要记下来")
	}, 5*time.Second)

	content := readFile(t, filepath.Join(dir, "plugin.demo.log"))
	if !strings.Contains(content, "[兜底写入]") {
		t.Fatalf("兜底写入必须标注原因（否则和正常路径分不出来）：%q", content)
	}
	if !strings.Contains(content, "| plugin.demo |") {
		t.Fatalf("兜底也要保持统一行格式：%q", content)
	}
	_ = s.Close()
}

// ③ 日志插件自己的输出：直接写，不转发（避免成环）
func TestSelfSourceWritesDirectly(t *testing.T) {
	dir := t.TempDir()
	fc := &fakeFwd{}
	s := New(dir, "plugin.log", 1<<20, 3, fc.call, "plugin.log")

	if _, err := s.Write([]byte("日志插件自己的输出\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool {
		return strings.Contains(readFile(t, filepath.Join(dir, "plugin.log.log")), "日志插件自己的输出")
	}, 3*time.Second)

	if fc.lines() != 0 {
		t.Fatalf("日志插件自己的输出不该转发给自己：%v", fc.batches)
	}
	content := readFile(t, filepath.Join(dir, "plugin.log.log"))
	if strings.Contains(content, "[兜底写入]") {
		t.Fatalf("自己写自己不是兜底，不该标兜底：%q", content)
	}
	_ = s.Close()
}

// ④ 通道未装配（内核启动早期）→ 走兜底并标注
func TestFallbackWhenForwarderMissing(t *testing.T) {
	dir := t.TempDir()
	s := New(dir, "plugin.demo", 1<<20, 3, nil, "plugin.log")
	if _, err := s.Write([]byte("通道还没装好\n")); err != nil {
		t.Fatal(err)
	}
	waitFor(t, func() bool {
		return strings.Contains(readFile(t, filepath.Join(dir, "plugin.demo.log")), "通道还没装好")
	}, 3*time.Second)
	if c := readFile(t, filepath.Join(dir, "plugin.demo.log")); !strings.Contains(c, "[兜底写入]") {
		t.Fatalf("未装配通道时的兜底也要标注：%q", c)
	}
	_ = s.Close()
}

// ⑤ 队列满：丢最旧并计数，Write 永不阻塞（子进程不能被日志拖死）
func TestQueueOverflowDropsOldestWithoutBlocking(t *testing.T) {
	dir := t.TempDir()
	fc := &fakeFwd{}
	s := New(dir, "plugin.demo", 1<<20, 3, fc.call, "plugin.log")
	// 用一个从不消费的通道制造积压：直接把 fwd 换成慢速阻塞
	block := make(chan struct{})
	s.fwd = func(ctx context.Context, pluginID, tool string, args map[string]any, d time.Duration) (map[string]any, error) {
		<-block
		return map[string]any{"ok": true}, nil
	}

	for i := 0; i < queueMax+500; i++ {
		if _, err := s.Write([]byte("line\n")); err != nil {
			t.Fatalf("Write 不该报错（最多丢行）：%v", err)
		}
	}
	st := s.Stats()
	if st["dropped"].(int64) == 0 {
		t.Fatalf("超容量应记丢弃数：%+v", st)
	}
	close(block)
	_ = s.Close()
}

func waitFor(t *testing.T, cond func() bool, d time.Duration) {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("等待条件超时")
}
