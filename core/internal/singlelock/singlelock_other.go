//go:build !windows

// 本文件：单实例锁的非 Windows 兜底实现（平台实际运行环境是 Windows，见 singlelock_windows.go）。
//
// 用"锁文件 + O_EXCL"近似命名互斥体语义：文件存在 = 有实例（进程崩溃会残留，
// 因此非 Windows 下只作为开发期保护，不作为生产保障）。
package singlelock

import (
	"fmt"
	"os"
	"path/filepath"
)

// Lock 持有的单实例锁
type Lock struct {
	path string
}

// Acquire 尝试获取名为 name 的单实例锁；已被占用时返回错误。
func Acquire(name string) (*Lock, error) {
	dir := filepath.Join(os.TempDir(), "core-locks")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	p := filepath.Join(dir, name+".lock")
	f, err := os.OpenFile(p, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		if os.IsExist(err) {
			return nil, fmt.Errorf("已有实例持有单实例锁 %s", p)
		}
		return nil, err
	}
	_, _ = fmt.Fprintf(f, "%d\n", os.Getpid())
	_ = f.Close()
	return &Lock{path: p}, nil
}

// Release 释放锁
func (l *Lock) Release() {
	if l == nil || l.path == "" {
		return
	}
	_ = os.Remove(l.path)
	l.path = ""
}

// Busy 探测锁是否被别的进程持有
func Busy(name string) bool {
	l, err := Acquire(name)
	if err != nil {
		return true
	}
	l.Release()
	return false
}
