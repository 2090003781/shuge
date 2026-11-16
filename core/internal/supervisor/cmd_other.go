//go:build !windows

package supervisor

import "os/exec"

// prepareCmd 非 Windows 平台无「控制台窗口」概念，无需处理（保持跨平台可编译）。
func prepareCmd(cmd *exec.Cmd) {}
