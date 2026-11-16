//go:build windows

package supervisor

import (
	"fmt"
	"os"
	"syscall"
	"unsafe"
)

// jobObject Windows 作业对象：把插件进程都放进去，**core 一死（句柄关闭）→ 整棵树立刻被回收**。
//
// 为什么需要（错误库 #45 的根治）：
//
//	Windows 下父进程死亡**不会**终止子进程，于是 core 被强杀（job 回收 / taskkill /F / 崩溃）
//	时会留下一堆孤儿插件——实测曾堆积 6 个：多个 clock 同时报时、多个 context-pack 互相覆盖写包。
//	插件侧现有"父进程看护"（5 秒内自退）只是**兜底**：要等、且靠插件自觉。
//	Job Object 是**内核级保证**：`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 一置，
//	持有句柄的进程（core）一消失，作业内全部进程立即终止。
type jobObject struct {
	handle syscall.Handle
}

var (
	kernel32                     = syscall.NewLazyDLL("kernel32.dll")
	procCreateJobObject          = kernel32.NewProc("CreateJobObjectW")
	procSetInformationJobObject  = kernel32.NewProc("SetInformationJobObject")
	procAssignProcessToJobObject = kernel32.NewProc("AssignProcessToJobObject")
	procOpenProcess              = kernel32.NewProc("OpenProcess")
)

const (
	jobObjectExtendedLimitInformation = 9
	jobObjectLimitKillOnJobClose      = 0x00002000
	processSetQuota                   = 0x0100
	processTerminate                  = 0x0001
)

type jobObjectBasicLimitInformation struct {
	PerProcessUserTimeLimit int64
	PerJobUserTimeLimit     int64
	LimitFlags              uint32
	MinimumWorkingSetSize   uintptr
	MaximumWorkingSetSize   uintptr
	ActiveProcessLimit      uint32
	Affinity                uintptr
	PriorityClass           uint32
	SchedulingClass         uint32
}

type ioCounters struct {
	ReadOperationCount  uint64
	WriteOperationCount uint64
	OtherOperationCount uint64
	ReadTransferCount   uint64
	WriteTransferCount  uint64
	OtherTransferCount  uint64
}

type jobObjectExtendedLimitInfo struct {
	BasicLimitInformation jobObjectBasicLimitInformation
	IoInfo                ioCounters
	ProcessMemoryLimit    uintptr
	JobMemoryLimit        uintptr
	PeakProcessMemoryUsed uintptr
	PeakJobMemoryUsed     uintptr
}

// newJobObject 创建作业并设置"句柄关闭即杀光"（KILL_ON_JOB_CLOSE）
func newJobObject() (*jobObject, error) {
	h, _, err := procCreateJobObject.Call(0, 0)
	if h == 0 {
		return nil, fmt.Errorf("CreateJobObject: %v", err)
	}
	info := jobObjectExtendedLimitInfo{}
	info.BasicLimitInformation.LimitFlags = jobObjectLimitKillOnJobClose
	r, _, serr := procSetInformationJobObject.Call(h, jobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)), unsafe.Sizeof(info))
	if r == 0 {
		_ = syscall.CloseHandle(syscall.Handle(h))
		return nil, fmt.Errorf("SetInformationJobObject: %v", serr)
	}
	return &jobObject{handle: syscall.Handle(h)}, nil
}

// assign 把已启动的进程放进作业。
// 失败不致命：可能该进程已在另一个不允许嵌套的作业里（Win8+ 支持嵌套作业）——
// 此时退化回插件侧"父进程看护"兜底，并留下告警。
func (j *jobObject) assign(p *os.Process) error {
	if j == nil || j.handle == 0 || p == nil {
		return fmt.Errorf("作业对象不可用")
	}
	ph, _, err := procOpenProcess.Call(processSetQuota|processTerminate, 0, uintptr(p.Pid))
	if ph == 0 {
		return fmt.Errorf("OpenProcess: %v", err)
	}
	defer func() { _ = syscall.CloseHandle(syscall.Handle(ph)) }()
	r, _, aerr := procAssignProcessToJobObject.Call(uintptr(j.handle), ph)
	if r == 0 {
		return fmt.Errorf("AssignProcessToJobObject: %v", aerr)
	}
	return nil
}

// Close 关闭作业句柄 —— 作业内仍存活的进程会被内核立即终止（这就是"整树回收"）
func (j *jobObject) Close() error {
	if j == nil || j.handle == 0 {
		return nil
	}
	err := syscall.CloseHandle(j.handle)
	j.handle = 0
	return err
}
