// 本文件：owner 登记（data/owner.json）——记录"这次 core 是谁拉起来的"。
//
// 为什么需要（辞玖 2026-09-12 待办："对于进程等问题做一个详细的解释"的延伸）：
//
//	此前 core 经常是某次工具调用里 detached 拉起的，父链随之消失（错误库 #44/#45 记录的
//	"无主进程"形态）。出问题时无法回答最基本的三问：**谁起的、什么时候起的、怎么起的**。
//	现在：看门狗拉起 core 时用环境变量带上身份，core 启动第一件事就把它写进 data/owner.json，
//	同时进账本（platform.core.started.launched_by）——事后翻账本或看文件都能立刻对上人。
package main

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"time"
)

// LauncherEnv 看门狗/其它启动方通过该环境变量告知"启动者"身份
const LauncherEnv = "COREHOST_LAUNCHED_BY"

// ownerInfo data/owner.json 的内容
type ownerInfo struct {
	PID        int      `json:"pid"`
	StartedAt  string   `json:"started_at"`
	StoppedAt  string   `json:"stopped_at,omitempty"`
	LaunchedBy string   `json:"launched_by"`
	Exe        string   `json:"exe"`
	Cwd        string   `json:"cwd"`
	Args       []string `json:"args"`
}

// OwnerFilePath owner 登记文件
func OwnerFilePath() string { return filepath.Join("data", "owner.json") }

// launchedByIdentity 启动者身份；没有任何标注就说明是"人手直接点 core.exe"
func launchedByIdentity() string {
	if v := os.Getenv(LauncherEnv); v != "" {
		return v
	}
	return "手动/未知（core.exe 直接启动）"
}

func currentOwner() *ownerInfo {
	exe, _ := os.Executable()
	cwd, _ := os.Getwd()
	return &ownerInfo{
		PID:        os.Getpid(),
		StartedAt:  time.Now().Format(time.RFC3339),
		LaunchedBy: launchedByIdentity(),
		Exe:        exe,
		Cwd:        cwd,
		Args:       os.Args[1:],
	}
}

// writeOwnerFile 写 owner 登记（失败只记日志，不影响启动）
func writeOwnerFile(log *slog.Logger, o *ownerInfo) {
	if err := os.MkdirAll("data", 0o755); err != nil {
		log.Warn("owner 登记失败：建目录失败", "err", err)
		return
	}
	b, err := json.MarshalIndent(o, "", "  ")
	if err != nil {
		log.Warn("owner 登记失败：序列化失败", "err", err)
		return
	}
	if err := os.WriteFile(OwnerFilePath(), append(b, '\n'), 0o644); err != nil {
		log.Warn("owner 登记失败：写文件失败", "err", err)
		return
	}
	log.Info("owner 登记已写", "file", OwnerFilePath(), "pid", o.PID, "launched_by", o.LaunchedBy)
}

// markOwnerStopped 退出时补一笔停止时间（保留"上次是谁起的、跑了多久"的信息）
func markOwnerStopped() {
	path := OwnerFilePath()
	b, err := os.ReadFile(path)
	if err != nil {
		return
	}
	var o ownerInfo
	if err := json.Unmarshal(b, &o); err != nil || o.PID != os.Getpid() {
		return // 不是自己写的（已被新实例接管）→ 不动
	}
	o.StoppedAt = time.Now().Format(time.RFC3339)
	if out, err := json.MarshalIndent(&o, "", "  "); err == nil {
		_ = os.WriteFile(path, append(out, '\n'), 0o644)
	}
}
