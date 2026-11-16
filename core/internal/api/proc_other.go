//go:build !windows

package api

import "os/exec"

// hideWindow 非 Windows 平台无事可做（编译期占位，保证跨平台构建）
func hideWindow(cmd *exec.Cmd) {}
