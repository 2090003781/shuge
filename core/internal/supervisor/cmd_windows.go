//go:build windows

package supervisor

import (
	"os/exec"
	"syscall"
)

// detachedProcess = DETACHED_PROCESS：子进程**既不继承、也不新建控制台**。
//
// 演进（2026-09-11 辞玖定案）：
//
//	· 旧做法 CREATE_NO_WINDOW 仍会给子进程**新建一个隐藏控制台**（各带一个 conhost，
//	  实测每个约 6MB），而且当系统默认终端是 Windows Terminal 时会闪窗口；
//	· 现在插件的 stdout/stderr 由平台统一收走（internal/logforward → 日志插件 plugin.log），**子进程根本不需要控制台**；
//	· 改用 DETACHED_PROCESS：既不弹窗，也不再产生隐藏控制台——"不弹窗"从此是结构性保证。
const detachedProcess = 0x00000008

// prepareCmd 设置子进程创建标志（幂等，可重复调用）。
func prepareCmd(cmd *exec.Cmd) {
	if cmd == nil {
		return
	}
	if cmd.SysProcAttr == nil {
		cmd.SysProcAttr = &syscall.SysProcAttr{}
	}
	cmd.SysProcAttr.HideWindow = true
	cmd.SysProcAttr.CreationFlags |= detachedProcess
}
