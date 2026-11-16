// Package pluginauth 插件面鉴权：本机进程也不能随便冒充插件。
//
// 为什么需要（2026-09-12 检修 P2）：
//
//	插件协议面（register / poll / result）此前**完全无鉴权**——本机任何进程都能自称
//	plugin.xxx 注册身份、领走平台发给别的插件的调用、或回伪造结果。功能上一直没出事，
//	但这是"平台完整性"的窟窿：办公室模型里，工位可以随便挂牌、还能替别人签收快递。
//
// 做法（插件协议设计 二.7 的落地）：
//
//	· 内核启动时生成/复用一个 token，存 data/plugins/token.json（私密、不入库）；
//	· spawn 插件时经环境变量 PLUGIN_TOKEN 注入（只有内核的子进程拿得到）；
//	· 插件面端点要求请求头 X-Plugin-Token（或 Authorization: Bearer xxx）。
//
// 范围（有意为之，见文档）：**只管插件面**。`/api/bridge/send`（agent 上报面）暂不强制——
//
//	reasonix/hook 与 DSH 插件是"平台外的接入方"，它们的能力拿不到内核环境变量，
//	贸然强制会把正在跑的接入链路打断；这一条留作后续（配 token 文件读取）。
package pluginauth

import (
	"crypto/rand"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// TokenFile 相对 core 根目录的 token 文件
const TokenFile = "data/plugins/token.json"

// Header 插件面鉴权头
const Header = "X-Plugin-Token"

// tokenDoc token.json 的内容
type tokenDoc struct {
	Token     string `json:"token"`
	CreatedAt string `json:"created_at"`
	Note      string `json:"note,omitempty"`
}

// LoadOrCreate 读 token；没有就生成一个（0600，只本机可读）
func LoadOrCreate(coreDir string) (string, error) {
	path := filepath.Join(coreDir, TokenFile)
	if b, err := os.ReadFile(path); err == nil {
		var d tokenDoc
		if json.Unmarshal(b, &d) == nil && len(d.Token) >= 16 {
			if d.CreatedAt == "" { // 老文件没有创建时间：补上，方便日后判断 token 年龄
				d.CreatedAt = time.Now().Format(time.RFC3339)
				if out, err := json.MarshalIndent(&d, "", "  "); err == nil {
					_ = os.WriteFile(path, append(out, '\n'), 0o600)
				}
			}
			return d.Token, nil
		}
	}
	buf := make([]byte, 24)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	tok := hex.EncodeToString(buf)
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return "", err
	}
	out, _ := json.MarshalIndent(tokenDoc{
		Token:     tok,
		CreatedAt: time.Now().Format(time.RFC3339),
		Note:      "插件面鉴权 token：内核 spawn 插件时经环境变量 PLUGIN_TOKEN 注入；勿外传、勿入库",
	}, "", "  ")
	if err := os.WriteFile(path, append(out, '\n'), 0o600); err != nil {
		return "", err
	}
	return tok, nil
}

// FromRequest 取请求里的 token（头 X-Plugin-Token 优先，兼容 Authorization: Bearer）
func FromRequest(r *http.Request) string {
	if v := r.Header.Get(Header); v != "" {
		return strings.TrimSpace(v)
	}
	if v := r.Header.Get("Authorization"); strings.HasPrefix(v, "Bearer ") {
		return strings.TrimSpace(strings.TrimPrefix(v, "Bearer "))
	}
	return ""
}

// Valid 常量时间比较（防时序侧信道；token 是随机 48 位十六进制）
func Valid(r *http.Request, want string) bool {
	if want == "" {
		return true // 未启用鉴权（例如测试/单机调试）
	}
	got := FromRequest(r)
	if got == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(want)) == 1
}
