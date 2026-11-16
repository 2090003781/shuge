// Package logging 平台日志落盘（自持）。
//
// 为什么需要（2026-09-11 实证）：core 的 slog 原本写 os.Stderr，而 core 常被
// detached + stdio:ignore 拉起 → **stderr = NUL**，于是"平台自己的抱怨"全部人间蒸发
// （实证：11:16 起的 core 活了 8 小时，data/core-run.log 一行没有）。
// 结论：**日志的落地必须由平台自己负责**，不能依赖拉起者给了什么 stdio。
//
// 本文件提供零依赖的按大小轮转写入器：写满即改名轮转，保留最近 keep 个。
package logging

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sync"
)

// RotatingWriter 按大小轮转的文件写入器（线程安全）
type RotatingWriter struct {
	mu       sync.Mutex
	path     string
	maxBytes int64
	keep     int
	file     *os.File
	size     int64
}

// NewRotatingWriter 创建轮转写入器（maxBytes<=0 → 32MB；keep<=0 → 5）
func NewRotatingWriter(path string, maxBytes int64, keep int) *RotatingWriter {
	if maxBytes <= 0 {
		maxBytes = 32 << 20
	}
	if keep <= 0 {
		keep = 5
	}
	w := &RotatingWriter{path: path, maxBytes: maxBytes, keep: keep}
	return w
}

// Write 实现 io.Writer
func (w *RotatingWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		if err := w.open(); err != nil {
			return 0, err
		}
	}
	if w.size+int64(len(p)) > w.maxBytes {
		w.rotate()
	}
	n, err := w.file.Write(p)
	w.size += int64(n)
	return n, err
}

// Close 关闭当前文件
func (w *RotatingWriter) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file == nil {
		return nil
	}
	err := w.file.Close()
	w.file = nil
	return err
}

func (w *RotatingWriter) open() error {
	if err := os.MkdirAll(filepath.Dir(w.path), 0o755); err != nil {
		return fmt.Errorf("log mkdir: %w", err)
	}
	f, err := os.OpenFile(w.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return fmt.Errorf("log open: %w", err)
	}
	w.file = f
	if st, err := f.Stat(); err == nil {
		w.size = st.Size()
	}
	return nil
}

// rotate 轮转：core.log → core.log.1 → … → core.log.keep（最老的丢弃）
func (w *RotatingWriter) rotate() {
	if w.file != nil {
		_ = w.file.Sync()
		_ = w.file.Close()
		w.file = nil
	}
	for i := w.keep - 1; i >= 1; i-- {
		old := fmt.Sprintf("%s.%d", w.path, i)
		next := fmt.Sprintf("%s.%d", w.path, i+1)
		if _, err := os.Stat(old); err == nil {
			_ = os.Rename(old, next)
		}
	}
	_ = os.Rename(w.path, w.path+".1")
	w.size = 0
	_ = w.open()
}

// BestEffort 包一层"写失败也不报错"的 writer。
// 用途：core 的日志主落盘是文件，stderr 只是兜底——而 detached 拉起时 stderr 可能是
// 无效句柄；io.MultiWriter 遇到第一个错误就会中断后续写入，必须先把 stderr 变成不会失败的 writer。
type bestEffort struct{ w io.Writer }

// BestEffort 返回不返回错误的包装 writer
func BestEffort(w io.Writer) io.Writer { return bestEffort{w: w} }

func (b bestEffort) Write(p []byte) (int, error) {
	if b.w != nil {
		_, _ = b.w.Write(p) // 刻意忽略错误：兜底通道不允许影响主通道
	}
	return len(p), nil
}
