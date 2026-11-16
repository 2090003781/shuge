// 本文件：把"机器上的绝对路径"纳入库（辞玖 2026-09-24 报："注入包那边的文件选择
// 系统路径没有自动更新"）。
//
// 背景：注入包清单支持两种条目 —— Vault 相对路径（推荐）与机器绝对路径（原生对话框
// /api/fs/pick 或手填的降级入口）。前者组包时**从库/镜像取正文**，文件一改就跟着更新；
// 后者是插件直接 open(那个绝对路径) 读磁盘，所以：文件改了库不知道、文件挪了就读不到、
// 换台机器直接断链。
//
// 这里给一个"纳入库"的口子：把外部文件**复制进 Vault 的「附件/导入/」**（重名不覆盖），
// 返回 Vault 相对路径 —— 前端把清单条目换成相对路径，从此走镜像/库那条自动更新的路。
package api

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const maxAdoptBytes = 256 << 20 // 纳入库的单文件上限 256MB（防误点大文件）

// handleVaultAdopt POST /api/vault/adopt {path}
//
// 返回 {ok:true, rel, name, bytes, copied}：
//   · 文件本来就在 Vault 里 → copied=false，rel 就是它的 Vault 相对路径
//   · 在机器别处 → 复制进「附件/导入/」（重名加时间戳前缀，绝不覆盖），copied=true
func (s *Server) handleVaultAdopt(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	if s.vault == nil {
		writeJSON(w, map[string]any{"ok": false, "error": "vault 未挂载"})
		return
	}
	var body struct {
		Path string `json:"path"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&body); err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "解析请求失败: " + err.Error()})
		return
	}
	raw := strings.Trim(strings.TrimSpace(body.Path), `"`)
	if raw == "" {
		writeJSON(w, map[string]any{"ok": false, "error": "缺 path"})
		return
	}
	abs, err := filepath.Abs(raw)
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "路径无效: " + err.Error()})
		return
	}
	st, err := os.Stat(abs)
	if err != nil || st.IsDir() {
		writeJSON(w, map[string]any{"ok": false, "error": "不是一个存在的文件：" + raw})
		return
	}
	root, err := filepath.Abs(s.vault.Root())
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "vault 根无效: " + err.Error()})
		return
	}
	// 已经在 Vault 里：直接给相对路径，不复制
	if rel, err := filepath.Rel(root, abs); err == nil && !strings.HasPrefix(rel, "..") {
		writeJSON(w, map[string]any{"ok": true, "rel": filepath.ToSlash(rel),
			"name": filepath.Base(abs), "bytes": st.Size(), "copied": false})
		return
	}
	if st.Size() > maxAdoptBytes {
		writeJSON(w, map[string]any{"ok": false,
			"error": fmt.Sprintf("文件太大（%.1f MB > 256MB），没有纳入", float64(st.Size())/1048576.0)})
		return
	}
	dirRel := filepath.Join("附件", "导入")
	dirAbs := filepath.Join(root, dirRel)
	if err := os.MkdirAll(dirAbs, 0o755); err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "建目录失败: " + err.Error()})
		return
	}
	name := safeUploadName(filepath.Base(abs))
	dstAbs := filepath.Join(dirAbs, name)
	if _, err := os.Stat(dstAbs); err == nil { // 重名 → 时间戳前缀，绝不覆盖
		name = fmt.Sprintf("%d-%s", time.Now().UnixMilli(), name)
		dstAbs = filepath.Join(dirAbs, name)
	}
	src, err := os.Open(abs)
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "读不到源文件: " + err.Error()})
		return
	}
	defer src.Close()
	dst, err := os.Create(dstAbs)
	if err != nil {
		writeJSON(w, map[string]any{"ok": false, "error": "写不进 Vault: " + err.Error()})
		return
	}
	n, err := io.Copy(dst, src)
	dst.Close()
	if err != nil {
		os.Remove(dstAbs)
		writeJSON(w, map[string]any{"ok": false, "error": "复制失败: " + err.Error()})
		return
	}
	rel := filepath.ToSlash(filepath.Join(dirRel, name))
	if b, err := json.Marshal([]map[string]any{{"name": name, "rel": rel, "bytes": n}}); err == nil {
		s.publishNote("note.uploaded", map[string]any{"files": json.RawMessage(b),
			"dir": filepath.ToSlash(dirRel), "via": "adopt", "from": abs})
	}
	writeJSON(w, map[string]any{"ok": true, "rel": rel, "name": name, "bytes": n,
		"copied": true, "from": abs})
}
