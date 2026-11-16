// 本文件：文件上传落 Vault —— 控制台"添加文件"的网页版做法（参考网页版 AI 的交互）。
//
// 为什么不用浏览器路径：网页 JS 拿不到本地文件绝对路径。网页版 AI 的做法是
// **把文件内容上传给服务器**，由服务器落盘。这里落进 Vault 的「附件/导入/」，
// 包内引用 Vault 相对路径 —— 既符合"Vault 是唯一真相源"（P9），也避免包引用机器外部路径。
//
// 与 /api/fs/pick（原生对话框取绝对路径）的分工：
//
//	upload  = 默认路径：同页选文件、复制进 Vault、不弹终端
//	fs/pick = 备选路径：保留文件在原位置（需有桌面会话）
package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

const maxUploadBytes = 32 << 20 // 单次上传上限 32MB（防误传大文件炸内存）

var unsafeNameChars = regexp.MustCompile(`[\\/:*?"<>|\x00-\x1f]+`)

// safeUploadName 清洗文件名（去路径成分与非法字符），返回空则用时间戳兜底
func safeUploadName(name string) string {
	n := filepath.Base(strings.TrimSpace(name))
	n = unsafeNameChars.ReplaceAllString(n, "_")
	n = strings.Trim(n, ". ")
	if n == "" {
		n = fmt.Sprintf("upload-%d", time.Now().UnixMilli())
	}
	if len(n) > 120 {
		ext := filepath.Ext(n)
		n = n[:120-len(ext)] + ext
	}
	return n
}

// handleVaultUpload POST /api/vault/upload —— multipart 上传，落 Vault「附件/导入/」
//
// 表单字段：file（可多个）。返回 {ok:true, files:[{name, rel, bytes}]}
func (s *Server) handleVaultUpload(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if s.vault == nil {
		writeJSON(w, map[string]any{"ok": false, "error": "vault 未挂载"})
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxUploadBytes)
	if err := r.ParseMultipartForm(8 << 20); err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "解析上传失败: " + err.Error()})
		return
	}
	fhs := r.MultipartForm.File["file"]
	if len(fhs) == 0 {
		writeJSON(w, map[string]any{"ok": false, "error": "没有收到文件（字段名应为 file）"})
		return
	}
	dirRel := filepath.Join("附件", "导入")
	dirAbs := filepath.Join(s.vault.Root(), dirRel)
	if err := os.MkdirAll(dirAbs, 0o755); err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "建目录失败: " + err.Error()})
		return
	}
	type item struct {
		Name  string `json:"name"`
		Rel   string `json:"rel"`
		Bytes int64  `json:"bytes"`
	}
	out := []item{}
	for _, fh := range fhs {
		src, err := fh.Open()
		if err != nil {
			continue
		}
		name := safeUploadName(fh.Filename)
		abs := filepath.Join(dirAbs, name)
		if _, err := os.Stat(abs); err == nil { // 重名 → 加时间戳前缀，绝不覆盖
			name = fmt.Sprintf("%d-%s", time.Now().UnixMilli(), name)
			abs = filepath.Join(dirAbs, name)
		}
		dst, err := os.Create(abs)
		if err != nil {
			src.Close()
			continue
		}
		n, err := io.Copy(dst, src)
		dst.Close()
		src.Close()
		if err != nil {
			os.Remove(abs)
			continue
		}
		out = append(out, item{Name: name, Rel: filepath.ToSlash(filepath.Join(dirRel, name)), Bytes: n})
	}
	if len(out) == 0 {
		writeJSON(w, map[string]any{"ok": false, "error": "写入 Vault 失败（0 个文件落盘）"})
		return
	}
	if b, err := json.Marshal(out); err == nil {
		s.publishNote("note.uploaded", map[string]any{"files": json.RawMessage(b), "dir": filepath.ToSlash(dirRel)})
	}
	writeJSON(w, map[string]any{"ok": true, "files": out, "dir": filepath.ToSlash(dirRel)})
}
