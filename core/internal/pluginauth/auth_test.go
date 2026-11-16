// 本文件：插件面/上报面鉴权的回归测试（2026-09-12 补测试网）。
//
// 钉住的契约：token 文件生成一次即稳定复用（重启不换、插件不用重新配）、
// 头解析兼容 X-Plugin-Token 与 Authorization: Bearer、常量时间比较前的空值语义。
package pluginauth

import (
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
)

// 首次生成 48 位 hex；再次调用复用同一个（重启不该换 token）
func TestLoadOrCreateIsStable(t *testing.T) {
	dir := t.TempDir()
	tok1, err := LoadOrCreate(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(tok1) != 48 {
		t.Fatalf("token 应为 48 位十六进制，实际 %d 位", len(tok1))
	}
	tok2, err := LoadOrCreate(dir)
	if err != nil {
		t.Fatal(err)
	}
	if tok1 != tok2 {
		t.Fatalf("token 应稳定复用：%s vs %s", tok1, tok2)
	}
	// 文件应存在且非空（权限 0600 在 Windows 上意义有限，这里只验存在与非空）
	if b, err := os.ReadFile(dir + "/" + TokenFile); err != nil || len(b) == 0 {
		t.Fatalf("token 文件应写入：err=%v len=%d", err, len(b))
	}
}

// 鉴权判定：无头 → 拒；错 token → 拒；对 token → 过；Bearer 形式 → 过
func TestRequestValidation(t *testing.T) {
	const want = "0123456789abcdef0123456789abcdef0123456789abcdef"

	req := func(h func(*http.Request)) *http.Request {
		r := httptest.NewRequest(http.MethodPost, "/api/bridge/plugins/register", nil)
		if h != nil {
			h(r)
		}
		return r
	}

	if Valid(req(nil), want) {
		t.Fatal("没有 token 应判不通过")
	}
	if Valid(req(func(r *http.Request) { r.Header.Set(Header, "wrong") }), want) {
		t.Fatal("错误 token 应判不通过")
	}
	if !Valid(req(func(r *http.Request) { r.Header.Set(Header, want) }), want) {
		t.Fatal("正确 token 应通过")
	}
	if !Valid(req(func(r *http.Request) { r.Header.Set("Authorization", "Bearer "+want) }), want) {
		t.Fatal("Bearer 形式应通过")
	}
	// want 为空 = 未启用鉴权（单机调试）→ 一律放行
	if !Valid(req(nil), "") {
		t.Fatal("未配置 token 时应放行（单机调试模式）")
	}
}
