// 本文件：单实例锁（Windows 命名互斥体实现）。
//
// 为什么必须有（真实事故，2026-09-12）：
//
//	账本是平台的"唯一真相源"，采用追加写 + 单调 seq。一旦两个 core 同时写同一个账本文件，
//	两边各自从文件末尾读到的 seq 起点相同 → seq 交叉重复（当天实测 924/925 各写两次、
//	校验器报 925→937 缺口）。这类损坏是**静默**的：进程不报错、事件照发，只有校验时才发现。
//	事故根因：控制台"重启"时，看门狗只等 /healthz 不通就拉新实例，而旧实例此时还在
//	收尾（关插件、关 Job、刷账本），HTTP 服务已经先停了。
//
// 因此：core 启动第一件事就是抢这把命名互斥体，抢不到 = 已经有 core 在跑 → 直接退出，
// 绝不打开账本。看门狗拉新实例前也先探测这把锁，确保旧实例"真的"退了。
package singlelock

import (
	"fmt"
	"syscall"
	"unsafe"
)

var (
	kernel32     = syscall.NewLazyDLL("kernel32.dll")
	procMutex    = kernel32.NewProc("CreateMutexW")
	procCloseH   = kernel32.NewProc("CloseHandle")
	errAlready   = syscall.Errno(183) // ERROR_ALREADY_EXISTS
	globalPrefix = "Local\\"          // Local\ = 当前会话（单用户桌面够用，且不需要管理员权限）
)

// Lock 持有的单实例锁
type Lock struct {
	handle syscall.Handle
	name   string
}

// Acquire 尝试获取名为 name 的单实例锁；已被占用时返回错误。
func Acquire(name string) (*Lock, error) {
	full := globalPrefix + name
	p, err := syscall.UTF16PtrFromString(full)
	if err != nil {
		return nil, err
	}
	h, _, callErr := procMutex.Call(0, 0, uintptr(unsafe.Pointer(p)))
	if h == 0 {
		return nil, fmt.Errorf("创建互斥体 %s 失败: %v", full, callErr)
	}
	if callErr == errAlready {
		procCloseH.Call(h)
		return nil, fmt.Errorf("已有实例持有单实例锁 %s", full)
	}
	return &Lock{handle: syscall.Handle(h), name: full}, nil
}

// Release 释放锁（进程退出时内核也会自动释放）
func (l *Lock) Release() {
	if l == nil || l.handle == 0 {
		return
	}
	procCloseH.Call(uintptr(l.handle))
	l.handle = 0
}

// Busy 探测锁是否被别的进程持有（自己短暂持有后立刻释放）。
// 看门狗用它判断"旧实例是否真的退干净了"——比 /healthz 可靠：
// core 停机时 HTTP 服务先关，进程还要几秒才退。
func Busy(name string) bool {
	l, err := Acquire(name)
	if err != nil {
		return true
	}
	l.Release()
	return false
}
