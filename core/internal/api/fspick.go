// 本文件：本机文件对话框（原生「打开文件」）——给控制台提供"选外部文件"的能力。
//
// 为什么需要它：网页（浏览器沙箱）拿不到本地文件绝对路径（<input type=file> 只给文件名+内容，
// 拖入也只有内容），而枢阁 core 是**本机后台服务**，跑在浏览器沙箱之外，可以弹 Windows 原生
// 对话框，把选中的绝对路径回给前端。
//
// 实现：调用 scripts/pick_files.ps1（纯 ASCII，避免 PS 5.1 按 ANSI 解析 .ps1 的坑；
// 结果写临时文件再读，绕开 stdout 编码问题）。
//
// 前提与降级：调用方进程需在**有桌面的会话**里（实测 core.exe 为 SessionId 1，可用）。
// 若跑在服务/无桌面会话，ShowDialog 弹不出来 → 脚本返回 ok:false，前端应降级为手填路径。
package api

import (
	"encoding/json"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// fsPickResult 脚本回执（与 scripts/pick_files.ps1 的 JSON 对应）
type fsPickResult struct {
	OK    bool     `json:"ok"`
	Files []string `json:"files"`
	Error string   `json:"error"`
}

// pickFilesNative 弹本机文件对话框（Windows 走 scripts/pick_files.ps1；其他平台降级）
func pickFilesNative(multi bool, initialDir, title string) (string, error) {
	if runtime.GOOS != "windows" {
		return "", &os.PathError{Op: "pick", Path: "native-dialog", Err: os.ErrInvalid}
	}
	ps, err := exec.LookPath("powershell.exe")
	if err != nil {
		ps, err = exec.LookPath("powershell")
		if err != nil {
			return "", err
		}
	}
	tmp, err := os.CreateTemp("", "sb-pick-*.json")
	if err != nil {
		return "", err
	}
	tmpPath := tmp.Name()
	tmp.Close()
	defer os.Remove(tmpPath)

	args := []string{"-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
		filepath.Join("scripts", "pick_files.ps1"), "-OutFile", tmpPath}
	if multi {
		args = append(args, "-Multi")
	}
	if initialDir != "" {
		args = append(args, "-InitialDir", initialDir)
	}
	if title != "" {
		args = append(args, "-Filter", title) // 保留参数位（脚本里 Filter 是文件类型过滤）
	}

	cmd := exec.Command(ps, args...)
	hideWindow(cmd) // 【关键】不设的话 Windows 会给 powershell 一个控制台窗口（黑框闪出）
	_ = cmd.Run()   // 脚本退出码不判定（取消也算 0）；以结果文件为准

	data, err := os.ReadFile(tmpPath)
	if err != nil || len(data) == 0 {
		return "", &os.PathError{Op: "pick", Path: tmpPath, Err: os.ErrNotExist}
	}
	var res fsPickResult
	if err := json.Unmarshal(data, &res); err != nil {
		return "", err
	}
	if !res.OK {
		if res.Error == "" {
			res.Error = "canceled"
		}
		return "", &os.PathError{Op: "pick", Path: "dialog", Err: os.ErrInvalid}
	}
	return strings.Join(res.Files, "\n"), nil
}

// handleFsPick GET/POST /api/fs/pick —— 弹原生文件对话框，返回选中的绝对路径
//
// 参数：multi=1 多选（默认单选）；dir=<初始目录>；title=<对话框标题>
// 返回：{ok:true, files:[...], count:N} 或 {ok:false, error:"canceled"|"..."}
func (s *Server) handleFsPick(w http.ResponseWriter, r *http.Request) {
	if runtime.GOOS != "windows" {
		writeJSON(w, map[string]any{"ok": false, "error": "本机文件对话框只在 Windows 上可用",
			"hint": "请手动填写文件绝对路径"})
		return
	}
	multi := r.URL.Query().Get("multi") == "1" || r.URL.Query().Get("multi") == "true"
	initialDir := r.URL.Query().Get("dir")
	title := r.URL.Query().Get("title")
	if initialDir == "" && s.vault != nil {
		initialDir = s.vault.Root() // 默认从 Vault 开始（也方便挑 Vault 内的文件）
	}

	joined, err := pickFilesNative(multi, initialDir, title)
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": err.Error(),
			"hint": "对话框不可用（可能无桌面会话）或已取消，可手动填写绝对路径"})
		return
	}
	files := []string{}
	for _, f := range strings.Split(joined, "\n") {
		if f = strings.TrimSpace(f); f != "" {
			files = append(files, f)
		}
	}
	writeJSON(w, map[string]any{"ok": true, "files": files, "count": len(files)})
}
