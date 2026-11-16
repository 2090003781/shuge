//go:build windows

package api

import (
	"os/exec"
	"syscall"
)

// hideWindow 让子进程不创建可见的控制台窗口（不设的话弹终端黑框）。
// CreateNoWindow + HideWindow 是 Windows 上启动控制台程序的标准静默组合。
func hideWindow(cmd *exec.Cmd) {
	// syscall.SysProcAttr（Windows）只有 HideWindow —— CreateNoWindow 是 os/exec.Cmd 的字段，这里不存在
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
}
