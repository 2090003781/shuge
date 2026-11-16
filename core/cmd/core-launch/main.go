// 枢阁 · 启动器（`启动枢阁.exe`）—— 包**根目录**那个显眼的启动器。
//
// 为什么要有它（2026-10-07 辞玖「我点这个启动咋没反应？」）：
//   · 原来根目录放的是 `.lnk` 快捷方式：它存的是**打包时那台机器的绝对路径**，
//     别人解压到别处双击 —— 要么指向不存在的位置（没反应），要么在压缩包里点（临时目录，一样没反应）；
//   · 快捷方式还容易被"解压到哪"这件事搞坏。一个真 exe 没这些毛病：它按**自己所在位置**推。
//
// 它做什么（只做一件事，出了错能看见）：
//   1. 找平台根：自己旁边有没有 `core\bin\core-host.exe`；没有就往下找一层
//      （有些人会把包再套一层目录解压）；
//   2. 找到就**用 `-open-web` 拉起宿主**（宿主负责按序把库与内核拉起来，并打开控制台页面）；
//      已经在跑的话宿主自己会直接开页面（主程序 2026-10-07 改的）；
//   3. 找不到就在**当前目录**写一个 `启动失败-看这个.txt`（写明它找了哪些位置），
//      再用系统弹窗说一声 —— 绝不"点了没反应、什么也不说"。
//
// 编译：`scripts\build_exe.ps1 -Target launch`（GUI 子系统，不弹黑窗）。
package main

import (
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"
	"unsafe"
)

const (
	detachedProcess    = 0x00000008 // DETACHED_PROCESS：子进程既不继承也不新建控制台
	createNewProcGroup = 0x00000200
)

// 候选：平台根（相对本 exe 所在目录）
func candidates(self string) []string {
	base := filepath.Dir(self)
	return []string{
		filepath.Join(base, "core"),               // 包根/启动枢阁.exe 与 包根/core/...
		filepath.Join(base, "..", "core"),         // 被套了一层目录解压
		filepath.Join(base, "..", "枢阁-程序包", "core"),
	}
}

func findHost(self string) (string, []string) {
	tried := []string{}
	for _, c := range candidates(self) {
		host := filepath.Join(c, "bin", "core-host.exe")
		tried = append(tried, host)
		if st, err := os.Stat(host); err == nil && !st.IsDir() {
			return host, tried
		}
	}
	return "", tried
}

func messageBox(title, text string) {
	dll := syscall.NewLazyDLL("user32.dll")
	proc := dll.NewProc("MessageBoxW")
	t, _ := syscall.UTF16PtrFromString(text)
	c, _ := syscall.UTF16PtrFromString(title)
	proc.Call(0, uintptr(unsafe.Pointer(t)), uintptr(unsafe.Pointer(c)), 0x00000040) // MB_ICONINFORMATION
}

func main() {
	self, err := os.Executable()
	if err != nil {
		messageBox("枢阁 · 启动器", "拿不到自身路径："+err.Error())
		return
	}
	host, tried := findHost(self)
	if host == "" {
		txt := "没找到宿主（core-host.exe）。\r\n\r\n它找了这些位置：\r\n  " +
			joinLines(tried) +
			"\r\n\r\n请确认压缩包**完整解压**后再双击（不要在压缩包里点），" +
			"并且 `core` 目录和本启动器在同一层。\r\n"
		_ = os.WriteFile(filepath.Join(filepath.Dir(self), "启动失败-看这个.txt"), []byte(txt), 0o644)
		messageBox("枢阁 · 启动器", txt)
		return
	}
	cmd := exec.Command(host, "-open-web", "-by=启动器（启动枢阁.exe）")
	cmd.Dir = filepath.Dir(filepath.Dir(host)) // 平台根
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true, CreationFlags: detachedProcess | createNewProcGroup}
	if err := cmd.Start(); err != nil {
		txt := "拉起宿主失败：" + err.Error() + "\r\n\r\n宿主位置：\r\n  " + host + "\r\n"
		_ = os.WriteFile(filepath.Join(filepath.Dir(self), "启动失败-看这个.txt"), []byte(txt), 0o644)
		messageBox("枢阁 · 启动器", txt)
		return
	}
	_ = cmd.Process.Release()
	// 给宿主一点时间，让它把页面打开（宿主自己开浏览器；这里不做别的）
	time.Sleep(300 * time.Millisecond)
}

func joinLines(xs []string) string {
	out := ""
	for i, x := range xs {
		if i > 0 {
			out += "\r\n  "
		}
		out += x
	}
	return out
}
